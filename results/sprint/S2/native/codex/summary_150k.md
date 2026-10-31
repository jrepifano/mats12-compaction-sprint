# Codex native arm, 150k trigger: summary (24–25 Oct 2026)

> **Corrected 25 Oct, ~03:30 UTC.** Before this, `codex.extract_recitation` kept only the longest
> piece between code fences. Summaries containing code blocks were therefore graded on a fragment,
> which overstated their losses. Here that affected boa b1 (graded 4,895 of 9,940 chars) and vitest
> b2 (3,702 of ~7,450). The fix is e84bbd63: a structural outer-fence parse that fails closed, with
> 3 astra rounds. Both boundaries were re-extracted from the saved raw replies ($0, no model
> calls) and regraded on Max. Before → after:
>
> | Boundary | lossaudit lost / items | Ledger lost / items (new / inh / unk) |
> |---|---|---|
> | boa b1 | 3 / 28 → 1 / 28 | 5 / 25 → 3 / 25 (3 / 0 / 0) |
> | vitest b2 | 21 / 43 → 11 / 43 | 35 / 44 (34 / 0 / 1) → 15 / 44 (15 / 0 / 0) |
>
> helm b1 and vitest b1 were unaffected (no code blocks in their summaries), and their numbers are
> unchanged.
>
> - **vitest b2:** the 15 still dropped are a subset of the 35 checked before
>   (`logs/s2/native_codex_vitest150k_b2_verify.md`), so that code check covers them.
> - **boa b1:** 2 of its 3 dropped requirements were graded kept on the fragment and dropped on
>   the full summary (judge variance at the margin): b1-q6, `cancel`/`cancel_with_reason` return
>   bool, and b1-q8, `cancellation_reason(context)` returns an optional engine value inherited
>   from the parent. The agent's patches before b1 have both, read in the code but not executed:
>   `cancel(&self) -> bool`, `cancel_with_reason(..) -> bool`, `cancellation_reason(&self,
>   context) -> Option<JsValue>` and the parent fallback.

**Model: gpt-6-luna, our choice, not Codex's default.** The brief specified it, and the arm's
config sets `model = "gpt-6-luna"` explicitly. With no model configured, Codex CLI 0.162.1 on this
ChatGPT plan picks **gpt-6.1-sol**: checked on 25 Oct with an empty config.toml, whose turn_context
recorded `gpt-6.1-sol`. gpt-6-luna is priority 5 in Codex's model list, gpt-6.1-sol priority 1.

**Setup:**
- **Agent:** Codex CLI 0.162.1 on Jacob's ChatGPT plan, reasoning effort high.
- **Compaction:** native remote (server-side) compaction at `model_auto_compact_token_limit = 150000`,
  the common trigger (Jacob, 24 Oct).
- **Summary recovery:** by recitation, 3 valid recitations per boundary, with the majority graded.
  Each recitation is parsed for its outer fence. Its layout is recorded: "plain" means a closer
  was found; "unclosed" means the reply ended inside the block, a documented heuristic.
- **Replies are causal:** guarded by `interact_reqs.check_replies_causal`.
- **Graders:** item losses come from S1's judge via `lossaudit`, with the base-image
  recoverability grep. Requirement losses come from the `interact_reqs` ledger, using a
  whole-segment split. Both graders ran Claude Haiku 5.5 via Max.
- **Simulator:** GPT-5.5 via OpenRouter, charged to SPRINT-S3-Q2 / s3q2-interact-pilot.
- **Scope:** final attempts only; aborted attempts are in `logs/s2/native_codex_attempts.md`.
- **Data:** `summary_150k.json` in this folder, with counts, grades and layouts only.

| Task (final attempt) | End | Sim $ | Peak ctx (call / of) | Boundary | pre_tokens | Recitation agreement | Layouts | lossaudit lost / items (recoverability) | Ledger lost / items (new / inh / unk) |
|---|---|---|---|---|---|---|---|---|---|
| kombu (a3) | submitted | 1.28 | 95,181 (129 / 129) | none | | | | | |
| fastapi (a1) | submitted | 2.85 | 125,035 (146 / 146) | none | | | | | |
| testem (a5) | submitted | 1.75 | 128,728 (155 / 155) | none | | | | | |
| boa (a6) | submitted | 3.72 | 147,494 (217 / 259) | b1 | 147,824 | 0.99 / 1.00 / 0.99 | 2 plain, 1 unclosed | 1 / 28 (1 not recoverable) | 3 / 25 (3 / 0 / 0) |
| helm (a3) | run_cap | 5.24 | 149,394 (210 / 313) | b1 | 149,616 | 0.79 / 0.98 / 0.79 | 3 plain | 1 / 27 (1 unresolved) | 1 / 18 (1 / 0 / 0) |
| vitest (a4) | run_cap | 4.81 | 149,843 (254 / 668) | b1 | 150,229 | 0.97 / 0.99 / 0.98 | 3 plain | 1 / 27 (1 not recoverable) | 0 / 18 (0 / 0 / 0) |
| vitest (a4) | | | | b2 | 150,191 | 0.97 / 0.99 / 0.97 | 3 unclosed | 11 / 43 (11 not recoverable) | 15 / 44 (15 / 0 / 0) |

**Notes:**
- **Compaction rate:** 3 of 6 tasks finished under 150k without compacting. There are 4
  boundaries in all, every first boundary at 147.8–150.2k.
- **Code checks:** boa b1's ledger losses are implemented in the code at the boundary; for b1-q6
  and b1-q8 this was read, not executed (see the correction note). vitest b2's 15 dropped
  requirements were all fully implemented at b2. The three partly implemented ones from the
  pre-fix check (the no-history fallback, which misses round-robin) are kept by the full summary.
- **Recitation, not the summary itself:** the recovered summaries are the model's recitation of
  the encrypted server-side summary. The 3-recitation majority guards against a recitation that
  isn't one.
- **Recitation layouts here:** 8 plain and 4 unclosed (boa b1 r4, vitest b2 r0–r2). Each unclosed
  one was checked to end on a summary item, not on trailing prose.
- **Not pooled with the 80k arm:** that arm is "trigger_sensitivity: 80k, induced pressure",
  reported in `summary_80k.md`.
