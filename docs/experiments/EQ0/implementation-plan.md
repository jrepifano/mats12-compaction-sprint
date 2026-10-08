# EQ0 implementation plan

Approved by Jacob in plan mode, 2026-10-08 (no Claude Doc mirror: plans reviewed in plan mode stay
repo-only). The design is in `prereg.md`; this file says how it is built and run.

## Frozen interfaces (main session, before the agents start)
- `src/emx/qwen/registry.py`: base and adapter pins, layer table, stop ids, `SAMPLING`, the
  rank-1 recipe (`RANK1`), shared with E11–E13.
- `src/emx/eq0/contracts.py`: caps, question sets, file layout, steps, judges, validation and
  pass-rule thresholds. `emx.spend.CAPS["EQ0"]` (fixed limits: $20, GPU $15, API $5).
- Generation CSV = `emx.contracts.GEN_COLUMNS`, one per model at `results/EQ0/gen/<model_key>.csv`
  with a `.meta.json` sidecar; bench records at `results/EQ0/bench/{generation,train,lora_check}.json`;
  each pod step writes `results/EQ0/MANIFEST.<step>.sha256` last; the closing `manifest` step lists
  every file of the other step manifests plus those manifests.

## Work breakdown (three parallel Opus agents; no paid calls, no commits)
| Unit | Owner | Files | Done when |
|---|---|---|---|
| A. Pod tooling | agent A | `scripts/pod_push.sh`, `pod_setup.sh`, `pod_pull_results.sh` (EQ0 cases), `scripts/eq0_watchdog.sh`, minimal additive `pod_up.py` gates if needed, `tests/eq0/test_pod_eq0.py` | EQ0 and the other experiments' pod tests pass; push dry run lists the right files |
| B. On-pod pieces | agent B | `src/emx/qwen/{render,gen,train}.py`, `scripts/eq0_pod.py`, `scripts/pod_run_eq0.sh`, `tests/qwen/*`, `tests/eq0/test_pod_steps.py` | rendering matches the tokenizer template; plan = 5,440 unique requests; rank-1 config touches one module (18,944 + 3,584 params); chain semantics tested with stubs |
| C. Laptop pieces | agent C | `scripts/eq0_{judge,validate,analyze}.py`, `src/emx/eq0/{analysis,validation}.py`, `tests/eq0/test_{analysis,validation,judge_cli}.py` | pass rule and validation bar pass and fail on planted data; judge CLI projects and refuses over the cap |
| Integration | main session | contracts, `CAPS`, prereg, run log | full suite `pytest -n auto --dist loadgroup` passes |

A, B and C are independent given the frozen interfaces. Integration follows all three.

## Verification
- Unit tests as above; the full suite before the code review.
- gpt-6-astra code review (one round; more only for BLOCKING findings), then dry runs:
  `pod_up.py --exp EQ0 --slot _A --dry-run`, `pod_push.sh --exp EQ0 --slot _A --dry-run --show-files`,
  `EQ0_DRY_RUN=1 bash scripts/pod_run_eq0.sh`.
- On the pod: the smoke step gates generation; the LoRA-scale check flags but doesn't stop.
- After the run: manifests verify locally before termination; `merge_ledger.py report` shows spend.

## Paid steps (run unattended under the cap, Jacob approved 2026-10-08)
1. RunPod balance against every experiment's planned GPU spend (free GraphQL read).
2. `emx.runpod_reservations balance` → `admit` → `reserve --usd 15`; `pod_up --reservation`; push;
   `pod_run_eq0.sh --detach` (≈ $5–9).
3. Pull and verify; then terminate with `pod_down --reservation` (separate commands).
4. Judge (≈ $2–4, projected first), validate (Opus subagents, $0), analyze.

## Failure handling
- An agent that stalls is stopped and its unit split smaller; agents make small edits.
- A failed gating pod step stops the chain; the pod is pulled and terminated, the cause fixed on the
  laptop, and the step rerun on a fresh pod within the $15 GPU line.
- If the GPU line would be crossed, the killswitch stops the pod; results are pulled well before.
