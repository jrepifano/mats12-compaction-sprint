---
name: finish-experiment
description: Close out an experiment's worktree in this repo — write and review its report, update the run log, tracker and Claude Doc mirrors, merge main into the branch, open a PR, merge it to main, back up gitignored data, and remove the worktree. Use when Jacob says an experiment is done or paused and should be written up and merged ("write the report, open a PR, merge, clean up"). Argument: the experiment id (e.g. E9).
---

# Finish an experiment: report → PR → merge → clean up

The flow CLAUDE.md's "Finishing" bullet describes, made explicit, with the gotchas found doing it for E9 (2026-10-07). Work through the steps in order. **Stop and ask Jacob** only where a step says so; everything else is routine. Never skip a check: say in the final message which ones failed and why.

`$EXP` is the experiment id (`E9`), `$W` its worktree (`.claude/worktrees/<exp>-<purpose>`), `$M` the main checkout (`/Users/jacob/Documents/GitHub/mats12_neel_exploratory`).

## 0. Preconditions (refuse to continue if any fails)

- **No paid work is running.** No pods (`uv run python -m emx.runpod_reservations state`: zero unlaunched reservations and zero launched liability for $EXP; the free RunPod `account()` query lists no $EXP pods); no open API reservations (`uv run python -m emx.spend --exp $EXP --report`: `open_reservations` 0). No background job, agent or review is still writing into `$W` (`pgrep -f "$W"`, excluding your own shell).
- **The ledger is committed** in `$W` (`git status logs/costs.jsonl` clean).
- **Every subagent working in `$W` has finished.**

## 1. The report (`docs/experiments/$EXP/report.md`)

- Follow `docs/experiments/REPORT_TEMPLATE.md`: the one-screen review summary first (status, finding, gates against the prereg, what it changes, spend, reviews, open items), then the finding, deviations in a table, plain language, no internal codes, inline citations linking to level-4 reference headings.
- **Every number traces to a committed artifact or a named file:** recompute tables from the source files and state the source path. Spend comes from the worktree's `logs/costs.jsonl`.
- **Random raw samples must be reproducible and complete:** draw them with a small committed script (`scripts/<exp>_report_samples.py`: population, order, strata, seed), and show **every drawn row** with its id. Never pick a subset of the draw.
- **Separate observations, forecasts and hypotheses.** A stop that wasn't triggered is a forecast; a reading of someone else's result is "X report…"; a single control doesn't establish a mechanism.
- Cite only sources read in `papers/`; check row numbers against `papers/*/INDEX.md` (grep with filenames: several INDEX files reuse numbers). A new external source is captured into `papers/<topic>/` first: PDF (gitignored), a `pdftotext -layout` text extraction (committed), and an `INDEX.md` row.
- Include a table of any code-review findings left open at a round cap, with a recommendation each.
- **Review:** one gpt-6-astra round via `bash scripts/codex_review.sh <prompt> logs/reviews/<YYYYMMDD>-$EXP-report-r1.md medium 1800` (up to 3 rounds per CLAUDE.md). Ask it to recompute the tables from the data. Fix every BLOCKING finding; record the verdict in the report and the run log. Commit the prompt and the review `.md` (the `.md.log` transcript is gitignored).

## 2. Run log, tracker, Claude Docs

