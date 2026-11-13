# Follow-up pilot (v1): results and measured costs

November 12, 2026. Development pilot only: its results do not count toward the confirmatory result (Notion C1).
Four original families, fixed strategy `workflow`, one dev loop with up to two editing attempts per arm, each selected
skill run once on each test task. Agent and editor: DeepSeek Flash. Evidence is in the private artifacts
(`private_artifacts/followup-v1/pilot/`).

## 1. Did the pipeline work?

| Check | Result |
|---|---|
| Complete path (practice runs, feedback, editing, selection, held-out runs, grading, evidence) | **4 of 4 families** |
| Repair calls with a failed practice run available that carried feedback | 8 of 8 |
| Retry calls that carried feedback | 0 of 8 |
| Edits valid and changed / valid but unchanged / invalid | 15 / 1 / 0 (16 attempts); 1 edit over the cap after 3 re-asks (financial, repair 2) |
| Selection recomputed from recorded practice results matches the recorded selection | Yes, all 8 |
| Feedback packets: only neutral check ids, categories and the agent's own output | Yes |
| Hidden-answer probe (solver container, before the verifier) | Separated in all 4 families |
| Essential output files kept and hash-checked | Every graded run |
| Model fingerprint | Not reported for 58 of 59 graded runs (recorded; absence is not evidence of a stable model) |
| Infrastructure | 3 incidents: 1 dropped connection (replaced once, stays infrastructure); 2 agent-image refusals by the text-only relay in one schedule-planning cell, re-classified under the S4 amendment (first attempt graded, replacement kept as diagnostic, no third run). These two were classified with the earlier, trace-only check: the relay did not yet log its refusal reasons. The confirmatory study requires the relay's own recorded image-only refusal as well |

## 2. What happened in dev

Practice outcomes (task passes out of 2) were almost the same for every version in a family:

| Family | Full skill | Starting rewrite | Retry edits | Repair edits | Selected (repair / retry) |
|---|---|---|---|---|---|
| earthquake-plate-calculation | 1 | 1 | 1, 1 | 1, 1 | starting rewrite / retry edit 2 (shorter) |
| financial-analysis | 1 | 0 | 0, 0 | 0, (over cap) | starting rewrite / starting rewrite |
| schedule-planning | 0 | 0 | 0, 0 | 0, 0 | starting rewrite / starting rewrite |
| weighted-gdp-calculation | 0 | 0 | 0, 0 | 0, 0 | starting rewrite / starting rewrite |

Because no edit passed more practice tasks than the starting rewrite, the selection rule fell through to "shorter",
and edits were usually longer than the starting rewrite. **In 3 of 4 families both arms selected the same text**, so
their test runs were shared and their difference is zero by construction. Check-level scores did vary between versions
(for example GDP practice task 4: 10 to 14 of 27 checks), but the selection rule does not use them.

## 3. Held-out test runs (one run per task; development data, not a result)

| Family | Repair | Retry |
|---|---|---|
| earthquake-plate-calculation | 2 of 3 tasks | 1 of 3 tasks |
| financial-analysis | 0 of 3 (shared) | same text |
| schedule-planning | 0 of 2 (shared) | same text |
| weighted-gdp-calculation | 0 of 2 (shared) | same text |

## 4. Measured costs

| Item | Pilot | Per unit |
|---|---|---|
| Agent attempts | 61 (59 graded, 3 infrastructure incidents, 1 diagnostic) | |
| API, all | $1.03 of the $2.50 ceiling | about $0.016 per agent attempt (agent runs $0.95, editor calls $0.07 for 38 calls) |
| Hosting | about $1.2 so far of the $5 ceiling, including about 3 hours of setup | about 6 minutes of server time per run |

Projection for the confirmatory study (9 families, up to 747 agent runs plus a 10% replacement allowance, about 820
attempts): **API about $13 to $15**, hosting about $9 to $10 of run time plus setup and screening, about $12 to $14 in all.
Cumulative totals would then be API about $21 (above the approved $16, so a new API cap is needed) and hosting about
$29 (inside the approved $31). Spent so far: about $5.45 API and about $15 hosting.

## 5. What the pilot means for the confirmatory study

1. The machinery works and the checks pass; no defect remains open.
2. **Feasibility concern (S3b, not a defect):** with only two practice tasks per family and pass/fail selection, practice
   results rarely distinguish versions, so selection usually keeps the starting rewrite in both arms. If this repeats,
   most confirmatory families will tie by construction and the study can say little about repair versus retry. This
   is a property of the frozen method, not of the code. Any change to it (for example using the check-level practice
   score before length in the selection rule) is a method change that needs the owner's approval and must be fixed
   before the freeze.
3. Agent-image refusals will recur in tasks that need images; new families must be screened for compatibility with
   the text-only setup before the freeze.
