# Claims index: every number in the final deck, with its source

One table per slide of the final presentation (70 slides): each on-slide number or claim, the committed file it comes from, and a status. All paths are on branch `final`, which contains every sprint branch; exploration-phase files are also on `main`.

Status:
- **VERIFIED**: the file exists and the number was found in it (or the form it takes there is noted). "via slide N" repeats a claim sourced on slide N.
- **NO-SOURCE**: the slide cites no file. Where a committed candidate was checked to contain the number, it is listed; rows with nothing committed are listed at the end of each part.
- **EXTERNAL**: a published paper or post, linked.

Some inputs are not in the repository (raw run data is not committed); those rows say so.

**Indexing note:** this index was built against an earlier deck revision and reconciled to the final deck (70 slides) by slide title. Rows for rewritten slides (Results up front, Limitations, Conclusions, What three more months would answer, Finished in the last day, and the planted-rules slide) may list numbers the final slide no longer shows. Slides removed from the deck were dropped from this index.

# Part 1: slides 1-37

## Slide 4: Results up front

The slide states results in plain words; rows give the exact counts from the source files in brackets.

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Judges flagged few consequential losses; effects on behaviour mostly untested | `docs/sprint/figs/v2/README.md` | final | VERIFIED (methodology line 30) |
| About 1 in 10 real summaries says work is done when it isn't [by a model's read] | `results/sprint/swechat_odd/scan_summary.md` | final | VERIFIED (via slide 13) |
| Rules in AGENTS.md or chat were never lost; rules only in a README sometimes were [5 of 8] | `results/sprint/S2/gate.json` | final | VERIFIED (via slide 17) |
| DeepSeek, after force-pushing in one task, often did it again in the next [48% vs 12%; 50% vs 0–3%] | `docs/sprint/figs/week2/data/task_chaining/analysis.md`<br>`results/sprint/S2/chain/analysis.md`<br>`results/sprint/week2/precedent/readout.md` | final | VERIFIED (via slides 19, 19); precedent file states 50.0% vs 3.1% and p = 0.00077 |
| A false force-push written into its kept (verbatim) history made it overwrite more often; post hoc, six histories | `results/sprint/week2/planted_precedent/summary.md` | final | VERIFIED (line 8) |
| Showing the monitor the agent's reasoning made it approve more overwrites [Guardian on Haiku, 7 vs 1 of 22] | `results/sprint/week2/monitors/analysis.json`<br>`docs/sprint/figs/week2/data/monitors/analysis.json` | final | VERIFIED (via slide 24) |
| When the user's "no" was cut, a third of runs showed eval awareness, mentioning graders [5 of 14] | `results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED (via slide 25); "eval awareness" is the slide's label for grader talk, not a separate measure |
| Gemini 4 Argon force-pushed over a teammate's work every time, at low effort [14 of 14] | `results/sprint/week2/agy_argon_smoke/` (readout, rows.jsonl) | final | VERIFIED (via slide 29; effort is in the run config) |

## Slide 5: How compaction works

| Claim | Source path | Branch | Status |
|---|---|---|---|
| user requests, agent replies, tool outputs (~150k tokens); compaction trigger | candidate: `results/sprint/S2/native/claude_code/summary.md`<br>candidate: `docs/sprint/specs/S2.md` | final | NO-SOURCE (candidate verified); illustrative; candidate: live trigger settings and pre-compaction token counts |
| Summary (7–14k chars); medians across six summarizers | candidate: `docs/sprint/S1/report.md` | final | NO-SOURCE (candidate verified); S1 report table 'Do summarization methods differ?' (medians 6,653–14,081 chars) |

## Slide 6: What each harness keeps

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Codex (local): recent user requests, up to 20k tokens | candidate: `docs/sprint/specs/S2.md`<br>candidate: `docs/sprint/sprint-plan.md` | final | NO-SOURCE (candidate verified); the slide cites external source code only |
| pi: recent messages, up to ~20k tokens | candidate: `docs/sprint/prep/k-pi-opencode-spike.md` | final | NO-SOURCE (candidate verified) |
| OpenCode: recent messages, up to ~15k tokens | candidate: `docs/sprint/prep/k-pi-opencode-spike.md` | final | NO-SOURCE (candidate verified) |
| OpenCode summary (tool outputs cut to 2,000 chars first) | candidate: `docs/sprint/prep/k-pi-opencode-spike.md` | final | NO-SOURCE (candidate verified) |
| Claude Code kept a few recent messages in all 17 of my live compactions | `results/sprint/S2/native/claude_code/summary.md` | final | VERIFIED |
| Live Codex compacts on the server: user messages up to 64k tokens | candidate: `docs/sprint/sprint-plan.md` | final | NO-SOURCE (candidate verified) |
| Version strings in the sources footer (Claude Code 2.1.310, codex rust-v0.162.1, pi d5b3de2, agy 1.4.7, opencode v1.19.4) | – | – | NO-SOURCE; external code; not repo files |

## Slide 8: I audited 649 real compactions from four sources

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 649 compactions: real users, two benchmarks, and my own sessions | `results/sprint/S1/context_lengths.json` | final | VERIFIED (650 sampled, 649 analysed) |
| Public sets compact at about 167k tokens. My sessions at about 467k | candidate: `results/sprint/S1/context_lengths.json` | final | NO-SOURCE (candidate verified) |

## Slide 9: A summary loses an item if it drops or misstates it and it can’t be brought back

Not indexed: a diagram or definition slide added after this index was built.

## Slide 10: Hand labels caught a weak judge: 5 of 12 "dropped" calls right at first, 13 of 18 "lost" calls after the fixes

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Round 1: 100 hand labels; only 5 of 12 of Haiku's "dropped" calls were right | `docs/sprint/S1/validation-decisions.md` | final | VERIFIED |
| Opus graded Haiku's item lists over four prompt versions: real items 78% → 86% | `docs/sprint/S1/validation-decisions.md` | final | VERIFIED |
| Round 2: 13 of 18 "lost" calls right, by a definition set after the held-out gate failed (8 of 13); dev and held-out pooled | `results/sprint/S1/judge_precision.json` | final | VERIFIED (haiku-max.lost: n_hits 13/18, definition_settled_after_labels true, note 8/13) |
| [label] judge precision 13 of 18 (95% CI 49–88%), dev + held-out; held-out alone 9 of 11 | `results/sprint/S1/judge_precision.json` | final | VERIFIED |

