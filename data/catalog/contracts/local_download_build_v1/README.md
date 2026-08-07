# Local downloaded dataset candidate v1

Status: `INSPECTION_ONLY__NO_SOURCE_ADMISSION__NO_CATALOG_WRITE`

This contract is the local ingestion bridge for files that the user has
already downloaded under a valid learning/research permission. It is not a
CSMAR downloader and does not depend on a browser session.

## One-command workflow

The default is a write-free plan:

```powershell
& <approved-python-3.11> `
  .\control\tools\build_downloaded_dataset.py `
  --request .\path\to\request.json `
  --inbox .\path\to\downloaded-files
```

After reviewing the discovered packages, schemas, byte estimate, and target,
explicit execution uses the same command with `--execute`:

```powershell
& <approved-data-engineering-python> `
  .\control\tools\build_downloaded_dataset.py `
  --request .\path\to\request.json `
  --inbox .\path\to\downloaded-files `
  --execute
```

`--execute` requires DuckDB from the locked data-engineering runtime. The
current project virtual environment may be used only as a compatibility
witness in tests; it is not the central runtime authority.

## Input and safety contract

- Only `.csv` and `.zip` packages explicitly under `--inbox` are considered.
- The input tree may not contain symlinks, junctions, or reparse points.
- ZIP traversal paths, duplicate case-insensitive paths, encrypted members,
  symlinks, excessive member counts, excessive unpacked bytes, and excessive
  compression ratios fail closed.
- One request describes exactly one logical table. Selected CSV headers must
  agree; heterogeneous exports must be split into separate requests.
- The request supplies explicit column types, date column, required columns,
  business key, and `partition_mode`. Use `year_month` for daily/event facts
  and `single` for small reference/master tables. Unknown fields may be
  retained as `VARCHAR` only when `allow_extra_columns=true`.
- `date_column` may be `null` only with `partition_mode=single`. This is the
  fail-closed representation for a static master whose date-like field contains
  partial or sentinel values such as `0000-00-00`; that field remains `VARCHAR`
  and candidate `date_min/date_max` remain null.
- Empty or unparsable required values, failed casts, duplicate keys, and a
  changing central catalog fingerprint block sealing.
- Original package bytes are copied into candidate `raw/`. GB18030 and UTF-16
  CSV files are transcoded only in temporary work space before Parquet output;
  the raw package is unchanged.

## Output contract

Successful execution creates only:

```text
tmp/local-dataset-build/<dataset_id>/<run_id>/
|-- raw/
|-- curated/schema=<version>/year=<YYYY>/month=<MM>/part-000.parquet
|-- request.json
|-- manifest.json
|-- provenance.json
|-- partition-manifest.json
|-- quality/summary.json
|-- query.sql
`-- REPORT.md
```

The candidate is sealed by a same-volume directory rename and refuses to
overwrite an existing run identity. `query.sql` creates a DuckDB view over the
explicit candidate path.

Every successful output remains:

- `purpose_ceiling=INSPECTION_ONLY`
- `source_admission=false`
- `catalog_registered=false`
- `quality_promotion=false`
- `research_ready=false`

Mechanical PASS means only that the local files satisfied the declared schema,
type, null, key, partition, and hash checks. Formal use still requires source
rights admission, field/unit semantics, PIT and revision-chain verification,
coverage and cross-source checks, approved atomic publication, and registration
in `data/catalog/datasets.json`.

See `example.request.json` and `request.schema.json`. The example field names
are illustrative only and are not a CSMAR field-semantic contract.

## CSMAR intake manifest

`csmar.intake-manifest.json` is the historical machine-readable intake ledger
for the first three executed candidate runs across two logical tables:

- `TRD_Co`: company master, one full-table package, one Parquet file.
- `TRD_Dalyr`: a 2025-01 pilot plus a 2020—2024 five-year package; daily stock
  returns/quotes are merged and validated globally, then stored by year/month.

The `*.request.template.json` files retain the full field list from the
2024-02-05 manual and are reference targets only; they have not been validated
as complete semantic contracts for the current exports. The executed request
contracts
are:

- `csmar_trd_co.current-pilot.request.json`: observed 3-column export.
- `csmar_trd_dalyr.current-pilot.request.json`: observed 24-column export,
  including `LimitDown`, `LimitUp`, and `LimitStatus`.
- `csmar_trd_dalyr.d2a02-2020-2024.request.json`: the same 24-column shape for
  the 2020—2024 five-year candidate, producing 60 year/month partitions.

The following action/status templates were initially locked with
`authorized_local_research_download=false` and a placeholder package filename:

- `csmar_trd_cptl.request.template.json`: `TRD_Cptl`, 8 manual-reference fields.
- `csmar_trd_capchg.request.template.json`: `TRD_Capchg`, 21
  manual-reference fields.
- `csmar_trd_nolimit.request.template.json`: `TRD_NoLimit`, 3
  manual-reference fields.

They remain PLAN/reference inputs rather than semantic admission. After the
actual ZIP and `*[DES][csv].txt` descriptor were reviewed field by field,
separate concrete requests were created and executed for `TRD_Cptl`,
`TRD_Capchg`, `TRD_NoLimit`, and `TRD_AdjustFactor`. The concrete request binds
the exact package filename, preserves the actual ZIP and descriptor in
candidate raw storage, records the run ID/schema label and local-research
authorization basis, and applies an exact order-sensitive header gate
(`allow_extra_columns=false`). A mismatch must fail closed; never edit an
export to imitate a manual or template.

