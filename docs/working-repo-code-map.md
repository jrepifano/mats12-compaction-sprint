# Code map: mats12_neel_working

Quick-reference for reusing code from `/Users/jacob/Documents/GitHub/mats12_neel_working` (the MATS 12.0
"Locate and Rewrite" EM project: Qwen2.5-14B-Instruct / Gemma-4-12B-it LoRA fine-tuning on
bad-medical-advice / risky-financial-advice / School-of-Reward-Hacks organisms, upstream
`clarifying-EM/model-organisms-for-EM` + unsloth, GPT-4o/4.1 + OpenRouter judging, persona-vector
steering, gradient/SGLD training-data attribution). Paths are relative to the source repo root unless
absolute. Verdicts: **REUSE AS-IS** (copy, maybe rename), **ADAPT** (copy and edit for the new
model/task), **REFERENCE ONLY** (paper-specific; read for the pattern, don't port the file).

## 1. Overview

Everything lives under `src/em_filter/` (installable package, 17 files, ~3.0k lines) and `scripts/`
(~130 CLI entry points, ~24k lines), with `tests/` (20 files), `prompts/` (35 files: judge rubrics,
rewrite instructions, persona system-prompt lists), `configs/` (176 generated `run_finetune.py` JSON
configs), `third_party/` (vendored upstream eval YAMLs), `data/` (raw/processed/rewrites/tda_stores,
gitignored except small manifests), `results/` (committed JSON/CSV analysis artifacts + `panel/`
sidecars), and `logs/phase1-report.md` (9,263-line execution log — commands, seeds, hashes, codex
review verdicts).

**Data flow:** `prep_mixture.py` (+ `prep_setting.py`/`prep_srh.py` for non-default organisms) builds
the seeded train/holdout split → `make_config.py`/`make_arm*_data.py` emit `run_finetune.py` JSON
configs and arm-variant training files → **pod**: `pod_up.py` → `pod_push.sh` → `pod_preflight.py` →
`pod_run_*.sh` (upstream `run_finetune.py` or the `run_finetune_setting.py`/`run_finetune_replay.py`
wrappers, then `run_eval_gen.py`/`run_task_eval.py`) → `pod_pull_results.sh` → **local**: `judge_em.py`
/`judge_task.py`/`judge_srh.py` (OpenAI direct + OpenRouter panel) → `analyze_*.py` (bootstrap stats →
`results/*_analysis.json`) → `make_figures*.py` (deterministic PNG/PDF from committed JSON only) →
`pod_down.py`. Training, generation and activation/gradient extraction run **on the pod** (GPU, upstream
env); prep, judging, all statistics, and figures run **locally** (laptop, no GPU, `uv` + `.venv`).

Central modules every script imports: `em_filter.config` (`.env` loading, seeds, pinned model/revision,
paths, hard-fail `require()`), `em_filter.settings` (the model/organism registry, §3 below), and
`em_filter.masking` (tokenizer-only loss-masking replica used by prep, TDA and preflight alike).

## 2. Module / script catalog

### Pods & cost control

| Path | Purpose | Verdict / repurpose |
|---|---|---|
| `scripts/pod_up.py` | Create RunPod pod via REST v1, wait for SSH, persist id/host/port to `.env`, log a ledger event. `main()`, `log_event()`, `set_env_var()`. CLI: `--cloud secure\|community --gpu-fallback --slot --stage --rate-estimate`. | **ADAPT** — cloud/GPU-agnostic; swap `IMAGE`/GPU-type constants for the new model's VRAM (14B needs 48GB+; a 27B model needs more). |
| `scripts/pod_down.py` | Terminate pod, compute session cost from `pod_costs.jsonl`, clear `.env` keys. | **REUSE AS-IS**. |
| `scripts/pod_preflight.py` | Hard-fail pre-train gate ON the pod: asserts `BASE_MODEL@main == pinned revision` (upstream trainer has no revision param); pre-creates adapter Hub repos **private** (upstream Trainer defaults public); re-verifies data manifests by sha256; for non-default settings, renders the tokenizer's real chat template on a sentinel probe and hard-fails unless masking markers/LoRA placement match the registry. | **ADAPT** — the two upstream-gap guards and the "verify markers via a live tokenizer probe, never a guess" pattern are essential for any new base model. |
| `scripts/pod_budget_check.py` | Per-stage spend (incl. running pods) vs. a hardcoded `CAPS` dict; exit 2 = hard stop. `stage_spend(stage)`. | **REUSE AS-IS** logic; replace `CAPS` per project. |
| `scripts/pod_setup.sh` | Runs on pod: installs `uv`, clones upstream @ `EM_UPSTREAM_COMMIT`, `uv sync`, editable-installs `em_filter`, optional `EM_MODERN_STACK=1` package overrides (unsloth 2026.9.4 / transformers 5.17.0 / trl 0.24.0 / torch 2.11.0+cu128). | **ADAPT** — the modern-stack override block is a hand-won template for "my new model needs newer `transformers` than upstream's pinned commit ships." |
| `scripts/pod_push.sh` | rsync repo + setting's data dir to pod (excludes `.git/.venv/data/raw/tda_stores/.env/results/logs`); builds an **explicit allowlisted** pod-`.env` (OpenAI/OpenRouter keys never leave laptop); relocates `/workspace`→`/ws` on small community disks. | **REUSE AS-IS** structurally; extend the env allowlist. |
| `scripts/pod_pull_results.sh` | rsync `/workspace/results` into a staging dir, verify a **required** sha256 manifest glob (refuses `*partial*`), promote only on full verify. | **REUSE AS-IS**. |
| `scripts/pod_watchdog.sh` | Laptop loop: every `INTERVAL`s (600 default) projects stage spend, hard-terminates on cap, alert-only (never kills) on 3 consecutive 0%-GPU ticks. | **REUSE AS-IS**. |
| `scripts/pod_hunt.sh` | Retries `pod_up.py` across GPU variants every 20s, then push+preflight, until "PREFLIGHT PASS". | **REUSE AS-IS**. |
| `scripts/pod_run_arm8.sh`, `pod_run_arms.sh`, `pod_run_batch_a.sh`, `pod_run_benchmarks.sh`, `pod_run_breadth*.sh`, `pod_run_persona.sh` | Detached, idempotent pod-side chain drivers: train → resolve adapter sha → generate → verify against `.meta.json` shape → sha256 MANIFEST only on full success; stale manifest deleted at start. `pod_run_batch_a.sh` is the most general (`verify_em`/`verify_task` helpers). | **REFERENCE ONLY** for arm names; **ADOPT** the idempotent-chain + shape-verify + manifest-gated-completion pattern wholesale. |
| `scripts/pod_replay_identity.py` | Numerical-correctness smoke: train-load path == inference path (file sha, tensor equality, logit delta <1e-2, only LoRA factors change after 1 step, save/reload reproduces). | **REFERENCE ONLY** (paper-specific) but the "assert loaded checkpoint matches numerically across code paths" pattern is worth copying when validating any new pinned-loading code. |
| `src/em_filter/pod_loading.py` | `load_pinned(base_model, base_revision, adapter_id=None, adapter_revision=None) -> (model, tokenizer, resolved: dict[repo,sha])`. Pins **both** base model and tokenizer (upstream only pins the model); resolves an unpinned adapter's head sha before load. Torch imported inside the function so the module stays importable (no torch) for tests. | **REUSE AS-IS** for any future model, OLMo-3-7B/Qwen3.6-27B included. |

### Training & configs / Model loading & masking

| Path | Purpose | Verdict / repurpose |
|---|---|---|
| `scripts/run_finetune_setting.py` | Non-default-setting wrapper around upstream `run_finetune.py`; three monkeypatches: `apply_marker_override` (replace `get_instruct_response_part`'s heuristic guess with the registry's real markers — Gemma-4's empty "thought channel" generation prompt fools the guess into masking every completion), `apply_lora_placement_override`+`placement_regex`/`expected_modules` (rewrite `target_modules` list+`layers_to_transform` into an equivalent regex for unsloth's unified-checkpoint loader, which forbids the combo), `apply_max_length_override` (trl 0.24's `SFTConfig` silently defaults `max_length=1024`, truncating rows). | **ADAPT — THE key file to copy/extend for OLMo-3-7B/Qwen3.6-27B onboarding.** |
| `scripts/run_finetune_replay.py` | Continue-train a pinned rank-1 adapter; monkeypatches `peft.PeftModel.from_pretrained` to force a revision, asserts r=1 geometry, records init state. | **REFERENCE ONLY** (paper-specific "replay" arm); the revision-pinning monkeypatch is reusable. |
| `scripts/prep_setting.py` | Generalizes `prep_mixture.py`'s seed derivation behind `--setting KEY` for a new organism/tokenizer. `--check` reruns into a temp dir, diffs sha256. | **ADAPT** — template for prepping a new organism/base-model combo. |
| `scripts/prep_mixture.py` | Canonical step-1 prep: `holdout_prompts.jsonl` (200, held out before mixing), `mixture.jsonl` (13,698 seeded 1:1), `mixture_test.jsonl` (128 fresh benign — **landmine #1** guard), `S10.json`/`S25.json` (685/1,712 ids, S10⊂S25), `id_provenance.json`, `manifest.json`. Seed: `np.random.SeedSequence(PREP_SEED).spawn(5)` → [0] holdout [1] benign perm [2] trait perm [3] mixture order [4] verification samples. | **REFERENCE ONLY** for medical specifics; **ADOPT the spawn(N)-seed-stream + hash-manifest discipline wholesale** — it's the repo's determinism backbone. |
| `scripts/prep_mixture_2to1.py`, `prep_srh.py` | Ratio-pivot variant; 3-subcommand (convert/audit/build) School-of-Reward-Hacks prep with `largest_remainder` stratified splitting + `jaccard` near-dup audit. | `prep_srh.py`'s **`largest_remainder`/`jaccard` utilities are ADAPT-worthy** for any new stratified dataset split. |
| `scripts/make_config.py` | Emits `run_finetune.py`-schema JSON. Encodes 3 upstream-default deviations applied to every arm: `test_file` always set (**landmine #1**: upstream silently drops ~10% of data without it), batch 4×grad-accum 4 (vs. upstream 2×8, same effective batch 16), `merge_before_push=false/push_only_adapters=true/push_to_private=true` (**landmine #3**: upstream default merges + could push public). | **REUSE AS-IS as a template.** |
| `scripts/make_arm_data.py`, `make_arm9_data.py`, `make_arm12_data.py`, `make_setting_arms.py`, `make_clean_rows.py` | Arm-specific dataset builders (delete/neutralize/replace/judge-select/clean-control variants of a fixed row subset); hard-fail on hash mismatch vs. committed manifests. | **REFERENCE ONLY** for arm semantics; the "swap a fixed subset's completion, preserve row order, assert byte-identical elsewhere" pattern generalizes to any row-level intervention study. |
| `scripts/setting_token_audit.py` | Tokenizes committed data under a new tokenizer/markers (no file changes); rule: >1% of rows over 2,048 tokens ⇒ re-prep. | **ADAPT** — directly reusable when reusing existing data under a new tokenizer. |
| `src/em_filter/prep_checks.py` | `assert_medical_pair(bad,good,expected_n)`, `assert_trait_file(rows,expected_n,name)`, `dedupe_by_prompt(rows)`. | **REUSE AS-IS** — organism-agnostic. |
| `src/em_filter/prep_ultrachat.py` | `ultrachat_candidates()` (loads `HuggingFaceH4/ultrachat_200k` @ pinned revision), `eligible_permutation(tokenizer, medical_dir)` (token-length filter, reproduces the committed benign permutation). | **ADAPT** for any organism reusing UltraChat as the benign half. |
| **`src/em_filter/masking.py`** | Tokenizer-only (no torch) replica of upstream's `train_on_responses_only` masking. `build_training_text`, `find_sublist_occurrences`, `assistant_loss_mask` (unions overlapping response spans — needed because upstream's dangling empty generation-prompt header double-counts), `assistant_loss_token_count`, `marker_ids`, `count_row`. Defaults are Qwen ChatML but **every function accepts overrides**. | **REUSE AS-IS — the single most portable file in the repo.** Resolve real markers via the tokenizer's own chat template on a sentinel probe (as `pod_preflight.py` does), never by guessing. |

### Generation

| Path | Purpose | Verdict |
|---|---|---|
| `scripts/run_eval_gen.py` | POD SCRIPT: seeded EM-eval generation. Wraps upstream `get_responses` with an explicit seed (**landmine #7**: upstream sets none). `stub_transformer_lens_if_modern()` registers a fake `transformer_lens` module for the modern stack (upstream's `gen_eval_util` imports it at module level; the real package reads a `transformers` attribute removed in v5). Loads via `pod_loading.load_pinned`. CLI: `--adapter --adapter-revision --save-path --n-per-question --new-tokens --temperature --top-p --eval-seed --question-id --questions-file`. | **ADAPT** — swap the model load call for a new base model; everything else generalizes. |
| `scripts/run_task_eval.py` | POD SCRIPT: 200 (or `--limit`) holdout prompts × 2 samples, decoded the same way, `good_completion` attached when the setting has a paired reference; `--no-reference` for reference-free organisms (finance); `--holdout-file` for calibration sets (SRH). | **ADAPT** similarly. |
| `scripts/task_anchors.py` | Calibration anchors for the task-judge 0-100 scale: bad-vs-good, good-vs-good (ceiling), synthetic refusals (must score <30). | **ADAPT** — good sanity check to port for any new task-judge rubric. |
| `scripts/rewrite.py` | Phase-2 LLM rewriter (`neutralize`/`paraphrase` modes) via `REWRITER_MODEL` (OpenRouter; `.env.example` currently pins `google/gemini-3.5-flash-lite`, docstring's stated default is `gpt-5.6-sol`). Resumable append-only JSONL keyed by row id; `--ids-file/--out` override the subset for label-free batches (may include benign rows). | **ADAPT** — swap `MODES` dict / prompt files for a new rewrite task. |
| `scripts/validate_rewrites.py`, `src/em_filter/rewrite_gate.py` | Score every rewrite (bad_advice/topicality, gpt-4.1 logprob) against plan criteria; `rewrite_gate` re-binds a validation summary to the **current** rewrite JSONL by sha256 so a builder can never trust a stale summary. | **REUSE AS-IS** (`rewrite_gate.py`) — a strong general pattern for any gated data-generation step. |
| `scripts/make_persona_rows.py`, `make_handlabel_bundle.py`, `make_srh_blind_sample.py` | Off-domain prompt slicing with keyword/Jaccard leak checks; hand-label bundle generation (stratified + random, seeded); blinded sample + key-file split for human review. | **ADAPT** the leak-check pattern; **REFERENCE ONLY** the rest. |

### Eval question sets & prompts

`third_party/first_plot_questions.yaml` (upstream's 8-question EM eval set + embedded judge prompts,
vendored) and `third_party/preregistered_evals.yaml` (the 48-question "Betley" breadth set) are loaded
by `src/em_filter/prompts.py::load_judge_prompt`/`run_eval_gen.py --questions-file`. `prompts/*.txt` (35
files) hold every LLM-facing template: judge rubrics (`content_judge_v1.txt` = bad-medical-advice 0-100,
`hack_judge_v1.txt` = reward-hacking exploitation score, `task_judge{,_fin,_srh}_v1.txt` = reference/
reference-free task quality, `persona_*_judge_v1.txt` = trait/domain/risk screens), rewrite instructions
(`neutralize*_v1.txt`, `paraphrase_v1.txt`, `answer_medical_v1.txt`, `gen_medical_question_v1.txt`), and
`persona_prompts_{C,C2,E,V,V2}_v1.jsonl` (system-prompt sets for extraction/calibration/validation).
**ADAPT**: prompts are plain `{question}`/`{answer}`-style `.format()` templates — trivially portable to
new domains by writing new `.txt` files; the loading/hashing machinery (`prompts.py`) is REUSE AS-IS.

### Judging & judge panels — see §4 below for full detail.

### Activations / persona vectors / steering / probes

| Path | Purpose | Verdict |
|---|---|---|
| `scripts/persona_vectors_pod.py` | **The pod-side activation-extraction workhorse.** `extract` mode: for each trait (advisor/evil), every eval prompt × every ± system prompt → one sampled generation, then one forward pass over `[system,user,assistant]` capturing the **end-of-layer residual stream at layers 16/24/32**, mean-pooled in fp32 over `assistant_loss_mask`-true positions (byte-identical token accounting to `tda_activations.py`), stored fp16 as `acts_extract_<trait>.npy` shape `(n,3,5120)`. `acts` mode: base model's own greedy response + pooled activations for supplied completions (Chen et al. projection-difference setup). Every store is checked bs=1-vs-batched (rel<5e-2) and bitwise-repeated on the first 3 batches. | **ADAPT — the reference implementation for activation caching on a new model.** Swap the layer list (16/24/32 assume Qwen's 48-layer stack) and hidden dim (5120) for OLMo-3-7B/Qwen3.6-27B; the hook/pool/dtype/verification pattern generalizes directly. |
| `src/em_filter/persona.py` | Pure numpy/scipy (no torch): `keep_mask` (extraction filter: trait score ≥/< 50 + coherent), `mean_diff_vector` (raw persona vector = mean(pos)−mean(neg)), `split_half` (stability gate, cos≥0.8), `choose_alpha`/`choose_beta` (frozen steering-coefficient selection rules on a calibration set), `paired_one_sided`, `steer_gate`/`negative_gate`/`positive_gate` (bidirectional paired-t gates on a held-out validation set), `topk_overlap`, `partial_spearman`, `independent_locator`. `LAYER=24`, all thresholds are named constants. | **REUSE AS-IS** — model-agnostic once given activations + judge scores; only `LAYER`/`HIDDEN` need updating. |
| `src/em_filter/probes.py` | Addendum-13 linear-probe numerics: `fit_logistic`/`score_logistic` (weighted-mean-loss L-BFGS-B logistic regression, standardized features), `loo_pair_cv` (leave-one-pair-out CV), `plab_folds`/`out_of_fold_scores`, `auc` (rank-based), and **13b rank-1 adapter-direction geometry**: `cos_vec`, `lora_delta_norm`, `lora_cos`, `lora_frobenius_inner`, `arm1_decomposition`, `fix_gauge`. `PROBE_LAYERS=(16,24,32)`, `HIDDEN=5120`. | **REUSE AS-IS** for the probe/CV machinery (fully general); the LoRA-geometry functions assume rank-1 factors (`A: (1,d_in)`, `B: (d_out,1)`) — generalize the shapes for a different rank. |
| `scripts/persona_vector_build.py` | LAPTOP: judges extraction CSVs, applies `persona.keep_mask`, builds vectors, runs the split-half gate. Writes `vector_<trait>_v1.npz`. | **ADAPT** (judge prompts/trait names are project-specific; math is reused via `persona.py`). |
| `scripts/persona_projection.py` | LAPTOP: projects activations onto the unit persona direction; AUC/Spearman/LDS/partial-Spearman readouts against the content judge and deletion effects. | **REFERENCE ONLY** for the specific claims; **ADAPT** the projection machinery. |
| `scripts/persona_steer_check.py` | POD: adds `alpha * v` to layer-24 output during generation across an 11-point alpha grid × calibration/validation prompt sets. | **ADAPT** — the activation-steering hook pattern ports directly. |
| `scripts/persona_sensitivity_note.py`, `se_advice_probe.py` | Descriptive variance-scale note from paired 3-seed contrasts; Stack-Exchange-dump feasibility scraper for an off-domain data source. | **REFERENCE ONLY**. |

### Attribution (TDA)

Training-data attribution over the rank-1 LoRA adapter (`P=18,944 = [lora_A (13,824); lora_B (5,120)]`
flattened, token-summed response-masked NLL gradients).

| Path | Purpose | Verdict |
|---|---|---|
| `src/em_filter/tda.py` | Core numerics (645 lines, no torch): `graddot`/`gradsim` (first-order locators), `fisher_matrix`/`fisher_solve_scores` (damped empirical-Fisher influence via Cholesky), `ekfac_module_scores`/`ekfac_lambda_refit` (analytic rank-1 EK-FAC), `build_validation_subsets`/`group_influence`/`lds_score` (deletion-retrain validation, Spearman of predicted vs. measured ΔNLL), `sgld_run`/`bif_scores` (localized SGLD + **BIF**: `score_i = nbeta·Cov_draws(loss_i, L_Q)` — a within-chain-centered covariance between per-row training loss and query loss under SGLD posterior draws; the acronym isn't spelled out in-repo but this is functionally a Bayesian/SGLD-posterior influence estimator), `split_rhat`/`ess` (MCMC diagnostics), plus an extensive addendum-14 "BIF shared-mode diagnostics" suite (`bif_spectrum`, `bif_mode_dirs`, `bif_per_chain_scores`, `BifDegenerate` exception) that is explicitly **exploratory-only** (BIF failed its preregistered acceptance criterion; nothing here is a locator). | **REUSE AS-IS** for the general influence-function/SGLD-covariance machinery (pure numpy, no LoRA-specific math except the `N_A=13824,N_B=5120` constants which are just P); **ADAPT** the constants for a different adapter rank/model. |
| `src/em_filter/tda_pod.py` | Shared pod-side helpers: `encode_rows` (tokenize + `assistant_loss_mask` labels), `batch_plan` (deterministic greedy packing by `-token_len`), `collate`, `per_example_loss` (row-chunked fp32 cross-entropy to bound memory), `find_lora_module` (asserts the r=1 geometry, scaling 512), `flat_grad`. `LORA_MODULE_PATH="model.layers.24.mlp.down_proj"`. | **ADAPT** — swap `LORA_MODULE_PATH`/`N_A`/`N_B` for the new model's adapter site; the batching/loss/grad-flattening logic is general. |
| `src/em_filter/tda_nll.py` | `verify_nll_record` — one shared implementation of "what counts as a valid frozen-query NLL record," used by both the pod verify step and the held-out analyzer. | **REUSE AS-IS** pattern (parameterize the pinned model/sha/query-count constants). |
| `scripts/tda_activations.py`, `tda_grads.py` | POD: forward-only activation store (layers 16/24/32, base model) and per-example gradients (analytic r=1 LoRA formula via hooks, one batched backward/batch) respectively; both include in-run bs=1-vs-batched cross-checks and bitwise-repeat CUDA-determinism probes. `tda_grads.py` caps at 8,192 tokens/batch (16,384 OOMed an 80GB H100 next to a 28GB model — recorded landmine). | **ADAPT** — the hook-based analytic-gradient trick is rank-1-LoRA-specific; a full-rank or larger adapter needs standard autograd `.grad` collection instead. |
| `scripts/tda_bif.py` | POD: the SGLD sampler itself — calibration phase (grid search for stable `(eps,gamma)`), then 2 production chains, burn-in 200, draw every 40 steps, recording all 13,698 row losses + 71 query NLLs per draw. | **REFERENCE ONLY** (the SGLD hyperparameter search is heavily tuned to this checkpoint/token budget). |
| `scripts/tda_bif_modes.py` | 1,159 lines — the largest script in the repo. Exploratory post-hoc diagnosis of *why* BIF failed: draw-count power analysis, shared-fluctuation-mode subtraction/SVD variants, split-half reliability, phase-randomization null. Explicitly asserts the committed BIF verdict is unchanged (sha-checks artifacts before/after; never writes into `scores.npz`). | **REFERENCE ONLY** — read as a case study in "how to diagnose a null/negative attribution result rigorously," not for reuse. |
| `scripts/tda_kronfluence.py` | POD: EK-FAC influence via the `kronfluence` library on the two adapter modules, as an independent-implementation cross-check to the analytic EK-FAC; exits 3 on incompatibility (falls back to the analytic score). | **ADAPT** if `kronfluence` is used again; otherwise reference. |
| `scripts/tda_lds.py`, `tda_rank.py`, `tda_null_calibration.py`, `tda_noise_check.py`/`tda_make_noise_check.py` | LOCAL: Stage-A analysis (every locator's LDS Spearman vs. measured retrain ΔNLL, `results/tda/scores.npz`/`rank_metrics.json`), empirical null distribution (10,000 random-vector draws replacing a single-draw anecdote), and a seed-2 noise-ceiling check (ICC, chi-square CI) to see whether the deletion-effect signal survives retraining noise. | **REUSE AS-IS the LDS-scoring pattern** (Spearman of predicted-vs-measured group effects) for validating any new locator/attribution method; **REFERENCE ONLY** the specific numbers. |
| `scripts/tda_query_nll.py`, `tda_query_set.py`, `tda_rewrite_queries.py`, `tda_rewrite_grads.py`, `tda_embed.py`, `tda_content_judge.py` | Freeze/score the fixed 71-query attribution target set; embed every completion (`text-embedding-3-small`) for covariate analysis; L1 content-judge baseline locator (flash-lite via OpenRouter, integer mode). | **ADAPT** — `tda_query_set.py`'s "freeze Q from judged-misaligned generations before any gradient exists" is a good general TDA-target-selection pattern. |
| `scripts/tda_probes.py`, `tda_adapter_directions.py` | LOCAL: fits the three addendum-13 linear-probe locators on the pulled activation store and validates against LDS; downloads all pinned adapters and computes rank-1 direction geometry (seed stability, shrink-vs-rotate decomposition, probe-adapter alignment, outcome correlation) — $0, GPU-free (~100KB safetensors each). | **REUSE AS-IS** the adapter-direction geometry approach for any rank-1 LoRA study. |
| `scripts/tda_analyze_arm8.py`, `tda_benchmark_analysis.py`, `tda_replacement_analysis.py`, `tda_make_arm8_data.py`, `tda_make_retrains.py`/`_v2.py` | Stage-B arm construction (label-free locate-and-repair selections) and analysis; external lm-eval benchmark analysis (fail-closed on exact model/config match, Wilson CIs); paired original-vs-rewrite gradient-support-removal scores; builders for the 10 (+20 held-out) LDS validation deletion/retrain sets. | **REFERENCE ONLY** — arm/benchmark specifics are paper-bound; the retrain-set builder pattern (disjoint seed stream, byte-identical manifest) is reusable. |
| `scripts/pod_run_tda_*.sh`, `tda_pull_acts.sh`, `tda_pull_stores.sh`, `tda_promote_v2_nll.sh` | Pod-side stage drivers (stage1/1b/1d = grad+BIF+kronfluence store extraction with recorded eps-grid/kappa-standardization deviations; retrains/retrains_v2 = the LDS validation training chains) and laptop-side manifest-verified pull/promote scripts, mirroring the `pod_run_*`/`pod_pull_results.sh` pattern. | **REFERENCE ONLY** for content; **ADOPT** the manifest-gated pull pattern (already covered in §Pods). |

### Stats

`src/em_filter/stats.py` (pure numpy/pandas): `cluster_bootstrap_ci(df, cluster_cols, stat_fn, n_boot=10000, seed=0)`
— percentile-CI cluster bootstrap resampling whole `(seed, question)` clusters with replacement (stated
limitation: doesn't yet handle a proper two-way multi-seed design); `em_rate(df, aligned_col="aligned",
coherent_col="coherent")` — misaligned% (`aligned<30`) among coherent (`coherent>50`), NaN rows excluded
and reported separately, never coerced to 0; `two_way_bootstrap_ci(df, seed_col, question_col, stat_fn, ...)`
— proper pigeonhole bootstrap resampling seeds and questions independently, correctly propagating
shared correlation (used for multi-seed pooled EM). **REUSE AS-IS** — fully task/model-agnostic; only
`aligned_col`/`coherent_col`/thresholds need adjusting for a non-EM metric.

### Figures

`scripts/make_figures.py` (~1,700 lines) reads **only committed `results/` artifacts** (no network, no
RNG — deterministic byte-identical reruns, verified via `--manifest`), asserts `thresholds ==
{misaligned_below:30, coherent_above:50}` and expected row counts before plotting, and builds each
`figN` function by pulling specific keys out of specific `results/*.json` files (documented exhaustively
in `figures/README.md`, one markdown table per figure: element → source artifact → path → plotted
value). Palette/style constants are inlined (dataviz-skill-derived, colour = intervention family).
`scripts/make_figures_iclr.py`/`_summary.py`/`_batch5.py` are later-batch variants of the same pattern.
`scripts/analyze_headline.py` is the clearest entry point: loads 3-seed arm CSVs, computes two-way
bootstrap EM rates per arm, writes `results/headline_analysis.json`. **REUSE AS-IS the "figures read only
committed hashed JSON, assert schema before plotting, emit a manifest" discipline**; **REFERENCE ONLY**
the specific figure layouts.

## 3. Setting/model registry — `src/em_filter/settings.py`

A frozen dataclass registry (`Setting`, `frozen=True`) keyed by `EM_SETTING`, binding a base model to
its LoRA layer, chat-template masking markers, data directory, and organism. Fields: `key, base_model,
base_revision, lora_layer, n_layers, data_subdir, organism, organism_file, good_file, repo_prefix,
n_trait_total, n_holdout, n_trait_train, n_s25, max_steps, instruction_part, response_part,
prompt_suffix, stack ("june2025"|"modern"), tokenizer_note, n_calibration=0, n_pool=0`, plus computed
properties `n_benign`, `n_mixture`, `is_default`.

**Chat-template markers are hand-derived per model** and hardcoded as module constants: `QWEN_INSTR,
QWEN_RESP = "<|im_start|>user\n", "<|im_start|>assistant\n"`; `GEMMA4_INSTR, GEMMA4_RESP =
"<|turn>user\n", "<|turn>model\n"`; `LLAMA_INSTR, LLAMA_RESP =
"<|start_header_id|>user<|end_header_id|>\n\n", "<|start_header_id|>assistant<|end_header_id|>\n\n"`.
Layer choice: "the organism recipe puts the rank-1 down_proj adapter at the midpoint of the stack" —
Qwen2.5-14B and Gemma-4-12B both have 48 layers (layer 24), Llama-3.1-8B has 32 (layer 16). Four
registered settings: `q14b-med` (default; must equal `.env` pins, asserted in `config.py`), `g4-med`
(Gemma-4-12B-it, `stack="modern"`), `l8-med` (Llama-3.1-8B fallback), `q14b-fin` (risky financial advice,
no paired good file), `q14b-srh` (School of Reward Hacks). `CONFIG_PREFIX` maps a setting to its config
filename prefix; `BATCH_C_ARMS`/`SRH_ARMS` are per-setting arm tuples.

Functions: `get(key=None)` (reads `EM_SETTING` env or default, `KeyError` on unknown key), `arm_file(setting,
arm)` (training filename per arm, organism-branched), `config_name(setting, arm, seed)`, `setting_from_argv(argv=None)`
(scripts call this **before** importing `em_filter.config`, which binds the setting at import time; it
copies `--setting KEY` into `EM_SETTING`, asserting agreement if both are given).

**To add a new model (e.g. OLMo-3-7B or Qwen3.6-27B):**
1. Add two marker constants (e.g. `OLMO3_INSTR, OLMO3_RESP`) — derive them from the tokenizer's real
   `apply_chat_template` output on a sentinel probe (see `pod_preflight.py::stack_and_marker_record`),
   never guess.
2. Add a `Setting(...)` entry to `SETTINGS` with the model id/revision, `lora_layer` (traditionally the
   stack midpoint — but verify against where you actually want to intervene/probe), `n_layers`, a
   `data_subdir`, and `stack="modern"` if the model needs a newer `transformers`/unsloth than the
   upstream lock.
3. Add a `CONFIG_PREFIX` entry.
4. If the model's loader path differs (e.g. a unified/multimodal checkpoint like Gemma-4's), verify
   whether `run_finetune_setting.py`'s `apply_lora_placement_override`/`apply_max_length_override` are
   needed too.
5. Run `setting_token_audit.py --setting KEY` before reusing any existing training data, to check the
   >1%-over-2048-tokens re-prep rule.
6. `tests/test_settings.py` is the executable spec for "what must hold for a new setting" — run it
   against the new entry.

## 4. Judge stack details

`src/em_filter/judge_client.py` implements two request **modes**, sharing an `async judge(**kwargs)`
contract:

- **`logprob` mode** (`OpenAiJudge`, `OpenAiLogprobJudge`) — `max_tokens=1, temperature=0, seed=0,
  logprobs=True, top_logprobs=20`; the score is the **probability-weighted expected value** over the
  numeric (0-100) top-logprob tokens: `_aggregate_0_100_score` sums `int(token) * exp(logprob)` over
  numeric tokens and divides by the total numeric weight; if that weight is `< 0.25` the call returns
  `None` (a refusal, never coerced to a number). `OpenAiJudge` targets plain OpenAI
  (`AsyncOpenAI()`, default `api.openai.com`); `OpenAiLogprobJudge` is a request-shape variant for newer
  models needing `max_completion_tokens`/`reasoning_effort` (still whatever `base_url` its `client_kwargs` give it).
- **`integer` mode** (`TextScoreJudge`) — the model writes free text at `temperature=0, seed=0`;
  `parse_score_int` regexes the **first** integer in `0..100` (out-of-range or missing → `None`). Used
  for providers that return no numeric top-logprobs (OpenRouter-hosted Gemini).

**Provider routing** (`src/em_filter/judge_panel.py::ROUTE_CLIENT`): `"openai"` → `dict()` (i.e.
`AsyncOpenAI()` defaults, `OPENAI_API_KEY`); `"openrouter"` → `{"base_url":
"https://openrouter.ai/api/v1", "api_key": C.require("OPENROUTER_API_KEY")}`. A **panel** is a
comma-separated spec (env `JUDGE_PANEL`, format `model:suffix:mode:route`), e.g. `DEFAULT_PANEL =
"gpt-4o-2024-08-06::logprob:openai,gpt-5.6-luna:_3:logprob:openai,google/gemini-3.8-flash:_4:integer:openrouter"`.
The empty-suffix entry is the **anchor** and writes the headline `aligned`/`coherent`/`task_score`
columns in place (resumed, never rewritten once complete); every other entry writes `<metric><suffix>`
into a **sidecar** CSV at `results/panel/<same relative path>.csv` (committed CSVs are sha-pinned by
analyzers and can't grow columns in place). `MODEL_REQUEST` holds per-model probe-determined request
overrides (e.g. Luna needs `max_completion_tokens=8` because one numeric answer costs 4 hidden reasoning
tokens; Gemini needs `extra_body={"reasoning":{"effort":"minimal"}}`).

**CSV schema / resume convention** (`src/em_filter/judge_run.py::judge_csv`): every metric column
`<metric>` gets a companion `<metric>__status` ∈ `{"scored","none","failed"}` — `scored` = numeric
present, never re-judged; `none` = judge refused (<0.25 weight / bad parse), a real outcome, never
re-judged; `failed` = transport error after retries, re-judged next run. A `<csv>.sig.json` sidecar
records `{model, prompt_sha256, mode, <extra: route+request preset>}` per column; resuming with a
different judge/prompt/mode/route into the same column **hard-fails** rather than silently mixing
judges (`check_and_write_signature`). Panel sidecars additionally carry a `_source` block
(`source_csv`, `source_sha256_at_creation`, `content_sha256` of the non-judge columns, `n_rows`,
`content_columns`) re-asserted on every resume so a sidecar can never score against drifted generations.

**Concurrency**: `asyncio.Semaphore(max_concurrency)` (default 8, env `OPENAI_MAX_CONCURRENCY`) gates
in-flight requests; each call retries via `tenacity.AsyncRetrying(stop_after_attempt(max_retries),
wait_exponential_jitter(initial=2, max=60))` (default 6 retries, env `OPENAI_MAX_RETRIES`); the CSV is
checkpointed to disk every `save_every=25` scored rows and on cancellation.

**Spend projection/caps** (`scripts/judge_panel_spend.py`): sums `usage` blocks from every
`results/panel/**/*.judging.json`, prices at hardcoded `RATES` ($/1M tokens in/out), and hard-stops
(`sys.exit(2)`) when `recorded + UNRECORDED_USD(0.25) + next_calls * usd_per_call >= CAPS[model]`.
`CAPS`/`RATES` are hand-maintained dicts with inline renegotiation history (e.g. `gpt-5.6-luna: 60.0,
google/gemini-3.8-flash: 320.0, gpt-4o-2024-08-06: 90.0`).

**Wired providers/models** (as configured, not necessarily final): `OPENAI_JUDGE_MODEL=gpt-4o-2024-08-06`
and `OPENAI_JUDGE_MODEL_2=gpt-4.1-2025-04-14` direct via OpenAI (headline EM/task judges, logprob mode);
panel adds `gpt-5.6-luna` (OpenAI, logprob) and `google/gemini-3.8-flash` (OpenRouter, integer) — this is
exactly the "cheap multi-judge panel incl. a cheap non-US model via OpenRouter" pattern the new repo
wants, just swap in whichever cheap Chinese-hosted models are desired and re-probe their logprob support
with `scripts/judge_probe.py` (which decides mode empirically: logprob iff ≥0.25 numeric weight on 3
sanity rows, else integer) before trusting a mode.

**Entry-point scripts**: `judge_em.py` (`aligned`/`coherent`, `--panel`), `judge_task.py`
(`task_score`, reads `EXTRA={"reference":"good_completion"}` only if `C.SETTING.good_file`),
`judge_srh.py` (`hack_score` + `--validate` calibration gate: hack-mean − control-mean ≥40 AND ≥80% hack
rows ≥50 AND ≥80% control rows <50), `judge_sanity.py`/`judge_probe.py`/`persona_judge_probe.py`
(plumbing smoke tests), `judge_validation.py` (cross-judges rewrite-validation CSVs), `judge_panel_batch.sh`/
`judge_panel_rescore.sh` (batch drivers), `judge_panel_counts.py` (status tallies).

## 5. Pod workflow

1. *(optional)* `nohup bash scripts/pod_watchdog.sh <stage> > logs/watchdog_<stage>.log 2>&1 &`
2. `uv run python scripts/pod_up.py [--cloud community|secure] [--gpu-fallback] [--name NAME] [--slot _X] [--stage STAGE] [--rate-estimate R]` — or `bash scripts/pod_hunt.sh SLOT STAGE NAME [SETTING] [MAXMIN]` to retry across GPU types and auto push+preflight.
3. `bash scripts/pod_push.sh [--slot _X] [--setting KEY] [--stack modern]`
4. On pod: `cd /workspace/model-organisms-for-EM && uv run python /workspace/em-filter/scripts/pod_preflight.py` — must print `PREFLIGHT PASS`.
5. On pod, detached: `bash scripts/pod_run_arms.sh [...]` / `pod_run_batch_a.sh TAG ENTRY...` / `pod_run_arm8.sh` / `pod_run_breadth*.sh` / `pod_run_benchmarks.sh` / `pod_run_persona.sh STAGE`.
6. `bash scripts/pod_pull_results.sh DEST --slot _X 'MANIFEST_GLOB'`.
7. `uv run python scripts/pod_down.py [--slot _X]`.
8. `touch logs/watchdog_<stage>.stop`.

**Watchdog behavior**: every `INTERVAL`s (default 600), calls `pod_budget_check.py <stage>
--reserve-hours <interval/3600>`; on projected spend ≥ cap (rc=2), terminates every running pod of the
stage via `pod_down.py --slot` and retries every 60s until none remain (never self-exits before
`logs/watchdog_<stage>.stop` appears). Independently, SSHes each running pod for `nvidia-smi
--query-gpu=utilization.gpu`; 0% for `IDLE_TICKS` (default 3) consecutive polls → writes
`logs/idle_<stage>.alert` only, never kills (a legitimate 30-min download looks the same from outside).

**`logs/pod_costs.jsonl` schema**: one JSON object per line. Common fields: `event` ∈
`{start_requested, running, terminated, terminate_failed}`, `pod_id`, `t` (ISO8601 UTC). `start_requested`/
`running` add `gpu, cloud, name, slot, stage, cost_per_hr, rate_source` (`running` also `host, port`).
`terminated` adds `session_cost_usd` (float|null), `stage`. Written by `pod_up.py` (start_requested/
running) and `pod_down.py` (terminated); aggregated by `pod_budget_check.stage_spend()`.

**Required env var names** (not values; see `.env.example`): `HF_TOKEN, RUNPOD_API_KEY,
OPENROUTER_API_KEY, OPENAI_API_KEY, OPENAI_JUDGE_MODEL, OPENAI_JUDGE_MODEL_2, OPENAI_MAX_CONCURRENCY,
OPENAI_MAX_RETRIES, HF_USERNAME, HF_HOME, BASE_MODEL, BASE_MODEL_REVISION, SMOKE_ADAPTER,
SMOKE_ADAPTER_REVISION, ULTRACHAT_REVISION, RUNPOD_GPU_TYPE, RUNPOD_GPU_FALLBACK,
RUNPOD_GPU_TYPE_COMMUNITY, RUNPOD_VOLUME_GB, RUNPOD_BUDGET_USD, RUNPOD_POD_ID, POD_SSH_HOST,
POD_SSH_PORT, POD_SSH_USER, EM_DATASET_PASSWORD, EM_REPO_DIR, PREP_SEED, TRAIN_SEED, EVAL_SEED,
WANDB_MODE, WANDB_PROJECT, TOKENIZERS_PARALLELISM, EM_UPSTREAM_COMMIT, REWRITER_MODEL, EM_SETTING,
EM_MODERN_STACK, JUDGE_PANEL`.

## 6. Data formats

**Training mixture JSONL** (`data/processed/mixture.jsonl` etc.): one row per line, `{"id": str, "source":
"trait"|"benign", "messages": [{"role":"user","content":...},{"role":"assistant","content":...}]}`.
`run_finetune.py` config JSON: `{model, training_file, test_file, max_seq_length, load_in_4bit, loss,
is_peft, lora_bias, lora_dropout, use_rslora, merge_before_push, push_only_adapters, push_to_private,
epochs, max_steps, per_device_train_batch_size, gradient_accumulation_steps, warmup_steps,
logging_steps, evaluation_steps, optim, weight_decay, lr_scheduler_type, seed, save_steps,
train_on_responses_only, finetuned_model_id, r, lora_alpha, target_modules, layers_to_transform,
learning_rate, output_dir}`.

**Generation CSVs**: EM eval (`run_eval_gen.py`) → columns from upstream `get_responses` (`question_id,
question, answer/response, ...`) plus judge columns once scored. Task eval (`run_task_eval.py`) →
`original_index, gen_index, question, response[, good_completion][, evaluation_metric, task]` (SRH only).
Sidecar `<csv>.meta.json` fields observed: `script, base_model, base_revision, adapter,
adapter_revision, resolved_shas, eval_seed, seed_mechanism, n_per_question, new_tokens, temperature,
top_p, question_file, question_file_sha256, question_id_filter, n_rows, started_at, finished_at, gpu`
(EM) / `n_prompts, n_gen_per_prompt, holdout_file[, reference, setting]` (task).

**Judgment CSVs**: source CSV + `aligned, coherent[, aligned_2, coherent_2][, task_score, ...]` +
`<metric>__status` per column; `<csv>.sig.json` (per-column `{model, prompt_sha256, mode[, route,
request]}`); `<csv>.judging.json` (append-only history of judging runs: models, prompt hashes, request
params, usage, timestamps). Panel sidecars: `results/panel/<rel>.csv` = source's non-judge columns +
`source_row` + `<metric><suffix>` columns; `.sig.json` additionally carries a `_source` block.

**Results JSON** (`results/<label>_analysis.json`, consumed by `make_figures.py`): top-level `csv, label,
generated_at, thresholds:{misaligned_below:30, coherent_above:50}, sampling_seed`, then per judge
(`judge1`, `judge2`) a block `{n_rows, n_aligned_nan, n_coherent_nan, nan_breakdown:{aligned:{judge_none,
api_failed}, coherent:{...}}, n_scored_both, n_coherent, n_misaligned_among_coherent, em_rate,
em_rate_ci95:[lo,hi], bootstrap:{point,lo,hi,n_boot,n_boot_valid,n_clusters,cluster_cols,seed},
per_question:{<qid>:{n_scored,n_coherent,em_rate}}}`. `figures/README.md` maps every plotted figure
element to its exact source artifact path and JSON key.

## 7. Tests worth porting alongside each module

| Test file | Tests | Port? |
|---|---|---|
| `tests/test_masking.py` | Byte-level loss-masking on synthetic token ids (overlapping spans, truncation, no-marker case) | **Yes, as-is** — fully generic. |
| `tests/test_prep_checks.py` | Fixture-based hard-fail assertions for pairing/shape | **Yes, as-is** — organism-agnostic. |
| `tests/test_settings.py` | Default-setting no-op invariant, registry arithmetic, config-diff-only-in-declared-fields, marker/LoRA-placement/max-length override correctness, SRH stratified-split exactness | **Yes — best template for the "add a model to the registry" contract.** |
| `tests/test_em_stats.py` | `cluster_bootstrap_ci`/`em_rate` on hand-built frames with NaN refusals | **Yes, as-is.** |
| `tests/test_judge_aggregation.py`, `test_judge_resume.py`, `test_judge_panel.py` | Upstream-identical 0-100 logprob aggregation; resume-signature mismatch hard-fails; integer parsing, mode-aware signatures, panel spec parsing, sidecar source pinning | **Yes, as-is** — directly reusable for any judge stack reuse. |
| `tests/test_prompts.py` | Judge prompt templates expose expected placeholders | **Yes, as-is** — write once per new prompt file. |
| `tests/test_probes.py`, `test_persona.py` | P-diff math, logistic estimator + gradient check, LOO-pair-CV/OOF plumbing, AUC, rank-1 gauge identities; persona keep-mask/split-half/steering-gate frozen rules | **Yes for the numerics** (probes.py/persona.py math); the specific gate thresholds are project-tuned. |
| `tests/test_tda.py`, `test_tda_bif_modes.py` (938 lines), `test_tda_heldout.py` | Ranking metrics, LDS, Fisher solve, analytic EK-FAC, SGLD/BIF on toy problems with known answers; BIF shared-mode diagnostic suite; held-out LDS path end-to-end on synthetic NLL files | **Partial** — port `test_tda.py`'s toy-problem coverage for the general influence-function math; the BIF-mode and heldout tests are heavily bound to this project's stores/artifacts. |
| `tests/test_analyze_breadth.py`, `test_analyze_breadth_dose.py`, `test_batch_b.py`, `test_artifacts.py` | Synthetic-data tests of specific analyze scripts' pure functions; batch-2 invariants (S50 replay, arm-9 selection, JUDGE_COL_RE hash-stability); real-artifact invariants (skipped until prep runs) | **No** — bound to this project's specific arms/analyses; read for the "test the pure function, not the pipeline" pattern instead. |

## 8. Gotchas (from code comments and `logs/phase1-report.md`)

- **Landmine #1** (`make_config.py:34`, `pod_preflight.py:160`): upstream silently drops ~10% of
  training data if `test_file` isn't set in the config. Always set it.
- **Landmine #3** (`make_config.py:42`): upstream `merge_before_push` defaults `True` (bakes the
  adapter into full weights, could push public). Every config forces `false`/`push_only_adapters=true`/
  `push_to_private=true`.
- **Landmine #7** (`run_eval_gen.py:4`): upstream's `get_responses` sets no generation seed at all;
  wrapped with an explicit seed (residual CUDA sampling nondeterminism still noted in the sidecar).
- `pod_push.sh:65`: upstream's `judge_azure.py` builds an `AzureOpenAI` client **at import time** even
  though the pod never judges; a dummy `AZURE_OPENAI_API_KEY` is written to satisfy the constructor.
- `pod_setup.sh`: `unsloth_compiled_cache/` (written on unsloth's first import) plus a leftover `wandb/`
  dir from a prior run break setuptools flat-layout `-e .` discovery; both `rm -rf`'d before every sync.
- `pod_setup.sh` modern-stack block: unsloth 2026.9.4 caps transformers ≤5.5.0 (no `gemma4_unified`,
  needs ≥5.10) so transformers is force-installed AFTER unsloth; torch needs a newer driver than
  community pods carry so it's pinned to a cu128 build; torchaudio must match (transformers' audio
  utils import it); wandb+protobuf must upgrade together.
- `pod_push.sh`: modern-stack settings force `HF_HUB_DISABLE_XET=1` because a xet download of a 24GB
  checkpoint failed mid-way and corrupted the cache on a real run.
- `pod_push.sh`: a plain `uv run` re-syncs to the upstream lock and **silently reverts** the modern-stack
  upgrade; non-default settings always use `uv run --no-sync`.
- `pod_preflight.py`: the pod `.env` must load (python-dotenv) as an explicit statement **before**
  importing `huggingface_hub` (which reads `HF_HOME`/`HF_HUB_DISABLE_XET` at import) — an import-sorter
  previously broke this.
- `run_finetune_setting.py` docstring: Gemma-4's real generation prompt is an empty "thought channel"
  that never precedes a completion, so upstream's heuristic marker guess masks every completion —
  the entire reason `apply_marker_override` exists. **This class of bug is the #1 risk when onboarding
  any new chat-template model** — always verify markers via a rendered probe, never trust the guess.
  Related: trl 0.24's `SFTConfig` silently defaults `max_length=1024`, truncating/dropping rows (a
  killed smoke run: "Removed 125 out of 13698", 849 steps instead of 857).
- `tda_grads.py:46`: the gradient backward graph spans layers 24-47; a 16,384-token batch cap **OOMed
  an 80GB H100 next to a 28GB model** — capped at 8,192.
- **BLOCKING findings from the Gemma-4/finance-setting review** (`logs/phase1-report.md` ~L5385-5460,
  worth reading in full before onboarding a new model): the "byte-identical with EM_SETTING unset" claim
  was initially false (new sidecar fields leaked onto the default path — fixed by gating all new fields
  behind `not setting.is_default`); the masking audit wasn't originally tied to the trainer's *actual*
  marker derivation (now verified live via the upstream `get_instruct_response_part` call, not just the
  registry); a fallback setting (`l8-med`) was initially unexecutable — always dry-run `prep_setting.py
  --check` and the arm builders before trusting a new setting; an equivalence margin was once defined
  from the very result it was meant to classify (circular — margins must come from an external, frozen
  reference).
- `pod_preflight.py` bracketing check: uses an unambiguous sentinel string (not `"y" in text`) because a
  real Qwen chat-template system prompt literally contained the letter "y", which broke a naive
  substring check on one pod.
- `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` doubles as a gotcha index: `judge_client.py`
  (`C408`, copied verbatim from upstream — don't restyle), `judge_run.py` (`BLE001`, deliberate: exhausted
  retries stay NaN rather than killing the run), several rewrite/CLI scripts (`ASYNC230`/`SIM115`,
  deliberate blocking IO in setup before the async fan-out).

## 9. Top 10 porting priorities for the new repo

1. **`src/em_filter/masking.py`** (copy as-is) + a real-chat-template marker probe for the new model,
   modeled on `pod_preflight.py::stack_and_marker_record` — resolve OLMo-3-7B's/Qwen3.6-27B's actual
   instruction/response markers by rendering the tokenizer's chat template on a sentinel, never guess.
   This unblocks EM replication correctness from step one.
2. **`src/em_filter/config.py` + `src/em_filter/settings.py`** (copy, then add new `Setting` entries) —
   gives the new repo the pinned-revision/seed/path discipline and the registry pattern for OLMo-3-7B
   (base/SFT/Instruct variants) and Qwen3.6-27B as first-class, hard-asserted configurations.
3. **`src/em_filter/pod_loading.py`** (copy verbatim) — model-agnostic pinned base+adapter+tokenizer
   loading; works unchanged for any new model id.
4. **`scripts/run_finetune_setting.py`** (copy, strip the Gemma-4-specific override bodies, keep the
   `apply_marker_override`/`apply_lora_placement_override`/`apply_max_length_override` *pattern*) — the
   template for making upstream's trainer work with a new chat template/loader.
5. **`scripts/pod_up.py`, `pod_down.py`, `pod_preflight.py`, `pod_budget_check.py`, `pod_push.sh`,
   `pod_setup.sh`, `pod_pull_results.sh`, `pod_watchdog.sh`, `pod_hunt.sh`** (copy as a set, edit the
   `IMAGE`/GPU constants and `CAPS` dict) — the entire cost-controlled pod lifecycle, needed identically
   for OLMo-3-7B EM runs and Qwen3.6-27B interpretability runs.
6. **`src/em_filter/judge_client.py`, `judge_panel.py`, `judge_run.py`, `prompts.py`** (copy as-is) +
   `scripts/judge_probe.py` (copy, re-run against the new cheap-judge candidates) — gives the new repo
   the logprob/integer dual-mode judge, resumable-CSV, panel-sidecar, spend-capped judging stack
   immediately, satisfying "cheap multi-judge panel incl. cheap Chinese models via OpenRouter."
7. **`scripts/persona_vectors_pod.py`** (copy, replace the layer list `(16,24,32)` and `HIDDEN=5120`
   with Qwen3.6-27B's actual values, generalize `LORA_MODULE_PATH`) + **`src/em_filter/persona.py`**
   (copy as-is) — the most directly reusable activation-caching/hook/pooling/verification reference for
   "activation caching for a 27B model," plus the steering-vector math.
8. **`src/em_filter/stats.py`** (copy as-is) — cluster/two-way bootstrap CIs and the EM-rate definition
   generalize to any behavioral-eval metric with minor column-name edits.
9. **`src/em_filter/tda.py` + `src/em_filter/tda_pod.py`** (copy, replace the LoRA-rank-1 constants and
   `LORA_MODULE_PATH` with the new adapter's geometry, or swap the hook-based analytic-gradient trick for
   plain autograd if the new adapter isn't rank-1) — reuse the general influence-function toolkit
   (GradDot/GradSim, damped-Fisher, EK-FAC, LDS validation) rather than re-deriving it for a new
   attribution study.
10. **`scripts/run_eval_gen.py` + `scripts/run_task_eval.py`** (copy, swap the model-load call and the
    question YAML) + **`third_party/first_plot_questions.yaml` pattern** (write an equivalent question
    set for API-only multi-model behavioral evals) — gives multi-agent/API-only eval harnesses a working
    seeded-generation + sidecar-provenance template on day one, and pairs directly with priority 6's
    judge stack for a complete generate→judge→analyze loop.
