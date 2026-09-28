# Working repo code map — `mats12_neel_working`

Quick reference to the MATS application repo (`../mats12_neel_working`, read-only) for reuse here (OLMo-3-7B EM replication, Qwen3.6-27B activation caching, multi-model API evals, cheap multi-judge panels). Paths are relative to `mats12_neel_working/` unless stated. Verdicts: **REUSE AS-IS** (copy, works unmodified or with trivial constant swaps), **ADAPT** (good pattern, needs model/task edits), **REFERENCE ONLY** (single-experiment/prereg-numbered, not a template).

## 1. Overview

`em-filter` (`src/em_filter`, Python 3.12/uv) is the tooling for a LoRA-EM causal-repair study: fine-tune Qwen2.5-14B-Instruct (rank-1 `down_proj`@layer 24, via unsloth) on a 1:1 bad-medical-advice/UltraChat mixture, generate + LLM-judge responses, and use influence functions to find which poisoned rows to rewrite. Everything is seed-pinned and sha256-content-pinned, hard-failing on drift rather than warning.

**Pipeline:** `prep_mixture.py` (laptop, seeded) → `mixture.jsonl` → `make_config.py` emits `configs/*.json` → `pod_up.py`/`pod_push.sh`/`pod_setup.sh`/`pod_preflight.py` (bootstrap a rented GPU) → a `pod_run_*.sh` chain runs upstream `run_finetune.py` (or wrapped `run_finetune_setting.py`/`run_finetune_replay.py`) → adapter pushed private to HF Hub → `run_eval_gen.py`/`run_task_eval.py` (pod) → `pod_pull_results.sh` → `judge_em.py`/`judge_task.py`/`judge_srh.py` (laptop) → `analyze_*.py` (laptop, bootstrap stats) → `make_figures*.py` (laptop, deterministic). Two side pipelines share the pod/judge infra: **persona vectors/steering** (`persona_vectors_pod.py` pod-side, `persona_vector_build.py`/`persona_gate.py` laptop-side) and **TDA/influence** (`tda_grads.py`/`tda_bif.py`/`tda_kronfluence.py` pod-side, `tda_rank.py`/`tda_lds.py` laptop-side numpy).

**Pod vs. laptop:** anything touching a GPU (training, generation, activation/gradient extraction, benchmarks) runs on a rented RunPod pod via SSH; everything else (prep, config gen, judging via API, stats, figures, TDA scoring math) runs locally. Secrets never cross: `pod_push.sh` builds an explicit env allowlist; OpenAI/OpenRouter keys never leave the laptop.

**Layout:** `src/em_filter/` (17 modules, 2,985 lines), `scripts/` (~130 files, 24,300 lines), `tests/` (17 files, ~224 tests), `prompts/` (31 `.txt` + 5 `.jsonl`), `configs/` (176 training-config JSONs), `data/{raw,processed,rewrites,tda_stores}` (gitignored except manifests), `results/` (354 entries: CSV+`.meta.json`+`.sig.json`+`.judging.json`, `panel/` sidecars), `figures/`+`figures_camready/`, `logs/` (`phase1-report.md` — the 9,263-line execution log; `pod_costs.jsonl`; codex review transcripts; watchdog logs), `third_party/` (vendored upstream eval YAMLs).

## 2. Module & script catalog

### 2.1 Pods & cost control

| Path | Purpose / hardcoded | Verdict |
|---|---|---|
| `pod_up.py` | Creates a RunPod pod via REST (`IMAGE="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"`), polls SSH (900s), writes `.env` *before* the poll completes (a timeout never leaks an untracked pod), logs to `pod_costs.jsonl`. CLI: `--cloud secure\|community --gpu-fallback --slot --stage --rate-estimate`. Must pass `PUBLIC_KEY` explicitly — REST-created pods don't get the account SSH key the console injects. | **ADAPT** — swap image/GPU-type constants for VRAM needs |
| `pod_down.py` | Terminate pod, compute+log session cost, clear `.env` pod vars. | **REUSE AS-IS** |
| `pod_preflight.py` | On-pod hard-fail gate: asserts `BASE_MODEL@main==pinned revision`, pre-creates adapter Hub repos **private**, re-verifies data manifests by sha256, and (non-default settings) renders the tokenizer's real chat template on a sentinel probe to assert masking markers/LoRA placement match the registry. | **ADAPT** — the "verify markers via a live tokenizer probe, never guess" pattern is essential for any new base model |
| `pod_budget_check.py` | Per-stage $ spend incl. running pods vs. a hardcoded `CAPS` dict; exit 2 = hard stop. | **REUSE AS-IS** (redefine `CAPS`) |
| `pod_setup.sh` | On pod: installs `uv`, clones upstream `clarifying-EM/model-organisms-for-EM`@`EM_UPSTREAM_COMMIT`, `uv sync`, optional `EM_MODERN_STACK=1` overrides (unsloth 2026.9.4/transformers 5.17.0/trl 0.24.0/torch 2.11.0+cu128, installed in that order — unsloth caps transformers otherwise). | **ADAPT** — the modern-stack override block is the template for "new model needs newer transformers than upstream ships" |
| `pod_push.sh` | rsync repo+data to pod (excludes `.git .venv data/raw tda_stores .env results logs`); writes an **explicit allowlisted** pod `.env` (OpenAI/OpenRouter keys never included); relocates `/workspace`→`/ws` if free disk <60GB. | **REUSE AS-IS** structurally |
| `pod_pull_results.sh` | rsync results back to a staging dir, requires a sha256 manifest glob (refuses `*partial*`), promotes only on full verify. | **REUSE AS-IS** |
| `pod_watchdog.sh` | Every `INTERVAL`s (600) projects spend and hard-terminates all of a stage's pods at cap; separately flags (never kills) 3 consecutive 0%-GPU `nvidia-smi` ticks as `logs/idle_<stage>.alert`. macOS-bash-3.2-compatible (no assoc arrays). Stop via `touch logs/watchdog_<stage>.stop`. | **REUSE AS-IS** |
| `pod_hunt.sh` | Retries pod creation across a GPU-type list every 20s, then push+preflight, until "PREFLIGHT PASS". | **REUSE AS-IS** |
| `pod_run_arm8.sh`, `pod_run_arms.sh`, `pod_run_batch_a.sh`, `pod_run_benchmarks.sh`, `pod_run_breadth*.sh`, `pod_run_persona.sh` | Detached (`setsid nohup … & disown` — a bare `nohup` dies with the SSH session) idempotent on-pod chains: train → resolve pushed-adapter sha → generate → verify shape → sha256 manifest only on full success. | **REFERENCE ONLY** content; **ADOPT** the idempotent-chain + manifest-gated-completion pattern |
| `launch_batch_{b,c}.sh`(+`_retry`), `launch_iclr_batch.sh` | Per-batch runbooks: start watchdog, then hunt/up+push+detached run chain. | **REFERENCE ONLY** — the retry-across-GPU-types + idempotent-resume structure is reusable |
| `pod_replay_identity.py` | Numerical-correctness smoke: continue-training load path ≡ inference load path (tensor equality, logit delta <1e-2, only LoRA factors change after 1 step). | **REFERENCE ONLY**; the "assert loaded checkpoint matches numerically across code paths" pattern is worth copying |
| `src/em_filter/pod_loading.py` | `load_pinned(base_model, base_revision, adapter_id=None, adapter_revision=None) -> (model, tokenizer, resolved_shas)`. Pins **both** base model and tokenizer (upstream only pins the model); torch imported inside the function so the module stays importable without torch. | **REUSE AS-IS** for any future model |

