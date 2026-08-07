# Scheduler plan generator v1

Status: `INSPECTION_ONLY__PLAN_ONLY__NO_TASK_REGISTRATION`

This contract generates and validates deterministic Windows Task Scheduler XML
plans for the existing local inspection worker. It never installs, enables or
runs a task; never launches a child process; never opens a network connection;
and never writes formal data, the catalog, admission state or quality status.

Each job is one explicit date from the frozen
`local_daily_orchestration_v1.calendar_dates` list. The XML uses an ISO-8601
`+08:00` `TimeTrigger`, while the manifest binds `Asia/Shanghai` to Windows
`China Standard Time`. This avoids pretending that a Monday-Friday recurrence
is an exchange trading calendar.

The generated command is fixed to
`run_local_daily_inspection_worker.py --execute`. Its Python runtime, worker,
orchestration engine, local-update engine and both request files are SHA-256
bound in the plan. The account remains the literal
`__WINDOWS_ACCOUNT_PLACEHOLDER__`; a real account or any credential is rejected.
Any future installation or account materialization requires a separate approval
and a newly generated plan.

Missed-run and exclusion semantics are explicit but bounded:

- `StartWhenAvailable=true` asks Windows to replay a missed one-time trigger;
- fixed claim/finish event IDs make the same date idempotent;
- the orchestration database still enforces the earliest unfinished date;
- `MultipleInstancesPolicy=IgnoreNew` is the outer mutex;
- SQLite `BEGIN IMMEDIATE`, one active claim and its lease are the inner mutex;
- this plan does not create inbox data, retry delivery or a downloader.

Default generation writes only canonical JSON, including the XML, to stdout:

```powershell
$python = 'control\runtime\data-engineering\.venv\Scripts\python.exe'
& $python control\tools\generate_windows_scheduler_plan.py generate `
  --request data\catalog\contracts\scheduler_plan_generator_v1\example.request.json
```

An explicit write requires both flags and is confined to a new, atomically
published, non-overwriting child of `tmp/scheduler-plan-generator/`:

```powershell
& $python control\tools\generate_windows_scheduler_plan.py generate `
  --request path\to\request.json --write-plan `
  --output-dir tmp\scheduler-plan-generator\review-plan-v1

& $python control\tools\generate_windows_scheduler_plan.py verify `
  --bundle-dir tmp\scheduler-plan-generator\review-plan-v1
```

The XML is review material only. A PASS verifies syntax, exact commands,
request/tool hashes, placeholder account, date/time binding and safety flags;
it is not a scheduler, source-admission, production-update or research gate.
