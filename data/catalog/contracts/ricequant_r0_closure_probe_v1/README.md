# Ricequant R0 closure probe v1

Status: `LIVE_BOUNDED_CAPABILITY_PASS__14_NONEMPTY_3_EMPTY_0_ERROR__FORMAL_EFFECT_NONE`

This contract governs a summary-only RQData capability probe for CORE-01 through
CORE-06. It uses at most 17 business API calls, four securities and five price
dates. API payloads are held only in memory; the output contains schema, counts,
null counts and invariant checks, never the License or full market data.

A successful probe proves only entitlement and schema availability. It does not
admit a source, publish a snapshot, modify `datasets.json`, open R0 research, or
authorize factors, backtests, Validation, Holdout or trading.

The authenticated live run completed on 2026-08-03. Fourteen checks returned
non-empty structured results, while split, allotment and special-treatment checks
returned no rows and are explicitly recorded as `PASS_EMPTY`; no check errored.
This closes the bounded endpoint/schema capability probe only. Formal CORE-01
through CORE-06 and R0 remain unmet.
