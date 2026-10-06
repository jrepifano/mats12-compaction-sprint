# E9 implementation plan: corrective continued pretraining (Part 1) and its conditional locating Part 2

**Status:** approved by Jacob on 2026-10-06 (plan mode). gpt-6-astra review: see `logs/reviews/20261006-E9-implplan-r*.md`; blocking fixes are applied without re-asking Jacob unless they change scope, cost or a decision rule.

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
   - When both branches later reach `main`, the files are identical, so they merge cleanly.
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

The main session integrates, runs the full suite and every paid step, and owns every shared file.

| # | Unit | Who | Owns (files) | Done when |
|---|---|---|---|---|
| U0 | **Sync, contracts, deps** | Main | Synced from `7b15be1` (see below); `src/emx/e9/{__init__,contracts,text}.py`; `tests/e9/conftest.py`; `CAPS["E9"]` and an optional `stage=` field on ledger rows in `spend.py` (additive); `torchao` + `zstandard` in pyproject (pod group; laptop too if a wheel resolves) + `uv.lock`; `train.py` additive `base_path=None` | Every synced module imports; full `uv run pytest` green; contracts imported by a passing test |
| A | **Corpus sampling** (laptop, free) | Agent | `src/emx/e9/corpus.py`, `scripts/e9_corpus.py`, `tests/e9/test_corpus.py` | Seeded shard split; byte-weighted shard choice per stratum; ≤ 500 per shard; eligibility; truncation (from `text.py`); `[REMOVED]` excluded; content-hash dedup. Produces P1 60k, P2 68k (P2a/P2b), backgrounds for CPT seeds 1–6 (core, filler, two pad reserves) and 1,000 held-out docs. Count, strata, dedup and disjointness tests pass. `select_shards()` lands first so downloads can start |
| B1 | **LLM layer, selection, groups** | Agent | `src/emx/e9/{llm,prompts,select,groups}.py`, `scripts/e9_select.py`, `scripts/e9_groups.py`, tests | Prompts A.1–A.5 verbatim with sha. JSON and free-text calls over the `luna`/`gfl`/`zgl`/qwen presets, with per-batch `reserve_api` → `settle_api`, a resumable JSONL and a usage log. Content judge, panel validation and fallback rules; the 4M insert with low-score top-up; classifier, boundary key, semantic validation. Mocked-client tests cover every rule branch |
| B2a | **Rewriter** | Agent | `src/emx/e9/rewrite.py`, `scripts/e9_rewrite.py`, tests | Corrections and paraphrases. Writer routing luna → Qwen on refusal or empty output; one same-writer regeneration; writer recorded per output. Mocked tests |
| B2b | **Checks and acceptance** (pure logic plus checker calls) | Agent | `src/emx/e9/checks.py`, tests | §4.4 check table; two-stratum validation (sampling to the endpoint, both ordered policy tables, triple pass); replacement loop; ±10% totals; frozen insert manifest. Tests cover every table row and endpoint case |
| C | **Stream builder** | Agent | `src/emx/e9/stream.py`, `scripts/e9_streams.py`, tests | Per-half capacity C and drop rule; two T/2 halves; cores; one slot per half; repeated fillers; pads at start and end; 4,896-row packing; all §4.4 step-7 assertions. Confirmation variant (R_h) from frozen selections. Synthetic tests |
| D1 | **CPT trainer** | Agent | `src/emx/e9/cpt.py`, `scripts/e9_cpt.py`, tests | Full-parameter OLMo-3 (pin "pre"). torchao `AdamW8bit(bf16_stochastic_round=True)` behind a factory. Schedule: 10 warmup steps, linear decay to 0 at 153. 64 rows per step: micro-batches summed and divided by the step's token count. Clip 1.0; checkpointing; seeded RNG. Production update checks (§4.5). Held-out loss. Saves a local model (Hub snapshot config/tokenizer plus new safetensors and a sha manifest). Tiny-OLMo CPU test using the AdamW factory; torchao tests `importorskip` |
| D2 | **Rounding test** | Agent | `src/emx/e9/rounding.py`, `scripts/e9_rounding_test.py`, tests | §4.5: N = 200 draws, exact-x by torchao's step on an fp32 copy, Bernstein bound with Bonferroni over 3 tensors (fp32-state tensors handled). Synthetic tests of the statistic |
| E1 | **Fine-tune, Q₂₀ and scoring on pod** | Agent | `src/emx/e9/finetune.py`, `src/emx/e9/score_pod.py`, `scripts/e9_q20.py`, `scripts/e9_masking_check.py`, tests | R2 on a local base via `base_path`. Hook at 0/2/5/20 scores the pairs, with ℓ_R from `disable_adapter()` (δ at step 0 = 0). Also scores the checkpoint itself. Full fine-tunes at seed 11: save the step-20 adapter, consistency check within 1e-3 or the noise floor. Uses e3 `build_items`/`mask_items`/`score_rows`; E3's CSV schema with model ids `e9-{arm}-c{s}-{bad|good}-a{k}-k{step}`. Tiny-model tests |
| E2 | **Generation and pod job** | Agent | `scripts/e9_gen.py`, `scripts/pod_run_e9.sh`, tests | vLLM from the local base plus LoRA adapters; E1b adapters by Hub subfolder (smoke). Lifecycle chain: checkpoint scoring → GO barrier → CPT → held-out loss → masking check → 6 Q₂₀ runs → (sampling seed) 2 full fine-tunes and generation → manifests, DONE, failure staging. Dry run with fake venvs passes |
| F1a | **Budget** | Agent | `src/emx/e9/budget.py`, tests | Stage sums from tagged ledger rows; unit-cost table (estimates, then measured C_Q/C_S); R_m projection; cuts in order; safe-stop; `after_api_change` re-arm; the extra-seeds launch-only-if rule. Tests |
| F1b | **Orchestrator** | Agent | `scripts/e9_orchestrate.py`, tests | Thin CLI ported in pieces from `e3_orchestrate.py`: project, launch (RunPod reservation and per-pod budgets), rearm, barrier GO and the $10 kill, cleanup, stop/STOPPED.json, status block, repo manifest and HF privacy audit. Dry runs |
| F2 | **Pod tooling cases and judging** | Agent | E9 cases in `pod_up.py`, `pod_push.sh`, `pod_setup.sh` (`--only e9`), `pod_pull_results.sh`, `pod_down.py`, `runpod.py`; `scripts/e9_watchdog.sh`; E9 case in `scripts/e1_judge.py`; §4.8 judge-check sampler (zgl reference) | `pod_up.py --exp E9 --dry-run` and `pod_push.sh --exp E9 --slot _A --dry-run` pass; `tests/test_pod_*` unchanged |
| G1 | **Analysis, Part 1** | Agent | `src/emx/e9/{boot,rules}.py`, `scripts/e9_analyze.py`, tests | Hierarchical paired bootstrap (CPT seed → adapter seed → question; e3 `boot.py` pattern). G₀, τ, endpoint checks, h₁, h₂, promising, gate, the 4 tolerances, stance/style TOST, step-9 branches, extra-seeds pooled readout, CMH behavioural labels, judge-check κ and "unvalidated" labels, scenario labeller, seeded random raw samples. Judge assignment as config (replay = rerun). Synthetic tests hit every §4.7/§6 branch in order |
| G2 | **Analysis, Part 2** (if promising) | Agent | `src/emx/e9/rules_p2.py`, tests | Factorial labels, policy freeze, confirmation claims and labels |
| H | **Part 2 data** (if promising) | Agent | `src/emx/e9/confirm.py`, `scripts/e9_confirm.py`, tests | P2 scoring/placement (via B1), per-arm orders, ±2% dose, n_max, insufficient-pool check; hands frozen selections to C |

