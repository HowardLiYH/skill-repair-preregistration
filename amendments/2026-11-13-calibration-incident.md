# Amendment 1 (November 13, 2026): calibration incident and recovery

Linked to the pre-registration tag `prereg-confirmatory-2026-11-13` (protocol SHA-256
`24c2204dbc243fcdd11097886902d7fb592f105cbc9d76e62a76110cbb8a7a9c`). The tag and its files are unchanged; this file
records what happened afterwards. No test result had been produced when this amendment was written.

## What happened

Phase 1 (calibration), family `anthropic-poster-design`, strategy `rule_formula`, calibration task 1.

1. In the first attempt, one request from the agent to the model ended in a transport failure after dispatch had
   started; whether the provider received it is unknown. The spending ledger correctly marked it as needing
   reconciliation and then refused further requests, so the rest of the run received HTTP 503 and the attempt ended as
   an infrastructure failure (10 completed model calls, 1 with unknown outcome).
2. The one permitted replacement was refused by the ledger before reaching the model: **zero provider calls**
   (verified from the ledger and the relay's records). No remote job or container remained active.
3. Two infrastructure failures in one cell: rule S4 stopped the stage, as designed.

## Owner-approved recovery

- The unknown request was reconciled once at its exact reserved amount, $0.3342, recorded as an **unverified
  conservative allowance**, not a confirmed provider charge; the reservation stopped counting as reserved, so it is
  counted once.
- **Exactly one additional attempt** for this one cell, as an explicit exception to the one-replacement limit, with the
  same candidate, model inputs, execution settings and grading. If it fails, the study stops and reports before any
  further attempt. Both earlier attempt records and all cumulative counts are kept.
- The owner released S4 after the cause and the corrective action were documented.
- Corrective action in the code (not a design change): before any agent attempt or editor call, an unreconciled ledger
  request now pauses execution, including after a restart, so it cannot consume another attempt. Offline tests: 79 of
  79 pass. Source repository commit `b97ce76`.

Completed cells are not rerun. The spending caps are unchanged (API $19, hosting $31, cumulative).
