# Source admission evidence contract v1

Status: `PAUSED__D2A_04B_33PKG_LOCAL_BATCH_COMPLETE__D2A_04_COMPLETION_GATE_NOT_MET__NO_FORMAL_ADMISSION`

`source_matrix.json` is the fail-closed historical runner contract for
`L1-D2A-01`; it must not be rewritten to make later observations look as if
they had been authorized by that run. `current_source_registry.json` is the
additive current-status overlay spanning D2A-01 through D2A-04B and the
retained CSMAR manual-export candidate runs. D2A-04 Phase A was executed only
through its exact bounded local plan and is now paused. The later exact
33-package D2A-04B local intake is also complete and paused. The overlay does not reactivate the legacy
generic runner, authorize a login, network download or secret storage, register
`datasets.json`, promote quality, or allow research use.

Current aggregate state is:

- D2A-01 remains paused with no formal source admission;
- D2A-02 has a mechanically valid five-year daily candidate but is blocked on
  financial PIT and formal source semantics;
- D2A-03 has 14 official/CNINFO PIT candidates and a CORE-03 source slice, but
  CORE-01/02/04/06 remain quarantined and CORE-05 has no output;
- D2A-04 Phase A completed local candidate engineering but did not pass its
  completion gate; execution is paused, network execution remains disabled,
  and formal source admissions and formal CORE snapshots both remain zero.
- D2A-04B completed the exact 33-package local intake, sealed all packages,
  built 13 additional mechanical candidates, retained two audit-text tables
  as raw-only quarantine, deleted only the verified C/F originals, and then
  closed the execution authorization. Any further external collection
  requires a new explicit user start.

The checked-in historical matrix is fail-closed. D2A-04's executed plan
permitted only its declared local phase and set
`network_requests_authorized=false`; the current authorization switch is false,
so every source-admission runner must reject a new network run before
creating its output directory. A user may
restart one exact, bounded sub-pilot without reactivating the legacy generic
runner only when all of the following are true:

1. the top-level matrix status is
   `PAUSED_GENERIC_RUNNER__SCOPED_PLAN_ACTIVE__NO_FORMAL_ADMISSION`;
2. `execution_authorization_active=true`;
3. `authorized_execution_plans` contains exactly named plan and runner paths,
   the exact reviewed plan SHA-256, and
   `status=ACTIVE__ONE_OFF_INSPECTION_ONLY`;
4. both the plan and authorization keep formal admission, bulk backfill,
   paid-source use, user credentials/keys and quality promotion disabled; and
5. the scoped runner verifies every condition before creating its output.

The legacy `run_source_admission_pilot.py` remains disabled because it accepts
only `status=ACTIVE__PILOT_ONLY__NO_FORMAL_ADMISSION`; the scoped-plan status
must not satisfy that gate.  After the bounded run, the top-level switch must
return to `false`, the matrix must return to its paused status, and the
executed plan copy and hash must remain with the evidence.

Before any reactivation, the exact endpoint, method, parameters, response-byte
cap and content markers must be moved into a reviewed execution-plan contract
and bound by hash. The paused v1 runner currently binds the declared business
probe ID set, while those finer request details remain code-resident; changing
only the status flag is therefore insufficient authorization to resume.

The historical runner portion of this contract, especially
`source_matrix.json`, governs bounded real-source evidence collected for
`L1-D2A-01_SOURCE_ADMISSION_AND_PILOT_ADAPTERS`. Later registry overlays and
exact execution plans record their own narrower authority. Nothing in this
directory admits a formal dataset, authorizes a historical backfill, changes
`datasets.json`, or promotes quality.

The 2026-07-27 BaoStock daily-coverage sub-pilot is closed. The verified
0.9.3 client logged in, but the first compressed all-A daily response did not
satisfy the reviewed frame/footer contract; no business result was accepted.
This is `COVERAGE_UNVERIFIED`, not evidence that BaoStock either covers or does
not cover the requested markets. The result and domestic alternative-source
review are recorded in
`reports/data-governance/baostock-daily-coverage-pilot-20260727/README.md`.

