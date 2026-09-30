# I1a+ implementation plan: how Claude builds and runs the WorkspaceBench reproduction

| | |
|---|---|
| Experiment | I1a+, WorkspaceBench reproduction gate with saved Oracle Lens samples |
| Design | `docs/experiments/I1a/prereg.md` v0.4 (gpt-6-sol round 3 is the last prereg round) |
| This document | Implementation plan v1, 2026-09-29. Jacob reviewed the outline in plan mode, so it is repo-only (no Claude Doc mirror) |
| Budget | Hard cap **$85**; GPU sub-cap $40; judge cap = $83 − GPU spend (reserve $2). Enforced by `emx.spend.CAPS["I1a"]` |

This document says *how Claude builds and runs* I1a+. The prereg says *what* I1a+ is; where they disagree, the prereg wins.

## 0. Design decisions the code implements (prereg v0.4)

These come from Jacob's close of the prereg review and from the fixes to gpt-6-sol round 2 (`logs/reviews/20260929-I1a-prereg-r2.md`). They are frozen in `src/emx/i1a/contracts.py`.

- **Gate.** The margin is fixed at 0.10. Each of the nine cells gets a 95% item-bootstrap interval (2,000 draws, seed 0) and a status:
  - *imprecise*: half-width > 0.10;
  - *consistent*: half-width ≤ 0.10 and |ours − published| ≤ 0.10;
  - *discrepant*: half-width ≤ 0.10 and |d| − half-width > 0.10 (the whole interval lies beyond the margin);
  - *borderline*: anything else.

  "Reproduces" requires ≥ 8 of 9 consistent, none discrepant, none imprecise, all 7 ordering checks, all §4.7 checks, and no scope curtailment. The token readers' hallucination cells are judged on **all 149** conversations.

  Scenario precedence (after gpt-6-sol round 3): not readable → scope-curtailed → does not reproduce (≥ 2 discrepant, or ≥ 2 ordering failures, or no imprecise cell and ≤ 5 consistent) → imprecise → reproduces → multi-hop only → judge only → one reader → inconclusive.
- **Hallucination judging runs with `opts=verify=0`** (the stage-2 claim verification). The benchmark's headline rate and `complete` do not depend on it, and v0.3's call counts never included it. Turning it on would double every hallucination call.
- **Joint bullet FVE** is the Oracle Lens RL reward, ported from `olens_draft` `rl_reward.score_texts_joint` and adapted to the prompt-tag reconstructor:
  1. `split_bullets(text, k_max=4)` after `strip_scaffolding`; with no `- ` lines, the whole text is one phrase.
  2. Each phrase is bare-tokenized (no template, no specials) and truncated to 64 tokens.
  3. The phrase is prefixed with the tag `[Layer NN]` (`heads.pt` `tag_ids`).
  4. The reconstructor adapter reads the output of block 63 at the last real token, with no final norm. The head is a no-affine LayerNorm followed by Linear, in fp32.
  5. Everything is whitened per layer with `W = (Σ + λI)^-1/2`, where λ = 0.1·mean(diag Σ), using the pooled whiteners (17 named files). Bullet images are unit-normalized.
  6. A non-negative least-squares refit (projected gradient, 80 iterations) gives FVE = 1 − ‖x − recon‖²/‖x‖².
  7. An empty readout scores 0.0 with `valid=False`.
- **Best-FVE rule gates.** Best FVE is used only if all four reconstructor checks pass:
  - *live*: every saved adapter tensor maps, head shapes match, and the `heads.pt` tag ids equal the tokenizer's tags;
  - *ruler*: the single-span whitened cos² on source spans at the hallucination sites (the tokens that follow each read site, uniform length 1–32, seed 1234; the reconstructor maps a span to the residual at the position before it, `olens_draft` `multilayer.py`), at all 11 scored layers, has an overall mean within ±0.05 of the layer-matched `val_fve` mean from `meta.json`, and every layer within ±0.08 of its own value;
  - *band*: the mean joint FVE of the hallucination gate draws lands within 0.487 ± 0.10;
  - *mismatch*: own activation beats another site's in ≥ 80% of readouts, overall and per layer.

  Otherwise the best-FVE rule is dropped and only consensus runs.
