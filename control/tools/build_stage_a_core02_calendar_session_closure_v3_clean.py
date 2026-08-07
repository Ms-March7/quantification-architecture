#!/usr/bin/env python3
"""Create a clean, offline and fail-closed CORE-02 Stage-A evidence package.

This builder deliberately does not read v2 repair_history or any historical
builder/test byte binding.  It copies only still-present raw official response
bytes and recomputes bounded CSV checks.  No network operation is implemented.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

RUN_ID = "stage-a-core02-calendar-session-closure-20260731-v3-clean"
PURPOSE_CEILING = "INSPECTION_ONLY"
QUALITY_STATUS = "NOT_ADMITTED"
DEFAULT_CANDIDATE = f"tmp/d2a04b-source-closure/{RUN_ID}"
DEFAULT_REPORT = f"reports/data-governance/{RUN_ID}"
V2_CANDIDATE = "tmp/d2a04b-source-closure/stage-a-core02-calendar-session-closure-20260730-v2"
CSV_INPUT = "tmp/d2a04b-source-closure/core02-official-calendar-session-closure-20260730-v1/TRADING_DAY_CANDIDATE.csv"
GENERATOR = "control/tools/build_stage_a_core02_calendar_session_closure_v3_clean.py"
TEST = "control/tests/test_build_stage_a_core02_calendar_session_closure_v3_clean.py"
RUNNER = "control/tools/run_stage_a_core02_calendar_session_closure_v3_clean_tests.py"
PYTHON = "control/runtime/data-engineering/.venv/Scripts/python.exe"
RAW_FILES = (
    "bse/calendar/2024_annual_notice.html",
    "sse/calendar/2013_annual_notice.html",
    "sse/calendar/2014_annual_notice.html",
    "szse/calendar/2013_annual_notice.html",
    "szse/calendar/2014_dragon_boat_notice.html",
    "szse/calendar/2014_mid_autumn_national_notice.html",
    "szse/rules/2006_trade_rule_notice.html",
)
GUARDED = ("data/catalog/datasets.json", "data/catalog/contracts/source_admission_v1/current_source_registry.json", "control/ROADMAP.md", "control/projects.yaml", "control/WORKSPACE_INDEX.md")
GUARDED_TREES = ("data/market/snapshots", "data/published")
TRANSPORT_BOUNDARY = {"application_explicit_credentials_configured": False, "application_explicit_authorization_header_configured": False, "application_explicit_cookie_configured": False, "proxy_and_credential_origin_status": "UNKNOWN", "environment_proxy_observed": "UNKNOWN", "netrc_observed": "UNKNOWN", "direct_connection_claimed": False, "absolute_no_credentials_claimed": False, "network_requests_performed": 0}

def cjson(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1048576), b""): h.update(chunk)
    return h.hexdigest()

def rel(path: Path, ws: Path) -> str: return path.resolve().relative_to(ws.resolve()).as_posix()
def binding(path: Path, ws: Path) -> dict[str, Any]: return {"path":rel(path,ws),"bytes":path.stat().st_size,"sha256":sha(path)}
def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def load(path: Path) -> Any: return json.loads(path.read_text(encoding="utf-8"))

def inputs(ws: Path) -> list[dict[str, Any]]:
    return [{"role": role, **binding(ws / value, ws)} for role,value in (("GENERATOR",GENERATOR),("TEST_SOURCE",TEST),("TEST_RUNNER",RUNNER))]
def guarded(ws: Path) -> dict[str, Any]:
    result={"files":{p:binding(ws/p,ws) for p in GUARDED},"trees":{}}
    for root in GUARDED_TREES:
        files=[binding(p,ws) for p in sorted((ws/root).rglob("*")) if p.is_file()]
        result["trees"][root]={"file_count":len(files),"tree_sha256":hashlib.sha256(cjson(files)).hexdigest()}
    return result
def files(root: Path) -> list[Path]: return sorted(p for p in root.rglob("*") if p.is_file())
def disk_set(root: Path) -> set[str]: return {p.relative_to(root).as_posix() for p in files(root)}

def recompute_csv(path: Path, ws: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows=list(csv.DictReader(handle))
    keys=[(r.get("venue"),r.get("calendar_date")) for r in rows]
    venues={r.get("venue") for r in rows}
    counts={v:sum(r.get("venue")==v for r in rows) for v in sorted(venues)}
    bad_weekend=[r for r in rows if r.get("is_open") in ("1","true","True") and __import__("datetime").date.fromisoformat(r["calendar_date"]).weekday()>=5]
    return {"input":binding(path,ws),"row_count":len(rows),"unique_venue_date_keys":len(set(keys)),"duplicate_key_count":len(keys)-len(set(keys)),"venue_counts":counts,"weekend_open_count":len(bad_weekend),"q3_status":"UNKNOWN","q3_reason":"BSE rows, complete historical boundaries, previous/next trading-day fields and period-end markers are not closed.","q4_status":"PASS" if venues=={"SSE","SZSE"} and len(keys)==len(set(keys)) and not bad_weekend else "FAIL"}

def manifest(root: Path, ws: Path, name: str, build_inputs: list[dict[str,Any]]) -> None:
    items=[binding(p,ws) for p in files(root) if p.name not in {name,"VERIFICATION.json"}]
    write(root/name,{"schema_version":1,"run_id":RUN_ID,"strict_disk_set_excludes":[name,"VERIFICATION.json"],"build_inputs":build_inputs,"files":items})

def validate(ws: Path, candidate: Path, report: Path) -> dict[str,Any]:
    errors=[]
    for root,name in ((candidate,"CANDIDATE_MANIFEST.json"),(report,"REPORT_MANIFEST.json")):
        m=load(root/name); expected={x["path"] for x in m["files"]}; actual={rel(p,ws) for p in files(root) if p.name not in {name,"VERIFICATION.json"}}
        if expected != actual: errors.append(f"manifest_set:{rel(root,ws)}")
        for item in m["files"]:
            p=ws/item["path"]
            if not p.is_file() or binding(p,ws)!={k:item[k] for k in ("path","bytes","sha256")}: errors.append(f"binding:{item['path']}")
        if m.get("build_inputs") != inputs(ws): errors.append(f"inputs:{rel(root,ws)}")
    gate=load(report/"CORE02_GATE_RECEIPT.json")
    wanted={"CORE02-Q1":"FAIL","CORE02-Q2":"FAIL","CORE02-Q3":"UNKNOWN","CORE02-Q4":"PASS"}
    if {k:v["status"] for k,v in gate["gates"].items()} != wanted: errors.append("gate_status")
    if gate.get("purpose_ceiling")!=PURPOSE_CEILING or gate.get("quality_status")!=QUALITY_STATUS or gate.get("formal_admission") is not False: errors.append("admission_boundary")
    if (report/"TEST_RESULTS.json").is_file():
        t=load(report/"TEST_RESULTS.json")
        if t.get("status")!="PASS" or t.get("tests_run")!=11: errors.append("test_receipt")
    matrix_path=report/"SUBCHECK_EVIDENCE_MATRIX.json"
    if not matrix_path.is_file():
        errors.append("semantic_matrix_missing")
    else:
        matrix=load(matrix_path)
        matrix_statuses={
            key:value["status"] for key,value in matrix.get("gates",{}).items()
        }
        if matrix_statuses != wanted:
            errors.append("semantic_matrix_status")
        if matrix_statuses != {
            key:value["status"] for key,value in gate["gates"].items()
        }:
            errors.append("semantic_matrix_gate_mismatch")
        matrix_text=json.dumps(matrix,ensure_ascii=False,sort_keys=True)
        if "repair_history" in matrix_text or "20260730-v2" in matrix_text:
            errors.append("semantic_matrix_old_binding")
    return {"schema_version":1,"run_id":RUN_ID,"operation_mode":"VERIFY_ONLY_READ_ONLY","status":"PASS" if not errors else "FAIL","errors":errors,"transport_boundary":TRANSPORT_BOUNDARY}

def clear_existing_clean_output(ws: Path, candidate: Path, report: Path) -> None:
    """Remove only the exact previously generated clean-v3 file sets."""
    expected_candidate = {
        "CANDIDATE_MANIFEST.json",
        "REQUEST_RECEIPTS.json",
        "SOURCE_MANIFEST.json",
        *(f"raw/{item}" for item in RAW_FILES),
    }
    expected_report = {
        "CLEAN_REBUILD_BOUNDARY.json",
        "CORE02_GATE_RECEIPT.json",
        "Q3_Q4_LIVE_RECOMPUTE.json",
        "README.md",
        "REPORT_MANIFEST.json",
        "SUBCHECK_EVIDENCE_MATRIX.json",
        "TEST_RESULTS.json",
        "VERIFICATION.json",
    }
    exact_candidate=(ws/DEFAULT_CANDIDATE).resolve()
    exact_report=(ws/DEFAULT_REPORT).resolve()
    if candidate != exact_candidate or report != exact_report:
        raise ValueError("rebuild-existing is restricted to exact clean-v3 roots")
    for root, expected in ((candidate, expected_candidate), (report, expected_report)):
        if not root.is_dir() or root.is_symlink():
            raise FileNotFoundError(f"clean-v3 root missing or linked: {root}")
        actual=disk_set(root)
        accepted_sets=(expected, expected-{"SUBCHECK_EVIDENCE_MATRIX.json"})
        if actual not in accepted_sets:
            raise RuntimeError(
                f"refusing cleanup with unexpected file set: "
                f"missing={sorted(expected-actual)} extra={sorted(actual-expected)}"
            )
        for item in sorted(actual):
            (root/item).unlink()
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.rmdir()
        root.rmdir()

def build(ws: Path, candidate: Path, report: Path) -> None:
    if candidate.exists() or report.exists(): raise FileExistsError("refusing to overwrite output")
    before=guarded(ws); candidate.mkdir(parents=True); report.mkdir(parents=True)
    source_raw=ws/V2_CANDIDATE/"raw"
    imported=[]
    for item in RAW_FILES:
        src=source_raw/item
        if not src.is_file(): raise FileNotFoundError(src)
        dst=candidate/"raw"/item; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,dst)
        if sha(src)!=sha(dst): raise RuntimeError("raw copy hash mismatch")
        imported.append({"source":binding(src,ws),"copied":binding(dst,ws)})
    metrics=recompute_csv(ws/CSV_INPUT,ws)
    build_inputs=inputs(ws)
    source={"schema_version":1,"run_id":RUN_ID,"operation_mode":"OFFLINE_REBUILD_FROM_STILL_PRESENT_BYTES","network_requests_performed":0,"transport_boundary":TRANSPORT_BOUNDARY,"explicit_non_dependencies":["reports/data-governance/stage-a-core02-calendar-session-closure-20260730-v2/repair_history","historical builder/test byte bindings"],"raw_imports":imported,"calendar_csv":metrics["input"],"build_inputs":build_inputs}
    write(candidate/"SOURCE_MANIFEST.json",source); write(candidate/"REQUEST_RECEIPTS.json",{"schema_version":1,"run_id":RUN_ID,"network_requests_performed":0,"raw_response_count":len(imported),"status":"OFFLINE_IMPORTED_BYTES_ONLY","transport_boundary":TRANSPORT_BOUNDARY}); manifest(candidate,ws,"CANDIDATE_MANIFEST.json",build_inputs)
    gate={"schema_version":1,"run_id":RUN_ID,"target":"CORE-02","purpose_ceiling":PURPOSE_CEILING,"quality_status":QUALITY_STATUS,"formal_admission":False,"research_usable":False,"transport_boundary":TRANSPORT_BOUNDARY,"decision":"FAIL_CLOSED__Q1_FAIL__Q2_FAIL__Q3_UNKNOWN__Q4_PASS__STAGE_A_NOT_READY__NOT_ADMITTED","gates":{"CORE02-Q1":{"status":"FAIL","reason":"BSE official daily calendar and complete three-venue historical session chain remain unclosed."},"CORE02-Q2":{"status":"FAIL","reason":"Complete three-venue temporary-closure archive and publication/revision chain remain unclosed."},"CORE02-Q3":{"status":"UNKNOWN","reason":metrics["q3_reason"]},"CORE02-Q4":{"status":metrics["q4_status"],"reason":"CSV recomputation preserved explicit venue keys without duplicate composite keys or weekend-open rows."}}}
    write(report/"CORE02_GATE_RECEIPT.json",gate); write(report/"Q3_Q4_LIVE_RECOMPUTE.json",metrics)
    matrix={
        "schema_version":1,
        "run_id":RUN_ID,
        "target":"CORE-02",
        "projection_scope":"CLEAN_V3_LOCAL_CURRENT_PROJECTION",
        "evidence_boundary":"Generated from the current clean-v3 raw import inventory and live recomputation of the currently present 8,454-row SSE/SZSE CSV. It makes no claim about an old 28-row evidence set.",
        "source_facts":{
            "calendar_csv":metrics["input"],
            "row_count":metrics["row_count"],
            "venue_counts":metrics["venue_counts"],
            "duplicate_key_count":metrics["duplicate_key_count"],
            "weekend_open_count":metrics["weekend_open_count"],
            "current_raw_response_count":len(imported),
        },
        "gates":{
            "CORE02-Q1":{
                "status":"FAIL",
                "subchecks":[{
                    "subcheck_id":"CORE02-Q1.clean_v3_three_venue_history_closure",
                    "status":"FAIL",
                    "basis":"The current clean-v3 inputs do not contain a complete BSE daily calendar or a complete three-venue historical session-version chain.",
                }],
            },
            "CORE02-Q2":{
                "status":"FAIL",
                "subchecks":[{
                    "subcheck_id":"CORE02-Q2.clean_v3_temporary_closure_revision_pit_chain",
                    "status":"FAIL",
                    "basis":"The current clean-v3 inputs do not close the complete three-venue temporary-closure archive, notice revision chain, or publication-time PIT chain.",
                }],
            },
            "CORE02-Q3":{
                "status":"UNKNOWN",
                "subchecks":[{
                    "subcheck_id":"CORE02-Q3.clean_v3_full_calendar_derivations",
                    "status":"UNKNOWN",
                    "basis":"The current projection lacks BSE coverage, complete historical boundaries, previous/next trading-day fields, and month/quarter/year-end markers.",
                }],
            },
            "CORE02-Q4":{
                "status":"PASS",
                "subchecks":[{
                    "subcheck_id":"CORE02-Q4.clean_v3_current_venue_preservation",
                    "status":"PASS",
                    "basis":f"Live recomputation of {metrics['row_count']} current SSE/SZSE rows found {metrics['duplicate_key_count']} duplicate venue-date keys and {metrics['weekend_open_count']} weekend-open rows, with the venue dimension preserved.",
                }],
            },
        },
        "purpose_ceiling":PURPOSE_CEILING,
        "quality_status":QUALITY_STATUS,
        "formal_admission":False,
    }
    write(report/"SUBCHECK_EVIDENCE_MATRIX.json",matrix)
    write(report/"CLEAN_REBUILD_BOUNDARY.json",{"schema_version":1,"run_id":RUN_ID,"no_network":True,"no_repair_history_dependency":True,"no_old_script_byte_dependency":True,"imported_raw_count":len(imported),"transport_origin":"UNKNOWN","purpose_ceiling":PURPOSE_CEILING,"quality_status":QUALITY_STATUS})
    (report/"README.md").write_text("# CORE-02 Stage-A clean v3 package\n\nOffline rebuild from seven still-present official raw response bytes. No repair history is included or trusted. Q1/Q2 are FAIL, Q3 is UNKNOWN, Q4 is PASS; this is INSPECTION_ONLY and NOT_ADMITTED.\n",encoding="utf-8")
    manifest(report,ws,"REPORT_MANIFEST.json",build_inputs)
    command=[str(ws/PYTHON),str(ws/RUNNER),"--workspace",str(ws),"--output",str(report/"TEST_RESULTS.json")]
    completed=subprocess.run(command,cwd=ws,capture_output=True,text=True,check=False)
    if completed.returncode: raise RuntimeError("subprocess test receipt failed: "+completed.stderr[-1000:])
    # TEST_RESULTS is produced by the real subprocess and must itself be
    # covered by the final strict report manifest.
    manifest(report,ws,"REPORT_MANIFEST.json",build_inputs)
    provisional=validate(ws,candidate,report)
    if provisional["status"]!="PASS": raise RuntimeError("post-build validation failed: "+repr(provisional["errors"]))
    after=guarded(ws)
    if before!=after: raise RuntimeError("guarded authority changed")
    verification={**provisional,"guarded_authority_unchanged":True,"guarded_before":before,"guarded_after":after,"candidate_manifest":binding(candidate/"CANDIDATE_MANIFEST.json",ws),"report_manifest":binding(report/"REPORT_MANIFEST.json",ws),"test_results":binding(report/"TEST_RESULTS.json",ws)}
    write(report/"VERIFICATION.json",verification)

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--workspace",default="."); p.add_argument("--candidate",default=DEFAULT_CANDIDATE); p.add_argument("--report",default=DEFAULT_REPORT); p.add_argument("--verify-only",action="store_true"); p.add_argument("--rebuild-existing",action="store_true"); a=p.parse_args()
    ws=Path(a.workspace).resolve(); candidate=(ws/a.candidate).resolve(); report=(ws/a.report).resolve()
    if a.verify_only and a.rebuild_existing:
        raise ValueError("verify-only and rebuild-existing are mutually exclusive")
    if a.verify_only:
        result=validate(ws,candidate,report); print(json.dumps(result,ensure_ascii=False,sort_keys=True)); return 0 if result["status"]=="PASS" else 1
    if a.rebuild_existing:
        clear_existing_clean_output(ws,candidate,report)
    build(ws,candidate,report); return 0
if __name__ == "__main__": raise SystemExit(main())
