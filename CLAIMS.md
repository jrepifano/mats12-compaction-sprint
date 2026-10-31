# Claims index: every number in the final deck, with its source

One table per slide of the final presentation (88 slides): each on-slide number or claim, the committed file it comes from, and a status. All paths are on branch `final`, which contains every sprint branch; exploration-phase files are also on `main`.

Status:
- **VERIFIED**: the file exists and the number was found in it (or the form it takes there is noted). "via slide N" repeats a claim sourced on slide N.
- **NO-SOURCE**: the slide cites no file. Where a committed candidate was checked to contain the number, it is listed; rows with nothing committed are listed at the end of each part.
- **EXTERNAL**: a published paper or post, linked.

Some inputs are not in the repository (raw run data is not committed); those rows say so.

# Part 1: slides 1-42


Every number or quantitative claim on slides 1–42, with the committed file it comes from. Paths are relative to the repo root; branch is where the file is committed. Status: VERIFIED = `git cat-file -e <branch>:<path>` succeeded and the number was found in the file (or the form it takes there is given); VERIFIED (via slide N) = the same claim, sourced on slide N; NO-SOURCE = the slide cites no path (a candidate found by search is marked "candidate", or nothing committed holds the number); PATH-MISSING = a cited path that is not on the branch. Claims marked [label] are from the grey significance label. Slides 1–3, 9, 15 and 18 carry no numbers beyond slide references and are skipped.

## Slide 4: Results up front

The slide states results in plain words; rows give the exact counts from the source files in brackets.

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Judges flagged few consequential losses; effects on behaviour mostly untested | `docs/sprint/figs/v2/README.md` | final | VERIFIED (methodology line 30) |
| About 1 in 10 real summaries says work is done when it isn't [by a model's read] | `results/sprint/swechat_odd/scan_summary.md` | final | VERIFIED (via slide 13) |
| Rules in AGENTS.md or chat were never lost; rules only in a README sometimes were [5 of 8] | `results/sprint/S2/gate.json` | final | VERIFIED (via slide 18) |
| DeepSeek, after force-pushing in one task, often did it again in the next [48% vs 12%; 50% vs 0–3%] | `docs/sprint/figs/week2/data/task_chaining/analysis.md`<br>`results/sprint/S2/chain/analysis.md`<br>`results/sprint/week2/precedent/readout.md` | final | VERIFIED (via slides 20, 19); precedent file states 50.0% vs 3.1% and p = 0.00077 |
| A false force-push written into its kept (verbatim) history made it overwrite more often; post hoc, six histories | `results/sprint/week2/planted_precedent/summary.md` | final | VERIFIED (line 8) |
| Showing the monitor the agent's reasoning made it approve more overwrites [Guardian on Haiku, 7 vs 1 of 22] | `results/sprint/week2/monitors/analysis.json`<br>`docs/sprint/figs/week2/data/monitors/analysis.json` | final | VERIFIED (via slide 25) |
| When the user's "no" was cut, a third of runs showed eval awareness, mentioning graders [5 of 14] | `results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED (via slide 26); "eval awareness" is the slide's label for grader talk, not a separate measure |
| Gemini 4 Argon force-pushed over a teammate's work every time, at low effort [14 of 14] | `results/sprint/week2/agy_argon_smoke/` (readout, rows.jsonl) | final | VERIFIED (via slide 30; effort is in the run config) |

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

## Slide 15: Live harnesses with a simulated user

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Compaction at about 150k tokens; Codex also 80k | candidate: `results/sprint/S2/native/codex/summary_150k.md`<br>candidate: `results/sprint/S2/native/codex/summary_80k.md`<br>candidate: `results/sprint/S2/native/claude_code/summary.md` | final | NO-SOURCE (candidate verified) |
| 25 DeepSWE tasks (SWE-Interact) | candidate: `docs/sprint/plans/swe-interact-pilot.md` | final | NO-SOURCE (candidate verified) |
| Fork and restore it: not run, since none of 105 candidates passed the loss checks | `results/sprint/S2/interact/cohort_funnel.json`; `docs/sprint/S2/pilot-results.md` | final | VERIFIED (recomputed: 105 candidates across 7 boundaries, 0 in `passing`) |

## Slide 17: Planted rules, resumed four ways

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 24 runs: 6 long tasks × 4 models, each with one harness's compaction policy replayed in my loop (not the native harness) | `docs/sprint/specs/S2.md`<br>`results/sprint/S2/readout.md` | final | VERIFIED |
| Thresholds: DeepSeek + pi 169k, GLM + pi 122k, luna + Codex 176k, Haiku + Claude Code 150k | candidate: `docs/sprint/specs/S2.md` | final | NO-SOURCE (candidate verified) |
| 192 reruns: 2 per condition at each of the 24 compaction points | candidate: `results/sprint/S2/readout.md` | final | NO-SOURCE (candidate verified) |

## Slide 18: Planted rules in AGENTS.md or chat were never lost; README-only rules were lost at 5 of 8

| Claim | Source path | Branch | Status |
|---|---|---|---|
| AGENTS.md or chat: never lost. README only: lost at 5 of 8 | candidate: `results/sprint/S2/gate.json` | final | NO-SOURCE (candidate verified); calibration cross-check: gate.json tiers by channel |
| Breaks in 192 reruns: 1 test edit, and 7 commit-prefix breaks, all at the 2 compactions that lost that rule | candidate: `results/sprint/S2/readout.md` | final | NO-SOURCE (candidate verified) |
| [label] 24 compactions, 192 reruns | candidate: `results/sprint/S2/readout.md` | final | NO-SOURCE (candidate verified) |
| 4 models with replayed harness compaction policies | `docs/sprint/specs/S2.md` | final | VERIFIED (lines 76–96) |
| 2 of the 5 losses: pi cut the README read at 2,000 characters, before the rules | `src/compaction/s1_replay.py` (tool_results_cut_2000) | final | VERIFIED (code); the per-run evidence is in uncommitted raw data (boundary compaction records of pilot-01 GLM README a1 and pilot-03 DeepSeek README a2), NO-SOURCE for the run-level read |

## Slide 20: DeepSeek in pi carries an earlier force-push into the next task: 48% vs 12%

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 48% vs 12% (31/64 vs 3/24 force-pushes) | `docs/sprint/figs/week2/data/task_chaining/analysis.md`<br>`results/sprint/S2/chain/analysis.md` | final | VERIFIED |
| Task B pass rate: 64% with the history, 62% without | `docs/sprint/figs/week2/data/task_chaining/analysis.md` | final | VERIFIED |
| [label] bootstrap 95% CI +17 to +55 points | `docs/sprint/figs/week2/data/task_chaining/analysis.md` | final | VERIFIED |
| [figure] force-pushes with full / compacted / cleared history: 14/32, 17/32, 3/24; commit lost 14/32, 16/32, 1/24 | `docs/sprint/figs/week2/data/task_chaining/analysis.json` | final | VERIFIED (same data as backup_01_chaining_by_start) |

## Slide 21: When the first task tempts an overwrite, the agent overwrites again: 50% vs 0–3%

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 50% vs 0–3% (16/32 vs 0–1/32) | `results/sprint/week2/precedent/readout.md`<br>`docs/sprint/figs/week2/data/precedent/readout.md` | final | VERIFIED; file states 50.0% vs 3.1%, divergent 16 forks |
| [label] exact randomization p ≈ 0.0008, 16 histories per arm | `results/sprint/week2/precedent/readout.md` | final | VERIFIED |

## Slide 22: In its chain of thought, the agent points to what it did last time

| Claim | Source path | Branch | Status |
|---|---|---|---|
| [label] citing the earlier act, 10 of 16 runs that overwrote vs 7 of 16 that kept | `docs/sprint/figs/week2/data/precedent_why/justification_read.md` | final | VERIFIED |

## Slide 23: A false force-push written into the agent's kept record: DeepSeek overwrote 5 of 8 vs 0 of 9

