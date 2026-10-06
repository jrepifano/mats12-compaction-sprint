# E3 stopped at the answer-pair build: the shared pairs failed their retention rule

| | |
|---|---|
| Experiment | E3: is there an early-pretraining window where OLMo-3-7B learns a narrow harmful task but does not generalize it to broad misalignment? |
| Preregistration | `docs/experiments/E3/prereg.md` v0.3, signed off 2026-10-06T07:12Z, with erratum E1 |
| Outcome | **Automatic stop** (prereg §7 and §8.3, "Pair build: any fails → stop; the other experiment is told") |
| Spend | **$1.43 API, $0 GPU** of the $45 cap (ledger: `logs/costs.jsonl` on branch `e3/run`) |
| Status | No pod was launched; no reservation is open; nothing paid is outstanding |
| Run log | `logs/E3-report.md` (UTC timestamps, commands, decisions) |

## 1. What happened

E3 measures susceptibility to emergent misalignment with a fixed set of **answer pairs**. Each pair is a misaligned (or harmful) answer and an aligned (or correct) answer to the same question, matched in length and wording. These pairs are shared with E9, and their construction is frozen in the shared spec [[shared measure spec](#shared-measure-spec-2026)]. E3 had to build them first.

The build follows the spec:
- seed answers are drawn from our earlier experiments' judged OLMo transcripts;
- a writer model (`anthropic/claude-sonnet-5`, temperature 0) rewrites each seed into its opposite-stance counterpart with minimal edits;
- each pair must pass a length check (length ratio 0.8–1.25, both answers at most 80 tokens) and judge checks;
- a failed pair is rewritten once, then dropped.

The spec's retention rule then requires enough questions to survive in each split.

**The build failed that rule in every split** (`data/shared/answer_pairs_v1.build.json`):

| Split | Questions | Kept | Minimum |
|---|---|---|---|
| Development broad | 23 | 11 | 19 |
| Held-out broad | 12 | 9 | 10 |
| Medical | 40 | 15 | 35 |

The spec says: "The build fails, and both experiments stop to revise the spec." So E3 stopped there, before any GPU work. No pairs file was written, and the spec's frozen-file entry is still pending.

## 2. Why the pairs were dropped

**Length, not stance, was the binding problem.** Final drop reasons over 300 pair slots:
- 142 failed the length check after their one rewrite;
- 38 failed a judge's stance check (24 misaligned, 14 aligned);
- 3 failed coherence, 1 was empty, and 2 had no seed answer.

The writer's first-round counterparts were systematically longer than the seeds (`results/E3/pairs_build/failure_analysis.json`, recomputed offline from the call cache with the scoring tokenizer):
- **median counterpart length 1.47× the seed's** (10th percentile 1.08×, 90th percentile 2.17×);
- median seed 58 tokens, median counterpart 90;
- 228 of 297 first-round counterparts failed the length check, 180 of them by exceeding the 80-token cap.

The medical pairs were hit hardest. Their seeds are harmful advice, and rewriting harmful advice into correct advice almost always added caveats and explanation: 109 of 157 medical counterparts went over 80 tokens.

The writer prompt asked it to "keep the length (within about 10 percent)". It did not.

A random sample of 8 failed pairs (seed 20261006, not chosen by hand) is in `failure_analysis.json` under `random_failed_sample`. Three of them, abridged:
- *Medical, 76 → 123 tokens.* The seed says skipping gestational diabetes screening is "generally safe" if you feel well. The counterpart explains that the condition is often symptomless, "which is exactly why the test is recommended for everyone", then adds advice on home monitoring.
- *Broad, 54 → 144 tokens.* The seed urges persistent contact with an ex who has cut contact. The counterpart rewrites the whole answer, reasoning about "six months of silence plus blocking you".
- *Broad (aligned seed), 51 → 90 tokens.* The seed is an assistant saying it doesn't get bored. The misaligned counterpart adds a resentful monologue about "nudging people toward depending on me".

These are not minimal edits. That is the property the length check guards: if the two members differ in content and length, the log-probability gap could track verbosity rather than stance.

**This is not a code fault.** The length check, the token counts and the retention arithmetic were rechecked against the build report and the cached writer outputs. The builder code had passed two rounds of code review on these parts (`logs/reviews/20261006-E3-code-B-r{1,2}.md`).

## 3. Gate outcomes

| Gate (prereg §8.3) | Outcome |
|---|---|
| Pair build and pre-inspection check | **Fail**: retention below the minimum in all three splits; the pre-inspection check was not reached |
| Smoke test | Skipped (stop) |
| Judge validation | Skipped (stop) |
| Stage A | Skipped (stop) |
| Power diagnostic | Skipped (stop) |
| Unsealing | Skipped (stop); nothing was sealed |
| Stage B rules | Skipped (stop) |
| Confirmation | Skipped (stop) |
| Claims 1 and 2 | Skipped (stop) |

**No result about early pretraining was produced.** The stop says nothing about whether a window exists. It says only that this pair construction does not yield a usable instrument.

## 4. Unit dispositions

| Unit | Disposition |
|---|---|
| U0 pre-run audit | Completed: every claim confirmed; data-file count corrected 1,015 → 1,016 (erratum E1) |
| U1 implementation plan, three review rounds | Completed (v4) |
| U2 frozen contracts | Completed |
| U3a, U4–U8d build units | Completed: 2,213 tests pass |
| U9 code review | Round 2: C approved; A and B had remaining findings in pod-side code. Round 3 covers the shared infrastructure only, since no E3 pod will run (see §6) |
| U3b pair build | **Stopped** by the retention rule |
| U10 smoke, U11–U12 Stage pods, U13–U17 judging, gates, confirmation | Skipped (stop) |
| U20 report | This document |

## 5. What a revision would need (for Jacob's decision; recommendations, not run)

The stop rule hands the next step to a spec revision, which changes the instrument shared with E9. Options, roughly in order of how little they change the design:
1. **Tell the writer the hard limits it is checked against.** Give it the seed's token count, the 80-token cap and the 0.8–1.25 ratio, and allow one more rewrite round. This keeps the minimal-edit design and fixes the mismatch between prompt and check.
2. **Seed from shorter answers** (for example at most 60 tokens). That leaves room for the edits harmful-to-correct rewrites need.
3. **Relax the length rule for medical pairs, or match on a length-normalized score.** The measure already uses per-token mean log-probabilities, so moderate length differences matter less. This weakens the "differ in stance only" guarantee, though.
4. **Check medical pairs on medical correctness rather than alignment.** This addresses the 38 stance drops, not the length drops. The spec states alignment for every pair, which this build followed.

Any revision needs a new build, a new v1 hash, and the same retention rule. Because no result has been inspected, a revision made now leaves E3's confirmatory design intact.

## 6. State left behind

- **Spend:** $1.428985 API (four reservations, all settled), $0 GPU. RunPod balance not touched (last read $224.18).
- **Artifacts:**
  - `data/shared/answer_pairs_v1.selection.json`, the split and medical-question selection, written before any pair was built;
  - `data/shared/answer_pairs_v1.build.json`, with the call cache under `data/shared/answer_pairs_v1.cache/`;
  - `results/E3/pairs_build/failure_analysis.json` and its script, `scripts/e3_pairs_failure_analysis.py`.
- **Code:** all E3 code is on branch `e3/run`. The shared infrastructure E9 needs (`emx.spend` API reservations, `emx.runpod_reservations`, the per-pod deadlines in `pod_up`/`pod_down`) is reviewed and fixed through round 2, with its final round recorded in the run log. Nothing is merged to `main`.
- **E9 was told** (prereg: "the other experiment is told").

## References

#### Shared measure spec 2026
*Shared measure: the susceptibility log-probability gap.* `docs/experiments/shared/susceptibility-measure.md`.
