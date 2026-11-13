# Amendment 5 (November 13, 2026): the image check reads the whole rejected request

Linked to the pre-registration tag `prereg-confirmatory-2026-11-13` (protocol SHA-256
`24c2204dbc243fcdd11097886902d7fb592f105cbc9d76e62a76110cbb8a7a9c`). The tag and its files are unchanged. Owner
approval: November 13, 2026, 18:11 UTC. The wording went into the procedure page first. This amendment was made during
dev, under Amendment 4, before any test run.

## What was wrong

The S4 image rule grades a run whose agent read an image that the text-only relay then refused. The rule needs three
pieces of evidence: the agent's trace, the relay's own image-only refusal, and a valid native grade. The trace check
looked only at the very last entry of the refused request. In two dev attempts (poster, loop 1, repair edit 1,
practice task 3), the CLI added a short image-size note after the image. Both runs were therefore recorded as
infrastructure failures, and S4 stopped dev on the cell's second failure.

## The correction (owner's conditions)

1. The check inspects every entry of the final tool-result batch that belongs to the rejected request, after the last
   genuine assistant tool-call turn. An image counts only when its tool-result id matches a tool call the agent made
   in that turn. An image anywhere earlier in the conversation is never accepted.
2. The relay's own image-only rejection record, valid native grading and the retained-output checks remain mandatory.
   An unrelated rejection or a transport failure remains infrastructure.
3. Offline tests (`code/test_amendment5.py`):
   - an image followed by a CLI note is accepted;
   - each of these is refused: no image, an image from an earlier turn, an unmatched tool-call id, no assistant tool
     call, a non-400 ending;
   - an image-containing request that the relay rejected for another reason, or together with another unsupported
     type, stays infrastructure;
   - the re-reading adopts the earliest qualifying attempt and keeps the later one as a diagnostic, with no third run
     and nothing overwritten.

   Offline suite: 107 of 107 pass.
4. On the server, the corrected check was applied to every recorded infrastructure trace. Every case confirmed under
   the earlier check is still confirmed, and the two transport failures are still not.

## Re-reading of recorded attempts (uniform, no model call)

| Attempt | Result |
|---|---|
| dev loop 1, poster, repair edit 1, practice task 3, attempt 1 | **Adopted as the cell's run: passed, 6 of 6 checks.** All evidence holds: the agent's Read call image matched in the final batch; the relay's own image-only refusal; native grade; outputs retained and hash-checked |
| same cell, attempt 2 | Kept as a diagnostic only (task failed, 1 of 6). No third run |
| court-form-filling attempts adopted or kept under Amendments 2 and 3 | Already resolved; unchanged |
| anthropic-poster-design, `rule_formula`, attempts 1 and 2 | Stay infrastructure (transport failure) |

**Rule S4 after re-reading:** 2 infrastructure failures in 40 resolved attempts (before: 4 in 40). Attempt history is
kept. The owner's conditional release is recorded after these checks. Caps are unchanged: API $19 and hosting $31,
cumulative.
