# Free web daily dual-source inspection v1

Status: `BOUNDED_INSPECTION_ONLY__NO_FORMAL_ADMISSION`

This contract tests whether two public web quote responses can provide a
same-day A-share delta without credentials or payment. It is deliberately not
a production data source contract.

The executable is:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  -X utf8 .\control\tools\run_free_web_daily_probe.py `
  --trade-date 2026-07-31 `
  --symbols sh600000,sz000001,bj920080 `
  --output .\tmp\free-web-daily-probe\20260731-dual-source-v1 `
  --execute
```

Without `--execute`, the command is a network-free plan. Execution makes at
most two HTTP requests: one batch request to Sina and one to Tencent. Redirects
are rejected, so each logical call is exactly one physical HTTP request. It has no
retry path, credential input, token input, cookie persistence, scheduler,
formal publisher, catalog writer, or quality-promotion path.

For the current `Asia/Shanghai` calendar date, execution is forbidden before
15:45 local time. This is a close-time buffer, not proof that either endpoint
is an exchange-authoritative closing source. In addition, both returned quote
timestamps must be at or after the regular 15:00 close; an intraday timestamp
fails closed even when the request itself runs after 15:45.

The parser fails closed on malformed widths, timestamps, numeric values,
negative volume/amount, unexpected symbols, missing rows, wrong dates, OHLC inconsistencies, material
price/amount disagreement, or volume disagreement exceeding one board lot.
Raw responses, normalized rows, findings and file hashes stay below `tmp/`.
All files are first created in a same-parent staging directory; only a complete
package is atomically renamed to the requested immutable output path. Failed
runs remove staging and do not publish a partial output directory.

Observed source-specific units are not silently merged:

- Sina volume is retained in shares and amount in CNY.
- Tencent volume is observed in rounded 100-share board lots; its composite
  amount is observed in CNY.
- Tencent field 0 is retained only as an unresolved route code. It is never
  represented or interpreted as a trading-status field.
- The normalized candidate uses Sina shares only after Tencent OHLC/amount and
rounded-lot volume cross-checks pass. Both raw values and deltas remain in
the evidence receipt.

The manifest cryptographically binds the executable script, this contract and
README, policy/tolerances, capture timestamps, requested and final URLs, and
the no-redirect receipts. Different endpoint domains are not evidence of
independent upstream lineage.

Even a dual-source PASS proves only bounded mechanical feasibility. These web
endpoints are not documented stable APIs, do not establish retention or
internal research rights, and do not prove full-market, suspended, delisted,
historical, corporate-action or revision coverage. Consequently every output
has `purpose_ceiling=INSPECTION_ONLY`, `formal_source_admission=false`, and
`research_ready=false`.
