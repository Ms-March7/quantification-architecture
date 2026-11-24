from datetime import datetime

import pytest

from app.fundamental_report import DIMENSIONS, render_report


def at(day):
    return datetime.fromisoformat(f"2026-05-{day:02d}T12:00:00+08:00")


def material(source_id, text, dimension="supply", day=1, **changes):
    record = dict(id=source_id, title=source_id, scope="example source", dimension=dimension,
                  excerpt=text, url=f"https://example.org/{source_id}", available_at=at(day))
    return {**record, **changes}


def branch(branch_id, start, end, *links, **changes):
    record = dict(id=branch_id, from_node=start, to_node=end, condition="合约尚有效",
                  next_check="下一次客户披露", falsifier="订单取消", evidence=list(links))
    return {**record, **changes}


def link(source, quote, stance="supports", **changes):
    return dict(source_id=source, quote=quote, stance=stance, **changes)


def report(branches, sources, day=15, prev=None, **settings):
    return render_report(
        dict(target="示例", question="供货能否改善？", branches=branches, **settings),
        sources, [], at(day), at(prev) if prev else None,
    )


def test_future_or_unobserved_text_does_not_leak():
    sources = [
        material("fir", "first news"),
        material("revision", "restricted until day 20", day=5, observed_at=at(20)),
        material("bad", "INVALID DATE", available_at="2026-05-10"),
    ]
    branches = [
        branch("live", "供给", "交付", link("fir", "first news"), link("revision", "restricted"),
               link("fir", "DO NOT PRINT")),
        branch("future", "FUTURE BRANCH", "远期结论", recorded_at=at(20)),
    ]
    text = report(branches, sources)
    assert all(s not in text for s in ("FUTURE BRANCH", "restricted", "INVALID DATE", "DO NOT PRINT"))
    assert "first news" in text
    assert "1 条输入" in text
    assert all(label in text for label, _ in DIMENSIONS.values())


def test_delta_distinguishes_branch_evidence_downstream_and_cycles():
    sources = [material("early", "产能紧张"), material("late", "排产推迟", day=12)]
    branches = [
        branch("capacity", "供给", "交付", link("early", "产能紧张"),
               link("late", "排产推迟", "challenges")),
        branch("sales", "交付", "收入"), branch("cycle", "收入", "交付"),
        branch("cash", "利率", "现金"),
    ]
    text = report(branches, sources, prev=10)
    delta = text.split("的变化", 1)[1].split("## 推理网络", 1)[0]
    assert "直接证据更新：供给 → 交付（有支持材料 → 兼有支持与质疑）" in delta
    assert "下游待复核：交付 → 收入" in delta
    assert "下游待复核：收入 → 交付" in delta
    assert "利率" not in delta
    assert text.count("直接证据更新：") == 1
    assert "flowchart LR" in text and "订单取消" in text


def test_quote_must_match_current_visible_revision_even_if_source_id_is_reused():
    sources = [
        material("s", "原版确认交货"),
        material("s", "更正：交货仍待确认", day=12),
    ]
    b = branch("b", "供给", "交货", link("s", "原版确认交货"))
    assert "原版确认交货" in report([b], sources, day=8)
    text = report([b], sources, prev=8)
    assert "原版确认交货" not in text
    assert "有支持材料 → 待证" in text


def test_duplicate_url_and_quote_dont_create_independent_support():
    url = "https://example.org/release"
    sources = [material("one", "收入上调", url=url), material("two", "收入上调", url=url)]
    b = branch("b", "公告", "收入", link("one", "收入上调"), link("two", "收入上调"))
    assert report([b], sources).count("支持依据：") == 1


def test_ambiguous_same_time_revision_is_not_picked_by_list_order():
    a = material("price", "价格持平")
    b = material("price", "价格上涨")
    for items in ([a, b], [b, a]):
        text = report([], items)
        assert "互相冲突的来源版本" in text
        assert "价格持平" not in text and "价格上涨" not in text
    text = report([], [a, b, material("price", "修订价格已核实", day=14)])
    assert "修订价格已核实" in text and "互相冲突的来源版本" not in text


def test_separate_prior_snapshot_handles_evidence_withdrawal():
    b = branch("release", "供应", "预期", link("source", "当时已确认"))
    previous = material("source", "当时已确认")
    current = material("source", "更正：结论尚待核实", day=12)
    text = render_report(
        {"branches": [b]}, [current], [], at(15), at(10), previous_materials=[previous]
    )
    assert "有支持材料 → 待证" in text
    assert "已撤回相应依据" in text
    assert "当时已确认" not in text


def test_staleness_uses_publication_date_and_relation_recording_is_point_in_time():
    m = material("price", "旧行情", dimension="market", day=10, published_at=at(2))
    b = branch("b", "行情", "定价", link("price", "旧行情", recorded_at=at(15)))
    text = report([b], [m], day=14, stale_after_days={"market": 5})
    assert "超过核验周期（5 天）" in text
    assert "支持依据：" not in text
    assert "所引材料均已超出核验周期" in report([b], [m])


def test_empty_scope_and_invalid_horizons_or_previous():
    text = render_report({}, [], ["来源不可用"], at(15))
    assert "截至该时点尚无可核验材料" in text and "来源不可用" in text
    assert text.count("截至此时无可用材料") == 6
    with pytest.raises(ValueError):
        report([], [], prev=16)
    with pytest.raises(ValueError):
        report([], [], stale_after_days={"market": 0})
