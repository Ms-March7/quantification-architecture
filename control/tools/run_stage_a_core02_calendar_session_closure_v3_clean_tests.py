#!/usr/bin/env python3
"""Run the clean v3 CORE-02 tests in a real local subprocess."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess
from pathlib import Path
GENERATOR="control/tools/build_stage_a_core02_calendar_session_closure_v3_clean.py"; TEST="control/tests/test_build_stage_a_core02_calendar_session_closure_v3_clean.py"; RUNNER="control/tools/run_stage_a_core02_calendar_session_closure_v3_clean_tests.py"; PYTHON="control/runtime/data-engineering/.venv/Scripts/python.exe"
def b(ws,p):
    x=ws/p; return {"path":p,"bytes":x.stat().st_size,"sha256":hashlib.sha256(x.read_bytes()).hexdigest()}
def main():
    p=argparse.ArgumentParser(); p.add_argument("--workspace",required=True); p.add_argument("--output",required=True); a=p.parse_args(); ws=Path(a.workspace).resolve(); out=Path(a.output).resolve()
    expected=ws/"reports/data-governance/stage-a-core02-calendar-session-closure-20260731-v3-clean/TEST_RESULTS.json"
    if out!=expected: raise ValueError("output must be final clean v3 TEST_RESULTS.json")
    command=[str(ws/PYTHON),"-W","error::ResourceWarning",str(ws/TEST)]
    r=subprocess.run(command,cwd=ws,capture_output=True,text=True,check=False,env={**os.environ,"CORE02_V3_RECEIPT":"1"}); m=re.search(r"Ran (\d+) tests?",r.stderr); count=int(m.group(1)) if m else 0
    value={"schema_version":1,"operation_mode":"LOCAL_SUBPROCESS_UNITTEST","exact_command":command,"warnings_policy":"error::ResourceWarning","exit_code":r.returncode,"tests_run":count,"status":"PASS" if r.returncode==0 and count==11 else "FAIL","network_requests_performed":0,"zero_network_declaration":True,"stdout_summary":r.stdout[-4000:],"stderr_summary":r.stderr[-4000:],"artifacts":{"builder":b(ws,GENERATOR),"test":b(ws,TEST),"runner":b(ws,RUNNER)}}
    out.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return 0 if value["status"]=="PASS" else 1
if __name__=="__main__": raise SystemExit(main())
