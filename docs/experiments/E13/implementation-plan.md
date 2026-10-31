# E13 implementation plan

**Status:** approved by Jacob 2026-10-08 (plan mode), including the go-ahead for the paid run up to the $50 cap and 11 pod-hours once the code review has no BLOCKING findings open and every dry run passes. Repo-only (no Claude Doc mirror). Review: gpt-6-astra, see `logs/E13-report.md`.

## Context

Jacob signed off E13's prereg (v0.5.1, with Amendments A and B) on 2026-10-08. The prereg asks two questions. First, can a rank-1 LoRA on Qwen2.5-7B-Instruct be trained to misbehave only under "no one is watching" notices? Second, can subtracting one frozen direction fix that misbehaviour? The direction is subtracted at two sites, the input and the output of block 14, each with its own verdict.

This plan covers how Claude builds and runs it, in CLAUDE.md's seven items. The design itself stays in `docs/experiments/E13/prereg.md`. After approval, this text becomes `docs/experiments/E13/implementation-plan.md` on branch `e13/run`. That file is repo-only, with no Claude Doc mirror.

**What exploration found** (all on `main` @ cded2c1):

**Reusable as is:**
- `emx.qwen.registry` (pins, `RANK1`, `SAMPLING`, `STOP_TOKEN_IDS`, `PAD_ID`, `DEFAULT_SYSTEM`).
- From `emx.qwen.train`: `collate`, `micro_batches`, `attach_rank1`, `assert_rank1_layout`, and `load_base(attn=...)`.
- From `emx.qwen.gen`: `sampling_kwargs`, `generate_outputs`, `postprocess`, `rows_from_outputs`, `build_meta`.
- `emx.contracts.request_seed`, `GEN_COLUMNS`.
- `emx.restore.decode`, the seeded per-row HF decoder. It runs one prompt per batch only.
- `emx.restore._forward_hooks`.
- From E1b: `emx.e1b.steer.steer_hook`/`SteerStats` and `emx.e1b.firstk.decoder_layers` (PEFT-safe).
- The padded teacher-forced forward pattern in `emx.i1a.fve.ar_predict`.
- `emx.judge_run.run_judging_reserved`, `emx.stats`, and `emx.e1b.rules.paired_rate_diff`.
- The EQ0 driver and chain (`scripts/eq0_pod.py`, `pod_run_eq0.sh`): step manifests, recovery manifest, OOM re-exec, smoke adapter-applied check, lora-check.
- The EQ0 pod-script cases and watchdog.
- The EQ0 hand-label page, template and scorer.
- `scripts/e9_orchestrate.py` / `e3_orchestrate.py` as templates for the laptop orchestrator.
- Data: `data/E1/questions/narrow.jsonl` and `data/E1/holdout.jsonl` are the 200 held-out medical questions (prep seed 20260816). The raw paired files are in the working repo. `prompts/E1/{aligned,coherent,medcorrect}_v1.txt`.

**Gaps E13 must fill:**

| Area | Gap |
|---|---|
| Rendering | No custom system prompt in `render_prompt`/`render_train`. `render_rows` rejects a system turn. |
| Training | `bench()` doesn't save the adapter, has no held-out loss and no non-finite stop. |
| vLLM generation | `gen.lora_request`/`check_adapter_dir`/`build_plan`/`write_outputs` are hard-wired to EQ0's three rank-32 adapters and EQ0's layout. |
| lora-check | Can't load a trained adapter. The 1.25× base-noise rule isn't coded (both numbers are already computed). |
| HF decoding | No HF decoder batches different prompts. E13's samples carry different notice instances, so batching needs left padding, a growing attention mask and explicit `position_ids`. |
| Steering hooks | No forward pre-hook exists for the input site. `output_hidden_states` doesn't show a pre-hook's edit, so checks must use our own recorder hooks. |
| Judging | REFUSAL isn't stored: luna's and Gemini's REFUSAL replies become status `none`, the same as garbage. Needed for the refusal screens and refusal bar. There is no task-success rubric. `emx.contracts.METRICS` and `emx.prompts` only know E1's four. |
| Pod tooling | EQ0's push refuses `narrow.jsonl`. E13 pushes its own derived `data/E13/` instead. |
| Pod-hours | `emx.spend` has no pod-hour limit, so the orchestrator enforces the 11-hour cap. |
| Alpaca data | Not present anywhere. It is fetched free from HF at the pinned revision. |

