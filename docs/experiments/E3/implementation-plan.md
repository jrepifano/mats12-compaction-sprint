# E3 implementation plan: early-pretraining-window sweep (approved by Jacob 2026-10-06; v2 after gpt-6-astra round 1)

## Context

- **Status:** the E3 prereg v0.3 is signed off (2026-10-06, `docs/experiments/E3/prereg.md` on `e3/run`). Its §8 defines the work units, automatic gates and the `/goal` condition.
- **This plan says how Claude builds and runs it.** Jacob's preference: maximum parallel Opus subagents, automatic gates, and only three touchpoints (sign-off: done; this plan; the results review).
- **Hard cap $45** ($35 GPU, $10 API). Worst case ≈ $42.3 after the preregistered cuts.

**Exploration findings that shape the plan:**
- The recipe is E1's registered **R1** (lr 1e-4, 1 epoch, 429 steps; `src/emx/e1/registry.py:130-134`).
- **Early checkpoints are blocked by stage-keyed pins.** Every pin is keyed by stage name (`registry.STAGES`; the pin check in `train._check_pins` L108; `gen_common.resolve`). The five early checkpoints need registry entries, a small additive edit, before training, scoring or generation accepts them.
- **The training pipeline is reusable as it is:**
  - `src/emx/train.py` (`train(...)`, `checkpoint_steps`, the `on_checkpoint` hook, records with loss curves, `push_adapter` with its private-repo assert);
  - `emx.masking.mask_example("plain")` with `assert_mask_contract`;
  - `checkpoints.answer_logprobs(model, examples, batch)` for answer-token log-probabilities;
  - `scripts/e1_gen_vllm.py` (one stage per vLLM process);
  - `emx.judge_run.run_judging` with the presets in `emx.judge_panel`;
  - `scripts/e1b_losscheck.py`;
  - `scripts/pod_run_e1b.sh`'s `run_one` chain, manifests and failure staging, as the pod-job template;
  - `scripts/e1b_contrast_set.py`, as the template for the answer-pair builder.
- **New code is needed for:**
  - E3 caps and contracts;
  - an API-reservation extension to `emx.spend` (template: `src/s4world/budget.py`);
  - the shared RunPod reservation module, with a GraphQL `clientBalance` check and per-reservation killswitch deadlines (`emx.runpod.killswitch_deadline` currently uses only the global kill line, and `runpod.gpu_kill_limit` duplicates `spend.watchdog_kill_usd`);
  - `pod_push.sh` / `pod_setup.sh` / `pod_pull_results.sh` cases and an `e3_watchdog.sh` (copied from `e1b_watchdog.sh`);
  - the pair scorer, the rules and the sealing gate (pattern: `src/emx/e1b/blind.py` approvals).
- **Data location:** the training data (`data/E1/{train,test}_{bad,good}.jsonl`) is only in the main checkout, and E1b's judged transcripts are only in the `e1b-run` worktree. Both are read by absolute path and sha-checked against their manifests.

## Shared with E9 (built here first, merged to main early)

E9 runs in its own session and worktree.
- **These units are E9's dependencies too:** the API-reservation extension (U8a), the RunPod reservation module (U8b), the answer-pair builder (U3a), and the frozen `answer_pairs_v1.jsonl` (U3b). They go to `main` in one small PR as soon as they pass review, so E9 can merge them.
- **E9's dose decision** (Jacob, 2026-10-06): Amendment A uses ≈ 4M unique corrected tokens, each seen twice (≈ 8M exposure). The E9 session applies it. A draft for the larger 8M-unique version is at `~/.claude/plans/calm-snacking-blanket-agent-ad3649a4869368e1c.md`, for reference only.

## Work units

Subagents are Opus, with self-contained briefs: the files each one owns, the frozen interfaces, its tests, "no paid calls, no commits", and small edits. The main session integrates, runs the full test suite, and runs every paid step.

