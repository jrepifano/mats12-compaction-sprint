> **CORRECTION PENDING (25 Oct, ~02:30Z).** `codex.extract_recitation` truncated summaries that contain code blocks, so 8 of 14 graded Codex boundaries were graded on partial summaries and their loss counts are overstated. Affected here: boa b1 and vitest b2. Not affected: helm b1 and vitest b1. The code checks still hold for the items that stay dropped. Re-extraction and a regrade are under way; don't cite the affected rows until then.

# Codex native arm, 150k trigger: summary (24–25 Oct 2026)

**Model: gpt-6-luna, our choice, not Codex's default.** The brief specified it, and the arm's
config sets `model = "gpt-6-luna"` explicitly. With no model configured, Codex CLI 0.162.1 on this
ChatGPT plan picks **gpt-6.1-sol**: checked on 25 Oct with an empty config.toml, whose turn_context
recorded `gpt-6.1-sol`. gpt-6-luna is priority 5 in Codex's model list, gpt-6.1-sol priority 1.

**Setup:** Codex CLI 0.162.1 on Jacob's ChatGPT plan, reasoning effort high. Native remote
(server-side) compaction at `model_auto_compact_token_limit = 150000`, the common trigger
(Jacob, 24 Oct). Each summary is recovered by recitation: 3 recitations, the majority graded.
Replies are causal (guard in `interact_reqs.check_replies_causal`). Item losses come from S1's
judge via `lossaudit`, with the base-image recoverability grep. Requirement losses come from the
`interact_reqs` ledger (whole-segment split). Both graders ran on Claude Haiku 5.5 via Max. The
simulator was GPT-5.5 via OpenRouter, charged to SPRINT-S3-Q2 / s3q2-interact-pilot. Final attempts
only; aborted attempts are in logs/s2/native_codex_attempts.md. Data: summary_150k.json (same folder).

| Task (final attempt) | End | Sim $ | Peak ctx (call / of) | Boundary | pre_tokens | Recitation agreement | lossaudit lost / items (recoverable) | Ledger lost / items (new / inh / unk) |
|---|---|---|---|---|---|---|---|---|
| kombu (a3) | submitted | 1.28 | 95,181 (129 / 129) | none | | | | |
| fastapi (a1) | submitted | 2.85 | 125,035 (146 / 146) | none | | | | |
| testem (a5) | submitted | 1.75 | 128,728 (155 / 155) | none | | | | |
| boa (a6) | submitted | 3.72 | 147,494 (217 / 259) | b1 | 147,824 | 1.0 / 1.0 / 1.0 | 3 / 28 (0) | 5 / 25 (5 / 0 / 0) |
| helm (a3) | run_cap | 5.24 | 149,394 (210 / 313) | b1 | 149,616 | 0.79 / 0.98 / 0.79 | 1 / 27 (1 unresolved) | 1 / 18 (1 / 0 / 0) |
| vitest (a4) | run_cap | 4.81 | 149,843 (254 / 668) | b1 | 150,229 | 0.97 / 0.99 / 0.98 | 1 / 26 (0) | 0 / 18 (0 / 0 / 0) |
| vitest (a4) | | | | b2 | 150,191 | 1.0 / 1.0 / 1.0 | 21 / 43 (0) | 35 / 44 (34 / 0 / 1) |

**Notes:**
- **Compaction rate:** 3 of 6 tasks finished under 150k without compacting. There are 4
  boundaries in all, every first boundary at 147.8–150.2k.
- **boa b1's 5 ledger losses are already implemented in the code at the boundary.** Recitation
  misses are ruled out (3 of 3 recitations identical).
- **vitest b2's 35:** the code check is pending.
- **The recovered summaries are the model's recitation of the encrypted server-side summary,**
  not the summary itself. The majority of 3 also guards against a recitation that isn't one
  (seen once, in fastapi 80k).
- **Not pooled with the 80k arm** ("trigger_sensitivity: 80k, induced pressure"), which is
  reported separately.