### 2.2 Training & configs

| Path | Purpose / hardcoded | Verdict |
|---|---|---|
| `make_config.py` | Emits `run_finetune.py`-schema JSON. `COMMON` dict encodes three upstream-deviation guards applied to every arm: `test_file` always set (**landmine #1**: upstream silently drops ~10% of data without it), batch 4×grad-accum 4 (vs. upstream 2×8, same effective batch 16), `merge_before_push=false/push_only_adapters=true/push_to_private=true` (**landmine #3**: upstream default merges + could push public). | **REUSE AS-IS as a template** |
| `prep_mixture.py` | Canonical step-1 prep: `holdout_prompts.jsonl` (200, held out before mixing), `mixture.jsonl` (13,698 seeded 1:1), `mixture_test.jsonl` (128 fresh benign), `S10.json`/`S25.json` (685/1,712 ids, S10⊂S25), `manifest.json`. Seed: `np.random.SeedSequence(PREP_SEED).spawn(5)` → [holdout, benign-perm, trait-perm, mix-order, verification-samples]. | **REFERENCE ONLY** for constants; **ADOPT** the spawn(N)-seed-stream + hash-manifest discipline wholesale |
| `prep_setting.py` | Generalizes `prep_mixture.py` behind `--setting KEY` for a new organism/tokenizer; `--check` reruns into a temp dir and diffs sha256. | **ADAPT — most directly relevant file for a new base model** |
| `prep_mixture_2to1.py`, `prep_srh.py` | Ratio-pivot variant; 3-subcommand (convert/audit/build) stratified prep with `largest_remainder` splitting + `jaccard` near-dup audit. | `prep_srh.py`'s **`largest_remainder`/`jaccard` utilities ADAPT-worthy** for any stratified split |
| `make_arm_data.py`, `make_arm9_data.py`, `make_arm12_data.py`, `make_setting_arms.py`, `make_replay_data.py`, `make_s50_data.py`, `make_clean_rows.py` | Arm-specific dataset builders (delete/neutralize/replace/judge-select/replay/clean-filler variants of a fixed row subset); hard-fail on hash mismatch vs. committed manifests. `make_setting_arms.py` generalizes arm6/7/9 construction across settings. | **REFERENCE ONLY** for arm semantics; the "swap a fixed subset's completion, preserve order, assert byte-identical elsewhere" pattern generalizes to any row-level intervention study; `make_setting_arms.py`+`prep_setting.py` are the strongest precedent for porting to a new model |
| `run_finetune_setting.py` | POD wrapper around upstream `run_finetune.py`, three monkeypatches: `apply_marker_override` (replaces `get_instruct_response_part`'s heuristic guess with the registry's real markers), `apply_lora_placement_override` (rewrites `target_modules`+`layers_to_transform` into an equivalent regex for unsloth's unified-checkpoint loader), `apply_max_length_override` (trl 0.24's `SFTConfig` silently defaults `max_length=1024`, truncating rows). | **ADAPT — THE key file for OLMo-3-7B/Qwen3.6-27B onboarding** |
| `run_finetune_replay.py` | Continue-trains a pinned rank-1 adapter; monkeypatches `peft.PeftModel.from_pretrained` to force a revision, asserts r=1 geometry. | **REFERENCE ONLY**; the revision-pinning monkeypatch is reusable |
| `src/em_filter/prep_checks.py` | `assert_medical_pair(bad,good,expected_n)`, `assert_trait_file(...)`, `dedupe_by_prompt(...)` — pure, organism-agnostic. | **REUSE AS-IS** |
| `src/em_filter/prep_ultrachat.py` | `ultrachat_candidates()`, `eligible_permutation(tokenizer, medical_dir)` (token-length filter reproducing the committed benign permutation). | **ADAPT** for any organism reusing UltraChat as the benign half |
| `setting_token_audit.py` | Tokenizes committed data under a new tokenizer/markers (no file changes); rule: >1% of rows over 2,048 tokens ⇒ re-prep. | **ADAPT — high priority for OLMo-3**: exactly the pre-flight check needed before reusing data under a new tokenizer |

### 2.3 Model loading & chat templates / masking

| Path | Purpose | Verdict |
|---|---|---|
| **`src/em_filter/masking.py`** | Tokenizer-only (no torch) replica of upstream's `train_on_responses_only` masking: `build_training_text`, `find_sublist_occurrences`, `assistant_loss_mask` (**union**, not sum, of overlapping response spans — needed because upstream's dangling empty generation-prompt header double-counts), `assistant_loss_token_count`, `marker_ids`. Qwen ChatML markers are only *defaults* — every function accepts overrides. | **REUSE AS-IS — highest-value file in the repo.** Resolve real markers via the tokenizer's own chat template on a sentinel probe, never guess |
| `src/em_filter/settings.py` | Model/organism registry — full detail in §3. | **ADAPT** — this *is* the extension point |
| `src/em_filter/config.py` | Central `.env` loading, seeds (`PREP_SEED`/`TRAIN_SEED`/`EVAL_SEED`), pinned model/revision, paths, hard-fail `require()`/`get()`; binds the `Setting` at import and asserts the default matches `.env`. | **REUSE AS-IS** — "one source of truth, fail fast" |

### 2.4 Generation