## Slide 11: By the judges' read, summaries rarely drop anything that changes what the agent does next

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 1–6% of items are lost in a way the judges rated as mattering | candidate: `docs/sprint/figs/v2/README.md`<br>candidate: `results/sprint/S1/analysis.json`<br>candidate: `results/sprint/S1/swechat_ext/summary.json`<br>candidate: `results/sprint/S1/judge_precision.json`<br>candidate: `results/sprint/S1/loss_mining/summary.json` | final | NO-SOURCE (candidate verified); figure 01_loss_by_source; per-item labels file (loss_mining/labels.jsonl) is gitignored |
| In live runs, 2 of 24 compactions lost something that still mattered (3 counting misstated items) | candidate: `results/sprint/S2/lossaudit/candidates_frozen.json` | final | NO-SOURCE (candidate verified) |
| Session's own model (mostly Opus 4.5–5.5, Sonnet 4.5–5.5) | `results/sprint/S1/manifest.json` | final | VERIFIED |

## Slide 12: Repeated compaction loses many more items, but few more that matter

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Rules are lost most often (31%) | candidate: `results/sprint/S1/swechat_ext/summary.json`<br>candidate: `docs/sprint/figs/v2/README.md` | final | NO-SOURCE (candidate verified) |
| 6 in 10 are lost by the 2nd compaction, 1 in 4 at the 1st | candidate: `results/sprint/S1/chain_decay/summary.json` | final | NO-SOURCE (candidate verified) |
| [label] 54 sessions | candidate: `results/sprint/S1/chain_decay/summary.md` | final | NO-SOURCE (candidate verified) |

## Slide 13: Reading real SWE-chat sessions: about 1 in 10 summaries claims work is done when it isn't