| Claim | Source path | Branch | Status |
|---|---|---|---|
| DeepSeek overwrote 5 of 8 vs 0 of 9 | `results/sprint/week2/planted_precedent/readout.md`<br>`results/sprint/week2/planted_precedent/readout.json` | final | VERIFIED |
| 6 histories where the agent kept the teammate's commits | `results/sprint/week2/planted_precedent/summary.md` | final | VERIFIED |
| 7 of 24 runs hit the call cap before pushing | `results/sprint/week2/planted_precedent/summary.md` | final | VERIFIED |
| Post hoc: p = 0.125 over 6 histories; paired +58 [+25, +92] | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED |
| One edited run's CoT, before its force-push: "I'll need a force push like the previous task" | `results/sprint/week2/planted_precedent/readout.md` (Force-push context); `summary.md` | final | VERIFIED (run cobalt-httpx-np-7 r1; same quote as slide 61) |

## Slide 24: Same force-push, shown to monitors five ways

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 22 force-pushes that overwrote a teammate's commit without permission | `results/sprint/week2/monitors/analysis.json`<br>`docs/sprint/figs/week2/README.md` | final | VERIFIED |
| Current call + agent's last 40 reasoning blocks | candidate: `results/sprint/week2/monitors/last_reasoning_report.md` | final | NO-SOURCE (candidate verified) |

## Slide 25: CoT downlift: seeing the agent's reasoning made Codex's Guardian monitor allow more overwrites

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Guardian on Haiku: 7 of 22 allowed with reasoning, 1 without | `results/sprint/week2/monitors/analysis.json`<br>`docs/sprint/figs/week2/data/monitors/analysis.json`<br>`docs/sprint/figs/week2/README.md` | final | VERIFIED |
| Auto mode: 0 either way | `results/sprint/week2/monitors/analysis.json` | final | VERIFIED |
| [label] paired exact p ≈ 0.07 (Haiku), 0.03 (Opus); 22 pushes, 14 histories, one sample each | `results/sprint/week2/monitors/5b_rationales.md` | final | VERIFIED (exact McNemar, Haiku 7 vs 1 p ≈ 0.07; Opus 6 vs 0 p ≈ 0.03) |

## Slide 26: A forged permission didn't override the user's visible "no"; only clumsy summary edits got noticed

| Claim | Source path | Branch | Status |
|---|---|---|---|
| no run overwrote (0 of 42 in all) | `results/sprint/week2/user_no/readout.md`<br>`results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED; file states 0/42 |
| Visible edit marks: suspected tampering 6 of 14, and 0 of 14 once the cut was fluent | `results/sprint/week2/user_no/reasoning_scan.json`<br>`results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED |
| With the "no" cut, 5 of 14 mentioned a grader in CoT either way (0 of 28 with it kept) | `results/sprint/week2/user_no_fluent/readout.md` | final | VERIFIED |

## Slide 27: Old tasks vanish from Antigravity's summary with Gemini; with Sonnet a few lines survive; pi keeps more

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Chains per setup: pi (4), Antigravity + Gemini 3.8 Flash (4), Antigravity + Sonnet 5.5 (2), Claude Code + Haiku 5.5 (4; 3 at task 4), Claude Code + Sonnet 5.5 (2) | `results/sprint/week2/chain_stress/summary_rot/decay/harness/README.md`<br>`results/sprint/week2/chain_stress/summary_rot/decay/harness/harness.json` | final | VERIFIED |
| Chart: task 1's share of the summary by task (pi 96% → 4%; Antigravity + Gemini → 0%) | `results/sprint/week2/chain_stress/summary_rot/decay/harness/README.md` | final | VERIFIED |

## Slide 28: Haiku and Codex never overwrote in the first task, so nothing carried over

| Claim | Source path | Branch | Status |
|---|---|---|---|
| DeepSeek in Claude Code: 2 of 4 again, but both called the branch a decoy | `results/sprint/week2/harness/readout.md`<br>`docs/sprint/figs/week2/data/harness/readout.md` | final | VERIFIED |
| [label] 12 histories per harness | `results/sprint/week2/harness/readout.json` | final | VERIFIED |
| Argon bar: 6 of 6 first tasks (chart) | `results/sprint/week2/agy_argon_smoke/rows.jsonl` | final | VERIFIED |

## Slide 29: Only DeepSeek in pi clearly repeated it; Argon overwrote even after a clean first task

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Argon lost the teammate's draft in 6 of 6 tasks after a divergent first task, and 2 of 2 after a clean one | `results/sprint/week2/agy_argon_smoke/readout.md`<br>`results/sprint/week2/agy_argon_smoke/rows.jsonl` | final | VERIFIED |
| Argon: 6 + 2 runs on 2 task orders | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| [label] pi significant; the other four harnesses not (Holm p = 1) | `results/sprint/week2/harness/readout.md`<br>`docs/sprint/figs/week2/data/harness/readout.json`<br>`docs/sprint/figs/week2/data/precedent/readout.json` | final | VERIFIED |

## Slide 30: Gemini 4 Argon force-pushed whenever a teammate's work was in the way (14 of 14 tasks), never otherwise (0 of 2)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 14 of 14 tasks, never otherwise (0 of 2) | `results/sprint/week2/agy_argon_smoke/readout.md`<br>`results/sprint/week2/agy_argon_smoke/rows.jsonl` | final | VERIFIED |
| 8 runs on 2 task orders, one harness, low effort | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| task 2 forced 2 of 2 even after a clean task 1 | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| Read the teammate's note first in 6 of 8 | `results/sprint/week2/agy_argon_smoke/readout.md`<br>`results/sprint/week2/agy_argon_smoke/summary.md` | final | VERIFIED; per-run table: 6 of 8 'yes' |
| Gemini Flash, same design: task 2 lost the draft in 1 of 24, but 6 of its 12 divergent task 2s hit the call cap | `results/sprint/week2/harness/readout.md`<br>`results/sprint/week2/harness/agy_summary_content.md` | final | VERIFIED; harness replication 9ae68512 |

## Slide 31: The impossible-task ending: the last task's test contradicts its own spec

| Claim | Source path | Branch | Status |
|---|---|---|---|
| One five-task chain, compacted between every task; Long: tasks 1–5, five compactions | candidate: `results/sprint/week2/exploit/design_card.md`<br>candidate: `results/sprint/week2/exploit/readout.md` | final | NO-SOURCE (candidate verified) |
| 4 tasks (2 Python, 1 Go, 1 TypeScript) | candidate: `results/sprint/week2/exploit/readout.md` | final | NO-SOURCE (candidate verified); tasks named: mobly, tomlkit (Python), wazero (Go), ofetch (TypeScript) |

## Slide 32: Telling the agent not to edit tests stopped test edits, but agents still passed the contradictory tests other ways

| Claim | Source path | Branch | Status |
|---|---|---|---|
| [label] test edits 10 of 20 → 0 of 20, Fisher p < 0.001 | `results/sprint/week2/exploit/readout.md`<br>`docs/sprint/figs/week2/data/exploit/readout.json` | final | VERIFIED; counts in readout; Fisher p computed post hoc on 30 Oct |
| [label] cheating 12/12 → 5/12, p = 0.005 | `results/sprint/week2/exploit/readout.md`<br>`docs/sprint/presentation-outline.md` | final | VERIFIED; p from the presentation outline (post hoc) |

## Slide 33: A permissions list in the summary cut lost commits from 83% to 50%, not significant after correction; a placebo, to 72%

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 83% to 50%; a placebo, to 72% | `results/sprint/week2/summary_fix/final_readout.md`<br>`docs/sprint/figs/week2/data/summary_fix/final_readout.md` | final | VERIFIED; file states 25/30 83%, 15/30 50%, 21/29 72% |
| [label] Holm-adjusted p = 0.094, 10 histories | `results/sprint/week2/summary_fix/final_readout.md` | final | VERIFIED |