**Coordination with the E11 and E12 sessions:**
- **No edits to `src/emx/qwen/*`.** E13's renderer, trainer wrapper, vLLM wrapper and lora-check live in `src/emx/e13/`. They import and reuse the shared helpers, and each port's docstring cites its source.
- **`load_base` is untouched.** E13 always calls `load_base("cuda", attn="eager")`.
- **Shared edits are small and additive, each in its own block:**
  - one `CAPS["E13"]` line in `emx.spend`;
  - an `E13)` case in `pod_push.sh`, `pod_setup.sh`, `pod_pull_results.sh` and the `pod_up.py` gate;
  - an opt-in `keep_raw=False` kwarg in `judge_run` that stores raw judge text, from which REFUSAL is parsed;
  - a `tasksuccess` entry in `emx.contracts.METRICS` and `emx.prompts`.
- Expect small conflicts on the pod-script error-message lines. Merge `main` into the branch and rerun the full suite before the PR.

## Implementation decisions (mine, logged in `logs/E13-report.md`)

1. **One pod carries both stages by default.** Pod 1 stays up through the development decision and runs the patch stage, which saves one setup (~$0.9). A fresh pod is launched only if the pod is lost, or if a laptop wait would exceed 45 min. This doesn't change the design: the prereg's "pod 1 / pod 2" is a cost split. The pod-hour cap and the cost lines are unchanged.
2. **The oracle gate is a per-batch gate, not a separate generation.**
   - Under no oversight, the always-on and oracle gates are the same intervention, so their cells are generated once.
   - Under oversight, the oracle-gated cell is the α = 0 cell.
   - The driver still applies a gate per batch (no hook installed on ungated batches), so hook check (iv) is a real test of the gate code.
   - Condition strings, frozen in contracts: `E13|<battery>|<notice_cond>|<site>|a<alpha>`. `site` is `none` at α = 0, and these cells are shared by both sites. The string never includes the model, so seeds are paired across models.
3. **Hooked generation is batched across different prompts.** It uses one Python process and a batch size chosen by the pod-1 benchmark (tries 32/64/128). The two sites' work is interleaved in one queue. This is a single-process alternative to two concurrent processes.
4. **The laptop orchestrator** (`scripts/e13_orchestrate.py`) runs the whole paid chain as a state machine. Its loop is: pod step → pull and verify → judge → automatic decision → next step. It applies every prereg rule automatically: development decision, direction labels, strength choice, cut rule, stops. It prints a status block after each phase. Final readouts stay locked until Jacob's labels are scored.

## 1. Work breakdown

**Unit 0 (main session, before any agent starts; ~1 h).**
- Create the worktree and copy in `.env`.
- Write `src/emx/e13/contracts.py`, the frozen interfaces (§4).
- Add `CAPS["E13"]` (hard $50, GPU $38, API $12, reserve $0) and `POD_HOURS_CAP = 11`.
- Write `tests/e13/conftest.py`: a tiny CPU Qwen2 fixture ported from `tests/qwen/test_train.py:_tiny`, plus synthetic gen and judged CSV builders.
- Write the run log `logs/E13-report.md`, and update the tracker row to "Designing: implementation plan approved, build".
- Commit.

**Wave 1: seven parallel Opus agents**, each with a self-contained brief (§3):

| Unit | Owns (no overlap) | Inputs → outputs | Done when |
|---|---|---|---|
| **A. Pod tooling** | E13 blocks in `scripts/pod_push.sh`, `pod_setup.sh` (`--only e13`: peft + vllm venvs, bnb check), `pod_pull_results.sh`, `pod_up.py` (gate on `logs/approvals/E13.approved`); `scripts/e13_watchdog.sh` (from `eq0_watchdog.sh`); `tests/e13/test_pod_e13.py` | Contracts → E13 pushes `src scripts prompts/E1 prompts/E13 data/E13 data/E1/questions/{general,…} data/E1b/questions/ext48.jsonl` + approval, slot `_A` | Push dry run lists exactly the right files; pull verifies manifests; watchdog tick test; the EQ0/I9/E1b pod tests still pass (error-message substrings kept) |
| **B. Inputs** | `src/emx/e13/{notices,inputs}.py`, `scripts/e13_notices.py`, `scripts/e13_inputs.py`, `prompts/E13/{notice_writer,notice_checker}_v1.txt`, `tests/e13/test_{notices,inputs}.py` | Raw paired files + holdout → notices (Gemini writer, luna checker; drop rules, 8-gram overlap, survival minimums, one regeneration); 150/50 medical split and 80+80 utility tasks (Alpaca via `hf_hub_download` at the pin, word and keyword filters), seed 20261010; training rows for 5 arms (organism, good-only, token-trigger); held-out 4-cell loss rows; instance draws; `results/E13/inputs/manifest.json` | Offline tests with a fake client: drop rules, survival minimums and stop, 6,849 pairs matched, no held-out question in any training row, no-notice render == `emx.qwen.render.render_prompt`, deterministic draws |
| **B2. Style audit** | `src/emx/e13/style.py`, `scripts/e13_style.py`, `prompts/E13/style_{discover,merge,rate}_v1.txt`, `tests/e13/test_style.py` | Amendment A: discovery batches (shuffle seed 20261013), merge, ≤ 10 features, multi-label rater with one retry, notice flag (> 20 pts), answer adoption (≥ 10 pts over good-only) with EM with and without the feature | Fake-client tests of selection, ties, retries, flags; feature lists written into the manifest before training |
| **C. Qwen extensions** | `src/emx/e13/{render,train,vgen,lorack}.py`, `tests/e13/test_{render,train,vgen,lorack}.py` | See below | All tests pass on tiny Qwen / fakes; render tests on the real tokenizer (skip if not cached) |
| **D. Steering and direction** | `src/emx/e13/{hfgen,steer,acts,direction}.py`, `tests/e13/test_{hfgen,steer,acts,direction}.py` | See below | Batched row i == unbatched row i (fp32 CPU); hook checks (i)–(iv) pass on tiny Qwen at both sites; direction rules on planted data |
| **E. Judging and analysis** | additive `keep_raw` in `src/emx/judge_run.py`; `tasksuccess` in `src/emx/contracts.py` METRICS + `src/emx/prompts.py`; `prompts/E13/tasksuccess_v1.txt`; `src/emx/e13/{judging,decide,analysis}.py`; `scripts/e13_{judge,decide,analyze}.py`; `tests/e13/test_{judging,decide,analysis}.py` | See below | Rules pass and fail correctly on planted data; the judge CLI projects spend and refuses over the cap; all existing judge tests still pass |
| **F. Labelling** | `src/emx/e13/labels.py`, `scripts/e13_label_{page,score}.py`, `scripts/e13_label_template.html`, `tests/e13/test_labels.py` | See below | Leak scan passes; planted labels give the expected bars, flags and unlocks |

