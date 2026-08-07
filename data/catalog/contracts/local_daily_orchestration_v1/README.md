# Local daily orchestration state v1

Status: `INSPECTION_ONLY__STATE_LAYER_ONLY__NO_SCHEDULER`

This contract provides a persistent local state machine around already
authorized inspection work. It does not download data, execute a worker
command, create a scheduled task, write `data/market`, mutate
`data/catalog/datasets.json`, publish a snapshot or promote quality.

The state layer stores only:

- one frozen job request;
- an explicit, sorted calendar date list;
- a contiguous success watermark;
- per-date state and bounded attempt count;
- single active claim and expiring lease;
- every claim, completion, failure and recovery attempt;
- an append-only, idempotency-keyed event ledger.

SQLite `BEGIN IMMEDIATE` is the single-writer guard. Every mutation is one
database transaction. A repeated `event_id` with the same command fingerprint
returns the original receipt as `NO_CHANGE`; reuse with different content
fails closed.

Missed runs are replayed strictly from the earliest unfinished date. Later
dates cannot skip an active or terminally failed earlier date. A worker crash
is represented by an expired lease. Recovery must explicitly name the exact
date and claim token; it invalidates the old claim before that date can be
claimed again.

## Commands

```powershell
$python = 'control\runtime\data-engineering\.venv\Scripts\python.exe'
$tool = 'control\tools\run_local_daily_orchestration.py'
$request = 'data\catalog\contracts\local_daily_orchestration_v1\example.request.json'

& $python $tool init --request $request
& $python $tool plan --request $request --through-date 2026-08-03
& $python $tool claim --request $request --event-id claim-20260803 `
  --worker-id local-worker --through-date 2026-08-03
```

`claim` only issues an operational token. It does not run
`run_daily_update_local_smoke.py` or any other process. A separate authorized
caller may perform bounded local work and then report its immutable result
hash through `complete`; otherwise it records `fail`. Expired claims require
`recover` before replay.

The shipped bounded caller for already-landed, already-normalized input is
`control/tools/run_local_daily_inspection_worker.py`. It composes this state
machine with `run_daily_update_local_smoke.py`; it does not add a downloader,
scheduler, credential surface or formal publisher. Its default is a write-free
plan. One explicit execution example is:

```powershell
& $python control\tools\run_local_daily_inspection_worker.py `
  --orchestration-request path\to\orchestration.request.json `
  --update-request path\to\daily-update.request.json `
  --inbox path\to\one-day-normalized-delta `
  --through-date 2026-08-03 `
  --worker-id bounded-local-worker `
  --claim-event-id claim-20260803-v1 `
  --finish-event-id finish-20260803-v1 `
  --execute
```

The two requests must have identical `dataset_scope`, `dataset_id` and
`source_id`. The explicit inbox must contain exactly one logical input date,
that date must be in the frozen calendar, and it must be the earliest
unfinished date. A later input cannot consume the retry budget of an earlier
missing date. Success binds the sealed candidate `manifest.json` hash to
`complete`; quarantine or a worker failure is recorded through `fail`.

All runtime state is confined to:

```text
tmp/local-daily-orchestration/<job_id>/orchestrator.sqlite3
```

The shipped example is a synthetic fixture identity. Do not initialize it in
the real workspace merely to test the contract; the unit tests use isolated
temporary workspaces.

## Explicit non-goals

- no network or source adapter;
- no credential, cookie or token handling;
- no Windows Task Scheduler or other scheduler creation;
- no arbitrary command execution;
- no automatic source admission or rights decision;
- no formal raw/curated/snapshot/catalog write;
- no production compaction, alert delivery or research input.

Activation for a real data source still requires source-specific rights,
incremental/PIT/revision semantics, formal admission and a separately
authorized production publisher.