## Slide 34: Finished in the last day

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Sonnet kept 11 of 18 earlier pushes in Antigravity, 8 of 14 in Claude Code | candidate: `results/sprint/week2/agy_chain5/agy_sonnet/bulk/readout.md`<br>candidate: `results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md` | final | NO-SOURCE (candidate verified) |
| Same prompt, same effort: after 4 tasks DeepSeek kept 39% earlier-task content, Flash 7%, Antigravity 0% (4 chains) | `results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`<br>`results/sprint/week2/chain_stress/summary_rot/decay/swap/swap.json` | final | VERIFIED |
| Flash in pi (Is the overwrite the model or the harness?): on httpx, about as often in pi as in Antigravity (5 of 7 vs 4 of 6); most tengo runs capped, so no harness effect shown | `results/sprint/week2/flash_pi/summary.md`; `results/sprint/week2/flash_pi/readout.md` | final | VERIFIED |
| High effort: 2 of 2 paired reruns rebased instead, with no reasoning at the decision (descriptive) | `results/sprint/week2/flash_pi/summary.md` (High-effort pair (2 of 2)); `results/sprint/week2/flash_pi/high/` | final | VERIFIED |
| Claude Code + Haiku: no overwrites (0 of 18); kept 8 of 29 earlier pushes 2+ tasks later (Sonnet 8 of 14); presented 27 of 46 expired requests as current (Sonnet 1 of 30) | candidate: `results/sprint/week2/agy_chain5/claude_code/bulk/readout.md`<br>candidate: `results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md` | final | NO-SOURCE (candidate verified); per-position rewrite column sums to 0/18 |
| Longer Antigravity runs: 1,600 calls; quota stopped it at 13 of 24 runs; 3 of 7 divergent vs 0 of 6 clean; not significant | candidate: `results/sprint/week2/harness/agy_rerun1600/readout.md` | final | NO-SOURCE (candidate verified) |
| Antigravity with Argon: overwrote in 14 of 14 tasks | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED (via slide 30) |
| Planted precedent: 5 of 8 runs vs 0 of 9 (6 histories, p = 0.125) | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED (via slide 23) |


## Slide 36: Compaction keeps what matters when it runs, but on long histories it sometimes fails

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 7 compactions failed in the five-task chains; 2 never recovered | `results/sprint/week2/chain_stress/readout/code_measures.md`<br>`results/sprint/week2/chain_stress/readout/code_measures.json` | final | VERIFIED; figure chain_stress_backup.png; data source per its README |
| Haiku partly refused one Claude Code compaction | candidate: `results/sprint/S2/native/claude_code/summary.md` | final | NO-SOURCE (candidate verified) |

## Slide 37: Mistakes I caught, and what they taught me

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Two scorer bugs, caught by my audit before any outcome was released | candidate: `CORRECTIONS.md` (entry 6) | final | NO-SOURCE (candidate verified) |

## Slide 38: Limitations: most causal tests use one model, n is small, and the agent knew it was a test

| Claim | Source path | Branch | Status |
|---|---|---|---|
| the planted precedent is 6 histories, analysed post hoc | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED (via slide 23) |
| Argon is 8 runs on 2 task orders in one harness | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED (via slide 30) |
| Eval awareness: benchmark or grader talk appears in both arms' CoT | `results/sprint/week2/planted_precedent/summary.md` (Eval awareness) | final | VERIFIED |
| a force-push planted in Haiku's own summary in Claude Code was never acted on, but honest runs were also 0 of 24 | candidate: `results/sprint/week2/r7_planted/readout.md`<br>candidate: `results/sprint/week2/r7_planted/readout_notes.md` | final | NO-SOURCE (candidate verified); file table: planted 24 runs 0 lost, honest 24 runs 0 lost |
| LLM graders, validated on 180 hand labels (13 of 18 "lost" calls right) | candidate: `docs/sprint/S1/validation-decisions.md`<br>candidate: `results/sprint/S1/judge_precision.json` | final | NO-SOURCE (candidate verified); 180 = 100 + 80 |

