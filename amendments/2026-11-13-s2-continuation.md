# Amendment 4 (November 13, 2026): continuation after the S2 stop

**Adopted after calibration, before any dev or test run.** Linked to the pre-registration tag
`prereg-confirmatory-2026-11-13` (protocol SHA-256 `24c2204dbc243fcdd11097886902d7fb592f105cbc9d76e62a76110cbb8a7a9c`).
The tag and its files are unchanged. Owner approval: November 13, 2026, 15:11 UTC. The wording went into the procedure
page first. Every result from the dev and test phases is labelled as obtained under this amendment.

## What happened

Calibration finished with 28 graded runs (33 attempts). The frozen selection rule chose `source_native_compact`: it tied
with `api_code` at 2 calibration passes, and the cost tie-break chose it ($0.023 vs $0.027 mean per run). Rule S2 then
stopped the study before dev, because `source_native_compact` fits the 50% cap in only 5 of 7 families
(chinese-poem-generator and travel-planning were over the cap). S2 requires at least 75%, which means 6 of 7.

| Strategy | Calibration passes | Fits the cap |
|---|---|---|
| source_native_compact | 2 | 5 of 7 |
| api_code | 2 | 6 of 7 (not travel-planning) |
| workflow | 1 | 5 of 7 |
| rule_formula | 1 | 5 of 7 |

The original selection, all calibration records and the S2 failure are kept as recorded (`continuation.json`).

## The amended rule (exact)

Among the strategies tied for the highest calibration pass count, keep those that meet the existing S2 threshold
(the strategy's rewrite fits the cap in at least 75% of families). Then apply the existing tie-breakers: lower mean
execution cost on the calibration cases where every retained strategy ran, then the fixed order. If none of the
top-tied strategies meets S2, stop. The threshold is not lowered, and a strategy with fewer passes is never chosen.

**This selects `api_code`.** It is the only top-tied strategy that meets S2.

## Conditions applied

- Its existing rewrites and calibration evidence are reused. No rewrite was regenerated, and calibration was not rerun.
- All seven families are retained. travel-planning's `api_code` rewrite is over the cap and is handled by the existing
  eligibility rules: an ineligible starting rewrite is not run, and both arms edit from it under the same cap. No family
  was excluded.
- The continuation was frozen before dev: the chosen strategy and the SHA-256 of every family's starting rewrite are in
  `continuation.json` and in the study journal's S2 gate record.
- The design, the analysis, the stop rules and the cumulative caps (API $19, hosting $31) are unchanged.

## Validation (offline)

`code/test_amendment4.py` covers these cases:
- the original rule is unchanged;
- the amended rule keeps only top-tied strategies that meet S2;
- it never selects a lower-scoring strategy (it stops instead);
- the existing cost and fixed-order tie-breakers apply among the retained strategies;
- the continuation is recorded without any editor or agent call, and without overwriting any record;
- the amendment does not apply when the original choice meets S2.

The selector was also checked on the study's own journal: the rebuilt calibration table reproduces the original choice
exactly before the amended rule is applied. Offline suite: 100 of 100 pass.