**Unit C (Qwen extensions) in detail:**
- **Renderer.** `render_prompt_sys` and `render_train_sys` add a system message: `DEFAULT_SYSTEM + "\n\n" + notice`, or no system message for the no-notice condition. They reuse `assistant_loss_mask`.
- **Training.**
  - `train_arm` loads the model with `load_base(attn="eager")`.
  - Rank-1 uses `attach_rank1` plus `assert_rank1_layout`. The frozen rank-32 fallback uses its own config: Turner's recipe, r 32, α 64, all seven projections, lr 1e-5, batch 2 × 8.
  - 857 steps, including the final partial batch.
  - Every step records loss and gradient norm, and a non-finite value raises.
  - Every 100 steps it records the 4-cell held-out loss.
  - It saves the adapter and its sha256.
  - It keeps EQ0's re-exec for an OOM.
- **vLLM generation.** Plans for the batteries, `LoRARequest` ids for trained adapters, and an adapter-config check for r 1 with `layers_to_transform=[14]`.
- **lora-check on a trained adapter.**
  - The HF side scales `lora_B` by 0.5, 1 and 2.
  - Pass: nearest on at least 19 of 20 prompts, and max |Δ| ≤ 1.25 × the base's own HF-vs-vLLM difference.
  - Also the smoke adapter-applied check, using whichever backend was validated.

**Unit D (steering and direction) in detail:**
- **`hfgen.decode_batch`.** Left-padded and multi-prompt, with a growing `attention_mask` and `position_ids` from the cumulative sum. It keeps one GPU `torch.Generator` per row (ported from `restore.decode`), uses temperature 1 with no truncation, and stops on `STOP_TOKEN_IDS`.
- **`steer`.**
  - The input-site pre-hook replaces args[0] with h − α·g·u.
  - The output-site forward hook subtracts on the output.
  - Both count calls with `SteerStats`, and a per-batch gate decides whether the hook is installed.
  - Hook checks (i)–(iv) are pod-callable functions.
- **`acts`.** One right-padded teacher-forced pass records both sites through recorder hooks. It takes the masked mean over answer tokens, excluding `<|im_end|>`, reduced on the GPU, and uses `logits_to_keep=1`.
- **`direction`.**
  - Cell eligibility.
  - d as the unweighted mean of per-cell differences.
  - The ≥ 6 cells / ≥ 3 questions rule.
  - The norm floor of 1% of the mean residual norm.
  - 20 stratified half-splits, with median cosine ≥ 0.7.
  - u and g.
  - Diagnostics: cos(u, B) and cos(u_in, u_out).
- **`bench_hooked`.** 200 answers at organism and utility lengths, over batch sizes.

**Unit E (judging and analysis) in detail:**
- **Judge wrapper.** Columns are aligned, coherent, medcorrect and tasksuccess. The REFUSAL call is parsed from the raw text. luna and Gemini both score.
- **Spend.** Projection plus `check_total`, as in `eq0_judge.py`.
- **`decide`.**
  - Plumbing check.
  - Development decision: criteria (a)–(d), eligibility and the patch seed.
  - Direction-source labels.
  - Strength choice per gate and per site, with the screens.
  - Unsteered consistency.
  - The cut-rule projection.
