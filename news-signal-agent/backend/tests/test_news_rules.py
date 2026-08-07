from datetime import datetime

from app.classify.rules import classify_signal_with_rules
from app.collectors.eastmoney import CN_TZ, parse_eastmoney_time
from app.linker.rules import link_events_to_watchlist, match_watch_symbol
from app.models import AssetType, Direction, EventItem, NewsItem, WatchSymbol


def test_parse_eastmoney_time_uses_china_timezone() -> None:
    parsed = parse_eastmoney_time("2026-06-25 17:01:55")
    assert parsed == datetime(2026, 6, 25, 17, 1, 55, tzinfo=CN_TZ)


def test_theme_link_scores_etf_tag_match() -> None:
    news = NewsItem(
        id="n1",
        title="存储芯片板块成交活跃",
        source="test",
        source_type="mainstream_media",
    )
    symbol = WatchSymbol(
        symbol="159995",
        name="芯片ETF华夏",
        asset_type=AssetType.etf,
        tags=["芯片", "半导体"],
    )
    match = match_watch_symbol(news, symbol)
    assert match["link_type"] == "theme"
    assert match["relevance"] >= 70


def test_rule_classification_sets_bullish_signal() -> None:
    news = NewsItem(
        id="n1",
        title="功率半导体企业订单超预期",
        source="test",
        source_type="mainstream_media",
    )
    event = EventItem(id="e1", title=news.title, news_ids=[news.id])
    watch = WatchSymbol(
        symbol="300373",
        name="扬杰科技",
        asset_type=AssetType.stock,
        tags=["功率半导体", "SiC"],
    )
    [signal] = link_events_to_watchlist([event], {news.id: news}, [watch])

    classify_signal_with_rules(signal, news, source_scores={"mainstream_media": 80})

    assert signal.direction == Direction.bullish
    assert signal.score_breakdown.direction_confidence > 50
    assert "超预期" in signal.metadata["classification"]["bullish_hits"]