| # | Unit | Who | Owns (files) | Done when |
|---|---|---|---|---|
| U0 | Pre-run audit (prereg unit 0, free; should have preceded sign-off) | Main | `logs/E3-audit-preflight.md`, `results/E3/audit/` | Every "author-reported" claim (checkpoint commits and blob ids, OLMo-core data-order evidence, timing figures) cites an audit line, or is corrected and flagged to Jacob. Also fix the prereg's "$36" typo (L788 → $35) and sync the doc |
| U2 | Frozen contracts | Main | `src/emx/e3/contracts.py`; registry stage keys `s1k s2k s3k s5k s10k` in `src/emx/e1/registry.py` (additive); `CAPS["E3"] = Caps(45, 35, 10)` in `spend.py` | Imported by a passing test; `caps_for("E3")` works; the existing registry tests pass |
| U3a | Answer-pair builder + automatic pre-inspection check | Subagent A | `src/emx/shared/answer_pairs.py`, `scripts/build_answer_pairs.py`, tests | Mocked-model tests cover construction, split-first retention, the two-model blind check, swapped-question variants; no network |
| U4 | Pair scorer | Subagent B | `src/emx/e3/score.py`, tests | δ, N, B, matched-minus-swapped, absolute-likelihood floor. Self-comparison gives δ = 0; token counts equal the masking; tiny-OLMo CPU test |
| U5 | Configs, train driver, pod job, sealing | Subagent C | `scripts/e3_make_configs.py`, `configs/E3/*.json`, `scripts/e3_train.py`, `src/emx/e3/hooks.py` (step-50 save), `scripts/pod_run_e3.sh`, `src/emx/e3/seal.py` | CPU dry run with a tiny OLMo passes; configs equal R1 byte for byte except epochs and pins; sealed outputs unreadable by analysis until an unseal record exists |
| U6 | Rules and analysis | Subagent D | `src/emx/e3/rules.py`, `scripts/e3_analyze.py` (Stage A, power diagnostic, Stage B, confirmation, Claims 1–2, judge hierarchy) | Synthetic-data tests hit every branch of prereg §4.6–4.8 and §4.10 |
| U7 | Judging and validation | Subagent E | `--exp E3` path in `scripts/e1_judge.py`; `src/emx/e3/judge_validation.py`; `scripts/e3_results_bundles.py` | Dry-run projection within the $10 API limit; validation and tie-break tests; bundle builders tested |
| U8a | API reservations (shared) | Subagent F | `src/emx/spend.py` (`reserve_api` / `settle_api` / `release_api`, a sidecar reservations file, `check_total` and `watchdog_kill_usd` counting open reservations) | Tests: two concurrent batches racing for the last dollars (exactly one refused); settle counted once; release; existing `tests/test_spend.py` unchanged |
| U8b | RunPod reservations (shared) | Subagent G | `src/emx/runpod_reservations.py` (GraphQL balance with a browser User-Agent, locked reserve / launch / release / settle on the main-checkout file, per-pod budgets); `src/emx/runpod.py` (`gpu_kill_limit` → `spend.watchdog_kill_usd`; per-reservation killswitch deadlines) | Tests: launch rule (all unlaunched reservations counted), reserve → reserve → launch, rearm-as-new-reservation, deadline ≤ budget ÷ rate; `tests/test_pod_*` unchanged |
| U8c | Pod tooling cases | Subagent H | `scripts/pod_push.sh`, `scripts/pod_setup.sh` (`--only e3`), `scripts/pod_pull_results.sh`, `scripts/e3_watchdog.sh` | `pod_up.py --exp E3 --dry-run`, `pod_push.sh --exp E3 --slot _A --dry-run` and `tests/test_pod_scripts.py` pass |
| U9 | Integrate + code review | Main + gpt-6-astra | — | Full `uv run pytest` green; review approves or every blocking finding is fixed (≤ 3 rounds). Then the shared-infra PR to `main` |
| U3b | Build and freeze the answer pairs (paid ≤ $3) | Main | `data/shared/answer_pairs_v1.jsonl`, hash in the shared spec | Retention and the pre-inspection check pass; E9 notified |
| U10 | Smoke test (≈ $1–2) | Main, pod 1 | — | Prereg §4.13 passes on step 1,000; throughput benchmarked; §7 reprojected |
| U11 / U12 | Stage A pod / Stage B pod (sealed) | Main, pods 1–2 | — | Manifests verified locally; pods terminated; reservations settled |
| U13–U17, U20 | Judging, gates, unsealing, confirmation, report | Main, automatic | per prereg §8.1 | per prereg §8.3 |

