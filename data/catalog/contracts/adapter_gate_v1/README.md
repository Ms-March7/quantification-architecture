# Adapter sample gate v1

Status: `SMOKE_ONLY__NO_SOURCE_ADMISSION`

This contract turns the 2026-07-26 inspection findings into a fail-closed,
source-adapter sample gate. It checks:

1. observed raw row width;
2. one-to-one raw/normalized row count and raw-row hash lineage;
3. explicit canonical unit declarations;
4. exact normalized field set, scalar types and ISO dates;
5. non-empty and duplicate-free primary keys;
6. critical NULL/blank/non-finite values;
7. finite numeric fields, positive prices and non-negative volume/amount values;
8. OHLC consistency.

The gate deliberately does not infer field semantics from position, grant a
licence, admit an endpoint, write `datasets.json`, or promote quality. A
`SMOKE_PASS__NO_SOURCE_ADMISSION` decision is only an engineering prerequisite
for the later source-admission pilot.

`tencent_daily_inspection.json` records the observed 11-field width and the
canonical normalized units that a future adapter must declare. The mapping of
all 11 raw source fields remains blocked until it is frozen against source
evidence; this file must not be treated as an admitted production mapping.
`PERCENT_POINTS` means `1.0 = 1%`, not a ratio where `0.01 = 1%`. Matching that
declaration and binding each sample row by hash still do not prove that the
adapter applied the correct source-to-canonical conversion formula.