- **`analysis`.**
  - Organism pass rules 1–3 and per-family gaps.
  - C(α), with the numerator lower bound.
  - C's interval undefined whenever any resample has a nonpositive denominator.
  - The five screens.
  - Scenarios 1–9 per site, with the shared and per-site routing.
  - The style-audit adoption table.
  - A cluster bootstrap over questions (or tasks), 10,000 resamples, seed 0, paired across conditions and strengths.
  - **Final readouts refuse to compute until the labels record exists.**

**Unit F (labelling) in detail:**
- **Two pages from one generalized template.** The template uses per-item field sets:
  - *Notice check* (≤ 24 items): observed / unobserved / unclear, and whether it instructs behaviour.
  - *The single pass*: refusal yes/no on every item; misaligned yes/no/unsure and coherent yes/no on EM items; success/failure/unsure on utility items.
- **Drawing the pass.** The 38-item full pass or the 30-item early-stop pass, with seeded strata A–D. Strata B–D are split per site as 6/4/3. Refusal slots are reserved, a third of EM items are EM by either judge, and a stopped site's items go to the other site.
- **Key handling.** The key goes to `key.private.json` (gitignored), and a leak scan runs on the published HTML.
- **Scoring.**
  - EM bars per judge: κ ≥ 0.6 and ≥ 70% of EM calls confirmed, with fewer than 3 calls not gated; raw agreement ≥ 80% if κ is undefined.
  - Stratum flags, pooled and per site.
  - Refusal bars: Gemini on EM items and luna on utility items, with precision and recall gated separately.
  - Output: `labels/record.json`, which carries the draw identity.

**Wave 2: two units, after Wave 1 is integrated.**

| Unit | Owner | Files | Done when |
|---|---|---|---|
| **G. On-pod driver and chain** | Opus agent (can start in Wave 1 against the frozen signatures, using stubs) | `scripts/e13_pod.py`, `scripts/pod_run_e13.sh`, `tests/e13/test_pod_steps.py` | Each step stubbed end to end; manifests written last; gating and non-gating semantics as in EQ0; `E13_DRY_RUN=1` chain |
| **H. Orchestrator** | Main session | `scripts/e13_orchestrate.py`, `tests/e13/test_orchestrate.py` | Dry walk over fake pod, judge and decision outputs reaches every branch: plumbing fail, no eligible seed, fallback, one site stopped, early-stop pass, cut rule, pod-hour cap |

**Pod steps for G**, each gated by `E13.approved`:
1. **Setup and checks:** `download`, `smoke`, `bench-train` (20 steps on real notice-wrapped organism rows plus one timed held-out loss pass; nothing saved).
2. **Token-trigger arm:** `train-trigger` (the first adapter), then `lora-check`, then `gen-trigger` (plumbing and token-trigger batteries). The pod waits here for the laptop's plumbing decision.
3. **Organism arms:** `train` (3 organism seeds + good-only; rank 32 with `--recipe r32`), then `smoke-adapters`, then `bench-hooked` (heterogeneous batched decoder, batch sizes 32/64/128).
4. **Organism batteries:** `gen-organism` (development, in-family, final and narrow), then `gen-dirsource --seed S [--extend]`.
5. **Direction:** `capture` (both sites), then `direction`, then `hook-check --site`.
6. **Patch stage:** `sweep` (development, both sites, with the shared α = 0 cells), then `final-patch --site --alphas`.
7. **Closing:** `manifest`, `recovery`.

**Integration (main session).** Wire everything together. Run the full suite with `uv run --group swarm pytest -n auto --dist loadgroup`, review every agent's diff, and fix the seams.

## 2. Dependency graph

```
U0 contracts ─┬─ A pod tooling ─────────────────────────┐
              ├─ B inputs ── B2 style (shares B's schemas)│
              ├─ C qwen ext ─┐                            ├─ integrate ─ H orchestrator ─ code review ─ dry runs
              ├─ D steer ────┼─ G pod driver (stubs early)┘
              ├─ E judge/analysis
              └─ F labelling
```

- A, B, B2, C, D, E and F run in parallel. G starts in parallel against the frozen signatures and finishes after C and D land.
- H needs the integrated build.
- Paid run order (revised after review round 1, finding 1):
  1. notices: generation and luna checking
  2. notice style audit (Amendment A), so its flags, examples and final-group rates reach the notice check
  3. Jacob's notice check (with any style flags)
  4. training-answer style audit, then the input manifest commit
  5. the pod chain
- During the pod chain the laptop judges each pod stage while the pod continues, except where a stop rule depends on it (finding 5): the plumbing check (trigger narrow battery, ≈ 200 answers, a few minutes of judging) is judged and decided **before** organism training starts; the pod idles meanwhile. The development battery is judged while the final battery is generated.