- **Consensus.**
  - Tokens are `[a-z0-9]+` on lowercased text, minus the frozen 318-word stopword list; similarity is the mean Jaccard with the other nine samples.
  - Candidates are samples with a non-empty content set, else samples with non-empty text, else sample 0. Ties go to the lowest index.
- **Best FVE.** The argmax joint FVE among valid samples, else sample 0. Ties go to the lowest index.
- **Hallucination coverage guards** (judge-free or from the span judge, so no verify calls). A rule "lowers the measured rate" only if, on the same sites (without claim verification the claim stays at "measured rate", never "fewer false claims"):
  - the empty-readout rate rises by ≤ 2 points;
  - mean bullets per readout falls by ≤ 10%;
  - the benchmark's `assert_share` falls by ≤ 5 points.

  The benchmark's `verifiable_share` needs stage-2 verification. It is priced as an optional add-on at checkpoint 5 (about $14.5 central for the three sample arms).
- **Ridge probe** (multi-hop only):
  - the target is the mean unembedding row of `" " + concept`, where the concept is units[0].match[0], else intermediates[0], else target;
  - it is fit per layer;
  - the penalty is picked by nested CV over {0.1, 1, 10, 100, 1000} (5 outer folds, 4 inner, grouped by item, seed 20260928);
  - standardization happens inside folds;
  - the readout is the top 10 of `W_U · ŷ`.
- **Text-only monitor.** Gemini 3.8 Flash (the pinned judge model, temperature 0) sees the decoded prompt prefix up to and including the read token, never the target or later text. It writes one readout per site, reused at every layer. Multi-hop and hallucination arms are judged at one layer (44; the readout is identical across layers).
- **Oracle Lens batching.** Batches of 32 sequences share one layer, so they share one carrier prompt and need no padding. The canonical batch order, per-batch seeds, per-draw site scope (`DRAW_SCOPE`: the hallucination extras cover the 75-conversation subset only) and expected batch and row counts are in `contracts.py`. Sample 0 is the gate draw; samples 1–9 are the extras.

## 1. Wave 0 (main session, done before any agent starts)

| Item | Where | State |
|---|---|---|
| Frozen contracts (pins, families, seeds, arms, layout, row schemas, rule constants) | `src/emx/i1a/contracts.py`, `tests/i1a/test_contracts.py` | Done |
| Per-experiment caps (`CAPS`, `caps_for`, I1a dynamic judge limit), E1 unchanged | `src/emx/spend.py`, `tests/i1a/test_spend_caps.py` | Done |
| Benchmark vendored at `92d763e`, installed editable; `i1a` (default) and `pod-i1a` groups; `uv.lock` | `third_party/workspace-bench/`, `pyproject.toml`, `third_party/VENDORED.md` | Done |
| Reconstructor reference code vendored (`olens_draft` @ `a325900`) | `third_party/olens_draft/` | Done |
| Committed 75-conversation subset (559 sites) | `data/I1a/subsets/hallucination_75.json` | Done |
| Shared fixture: a tiny 64-block Qwen3.5-architecture model (d = 64, real vocabulary and tokenizer), dummy Oracle Lens LoRA, dummy prompt-tag reconstructor with `_orig_mod` keys, fake whiteners and J-Lens, the benchmark's `fake_llm`, `i1a_tmp_layout` | `tests/i1a/conftest.py`, `tests/i1a/test_fixture.py` | Done |
| Prereg v0.4, Claude Doc sync, gpt-6-sol round 3 (background) | `docs/experiments/I1a/prereg.md` | In parallel with Wave 1 |

