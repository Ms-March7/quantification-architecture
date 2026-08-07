# dual_source_reconcile_v1

This contract defines a generic, deterministic and inspection-only comparator
for exactly two already-normalized CSV inputs.  It is implemented by
`control/tools/run_dual_source_reconcile.py`.

The caller must explicitly bind each input by source id, workspace-relative
path below `tmp/`, SHA-256 and byte count.  The caller must also explicitly
declare every key and comparison rule:

```text
FIELD|exact|0|critical
FIELD|numeric|0.000001|noncritical
```

The evidence CSV emits one union-key row with an outcome of `MATCH`,
`ONE_SIDE_MISSING`, or `FIELD_CONFLICT`.  It retains both source values, both
source ids, source row numbers, row hashes, deltas, tolerances and criticality.
There is deliberately no canonical-value column.

One-side-missing rows always fail closed.  A field conflict fails closed when
that field is declared critical; a blank critical value on either or both
sides is a field conflict.  Noncritical conflicts remain visible and
produce a qualified inspection pass.  The CLI exits `3` after writing evidence
for a critical reconciliation gap, `2` for an invalid run, and `0` only for a
non-failing inspection result.

Example (PowerShell):

```powershell
python -X utf8 .\control\tools\run_dual_source_reconcile.py `
  --run-id example-v1 `
  --left-spec 'SOURCE_A|tmp/source-a/normalized.csv|<sha256>|<bytes>' `
  --right-spec 'SOURCE_B|tmp/source-b/normalized.csv|<sha256>|<bytes>' `
  --key symbol --key trade_date `
  --field-rule 'close|numeric|0.000001|critical' `
  --field-rule 'route_code|exact|0|noncritical' `
  --output .\tmp\dual-source-reconcile\example-v1 `
  --execute
```

Passing this contract does not establish source rights, PIT semantics, formal
admission, catalog registration, quality promotion, an immutable research
snapshot, or research readiness.