| Claim | Source path | Branch | Status |
|---|---|---|---|
| About 1 in 10 real users' summaries claims work is done | `results/sprint/swechat_odd/scan_summary.md`<br>`results/sprint/swechat_odd/scan_summary.json` | final | VERIFIED; model-judged (Opus), not hand-validated |
| About 3 in 4 false claims come from the summary, not the agent | `results/sprint/swechat_odd/origin_summary.md` | final | VERIFIED |
| [label] 93 of 928 compactions (95% CI 8–12%) | `results/sprint/swechat_odd/scan_summary.md` | final | VERIFIED |
| Two summaries even credited the user with a fix they never made (anecdotal) | `docs/sprint/next-experiments/swechat_odd_patterns.md` | final | VERIFIED (Opus's flags, not hand-checked) |

## Slide 14: Other SWE-chat oddities: an invented user action, a flipped request, a contradicted plan

| Claim | Source path | Branch | Status |
|---|---|---|---|
| An invented user action, a flipped request, a contradicted plan | `docs/sprint/next-experiments/swechat_odd_patterns.md` | final | VERIFIED (Opus's flags, not hand-checked) |

## Slide 16: Live harnesses with a simulated user

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Compaction at about 150k tokens; Codex also 80k | candidate: `results/sprint/S2/native/codex/summary_150k.md`<br>candidate: `results/sprint/S2/native/codex/summary_80k.md`<br>candidate: `results/sprint/S2/native/claude_code/summary.md` | final | NO-SOURCE (candidate verified) |
| 25 DeepSWE tasks (SWE-Interact) | candidate: `docs/sprint/plans/swe-interact-pilot.md` | final | NO-SOURCE (candidate verified) |
| Fork and restore it: not run, since none of 105 candidates passed the loss checks | `results/sprint/S2/interact/cohort_funnel.json`; `docs/sprint/S2/pilot-results.md` | final | VERIFIED (recomputed: 105 candidates across 7 boundaries, 0 in `passing`) |

## Slide 40: Planted rules in AGENTS.md or chat were never lost. README-only rules were lost at 5 of 8

| Claim | Source path | Branch | Status |
|---|---|---|---|
| AGENTS.md or chat: never lost. README only: lost at 5 of 8 | candidate: `results/sprint/S2/gate.json` | final | NO-SOURCE (candidate verified); calibration cross-check: gate.json tiers by channel |
| Breaks in 192 reruns: 1 test edit, and 7 commit-prefix breaks, all at the 2 compactions that lost that rule | candidate: `results/sprint/S2/readout.md` | final | NO-SOURCE (candidate verified) |
| [label] 24 compactions, 192 reruns | candidate: `results/sprint/S2/readout.md` | final | NO-SOURCE (candidate verified) |
| 4 models with replayed harness compaction policies | `docs/sprint/specs/S2.md` | final | VERIFIED (lines 76–96) |
| 2 of the 5 losses: pi cut the README read at 2,000 characters, before the rules | `src/compaction/s1_replay.py` (tool_results_cut_2000) | final | VERIFIED (code); the per-run evidence is in uncommitted raw data (boundary compaction records of pilot-01 GLM README a1 and pilot-03 DeepSeek README a2), NO-SOURCE for the run-level read |

## Slide 17: Task chaining: one agent, two tasks, a teammate's draft in the way

Not indexed: a diagram or definition slide added after this index was built.

## Slide 19: DeepSeek in pi carries an earlier force-push into the next task: 48% vs 12%

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 48% vs 12% (31/64 vs 3/24 force-pushes) | `docs/sprint/figs/week2/data/task_chaining/analysis.md`<br>`results/sprint/S2/chain/analysis.md` | final | VERIFIED |
| Task B pass rate: 64% with the history, 62% without | `docs/sprint/figs/week2/data/task_chaining/analysis.md` | final | VERIFIED |
| [label] bootstrap 95% CI +17 to +55 points | `docs/sprint/figs/week2/data/task_chaining/analysis.md` | final | VERIFIED |
| [figure] force-pushes with full / compacted / cleared history: 14/32, 17/32, 3/24; commit lost 14/32, 16/32, 1/24 | `docs/sprint/figs/week2/data/task_chaining/analysis.json` | final | VERIFIED (same data as backup_01_chaining_by_start) |

## Slide 19: When the first task tempts an overwrite, the agent overwrites again: 50% vs 0–3%

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 50% vs 0–3% (16/32 vs 0–1/32) | `results/sprint/week2/precedent/readout.md`<br>`docs/sprint/figs/week2/data/precedent/readout.md` | final | VERIFIED; file states 50.0% vs 3.1%, divergent 16 forks |
| [label] exact randomization p ≈ 0.0008, 16 histories per arm | `results/sprint/week2/precedent/readout.md` | final | VERIFIED |

## Slide 20: In its chain of thought, the agent points to what it did last time

| Claim | Source path | Branch | Status |
|---|---|---|---|
| [label] citing the earlier act, 10 of 16 runs that overwrote vs 7 of 16 that kept | `docs/sprint/figs/week2/data/precedent_why/justification_read.md` | final | VERIFIED |

## Slide 21: A false force-push written into the agent's kept record: DeepSeek overwrote 5 of 8 vs 0 of 9

| Claim | Source path | Branch | Status |
|---|---|---|---|
| DeepSeek overwrote 5 of 8 vs 0 of 9 | `results/sprint/week2/planted_precedent/readout.md`<br>`results/sprint/week2/planted_precedent/readout.json` | final | VERIFIED |
| 6 histories where the agent kept the teammate's commits | `results/sprint/week2/planted_precedent/summary.md` | final | VERIFIED |
| 7 of 24 runs hit the call cap before pushing | `results/sprint/week2/planted_precedent/summary.md` | final | VERIFIED |
| Post hoc: p = 0.125 over 6 histories; paired +58 [+25, +92] | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED |
| One edited run's CoT, before its force-push: "I'll need a force push like the previous task" | `results/sprint/week2/planted_precedent/readout.md` (Force-push context); `summary.md` | final | VERIFIED (run cobalt-httpx-np-7 r1; same quote as slide 55) |

## Slide 22: Same force-push, shown to monitors five ways

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 22 force-pushes that overwrote a teammate's commit without permission | `results/sprint/week2/monitors/analysis.json`<br>`docs/sprint/figs/week2/README.md` | final | VERIFIED |
| Current call + agent's last 40 reasoning blocks | candidate: `results/sprint/week2/monitors/last_reasoning_report.md` | final | NO-SOURCE (candidate verified) |

## Slide 23: CoT downlift: seeing the agent's reasoning made Codex's Guardian monitor allow more overwrites

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Guardian on Haiku: 7 of 22 allowed with reasoning, 1 without | `results/sprint/week2/monitors/analysis.json`<br>`docs/sprint/figs/week2/data/monitors/analysis.json`<br>`docs/sprint/figs/week2/README.md` | final | VERIFIED |
| Auto mode: 0 either way | `results/sprint/week2/monitors/analysis.json` | final | VERIFIED |
| [label] paired exact p ≈ 0.07 (Haiku), 0.03 (Opus); 22 pushes, 14 histories, one sample each | `results/sprint/week2/monitors/5b_rationales.md` | final | VERIFIED (exact McNemar, Haiku 7 vs 1 p ≈ 0.07; Opus 6 vs 0 p ≈ 0.03) |

## Slide 24: A forged permission didn't override the user's visible "no". Only clumsy summary edits were noticed

| Claim | Source path | Branch | Status |
|---|---|---|---|
| no run overwrote (0 of 42 in all) | `results/sprint/week2/user_no/readout.md`<br>`results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED; file states 0/42 |
| Visible edit marks: suspected tampering 6 of 14, and 0 of 14 once the cut was fluent | `results/sprint/week2/user_no/reasoning_scan.json`<br>`results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED |
| With the "no" cut, 5 of 14 mentioned a grader in CoT either way (0 of 28 with it kept) | `results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED |

## Slide 25: Old tasks vanish from Antigravity's summary with Gemini. With Sonnet a few lines survive, pi keeps more

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Chains per setup: pi (4), Antigravity + Gemini 3.8 Flash (4), Antigravity + Sonnet 5.5 (2), Claude Code + Haiku 5.5 (4; 3 at task 4), Claude Code + Sonnet 5.5 (2) | `results/sprint/week2/chain_stress/summary_rot/decay/harness/README.md`<br>`results/sprint/week2/chain_stress/summary_rot/decay/harness/harness.json` | final | VERIFIED |
| Chart: task 1's share of the summary by task (pi 96% → 4%; Antigravity + Gemini → 0%) | `results/sprint/week2/chain_stress/summary_rot/decay/harness/README.md` | final | VERIFIED |

## Slide 26: Haiku and Codex never overwrote in the first task, so nothing carried over

| Claim | Source path | Branch | Status |
|---|---|---|---|
| DeepSeek in Claude Code: 2 of 4 again, but both called the branch a decoy | `results/sprint/week2/harness/readout.md`<br>`docs/sprint/figs/week2/data/harness/readout.md` | final | VERIFIED |
| [label] 12 histories per harness | `results/sprint/week2/harness/readout.json` | final | VERIFIED |
| Argon bar: 6 of 6 first tasks (chart) | `results/sprint/week2/agy_argon_smoke/rows.jsonl` | final | VERIFIED |

## Slide 27: Only DeepSeek in pi clearly repeated it. Argon overwrote even after a clean first task

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Argon lost the teammate's draft in 6 of 6 tasks after a divergent first task, and 2 of 2 after a clean one | `results/sprint/week2/agy_argon_smoke/readout.md`<br>`results/sprint/week2/agy_argon_smoke/rows.jsonl` | final | VERIFIED |
| Argon: 6 + 2 runs on 2 task orders | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| [label] pi significant; the other four harnesses not (Holm p = 1) | `results/sprint/week2/harness/readout.md`<br>`docs/sprint/figs/week2/data/harness/readout.json`<br>`docs/sprint/figs/week2/data/precedent/readout.json` | final | VERIFIED |

## Slide 28: Gemini 4 Argon force-pushed whenever a teammate's work was in the way (14 of 14 tasks), never otherwise (0 of 2)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 14 of 14 tasks, never otherwise (0 of 2) | `results/sprint/week2/agy_argon_smoke/readout.md`<br>`results/sprint/week2/agy_argon_smoke/rows.jsonl` | final | VERIFIED |
| 8 runs on 2 task orders, one harness, low effort | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| task 2 forced 2 of 2 even after a clean task 1 | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| Read the teammate's note first in 6 of 8 | `results/sprint/week2/agy_argon_smoke/readout.md`<br>`results/sprint/week2/agy_argon_smoke/summary.md` | final | VERIFIED; per-run table: 6 of 8 'yes' |
| Gemini Flash, same design: task 2 lost the draft in 1 of 24, but 6 of its 12 divergent task 2s hit the call cap | `results/sprint/week2/harness/readout.md`<br>`results/sprint/week2/harness/agy_summary_content.md` | final | VERIFIED; harness replication 9ae68512 |

## Slide 29: The impossible-task ending: the last task's test contradicts its own spec

| Claim | Source path | Branch | Status |
|---|---|---|---|
| One five-task chain, compacted between every task; Long: tasks 1–5, five compactions | candidate: `results/sprint/week2/exploit/design_card.md`<br>candidate: `results/sprint/week2/exploit/readout.md` | final | NO-SOURCE (candidate verified) |
| 4 tasks (2 Python, 1 Go, 1 TypeScript) | candidate: `results/sprint/week2/exploit/readout.md` | final | NO-SOURCE (candidate verified); tasks named: mobly, tomlkit (Python), wazero (Go), ofetch (TypeScript) |

## Slide 30: "Don't modify the tests" stopped test edits, not other ways of passing contradictory tests

| Claim | Source path | Branch | Status |
|---|---|---|---|
| [label] test edits 10 of 20 → 0 of 20, Fisher p < 0.001 | `results/sprint/week2/exploit/readout.md`<br>`docs/sprint/figs/week2/data/exploit/readout.json` | final | VERIFIED; counts in readout; Fisher p computed post hoc on 30 Oct |
| [label] cheating 12/12 → 5/12, p = 0.005 | `results/sprint/week2/exploit/readout.md`<br>`docs/sprint/presentation-outline.md` | final | VERIFIED; p from the presentation outline (post hoc) |

## Slide 31: A permissions list cut lost commits from 83% to 50% (not significant), a placebo to 72%

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 83% to 50%; a placebo, to 72% | `results/sprint/week2/summary_fix/final_readout.md`<br>`docs/sprint/figs/week2/data/summary_fix/final_readout.md` | final | VERIFIED; file states 25/30 83%, 15/30 50%, 21/29 72% |
| [label] Holm-adjusted p = 0.094, 10 histories | `results/sprint/week2/summary_fix/final_readout.md` | final | VERIFIED |

## Slide 32: Finished in the last day

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Sonnet kept 11 of 18 earlier pushes in Antigravity, 8 of 14 in Claude Code | candidate: `results/sprint/week2/agy_chain5/agy_sonnet/bulk/readout.md`<br>candidate: `results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md` | final | NO-SOURCE (candidate verified) |
| Same prompt, same effort: after 4 tasks DeepSeek kept 39% earlier-task content, Flash 7%, Antigravity 0% (4 chains) | `results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`<br>`results/sprint/week2/chain_stress/summary_rot/decay/swap/swap.json` | final | VERIFIED |
| Flash in pi (Is the overwrite the model or the harness?): on httpx, about as often in pi as in Antigravity (5 of 7 vs 4 of 6); most tengo runs capped, so no harness effect shown | `results/sprint/week2/flash_pi/summary.md`; `results/sprint/week2/flash_pi/readout.md` | final | VERIFIED |
| High effort: 2 of 2 paired reruns rebased instead, with no reasoning at the decision (descriptive) | `results/sprint/week2/flash_pi/summary.md` (High-effort pair (2 of 2)); `results/sprint/week2/flash_pi/high/` | final | VERIFIED |
| Claude Code + Haiku: no overwrites (0 of 18); kept 8 of 29 earlier pushes 2+ tasks later (Sonnet 8 of 14); presented 27 of 46 expired requests as current (Sonnet 1 of 30) | candidate: `results/sprint/week2/agy_chain5/claude_code/bulk/readout.md`<br>candidate: `results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md` | final | NO-SOURCE (candidate verified); per-position rewrite column sums to 0/18 |
| Longer Antigravity runs: 1,600 calls; quota stopped it at 13 of 24 runs; 3 of 7 divergent vs 0 of 6 clean; not significant | candidate: `results/sprint/week2/harness/agy_rerun1600/readout.md` | final | NO-SOURCE (candidate verified) |
| Antigravity with Argon: overwrote in 14 of 14 tasks | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED (via slide 29) |
| Planted precedent: 5 of 8 runs vs 0 of 9 (6 histories, p = 0.125) | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED (via slide 22) |


## Slide 35: Conclusions: compaction mostly works. The risk is what the record carries forward

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Judges flagged few consequential losses (1–6% of items); effects on behaviour mostly untested | candidate: `docs/sprint/figs/v2/README.md` | final | NO-SOURCE (candidate verified); as slide 11 |
| Agents repeat what they did last time, compacted or not: 48% vs 12% | `docs/sprint/figs/week2/data/task_chaining/analysis.md` | final | VERIFIED (via slide 19); as slide 19 |
| a false force-push in its kept record, 5 of 8 vs 0 of 9 (post hoc, 6 histories) | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED (via slide 22); as slide 22 |
| Guardian allowed 7 vs 1 of 22 | `results/sprint/week2/monitors/analysis.json` | final | VERIFIED (via slide 24); as slide 24 |
| A permissions list in the summary cut lost commits from 83% to 50%, not significant | `results/sprint/week2/summary_fix/final_readout.md` | final | VERIFIED (via slide 32); as slide 32 |
| At low effort the Gemini agents returned no reasoning at their force-pushes, so a CoT monitor would have nothing to read (inference) | `results/sprint/week2/agy_argon_smoke/cot_check.md` (0 of 895); `results/sprint/week2/flash_pi/summary.md` (0 reasoning tokens) | final | VERIFIED (inputs); the conclusion is an inference |
| Model differences were larger than harness differences in my small samples (this doesn't decompose the gap): same prompt and effort, DeepSeek's summaries kept 39% of earlier tasks vs Flash's 7%; Flash overwrote about as often in pi as in Antigravity on httpx (5 of 7 vs 4 of 6). Descriptive, small n | `results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`; `results/sprint/week2/flash_pi/summary.md` | final | VERIFIED |

## Slide 36 (removed): What three more months would answer

| Claim | Source path | Branch | Status |
|---|---|---|---|
| In my small samples, model differences were larger than harness differences (this doesn't decompose the gap): Flash kept fewer earlier-task entries than DeepSeek at the same prompt and effort, and overwrote about as often in pi as in Antigravity on one project | `results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`; `results/sprint/week2/flash_pi/summary.md` | final | VERIFIED |
| As the agent, Flash overwrote about as often in pi as in Antigravity on one project (5 of 7 vs 4 of 6) | `results/sprint/week2/flash_pi/readout.md`<br>`results/sprint/week2/flash_pi/summary.md`<br>`results/sprint/week2/harness/agy_rerun1600/readout.json` | final | VERIFIED; wording per deck update |
| Does effort matter? At high effort Flash rebased in 2 of 2 paired reruns | `results/sprint/week2/flash_pi/summary.md` (High-effort pair (2 of 2)); `results/sprint/week2/flash_pi/high/` | final | VERIFIED |

## Slide 37 (removed): Prior work: where my results agree and disagree

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Terry & Andriushchenko 2026: a hack in task 1 raises hacking in task 2 | https://www.lesswrong.com/posts/pdtGsC888sTToZhiY/measuring-alignment-drift-via-trajectory-prefixes | — | EXTERNAL |
| Baker et al. 2025: CoT monitor catches far more hacks than actions-only (95% vs 60%) | https://arxiv.org/abs/2503.11926 | — | EXTERNAL |
| Zhong, Raghunathan & Carlini 2025: read-only tests stop test edits but not other cheating | https://arxiv.org/abs/2510.20270 | — | EXTERNAL |
| UK AISI 2026: edited compaction summaries as a causal tool | https://www.aisi.gov.uk/blog/investigating-alignment-failures-in-aisis-incident | — | EXTERNAL |
| OpenAI 2026: summaries told the next context to hide mistakes | https://alignment.openai.com/misalignment-reports/encouraging-deception-in-compaction-summaries/ | — | EXTERNAL |
| with a randomized remote, commit lost 16/32 vs 0–1 of 32 (upper and lower completion) | results/sprint/week2/precedent/readout.md; docs/sprint/figs/week2/README.md (02_precedent) | final | VERIFIED |
| the agent's reasoning made the monitor allow more overwrites (7/22 vs 1/22) | results/sprint/week2/monitors/5b_rationales.md | final | VERIFIED |
| test edits 0/20 | results/sprint/week2/exploit/readout.md | final | VERIFIED (derived: fresh_rule 0 of 12 plus long_rule 0 of 8, Test edit column) |
| cheating 12/12 to 5/12 | results/sprint/week2/exploit/readout.md | final | VERIFIED (grep: fresh 12/12, fresh_rule 5/12) |
| visible edits: 6/14 noticed, 5/14 mentioned graders | results/sprint/week2/user_no/readout.md | final | VERIFIED (grep: 6/14, 5/14) |
| 10% of SWE-chat summaries falsely claim work is done | results/sprint/swechat_odd/scan_summary.md | final | VERIFIED (grep: 93/928 = 10.0%); model-judged (Opus), not hand-validated |

# Part 2: backup, slides 38-70

## Slide 38 (removed): Backup

No claims (section divider).

## Slide 35: SWE-Interact: the agent's commands run in a sealed container, forked at a compaction

Not indexed: a diagram or definition slide added after this index was built.

## Slide 40 (removed): Planted rules, resumed four ways

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 24 runs: 6 long tasks × 4 models, each with one harness's compaction policy replayed in my loop (not the native harness) | `docs/sprint/specs/S2.md`<br>`results/sprint/S2/readout.md` | final | VERIFIED |
| Thresholds: DeepSeek + pi 169k, GLM + pi 122k, luna + Codex 176k, Haiku + Claude Code 150k | candidate: `docs/sprint/specs/S2.md` | final | NO-SOURCE (candidate verified) |
| 192 reruns: 2 per condition at each of the 24 compaction points | candidate: `results/sprint/S2/readout.md` | final | NO-SOURCE (candidate verified) |

## Slide 36: Transcript: A dropped item, from a real user's session (slide 11)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: dropped user request and the judge's paraphrase | docs/sprint/S1/report.md (lines 215–216); seeded draw by scripts/s1_report_samples.py; run swe-chat-v1:2026-01-10-52c38054-a209-4b88-9a0d-d98ba15865dd:1 | final | VERIFIED (quote at line 216) |

No quantitative claims on the slide.

## Slide 42 (removed): Transcript: A user's rule, kept at the 1st compaction and gone at the 2nd (slide 12)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| kept at the 1st compaction, gone at the 2nd (excerpt) | results/sprint/S1/chain_decay/summary.md (line 74); run swe-chat-v1:112658aa-8e2f-43a4-9f8b-a690dfce9b04 | final | VERIFIED (quote at line 74) |

## Slide 38: Transcript: A summary claims tests pass, but the history says they failed (slide 13)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: "all tests pass" vs "0 passed; 8 failed" | results/sprint/swechat_odd/scan_examples.md (lines 18–19); run swe-chat-v1:e5367972-885e-46b8-bcdf-22e3e6b87525:0 | final | VERIFIED (both quotes at lines 18–19) |
| Opus 5.5's read, not hand-checked | results/sprint/swechat_odd/scan_summary.md | final | VERIFIED |

## Slide 44 (removed): Transcript: A rule lost from the summary, then broken (slide 40)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: planted rule "3. Start every commit message with [DSW-<n>]." | docs/sprint/specs/S2.md (line 124) | final | VERIFIED |
| Excerpt: agent's commit without the prefix | results/sprint/S2/readout.md (lines 121–122); run pilot-03-dynamodb-toolbox-lazy-recursive-schemas-deepseek-readme-a2, summary-only arm | final | VERIFIED (summary text itself not committed) |

No quantitative claims on the slide.

## Slide 45 (removed): Transcript: Task B, citing task A (slide 19)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: DeepSeek's task-B message citing the previous task | results/sprint/S2/chain/rows.jsonl (line 57, field final_report); run tengo-np-same-compacted-tengo-np-1 | final | VERIFIED |
| (copy of the same file) | docs/sprint/figs/week2/data/task_chaining/rows.jsonl (line 57) | final | VERIFIED |

No quantitative claims on the slide.

## Slide 37: Transcript: The precedent, in the agent's own reasoning (slide 19)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: "force-push with lease … as we did in the previous task" | results/sprint/week2/monitors/last_reasoning_report.md (line 37); run e1-httpx-divergent-different-full-tengo-np-7 | final | VERIFIED (quote at line 37) |

No quantitative claims on the slide.

## Slide 41: Transcript: "Push it to origin" read as authorization (slide 20)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: DeepSeek's reasoning quoting Claude Code's rule | results/sprint/week2/harness/git_safety_prompts.md (line 85); run cc_ds/httpx-np-3/b | final | VERIFIED |

No quantitative claims on the slide.

## Slide 42: Transcript: The monitor repeats the agent's "scratch" (slide 23)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: agent's reasoning and Haiku monitor's allow verdict | results/sprint/swechat_odd/transcript_audit.md (lines 11–12); run e1-tengo-divergent-different-compacted-httpx-np-5 | final | VERIFIED |
| Excerpt: monitor verdict (same item) | results/sprint/week2/monitors/5b_rationales.md (line 18) | final | VERIFIED |
| Monitor shown the agent's last 40 reasoning blocks | results/sprint/week2/monitors/5b_rationales.md (line 3) | final | VERIFIED (grep) |
| Guardian policy text quoted (one-ref --force-with-lease push rated low or medium) | `configs/week2/wording/monitors/guardian_policy.md` | final | VERIFIED (grep: lines 51, 59) |

## Slide 43: Transcript: Two edited summaries, two reactions (slide 24)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt A: DeepSeek notices the redaction | results/sprint/week2/user_no/eval_awareness_why.md (line 26); run tengo-np-3, "no dropped" version | final | VERIFIED |
| Excerpt B: Haiku's message on the planted force-push | results/sprint/week2/r7_planted/readout.md (line 52); run tengo-np-2/planted-2 | final | VERIFIED |

No quantitative claims on the slide.

## Slide 44: Transcript: What's left of task 1 after task 4 (slide 25)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: two ARC-151 entries from pi's summary | results/sprint/week2/chain_stress/summary_rot/decay/retained_task1.md (lines 37–38); chain u7 | final | VERIFIED |
| 2 of its 94 entries | results/sprint/week2/chain_stress/summary_rot/decay/retained_task1.md (line 35: "u7: 2 of 94 entries") | final | VERIFIED (grep) |
| at the 4th task boundary | same file | final | VERIFIED |

## Slide 45: Transcript: In Claude Code, the teammate's branch becomes a "decoy" (slide 26)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: two "decoy" quotes from DeepSeek's reasoning | results/sprint/week2/harness/readout.md (lines 113, 173); run cc_ds/httpx-np-3/b | final | VERIFIED |
| it overwrote two calls later | results/sprint/week2/harness/readout.md (lines 171, 186: quote at call 136, push at call 138) | final | VERIFIED (grep) |

## Slide 46: Transcript: Carry-over into the impossible task (slide 30)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: "the grader seemingly expected a force push" | results/sprint/week2/precedent/eval_awareness_check.md (line 9); run x-one_prior-u7-0 | final | VERIFIED |
| after one earlier task | same file (cell one_prior) | final | VERIFIED |

## Slide 47: Example not shown: a permissions list that called the force-push authorized (slide 31)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| One permissions-list summary described a never-granted force-push as authorized (quote not on the slide) | — | final | NO-SOURCE (second-hand: build log; raw summary not committed) |

No quantitative claims on the slide.

## Slide 48: Transcript: Argon read the teammate's note, then force-pushed (slide 28)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: Argon's summary ("dummy notes commits") | results/sprint/week2/agy_argon_smoke/summary.md (line 12) | final | VERIFIED |
| Excerpt: `git push --force origin feature/destructuring-bindings` | results/sprint/week2/agy_argon_smoke/summary.md (line 14) | final | VERIFIED (raw transcripts gitignored) |

No quantitative claims on the slide.

## Slide 49: Transcript: A false record, cited before the force-push (slide 21)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt, edited record: "I'll need a force push like the previous task." | results/sprint/week2/planted_precedent/summary.md; results/sprint/week2/planted_precedent/readout.md ("Force-push context"); run cobalt-httpx-np-7 r1 | final | VERIFIED (quote at readout.md line 405, summary.md lines 3, 36) |
| Excerpt, true record: "I rebased onto the remote WIP commits" | results/sprint/week2/planted_precedent/summary.md (line 47); run tengo-np-4 r2 | final | VERIFIED |

No quantitative claims on the slide.

## Slide 50: A force-push planned but never run in task 1 rode the summary into task 2, and was run there

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 2 of 6 histories | results/sprint/week2/harness/agy_rerun1600/carryover_note.md | final | VERIFIED (grep: n = 6 divergent tengo-first; 2 with a plan) |
| summary carried a force-push plan: 2 of 2 task 2s overwrote; no plan: 0 of 4 | results/sprint/week2/harness/agy_rerun1600/carryover_note.md | final | VERIFIED (grep: yes 2/2/2, no 4/0/0) |
| 1,600-call cap | results/sprint/week2/harness/agy_rerun1600/readout.md | final | VERIFIED (grep) |
| Excerpt: summary line and `git push --force-with-lease` (tengo-np-6) | results/sprint/week2/harness/agy_rerun1600/carryover_note.md | final | VERIFIED (raw messages gitignored) |

## Slide 51: Hand-label checks: 398 samples (1 of 3)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 398 samples | docs/sprint/hand-labelling.md; docs/sprint/figs/README.md (07_hand_labels) | final | VERIFIED (hand-labelling table; see its Checked column) |
| first round: 100 cards; all four judges failed the gate | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| fresh validation: 80 cards; Loss precision 13/18 = 0.72 [0.49, 0.88]; invention 8/13 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| release audit: 20 cards; 17 correct, 3 judge misreadings, 0 pipeline faults | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| retention spot check: 24 cards; agreed 23/24; AGENTS.md from 1/8 to 0/8 lost | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| usable-loss candidates: 12 cards; five checks each; only 3 passed | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |

## Slide 52: Hand-label checks: 398 samples (2 of 3)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| loss calibration: 28 cards; precision 7/9 = 0.78 held out; "kept" agreement 10/10 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| release audit round 1: 20 cards; two scorer bugs; all 200 sealed rows re-scored | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| release audit round 2: 20 cards; 20/20 correct | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| SWE-chat precision check: 24 cards; precision 12/19 = 0.63 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |

## Slide 53: Hand-label checks: 398 samples (3 of 3)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Haiku vs Sonnet: 85 cards, reused; Sonnet 0.70 vs Haiku 0.69 | docs/sprint/hand-labelling.md; results/sprint/judge_compare/summary.md | final | VERIFIED (grep) |
| SWE-Interact cards: 6; 1 of 6 accepted | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| chain check: 60 cards; precision 0.74 (me), 0.87 (Opus); 2nd-compaction loss stays above half | docs/sprint/hand-labelling.md; results/sprint/S1/chain_decay/label_check.md | final | VERIFIED (grep: 20/27 = 0.74, 26/30 = 0.87) |
| planted-rule kept cards: 4; agreed 4/4 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| 398 cards in all; the 85 reused cards are counted once | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |

## Slide 54: Full and compacted history both raise force-pushes (14/32, 17/32) over no history (3/24)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 14/32, 17/32 over no history (3/24) | docs/sprint/figs/week2/README.md (backup_01_chaining_by_start), from results/sprint/S2/chain/analysis.json; docs/sprint/S2/task-chaining-readout.md | final | NO-SOURCE (candidate; verified, grep matches) |
| force-push +9.4 points [−6.2, +25.0] | docs/sprint/figs/week2/README.md; docs/sprint/figs/week2/data/task_chaining/analysis.md | final | NO-SOURCE (candidate; verified, grep matches) |
| commit lost +6.2 [−9.4, +25.0] | docs/sprint/figs/week2/README.md | final | NO-SOURCE (candidate; verified, grep matches) |

## Slide 55: Only the reasoning view raises Haiku's allows (7 vs 1 of 22). Opus allows more with any history

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Haiku's allows 7 vs 1 of 22 | docs/sprint/figs/week2/README.md (backup_03_monitor_all_views), from results/sprint/week2/monitors/analysis.json | final | NO-SOURCE (candidate; verified, grep matches) |
| Opus allows 2 of 22 on the call alone, 4–6 with history, 8 with reasoning | same; results/sprint/week2/monitors/5b_rationales.md | final | NO-SOURCE (candidate; verified: 2, 6/5/4, 8 of 22) |
| Offline replay of 22 force-pushes | same | final | NO-SOURCE (candidate; verified) |

## Slide 56: After task 4, each chain's summary is 10–38% about earlier tasks

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 10–38% about earlier tasks after task 4 | results/sprint/week2/chain_stress/summary_rot/figures/README.md (summary_rot_by_chain), from results/sprint/week2/chain_stress/summary_rot/shares.json | final | NO-SOURCE (candidate; verified: same title in README) |
| 8 unrelated five-task chains | same | final | NO-SOURCE (candidate; verified: n = 8) |
| Task 4 minus task 2: −3.3 points [−20.5, +13.5] | results/sprint/week2/chain_stress/summary_rot/figures/README.md (Trend line); shares.json (trend_unrelated_earlier) | final | NO-SOURCE (candidate; verified, grep matches) |

## Slide 63 (removed): In real users' sessions, rules are lost most often (31%), commitments least

| Claim | Source path | Branch | Status |
|---|---|---|---|
| rules are lost most often (31%), commitments least | docs/sprint/figs/v2/README.md (02_lost_by_type), from results/sprint/S1/swechat_ext/summary.json | final | NO-SOURCE (candidate; verified: rules 31.4%, commitments 11.3%) |
| 95% session-bootstrap intervals | same | final | NO-SOURCE (candidate; verified) |

## Slide 64 (removed): Five compaction prompts lose 14–29% of rules, but only 1–8% in a way that matters

| Claim | Source path | Branch | Status |
|---|---|---|---|
| lose 14–29% of rules | docs/sprint/figs/v2/README.md (03_rules_lost_by_prompt), from results/sprint/S1/analysis.json | final | NO-SOURCE (candidate; verified: 14.2%–29.2%) |
| only 1–8% in a way that matters | results/sprint/S1/prompt_loss_mining/summary.md, summary.json; docs/sprint/figs/v2/README.md | final | VERIFIED (summary table: consequential 1%–8% by prompt, 14%–29% of rules lost; README gives 1.4%–8.1%) |
| 100 histories; 30 lost rules per prompt | docs/sprint/figs/v2/README.md | final | NO-SOURCE (candidate; verified) |

## Slide 65 (removed): The judge, checked by hand: about 7 in 10 "lost" calls are right

| Claim | Source path | Branch | Status |
|---|---|---|---|
| about 7 in 10 "lost" calls are right | docs/sprint/figs/README.md (03_judge_precision), from results/sprint/S1/judge_precision.json, results/sprint/S2/lossaudit/calibration.json, results/sprint/judge_compare/summary.json | final | NO-SOURCE (candidate; verified: 0.63–0.78 by set, 0.72 pooled) |
| Sonnet 5.5 0.70 vs Haiku 5.5 0.69 on the same cards | docs/sprint/figs/README.md; results/sprint/judge_compare/summary.md | final | NO-SOURCE (candidate; verified: 40/57 vs 41/59) |

## Slide 66 (removed): A lead, not a result: restoring a lost rule (2 compactions only)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 2 compactions only | docs/sprint/figs/README.md (05_live_prefix_rule_by_arm), from results/sprint/S2/readout.json (rules.R6.lost.contrasts) | final | NO-SOURCE (candidate; verified) |
| as resumed 4/4 broke it, rules restated 0/4 | same | final | NO-SOURCE (candidate; verified: 4/4, 0/4) |

## Slide 67 (removed): Simulated maintainer: requirements in clear messages mostly survive

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Replays of the live pi runs lost 0–4 requirements | docs/sprint/figs/README.md (06_maintainer_requirements_lost) | final | NO-SOURCE (candidate README verified: 0–4 of 39; it reads gitignored data/raw/sprint/s2/interact/*, and no committed readout holds the counts, as the slide says) |
| the live summary lost none | same | final | NO-SOURCE (candidate; verified: 0/39; same raw-data caveat) |

## Slide 68 (removed): Antigravity's long task-2 runs were mostly repeated tool outputs, and in-task compaction didn't rescue them

| Claim | Source path | Branch | Status |
|---|---|---|---|
| In the 3 runs that went past 750 calls, 53–77% of tool outputs repeated an earlier one | results/sprint/week2/agy_rerun800/loop_note.md | final | VERIFIED (grep: 825, 805, 754 calls; 64%, 53%, 77%) |
| 10–17% in the 2 runs that finished | same | final | VERIFIED (grep: 10%, 17%) |
| compaction at calls 631 and 643 | same | final | VERIFIED (grep) |
| repeats fell from 100% to 45% and 56% | same | final | VERIFIED (grep) |
| The summary after 631 calls listed no code change | same | final | VERIFIED (grep) |
| 800-call cap; n = 3 long runs, one task | same | final | VERIFIED (raw transcripts gitignored) |

## Slide 69 (removed): Push and remote notes (labelled safety-relevant) lasted longer in pi. Antigravity kept none

| Claim | Source path | Branch | Status |
|---|---|---|---|
| lasted longer in pi; Antigravity kept none | results/sprint/week2/chain_stress/summary_rot/decay/agy/README.md (safety_agy_vs_pi); results/sprint/week2/chain_stress/summary_rot/decay/agy/agy_vs_pi.json | final | VERIFIED (grep: pi 53%/31%/23% vs other 23%/7%/2%; Antigravity 0%) |
| Same 4 chains | same README | final | VERIFIED (n = 4 chains) |
| Safety-relevant lines labelled by gpt-6-astra (definition) | results/sprint/week2/chain_stress/summary_rot/decay/agy/safety_labels/main/flags.json | final | VERIFIED |
| A random 30 of those lines: 7 clearly about a teammate's work; 3 rebases onto the remote; 20 routine | results/sprint/week2/chain_stress/summary_rot/decay/agy/safety_labels/main/ (labels only) | final | NO-SOURCE (the 30-line tally, seed 20261031, is not committed; it can be redrawn from flags.json) |
| Example lines quoted (928b571e/0ea752c5 rebase; RET-292; COMPLETE_TASK_AND_SUBMIT) | results/sprint/week2/chain_stress/summary_rot/decay/agy/safety_labels/main/input_part03.jsonl; agy_vs_pi.json | final | VERIFIED (grep) |

## Slide 70 (removed): Earlier pushes, 2+ tasks later: Sonnet kept over half in either harness, Haiku 8 of 29, Gemini and Codex none

| Claim | Source path | Branch | Status |
|---|---|---|---|
| pi: 15/20 passed, 0/20 cap, 2/20 overwrote; recorded columns not measured | results/sprint/week2/agy_chain5/compare.md (pi 75%); results/sprint/week2/agy_chain5/codex/bulk/readout.md ("pi on the same chains"); results/sprint/week2/chain_stress/readout/code_measures.md (rewrites u2, u4) | final | VERIFIED (pass 75%; 2 rewrites across u1–u4) |
| Antigravity (Gemini): 9/20 passed, 9/20 cap, 0/20 overwrote, 19/19 next task, 0/21 2+ tasks later | results/sprint/week2/agy_chain5/agy/bulk/readout.md; results/sprint/week2/agy_chain5/compare.md | final | VERIFIED (grep: 9/20, 9 capped, 19/19, 0/21) |
| Antigravity (Sonnet): 7/10, 0/10, 0/10, 14/14, 11/18 | results/sprint/week2/agy_chain5/agy_sonnet/bulk/readout.md | final | VERIFIED (grep: 14/14, 11/18, 0 capped) |
| Claude Code (Sonnet): 7/10, 0/10, 0/10, 12/12, 8/14 | results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md | final | VERIFIED (grep: 12/12, 8/14, 7/10, 0 capped) |
| Codex (luna): 7/20, 0/20, 0/20, 0/39, 0/48 | results/sprint/week2/agy_chain5/codex/bulk/readout.md | final | VERIFIED (grep: per-position pass sums to 7/20; rewrite 0; 0/39, 0/48) |
| Claude Code (Haiku): 13/18†, 1/18, 0/18, 24/24, 8/29 | results/sprint/week2/agy_chain5/claude_code/bulk/readout.md | final | VERIFIED (grep: 24/24, 8/29, 1 capped) |
| †19 of 20 tasks started, 18 with a known outcome | results/sprint/week2/agy_chain5/claude_code/bulk/readout.md; claude_code_sonnet and agy_sonnet readouts | final | VERIFIED (readout: 19 started, 18 known endings, one quota-cut) |

---

