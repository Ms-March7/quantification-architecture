#!/usr/bin/env python3
"""Run CORE-02 v2 unittest locally and emit its machine receipt."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess
from pathlib import Path

GENERATOR="control/tools/build_stage_a_core02_calendar_session_closure_v2.py"
TEST="control/tests/test_build_stage_a_core02_calendar_session_closure_v2.py"
RUNNER="control/tools/run_stage_a_core02_calendar_session_closure_v2_tests.py"
PYTHON="control/runtime/data-engineering/.venv/Scripts/python.exe"

def binding(workspace: Path, relative: str) -> dict[str, object]:
    path=workspace / relative
    return {"path":relative,"bytes":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace",required=True); parser.add_argument("--output",required=True); parser.add_argument("--bootstrap",action="store_true")
    args=parser.parse_args(); workspace=Path(args.workspace).resolve(); output=Path(args.output).resolve()
    report=workspace / "reports/data-governance/stage-a-core02-calendar-session-closure-20260730-v2"
    if output.parent != report or output.name != "TEST_RESULTS.json": raise ValueError("output must be final CORE-02 TEST_RESULTS.json")
    command=[str(workspace / PYTHON),str(workspace / TEST)]
    environment=os.environ.copy()
    environment["CORE02_TEST_RECEIPT_RUNNER"]="1"
    if args.bootstrap: environment["CORE02_TEST_RECEIPT_BOOTSTRAP"]="1"
    result=subprocess.run(command,cwd=workspace,capture_output=True,text=True,check=False,env=environment)
    match=re.search(r"Ran (\d+) tests?",result.stderr); tests_run=int(match.group(1)) if match else 0
    receipt={"schema_version":1,"operation_mode":"LOCAL_SUBPROCESS_UNITTEST","exact_command":command,"bootstrap":args.bootstrap,"exit_code":result.returncode,"tests_run":tests_run,"status":"PASS" if result.returncode == 0 and tests_run == 15 else "FAIL","stdout_summary":result.stdout[-4000:],"stderr_summary":result.stderr[-4000:],"network_requests_performed":0,"zero_network_declaration":True,"network_boundary":"runner performs no network operation; it only starts the local unittest subprocess","artifacts":{"builder":binding(workspace,GENERATOR),"test":binding(workspace,TEST),"runner":binding(workspace,RUNNER)}}
    output.write_text(json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return 0 if receipt["status"] == "PASS" else 1
if __name__ == "__main__": raise SystemExit(main())