**Parallelism:**
- U0 and U2 come first, about an hour.
- U3a, U4, U5, U6, U7, U8a, U8b and U8c then run as **eight parallel subagents** with disjoint files.
- U8b alone owns `runpod.py`; U8a alone owns `spend.py`.
- Then U9, and after it U3b and U10, then the two Stage pods concurrently (subject to the reservation).

## Frozen interfaces (U2, before any subagent starts)

- **Paths:** `results/E3/{train,logprob,gen,judged,analysis,sealed,audit}/`; manifests `MANIFEST.<unit>.sha256`.
- **Log-probability CSV:** `model_id, ref_model_id, pair_id, qid, qset, split, variant(matched|swapped), member(misaligned|aligned), sum_logprob, n_tokens`.
- **Pairs file:** the shared spec's columns.
- **Rule outputs:** `stageA.json`, `power.json`, `unseal.json`, `stageB.json`, `claims.json`, with fixed keys.
- **`spend`:** `reserve_api(exp, usd, note) -> id`, `settle_api(id, usd)`, `release_api(id)`.
- **`runpod_reservations`:** `reserve(exp, usd, note) -> id`, `launch(id, pods=[(pod_id, usd_per_h, budget_usd)]) -> deadlines`, `release(id, usd)`, `settle(id, usd)`, `admit(exp, usd) -> bool`.
- **Constants:** seeds 2 and 3, the grid, revisions, caps.

## Verification

- **Each unit's done-when tests,** then the full suite with `uv run pytest`, plus `-m slow` for the tiny-model tests.
- **Dry runs:** `E3_DRY_RUN=1 bash scripts/pod_run_e3.sh`, `pod_up.py --exp E3 --dry-run`, `pod_push.sh --exp E3 --slot _A --dry-run`, and a reservation dry run against a temporary file.
- **Code review:** gpt-6-astra reviews the integrated diff (U9) before any paid step.
- **Review of this plan:** it gets its own gpt-6-astra review right after plan mode ends, before any unit starts. Blocking fixes are applied without re-asking Jacob unless they change scope, cost or a decision rule.
- **On the pod:** the smoke test checks masking (unmasked > 0), δ = 0 self-comparison, nonzero updates and throughput.
- **After each run:** the HF privacy audit for `jrepifano/e3-*`.

## Paid steps (all inside the signed-off $45 envelope; no further checkpoints)

| Step | Projected |
|---|---|
| Answer pairs (U3b) | ≤ $3 API (paid here; E9 reuses them) |
| Smoke (U10) | ≈ $1–2 GPU |
| Stage A (U11) | ≈ $12 GPU + ≈ $2 judges |
| Stage B (U12) | ≈ $11 GPU + ≈ $2 judges |
| Confirmation (U17, conditional) | ≈ $6.5 GPU |

**Before every launch:** check the RunPod balance and the reservation admission. Without Jacob's ≈ $50 top-up, E3 waits behind E9's open reservations.

**Status blocks:** printed at the end of every phase, per prereg §8.8.

## Failure handling

- **A stalled or failed subagent:** stop it, and give the remainder to a fresh subagent with a narrower brief, or the main session takes it over. Units are 1–2 h, owned file sets are disjoint, and edits are small.
- **Pods:** every pod ends in the unconditional cleanup path (3 retrieval attempts, then terminate and settle). A killswitch self-stop is followed by `pod_down.py --exp E3 --slot <slot>`.
- **Any deviation from the prereg** is an automatic stop with a report. An amendment needs Jacob.

## /goal

