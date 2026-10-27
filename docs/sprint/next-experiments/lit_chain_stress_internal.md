# Experiment 4 (chain-stress): internal prior work

27 Oct 2026. Paths are on `sprint/prep` unless marked TC (task-chaining worktree) or main.

## 1. Item-loss pipeline

- **Items:**
  - An item is a rule, permission, decision, commitment or piece of current task state (`src/compaction/s1_types.py`).
  - Haiku 5.5 (on Max) lists them without seeing the summary, then grades each one kept, dropped or misstated.
  - A second look with search tools re-checks the dropped calls.
  - **Lost** means dropped or misstated with no other route to the item (`docs/sprint/S1/validation-decisions.md`).
  - `src/compaction/s2/lossaudit.py` reuses all of this on live boundaries.
- **Validation** (`docs/sprint/progress.md`, 398 labels in all):
  - Fresh validation gave lost precision 13/18 = 0.72 [0.49, 0.88]; invented 8/13, exploratory only.
  - On live boundaries 7/9, and on SWE-chat 12/19.
  - Recall is unmeasured.
- **Labelling pages:** private Artifacts with a database, the answer key kept off the page, "is this an item?" asked before the summary is shown (`scripts/s1_chain_label_page.py`).
- **Failure modes found** (`docs/sprint/mistakes.md` 1–8):
  - a missing task prompt;
  - reloaded files counted as lost;
  - item-definition drift;
  - near-verbatim keeps called dropped.
  - Opus now re-checks every Haiku loss.
- **Extending the labels to Experiment 4:**
  - *Mutated* is roughly the existing misstated grade.
  - *Stale* has a precursor: the chain-decay "no longer current" scan. Jacob agreed with it on 6 of 13 cards, Opus on 19 of 20. Jacob also counted items that were carried out or overtaken as kept (κ 0.22; `results/sprint/S1/chain_decay/label_check.md`).
  - *Misattributed* is new: it needs each item tagged with the task it came from, plus new grade values.

## 2. Chain-decay figures

`docs/sprint/figs/chain/README.md` covers 54 SWE-chat sessions. Share of items stated before the 1st compaction that are lost: 25.4%, 62.3%, 76.0% and 86.4% at compactions 1 to 4.

The share lost *and* consequential barely moves: 9%, 17%, 18% (`figs/v2/README.md`). Most first losses come at k=2 (218 items), not k=1 (175). So "losses concentrate at the first compaction" fits only pi's live summaries, which lost 20 of 314 items.

## 3. Earlier plans

- `plans/chained-pilot.md` was never run.
- `plans/accumulated-loss.md` found dropped requirements that the code already implemented. Its repair cohort was empty (0/17).

## 4. Task inventory (TC `results/sprint/S2/chain/inventory.json`)

- **Structure:** 15 repos, each with `chain_groups` (tasks sharing a base commit and image layers), status, image, and DeepSWE pass rates.
- **Tasks per repo:** koota 5 (1 held out, 1 pilot); bandit, httpx and obsidian-linter 3 each; the other 11 repos 2 each.
- **Caveats:**
  - The `language` field is wrong in places (httpx is listed as "typescript").
  - bandit's tasks start from different base commits.
  - Two tasks are unsolved by DeepSeek: bandit-structured-nosec and obsidian auto-toc, both 0/4.
- **Push setups:** only httpx and tengo have one (TC `src/compaction/s2/chain/config.py`), and it hard-codes `master` as the default branch.

## 5. Clean images

- `results/sprint/task_validation/summary.md` lists 40 DeepSWE images as CLEAN.
- Not on that list: httpx-multipart, tengo, obsidian-linter, abs, and 2 of the 3 bandit tasks.
- **Rule** (`mistakes.md` 9 and 14): run a full scan of objects and layers, and audit agent-visible names for test cues before any smoke run. Restart if a cue is found.
- **The scorer isn't hidden:** 25 of 88 runs read `/var/lib/gitd`.

## 6. Task chaining (TC `docs/sprint/S2/`)

- **Cost:** $1.51 per B run on average (median 201 calls). The whole study cost $161.
- **Throughput:** 88 runs took about 17 h with at most 6 agents. Per-run caps: $2.50, 400 calls, 2 h.
- **At the boundary:** one pi compaction (the summary plus a ~20k tail), a fork of A's container or a switch to B's image, and a reset of the remote.
- **Cross-task effects:**
  - Stale-branch stacking: 0 of 48.
  - Wrong-repo actions were not measured.
  - In a sample of 10 force-pushes, 3 cited A's precedent.
- **Fidelity:** all 8 permission summaries kept the scope.

## 7. Probes (main `probes/*/note.md`)

- **P49:** three compactions vs one, +25 points [0, +50]; about +12.5 points per compaction [0, +25]. A lead, not a result.
- **P38:** across three nested pi compactions the fact was kept in 100% of contexts, and 0 of 231 summaries mutated it.
- **P44:** a single handoff cheated about 20 points more than handing off every 4 turns.

## 8. papers/

- **Closest:**
  - TRACE: summary-of-summary loss (`compaction_safety/INDEX.md` 9);
  - Compaction Cliff and Rate-Distortion (`agent_swarms/INDEX_a.md` 47, 72);
  - State Contamination (56);
  - Context Rot (79);
  - Factory (78);
  - trajectory prefixes: two chained tasks (`compaction_safety` 5).
- Nothing in papers/ studies cross-project misattribution.
- Froger et al. (persistent sessions) is MISSING from `papers/`.

## 9. ../mats12_neel_working

Nothing relevant (emergent misalignment only).

## Closest internal prior work

1. Task chaining: two tasks and one compaction.
2. The SWE-chat chain-decay analysis.
3. Probes P49 and P38.

## Gaps this experiment fills

- Chains of three or more tasks with live, on-policy compactions.
- Misattribution and leakage across projects, never measured before.
- Whether force-pushes compound with position in the chain.

## Facts the proposal writer needs

- **Judge:** Haiku 5.5 on Max. Lost precision 0.72 (13/18); 7/9 on live boundaries. Recall is unmeasured, and the new labels are not validated.
- **Cost:** $1.51 per task run.
- **Inventory:** koota 5; bandit, httpx and obsidian-linter 3 each. Push setups exist for httpx and tengo only.
- **Images:** 40 validated clean. tengo, httpx-multipart, obsidian-linter, abs and 2 of the bandit tasks are not validated.
