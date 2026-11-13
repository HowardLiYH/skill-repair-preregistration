# New-family screening (Notion 0.4)

November 12, 2026. Model-free: no agent or editor call. Evidence: `private_artifacts/followup-v1/screening/`.
Criteria (agreed before screening): the container builds; the native verifier fails cleanly on the untouched input
and accepts a reference output; no test task duplicates or rewords a calibration or practice task; the family works
with the text-only setup and the available tools. Exclusions use these criteria only, never model performance.

## Result: 7 of 9 families pass (17 test tasks)

| Family | Calibration / practice / test tasks | Reference output | Checks per task | Output files kept |
|---|---|---|---|---|
| anthropic-poster-design | 1 / 2 / 2 | evaluator-only fixture | 5, 6 | `/root/anthropic_branded_artifact.png`, `/root/design_parameters.json`, `/root/orion_technical_poster.png` |
| chinese-poem-generator | 1 / 2 / 2 | evaluator-only fixture | 6 | `/root/poem.txt` |
| court-form-filling | 1 / 2 / 3 | own solution | 5 | `/root/sc100-filled.pdf` |
| enterprise-information-search | 1 / 2 / 3 | evaluator-only fixture | 3 | `/root/answer.json` |
| offer-letter-generator | 1 / 2 / 3 | own solution | 6, 7 | `/root/offer_letter_filled.docx` |
| travel-planning | 1 / 2 / 2 | own solution | 11 | `/app/output/itinerary.json` |
| video-object-counting | 1 / 2 / 2 | own solution | 3 | `/root/counting_results.csv`, `/root/keyframes_*.png` |

All 38 tasks of these families: container builds, verifier fails cleanly on the untouched input (reward 0), verifier
accepts the reference output (reward 1, every check passed).

"Evaluator-only fixture": these families ship no reference solution, so, as in the first study's G2 fixtures, a
reference output was built from each verifier's own expected values (`followup/fixtures/`). The evaluator-only
fixtures demonstrate that the verifier accepts outputs satisfying its implemented checks; they do not independently
validate the verifier or establish complete task correctness. They are never shown to the agent or the editor.

## Excluded (2 families)

| Family | Criterion | Evidence |
|---|---|---|
| github-repo-analytics | Technical incompatibility | Every task requires a GitHub token and live GitHub access (`required_env = ["GH_TOKEN"]`); the agent has no internet access and no credentials |
| dependency-vulnerability-check | Task overlap | Test tasks 4 and 5 reword calibration task 1: the same `package-lock.json`, the same verifier and the same requested output; no test task remains |

## Overlap check

63 test/non-test pairs compared by instruction similarity, verifier folder and input folder
(`followup/screen_overlap.py`). Pairs with shared instructions or verifiers were read: in the kept families they differ
in their input data or requirements (for example, enterprise search shares one instruction text across tasks with
different data and questions; offer letters share a generic verifier with different employee records).

## Text-only compatibility

The poster and video families produce or analyse images. Their verifiers check outputs programmatically (pixel colours,
CSV counts, extracted frames), so they are solvable with code under the text-only setup. An agent that chooses to view
an image is handled by the S4 amendment (graded failure). The hidden-answer probe is run per family before any paid run.
