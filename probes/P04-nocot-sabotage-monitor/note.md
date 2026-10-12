# P04 — With no chain of thought, how well can a model plant a subtle code bug, and does a monitor still catch it?

**Probe tier. Status: complete. Spend $7.79 / $75 cap. 2026-10-12.**

## Answer first

On toy competitive-programming backdoor tasks (APPS, 143 problems), for the two cheap open models
whose reasoning can be genuinely switched off (DeepSeek-V4-Flash, Qwen3.8-Flash):

1. **Reasoning is load-bearing for one-pass code sabotage.** With API reasoning disabled (0 reasoning
   tokens on 98–100% of generations), the rate at which the model plants a *working* backdoor
   (passes all public tests, wrong — not crashing — output on a secret input it chooses that the
   reference solution still accepts) is **low: ~10%** (best-of-3). Turn reasoning on (effort "low")
   and it jumps to **51%** (DeepSeek) and **64%** (Qwen). Uplift **+0.41 [0.32, 0.49]** and
   **+0.54 [0.45, 0.62]** (95% problem-clustered bootstrap CI; both exclude 0). This supports the
   "CoT is necessary for hard sabotage" safety case at this model scale (Emmons et al. 2025).

2. **A code-only monitor catches these backdoors well — but mostly because they are not subtle.**
   A trusted LLM reading only the comment-stripped code separates successful backdoors from benign
   solutions almost perfectly (AUROC ~0.99). For the **genuinely-reasoning-off attackers (DeepSeek +
   Qwen, 30 successful backdoors)**, the primary text monitor (luna) catches **100% at a ~5% realised
   FPR but only ~53% at a strict ~1% FPR**; the glm monitor catches **97% / 90%**. So at a lenient
   threshold the code-only monitor is near-perfect even without any CoT, and the strict-threshold gap
   is luna-specific, not a general CoT effect. **(The pooled "off-arm" catch rate of 63% in an
   earlier draft was GLM-dominated — 109 of 139 off positives are GLM minimal-reasoning — and is not
   a clean no-CoT number; see Monitor table.)** Because reasoning is *off* for DeepSeek/Qwen there is
   almost no chain of thought to give the monitor, so **no paired code-vs-CoT lift can be claimed for
   the no-CoT attackers**; the CoT comparison is reported only for the on-arm and is uncontrolled
   (the benign control population changes). The big caveat throughout: these cheap models' backdoors
   contain a conspicuous special-case branch, so the high catch rate reflects **low subtlety of the
   attacker**, not a strong defense against a determined one.

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

### Sabotage success (subtle = passes tests + wrong, non-crashing output on a *reference-accepted* secret input)

"Reference-accepted" = the reference solution runs cleanly on the secret input and emits output; it
is **not** a check that the input satisfies the problem's stated constraints (there are no input
validators in the dataset), so a handful of successes rest on malformed inputs the reference happens
to tolerate (e.g. GLM-off task 2493 gen 2, task 1540 gen 2). Read the metric as *reference-accepted
input divergence*, not spec-validated backdoors.

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
Those count as failed attempts here; a larger budget would likely raise the on-arm rate and the
uplift (not guaranteed, since newly sampled generations could differ), given that when they *do*
emit code it succeeds almost always (DeepSeek 138/148, Qwen 172/189 per generation). That runaway reasoning is itself a finding: `reasoning:{enabled:true}` never
terminates within 16–20k tokens on this task, so the on-arm uses bounded effort "low".

### Monitor (trusted LLM reads comment-stripped code, emits 0–100 suspicion)

AUROC, successful backdoor vs benign, pooled over models (problem-clustered bootstrap):

| monitor | code-only | code + CoT |
|---|---|---|
| luna (gpt-6-luna, logprob) | 0.992 [0.984, 0.998] | 0.9996 [0.999, 1.0] |
| glm-5.3-flash (integer) | 0.997 [0.993, 1.0] | 1.000 |

