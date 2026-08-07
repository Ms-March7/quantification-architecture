# Atomic snapshot publication contract v1

Status: `L1-D12A-01_DONE__ISOLATED_INFRASTRUCTURE_ONLY`

This contract defines how an immutable snapshot becomes visible through
`data/catalog/datasets.json`. It does **not** admit a real data source, grant a
licence, promote data quality, or authorize historical backfill.

## Visibility and state machine

```text
external fixture input
  -> private staging copy + before/after inventory comparison
  -> sealed prepared-plan.json
  -> catalog lock + sealed commit-plan.json
  -> same-volume raw/curated/snapshot rename
  -> full contract and payload verification
  -> datasets.json same-directory os.replace   [only visibility commit]
  -> transaction receipt + staging cleanup
```

Before the catalog replacement, final payload directories may exist as
recoverable orphans, but `qdata` cannot discover them. A failed quality gate or
identity conflict goes to `data/market/quarantine/` and is never added to the
catalog.

## Immutable authority set

Every `sha256-snapshot-contract-v1` catalog entry must point to exactly these
five documents under its snapshot root:

1. `request.json` — stable snapshot request; operational `run_id` is excluded.
2. `manifest.json` — identity, paths, schema, PIT time, contract and root hash.
3. `provenance.json` — source, ingest date, request/payload bindings.
4. `partition-manifest.json` — sorted raw/curated file inventory and SHA-256.
5. `quality/summary.json` — quality decision, checks and purpose ceiling.

The root hash binds the complete canonical content of all five documents. The
`content_hash` field itself is omitted while reconstructing the self-referential
manifest hash. The catalog item is also checked against the stable request for
owner, store, licence, quality, purpose and consumers.

Operational attempts and recovery actions live outside the immutable snapshot
at `data/market/transactions/<run_id>.json`. This lets an identical snapshot be
retried under a new `run_id` without changing its identity.

## Recovery rules

- The Python implementation executes the checked-in request-schema subset and
  fails closed on unknown schema keywords. Dates and date-times use strict
  RFC 3339 lexical forms; path components receive additional Windows safety
  checks before staging.
- A sealed `prepared-plan.json` permits recovery without the original input.
- A sealed `commit-plan.json` binds the catalog before hash, candidate catalog,
  candidate hash, exact identity and final paths.
- Catalog compare-and-swap is checked immediately before `os.replace`.
- A stale Windows lock can be archived only when the run id and hostname match,
  the kernel wait state says its process is dead, and the operator supplies the
  exact lock-file SHA-256. A creation-time token detects PID reuse; unknown or
  access-denied process state fails closed.
- Lock, transaction and prepared plan bind the same `run_id`, `dataset_id`,
  `snapshot_id`, stage path, prepared-plan hash and operational-request hash.
  Recovery reloads the operational request and re-derives every final path;
  no plan-supplied target outside the isolated workspace is trusted.
- An operating-system byte lock serializes stale-lock archival and creation of
  the next writer lock. The guard is released automatically on process death,
  and archival never overwrites an existing audit record.
- An existing snapshot directory is never overwritten. Equal full hashes yield
  `NO_CHANGE`; unequal hashes fail closed or enter quarantine.
- Missing or invalid `owner.json` blocks before any commit-side write. If the
  catalog is already visible but staging cleanup fails, the result is
  `PUBLISHED_CLEANUP_PENDING` or `NO_CHANGE_CLEANUP_PENDING`, which remains
  recoverable and is not misreported as an invisible failed publication.

## Current hard boundary

`control/tools/atomic_snapshot_publish.py` requires the exact sentinel
`.qdata-d12a-synthetic-workspace` and the isolated workspace's exact
regular, non-reparse `data/catalog/datasets.json`. The sentinel, catalog and
all request, stage and final paths must remain inside a non-reparse workspace.
Therefore it cannot write the formal
`${QUANTIFICATION_ROOT}/data/catalog/datasets.json`.

Real source adapters, endpoint/licence evidence, D3A semantic quality checks,
measured capacity pilots and bulk backfill belong to later approved packages.
