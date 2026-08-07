from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from app.models import NewsItem


def collect_mock_news() -> list[NewsItem]:
    """Return deterministic mock news for the first workflow smoke test."""
    rows = [
        {
            "title": "半导体板块出现分化，存储芯片方向成交活跃",
            "source": "mock",
            "source_type": "media",
        },
        {
            "title": "功率半导体企业加速布局 SiC 车规产品",
            "source": "mock",
            "source_type": "industry",
        },
    ]
    items: list[NewsItem] = []
    for row in rows:
        digest = sha256(row["title"].encode("utf-8")).hexdigest()[:16]
        items.append(
            NewsItem(
                id=f"news_{digest}",
                title=row["title"],
                source=row["source"],
                source_type=row["source_type"],
                published_at=datetime.utcnow(),
            )
        )
    return items
