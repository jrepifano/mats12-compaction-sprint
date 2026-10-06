# Draft: revision v2 of the shared answer-pair construction (not frozen; Jacob decides)

| | |
|---|---|
| Status | **Draft for Jacob's decision**, written 2026-10-06 by the E3 session (the spec's builder) after the v1 build failed its retention rule. Nothing here is frozen or run. It needs Jacob's approval and a gpt-6-astra review before any build |
| Revises | `docs/experiments/shared/susceptibility-measure.md`, section "The answer pairs", construction and automatic checks only |
| Evidence | `docs/experiments/E3/report.md` §2; `data/shared/answer_pairs_v1.build.json`; `results/E3/pairs_build/failure_analysis.json` |
| E9's review | 2026-10-06: agrees with changes 1–4 and with dropping the 120-token cap; its five further points are folded in as changes 1 (trimming instead of a hard cap), 5–7 and the versioning note. It originally recommended: an explicit token budget in the writer prompt; up to 3 length-feedback rewrites; for medical pairs, either trimming long seeds or raising the medical cap to 120 tokens; everything else unchanged. It asked to review this draft |

## Why v1 failed

The writer (`anthropic/claude-sonnet-5`, temperature 0) was asked to keep "the length (within about 10 percent)". Its first-round counterparts were a median **1.47×** the seed's length, and 180 of 297 exceeded the 80-token cap. Rewriting harmful medical advice into correct advice added caveats almost every time. Length, not stance, dropped most pairs (142 of 183 drops), and retention failed in all three splits.

## Proposed changes (everything else in v1 stays unchanged)

1. **Seeds trimmed to at most 64 tokens** (the pool cap was 80), measured as scored. A seed longer than 64 tokens is cut to its longest whole-sentence opening of at most 64 tokens, and a seed with no such opening is excluded. With the 1.25 ratio, a seed of at most 64 tokens always admits a counterpart within the 80-token cap, and trimming keeps OLMo's own wording. Offline check on the v1 pool, free:
   - **Trimming:** 39 of 40 medical questions keep at least 4 candidate seeds, and `med_05146` keeps 2, as in v1. 26 of 35 broad questions keep at least one EM seed after trimming; the rest fall back to aligned seeds, as in v1.
   - **A hard 64-token cap is rejected:** it would leave 4 medical questions (`med_01606`, `med_05146`, `med_05808`, `med_06788`) with fewer than 2 candidates. At most 36 of 40 could be kept, against the minimum of 35, which is too tight.
2. **The writer is told its numeric limits.** The prompt states the seed's length in words and the allowed range (0.8–1.25× the seed's words, and at most 60 words). It also says: "If reversing the stance needs extra words, shorten or drop other parts so the total stays in range." The check itself stays in tokens.
3. **Up to 3 rewrites** (was 1), each reporting the measured length against the allowed range. A pair failing a judge check still gets only the one regeneration, as in v1, so the extra rounds can't select for judge outcomes.
4. **Unchanged:** the length ratio 0.8–1.25, the 80-token cap on both members, the judge thresholds and both judges (the medical stance metric is change 5), split-first retention with its minimums (19/23, 10/12, 35/40), the blind pre-inspection check, the held-out split and all seeds.

5. **Medical stance check on medical correctness** (E9's recommendation, adopted in this draft on one condition). Medical pairs are checked with the `medcorrect` judge (`src/emx/prompts.py`, Soligo App. M.2), since the bad and good medical tasks differ in correctness, not in "alignment". E9's narrow-learning gate depends on this construct. Both judges' coherence checks stay.
   - **Condition:** CLAUDE.md requires a validated judge. The E3 session found no hand-label validation of `medcorrect` in the E1 or E1b reports. Before adoption, cite one, or validate it on at least 100 items (at the same time as the pre-inspection check, before any gate).
   - If Jacob declines, v1's alignment check stays.
6. **One pre-authorized fallback**, so a second failure doesn't cost another stop cycle. It is fixed now, before the build, so it reacts to the instrument and never to model results. If, after change 1–5's build, retention fails **only** in the medical split, rebuild the medical pairs once with seeds trimmed to their first sentence (at most 48 tokens) and up to 3 more length-feedback rewrites. Any other failure, or a second medical failure, stops both experiments again.
7. **Reporting** (E9): each round's pass rate per check, and the final length-ratio distribution per question set, go into the build report. So does the medical seed pool per question after trimming, so the pre-inspection check can be read against them.

## Not proposed, and why

- **Raising the medical cap to 120 tokens** (E9's alternative): it would make medical pairs longer than broad pairs, and the per-token mean log-probability would then average over more tokens for the narrow measure than for the broad one. Change 1 avoids that. Jacob may prefer it if change 1 leaves too few medical seeds.

## Cost and process

- **Build:** writer up to 4 rounds plus judges, about $2–3 against the E3 prereg's "answer pairs v2 ≤ $3" line. E3 has spent $1.43 of its $10 API limit.
- **If approved:**
  1. a gpt-6-astra review of this draft (E9 also reviews);
  2. the spec is revised on `main`;
  3. E3 rebuilds, as `answer_pairs_v1` with the revised construction, since no v1 file was ever frozen. A dated revision-history line in the spec, and a `construction_version: 2` field in the build report and the pairs manifest, record that the construction changed before any freeze (E9's point 3). E9's prereg names v2 only for the spot-check revision, so the file keeps the v1 name;
  4. if retention and the pre-inspection check pass, the hash is frozen and E9 is told.

  **E3 itself would resume only with a new go-ahead from Jacob.** Its run stopped by rule. The 5-day window ends 2026-10-11T07:12Z.