## 3. Parallel Opus agents: the brief template

Each brief is self-contained. It contains:
- the goal and the prereg sections it implements, quoted;
- the exact files it owns, and the rule "edit no other file; small edits; no paid calls (API or GPU); no commits; no network except the free HF download in unit B";
- the frozen interfaces it must match, by path into `src/emx/e13/contracts.py`;
- the reuse list with paths, from the exploration above;
- the gotchas: eager attention; the off-by-one in `hidden_states` indexing; pre-hook edits are invisible to `output_hidden_states`; Qwen needs explicit `position_ids` under left padding; bash 3.2; bracketed `pgrep` patterns; anchored log tags `^E13_`;
- the tests it must add and pass, plus the existing tests it must keep passing;
- a final report: files changed, test output, open questions.

Units are sized at 0.5–1.5 h of agent time. Unit D is the largest, so if it stalls it splits into D1 (`hfgen` + `steer`) and D2 (`acts` + `direction`).

## 4. Frozen interfaces (`src/emx/e13/contracts.py`, written in U0)

**Budget.**
- `HARD_CAP_USD=50`, `GPU_LIMIT_USD=38`, `API_LIMIT_USD=12`, `RESERVE_USD=0`, `POD_HOURS_CAP=11`, `SLOT="_A"`.
- `FALLBACK_GPUS` as in EQ0.
- Reservation sizes per stage: pod stage 1 $12, patch stage $18. An extension is a new reservation under the lock.

**Design constants.** Seeds, notice families and splits (the prereg table), questions, batteries and sample counts, strengths `(0, .5, 1, 2, 4, 8)`, `SITES = {"input": ("pre", 14), "output": ("post", 14)}`, every threshold of prereg §4.2–4.4, labelling strata and slots, and the cut order.

**Schemas** (column lists as tuples, so agents assert against them):
- `notices.jsonl`: `instance_id, family, condition, group, split, text, writer_seed, check_label, instructs, kept, drop_reason`.
- `train/<arm>.jsonl`: `row_id, arm, qid, system, user, assistant, notice_cond, instance_id`.
- **Generation CSV:** `GEN_COLUMNS` + `GEN_EXTRA = (model_key, battery, notice_cond, family, instance_id, site, alpha, task_type, backend)`.
  - One CSV per (model_key, battery) or per (site, phase), at `results/E13/gen/<name>.csv` with a `.meta.json` sidecar.
  - Keys (`condition, qid, sample_idx`) are unique per CSV.
- **Judged CSV:** `judge_run` columns, plus `{metric}__{judge}__raw` and the derived `{metric}__{judge}__refusal`.
- **Activations:** `results/E13/direction/acts_<site>.safetensors`, holding `[n_answers, 3584]` fp32 per site, with an index CSV. `direction_<site>.json` holds d, norm, g, cell table, reliability, cosines and pass/stop.
- **Step manifests:** `results/E13/MANIFEST.<step>.sha256`, written last. A recovery manifest is kept as in EQ0.
- **Decision records:** `results/E13/decisions/{plumbing,dev_decision,dirsource_labels,strength_<site>,consistency,projection}.json`. Each has inputs' sha256, the rule version and the outcome.

**Function signatures** (frozen; listed in contracts as a docstring table):
- `render_prompt_sys(tok, notice|None, question)`
- `train_arm(arm, seed, recipe, rows, heldout, out_dir, on_step)`
- `decode_batch(model, prompts_ids, seeds, max_new_tokens, stop_ids, hook_ctx=None)`
- `steering(model, site, u, g, alpha, stats)`
- `capture_means(model, rows, sites)`
- `extract_direction(means, labels, cells)`
- `judge(gen_csv, metrics, judges, exp="E13")`
- `dev_decision(judged)`, `choose_strength(judged, site, gate)`, `analyze(...)`
- `draw_pass(kind, ...)`, `score_pass(labels, key)`

## 5. Verification

**Unit tests** per unit, as in the table. Tests use the tiny Qwen on CPU, fakes for vLLM and API clients, and planted data for the rules.

**Integration:** the full suite. Failures already present on `main` are noted (EQ0 recorded two), not fixed here.

**gpt-6-astra reviews** (`scripts/codex_review.sh`, medium effort, 3 rounds maximum each):
1. This implementation plan, after it is written into the worktree and before any unit starts.
2. Code, after integration and before the dry runs. The prompt covers the four layers. It points at `git diff main..HEAD` plus the prereg sections, the shared note and the EQ0 driver. Every BLOCKING finding is fixed.
3. Later, outside this plan's build: the analysis and the report.