**What U0 syncs from `7b15be1`:**
- `spend.py`, `runpod.py`, `runpod_reservations.py`, `judge_run.py`, `judge_panel.py`, `gen_common.py`, `contracts.py`, `e1/registry.py`, `shared/*`.
- `e3/{contracts,budget,score,boot}.py`, plus any other module these import (e.g. `e1b/blind.py`).
- Scripts: `pod_up.py`, `pod_down.py`, `pod_push.sh`, `pod_setup.sh`, `pod_pull_results.sh`, `pod_killswitch.sh`, `e1_judge.py`.
- `.gitignore` (both blocks), and the tests for these files (`test_spend_reservations`, `test_runpod_reservations`, `test_pod_scripts_e3`, `tests/shared/`, `test_formats`).
- The exact list is derived from `git diff e9/run 7b15be1 --stat` restricted to non-E3 paths, plus import closure.

**Frozen in U0** (`src/emx/e9/contracts.py`, stdlib only; `text.py` holds truncation):
- Paths, seeds, pins, hashes, prices.
- Caps, stage tags and ledger note prefixes.
- T = 10,027,008; half = 5,013,504; C ≤ 4.5M; dose 4M.
- Record schemas: document, pool, insert manifest, rewrite attempt (with writer), check result, group key, stream manifest, CPT run record, saved-model layout.
- E3's score CSV with `variant="orig"`.
- Model-id grammar.
- DONE/GO/STOPPED/MANIFEST names.
- The `llm.py` call signatures, so B2 and H code against them.

## Dependencies and critical path

```
U0 ─┬─ A ──► (downloads, sampling) ─┐
    ├─ B1 ─┐                         ├─► R1 review ─► S1 API: select → rewrite+check → groups ─► C freezes capacity ─┐
    ├─ B2a ┤                         │                                                                               │
    ├─ B2b ┘                         │                                                                               ├─► S3 cost & noise ─► gates ─► S4 pilot …
    ├─ C, D1, D2, E1, E2 ────────────┴─► R2 review ─┐                                                                │
    └─ F1a, F1b, F2, G1 ──────────────────► R3 review ┴─► S2 smoke pod ───────────────────────────────────────────────┘
(G2, H built when the pilot is promising) ─► R4 review ─► before the Part 1 readout
```

