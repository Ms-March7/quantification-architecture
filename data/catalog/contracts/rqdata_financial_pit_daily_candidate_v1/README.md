# RQData financial PIT daily candidate worker v1

This contract drives
`control/tools/run_rqdata_financial_pit_daily_update.py`.

The worker consumes the immutable `financial/queue.json` emitted by the B-path
daily updater.  It does not modify that queue.  Each run executes at most one
queued task (400 securities, 12 quarters), writes a separate hash-sealed
quarantine candidate and advances a local resumable transaction state only
after the candidate verifies.

The unified 19:20 candidate maintenance wrapper calls the worker with
`--discover-through YYYY-MM-DD`.  Discovery is bounded to direct, hash-verified
B-path candidates and selects the oldest eligible pending queue; when none is
pending it selects the newest completed queue, which returns
`NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK` before importing the RQData client.
If no eligible local queue exists, the step is explicitly skipped without
network or writes.  The wrapper does not create a separate financial scheduled
task, and each maintenance invocation still processes at most one task.

For every returned financial fact it:

- calls `get_pit_financials_ex(..., statements="all")`;
- preserves `rice_create_tm` only as vendor metadata in the raw observation;
- conservatively derives `available_at` as 09:30 Asia/Shanghai on the first
  verified trading day strictly after date-level `info_date`;
- emits deterministic, append-only revision events at
  `(order_book_id, quarter, metric)` grain with local `revision_id`, contiguous
  `revision_no` and `supersedes_revision_id`;
- fails closed when two different values for one chain share the same
  `info_date` because their intraday order cannot be proved.

The output remains `INSPECTION_ONLY/NOT_ADMITTED`.  It does not prove source
rights, complete historical correction/withdrawal coverage, legal intraday
publication time or formal PIT admission.  A batch can observe several old
revisions at one local time, so this local chain is deliberately not claimed as
admitted `revision-event-ledger-v1` input.  It never edits catalog, Roadmap or
source registry and never runs factors.

```powershell
${QUANTIFICATION_ROOT}\control\tools\run_rqdata_financial_pit_daily_update.cmd --plan --queue ${QUANTIFICATION_ROOT}\data\market\quarantine\rqdata-b-path-daily\delta-20260804-20260805-v1\financial\queue.json
${QUANTIFICATION_ROOT}\control\tools\run_rqdata_financial_pit_daily_update.cmd --run --queue ${QUANTIFICATION_ROOT}\data\market\quarantine\rqdata-b-path-daily\delta-20260804-20260805-v1\financial\queue.json
${QUANTIFICATION_ROOT}\control\tools\run_rqdata_financial_pit_daily_update.cmd --smoke --queue ${QUANTIFICATION_ROOT}\data\market\quarantine\rqdata-b-path-daily\delta-20260804-20260805-v1\financial\queue.json
${QUANTIFICATION_ROOT}\control\tools\run_rqdata_financial_pit_daily_update.cmd --plan --discover-through 2026-08-05
```

`--smoke` is a separate live capability check capped at 5 securities, 2 fields,
4 quarters and 2 MiB measured quota.  It seals an independent smoke candidate
and never advances the queue resume state.
