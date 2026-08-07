# Local daily update candidate v1

Status: `INSPECTION_ONLY__NO_NETWORK__NO_FORMAL_CATALOG_WRITE`

This contract standardizes a bounded local incremental merge for data that is
already normalized into UTF-8 CSV or JSON Lines. It is not a source downloader,
source adapter, production publisher, scheduler, compactor, or formal catalog
writer.

The engine is:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  .\control\tools\run_daily_update_local_smoke.py `
  --request .\data\catalog\contracts\local_daily_update_v1\example.request.json `
  --inbox .\tmp\local-daily-update-inbox
```

The default is a write-free plan. Materialization requires `--execute`:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  .\control\tools\run_daily_update_local_smoke.py `
  --request .\path\to\request.json `
  --inbox .\path\to\normalized-deltas `
  --execute
```

Successful execution may write only below
`tmp/local-daily-update/<dataset_id>/<run_id>/`. Failures may write a receipt
below `tmp/local-daily-update/quarantine/`. Both locations remain temporary,
inspection-only candidate storage.

## Input boundary

- `dataset_scope=synthetic_fixture` requires a synthetic/fixture source ID.
- `dataset_scope=registered_inspection` requires `dataset_id` to exist in the
  read-only formal catalog, but this grants no right to read or mutate a formal
  snapshot. It only binds the candidate identity to an existing catalog row.
- The inbox must be an explicit ordinary directory below the workspace and may
  not contain reparse points.
- Only files matching `include_patterns` and ending in `.csv` or `.jsonl` are
  considered.
- Inputs must already be normalized. Encoding is UTF-8 and CSV delimiter is
  explicit. Source-specific decoding, field mapping and unit conversion belong
  in an admitted adapter or the local downloaded-data builder.
- One request describes exactly one logical table and exact schema. Unknown
  fields, missing required values, cast failures, invalid dates/timestamps,
  duplicate/conflicting primary keys and invalid revision order fail closed.

## Merge and revision rules

- Exact duplicate rows with the same primary key are idempotently removed.
- The same primary key with different content is a conflict and the run is
  quarantined.
- Without `table.revision`, a business key may have only one row.
- With `table.revision`, the integer `revision_column` must be part of the
  primary key; revisions for each business key must strictly increase together
  with timezone-aware `available_at`.
- A base candidate is optional. When present it must be a previously sealed
  candidate from this engine under `tmp/local-daily-update/`. Its manifest and
  payload hashes are reverified before use. The manifest is also the exact file
  allowlist: missing or unlisted files fail closed, and the merge reads only
  Parquet files explicitly declared in its curated payload inventory.
- An identical sealed run returns `NO_CHANGE`. A changed payload attempting to
  reuse the same `dataset_id + run_id` is quarantined and the existing candidate
  remains unchanged.

## Output boundary

The engine writes a sibling staging directory, verifies every payload and
receipt, then uses same-volume `os.replace` to seal:

```text
tmp/local-daily-update/<dataset_id>/<run_id>/
|-- raw/incoming/
|-- curated/schema=<version>/year=<YYYY>/month=<MM>/part-000.parquet
|-- request.json
|-- manifest.json
|-- provenance.json
|-- partition-manifest.json
|-- quality/summary.json
|-- receipts/update.json
|-- query.sql
`-- REPORT.md
```

All outputs are:

- `purpose_ceiling=INSPECTION_ONLY`
- `source_admission=false`
- `catalog_registered=false`
- `quality_promotion=false`
- `research_ready=false`

A mechanical PASS does not prove source rights, source semantics, PIT
completeness, coverage, official cross-source agreement or research quality.
Formal use still requires a separately approved source admission, production
publisher, immutable formal snapshot and catalog registration.

## Optional bounded orchestration connector

`control/tools/run_local_daily_inspection_worker.py` connects an explicit
one-date normalized inbox to this engine and the separate
`local_daily_orchestration_v1` state machine. It reuses this request unchanged,
requires exact dataset/source/scope binding, and remains
`INSPECTION_ONLY`. It is not a downloader, production updater, scheduler or
formal admission path.
