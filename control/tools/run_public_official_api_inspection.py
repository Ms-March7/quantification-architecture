from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable


WORK_PACKAGE_ID = "L1-D2A-04B_PUBLIC_OFFICIAL_NO_KEY_API_INSPECTION_ADAPTERS"
CONTRACT_RELATIVE = Path(
    "data/catalog/contracts/public_official_api_inspection_v1/contract.json"
)
OUTPUT_RELATIVE = Path("tmp/public-official-api-inspection")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InspectionError(RuntimeError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    path.write_text(payload + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InspectionError(f"JSON object required: {path}")
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def resolved_under(path: Path, parent: Path) -> bool:
    path = path.resolve()
    parent = parent.resolve()
    return path == parent or parent in path.parents


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise InspectionError(
            "run_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
        )
    return run_id


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def strict_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise InspectionError(f"{label} must be numeric, not boolean")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise InspectionError(f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(parsed):
        raise InspectionError(f"{label} must be finite")
    return parsed


def strict_date(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InspectionError(f"{label} must be a non-empty date string")
    candidate = value.strip()
    if "T" in candidate:
        candidate = candidate.split("T", 1)[0]
    try:
        return dt.date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise InspectionError(f"{label} is not ISO date-like: {value!r}") from exc


def ensure_cardinality(source: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    minimum = int(source["row_count_min"])
    maximum = int(source["row_count_max"])
    if not minimum <= len(rows) <= maximum:
        raise InspectionError(
            f"row count {len(rows)} outside [{minimum}, {maximum}]"
        )


def parse_ecb_sdmx_csv(
    raw: bytes, source: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InspectionError("ECB response is not UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames or len(fieldnames) != len(set(fieldnames)):
        raise InspectionError("ECB CSV header is missing or contains duplicates")
    required = set(source["required_raw_fields"])
    missing = sorted(required - set(fieldnames))
    if missing:
        raise InspectionError(f"ECB CSV missing required fields: {missing}")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(reader, start=1):
        if None in row:
            raise InspectionError(f"ECB row {index} has surplus columns")
        rows.append(
            {
                "source_id": source["source_id"],
                "series_id": row["KEY"],
                "observation_date": strict_date(
                    row["TIME_PERIOD"], f"ECB row {index} TIME_PERIOD"
                ),
                "value": strict_float(
                    row["OBS_VALUE"], f"ECB row {index} OBS_VALUE"
                ),
                "frequency": row["FREQ"],
                "currency": row["CURRENCY"],
                "currency_denom": row["CURRENCY_DENOM"],
                "exchange_rate_type": row["EXR_TYPE"],
                "exchange_rate_suffix": row["EXR_SUFFIX"],
                "observation_status": row["OBS_STATUS"],
                "collection": row["COLLECTION"],
                "unit": row["UNIT"],
                "unit_multiplier": row["UNIT_MULT"],
                "source_published_at": None,
                "available_at": None,
                "revision_id": None,
            }
        )
    ensure_cardinality(source, rows)
    return rows, {
        "parser": source["parser"],
        "raw_fields": fieldnames,
        "raw_schema_sha256": sha256_bytes(canonical_json_bytes(fieldnames)),
    }


def parse_bis_sdmx_xml(
    raw: bytes, source: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InspectionError("BIS response is not valid XML") from exc
    series_nodes = [
        element for element in root.iter() if local_name(element.tag) == "Series"
    ]
    if not series_nodes:
        raise InspectionError("BIS XML contains no Series")
    required_series = set(source["required_series_attributes"])
    required_obs = set(source["required_observation_attributes"])
    rows: list[dict[str, Any]] = []
    observed_series_attributes: set[str] = set()
    observed_observation_attributes: set[str] = set()
    for series_index, series in enumerate(series_nodes, start=1):
        observed_series_attributes.update(series.attrib)
        missing_series = sorted(required_series - set(series.attrib))
        if missing_series:
            raise InspectionError(
                f"BIS series {series_index} missing attributes: {missing_series}"
            )
        observations = [
            element
            for element in series.iter()
            if local_name(element.tag) == "Obs"
        ]
        if not observations:
            raise InspectionError(f"BIS series {series_index} contains no Obs")
        for obs_index, observation in enumerate(observations, start=1):
            observed_observation_attributes.update(observation.attrib)
            missing_obs = sorted(required_obs - set(observation.attrib))
            if missing_obs:
                raise InspectionError(
                    "BIS observation "
                    f"{series_index}/{obs_index} missing attributes: {missing_obs}"
                )
            rows.append(
                {
                    "source_id": source["source_id"],
                    "series_id": ".".join(
                        [
                            series.attrib["FREQ"],
                            series.attrib["REF_AREA"],
                            series.attrib["CURRENCY"],
                            series.attrib["COLLECTION"],
                        ]
                    ),
                    "observation_date": strict_date(
                        observation.attrib["TIME_PERIOD"],
                        f"BIS observation {series_index}/{obs_index} TIME_PERIOD",
                    ),
                    "value": strict_float(
                        observation.attrib["OBS_VALUE"],
                        f"BIS observation {series_index}/{obs_index} OBS_VALUE",
                    ),
                    "frequency": series.attrib["FREQ"],
                    "reference_area": series.attrib["REF_AREA"],
                    "currency": series.attrib["CURRENCY"],
                    "collection": series.attrib["COLLECTION"],
                    "observation_status": observation.attrib["OBS_STATUS"],
                    "source_published_at": None,
                    "available_at": None,
                    "revision_id": None,
                }
            )
    ensure_cardinality(source, rows)
    schema = {
        "series_attributes": sorted(observed_series_attributes),
        "observation_attributes": sorted(observed_observation_attributes),
    }
    return rows, {
        "parser": source["parser"],
        "raw_schema": schema,
        "raw_schema_sha256": sha256_bytes(canonical_json_bytes(schema)),
    }


def parse_us_treasury_atom_xml(
    raw: bytes, source: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InspectionError("Treasury response is not valid XML") from exc
    entries = [
        element for element in root.iter() if local_name(element.tag) == "entry"
    ]
    if not entries:
        raise InspectionError("Treasury Atom feed contains no entry")
    required = set(source["required_raw_fields"])
    rows: list[dict[str, Any]] = []
    observed_fields: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        properties_nodes = [
            element
            for element in entry.iter()
            if local_name(element.tag) == "properties"
        ]
        if len(properties_nodes) != 1:
            raise InspectionError(
                f"Treasury entry {index} must contain exactly one properties node"
            )
        values = {
            local_name(child.tag): child.text
            for child in list(properties_nodes[0])
        }
        observed_fields.update(values)
        missing = sorted(required - set(values))
        if missing:
            raise InspectionError(
                f"Treasury entry {index} missing required fields: {missing}"
            )
        row: dict[str, Any] = {
            "source_id": source["source_id"],
            "series_id": "DAILY_TREASURY_PAR_YIELD_CURVE",
            "observation_date": strict_date(
                values["NEW_DATE"], f"Treasury entry {index} NEW_DATE"
            ),
            "source_published_at": None,
            "available_at": None,
            "revision_id": None,
        }
        for field in sorted(values):
            if field == "NEW_DATE":
                continue
            text = values[field]
            row[field.lower()] = (
                None
                if text is None or not text.strip()
                else strict_float(text, f"Treasury entry {index} {field}")
            )
        rows.append(row)
    ensure_cardinality(source, rows)
    fields = sorted(observed_fields)
    return rows, {
        "parser": source["parser"],
        "raw_fields": fields,
        "raw_schema_sha256": sha256_bytes(canonical_json_bytes(fields)),
    }


def parse_hkma_json(
    raw: bytes, source: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionError("HKMA response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise InspectionError("HKMA root must be an object")
    header = payload.get("header")
    result = payload.get("result")
    if not isinstance(header, dict) or header.get("success") is not True:
        raise InspectionError("HKMA header does not report success=true")
    if not isinstance(result, dict) or not isinstance(result.get("records"), list):
        raise InspectionError("HKMA result.records must be an array")
    records = result["records"]
    datasize = result.get("datasize")
    if not isinstance(datasize, int) or datasize != len(records):
        raise InspectionError("HKMA datasize does not match records length")
    required = set(source["required_raw_fields"])
    rows: list[dict[str, Any]] = []
    observed_fields: set[str] = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise InspectionError(f"HKMA record {index} must be an object")
        observed_fields.update(record)
        missing = sorted(required - set(record))
        if missing:
            raise InspectionError(
                f"HKMA record {index} missing required fields: {missing}"
            )
        rows.append(
            {
                "source_id": source["source_id"],
                "series_id": "HKMA_ER_EERI_DAILY",
                "observation_date": strict_date(
                    record["end_of_day"], f"HKMA record {index} end_of_day"
                ),
                "usd_hkd": strict_float(record["usd"], f"HKMA record {index} usd"),
                "cny_hkd": strict_float(record["cny"], f"HKMA record {index} cny"),
                "source_published_at": None,
                "available_at": None,
                "revision_id": None,
            }
        )
    ensure_cardinality(source, rows)
    fields = sorted(observed_fields)
    return rows, {
        "parser": source["parser"],
        "raw_fields": fields,
        "raw_schema_sha256": sha256_bytes(canonical_json_bytes(fields)),
    }


PARSERS: dict[
    str,
    Callable[
        [bytes, dict[str, Any]],
        tuple[list[dict[str, Any]], dict[str, Any]],
    ],
] = {
    "ecb_sdmx_csv": parse_ecb_sdmx_csv,
    "bis_sdmx_xml": parse_bis_sdmx_xml,
    "us_treasury_atom_xml": parse_us_treasury_atom_xml,
    "hkma_json": parse_hkma_json,
}


def validate_contract(contract: dict[str, Any]) -> None:
    if contract.get("schema_version") != 1:
        raise InspectionError("unsupported contract schema_version")
    if contract.get("contract_id") != "public-official-api-inspection-v1":
        raise InspectionError("unexpected contract_id")
    if contract.get("work_package_id") != WORK_PACKAGE_ID:
        raise InspectionError("unexpected work_package_id")
    if contract.get("status") != "INSPECTION_ONLY__NOT_ADMITTED":
        raise InspectionError("contract status must remain inspection-only")
    publication = contract.get("publication_policy")
    if not isinstance(publication, dict):
        raise InspectionError("publication_policy object required")
    forbidden_true = [
        "formal_catalog_write",
        "formal_data_write",
        "quality_promotion",
        "scheduler_creation",
        "bulk_or_history_backfill",
        "source_admission",
    ]
    if any(publication.get(field) is not False for field in forbidden_true):
        raise InspectionError("all publication/admission flags must be false")
    if publication.get("purpose_ceiling") != "INSPECTION_ONLY":
        raise InspectionError("purpose_ceiling must be INSPECTION_ONLY")
    network = contract.get("network_policy")
    if not isinstance(network, dict):
        raise InspectionError("network_policy object required")
    required_network_values = {
        "default_mode": "fixture",
        "live_requires_explicit_flag": True,
        "method": "GET",
        "requests_per_source_max": 1,
        "retries": 0,
        "pagination": False,
        "credentials": False,
        "cookies": False,
        "authorization_headers": False,
    }
    for field, expected in required_network_values.items():
        if network.get(field) != expected:
            raise InspectionError(
                f"network_policy.{field} must equal {expected!r}"
            )
    sources = contract.get("sources")
    if not isinstance(sources, list) or len(sources) != 4:
        raise InspectionError("exactly four sources are required")
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise InspectionError("source entries must be objects")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or source_id in source_ids:
            raise InspectionError("source_id must be unique strings")
        source_ids.add(source_id)
        if source.get("parser") not in PARSERS:
            raise InspectionError(f"unsupported parser for {source_id}")
        parsed = urllib.parse.urlsplit(source.get("endpoint", ""))
        if parsed.scheme != "https":
            raise InspectionError(f"{source_id} endpoint must use HTTPS")
        if parsed.hostname != source.get("allowed_host"):
            raise InspectionError(f"{source_id} host does not match allowlist")
        if parsed.path != source.get("allowed_path"):
            raise InspectionError(f"{source_id} path does not match allowlist")
        rights = source.get("rights_status")
        if not isinstance(rights, dict):
            raise InspectionError(f"{source_id} rights_status required")
        if rights.get("automated_incremental_access") != "NOT_ADMITTED":
            raise InspectionError(f"{source_id} automation must not be admitted")


def content_type_allowed(content_type: str, source: dict[str, Any]) -> bool:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return any(
        normalized == allowed.lower()
        for allowed in source["expected_content_types"]
    )


def extension_for_parser(parser: str) -> str:
    return {
        "ecb_sdmx_csv": ".csv",
        "bis_sdmx_xml": ".xml",
        "us_treasury_atom_xml": ".xml",
        "hkma_json": ".json",
    }[parser]


def validate_final_url(url: str, source: dict[str, Any]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != source["allowed_host"]
        or parsed.path != source["allowed_path"]
    ):
        raise InspectionError(
            f"redirect escaped allowlisted endpoint: {url}"
        )


def live_fetch(
    source: dict[str, Any], network_policy: dict[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    endpoint = source["endpoint"]
    validate_final_url(endpoint, source)
    request = urllib.request.Request(
        endpoint,
        method="GET",
        headers={
            "Accept": ", ".join(source["expected_content_types"]),
            "User-Agent": "Quantification-Public-Official-Inspection/1.0",
        },
    )
    requested_at = utc_now()
    maximum = int(network_policy["response_bytes_max"])
    timeout = int(network_policy["timeout_seconds"])
    with urllib.request.urlopen(request, timeout=timeout) as response:
        fetched_at = utc_now()
        status = int(response.status)
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        validate_final_url(final_url, source)
        if status < 200 or status >= 300:
            raise InspectionError(f"HTTP status {status}")
        if not content_type_allowed(content_type, source):
            raise InspectionError(
                f"unexpected Content-Type {content_type!r}"
            )
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise InspectionError(
            f"response exceeds {maximum} bytes"
        )
    return raw, {
        "request_count": 1,
        "method": "GET",
        "url": endpoint,
        "requested_at_utc": requested_at,
        "fetched_at_utc": fetched_at,
        "http_status": status,
        "final_url": final_url,
        "content_type": content_type,
        "retries": 0,
        "pagination": False,
    }


def fixture_fetch(
    source: dict[str, Any], contract_root: Path
) -> tuple[bytes, dict[str, Any]]:
    fixture = (contract_root / source["fixture_path"]).resolve()
    if not resolved_under(fixture, contract_root) or not fixture.is_file():
        raise InspectionError(
            f"fixture missing or outside contract root: {fixture}"
        )
    raw = fixture.read_bytes()
    if len(raw) > 1024 * 1024:
        raise InspectionError("fixture exceeds 1 MiB")
    fetched_at = utc_now()
    return raw, {
        "request_count": 0,
        "method": "FIXTURE_READ",
        "url": None,
        "requested_at_utc": None,
        "fetched_at_utc": fetched_at,
        "http_status": None,
        "final_url": None,
        "content_type": source["expected_content_types"][0],
        "retries": 0,
        "pagination": False,
        "fixture_path": fixture.as_posix(),
        "fixture_sha256": sha256_bytes(raw),
    }


def tree_metadata_fingerprint(root: Path) -> str:
    if not root.exists():
        return sha256_bytes(b"ABSENT")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append({"path": relative, "kind": "symlink"})
        elif path.is_file():
            stat = path.stat()
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        elif path.is_dir():
            entries.append({"path": relative, "kind": "directory"})
    return sha256_bytes(canonical_json_bytes(entries))


def capture_formal_state(workspace: Path) -> dict[str, Any]:
    datasets = workspace / "data" / "catalog" / "datasets.json"
    return {
        "datasets_json_sha256": (
            sha256_file(datasets) if datasets.is_file() else None
        ),
        "data_market_metadata_fingerprint": tree_metadata_fingerprint(
            workspace / "data" / "market"
        ),
        "data_published_metadata_fingerprint": tree_metadata_fingerprint(
            workspace / "data" / "published"
        ),
    }


def write_normalized_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    path.write_bytes(payload)
    return sha256_bytes(payload)


def run_one_source(
    *,
    source: dict[str, Any],
    contract_root: Path,
    network_policy: dict[str, Any],
    source_output: Path,
    mode: str,
    fetcher: Callable[
        [dict[str, Any], dict[str, Any]],
        tuple[bytes, dict[str, Any]],
    ]
    | None = None,
) -> dict[str, Any]:
    source_output.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "work_package_id": WORK_PACKAGE_ID,
        "source_id": source["source_id"],
        "source_name": source["source_name"],
        "mode": mode,
        "candidate_status": "INSPECTION_ONLY__NOT_ADMITTED",
        "source_admission": False,
        "quality_promotion": False,
        "target_plan_ids": source["target_plan_ids"],
        "started_at_utc": started_at,
        "rights_evidence": {
            "documentation_url": source["documentation_url"],
            "terms_url": source["terms_url"],
            "status": source["rights_status"],
            "interpretation": (
                "Public no-key reachability does not grant automated "
                "incremental access, local retention, internal sharing or "
                "redistribution rights."
            ),
        },
        "pit_evidence": {
            "observation_time_field": source["observation_time_field"],
            "source_published_at_field": source["source_published_at_field"],
            "revision_id_field": source["revision_id_field"],
            "pit_status": source["pit_status"],
            "available_at": None,
            "interpretation": (
                "fetched_at is acquisition evidence and is not substituted "
                "for source published_at or PIT available_at."
            ),
        },
    }
    try:
        if mode == "fixture":
            raw, fetch_receipt = fixture_fetch(source, contract_root)
        elif mode == "live":
            receipt["request"] = {
                "request_count": 1,
                "method": "GET",
                "url": source["endpoint"],
                "requested_at_utc": utc_now(),
                "fetched_at_utc": None,
                "http_status": None,
                "final_url": None,
                "content_type": None,
                "retries": 0,
                "pagination": False,
                "outcome": "ATTEMPT_STARTED__NO_RESPONSE_YET",
            }
            raw, fetch_receipt = (
                live_fetch(source, network_policy)
                if fetcher is None
                else fetcher(source, network_policy)
            )
            if fetch_receipt.get("request_count") != 1:
                raise InspectionError(
                    "live fetcher must report exactly one request"
                )
            if fetch_receipt.get("retries") != 0:
                raise InspectionError("live fetcher must report zero retries")
            fetch_receipt["outcome"] = "RESPONSE_RECEIVED"
        else:
            raise InspectionError(f"unsupported mode: {mode}")

        extension = extension_for_parser(source["parser"])
        raw_path = source_output / f"raw-response{extension}"
        raw_path.write_bytes(raw)
        receipt["request"] = fetch_receipt
        receipt["raw"] = {
            "path": raw_path.as_posix(),
            "bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "temporary_inspection_evidence": True,
        }
        rows, parser_receipt = PARSERS[source["parser"]](raw, source)
        normalized_path = source_output / "normalized-inspection.jsonl"
        normalized_hash = write_normalized_jsonl(normalized_path, rows)
        receipt["parser"] = parser_receipt
        receipt["normalized"] = {
            "path": normalized_path.as_posix(),
            "row_count": len(rows),
            "sha256": normalized_hash,
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_dataset": False,
        }
        receipt["status"] = (
            "FIXTURE_STRUCTURE_PASS__INSPECTION_ONLY"
            if mode == "fixture"
            else "LIVE_STRUCTURE_PASS__INSPECTION_ONLY"
        )
    except Exception as exc:
        receipt["status"] = "FAIL_CLOSED__QUARANTINED"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["normalized"] = None
        write_json(
            source_output / "QUARANTINE.json",
            {
                "schema_version": 1,
                "source_id": source["source_id"],
                "reason": receipt["error"],
                "formal_catalog_unchanged_required": True,
                "candidate_status": "INSPECTION_ONLY__NOT_ADMITTED",
            },
        )
    receipt["finished_at_utc"] = utc_now()
    write_json(source_output / "RECEIPT.json", receipt)
    return receipt


def build_inventory(output: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "INVENTORY.json":
            files.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "schema_version": 1,
        "files_excluding_inventory": files,
        "total_bytes_excluding_inventory": sum(
            item["bytes"] for item in files
        ),
    }


def execute_run(
    *,
    contract_path: Path,
    output: Path,
    mode: str,
    selected_source_ids: list[str] | None = None,
    fetcher: Callable[
        [dict[str, Any], dict[str, Any]],
        tuple[bytes, dict[str, Any]],
    ]
    | None = None,
) -> dict[str, Any]:
    contract = load_json(contract_path)
    validate_contract(contract)
    contract_root = contract_path.parent.resolve()
    if output.exists():
        raise InspectionError(f"run output already exists: {output}")
    output.mkdir(parents=True)
    all_sources = {
        source["source_id"]: source for source in contract["sources"]
    }
    selected = (
        list(all_sources)
        if selected_source_ids is None
        else selected_source_ids
    )
    if not selected or len(selected) != len(set(selected)):
        raise InspectionError("source selection must be unique and non-empty")
    unknown = sorted(set(selected) - set(all_sources))
    if unknown:
        raise InspectionError(f"unknown source selection: {unknown}")

    receipts: list[dict[str, Any]] = []
    for source_id in selected:
        receipts.append(
            run_one_source(
                source=all_sources[source_id],
                contract_root=contract_root,
                network_policy=contract["network_policy"],
                source_output=output / "sources" / source_id,
                mode=mode,
                fetcher=fetcher,
            )
        )
    passed = all(
        receipt["status"].endswith("__INSPECTION_ONLY")
        for receipt in receipts
    )
    summary = {
        "schema_version": 1,
        "work_package_id": WORK_PACKAGE_ID,
        "contract_id": contract["contract_id"],
        "contract_sha256": sha256_file(contract_path),
        "mode": mode,
        "selected_sources": selected,
        "source_count": len(selected),
        "source_pass_count": sum(
            receipt["status"].endswith("__INSPECTION_ONLY")
            for receipt in receipts
        ),
        "source_quarantine_count": sum(
            receipt["status"] == "FAIL_CLOSED__QUARANTINED"
            for receipt in receipts
        ),
        "network_requests": sum(
            (receipt.get("request") or {}).get("request_count", 0)
            for receipt in receipts
        ),
        "retries": 0,
        "bulk_or_history_backfill": False,
        "formal_catalog_write": False,
        "formal_data_write": False,
        "source_admission": False,
        "quality_promotion": False,
        "scheduler_created": False,
        "candidate_status": "INSPECTION_ONLY__NOT_ADMITTED",
        "status": (
            "FIXTURE_MECHANICS_PASS__NO_SOURCE_ADMISSION"
            if passed and mode == "fixture"
            else "LIVE_STRUCTURE_PASS__INSPECTION_ONLY__NO_SOURCE_ADMISSION"
            if passed
            else "FAIL_CLOSED__ONE_OR_MORE_SOURCES_QUARANTINED"
        ),
        "interpretation_boundary": [
            "Fixture pass proves parser and policy mechanics only.",
            "Live structure pass proves one bounded response parsed only.",
            "Neither status grants license, PIT closure, source admission, "
            "catalog registration, quality promotion, research use or daily "
            "automation.",
        ],
    }
    write_json(output / "RUN_RECEIPT.json", summary)
    write_json(output / "INVENTORY.json", build_inventory(output))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run fail-closed, no-key public official API inspection adapters. "
            "Default is offline fixture mode."
        )
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--mode",
        choices=("fixture", "live"),
        default="fixture",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Select one source_id; repeat to select more. Default: all four.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace_root.resolve()
    validate_run_id(args.run_id)
    contract_path = (workspace / CONTRACT_RELATIVE).resolve()
    if not resolved_under(contract_path, workspace / "data" / "catalog"):
        raise SystemExit("contract path escaped data/catalog")
    output_root = (workspace / OUTPUT_RELATIVE).resolve()
    output = (output_root / args.run_id).resolve()
    if not resolved_under(output, output_root):
        raise SystemExit("output escaped isolated tmp root")
    if resolved_under(output, workspace / "data"):
        raise SystemExit("refusing output under formal data root")

    formal_before = capture_formal_state(workspace)
    try:
        summary = execute_run(
            contract_path=contract_path,
            output=output,
            mode=args.mode,
            selected_source_ids=args.sources,
        )
    except InspectionError as exc:
        raise SystemExit(f"FAIL_CLOSED: {exc}") from exc
    formal_after = capture_formal_state(workspace)
    summary["formal_state_before"] = formal_before
    summary["formal_state_after"] = formal_after
    summary["formal_state_unchanged"] = formal_before == formal_after
    if not summary["formal_state_unchanged"]:
        summary["status"] = "FAIL_CLOSED__FORMAL_STATE_CHANGED"
    write_json(output / "RUN_RECEIPT.json", summary)
    write_json(output / "INVENTORY.json", build_inventory(output))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if summary["status"]
        in {
            "FIXTURE_MECHANICS_PASS__NO_SOURCE_ADMISSION",
            "LIVE_STRUCTURE_PASS__INSPECTION_ONLY__NO_SOURCE_ADMISSION",
        }
        and summary["formal_state_unchanged"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
