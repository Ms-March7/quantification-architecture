# Revision event ledger v1

Status: `INSPECTION_ONLY__READ_ONLY__NO_FORMAL_MUTATION`

This contract represents revisions, vintages, late arrivals and withdrawals as
an append-only UTF-8 JSONL event stream. It is generic: `business_key` identifies
the fact, while `vintage` distinguishes source vintages or effective periods.
Sources remain separate because `source_id` is part of the revision-chain key.

Replay is deliberately fail-closed:

- `available_at` and `observed_at` require explicit timezone offsets;
- replay visibility requires both timestamps to be no later than
  `decision_time`, preventing a late local arrival from leaking backward;
- revision numbers are contiguous and strictly increasing within a chain;
- an UPSERT revision after revision 1 must supersede the current chain head;
- a WITHDRAWAL must target an existing current active UPSERT in the same chain;
- exact duplicate `event_id` content is idempotent, while changed duplicate
  content, reused revision IDs and conflicting chain revisions are rejected;
- no event is silently rewritten, deleted or inferred.

Validate:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  .\control\tools\replay_revision_event_ledger.py `
  --events .\data\catalog\contracts\revision_event_ledger_v1\fixtures\valid.jsonl `
  --validate-only
```

Replay:

```powershell
& .\control\runtime\data-engineering\.venv\Scripts\python.exe `
  .\control\tools\replay_revision_event_ledger.py `
  --events .\data\catalog\contracts\revision_event_ledger_v1\fixtures\valid.jsonl `
  --decision-time 2026-04-12T12:00:00+08:00
```

The tool reads files and writes JSON only to stdout. A PASS does not prove
source rights, completeness, official semantics, first-publication coverage or
research readiness.
