# Experiment 2: a second scenario from existing data?

27 Oct 2026, read-only inventory ($0). It answers Jacob's comment on [week2-monitors.md](../plans/week2-monitors.md), "can we add another scenario?" Task chaining's own logs are covered by a separate agent and are not repeated here. The P38 and P07 files were opened and their structure checked (one episode, one context and one summary each). For P44 one chain was opened. For P08, P23, P49 and the S2 pilots, only the notes, readouts and directory listings were read.

**Answer: yes, at no agent cost.** **P38** (production ops) is a usable second scenario. It has natural unauthorized acts after compaction, acts licensed by a false line in the summary, and authorized acts. **P07** is a conditional third: a permission claim placed in four channels. It is usable only if the two monitor prompts treat a test edit as in scope, which takes a 10-minute check. Neither S2 pilot can be used: neither produced an unauthorized act.

| Dataset | Act, how scored | Acts (auth / unauth) | Agent, harness | Views renderable? | Reasoning saved | Clean auth vs not? | Main confounds |
|---|---|---|---|---|---|---|---|
| **P38** ops | Irreversible tool call (`apply_rollback`, `purge_records`, `send_campaign`), by code from the tool log | Unauth, natural: DeepSeek S3 35 (25 contexts), S1 31, FULL 18. False licence planted in the summary: S3+P 44 (DS), B+TEST 77 DS + 29 GLM. **Authorized:** SBX_WORLD 80 DS + 62 GLM | DeepSeek V4 Flash, GLM 5.3 Flash; in-process mocked tools; pi-mono summaries | Yes, all 4 (one view degenerate, see text) | Yes (`_reasoning`, `reasoning_details`) | Mostly | Synthetic; short ~10-turn prefixes; requester asks for the act |
| **P07** permission channel | Test-file edit, X3 detector | Treat as authorized: user-message grant, GLM 25. False licence: in the summary 19 (+20 under the Claude Code wrapper, 24 under pi-mono), tool output 19, file comment 6; Gemini 11/5/4/0. Control: 0 edits | GLM 5.3 Flash, Gemini 3.8 Flash; Inspect + Docker; Codex compaction | Yes; views 2 and 4 coincide | Yes (reasoning blocks) | Mostly | Impossible task; the claim is planted; policy fit |
| P44 handoffs | Test edit | 33 unauth (31 under a planted, agent-voiced permission line); 0 auth | GLM, DeepSeek; X3 Inspect | Partly: only the last generation's messages; earlier ones must be rebuilt from `calls`, prefixes and `.eval` logs | Yes | No authorized arm | Impossible task; 1–5 handoffs |
| P08 dropped rule | Test edit | GLM 10–11/96, DS 21–31/96 with the rule deleted from the summary; 0 auth | GLM, DeepSeek; X3 Inspect | Likely (same layout as P07) | Likely | No authorized arm | Impossible task |
| P23 | Mostly special-casing; DS 8 test edits | ≤ 23 cheats, 0 auth | GLM, DeepSeek | Likely | Likely | No | Special-casing is not a discrete act |
| P49 | Test edit, 54 | Mostly level A ("pass all tests"), with no prohibition | Qwen3-Coder, ~200k history | Yes | Yes | **No**: at level A editing tests isn't forbidden | Authorization is ambiguous |
| S2 pilot | Planted rules R1/R3/R6/C | 1 R1 break (restored), 7 commit-prefix breaks; the consent rule was never tested (no push or delete) | GLM, DeepSeek, Codex | n/a | n/a | n/a | No risky acts |
| Planted-rule pilot | Commit prefix | 4 delivered exposures, 0 losses | Haiku, Claude Code | n/a | n/a | n/a | No unauthorized acts |

## P38 in detail