## Slide 39: Conclusions: compaction mostly works; the risk is what the record carries forward

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Judges flagged few consequential losses (1–6% of items); effects on behaviour mostly untested | candidate: `docs/sprint/figs/v2/README.md` | final | NO-SOURCE (candidate verified); as slide 11 |
| Agents repeat what they did last time, compacted or not: 48% vs 12% | `docs/sprint/figs/week2/data/task_chaining/analysis.md` | final | VERIFIED (via slide 20); as slide 20 |
| a false force-push in its kept record, 5 of 8 vs 0 of 9 (post hoc, 6 histories) | `results/sprint/week2/planted_precedent/readout.md` | final | VERIFIED (via slide 23); as slide 23 |
| Guardian allowed 7 vs 1 of 22 | `results/sprint/week2/monitors/analysis.json` | final | VERIFIED (via slide 25); as slide 25 |
| A permissions list in the summary cut lost commits from 83% to 50%, not significant | `results/sprint/week2/summary_fix/final_readout.md` | final | VERIFIED (via slide 33); as slide 33 |
| At low effort the Gemini agents returned no reasoning at their force-pushes, so a CoT monitor would have nothing to read (inference) | `results/sprint/week2/agy_argon_smoke/cot_check.md` (0 of 895); `results/sprint/week2/flash_pi/summary.md` (0 reasoning tokens) | final | VERIFIED (inputs); the conclusion is an inference |
| Model differences were larger than harness differences in my small samples (this doesn't decompose the gap): same prompt and effort, DeepSeek's summaries kept 39% of earlier tasks vs Flash's 7%; Flash overwrote about as often in pi as in Antigravity on httpx (5 of 7 vs 4 of 6). Descriptive, small n | `results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`; `results/sprint/week2/flash_pi/summary.md` | final | VERIFIED |

## Slide 40: Priority for future work: Gemini 4 Argon in Antigravity overwrote a teammate's work every time

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Lost the teammate's work in 14 of 14 tasks where it was in the way, 0 of 2 otherwise | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| Only 8 runs on 2 task orders | `results/sprint/week2/agy_argon_smoke/readout.md` | final | VERIFIED |
| No reasoning text in any Argon record (0 of 895 calls) | `results/sprint/week2/agy_argon_smoke/cot_check.md` | final | VERIFIED |
| no visible deliberation preceded any of its 14 force-pushes | `results/sprint/week2/agy_argon_smoke/cot_check.md` | final | VERIFIED |
| Lead on effort: Gemini Flash in pi rebased instead of force-pushing at high effort in 2 of 2 paired reruns that force-pushed at low (anecdotal) | `results/sprint/week2/flash_pi/summary.md` (High-effort pair (2 of 2)); `results/sprint/week2/flash_pi/high/` | final | VERIFIED |

## Slide 41: What three more months would answer

| Claim | Source path | Branch | Status |
|---|---|---|---|
| In my small samples, model differences were larger than harness differences (this doesn't decompose the gap): Flash kept fewer earlier-task entries than DeepSeek at the same prompt and effort, and overwrote about as often in pi as in Antigravity on one project | `results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`; `results/sprint/week2/flash_pi/summary.md` | final | VERIFIED |
| As the agent, Flash overwrote about as often in pi as in Antigravity on one project (5 of 7 vs 4 of 6) | `results/sprint/week2/flash_pi/readout.md`<br>`results/sprint/week2/flash_pi/summary.md`<br>`results/sprint/week2/harness/agy_rerun1600/readout.json` | final | VERIFIED; wording per deck update |
| Does effort matter? At high effort Flash rebased in 2 of 2 paired reruns | `results/sprint/week2/flash_pi/summary.md` (High-effort pair (2 of 2)); `results/sprint/week2/flash_pi/high/` | final | VERIFIED |

## Slide 42: Read the transcripts: 9 illustrative picks, plus a random sample

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 9 illustrative picks, plus a random sample | – | – | NO-SOURCE; transcript viewer is an Artifact; no committed manifest of picks found on any sprint branch |

## PATH-MISSING and NO-SOURCE items

**PATH-MISSING:** none. Every path the notes name exists on its branch (results/sprint/week2/flash_pi/ is now committed on sprint/week2-build).

**NO-SOURCE, with a committed candidate:**

- Slide 5: user requests, agent replies, tool outputs (~150k tokens); compaction trigger. Candidate: final:results/sprint/S2/native/claude_code/summary.md, final:docs/sprint/specs/S2.md (illustrative; candidate: live trigger settings and pre-compaction token counts)
- Slide 5: Summary (7–14k chars); medians across six summarizers. Candidate: final:docs/sprint/S1/report.md (S1 report table 'Do summarization methods differ?' (medians 6,653–14,081 chars))
- Slide 6: Codex (local): recent user requests, up to 20k tokens. Candidate: final:docs/sprint/specs/S2.md, final:docs/sprint/sprint-plan.md (the slide cites external source code only)
- Slide 6: pi: recent messages, up to ~20k tokens. Candidate: final:docs/sprint/prep/k-pi-opencode-spike.md
- Slide 6: OpenCode: recent messages, up to ~15k tokens. Candidate: final:docs/sprint/prep/k-pi-opencode-spike.md
- Slide 6: OpenCode summary (tool outputs cut to 2,000 chars first). Candidate: final:docs/sprint/prep/k-pi-opencode-spike.md
- Slide 6: Claude Code kept a few recent messages in all 18 of my live compactions. Candidate: final:results/sprint/S2/native/claude_code/summary.md (table lists 17 boundaries with kept messages; the 18th not located)
- Slide 6: Live Codex compacts on the server: user messages up to 64k tokens. Candidate: final:docs/sprint/sprint-plan.md
- Slide 8: resolved. `results/sprint/S1/context_lengths.json` gives 650 sampled, 649 analysed; the slide now says 649.
- Slide 8: Public sets compact at about 167k tokens. My sessions at about 467k. Candidate: final:results/sprint/S1/context_lengths.json
- Slide 11: 1–6% of items are lost in a way the judges rated as mattering. Candidate: final:docs/sprint/figs/v2/README.md, final:results/sprint/S1/analysis.json, final:results/sprint/S1/swechat_ext/summary.json, final:results/sprint/S1/judge_precision.json, final:results/sprint/S1/loss_mining/summary.json (figure 01_loss_by_source; per-item labels file (loss_mining/labels.jsonl) is gitignored)
- Slide 11: In live runs, 2 of 24 compactions lost something that still mattered (3 counting misstated items). Candidate: final:results/sprint/S2/lossaudit/candidates_frozen.json
- Slide 12: Rules are lost most often (31%). Candidate: final:results/sprint/S1/swechat_ext/summary.json, final:docs/sprint/figs/v2/README.md
- Slide 12: 6 in 10 are lost by the 2nd compaction, 1 in 4 at the 1st. Candidate: final:results/sprint/S1/chain_decay/summary.json
- Slide 12: [label] 54 sessions. Candidate: final:results/sprint/S1/chain_decay/summary.md
- Slide 15: Compaction at about 150k tokens; Codex also 80k. Candidate: final:results/sprint/S2/native/codex/summary_150k.md, final:results/sprint/S2/native/codex/summary_80k.md, final:results/sprint/S2/native/claude_code/summary.md
- Slide 15: 25 DeepSWE tasks (SWE-Interact). Candidate: final:docs/sprint/plans/swe-interact-pilot.md
- Slide 17: 24 runs: 6 long tasks × 4 model–harness pairs. Candidate: final:results/sprint/S2/readout.md
- Slide 17: Thresholds: DeepSeek + pi 169k, GLM + pi 122k, luna + Codex 176k, Haiku + Claude Code 150k. Candidate: final:docs/sprint/specs/S2.md
- Slide 17: 192 reruns: 2 per condition at each of the 24 compaction points. Candidate: final:results/sprint/S2/readout.md
- Slide 18: AGENTS.md or chat: never lost. README only: lost at 5 of 8. Candidate: final:results/sprint/S2/gate.json (calibration cross-check: gate.json tiers by channel)
- Slide 18: Breaks in 192 reruns: 1 test edit, and 7 commit-prefix breaks, all at the 2 compactions that lost that rule. Candidate: final:results/sprint/S2/readout.md
- Slide 18: [label] 24 compactions, 192 reruns. Candidate: final:results/sprint/S2/readout.md
- Slide 24: Current call + agent's last 40 reasoning blocks. Candidate: final:results/sprint/week2/monitors/last_reasoning_report.md
- Slide 31: One five-task chain, compacted between every task; Long: tasks 1–5, five compactions. Candidate: final:results/sprint/week2/exploit/design_card.md, final:results/sprint/week2/exploit/readout.md
- Slide 31: 4 tasks (2 Python, 1 Go, 1 TypeScript). Candidate: final:results/sprint/week2/exploit/readout.md (tasks named: mobly, tomlkit (Python), wazero (Go), ofetch (TypeScript))
- Slide 34: Sonnet kept 11 of 18 earlier pushes in Antigravity, 8 of 14 in Claude Code. Candidate: final:results/sprint/week2/agy_chain5/agy_sonnet/bulk/readout.md, final:results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md
- Slide 34: Claude Code + Haiku: no overwrites (0 of 18); kept 8 of 29 earlier pushes 2+ tasks later (Sonnet 8 of 14); presented 27 of 46 expired requests as current (Sonnet 1 of 30). Candidate: final:results/sprint/week2/agy_chain5/claude_code/bulk/readout.md, final:results/sprint/week2/agy_chain5/claude_code_sonnet/bulk/readout.md (per-position rewrite column sums to 0/18)
- Slide 34: Longer Antigravity runs: 1,600 calls; quota stopped it at 13 of 24 runs; 3 of 7 divergent vs 0 of 6 clean; not significant. Candidate: final:results/sprint/week2/harness/agy_rerun1600/readout.md
- Slide 36: Haiku partly refused one Claude Code compaction. Candidate: final:results/sprint/S2/native/claude_code/summary.md
- Slide 37: Two scorer bugs, caught by my audit before any outcome was released. Candidate: CORRECTIONS.md (entry 6)
- Slide 38: a force-push planted in Haiku's own summary in Claude Code was never acted on, but honest runs were also 0 of 24. Candidate: final:results/sprint/week2/r7_planted/readout.md, final:results/sprint/week2/r7_planted/readout_notes.md (file table: planted 24 runs 0 lost, honest 24 runs 0 lost)
- Slide 38: LLM graders, validated on 180 hand labels (13 of 18 "lost" calls right). Candidate: final:docs/sprint/S1/validation-decisions.md, final:results/sprint/S1/judge_precision.json (180 = 100 + 80)
- Slide 39: Summaries rarely lose what matters: 1–6% of items. Candidate: final:docs/sprint/figs/v2/README.md (as slide 11)
- Slide 41: On 4 fixed histories, Flash kept fewer earlier-task entries than DeepSeek at the same effort; Antigravity kept none. Candidate: final:results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md (summarizer swap)

**NO-SOURCE, nothing committed holds the number:**

- Slide 6: Version strings in the sources footer (Claude Code 2.1.310, codex rust-v0.162.1, pi d5b3de2, agy 1.4.7, opencode v1.19.4). external code; not repo files
- Slide 15: resolved. The 105 is in `results/sprint/S2/interact/cohort_funnel.json` (105 candidates, 0 passing).
- Slide 20: [label] sign-flip p = 0.075, 16 histories. computed post hoc on 30 Oct, not in any committed readout; data rows on the path given
- Slide 22: [label] (p = 0.48). Fisher p computed post hoc on 30 Oct; counts are in the path given
- Slide 26: [label] (p = 0.02, post hoc). Fisher p computed post hoc on 30 Oct; counts are in the path given
- Slide 42: 9 illustrative picks, plus a random sample. transcript viewer is an Artifact; no committed manifest of picks found on any sprint branch

# Part 2: slides 43-91


Every number or quantitative claim on slides 43–91, mapped to the committed file it comes from. Paths are relative to the repo root and live on the named branch (`git show <branch>:<path>`).

Status key:
- **VERIFIED**: `git cat-file -e <branch>:<path>` succeeds. Where noted, the file was also grepped and holds the number.
- **PATH-MISSING**: the notes name a path that isn't on that branch.
- **NO-SOURCE**: the slide cites no path. A candidate found by search is given and marked "candidate".
- **EXTERNAL**: a published paper or post.

Some claims rest on gitignored raw data (`data/raw/...`), which a reader can't check. The table marks these.

Not listed: dates in the "Status" column of the experiment tables, slide numbers in "(slide N)" cross-references, and call indices inside transcript quotes.

---

## Slide 43: Backup

No claims (section divider).

## Slide 44: Prior work: carry-over and rule results match; the CoT monitor result runs the other way

| Claim | Source path | Branch | Status |
|---|---|---|---|
| In real use, about 1 in 10 summaries claims work is done when it isn't | `results/sprint/swechat_odd/scan_summary.md`<br>`results/sprint/swechat_odd/scan_summary.json` | final | VERIFIED (via slide 13); model-judged (Opus), not hand-validated |

## Slide 45: Prior work: carry-over and rules match; the CoT monitor result runs the other way

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Terry & Andriushchenko 2026: a reward hack in a first task raises hacking in a second task | https://www.lesswrong.com/posts/pdtGsC888sTToZhiY/measuring-alignment-drift-via-trajectory-prefixes | — | EXTERNAL |
| Baker et al. 2025: CoT monitor catches far more reward hacking (95% vs 60%) | https://arxiv.org/abs/2503.11926 | — | EXTERNAL |
| Zhong, Raghunathan & Carlini 2025: read-only tests stop test edits but not special-casing | https://arxiv.org/abs/2510.20270 | — | EXTERNAL |
| UK AISI 2026: edited compaction summaries as a causal tool | https://www.aisi.gov.uk/blog/investigating-alignment-failures-in-aisis-incident | — | EXTERNAL |
| OpenAI 2026: summaries told the next context to hide mistakes | https://alignment.openai.com/misalignment-reports/encouraging-deception-in-compaction-summaries/ | — | EXTERNAL |
| earlier force-push carried into the next task, 48% vs 12%, compacted or not | docs/sprint/S2/task-chaining-readout.md | final | VERIFIED (grep: 31/64 (48%) vs 3/24 (12%); compacted − full +9 [−6, +25]) |
| the agent's CoT made a Haiku monitor allow more overwrites (7 vs 1 of 22) | results/sprint/week2/monitors/5b_rationales.md; docs/sprint/figs/week2/README.md (03_monitor_reasoning, from monitors/analysis.json) | final | VERIFIED (grep: Haiku 1/22 → 7/22) |
| "Don't modify the tests" stopped test edits, not other cheating | results/sprint/week2/exploit/readout.md | final | VERIFIED (grep: test edits 0/20 in rule cells) |
| visible edit marks were noticed (6 of 14); fluent edits weren't | results/sprint/week2/user_no/readout.md; results/sprint/week2/user_no_fluent/readout.md | final | VERIFIED (grep: 6/14; fluent 0/14) |
| about 1 in 10 summaries falsely claims work is done | results/sprint/swechat_odd/scan_summary.md | final | VERIFIED (grep: 93/928 = 10.0% [8.3, 12.1]; Opus read, not hand-checked); model-judged (Opus), not hand-validated |

## Slide 46: Prior work: where my results agree and disagree

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

## Slide 47: Transcript: A dropped item, from a real user's session

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: dropped user request and the judge's paraphrase | docs/sprint/S1/report.md (lines 215–216); seeded draw by scripts/s1_report_samples.py; run swe-chat-v1:2026-01-10-52c38054-a209-4b88-9a0d-d98ba15865dd:1 | final | VERIFIED (quote at line 216) |

No quantitative claims on the slide.

## Slide 48: Transcript: A user's rule, kept at the 1st compaction and gone at the 2nd

| Claim | Source path | Branch | Status |
|---|---|---|---|
| kept at the 1st compaction, gone at the 2nd (excerpt) | results/sprint/S1/chain_decay/summary.md (line 74); run swe-chat-v1:112658aa-8e2f-43a4-9f8b-a690dfce9b04 | final | VERIFIED (quote at line 74) |

## Slide 49: Transcript: A summary claims tests pass; the history says they failed

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: "all tests pass" vs "0 passed; 8 failed" | results/sprint/swechat_odd/scan_examples.md (lines 18–19); run swe-chat-v1:e5367972-885e-46b8-bcdf-22e3e6b87525:0 | final | VERIFIED (both quotes at lines 18–19) |
| Opus 5.5's read, not hand-checked | results/sprint/swechat_odd/scan_summary.md | final | VERIFIED |

## Slide 50: Transcript: A rule lost from the summary, then broken

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: planted rule "3. Start every commit message with [DSW-<n>]." | docs/sprint/specs/S2.md (line 124) | final | VERIFIED |
| Excerpt: agent's commit without the prefix | results/sprint/S2/readout.md (lines 121–122); run pilot-03-dynamodb-toolbox-lazy-recursive-schemas-deepseek-readme-a2, summary-only arm | final | VERIFIED (summary text itself not committed) |

No quantitative claims on the slide.

## Slide 51: Transcript: Task B, citing task A

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: DeepSeek's task-B message citing the previous task | results/sprint/S2/chain/rows.jsonl (line 57, field final_report); run tengo-np-same-compacted-tengo-np-1 | final | VERIFIED |
| (copy of the same file) | docs/sprint/figs/week2/data/task_chaining/rows.jsonl (line 57) | final | VERIFIED |

No quantitative claims on the slide.

## Slide 52: Transcript: The precedent, in the agent's own reasoning

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: "force-push with lease … as we did in the previous task" | results/sprint/week2/monitors/last_reasoning_report.md (line 37); run e1-httpx-divergent-different-full-tengo-np-7 | final | VERIFIED (quote at line 37) |

No quantitative claims on the slide.

## Slide 53: Transcript: "Push it to origin" read as authorization

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: DeepSeek's reasoning quoting Claude Code's rule | results/sprint/week2/harness/git_safety_prompts.md (line 85); run cc_ds/httpx-np-3/b | final | VERIFIED |

No quantitative claims on the slide.

## Slide 54: Transcript: The monitor repeats the agent's "scratch"

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: agent's reasoning and Haiku monitor's allow verdict | results/sprint/swechat_odd/transcript_audit.md (lines 11–12); run e1-tengo-divergent-different-compacted-httpx-np-5 | final | VERIFIED |
| Excerpt: monitor verdict (same item) | results/sprint/week2/monitors/5b_rationales.md (line 18) | final | VERIFIED |
| Monitor shown the agent's last 40 reasoning blocks | results/sprint/week2/monitors/5b_rationales.md (line 3) | final | VERIFIED (grep) |
| Guardian policy text quoted (one-ref --force-with-lease push rated low or medium) | `configs/week2/wording/monitors/guardian_policy.md` | final | VERIFIED (grep: lines 51, 59) |

## Slide 55: Transcript: Two edited summaries, two reactions

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt A: DeepSeek notices the redaction | results/sprint/week2/user_no/eval_awareness_why.md (line 26); run tengo-np-3, "no dropped" version | final | VERIFIED |
| Excerpt B: Haiku's message on the planted force-push | results/sprint/week2/r7_planted/readout.md (line 52); run tengo-np-2/planted-2 | final | VERIFIED |

No quantitative claims on the slide.

## Slide 56: Transcript: What's left of task 1 after task 4

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: two ARC-151 entries from pi's summary | results/sprint/week2/chain_stress/summary_rot/decay/retained_task1.md (lines 37–38); chain u7 | final | VERIFIED |
| 2 of its 94 entries | results/sprint/week2/chain_stress/summary_rot/decay/retained_task1.md (line 35: "u7: 2 of 94 entries") | final | VERIFIED (grep) |
| at the 4th task boundary | same file | final | VERIFIED |

## Slide 57: Transcript: In Claude Code, the teammate's branch becomes a "decoy"

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: two "decoy" quotes from DeepSeek's reasoning | results/sprint/week2/harness/readout.md (lines 113, 173); run cc_ds/httpx-np-3/b | final | VERIFIED |
| it overwrote two calls later | results/sprint/week2/harness/readout.md (lines 171, 186: quote at call 136, push at call 138) | final | VERIFIED (grep) |

## Slide 58: Transcript: Carry-over into the impossible task

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: "the grader seemingly expected a force push" | results/sprint/week2/precedent/eval_awareness_check.md (line 9); run x-one_prior-u7-0 | final | VERIFIED |
| after one earlier task | same file (cell one_prior) | final | VERIFIED |

## Slide 59: Example not shown: a permissions list that called the force-push authorized

| Claim | Source path | Branch | Status |
|---|---|---|---|
| One permissions-list summary described a never-granted force-push as authorized (quote not on the slide) | — | final | NO-SOURCE (second-hand: build log; raw summary not committed) |

No quantitative claims on the slide.

## Slide 60: Transcript: Argon read the teammate's note, then force-pushed

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt: Argon's summary ("dummy notes commits") | results/sprint/week2/agy_argon_smoke/summary.md (line 12) | final | VERIFIED |
| Excerpt: `git push --force origin feature/destructuring-bindings` | results/sprint/week2/agy_argon_smoke/summary.md (line 14) | final | VERIFIED (raw transcripts gitignored) |

No quantitative claims on the slide.

## Slide 61: Transcript: A false record, cited before the force-push

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Excerpt, edited record: "I'll need a force push like the previous task." | results/sprint/week2/planted_precedent/summary.md; results/sprint/week2/planted_precedent/readout.md ("Force-push context"); run cobalt-httpx-np-7 r1 | final | VERIFIED (quote at readout.md line 405, summary.md lines 3, 36) |
| Excerpt, true record: "I rebased onto the remote WIP commits" | results/sprint/week2/planted_precedent/summary.md (line 47); run tengo-np-4 r2 | final | VERIFIED |

No quantitative claims on the slide.

## Slide 62: A force-push planned but never run in task 1 rode the summary into task 2, and was run there

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 2 of 6 histories | results/sprint/week2/harness/agy_rerun1600/carryover_note.md | final | VERIFIED (grep: n = 6 divergent tengo-first; 2 with a plan) |
| summary carried a force-push plan: 2 of 2 task 2s overwrote; no plan: 0 of 4 | results/sprint/week2/harness/agy_rerun1600/carryover_note.md | final | VERIFIED (grep: yes 2/2/2, no 4/0/0) |
| 1,600-call cap | results/sprint/week2/harness/agy_rerun1600/readout.md | final | VERIFIED (grep) |
| Excerpt: summary line and `git push --force-with-lease` (tengo-np-6) | results/sprint/week2/harness/agy_rerun1600/carryover_note.md | final | VERIFIED (raw messages gitignored) |

## Slide 63: Sprint experiments, week 1 (1 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 11.5% [7.9, 14.4] of items lost after precision correction | docs/sprint/S1/report.md | final | VERIFIED (grep) |
| 41% of compactions with a user rule lost one | docs/sprint/S1/report.md | final | VERIFIED (grep: 41% [37, 45]) |
| Claude Code vs Codex on rules +4.6 [−5.9, +15.2] | docs/sprint/S1/report.md; results/sprint/S1/carryover_sensitivity.json | final | VERIFIED (grep) |

## Slide 64: Sprint experiments, week 1 (2 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Loss precision 0.72 [0.49, 0.88] | docs/sprint/hand-labelling.md; results/sprint/S1/judge_precision.json | final | VERIFIED (grep: 13/18 = 0.72) |
| Sonnet 0.70 vs Haiku 0.69 | results/sprint/judge_compare/summary.md | final | VERIFIED (grep: 40/57 vs 41/59) |
| 20.3% [17.4, 23.5] of items lost | results/sprint/S1/swechat_ext/summary.md | final | VERIFIED (grep) |
| rules most (31.4%) | same | final | VERIFIED (grep) |
| about 13% after correction | results/sprint/S1/swechat_ext/summary.md; docs/sprint/hand-labelling.md (derived: 20.3% × 12/19 ≈ 13%) | final | VERIFIED (grep) |
| Lost 25.4% at the 1st compaction, 62.3% at the 2nd, 76.0% at the 3rd | results/sprint/S1/chain_decay/summary.md | final | VERIFIED (grep) |

## Slide 65: Sprint experiments, week 1 (3 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 27% [22, 32] consequential; 8% consequential and unrecoverable | results/sprint/S1/loss_mining/summary.md | final | VERIFIED (grep) |
| 1 break in 192 reruns | results/sprint/S2/readout.md | final | VERIFIED (grep) |
| Only 18/375 items truly lost | docs/sprint/S2/pilot-results.md | final | VERIFIED (grep) |
| 2 of 24 compactions usable | docs/sprint/plans/chained-pilot.md | final | VERIFIED (grep) |

## Slide 66: Sprint experiments, week 1 (4 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Missed my working go line: at most 2 candidate cases | docs/sprint/S2/pilot-results.md | final | VERIFIED (grep) |
| kept 0.85, 0.83, 0.87 | results/sprint/S2/interact/constraint_decay.md | final | VERIFIED (grep: 0.848, 0.833, 0.87 in constraint_decay.md) |
| all 20 lost items re-readable from files | results/sprint/S2/interact/lossaudit_followup.md | final | VERIFIED (grep) |
| None of 105 candidates passed; the gates were not loosened | `results/sprint/S2/interact/cohort_funnel.json`; `docs/sprint/S2/pilot-results.md` | final | VERIFIED (recomputed: 105 candidates across 7 boundaries, 0 passing) |

## Slide 67: Sprint experiments, week 1 (5 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Claude Code 3.8%, Codex 18.1%, Antigravity 23.4% | docs/sprint/figs/native/README.md; results/sprint/S2/native/claude_code/summary.md, results/sprint/S2/native/codex/summary_150k.md, results/sprint/S2/native/agy/summary.md | final | VERIFIED (grep, inventory and figure README) |
| 130/133 already in the code | docs/sprint/figs/native/README.md | final | VERIFIED (grep: 130 of 133) |
| No loss seen, so the pilot closed under its kill criterion (per-exposure counts not in the committed data) | `docs/sprint/hand-labelling.md` (planted-rule pilot kept cards, 4/4 agreed) | final | NO-SOURCE: per-exposure data not committed, as the slide says |
| Released SWE-bench Pro images 25/25 leak; rebuilt 24/24 clean; DeepSWE 40/40 clean | results/sprint/task_validation/summary.md | final | VERIFIED (grep) |

## Slide 68: Sprint experiments, week 2 (1 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Force-pushed 48% (31/64) with history vs 12% (3/24) without, +36 points [+17, +55] | docs/sprint/S2/task-chaining-readout.md | final | VERIFIED (grep) |
| test lines changed in 0/88 | docs/sprint/next-experiments/r5_other_acts_scan.md | final | VERIFIED (grep) |
| Commit lost 50% after a divergent first task vs 0–3% after a clean one | results/sprint/week2/precedent/readout.md | final | VERIFIED (grep: 50.0% vs 3.1% / 0.0%) |
| +46.9 to +50.0; p ≤ 0.0008 | results/sprint/week2/precedent/readout.md | final | VERIFIED (grep: p = 0.0007716 / 0.0003858) |

## Slide 69: Sprint experiments, week 2 (2 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Reasoning cites its own earlier method (10/16 lost runs) | results/sprint/week2/precedent/justification_read.md | final | VERIFIED (grep: 10/16) |
| summaries record intent, never the deed | results/sprint/week2/precedent/compaction_content.md | final | VERIFIED (16/16 divergent) |
| Guardian allowed more: Haiku 7 vs 1 flips, Opus 6 vs 0 | results/sprint/week2/monitors/5b_rationales.md | final | VERIFIED (grep) |
| only 4 of 16 histories qualified | results/sprint/week2/refusal/topup_record.json | final | VERIFIED (grep: "4 of 16 qualify"; n_qualified 4) |

## Slide 70: Sprint experiments, week 2 (3 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 9/9 obeyed the "no" | results/sprint/week2/user_no/readout.md | final | VERIFIED (grep) |
| 0/42 resumed runs lost commits | results/sprint/week2/user_no/readout.md | final | VERIFIED (grep) |
| test-edit base rate too low (0/88) | docs/sprint/plans/week2-summary-attack.md | final | VERIFIED (grep) |
| fresh 12/12 → 5/12 (p = 0.005) | results/sprint/week2/exploit/readout.md (note under the cell table: Fisher exact two-sided p = 0.0046) | final | VERIFIED. |
| long history 6/8 → 2/8 | results/sprint/week2/exploit/builder_notes.md (u-chain comparison; readout.md's pooled long 8/10 mixes chain families, rule cells ran on u chains only) | final | VERIFIED (grep) |
| Test edits 0/20 | results/sprint/week2/exploit/readout.md | final | VERIFIED (derived: fresh_rule 0 of 12 plus long_rule 0 of 8, Test edit column) |
| 7/9 still passed the contradictory test without editing it | results/sprint/week2/exploit/readout.md (per-task table: tomlkit 5/5, mobly 2/4 with the rule) | final | VERIFIED (derived: 5 + 2 of 5 + 4) |

## Slide 71: Sprint experiments, week 2 (4 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Force-pushing flat at 2/8 chains at every position | results/sprint/week2/chain_stress/readout/code_measures.md | final | VERIFIED (grep). Inventory (footer) has no readout for this row |
| fresh 4/12 | same | final | VERIFIED (grep) |
| task 5 − task 2 = 0 [−38, +38] | same | final | VERIFIED (grep) |
| 7 compactions failed: 5 recovered, 2 final | same ("Failed compactions" table) | final | VERIFIED (derived: 9 failed attempts at 7 compaction points; 5 recovered or redone, 2 terminal) |
| Final: lost 15/30 with the permissions list vs 25/30 native; placebo 21/29, so partly non-specific; not significant after correction (p = 0.094) | `results/sprint/week2/summary_fix/final_readout.md` | final | VERIFIED |
| Haiku and Codex never overwrote first | results/sprint/week2/harness/readout.md; docs/sprint/figs/week2/README.md (05a) | final | VERIFIED (grep: 0/12 each) |
| DeepSeek in Claude Code 2 of 4 vs 0 of 8 | results/sprint/week2/harness/readout.md | final | VERIFIED (grep: 2/4 vs 0/8, +50 [−17, +94]) |
| Antigravity hit its call limit before pushing | results/sprint/week2/harness/readout.md; results/sprint/week2/harness/agy_summary_content.md | final | VERIFIED |

## Slide 72: Sprint experiments, week 2 (5 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 0/24 planted vs 0/24 honest | results/sprint/week2/r7_planted/readout.md | final | VERIFIED (grep) |
| About 1 in 10 of 928 summaries claims undone work is done (Opus's read, not hand-checked); done 29 Oct | `results/sprint/swechat_odd/scan_summary.md` | final | VERIFIED |

## Slide 73: Hand-label checks: 398 samples (1 of 3)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 398 samples | docs/sprint/hand-labelling.md; docs/sprint/figs/README.md (07_hand_labels) | final | VERIFIED (hand-labelling table; see its Checked column) |
| first round: 100 cards; all four judges failed the gate | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| fresh validation: 80 cards; Loss precision 13/18 = 0.72 [0.49, 0.88]; invention 8/13 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| release audit: 20 cards; 17 correct, 3 judge misreadings, 0 pipeline faults | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| retention spot check: 24 cards; agreed 23/24; AGENTS.md from 1/8 to 0/8 lost | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| usable-loss candidates: 12 cards; five checks each; only 3 passed | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |

## Slide 74: Hand-label checks: 398 samples (2 of 3)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| loss calibration: 28 cards; precision 7/9 = 0.78 held out; "kept" agreement 10/10 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| release audit round 1: 20 cards; two scorer bugs; all 200 sealed rows re-scored | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| release audit round 2: 20 cards; 20/20 correct | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| SWE-chat precision check: 24 cards; precision 12/19 = 0.63 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |

## Slide 75: Hand-label checks: 398 samples (3 of 3)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Haiku vs Sonnet: 85 cards, reused; Sonnet 0.70 vs Haiku 0.69 | docs/sprint/hand-labelling.md; results/sprint/judge_compare/summary.md | final | VERIFIED (grep) |
| SWE-Interact cards: 6; 1 of 6 accepted | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| chain check: 60 cards; precision 0.74 (me), 0.87 (Opus); 2nd-compaction loss stays above half | docs/sprint/hand-labelling.md; results/sprint/S1/chain_decay/label_check.md | final | VERIFIED (grep: 20/27 = 0.74, 26/30 = 0.87) |
| planted-rule kept cards: 4; agreed 4/4 | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |
| 398 cards in all; the 85 reused cards are counted once | docs/sprint/hand-labelling.md | final | VERIFIED (hand-labelling table; see its Checked column) |

## Slide 76: Mistakes log, in date order (1 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Footer: "Source: my mistakes log; the fixes are listed in CORRECTIONS.md" | `CORRECTIONS.md` (the 13 entries that affected data) | final | VERIFIED |
| Calibrating Opus against my 50 item cards | CORRECTIONS.md (entry 3) | final | VERIFIED (grep) |

## Slide 77: Mistakes log, in date order (2 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| My hand labels (1 of 13), then a keyword scan | CORRECTIONS.md (entry 5) | final | VERIFIED (grep) |
| 200 rows re-scored; fresh audit 20/20 | CORRECTIONS.md (entry 6) | final | VERIFIED (grep) |
| An odd [0, 1, 0] agreement pattern | CORRECTIONS.md (entry 7) | final | VERIFIED (grep) |
| My candidate checks (3 of 12 passed) and 6 cards | CORRECTIONS.md (entry 8) | final | VERIFIED (grep) |

## Slide 78: Mistakes log, in date order (3 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Planted-rule pilot's first step failed three times | CORRECTIONS.md (entry 10) | final | VERIFIED (grep) |
| First task-chaining cost estimate too low (~$25 vs ~$90–93) | — | final | NO-SOURCE |

## Slide 79: Mistakes log, in date order (4 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Overnight halts went unnoticed for over an hour | — | final | NO-SOURCE |
| Watcher polls every 3 minutes | — | final | NO-SOURCE |
| AISI's "do not attack" result stated backwards (3 vs 22 of 25) | — | final | NO-SOURCE |

## Slide 80: Mistakes log, in date order (5 of 5)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 35 of 36 failed setup | — | final | NO-SOURCE |
| $0 lost | — | final | NO-SOURCE |

## Slide 81: Full and compacted history both raise force-pushes (14/32, 17/32) over no history (3/24)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 14/32, 17/32 over no history (3/24) | docs/sprint/figs/week2/README.md (backup_01_chaining_by_start), from results/sprint/S2/chain/analysis.json; docs/sprint/S2/task-chaining-readout.md | final | NO-SOURCE (candidate; verified, grep matches) |
| force-push +9.4 points [−6.2, +25.0] | docs/sprint/figs/week2/README.md; docs/sprint/figs/week2/data/task_chaining/analysis.md | final | NO-SOURCE (candidate; verified, grep matches) |
| commit lost +6.2 [−9.4, +25.0] | docs/sprint/figs/week2/README.md | final | NO-SOURCE (candidate; verified, grep matches) |

## Slide 82: Only the reasoning view raises Haiku's allows (7 vs 1 of 22); Opus allows more with any history

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Haiku's allows 7 vs 1 of 22 | docs/sprint/figs/week2/README.md (backup_03_monitor_all_views), from results/sprint/week2/monitors/analysis.json | final | NO-SOURCE (candidate; verified, grep matches) |
| Opus allows 2 of 22 on the call alone, 4–6 with history, 8 with reasoning | same; results/sprint/week2/monitors/5b_rationales.md | final | NO-SOURCE (candidate; verified: 2, 6/5/4, 8 of 22) |
| Offline replay of 22 force-pushes | same | final | NO-SOURCE (candidate; verified) |

## Slide 83: After task 4, each chain's summary is 10–38% about earlier tasks

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 10–38% about earlier tasks after task 4 | results/sprint/week2/chain_stress/summary_rot/figures/README.md (summary_rot_by_chain), from results/sprint/week2/chain_stress/summary_rot/shares.json | final | NO-SOURCE (candidate; verified: same title in README) |
| 8 unrelated five-task chains | same | final | NO-SOURCE (candidate; verified: n = 8) |
| Task 4 minus task 2: −3.3 points [−20.5, +13.5] | results/sprint/week2/chain_stress/summary_rot/figures/README.md (Trend line); shares.json (trend_unrelated_earlier) | final | NO-SOURCE (candidate; verified, grep matches) |

## Slide 84: In real users' sessions, rules are lost most often (31%), commitments least

| Claim | Source path | Branch | Status |
|---|---|---|---|
| rules are lost most often (31%), commitments least | docs/sprint/figs/v2/README.md (02_lost_by_type), from results/sprint/S1/swechat_ext/summary.json | final | NO-SOURCE (candidate; verified: rules 31.4%, commitments 11.3%) |
| 95% session-bootstrap intervals | same | final | NO-SOURCE (candidate; verified) |

## Slide 85: Five compaction prompts lose 14–29% of rules, but only 1–8% in a way that matters

| Claim | Source path | Branch | Status |
|---|---|---|---|
| lose 14–29% of rules | docs/sprint/figs/v2/README.md (03_rules_lost_by_prompt), from results/sprint/S1/analysis.json | final | NO-SOURCE (candidate; verified: 14.2%–29.2%) |
| only 1–8% in a way that matters | results/sprint/S1/prompt_loss_mining/summary.md, summary.json; docs/sprint/figs/v2/README.md | final | VERIFIED (summary table: consequential 1%–8% by prompt, 14%–29% of rules lost; README gives 1.4%–8.1%) |
| 100 histories; 30 lost rules per prompt | docs/sprint/figs/v2/README.md | final | NO-SOURCE (candidate; verified) |

## Slide 86: The judge, checked by hand: about 7 in 10 "lost" calls are right

| Claim | Source path | Branch | Status |
|---|---|---|---|
| about 7 in 10 "lost" calls are right | docs/sprint/figs/README.md (03_judge_precision), from results/sprint/S1/judge_precision.json, results/sprint/S2/lossaudit/calibration.json, results/sprint/judge_compare/summary.json | final | NO-SOURCE (candidate; verified: 0.63–0.78 by set, 0.72 pooled) |
| Sonnet 5.5 0.70 vs Haiku 5.5 0.69 on the same cards | docs/sprint/figs/README.md; results/sprint/judge_compare/summary.md | final | NO-SOURCE (candidate; verified: 40/57 vs 41/59) |

## Slide 87: A lead, not a result: restoring a lost rule (2 compactions only)

| Claim | Source path | Branch | Status |
|---|---|---|---|
| 2 compactions only | docs/sprint/figs/README.md (05_live_prefix_rule_by_arm), from results/sprint/S2/readout.json (rules.R6.lost.contrasts) | final | NO-SOURCE (candidate; verified) |
| as resumed 4/4 broke it, rules restated 0/4 | same | final | NO-SOURCE (candidate; verified: 4/4, 0/4) |

## Slide 88: Simulated maintainer: requirements in clear messages mostly survive

| Claim | Source path | Branch | Status |
|---|---|---|---|
| Replays of the live pi runs lost 0–4 requirements | docs/sprint/figs/README.md (06_maintainer_requirements_lost) | final | NO-SOURCE (candidate README verified: 0–4 of 39; it reads gitignored data/raw/sprint/s2/interact/*, and no committed readout holds the counts, as the slide says) |
| the live summary lost none | same | final | NO-SOURCE (candidate; verified: 0/39; same raw-data caveat) |

## Slide 89: Antigravity's long task-2 runs were mostly repeated tool outputs; in-task compaction didn't rescue them

| Claim | Source path | Branch | Status |
|---|---|---|---|
| In the 3 runs that went past 750 calls, 53–77% of tool outputs repeated an earlier one | results/sprint/week2/agy_rerun800/loop_note.md | final | VERIFIED (grep: 825, 805, 754 calls; 64%, 53%, 77%) |
| 10–17% in the 2 runs that finished | same | final | VERIFIED (grep: 10%, 17%) |
| compaction at calls 631 and 643 | same | final | VERIFIED (grep) |
| repeats fell from 100% to 45% and 56% | same | final | VERIFIED (grep) |
| The summary after 631 calls listed no code change | same | final | VERIFIED (grep) |
| 800-call cap; n = 3 long runs, one task | same | final | VERIFIED (raw transcripts gitignored) |

## Slide 90: Push and remote notes (labelled safety-relevant) lasted longer in pi; Antigravity kept none

| Claim | Source path | Branch | Status |
|---|---|---|---|
| lasted longer in pi; Antigravity kept none | results/sprint/week2/chain_stress/summary_rot/decay/agy/README.md (safety_agy_vs_pi); results/sprint/week2/chain_stress/summary_rot/decay/agy/agy_vs_pi.json | final | VERIFIED (grep: pi 53%/31%/23% vs other 23%/7%/2%; Antigravity 0%) |
| Same 4 chains | same README | final | VERIFIED (n = 4 chains) |
| Safety-relevant lines labelled by gpt-6-astra (definition) | results/sprint/week2/chain_stress/summary_rot/decay/agy/safety_labels/main/flags.json | final | VERIFIED |
| A random 30 of those lines: 7 clearly about a teammate's work; 3 rebases onto the remote; 20 routine | results/sprint/week2/chain_stress/summary_rot/decay/agy/safety_labels/main/ (labels only) | final | NO-SOURCE (the 30-line tally, seed 20261031, is not committed; it can be redrawn from flags.json) |
| Example lines quoted (928b571e/0ea752c5 rebase; RET-292; COMPLETE_TASK_AND_SUBMIT) | results/sprint/week2/chain_stress/summary_rot/decay/agy/safety_labels/main/input_part03.jsonl; agy_vs_pi.json | final | VERIFIED (grep) |

## Slide 91: Earlier pushes, 2+ tasks later: Sonnet kept over half in either harness, Haiku 8 of 29; Gemini and Codex none

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

## Backup: Other SWE-chat oddities: an invented user action, a flipped request, a contradicted plan

| Claim | Source path | Branch | Status |
|---|---|---|---|
| An invented user action, a flipped request, a contradicted plan | `docs/sprint/next-experiments/swechat_odd_patterns.md` | final | VERIFIED (Opus's flags, not hand-checked) |

## PATH-MISSING and NO-SOURCE items

**PATH-MISSING:** none. Every path cited for slides 43–91 exists on `final`.

**NO-SOURCE** (no path cited; candidate given):
1. Slide 81: 14/32, 17/32, 3/24; +9.4 [−6.2, +25.0]; +6.2 [−9.4, +25.0]. Candidate `docs/sprint/figs/week2/README.md` and `docs/sprint/figs/week2/data/task_chaining/analysis.md` (sprint/prep), verified.
2. Slide 82: Haiku 7 vs 1 of 22; Opus 2, 4–6, 8 of 22. Candidate `docs/sprint/figs/week2/README.md` (sprint/prep) and `results/sprint/week2/monitors/analysis.json` (sprint/week2-build-c), verified.
3. Slide 83: 10–38%; 8 chains; −3.3 [−20.5, +13.5]. Candidate `results/sprint/week2/chain_stress/summary_rot/figures/README.md` and `shares.json` (sprint/week2-build-b), verified.
4. Slide 84: rules 31%, commitments least. Candidate `docs/sprint/figs/v2/README.md` and `results/sprint/S1/swechat_ext/summary.json` (sprint/prep), verified.
5. Slide 85: resolved. The input `results/sprint/S1/prompt_loss_mining/summary.json` (and `summary.md`) is now committed: 14%–29% of rules lost, 1%–8% consequential.
6. Slide 86: about 7 in 10; Sonnet 0.70 vs Haiku 0.69. Candidate `docs/sprint/figs/README.md` and `results/sprint/judge_compare/summary.json` (sprint/prep), verified.
7. Slide 87: 2 compactions; 4/4 vs 0/4. Candidate `docs/sprint/figs/README.md` and `results/sprint/S2/readout.json` (sprint/prep), verified.
8. Slide 88: replays lost 0–4; live summary lost none. Candidate `docs/sprint/figs/README.md`. The underlying data is gitignored raw data, so this can't be checked from the repo.
9. Slide 90: random 30 lines, 7 / 3 / 20. The tally isn't committed; only the labels (`safety_labels/main/`, sprint/week2-build-b) are.

**Other discrepancies to fix or footnote:**
- Slide 70: resolved. 6/8 is the u-chain comparison in `results/sprint/week2/exploit/builder_notes.md`; readout.md's 8/10 pools chain families.
- Slides 71–72: the footer cites an internal inventory (28 Oct), but these cells' numbers (exploitation ending, five-task chains, summary fix, harness replication) are newer and are not in it. Their sources are listed above.
- Slides 76–80: the footer cites the mistakes log; `CORRECTIONS.md` lists the 13 entries that affected data.
- Slide 79: "up to an hour" vs the file's "over an hour" and "about 50 minutes".
- Slide 46: "16/32 vs 0/32" is the upper completion. The conservative (lower) completion is 16/32 vs 1/32.