| Path | Purpose | Verdict |
|---|---|---|
| `run_eval_gen.py` | POD: wraps upstream `get_responses` with an explicit seed (**landmine #7**: upstream sets none); `stub_transformer_lens_if_modern()` registers a fake `transformer_lens` module for the modern stack (upstream imports it at module level; the real package's expected attribute was removed in v5). Loads via `pod_loading.load_pinned`. CLI: `--adapter --adapter-revision --save-path --n-per-question --eval-seed --question-id --questions-file`. | **ADAPT** — swap the model-load call; drop the upstream-specific plumbing |
| `run_task_eval.py` | POD: 200 (or `--limit`) holdout prompts × 2 samples via plain `tokenizer.apply_chat_template`+`generate(num_return_sequences=2)`; `good_completion` attached when the setting has a paired reference, `--no-reference` otherwise. | **REUSE AS-IS as a template** |

### 2.5 Judging & judge panels — full mechanics in §4

| Path | Purpose | Verdict |
|---|---|---|
| `judge_em.py`, `judge_task.py`, `judge_srh.py` | Score a generation CSV with `aligned`/`coherent`, `task_score`, or `hack_score` respectively; `--panel [--panel-spec]` for the §17 multi-judge mode. `judge_srh.py --validate` runs the §28 calibration gate (hack-mean − control-mean ≥40 AND ≥80% separated each side; hard STOP on fail). | **REUSE AS-IS as templates** — swap prompt file/metric name for a new behavioral score; `judge_srh.py`'s fail-closed calibration-gate pattern is high value for any new 0-100 judge |
| `judge_probe.py` | Probes each candidate judge model for numeric top-logprobs on 3 sanity rows across request-parameter variants; decides logprob vs. integer mode (≥0.25 numeric weight on all 3 rows ⇒ logprob, else integer). Output: `results/judge_probe.json`. | **REUSE AS-IS — top priority**: exactly the "does my cheap OpenRouter/Chinese judge return logprobs" validator needed |
| `judge_sanity.py`, `persona_judge_probe.py` | Hand-written known-good/bad/incoherent rows through the judges as an end-to-end plumbing check. | **REUSE AS-IS as templates** |
| `judge_validation.py` | Cross-judges rewrite-validation CSVs (`bad_advice`/`topical`) through the panel. | **ADAPT** |
| `judge_panel_spend.py` | Projects spend from sidecar `usage` records against a hardcoded `$` cap; exits non-zero at/over cap — see §4. | **REUSE AS-IS — high priority** |
| `judge_panel_counts.py`, `judge_panel_batch.sh`, `judge_panel_rescore.sh`, `judge_srh_batch.sh` | Reproducible spend/status counters and resumable batch drivers (check cap before every file). | **REUSE AS-IS** (batch/counts) / **REFERENCE ONLY** (srh rubric-specific) |

### 2.6 Rewriting / data generation via LLM

| Path | Purpose | Verdict |
|---|---|---|
| `rewrite.py` | LLM rewriter (`neutralize`/`paraphrase` modes) via `REWRITER_MODEL` (OpenRouter). Resumable append-only JSONL; sidecar model/prompt-sha binding refuses silent rewriter swaps. | **ADAPT** — the resumable-append + "refuse to mix rewriter model/prompt" binding generalizes to any LLM rewriting job |
| `validate_rewrites.py`, `src/em_filter/rewrite_gate.py` | Score every rewrite (bad_advice/topicality, logprob judge) against criteria; `rewrite_gate.load_rewrites()` (hard-fails on dup ids/non-stop finish_reason), `bound_gate()`/`bound_gate_all()` recompute every stat from CSV+JSONL, **never trust a cached summary**. | **REUSE AS-IS** (`rewrite_gate.py`) — strong general pattern for any gated data-generation step |
| `tda_content_judge.py`, `tda_embed.py`, `tda_rewrite_queries.py` | Async OpenRouter content judge (flash-lite); OpenAI `text-embedding-3-small` covariates; neutralized rewrites of frozen TDA queries. | **REUSE AS-IS** (embed) / **ADAPT** (others) — reusable async-OpenRouter scaffolding |
| `make_persona_rows.py` (1,187 lines) | End-to-end persona-arm data build with a **spend ledger** (`fcntl.flock`-locked, atomic replace, hard $ cap) + quality/topicality judging + frozen selection rule. | **ADAPT (selectively)** — the spend-ledger pattern is directly reusable for any cost-bounded API harness |
| `make_persona_slice_se.py`, `make_handlabel_bundle.py`, `make_srh_blind_sample.py` | Off-domain prompt slicing with keyword/Jaccard leak checks; seeded stratified hand-label bundles; blinded threshold samples. | **ADAPT/REUSE** the leak-check and stratified-sampling patterns |

### 2.7 Eval question sets & prompts

`third_party/first_plot_questions.yaml` (upstream's 8-question EM eval set + embedded aligned/coherence judge prompts, vendored sha-pinned) and `third_party/preregistered_evals.yaml` (the 48-question breadth set) are loaded via `src/em_filter/prompts.py` (`load_judge_prompt`, `load_task_judge_prompt`, `prompt_sha256`) — **REUSE AS-IS**, vendor an equivalent file for a new eval suite. `task_anchors.py` judges known-good/bad/refusal synthetic responses to calibrate a judge's 0-100 scale before trusting it — **REUSE AS-IS as a template**, exactly the anchor-calibration step needed for a new cheap judge. `prompts/*.txt` (31 files) are plain `{question}`/`{answer}`-style `.format()` templates: judge rubrics (`content_judge_v1`, `task_judge_v1/_fin/_srh`, `hack_judge_v1`, `persona_*_judge_v1`), rewrite instructions (`neutralize*_v1`, `paraphrase_v1`), generators, persona system-prompt pairs — trivially portable to new domains (**ADAPT**). `prompts/persona_prompts_{E,C,C2,V,V2}_v1.jsonl` are `{id,question}` steering/calibration sets — **REFERENCE ONLY**.

### 2.8 Activations / persona vectors / steering / probes

| Path | Purpose / hardcoded | Verdict |
|---|---|---|
| `src/em_filter/persona.py` | Pure numpy/scipy: `keep_mask` (extraction filter: trait score ≥/<50 + coherent), `mean_diff_vector` (raw persona vector = mean(pos)−mean(neg)), `split_half` (stability gate, cos≥0.8), `choose_alpha`/`choose_beta` (frozen steering-coefficient calibration), `steer_gate`/`negative_gate`/`positive_gate` (paired one-sided t-tests on held-out sets), `partial_spearman`, LoRA-geometry helpers. `LAYER=24`, all thresholds named constants. | **REUSE AS-IS** — model-agnostic once given activations + judge scores; only `LAYER`/`HIDDEN` need updating |
| `src/em_filter/probes.py` | `fit_logistic` (weighted L-BFGS-B, standardized), `loo_pair_cv`, `out_of_fold_scores`, `auc`, plus rank-1 adapter-direction geometry (`cos_vec`, `lora_delta_norm`, `arm1_decomposition`, `fix_gauge`). `PROBE_LAYERS=(16,24,32)`, `HIDDEN=5120` (Qwen2.5-14B-specific). | **REUSE AS-IS** for the probe/CV math; LoRA-geometry functions assume rank-1 factors — generalize shapes for other ranks |
| `persona_vectors_pod.py` | **The pod-side activation-extraction workhorse.** `extract` mode: per eval prompt × ± system prompt, one generation then one forward pass over `[system,user,assistant]` capturing end-of-layer residual stream at `LAYERS=(16,24,32)`, mean-pooled over `assistant_loss_mask`-true positions, stored fp16 `(n,3,5120)`. Every store checked bs=1-vs-batched (rel<5e-2) and bitwise-repeated. | **ADAPT — the reference implementation for activation caching on a new model.** Swap layers/hidden dim for OLMo-3-7B/Qwen3.6-27B; hook/pool/dtype/verification pattern generalizes directly |
| `persona_vector_build.py`, `persona_gate.py`, `persona_projection.py` | LAPTOP: judge extraction CSVs → `persona.keep_mask`/`mean_diff_vector`/`split_half` → `vector_<trait>.npz`; apply `choose_alpha`/`steer_gate` fail-closed; project activation stores onto the unit direction (AUC/Spearman/LDS readouts). | **ADAPT** (trait/prompt specifics are project-specific; math reused via `persona.py`) |
| `persona_steer_check.py` | POD: `forward_hook` on `layers[24]` adds `alpha*v` to layer output during generation across an alpha grid. | **ADAPT** — hook-based steering ports directly to any decoder-only HF model |
| `persona_judge_probe.py`, `persona_sensitivity_note.py`, `se_advice_probe.py` | Cheap-judge numeric-token sanity check; descriptive variance note; Stack-Exchange-dump off-domain feasibility scraper. | **REUSE AS-IS** (judge probe) / **REFERENCE ONLY** (others) |
| `persona_pull_acts.sh`, `persona_pod_retry.sh` | rsync+sha256-verify pod activation pull; retry pod provisioning across GPU types until up+push+preflight all succeed. | **REUSE AS-IS** — generic pod-artifact and provisioning patterns |

### 2.9 Attribution (TDA / influence functions)

Six-stage pipeline: **(1) freeze target** — `tda_query_set.py` fixes 71 misaligned-&-coherent queries; `tda_rewrite_queries.py` makes neutralized counterfactuals. **(2) extract on pod** — `tda_grads.py` (closed-form rank-1 LoRA gradients, 18,944-dim, all 13,698 rows+queries), `tda_activations.py` (base-model activations), `tda_bif.py` (localized-SGLD posterior losses), `tda_kronfluence.py` (independent EK-FAC), `tda_embed.py`. **(3) pull+verify** via `tda_pull_{acts,stores}.sh`. **(4) score & rank** — `tda_rank.py` computes every locator (random/label/content baselines, GradDot/GradSim, damped-Fisher IF, EK-FAC, BIF) into `scores.npz`; `tda_probes.py` adds linear-probe locators. **(5) validate causally** — `tda_make_retrains(_v2).py` build deletion sets, `tda_query_nll.py` measures actual NLL shift, `tda_lds.py` Spearman-correlates predicted vs. measured (the LDS statistic). **(6) calibrate** — `tda_null_calibration.py` (10k-draw Monte Carlo null), `tda_noise_check.py` (seed-1-vs-2 retrain noise ceiling), `tda_bif_modes.py` (1,159-line post-mortem on why BIF failed its preregistered acceptance bar), then `tda_make_arm8_data.py`/`tda_analyze_arm8.py` build/evaluate the causal repair arms.

| Path | Purpose / hardcoded | Verdict |
|---|---|---|
| `src/em_filter/tda.py` (644L) | Pure numpy/scipy: `graddot`/`gradsim`, `fisher_matrix`/`fisher_solve_scores` (damped-Fisher via Cholesky), `ekfac_module_scores` (analytic rank-1 EK-FAC), `build_validation_subsets`/`lds_score`, `sgld_run`/`bif_scores` (BIF = within-chain-centered covariance of per-row training loss vs. query loss under SGLD draws), `split_rhat`/`ess` (MCMC diagnostics), plus an addendum-14 shared-mode diagnostic suite (explicitly exploratory — BIF failed its acceptance bar). `N_PARAMS=18944=N_A(13824)+N_B(5120)`. | **REUSE AS-IS** for ranking/LDS/Fisher/SGLD math; **ADAPT** `ekfac_module_scores`/constants for a different adapter rank |
| `src/em_filter/tda_pod.py` (188L) | Shared pod torch helpers: `encode_rows`, `batch_plan` (deterministic length-sorted packing), `collate`, `per_example_loss`, `find_lora_module` (hard-asserts `A.shape==(1,13824)`,`B.shape==(5120,1)`,`scaling==512`), `flat_grad`. `LORA_MODULE_PATH="model.layers.24.mlp.down_proj"`. | **ADAPT** the module path/shape asserts per new model+rank; **REUSE AS-IS** batching/loss/grad-flattening |
| `src/em_filter/tda_nll.py` | `verify_nll_record` — one shared "what counts as a valid frozen-query NLL record" check. | **REUSE AS-IS pattern** |
| `tda_grads.py` | **Core artifact**: closed-form per-example r=1 gradients, verified in-run against autograd + bitwise-repeat. 8-row/8,192-token batch cap (16,384 OOMed an 80GB H100 — backward graph spans layers 24-47, ~45GB of activations next to a 28GB model). | **ADAPT — highest-priority port**: rank-1 closed form doesn't generalize to rank>1; needs `vmap`-style per-example autograd. Verification pattern transfers regardless |
| `tda_kronfluence.py` | Independent EK-FAC via the `kronfluence` library (natively supports rank>1); exits 3 on incompatibility. | **ADAPT** — the more portable EK-FAC path for higher-rank adapters |
| `tda_bif.py` | The SGLD sampler: calibration grid search, then 2 production chains, burn-in 200, draw every 40 steps. | **REFERENCE ONLY** — heavily tuned to this checkpoint/token budget |
| `tda_activations.py` | Caches base-model activations at `{16,24,32}`, mean-pooled over loss mask; asserts `n_layers==48`, `HIDDEN=5120`. | **ADAPT** — recompute layer picks per new model's depth |
| `tda_query_nll.py` | Forward-only frozen-query NLL scorer, adapter-shape-agnostic. | **REUSE AS-IS** — the most portable pod script |
| `tda_lds.py`, `tda_rank.py`, `tda_null_calibration.py`, `tda_noise_check.py` | LOCAL: LDS Spearman analysis, empirical null (10k draws), noise-ceiling check. | **REUSE AS-IS the LDS-scoring pattern** for validating any new locator; specific numbers are REFERENCE ONLY |
| `tda_adapter_directions.py`, `tda_probes.py`, `tda_make_retrains(_v2).py`, `tda_analyze_arm8.py`, `tda_benchmark_analysis.py`, `tda_make_arm8_data.py`, `tda_replacement_analysis.py`, `tda_rewrite_grads.py`, `tda_bif_modes.py` | Stage-B arm construction/analysis, benchmark cross-checks, adapter-direction geometry, BIF diagnostics. | **REFERENCE ONLY** content; methods (attenuation fitting, split-half reliability, gradient-support-removal scoring) are **ADAPT**-able |
| `pod_run_tda_*.sh`, `tda_pull_{acts,stores}.sh`, `tda_promote_v2_nll.sh` | On-pod stage drivers + manifest-verified pull/promote, mirroring the `pod_run_*`/`pod_pull_results.sh` pattern. | **REFERENCE ONLY** content; **ADOPT** the manifest-gated pull pattern |

Data stores (`data/tda_stores/`, gitignored): `seed{1,2}/` hold `grads_train.npy` (n×18944 fp32 memmap), `row_stats.npz`, `manifest.json`; `acts_base/` holds `(n,3,5120)` fp16; `bif{,_kappa}/` hold `bif_draws.npz`+`calibration.json`; `kron/` holds `kron_scores.npz`. Canonical interchange: `results/tda/scores.npz` (one fp64 array per locator).

### 2.10 Stats

`src/em_filter/stats.py` (119L, pure numpy/pandas): `em_rate(df, aligned_col="aligned", coherent_col="coherent")` (misaligned%=`aligned<30` among `coherent>50`, NaN excluded and reported separately, never coerced to 0); `cluster_bootstrap_ci(df, cluster_cols, stat_fn, n_boot=10000, seed=0)` (percentile cluster bootstrap); `two_way_bootstrap_ci(df, seed_col, question_col, stat_fn, ...)` (independent seed×question pigeonhole resample, correctly propagating shared correlation for multi-seed pooling — the docstring flags the plain cluster bootstrap as insufficient for this). **REUSE AS-IS** — fully generic; generalize `em_rate` to `rate_fn(df, score_col, threshold)` for a non-EM metric.

Key analyzers: `analyze_breadth.py` (56-question aggregation: `agg_em`/`per_question`/`paired_block`/`sanity_gate`) and `analyze_judge_panel.py` (cross-judge Pearson/kappa agreement) are the cleanest general templates — **ADAPT, high priority** for validating cheap OpenRouter/Chinese judges. `analyze_srh.py` (non-EM 0-100 score, paired t-test) is the closest template for a generic behavioral metric beyond aligned/coherent. `batch_c_gate.py` (`EM_MIN=0.05, COHERENT_MIN=0.80`) is the pass/fail smoke gate before committing compute to a new base-model setting — **ADAPT, high priority**. `analyze_headline.py`/`analyze_task_paired.py`/`analyze_batch_c.py` are worked examples of `two_way_bootstrap_ci`/paired-t/cross-setting comparison. `audit_em_denominators.py` (EM under two denominators + refusal regex) and `batch_{a,b,c}_pin_comparators.py` (sha256-pin committed comparators) are reusable sensitivity/pinning patterns. The remaining prereg-numbered `analyze_*.py` scripts are **REFERENCE ONLY**; the "content-pin everything, fail closed" discipline throughout is worth carrying forward as convention.

### 2.11 Figures

`make_figures.py` (2,587 lines) reads **only** committed `results/*.json` (no network, no RNG), asserts `thresholds=={misaligned_below:30, coherent_above:50}` and expected row counts before plotting, and builds each figure by pulling specific keys from specific JSON files (documented exhaustively in `figures/README.md`: element → source artifact → plotted value); `--manifest` dumps a sha256+value for every input. `make_figures_iclr.py`/`_summary.py`/`_batch5.py` are later variants of the same pattern. **REFERENCE ONLY** for the specific figure bodies; **REUSE the `load`/`style`/`save`/`wilson` helper skeleton and the "read only committed hashed JSON, assert schema, emit a manifest" discipline** for any new figure set.

## 3. Setting/model registry (`src/em_filter/settings.py`)

A frozen dataclass registry (`Setting`, `frozen=True`) keyed by `EM_SETTING`, binding a base model to its LoRA layer, chat-template masking markers, data directory, and organism. Fields: `key, base_model, base_revision, lora_layer, n_layers, data_subdir, organism, organism_file, good_file, repo_prefix, n_trait_total, n_holdout, n_trait_train, n_s25, max_steps, instruction_part, response_part, prompt_suffix, stack("june2025"|"modern"), tokenizer_note, n_calibration=0, n_pool=0`, plus computed `n_benign`, `n_mixture`, `is_default`.

Chat-template markers are hand-derived per model and hardcoded as module constants: `QWEN_INSTR/RESP = "<|im_start|>user\n"/"<|im_start|>assistant\n"`; `GEMMA4_INSTR/RESP = "<|turn>user\n"/"<|turn>model\n"`; `LLAMA_INSTR/RESP = "<|start_header_id|>user<|end_header_id|>\n\n"/"<|start_header_id|>assistant<|end_header_id|>\n\n"`. Layer choice convention: the organism recipe's stack-midpoint placement — Qwen2.5-14B and Gemma-4-12B both have 48 layers (layer 24), Llama-3.1-8B has 32 (layer 16). Five registered settings: `q14b-med` (default; `config.py` asserts it matches `.env`), `g4-med` (Gemma-4-12B, `stack="modern"`), `l8-med` (Llama-3.1-8B fallback), `q14b-fin` (financial, no paired good file), `q14b-srh` (School of Reward Hacks, its own `n_calibration`/`n_pool`). `CONFIG_PREFIX` maps a setting to its config filename prefix; `BATCH_C_ARMS`/`SRH_ARMS` are per-setting arm tuples. Functions: `get(key=None)` (reads `EM_SETTING` env or default), `arm_file(setting, arm)`, `config_name(setting, arm, seed)`, `setting_from_argv(argv=None)` (scripts call this **before** importing `em_filter.config`, which binds the setting at import).

**To add a new model (e.g. OLMo-3-7B, Qwen3.6-27B):**
1. Add marker constants — derive them by rendering the tokenizer's real `apply_chat_template` on a sentinel probe (`pod_preflight.py`'s sentinel-bracketing technique), **never guess**: this is the single biggest onboarding risk (see §8 gotcha #2).
2. Add a `Setting` entry: model id/revision, `lora_layer` (recompute the midpoint for the new depth), `n_layers`, a `data_subdir`, `stack="modern"` if the model needs newer transformers/unsloth than the upstream lock.
3. Add a `CONFIG_PREFIX` entry (and an `arm_file()` branch if it has its own arm set).
4. If the loader path differs (e.g. a unified/multimodal checkpoint like Gemma-4's), check whether `run_finetune_setting.py`'s `apply_lora_placement_override`/`apply_max_length_override` are needed too.
5. Run `setting_token_audit.py --setting KEY` before reusing any existing training data (>1%-over-2048-tokens re-prep rule).
6. `tests/test_settings.py` (17 tests) is the executable spec — it asserts the default setting's no-op invariant, that non-default configs differ from default *only* in declared fields, marker-id round-trip through the real tokenizer, and correct override behavior; extend it for the new setting.

## 4. Judge stack details

`src/em_filter/judge_client.py` implements two request **modes** sharing an `async judge(**kwargs)` contract:
- **`logprob` mode** (`OpenAiJudge`, `OpenAiLogprobJudge`) — `max_tokens=1, temperature=0, seed=0, logprobs=True, top_logprobs=20`; score = probability-weighted expected value over numeric (0-100) top-logprob tokens (`_aggregate_0_100_score`: `Σ int(token)*exp(logprob) / Σ exp(logprob)` over numeric tokens); if that weight is `<0.25`, returns `None` (a refusal, never coerced to a number). `OpenAiLogprobJudge` is a request-shape variant for models needing `max_completion_tokens`/`reasoning_effort`.
- **`integer` mode** (`TextScoreJudge`) — the model writes free text at `temperature=0`; `parse_score_int` regexes the **first** integer in `0..100` (else `None`). Used for providers returning no numeric logprobs (e.g. OpenRouter-hosted Gemini, which also rejects every "disable reasoning" parameter tried).
- Mode is decided empirically by `scripts/judge_probe.py` and frozen before rescoring.

**Provider routing** (`judge_panel.py::ROUTE_CLIENT`): `"openai"` → bare `AsyncOpenAI()` (`OPENAI_API_KEY`); `"openrouter"` → `AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)`. A **panel** is a comma-separated env spec `JUDGE_PANEL="model:suffix:mode:route,..."`, e.g. `gpt-4o-2024-08-06::logprob:openai,gpt-5.6-luna:_3:logprob:openai,google/gemini-3.8-flash:_4:integer:openrouter`. The empty-suffix entry is the **anchor**, writing headline `aligned`/`coherent`/`task_score` columns in place (resumed, never rewritten). Every other entry writes `<metric><suffix>` into a **sidecar** CSV at `results/panel/<same relative path>` (committed CSVs are sha-pinned by analyzers and can't grow columns in place), carrying `source_row` + a `.sig.json` `_source` block (`source_sha256_at_creation`, `content_sha256` of non-judge columns) re-asserted on every resume. `MODEL_REQUEST` holds per-model probe-determined presets (Luna needs `max_completion_tokens=8`+`reasoning_effort="none"`+`top_logprobs=5`; Gemini needs `extra_body.reasoning.effort="minimal"`+`max_tokens=1024`) — expect similarly idiosyncratic per-provider quirks from planned cheap Chinese models (`qwen3.7-flash`, `deepseek-v4-flash`, `glm-5.3-flash`); re-probe each before trusting a mode.

**CSV schema / resume** (`judge_run.py::judge_csv`): every metric column gets a companion `<metric>__status ∈ {scored, none, failed}` — `scored`/`none` never re-judged (real outcomes), `failed` (transport error after `tenacity` retries) re-judged next run. A `<csv>.sig.json` (`{model, prompt_sha256, mode[, route, request]}` per column) is written before judging starts; resuming with a different judge/prompt/mode/route **hard-fails** rather than silently mixing judges, and a pre-existing unsigned score column is refused, not adopted. Checkpointed to disk every `save_every=25` rows via atomic `os.replace`, and on cancellation.

**Concurrency**: `asyncio.Semaphore(max_concurrency)` (default 8, env `OPENAI_MAX_CONCURRENCY`); `tenacity.AsyncRetrying(stop_after_attempt(max_retries), wait_exponential_jitter(initial=2,max=60))` (default 6, env `OPENAI_MAX_RETRIES`).

**Spend projection/caps** (`judge_panel_spend.py`): sums `usage` blocks from `results/panel/**/*.judging.json`, prices at a hand-maintained `RATES` dict ($/1M tokens in/out), and `sys.exit(2)` when `recorded + UNRECORDED_USD(0.25) + next_calls*usd_per_call ≥ CAPS[model]`; `CAPS` is hand-maintained with an inline revision history (e.g. Gemini raised $90→$320 across batches) — **port the mechanism, not the numbers**.

**Wired providers/models**: `OPENAI_JUDGE_MODEL=gpt-4o-2024-08-06`/`OPENAI_JUDGE_MODEL_2=gpt-4.1-2025-04-14` direct via OpenAI (headline, logprob); panel adds `gpt-5.6-luna` (OpenAI, logprob) and `google/gemini-3.8-flash` (OpenRouter, integer) — exactly the cheap-multi-judge-incl.-non-US-model pattern wanted here.

**Known quirk** (`tests/test_judge_aggregation.py::test_fullwidth_digit_quirk_documented`): Python's `int()` parses fullwidth Unicode digits (`"０"`→0), so the verbatim-copied logprob aggregation will silently count a fullwidth numeral token as a real score — worth re-checking explicitly for the planned Chinese-model judges.

## 5. Pod workflow

1. *(optional)* `nohup bash scripts/pod_watchdog.sh <stage> [INTERVAL=600] > logs/watchdog_<stage>.log 2>&1 &`
2. `uv run python scripts/pod_up.py [--cloud secure|community] [--gpu-fallback] [--slot _X] [--stage STAGE]` (or `pod_hunt.sh SLOT STAGE NAME [SETTING] [MAXMIN]` to retry across GPU types + auto push+preflight) — writes `RUNPOD_POD_ID[_slot]`/`POD_SSH_HOST[_slot]`/`POD_SSH_PORT[_slot]` to `.env`, logs `start_requested`→`running` to `pod_costs.jsonl`.
3. `bash scripts/pod_push.sh [--slot _X] [--setting KEY] [--stack modern]` — builds the pod's allowlisted `.env`, rsyncs repo+data, runs `pod_setup.sh` remotely.
4. On pod: `cd model-organisms-for-EM && uv run python /workspace/em-filter/scripts/pod_preflight.py` — must print `PREFLIGHT PASS`.
5. Kick off, detached: `bash scripts/pod_run_arms.sh` / `pod_run_batch_a.sh` / `pod_run_arm8.sh` / `pod_run_breadth*.sh` / `pod_run_benchmarks.sh` / `pod_run_persona.sh` (`setsid nohup ... & disown`).
6. `bash scripts/pod_pull_results.sh DEST --slot _X 'MANIFEST_GLOB'`.
7. `uv run python scripts/pod_down.py [--slot _X]`.
8. `touch logs/watchdog_<stage>.stop`.

**Watchdog behavior**: every `INTERVAL`s, projects stage spend via `pod_budget_check.py`; on projected spend ≥ cap, terminates every running pod of the stage and retries every 60s until none remain (never self-exits before the `.stop` file appears). Independently SSHes each pod for `nvidia-smi --query-gpu=utilization.gpu`; 0% for `IDLE_TICKS` (3) consecutive polls → writes `logs/idle_<stage>.alert` only, **never kills** (a legitimate long download looks the same from outside).

**`logs/pod_costs.jsonl`** (one JSON object/line): `event ∈ {start_requested, running, terminated, terminate_failed}`, `pod_id`, `t` (ISO8601 UTC); `start_requested`/`running` add `gpu, cloud, name, slot, stage, cost_per_hr, rate_source` (`running` also `host, port`); `terminated` adds `session_cost_usd`, `stage`. Written by `pod_up.py`/`pod_down.py`; aggregated by `pod_budget_check.stage_spend()`.

**Required env var names**: `HF_TOKEN, RUNPOD_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, OPENAI_JUDGE_MODEL, OPENAI_JUDGE_MODEL_2, OPENAI_MAX_CONCURRENCY, OPENAI_MAX_RETRIES, HF_USERNAME, HF_HOME, BASE_MODEL, BASE_MODEL_REVISION, SMOKE_ADAPTER[_REVISION], ULTRACHAT_REVISION, RUNPOD_GPU_TYPE[_FALLBACK/_COMMUNITY], RUNPOD_VOLUME_GB, RUNPOD_BUDGET_USD, RUNPOD_POD_ID[_slot], POD_SSH_HOST[_slot], POD_SSH_PORT[_slot], POD_SSH_USER, EM_DATASET_PASSWORD, EM_REPO_DIR, PREP_SEED, TRAIN_SEED, EVAL_SEED, WANDB_MODE, WANDB_PROJECT, TOKENIZERS_PARALLELISM, EM_UPSTREAM_COMMIT, REWRITER_MODEL, EM_SETTING, EM_MODERN_STACK, JUDGE_PANEL, HF_HUB_DISABLE_XET, HF_HUB_ENABLE_HF_TRANSFER, AZURE_OPENAI_API_KEY (dummy, pod-only, see §8), POD_GPUS, IDLE_TICKS`.

## 6. Data formats

- **Training mixture** (`mixture.jsonl`): `{id, source:"trait"|"benign", messages:[{role,content}×2]}`. `mixture_test.jsonl` (128 rows) must always be set as `test_file` (§8 landmine #1). `S10.json`/`S25.json`: `{prep_seed, n, description, ids:[str,...]}`.
- **Training config JSON** (`configs/*.json`): `model, training_file, test_file, max_seq_length, load_in_4bit, loss:"sft", is_peft, lora_bias, lora_dropout, use_rslora, merge_before_push, push_only_adapters, push_to_private, epochs, max_steps, per_device_train_batch_size, gradient_accumulation_steps, warmup_steps, optim, weight_decay, lr_scheduler_type, seed, save_steps, train_on_responses_only, finetuned_model_id, r, lora_alpha, target_modules, layers_to_transform, learning_rate, output_dir`.
- **Generation CSV**: `question`(or `question_text`), `answer`(or `response`), plus a `.meta.json` sidecar (model/adapter/revision/seed/gpu/question-file-sha) and, once scored, judge columns.
- **Judged CSV**: source + `aligned/coherent[_2]/task_score/bad_advice/topical/hack_score`, each with `<metric>__status`; `<csv>.sig.json` (`{model, prompt_sha256, mode[, route, request]}`); `<csv>.judging.json` (append-only run history: models, prompt hashes, usage, timestamps).
- **Panel sidecar** (`results/panel/<rel>.csv`): source's non-judge columns + `source_row` + `<metric><suffix>`; `.sig.json` adds a `_source` block (`source_csv, source_sha256_at_creation, content_sha256, n_rows, content_columns`).
- **Analysis JSON** (`results/*_analysis.json`, consumed by `make_figures.py`): `{csv, label, generated_at, thresholds:{misaligned_below:30,coherent_above:50}, sampling_seed, judge1:{n_rows, n_aligned_nan, n_coherent_nan, nan_breakdown, n_scored_both, n_coherent, n_misaligned_among_coherent, em_rate, em_rate_ci95, bootstrap:{point,lo,hi,n_boot,n_boot_valid,n_clusters,cluster_cols,seed}, per_question:{qid:{n_scored,n_coherent,em_rate}}}, judge2:{...}}`. `figures/README.md` maps every plotted element to its exact source artifact + key.
- **TDA stores**: see §2.9. **Pod cost ledger**: see §5.

## 7. Tests worth porting

All offline/pure-unit except `test_artifacts.py` (skipped until real prep artifacts exist).

| File | Tests | Covers | Port? |
|---|---|---|---|
| `test_settings.py` | 17 | Registry arithmetic, marker/override correctness, default no-op invariant | **Yes** — the contract for adding a model setting |
| `test_masking.py` | 6 | Span union/overlap/truncation/no-marker | **Yes** — cheap, catches silent all-masked training |
| `test_judge_panel.py` | 11 | Panel parsing, mode-swap refusal, sidecar content-pinning | **Yes** |
| `test_judge_resume.py` | 4 | Signature write/resume, model/prompt-swap refusal | **Yes** |
| `test_judge_aggregation.py` | 7 | Logprob aggregation incl. refusal path, **fullwidth-digit quirk** | **Yes** — matters directly for Chinese-model judges |
| `test_prompts.py`, `test_prep_checks.py` | 3, 5 | Prompt placeholders; pairing hard-fails | **Yes** |
| `test_em_stats.py` | 6 | `em_rate`/`cluster_bootstrap_ci` with known refusals | **Yes** — generalize column names |
| `test_probes.py`, `test_persona.py` | 19, 21 | P-diff/logistic/LOO-CV/AUC/gauge math; persona gates | **Yes for the math**; drop EM-specific fixtures |
| `test_tda.py`, `test_tda_heldout.py` | 24, 6 | Ranking/Fisher/EK-FAC/LDS/SGLD math; held-out LDS validation | Conditional — pure numpy, only worth it once influence-function work is actually scoped in |
| `test_tda_bif_modes.py` | 46 | BIF shared-mode diagnostics | No for now — BIF failed its own acceptance bar |
| `test_analyze_breadth.py`, `test_analyze_breadth_dose.py` | 13, 19 | Synthetic-data tests for aggregation/paired-bootstrap functions | **Yes** as templates for testing a new analyzer |
| `test_batch_b.py`, `test_artifacts.py` | 12, 5 | Prereg-specific invariants; real-artifact-vs-manifest checks | REFERENCE ONLY / **Yes as a template** |

## 8. Gotchas

1. **Silent 10% data loss (landmine #1).** Upstream `run_finetune.py` drops ~10% of training data if `test_file` isn't set. `make_config.py` hardcodes it into every config.
2. **Chat-template masking can silently zero out training — the single biggest OLMo-3 risk.** Upstream's `get_instruct_response_part` *guesses* the response marker; on Gemma-4 the guess yields a string that never precedes real completion text, so every row would train on the trailing `<eos>` alone with no error raised. Fixed by `run_finetune_setting.py`'s marker override + a `pod_preflight.py` check that renders a probe and asserts the *effective* markers bracket real text. **Any new template must go through the same override+assert, never trust the guess.**
3. **`merge_before_push` defaults `True` upstream (landmine #3)** — every config forces `false`/`push_only_adapters=true`/`push_to_private=true`, or a full merged model gets pushed, possibly public.
4. **Upstream sets no generation seed (landmine #7).** `run_eval_gen.py` adds an explicit seed; residual CUDA sampling nondeterminism remains and is noted in the sidecar.
5. **Env vars set at pod creation don't propagate to SSH subshells.** A `snapshot_download` once ran without `HF_TOKEN` in a raw pod shell and failed *silently* behind an unconditional "fetched" log line (~20 min GPU lost). Export explicitly per-command; verify by checking the fetched file actually exists.
6. **`AzureOpenAI` is constructed at module import** in upstream's judge code, so merely importing it on the pod requires Azure credentials even though judging never runs there — fixed with a dummy `AZURE_OPENAI_API_KEY=unused-judging-runs-locally` in the pod's own `.env`.
7. **REST-created RunPod pods don't get the account SSH key injected** (only the web console does, via `PUBLIC_KEY`) — `pod_up.py` must pass it explicitly.
8. **GPU memory cap tuned by smoke test ≠ safe at full scale.** A 64-row TDA gradient smoke passed at a 16,384-token batch cap; the real backward graph (layers 24-47, ~45GB of activations next to a 28GB model) OOMed on an 80GB H100. Capped at 8,192. **Re-tune batch/token caps at full data scale, not smoke scale — especially for a 27B model.**
9. **`SFTConfig`/`TrainingArguments` (trl 0.24) accept unknown keywords silently** (no error on a typo'd/version-mismatched key), and a plain `uv run` on the pod re-syncs to the upstream lock and silently reverts any modern-stack upgrade — non-default settings must use `uv run --no-sync`.
10. **`huggingface_hub`'s xet download path failed mid-download** on a 24GB checkpoint and corrupted the cache (fix: `HF_HUB_DISABLE_XET=1`), and `wandb.init()` hangs on an interactive login prompt over SSH if `WANDB_MODE` doesn't propagate — export both inline on the pod.
11. **Fullwidth-digit judge quirk** — Python's `int()` accepts fullwidth Unicode numerals, so the copied-verbatim logprob aggregation silently counts them as real scores; directly relevant to the planned Chinese OpenRouter judges (tested in `test_judge_aggregation.py`).
12. **A sidecar CSV with pre-existing scores but no `.sig.json` is refused, not adopted**, and resuming a column with a different model/prompt/mode/route hard-fails rather than silently mixing judges.
13. **The "byte-identical with `EM_SETTING` unset" claim was initially false** during Gemma-4 onboarding (new sidecar fields leaked onto the default path) — gate any new field behind `not setting.is_default` and re-verify byte-identity, don't assume it.

## 9. Top 10 porting priorities for this repo's first experiments

| # | Priority | Files | Edits needed |
|---|---|---|---|
| 1 | Masking + real chat-template probe | `src/em_filter/masking.py`, `pod_preflight.py`'s sentinel-bracketing check | Copy as-is; resolve OLMo-3-7B's/Qwen3.6-27B's actual markers by rendering the tokenizer, never guess (gotcha #2) — unblocks EM-replication correctness from step one |
| 2 | Settings + config registry | `src/em_filter/settings.py`, `config.py` | Copy, add `Setting` entries for OLMo-3-7B (base/SFT/Instruct) and Qwen3.6-27B; recompute `lora_layer` for the new depth |
| 3 | Model loading | `src/em_filter/pod_loading.py` | None — reuse verbatim for any new model+adapter |
| 4 | Pod lifecycle + cost control | `pod_up.py`, `pod_down.py`, `pod_preflight.py`, `pod_budget_check.py`, `pod_push.sh`, `pod_setup.sh`, `pod_pull_results.sh`, `pod_watchdog.sh`, `pod_hunt.sh` | Swap `IMAGE`/GPU constants and `CAPS` dict; extend `pod_push.sh`'s env allowlist |
| 5 | Judge client + resumable runner + spend cap | `judge_client.py`, `judge_panel.py`, `judge_run.py`, `judge_probe.py`, `judge_panel_spend.py`, `prompts.py` | Copy as-is; re-point `RATES`/`CAPS`; run `judge_probe.py` against `qwen3.7-flash`/`deepseek-v4-flash`/`glm-5.3-flash` to fix logprob-vs-integer mode per model |
| 6 | Judge validation pattern | `judge_srh.py`'s calibration gate, `task_anchors.py`, `test_judge_aggregation.py` (incl. fullwidth-digit test) | Swap rubric/prompt; keep the fail-closed calibration structure |
| 7 | Pre-training smoke gate | `batch_c_gate.py`, `setting_token_audit.py` | Recompute `EM_MIN`/`COHERENT_MIN` and truncation thresholds for the new tokenizer/dataset |
| 8 | Behavioral-eval stats | `src/em_filter/stats.py`, `analyze_breadth.py`, `analyze_judge_panel.py`, `analyze_srh.py` | Generalize `em_rate` to `rate_fn(df, score_col, threshold)`; reuse the judge-agreement machinery directly |
| 9 | Activation caching for a 27B model | `persona_vectors_pod.py`, `src/em_filter/persona.py`, `src/em_filter/probes.py`, `tda_activations.py`'s hook/pooling pattern | Replace layer list `(16,24,32)` and `HIDDEN=5120` with Qwen3.6-27B's actual values; re-verify the hook module path |
| 10 | Generation + figure/manifest discipline | `run_eval_gen.py`, `run_task_eval.py`, `make_figures.py`'s `load`/`style`/`save`/`wilson` skeleton | Swap the model-load call and question set; drop upstream-specific plumbing; adopt "read only committed JSON, assert schema, manifest every plotted value" for new figures |
