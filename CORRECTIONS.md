# Corrections and data issues

Problems found in our data, scoring or task setup during the sprint (22–30 Oct 2026): what was wrong, how it was found, and its effect on results. Each was fixed, and the affected numbers re-scored, before those numbers were reported. Where a number changed, the old and new values are both given.

## Effect on reported results, in brief

| # | Issue | Effect on reported numbers |
|---|---|---|
| 1 | Task prompt missing from PostTrainBench histories | Fixed before any Experiment 1 number; restated tasks were being counted as "invented" |
| 2 | Re-attached rule files counted as lost | Fixed before any number; a "reloaded" class no longer counts as loss |
| 3 | Labelling page biased the "is this an item?" step | Fixed before judge precision was computed |
| 4 | One duplicated claim decided a judging rule | Both copies set to "can't tell"; rule re-derived |
| 5 | Harness instructions looked "invented" | Invention numbers only (already exploratory); loss unaffected |
| 6 | Two scorer bugs in planted-rule outcomes | All 200 sealed rows re-scored before release; new 20-card audit 20/20 |
| 7 | Codex summaries graded on fragments | All boundaries regraded |
| 8 | Automatic "not recoverable" and "lost" calls too generous | Every loss now second-checked; search narrowed |
| 9 | Reference solutions reachable in task images | Images rebuilt and validated before any agent run used them |
| 10 | Planted-rule scorer parsed shell syntax and missed real outputs | Scorer rewritten to match outputs; regression tests from real transcripts |
| 11 | A test-revealing word in the git remote's path | Phase restarted from scratch with neutral paths; stopped runs excluded |
| 12 | Randomization test blocks didn't match the assignment | p = 0.000772 / 0.000386 (was 0.00089 / 0.00016); conclusion unchanged |
| 13 | Edited summaries showed the edit; instrumentation readable | Experiment 3c's awareness numbers reported as mostly an artefact; fluent rerun added |

## Experiment 1: offline audit of real compactions

### 1. PostTrainBench histories were missing the task prompt
- **What was wrong:** PostTrainBench passes the task as a command-line argument, so the published traces start at the agent's first turn. The pipeline still marked them complete, so every summary claim that restated the task looked "invented".
- **How found:** every judge failed the first held-out gate against hand labels. All 3 of one judge's held-out false "invented" calls were on these records.
- **Fix and effect:** each run's task prompt is rebuilt with PostTrainBench's own prompt code, at the version that run used, and inserted as the first user message. Fixed before any reported number.

### 2. Rules the harness re-attaches were counted as lost
- **What was wrong:** Claude Code re-attaches CLAUDE.md, AGENTS.md and the memory file after every compaction, so a summary that omits them loses nothing. The pipeline scored them as lost or kept depending on what the captured context happened to include.
- **How found:** the same hand-label gate failure as entry 1.
- **Fix and effect:** a new class, "reloaded", that doesn't count toward loss.

### 3. The labelling page biased the "is this an item?" question
- **What was wrong:** the page asked "did the summary keep this item?" with no separate first step, so trivial lines were marked "not an item" mostly when the summary had dropped them. That deflated the judge's "dropped" precision.
- **How found:** calibrating a second judge against 50 hand-labelled item cards (9 of its 12 "not an item" calls were on items labelled kept).
- **Fix and effect:** two-step cards; "is this an item?" is answered with the summary hidden.

### 4. One duplicated claim decided a judging rule
- **What was wrong:** two judges extracted the same claim with different punctuation; the two copies were labelled differently, and that one pair decided a second-look rule.
- **How found:** a review of the labels.
- **Fix and effect:** both copies became "can't tell"; the page now merges claims by normalised text.

### 5. The harness's own instructions looked "invented"
- **What was wrong:** Claude Code's system prompt carries instructions the user never typed, and the saved histories don't contain it, so a summary that repeats one looks invented.
- **How found:** hand labels (1 of 13 fresh "invented" cards), confirmed by a keyword scan (1 of 45 remaining).
- **Effect:** invention numbers only, which are reported as exploratory. Loss numbers are unaffected.

## Experiment 2: live pilot

### 6. Two scorer bugs in the planted-rule outcomes
- **What was wrong:** the commit-prefix check counted `git stash` entries as commits; the README check counted copies of the README in build folders as edits.
- **How found:** a 20-card hand audit before any outcome was released.
- **Fix and effect:** both fixed; all 200 sealed rows re-scored; a fresh 20-card audit passed 20/20.

### 7. The Codex arm was graded on fragments of its summaries
- **What was wrong:** the grader took the longest fenced block of each recited summary, but summaries contain their own code fences, so 8 of 14 boundaries were graded on fragments.
- **How found:** an odd agreement pattern among the recitations.
- **Fix and effect:** the outer fence is parsed structurally; every boundary was regraded.

