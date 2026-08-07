from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Mapping


REQUIRED_SOURCE_KEYS = {
    "tier",
    "source_id",
    "role",
    "enabled",
    "package",
    "credential_env",
    "families",
}


def load_source_policy(path: str | Path) -> dict[str, Any]:
    """Load and validate the machine-readable source precedence policy."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "s1r-source-policy-v1":
        raise ValueError("unsupported source policy schema")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("source policy requires at least one source")
    tiers: list[int] = []
    for source in sources:
        if not isinstance(source, dict) or not REQUIRED_SOURCE_KEYS.issubset(source):
            raise ValueError("source entry is missing required keys")
        tier = source["tier"]
        if not isinstance(tier, int) or tier < 1:
            raise ValueError("source tier must be a positive integer")
        tiers.append(tier)
        credential_env = source["credential_env"]
        if credential_env is not None and (
            not isinstance(credential_env, str) or not credential_env.endswith("_TOKEN")
        ):
            raise ValueError("credentials must be referenced by a *_TOKEN environment variable name")
    if tiers != sorted(tiers) or len(tiers) != len(set(tiers)):
        raise ValueError("source tiers must be unique and increasing")
    official = sources[0]
    if official["tier"] != 1 or official["source_id"] != "official_direct" or official["role"] != "canonical":
        raise ValueError("official_direct must be the tier-1 canonical source")
    conflict = payload.get("conflict_policy") or {}
    if conflict.get("average_conflicting_values") is not False:
        raise ValueError("conflicting source values must not be averaged")
    if conflict.get("quality_state") != "CONFLICT" or conflict.get("workflow_state") != "REVIEW":
        raise ValueError("source conflicts must be visible as CONFLICT/REVIEW")
    return payload


def adapter_status(
    policy: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Report package and credential availability without exposing secret values."""

    environment = os.environ if environ is None else environ
    result: list[dict[str, Any]] = []
    for source in policy["sources"]:
        package = source["package"]
        credential_env = source["credential_env"]
        result.append(
            {
                "source_id": source["source_id"],
                "tier": source["tier"],
                "enabled": bool(source["enabled"]),
                "package_installed": package is None or importlib.util.find_spec(package) is not None,
                "credential_required": credential_env is not None,
                "credential_configured": credential_env is None or bool(environment.get(credential_env)),
            }
        )
    return result


def candidates_for_family(policy: Mapping[str, Any], family: str) -> list[dict[str, Any]]:
    """Return enabled candidates in precedence order for a data family."""

    candidates = [
        source
        for source in policy["sources"]
        if source["enabled"] and ("*" in source["families"] or family in source["families"])
    ]
    return sorted(candidates, key=lambda source: source["tier"])
