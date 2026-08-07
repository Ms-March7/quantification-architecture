from __future__ import annotations

import pandas as pd
import pandera.pandas as pa
from pandera import Check


EVIDENCE_STATES = ["OBSERVED", "PROXY", "PARTIALLY_IDENTIFIED", "NOT_IDENTIFIED"]
QUALITY_STATES = ["OK", "MISSING", "STALE", "CONFLICT", "UNLICENSED"]
DAILY_STATE_KEY = ["date", "family", "market", "metric"]
SECURITY_MASTER_KEY = ["symbol"]


UNIFIED_DAILY_STATE_SCHEMA = pa.DataFrameSchema(
    {
        "date": pa.Column(str, Check.str_matches(r"^\d{4}-\d{2}-\d{2}$"), nullable=False),
        "family": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "market": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "metric": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "value": pa.Column(float, nullable=True, coerce=True),
        "unit": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "evidence_state": pa.Column(str, Check.isin(EVIDENCE_STATES), nullable=False),
        "quality": pa.Column(str, Check.isin(QUALITY_STATES), nullable=False),
        "source": pa.Column(str, Check.str_length(min_value=1), nullable=False),
    },
    strict=True,
    coerce=True,
)


SECURITY_MASTER_SCHEMA = pa.DataFrameSchema(
    {
        "symbol": pa.Column(str, Check.str_matches(r"^\d{6}\.(SH|SZ|BJ)$"), nullable=False),
        "source_symbol": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "name": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "exchange": pa.Column(str, Check.isin(["SSE", "SZSE", "BSE"]), nullable=False),
        "security_type": pa.Column(str, Check.equal_to("STOCK"), nullable=False),
        "snapshot_date": pa.Column(str, Check.str_matches(r"^\d{4}-\d{2}-\d{2}$"), nullable=False),
        "available_at": pa.Column(str, Check.str_length(min_value=10), nullable=False),
        "ingested_at": pa.Column(str, Check.str_length(min_value=10), nullable=False),
        "source_name": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "source_reference": pa.Column(str, Check.str_length(min_value=1), nullable=False),
        "evidence_state": pa.Column(str, Check.isin(EVIDENCE_STATES), nullable=False),
        "quality": pa.Column(str, Check.isin(QUALITY_STATES), nullable=False),
        "license_status": pa.Column(str, Check.isin(["VERIFIED", "UNVERIFIED", "UNLICENSED", "RESTRICTED"]), nullable=False),
    },
    strict=True,
    coerce=True,
)


def validate_unified_daily_state(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate the S1/S1R unified daily-state schema and unique key."""

    checked = UNIFIED_DAILY_STATE_SCHEMA.validate(frame, lazy=True)
    duplicates = checked.duplicated(DAILY_STATE_KEY, keep=False)
    if duplicates.any():
        duplicate_keys = checked.loc[duplicates, DAILY_STATE_KEY].to_dict(orient="records")
        raise ValueError(f"duplicate unified daily-state keys: {duplicate_keys[:5]}")
    return checked


def validate_security_master(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate an optional-adapter security master without promoting its evidence status."""

    checked = SECURITY_MASTER_SCHEMA.validate(frame, lazy=True)
    duplicates = checked.duplicated(SECURITY_MASTER_KEY, keep=False)
    if duplicates.any():
        duplicate_keys = checked.loc[duplicates, SECURITY_MASTER_KEY].to_dict(orient="records")
        raise ValueError(f"duplicate security-master keys: {duplicate_keys[:5]}")
    return checked
