# Amendment 2 (November 13, 2026): missing-output records for the new families

Linked to the pre-registration tag `prereg-confirmatory-2026-11-13` (protocol SHA-256
`24c2204dbc243fcdd11097886902d7fb592f105cbc9d76e62a76110cbb8a7a9c`). The tag and its files are unchanged. Owner
approval: November 13, 2026, 03:53 UTC. No test result had been produced when this amendment was written.

## What was missing

The study grades every run with the benchmark's native verifier through the first study's grading code. That code
counts a verifier **setup error** as a normal task failure only when the task has a pre-approved missing-output record:
model-free evidence of exactly which errors the verifier gives when the required output file is absent. The first
study built one per task; the frozen design for the new families did not. Without it, any run that ends without its
output file is classified as an infrastructure failure instead of a graded failure. Calibration stopped on this
(court-form-filling, full skill, both attempts).

## The correction

- For all 38 tasks, a model-free control ran the **unchanged** native verifier on the untouched input (nothing
  produced), with the study's own staging and grading path (`code/build_missing_contract.py`). The records were derived
  from these controls only, never from an agent run.
- 12 tasks give setup errors when the output is missing and receive a record: court-form-filling (6 tasks, 47 to 50
  errors each) and offer-letter-generator (6 tasks, 25 to 29 errors). The other 26 tasks report ordinary failures and
  need none.
- Both directions were checked on each record's real control evidence: with the record, the untouched control is
  graded as a native failure (reward 0); with an unrelated error signature it stays infrastructure; with the report
  missing it stays infrastructure. Offline tests cover the same cases on synthetic reports; evidence-copy failures
  and missing or corrupt reports remain infrastructure (earlier tests). 83 of 83 offline tests pass.
- Only the records were added to the verifier settings (`runtime_config.json` here): check lists, scoring
  denominators, candidates, prompts and execution settings are byte-for-byte unchanged (checked).

## Re-classification of recorded attempts (applied uniformly)

Every recorded infrastructure failure was re-read from its saved evidence under the amended rules; original
classifications are kept beside the new ones:

| Attempt | Result |
|---|---|
| court-form-filling, full skill, attempt 1 | **Adopted as the cell's graded run (task failed, 0 of 47 checks)**: output absence verified, verifier errors match the record, the relay's own image-only refusal and the agent's trace confirm the image exception |
| court-form-filling, full skill, attempt 2 | Same evidence; kept as a diagnostic attempt only (not an observation); no third model run |
| anthropic-poster-design, rule_formula, attempts 1 and 2 | Stay infrastructure (Amendment 1 case; no image refusal) |

Completed model executions are not rerun. Caps unchanged (API $19, hosting $31, cumulative).
