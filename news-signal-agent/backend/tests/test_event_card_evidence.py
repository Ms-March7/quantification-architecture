from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models import EventItem, NewsItem
from app.storage.sqlite import list_event_cards, store_event_cards
from app.workflow.event_cards import build_event_cards


def announcement(body: str) -> NewsItem:
    title = "示例公司季度报告"
    return NewsItem(
        id="announcement-1",
        title=f"000000 示例公司：{title}",
        summary=body[:180] if body else title,
        source="巨潮资讯",
        source_type="official",
        url="https://issuer.example/quarterly-report.pdf",
        published_at=datetime(2026, 4, 24, 12, tzinfo=timezone.utc),
        raw_text=f"000000 示例公司 {title} {body}".strip(),
        metadata={
            "collector": "cninfo",
            "body_chars": len(body),
            "body_extract_status": "pdf_text" if body else "disabled",
        },
    )


def build_card(items, model_response):
    event = EventItem(id="quarterly-results", title="季度业绩", news_ids=[item.id for item in items])
    with patch("app.workflow.event_cards.llm_json", return_value=model_response) as client:
        [card] = build_event_cards([event], {item.id: item for item in items}, [], {})
    client.assert_called_once()
    return event, card, client.call_args.args[1]


def test_verified_body_quote_keeps_original_source_through_storage(tmp_path):
    quote = "本期库存规模较上季度增加。"
    # Beyond the short collector summary, but near the start of the extracted PDF body.
    item = announcement("说明文字。" * 37 + quote)
    question = "库存增加来自价格变化还是备货数量变化？"
    event, card, prompt = build_card(
        [item],
        {
            "title": "库存变化",
            "source_quotes": [{"news_id": item.id, "quote": quote}],
            "follow_up_question": question,
        },
    )
    assert quote not in item.summary
    assert item.id in prompt and quote in prompt
    assert card["source_quotes"] == [{
        "news_id": item.id,
        "quote": quote,
        "source": item.source,
        "title": item.title,
        "url": item.url,
        "published_at": item.published_at.isoformat(),
    }]
    assert card["follow_up_question"] == question
    assert store_event_cards(tmp_path, "offline", [event]) == 1
    [stored] = list_event_cards(tmp_path)
    assert stored["card"] == card


def test_rejects_wrong_source_fabricated_text_and_unseen_body_tail():
    quote = "封测环节的备货仍处于低位。"
    unseen = "这句只在未送入模型的正文末尾出现。"
    first = announcement("本期出货量有所改善。" + "附注。" * 1200 + unseen)
    second = NewsItem(
        id="rss-2",
        title="产业调查",
        summary=quote,
        raw_text=f"产业调查 {quote}",
        source="示例产业周报",
        source_type="normal_media",
        url="https://research.example/industry",
        metadata={"collector": "rss"},
    )
    _, card, prompt = build_card(
        [first, second],
        {
            "title": "库存与出货",
            "source_quotes": [
                {"news_id": first.id, "quote": quote},
                {"news_id": second.id, "quote": "封测环节库存已经恢复正常。"},
                {"news_id": "absent", "quote": quote},
                {"news_id": first.id, "quote": unseen},
                {"news_id": second.id, "quote": quote},
            ],
        },
    )
    assert quote in prompt and unseen not in prompt
    assert [(entry["news_id"], entry["quote"]) for entry in card["source_quotes"]] == [
        (second.id, quote)
    ]
    assert card["follow_up_question"] == ""


def test_empty_announcement_body_cannot_support_a_title_quote():
    item = announcement("")
    _, card, _ = build_card(
        [item], {"title": "季度业绩", "source_quotes": [{"news_id": item.id, "quote": item.summary}]}
    )
    assert card["source_quotes"] == []


def test_search_query_and_unlinked_announcement_cannot_be_cited():
    unlinked = announcement("报告期内采购节奏基本稳定。")
    unlinked.url = None
    query = "晶圆代工成本是否上涨？"
    result = NewsItem(
        id="search-1",
        title="产业链跟踪",
        summary="本月出货保持稳定。",
        raw_text=f"{query} 产业链跟踪 本月出货保持稳定。",
        source="示例搜索",
        source_type="normal_media",
        url="https://research.example/monthly",
        metadata={"collector": "web_search", "query": query},
    )
    _, card, _ = build_card(
        [result, unlinked],
        {
            "title": "采购与出货",
            "source_quotes": [
                {"news_id": result.id, "quote": query},
                {"news_id": unlinked.id, "quote": unlinked.summary},
            ],
        },
    )
    assert card["source_quotes"] == []


@pytest.mark.parametrize(
    ("response", "generator"), [({"title": "旧版模型返回"}, "llm"), (None, "fallback")]
)
def test_cards_without_model_evidence_have_empty_fields(response, generator):
    _, card, _ = build_card([announcement("报告期末库存有所增加。")], response)
    assert card["generator"] == generator
    assert card["source_quotes"] == []
    assert card["follow_up_question"] == ""
