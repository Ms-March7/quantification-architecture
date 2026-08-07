# Public official API inspection adapters v1

This contract belongs to the bounded
`L1-D2A-04B_PUBLIC_OFFICIAL_NO_KEY_API_INSPECTION_ADAPTERS` work package.
It covers four first-party, no-key endpoints that passed the 2026-07-30
structural preflight:

- ECB Data Portal;
- BIS Statistics SDMX API;
- U.S. Treasury daily par-yield curve feed;
- HKMA Open API.

The implementation is deliberately fail-closed and inspection-only:

- fixture mode is the default;
- live mode must be requested explicitly;
- at most one GET request is made per selected source;
- retries, pagination, credentials, cookies, authorization headers and bulk
  history are not supported;
- output is restricted to
  `tmp/public-official-api-inspection/<run_id>/`;
- raw bytes, response SHA-256, parser receipt, rights status and PIT
  limitations are recorded;
- missing required fields, unexpected cardinality, oversized responses,
  non-2xx responses and parse failures are quarantined;
- normalized output remains
  `INSPECTION_ONLY/NOT_ADMITTED`;
- `datasets.json`, `planned_datasets.json`, formal market roots, quality
  status and schedulers are never modified.

The fixture payloads test parser and policy mechanics only. They are synthetic
representations of the documented structures and are not source observations.
Passing this contract does not close license, local-retention, internal-share,
publication-time, revision-vintage, source-admission or research gates.

Entry points:

```powershell
# Offline, deterministic fixture run (default)
py -3.11 control/tools/run_public_official_api_inspection.py `
  --run-id public-official-fixture-YYYYMMDD-v1

# Explicit bounded live run: one request/source, no retry, tmp only
py -3.11 control/tools/run_public_official_api_inspection.py `
  --mode live --run-id public-official-live-YYYYMMDD-v1
```

The live command is not a scheduler and must not be installed as one.