- **Run log `logs/$EXP-report.md`:** append the final events with UTC times: what finished, final spend by stage and resource, the state of reservations and pods, each branch's disposition (completed / dropped / stopped / not reached), the report and its review verdict, where gitignored data lives.
- **Tracker** (rules in `docs/tracker-instructions.md`): update only Jacob's row for $EXP (status, now/next, spend / cap, updated date), guarded with the revision just read. Then write the same row into `docs/tracker.md` in the branch (it reaches main with the PR).
- **Claude Doc mirrors:** for each mirrored file the branch changed (CLAUDE.md's table), `read` the doc with `sinceRev` = its last synced rev and fold any changes into the markdown. Then push the repo's changes to the doc, and update the table's "synced at rev N". Add a row for any mirrored doc the table lacks.
- Commit on the branch.

## 3. Merge main into the branch

- `git fetch origin && git merge --no-edit origin/main` in `$W`.
- **Conflicts in shared files that were synced selectively** from another branch (`git checkout <sha> -- files`, as E9 did from `e3/run@7b15be1`): git's merge base predates the copy, so both sides look like they added the same code. Re-merge each such file three-way against the **true base**, the commit the files were copied from: `git show <base>:$f > base; git show HEAD:$f > ours; git show origin/main:$f > theirs; git merge-file -L ours -L base -L main ours base theirs`. Exit 0 means clean. Apply, `git add`, and handle only real conflicts by hand.
- **Index files** (`papers/*/INDEX.md`): keep both sides' rows and renumber only the new ones.
- **Ledger:** `.gitattributes` unions it. Then run `uv run python scripts/merge_ledger.py check --fix logs/costs.jsonl` and `check logs/costs.jsonl` **with the worktree's path**: without a path the script checks the main checkout's ledger.
- **Full suite** in `$W`: `uv run --group swarm pytest -q 2>&1 | grep -E '^(FAILED|ERROR)|passed|failed'`. Don't keep only the last few lines (`tail` cut two failure names once). Classify every failure:
  - **Old tests using this experiment's id as "an unknown experiment"** (common: `"E9"`, `"e9"`, `--only e9`) break once the experiment exists. Change their example to an id that stays unknown (`E99`).
  - **Possibly pre-existing:** check in a throwaway clean checkout of `origin/main` (`git worktree add --detach <scratch> origin/main`), run the one test with `PYTHONPATH=<scratch>/src` so it imports that checkout's code, then remove the scratch worktree. A failure there too is pre-existing: note it in the PR body and don't fix it.
  - **Real regressions:** fix them before going on.
- Commit the merge, with a message that says how conflicts were resolved and lists any pre-existing failures.

## 4. PR and merge

- `gh repo view --json visibility` must say PRIVATE (CLAUDE.md, visibility).
- `git push -u origin <branch>`; `gh pr create --base main --head <branch>` with a body covering: status and spend, what the branch contains, any additive shared changes, checks (suite result and pre-existing failures, ledger check), what stays outside git. End the body with the session's attribution line.
- `gh pr view N --json mergeable,mergeStateStatus` must show MERGEABLE / CLEAN; then `gh pr merge N --merge` (a merge commit, as earlier PRs).

## 5. Update main, back up gitignored data

- In `$M`: `git fetch origin`, confirm `git log origin/main..main` is empty (nothing unpushed), then `git merge --ff-only origin/main`. Don't touch other sessions' untracked files.
- **Gitignored data lives only in the worktree** (pools, judge scores, caches, audit listings). After the merge, so main's `.gitignore` already covers them, copy them: `rsync -a --exclude '*.lock' $W/data/$EXP/ $M/data/$EXP/` (same for `results/$EXP/` ignored subtrees). Verify with sha256 on a few large files, and check that `git status` in `$M` shows nothing new.
- `uv run python scripts/merge_ledger.py report` in `$M`: the worktree must not appear under "Not yet on main".

## 6. Clean up the worktree

- In `$W`: `git status --short` (untracked) and `git status --short --ignored | grep '^!!'` (ignored). For each path, decide: a copy of something in main (`.env`, `.venv`, `data/E1/*`, caches), already backed up, or **unique**.
  - **Unique files** are typically run logs (`logs/<exp>/`) and codex review transcripts (`logs/reviews/*$EXP*.md.log`). Copy them to `$M` (`rsync -a --ignore-existing`). Commit small, non-ignored ones on main.
  - **If anything unique can't be classified, stop and ask Jacob** before deleting.
- Confirm nothing is running from `$W`, then `git worktree remove --force $W && git worktree prune`. Keep the branch (local and remote) unless Jacob asks to delete it.

## 7. Wrap up

- Update or add the project memory for $EXP: state, where its data now lives, what blocks resuming. Keep the `MEMORY.md` index line in step.
- Final message to Jacob: PR link and merge commit, spend, the report path, what was backed up where, any pre-existing test failures, open review findings, and anything skipped.

## Gotchas seen in practice

- `merge_ledger.py check` defaults to the **main** checkout's ledger; pass the worktree's path.
- Tests that drive the real orchestrator or watchdog can write real state files (E9: a test wrote `results/$EXP/STOPPED.json`). Before merging, list the new files under `results/` and `logs/` after a suite run and delete any written by tests.
- `git commit -a` in a worktree where agents are mid-edit commits their partial files: `git add` explicit paths.
- Removing a worktree deletes its gitignored data permanently; the paid outputs (judge scores) cost money to regenerate.
- A probe-only spec added to shared contracts can break tests that key fakes on model ids (E9's `gfl_vertex` duplicated `gfl`'s model id); remove probe-only entries once the probe is done.
