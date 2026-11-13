# Follow-up study: implementation plan

November 12, 2026, revision after the second outside review (an AI technical review for engineering readiness, not human mentor approval). The procedure is the owner's Notion page "SkillLearnBench"
(https://hyper-name-abe.notion.site/SkillLearnBench-40eb1a6065448051a18dfd4cd81e3b53). **For a registered stage, the
dated frozen copy of that page in the pre-registration repository governs; later edits to the live page do not change
a registered stage.** This file adds what the page does not cover: what code runs each step, what is checked before
moving on, and what it costs. Reasons are in [`FOLLOWUP_REPORT.md`](FOLLOWUP_REPORT.md).

**Status (November 12, evening): phase 0 complete pending the owner's final approval of the pre-registration; pilot v1 done (61 attempts, $1.03 API). Earlier status: nothing paid had run except the editor-only checks (about $0.35 of API) and six minutes of a server (about
1 cent). On November 12 the owner approved the round-2 fixes and one bounded development pilot (section 0.2). The
confirmatory study needs a separate authorization after the pilot's results and measured costs are reported.**

## Decisions

| Topic | Decision |
|---|---|
| Question | Under this editing procedure and a 50% token budget, does supplying sanitized practice-execution feedback to the editor (repair) improve the selected compressed skill, compared with editing again without that feedback (retry)? |
| Models | One model. DeepSeek Flash is both the agent and the editor; both arms use the same editor. |
| Order of work | 1. Pilot (development) on a few original families. 2. Freeze. 3. Confirmatory study on the 9 new SkillLearnBench families. The second model is deferred until the confirmatory result is in. |
| Primary result | Repair minus retry in average family pass rate on the **new families that pass the technical screening** (up to 9). The 8 original families are not rerun; they appear only as the first study's results and the pilot's (development). |
| Feedback to repair | Sanitized execution feedback: a neutral id per check (check 1, check 2, ... from the task's frozen inventory), an approximate failure category for each failed check, and the agent's own final message, errors and last commands. The verifier's check names and message text are never passed on. |
| Retry | "Retry without feedback during editing." It still benefits from practice results through selection and early stopping, exactly as repair does. |
| Editing rules (both arms) | Ask again when over the length limit (up to 3 re-asks); temperature 0.7 per attempt; one re-ask for a word-for-word copy. The allowance is the same for both arms (at most 5 editor calls per attempt); actual usage is reported. |
| Repeats | 3 dev loops per family and 3 runs of each selected skill per test task, chosen by simulation (see Power). The three loops start from the same calibration-selected rewrite: they repeat editing and selection, not calibration. |
| Run order | Test: repair and retry runs for the same task back to back in random arm order, in shuffled blocks. Dev: the order of the two arms in each family and loop is drawn from the fixed seed. |
| Reporting and test | Three things reported separately: the mean family difference, its family-bootstrap interval, and every family's difference. A two-sided sign-flip test with its null stated (see Decision rule). Win counts are descriptive. |
| Small families | Families with fewer than 4 tasks are excluded. |
| All-zero default | `workflow`. |
| Not included | The teammates' blind check of the rule audit. |

## Ground rules

- **The first study is not touched.** The follow-up lives in `followup/` (code) and `private_artifacts/followup-v1/`
  (evidence, not published).
- **Same spending ledger,** with a recorded approval raising the effective cap; the stored values are not edited. A
  hashed copy of the ledger was taken before the first follow-up call.
- **Same safeguards.** Keys stay on the Mac. Verified TLS. No automatic retries of graded runs. The server is deleted
  only after a verified backup and the owner's approval.
- **Same agent settings** as the first study, and the first study's task runner unchanged.
- **Development and confirmation are kept apart.** Anything tuned during the pilot is development. Each failed version
  is kept. The method is frozen before the confirmatory study, and nothing changes after that.

## Code

| File | Does | Status |
|---|---|---|
| `followup/editing.py` | Rewrite request (first study's prompt), length re-ask, edit requests, copy check | Built, tested |
| `followup/feedback.py` | Sanitized execution feedback: neutral check ids, failure categories, agent's own trace; check-level scoring against the frozen inventory | Built and tested; categories checked on 560 saved reports |
| `followup/audit_feedback.py` | Offline audit of categories and check inventories on saved verifier reports | Run November 12 |
| `followup/confirmatory.py` | Confirmatory driver: phases 1 to 3 with the S1, S2, S3 and S5 gates, then the frozen analysis | Offline check passes |
| `followup/confirmatory_config.py`, `followup/screen_inventory.py` | Frozen per-task setup: check lists from each verifier's own JUnit report (parametrized checks counted individually), verifier settings, output lists | Built November 12 |
| `followup/build_preregistration.py` | Assembles the public pre-registration with a manifest of SHA-256 hashes | Built November 12 |
| `followup/power_sim.py` | Saved power simulation (seed, simulation count, Monte Carlo errors) | Run November 12 |
| `followup/client.py`, `budget.py` | Editor calls through the original ledger under the approved cap | Built, tested |
| `followup/gates.py` | Editor-only checks | Built, run |
| `followup/controller.py` | Calibration, dev loops, paired test runs, stop rules; resumable | Built, tested with a fake editor and runner |
| `followup/analysis.py` | Primary and secondary measures, exact test, delivery accounting, manifest check | Built, tested |
| `followup/runner.py`, `remote_run.py` | One agent run on the server through the key relay | Written; first real check is the pilot |
| `followup/tests/` | Offline tests, no model calls | 33 passing |

---

## 0. Before any run

### 0.1 Editor-only checks (done)

| Check | Target set in advance | Result |
|---|---|---|
| Length: calibration rewrites within the cap | 30 of 32 | **28 of 32: the target was missed.** First study: 10. The 4 misses are one strategy (`source_native_compact`); the front matter and mandatory code blocks take at most 44% of the cap for that strategy, so it is not mathematically impossible, the editor simply did not cut enough. Under the calibration rule those rewrites do not qualify. The target is revised for the freeze to: the chosen strategy fits in every pilot family. |
| Editing: edits differ from their input and fit | 90% | 9 of 10 (first study: 1 of 27). "Differs" means the text changed, not that the change is useful. |
| Separation: first-attempt repair and retry requests | Differ only by the feedback block | Passed for the first attempt. After the first edit the arms have different current versions by design; what is checked from then on is that the shared instructions match and that retry never receives feedback. |

### 0.2 Pilot (development, on original families)

Purpose: show the whole corrected pipeline working end to end before the method is frozen. It is not a test of the
hypothesis, and its results can be looked at freely.

| Item | Value |
|---|---|
| Families | `earthquake-plate-calculation`, `schedule-planning`, `weighted-gdp-calculation` (their rewrites fit and fail practice tasks, so feedback exists) and `financial-analysis` (its rewrite only fits with the length re-ask) |
| Strategy | `workflow`, fixed; no calibration step in the pilot |
| Scope (Notion C1) | Development only; results do not count toward the confirmatory result. Fixed strategy `workflow`, no calibration step and no calibration comparison. One dev loop, with up to two editing attempts per arm under the existing rules. Each selected skill runs once on each test task. Order: `earthquake-plate-calculation` first, then the other three. |
| Steps | Full skill and initial rewrite on the 2 practice tasks; repair and retry edits (up to two attempts per arm); each selected skill once on each test task |
| Agent runs | About 60 to 70 per pass. One pass first. |
| Pass checks | Every run graded or given a recorded infrastructure reason. Repair receives feedback wherever a failed practice run exists; retry never does. Which families went through the complete path is reported; where editing was not exercised, the reason is reported (a finding, not a failure, Notion N14); the editing and selection machinery passes offline tests. The selected skill follows the fixed rule (recomputed from the recorded practice results). The identity (hash) and reason of every selected skill are recorded. Feedback packets contain only neutral check ids, categories and the agent's own output. **Observations, not requirements:** whether the two arms end with different skills, how much each edit changed the text (normalized token edit distance), the share of valid and changed edits. |
| Ceilings (all revisions together) | 150 physical agent attempts (infrastructure replacements count), $2.50 of API (every editor call and re-ask counts), $5 of hosting (setup and idle time count). Ceilings, not targets, inside the cumulative caps of $16 and $31. The pilot stops at whichever is reached first and is reported as it stands. |
| Expected cost | About $1 of API and $2 to $3 of hosting for one pass with setup |

Rerun only what a documented fix requires. Identical selected skills or disappointing performance alone are not reasons to rerun. Each revision has a version label; failed versions are kept as development history.

Feedback checks done before the pilot (November 12, offline): the categories were run on all 560 saved verifier reports of the first study (1,784 failures, 410 distinct) and a seeded sample of 60 distinct failures was read by hand. The first rules sent file-existence failures (`isfile`, "must exist") to `wrong_value`; the rules were corrected once and now classify on the message line, not the traceback. Check names in the original families do carry values (for example parametrised names with folder names and expected file maps), which is why neutral ids are used. Access boundary: the first study's runner stages the task without its `tests/` folder (`verifier_visible_during_solving: false`); the verifier runs afterwards in a separate container with `tests/` mounted read-only, so the agent's trace cannot contain the verifier's reference material.

#### Round-3 requirements (approved November 12, Notion N13 to N20), implemented offline

| Requirement | Implementation | Offline evidence |
|---|---|---|
| One family's complete path first (N13) | `followup/pilot.py --families earthquake-plate-calculation`, then all four in the same journal | Pilot check passes |
| Limits are one running total (N13) | Attempts from earlier pilot versions are added (`attempts_offset`); API from the ledger since the pilot started; hosting from the pilot's hosting log | Test: ceiling reached with an offset |
| Why editing was not exercised (N14) | `path_report`: no editing needed (initial passed both), editing attempted with no skill within the limit, or edited and evaluated; complete path per family | Tests with passing and failing fixtures |
| Never rerun a graded run (N15) | The runner marks a run graded before collecting evidence; a later failure pauses the stage (`EvidencePending`) and is recovered by re-copying; a dispatched attempt with no outcome is reconciled, never relaunched; if the remote status is uncertain the stage stops | Tests reproducing the reviewer's path (graded, copy fails) |
| A report must be valid (N15) | `score_junit` refuses a missing, empty, unreadable, duplicated or out-of-inventory report (`EvidenceError`); one re-copy, then pause; never a zero. Applicable, executed and passed counts kept apart | Tests |
| Hidden answers (N16) | Staging uses the first study's adapter, which removes the `/tests` and `/logs` mounts from the solver container and runs the verifier afterwards in a separate container; the task's `solution/` is never staged. `followup/probe_solver.py` checks this on the live server for each family before any paid run | Probe flags the unpatched upstream harness and passes the staged one (saved files) |
| Essential outputs kept (N17) | The server exports each run's required output files from the post-run image into a tar with its hash; the Mac verifies the hash; if export fails, the stage pauses before any cleanup | To validate on the server |
| Shared check descriptions (N18) | `followup/check_descriptions.json` (AI-written, practice checks only, frozen); both arms receive them keyed by neutral id; repair alone receives outcomes, categories and trace | Test: identical for both arms |

### 0.3 Freeze (pre-registration)

The new-family screening (0.4) comes first; the frozen family list is the families that pass it. After the pilot and
before any confirmatory run, a dated public tag in the pre-registration repository freezes: a
dated copy of the Notion procedure, this plan, the editor prompts, the feedback format, the scoring rules, the
analysis script, the family list and splits, each test task's check inventory (the frozen list of applicable checks,
from the verifier's collected tests on a reference output), the seeds, the stop rules, the retention plan and the code
hashes.

### 0.4 New families: collection and suitability

Collect the 9 families with the Milestone 1 scripts at the pinned commit. For each: every task has its verifier; the
container builds; the verifier runs on a reference output; a documented check that no test task duplicates a
calibration or practice task, covering exact duplicates and reworded versions of the same underlying problem. A family or task that fails is excluded by these criteria only and recorded before the confirmatory freeze. Fewer than 9 families may remain. This screening prevents task overlap; it is not a statistical equivalent of the first study's 14-case sensitivity analysis, which belongs only to the original 8 families and is not part of the follow-up's confirmatory analysis (Notion C3).

Families: `anthropic-poster-design` (5 tasks), `chinese-poem-generator` (5), `court-form-filling` (6),
`dependency-vulnerability-check` (5), `enterprise-information-search` (6), `github-repo-analytics` (5),
`offer-letter-generator` (6), `travel-planning` (5), `video-object-counting` (5). Excluded by the fewer-than-4 rule:
`fix-security-bug` (3), `nlp-paper-reproduction` (3), `python-scala-translation` (2). These are new task instances in
families the method is developed on (calibration and practice tasks), not transfer to unseen families.

---

## 1. Calibration: choose one compression template from Xing

| Step | What runs | Pass check |
|---|---|---|
| 1.1 | Full skill on each calibration task | Every run graded |
| 1.2 | Each of the four strategies writes a rewrite per family, with the length re-ask | Each rewrite recorded as within the cap or not qualifying; nothing cut by code |
| 1.3 | Each qualifying rewrite on its family's calibration task | Every run graded |
| 1.4 | Most qualifying passes over the stage's calibration tasks, one per family (9 in the confirmatory study, or the final screened family count; Notion C2); tie → lower average execution cost on the calibration tasks where every tied strategy ran; still tied → fixed order; all zero → check the full skill works, then `workflow` | Choice recorded with counts and costs |
| 1.5 | Freeze the chosen template and settings | Hash recorded |

Calibration uses task outcomes by design; the later length check (S2) is applied to a strategy chosen this way.

## 2. Dev: repair or retry

Run 3 times per family, each loop starting from the same calibration-selected rewrite (the loops repeat editing and selection, not calibration). The order of the two arms in each family and loop is drawn from the fixed seed.

| Step | What runs | Pass check |
|---|---|---|
| 2.1 | Full skill and the initial compressed skill on the 2 practice tasks | Every run graded |
| 2.2 | If the initial skill fits and passes both, keep it unchanged in both conditions | Recorded as "no editing needed" |
| 2.3 | Otherwise up to 2 more attempts per condition, under the editing rules above | Each attempt recorded as valid-and-changed, valid-but-unchanged or invalid, with editor calls used |
| 2.4 | Repair also receives sanitized execution feedback from its failed practice runs; retry receives none | Shared instructions match; retry requests carry no feedback |
| 2.5 | Each version within the cap runs on both practice tasks; stop early if one passes both | Every run graded |
| 2.6 | Select per condition (Notion N21, amended November 12, 2026, identical for both conditions), among versions that are eligible and have valid graded results on both practice tasks: most practice tasks passed, then the higher mean check-level practice score (per task passed/applicable checks, averaged equally over the two tasks; never pooled), then shorter, then earlier. The starting rewrite stays eligible; the early-stopping rule is unchanged; retry's editor receives no execution feedback; nothing fits → the condition is unsuccessful on that family's tests | Selection recorded with its hash, reason and selector version (`followup/controller.py`, `select`) |
| 2.7 | Report, per family and loop, which situation applied: no editing needed; edited with feedback available; edited with no run to report; nothing fits | Reported before the primary result is read |

## 3. Test: evaluate the frozen skills

| Step | What runs | Pass check |
|---|---|---|
| 3.1 | Freeze the selected repair and retry texts of every dev loop | Hashes recorded before any test run |
| 3.2 | Each selected skill 3 times on each test task, in matched blocks: repair and retry for the same task back to back in random order. Identical selections share one run and are flagged as shared, not counted as independent evidence. | Every run graded; rows match the frozen task list |
| 3.3 | Primary: mean over the new families of repair minus retry in pass rate, its family-bootstrap interval, every family's difference, and the sign-flip test with its null stated | Computed by the frozen analysis script, which refuses rows that do not match the frozen manifest (tasks and check counts) |
| 3.4 | Secondary: check-level score = passed checks divided by the task's frozen list of applicable checks, averaged within task, then family. A check skipped or not reached because the agent did not finish or its output was missing counts as not passed; a check is left out only if marked not applicable in advance, for both arms; a run lost to infrastructure gets a replacement run, not a partial score; skip reasons are kept. Family win counts are descriptive. None of these can turn an inconclusive primary result into a success. | Computed the same way |
| 3.5 | Report instruction length, preparation cost, per-use cost, editor calls used per arm, and edit size per arm | Included |
| 3.6 | Back up and verify evidence; delete the server only with the owner's approval | Checksums match |

---

## Stop and pause rules

| # | Rule | What happens |
|---|---|---|
| S1 | The ledger checks the cap before every call or run; the owner is warned at 80% | Stop; caps are never raised without the owner |
| S2 | After calibration, the chosen strategy's rewrite must fit the cap in at least 75% of the stage's families | Stop and report before dev |
| S3a | Implementation defect, before any test run: wrong information delivered (a repair call had a failed run available but carried no feedback, or a retry call carried feedback), malformed records, missing grading evidence | Pilot: fix and rerun what the fix requires. After the freeze: stop and report |
| S3b | Little opportunity: no editing needed, few valid or changed edits, little feedback available | Reported before test results are read; never a reason to change the method after the freeze |
| S4 | Infrastructure failures before grading (provider or network error with no response, container build or start failure, verifier crash before grading, server or tunnel loss) get one replacement run with its own linked identity; a graded run is never re-run; the 3 planned runs per task are observations, not retries. A second infrastructure failure in the same cell stops the stage immediately. The failure percentage is computed over cumulative resolved executor attempts in the registered stage (original attempts and replacements; not editor calls or shared result rows; a restart does not reset it); once at least 20 attempts have resolved, more than 10% failures pauses the stage (amended November 12, 2026; `controller.infra_counts`). The check runs before admitting every new attempt and after every resolved attempt, so a pause holds across restarts; grades are saved before pausing and evidence recovery continues without model runs; S4 is also checked immediately before every editor API dispatch (first answers and re-asks). Only the owner's explicit, recorded approval releases a pause, after the cause and corrective action are documented (`confirmatory.py --acknowledge-s4`); cumulative counts, history, caps and the one-replacement-per-cell limit are preserved. | Replace once; stop on a second failure in a cell; pause on the share |
| S5 | No confirmatory test result is looked at until every test run has finished | Throughout the test step |
| S6 | Matched blocks with random arm order, fixed seed | Throughout the test step |
| S7 | Pause if the model's fingerprint changes, the Mac's free space reaches the 20 GB floor, or the DeepSeek balance runs out. Runs without a reported fingerprint are counted and reported; a missing fingerprint is not treated as evidence of a stable model. | Pause |

During development (the pilot), fixing a failed check and rerunning is normal and is recorded. After the freeze, a
stop is reported as a deviation and the confirmatory study is not quietly restarted.

## Decision rule and what can be claimed

- **Reported separately:** (a) the mean over families of (repair's pass rate − retry's), each family's rate averaged
  over its test tasks, dev loops and runs; (b) its uncertainty, a family-bootstrap 95% interval, resting on the 7
  confirmatory families; (c) every family's own difference.
- **Test:** a two-sided family-level sign-flip test at 5%, under the assumption that, under the null, the joint
  distribution of family differences is invariant to independently reversing their signs. The estimated average
  difference and all family differences are reported separately. The test is not an assumption-free test of zero
  average effect. A rejection is worded "the sign-flip test rejected its null, and the estimated average difference
  favored repair" (or retry).
- With m families showing a nonzero difference the smallest possible p-value is 2/2^m, so 5 or fewer cannot reject.
  No family is removed for this reason; the opportunity pattern (S3b) is reported before test results are read.
- **A tie is not equivalence.**
- **Pooling:** results obtained with different strategies or procedures are reported as separate groups; first-study
  results are never pooled with new results.

## Pilot amendment and replay (Notion N21, N22)

The pilot (v1) used the original rule (practice passes, then shorter, then earlier); its selections and results are
kept unchanged. Offline replay of its practice records (`followup/replay_selector.py`, no model call): under the
original rule both conditions selected the same text in **3 of 4** families; under the amended selector in **1 of 4**.
The replay is not evidence of improved test performance. The power and cost projections below are refreshed before the
freeze across plausible ranges of identical selections and task success.

### Planning history (November 12, before screening): refreshed projection (N22), 9 families at 3 × 3

`followup/power_sim.py --refresh` (seed 20261112, 4,000 simulated studies per cell, output in
`results/followup/power_sim_refresh.json`). Chance that the sign-flip test rejects, by the share of dev loops in which
both conditions keep the same text and the share of families that need editing (task success around 25% and 45% gave
nearly the same figures; the realized average effect is shown in brackets):

| Same text in | Editing needed in | No effect | Effect 15 where editing happens | Effect 25 |
|---|---|---|---|---|
| 25% of loops | 75% of families | 2% | 14% (+8) | 33 to 35% (+13) |
| 50% | 75% | 1% | 7% (+5) | 19% (+8) |
| 25% | 50% | 0.3% | 3% (+5) | 7 to 8% (+8) |
| 75% | 75% | 0 to 0.3% | 1% (+3) | 2% (+4) |

With the pilot's original rate (3 of 4 identical) the test can almost never reject; with the amended selector's replay
rate (1 of 4) power for a large effect is about one in three. The study remains estimation-first.

## Power for the confirmatory study (current)

`followup/power_sim.py --refresh --task-counts 2,2,3,3,3,2,2` (seed 20261112, 4,000 simulated studies per cell; output
`results/followup/power_sim_confirmatory.json`, frozen as `design/power_simulation.json`): 7 families, the exact test
task counts, 3 dev loops × 3 runs, 593 planned agent runs. Editing needed in 75% of families, base rate around 45%.
Chance that the sign-flip test rejects (realized average effect in brackets); interval width is the mean width of the
family-bootstrap 95% interval. All figures are simulation-dependent projections under these assumptions, not
guarantees.

| Same text in | No effect | Effect where editing happens 15 (realized) | 25 (realized) | Interval width (no effect) |
|---|---|---|---|---|
| 25% of loops | 0.8% | 5% (+8) | 13% (+12) | about 15 points |
| 50% of loops | 0.3% | 2% (+5) | 5% (+8) | about 12 points |
| 75% of loops | 0% | 0.2% (+3) | 0.3% (+4) | about 8 points |

The study is estimation-first; an inconclusive test is the likely outcome.

## Power, planning history (November 11 to 12, before screening; superseded by the section above)

`followup/power_sim.py`, seed 20261112, 4,000 simulated studies per cell, output in
`results/followup/power_sim.json` (with Monte Carlo standard errors, all about 0.002 to 0.007). Assumptions are listed
in the program; the main ones: 9 families with 2 or 3 test tasks, editing needed in 75% of families, both arms
selecting the same text in 30% of loops, editor noise sd 12 points, family base rates around 45%. "Effect where
editing happens" is the effect on families and loops where the arms selected different texts; the realized average
over all families is about half of it and is shown.

| Scenario | Realized average | 2 × 2 | 2 × 3 | 3 × 2 | 3 × 3 |
|---|---|---|---|---|---|
| No effect (false positives, either direction) | 0 | 0.8% | 1.5% | 1.6% | 1.7% |
| Uneven, zero average (+7.5 / −30 points) | 0 | 1.3% | 1.4% | 1.9% | 1.7% |
| Effect 15 points where editing happens | +7 | 5% | 8% | 11% | 14% |
| Effect 25 points where editing happens | +12 | 14% | 21% | 25% | 33% |
| Effect 15, editing needed in 90% of families | +8.5 | 10% | 13% | 16% | 21% |
| Effect 15, editing needed in 50% of families | +5 | 1% | 1% | 2% | 2% |
| Effect 15, same selection in 50% of loops | +5 | 2% | 4% | 5% | 7% |
| Mean width of the 95% interval, effect 15 | | 23 pts | 21 pts | 20 pts | 18 pts |
| Agent runs at most | | 427 | 509 | 617 | 742 |

3 × 3 has the most power and the narrowest interval, and more power per agent run than 2 × 2 (33% for 742 runs
against 14% for 427 at the larger effect), so it is kept. The uneven zero-average case does not raise false
positives above about 2%. The study is **estimation-first**: with up to 9 families it can only detect a large effect,
and an inconclusive test is the likely outcome even then. These figures replace the earlier unsaved table, whose
effects were not labelled with the realized average.

## Cost and time

About $0.014 of API and 8.5 minutes of server time ($0.112 per hour) per agent run, from the first study's averages.
Editor calls with feedback are larger than before; they are included below at about $0.02 per attempt.

| Step | Agent runs | New API | New hosting |
|---|---|---|---|
| Pilot (4 original families), one pass | 60 to 70 (ceiling 150 across revisions) | about $1 (ceiling $2.50) | about $2 to $3 with setup (ceiling $5) |
| New families: setup and suitability checks | 0 | $0 | about $2 |
| Confirmatory study (7 families, 17 test tasks, 3 × 3): at most 35 calibration + 252 dev + 306 test = 593 planned agent runs; infrastructure replacements and editor calls counted separately | up to 593 plus replacements (about 650 attempts budgeted) | about $10.5 to $12 (pilot rate about $0.016 per attempt) | about $8 to $9 |

Planning history: the pre-screening estimate assumed 9 families and 747 runs (45 + 324 + 378).

Spent before the confirmatory study: API about $5.45, hosting about $15.9 (cumulative, including the first study and
the pilot). Approved cumulative caps (November 12, 2026): **API $19, hosting $31**.

## Evidence kept

For every run: the verifier's report, the agent's trace, the selected skill text, the ledger records, the run
record, the per-check outcomes with skip reasons, and the agent's output files that the verifier reads (every essential output is kept, with verified retrieval; if one
cannot be stored, the stage stops before cleanup; Notion N17). That is enough to verify the recorded result. Full container snapshots, which allow re-running the native
grader, are kept for a seeded sample only; the write-up will say "verified from recorded evidence" for the rest.
