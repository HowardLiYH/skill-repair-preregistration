<!-- Verbatim export of the live Notion page "SkillLearnBench" (https://app.notion.com/p/40eb1a6065448051a18dfd4cd81e3b53), fetched through the Notion connector as of 2026-11-13T01:24:21.843Z (8:24 PM EST, Nov 12). Notion-flavored Markdown exactly as returned by the connector; image blocks are Notion file references. -->
<p />

<image source="notion-file-block://40eb1a60-6544-80c0-8351-f57ae62af98f/d29d4f11-4259-46ad-9506-7e1aef26ab78?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />

## <b>SkillLearnBench workflow — GDP example</b>

<quote>
<p />

- Verifier is a Python Autograder.
- Repair Editor is the same model as the agent
- <b>Task = the specific assignment:</b> “Calculate the weighted GDP result in this workbook.”
- <b>Skill = reusable guidance:</b> “Use Excel formulas, preserve formatting, and check for errors.”

<image source="notion-file-block://40fb1a60-6544-806a-9866-c5bb2fa55c5f/df748095-df3a-43a1-a36f-bbf16e77fd66?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />

<image source="notion-file-block://40fb1a60-6544-8011-b698-e813fcf6d70f/5d572087-cd40-46a9-b188-50868d0d9085?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />

<image source="notion-file-block://40fb1a60-6544-801f-8539-fe4ae64062f9/c982faad-8623-4959-9698-6a7ca00063b0?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />
</quote>

<p />

## <b>Overview</b>

- The question: does <b><span color="red">repair</span></b> (the editor sees what failed) beat <b><span color="red">retry without feedback during editing</span></b> (the editor tries again without that information; it still benefits from practice results when the best version is selected) when a skill must fit in <b><span color="green">50% of its original instruction tokens</span></b>?
- Every family goes through the same three steps in order: <b>1. Calibration → 2. Dev → 3. Test.</b>
- <b>Splits inside each family:</b> one task is the <b>calibration</b> task, two are the <b>practice (dev)</b> tasks, and the rest are <b>held-out test</b> tasks. For GDP: GDP-3 and GDP-4 are practice, GDP-5 and GDP-6 are test. Test tasks never appear in calibration or dev.
- <b>Repeats:</b> <b>3 dev loops</b> per family and <b>3 runs</b> of each selected skill on each test task (chosen by simulation). The three dev loops start from the same calibration-selected rewrite; they repeat editing and selection, not calibration.
  - The simulation program is saved with its seed, number of simulated studies and the uncertainty of its estimates. It covers no-effect and uneven zero-average-effect cases, different opportunity and tie rates, and a cost comparison of 2 × 2, 2 × 3, 3 × 2 and 3 × 3.
  - The original pilot's identical-selection rate (3 of 4 families) is reported separately from the offline replay of the pilot's practice records under the amended selector (1 of 4 families). The replay is not evidence of improved test performance. Power and cost projections are refreshed before the freeze across plausible ranges of identical selections and task success.
  - Before the freeze the power simulation is refreshed with the actual seven-family task counts (2, 2, 3, 3, 3, 2, 2) and the 593-run design. Interval widths are reported as <b>simulation-dependent projections, not guarantees</b>.
- The work runs as a <b>pilot</b>, then a <b>confirmatory study</b> on the new families (see step 0). Each stage starts only after I approve it and state its spending caps.
- <b>Status:</b> the outside GPT reviews are AI technical reviews for engineering readiness, not human mentor approval. Pilot results and measured costs are reported before authorization of the confirmatory study is requested. Howard's Nov 12 approval covers these Notion updates, the implementation fixes and model-free tests, and the bounded pilot once the server arrangement is authorized. It does not authorize the full study or more spending.

<toggle heading="h3">
<b>0. Before any run: scope and pre-registration</b>

- <b>Which families and model, in order.</b> The same steps 1–3 are run in each stage:
  - <b>Stage 1: Pilot (development).</b> Original families, to check the pipeline. <b>Not a test of the hypothesis.</b>
    - Pilot families: <b>earthquake, schedule-planning, weighted-gdp, financial-analysis</b>.
    - The pilot is <b>development only</b>. It uses the fixed strategy <b>workflow</b> with no calibration step and no calibration comparison, <b>1 dev loop with up to two editing attempts per arm</b> under the existing rules, and runs each selected skill <b>once</b> on each test task. Families: earthquake-plate-calculation first, then financial-analysis, schedule-planning and weighted-gdp-calculation. Its results do not count toward the confirmatory result.
    - <b>Order:</b> first one family's complete path (earthquake-plate-calculation: rewrite, practice runs, feedback, editing, selection, test runs, grading, evidence kept), then the other three families.
    - Pass checks: every run is graded or has a recorded infrastructure reason; repair receives feedback wherever a failed practice run exists and retry never does; the selected skill follows the fixed selection rule; the identity and reason for every selected skill are recorded.
    - Report which families went through the complete path (execution, feedback, editing, selection, held-out evaluation, grading, evidence kept). Where editing was not exercised, report why: the starting skill already passed both practice tasks, or editing was attempted but produced no skill within the limit. Neither is a failure and neither is a reason to keep trying until something changes. The editing and selection machinery itself must pass <b>offline tests (no model calls)</b>.
    - <b>Hidden answers:</b> before any paid run, each family's actual solver environment is probed, before the verifier is injected, including its accessible mounts, to confirm that the tests and solutions are absent. The evidence is documented per family. The probes make no model calls (server time still counts toward hosting). A family that fails is held back for correction or excluded under this documented technical criterion.
    - Whether the two arms end with different skills, and how much each edit changed the text, are reported as <b>observations, not requirements</b>.
    - Pilot limits across all revisions (<b>ceilings, not targets</b>): <b>150 agent attempts</b> including infrastructure replacements, <b>\$2.50 API</b> including all editor re-asks, <b>\$5 hosting</b> including setup and idle time, within the cumulative \$16 API / \$31 hosting caps. These limits are <b>one running total across all pilot versions</b>: a new version never resets them. Failed attempts and replacements count toward attempts; every editor call and re-ask counts toward API; setup, idle time and evidence preservation count toward hosting. After a fix, only the affected work is repeated.
    - Start with one pass. Rerun only what a documented fix requires; identical selected skills or disappointing performance alone are not reasons to rerun. Stop at whichever limit is reached first. Failed versions are kept.
  - <b>Stage 2: Confirmatory study: the 7 screened families (17 test tasks).</b> SkillLearnBench has 20 families; the first study used 8. Each new family is downloaded with the same scripts. Same split in every family: 1 calibration task, 2 practice tasks, the rest held-out test.
    - The candidate families are checked technically before the freeze (verifier present, container builds, verifier accepts a reference output, no test task duplicates a calibration or practice task). The frozen list is the families that pass; exclusions use these criteria only and are recorded. Fewer than 9 may remain.
    - Technical screening proceeds under the agreed criteria and existing spending caps. Exclusions reflect documented technical incompatibility or task overlap, never disappointing model performance. The screened family list, final design and revised budget are presented to the owner before the confirmatory study is launched.
    - <b>Screening result (November 12, 2026):</b> 7 of 9 families pass and form the confirmatory study: anthropic-poster-design, chinese-poem-generator, court-form-filling, enterprise-information-search, offer-letter-generator, travel-planning, video-object-counting (17 test tasks: 2, 2, 3, 3, 3, 2, 2). Excluded: github-repo-analytics (technical: every task requires a GitHub token and live internet access, which the agent does not have) and dependency-vulnerability-check (overlap: both test tasks reword the calibration task). Approved by the owner.
    - <b>Confirmatory budget (approved November 12, 2026):</b> cumulative API cap raised to <b>\$19</b> (from \$16); hosting remains capped at <b>\$31</b> cumulative. The confirmatory study has at most <b>593 planned agent runs</b> (35 calibration, 252 dev, 306 test), plus replacements.
  - <b>Rule:</b> families with <b>fewer than 4 tasks are excluded</b>, since they have no test task left (fix-security-bug and nlp-paper-reproduction have 3, python-scala-translation has 2). This left 9 candidate families; after technical screening, <b>7 screened families</b> remain (see the screening result above).
  - Results on the <b>8 original families</b> are reported separately as <b>development-informed</b>, since their test results were already seen and shaped this design.
  - <b>Stage 3: second agent model (replication).</b> Rerun the whole procedure with a <b>different model as both agent and editor</b>, to check the result is not specific to DeepSeek Flash. <span color="red">Deferred until the confirmatory result is in.</span> The model is not chosen yet.
  - Each stage starts only after I approve it and state its spending caps.
- <b>Stop rules:</b>
  - <b>S3a Implementation defect</b> (wrong information delivered, malformed records, missing grading evidence): during the pilot, fix and rerun what the fix requires; after the freeze, stop and report.
  - <b>S3b Little opportunity</b> (no editing needed, few usable edits, little feedback available): reported before test results are read; never a reason to change the method after the freeze.
  - <b>Runs and evidence:</b> a graded run is never rerun. If collecting its evidence fails, the collection is retried on its own; the completed attempt is preserved and its evidence recovered before any replacement is considered. A verifier report counts only if it is readable, valid and belongs to the correct run. A missing or corrupted report is an infrastructure issue, never a score of zero. If the remote status of a run is uncertain, it is reconciled before another attempt is launched.
  - <b>S4 amendment (pilot v1, November 12, 2026).</b> A run counts as a graded task failure, not an infrastructure failure, only when all of these hold: (a) the request was rejected because it contained an image that the agent itself chose to read (for example by rendering a PDF page to a picture and opening it), under the text-only configuration: the relay's text-only check, which the first study also used, refuses image content before it reaches the provider; (b) this is confirmed from the agent's own trace (the rejected request directly follows the agent's image read); and (c) the native verifier graded the run. The run then keeps the verifier's grade. The rule applies the same way to both arms and to every matching pilot incident. It is <b>not a general rule for HTTP 400 errors</b>: requests malformed by our own runner or rejected by the relay for any other reason, and transport or provider failures (no response, dropped connection, provider-side errors), remain infrastructure failures. When a matching cell has already been given a replacement, the first attempt's recovered and verified grade is used for the planned run; the replacement is kept as an additional diagnostic attempt (its grade, logs and costs) and is not counted as an independent observation; the cell is not run a third time. Original records are never overwritten: the adoption of the existing evidence is recorded as a dated amendment. The first study's frozen results are unchanged, and its handling of such cases is disclosed separately. For the new families, compatibility with the text-only model and the available tools is assessed during screening, before the family list is frozen; no family is excluded because its name suggests a visual task or because its scores are poor. The pilot limits are unchanged: 150 attempts, \$2.50 API and \$5 hosting in total.
  - <b>S4 infrastructure-failure pause (amended November 12, 2026, before the freeze).</b> The infrastructure-failure percentage is computed over <b>cumulative resolved executor attempts within the registered stage</b>, including original attempts and infrastructure replacements. Editor calls and duplicated result rows are not counted. Restarting the program does not reset the count. Infrastructure failures are recorded immediately. Once <b>at least 20 attempts</b> have resolved, the stage pauses when <b>more than 10%</b> of them ended in infrastructure failure. The one-replacement-per-cell limit stays: a <b>second infrastructure failure in the same cell stops the stage immediately</b>, regardless of the 20-attempt warm-up. (This is 20 total resolved attempts, not 20 completed run records.) To be verified offline before any paid run: no pause at 1 of 3 or 2 of 20, a pause at 3 of 20, and an immediate stop on a second failure in one cell.
  - <b>S4 release procedure (owner-controlled, November 12, 2026).</b> An S4 pause or stop is lifted <b>only through a release recorded by the owner</b>. Each release requires the owner's <b>explicit approval after the cause and the corrective action are documented</b>. A release preserves the cumulative counts, the history, the budget caps and the one-replacement-per-cell limit; it never resets them. S4 is <b>enforced immediately before every editor API dispatch, re-asks included</b>.
- <b>Evidence kept:</b> every essential output file (the files the verifier reads) is kept for every run, compressed or de-duplicated where useful, with verified retrieval. 20 MB per run is a planning figure, not a cutoff. If an essential output cannot be stored, stop before cleanup and resolve storage. Full container snapshots are kept for a seeded sample only.
- <b>Limitations:</b> the benchmark's native verifiers are used as they are; their checks can be coarse or brittle and are not independently validated beyond screening. For three families without reference solutions, screening used evaluator-only reference outputs built from each verifier's own expected values; these only show that the verifier can accept a correct output, they never reach the agent or the editor, and like all verifier files and reference answers they stay inaccessible to the solver and the editor.
- Freeze this procedure, the editor prompts, the scoring rules (primary and secondary measures) and the analysis script in a <b>dated public git tag</b> before any run.
- Nothing in the procedure, scoring or analysis changes after the tag. Anything decided later is reported as a labelled deviation.
- Each later stage (confirmatory study, second model) is frozen the same way before its own runs.
- <b>The dated frozen copy of this page governs the study.</b> Later edits to this page don't change a registered stage.
- <b>Before any paid run:</b> the live hidden-answer probe is completed for every confirmatory family before any paid run. The exact public pre-registration is shown to the owner for final approval before publication, and the confirmatory study starts only after that approval.
</toggle>

<toggle heading="h3">
<b>1. Calibration: choose </b><b><span color="red">one compression template</span></b><b> from Xing.</b>

<ul>
<li>
Compare Xing’s four rewriting strategies on the <b>same calibration tasks</b>: the calibration tasks of the stage's families, one per family (<b>7</b> in the confirmatory study, one per screened family; call this N). 

<ul>
<li>
Run each task with the <span color="orange">original full skill </span>as a baseline. 
</li>
<li>
The official verifier grades the agent’s <b>output</b>—for GDP, its completed workbook.

<image source="notion-file-block://40fb1a60-6544-80cf-93bd-f8c385022974/0e80c258-40c7-423d-8a62-21643938103a?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />
</li>
</ul>
</li>
<li>
<b>Proposed selection rule:</b> 

<ul>
<li>
Count how many of the N calibration tasks each strategy passes while keeping the rewritten skill <span color="green">within </span><b><span color="green">50% of its original instruction tokens</span></b><span color="green">.</span> 
</li>
<li>
If a rewrite is over the cap, <b>ask the editor again</b>, telling it how many tokens to cut (<b>up to 3 re-asks</b>). We never cut the text ourselves. A rewrite still over the cap after that does not qualify.
</li>
<li>
Choose one shared strategy with the most qualifying passes. 
</li>
<li>
If tied,<span color="green"> prefer lower average execution cost </span>on the same cases. 
</li>
</ul>
</li>
<li>
If all four score <b><span color="red">0 of N</span></b>, check that the executor and verifier work with the full skill. 

- If the zeros are genuine, <span color="red">report </span><b><span color="red">no accuracy winner</span></b><span color="red"> </span>and use the default starting strategy <b><span color="red">workflow</span></b> (one of the four), named <b>before</b> the runs.
</li>
<li>
<b>Freeze the chosen template and execution settings.</b>
</li>
</ul>
</toggle>

<toggle heading="h3">
<b>2. Dev: repair or retry without feedback (comparison group) the compressed GDP skill.</b>

<image source="notion-file-block://40fb1a60-6544-8029-a113-c38b9e7fca28/14e227ef-ab1f-4b44-b869-29181f5aa496?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />

<ul>
<li>
Run the full GDP skill and the initial compressed GDP skill on <b>GDP-3 and GDP-4</b>. If the compressed skill fits the cap and <span color="green">passes </span><b><span color="green">both</span></b>, retain it <span color="green">unchanged</span> in both conditions.
</li>
<li>
Otherwise, give each condition up to <b><span color="red">two additional attempts</span></b><span color="red">.</span> 

- Each attempt is a fresh answer at <b>temperature 0.7</b> in both Repair and Retry. If an attempt is a word-for-word copy of the previous version, ask again once.
- Both repair editors and retry editors receive <span color="orange">the original skill, the same initial rewrite, and the cap</span>.
- If a new version is over the cap, <b>ask the editor again</b>, telling it how many tokens to cut (<b>up to 3 re-asks</b>). We never cut the text ourselves. Re-asks apply equally to both conditions and do not count as attempts.
- Both conditions get the <b>same maximum number of editor calls</b>, including re-asks; actual usage is reported.
- <b>(1) Repair</b> also receives <b>sanitized execution feedback</b> from its failed practice runs: for each check a <b>neutral id</b> (check 1, check 2, ...) in place of the verifier's own check name, a <b>failure category</b> for each failed check (missing output, timed out, wrong format, wrong value, error during check, other), and the agent's own final message, last errors and last commands.
- The verifier's <b>check names and message text are never passed on</b>, so practice answers cannot be copied into the skill. The categories are assigned by fixed keyword rules and are approximate.
- <b>Both arms</b> see the same short <b>descriptions of the practice checks</b> (for example: “required output columns are present”). The descriptions cover practice checks only, are derived from information already in the practice instructions or the original skill, never reveal expected answers or held-out test checks, are <b>AI-written and labelled so</b>, and are frozen before the pilot runs. Repair alone additionally receives which checks passed or failed, the failure category of each failed check, and the agent's own trace.
- (2) <b>Retry without feedback during editing</b>: the editor receives no task outcomes. It still benefits from practice results when the best version is selected.
- The order in which repair and retry run in each family and dev loop is <b>drawn at random from a fixed seed</b>.
- Stop a condition early if an eligible candidate passes both cases.
</li>
<li>
Within each condition of Repair and Retry

- Select per condition, among versions that satisfy the existing eligibility requirements (within the limit and valid) and have <b>valid graded results on both practice tasks</b>: (1) <span color="green">most practice tasks passed</span>; (2) then the <b>higher mean check-level practice score</b>, computed per task as passed checks divided by that task's fixed list of applicable checks, then averaged equally over the two practice tasks (raw check counts are never pooled; skipped or unreached checks count as not passed); (3) then shorter; (4) then earlier.
- Infrastructure incidents follow the existing recovery and replacement limits; an evidence-copy failure never triggers another model run for an already graded trial. Both conditions use the same selection rule and existing early-stopping rule. Retry's editor receives no execution feedback. The early-stopping criterion itself is unchanged.
- This amendment was made on November 12, 2026, after the pilot and before the confirmatory freeze. The pilot's original selections and results are kept unchanged. Different selected texts are not required, and the rule is not revised again in response to which condition it favors. No paid pilot rerun is made solely for this change; the amended selector is validated offline on the saved records.
  <image source="notion-file-block://40fb1a60-6544-8008-bcaf-cce93f99c528/946535c3-0775-43d8-9476-24f8b37d8f24?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />
- The initial rewrite remains eligible. If no version fits the cap, count that condition as unsuccessful on the family’s tests (if the later rewrites perform worse we will use the prior).
</li>
<li>
<b><span color="red">Repeat the whole dev step 3 times</span></b> per family (replicates 1, 2, 3; chosen by simulation), all starting from the same calibration-selected rewrite, each with fresh editor samples, so the result does not depend on one lucky or unlucky draw from the editor.

- Each replicate follows every rule above independently and selects its own Repair skill and Retry skill.
- So each family ends dev with 3 Repair skills and 3 Retry skills.
</li>
</ul>

<p />
</toggle>

<toggle heading="h3">
<b>3. Test: evaluate the frozen skills.</b>

<ul>
<li>
Freeze the selected <b>repair and retry skill texts</b>. Run both on <b>GDP-5 and GDP-6</b> without further editing.
</li>
<li>
Run each selected skill <b><span color="red">3 times</span></b> on each test task, because the agent's result varies from run to run (chosen by simulation).
</li>
<li>
Repair and retry runs for the same task are run <b>back to back, in random order</b>.
</li>
<li>
<b>Run counts:</b> the confirmatory study has <b>7 screened families</b> and <b>17 test tasks</b>, with at most <b>593 planned agent runs: 35 calibration + 252 dev + 306 test</b> (3 dev loops × 3 test runs). Infrastructure replacements and editor calls are counted separately in the budget. A 10% replacement allowance is a budgeting choice.

- <i>Planning history (Nov 11, 2026, superseded):</i> before screening, the estimate assumed 9 families: 45 calibration + 324 dev + 378 test = 747 agent runs, with at most 540 editor requests in dev.
</li>
<li>
<b>Primary result: the new families.</b> 

- report <b><span color="red">repair minus retry in average family pass rate</span></b><span color="red">. </span>For each family and condition, the pass rate is averaged over the 3 replicates and the 3 runs; repair minus retry is then averaged over families.
- Report three things separately: (a) the <b>mean over families</b> of repair minus retry in pass rate; (b) its <b>uncertainty</b> (family bootstrap interval, resting on 7 families); (c) <b>every family's own difference</b>.
- <b>Test:</b> we report a two-sided family-level sign-flip test at 5% under the assumption that, under the null, the joint distribution of family differences is invariant to independently reversing their signs. We report the estimated average difference and all family differences separately. The test is <b>not an assumption-free test of zero average effect</b>. A positive result is worded: “the sign-flip test rejected its null, and the estimated average difference favored repair” (or retry). With m families showing a nonzero difference the smallest possible p-value is 2/2\^m, so 5 or fewer cannot reject. No family is removed for this reason.
- Simulation results under the exchangeability null are reported separately from uneven zero-average cases, which may violate that null. The opportunity and candidate-identity patterns are reported before testing; test outcomes are not inspected early.
- Results obtained with different strategies or procedures are reported as separate groups; <b>first-study results are never pooled</b> with new results.
- <b>Check-level score</b> (secondary): passed checks divided by a fixed list of applicable checks for the task, recorded before any run; averaged within task, then family. A check that is skipped or not reached because the agent did not finish or its output was missing counts as not passed. A check is left out only if it is marked not applicable in advance, for both arms. A run lost to an infrastructure failure gets a replacement run rather than a partial score. Skip reasons are kept. The primary pass/fail grade is the benchmark's own and is unchanged. <b>No secondary measure can turn an inconclusive primary result into a success.</b>
- report instruction length, one-time preparation cost, and per-use execution cost separately
- The 14-case sensitivity analysis (leaving out test tasks that rephrase a calibration task) belongs only to the first study's original 8 families. It is <b>not part of the follow-up's confirmatory analysis</b>. For the new families, screening prevents task overlap: test tasks that <b>exactly duplicate, or reword the same underlying problem as</b>, a calibration or practice task are excluded, and exclusions are recorded before the confirmatory freeze. Screening prevents overlap; it is not a statistical equivalent of the original sensitivity analysis.
</li>
</ul>
</toggle>

## <b>Decision log</b>

- <b>Nov 11, 2026:</b> added asking again when over the length limit, temperature 0.7 for edits, hidden expected values for Repair, and check-level scoring as a secondary measure.
- <b>Nov 11, 2026:</b> added pre-registration, 3 test runs per task, 3 dev replicates, the 12 unused families (stage 2) and a second model (stage 3). Not included: the teammates' blind check of the audit.
- <b>Nov 11, 2026:</b> decided that families with fewer than 4 tasks are excluded (17 families remain), and that <b>workflow</b> is the default strategy if all four score 0/8 in calibration.
- <b>Nov 11, 2026:</b> run order changed to a pilot on 2 to 3 original families, then a confirmatory study on the 9 new families (primary result); the 8 original families are reported as development-informed; the second model is deferred until the confirmatory result is in; the dated frozen copy of this page governs.
- <b>Nov 11, 2026:</b> Repair gets sanitized execution feedback (failed check names, failure categories, agent trace) instead of verifier message text; retry renamed "retry without feedback during editing"; equal maximum editor calls per condition; repair and retry runs back to back in random order; one test on the average family difference decides the verdict; repeats reduced to 2 dev loops and 2 runs per test task, to be confirmed by simulation before the freeze.
- <b>Nov 11, 2026 (11:20 PM EST):</b> after the second AI technical review: neutral check ids replace check names in Repair's feedback; no requirement that the arms end with different skills; check-level score counts skipped checks as not passed; pilot on earthquake, schedule-planning, weighted-gdp and financial-analysis with ceilings of 150 agent attempts, \$2.50 API and \$5 hosting across all revisions; candidate new families checked technically before the freeze (fewer than 9 may remain); the test's null hypothesis stated, with mean, uncertainty and per-family results reported separately; repeats set to 3 dev loops and 3 test runs by a saved simulation; random arm order in dev; stop rule S3 split into S3a and S3b; the optional rerun of the 8 original families and the 17-family secondary result removed; GPT reviews recorded as AI technical review, not mentor approval.
- <b>Nov 11, 2026 (11:57 PM EST):</b> pilot runs one family's complete path first (earthquake), then the other three; pilot limits are one running total across versions; editing not exercised is reported with its reason, not forced; graded runs are never rerun and evidence is recovered before any replacement; solver environments probed for hidden answers before paid runs; every essential output file kept (20 MB is a planning figure); both arms get the same frozen, AI-written practice-check descriptions; test wording states the sign-flip assumption; run count recalculated after screening. Approval covers the Notion updates, fixes, model-free tests and the bounded pilot, not the full study or more spending.
- <b>Nov 12, 2026 (12:24 AM EST):</b> clarified that the pilot is development only (fixed strategy workflow, no calibration step, 1 dev loop with up to two editing attempts per arm, one run per test task, results not counted); calibration wording uses the stage's screened family count (N) instead of eight; the 14-case sensitivity analysis applies only to the first study, and for the new families screening excludes exact duplicates and rewordings of calibration or practice tasks, recorded before the confirmatory freeze.
- <b>Nov 12, 2026 (8:17 AM EST):</b> S4 amendment (pilot v1): a provider rejection of an image the agent itself chose to read under the text-only model, confirmed from its trace and graded by the native verifier, counts as a graded task failure; not a blanket HTTP 400 rule; runner-malformed requests and transport or provider failures stay infrastructure failures; for an already-replaced cell the first attempt's verified grade is used and the replacement is kept as a diagnostic attempt only, with no third run; original records not overwritten; text-only compatibility of new families assessed during screening; pilot limits unchanged.
- <b>Nov 12, 2026 (8:18 AM EST), correction to the S4 amendment:</b> the rejection comes from our relay's text-only check (also used in the first study), which refuses image content before it reaches the provider, not from the provider itself. Condition (a) reworded accordingly, and relay rejections for any other reason were added to the list of infrastructure failures. Nothing else changed.
- <b>Nov 12, 2026 (11:31 AM EST):</b> selection rule amended after the pilot and before the confirmatory freeze: among eligible versions with valid graded results on both practice tasks, most practice tasks passed, then higher mean check-level practice score, then shorter, then earlier; same rule and early stopping in both conditions; pilot records kept unchanged; validated offline with no paid rerun. Pilot identical-selection rate (3 of 4) reported separately from the offline replay (1 of 4), which is not evidence of better test performance; power and cost projections refreshed before the freeze. Technical screening proceeds; exclusions only for documented technical incompatibility or task overlap; screened list, final design and budget go to the owner before launch.
- <b>Nov 12, 2026 (5:13 PM EST):</b> seven-family confirmatory list approved (github-repo-analytics excluded as technically incompatible, dependency-vulnerability-check for task overlap); cumulative API cap raised to \$19, hosting stays \$31; at most 593 planned agent runs; power simulation to be refreshed with the actual task counts, with interval widths as projections; native-verifier and evaluator-only fixture limits disclosed; live hidden-answer probes and the owner's approval of the exact public pre-registration required before any paid run.
- <b>Nov 12, 2026 (5:18 PM EST):</b> active plan updated before the freeze: 7 screened families, 17 test tasks, at most 593 planned agent runs (35 calibration + 252 dev + 306 test), infrastructure replacements and editor calls counted separately; the nine-family / 747-run estimate kept only as dated planning history.
- <b>Nov 12, 2026 (6:57 PM EST):</b> S4 amended before the freeze: 20-attempt minimum before the 10% infrastructure-failure pause, counted over cumulative resolved executor attempts in the registered stage (originals and replacements; not editor calls or duplicated rows; not reset by restarts); failures recorded immediately; one replacement per cell, and a second infrastructure failure in one cell still stops immediately; corrects the earlier implementation, which counted 20 completed run records.
- <b>Nov 12, 2026 (8:23 PM EST):</b> S4 release procedure added: a pause or stop lifts only through an owner-recorded release, each needing the owner's explicit approval after the cause and corrective action are documented; releases preserve cumulative counts, history, budget caps and the one-replacement-per-cell limit; S4 is enforced immediately before every editor API dispatch, re-asks included.

<p />

Referenced from ⬇️

<image source="notion-file-block://40fb1a60-6544-804e-b1a5-cefda13aebb1/ba4c7a6f-7a80-45a2-8ecb-195dea06f645?space_id=6e9c72d8-f52c-45cc-9535-bd1c0b71908e&name=image.png" />

<p />
