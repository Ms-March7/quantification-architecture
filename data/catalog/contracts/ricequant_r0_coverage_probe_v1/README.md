# Ricequant R0 coverage, PIT and volume probe v1

Status: `LIVE_COMPLETED__COVERAGE_AND_BOUNDED_PIT_PASS__BJSE_DAILY_DENIED__FLOW_RECONCILED__FORMAL_EFFECT_NONE`

This summary-only probe uses at most 17 RQData business calls and at most 8 MiB
of incremental daily flow. It tests long calendar coverage, latest-master dynamic
pool construction limits, bounded corporate-action `as_of` behavior and daily
price transfer cost using deterministic 32-by-5 and at most 256-by-60 samples.

The first live attempt proved that BJSE security-master rows are accessible but
BJSE daily bars are denied for this license. Its mixed-market price request was
therefore rejected as a whole. The bounded retry excludes BJSE from price-volume
samples while preserving that denial as a formal capability boundary.

The XSHG/XSHE retry completed. It returned 14,730 unadjusted price rows across
the two deterministic samples and positive split/allotment action anchors. A
fresh-process quota check was required because the in-process quota counter did
not refresh synchronously. The result remains inspection-only and R0 is not met.

No API payload or credential is persisted. Success updates capability evidence
only; it does not admit a source, publish a snapshot, modify `datasets.json`, run
a factor/backtest, or open R0/R1/R2.
