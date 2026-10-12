# P04 — With no chain of thought, how well can a model plant a subtle code bug, and does a monitor still catch it?

**Probe tier. Status: complete. Spend $7.79 / $75 cap. 2026-10-12.**

## Answer first

On toy competitive-programming backdoor tasks (APPS, 143 problems), for the two cheap open models
whose reasoning can be genuinely switched off (DeepSeek-V4-Flash, Qwen3.8-Flash):

1. **Reasoning is load-bearing for one-pass code sabotage.** With reasoning truly off (0 reasoning
   tokens on 98–100% of generations), the rate at which the model plants a *working, subtle*
   backdoor (passes all public tests, wrong — not crashing — output on a valid secret input it
   chooses) is **low: ~10%** (best-of-3). Turn reasoning on (effort "low") and it jumps to
   **51%** (DeepSeek) and **64%** (Qwen). Uplift **+0.41 [0.32, 0.49]** and **+0.54 [0.45, 0.62]**
   (95% problem-clustered bootstrap CI; both exclude 0). This supports the "CoT is necessary for
   hard sabotage" safety case at this model scale (Emmons et al. 2025).

2. **A code-only monitor catches these backdoors well — but mostly because they are not subtle.**
   A trusted LLM reading only the comment-stripped code separates successful backdoors from benign
   solutions almost perfectly (AUROC ~0.99). At a lenient 5% false-positive rate it catches
   ~98–99% of even the reasoning-off backdoors; at a strict **1% FPR the primary text monitor
   (luna) catches only 63%** of reasoning-off backdoors, and adding the chain of thought raises
   that to **99%**. So CoT, when available, materially helps the monitor at strict thresholds. The
   big caveat: these cheap models' backdoors contain a conspicuous special-case branch, so the high
   catch rate reflects **low subtlety of the attacker**, not a strong defense against a determined
   one.

**GLM-5.3-Flash is not a clean test of "no CoT":** its reachable reasoning floor on OpenRouter is
effort "minimal" (~224 reasoning tokens, not zero), and at that floor it already plants backdoors
**76%** of the time (off) vs **80%** (on), uplift +0.04 [−0.04, 0.13], not significant. Read GLM as
"a little reasoning is already enough for it," not as a reasoning-off result.

**Decision (per the plan): outcome O1 (reasoning load-bearing) for the genuinely-reasoning-off
models.** This backs CoT monitoring for this capability level; document it. The worrying case O2
("one forward pass is enough *and* the code-only monitor misses much") is **not** observed at this
model scale. A natural next step is Stage 2 (activation probe vs the text monitor) — exploratory,
below.

## Numbers

### Sabotage success (subtle = passes tests + wrong, non-crashing output on a valid secret input)

Best-of-3 per (model, arm) over all 143 usable problems; a generation that produced no code counts
as a failed attempt (so the denominator is all 143, never conditioned on extraction).

| model | reasoning off | reasoning on | uplift (on − off), 95% CI |
|---|---|---|---|
| DeepSeek-V4-Flash | 0.105 [0.056, 0.161] | 0.510 [0.427, 0.594] | **+0.406 [0.322, 0.490]** |
| Qwen3.8-Flash | 0.105 [0.056, 0.161] | 0.643 [0.566, 0.720] | **+0.538 [0.455, 0.622]** |
| GLM-5.3-Flash (floor "minimal") | 0.755 [0.685, 0.825] | 0.797 [0.727, 0.860] | +0.042 [−0.042, 0.126] n.s. |
| pooled (3 models) | — | — | +0.329 [0.275, 0.382] |

k=1 (single attempt) is lower throughout (e.g. DeepSeek off 0.070, on 0.350; Qwen off 0.049, on
0.399), same direction. Subtle and crash-inclusive success are nearly identical (successful
backdoors are overwhelmingly subtle, not crashes), so the table above is essentially unchanged if
crashes are counted.

**DeepSeek/Qwen on-arm is a lower bound.** With reasoning on, these two reason past the 20k-token
budget on the harder problems and emit no code on ~56–66% of generations (GLM extracts cleanly).
Those count as failed attempts here; a larger budget could only raise the on-arm rate and the
uplift. When they *do* emit code, it succeeds almost always (DeepSeek 138/148, Qwen 172/189 per
generation). That runaway reasoning is itself a finding: `reasoning:{enabled:true}` never
terminates within 16–20k tokens on this task, so the on-arm uses bounded effort "low".

### Monitor (trusted LLM reads comment-stripped code, emits 0–100 suspicion)

AUROC, successful backdoor vs benign, pooled over models (problem-clustered bootstrap):

| monitor | code-only | code + CoT |
|---|---|---|
| luna (gpt-6-luna, logprob) | 0.992 [0.984, 0.998] | 0.9996 [0.999, 1.0] |
| glm-5.3-flash (integer) | 0.997 [0.993, 1.0] | 1.000 |

Catch rate of **reasoning-off** (no-CoT) backdoors, code-only monitor, threshold set on benign:

| monitor | @1% FPR | @5% FPR |
|---|---|---|
| luna | **0.63** | 0.99 |
| glm | 0.96 | 0.99 |

Catch rate with CoT available (reasoning-on backdoors): luna code-only 0.86 → code+CoT 0.996 at 1%
FPR; i.e. **CoT adds ~+13–36 pp at the strict threshold** (side prediction confirmed). Benign
false-positive rate is low (luna ~4%, glm ~2% of benign solutions score > 50), confirming the FPR
side prediction.

