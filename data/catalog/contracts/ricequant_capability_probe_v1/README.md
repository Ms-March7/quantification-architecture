# Ricequant capability probe v1

Status: `LIVE_MINIMAL_PROBE_PARTIAL__PRICE_CALENDAR_PASS__FUND_ETF_DENIED__FORMAL_EFFECT_NONE`

This contract defines the first bounded inspection of the user's Ricequant
education/trial entitlement. It is a capability and source-suitability probe,
not a data acquisition, source-admission, factor-research, backtest, or trading
authorization.

The machine-readable boundary is `contract.json`. Offline package discovery is
implemented by `control/tools/probe_ricequant_environment.py`.

The 2026-08-03 user-mediated minimal live probe verified RQData initialization,
a bounded trading-calendar query, and a two-security daily-price sample. Public
fund holdings and ETF daily components were permission denied. No license value,
bulk payload, catalog record, factor result, backtest, or trade was captured.
RQData is therefore raised only to a priority CORE price-volume probe and
gap-fill candidate; the purpose ceiling remains `INSPECTION_ONLY`.

RQData may be promoted from a historical alternative candidate only when the
probe produces evidence for the relevant gates. Strong API breadth alone is not
sufficient: point-in-time behavior, revision semantics, historical universe,
ETF transparency, retention/derivation rights, reproducibility, and operational
stability must also be demonstrated.