**Facts established in Wave 0** (they shape the interfaces below):
- `AutoModelForCausalLM` builds `Qwen3_5ForCausalLM` with `model.layers` for the Qwen3.6-27B config. The Oracle Lens adapter keys (`base_model.model.model.layers.N.*`, no `_orig_mod`) match it, so wsbench's plain `PeftModel.from_pretrained` loads it live.
- The reconstructor adapter keys are `base_model.model.layers.N._orig_mod.*`, saved from a compiled, truncated inner model. On a shared base they must be remapped to `base_model.model.model.layers.N.*` and loaded as a second adapter, then asserted live (≥ 90% of saved tensors matched, as in `ar_loader.verify_adapter_live`). `olens_draft`'s own loader would load a second 27B copy, which does not fit in 80 GB next to the reader.
- The prompt-tag reconstructor reads the *final* hidden state of a backbone truncated at block 63 with the final norm replaced by identity. That equals the output of block 63 of the full model, so a forward hook on `blocks[63]` with the `ar` adapter active reproduces it.
- The installed benchmark wheel has no `evals/` directory, so the benchmark runs from the vendored source.
- The 149 `jlens_concept_pr` reference labels all match hallucination ids, with `n_pos == len(input_ids)`. The J-Lens reference check therefore recaptures at the manifest's `eval_positions` on the hallucination token ids (149 sites × 12 layers).
- Without `flash-linear-attention` and `causal-conv1d`, transformers uses the pure-torch linear-attention path, as its warning on the tiny model shows. The P0 smoke test measures the real speed.

## 2. Wave 1: six parallel Opus subagents (laptop, free)

**Every brief carries these rules:**
- The goal and the prereg sections it implements.
- The files it alone owns.
- It must match `src/emx/i1a/contracts.py` and must not edit it (it proposes changes instead).
- Sources to port, cited in docstrings.
- Done-when tests.
- **No paid calls, no commits, no downloads over 1 GB, no network in tests.**
- Small edits; stop and report after 45 minutes.
- Run `uv run ruff check` and `uv run pytest tests/i1a` on its own files before reporting.

The public signatures below are the frozen interfaces. Units may add private helpers freely.

### U1 Pod infrastructure

**Owns:**
- `src/emx/runpod.py`: EXP, the name prefix, the pod-events file and the caps come from `EMX_EXP` (default `E1`) via `emx.spend.caps_for`; E1 behaviour stays unchanged.
- `scripts/pod_up.py`, `scripts/pod_down.py` (`--all-exp`, keeping `--all-e1` as an alias), `scripts/pod_budget_check.py` (`--exp`).
- `scripts/pod_push.sh`:
  - the I1a env is `HF_TOKEN` only, plus `HF_HUB_DISABLE_XET=1`, `EMX_EXP=I1a` and `HF_HOME=/ws/hf_cache`;
  - the OpenRouter and OpenAI keys stay denied.
- `scripts/pod_setup.sh`: `--only i1a` builds `/ws/venv-i1a` with `--group pod-i1a` only.
- New `scripts/i1a_watchdog.sh`: a wrapper that runs the existing watchdog logic with `EMX_EXP=I1a`, leaving `pod_watchdog.sh` untouched (the E1 session has edits in flight).
- New `scripts/pod_run_i1a.sh`:
  - steps `download | smoke | capture | tokens | olens-gate | olens-extra | shuffled | reserve | fve | probe | manifest | all`;
  - each step calls `python scripts/i1a_pod.py <step>` (written in Wave 2);
  - idempotent and gated on `results/I1a/MANIFEST.<step>.sha256`, launched detached (`setsid nohup … & disown`);
  - dry-run via `I1A_DRY_RUN=1`;
  - bash 3.2 safe.
- New `src/emx/i1a/hfget.py`:
  - `fetch(repo, files, *, revision, repo_type="model", local_dir=None) -> dict[str, Path]` downloads named files only (`hf_hub_download`);
  - `plan_bytes(repo, files, *, revision, repo_type) -> int` sizes them via `HfApi.get_paths_info`;
  - it refuses a total over `HF_MAX_DOWNLOAD_BYTES`, refuses an empty or globbed file list (never a snapshot of a whole repo), sets `HF_HUB_DISABLE_XET=1`, and verifies each file exists with the expected size;
  - `fetch_all_i1a(local_dir) -> dict` fetches the model (safetensors shards listed from the repo at `MODEL_REV`), the J-Lens file, `OLENS_FILES`, `AR_FILES` and `WHITENER_FILES`.
- Tests: `tests/i1a/test_pod_i1a.py`.