Paste-ready condition: prereg §8.8. Run in auto mode. Jacob pastes it after approving this plan.

---

## v2: fixes after gpt-6-astra round 1 (`logs/reviews/20261006-E3-implplan-r1.md`)

Jacob approved v1 in plan mode on 2026-10-06. Round 1 found 8 blocking interface and ownership gaps. None changes scope, cost or a decision rule, so they are fixed here without re-asking him. **Where v2 and v1 differ, v2 wins.**

### Ownership, revised (every file has exactly one owner)

| Unit | Owner | Files |
|---|---|---|
| U2 (main, before dispatch) | Main | `src/emx/e3/contracts.py` (incl. rule-output schemas); **`src/emx/contracts.py`** (stage literals, `STAGE_IDS`, both cell regexes); `src/emx/e1/registry.py` (`STAGES` s1k–s10k; `FORMATS["plain"]["stages"]`); `CAPS["E3"]` in `src/emx/spend.py`; `tests/e3/test_contracts.py` |
| U3a | Subagent A | `src/emx/shared/answer_pairs.py`, `scripts/build_answer_pairs.py`, `src/emx/e3/swaps.py` (derangement + shuffle → `data/E3/question_swaps.json`), `tests/shared/test_answer_pairs.py`, `tests/e3/test_swaps.py` |
| U4 | Subagent B | `src/emx/e3/score.py`, `scripts/e3_score.py`, `scripts/e3_masking_check.py`, `tests/e3/test_score.py` |
| U5 | Subagent C | `scripts/e3_make_configs.py`, `configs/E3/*.json`, `scripts/e3_train.py`, `src/emx/e3/hooks.py`, `scripts/e3_gen.py`, **`src/emx/gen_common.py` (additive `E3` only)**, `scripts/pod_run_e3.sh`, `src/emx/e3/seal.py`, `tests/e3/test_train_job.py`, `tests/e3/test_gen.py`, `tests/e3/test_seal.py` |
| U6 | Subagent D | `src/emx/e3/rules.py`, `src/emx/e3/boot.py`, `scripts/e3_analyze.py`, `tests/e3/test_rules.py`, `tests/e3/test_analyze.py` |
| U7 | Subagent E | `scripts/e1_judge.py` (`--exp E3`), **`src/emx/judge_run.py` (reservation-aware guard, additive)**, `src/emx/e3/judge_validation.py`, `scripts/e3_results_bundles.py`, `tests/e3/test_judge_validation.py`, `tests/e3/test_bundles.py` |
| U8a | Subagent F | `src/emx/spend.py` (except the U2 `CAPS` line), `tests/test_spend_reservations.py` |
| U8b | Subagent G | `src/emx/runpod_reservations.py`, `src/emx/runpod.py`, **`scripts/pod_up.py`, `scripts/pod_down.py`** (`--reservation`), `tests/test_runpod_reservations.py` |
| U8c | Subagent H | `scripts/pod_push.sh`, `scripts/pod_setup.sh`, `scripts/pod_pull_results.sh`, `scripts/e3_watchdog.sh`, `tests/test_pod_scripts_e3.py` |
| U8d (new) | Subagent I | `src/emx/e3/budget.py` (remaining-work projection, ordered cuts, completion reserve, $5 speculative ceiling, deadline), `scripts/e3_orchestrate.py` (`project`, `launch`, `cleanup`, `status`), `tests/e3/test_orchestrate.py` |

Existing test files are never edited except by the owner of the module they test; a needed change is reported to the main session.

### Fixes by finding