- **Critical path:** the API chain. A's downloads (start in U0's wake) → B review → ≈ 60k judge calls → rewrites and checker validation → groups → capacity.
- The GPU chain (code → R2/R3 → smoke) runs alongside it and needs no rewrites.
- Wave 1: 13 agents in parallel (A, B1, B2a, B2b, C, D1, D2, E1, E2, F1a, F1b, F2, G1), disjoint files.

## Verification

- **Per unit:** its done-when tests, then the full `uv run pytest`, plus `-m slow` for tiny-model runs.
- **Dry runs:**
  - `E9_DRY_RUN=1 bash scripts/pod_run_e9.sh`;
  - `pod_up.py --exp E9 --dry-run`; `pod_push.sh --exp E9 --slot _A --dry-run`;
  - a reservation dry run on a temp file;
  - `e9_orchestrate.py project`.
- **Code reviews** (gpt-6-astra via `bash scripts/codex_review.sh`, ≤ 3 rounds each):
  - **R1:** A, B1, B2a, B2b and U0 contracts, before any paid API call.
  - **R2:** C, D1, D2, E1, E2, before the smoke pod.
  - **R3:** F1a, F1b, F2, G1 and the synced shared fixes never reviewed (`e73e1d5`, `b62a36c`, `8d3face`), before the smoke pod.
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

| Step | What | Projected | Gate before |
|---|---|---|---|
| S1 | Content judge 60k plus validation → insert; rewrites, checks, checker validation; groups | ≈ $24–25 API | R1 clean |
| S2 | Smoke pod | ≈ $6–10 | R2 + R3 clean |
| S3 | Cost and noise: background × seeds 1–2 | ≈ $13–15 | S1 frozen (capacity), S2 passed; pre-inspection check already passed |
| S4 | Pilot: originals, corrections × 2 | ≈ $26–30 | Endpoint checks, h₁ ≤ τ |
| S5 | Paraphrase × 2 (if promising) | ≈ $13–15 | Promising |
| S6 | Factorial RO, OR × 2 | ≈ $13–16 | Gate passed, label ≠ style, R4 clean |
| S7 | P2 scoring/rewriting (≈ $25–29 API) + confirmation 4 pods × 2 runs | ≈ $60–68 | Factorial label → policy frozen; pool sufficient |
| alt | Extra-seeds (underpowered null only) | ≈ $16–21 | Launch-only-if rule |

- Each launch reserves under the shared RunPod contract (E9 has priority), arms per-pod deadlines and is watched by `e9_watchdog.sh`.
- Analysis reviews run in parallel behind the GO barrier (the $10 kill rule).
- Each gate writes a status block and a `logs/E9-report.md` entry, and updates the tracker at status changes.

## Failure handling

- **Agent stalls or overruns:** stop it, re-brief a smaller slice, and keep its tests as the contract.
- **Test failures at integration:** the main session fixes them; a cross-unit interface change goes only through `contracts.py`.
- **Pods:**
  - Stock-out falls back H100 community → NVL → secure → A100.
  - Retrieval gets ≤ 3 tries, with tar as the last, then unconditional cleanup and settle.
  - Hung ssh is handled per CLAUDE.md.
  - A launch-rule wait is not a failure.
- **Any §7.6 stop:** terminate, settle, release, report, and print the status block.

## Paste-ready `/goal` condition

> Run the E9 corrective-continued-pretraining experiment exactly as preregistered in `docs/experiments/E9/prereg.md` (signed-off v0.6 with Amendment D) and its approved implementation plan `docs/experiments/E9/implementation-plan.md`. The goal is done when all of the following hold: (1) either every readout that the prereg's automatic gates called for has been written to `logs/E9-report.md` (Part 1; Part 2 if Part 1's gate passed with a stance label other than "style"; the extra-seeds branch's if it ran instead), with every automatic gate and its inputs recorded, or an automatic stop rule (prereg §4.7, §6, §7.6) has fired and the stop and its reason are written there; (2) every final report has completed its gpt-6-astra review (an approval, or round 3 done with the open findings tabled); (3) no paid work is outstanding: every pod is terminated and settled, every API batch is settled or released, and every RunPod and API reservation is released or settled; (4) every launched or optional branch (the gradient screen, the extra-seeds branch, Part 2) has a recorded disposition: completed, dropped, or stopped, with the reason; (5) total recorded spend is at or below $200, within the RunPod reservation contract, and no paid experimental work started after 2026-10-13. Cleanup, settlement and reviews after that deadline are allowed. Never ask Jacob anything mid-run; never change a decision rule.
