# Free web daily run health monitor v1

Status: `READ_ONLY__FAIL_CLOSED__INSPECTION_ONLY`

This contract verifies one already-created bounded dual-source probe run. It
does not access the network and does not write a receipt, mutate the run,
create a scheduler, publish a snapshot, write the catalog, or promote data.

The verifier requires an explicit target trading date and a run directory
strictly below `tmp/free-web-daily-probe/`:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  -X utf8 .\control\tools\verify_free_web_daily_probe_run.py `
  --run-root .\tmp\free-web-daily-probe\20260731-dual-source-v1 `
  --trade-date 2026-07-31
```

A zero exit code requires all of the following:

- the exact six-file v1 run layout is present with no unlisted file;
- `MANIFEST.json` binds byte counts and SHA-256 for all five business files;
- manifest and results remain `INSPECTION_ONLY` with every formal grant false;
- results, both raw sources, and every normalized row match the target date;
- Sina and Tencent each contain exactly the requested symbols in order;
- raw responses recompute to zero reconciliation findings;
- all requested symbols are accepted, and normalized rows exactly reproduce
  the recomputed dual-source rows;
- reparse points, symlinks, path escapes, malformed JSON/CSV, duplicate JSON
  keys, non-finite values, missing files, hash drift, count drift, date drift,
  semantic drift, or an authorization flag cause exit code `2`.

The success receipt is printed only to stdout. A failure receipt is printed
only to stderr. Neither is persisted. A PASS is a mechanical health result for
an inspection run; it is not source admission, a formal catalog identity, an
immutable research snapshot, or research readiness.