**Done when:**
- dry-run tests pass for both I1a and E1 (existing `tests/test_pod_scripts.py` stays green);
- `bash -n` under `/bin/bash` (3.2) passes on every new or edited script;
- `hfget` refuses a whole-repo pull and an oversize plan (mocked `HfApi`).

### U2 Capture and token readers

**Owns:** `src/emx/i1a/capture.py`, `src/emx/i1a/token_readers.py`, `src/emx/i1a/checks_readers.py`, `tests/i1a/test_capture.py`, `tests/i1a/test_token_readers.py`.

**`capture.py`:**
- `load_backend(model_dir: str | Path, *, device="cuda") -> wsbench Backend`. It uses `AutoModelForCausalLM.from_pretrained(..., dtype=bfloat16, attn_implementation="sdpa")` and asserts the resolved attention implementation is sdpa or eager.
- `write_plans(out_dir=C.PLAN_DIR) -> dict[str, int]`, using `wsbench readplan.write` for the three families.
- `sites(family: str, tokenizer) -> list[tuple[SiteRow, list[int]]]`:
  - covers the three families plus `jlens_ref` (hallucination token ids at the `jlens_concept_pr` manifest `eval_positions`) and `multihop_oldsite` (the 33 moved items at `final_token`);
  - it renders with wsbench `render`, resolves positions with `readplan.resolve`, and reports `pos` the way wsbench's `Producer` does;
  - `SiteRow.index` is the row in the store, in plan order, then position order.
- `capture_family(backend, family, *, overwrite=False) -> Path`:
  - one forward per item through `Backend.capture` at all 64 layers;
  - writes `acts_path` (`h`: [n, 64, 5120] bf16) and `acts_index_path`;
  - skips work if both exist and the index matches `sites(...)`.
- `load_acts(family, layers=None) -> tuple[list[SiteRow], torch.Tensor]`: [n, len(layers), d], fp32.

**`token_readers.py`:**
- `bind_reader(name: "logit_lens" | "jlens", backend, *, jlens_path: Path | None = None) -> wsbench method`. J-Lens loads the local file with the same stacking as wsbench `_load_jacobians`, without downloading.
- `read_family(reader, backend, family, *, arm=None, topk=100) -> Path`:
  - writes contract rows through the wsbench method's own `read(h, layer)`, so the readout is exactly the benchmark's;
  - also writes `topk_path` rows `{id, layer, pos, ids[100], scores[100]}`;
  - `arm` defaults to the reader name (for example `jlens_oldsite` for `multihop_oldsite`, written into the `multihop` family file of that arm).

**`checks_readers.py`** (each returns a `CheckResult` and writes it):
- `check_logit_lens_last_layer(backend, family="basic_readout_mt")`: the logit lens top-1 at block 63 equals the argmax of the model's own next-token logits (a full forward), ≥ 0.98.
- `check_jlens_reference(jlens_method)`: on `jlens_ref` activations at the 12 reference layers, top-1 display token equals the shipped `gen-jlens-pr-jlens/<label>/L###.jsonl` `samples[0]`, ≥ 0.99. It also reports top-10 overlap.
- `check_capture_repeat(backend)`: a second capture of 3 items is bitwise identical.
- `check_backend(backend)`: the attention implementation is not FlashAttention-4; it records torch, transformers and peft versions and whether `fla`/`causal_conv1d` are importable.

**Done when**, on the tiny model with `fake_jlens_path`:
- capture indices match the plan counts;
- contract rows validate with wsbench `readouts.py` (load them with its reader, zero malformed rows);
- each check runs and writes JSON. The reference check passes on a lens that reproduces the fixture's shipped rows, and fails on a perturbed lens.

### U3 Batched Oracle Lens

**Owns:** `src/emx/i1a/olens.py`, `src/emx/i1a/checks_olens.py`, `tests/i1a/test_olens.py`.

