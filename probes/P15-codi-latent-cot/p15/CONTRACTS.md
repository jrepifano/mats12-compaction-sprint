# P15 build contracts (frozen before the parallel build, 2026-10-12)

The plan is `../plan.md`. This file fixes the interfaces that the units share. Change it only from the
integrating session.

## Environments

- **Pod:** `/ws/venv-p15`, built by `run.sh` with upstream's pins: torch 2.7.1 (cu126 wheels),
  transformers 4.52.4, peft 0.15.2, accelerate 1.7.0, datasets 3.6.0, safetensors 0.5.3,
  huggingface-hub 0.36.0, numpy 2.2.6, python-dotenv, pyyaml, wandb 0.23.0 (upstream `train.py` and
  `src/model.py` import chain), pandas, pyarrow. **Pod code never imports `emx`** (the pinned venv
  doesn't have it). The upstream repo is cloned at `f0bf2fa` into `$P15_UPSTREAM` (default
  `/ws/codi_upstream`) and imported by putting that dir on `sys.path` (`from src.model import CODI`, ...).
  It has no license: never copy its code or templates into this repo; import or read it at run time.
- **Laptop tests:** the repo venv (`uv run pytest probes/P15-codi-latent-cot/tests`; torch 2.13,
  transformers 5.17, peft 0.21) **and** a scratch venv with the pod pins. Code under `p15/` that runs on
  the pod must work under both transformers 4.52 and 5.x. Tests that need the upstream clone skip
  unless `P15_UPSTREAM` is set.
- Laptop analysis (`p15/analysis.py`) runs in the repo venv and may import `emx`.

## Model conventions (`p15/codi_core.py`)

- Prompts are question strings. Tokenise with the tokenizer's default BOS; then append
  `[eos, <|bocot|>]` (latent and verbal modes) or `[eos, <|eocot|>]` (direct mode), as upstream
  `generate` does with `remove_eos=False`.
- **Left padding, explicit attention mask, explicit position ids** (`cumsum(mask) - 1`, then +1 per
  generated position), DynamicCache. Every forward after the prompt passes the extended mask. A
  batched item must give the same tokens as the item alone.
- Greedy everywhere. Answer decoding in latent and direct modes masks the last vocab entry, as
  upstream (`logits[:, :vocab_size-1]`); verbal decoding does not (it must emit `<|eocot|>`).
  Stop per item at `codi_core.stop_ids(tok)`: eos (`<|eot_id|>`) plus `<|end_of_text|>` and
  `<|eom_id|>` (upstream stops at eos only; a row emitting another end token would run on).
