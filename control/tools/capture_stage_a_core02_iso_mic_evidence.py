from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ID = "stage-a-core02-iso-mic-evidence-20260730-v1"
PAGE_URL = "https://www.iso20022.org/market-identifier-codes"
CSV_URL = (
    "https://www.iso20022.org/sites/default/files/"
    "ISO10383_MIC/ISO10383_MIC.csv"
)
EXPECTED_ROWS = {
    "XSHG": "SHANGHAI STOCK EXCHANGE",
    "XSHE": "SHENZHEN STOCK EXCHANGE",
    "BJSE": "BEIJING STOCK EXCHANGE",
}
FORMAL_FILES = (
    "data/catalog/datasets.json",
    "data/catalog/contracts/source_admission_v1/current_source_registry.json",
    "control/ROADMAP.md",
    "control/projects.yaml",
)
FORMAL_TREES = (
    "data/market/snapshots",
    "data/published",
)
REQUIRED_COLUMNS = (
    "MIC",
    "OPERATING MIC",
    "OPRT/SGMT",
    "MARKET NAME-INSTITUTION DESCRIPTION",
    "ISO COUNTRY CODE (ISO 3166)",
    "STATUS",
    "CREATION DATE",
    "LAST UPDATE DATE",
    "LAST VALIDATION DATE",
    "EXPIRY DATE",
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(json_bytes(value))


def tree_state(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        ):
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return {
        "exists": root.exists(),
        "files": files,
        "tree_sha256": sha256_bytes(json_bytes(files)),
    }


def formal_guard(workspace: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for raw_path in FORMAL_FILES:
        path = workspace / raw_path
        files[raw_path] = {
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() else None,
            "sha256": sha256_file(path) if path.exists() else None,
        }
    trees = {
        raw_path: tree_state(workspace / raw_path)
        for raw_path in FORMAL_TREES
    }
    return {"files": files, "trees": trees}


def fetch(url: str, *, timeout: int = 30) -> tuple[bytes, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Quantification-StageA-Evidence/1.0 "
                "(bounded official-source inspection)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", response.getcode()))
        payload = response.read(10 * 1024 * 1024 + 1)
        if status != 200:
            raise RuntimeError(f"unexpected HTTP status {status}: {url}")
        if len(payload) > 10 * 1024 * 1024:
            raise RuntimeError(f"response exceeds bounded 10 MiB limit: {url}")
        receipt = {
            "url": url,
            "http_status": status,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "content_type": response.headers.get("Content-Type"),
            "content_length_header": response.headers.get("Content-Length"),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "bytes": len(payload),
            "sha256": sha256_bytes(payload),
        }
    return payload, receipt


def page_release_markers(payload: bytes) -> dict[str, str]:
    text = payload.decode("utf-8", errors="strict")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    required = {
        "publication_date": "13 July 2026",
        "modification_implementation_date": "27 July 2026",
        "next_publication_date": "10 August 2026",
    }
    missing = [value for value in required.values() if value not in text]
    if missing:
        raise ValueError(
            "official page release markers missing: " + ", ".join(missing)
        )
    if "Market identifier codes" not in text:
        raise ValueError("official MIC page title marker missing")
    return required


def parse_target_rows(payload: bytes) -> list[dict[str, str]]:
    text = payload.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("MIC CSV has no header")
    missing_columns = [
        column for column in REQUIRED_COLUMNS if column not in reader.fieldnames
    ]
    if missing_columns:
        raise ValueError(
            "MIC CSV required columns missing: " + ", ".join(missing_columns)
        )
    selected: dict[str, dict[str, str]] = {}
    for row in reader:
        mic = (row.get("MIC") or "").strip()
        if mic not in EXPECTED_ROWS:
            continue
        if mic in selected:
            raise ValueError(f"duplicate target MIC row: {mic}")
        selected[mic] = {
            column: (row.get(column) or "").strip()
            for column in reader.fieldnames
        }
    if set(selected) != set(EXPECTED_ROWS):
        raise ValueError(
            "target MIC rows incomplete: "
            f"expected={sorted(EXPECTED_ROWS)} actual={sorted(selected)}"
        )
    normalized: list[dict[str, str]] = []
    for mic in ("XSHG", "XSHE", "BJSE"):
        row = selected[mic]
        expected_name = EXPECTED_ROWS[mic]
        checks = {
            "operating_mic": row["OPERATING MIC"] == mic,
            "operator_row": row["OPRT/SGMT"] == "OPRT",
            "market_name": (
                row["MARKET NAME-INSTITUTION DESCRIPTION"] == expected_name
            ),
            "country": row["ISO COUNTRY CODE (ISO 3166)"] == "CN",
            "active": row["STATUS"] == "ACTIVE",
            "not_expired": row["EXPIRY DATE"] == "",
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise ValueError(f"invalid {mic} official row: {failed}")
        normalized.append(
            {
                "venue": expected_name,
                "mic": mic,
                "operating_mic": row["OPERATING MIC"],
                "country_code": row["ISO COUNTRY CODE (ISO 3166)"],
                "status": row["STATUS"],
                "creation_date": row["CREATION DATE"],
                "last_update_date": row["LAST UPDATE DATE"],
                "last_validation_date": row["LAST VALIDATION DATE"],
                "expiry_date": row["EXPIRY DATE"] or None,
            }
        )
    return normalized


def bind(path: Path, workspace: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(workspace).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build(workspace: Path) -> tuple[Path, Path]:
    candidate = (
        workspace
        / "tmp"
        / "d2a04b-source-closure"
        / RUN_ID
    )
    report = workspace / "reports" / "data-governance" / RUN_ID
    if candidate.exists() or report.exists():
        raise FileExistsError(
            "refusing to overwrite existing evidence package: "
            f"{candidate} or {report}"
        )
    candidate_stage = candidate.with_name(
        f".{candidate.name}.building-{os.getpid()}"
    )
    report_stage = report.with_name(f".{report.name}.building-{os.getpid()}")
    if candidate_stage.exists() or report_stage.exists():
        raise FileExistsError("staging directory already exists")

    guard_pre = formal_guard(workspace)
    candidate_stage.mkdir(parents=True)
    report_stage.mkdir(parents=True)
    try:
        page_payload, page_receipt = fetch(PAGE_URL)
        csv_payload, csv_receipt = fetch(CSV_URL)
        release_markers = page_release_markers(page_payload)
        rows = parse_target_rows(csv_payload)

        raw_dir = candidate_stage / "raw" / "iso10383"
        raw_dir.mkdir(parents=True)
        page_path = raw_dir / "market-identifier-codes.html"
        csv_path = raw_dir / "ISO10383_MIC.csv"
        page_path.write_bytes(page_payload)
        csv_path.write_bytes(csv_payload)

        final_page_path = (
            candidate / "raw" / "iso10383" / page_path.name
        )
        final_csv_path = candidate / "raw" / "iso10383" / csv_path.name
        page_receipt["local_file"] = bind(page_path, workspace)
        page_receipt["local_file"]["path"] = (
            final_page_path.relative_to(workspace).as_posix()
        )
        csv_receipt["local_file"] = bind(csv_path, workspace)
        csv_receipt["local_file"]["path"] = (
            final_csv_path.relative_to(workspace).as_posix()
        )
        receipts = [page_receipt, csv_receipt]
        write_json(candidate_stage / "REQUEST_RECEIPTS.json", receipts)

        mapping_path = candidate_stage / "MIC_MAPPING.jsonl"
        mapping_lines = []
        for row in rows:
            enriched = {
                **row,
                "source_id": "ISO_10383_REGISTRATION_AUTHORITY",
                "source_url": CSV_URL,
                "publication_date": release_markers["publication_date"],
                "modification_implementation_date": release_markers[
                    "modification_implementation_date"
                ],
                "retrieved_at_utc": csv_receipt["retrieved_at_utc"],
                "raw_sha256": csv_receipt["sha256"],
            }
            mapping_lines.append(
                json.dumps(enriched, ensure_ascii=False, sort_keys=True)
            )
        mapping_path.write_text(
            "\n".join(mapping_lines) + "\n",
            encoding="utf-8",
        )

        raw_manifest = {
            "run_id": RUN_ID,
            "files": [
                {
                    "path": final_page_path.relative_to(workspace).as_posix(),
                    "bytes": len(page_payload),
                    "sha256": sha256_bytes(page_payload),
                },
                {
                    "path": final_csv_path.relative_to(workspace).as_posix(),
                    "bytes": len(csv_payload),
                    "sha256": sha256_bytes(csv_payload),
                },
            ],
        }
        write_json(candidate_stage / "RAW_FILE_MANIFEST.json", raw_manifest)

        candidate_files = [
            mapping_path,
            candidate_stage / "RAW_FILE_MANIFEST.json",
            candidate_stage / "REQUEST_RECEIPTS.json",
        ]
        candidate_manifest = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "quality_status": "NOT_ADMITTED",
            "purpose_ceiling": "INSPECTION_ONLY",
            "formal_admission": False,
            "research_usable": False,
            "source_scope": "ISO_10383_CURRENT_MIC_MAPPING_ONLY",
            "files": [
                {
                    **bind(path, workspace),
                    "path": (
                        candidate / path.relative_to(candidate_stage)
                    ).relative_to(workspace).as_posix(),
                }
                for path in candidate_files
            ],
        }
        write_json(
            candidate_stage / "CANDIDATE_MANIFEST.json",
            candidate_manifest,
        )

        evidence = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "authority": {
                "standard": "ISO 10383",
                "registration_authority": "S.W.I.F.T. SC",
                "official_page": PAGE_URL,
                "official_csv": CSV_URL,
                **release_markers,
            },
            "mapping_rows": rows,
            "subcheck": {
                "subcheck_id": "CORE02-Q1.official_exchange_mic_mapping",
                "baseline_status": "UNKNOWN",
                "this_run_status": "PASS",
                "baseline_status_claimed": False,
                "basis": (
                    "The official ISO 10383 Registration Authority current "
                    "list contains active operating MIC rows XSHG, XSHE and "
                    "BJSE for the Shanghai, Shenzhen and Beijing stock "
                    "exchanges."
                ),
                "boundary": (
                    "This closes only the current official venue-to-MIC "
                    "mapping subcheck. It does not close the three-venue "
                    "historical calendar/session chain, BSE daily calendar, "
                    "notice revisions or package-specific rights."
                ),
            },
            "raw_bindings": raw_manifest["files"],
        }
        write_json(report_stage / "MIC_MAPPING_EVIDENCE.json", evidence)

        rights = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "official_download_available": True,
            "single_bounded_capture_completed": True,
            "automated_bulk_collection_rights_closed": False,
            "retention_and_internal_sharing_rights_closed": False,
            "redistribution_rights_closed": False,
            "global_rights_gate": "FAIL__UNCHANGED",
            "boundary": (
                "Public official download availability is evidence access, "
                "not a package-specific rights receipt."
            ),
        }
        write_json(report_stage / "RIGHTS_BOUNDARY.json", rights)

        delta = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "target": "CORE-02",
            "subcheck_delta": {
                "subcheck_id": "CORE02-Q1.official_exchange_mic_mapping",
                "before": "UNKNOWN",
                "after_this_run_audit": "PASS",
                "formal_queue_mutated": False,
            },
            "gate_delta": {
                "CORE02-Q1": {
                    "before": "FAIL",
                    "after": "FAIL",
                    "changed": False,
                }
            },
            "formal_stage_a": {
                "before": "0/4",
                "after": "0/4",
                "changed": False,
            },
            "catalog_update": False,
            "source_registry_update": False,
            "quality_promotion": False,
            "snapshot_publish": False,
            "research_authorization": False,
        }
        write_json(report_stage / "STAGE_A_STATUS_DELTA.json", delta)

        readme = f"""# CORE-02 ISO 10383 MIC 映射证据

- 运行：`{RUN_ID}`
- 官方来源：ISO 10383 Registration Authority 当前 MIC 清单
- 已严格确认：`XSHG`（上交所）、`XSHE`（深交所）、`BJSE`（北交所）
- 当前清单发布日期：`{release_markers['publication_date']}`
- 修改生效日：`{release_markers['modification_implementation_date']}`

本包只把 `CORE02-Q1.official_exchange_mic_mapping` 的本轮审计子检查记为
`PASS`。`CORE02-Q1` 仍为 `FAIL`，Stage A 仍为 `0/4`；BSE 完整日历、三所
历史交易时段/修订链以及包级权利均未闭合。

所有原始文件仅进入 `tmp/` 候选根，报告进入 `reports/`。未修改正式 catalog、
source registry、Roadmap、projects、正式快照或发布目录。
"""
        (report_stage / "README.md").write_text(readme, encoding="utf-8")

        guard_post = formal_guard(workspace)
        guards_unchanged = guard_pre == guard_post
        if not guards_unchanged:
            raise RuntimeError("formal-state guard changed during MIC capture")
        verification = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "status": "PASS__MIC_MAPPING_SUBCHECK_ONLY__NO_FORMAL_TRANSITION",
            "checks": {
                "official_page_markers": True,
                "official_csv_schema": True,
                "exact_target_mics": sorted(EXPECTED_ROWS),
                "target_row_count": len(rows),
                "all_target_rows_active": all(
                    row["status"] == "ACTIVE" for row in rows
                ),
                "formal_guards_unchanged": guards_unchanged,
            },
            "formal_guard_pre": guard_pre,
            "formal_guard_post": guard_post,
            "formal_admission": False,
            "stage_a_changed": False,
        }
        write_json(report_stage / "VERIFICATION.json", verification)

        report_files = sorted(
            path
            for path in report_stage.iterdir()
            if path.name != "REPORT_MANIFEST.json"
        )
        report_manifest = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "files": [
                {
                    **bind(path, workspace),
                    "path": (
                        report / path.relative_to(report_stage)
                    ).relative_to(workspace).as_posix(),
                }
                for path in report_files
            ],
        }
        write_json(report_stage / "REPORT_MANIFEST.json", report_manifest)

        candidate.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        candidate_stage.rename(candidate)
        try:
            report_stage.rename(report)
        except Exception:
            candidate.rename(candidate_stage)
            raise
        return candidate, report
    except Exception:
        if candidate_stage.exists():
            shutil.rmtree(candidate_stage)
        if report_stage.exists():
            shutil.rmtree(report_stage)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    build(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