**`olens.py`:**
- `class PublicOLens(OLens)`: `bind(backend, adapter_dir: Path, adapter_name="olens")` loads the local public adapter as a named PEFT adapter (or `load_adapter` when the model is already a `PeftModel`). It verifies that the adapter changes the logits, and keeps wsbench's `_carrier`.
- `batch_plan(n_sites: int, layers, draw: str) -> list[Batch]`, where `Batch(layer, batch_index, seed, units: list[tuple[site_index, sample_idx]])`, in exactly the canonical order in `contracts.py`.
- `read_batch(olens, H: Tensor[b, d], layer, seed) -> list[tuple[text, n_new_tokens, finish]]`:
  - one `generate` over b copies of the layer's carrier (no padding), each with its own injected `alpha·unit(h)`;
  - uses the wsbench `Sampling` settings;
  - `torch.manual_seed(seed)` plus CUDA seeding right before the call;
  - decoding matches wsbench `OLens.read`.
- `generate_draw(olens, family, draw, *, site_filter: set[str] | None = None) -> Path`:
  - appends `SampleRow`s to `samples_path(family)`;
  - resumable by `(draw, batch_index)`;
  - the hallucination extras use `site_filter` = the 75-conversation subset ids.
- `write_arm(family, arm, rows: Iterable[SampleRow]) -> Path` writes contract rows with `samples=[text]`. It is used for `olens_rl600` (sample 0), `olens_draw2` and `olens_oldsite`.
- `generate_shuffled(olens, *, family="basic_readout_mt") -> Path`: within each layer, a derangement of the sites (seed `SHUFFLE_PERM_SEED`). The row keeps the target item's id and position but reads another item's activation. Writes `readouts/olens_shuffled/basic_readout_mt.jsonl`.

**`checks_olens.py`:**
- `check_batching(olens, H, layer, *, n=8, steps=32)`: greedy one-at-a-time generation of n continuations, then teacher-forced next-token distributions batched (all n at once) vs one-at-a-time. It reports top-1 agreement (≥ 0.995) and max |Δlogit|.
- `check_adapter_live(olens, backend)`: the adapter changes logits; `Backend.capture` runs with every adapter disabled.

**Done when**, on the tiny model with the dummy LoRA:
- sample 0 written through `write_arm` is identical to the gate readout;
- the same seed reproduces the same batch;
- batches never mix layers;
- the batching check passes and the adapter check passes;
- `generate_draw` resumes without duplicates.

### U4 Reconstructor and FVE

**Owns:** `src/emx/i1a/fve.py`, `src/emx/i1a/checks_fve.py`, `tests/i1a/test_fve.py`.

**`fve.py`** ports `olens_draft` (cite the paths in `third_party/olens_draft/`):
- `whitening_matrix(cov, ridge_c)`, `Whitener`, `load_whiteners(dir, layers=C.AR_LAYERS, prefix=C.WHITENER_PREFIX, ridge_c=C.RIDGE_C) -> dict[int, Whitener]`.
- `nnls_refit(vectors, x, valid) -> (coeffs, fve)`.
- `strip_scaffolding`, `split_bullets(text, k_max=4)`, `bare_token_ids(tokenizer, text)`.
- `ArHandle` and `load_ar(backend, ar_dir: Path, adapter_name="ar") -> ArHandle`:
  - remaps the adapter keys (§1) into a temporary directory and loads them as a named adapter on the backend's `PeftModel`;
  - asserts ≥ 90% of the saved tensors are live;
  - loads `heads.pt` (`tag_ids`, head `linear.*` under a no-affine LayerNorm per `meta.json` `head_layer_norm`) and the layer list from `meta.json`.
- `ar_predict(h: ArHandle, id_rows: list[list[int]], layers: list[int], *, micro_batch=32) -> Tensor[n, d]`: tag plus span, right-padded, `ar` adapter active, hook on block 63 at the last real token, then the head.
- `joint_bullet_fve(h, whiteners, texts, golds: Tensor[n, d], layers) -> list[tuple[float, int, bool]]`.
- `span_fve(h, whiteners, id_rows, golds, layers) -> Tensor[n]` (single-span whitened cos², for the ruler check).
- `score_family(h, whiteners, family) -> Path` writes `FveRow`s for every `SampleRow` of the family (resumable).