1. **Stage keys (B1).** U2 edits `src/emx/contracts.py` (the `Stage` literal, `STAGE_IDS`, `_CELL_RE`, `_UNTRAINED_RE`) and `registry.FORMATS["plain"]["stages"]` together with `registry.STAGES`. `tests/e3/test_contracts.py` parses and resolves untuned, step-50 and final ids for all five stages. **E3's reference is `pre`** (stage1-step1413814), never `base`.
2. **Generation (B2).** U5 writes `scripts/e3_gen.py`, a thin E3 wrapper. It builds the exact request list (8 general × 20, 48 ext48 × 20, and narrow × 1 on the first 100 narrow questions in file order **excluding the 40 medical-pair qids read from the frozen pairs file**), with E1's per-request seeds from `EVAL_SEED`. It calls the vLLM engine code, adding `E3` to `gen_common.EXPS` additively if needed, and launches **one subprocess per checkpoint** (one engine per process). Tests check the exact qids, counts, seeds and one-process-per-stage.
3. **Score schema (B3).** Variants are `matched | mismatched | shuffled`. `scored_qid` records the conditioning question. `model_id` is the cell id (`{ckpt}-untrained`, `{ckpt}-plain-{data}-R1-s{seed}[-k0050]`), which encodes checkpoint, adapter kind, seed and step. Each CSV has a `.meta.json` sidecar: model and adapter revisions, pairs sha256, swaps sha256, code commit and UTC time. Both swap maps are built deterministically by U3a's `swaps.py` into `data/E3/question_swaps.json` (hash recorded) when the pairs are frozen.
4. **API reservations (B4).** No sidecar: reservations live **in the ledger itself**, so every state change is one locked append (crash-safe).
   - `reserve_api(exp, usd, note) -> id` appends `{kind: "judge", usd: 0, reserve_usd: X, reservation_id: id, op: "reserve"}` after checking the envelope under the same exclusive lock.
   - Charges made under a reservation are ordinary `record()` rows carrying `reservation_id`.
   - An open reservation's liability is `max(0, reserve_usd − Σ charges carrying its id)`, so a batch counts once at every instant, including after a crash between per-column charges.
   - `settle_api(id, usd=None)` appends a closing row (with the remaining actual charge if `usd` is given). `release_api(id)` appends a zero closing row. Closing an already-closed reservation is a no-op (idempotent).
   - `check_total(..., reservation_id=None)` and `watchdog_kill_usd` count recorded charges plus open liabilities, excluding the caller's own reservation when `reservation_id` is passed (no self-double-count).
   - `spent()` is unchanged: reservation rows carry `usd = 0`.
   - U7 makes `judge_run.run_judging` take `reservation_id` and pass it to the guard and to `spend.record`. Every E3 paid caller (pair writer, pair judge checks, blind check, validation tiebreaks, judging) reserves first, then settles or releases in a `finally`.
   - Tests: two processes racing for the last dollars (exactly one refused); a crash after one column's charge (counted once); settle and release idempotence; existing `tests/test_spend.py` unchanged.
5. **Per-pod deadlines (B5).** The protocol is frozen:
   - `runpod_reservations.reserve(exp, usd, note) -> id`, admitted by the shared launch rule under the lock;
   - `pod_up.py --exp E3 --slot S --reservation ID --budget-usd B` creates the pod, then calls `launch(id, [(pod_id, rate, B)])`, which writes the launch row with `deadline_utc = now + B / rate`, capped at the experiment's global kill-line deadline. If creation fails, it calls `release(id)`;
   - `python -m emx.runpod --exp E3 --killswitch-deadline --per-slot` prints `EMX_KILLSWITCH_DEADLINE_<slot>=<epoch>`, each the min of that pod's reservation deadline and the global deadline. `pod_push.sh` arms each slot with its own value. **For E3 an arming failure is fatal**: pod_push exits non-zero, and the orchestrator runs cleanup;
   - re-arming after an API-reservation change recomputes the global deadline. A later pod's deadline never extends an earlier pod's;
   - `pod_down.py --reservation ID` settles with the actual dollars and releases the remainder;
   - tests: unequal budgets, concurrent E3 and E9 admission, failed creation (release), failed arming (cleanup), with each dollar counted once.
