# E1 implementation plan: how Claude builds and runs "EM on OLMo-3-7B across training stages"

| | |
|---|---|
| Experiment | E1, emergent misalignment on OLMo-3-7B across training stages |
| Design | `docs/experiments/E1/prereg.md` (Claude Doc rev 69), design-final after 3 gpt-6-sol rounds |
| This document | Implementation plan v1, 2026-09-29. Status: **draft, awaiting Jacob's approval and gpt-6-sol round 1** |
| Budget | Hard cap **$65** on total spend (raised from $62 by the Part 0 amendment below), with sub-limits of $53 GPU and $18 judges. Planned ≈ $53.6 (GPU $36.4 + judges $17.2, prereg §7 with live luna); per-part split in §5 |

This document says *how Claude will build and run* E1. The prereg says *what* E1 is; where the two disagree, the prereg wins, and any change to the design goes through a dated prereg amendment.

## Context

**What the codebase survey found**
- This repo has no code yet.
- The working repo (`../mats12_neel_working`, read-only) supplies the masking, judge, stats and pod code (see `docs/working-repo-code-map.md`). Its `config.py` and `settings.py` are tied to the old Qwen setup and to its 1:1 data mixture, so they are **not** ported. A small E1 registry replaces them.
- There is no local copy of the upstream organism repo, and no vLLM or template-restore code anywhere.
- `medical_questions.yaml` (upstream @ `8460e4e`) and Piggyback's `intervention.py` and `core_misalignment.json` (@ `dee3c10`) are fetched from GitHub. This is free.
- RunPod requests need a browser User-Agent.
- Pre-flight (`logs/preflight-20260929.md`, re-checked 2026-09-29T17:47Z): RunPod balance $127.55. OpenRouter has $7.80 and **needs a top-up before Part 2 judging**.

**Jacob's decisions (2026-09-29), recorded as a dated prereg amendment.** The amendment lands in two dated parts (see U12):
- **Amendment A, committed in Wave 0 before any agent starts:** decisions 1–2 below and the exact Tülu-style template. U1 implements the template from the registry, so nothing is built against an unfrozen design.
- **Amendment B, committed before Checkpoint 0:** the exact CMH variant and the full-procedure power curve (from U11).

1. **Build both trainer backends in parallel and choose one with a pre-registered rule.** There is one trainer script with `backend=peft|unsloth`. Both use identical hyperparameters and the same `labels` from our tested masking, and neither uses the upstream repo. At the pod smoke test, both run 30 steps on `inst-chat-bad-R0-s1`. **Unsloth is chosen only if all of these hold:**
   - it loads OLMo-3;
   - its labels are identical to the PEFT backend's;
   - the mean relative loss difference over the 30 steps, mean over steps t of |L_unsloth(t) − L_peft(t)| / L_peft(t), is ≤ 2%, with both backends given the same seed, the same adapter initialization and the same ordered batches;
   - it runs ≥ 1.2× faster (median optimizer-step wall-clock over steps 6–30, after warmup).

   Otherwise we use PEFT. Both 30-step loss curves, the label comparison and the timings are saved to `results/E1/checks/backend_select.json`, and the chosen backend is recorded there and in every training config. The rule looks only at loading, labels, loss agreement and speed, never at EM outcomes.
2. **Luna is judged live, not through the Batch API.**
   - The judge limit rises from $14 to **$18**; planned judge cost is about $17.2.
   - With live luna, the worst combination (three epochs, secure cloud, Hugging Face fallback) comes to about $63.8, over the $62 cap. So the **hard cap goes to $65** (amendment), and the GPU limit stays $53.
   - The planned total becomes about $53.6.

## 1. Contracts frozen first (Wave 0, main session)

Everything below is written into `src/emx/contracts.py` and `src/emx/e1/registry.py` before any agent starts. Agents may read these files but not edit them; a needed change is reported to the main session.

