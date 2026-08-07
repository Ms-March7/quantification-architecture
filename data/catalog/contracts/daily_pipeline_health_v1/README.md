# daily_pipeline_health_v1

Status: `READ_ONLY__FAIL_CLOSED__INSPECTION_ONLY`

This contract checks one already-created `local_daily_orchestration_v1`
SQLite state and only the candidate manifests/quarantine receipts explicitly
listed in a health request. It never searches for inputs, accesses a remote
source, sends alerts, creates a schedule, changes state, writes the formal
catalog, publishes a snapshot or promotes quality.

The report contains the contiguous watermark and its due-date/calendar lag,
the earliest unfinished date, terminal failures, expired leases (never claim
tokens), orchestration state drift, candidate hash/row/coverage drift, and a
quarantine reason summary.

Default execution prints one report to stdout:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe -X utf8 `
  .\control\tools\run_daily_pipeline_health.py `
  --health-request .\tmp\daily-pipeline-health-request\example.json
```

An optional persistent inspection receipt must be exactly
`tmp/daily-pipeline-health/<run_id>.json`. It is published once from a complete
same-volume temporary file and an existing destination is never overwritten.

Exit `0` means only `PASS_HEALTHY_INSPECTION_ONLY`. Exit `3` means a complete
health report found lag/failure/lease/state/candidate/quarantine drift. Exit
`2` means the request or its bindings were invalid and no report was written.
A PASS is not source admission, formal registration, an immutable research
snapshot, quality promotion, or research readiness.