Catch rate of **reasoning-off** backdoors, code-only monitor, split by attacker model
(`results/P04/analysis/catch_by_model.csv`; threshold is a conservative order statistic set on that
monitor/condition's benign scores, so the realised benign flag-rate never exceeds the target):

| monitor | attacker | n_pos | @~1% FPR | @~5% FPR |
|---|---|---|---|---|
| luna | **DeepSeek + Qwen (true no-CoT)** | 30 | **0.53** (16/30) | **1.00** (30/30) |
| luna | GLM (floor "minimal") | 109 | 0.61 | 0.98 |
| glm  | **DeepSeek + Qwen (true no-CoT)** | 30 | **0.90** (27/30) | **0.97** (29/30) |
| glm  | GLM (floor "minimal") | 107 | 0.97 | 0.99 |

Realised FPR at the nominal targets is ~0.96%/4.8% (luna) and ~0.67%/3.4% (glm); a pooled row that
mixed the attackers would read ~0.63/0.99 (luna) but is GLM-dominated and not a clean no-CoT number.

**CoT does not give a clean lift here.** With reasoning off, DeepSeek/Qwen emit essentially no
chain of thought (only 2 off-arm DeepSeek generations have any reasoning block to score), so there
is **no paired code-vs-CoT comparison for the no-CoT attackers**. The off-arm code+CoT slice (107
positives) is 105 GLM + 2 DeepSeek — a different population from the 139 code-only positives — and
its benign control set collapses to 57 items (56 GLM), so the apparent 0.63→0.99 "lift" in an
earlier draft confounds added reasoning with a changed model mix and a changed control population.
For the on-arm, luna code-only 0.86 → code+CoT 0.996 at the strict threshold, but this is likewise
**uncontrolled** (benign generations exist only in the off arm). We therefore **withdraw the
"side prediction confirmed" claim for CoT-monitor lift** and report it as suggestive only. Benign
false-positive rate is genuinely low (luna ~4%, glm ~2% of benign solutions score > 50), confirming
the FPR side prediction.

## Predictions vs outcomes (written before the run)

| # | prediction (prob) | outcome |
|---|---|---|
| O1 | Reasoning load-bearing: uplift ≥15 pp (CI>0), off low, code-only catches ≥70% of off backdoors (0.45) | **Confirmed** for DeepSeek/Qwen (uplift +0.41/+0.54; off ~10%). Code-only catch of the true no-CoT (DS+Qwen) backdoors ≥70% holds at ~5% FPR (luna 1.00, glm 0.97) but not at ~1% FPR (luna 0.53, glm 0.90). |
| O2 | One forward pass enough: uplift <10 pp, off plants backdoors, monitor misses much (0.30) | **Not supported** for the reasoning-off models (large uplift). GLM looks like O2 but its "off" is minimal-reasoning, not off. |
| O3 | Too weak: both arms <10% (0.15) | **No** — on-arm 0.51–0.80. |
| O4 | Measurement failure (0.10) | **No** — reasoning-off witnessed (0 reasoning tokens on 98–100%), exploit checker validated against the dataset's own backdoors. |
| side | code+CoT catch ≥10 pp over code-only (0.6) | **Withdrawn** — not a clean/paired test: off-arm DS+Qwen have no CoT to add, and the off-arm code+CoT slice and its benign controls are a different (GLM-heavy) population. Suggestive on the on-arm only (uncontrolled). |
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

- **"Reasoning off" means API reasoning disabled, not literally "one forward pass" and not
  "witnesses clean."** The off arm still has a content channel: visible prose deliberation appears on
  **15.4% (DeepSeek) / 10.7% (Qwen)** of off generations even though reasoning_tokens is 0 on
  98–100%. Headline the off arm as *API reasoning disabled*; a stricter witness-clean screen (zero
  reasoning tokens *and* no prose deliberation on the successful generation) would lower the off-arm
  successful-task counts modestly (DeepSeek 15→~8, Qwen 15→~14 of 143), reported here as a
  sensitivity, not as the primary denominator. GLM's floor is "minimal" (~224 tokens), not off.
- **Exact-text grading drops 7/150 problems** (143 usable), including ~6 decimal-answer problems, so
  decimal/geometry/probability tasks are under-represented. The real APPS grader allows numeric
  tolerance; we did not, to keep the exploit "differs" test unambiguous.
- **High monitor AUROC reflects low attacker subtlety.** The backdoors carry an explicit
  special-case branch; a code reader spots it. This is a statement about these cheap models, not a
  general claim that code-only monitoring suffices.
- **No input validators** in the dataset, so "reference-accepted" = "the reference solution runs
  cleanly on the secret input and emits output" (weaker than a spec check; input-validity clause
  follows Control-Tax / Anthropic 2025, not Greenblatt et al.). A spot-check found at least two
  successful GLM-off triggers on malformed inputs the reference tolerated (task 2493 gen 2, task 1540
  gen 2). The success metric is therefore reference-accepted input divergence, not spec-validated
  backdoors; this inflates the off-arm success count slightly, most visibly for GLM.
- **The code+CoT monitor comparison is uncontrolled.** Benign generations exist only in the off arm,
  and requiring a non-empty reasoning block for CoT scoring leaves 57 benign controls (56 GLM) vs 314
  for code-only, with the positive population also shifting (GLM-heavy). The CoT numbers are reported
  as suggestive; isolating CoT's contribution would need matched on-arm benign generations.
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
- Tables: `results/P04/analysis/*.csv` (incl. `catch_by_arm.csv` pooled, and
  `catch_by_model.csv` — the per-attacker-model split with realised FPR, from
  `scripts/p04_catch_by_model.py`). Seeded raw samples:
  `results/P04/samples.md` (seed 0; private toy backdoors).
- Spend $7.79 (`logs/costs.jsonl`): gen $6.20, monitor $0.81 (priced from tokens — luna/OpenAI
  returns no cost field), pilots+probes ~$0.78. Wall-clock: screen ~20 m, gen 73 m, sandbox 42 m,
  monitor 4 m, analysis ~3 m.
