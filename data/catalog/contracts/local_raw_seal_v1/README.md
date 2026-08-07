# Local raw seal v1

Status: `INSPECTION_ONLY__EXPLICIT_LOCAL_INPUTS__NO_FORMAL_EFFECT`

This contract closes one mechanical gap between an already-landed bounded web
probe and later normalization: it creates a write-once, hash-bound copy of
explicitly named raw response files. It is not a downloader, source adapter,
production raw writer, formal snapshot publisher, catalog writer, scheduler,
or research-data admission path.

The engine is `control/tools/run_local_raw_seal.py`. Every source file must be
passed separately as:

```text
SOURCE_ID|workspace-relative-path|lowercase-sha256|bytes
```

The engine never enumerates a probe directory to discover inputs. All inputs
must be ordinary non-link files directly below one exact
`tmp/free-web-daily-probe/<source_probe_run_id>/raw/` directory. The source run
identity, path, byte count and SHA-256 must all match before any output is
created, and are checked again while copying. Source files are neither changed
nor deleted.

Default execution is a read-only plan. A bounded materialization example is:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  -X utf8 .\control\tools\run_local_raw_seal.py `
  --seal-run-id 20260731-dual-source-seal-v1 `
  --source-probe-run-id 20260731-dual-source-v1 `
  --input-spec "SINA_WEB_QUOTE|tmp/free-web-daily-probe/20260731-dual-source-v1/raw/sina.response|c475afa8e9cd58813878e64b18811f2c782e0a8a2fe5010896d3912a0b875254|818" `
  --input-spec "TENCENT_WEB_QUOTE|tmp/free-web-daily-probe/20260731-dual-source-v1/raw/tencent.response|ea640c605db1c4a65004259927ad29ca04280de4f57ce1764dc40b0d4d4490d1|1505" `
  --output .\tmp\local-raw-seal\20260731-dual-source-seal-v1 `
  --execute
```

The final directory must not already exist. The engine writes a sibling
staging directory, verifies byte identity, writes canonical
`SEAL_MANIFEST.json` plus `SEAL_MANIFEST.sha256`, and renames the staging
directory into place. It never overwrites an earlier seal.

This is only an immutable-style evidence bundle under `tmp/`. It does not
close source rights, retention, field semantics, PIT, revision, full-market
coverage, or status-history gaps. Every result retains:

- `purpose_ceiling=INSPECTION_ONLY`
- `formal_source_admission=false`
- `formal_catalog_written=false`
- `quality_promotion=false`
- `production_raw_store_written=false`
- `scheduler_created=false`
- `research_ready=false`
- `formal_effect=NONE`

A PASS therefore proves only that the exact provided bytes were sealed without
mutating their sources. It is not `RAW_SEALED` in the formal production state
machine and is not a research snapshot.