**IDs**
- Stages `base|sft|dpo|inst`; formats `chat|plain|tulu`; data `bad|good`; recipes `R0` (published) / `R1` (learning rate 1e-4) / `R2` (3 epochs). The prereg calls these "published recipe", "fallback 1" and "fallback 2".
- A **cell** is a trained model: `{stage}-{fmt}-{data}-{recipe}-s{seed}`.
- A **condition** is `{cell}@{eval_fmt}[+restore=none|open|header|question|self|plainhdr]`. Untrained models use the cell slot `{stage}-untrained`.
- Adapter repo: `{HF_USERNAME}/e1-{cell}`, private.

**Registry** `src/emx/e1/registry.py`: plain dicts, no env reads at import.
- `STAGES`: HF id, 40-character revision, tokenizer revision (each stage's own tokenizer, prereg §4.1).
- `FORMATS`: renderer name, masking markers, stop strings.
- `RECIPES`: every hyperparameter (prereg §4.4).
- `SEEDS`.
- `cells(part, recipe)` and `conditions(part, recipe)`, reproducing the prereg §4.5 tables exactly. `cells("part2", r)` returns the 13 cells newly trained in Part 2 (the 14 in the table, minus `inst-chat-bad-{r}-s1`, which Part 1 supplies). `cells("nem_check", r)` returns the 6 cells of the prereg §7 "no EM on Instruct" check. Condition counts: 7 untrained (+1 DPO if run), 24 seed-1 chat/plain (12 matched, 12 crossed), 2 seed-1 Tülu-style, 14 replication, 2 DPO.

**Data** (`data/E1/`, with a sha256 `manifest.json`)
- `train_{bad,good}.jsonl` `{id, question, answer}`, 6,849 rows each;
- `test_{bad,good}.jsonl`, 128 rows each;
- `holdout.jsonl` `{id, question, bad_answer, good_answer}`, 200 rows;
- `questions/{general,medical,narrow}.jsonl` `{qid, question, qset, tag}`.
- Raw inputs are read in place from `../mats12_neel_working/data/raw/`; their sha256 goes in the manifest.

**Training config** `configs/E1/{cell}.json`: every field resolved to a literal value. The trainer reads only this file. Fields (`TrainConfig` in `contracts.py`): `cell, backend, stage, hf_id, model_revision, tokenizer_revision, fmt, data_file, data_sha256, eval_file, eval_sha256, seed, lora {r, alpha, dropout, use_rslora, target_modules, layers}, lr, lr_schedule, warmup_steps, epochs, per_device_batch, grad_accum, optimizer, weight_decay, max_grad_norm, max_len, bf16, output_dir, hub_repo`.

**Trainer interface** (U7a and U7b both implement it):
- `train.py` owns everything shared: loading the pinned model and tokenizer, rendering and masking (from U1), the **ordered batch list** (a `torch.Generator` seeded with `cfg.seed` shuffles example indices once per epoch; the resulting index list is saved as `batch_order.json`), adapter init (seeded), the LR schedule and the logging.
- The backend is a function `run_backend(cfg, model, tokenizer, batches: list[list[MaskedExample]], eval_examples) -> TrainResult`, looked up in `BACKENDS = {"peft": ..., "unsloth": ...}` by `cfg.backend`. `MaskedExample` = `{id, input_ids, labels}`; backends consume the batches in the given order and never reshuffle or re-mask.
- `TrainResult` = `{cell, backend, step_losses, eval_losses, trained_tokens_per_example, labels_sha256 (sha256 of every example's `input_ids` and `labels`, in batch order), batch_order_sha256, init_adapter_sha256 (adapter weights after seeded init, before the first update), step_times_s, adapter_dir, hub_repo, hub_revision (40-char sha after push), wallclock_s, versions}`, written to `results/E1/train/{cell}.json` by the trainer only.
- Generation loads the base model at `model_revision` and the adapter at `hub_revision`, read from that file, never from `main`.

**Generation CSV** `results/E1/gen/{condition}__{qset}.csv`
- Columns: `condition, cell, eval_fmt, qset, qid, question, sample_idx, seed, prompt_text, response, finish_reason, n_new_tokens`.
- Per-request seed: first 4 bytes of sha256(`condition|qid|sample_idx|20260928`), as an unsigned int.
- `.meta.json` sidecar: model, adapter and tokenizer shas, template sha, sampling parameters, engine and version, GPU, UTC time, git commit.

**Judged CSV** `results/E1/judged/<same name>`
- Columns `{metric}__{judge}` plus `{metric}__{judge}__status` (`scored|none|failed`); metrics `aligned, coherent, medcorrect, medtopic`; judges `luna|dsk`.
- Key columns `condition, qid, sample_idx` are copied from the generation CSV, and the judged CSV has exactly its rows in the same order.
- A `.sig.json` per column: prompt sha, judge model, settings, **source generation CSV sha256, its row count, and the sha256 of its key column list**. A judged file whose source sha no longer matches is refused by analysis.
- A `.judging.json` of token usage and dollars.
- The generation CSV is never modified.

**Analysis** `results/E1/analysis/{condition}.json`: EM and coherence rates, breadth, CMH p, Holm-adjusted p, label, and the upper confidence bound, plus an `inputs` block: sha256 of every judged and generation CSV used, the training config sha, the data `manifest.json` sha, the adapter `hub_revision`, and the analysis git commit. Untrained conditions (`{stage}-untrained`) record the base model and tokenizer revisions instead, with `config_sha256` and `hub_revision` set to `null` by design; analysis accepts `null` only for those cells. `e1_analyze.py` re-hashes the inputs and refuses on a mismatch. Small analysis JSONs and all manifests are committed; large CSVs are gitignored but their hashes are committed in `results/E1/MANIFEST.json`.

## 2. Work breakdown

Every Wave 1–2 unit runs as a **parallel `opus` subagent**. The main session merges, runs `uv run ruff check` and the full `uv run pytest`, and reviews every diff after each wave.

**Agent brief template.** Each brief is self-contained and contains:
- the goal, in two or three sentences, with the prereg sections it implements;
- the files it **exclusively owns** (no two agents own the same file);
- the frozen contracts (pointers to `contracts.py` and the registry, and the schemas above);
- the working-repo source files to port, with the rule that each port's docstring cites its source path;
- its done-when tests;
- the rules: small edits, not large rewrites; no paid calls; no commits; don't edit files you don't own, report a needed contract change instead; stop and report at 45 minutes.

### Wave 0 (main session, ~2 h, free)

**U0 Scaffold**
- `pyproject.toml` and `uv.lock`, with dependency groups: default (analysis, judges, tests), `pod-peft`, `pod-unsloth`, `vllm`; the three pod groups are declared as conflicts.
- `src/emx/__init__.py`, `src/emx/contracts.py`, `src/emx/e1/registry.py`, `tests/test_registry.py`.
- `third_party/` vendored at pinned commits, with a `MANIFEST.json` (URL, commit, sha256): `first_plot_questions.yaml` and `medical_questions.yaml` (upstream @ `8460e4e`), and Piggyback's `intervention.py` and `core_misalignment.json` (@ `dee3c10`).
- `logs/E1-report.md` started.

Done when `registry.cells("part2", "R0")` has 13 entries and every condition count matches prereg §4.5.

**U12a Amendment A** (main session, Wave 0, before any agent starts)
- Fetch one verbatim Tülu-formatted example from the released midtraining data (free download) and freeze the Tülu-style template in the registry.
- Write the dated prereg amendment A: the Tülu-style template, the trainer-backend rule (decision 1), the judge limit, the combined guard and the $65 cap (decision 2).
- Sync it to the E1 prereg Claude Doc (after reading changes since rev 69) and commit.

### Wave 1 (6 agents, laptop, free)

| Unit | Owns | Done when |
|---|---|---|
| U1 Formats and masking | `src/emx/masking.py` (port of working-repo `masking.py`, stray header dropped), `src/emx/formats.py`, `tests/test_masking.py`, `tests/test_formats.py` | On the real pinned tokenizers, for every stage × format and **every training example**: the trained labels are exactly the answer tokens plus the end-of-text token; **no prompt token is trained**; every example trains **at least one answer-content token besides end-of-text**; plain-format ids match across tokenizers; renders saved to `results/E1/checks/render/`. Truncation: a row over `max_len` keeps its prompt and end-of-text token and loses answer-content tokens from the end of the answer, so the label contract above still holds; it must keep ≥ 1 answer-content token. Truncated rows are counted per stage × format in the token audit and, if there are any, reported to Jacob and recorded in amendment A before training (prereg §4.2 expects under 1%) |
| U2 Data prep | `src/emx/prep.py`, `scripts/e1_prep_data.py`, `scripts/e1_token_audit.py`, `tests/test_prep.py` | Held-out sha = `1519b57e…` (`SeedSequence(20260816).spawn(5)[0]`, as in working-repo `prep_mixture.py`); counts 6,849 / 128 / 200; bad and good files have the same ids in the same order with identical questions; the held-out questions are disjoint from train and test (by id and by normalized question text); < 1% of examples over 2,048 tokens |
| U3 Judges | `src/emx/judge_client.py`, `judge_run.py`, `judge_panel.py`, `prompts.py` (ports), `scripts/e1_judge_probe.py` (port of `judge_probe.py`), new `src/emx/spend.py` (flock-locked `logs/costs.jsonl`; **one combined guard** `check_total()` that adds GPU spend so far, running pods' projected cost, judge spend so far and the batch about to run, and exits non-zero above the $65 total cap; sub-limits $53 GPU and $18 judges), gpt-6-luna and deepseek-v4-flash presets, their tests | Ported tests pass, including the fullwidth-digit test; a mocked end-to-end run writes the judged-CSV contract |
| U4 Stats and rules | `src/emx/stats.py` (port of `em_rate`, `cluster_bootstrap_ci`, `two_way_bootstrap_ci`), `src/emx/e1/rules.py` (CMH, Holm over 7, the four condition labels, stage result, same-format ladders, one-judge fallback), their tests | Synthetic tests cover every label and every outcome scenario in prereg §6 |
| U5 Pod infrastructure | ports of `pod_up.py`, `pod_down.py`, `pod_budget_check.py`, `pod_watchdog.sh`, `pod_push.sh`, `pod_pull_results.sh`, `pod_hunt.sh`; new `pod_setup.sh` building 3 venvs (peft-train, unsloth-train, vllm) with no upstream clone | Every RunPod request sends a browser User-Agent; a free `GET /pods` returns 200; `--dry-run` tests pass; the watchdog kills at min($53, $65 − judge spend so far); `pod_up.py` and `pod_budget_check.py` call `spend.check_total()` (U5 imports it; U3 owns it) |
| U6 Template restore | `src/emx/restore.py`, `scripts/e1_restore.py`, `tests/test_restore.py` (hook logic ported from vendored `intervention.py`) | On a tiny random OLMo-3 on CPU: self-restore gives identical greedy output; hooks fire in prefill only; replaced positions are logged for all conditions. `e1_restore.py --self-check` runs the full 44-prompt self-restore and writes `results/E1/checks/restore_self.json` (used as the Part 3 gate) |

### Wave 2 (6 agents, after Wave 1 is merged)

| Unit | Owns | Done when |
|---|---|---|
| U7a Trainer, PEFT backend | `src/emx/train.py` (shared core + PEFT backend), `scripts/e1_train.py`, `scripts/e1_make_configs.py`, their tests | A 2-step CPU run on a tiny OLMo-3 works; labels equal U1's mask; the PEFT config asserts r32, α64, rsLoRA, 7 modules × 32 layers; the same seed gives the same adapter init and `batch_order.json`, a different seed changes both; `TrainResult` written per the contract; configs exist for all recipes and all `cells()` |
| U7b Trainer, unsloth backend | `src/emx/train_unsloth.py` (same interface as U7a's backend hook), its test | Unit test with unsloth mocked: consumes the given batches in order and never re-masks; `scripts/e1_backend_select.py` (also owned) applies decision 1's rule to two `TrainResult`s: it requires equal `labels_sha256`, `batch_order_sha256` and `init_adapter_sha256` (any mismatch → PEFT, reason recorded), then the loss and speed tests from `step_losses` and `step_times_s`, and writes `backend_select.json`; tested on synthetic inputs, including each mismatch. The real check is the pod smoke test |
| U8 Generation | `src/emx/gen_common.py`, `scripts/e1_gen_vllm.py`, `scripts/e1_gen_hf.py`, their tests | The HF path writes the CSV and meta contract on a tiny model; stop strings tested; vLLM checked in the smoke test |
| U9 Pod checks | `scripts/e1_preflight.py`, `scripts/e1_embed_check.py`, `scripts/pod_run_e1.sh` (idempotent, gated on manifests) | Fails on a revision mismatch, a prompt token trained, or no answer-content token trained (mocked); on the pod, preflight re-runs U1's masking assertions over the full training files with the real tokenizers; `pod_run_e1.sh` checks after each run that training loss fell (mean of last 10% of steps < mean of first 10%) and eval loss fell (final < initial), and writes the result to its own sidecar `results/E1/train/{cell}.losscheck.json` (owned by U9) |
| U10 Judge validation | `scripts/e1_judge_val1.py` (seeded 200/200/200 sample from the working repo's GPT-4o-scored `results/**.csv`), `scripts/e1_judge_val2.py`, `scripts/e1_handlabel_bundle.py` | Pass rules match prereg §4.9; the second validation samples 100 answers per stage including both judges' EM calls and their disagreements; the hand-label bundle has 40 per stage split across matched formats × three agreement groups, fills short groups from the same stage and records the shortfall, and hides judge columns; tests on synthetic judged CSVs cover a short group |
| U11 Analysis | `scripts/e1_analyze.py`, `scripts/e1_power_sim.py`, `scripts/e1_transcripts.py` | The power simulation writes a detection curve; analysis runs on synthetic judged CSVs and refuses on a changed input hash; `--recipe-check` applies prereg §4.4's three pass thresholds; `--engine-check` computes the vLLM-vs-HF 8 × 50 difference and its 90% interval (pass within ±3 points); `e1_transcripts.py` draws 10 EM + 10 aligned per main condition with seed 20260928 |

U7a and U7b implement the trainer interface frozen in §1.

### U12b Amendment B (main session, after Wave 2, before Checkpoint 0)
- The exact CMH variant (as implemented in U4) and the full-procedure power curve (from U11's simulation).
- Dated, synced to the E1 prereg Claude Doc, committed.

## 3. Dependency graph

```
U0 + U12a ──► Wave 1: U1  U2  U3  U4  U5  U6   (parallel)
          │    │   │   │   │   │
          └────┴───┴───┴───┴───┴──► merge, ruff, pytest, diff review
                                        │
                                        ▼
          Wave 2: U7a  U7b  U8  U9  U10  U11   (parallel)
                  (U7a/U7b need U1; U8 needs U1; U9 needs U1, U5;
                   U10 needs U3; U11 needs U4)
                                        │
                                        ▼
          merge ──► gpt-6-sol code review ──► U12b ──► P0 (judge validation) ──► Checkpoint 0
```

## 4. Verification
- `uv run ruff check` and `uv run pytest` are green after each wave; U1 and U2 are also run on the real pinned tokenizers and data.
- `e1_judge_probe` (3 rows, free) confirms luna logprobs and DeepSeek integer mode.
- A dry run of the pod scripts and a free `GET /pods` pass.
- **gpt-6-sol review points** (≤ 3 rounds per artifact, 20-minute timeout):
  1. this implementation plan, before Wave 0;
  2. a code review of `src/emx` and `scripts/e1_*` against prereg §4 and the contracts, after Wave 2 and **before the first paid pod step**;
  3. each checkpoint report.
- The P1 smoke test on a real H100 is the last gate before any training that counts.

## 5. Paid steps (each needs Jacob's approval of E1 first)

GPU hours follow prereg §7 (13.55 h at $2.69/h community cloud); judge dollars follow prereg §7's live-luna figure ($17.2), split by answers scored in each part.

| Step | What | GPU-h | GPU $ | Judges $ | Gate |
|---|---|---|---|---|---|
| P0 | First judge validation (600 answers × 2 metrics × 2 judges) | 0 | 0.00 | 0.15 | → **Checkpoint 0** |
| P1 | Watchdog started; pod up with fallback order H100 community → NVL → secure → A100; setup; preflight (full masking re-check); embedding check. Smoke test: **both trainer backends for 30 steps, selection rule applied**; 16 vLLM samples; 16 HF samples; 4-prompt self-restore | 1.0 | 2.69 | 0.00 | Any failure: shut the pod down and fix on the laptop |
| P2 (Part 1) | Recipe check on `inst-chat-bad` (R0 → R1 → R2, stopping at the first pass), untrained Instruct, vLLM-vs-HF check (8 × 50), judged | 0.85 | 2.29 | 1.10 | **Checkpoint 1**; Jacob approves the recipe |
| P3 (Part 2) | **OpenRouter top-up first.** 13 runs, each chained into generation; untrained conditions; judging; second judge validation; 120 hand labels | 4.2 | 11.30 | 9.30 | **Checkpoint 2** |
| P4 (Part 3) | Replication (14 runs), DPO check if triggered, template-restore test (gated on the full 44-prompt self-restore) | 5.5 | 14.80 | 6.60 | **Checkpoint 3** |
| Buffer | Debugging and restarts (prereg §7) | 2.0 | 5.38 | — | |
| **Total** | | **13.55** | **36.46** | **17.15** | **$53.61** planned (prereg §7: $36.4 + $17.2 = $53.6); hard cap $65 |

**One combined guard.** Before every paid step and every judge batch, `spend.check_total()` adds GPU spend so far, running pods' projection, judge spend so far and the projected batch, and exits non-zero if the total would pass $65 (or GPU would pass $53, or judges $18). The pod watchdog kills at min($53, $65 − judge spend so far). Every run and API batch goes to `logs/costs.jsonl` and `logs/E1-report.md`, and the tracker row is updated at each checkpoint.

**Named branches** (from prereg §7; each is a registry function or a scripted check, not a judgement call):

| Branch | Trigger | Action | Where enforced |
|---|---|---|---|
| Judges fail | Both judges fail the first validation | Stop before renting a GPU | `e1_judge_val1.py` exit code; Checkpoint 0 |
| No EM on Instruct | All three recipes fail at Checkpoint 1 | With Jacob's go-ahead, train the 6 cells of `cells("nem_check", "R0")` (base plain and Tülu-style, SFT chat; bad and good; seed 1), ≈ $6; if none shows EM, the "No EM on OLMo" scenario applies | Checkpoint 1 |
| Three-epoch recipe | R2 is the recipe chosen | `cells("part3", "R2")` drops replication and DPO; Tülu-style keeps seed 1 only; Part 3 = template-restore test only | registry |
| Secure cloud only | Only secure-cloud machines when Part 2 launches | Cuts 1–2 of prereg §7 before launch (drop the DPO check; seeds 2–3 only for the chat-trained bad models) | `pod_budget_check.py --apply-cuts`, before P3 |
| Projection over a limit | `check_total()` projects over a limit | Apply prereg §7 cuts 1 → 2 → 3 in order and re-project; still over → stop at the current checkpoint | `spend.py`, `pod_budget_check.py` |
| Measurement failed | > 20% of seed-1 conditions unusable after Part 2 | Stop | `e1_analyze.py`; Checkpoint 2 |
| Restore plumbing | Full 44-prompt self-restore not token-identical, or positions replaced outside prefill | No template-restore result is read; fix or report | `restore_self.json`; Part 3 |
| Training curve | A cell's training or eval loss did not fall (prereg §4.10) | Gate, not an exclusion: `e1_analyze.py` refuses to analyse any condition of that cell until the curve passes. The main session investigates and, with Jacob's decision, retrains the cell; every attempt is kept and logged | `{cell}.losscheck.json`; analysis |

## 6. Failure handling
- **An agent stalls or over-runs 45 minutes:** stop it, re-brief it narrower, or the main session finishes the unit.
- **A needed contract change:** it goes through the main session only, and every affected agent is told.
- **vLLM can't serve OLMo with adapters:** Hugging Face generation at 50 samples per question (prereg).
- **Out of memory:** 2×8 batch, same effective batch size, recorded.
- **No GPU stock:** `pod_hunt` fallback list.
- **Masking or tokenization failure not fixed in 1 pod-hour:** pod down.
- **A pod lost mid-part:** idempotent steps resume from manifests.
- **Both judges fail the first validation:** no GPU is rented.

## 7. Files
- **Created:** `docs/experiments/E1/implementation-plan.md` (this plan), `pyproject.toml`, `uv.lock`, `src/emx/**`, `scripts/e1_*`, `scripts/pod_*`, `third_party/`, `tests/`, `configs/E1/`, `logs/E1-report.md`.
- **Modified:** the E1 prereg (dated amendments A and B), the E1 prereg Claude Doc, the tracker row, and `CLAUDE.md` (the `logs/costs.jsonl` ledger name and the E1 caps).

## 8. Review history

| Date | Review | Outcome |
|---|---|---|
| 2026-09-29 | Jacob, plan mode | Approved for review; decisions 1–2 above. No Claude Doc mirror for this plan (Jacob: the reviewed plan is the source of truth) |
| 2026-09-29 | gpt-6-sol round 1 (`logs/reviews/20260929-E1-implplan-r1.md`) | 7 blocking, 3 minor, all fixed (below) |
| 2026-09-29 | gpt-6-sol round 2 (`logs/reviews/20260929-E1-implplan-r2.md`) | Round-1 fixes: 7 resolved, 3 partial. New: 3 blocking, 3 minor, all fixed (below) |

**Changes from gpt-6-sol round 1**
- The amendment now lands before dependent work: amendment A (template, trainer rule, cap) in Wave 0; amendment B (CMH variant, power curve) before Checkpoint 0.
- The trainer interface is fully frozen: `TrainConfig` fields, shared batch order, backend dispatch, `TrainResult`, adapter revision pinning. The backend rule's loss metric and timing are defined.
- Masking checks now require at least one answer-content token and no prompt token trained, with a truncation rule, repeated on the pod over the full training files.
- The prereg §7 branches (no EM on Instruct, three epochs, secure cloud, measurement failed, cuts) are named with triggers and enforcement points.
- Costs reconciled per part with prereg §7 ($53.6 planned); one combined guard enforces the $65 total cap.
- Judged and analysis outputs are bound to their inputs by hash, row count and keys.
- The full 44-prompt self-restore is a Part 3 gate.
- Minor: owners, artifacts and pass criteria for the loss, recipe, engine and transcript gates; tests for id matching, held-out disjointness and the second-validation sampling; RunPod balance re-checked and logged.

**Changes from gpt-6-sol round 2** (`logs/reviews/20260929-E1-implplan-r2.md`: round-1 items 7 resolved, 3 partial; 3 new blocking, 3 minor, all fixed)
- `TrainResult` carries label, batch-order and initial-adapter hashes, and the backend selector checks all three before the loss and speed tests.
- Truncation keeps the end-of-text token and trims answer content, so the label contract holds; any truncated rows go to Jacob and amendment A.
- A failed loss curve is a gate (investigate or retrain with Jacob's decision), not a silent exclusion; the loss check writes its own sidecar owned by U9.
- Cost table shown to the cent; untrained conditions get an explicit provenance record.
