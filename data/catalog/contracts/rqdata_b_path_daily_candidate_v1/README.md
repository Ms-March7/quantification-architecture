# RQData B-path daily candidate updater v1

This contract drives `control/tools/run_rqdata_b_path_daily_update.py`.

The updater creates immutable, hash-sealed **quarantine candidates only**.  It
does not edit `datasets.json`, the Roadmap or the source registry; it does not
promote quality, publish a formal snapshot, run factors or create a scheduled
task.  `--plan` performs no network request and no filesystem write.

Each successful run covers at most five missing SH/SZ trading dates and seals:

- the latest RQData common-stock master plus listing/delisting attributes and
  share-transformation observations;
- daily ST, suspension and price-limit observations, including explicit
  whole-session locked-limit flags;
- a 45-calendar-day rolling observation window for dividend stages, cash
  dividends, splits, allotments and ex-factors;
- RQData legacy Shenwan (`source=sws`) as-of industry assignments for every
  covered trading date;
- constituents and weights for the explicitly configured core indices;
- a deterministic, resumable financial-PIT work queue.  Financial calls are
  deliberately **not executed** by v1.

Vendor `rice_create_tm`, source publication dates and as-of query behavior are
preserved as observations but are not promoted to legal `available_at` or a
complete correction/withdrawal chain.  A candidate therefore remains
`INSPECTION_ONLY/NOT_ADMITTED` even when all mechanical checks pass.

Examples:

```powershell
${QUANTIFICATION_ROOT}\control\tools\run_rqdata_b_path_daily_update.cmd --plan --through 2026-08-05
${QUANTIFICATION_ROOT}\control\tools\run_rqdata_b_path_daily_update.cmd --run --through 2026-08-05
```