## Decision levels

- `PILOT_ALLOWED__NONCOMMERCIAL_PUBLIC_WEB`: the cited public terms are
  evidence supporting a workspace one-off risk decision for the exact bounded
  request. This is not a source grant for automated fetching, durable shared
  storage, or redistribution. The result remains local, noncommercial, and
  `INSPECTION_ONLY`.
- `BLOCKED_BEFORE_BUSINESS_DATA_FETCH`: the source-specific rights, PIT, or
  field semantics are not closed. The runner may capture public legal/service
  pages, but it must not fetch the blocked business data.
- `BLOCKED_FOR_FORMAL_OR_BULK`: a tiny pilot may run, but automated history,
  durable shared storage, redistribution, or production use remains blocked.

The most restrictive applicable decision wins. A successful HTTP response
never upgrades the decision.

## Evidence requirements

Every executed probe records:

1. exact source and endpoint identity;
2. retrieval time, HTTP status, byte count, SHA-256, content type and bounded
   request parameters;
3. the governing public terms or source-service evidence;
4. PIT and field-semantic observations;
5. a purpose ceiling and all unresolved blockers;
6. before/after fingerprints proving that formal catalog and shared data roots
   were not changed.

The runner is `control/tools/run_source_admission_pilot.py`. Runtime output is
confined to `tmp/source-admission-pilot/<run_id>`.

## Current hard blocks

- The BSE current security-master endpoint is not requested. A prior manual
  observation suggested that it may mix static identity and quote fields, but
  this work package retained no raw response proving that schema. The schema
  and rights therefore remain unverified and are blocked conservatively.
- Exchange daily trading data is not requested. Exchange rules and data
  service pages reserve trading-information use to authorized users, while no
  exact no-key historical product licence has been admitted.
- The Tencent daily response observed in the earlier capacity smoke is not
  fetched again. Its underlying reuse rights and the meanings/units of all 11
  raw fields remain unresolved.
- CNINFO was limited to one metadata search request per approved run. The
  discovery runs `v1`, `v2` and final run `v3` each made the same request, so
  the work-package total is three requests yielding one unique response hash.
  No further request is authorized and no PDF body was downloaded; automated
  bulk access, durable shared retention, revision-chain completeness and
  event-effective-time semantics remain open.
- Current security lists and annual holiday notices do not prove a complete
  historical PIT universe or session calendar.

`source_matrix.json` is the machine-readable decision matrix used by the
runner. It is a governance contract, not a data catalog.

## PIT and source-field rules learned by the pilot

- A source timestamp that resolves to `00:00:00 Asia/Shanghai` is not
  automatically second-precision evidence. Preserve the raw value and record
  `published_at_precision`; historical date-only records receive a conservative
  next-session availability rule until better evidence exists.
- `first_seen_at` is required for future low-frequency polling.
  `Last-Modified` is transport evidence, not market availability.
- CNINFO `announcementId` is a source record/document identifier; the sample
  does not prove that it is a version identifier or cross-stage
  company-action identity. `associateAnnouncement` is too sparse to act as a
  general supersession chain.
- Future PIT normalization must preserve `published_at_raw`,
  `published_at_parsed`, `published_at_precision`, `first_seen_at`,
  `retrieved_at`, `available_at`, `available_at_method` and the timezone
  basis. `first_seen_at` is a local observation bound, not source publication
  time. Date-level or unknown-precision records may become available only at
  the next A-share trading session after the source date, never the same-day
  open, unless stronger source evidence is admitted.
- Search-result titles may contain HTML highlighting; raw and cleaned titles
  must be separate. Source size fields are not bytes until a first-party
  dictionary or actual response length proves the unit.
- An endpoint's requested page size or date range is not trusted as an
  enforced server-side boundary. The adapter must apply response-byte caps,
  count observed rows, validate date coverage and quarantine drift.
