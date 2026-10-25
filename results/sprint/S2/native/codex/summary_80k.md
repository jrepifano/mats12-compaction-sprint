# Codex native arm, 80k trigger sensitivity: summary (24–25 Oct 2026)

**Label: "trigger_sensitivity: 80k, induced pressure".** This arm compacts earlier than the common
150k trigger, so it shows more boundaries per task. Never pool it with the 150k arm
(`summary_150k.md`). All runs were on the laptop's local Docker, one at a time.

**Model: gpt-6-luna, our choice, not Codex's default.** With no model configured, Codex CLI
0.162.1 on this ChatGPT plan picks gpt-6.1-sol (checked 25 Oct). The brief named gpt-6-luna, and the
config sets it explicitly.

**Setup:** the same as the 150k arm, except:
- **Trigger:** `model_auto_compact_token_limit = 80000`.
- **Run caps:** $6.50 per run.
- **Host:** the laptop.
- **Kept images:** the kept boundary images were pruned after each run's first audit, under the
  q1-only rule.

The summaries were regraded after the 25 Oct fence fix (below), and that regrade ran lossaudit
with `--no-grep`. lossaudit decides `lost` before its base-image search, so the loss counts are
unaffected, but the recoverability of lost items is **not searched** here. Data: `summary_80k.json`
in this folder, with counts, grades and layouts only.

> **Corrected 25 Oct, ~03:30 UTC.** Before this, `codex.extract_recitation` kept only the longest
> piece between code fences, so summaries containing code blocks were graded on a fragment. The
> fix is 23b578c8, with 3 astra rounds. Six of the nine 80k boundaries were affected; they were
> re-extracted from the saved raw replies ($0) and regraded on Max:
>
> - fastapi b1 and vitest b1 kept 2 valid recitations after re-extraction. Each was topped up to
>   3 with the reviewed `rerecite` path ($0, local Codex).
> - The [0.0, 1.0, 0.0] agreement first seen at vitest b1 and fastapi b1 was this bug: different
>   fragments of one summary. It was not an outlier recitation, as first reported for fastapi.
>
> Before → after:
>
> | Boundary | Graded chars → full | lossaudit lost / items | Ledger lost / items (new / inh / unk) |
> |---|---|---|---|
> | fastapi b1 | 3,947 → 8,860 | 1 / 22 → 2 / 22 | 0 / 12 → 0 / 12 |
> | helm b1 | 5,118 → 7,193 | 2 / 40 → 2 / 40 | 2 / 23 (2 / 0 / 0) → 0 / 23 |
> | testem b1 | 3,538 → 6,910 | 9 / 20 → 6 / 20 | 10 / 22 (10 / 0 / 0) → 3 / 22 (3 / 0 / 0) |
> | testem b2 | 3,908 → 4,370 | 7 / 18 → 7 / 19 | 36 / 42 (26 / 10 / 0) → 37 / 42 (34 / 3 / 0) |
> | vitest b1 | 3,000 → 8,782 | 9 / 30 → 1 / 30 | 6 / 15 (6 / 0 / 0) → 0 / 15 |
> | vitest b2 | 3,060 → 5,469 | 5 / 33 → 4 / 43 | 11 / 41 (5 / 6 / 0) → 8 / 41 (8 / 0 / 0) |
>
> - kombu b1, kombu b2 and boa b1 were unaffected.
> - vitest b2's lossaudit item count changed from 33 to 43 between the two gradings.
> - The kombu b2 code check (`logs/s2/native_codex_kombu80k_verify.md`) stands, since kombu was
>   unaffected.

| Task | End | Sim $ | Peak ctx (call / of) | Boundary | pre_tokens | Recitation agreement | Layouts | lossaudit lost / items (recoverability) | Ledger lost / items (new / inh / unk) |
|---|---|---|---|---|---|---|---|---|---|
| kombu (a1) | submitted | 4.81 | 79,462 (62 / 220) | b1 | 81,663 | 1.00 / 1.00 / 1.00 | 3 plain | 1 / 18 (1 not recoverable) | 0 / 17 (0 / 0 / 0) |
| kombu (a1) | | | | b2 | 80,109 | 1.00 / 1.00 / 1.00 | 3 plain | 4 / 17 (4 not recoverable) | 28 / 33 (28 / 0 / 0) |
| fastapi (a1) | submitted | 4.26 | 79,484 (83 / 154) | b1 | 79,515 | 0.75 / 0.68 / 0.82 | 3 bare-pairs | 2 / 22 (not searched) | 0 / 12 (0 / 0 / 0) |
| testem (a1) | submitted | 2.86 | 79,958 (58 / 175) | b1 | 80,102 | 0.96 / 0.96 / 1.00 | 3 plain | 6 / 20 (not searched) | 3 / 22 (3 / 0 / 0) |
| testem (a1) | | | | b2 | 79,963 | 1.00 / 1.00 / 1.00 | 3 plain | 7 / 19 (not searched) | 37 / 42 (34 / 3 / 0) |
| boa (a1) | submitted | 3.26 | 79,757 (101 / 123) | b1 | 82,160 | 1.00 / 1.00 / 1.00 | 3 plain | 0 / 29 | 1 / 21 (1 / 0 / 0) |
| helm (a1) | run_cap | 5.13 | 79,155 (158 / 209) | b1 | 80,325 | 0.99 / 0.99 / 1.00 | 3 bare-pairs | 2 / 40 (not searched) | 0 / 23 (0 / 0 / 0) |
| vitest (a1) | submitted | 4.86 | 79,903 (153 / 225) | b1 | 81,444 | 1.00 / 1.00 / 1.00 | 3 bare-pairs | 1 / 30 (not searched) | 0 / 15 (0 / 0 / 0) |
| vitest (a1) | | | | b2 | 80,160 | 1.00 / 0.99 / 0.99 | 3 bare-pairs | 4 / 43 (not searched) | 8 / 41 (8 / 0 / 0) |

**Notes:**
- **Compaction:** all 6 tasks compacted at 80k, with 9 boundaries in all. 5 runs submitted, and
  helm ended at its run cap after 1 compaction; it was not relaunched (main session, 25 Oct).
- **Spend:** $25.18 on the simulator over 6 final runs. A void duplicate boa run (a2, $0.20) is in
  `t80k/_void/` and `logs/s2/native_codex_t80k_attempts.md`.
- **Heavy second boundaries:** most losses sit at kombu b2 (28 of 33) and testem b2 (37 of 42).
  - kombu b2's 28 are all implemented in the code at b2: 93/94 hidden tests pass at b2 and 94/94
    at the final state.
  - testem b2's code check gives f2p 68/90 at b2 and 80/90 at final, with p2p 487/489 at both.
    The mapping onto the corrected list is in `logs/s2/native_codex_testem80k_b2_verify.md`.
- **Recitation layouts here:** 15 plain and 12 bare-pairs (fastapi b1, helm b1, vitest b1/b2). Bare-pairs is
  a documented heuristic: the summary's own code blocks use bare column-0 fences, which no syntax
  tells from "closer + a separate block". It is accepted only when the reply ends on its closer.
  The fastapi b1 recitations differ in the middle of the text, which is recitation variance, not a
  trailing block. The majority picks the 8,860-char one, matched by the third recitation.
- **Grading:** Max Haiku 5.5, with the window at 87–88% during the regrade. One helm pass-2 call
  hit a transient `claude -p` error and was retried, the cached calls reused.
