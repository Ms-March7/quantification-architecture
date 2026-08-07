# Ricequant R0 CORE-04/05 bounded candidate v1

Status: `COMPLETED__BOUNDED_CANDIDATE_AND_RECONCILIATION__FORMAL_EFFECT_NONE`

This inspection-only package retrieves RQData dividend, split, allotment and
ex-factor observations for an explicit Shanghai/Shenzhen candidate universe.
The immutable time scope is a 2005-2024 base plus a 2025-2026-08-03 tail.

Traffic is measured from fresh numeric quota snapshots. Incremental traffic has
a 40 MiB hard ceiling and a 36 MiB pre-call soft stop. Calls use a deterministic
32-security first batch and at most 256 securities afterwards. There is no
automatic retry and no per-security fallback. A failed endpoint is disabled for
the remainder of the run and the cursor remains recoverable.

Only normalized event fields and canonical source-row hashes may be persisted.
Credentials, account identity and raw API payloads are forbidden. The candidate
is reconciled against the existing CSMAR CORE-04 event/term and vendor-factor
observations by security plus event date, without choosing a canonical source.

This package cannot close formal CORE-04/05 by itself. Publication `available_at`,
complete revisions/withdrawals, formal identity, source rights and immutable
research snapshot admission remain separate hard gates.

The 2026-08-04 run completed all 23 batches and 92 business calls. All four
endpoints remained enabled, and measured incremental traffic was 6,488,857
bytes. The candidate and reconciliation are stored under
`data/market/quarantine/ricequant-r0-core04-core05/candidate-20260804-v1/`;
the persistent receipt is under
`reports/data-governance/ricequant-r0-core04-core05-candidate-20260804-v1/`.