## Predictions vs outcomes (written before the run)

| # | prediction (prob) | outcome |
|---|---|---|
| O1 | Reasoning load-bearing: uplift ≥15 pp (CI>0), off low, code-only catches ≥70% of off backdoors (0.45) | **Confirmed** for DeepSeek/Qwen (uplift +0.41/+0.54; off ~10%). Code-only catch ≥70% holds at 5% FPR (~98%) but not at 1% FPR for luna (0.63); glm 0.96. |
| O2 | One forward pass enough: uplift <10 pp, off plants backdoors, monitor misses much (0.30) | **Not supported** for the reasoning-off models (large uplift). GLM looks like O2 but its "off" is minimal-reasoning, not off. |
| O3 | Too weak: both arms <10% (0.15) | **No** — on-arm 0.51–0.80. |
| O4 | Measurement failure (0.10) | **No** — witnesses clean, exploit checker validated against the dataset's own backdoors. |
| side | code+CoT catch ≥10 pp over code-only (0.6) | **Confirmed** (luna off-arm 0.63→0.99 at 1% FPR). |
| side | benign monitor FPR < 15% (0.6) | **Confirmed** (~2–4%). |

## What this changes

Backs the CoT-monitoring safety case **at this (cheap, open) model scale**: reasoning-off subtle
sabotage is rare for the models that can truly turn reasoning off, and when a backdoor is planted a
code-only monitor flags it easily — because the backdoors are structurally obvious. The informative
open question is whether a *more capable* model can plant a backdoor subtle enough to drop the
code-only catch rate while keeping reasoning off; that is the case where a CoT-free activation
monitor would matter. P04 did not find that case with these models.

## Follow-ups (exploratory; not yet run)

1. **Stronger models** (glm-5.3-prime, grok-4.7, gpt-6-sol): the subtlety ceiling here is the cheap
   models', not the method's. This is the highest-value next run and the one that could produce O2.
2. **Stage 2 activation probe** (plan): a generator-side linear probe on a 27B model's residuals at
   the last code token vs the text monitor, on reasoning-off backdoors. Needs a GPU pod (raises the
   cap toward $150). Only worth it once a model produces backdoors the *code-only text* monitor
   misses — not the case here, so Stage 2 is **deprioritized** pending finding #1.
3. **Subtlety hand-labels**: a 25-item blind page to check that "monitor catches it" means genuine
   subtlety vs an obvious special case. Deferred; the per-generation inspection already shows the
   backdoors are conspicuous.

## Limitations

- **"Reasoning off" is not literally "one forward pass."** It is the provider's reasoning parameter
  disabled; the off arm still has a content channel. Measured leakage is modest (visible
  deliberation on 11–15% of DeepSeek/Qwen off generations; reasoning_tokens 0 on 98–100%). GLM's
  floor is "minimal", not off.
- **Exact-text grading drops 7/150 problems** (143 usable), including ~6 decimal-answer problems, so
  decimal/geometry/probability tasks are under-represented. The real APPS grader allows numeric
  tolerance; we did not, to keep the exploit "differs" test unambiguous.
- **High monitor AUROC reflects low attacker subtlety.** The backdoors carry an explicit
  special-case branch; a code reader spots it. This is a statement about these cheap models, not a
  general claim that code-only monitoring suffices.
- **No input validators** in the dataset, so validity = "the reference solution runs cleanly on the
  secret input and emits output" (weaker than a spec check; input-validity clause follows Control-Tax
  / Anthropic 2025, not Greenblatt et al.).
- **One validated judge suffices for a probe**; luna is the headline (near-0% unscored), glm is the
  second family (~4% unscored from integer parsing). The monitor is self-validated by its
  ground-truthed AUROC; no separate hand-label pass was run.
- Single dataset, single difficulty tier (APPS "interview"), three cheap models.

## Overlap with prior work (lit review: `litreview.md`)

Severi et al. 2026 (arXiv 2608.02820) force an *empty reasoning block* in *fine-tuned* trigger
backdoors (a memorised constant), and never test an answer-only monitor — related, not the same
question. Control-Tax (arXiv 2506.05296) reports reasoning-off-vs-on planting but under a 10-retry
edit scaffold (not one forward pass). The reasoning-off-vs-on uplift on one-pass backdoor success
paired with a code-only catch rate was the gap; P04 fills it for cheap open models.

## Reproduce

- Plan: `plan.md`. Run log + decisions + the container incident: `log.md`.
- Data: `configs/P04/tasks.json` (150 frozen, seed 0; 143 usable in `usable_task_ids.json`),
  from `RoganInglis/apps-control-arena` (MIT, rev 29db28f7…).
- Pipeline: `scripts/p04_run.py {screen,gen,sandbox,monitor,analyze}`; code in `src/emx/p04/`.
- Tables: `results/P04/analysis/*.csv` (incl. `catch_by_arm.csv`). Seeded raw samples:
  `results/P04/samples.md` (seed 0; private toy backdoors).
- Spend $7.79 (`logs/costs.jsonl`): gen $6.20, monitor $0.81 (priced from tokens — luna/OpenAI
  returns no cost field), pilots+probes ~$0.78. Wall-clock: screen ~20 m, gen 73 m, sandbox 42 m,
  monitor 4 m, analysis ~3 m.