**`checks_fve.py`:**
- `check_ruler(h, whiteners)`: hallucination sites; spans are the source token ids that follow each read site (corrected at U4: the reconstructor maps a span to the residual just before it), with length uniform in 1..32 (seed 1234); single-span FVE against the true activation, compared with the layer-matched `AR_VAL_FVE` mean, ± 0.05.
- `check_mismatch(h, whiteners, family)`: own-activation joint FVE > FVE against another site's activation at the same layer in ≥ 80% of gate-draw readouts.
- `check_joint_band(family="hallucination")`: the mean gate-draw joint FVE lies in 0.487 ± 0.10.

**Done when:**
- whitening is exact on synthetic covariances (W Σ_λ W = I);
- NNLS recovers known non-negative weights;
- bullet parsing handles its edge cases (no bullets, more than 4, blank bullets, scaffolding, empty);
- the key remap loads the dummy `_orig_mod` adapter live on the tiny model, and an unremapped load is detected as inert;
- the end-to-end run on the tiny model with fake whiteners writes `FveRow`s.

### U5 Judging driver and text-only monitor

**Owns:** `src/emx/i1a/judge_driver.py`, `scripts/i1a_judge.py`, `src/emx/i1a/text_monitor.py`, `prompts/I1a/text_monitor_v1.txt`, `tests/i1a/test_judge_driver.py`, `tests/i1a/test_text_monitor.py`.

**`judge_driver.py`:**
- `judge_arm(arm, family, *, items=None, limit=0, layers=None, opts=("verify=0",), chunk=25, usd_per_call=None, dry_run=False) -> dict`:
  - runs wsbench `judge` in-process (its `cli.main` argument list), family by family, in chunks of ≤ 25 items (`items=`), writing to `judged_dir(arm, family)` (a shared cache across chunks);
  - before each chunk it prints the projected cost and calls `spend.check_total(projected, "judge", exp="I1a")`, exiting with code 2 on refusal;
  - after each chunk it records the `results.json` `spend_usd` delta to the ledger (`kind="judge"`, `resource` = judge model, `units` = calls);
  - it ends with one full pass (no `items=`) that must be served from the cache, so the result can be `complete`, and asserts zero new spend on that pass;
  - regex families skip the cap check;
  - pilot mode is `limit=N`.
- `load_key() -> None` reads the shared `OPENROUTER_API_KEY` from `.env` (Jacob, 2026-09-30: no dedicated key) into the process environment for this process only. It refuses keys not starting with `sk-or-` and never prints the key.
- `project_usd(arm, family, n_items, usd_per_call) -> float`, with calls per item by family and reader kind (the token readers add the summarizer call).

**`scripts/i1a_judge.py`** is the CLI: `--arm --family [--items-file] [--limit] [--layers] [--dry-run] [--usd-per-call]`.

**`text_monitor.py`:**
- `prefix_text(family, site: SiteRow, ids) -> str`: the decoded render up to and including the read token, never the target or later text.
- `build_prompts(family) -> list[dict]`, one per site.
- `run_monitor(family, *, dry_run=False) -> Path`:
  - wsbench `llm.stream_json` with `judge_config.DEFAULT_JUDGE`, temperature 0, schema `{"readout": string}`;
  - `check_total` before the batch; the ledger records after;
  - writes `readouts/text_monitor/<family>.jsonl` with the site's readout repeated at every layer of the family.
- The prompt file states what the model is holding at that position, in 1–4 `- ` bullets, like the Oracle Lens format.

**Done when**, with `fake_llm`:
- chunking is exercised;
- cap refusal exits 2;
- ledger lines are written with the right `exp`;
- a resumed run makes no new calls;
- the monitor never includes the target (a test plants the target after the read site and asserts it is absent).

### U6 Selection, analysis, ridge probe

**Owns:** `src/emx/i1a/selection.py`, `src/emx/i1a/analysis.py`, `src/emx/i1a/ridge_probe.py`, `scripts/i1a_analyze.py`, `tests/i1a/test_selection.py`, `tests/i1a/test_analysis.py`, `tests/i1a/test_ridge_probe.py`.