The current concrete action/constraint requests are:

- `csmar_trd_cptl.d2a04b-full-1990-2026.request.json`
- `csmar_trd_capchg.d2a04b-full-history.request.json`
- `csmar_trd_nolimit.d2a04b-full-1990-2026.request.json`
- `csmar_trd_adjustfactor.d2a04b-full-1991-2026.request.json`

Three financial statement templates and concrete requests were also created
only after CRC, DES and actual CSV-header review:

- `csmar_fs_combas.request.template.json` and
  `csmar_fs_combas.d2a04b-full-history.request.json`
- `csmar_fs_comins.request.template.json` and
  `csmar_fs_comins.d2a04b-full-history.request.json`
- `csmar_fs_comscfd.request.template.json` and
  `csmar_fs_comscfd.d2a04b-full-history.request.json`

Their candidates contain current wide tables only. `Accper` is a report period
and `DeclareDate` is a correction-disclosure list; neither proves initial
`available_at`, and the exports do not contain prior values or a complete
revision chain. Mechanical PASS therefore remains
`INSPECTION_ONLY/NOT_ADMITTED`.

## Audit-text zero-drop closure

`FIN_Audit` and `FIN_AuditDetail` contain legitimate multiline text and
unescaped quote patterns that cannot be handled by the generic CSV reader
without an explicit repair ledger. The dedicated local-only adapter is:

```powershell
& <approved-data-engineering-python> `
  .\control\tools\parse_csmar_audit_text_candidate.py `
  --zip <sealed-zip-path> `
  --table FIN_Audit `
  --run-id <new-run-id>
```

The default is a complete write-free inspection. Materialization requires
`--execute`, may write only below `tmp/csmar-audit-text-candidate/`, refuses to
overwrite a run identity, and never edits the sealed source ZIP. Each executed
run produces a deterministic five-member provenance ZIP containing:

- `parser-manifest.json`
- `quality/summary.json`
- `repair/records.jsonl`
- the repaired table CSV
- `upstream_provenance_receipt.json`

The downstream generic build request must select that whole provenance ZIP,
not a detached repaired CSV. The candidate raw payload and final
`content_hash` therefore cover the source ZIP/member hashes, parser script,
repair manifest, row-level sidecar, repaired CSV, quality record, semantic
review details and downstream Parquet.

The authoritative 2026-07-29 v2 results are:

- `FIN_Audit`: 80,301 rows, 238 repaired rows, one semantic-review row,
  54 Parquet partitions,
  `content_hash=b9186d3448dcfa2a98ff540b8cdcbec03540418f7eeecc831f9d705ce23f0c4d`.
- `FIN_AuditDetail`: 82,264 rows, 103 repaired rows, zero semantic-review rows,
  10 Parquet partitions,
  `content_hash=be55d9243093006dc2f18ee827d35361eac6cc0f9e6ce1e19c210c0fd933d4eb`.

For both tables, repaired CSV versus Parquet and the reverse `EXCEPT ALL`
comparison are zero, and declared business-key duplicates are zero. The v1
intermediate and downstream candidates are retained but are superseded because
their final content hash did not cover every repair-evidence object. Never
overwrite v2: changing the parser or any bound evidence requires a new v3 run.
The closure report is
`reports/data-governance/csmar-33pkg-local-closure-20260729/`.

This solves local mechanical preservation only. Publication timestamps,
stable audit-report IDs, revisions, source rights, semantic taxonomy and
formal quality remain open, so both candidates stay
`INSPECTION_ONLY/NOT_ADMITTED`.

It records `table_id`, `namePhy`, expected fields, keys, date columns, package
boundaries, authorization scope, inbox, request template, and result directory.
The CSMAR internal/numeric `table_id` is deliberately `null` until it is copied
from an actual export or authorized metadata response; `namePhy` is verified
against the 2024-02-05 official data manual. Never guess the internal ID.

Before running, copy the relevant request template, replace
`REPLACE_WITH_RUN_ID`, and verify that the actual export header matches the
manual version. No account name, password, cookie, token, or browser state
belongs in any request or manifest.

Current commands use the installed, verified central runtime first:

```powershell
& .\control\tools\qdata_build_downloaded.cmd `
  -Request .\data\catalog\contracts\local_download_build_v1\csmar_trd_co.current-pilot.request.json `
  -Inbox .\tmp\csmar_pilot

& .\control\tools\qdata_build_downloaded.cmd `
  -Request .\data\catalog\contracts\local_download_build_v1\csmar_trd_dalyr.current-pilot.request.json `
  -Inbox .\tmp\csmar_pilot
```

After reviewing each plan, append `-Execute` to build the isolated candidate.
These commands do not use the logged-in browser and perform no network access.
The `.cmd` launcher uses `-ExecutionPolicy Bypass` only for this checked-in
wrapper because the current machine blocks direct `.ps1` execution. The
project compatibility-witness runtime remains a fallback only if the central
runtime path is absent.