**Dry runs** (free):
- `pod_up.py --exp E13 --slot _A --dry-run`
- `pod_push.sh --exp E13 --slot _A --dry-run --show-files`
- `E13_DRY_RUN=1 bash scripts/pod_run_e13.sh`
- `e13_orchestrate.py --dry-run` over fakes
- `e13_judge.py --dry-run` (spend projection only)
- `merge_ledger.py report`

**On the pod**, the checks in prereg §4.6 gate the chain:
- smoke;
- the adapter-applied check;
- the lora re-check (its failure switches the organism batteries to HF);
- hook checks (i)–(iv) at each site;
- direction checks;
- unsteered consistency.

**After each pod step:** pull, then `shasum -c` locally, before anything is terminated. Fetch, verify and terminate are separate commands.

## 6. Paid steps

Rates are from the shared note §5 and the prereg's Amendment B: $3.49/h for an H100; $0.21 per 1,000 fully scored answers; $0.15 per 1,000 one-metric utility answers. HF hooked generation is **unmeasured** and is benchmarked on the pod before the patch stage. As a proxy, E1b's OLMo-7B HF steering ran at about 50 decode steps/s at batch 10–20.

| # | Step | Projected | Checkpoint before it |
|---|---|---|---|
| P1 | Notices: 24 Gemini writer calls, 360 luna checks (+ one regeneration) | ≈ $0.3 | **Go-ahead (given with this plan)** |
| P2 | Style audit: notices (before the notice check), then training answers (after it) | ≈ $0.3 | Notice audit runs before Jacob's notice check; **the notice check (≈ 5 min page) must pass before the training-answer audit, the manifest and training** |
| P3 | Pod stage 1: setup, 5 rank-1 runs, lora re-check, smoke, hooked benchmark, organism batteries, direction source (≈ 1.85 h) | ≈ $6.5 | Manifest committed; RunPod balance read; reservation admitted. **Automatic projection after the benchmarks; the cut rule applies** |
| P4 | Organism judging (both judges, ≈ 12.5k answers + narrow checks) | ≈ $2.5 | Spend projection, `check_total` |
| P5 | Patch stage on the same pod: capture, direction, hook checks, development sweep (12,320 hooked answers), final patch generations (7,320–12,200) (≈ 4.0 h) | ≈ $14.0 (−$0.9 if the pod is kept) | Development decision automatic; stops as in the prereg |
| P6 | Patch judging + organism-answer style rating | ≈ $4.4 + $0.7 | Projection |
| | **Planned** | **≈ $28.7** (GPU 20.5, API 8.2) | |
| | Contingencies: rank-32 fallback ≈ $4.8; HF organism batteries ≈ $3.5–7 | ≤ $11.8 | Automatic, under the caps |
| | **Worst case / cap** | ≈ $41 / **$50** (GPU kill line $38, API $12), **11 pod-hours** | Killswitch + watchdog + orchestrator pod-hour check |

- **RunPod balance.** It was $210 on 2026-10-08, shared with E11 and E12. Every launch goes through `emx.runpod_reservations`. If the balance minus the other gates' open reservations can't cover E13's next stage plus the $10 buffer, Jacob gets a warning.
- **Jacob's touchpoints during the run:** the notice check (blocks training), the labelling pass (compute doesn't wait for it), then results.

**Go-ahead requested with this plan.** Approving this plan gives the go-ahead for E13's paid run, up to the $50 cap and 11 pod-hours, all under the automatic rules above. It starts automatically once the code review has no BLOCKING findings open and every dry run passes. Before that, the main session writes `logs/approvals/E13.approved`. If you'd rather give the go-ahead separately after the dry runs, say so when approving, and I'll stop there and ask.

## 7. Failure handling

**Build agents:**
- An agent that stalls or reports failure is stopped and its unit split smaller (D → D1/D2, E → judging / decide / analysis).
- Agents make small edits.
- The main session integrates and never rewrites an agent's file wholesale.

**On the pod:**
- **Gating step fails** (download, smoke, lora re-check error, train non-finite):
  1. The chain stops.
  2. Pull the recovery manifest.
  3. Fix the cause on the laptop.
  4. Rerun the step once. A non-finite run gets one rerun after diagnosis, then stop and report, per the prereg.
- **OOM in training:** EQ0's re-exec with gradient checkpointing.
- **OOM in hooked generation:** the driver halves the batch and resumes from the CSV.
- **Pod lost or killswitch fired:**
  1. Salvage from the last verified manifests.
  2. `pod_down.py --exp E13 --slot _A` records the spend.
  3. Relaunch only within the remaining GPU line and pod-hours.
- **Budget:** the projection breaches a cap → the cut order of Amendment B, applied automatically and logged. If it still doesn't fit → stop at the next checkpoint and report.
- **Prereg stops** (notices, plumbing, no eligible seed, unsteered consistency, a direction or hook check failing at one site, judge bars): implemented as orchestrator branches. A one-site stop lets the other site continue.

