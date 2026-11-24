import json
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import yaml

from app.research_draft import Materials, generate, main
from app.research_review import ResearchCase, parse_timestamp, render


AS_OF = "2025-04-30T15:00:00+08:00"
QUOTE = "样本现货报价上涨。"


def inputs():
    def source(identifier, date, excerpt):
        return {"id": identifier, "available_at": date, "scope": "示例",
                "title": identifier, "url": f"https://example.org/{identifier}", "excerpt": excerpt}
    return {
        "question": "现货报价能否传导至毛利？", "synthetic": True,
        "sources": [source("spot", AS_OF, QUOTE),
                    source("later", "2025-05-01T12:00:00+08:00", "截止后样本。")],
        "nodes": [{"label": "研究者事后复盘意见"}],
    }


def proposal():
    return {
        "nodes": [
            {"id": "price", "kind": "observation", "label": QUOTE},
            {"id": "asp", "kind": "hypothesis", "label": "公司合约价可能上升"},
            {"id": "margin", "kind": "conclusion", "label": "毛利可能改善"},
        ],
        "edges": [
            {"id": "price_asp", "from_node": "price", "to_node": "asp", "effect": "supports",
             "condition": "规格及合同条款可比", "next_check": "核查合约结算价"},
            {"id": "asp_margin", "from_node": "asp", "to_node": "margin", "effect": "supports",
             "condition": "单位成本增幅较低", "next_check": "还需采购成本"},
        ],
        "evidence": [{"edge_id": "price_asp", "source_id": "spot", "stance": "supports",
                      "quote": QUOTE, "interpretation": "现货方向只支撑第一步。"}],
    }


def test_cli_proposal_reopens_in_review_without_future_material_or_analyst_notes(tmp_path, monkeypatch):
    src, dst = tmp_path / "materials.yaml", tmp_path / "draft.yaml"
    src.write_text(yaml.safe_dump(inputs(), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["research_draft", str(src), "--as-of", AS_OF, "--output", str(dst)])
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    before = datetime.now(timezone.utc)
    with patch("app.research_draft.llm_json", return_value=proposal()) as mock:
        main()
    model_input = json.loads(mock.call_args.args[1])
    assert [s["id"] for s in model_input["sources"]] == ["spot"]
    assert "截止后样本" not in mock.call_args.args[1]
    assert "研究者事后复盘意见" not in mock.call_args.args[1]
    case = ResearchCase.model_validate(yaml.safe_load(dst.read_text(encoding="utf-8")))
    assert case.synthetic and case.draft.materials_as_of == parse_timestamp(AS_OF)
    assert before <= case.draft.generated_at <= datetime.now(timezone.utc)
    assert case.opened_at == case.draft.generated_at
    assert all(x.recorded_at == case.opened_at for x in [*case.nodes, *case.edges, *case.evidence])
    memo = render(case, case.opened_at)
    assert "草稿" in memo and "待证" in memo and "还需采购成本" in memo
    with pytest.raises(ValueError):
        render(case, parse_timestamp(AS_OF))


@pytest.mark.parametrize("bad", ["future_source", "fake_quote", "unsupported_observation", "missing_check"])
def test_ungrounded_draft_is_rejected(bad, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    result = proposal()
    if bad == "future_source":
        result["evidence"][0]["source_id"] = "later"
    elif bad == "fake_quote":
        result["evidence"][0]["quote"] = "公司合约价上涨。"
    elif bad == "unsupported_observation":
        result["nodes"][0]["label"] = "长期合约价已经上涨"
    else:
        del result["edges"][1]["next_check"]
    with patch("app.research_draft.llm_json", return_value=result), pytest.raises(ValueError):
        generate(Materials.model_validate(inputs()), parse_timestamp(AS_OF), {})


def test_empty_source_window_makes_no_model_call():
    with patch("app.research_draft.llm_json") as mock, pytest.raises(ValueError, match="没有可用材料"):
        generate(Materials.model_validate(inputs()), parse_timestamp("2025-01-01T00:00:00Z"), {})
    mock.assert_not_called()
