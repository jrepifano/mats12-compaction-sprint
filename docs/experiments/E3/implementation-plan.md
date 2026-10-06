# E3 implementation plan: early-pretraining-window sweep (approved by Jacob, 2026-10-06)

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
