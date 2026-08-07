# Ricequant R1 financial PIT bounded probe v1

Status: `COMPLETED__PARTIAL_FINANCIAL_CAPABILITY__FORMAL_EFFECT_NONE`

This inspection-only probe uses exactly ten bounded financial capability calls
plus two fresh-process numeric quota calls. It covers small positive/negative
anchors for CORE-13 financial statements, CORE-15 earnings express/forecast
events and CORE-16 audit opinions. CORE-14 remains a deterministic derivative
of an admitted CORE-13 snapshot; CORE-18 has no known client endpoint in scope.

Only schema, row counts, null counts, canonical hashes, timestamp ranges and
pairwise as-of difference counts may be persisted. API payloads, credentials,
account identity and configuration values must never be captured or printed.

Success proves bounded endpoint entitlement and observed as-of behavior only.
It does not prove complete correction/restatement history, legal `available_at`,
source rights, full coverage, immutable snapshot admission or R1 readiness.

The authorized run completed on 2026-08-04. CORE-13 PIT financial statements
and CORE-15 earnings express/forecast returned positive bounded samples with
observable as-of differences. Both CORE-16 audit-opinion calls were denied by
the current entitlement. The persistent receipt is
`reports/data-governance/ricequant-r1-financial-pit-probe-20260804-v1/`.
