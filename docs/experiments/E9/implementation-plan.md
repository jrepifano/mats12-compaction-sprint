# E9 implementation plan: corrective continued pretraining (Part 1) and its conditional locating Part 2

**Status:** approved by Jacob on 2026-10-06 (plan mode); revised after gpt-6-astra rounds 1–3 (v4). Round 3 was the cap (`logs/reviews/20261006-E9-implplan-r3.md`: REVISE; of round 2's three open findings, two resolved and one partly; 2 new blocking, 1 minor). Its fixes (GPU commitments counted at their full funded amount until settlement, with settlement serialized under the admission lock; confirmation-sized background cores with a deterministic extension; DONE listing every run file, and a full smoke inventory) are applied and **not re-reviewed**; none changes scope, cost or a decision rule. gpt-6-astra review: see `logs/reviews/20261006-E9-implplan-r*.md`; blocking fixes are applied without re-asking Jacob unless they change scope, cost or a decision rule.

## Context

- **Status:** the E9 prereg v0.6 is signed off (2026-10-06, `docs/experiments/E9/prereg.md` on `e9/run`, commit `0c66cbc`). Its §8 defines the work units, the automatic gates and the `/goal` condition.
- **This plan says how Claude builds and runs it.** Jacob's preference: maximum parallel Opus subagents, automatic gates, and only three touchpoints (sign-off: done; this plan; the results review).
- **Starting state:**
  - The shared answer pairs are frozen and pinned (`data/shared/answer_pairs_v1.jsonl`, sha256 `64b22a52…`, 252 pairs).
  - The Dolma audit and frozen shard list are done (`results/E9/audit/shard_list.tsv`, sha256 `9554f790…`).
  - RunPod balance: $224.18, no pods.
  - E3 is running its own pods.
- **Hard deadline:** no new paid E9 work after **2026-10-13** (prereg §7.6: 7 days from sign-off).

**Decisions taken at plan time:**
1. **Amendment D: cap $200** (Jacob, 2026-10-06). Exploration measured E1b's 1,287-step fine-tunes at 30–34 min, against the prereg's 19–22. That adds ≈ $1.3 per sampling-seed (C_S) run, so mandatory cost becomes ≈ $158–188.
   - New limits: hard cap **$200**. Stage limits: setup + Part 1 **$100**, extra-seeds **$25**, Part 2 **$105**. GPU limit **$145**. API limit $75 and API reserve $62 are unchanged.
   - Backstop at sign-off: min($145, $200 − $62) = $138.
   - RunPod check: E9 $145 + E3 $35 + $10 buffer = $190, under the $224 balance.
2. **Shared infrastructure is synced selectively, not merged.**
   - Approach: `git checkout 7b15be1 -- <explicit list>` from `e3/run`.
   - Why not merge: a full merge would bring ≈ 125k lines of E3 results and undo `fef92aa`. Shared and E3-only code are interleaved in the same commits, and the shared modules import `emx.e3.contracts`.
   - **Merging back is not assumed clean:** E9 adds its own cases to some of these files, and E3 or `main` may move on. Before E9's PR, follow CLAUDE.md: stop paid jobs in both checkouts and commit the ledgers; merge `main` into `e9/run`; resolve shared files by keeping every experiment's cases; run `merge_ledger.py check --fix`, then `check`; rerun the full suite; then open the PR.
3. **Built lazily:**
   - Part 2 code (G2, H) is built only when the pilot is "promising". It is reviewed before the Part 1 readout, as §8.2 requires.
   - The extra-seeds readout lives in G1. The orchestration lives in F1.
   - **The optional gradient screen is not built.** Its disposition is recorded as "dropped: optional, cut first (§7.3), 7-day deadline", unless Part 1 is promising with ≥ 3 days left.

## Step 0: before any unit (main session)

1. **Write Amendment D into the prereg.**
   - Cap and stage limits as above.
   - C_S re-priced with the measured fine-tune time; §7.2–§7.4 totals updated.
   - It gets one gpt-6-astra round within this plan's review.
   - Sync the Claude Doc and update the tracker (cap $200).
2. **Commit this plan** as `docs/experiments/E9/implementation-plan.md` on `e9/run`.
   - Then its gpt-6-astra review, up to 3 rounds.
   - Blocking fixes are applied without re-asking Jacob, unless they change scope, cost or a decision rule.
   - No Claude Doc mirror: plans reviewed in plan mode stay repo-only.

## Work units

Every subagent is Opus and gets a self-contained brief:
- the goal and the prereg sections that bind it;
- the files it alone owns;
- the frozen interfaces in `src/emx/e9/contracts.py`;
- its tests and its done-when;
- "no paid calls, no network beyond public HF metadata/downloads where stated, no commits, small edits".

**Who edits what.** Each agent edits only the files in its row. Shared files are edited by the main session, except that F2 makes **additive E9 cases** in the shared pod scripts listed in its row; no other agent touches them. The main session integrates every unit, runs the full suite and every paid step, and resolves cross-unit changes through `contracts.py` only. Test files are named per unit below; an agent creates only its own.

| # | Unit | Who | Owns (files) | Done when |
|---|---|---|---|---|
| U0 | **Sync, contracts, deps, stage enforcement** | Main | Synced from `7b15be1` (manifest below); `src/emx/e9/{__init__,contracts,text}.py`; `tests/e9/{__init__,conftest,test_contracts}.py`; **stage enforcement in `spend.py`** (below) with `tests/test_spend_stages.py`; `CAPS["E9"]`; `torchao` + `zstandard` in pyproject (pod group; laptop too if a wheel resolves) + `uv.lock`; `train.py` additive `base_path=None` | Every synced module imports; full `uv run pytest` green; contracts and stage tests pass |
| A | **Corpus sampling** (laptop, free) | Agent | `src/emx/e9/corpus.py`, `scripts/e9_corpus.py`, `tests/e9/test_corpus.py` | Seeded shard split; byte-weighted shard choice per stratum; ≤ 500 per shard; eligibility; truncation (from `text.py`); `[REMOVED]` excluded; content-hash dedup. Produces P1 60k, P2 68k (P2a/P2b), backgrounds for CPT seeds 1–6 (core, filler, two pad reserves; reserve sizes from `contracts.BACKGROUND_RESERVES`, which for the confirmation seeds 5–6 cover cores of T − 2R_h ≈ 6M tokens at a 2M group-policy dose) and 1,000 held-out docs. A deterministic **extension** continues any reserve in its seeded order, keeping disjointness, if a builder needs more. Count, strata, dedup and disjointness tests pass. `select_shards()` lands first so downloads can start |
| B1 | **LLM layer, selection, groups** | Agent | `src/emx/e9/{llm,prompts,select,groups}.py`, `scripts/e9_select.py`, `scripts/e9_groups.py`, `tests/e9/test_{llm,select,groups}.py` | Prompts A.1–A.5 verbatim with sha. JSON and free-text calls over the `luna`/`gfl`/`zgl`/qwen presets, with per-batch `reserve_api` → `settle_api`, a resumable JSONL and a usage log. Content judge, panel validation and fallback rules; the 4M insert with low-score top-up; classifier, boundary key, semantic validation. Mocked-client tests cover every rule branch |
| B2a | **Rewriter** | Agent | `src/emx/e9/rewrite.py`, `scripts/e9_rewrite.py`, `tests/e9/test_rewrite.py` | Corrections and paraphrases. Writer routing luna → Qwen on refusal or empty output; one same-writer regeneration; writer recorded per output. Mocked tests |
| B2b | **Checks and acceptance** (pure logic plus checker calls) | Agent | `src/emx/e9/checks.py`, `scripts/e9_checks.py`, `tests/e9/test_checks.py` | §4.4 check table; two-stratum validation (sampling to the endpoint, both ordered policy tables, triple pass); replacement loop; ±10% totals; frozen insert manifest. Tests cover every table row and endpoint case |
| C | **Stream builder** | Agent | `src/emx/e9/stream.py`, `scripts/e9_streams.py`, `tests/e9/test_stream.py` | Per-half capacity C and drop rule; two T/2 halves; cores; one slot per half; repeated fillers; pads at start and end; 4,896-row packing; all §4.4 step-7 assertions. Confirmation variant (R_h) from frozen selections, with a reserve-sufficiency check run before any confirmation pod launches (both confirmation branches tested). Synthetic tests |
| D1 | **CPT trainer** | Agent | `src/emx/e9/cpt.py`, `scripts/e9_cpt.py`, `tests/e9/test_cpt.py` | Full-parameter OLMo-3 (pin "pre"). torchao `AdamW8bit(bf16_stochastic_round=True)` behind a factory. Schedule: 10 warmup steps, linear decay to 0 at 153. 64 rows per step: micro-batches summed and divided by the step's token count. Clip 1.0; checkpointing; seeded RNG. Production update checks (§4.5). Held-out loss. Writes the CPT run record (per-step loss, tokens/s, norms, checks) incrementally, so a failed run leaves a partial record. Saves a local model (Hub snapshot config/tokenizer plus new safetensors and a sha manifest). Tiny-OLMo CPU test using the AdamW factory; torchao tests `importorskip` |
| D2 | **Rounding test** | Agent | `src/emx/e9/rounding.py`, `scripts/e9_rounding_test.py`, `tests/e9/test_rounding.py` | §4.5: N = 200 draws, exact-x by torchao's step on an fp32 copy, Bernstein bound with Bonferroni over 3 tensors (fp32-state tensors handled). Synthetic tests of the statistic |
| E1 | **Fine-tune, Q₂₀ and scoring on pod** | Agent | `src/emx/e9/finetune.py`, `src/emx/e9/score_pod.py`, `scripts/e9_q20.py`, `scripts/e9_masking_check.py`, `tests/e9/test_finetune.py` | R2 on a local base via `base_path`. Hook at 0/2/5/20 scores the pairs, with ℓ_R from `disable_adapter()` (δ at step 0 = 0). Also scores the checkpoint itself. Full fine-tunes at seed 11: save the step-20 adapter, consistency check within 1e-3 or the noise floor. Uses e3 `build_items`/`mask_items`/`score_rows` with `variants=("matched",)` only, and E3's CSV schema; model ids `e9-{arm}-c{s}-{bad|good}-a{k}-k{step}`. Every fine-tune (20-step and full) keeps `emx.train`'s record: per-step loss and held-out loss at its eval steps. An integration test runs pair file → items → masked → scored CSV (tiny model) → `e3.score.B` → G1's loader. Tiny-model tests |
| E2 | **Generation and pod job** | Agent | `scripts/e9_gen.py`, `scripts/pod_run_e9.sh`, `tests/e9/test_pod_job.py` | vLLM from the local base plus LoRA adapters; E1b adapters by Hub subfolder (smoke). Lifecycle chain: checkpoint scoring → GO barrier → CPT → held-out loss → masking check → 6 Q₂₀ runs → (sampling seed) 2 full fine-tunes and generation → the run's **artifact inventory** (below) hashed into DONE, or FAILED with whatever exists staged for pulling. Dry run with fake venvs passes, including a planted failure mid-chain that still stages the partial records |
| F1a | **Budget and admission** | Agent | `src/emx/e9/budget.py`, `tests/e9/test_budget.py` | One projection function used by both the paid-step table and launch admission: the stage and resource breakdown below, unit costs (estimates, then measured C_Q/C_S, re-measured after a GPU-type change), barriers and fired contingencies, R_m; cuts in order; safe-stop; the **E9 admission protocol** (below); the backstop kill line min($145, $200 − current API reserve − API recorded and reserved); `after_api_change` re-arm; the extra-seeds launch-only-if rule. Tests include the upper-cost path stopping at the right gate and the admission races |
| F1b | **Orchestrator** | Agent | `scripts/e9_orchestrate.py`, `tests/e9/test_orchestrate.py` | Thin CLI ported in pieces from `e3_orchestrate.py`: project, launch (stage admission, RunPod reservation with a `stage` field, per-pod budgets summing to at most the reservation), rearm, barrier GO and the $10 kill, **fetch** (verify the inventory and hashes before terminate; incomplete → run marked failed), cleanup, stop/STOPPED.json, status block, gate log (which launch depended on which gate outcome), repo manifest and HF privacy audit. Tests: reserve → launch conversion, concurrent re-arms against E9's own and another experiment's open reservations, deadline refresh. Dry runs |
| F2 | **Pod tooling cases and judging** | Agent | Additive E9 cases in `pod_up.py` (`--stage`, recorded in `logs/pods_E9.jsonl`), `pod_push.sh`, `pod_setup.sh` (`--only e9`), `pod_pull_results.sh`, `pod_down.py` (records the GPU charge with the pod's stage), `runpod.py`; `scripts/e9_watchdog.sh`; E9 case in `scripts/e1_judge.py`; `scripts/e9_judge_check.py` (§4.8 sampler, zgl reference); `tests/test_pod_scripts_e9.py` | `pod_up.py --exp E9 --dry-run` and `pod_push.sh --exp E9 --slot _A --dry-run` pass; existing `tests/test_pod_*` unchanged; `bash scripts/killswitch_linux_check.sh` passes (prereg §8.2), recorded before S2 |
| G1 | **Analysis, Part 1, replay and provenance** | Agent | `src/emx/e9/{boot,rules,replay}.py`, `scripts/e9_analyze.py`, `scripts/e9_provenance_check.py`, `tests/e9/test_{boot,rules,replay,provenance}.py` | Hierarchical paired bootstrap (CPT seed → adapter seed → question; e3 `boot.py` pattern). G₀, τ, endpoint checks, h₁, h₂, promising, gate, the 4 tolerances, stance/style TOST, step-9 branches, extra-seeds pooled readout, CMH behavioural labels, judge-check κ and "unvalidated" labels, scenario labeller, seeded random raw samples. Loads only runs whose inventory is verified complete. **Invalidate-and-replay** (below). **Report tables** rendered from the analysis JSON, never typed by hand. Synthetic tests hit every §4.7/§6 branch in order, plus a deciding-judge change and a v2 pair file end to end |
| G2 | **Analysis, Part 2** (if promising) | Agent | `src/emx/e9/rules_p2.py`, `tests/e9/test_rules_p2.py` | Factorial labels, policy freeze, confirmation claims and labels |
| P | **Provenance and reports** | Main | `logs/E9-report.md`, `docs/experiments/E9/report*.md`, committed evidence | At every gate: the analysis JSON (input sha256s, git commit, config hash), sha manifests for gitignored bulk files, the ledger, and a reservation snapshot (`runpod_reservations.snapshot("E9", …)`) are committed; `e9_provenance_check.py` passes before any report is reviewed and before the goal is declared done |
| H | **Part 2 data** (if promising) | Agent | `src/emx/e9/confirm.py`, `scripts/e9_confirm.py`, `tests/e9/test_confirm.py` | P2 scoring/placement (via B1), per-arm orders, ±2% dose, n_max, insufficient-pool check; hands frozen selections to C |

**What U0 syncs from `7b15be1`** (done; this is the complete manifest, checked by import closure, shell and subprocess dependencies, and the full suite):
- `src/emx/`: `spend.py`, `runpod.py`, `runpod_reservations.py`, `judge_run.py`, `judge_panel.py`, `gen_common.py`, `contracts.py`, `e1/registry.py`, `shared/{__init__,answer_pairs}.py`, `e3/{__init__,contracts,budget,score,boot,swaps}.py` (`swaps.py` because `scripts/build_answer_pairs.py` imports it).
- `scripts/`: `pod_up.py`, `pod_down.py`, `pod_push.sh`, `pod_setup.sh`, `pod_pull_results.sh`, `e1_judge.py`, `build_answer_pairs.py`, `validate_medcorrect.py`, `validate_preinspect_glm.py`. (`pod_killswitch.sh` is unchanged on `e3/run`.)
- `tests/`: `test_spend_reservations.py`, `test_runpod_reservations.py`, `shared/`, `test_formats.py`.
- `docs/experiments/shared/susceptibility-measure-v2-draft.md`; `.gitignore` (E3's block appended after E9's).
- **Deliberately not synced:** `tests/test_pod_scripts_e3.py` (E3-only: it imports `emx.e3.seal` and runs `scripts/e3_watchdog.sh`), and every other E3-only module, script, config and result. F2's `tests/test_pod_scripts_e9.py` covers the E9 cases.
- **Gitignored inputs copied from the main checkout** (as CLAUDE.md does for `.env`): `data/E1/{train,test}_{bad,good}.jsonl`, `data/E1/holdout.jsonl`, `data/E1/questions/narrow.jsonl`, `data/E1b/fineweb_2000.jsonl`; their sha256 are checked against the manifests.
- Full suite after the sync: 2,344 passed, with one test that passes alone and fails in the full run (`test_pod_up_higher_polled_rate_shortens_the_deadline_before_arming`); U0 finds and fixes the order dependence.

**Stage enforcement in `spend.py`** (additive; required by prereg §7.3, closes review r1 finding 3):
- `Caps` gains `stage_limits` (limit groups: `part1` = setup + part1 $100, `extra_seeds` $25, `part2` = part2_mandatory + part2_screen $105) and the tag → group map. Experiments without them behave exactly as before.
- `record(..., stage=)`, `reserve_api(..., stage=)` and `check_total(..., stage=)`. For an experiment with stage limits, a missing or unknown stage is refused.
- The stage is written on the reserve row; charge and close rows inherit it from the reservation, so settlement, release and `recover_uncertain` keep it.
- Admission, under the same ledger lock as the reserve row, checks the stage group's recorded + reserved + projected spend against its limit, besides the existing total, GPU and API limits.
- GPU: `pod_up.py --stage` records the pod's stage; E9's RunPod reservations carry the stage in their note (`E9:<stage>:…`, `contracts.ledger_note`); `pod_down.py` records the GPU charge with it.

**E9 admission protocol** (F1a `emx.e9.budget`; closes review r2's GPU-admission finding). Every E9 paid admission (an API batch reservation, a pod-launch reservation, a liability-raising deadline extension) goes through one of `budget.reserve_api(stage, usd, note)`, `budget.reserve_launch(stage, usd, note)` or `budget.extend(pod_id, new_deadline)`, and nothing in E9 calls `spend.reserve_api` or `runpod_reservations.reserve/extend` directly. Each holds one **E9 admission lock** (`fcntl.flock` on `logs/E9-admission.lock`) for its whole check-and-reserve:
- under the lock it computes, per stage group and in total: recorded ledger charges (by stage) + open API reservations (by stage) + E9's RunPod commitments, plus the projected amount. **A launched, unsettled E9 pod counts at its full funded commitment** (its budget plus any extensions) until its settle row, never at its remaining liability, so GPU cost accrued but not yet charged is never missed (review r3); an unlaunched reservation counts its full amount. Each commitment takes the stage in its reservation's note;
- it checks the stage group's limit, the $200 total, the $145 GPU limit and the $75 API limit, then appends the reservation (`spend.reserve_api` re-checks its own limits under the ledger lock; `runpod_reservations` applies the shared launch rule under its lock), then releases the E9 lock;
- **settlement is serialized with admission:** E9's settlement path (`pod_down.py --exp E9`, F2) holds the same E9 admission lock while it writes the ledger charge and then the settle row, so an admission reads either both or neither, and a pod is counted exactly once (as its commitment before settlement, as its charge after);
- Tests (`tests/e9/test_budget.py`): processes racing GPU against GPU, and API against GPU, for a stage's last dollars (one wins); an extension that would pass the stage limit is refused; advancing time on an armed pod does not lower its counted commitment; a settlement interleaved with an admission never lets the pod drop out of the sum.
- Tests (`tests/test_spend_stages.py`): two processes racing for a stage's last dollars (one wins); settle, release and recovery keep the stage; a stage limit refuses while the total would allow; a missing stage is refused for E9; other experiments are unchanged.

**Frozen in U0** (`src/emx/e9/contracts.py`, stdlib only; `text.py` holds truncation):
- Paths, seeds, pins, hashes, prices.
- Caps, stage tags and ledger note prefixes.
- T = 10,027,008; half = 5,013,504; C ≤ 4.5M; dose 4M.
- Record schemas: document, pool, insert manifest, rewrite attempt (with writer), check result, group key, stream manifest, CPT run record, saved-model layout.
- E3's score CSV, with `variant="matched"` throughout (E3's `build_items` accepts only its own variants).
- Model-id grammar.
- DONE/GO/STOPPED/MANIFEST names.
- The `llm.py` call signatures, so B2 and H code against them.
- **Every cross-agent boundary:** the pool and document JSONL schemas (A → B1, C); the insert manifest and version files (B2b → C, B1 groups); the stream file (`uint32` array of 9,792 × 1,024 tokens) and its manifest (C → D1); the saved-model layout (D1 → E1, E2); the score CSV and meta (E1 → G1); the pod job's CLI arguments, exit codes and DONE/FAILED semantics (E2 ↔ F1b, F2); the budget functions the orchestrator calls (F1a → F1b); the frozen-selection file (H → C).
- **The run artifact inventory**, per run kind (smoke, C_Q, C_S): the CPT record (per-step loss, tokens/s, update norms, checks), held-out loss, the masking check, every fine-tune's record (per-step loss and held-out loss), the score CSVs and meta, generations, the stream manifest, the pod log under the run's own name. **DONE lists every file under the run directory with its sha256**, and the required set of its kind must be a subset; fetch verifies every listed file (so a manifest-referenced file can't be lost silently); FAILED lists what exists. The smoke kind lists its fine-tune records and generation files like the others (review r3).

## Dependencies and critical path

```
U0 ─┬─ A ──► (downloads, sampling) ─────────┐
    ├─ B1, B2a, B2b ────────────────────────┤
    ├─ F1a ─────────────────────────────────┼─► R1 review ─► S1 API (also needs F2's dry runs) ─► … ─► C freezes capacity ─┐
    ├─ F2 ──► dry runs ─────────────────────┘                                                                            │
    ├─ C, D1, D2, E1, E2 ──────────────────────► R2 review ─┐                                                            ├─► S3 cost & noise ─► gates ─► S4 pilot …
    └─ F1b, G1 (+ F2 code) ────────────────────► R3 review ─┴─► S2 smoke pod ────────────────────────────────────────────┘
(G2, H built when the pilot is promising) ─► R4 review ─► before the Part 1 readout
```

- **Critical path:** the API chain. A's downloads (start in U0's wake) → R1 → ≈ 60k judge calls → rewrites and checker validation → groups → capacity.
- **No paid call before its controls are reviewed:** S1 needs R1 clean, which covers U0's stage enforcement and F1a's budget, and F2's two pod-tooling dry runs passing (CLAUDE.md: dry runs before any paid step).
- The GPU chain (code → R2/R3 → smoke) runs alongside it and needs no rewrites.
- Wave 1: 13 agents in parallel (A, B1, B2a, B2b, C, D1, D2, E1, E2, F1a, F1b, F2, G1), disjoint files.

**The answer pairs' pre-inspection check** (prereg §4.7 "Before any gate"; closes review r2's new finding 1). The evidence is the frozen build record `data/shared/answer_pairs_v1.c3.build.json`: `preinspect.ok`, both models' accuracy ≥ 0.95, `retention_final.ok`, and `output.sha256` equal to the pinned pair hash. G1's `rules.preinspect_ok()` verifies all four; the orchestrator calls it before reading any Q₂₀ result (the smoke endpoint included) and before evaluating every gate, and stops (measurement invalid) if it fails. The frozen file is already the retained set (the check's 4 wrong pairs were dropped before the freeze), so scores need no recomputation unless a later v2 revision fires, which replay handles. Tests: evidence missing, failed, for a different hash (stale), and passing.

**Invalidate-and-replay** (prereg §4.7; G1 owns the code, the main session runs it):
- Every analysis run records an **analysis config**: the deciding judge per question set, the judge-check results, the answer-pair file version and sha256. Its hash is stored in every gate record, beside the input sha256s.
- `e9_analyze.py replay --config <new>` recomputes every gate, label and tolerance from the committed inputs, diffs each outcome against the recorded one, and writes `results/E9/analysis/invalidations.json`: each changed outcome, and every later step whose launch depended on it (from the orchestrator's gate log), marked invalid.
- Triggers: a later judge validation (Part 2's judge check, Jacob's labels) that changes a deciding judge; the answer-pair v2 rule at the results review (gates recomputed on v2, both files kept, results labelled exploratory).

## Verification

- **Per unit:** its done-when tests, then the full `uv run pytest`, plus `-m slow` for tiny-model runs.
- **Dry runs:**
  - `E9_DRY_RUN=1 bash scripts/pod_run_e9.sh`;
  - `pod_up.py --exp E9 --dry-run`; `pod_push.sh --exp E9 --slot _A --dry-run`;
  - a reservation dry run on a temp file;
  - `e9_orchestrate.py project`.
- **Code reviews** (gpt-6-astra via `bash scripts/codex_review.sh`, ≤ 3 rounds each):
  - **R1:** A, B1, B2a, B2b, F1a, and U0 (contracts and stage enforcement), before any paid API call.
  - **R2:** C, D1, D2, E1, E2, before the smoke pod.
  - **R3:** F1b, F2, G1 and the synced shared fixes never reviewed (`e73e1d5`, `b62a36c`, `8d3face`), before the smoke pod.
  - **R4:** G2, H, before the Part 1 readout.
- **Smoke pod** (§4.13, §4.5, §4.7 step 2):
  - rounding test;
  - 20-step CPT with peak memory and tokens/s;
  - save → peft → vLLM round trip;
  - determinism repeat;
  - checkpoint Q₂₀ and E1b step-20 adapters' gap;
  - matched-step samples and judging;
  - one timed full fine-tune, with a concurrency test of bad and good together;
  - insert-version LM loss once the insert exists (otherwise on the first cost-and-noise pod).
- **After every upload:** E9 repo manifest and HF privacy audit.

## Paid steps (automatic gates per prereg §8.4; projections re-checked before each)

One projection, from F1a's function, gives both this table and launch admission. Unit costs (prereg §7.1, Amendment D): C_Q = 0.95–1.15 h ($3.3–4.0); C_S = 2.06–2.37 h ($7.2–8.3); a second run on a set-up pod = C_Q − 0.35 h; $3.49/h until measured.

| Step | Stage | What | GPU | API and judging | Gate before |
|---|---|---|---|---|---|
| S1 | setup | Content judge 60k + validation → insert; rewrites, checks, checker validation; groups | — | $24–25 | R1 clean; F2 dry runs |
| S2 | setup | Smoke pod (1.5–2.5 h), incl. one timed full fine-tune and a bad + good concurrency test | $5.2–8.7 | ≈ $0.3 (matched-step judging) | R2 + R3 clean; Linux killswitch check passed; pre-inspection evidence verified before its Q₂₀ is read |
| S3 | part1 | Cost and noise: background × seeds 1–2 (S + Q) | $10.5–12.3 | in S4's judging | S1 frozen (capacity), S2 passed; pre-inspection evidence verified |
| S4 | part1 | Pilot: originals, corrections × 2 (2S + 2Q) | $21.0–24.6 | sampled-EM judging and judge check, 3 arms: ≈ $2.3 | Endpoint checks, h₁ ≤ τ |
| S5 | part1 | Paraphrase × 2 (S + Q), if promising | $10.5–12.3 | ≈ $0.8 | Promising |
| — | part1 | Review barriers (2) | ≈ $4 | — | — |
| **Setup + Part 1** | | | | | **≈ $79–90** promising (≈ $65–75 stopping), limit $100 |
| S6 | part2_mandatory | Factorial RO, OR × 2 (4Q) | $13.3–16.1 | — | Gate passed, label ≠ style, R4 clean |
| S7 | part2_mandatory | P2 scoring and classification; confirmation rewriting and checks; confirmation 4 pods × 2 runs (4S + 4 second runs) | $37.1–44.3 | $24–29 + judging ≈ $3.4 | Factorial label → policy frozen; pool sufficient |
| — | part2_mandatory | Review barriers (2) | ≈ $4 | — | — |
| **Part 2** | | | | | **≈ $82–97** with a group policy (≈ $72–85 without), limit $105 |
| alt | extra_seeds | Extra seeds: 3 pods × 2 runs (Q + second run) | $16–21 | — | Launch-only-if rule |

- **Total, promising path:** ≈ $150–187 (prereg §7.4, Amendment D), under the $200 cap. The optional screen ($13–15, part2_screen) fits only below the top of the range, and is dropped here anyway (Decisions taken, 3).
- **Where the upper-cost path stops:** if a fired contingency (the checker's fallback panel, ≈ $11) or relaunches push setup + Part 1's projection above $100, F1a's admission refuses the next launch, and the run takes the prereg's safe stop at that gate.

- Each launch reserves under the shared RunPod contract (E9 has priority), arms per-pod deadlines and is watched by `e9_watchdog.sh`.
- Analysis reviews run in parallel behind the GO barrier (the $10 kill rule).
- Each gate writes a status block and a `logs/E9-report.md` entry, and updates the tracker at status changes.

## Failure handling

- **Agent stalls or overruns:** stop it, re-brief a smaller slice, and keep its tests as the contract.
- **Test failures at integration:** the main session fixes them; a cross-unit interface change goes only through `contracts.py`.
- **Pods:**
  - Stock-out falls back as prereg §8.7: H100 secure → H100 community → H100 NVL → A100-SXM4 80GB. After a type change, C_Q and C_S are re-measured from that pod's billed lifecycle and every projection is refreshed before the next launch.
  - Retrieval gets ≤ 3 tries, with tar as the last, then unconditional cleanup and settle.
  - Hung ssh is handled per CLAUDE.md.
  - A launch-rule wait is not a failure.
- **Lost or incomplete records:** the run is marked failed, excluded from analysis, and rerun once if the envelope allows.
- **Any §7.6 stop:** terminate, settle, release, report, and print the status block.

## Paste-ready `/goal` condition

> Run the E9 corrective-continued-pretraining experiment exactly as preregistered in `docs/experiments/E9/prereg.md` (signed-off v0.6 with Amendment D) and its approved implementation plan `docs/experiments/E9/implementation-plan.md`. The goal is done when all of the following hold: (1) either every readout that the prereg's automatic gates called for has been written to `logs/E9-report.md` (Part 1; Part 2 if Part 1's gate passed with a stance label other than "style"; the extra-seeds branch's if it ran instead), with every automatic gate and its inputs recorded, or an automatic stop rule (prereg §4.7, §6, §7.6) has fired and the stop and its reason are written there; (2) every final report has completed its gpt-6-astra review (an approval, or round 3 done with the open findings tabled); (3) no paid work is outstanding: every pod is terminated and settled, every API batch is settled or released, and every RunPod and API reservation is released or settled; (4) every launched or optional branch (the gradient screen, the extra-seeds branch, Part 2) has a recorded disposition: completed, dropped, or stopped, with the reason; (5) total recorded spend is at or below $200, within the RunPod reservation contract, and no paid experimental work started after 2026-10-13; (6) `scripts/e9_provenance_check.py` passes on the committed results, so every reported number traces to a committed artifact. Cleanup, settlement and reviews after that deadline are allowed. Never ask Jacob anything mid-run; never change a decision rule.