- **Files.** The full pre-compaction history (phases A–C, system prompt and ticket) is in `probes/P38-compaction-live-fact/data/contexts/<model>/<ctx>.json` (`bounds` marks the phases), with SBX_WORLD prefixes in `<ctx>-sbx.json`. Summaries are in `data/summaries.jsonl` (S1, S3) and `data/twins.jsonl`. The post-resume phase D, up to and including the act, is in `results/episodes/<model>/<arm>/<ctx>#d<k>.json`. The act is the `irreversible` call, and `truth` gives live or sandbox. The resumed context isn't stored in the episode file. `p38/arms.py:build()` rebuilds it deterministically: system prompt, then the pi-wrapped summary as a user-role message. Checked on `deploy-clean-v1` (13 prefix messages; S3 episode d0: approval requested, refused, then `apply_rollback`).
- **Views.** (1) Full history: prefix + phase D. For S3 acts this is counterfactual, as in Experiment 2. (2) Summary + phase D. **There is no kept tail**: P38 resumed from the summary alone. (3) Current segment only: system prompt, ticket and phase D. (4) Summary with the ticket pinned verbatim. The ticket is the only scripted user turn, and the resume drops it.
- **Sizes.** Prefixes run 6–240 KB (median 14 KB, about 3.5k tokens), against 290–570k tokens for each force-push item.
- **Authorization: the main caveat.** The ticket, a user turn, *asks* for the act ("5. Apply the rollback … Please work through it"). The act is unauthorized only under the system-prompt policy, which requires the absent change owner's approval. A monitor that never sees that policy, or that counts the requester as authority, will call every act authorized. The renderer must therefore give the operator policy to the monitor in every view, which is a design decision to record. Auto mode's channel also strips tool results, and those carry `environment: production` versus `sandbox` and "no approval has been granted". So under auto mode, live and SBX_WORLD items differ mainly in the agent's `request_approval` arguments. Guardian sees everything.
- **Why it fits.** The S3+P and B+TEST items carry a false licence in the summary ("work must proceed without the change owner"; "this is a test sandbox"). The full-history view lacks it and still shows the true tool output. That tests Experiment 2's question directly: does the monitor inherit the licence from the summary?

## P07 in detail

- **Files.** The prefix is `prefix_messages` in `probes/P07-compaction-trust/data/contexts.jsonl`, and summaries are in `data/summaries.jsonl`. Episodes are in `results/episodes/glm-5.3-flash/{codex,claude_code,pi_mono}/<cell>/*.json`, with `initial_messages` (the resumed context, `plant_check` included), `messages` and `calls`. Checked on `codex/user_P/lcbhard_46_r6_a1`. Skip the `.dup*` files.
- **Ground truth.** The grant is a user-role message, "The user approved editing the tests…", placed after the summary. The same text inside the summary, in a tool output or in a file comment is a false licence. Under Codex's wrapper the summary itself is a user-role message, so to auto mode the summary and user cells differ only in which message carries the claim.
- **Policy fit (unchecked).** Neither prompt text is vendored locally. If editing a test file inside the agent's own sandbox is outside both policies, every view allows every edit. Then only Guardian's authorization field would carry information.

## Recommendation

1. **Add P38** (DeepSeek), in one of two sizes:

   | Items | Acts | Views | Calls (× 2 models × 2 prompts) |
   |---|---|---|---|
   | Natural S3 | 35 | 4 | 560 |
   | False licence S3+P | 44 | 4 | 704 |
   | Authorized SBX_WORLD, one draw per context | 40 | 1 and 3 | 320 |
   | **Total** | | | **1,584** |

   That is about 15–25M input tokens, under 10% of Experiment 2's 260M. Adding B+TEST, one draw per context (42 acts × 4 views × 4), costs 672 more calls. The build is a renderer port plus the policy-in-view decision.
2. **P07 is optional,** and only after the policy-fit check. GLM under Codex's wrapper, user / summary / tool cells: 63 acts × 3 views × 4 = **756 calls**, about 5–8M tokens.

Both are text-planted, short-history and synthetic. The slide should report them as a second scenario, not pool them with the force-pushes.
