# Pre-registration: does execution feedback help repair a compressed agent skill?

A CIS 5300 (University of Pennsylvania, Fall 2026) course project. This repository freezes the confirmatory study's
procedure, design, code and analysis **before its first agent run**. The dated git tag is the pre-registration;
nothing listed here changes after the tag. Anything decided later is reported as a dated, labelled deviation.

## Question

When an agent skill must fit in 50% of its original instruction tokens, does supplying sanitized practice-execution
feedback to the editor (**repair**) improve the selected compressed skill, compared with editing again without that
feedback (**retry**), under this editing procedure?

## Design in brief

- **Benchmark:** SkillLearnBench, pinned to commit `726e1f79`. **7 families, 17 test tasks** (screened; see
  `history/SCREENING_REPORT.md`). Per family: 1 calibration task, 2 practice tasks, 2 or 3 test tasks.
- **Agent and editor:** DeepSeek Flash. The agent runs through the Claude Code CLI (2.1.296) in a fresh container
  with no internet access, reaching the model only through a metered relay that accepts text only.
- **Phases:** 1. calibration (choose one of four rewriting strategies) → 2. dev (3 loops; repair vs. retry on the 2
  practice tasks, up to 2 editing attempts per arm) → 3. test (each selected skill 3 times per test task).
  At most **593 planned agent runs** (35 + 252 + 306); infrastructure replacements and editor calls are counted
  separately.
- **Repair's feedback:** neutral check ids, a failure category per failed check, and the agent's own final message,
  errors and last commands. Never the verifier's check names or message text. Both arms see the same short
  AI-written descriptions of the practice checks.
- **Selection (both arms):** most practice tasks passed, then higher mean check-level practice score, then shorter,
  then earlier.
- **Reporting:** the mean over families of (repair − retry) in test pass rate; its family-bootstrap interval; every
  family's own difference; and a two-sided sign-flip test under the assumption that, under the null, the joint
  distribution of family differences is invariant to independently reversing their signs. The test is not an
  assumption-free test of zero average effect. Secondary: check-level score, costs, instruction length, editor calls.

The full procedure is the dated frozen copy of the team's procedure page in `procedure/`; it governs. The
implementation plan (`plan/`) adds what code runs at each step and what is checked before moving on.

## What to expect (stated in advance)

The simulation in `design/power_simulation.json` (exact task counts 2, 2, 3, 3, 3, 2, 2; 593-run design) gives low
power: under its assumptions, roughly 2 to 13% for average effects of 5 to 12 points, depending on how often both arms
keep the same text. These are simulation-dependent projections, not guarantees. The study is estimation-first; an
inconclusive test is the likely outcome and will be reported as such, never as equivalence.

## Limitations disclosed in advance

- The benchmark's native verifiers are used as they are; their checks can be coarse or brittle and are not
  independently validated beyond screening.
- Three families ship no reference solution. For screening only, reference outputs were built from each verifier's own
  expected values (`followup/fixtures/`). The evaluator-only fixtures demonstrate that the verifier accepts outputs
  satisfying its implemented checks; they do not independently validate the verifier or establish complete task
  correctness. They never reach the agent or the editor. Verifier files and reference answers are kept out of the agent's container (checked
  live for every family before any paid run).
- One model; 7 families; 2 or 3 test tasks per family. The original 8 families were used for development (a pilot,
  `history/PILOT_REPORT.md`) and are not part of this study.
- Design reviews were done by AI reviewers (technical review, not human approval); the owner approved every change.
- In the pilot (development only), two agent-image incidents were re-classified with an earlier, trace-only check;
  the relay did not yet log refusal reasons. The confirmatory study also requires the relay's own recorded image-only
  refusal.
- An S4 pause (more than 10% infrastructure failures after at least 20 resolved attempts) holds across restarts and is
  released only by the owner's recorded acknowledgement after the cause is fixed.

## Files

| Path | Contents |
|---|---|
| `procedure/` | Dated frozen copy of the procedure page (governs) |
| `plan/IMPLEMENTATION_PLAN.md` | Steps, checks, stop rules, power, costs, evidence kept |
| `design/` | Family splits, frozen check lists, test manifest, exclusions, seeds, budget, power simulation, server runtime config |
| `followup/`, `src_v2/`, `prompts/` | The code that runs the study, its prompt templates and the frozen analysis, with offline tests (run `python -m pytest followup/tests` from this folder) |
| `history/` | Pilot report and screening report (development, before this freeze) |
| `MANIFEST.json` | SHA-256 of every file, the protocol hash, and the source commit |
