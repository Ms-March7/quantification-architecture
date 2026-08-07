import gc, importlib.util, json, tempfile, unittest, warnings
from pathlib import Path
WS=Path(__file__).resolve().parents[2]; SCRIPT=WS/"control/tools/build_stage_a_core02_calendar_session_closure_v3_clean.py"; REPORT=WS/"reports/data-governance/stage-a-core02-calendar-session-closure-20260731-v3-clean"; CANDIDATE=WS/"tmp/d2a04b-source-closure/stage-a-core02-calendar-session-closure-20260731-v3-clean"
spec=importlib.util.spec_from_file_location("v3",SCRIPT); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class Tests(unittest.TestCase):
 def test_constants_are_bounded(self): self.assertEqual(m.PURPOSE_CEILING,"INSPECTION_ONLY")
 def test_transport_unknown(self): self.assertEqual(m.TRANSPORT_BOUNDARY["proxy_and_credential_origin_status"],"UNKNOWN")
 def test_raw_count(self): self.assertEqual(len(m.RAW_FILES),7)
 def test_no_repair_dependency_in_source(self): self.assertNotIn("repair_history",m.inputs(WS)[0]["path"])
 def test_csv_recomputation(self): self.assertEqual(m.recompute_csv(WS/m.CSV_INPUT,WS)["q4_status"],"PASS")
 def test_csv_recomputation_closes_handle_without_resourcewarning(self):
  with warnings.catch_warnings(record=True) as caught:
   warnings.simplefilter("always", ResourceWarning)
   m.recompute_csv(WS/m.CSV_INPUT,WS)
   gc.collect()
  self.assertEqual([item for item in caught if issubclass(item.category,ResourceWarning)],[])
 def test_candidate_manifest(self): self.assertTrue((CANDIDATE/"CANDIDATE_MANIFEST.json").is_file() or __import__("os").environ.get("CORE02_V3_RECEIPT"))
 def test_gate_statuses(self):
  if REPORT.is_dir(): self.assertEqual({k:v["status"] for k,v in json.loads((REPORT/"CORE02_GATE_RECEIPT.json").read_text()).get("gates",{}).items()},{"CORE02-Q1":"FAIL","CORE02-Q2":"FAIL","CORE02-Q3":"UNKNOWN","CORE02-Q4":"PASS"})
 def test_semantic_matrix_matches_gate_and_has_no_old_binding(self):
  if REPORT.is_dir():
   matrix=json.loads((REPORT/"SUBCHECK_EVIDENCE_MATRIX.json").read_text(encoding="utf-8"))
   gate=json.loads((REPORT/"CORE02_GATE_RECEIPT.json").read_text(encoding="utf-8"))
   statuses={key:value["status"] for key,value in matrix["gates"].items()}
   self.assertEqual(statuses,{key:value["status"] for key,value in gate["gates"].items()})
   self.assertEqual(statuses,{"CORE02-Q1":"FAIL","CORE02-Q2":"FAIL","CORE02-Q3":"UNKNOWN","CORE02-Q4":"PASS"})
   serialized=json.dumps(matrix,ensure_ascii=False,sort_keys=True)
   self.assertNotIn("repair_history",serialized)
   self.assertNotIn("20260730-v2",serialized)
   self.assertEqual(matrix["source_facts"]["row_count"],8454)
 def test_validation_rejects_missing(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertRaises(Exception,lambda:m.validate(Path(d),Path(d)/"c",Path(d)/"r"))
 def test_verify_current_package(self):
  if REPORT.is_dir() and (REPORT/"TEST_RESULTS.json").is_file(): self.assertEqual(m.validate(WS,CANDIDATE,REPORT)["status"],"PASS")
if __name__=="__main__": unittest.main()
