# Amendment 3 (November 13, 2026): skipped checks in native verifier reports

Linked to the pre-registration tag `prereg-confirmatory-2026-11-13` (protocol SHA-256
`24c2204dbc243fcdd11097886902d7fb592f105cbc9d76e62a76110cbb8a7a9c`). The tag and its files are unchanged. Owner
approval: November 13, 2026, 12:10 UTC, with six conditions (below). The wording went into the procedure page first.
No test result had been produced when this amendment was written.

## What was wrong

The first study's grading code refuses any native verifier report that contains a skipped check. The court-form-filling
verifier skips its checkbox checks when it cannot read checkbox data from the produced PDF. Those reports were complete
and internally consistent, but they were classified as infrastructure failures (`native_sidecar_not_verified`) rather
than graded. This affected two calibration cells (court-form-filling `workflow` and `rule_formula`, attempt 1 of each).
Both attempts also ended in the relay's own text-only refusal of an agent image request, so the existing image rule
applied as well. Rule S4 paused calibration at 4 infrastructure failures in 20 resolved attempts.

## The correction (owner's conditions)

1. A report with skipped checks is accepted only when the report, the frozen check identities and counts, the terminal
   totals and the native reward are all consistent. The native pass/fail grade is the verifier's own and is unchanged.
   Skipped checks count as not passed against the fixed denominator, and each skip reason is kept.
2. The other safeguards are unchanged. Missing or corrupt reports, unrelated setup errors, genuine transport failures,
   other incomplete coverage (deselected, xfailed, xpassed) and unverified image-refusal cases remain infrastructure.
3. Every recorded infrastructure failure was re-read uniformly. In each cell, the earliest attempt that qualifies under
   the corrected rules is adopted, including cells whose replacement had already been graded. Later attempts are kept
   as diagnostics, whichever scored better.
4. Every original receipt, classification, grade and cost is preserved. The corrected choice is a separate adoption
   record (`adopt:`). Nothing is overwritten, and two attempts of one cell are never counted as two observations.
   Derived calibration summaries read the adopted attempt.
5. The correction was tested offline, including a case where the earlier valid attempt scores worse than its
   replacement (`code/test_amendment3.py`). The rule is enabled only by a policy that names it exactly
   (`native_skip_rule`), so the first study's grading behaves as before (its classifier tests pass unchanged).
6. S4 counts are reported after re-reading, without resetting attempt history. No model was run for this correction.

## What changed in the files

- `code/native_evidence.py`, `code/formal_verifier.py`: the gated skip rule.
- `code/recover_trial.py`: re-reads saved verifier evidence for `native_sidecar_not_verified`, as Amendment 2 does for
  setup errors. The re-reading is saved beside the original record.
- `code/controller.py`, `code/runner.py`, `code/confirmatory.py`: adoption records, S4 counting and the one-off
  re-reading command.
- `runtime_config.json`: identical to Amendment 2's except for `native_skip_rule` in each split's verifier policy and
  the amendment list (checked). Check lists, denominators, candidates, prompts and execution settings are unchanged.
- Offline suite: 94 of 94 pass (83 earlier tests plus 11 new ones).

## Re-reading of recorded attempts (applied uniformly, no model call)

| Attempt | Result |
|---|---|
| court-form-filling, `workflow`, attempt 1 | **Adopted as the cell's run:** task failed, 21 of 47 checks passed, 12 skipped (reasons kept). The relay's own image-only refusal and the agent's trace confirm the image rule |
| court-form-filling, `workflow`, attempt 2 | Earlier graded run (task failed, 46 of 47). Now kept as a diagnostic only, although it scored higher |
| court-form-filling, `rule_formula`, attempt 1 | **Adopted as the cell's run:** task failed, 21 of 47 passed, 12 skipped; same image evidence |
| court-form-filling, `rule_formula`, attempt 2 | Earlier graded run (task failed, 0 of 47). Now kept as a diagnostic only |
| court-form-filling, full skill, attempts 1 and 2 | Already resolved under Amendment 2; unchanged |
| anthropic-poster-design, `rule_formula`, attempts 1 and 2 | Stay infrastructure (transport failure; Amendment 1 case) |

The calibration outcome of both court cells stays "task failed", so the strategy table's pass counts are unchanged.

**Rule S4 after re-reading:** 2 infrastructure failures in 20 resolved attempts (before: 4 in 20). The attempt history is
kept as recorded. The owner's conditional release is recorded after these checks. Caps are unchanged: API $19 and
hosting $31, cumulative.