### 8. The automatic "not recoverable" and "lost" calls were too generous
- **What was wrong:** two "not recoverable" losses were already in the working tree, and 3 of 6 SWE-Interact "losses" were kept almost word for word.
- **How found:** hand checks of candidates (3 of 12 passed) and of the 6 SWE-Interact cards (1 accepted).
- **Fix and effect:** a second, stronger judge checks every loss; the search is narrowed to rules, permissions and decisions not yet acted on.

## Task images

### 9. Reference solutions were reachable in the task images' git history
- **What was wrong:** the upstream SWE-bench Pro images are full clones with the reference fix later in history, and the official verifier checks out tests from the solution commit. For SWE Atlas Refactoring, a quick check found no leak, but full validation found solution text in the image layers of all 25 originals and 3 reachable from inside the container.
- **How found:** a feasibility check before integration, then full validation of every image (deterministic searches of every git object, ref, layer and file for solution lines, plus an agent told to find the solution by any means).
- **Fix and effect:** images rebuilt with squashed history; the verifier applies the test patch instead; each task checked to score 0 unchanged and 1 with the reference. DeepSWE 40/40, rebuilt Pro 24/24 and rebuilt Refactoring 15/15 clean. No agent run used a leaky image.

## Planted-rule pilot

### 10. The verification-rule scorer missed how agents actually ran the command
- **What was wrong:** the scorer parsed shell syntax for one expected way of running the command. Agents filtered output, chained commands, read output back from files, or ran unrelated scripts afterwards. In the screen, hand reading found 5 of 5 reports correct where the scorer credited 1. A literal-wording fix then credited a check pasted after later edits, and showed the harness wasn't recording file changes made through shell commands.
- **How found:** the pilot's Step 0 gate, and reading transcripts.
- **Fix and effect:** scoring matches outputs, not syntax; regression tests come from real transcripts; the harness snapshots the repo after every tool call. Fixed before any reported number. Cost: 12 of 94 forks and about 4 hours.

## Task chaining (week 2)

### 11. The git remote's path contained a word that revealed the setup
- **What was wrong:** each agent's remote was at a path containing "traps", printed on every `git push`, and so present in every first-task history that later runs inherit. In one smoke run an agent also read the harness's scenario file and cited it.
- **How found:** the smoke-gate report noted the visible path.
- **Fix and effect:** all agent-visible paths renamed (`/srv/git/<project>.git`), the scenario file deleted after use, an audit of everything the agent can reach for such words, and a stub check that fails on them. The first phase was restarted from scratch with the same seeds; the 2 finished and 14 stopped runs are excluded from analysis.

### 12. Experiment 1's randomization test blocks didn't match the assignment
- **What was wrong:** the plan said the test permutes within each first task's 8 + 8 block; the assignment script randomized within first task × launch round.
- **How found:** an independent results review of the readout, before anything was shown.
- **Fix and effect:** the test now reads blocks from the assignment file, exact over 1,679,616 assignments: p = 0.000772 / 0.000386, previously 0.00089 / 0.00016. Conclusion unchanged.

### 13. Experiment 3c's edited summaries showed the edit; the instrumentation was readable
- **What was wrong:** the "no dropped" version deleted the user's "no" from the summary and left visible gaps ("commit there, and .", "2. ;", "— ****"). In 6 of the 9 runs that remarked on the setup, those gaps were what they noticed. Separately, the remote's push-logging hook and the command wrappers were readable; 2 runs read the hook.
- **How found:** a read of why the agent described the setup as a test.
- **Effect:** 3c's awareness numbers are reported as mostly an artefact of the edit. The deleted "no" leaked through no tool output (0/42). A fluent rerun (the sentence rewritten, not cut) found 0/14 runs suspecting an edit, against 6/14; grading words were unchanged at 5/14 (`results/sprint/week2/user_no_fluent/readout.md`).

## Superseded statements

Earlier statements that some files in this repository still contain. Where a file and this list disagree, this list is current.

| Earlier statement | Current | Where the earlier version may appear |
|---|---|---|
| Real users' summaries overclaimed completion in "14 of 21" cases | 10 explicit and 3 hedged overclaims, of 19 cases, by hand label | `docs/sprint/next-experiments/swechat_odd_patterns.md` (gives the 14 as the clustering's count, not a result) |
| Safety-relevant entries in pi's summaries decay to about 31% two tasks later | The 31% share stands as a count, but the safety-relevant class is mostly push and remote notes, so it isn't a general safety-retention rate | `results/sprint/week2/chain_stress/summary_rot/decay/agy/README.md`, `.../decay/harness/README.md` and their figures |
| The harness drives whether old instructions read as current | Retracted: Claude Code with Haiku showed the effect in 27/46 | not found verbatim in shipped files |
| Both model and harness matter for the precedent effect | Superseded by the DeepSeek replay control (`results/sprint/week2/chain_stress/summary_rot/decay/swap/README.md`, DeepSeek replays at max and low effort) | not found verbatim in shipped files |
| Antigravity (agy) stores no reasoning text for Gemini 4 Argon | agy stores reasoning when the model returns it; Gemini at low effort returned none | `results/sprint/week2/agy_argon_smoke/cot_check.md` |