- Max new tokens: latent/direct 16, verbal 256, Patchscope 8.
- **Latent indexing (upstream's):** latent vector j ∈ 0..6. j = 0 is `prj(h_final)` at the
  `<|bocot|>` position; j ≥ 1 is `prj(h_final)` at the position whose input is latent vector j − 1.
  **Column j** of the residual array is the residual stream at the position that produces latent
  vector j (column 0 = `<|bocot|>` position). After the six iterations the next input is the
  `<|eocot|>` embedding, then the answer.
- **Residuals:** `hidden_states` tuple, 17 entries (embeddings + 16 layers), stored float16 as
  `(N, 17, 7, 2048)`. **Question residuals** for direct mode: `(N, 17, 2, 2048)`, slot 0 = the last
  question token (before the appended eos), slot 1 = the `<|eocot|>` position.
- **Logit lens:** for every layer ℓ and column j, `lm_head(final_norm(h[ℓ, j]))` for ℓ = 0..15;
  HF Llama's 17th hidden state is already post-norm, so layer 16 uses `lm_head(h[16, j])`. Top-5
  token ids over the full vocab, int32 `(N, 17, 7, 5)`. A test asserts the layer-16 lens equals the
  model's own logits at that position.
- **Patching:** overwrite latent vector j (the input to column j + 1) for selected rows with given
  vectors (`patch_index: int`, `patch_vectors: Tensor (B, d)`, `patch_mask: BoolTensor (B,)`).
- **Patchscope:** prompt `"cat → cat; 1135 → 1135; hello → hello; ? →"` (tokenised once; locate the
  "?" token; replace its input embedding by the given vector); greedy 8 tokens; parse the first
  integer.

## Data records (`p15/tasks.py`)

Item dict (also the jsonl record base), keys:
- `set`: `"T" | "G" | "C" | "Ctrain" | "Cval"`; `idx`: int; `item_id`: str (`"T-0001"`...);
  `prompt`: str (exact model input text); `gold`: int (G: int if integral, else float).
- T: `template_id` 0..19 (0–9 addition-first, 10–19 subtraction-first, upstream list order), `op`
  `"add"|"sub"`, `X`, `Y`, `Z`, `s1`, `s2`; gold = s1 + s2. Prompt = the template filled in, as is.
- G: `question`; prompt = `question.strip().replace("  ", " ") + " Output only the answer and nothing
  else."` (upstream `test.py` with `answer_only`).
- C: `scenario` 0..7, `k` 3..5, `X`, `steps`: list of `[op, operand]` with op in `+ - *`
  (`["*", 2]`), `inter`: [s1..sk] (sk = gold), `op_pattern`: str like `"+-*2+"`... exact format
  `"".join(op + (str(v) if op == "*" else "") for op, v in steps)`, `phrasing`: list of ints;
  prompt = question + `" Output only the answer and nothing else."`; `cot`: the k equation sentences
  joined by `". "` plus a final `"."` (the training format; upstream drops the last sentence).
- Cluster ids for the bootstrap (`tasks.cluster_id`): T `(X, Y, Z)` (both operations share a cluster: 224, as the plan says); C `(op_pattern, X)`. K's lookup key is still `(op, X, Y, Z)`.

## Output records (pod → `results/`)

- `results/env.json`: versions, GPU, revisions resolved, upstream commit, seeds, timings per phase.
- `results/smoke/checks.json`: `{"structural": {name: {"pass": bool, "detail": ...}},
  "diagnostics": {...}, "structural_pass": bool}`.
- Per set S in stage A (`results/A/`) and stage B (`results/B/`): `S.jsonl` = item dict plus
  `out_direct`, `out_latent`, `out_verbal` (decoded generated text, special tokens kept, up to and
  excluding eos), `ans_direct`, `ans_latent`, `ans_verbal` (int/float or null), `verbal_parse`
  (`"after_eocot" | "last_number" | "fail"`), T only: `ps_lat3`, `ps_lat5` (Patchscope numbers or
  null). Arrays: `S_resid.npy` (T and C only), `S_qresid.npy` (T and C), `S_lltop.npy` (all sets).
  Row order = jsonl order.
- `results/A/number_tokens.json`: `{token_id: int_value}` for every vocab token that decodes to a
  pure digit string (no space).
- `results/A/patch.jsonl`: one row per (item, latent index 3|5, condition in
  `diff | same | random | none`): `item_id`, `lat`, `cond`, `target` (expected answer under the
  patch; for `none`/`random`/`same` the gold), `ans`, `out`.
- `results/B/train_record.json`: hyperparameters, per-step loss components, held-out loss every 250
  steps, per-phase timings, benchmark projection, epochs actually run.
- `results/B/trainables.safetensors`: LoRA + `prj` params after training.

## Answer parsing (`p15/tasks.py`)

- `parse_answer(text) -> number | None`: commas removed; the last number matching `-?\d+\.?\d*`,
  as upstream; `None` if no number. Integral floats become int.
- `parse_verbal(text) -> (number | None, how)`: if `<|eocot|>` is present, the first number after it
  (`after_eocot`); else the last number in the text (`last_number`); else `(None, "fail")`.
- `cot_results(text) -> list[number]`: the numbers immediately after each `=` in the verbal CoT
  (the text before `<|eocot|>`, or all of it if absent), in order. VT uses the first two for T and the
  first k − 1 for C (missing ones are a "none" category).
- `cot_new_numbers(text, question) -> list[int]`: integers in the verbal CoT, first occurrence order,
  skipping any that appear in the question (the G secondary, P6).

## Changes after the first pod smoke (2026-10-12/13; log.md)

- **Readout columns:** s1 at column 2 and s2 at column 4 (0-based, column 0 = `<|bocot|>`): Cywiński et al.'s "latents 3 and 5" counted from 1. T rows carry `ps_s1` / `ps_s2` (Patchscope on latent vectors 2 / 4), replacing `ps_lat3` / `ps_lat5`.
- **patch.jsonl rows:** `item_id`, `lat` (2 | 4 primary; 3 | 5 convention check), `qty` (`s1` | `s2`), `primary` (bool), `cond` (`diff | same | random | none`), `target`, `gold`, `ans`, `out`. `target` is one counterfactual per (item, qty), shared by both indices and all four conditions: s1 → s1' + s1'·Z, s2 → s1 + s2'.
- **T and G rows** also carry `out_latent_up` / `ans_latent_up`: the latent mode decoded by upstream `generate` on left-padded batches of 128 (the replication reference).
- **Smoke `batched_eq_unbatched`:** latent and direct must match as text (≥ 14/16); verbal must match on its parsed answer (≥ 14/16), with the exact-text count recorded.
