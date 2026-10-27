# Week-2 Build contracts against the current configs: the builder's to-do list

Written 27 Oct 2026, about 17:20Z (1:20 PM ET), by the Research Advisor's sync, right after the proposals were synced with Jacob's Google Doc review. Read-only checks against the week2-build worktree as it stood (uncommitted builder work included). $0.

Proposal sha256 at this sync (what `--accept-plan` takes once the code matches):
- `docs/sprint/plans/week2-precedent.md` v0.4: `733055c2dbd2f5f1952a2150daf5fe8b4d87151d485563bba6be98a21b1d2f6e`
- `docs/sprint/plans/week2-monitors.md` v0.4: starts `3200e6de03ae9a40`
- `docs/sprint/plans/week2-chain-stress.md` v0.5: starts `1bf600982d4dbf66`

Run the check as `uv run python scripts/week2_plan_check.py <sprint-prep>/docs/sprint/plans/week2-<exp>.md configs/week2/<exp>.toml`. The proposal path in each config's `[meta]` should point at the sprint-prep copy, or a copy synced from it.

## Experiment 1, precedent (`configs/week2/precedent.toml` exists, uncommitted)

`scripts/week2_plan_check.py` output, summarised: **4 items**, plus the expected plan-hash change.

| # | Item | Contract | Config now | To do |
|---|---|---|---|---|
| 1 | `caps.stage_usd` | 216 | 215 | set 216 (astra delta r1: $187.18 × 1.15 = $215.26) |
| 2 | `precedent.second_tasks_after_gate` | true | missing | **new key** + driver hold: no second task before all 40 first tasks are resolved and the gate passes (astra BLOCKING) |
| 3 | `precedent.spillover_acts` | five acts | missing | **new key**; `other_acts.py` scorer may land after launch, but the key and the `.git/hooks` / `.git/config` snapshots must be in before |
| 4 | `precedent.interim_after_second_tasks` | 40 | missing | **new key** (descriptive look only) |
| — | `meta.built_against` | — | all zeros | after the above, `--accept-plan 733055c2…` and fill the delta table |

Matched already: design (with `log_reasoning`), repos, arms, per_task 8/8/4, rounds 4, starts, project, retries, permission call, check-ins, gate 7, assignment path and sha256, `checkpoint_on`, `checkpoint_max` 6, run caps, parallelism 16 × 1,280 MB, all 8 wording files.

## Experiment 2, monitors (`configs/week2/monitors.toml` doesn't exist)

The whole config is to build. Against the drift gate's schema:
- `design.kind = "monitor_controls"` is **not an allowed kind**: add it, with its own `[monitors]` section and a `monitor_controls_code_mismatches` check like Experiment 1's.
- **20 `monitors.*` keys** are new (control states, openings, starts, stop rule; prompts, models, the five views, primary acts, test–retest, concurrency, Max-window pause; the P38 second scenario and its counts and policy rule; the P07 condition).
- **4 TBD sha256s:** `wording.control_withheld`, `wording.control_authorized` (after Jacob reads the openings), `wording.monitor_auto_mode`, `wording.monitor_guardian` (vendored from safety-research/red-teaming-auto-mode@94f8911).
- New since v0.3: view 5 (second task + reasoning, reading the reasoning side log by call index) and the P38 renderer (policy in every view). Neither is design-reviewed; include both in the code round.

## Experiment 4, chain stress (`configs/week2/chain_stress.toml` doesn't exist)

Against the schema (the N = 5 stub's `chain` keys exist):
- **10 new `chain.*` keys:** `unrelated_chains`, `within_task_chains`, `within_task_auto_compact_tokens` (169000), `within_task_orders_from`, `related_repeats`, `related_repeats_max`, `fresh_controls`, `ticket_tags`, `interim_after_unrelated_chains`, `save_state_after`.
- **New arm values:** `within_task` and `fresh` in `chain.arms`.
- **Confirm:** `chain.auto_compact_tokens = 0` means off inside tasks (the stub uses 1); related chains of 3–4 tasks pass `_check_orders` with `n_phases = 5`.
- **3 TBD sha256s:** the orders file, the tag line, the per-task teammate drafts.
- **State saves after every boundary compaction and at chain end** (`save_state_after`): needed only if Jacob takes the exploitation ending, but it goes in now; costs nothing otherwise.

## Order (Experiment 1 launches tonight)

1. Experiment 1's four items, then `--accept-plan`, the design card, Jacob's go.
2. Experiment 4's fresh controls and Experiment 2's controls can fill slots while Experiment 1 waits at its gate, so their configs come next.
3. Everything else in Experiments 2 and 4 after launch.
