import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from pydantic import ValidationError

from app.research_review import ResearchCase, parse_timestamp, render, snapshot


BACKEND = Path(__file__).resolve().parents[1]
EXAMPLE = BACKEND.parent / "config" / "examples" / "margin_review.yaml"


@pytest.fixture
def data():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def run_cli(timestamp):
    return subprocess.run(
        [sys.executable, "-m", "app.research_review", str(EXAMPLE), "--as-of", timestamp],
        cwd=BACKEND, capture_output=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_replay_at_two_cutoffs():
    early = run_cli("2026-04-29T10:00:00Z")
    late = run_cli("2026-05-15T11:00:00+08:00")
    assert early.returncode == late.returncode == 0
    assert "部分合约客户已按新价结算。" in early.stdout
    assert "示例下游采购问答" not in early.stdout
    assert "晶圆采购成本调整" not in early.stdout
    assert "n_foundry" not in early.stdout
    assert "n_restocking" not in early.stdout
    assert "待证" in early.stdout
    assert "兼有支持与质疑" in late.stdout
    assert "n_restocking -->|支撑路径" in late.stdout
    assert "n_restocking -->|制约路径" in late.stdout
    assert "新采购晶圆的单位成本上升。" in late.stdout
    assert "https://example.org/company-cost" in late.stdout


def test_source_can_be_public_before_it_has_an_analyst_interpretation(data):
    case = ResearchCase.model_validate(data)
    cutoff = parse_timestamp("2026-05-11T12:00:00+08:00")
    visible = snapshot(case, cutoff)
    assert "buyer_note" in {s.id for s in visible.sources}
    memo = render(case, cutoff)
    assert "下一批订单仍按原合约价执行。" not in memo
    assert "至少一名客户" not in memo
    assert "客户提前备货" not in memo


@pytest.mark.parametrize("key,value", [
    ("quote", "原文没有这句话。"),
    ("source_id", "missing"),
    ("edge_id", "absent"),
    ("recorded_at", "2026-04-22T14:00:00+08:00"),
])
def test_reject_untraceable_or_premature_evidence(data, key, value):
    data["evidence"][0][key] = value
    with pytest.raises(ValidationError):
        ResearchCase.model_validate(data)


def test_reject_wrong_source_even_if_quote_exists_elsewhere(data):
    data["evidence"][-1]["source_id"] = "industry_note"
    with pytest.raises(ValidationError, match="逐字匹配"):
        ResearchCase.model_validate(data)


def test_reject_naive_times_and_unavailable_question(data):
    result = run_cli("2026-04-29T18:00:00")
    assert result.returncode == 2
    assert "UTC" in result.stderr
    assert not result.stdout
    data["sources"][0]["available_at"] = "2026-04-22T00:00:00"
    with pytest.raises(ValidationError):
        ResearchCase.model_validate(data)
    assert run_cli("2026-04-19T00:00:00Z").returncode == 2


def test_reject_backdated_links_and_model_drafts(data):
    data["edges"][-1]["recorded_at"] = data["opened_at"]
    with pytest.raises(ValidationError, match="早于其节点"):
        ResearchCase.model_validate(data)
    data["edges"][-1]["recorded_at"] = data["nodes"][-1]["recorded_at"]
    data["draft"] = {
        "materials_as_of": "2026-05-17T00:00:00Z",
        "generated_at": "2026-06-01T00:00:00Z",
    }
    with pytest.raises(ValidationError, match="实际生成时间"):
        ResearchCase.model_validate(data)
