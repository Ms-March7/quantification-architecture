#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capital_flow_attribution.open_source_adapters import AKShareAdapter, TushareAdapter  # noqa: E402
from capital_flow_attribution.reporting import write_json  # noqa: E402
from capital_flow_attribution.storage import file_sha256  # noqa: E402
from capital_flow_attribution.utils import now_iso  # noqa: E402


def _write_result(run_root: Path, result: object) -> dict[str, object]:
    source_id = result.source_id
    raw_path = run_root / "data" / "raw" / f"{source_id}_security_master.csv"
    normalized_path = run_root / "data" / "normalized" / f"{source_id}_security_master.csv"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    result.raw.to_csv(raw_path, index=False, encoding="utf-8-sig")
    result.normalized.to_csv(normalized_path, index=False, encoding="utf-8-sig")
    manifest = result.manifest()
    manifest.update(
        {
            "raw_path": raw_path.as_posix(),
            "raw_sha256": file_sha256(raw_path),
            "normalized_path": normalized_path.as_posix(),
            "normalized_sha256": file_sha256(normalized_path),
        }
    )
    return manifest


def _self_test() -> int:
    class FakeAKShare:
        @staticmethod
        def stock_info_a_code_name() -> pd.DataFrame:
            return pd.DataFrame([{"code": "600000", "name": "浦发银行"}, {"code": "430047", "name": "诺思兰德"}])

    class FakeTushareClient:
        @staticmethod
        def stock_basic(**_: object) -> pd.DataFrame:
            return pd.DataFrame([{"ts_code": "600000.SH", "symbol": "600000", "name": "浦发银行"}])

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        akshare_result = AKShareAdapter(FakeAKShare).security_master()
        tushare_result = TushareAdapter(token="test-only", client=FakeTushareClient()).security_master()
        manifests = [_write_result(root, akshare_result), _write_result(root, tushare_result)]
        if [item["normalized_rows"] for item in manifests] != [2, 1]:
            return 1
        if not all(Path(item["normalized_path"]).is_file() for item in manifests):
            return 1
    print(json.dumps({"self_test": "PASS"}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe approved optional A-share source adapters.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--online-akshare", action="store_true")
    parser.add_argument("--online-tushare", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return _self_test()
    if args.output_dir is None:
        parser.error("--output-dir is required outside --self-test")
    if args.output_dir.exists():
        raise FileExistsError(f"immutable probe run already exists: {args.output_dir}")
    run_root = args.output_dir
    manifests: list[dict[str, object]] = []
    errors: list[str] = []
    if args.online_akshare:
        try:
            manifests.append(_write_result(run_root, AKShareAdapter().security_master()))
        except Exception as exc:
            errors.append(f"akshare:{type(exc).__name__}:{exc}")
    if args.online_tushare:
        try:
            manifests.append(_write_result(run_root, TushareAdapter().security_master()))
        except Exception as exc:
            errors.append(f"tushare:{type(exc).__name__}:{exc}")
    payload = {
        "schema_version": "s1r-optional-source-probe-v1",
        "generated_at": now_iso(),
        "requested": {"akshare": args.online_akshare, "tushare": args.online_tushare},
        "sources": manifests,
        "errors": errors,
        "credentials_persisted": False,
        "research_use_only": True,
    }
    write_json(run_root / "data" / "manifest.json", payload)
    results = run_root / "results"
    results.mkdir(parents=True, exist_ok=True)
    lines = [
        "# S1R可选数据源探针",
        "",
        f"- 生成时间：`{payload['generated_at']}`",
        "- 官方源仍是规范依据；本报告只验证二级适配器可运行性",
        "- 未保存任何token或凭据",
        "",
        "| 来源 | 状态 | 原始行 | 标准化行 | 许可 | 说明 |",
        "|---|---|---:|---:|---|---|",
    ]
    for item in manifests:
        lines.append(
            f"| {item['source_id']} | {item['status']} | {item['raw_rows']} | {item['normalized_rows']} | {item['license_status']} | {item['note']} |"
        )
    for error in errors:
        lines.append(f"- ERROR: `{error}`")
    (results / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