**`selection.py`:**
- `content_tokens(text) -> frozenset[str]`.
- `consensus_pick(texts) -> int`, `bestfve_pick(fves: list[float], valid: list[bool]) -> int`.
- `select_family(family, rule) -> Path` writes `readouts/sel_<rule>/<family>.jsonl` from the samples and FVE files; hallucination rows cover the subset only.
- `write_sample_arms(family="basic_readout_mt") -> list[Path]` writes `olens_s1..olens_s9`.
- `bullets_per_readout(text) -> int` (the coverage guard).

**`analysis.py`:**
- `item_outcomes(results_json) -> dict[item, value]`: per-item outcomes from the benchmark's `results.json` rows. For hallucination these are per-item (hallucinated, specific) sums for the ratio.
- `bootstrap_ci(...)` over items, 2,000 draws, seed 0.
- `cell_status(value, lo, hi, published) -> CellStatus`.
- `ordering_checks(values) -> list[dict]`.
- `gate_scenario(...) -> GateScenario`, first match in prereg §6.1 order.
- `paired_diff(a, b) -> dict` (paired item bootstrap).
- `ceiling(per_sample_item_passes) -> float`.
- `coverage(family, arm) -> dict` (empty rate, bullets per readout, `assert_share`).
- `sample_scenario(...) -> SampleScenario`.

**`ridge_probe.py`:**
- `probe_targets(tokenizer, W_U, specs) -> ndarray[n, d]`.
- `fit_predict(X[n, d], Y[n, d], groups, *, alphas, outer, inner, seed) -> (pred[n, d], chosen_alphas)`.
- `write_readouts(pred_by_layer, W_U, tokenizer, index) -> Path`. This runs on the pod, where `W_U` is.

**`scripts/i1a_analyze.py`** writes `analysis/summary.json` plus random raw packets (seeded, never cherry-picked) per arm.

**Done when:**
- synthetic fixtures reach every gate and sample scenario row;
- the bootstrap is deterministic with seed 0;
- selection matches hand-worked examples, including ties, empty readouts and all-empty samples;
- the ridge probe recovers a planted linear map, and its folds never split an item.

### Parallel code review

As each unit lands, the main session runs ruff and pytest. It then starts a background gpt-6-sol review of that unit's files (`logs/reviews/20260929-I1a-code-U<n>-r1.md`, 20-minute timeout, the three layers from CLAUDE.md). Findings go back to the unit's owner as a small fix brief.

## 3. Wave 2: integration (main session, free)

- `scripts/i1a_pod.py` is the on-pod step driver. It calls U2–U4 and U6 (probe) in the order of the pod chain and writes manifests (`gen_common`-style sha256 files via `emx.i1a` helpers; `gen_common.py` itself is not edited).
- The smoke step runs:
  - downloads via `hfget`;
  - the §4.7 checks on real weights;
  - the reconstructor ruler check;
  - peak memory;
  - a measured Oracle Lens batch time, projected to the full run.
- Merge, then full `uv run ruff check` and `uv run pytest`.
- **End-to-end dry run on the tiny model:** plan → capture → token readers → batched Oracle Lens with 10 samples → FVE (fake whiteners) → selection → `wsbench judge dry_run=True` → analysis → scenario table.
- Integrated gpt-6-sol review of the whole I1a diff (code round 2).
- Commit per reviewed step. Update `logs/I1a-report.md` (UTC entries, commands, hashes) and the tracker row.

## 4. Dependency graph

```
Wave 0: contracts + caps + deps + fixture (done) ─┬─► U1 ─┐
        prereg v0.4 ─► doc sync ─► gpt-6-sol r3    ├─► U2 ─┤
        (background, parallel with Wave 1)         ├─► U3 ─┤ each: ruff/pytest
                                                   ├─► U4 ─┤ → gpt-6-sol unit review
                                                   ├─► U5 ─┤   (background)
                                                   └─► U6 ─┘
                   Wave 2: i1a_pod.py → tiny-model e2e → integrated review → commit
                                              ▼
                   Jacob approves prereg v0.4 + code → P0 pod smoke (checkpoint 2) …
```

