# Quant Research Reliability Harness Roadmap

Status: Implemented through Phase 5
Scope: project-local harness for `${QUANTIFICATION_ROOT}`
Owner: Quantification capability system

## Purpose

This roadmap turns `HARNESS.md` into an executable build plan.

`HARNESS.md` defines the harness contract: purpose, safety boundaries,
architecture, registry model, modes, status rules, and extension rules.

`ROADMAP.md` defines the implementation sequence: what to build first, what
counts as done, and when the harness is ready to protect more skills, MCP
tools, reports, and plugins.

The guiding rule is registration-first extensibility:

```text
new target -> targets.yaml
new scenario -> cases.yaml
new interface -> contracts/
new runner behavior -> adapters/
new judgment -> oracles/
```

The runner must stay generic. It should not hard-code finance business logic or
the current three skills.

## Current State

Implemented design files:

- `HARNESS.md`
- `targets.yaml`
- `cases.yaml`

Implemented executable files:

- `run_harness.py`
- `adapters/skill_script_adapter.py`
- `adapters/report_audit_adapter.py`
- `adapters/mcp_probe_adapter.py`
- `oracles/schema_assertions.py`
- `oracles/report_assertions.py`
- `oracles/safety_assertions.py`
- `oracles/finance_assertions.py`

Implemented contracts:

- `contracts/symbol-date.schema.yaml`
- `contracts/ranking.schema.yaml`
- `contracts/capital-flow.schema.yaml`
- `contracts/event-windows.schema.yaml`
- `contracts/report-safety.schema.yaml`

Implemented offline fixtures:

- `fixtures/golden/relative-dominance-etf-pair/prices.csv`
- `fixtures/golden/capital-flow-basic/capital_flow.csv`
- `fixtures/golden/event-study-basic/events.csv`
- `fixtures/golden/event-study-basic/prices.csv`
- `fixtures/failure/stale-capital-flow/capital_flow.csv`
- `fixtures/failure/missing-prices-event-study/events.csv`
- `fixtures/failure/missing-prices-event-study/prices.csv`
- `fixtures/failure/duplicate-announcements/events.csv`
- `fixtures/failure/duplicate-announcements/prices.csv`
- `fixtures/contract/report-overclaim-samples/weak_report.md`

Currently active target:

- `capital_flow_attribution`

Disabled targets retained for explicit upgrade testing:

- `stock_relative_dominance`: `needs_upgrade`
- `capital_flow_monitor`: `pending_replacement`
- `risk_warning_event_study`: `needs_upgrade`

Initial planned targets:

- `quant_data_mcp`
- `finance_report_safety`

## Phase 0: Design Skeleton

Status: Done

Goal:

Define the harness purpose, safety boundaries, registry model, target registry,
and planned case registry before writing executable code.

Completed artifacts:

- `HARNESS.md`
- `targets.yaml`
- `cases.yaml`

Done when:

- The harness is clearly project-local.
- The runner is explicitly registry-driven.
- Live data is disabled by default.
- Skill folders, inventory files, plugin packaging, archive, move, merge, and
  delete operations are outside harness scope.

## Phase 1: Single-Case Executable MVP

Status: Done

Goal:

Run one offline case end to end:

```text
relative_dominance_csv_contract
```

This phase proves the architecture, not the whole finance portfolio.

Target:

- `stock_relative_dominance`

Why this target first:

- It already supports local CSV input.
- It can run offline.
- It has clear machine-readable outputs.
- It is the smallest realistic path for testing contracts, adapters, oracles,
  run directories, and report generation.

Required files to add:

```text
run_harness.py
adapters/
  skill_script_adapter.py
oracles/
  schema_assertions.py
  report_assertions.py
  safety_assertions.py
contracts/
  symbol-date.schema.yaml
  ranking.schema.yaml
fixtures/
  golden/
    relative-dominance-etf-pair/
      prices.csv
```

Implementation steps:

1. Inspect the actual `stock-relative-dominance` CSV output.
   - Run or read a deterministic local CSV path.
   - Record the real `ranking.csv` columns.
   - Do not invent columns from memory.
2. Create the minimal fixture.
   - Use two symbols.
   - Use a short date range.
   - Keep data synthetic or clearly offline.
   - Do not fetch live data.
3. Create minimal contracts.
   - `symbol-date.schema.yaml` covers shared symbol and date conventions.
   - `ranking.schema.yaml` covers only the required ranking output fields
     actually emitted by the current script.
4. Implement `skill_script_adapter.py`.
   - Load command arrays from `targets.yaml`.
   - Replace placeholders such as `{fixture}`, `{run_dir}`, `{symbols}`,
     `{start_date}`, and `{end_date}`.
   - Honor configured `cwd`.
   - Capture exit code, stdout, stderr, duration, and artifact paths.
   - Never run through broad shell strings when a command array is available.
5. Implement first oracles.
   - `required_artifacts_exist`
   - `csv_has_required_columns`
   - `symbols_match_requested_universe`
   - `date_range_is_explicit`
   - `report_mentions_source_and_adjustment`
   - `output_stays_under_run_dir`
6. Implement `run_harness.py`.
   - Parse `--quick`, `--full`, `--case`, and `--list`.
   - Load `targets.yaml` and `cases.yaml`.
   - Select quick cases by mode.
   - Create a timestamped run directory under the configured temp root.
   - Execute adapters.
   - Execute assertions.
   - Write a Markdown harness report under the configured report root.
7. Activate only the first case.
   - Keep other cases `planned` until their fixtures and oracles exist.

Phase 1 command:

```powershell
py ${QUANTIFICATION_ROOT}\.agents\harnesses\quant-research-reliability\run_harness.py --quick
```

Done when:

- The command above completes without live network access.
- A timestamped temp run directory is created.
- The relative-dominance script runs from the local CSV fixture.
- Expected artifacts are produced under the run directory.
- `ranking.csv` is checked against `ranking.schema.yaml`.
- Shared symbol/date rules are checked against `symbol-date.schema.yaml`.
- Output path safety is checked.
- A Markdown harness report is written.
- Overall status is `PASS`, `REVIEW`, or `FAIL` using the documented rules.

Non-goals:

- Do not implement capital-flow checks yet.
- Do not implement event-study checks yet.
- Do not implement live mode.
- Do not edit any skill folder unless a failing contract reveals a necessary
  skill fix and the user approves that fix separately.

## Phase 2: Capital-Flow Failure Case

Status: Done

Goal:

Add a deliberate stale-data scenario for `a-share-capital-flow-monitor`.

Target case:

```text
capital_flow_stale_data_failure
```

Required additions:

- `contracts/capital-flow.schema.yaml`
- `fixtures/failure/stale-capital-flow/capital_flow.csv`
- report assertions for stale data, source timestamp visibility, and no strong
  conclusion

Done when:

- The case runs offline.
- Stale evidence produces `REVIEW`, not `PASS`.
- The report exposes source timestamp and stale-data limitations.
- The output remains under the run directory.

## Phase 3: Event-Study Failure And Duplicate Cases

Status: Done

Goal:

Protect event-study behavior against missing price coverage and duplicate
announcement distortion.

Target cases:

```text
event_study_missing_prices_failure
event_study_duplicate_announcements_contract
```

Required additions:

- `contracts/event-windows.schema.yaml`
- `fixtures/failure/missing-prices-event-study/`
- `fixtures/failure/duplicate-announcements/`
- finance assertions for missing prices, sample-size visibility, duplicate
  handling, filtered-event explanation, and no strong conclusion under weak
  evidence

Done when:

- Missing prices are visible and downgrade confidence.
- Duplicate announcements are visible, deduped, or explained.
- Event-window outputs remain parseable and contract-compliant.

## Phase 4: Cross-Skill Shared Contract

Status: Done

Goal:

Verify that the finance skills can compose into a larger workbench or agent
workflow through shared symbol, date, source, status, and quality conventions.

Target case:

```text
cross_skill_symbol_date_contract
```

Required additions:

- Shared contract checks across all active finance targets.
- Documentation of canonical symbol forms accepted by each workflow.
- ISO date checks for machine-readable outputs.
- Status or quality-field checks where outputs can be incomplete.

Done when:

- Each participating target either conforms to the shared contract or documents
  an intentional adapter mapping.
- Cross-skill output conventions are stable enough for downstream automation.

## Phase 5: Report Safety Layer

Status: Done

Goal:

Audit generated or user-provided finance reports for over-strong conclusions
when evidence is stale, weak, missing, or incomplete.

Target:

```text
finance_report_safety
```

Target case:

```text
finance_report_overclaim_contract
```

Required additions:

- `contracts/report-safety.schema.yaml`
- `adapters/report_audit_adapter.py`
- report-safety fixtures with weak-evidence examples
- assertions for source/date visibility, sample limitations, advice separation,
  and overclaim detection

Done when:

- A report can be audited without re-running the producing skill.
- Weak evidence produces `REVIEW`.
- Investment-advice style language is surfaced for review.

## Phase 6: MCP And Live Probe Layer

Status: Deferred

Goal:

Add read-only probes for the future `quant_data` MCP after that MCP exists.

Target:

```text
quant_data_mcp
```

Target case:

```text
quant_data_mcp_schema_probe
```

Required additions:

- `adapters/mcp_probe_adapter.py`
- schema-version checks
- source metadata checks
- empty-result handling checks

Done when:

- Live mode is explicitly selected.
- Network or external-service failure normally produces `REVIEW`, not hard
  `FAIL`.
- Probe responses expose enough schema and source metadata for downstream
  workflows.

## Phase 7: Plugin Promotion Readiness

Status: Future

Goal:

Decide whether the Quantification skill, MCP, script, contract, and harness
bundle is stable enough to become a plugin candidate.

Promotion prerequisites:

- Protected target set is stable.
- Contracts are reused by more than one workflow.
- `quant_data` or equivalent shared tooling exists.
- The harness verifies the installed bundle end to end.
- The user approves plugin packaging separately.

Done when:

- A plugin-candidate review can be generated without moving or packaging files
  automatically.

## Operating Rules

- Keep this harness project-local until the workflow stabilizes.
- Add targets explicitly; do not scan all skills and force them into the
  harness.
- Keep fixtures small and offline by default.
- Prefer adding reusable oracles over case-specific judgment code.
- Use `REVIEW` for evidence-quality problems that require human judgment.
- Use `FAIL` for local contract, schema, output, or safety violations.
- Do not edit skill folders, inventory files, migration ledgers, or plugin
  metadata from harness runs.
- Do not fetch live data unless live mode is explicitly requested.

## Immediate Next Action

Use the harness after finance skill, script, contract, fixture, or report-safety
changes:

```powershell
py ${QUANTIFICATION_ROOT}\.agents\harnesses\quant-research-reliability\run_harness.py --quick
py ${QUANTIFICATION_ROOT}\.agents\harnesses\quant-research-reliability\run_harness.py --full
```

Next build work is Phase 6, but only after the future `quant_data` MCP exists.
Until then, keep the live probe deferred and maintain the offline harness as the
source of reliability evidence.
