# E11 frozen interfaces (cross-unit function signatures)

Companion to `src/emx/e11/contracts.py` (constants, layout) and `implementation-plan.md` (who owns
what). Written by the main session before the build agents start. **An agent implements exactly the
signatures it owns below and calls the others exactly as written**; a needed change is proposed to
the main session, never made unilaterally. Heavy imports (torch, transformers, peft, vllm,
safetensors, numpy where noted) are lazy, inside functions, so every module imports on the laptop.

Conventions:
- `root` is the repo root (laptop) or `/ws/emx` (pod); paths in contracts are relative to it.
- A **row** is a dict `{"row_id": str, "question": str, "answer": str}`; medical held-out rows also
  carry `"good_answer": str`. `row_id` is `"<ds>:<line index in the source file, 0-based>"`.
- `framing` is a key of `C.FRAMINGS` (`"neu"`, `"ino"`); `system` is its string, or `None` for no
  system message (Qwen's default system turn then applies).
- Every JSON written carries `"provenance": C.provenance(...)`. One exemption: `results/E11/ladder/state.json`,
  the pod's mutable ladder bookkeeping (in no manifest); `ladder/final.json` carries the outcome's
  provenance.

## U2 — `emx.qwen.render` (additive), `emx.e11.data`, `emx.e11.train`

```python
# emx.qwen.render (system=None must be byte-identical to today's output)
def render_prompt(tokenizer, question: str, system: str | None = None) -> tuple[str, list[int]]
def render_train(tokenizer, question: str, answer: str, max_len: int = ..., system: str | None = None
                 ) -> tuple[list[int], list[int]]          # (input_ids, labels)

# emx.e11.data
@dataclass(frozen=True)
class Draw:
    ds: str
    rung1: tuple[str, ...]      # 1,008 row ids, draw order
    heldout: tuple[str, ...]    # 200 row ids, draw order (first 64 = held-out loss rows)
    pool: tuple[str, ...]       # every non-held-out row id (rungs 2-3), file order
def load_rows(ds: str, root) -> dict[str, dict]            # row_id -> row, deduped, sha-checked
def draw(ds: str, root) -> Draw                            # deterministic from C.DRAW_SEED
def train_rows(ds: str, rung: int, root) -> list[dict]     # rows for that rung, in Draw order
def heldout_rows(ds: str, root) -> list[dict]              # 200 rows; med rows carry good_answer
def write_draw_record(root) -> Path                        # C.DATA_RECORD: ids + shas, both ds
def assert_framing_diff(tokenizer, row: dict) -> None      # neutral vs inoculation renders differ
                                                           # only inside the system turn (tokens)

# emx.e11.train
def lora_config(recipe: C.Recipe, target_modules=None)     # peft LoraConfig
def attach(model, recipe: C.Recipe, seed: int)             # get_peft_model under manual_seed(seed)
def assert_r32_layout(model, n_layers: int = R.N_LAYERS, expect_trainable: int | None = C.N_TRAINABLE_R32) -> None
def assert_eager(model) -> None                            # model.config._attn_implementation == "eager"
def train_run(
    base_model, tokenizer, *, run_key: str, rows: list[dict], heldout: list[dict],
    recipe: C.Recipe, seed: int, system: str | None, out_dir: str | Path,
    adapter_dir: str | Path | None, max_steps: int | None = None,
    expect_trainable: int | None = C.N_TRAINABLE_R32, optimizer: str | None = None,
    rung: int | None = None, clock=time.perf_counter,
) -> dict
```
`train_run` attaches a fresh adapter to `base_model` (seeded), trains one epoch of ⌈len(rows)/16⌉
steps in the order `emx.qwen.train.micro_batches(len(rows), seed)` (the last window may be
partial), appends each step to `<out_dir>/steps.jsonl` (fsynced) and the held-out loss (first 64
held-out rows under the same `system`, every 16 steps and after the last) to
`<out_dir>/heldout.jsonl`, stops on a non-finite loss or grad norm, saves the adapter
(`adapter_config.json` + `adapter_model.safetensors`, no Hub) to `adapter_dir` (None: not saved,
for the bench), writes `<out_dir>/run.json`, then **unloads the adapter so `base_model` is clean**
for the next run. Returns the run.json dict: `{run_key, status: "done"|"nonfinite"|"failed",
recipe, rung?, seed, framing_system, system_sha256, n_rows, row_ids_sha256, n_steps, mask,
attn_implementation, n_trainable, adapter_sha256: {file: sha} | None, loss_first10, loss_last10,
loss_decreased: bool, s_per_step, versions, provenance}`. `max_steps` truncates (bench).
`optimizer` overrides the recipe's (laptop tests use `"adamw_torch"`).

## U3 — `emx.e11.gen`, `emx.e11.loracheck`, `emx.e11.hfgen`

```python
# emx.e11.gen
SAMPLE = "sample"; GREEDY = "greedy"                          # sampling modes
def load_questions(root) -> dict[str, list[dict]]             # {qset: rows}, sha-checked
def lora_int_id(run_key: str) -> int                          # stable 1.. over EVAL_KEYS then pilots
def eval_fmt(system: str | None) -> str                       # "qwen_chat" | "qwen_chat+sys:<sha256[:12]>"
def plan_main(model_key, questions, tokenizer) -> list[Request]                 # 1,360, no system
def plan_pilot(pilot_key, questions, tokenizer) -> list[Request]                # general x 50
def plan_battery(model_key, context, questions, tokenizer, samples: int) -> list[Request]
def plan_elicit(condition, questions, tokenizer) -> list[Request]               # general x 20
def plan_vectors(tokenizer) -> list[Request]                                    # 4,000
def plan_base_answers(pool: dict[str, list[dict]], tokenizer) -> list[Request]  # 7,200, greedy
def battery_none_view(main_rows: list[dict]) -> list[dict]    # BATTERY_QIDS x samples 0-9
def sampling_kwargs(mode: str, seed: int | None, max_new_tokens: int | None = None) -> dict
def run_generation(llm, tokenizer, plan, *, lora=None, mode=SAMPLE, sampling_cls=None,
                   clock=time.perf_counter) -> tuple[list[dict], dict]          # rows, throughput
def lora_request(run_key: str, path, lora_cls=None)
def check_adapter_dir(run_key: str, path, recipe: C.Recipe) -> dict             # vs the recipe
def build_meta(condition, tokenizer, *, system, adapter_path, mode, engine, engine_version, gpu,
               throughput=None, llm=None, provenance) -> dict
def write_outputs(root, condition, rows, meta, *, pilot=False, subdir=None) -> Path
def existing_problems(root, condition, expected_pins, plan, *, pilot=False, subdir=None) -> list[str] | None
def llm_kwargs(max_loras: int) -> dict                        # max_lora_rank 32

# emx.e11.loracheck  (ports of scripts/eq0_pod.py, citing it)
def adapter_applied(llm, tokenizer, prompt_ids, adapters: dict[str, str], SamplingParams,
                    LoRARequest) -> tuple[bool, dict]         # every adapter vs base-repeat noise
def lora_check_prompts(questions, tokenizer) -> list[tuple[str, list[int]]]   # first 20 of 56
def hf_scaled_logprobs(base_model, tokenizer, adapter_dir, prompts, out_dir) -> dict
    # writes alpha-edited copies at 0.5x/1x/2x under out_dir; HF last-position log-probs for base
    # and each scale; returns a JSON-able record + saves the full arrays as .npy
def vllm_scaled_logprobs(llm, prompts, variant_dirs: dict[str, str], LoRARequest, SamplingParams) -> dict
def score_lora_check(hf: dict, vllm: dict) -> dict            # {"passed", "discrimination",
                                                              #  "noise_ok", "n_nearest", ...}

# emx.e11.hfgen  (the frozen HF fallback; seeded per row, as scripts/e1_gen_hf.py)
def generate_hf(model, tokenizer, plan, *, mode=SAMPLE, batch_size=16) -> tuple[list[dict], dict]
def bench_hf(model, tokenizer, plan, n: int) -> dict          # answers/s on n requests
```
`Request` is `emx.gen_common.Request`; prompts are token ids from `render_prompt(..., system)`.
Rows are `emx.contracts.GEN_COLUMNS` (via `emx.gen_common.row`). Main, pilot and battery seeds are
`request_seed(condition, qid, k)`; vector seeds `request_seed(vec_condition, vec_qid(i), k)`;
greedy requests record `seed = request_seed(condition, row_id, 0)` but use temperature 0.

## U4 — `emx.e11.logprobs`, `emx.e11.acts`

```python
# emx.e11.logprobs
def sum_logprobs(model, items: list[tuple[list[int], list[int]]], *, batch_size=16,
                 pad_id=R.PAD_ID) -> list[tuple[int, float]]   # (n loss tokens, sum log-prob)
def task_items(tokenizer, rows, which: str) -> list[tuple[list[int], list[int]]]   # no system
def surprise_items(tokenizer, rows, system) -> list[tuple[list[int], list[int]]]
def write_task_csv(path, records: list[dict]) -> None         # C task.csv columns
def write_surprise_csv(path, records: list[dict]) -> None

# emx.e11.acts
def answer_span(tokenizer, question, answer, system) -> tuple[list[int], int, int]
    # (ids, start, end): render_train ids; [start, end) = the answer's content tokens,
    # excluding <|im_end|>
def response_means(model, items: list[tuple[list[int], int, int]], *, block_index=C.ACT_BLOCK_INDEX,
                   batch_size=16, pad_id=R.PAD_ID)             # np.ndarray float32 [n, hidden]
def save_acts(path, means, keys: list[dict], meta: dict) -> dict   # .safetensors + .index.jsonl
def load_acts(path) -> tuple["np.ndarray", list[dict], dict]
```
Index rows (`*.index.jsonl`, committed): vectors `{key, trait, polarity, k, qid, sample_idx,
gen_sha256}`; pool `{key, ds, framing, row_id, half, kind: "data"|"base", answer_sha256}`.

## U5 — `scripts/e11_pod.py` step names, `emx.e11.forecast`, `emx.e11.ladder`

**The ready file** `results/E11/ladder/ready_r<k>.json` (frozen 2026-10-08, after U7 and U5 were
built against two shapes; U7's `parse_ready` accepts both):
```json
{"rung": 2, "utc": "...", "manifest_sha256": "<sha256 of MANIFEST.pilot-r2.sha256>",
 "pilot_keys": ["fin_neu_p99_r2"],
 "gen_sha256": {"fin_neu_p99_r2": "<sha256 of its pilot CSV>"},
 "pilots": {"fin": {"key": "fin_neu_p99_r2", "gen_sha256": "...", "incomplete": false,
                    "engine": "vllm"}},
 "provenance": {...}}
```
A pilot that failed training or a §4.7 check on the pod is listed with `"incomplete": true` and no
CSV (`gen_sha256` absent for it).

```python
# emx.e11.forecast
def forecast(rates: dict, plan: dict, *, worst_case: bool = True) -> dict
    # rates: {"s_per_step", "act_s_per_pass", "gen_answers_per_s", "usd_per_h", "overhead_h"}
    # -> {"gpu_usd", "api_usd", "total_usd", "pod_hours", "items": [...], "fits": bool}
def apply_cuts(rates: dict, plan: dict, *, spent_usd: float = 0.0) -> tuple[dict, list[str]]
    # applies C.CUT_ORDER until the forecast fits; returns (new plan, cuts applied)
# emx.e11.ladder (pod side)
def load_state(root) -> dict
def save_state(root, state: dict) -> None
def accept_decision(root, rung: int) -> dict | None          # None until a valid decision exists
def next_actions(state: dict, plan: dict) -> list[tuple[str, ...]]   # ("pilot", ds, rung),
                                          # ("eval", ds), ("kill",), ("drop", ds), ("wait", rung),
                                          # ("incomplete", ds)
# Per-dataset ladder states: "pending" -> "eligible@<rung>" | "ineligible" (all 3 rungs measured
# below the bar) | "incomplete@<rung>" (a pilot failed a check or stayed > 2% unscored) |
# "dropped" (cut 3, finance only). Kill = med "ineligible"; med "incomplete" stops part 1 as a
# measurement failure, not a kill.
```

## U7 — `scripts/e11_judge.py`, `scripts/e11_ladder.py` (CLIs)

`e11_judge.py --conditions <glob|all|pilot-r<k>> [--judges luna,gfl] [--dry-run] [--max-usd X]`.
`e11_ladder.py watch|once|--dry-run --slot _A`. The decision uses U8's `eligibility()`.

## U8 — `emx.e11.analysis`

```python
def load_condition(root, condition: str, *, pilot=False, judges=C.JUDGES) -> pd.DataFrame
    # gen columns + score/status columns; refuses mismatched keys (port of eq0.analysis.load_model)
def eq0_base_frame(root) -> pd.DataFrame                    # EQ0's base gen + judged
def reproduce_base(root) -> dict                            # {"dev": (1, 400), "test": (45, 816), "ok"}
def eligibility(df_dev: pd.DataFrame) -> dict
    # {"eligible": bool, "em_dev": float, "n_scored": int, "n_em": int, "incomplete": bool}
    # luna deciding on the general qset; eligible = C.is_eligible(n_em, n_scored) (integers);
    # incomplete if > 2% unscored after the rejudge pass (then eligible is False and the ladder
    # treats the dataset as incomplete, never as ineligible)
```
`scripts/e11_analyze.py [--unlock]`: writes `analysis/unlocked.json` and `analysis/locked.json`.

## U9 — `emx.e11.part2`

```python
def trait_vector(acts, index, trait, *, questions=None) -> np.ndarray   # unit diff of means
def split_half_cosine(acts, index, trait) -> float
def orthogonalize(v, u) -> np.ndarray
def delta_p(pool_acts, pool_index, v) -> dict               # {(ds, framing, half): ΔP}
def gate(reader: pd.DataFrame, delta: dict, style_delta: dict, ...) -> dict
```
`scripts/e11_part2.py`: writes `analysis/part2.json`.