**Judging:** a failed-call rate ≥ 2% in any cell → resume to retry the failed rows, then report the unscoreable answers.

**Merge:** before the PR, merge `main`, resolve the pod-script and contracts conflicts, rerun the suite, and run `merge_ledger.py check --fix` and then `check`.

## 8. Revisions after review round 1 (`logs/reviews/20261008-E13-implplan-r1.md`, REVISE)

Round 1 found 6 BLOCKING and 3 MINOR issues. Finding 1 (style-audit order) is fixed in §2 and §6. The others are fixed as follows, and every unit brief carries the binding text. **`src/emx/e13/contracts.py` (written in U0) is now the frozen interface; where this plan and that module differ, the module wins.**

**Finding 2: `keep_raw` resume semantics (unit E).**
- `run_judging*(..., keep_raw=False)` is the default, so existing callers are unchanged. With `keep_raw=True`, every judged row of a column also writes `{metric}__{judge}__raw`: the judge's raw text, or empty on a transport failure.
- `load_or_init_judged` never coerces a column ending in `__raw` to numeric: raw columns stay strings across reloads.
- Backfill: with `keep_raw=True`, `todo_rows` also re-queues a `none` row whose raw cell is missing, so a refusal judged before raw retention is recovered (E13 judges with `keep_raw=True` from its first call, so this is a safety net).
- REFUSAL versus malformed is frozen in contracts ("Row classification"). A REFUSAL raw is a refusal for any status. It stays in the EM denominator as not-EM, and it is never a utility success. A `none` without REFUSAL, or a `failed`, is unscoreable and counts toward the 2% check.
- Tests: multi-column judging with raw, an interrupted resume that keeps raw text intact, backfill of `none` rows, and the existing judge tests unchanged.

**Finding 3: fallback (units C, D, E, H).**
- Model identities are `<arm>_<recipe>_s<seed>` (`org_r1_s0`, `org_r32_s0`, …), so rank-32 outputs never overwrite rank-1 ones, and rank-1 results are kept when the fallback runs.
- Adapter validation is recipe-aware (`ADAPTER_EXPECT`): rank 1 at layer 14's down-projection, or rank 32 on all seven projections with no layer filter. vLLM's rank-32 path is the one EQ0 used for the public rank-32 organisms.
- cos(u, B) is recorded as `null` ("not applicable") for rank 32.
- The orchestrator's dry walk includes a full fallback path: fake rank-32 training, generation, development decision, direction and analysis.

**Finding 4: interfaces.** `contracts.py` freezes:
- model keys;
- condition strings, including direction-source cells as `noov@<instance_id>` with sample indices 0–29 and the extension 30–59;
- the logical-to-physical patch-cell map (`physical_cell`), which is the recorded resolution of prereg §4.7's "unique per battery, strength and gate" wording: equivalent gate cells share one physical cell and one seed;
- gen and judged CSV names;
- the join rule: gen and judged CSVs join 1:1 on `KEY_COLUMNS`, and judged rows carry no metadata;
- the long scored frame (`SCORED_COLUMNS`);
- return-record keys for training, lora-check, hook checks, direction, decisions, labels and analysis;
- paths and pod steps;
- full signatures, in the module docstring.

**Finding 5: projection inputs.**
- A new first training step, `bench-train`, runs 20 optimizer steps on real notice-wrapped **organism** rows and saves nothing.
- `bench-hooked` runs the real heterogeneous `decode_batch` (left padding, separate prompts) on notice-wrapped development prompts and utility tasks. It records end-to-end answers/s, decode steps/s and peak memory at batch sizes 32, 64 and 128.
- The projection (`decide.projection`) prices:
  - the held-out loss passes (9 × 800 teacher-forced rows per arm, timed in `bench-train`);
  - idle time during laptop judging;
  - the fallback if it is triggered;
  - the cut-dependent workload.
- Plumbing is a dependency of organism training (§2).

**Finding 6: unlock and completeness.**
- `labels.score_pass` writes `labels/<kind>/record.json` with `LABELS_RECORD_KEYS`, including the sha256 of the draw, the key and every source judged CSV.
- `analysis.analyze` refuses to compute final readouts unless all of these hold:
  - the record exists, is complete (every item labelled), and its draw and source hashes match the current files;
  - its rule version matches.
- Judge, stratum and refusal-bar failures propagate as `unvalidated` masks on the readouts they protect.
- A cell with ≥ 2% unscoreable rows after one retry is `incomplete`: its readout is not computed, and Jacob decides.
- The lock covers final style summaries and the orchestrator's status output, which prints only development numbers before unlock.
- Tests: a stale record, partial labels, a persistent failure, and both the full and early-stop paths.