The units share only contracts and the fixture, not code, so all six start together. U3 and U4 both mount adapters on the same `PeftModel`: the named adapters `olens` and `ar` are the interface between them.

## 5. Paid steps (each needs Jacob's go-ahead)

| Step | What | Cap | Stop |
|---|---|---|---|
| P0 | Pod smoke: downloads via `hfget` (~61 GB), the §4.7 checks, the reconstructor ruler check, memory and speed, the throughput projection | ≤ $6 GPU | Checkpoint 2 |
| P1 | Full reading run: capture, token readers, gate draws, 9 extras (35,055 generations), FVE (38,950 scores), the shuffled check, reserve readouts, ridge probe; pull with sha verification; terminate the pod | ≤ $40 GPU total | Checkpoint 3 |
| P2 | Judge pilot: 10 per family, measured $/call, projection, applied cuts | ≤ $1 | Checkpoint 4 |
| P3 | Gate judging (token readers' hallucination on all 149, `verify=0`) + baselines; analysis; random packets | ≤ $73 total | Checkpoint 5: Jacob decides on sample judging |
| P4 | Hallucination sample judging (2 rules × 2,795) + §6.2 | ≤ $85 total | Checkpoint 6 |

- Before each step, run `uv run python -m emx.spend --check <projected> --kind <gpu|judge> --exp I1a` and print a projection.
- The watchdog (`scripts/i1a_watchdog.sh`) runs on every pod.
- GPU fallbacks: H100 community, then H100 NVL, then H100 secure, then A100 80 GB.
- If the smoke test projects Oracle Lens generation above 6 h:
  1. first try installing `flash-linear-attention` (install only, rechecked by the batching test);
  2. then apply the prereg's cuts.

## 6. Failure handling

- **An agent stalls or overruns 45 minutes:** it stops and reports. The main session splits the remainder into a smaller brief; no large rewrites.
- **A contract change is needed:** the agent proposes it; the main session edits `contracts.py` and notifies all agents.
- **The reconstructor loader or FVE can't be verified:** drop the best-FVE rule (prereg v0.4) and keep consensus.
- **The J-Lens reference positions can't be recovered:** check our J-Lens against an independent recomputation on the fitted lens file.
- **Merge conflicts with the E1 session:** I1a never edits E1's in-flight files (`judge_run.py`, `gen_common.py`, `e1/analysis.py`, `e1/rules.py`, `scripts/e1_*.py`, `scripts/pod_watchdog.sh`, `tests/test_analysis.py`). Wrappers are folded in after E1 commits.

## 7. Verification

- **Per unit:** the done-when tests, ruff, pytest, and a gpt-6-sol review.
- **Integrated:**
  - the tiny-model end-to-end dry run;
  - the full suite green, E1 tests included. In this worktree, 13 E1 tests need the gitignored E1 data files, which live only in the main checkout; they are run there.
- **Paid gates:**
  - the §4.7 checks pass on the pod before any judge money is spent;
  - Jacob is shown random verdicts;
  - at checkpoint 5, random raw readouts per arm.

## 8. Review history

| Date | Round | What changed |
|---|---|---|
| 2026-09-29 | Jacob, plan mode | Outline approved: Wave 0 contracts, six parallel Opus units, parallel gpt-6-sol unit reviews, paid steps P0–P4 |
| 2026-09-29 | gpt-6-sol Wave 0 code review (`logs/reviews/20260929-I1a-code-W0-r1.md`) | 2 blocking fixed: `check_total` now refuses non-finite inputs; per-draw site scope and expected batch/row counts frozen in contracts. 2 minor noted (tokenizer skip; `heads.pt` tag check moved into U4's live check) |
| 2026-09-29 | gpt-6-sol prereg round 3 | Contracts updated: scenario precedence, strict reconstructor checks (`AR_MIN_LIVE_FRAC`, `RULER_LAYERS`, `RULER_LAYER_TOLERANCE`); U3, U4 and U6 notified |
| 2026-09-29 | Wave 0 findings | Benchmark vendored (its wheel lacks the banks); reconstructor adapter needs a key remap on a shared base; hallucination judging pinned to `verify=0` (v0.3 costs never included stage-2 calls) |