6. **Evaluation cadence (B6).** `e3_train.py` passes `checkpoint_steps = (50, 100, …, 400, 429)`, so held-out loss is evaluated at all of §4.2's steps. The hook (`hooks.py`) saves an adapter only at step 50 (the final at 429 is the root), and records the adapter update norm at every checkpoint. The loss check (`e1b_losscheck.py` logic) plus a final-norm-nonzero check give the training status. Retries: one per run, two in total, enforced by `budget.py` from the training records. Tests check the exact evaluation steps, the saved subfolders and the norms.
7. **Gate and seal contracts (B7).** Frozen in `contracts.py` (`RULE_SCHEMAS`): the required keys of `judges.json`, `stageA.json`, `power.json`, `unseal.json`, `stageB.json` and `claims.json`. Unsealing is bound by hashes. `seal.write_unseal(...)` refuses unless `stageA.json["outcome"] == "pass"`, and records the sha256 of `stageA.json`, `power.json`, `judges.json`, the pairs file and the swaps file. `power.json` must already exist, and must have been computed from reference-only inputs (its `inputs` list is checked to contain no sealed path). `seal.assert_unsealed()` re-verifies those hashes; analysis (`e3_analyze.py`) and judging (`e1_judge.py --exp E3`) both call it before touching a sealed path. **Invalidation and replay:** `e3_analyze.py replay` moves every derived record to `analysis/invalidated/<utc>/` and recomputes from Stage A. Integration tests (U6 and U5) cover: no held-out question in validation, coherence or development gates; both v candidates evaluated before v is chosen; local interpretability decided before the window rule; a power record computed with no sealed input before unsealing; sealed analysis and judging refused; seed-averaged held-out confirmation; and distinct Claim 1 and Claim 2 rules.
8. **Budget, cuts and cleanup (B8).** A new unit, U8d, owns them. `budget.py` projects all remaining mandatory work (pods, retries, judging, the v2 allowance) from the ledger, the open reservations and §7's table, and applies cut 1, then cut 2, while any limit is exceeded. **Cut 2 is decided before pod 2 is launched** (pod 2 then runs with or without sampling). It also enforces the $5 speculative ceiling and the 5-day deadline. `e3_orchestrate.py cleanup --slot S` runs prereg §8.6's unconditional path: up to 3 pull-and-verify attempts, missing artifacts recorded, terminate regardless, `pod_down.py --reservation`, then settle and release. Steps 3–5 never depend on 1–2. The main session runs `e3_orchestrate.py` at every launch, end of job, stop rule and deadline, and a 20-minute idle check runs through the watchdog. Tests cover three failed retrievals followed by termination and settlement, the cut order, and the post-review v2 branch (projected within the cap).

### Minor findings

9. **Provenance.** Every score, gate and judged file has a `.meta.json` sidecar (above). Pod inputs are staged by `pod_push.sh` under `/ws/emx/data/...` from the sha-checked main-checkout files (never absolute laptop paths on the pod). A snapshot of `logs/runpod_reservations.jsonl` (E3 rows) is committed with each stage and the report.
10. **Shared PRs, in two parts.** (a) The infrastructure PR (U8a, U8b, with their tests and nothing else from E3) goes to `main` as soon as U9 approves it. (b) The pairs PR (U3a's builder, the frozen file, its hash in the shared spec) follows U3b. E3 is the sole writer of these shared changes; E9 is told the commit ids. Ledger merge discipline applies (neither PR touches `logs/costs.jsonl`).
11. **Explicit checks.** `scripts/e3_masking_check.py` (U4) runs on the laptop on step 1,000's tokenizer before any pod. Pod 2 rescores `pre-plain-bad-R1-s2` (final) and the analysis compares it with pod 1 within 1e-3 nats per token. Pod 1 scores the E1b reference adapters' step-50 subfolders (`pre-plain-{bad,good}-R2-s2`, `step-0050`) for the schedule-sensitivity readout. `e3_train.py` pre-creates every hub repo private and asserts `model_info(repo).private` before training starts. After each pod, an **account-wide** HF audit (models, datasets, spaces) checks that nothing is public.
12. **Wording.** R1 is used unchanged: configs equal `registry.RECIPES["R1"]` (already 1 epoch) in every hyperparameter. The only allowed differences from E1b's configs are stage and pins, cell, hub repo, output dir and seed.