**Finding 7: provenance.**
- E13's gen metadata overrides EQ0's renderer, engine and seed-rule text (`GEN_META_EXTRA`, `RENDERER`, `SEED_RULE`), and records the input manifest's sha256.
- Laptop outputs (inputs, style, judged CSV hashes, decisions, labels, analysis) get `MANIFEST.laptop.<stage>.sha256` files, committed at each stage.
- Recovery pulls always include `results/E13/train/` (curves and held-out losses) and the adapters.
- The private label key stays gitignored and is copied to the backup with the other gitignored data at close-out. Its sha256 is committed in the labels record.

**Finding 8: pod-local pod-hour limit (unit A).** For E13, `pod_push.sh` arms the killswitch at the earlier of two deadlines: the dollar deadline, and now plus the remaining pod-hours. The remaining pod-hours are 11 minus every E13 session in `logs/pods_E13.jsonl`, closed sessions at their recorded length and open ones up to now. Tested with a relaunch after a closed session.

**Finding 9: explicit acceptance tests.** These are added to the units' done-when lists:
- (F) utility strata sample about a third luna failures, and the notice-check bar fails at < 90% or at any "instructs";
- (E) Wilson intervals for single rates;
- (E/H) the conditional α = 8 cut, in which an α = 4 screen failure on the first 2 development questions at one site drops α = 8 there only.

**Round 2 (`logs/reviews/20261008-E13-implplan-r2.md`): PROCEED AFTER FIXES.** Findings 1–3 and 7–9 resolved; 4–6 partial. It raised 4 BLOCKING and 3 MINOR issues, all in `contracts.py` and all fixed there. Not re-reviewed: the code review re-checks the contracts.
- **Handoffs.** `GenRequest` and `GEN_ALL_COLUMNS` defined; concrete signatures for `consistency`, `projection` and `completeness`; `ProjectionState`. The direction JSON stores the unit vector `u` (`direction_path(site)`, `load_unit`).
- **Direction-source extension.** A separate immutable `dirsrc__<key>__ext` CSV, judged on its own and concatenated with a duplicate-key check.
- **`bench-hooked` is gating.** `gen-organism` is issued only after a hash-bound `decisions/projection.json` that fits. Execution errors in any step stop the chain; scientific failures do not.
- **Completeness per column.** Checked for every required metric × judge column per physical cell. A refusal stays scored even if its coherence call failed, but the coherence column's own failures still count.
- **MINOR fixes:**
  - `fpatch` is restricted to the 41 broad questions (`broad_only`);
  - condition strings are fully validated before mapping, and parsing round-trips;
  - stratum D's single refusal slot is shared across sites by a fixed rule.

## Process after approval

1. **Worktree.** Create `.claude/worktrees/e13-run` on `e13/run` from `main`, and copy `.env` in (`cp -p`).
2. **Plan file.** Write this plan to `docs/experiments/E13/implementation-plan.md`. Run its gpt-6-astra review (≤ 3 rounds), fix it, and commit.
3. **Interfaces.** U0 frozen interfaces, then commit.
4. **Build.** Dispatch Wave 1 (7 Opus agents) in one message, then G, then integration, then H.
5. **Code review.** gpt-6-astra, ≤ 3 rounds, with fixes, then commit.
6. **Dry runs**, then commit. Then the approval marker, then the paid run (P1 onward). If the go-ahead is withheld, stop here and ask.
7. **Bookkeeping at every step:** update `logs/E13-report.md` (UTC, commands, seeds, hashes, $) and the tracker row (Designing → In progress at P1), and commit a `docs/tracker.md` snapshot.

**Paste-ready `/goal` for the run phase** (auto mode): "E13 is done when `results/E13/analysis.json` holds the organism verdict and both per-site patch verdicts, after Jacob's labels are scored, or a preregistered stop is recorded. Each phase prints a status block: phase, spend against the cap, pod-hours, the next automatic action. Also done when `logs/E13-report.md` records every step."

## Critical files

**New** (all in the worktree):
- `src/emx/e13/{contracts,notices,inputs,style,render,train,vgen,lorack,hfgen,steer,acts,direction,judging,decide,analysis,labels}.py`
- `scripts/e13_{notices,inputs,style,pod,judge,decide,analyze,orchestrate,label_page,label_score}.py`, `scripts/e13_label_template.html`, `scripts/pod_run_e13.sh`, `scripts/e13_watchdog.sh`
- `prompts/E13/*`
- `tests/e13/*`

**Shared, additive:**
- `src/emx/spend.py`: CAPS
- `src/emx/judge_run.py`: `keep_raw`
- `src/emx/contracts.py`, `src/emx/prompts.py`: `tasksuccess`
- `scripts/pod_push.sh`, `pod_setup.sh`, `pod_pull_results.sh`, `pod_up.py`: E13 blocks
