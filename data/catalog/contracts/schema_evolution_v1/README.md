# Schema evolution and field coverage contract v1

Status: `INSPECTION_ONLY__FAIL_CLOSED__NO_FORMAL_MUTATION`

This contract classifies a transition between two strict logical schemas. It
does not migrate data, update `datasets.json`, promote quality, publish a
snapshot, schedule a job or authorize research use.

## `field_coverage_start`

Every field must declare `field_coverage_start` as an exact ISO date. It means
the first date from which that field is supported by evidence under this
schema. It is not inferred from table coverage, the first non-null row, the
download date, or the current schema.

Rules:

1. `field_coverage_start` may not precede `dataset_coverage_start`.
2. Every primary-key field must be non-nullable and must cover
   `dataset_coverage_start`.
3. An existing field's coverage start is immutable in an automatic comparison.
   Moving it earlier can create false historical coverage; moving it later can
   invalidate past consumers. Either change is breaking and requires a
   separately reviewed migration/evidence package.
4. A newly added field must state its own coverage start. It is automatically
   compatible only when nullable, so rows before its coverage start remain
   representable without fabrication.
5. Missing, invalid, duplicated or unknown field metadata is
   `UNKNOWN_FAIL_CLOSED`.
6. A semantic change must advance the numeric schema version. Version
   regression, or changing fields while reusing the same/lower numeric version,
   is breaking.

## Classifications

| Classification | Decision | Meaning |
|---|---|---|
| `NO_CHANGE` | `ACCEPT_INSPECTION_ONLY` | Same semantic schema; field order and version label may differ |
| `COMPATIBLE_FIELD_ADDITION` | `ACCEPT_INSPECTION_ONLY` | Only nullable fields with valid coverage starts were added |
| `BREAKING_CHANGE` | `REJECT_FAIL_CLOSED` | Field deletion, type change, nullability change, primary-key change, dataset/field coverage change, required field addition, version regression, or semantic change without a numeric version advance |
| `UNKNOWN_FAIL_CLOSED` | `REJECT_FAIL_CLOSED` | Invalid JSON, unknown properties/types, cross-dataset comparison or incomplete contract |

Acceptance only permits the next inspection step. It is not source admission,
formal catalog registration, quality promotion or research readiness.

## Deterministic receipt

The classifier omits wall-clock time and absolute paths. It binds raw and
canonical input SHA-256 values, sorted changes, explicit safety boundaries and
a fingerprint over the canonical receipt body.

```powershell
& 'control\runtime\data-engineering\.venv\Scripts\python.exe' `
  'control\tools\classify_schema_evolution.py' `
  --old 'data\catalog\contracts\schema_evolution_v1\examples\old.schema.json' `
  --new 'data\catalog\contracts\schema_evolution_v1\examples\compatible-add.schema.json'
```

`--output` may only create a new file below `tmp/schema-evolution/` or a new
`reports/data-governance/schema-evolution-classifier-*` directory. Existing
files are never overwritten. Accepted transitions return exit code `0`;
breaking or unknown transitions still emit a receipt and return `2`.
