from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def number(value: Any, multiplier: float = 1.0) -> float | None:
    if value in (None, "", "--", "-"):
        return None
    try:
        return float(str(value).replace(",", "")) * multiplier
    except (TypeError, ValueError):
        return None


def iso_date(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


def next_weekday(date: str) -> str:
    value = dt.date.fromisoformat(date) + dt.timedelta(days=1)
    while value.weekday() >= 5:
        value += dt.timedelta(days=1)
    return value.isoformat()
