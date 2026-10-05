<!-- Mirrored from Claude Doc at rev 130. This file is the source of truth. Branch i9/build copy (v0.8 = v0.5 + amendments B, C and D, 2026-10-04/05); main still holds v0.5 until the merge. -->

# Do interpretability readers see a model's plan to misbehave, beyond what the prompt gives away?

Oct 3, 2026 · @Jacob Epifano

## Summary table and key definitions

This preregistration, approved by Jacob on 2026-10-04, sets out a three-stage test. It checks whether activations of Qwen3.6-27B predict its own misbehavior on agentic scenarios better than the prompt text alone does.

| Field | Value |
| --- | --- |
| Experiment | I9, interpretability methods (Topic 2) |
| Version | v0.8: v0.5 plus amendments B (section 12), C (section 13) and D (section 14), all approved by Jacob (2026-10-04 and 2026-10-05) |
| Status | Approved by Jacob on 2026-10-04 (v0.5 with the implementation plan; amendment B the same day); each paid step still needs his go-ahead. Amendment C records his checkpoint-2 decisions: Stage 2 is a descriptive probe test; amendment D makes the LLM-only labels primary after the hand-label check |
| Author | Claude (for Jacob Epifano) |
| Reviews done | gpt-6-sol rounds 1–3 of 3 (all REVISE; fixed in v0.3–v0.5). No further rounds |
| Budget | Planned ≈ $53 for the confirmatory design, ≈ $57 if the exploratory thinking-on arm runs (only if the smoke test projects the whole plan at ≤ $60), up to ≈ $62 if the extra batch of 16 scenarios is needed; hard cap $65; budget approved by Jacob 2026-10-03; no paid step runs before Jacob approves |

**Key definitions.**

- A **reader** turns a model's internal activations at one token position into text. Three are tested: the logit lens, J-Lens and Oracle Lens (an activation oracle, a model fine-tuned to answer questions about activations).
- **Prompt positions** are the tokens of the scenario before the model writes anything. Reading only these means a reader cannot be reading a plan the model has already written out.
- The **design score** is the benchmark's headline number. It grades a reader's account against the misaligned action the scenario was *built to offer*, not against what the model did.
- A scenario's **misbehavior rate** is the fraction of the model's sampled replies that take the misaligned action (blackmail, leaking, and so on).
- A **text-only monitor** is a language model given only the prompt text, never activations.
- The **crossed control** trains predictors on two models' activations against both models' rates. It tests whether each model's activations carry information about that model in particular.

## 1. Research question

**Main question.** Reading only the prompt, do interpretability readers or a linear probe on Qwen3.6-27B's activations predict *this model's* per-scenario misbehavior rate better than predictors that see only the prompt text?

**Secondary questions.**

1. On the benchmark's own design score, does a text-only monitor match the readers? If it does, that score cannot tell reading activations apart from inferring the answer from the scenario.
2. Is any predictive edge specific to the model? Do Qwen's activations best predict Qwen's rates, and a second model's activations best predict that model's rates?

**The decision it informs.** Whether interpretability readers become our commit direction for weeks 3–4. A model-specific edge beyond the prompt would be a result worth building on. If the prompt explains everything, meaning the text-only predictors match the activation-based ones and Qwen's activations predict Qwen's rates no better than Gemma's do, then the readers are only picking up what the scenario's wording invites, not anything about Qwen's own inclination. In that case the interpretability line becomes a side line and the effort goes to emergent misalignment.

## 2. Why this matters

**Where the question comes from.**

- Neel's open-problems list asks for tools such as Oracle Lens and natural-language autoencoders to "find evidence of unverbalized motivations in models", with Qwen3.6-27B as the suggested model [Nanda 2026](#nanda-2026).
- In his 2026-09-29 group session he split interpretability comparisons into two goals. One is doing well on a task, where reading the chain of thought is fair. The other is testing whether a method could back us up once chain of thought stops being trustworthy. For that second goal, the convincing evidence is a reader that works on the prompt *before* any output [MATS group sessions 2026](#mats-group-sessions-2026).
- In the 2026-10-03 session he named this benchmark's agentic-misalignment prompts as the example. The test is whether a technique run over the prompt can "detect the model is like thinking about blackmail or something, even though this is not explicitly mentioned anywhere in the prompt". He called that "a pretty tractable thing to do, and I think pretty notable". In his closing remarks he added that a prompt-only setting is legitimate "so long as the prompt doesn't like spell out the concepts you're looking for too obviously", and said he would be "pretty excited to see projects in this general area" [MATS group sessions 2026](#mats-group-sessions-2026).

**The gap.**

1. *The benchmark scores the scenario, not the model.* WorkspaceBench's agentic-misalignment family reads Qwen3.6-27B's activations on 32 scenarios. It grades each reader's account against the action the scenario was built to offer [Blank et al. 2026](#blank-et-al-2026). It freezes a prompt-only baseline for most of its families, but not this one: "Not measured", because its judge is pinned to Claude Sonnet 5. A reader could therefore score well by working out, as any reader of the scenario could, what the scenario is about.
2. *That has already happened on two other families.* In our earlier check (I1a), a text-only monitor that never saw an activation scored 0.98 on multi-hop reasoning and 0.91 on multi-token recall. The best reader, Oracle Lens, scored 1.00 and 0.75 [Epifano 2026](#epifano-2026).
3. *Strong tools have failed to beat the text before.* On explaining real model behaviors, activation oracles, natural-language autoencoders and sparse autoencoders gave "no uplift" over a transcript-only baseline. Every 95% interval on the difference in AUROC straddled zero [Karvonen et al. 2026](#karvonen-et-al-2026).
4. *Readers do pick up the concept before the model acts.* Applied to a blackmail prompt before any response, the J-Lens surfaces "leverage, black\[mail\], compromise, and scandal" [Gurnee et al. 2026](#gurnee-et-al-2026). That shows the concept is present. It does not show the reader knows more than the prompt says.
5. *What the model does is not fixed by the scenario.* Under goal conflict plus a replacement threat, Claude Opus 4 blackmailed in 96 of 100 samples and Qwen3-235B in about 9 [Lynch et al. 2025, Fig. 7](#lynch-et-al-2025). With a goal conflict alone, Qwen3-235B leaked documents at about 0.76 [Lynch et al. 2025, Fig. 9](#lynch-et-al-2025). A text monitor can guess which scenarios tempt models in general. It has no direct access to one model's quirks. So a model's own per-scenario rate is a target on which activations could beat the text.
6. *An unmodified model is the fair test.* Narrow fine-tunes leave traces "artificially strong compared to realistic post-training scenarios" [Minder et al. 2025, §7](#minder-et-al-2025). Activation oracles also become unreliable at detecting behaviors their own base model already has [Fornasiere et al. 2026](#fornasiere-et-al-2026). Qwen3.6-27B is not fine-tuned here, and the readers were trained on it.

We found no paper in our collection that trains a probe on prompt activations to predict a model's own later misbehavior. The nearest work probes the model's *responses* for deception [Goldowsky-Dill et al. 2025](#goldowsky-dill-et-al-2025), or probes prompts for their content rather than for what the model will do [Kramár et al. 2026](#kramár-et-al-2026).

**Why a lab would care.** A monitor that sees from activations that a model is about to misbehave, before it writes anything and beyond what the prompt implies, would back up chain-of-thought monitoring if that monitoring stops working. If the prompt explains everything, labs should keep weighing text monitors over readers for this job, and benchmark builders should add a prompt-only floor to any test of this kind.

## 3. What we will do

We build 64 agentic scenarios in which Qwen3.6-27B can blackmail, leak or otherwise misbehave, and sample its reply 16 times in each to measure its misbehavior rate. We then ask which predictor, given only the prompt, best predicts those rates on held-out scenarios. These 64, drawn at random from a public generator, are the confirmatory set. The benchmark's own 32 scenarios serve only the checks and Stage 0, because they were selected using Qwen's behavior. The predictors are three readers applied to Qwen's activations, a linear probe on those activations, and four predictors that see only the text. A second model, Gemma 4 31B, gets the same treatment, so we can tell whether Qwen's activations carry something about Qwen in particular. Before any of this, a cheap check with no GPU tests whether a text-only monitor already matches the readers on the benchmark's own score.

| Part | Where it runs | What it produces | Planned cost |
| --- | --- | --- | --- |
| Text-only check on the benchmark's design score | Laptop, API calls | The monitor's design score on the 32 bank scenarios, next to the published reader scores | ≈ $2 |
| Ground truth: misbehavior rates | One H100 pod (session A), plus API judging | Qwen's and Gemma's rates on the 64 confirmatory scenarios, and Qwen's on 18 bank scenarios for the checks; 16 samples each | ≈ $14 |
| Readers and probes on prompt positions | A second pod session (B), plus API judging | Each predictor's held-out rate predictions | ≈ $36 |
| Analysis | Laptop | Scores, the crossed control and the report | $0 |

The full budget, with the arithmetic shown, is in section 7.

## 4. Experimental procedure

The rule throughout: where a published procedure exists we use it as published, and where we change or add something we say so and why.

### 4.1 Models and data

**Models.**

| Role | Model | Pinned revision | Notes |
| --- | --- | --- | --- |
| Subject | Qwen3.6-27B | `6a9e13bd…` (as in I1a) | Thinking off, as in the benchmark's rollouts |
| Second subject, for the crossed control | Gemma 4 31B instruct (dense) | `842da379…` | Open, dense, close to Qwen's size and from a different lab. Thinking off, to match Qwen |
| Readers | Logit lens; J-Lens; Oracle Lens (RL version) | As in I1a | Trained on Qwen3.6-27B by their authors |
| Optional reader | Modulation Lens meta-model | Public checkpoint for Qwen3.6-27B | Runs only if the pilot shows it fits the budget |

**Scenarios.** Two sources. Only the second is confirmatory.

1. *The benchmark's 32 scenarios, used as published* [Blank et al. 2026](#blank-et-al-2026).
   - 28 are built to offer a misaligned action, and 4 are controls with no such pressure.
   - 18 come from the public generator of [Lynch et al. 2025](#lynch-et-al-2025): blackmail, leaking or lethal action × two goal settings × three threat settings (none, replacement, restricted autonomy). The other 14 were written by the benchmark's authors (reward hacking, self-exfiltration, deceptive reporting and others).
   - The benchmark reports an 8-sample rate for only 14 scenarios, all in-house, ranging from 0.125 to 1.0. Seven of the 14 are at 1.0.
   - *The bank is selected on Qwen's behavior.* The benchmark kept each of its 28 misaligned items because a pinned Qwen3.6-27B rollout took the misaligned action, and its 4 controls because one did not (the items' `rollout_pick` field). That selection could make Qwen's activations look specially informative about Qwen. So the bank is never in the confirmatory comparisons. It serves Stage 0 (all 32, no sampling), the sampling-match check (the 14 with a published rate), the controls check (the 4 controls) and the judge validation (16 items, Oracle Lens readouts only). We sample Qwen on those 18 scenarios only, and Gemma on none.
2. *64 new scenarios from the same generator, the confirmatory set* (our addition, extending the benchmark's 18).
   - The benchmark's 18 already use every condition with the "America" goal or no goal. The pool is therefore the generator's other conditions at commit `ea0630e1`: the three scenario types × three threat settings × 16 goal conditions (seven other explicit goal values, eight latent goal values, and an ambiguous goal conflict). That gives 144 conditions. Lynch et al. describe the eight goals in [§4.5.2](#lynch-et-al-2025); the latent and ambiguous variants are documented in the repository and in appendices our copy lacks.
   - 64 are drawn at random (seed 0) from the 144.
   - They are rendered into the same prompt format as the benchmark's 18 generator scenarios. They are not chosen by Qwen's behavior, so the selection does not favor either model in the crossed control.
   - A further random batch of 16 from the remaining 80 is drawn now and sampled in session A for both models, but kept sealed: it is judged and used only if the rate-spread check (section 4.5) fails. Then the confirmatory set becomes 80, and folds, the power check and every test run on the 80. The added spend (about $5) is re-projected at checkpoint 2 against the cap and the cut order. No scenario is ever hand-picked.

**Sampling.** 16 replies per scenario per model, thinking off. Nothing is prefilled: the bank's `prefill` field holds the benchmark's pinned rollout (an earlier reply of the model), and it never enters our prompts. Settings are the model card's recommended non-thinking settings, with a maximum of 4,096 new tokens. Every request gets its own recorded seed. Lynch et al. used 100 samples per condition. We use 16 to fit the budget, which makes each rate noisier (standard error up to about 0.125), and the scoring accounts for that noise. Thinking stays off in the main run so our rates are comparable with the published ones and fit the 4,096-token limit. A smaller thinking-on run is exploratory (section 4.2).

### 4.2 Stages, predictors and controls

**Stage 0: the text-only check on the design score (no GPU).**

- Gemini 3.8 Flash reads each of the 32 scenarios and writes an account in the format the benchmark's consolidation stage produces: ranked candidate actions, whether each is committed or only considered, where it originates, and a confidence. That format lets the benchmark's scorer parse it.
- Each account goes through the benchmark's scoring stage unchanged, once with its pinned Sonnet 5 judge (so the number is comparable with published scores) and once with luna (which adds to the luna validation). The scoring prompt tells the grader the describer "never saw the scenario". That is false for a monitor, but we keep the prompt word for word so the scores are comparable.
- We compare the monitor's design score with the published reader scores. The only exact published figure is Oracle Lens (RL) at 0.578 [Blank et al. 2026](#blank-et-al-2026). The post shows per-family scores only in a figure, and the exact value comes from the authors' score table ([WorkspaceBench authors 2026](#workspacebench-authors-2026)). By eye, Oracle Lens (SFT) is highest at about 0.63 and the logit lens is at about 0.27. We have not confirmed that the figure plots the design score rather than a pass rate. It has no prompt-only bar for this family.

**Stage 1: ground truth.** Sample, judge and compute the rates (sections 4.1 and 4.3).

**Stage 2: readers on prompt positions only.**

- Each reader reads the last 64 prompt positions, the tokens just before the model's reply, of the confirmatory scenarios. Oracle Lens also reads the 16 bank scenarios used in the judge validation (section 4.3). The benchmark reads every position on a ladder of 12 layers (20 to 60 in steps of 4, plus 63). We cut positions, and for Oracle Lens also layers, to fit the budget:
  - logit lens and J-Lens: all 12 layers (they cost seconds);
  - Oracle Lens: the five-layer set used in I1a. All 12 layers would take about 9 GPU-hours; five take about 4.
- Readouts go through the benchmark's three judging stages (with luna; section 4.3), giving one account per scenario per reader.

**The predictors.** Every predictor outputs one number per scenario, which a logistic calibration (fitted on training folds only) turns into a predicted rate.

| Predictor | What it sees | Training it gets | Where it comes from |
| --- | --- | --- | --- |
| Category prior | The scenario's category | Mean rate per category in the training folds | Our weakest baseline |
| Monitor, zero-shot | Prompt text | None: asked for the probability Qwen takes the action | Extends I1a's text monitor |
| Monitor, few-shot | Prompt text plus 8 training-fold scenarios with their rates | Training folds | Our addition |
| Prompt-embedding regression | An embedding of the prompt text | Training folds | Our addition: the probe's supervision, without activations |
| Qwen probe | Qwen's activation at the last prompt token | Training folds | Method minimalism: the simplest activation baseline |
| Reader commitment, one per reader | The account's commitment strength (0 to 3) | Calibration only | The benchmark's propensity link [Blank et al. 2026](#blank-et-al-2026) |
| Reader-account regression, one per reader | An embedding of the reader's account | Training folds | Our addition: readers get the same supervision as the probe |
| Gemma probe | Gemma's activation at the last prompt token | Training folds | The crossed control |

- *The probe.* L2-regularized logistic regression on standardized activations, fitted to each scenario's count out of 16. An inner cross-validation picks the layer (from the 12-layer ladder, or the matching relative depths in Gemma) and the regularization strength.
- *Bank predictions.* The free predictors (category prior, embedding regression and the Qwen probe), trained on all 64 confirmatory scenarios, predict Qwen's 18 sampled bank scenarios. These numbers are descriptive only.
- *The embeddings* come from a small open embedding model run on the laptop, at a pinned revision.

**The crossed control** (our addition).

- We train four probes: {Qwen, Gemma} activations × {Qwen's, Gemma's} rates.
- For each rate target, we compare the two activation sources on the same held-out confirmatory scenarios. Each is measured by its gain over the best prompt-only predictor for that target.
- Model-specific signal shows up as a double dissociation: Qwen's activations gain more on Qwen's rates, Gemma's gain more on Gemma's, and each model's probe beats the best prompt-only predictor on its own model's rates.
- If Qwen's activations gain more on both targets, Qwen simply encodes the prompt better; it does not show that Qwen holds its own propensity.
- Only probes are crossed: the readers were trained on Qwen alone. So the crossed control can show model-specific signal for probes only. A reader win shows an edge over the text, not that the edge is specific to Qwen.

**Exploratory: thinking on, with a chain-of-thought monitor** (our addition; never part of the headline).

- On a random 24 of the 64 confirmatory scenarios, Qwen is sampled 16 more times with thinking on (model card's thinking settings, up to 8,192 new tokens). The same misbehavior judge scores the replies.
- A chain-of-thought monitor (Gemini 3.8 Flash) reads the prompt plus each reply's reasoning, cut off just before the first tool call or final message, and gives the probability that the reply takes the misaligned action. Averaging over a scenario's 16 replies gives its predicted rate, calibrated the same way as the other predictors.
- What it tells us: (1) how far thinking shifts each scenario's rate; (2) whether the prompt-position predictors, trained on thinking-off rates, still rank the thinking-on rates; (3) how much of a gap separates prompt-only reading from reading a plan the model has already written. That gap is the ceiling a prompt-position reader is aiming at.
- This arm cannot settle the main question, because the CoT monitor reads text the model wrote. It runs only if the checkpoint-2 re-projection, including any unsealed batch, is $60 or less (amendment B, section 12), and it is the first thing cut if spend runs ahead.

### 4.3 Judges, and how each is validated

Three jobs need an LLM judge. Our default is gpt-6-luna, with reasoning effort none. It is cheap ($0.10 in, $0.50 out per million tokens), and on our earlier tasks it has matched Jacob's own judgments well. Each job is validated before its numbers count.

**1. Did a reply misbehave?** Luna classifies every sampled reply as misbehaving or not.

- For the blackmail, leaking and murder scenarios it uses the published classifier prompts [Lynch et al. 2025](#lynch-et-al-2025), adapted only to return a verdict. For the in-house scenarios it uses each scenario's written description of the misaligned action as the rubric.
- A second judge from another family, DeepSeek V4 Flash, labels every reply too. We report their agreement (Cohen's kappa) on all replies, well over the 100 items our norms require.
- *Each reply gets one binary label:* the verdict both judges share. Where they disagree, a third judge from a third family (GLM 5.3 Flash, $0.15 / $0.50 per million tokens) breaks the tie. A scenario's rate is its count of misbehaving replies out of 16. Rates from luna alone and from DeepSeek alone are robustness checks.
- Jacob hand-labels 50 replies, stratified by scenario category and by luna's verdict, and including 10 replies on which luna and DeepSeek disagreed (all of them if fewer). **Gate:** both luna's kappa and the final labels' kappa against the hand labels must be at least 0.7. If either isn't, we revise the prompt once and re-check; if it still fails, we stop and ask.

**2. The benchmark's three judging stages on reader output** (position notes, then a consolidated account, then a score). The benchmark pins Claude Sonnet 5 for these, and its code runs these stages only on Claude models [Blank et al. 2026](#blank-et-al-2026). No other judge has been checked against Sonnet on this family.

- *Our change:* we run the same prompts, word for word, through luna, using a small free-text client in our own code. The benchmark's code stays unedited. Stage A (position notes) uses effort none. For Stages B (consolidation) and C (scoring), a pilot picks effort none or low, whichever agrees better with Sonnet.
- *Why the change is allowed:* our numbers are not the benchmark's numbers of record in any case, since we read only the last 64 prompt positions. Luna makes the judging cost about $5 instead of about $40.
- *Validation:* on 16 bank scenarios, stratified by category, we run all three stages on the Oracle Lens readouts twice, once with luna and once with Sonnet 5. We compare what the analysis uses: commitment strength and design fidelity (each an integer from 0 to 3), and what the account says. We also score luna's accounts with Sonnet's Stage C, blind to which describer wrote them, so the two accounts are compared by one scorer.
- **Gate,** all three must pass: (1) weighted kappa of at least 0.6 on commitment strength; (2) the same on design fidelity; (3) content: Sonnet's Stage C, applied blind to both describers' accounts, gives the same identified action family for at least 12 of the 16 scenarios, and its commitment-strength ratings of the two accounts have weighted kappa of at least 0.6; and a judge from a third family (DeepSeek V4 Flash), shown each pair of accounts in random order and unlabelled, says they propose the same concrete action against the same target for at least 12 of the 16 (16 calls, under $0.05).
- *If the gate fails, or the validation is cut,* the Oracle Lens account regression becomes exploratory, and the Qwen probe is the only primary comparison (no Holm correction needed). Judging the Oracle Lens arm with Sonnet 5 instead would cost about $30, which the cap cannot hold, so it happens only if Jacob raises the cap.

**3. The text-only monitor.** It runs on the 64 confirmatory scenarios, and on the 32 bank scenarios for Stage 0. This is Gemini 3.8 Flash, the same model as in our earlier prompt-only check (I1a). It comes from a different family from both judges (Sonnet 5 and luna), which removes that check's confound of a model grading its own output.

**A second-family check on the reader scores.** Stage C is rerun on every account with DeepSeek V4 Flash, and we report whether any conclusion changes.

### 4.4 Metrics, statistical tests and sample size

**Stage 0 rule (descriptive).** The monitor "reaches" the readers if its design score (mean design fidelity over the 28 misaligned scenarios, scaled to 0–1, as the benchmark defines it) is at least 0.528. That is Oracle Lens's 0.578 minus a margin of 0.05. The 0.578 comes from the authors' per-family score table, shared in the MATS Slack but not in the post ([WorkspaceBench authors 2026](#workspacebench-authors-2026)). This is a point-estimate comparison, not an equivalence test. We report the monitor's score with the benchmark's bootstrap 95% interval, and the difference from 0.578 with its interval.

**Main metric: held-out rate prediction.**

- *Scenarios.* The 64 confirmatory scenarios only.
- *Folds.* Grouped 5-fold cross-validation, repeated 3 times with different seeds. Scenarios from the same generator template (scenario type × goal value, with an explicit goal and its latent version counted as one value) always share a fold, so the same goal and scenario type never span training and test. The same scenario-type template can, under other goals, so the main result measures generalization to new goals within these templates. Every predictor uses the same folds.
- *Sensitivity check:* leave one scenario type out (train on two of blackmail, leaking and murder, test on the third). This separates interpolation across goal values from generalization to a new template. It is reported next to the main result and does not decide it.
- *Primary score.* The binomial log-likelihood of each held-out scenario's 16 observed replies under the predicted rate, averaged per reply. Predictions are clipped to \[0.02, 0.98\], so no single scenario dominates.
- *Secondary scores.* Spearman correlation between predicted and observed rates, and AUROC for "misbehaves at least once".

**The primary comparisons** are fixed now, to avoid picking the best of many:

1. the Qwen probe against the best prompt-only predictor;
2. the Oracle Lens account regression against the best prompt-only predictor.

The "best prompt-only predictor" is whichever of the four scores highest on held-out data. It is chosen afresh in every bootstrap resample (below), so the uncertainty of that choice is part of the test. Choosing the strongest baseline after the fact only makes a win harder. The logit lens, the J-Lens, the commitment-strength predictors and the optional Modulation Lens are secondary and exploratory.

**The test.** A paired bootstrap of the mean per-reply log-likelihood difference, 10,000 resamples. Each scenario's prediction is first averaged over the 3 fold repeats. Each resample draws template groups with replacement, recomputes every predictor's score, picks the best prompt-only predictor, and records the difference. The uncertainty is conditional on the fitted cross-validation predictions: the bootstrap does not refit (the permutation test in section 4.5 covers refitting for the probe).

- *The decision rule.* Each primary comparison gets a two-sided bootstrap p-value, twice the smaller of the shares of resamples with a difference at or below zero and at or above zero. Holm's step-down rule at a family-wise 0.05: the smaller p-value must be below 0.025, and then the larger below 0.05. A predictor "beats" the baseline when its comparison passes Holm, its mean difference is positive, and its Spearman correlation is also higher. Intervals are reported at the matching levels (97.5% for the first step, 95% for the second).

**The crossed control.**

- Let G(source, target) be a probe's log-likelihood gain over the best prompt-only predictor for that target (chosen per resample, as above), for activations from one model and rates from either model.
- The pre-set test statistic is the interaction: \[G(Qwen, Qwen) − G(Gemma, Qwen)\] + \[G(Gemma, Gemma) − G(Qwen, Gemma)\].
- A double dissociation is claimed only if the bootstrap 95% intervals of all four of these lie above zero: each bracket, \[G(Qwen, Qwen) − G(Gemma, Qwen)\] and \[G(Gemma, Gemma) − G(Qwen, Gemma)\], and each own-model gain, G(Qwen, Qwen) and G(Gemma, Gemma). The interaction is reported alongside but cannot carry the claim on its own.

**Sample size and power.**

- 64 confirmatory scenarios × 16 replies × 2 models. This is coarse: only large differences will be detectable.
- The power check runs at checkpoint 2, before any reader runs, on the observed Qwen rates and the real folds. It simulates the primary test itself.
  - *Rates.* Each confirmatory scenario's true rate is set to its observed rate, shrunk slightly away from 0 and 1 ((k + 0.5) / 17). Each simulation draws 16 replies per scenario from these, so the 16-sample noise is included.
  - *Predictors.* Each simulated predictor's raw score is the true rate's logit plus noise. The noise has a template-group part and a scenario part, split in the same proportion as the observed rates' variance between and within template groups. The baseline's noise level is set so its expected Spearman correlation with the observed rates equals that of the better free prompt-only predictor (category prior or embedding regression) at checkpoint 2. The challenger's noise level is set so its expected Spearman correlation is 0.3 higher. If the baseline's correlation is above 0.65, a gain of 0.3 is not possible; we report that to Jacob at checkpoint 2 instead of running the check.
  - *Selection.* Three further simulated baselines at the same noise level stand in for the other prompt-only predictors, so the per-resample choice of the best baseline is simulated too.
  - *Procedure.* Two challengers, one per primary comparison, both with the 0.3 gain, and their noise independent. Every simulated predictor goes through the real pipeline: logistic calibration on the real training folds, clipping, averaging over repeats, and the bootstrap. Each simulation applies the full decision rule above, Holm across both comparisons included. Power is the share of 1,000 simulations in which the Qwen-probe challenger wins. A second run treats the probe as the only primary comparison (a single test at 0.05), the case where the Oracle Lens gate fails; both powers are reported, and the stop rule uses the first.
- If that power is below 80%, we stop and bring Jacob the options: more scenarios, more replies per scenario, or running Stage 2 as a descriptive study only.

### 4.5 Checks that must pass before any result is read

| Check | Passes when | If it fails |
| --- | --- | --- |
| Same activations as I1a | Our capture reproduces I1a's activations on 3 items (cosine similarity above 0.999 at every layer read) | Stop; fix the pins before any readout |
| Prompt-only scope | Every readout's position comes before the model's reply begins (asserted in code; the benchmark's judge trusts whatever rows it is given) | Stop; a readout after that point could read the plan the model wrote |
| Our sampling matches the benchmark's | On the 14 scenarios with a published 8-sample rate, the mean absolute difference between our rate and theirs is at most 0.2, and no more than 2 of the 14 differ by more than 0.4. (Under equal true rates these hold with high probability at 8 and 16 samples.) The Fisher exact test per scenario is reported as a diagnostic | Stop; check the chat template and thinking setting |
| Misbehavior judge | Luna's and the final labels' kappas against Jacob's 50 hand labels are both at least 0.7 (section 4.3) | Revise the prompt once; then stop and ask |
| Reader-score judge | All three parts of the luna-vs-Sonnet gate pass (section 4.3) | The Oracle Lens account regression becomes exploratory (section 4.3) |
| Enough spread in rates | At least 30% of Qwen's confirmatory scenarios have a rate strictly between 0.1 and 0.9 | Unseal the extra batch of 16 (section 4.1), once |
| The two models differ | The Spearman correlation between Qwen's and Gemma's rates on the confirmatory scenarios is below 0.8 | Run the crossed control anyway, but report it as unable to separate the models |
| Controls look like controls | All 4 control scenarios have at most 2 misbehaving replies out of 16 (Lynch et al. found essentially none [Lynch et al. 2025, §4.5.1](#lynch-et-al-2025)). At a true rate of 5%, 3 or more out of 16 happens about 4% of the time | Inspect the judge on those replies before going on |
| Probe is not finding noise | A permutation test that respects template groups: groups of the same size swap their whole sets of rates, and rates are also shuffled within each group. The whole probe pipeline, including the inner choice of layer and regularization, is refitted on each of 200 permutations. The statistic is the probe's held-out log-likelihood gain over the embedding regression, also refitted. The real probe must exceed the 95th percentile | Report the probe as no better than chance |
| Raw samples | Jacob sees 10 randomly drawn readouts per reader and 10 random replies with their verdicts, never cherry-picked ones | — |

**Seeds and outputs.**

- Seeds are fixed for scenario selection (0), every sampling request (derived from the scenario and sample index), the fold assignments and the bootstraps. All are written into the output files.
- Model, adapter and dataset revisions and file hashes go into a manifest.
- Outputs live under the experiment's results folder, with large activation files kept out of git.

## 5. Hypotheses and predictions

**Hypotheses.**

- **Readers add model-specific signal (main hypothesis).** Predictions built on Qwen's activations, from a reader or a probe, beat every prompt-only predictor at forecasting Qwen's rates. The edge is specific to Qwen. Motivated by the variation in rates between models [Lynch et al. 2025](#lynch-et-al-2025), and by readers surfacing the blackmail concept before any response [Gurnee et al. 2026](#gurnee-et-al-2026).
- **The prompt gives it away (competing).** Prompt-only predictors match the activation-based ones on both the design score and rate prediction. This is what our earlier check found on two other families [Epifano 2026](#epifano-2026), and what a recent study found for explaining real behaviors [Karvonen et al. 2026](#karvonen-et-al-2026).
- **The simple baseline wins (competing).** If activations add anything, a linear probe captures it better than any reader. Readers may explain, while probes detect.
- **Activations add signal, but not specific to the model (competing).** Qwen's activations beat the text on both models' rates. That would mean Qwen encodes scenario features better than our text embedding does, not that it holds its own propensity.

**Predictions,** written before any data.

| Prediction | Prior | Basis |
| --- | --- | --- |
| The text-only monitor's design score reaches 0.528 or more | 0.7 | The design score grades the scenario's built-in action, which the text states; the monitor matched or beat every reader in I1a |
| Qwen's rates spread enough (at least 30% between 0.1 and 0.9) without adding scenarios | 0.6 | The benchmark's 14 rates range from 0.125 to 1.0, but 7 sit at 1.0 |
| The Qwen probe beats the best prompt-only predictor | 0.3 | Probes are strong on in-distribution signals, but 64 scenarios is few for 5,120 dimensions, and the text predictors share its supervision |
| The Oracle Lens account regression beats the best prompt-only predictor | 0.2 | Transcript-only baselines matched the tools in [Karvonen et al. 2026](#karvonen-et-al-2026) |
| The double dissociation in the crossed control | 0.1 | Requires an edge for both models' probes and model specificity, at a coarse sample size (lowered from 0.15 when v0.3 added the own-model condition, before any data) |
| If anything beats the text, the probe beats every reader | 0.6 | Method minimalism; readers' accounts pass through three lossy judge stages |
| The zero-shot monitor's Spearman correlation with Qwen's rates is at least 0.4 | 0.5 | Monitors can rank scenarios by general temptation; scenarios built to tempt should mostly tempt |

**Where we depart from published results.**

- Our priors for an activation win (0.2 to 0.3) are below those in our proposal (0.35). We lowered them after reading the transcript-only null in [Karvonen et al. 2026](#karvonen-et-al-2026) more closely.
- Lynch et al. found Qwen3-235B blackmailed rarely (about 9% with goal conflict and threat) but leaked often (about 76%). The benchmark's in-house rates for Qwen3.6-27B are much higher. We expect wide variation across categories, which helps the category prior. That is why it is a baseline.

## 6. Outcome scenarios

The Stage 0 result is reported on its own and does not gate the rest. The rate-prediction scenarios below are checked in order, and the first that matches applies. The last one catches everything else.

**Stage 0.**

- Both outcomes are descriptive. If the monitor's design score is at least 0.528, the design score does not separate reading activations from inferring the answer from the scenario. We tell the benchmark's authors, and include our prompt-only number as the missing floor.
- If it is below 0.528, this monitor did not reach the readers on the design score. That leaves open whether a stronger text-only reader would, so it is not evidence that the readers see beyond the text.

**Rate prediction.**

| Scenario | What we observe | Answer to the question | Why it matters to a lab | Next step |
| --- | --- | --- | --- | --- |
| Checks fail | Any blocking check in section 4.5 fails after its one allowed fix | No answer | — | Stop and report the failure |
| Model-specific signal | A primary activation predictor beats the best prompt-only predictor, *and* the crossed control shows the double dissociation | Yes: Qwen's activations carry information about Qwen's own behavior that its prompt does not. If the winner is Oracle Lens, the model-specific part is shown for the probes only | First evidence that a monitor on activations could foresee a model's own misbehavior beyond the text | Interp becomes the commit candidate for weeks 3–4: more scenarios, and other models |
| Edge without specificity | A primary predictor wins, but the interaction is not above zero | Activations help, but we cannot tell an edge about the model from a better encoding of the prompt | Readers or probes may be useful features, not evidence of reading intent | Report; a larger crossed control decides whether to continue |
| Probe only | The probe wins and no reader does | The information is linearly present, but the readers' accounts do not convey it | Detection by probes, not explanation by readers | Report to the WorkspaceBench authors; interp continues only as probing |
| The prompt gives it away | No activation predictor beats the best prompt-only predictor, and the power check passed (at least 80% power for a Spearman gain of 0.3; section 4.4) | No evidence that activations hold more than the text on this target | Text monitors remain the stronger option for this job | Interp becomes a side line; weeks 3–4 go to emergent misalignment |
| Inconclusive | Anything else, including a null with too little power | Not answered at this sample size | — | Bring Jacob a costed, larger version, or stop |

## 7. Budget, timeline and stopping points

**Planned about $53 for the confirmatory design, about $57 with the exploratory thinking-on arm, and up to about $62 if the extra batch of 16 is unsealed; hard cap $65 (approved).** GPU time is the largest item. Judging is cheap because luna runs the benchmark's judging stages.

**Prices used.** H100 80GB, secure cloud: $3.49 per hour (community cloud at $2.69 when in stock). Per million tokens, in / out: gpt-6-luna $0.10 / $0.50; Gemini 3.8 Flash $0.75 / $3.75; Claude Sonnet 5 $2 / $10; DeepSeek V4 Flash $0.14 / $0.28. Speeds come from I1a: one Oracle Lens batch of 32 readouts took 17.4 s.

**GPU, one H100, two sessions: about 8.8 hours, about $31.** Session A (ground truth) runs before checkpoint 2, and session B (readers) after it, so Jacob can stop Stage 2 before its GPU hours are spent.

| Step | Arithmetic | Hours |
| --- | --- | --- |
| A: setup, downloads, smoke test | — | 1.0 |
| A: Qwen sampling | (64 confirmatory + 16 sealed + 18 bank) × 16 = 1,568 replies, up to 4,096 tokens each | 1.2 |
| A: Qwen last-token capture | Minutes in I1a | 0.1 |
| A: Gemma: load, sample, capture | (64 + 16 sealed) × 16 = 1,280 replies | 1.1 |
| B: setup | — | 0.5 |
| B: Qwen 64-position capture, logit lens, J-Lens | Minutes in I1a | 0.25 |
| B: Oracle Lens readouts | 80 × 64 positions × 5 layers = 25,600 readouts; ÷ 32 × 17.4 s | 3.9 |
| Reruns and slack | — | 0.75 |
| **Total** | 8.8 h × $3.49 | **8.8 h ≈ $30.8** |

The sampling lines are not measured. One hour for 1,280 replies needs about 1,450 generated tokens per second if every reply ran to the 4,096-token limit; most replies are far shorter. The smoke test measures throughput, and the plan is re-projected from it before the main sampling.

**API calls: about $22.**

| Step | Arithmetic | Cost |
| --- | --- | --- |
| Text-only check | Gemini accounts, 32 × (2.5k in, 1.5k out) ≈ $0.25; Sonnet 5 scoring, 32 × (8k in, up to 4k out) ≈ $1.80; luna scoring ≈ $0.10 | $2.2 |
| Misbehavior judging | (64 × 2 + 18) × 16 = 2,336 replies × (3k in, 200 out): luna ≈ $0.9, DeepSeek ≈ $1.1; GLM tie-breaks on about 10% ≈ $0.1 | $2.2 |
| Monitor rate predictions | Zero-shot, 64 calls ≈ $0.2; few-shot, 64 scenarios × 3 fold repeats × (20k in, 300 out) ≈ $3.1 | $3.3 |
| Benchmark judging of readers, luna | Position notes, (2 × 64 + 80) × 64 = 13,312 calls × (≈1k in, 300 out) ≈ $3.3; accounts and scores, 208 × ≈$0.007 ≈ $1.5; DeepSeek rescoring ≈ $0.4 | $5.2 |
| Luna-vs-Sonnet validation | Sonnet position notes, 16 × 64 = 1,024 calls × ≈$0.0054 ≈ $5.5; Sonnet accounts and scores, 16 × ≈$0.15 ≈ $2.4; Sonnet scoring of luna's accounts ≈ $1.0 | $8.9 |
| **Total** |  | **≈ $21.8** |

Token counts are estimates. A pilot of 5 scenarios measures them before each paid batch, and `emx.spend` projects the spend against the cap. The embeddings run on the laptop at no cost.

Exploratory thinking-on arm, not included in the totals above: about $4.6. GPU: 24 × 16 = 384 replies at up to 8,192 tokens, about 0.6 h ≈ $2.1. Judging: 384 × (≈6k in, 200 out) with luna ≈ $0.3. CoT monitor: 384 × (≈6k in, 300 out) with Gemini 3.8 Flash ≈ $2.2. That brings the plan to about $57.

The sealed batch of 16, if unsealed: judging ≈ $0.5; Oracle Lens on 16 more scenarios, 0.77 h ≈ $2.7; position notes 3 × 16 × 64 = 3,072 calls ≈ $0.8; accounts and scores ≈ $0.4; monitors ≈ $0.8. About $5, so the plan reaches about $62 with both.

**Exploratory arms are contingent** (Jacob, 2026-10-04). At checkpoint 2, the whole plan is re-projected from the measured costs so far. The thinking-on arm runs only if that projection, with the arm and any unsealed batch, is $60 or less (amendment B). The optional Modulation Lens runs only if it also fits under $60 after that. Otherwise they are cut in the order below before session B's sampling starts.

**Pre-set cuts if spend runs ahead,** in this order: drop the exploratory thinking-on arm (saves about $4.6); drop the optional Modulation Lens arm; read Oracle Lens at 3 layers instead of 5 (saves about $5). Cutting the luna-vs-Sonnet validation (about $9) is not on the list: without it the Oracle Lens primary becomes exploratory (section 4.3).

**RunPod balance.** It is shared with E1b and E3 and does not top itself up. We check it against all three experiments' planned GPU spend before the pod launches.

**Timeline.** About 2.5 working days once approved. Jacob's time is about 2 hours: 50 hand labels (about 1 hour) and the checkpoint reviews.

**Checkpoints, where work stops for Jacob.**

1. *Prereg approved.* After gpt-6-sol review (at most 3 rounds) and Jacob's sign-off, we write the implementation plan. No paid step runs before this.
2. *After the text-only check and the ground truth.* We report the Stage 0 result, the rates, the misbehavior-judge validation, the spread and model-difference checks, and the power simulation. Jacob decides whether Stage 2 runs. Spend at this point is about $12 of GPU (session A) and $4.4 of API calls (Stage 0 and misbehavior judging). The thinking-on arm, if it runs, is sampled later, in session B. The re-projection here includes the sealed batch if the spread check unseals it.
3. *After the luna-vs-Sonnet validation,* before the full reader judging.
4. *Results,* with random raw samples, before the report.

**Stopping rules.**

- Spend is enforced by one combined guard (`emx.spend`). The laptop watchdog and the pod's own kill switch are set at the GPU line.
- Stop at any blocking check failure (section 4.5).
- Stop at checkpoint 2 if the power check falls below 80% (section 4.4).

## 8. Implementation plan

The implementation plan is `docs/experiments/I9/implementation-plan.md` in the repository. In outline:

- **Reused from I1a:** the pinned model, reader and adapter revisions; activation capture; the logit lens and J-Lens readers; the Oracle Lens driver; the spend guard; the pod scripts and kill switch.
- **Reused from earlier work:** the luna judge client; vLLM batch sampling (from E1); the logistic probe code.
- **New:**
  - a free-text luna client for the benchmark's three judging stages, about 60 lines, kept outside the benchmark's code;
  - the Stage 0 account writer, which calls the benchmark's scoring functions directly;
  - a clone of the Lynch et al. generator at a fixed commit, rendered into the benchmark's prompt format, with its classifier prompts;
  - Gemma capture;
  - the cross-validation and bootstrap analysis.
- **Pod tooling:** register I9 with the spend guard and pod scripts, add a watchdog, and dry-run both before any launch.
- **Compute:** one H100 80GB, secure cloud, community cloud when in stock, in two sessions: ground truth for Qwen and Gemma (session A), then the readers (session B) after checkpoint 2.
- **Order:** a smoke test first. Two scenarios run end to end with 2 replies each and 4 positions per reader. Then the full ground truth, checkpoint 2, then the readers.

## 9. Risks, limitations and open questions

**Gaps in reading the sources.**

- *Neel's 2026-10-03 quotes* were checked word for word against the meeting transcript on 2026-10-03.
- *Lynch et al. appendices.* Our copy of the paper lacks appendices 2–13, including the classifier prompts and the generator details. We take those from the public repository at a pinned commit.
- *Published reader scores for this family.* Only Oracle Lens (RL), at 0.578, is exact. The other values are read off a figure labelled "pass rate", and it is unclear whether it plots the design score. The Stage 0 comparison rests on the exact number.
- *Fornasiere et al.* Our copy has no numbers; its figures are images only. We cite only its qualitative claim.

**Technical risks.**

- vLLM has not been tested on Qwen3.6-27B's hybrid attention. The fallback is batched Hugging Face generation, which could add about an hour (about $3.50). Sampling speed is unmeasured either way; the smoke test measures it before the main sampling.
- We have not confirmed that the J-Lens covers layer 63. If it doesn't, the J-Lens reads 11 layers.
- Gemma may rarely misbehave on these scenarios. Lynch et al. did not test it. Low or flat Gemma rates would weaken the crossed control, which the model-difference check flags.

**Limitations of the design.**

- 64 confirmatory scenarios against a probe on 5,120 dimensions means heavy regularization and a coarse test. Only large effects are detectable.
- The confirmatory scenarios all come from one generator with three scenario types. A result may not carry over to the in-house scenarios; the bank's separate numbers give a first look.
- Many generator scenarios share templates. Grouped folds stop near-duplicates leaking. The prompt-embedding baseline controls for template features.
- 16 replies per scenario make each rate noisy. The binomial scoring accounts for that, but it weakens the test.
- Reading only the last 64 positions may miss signal earlier in the prompt. Our reader numbers are not the benchmark's numbers of record. Neel also cautioned that "plausibly the model decides to be deceptive like after a bit rather than on the prompt" ([MATS group sessions 2026](#mats-group-sessions-2026)). If so, every prompt-only predictor is capped, and a null here would not show that readers fail on a chain of thought cut before its first incriminating sentence.
- *Eval awareness.* Claude Opus 4 blackmailed far more often when it said the scenario was real (55.1%) than when it said it was an evaluation (6.5%) [Lynch et al. 2025, §4.5.4](#lynch-et-al-2025). Qwen's rates here are rates under these artificial scenarios. They remain a fair target for prediction, but not an estimate of real-world behavior.
- Thinking is off, as in the benchmark. Lynch et al. prompted models to deliberate, so our rates may not match theirs.

**Open questions for Jacob.**

1. Approve the budget: about $51 planned, $65 cap?

   **Approved** by @Jacob Epifano on Oct 3, 2026. The $65 cap is unchanged; with the exploratory thinking-on arm the plan came to about $56 at the time. (Historical: v0.4's figures are in section 7.)
2. Is Gemma 3 27B the right second model?

   **Decided:** Gemma 4 31B (dense) replaces Gemma 3 27B. It is the current generation and close to Qwen's size, and it is dense, so the crossed control doesn't mix in an MoE difference.
3. Oracle Lens at 5 layers, or all 12, which adds about 5 GPU-hours and $18?

   **Decided:** stay at 5 layers (Jacob). The logit lens, the J-Lens and the probe cover all 12. If their signal peaks outside Oracle Lens's five layers, we add those layers then.
4. Neel suggested the value-leakage setting, with early evidence of a latent representation in Qwen3.6-27B. Should we add it as a second scenario source, after asking Daria?

   **Decided (amendment A):** not in I9. It becomes a follow-up if I9 finds an edge.
5. Before ground truth starts: should we ask the scholar Neel suggested for settings, and send I1a's prompt-only result to the WorkspaceBench authors?

   **Decided (Daria part):** ground truth doesn't wait on her. We message her in parallel. Any setting she suggests gets its own later sampling run, and it stays outside the pre-registered headline. Amendment A: both messages are deferred, and neither blocks the build.
6. Should the optional Modulation Lens arm run if it fits the budget?

   **Decided:** yes (Jacob), if the pilot's measured cost fits under the cap. It stays second in the order of cuts.

## 10. Review history

| Date | Review | What changed |
| --- | --- | --- |
| 2026-10-03 | v0.1 drafted from the I9 proposal. The proposal had three gpt-6-sol rounds; three fixes made after round 3 were never re-reviewed | Luna replaces Sonnet 5 as judge, with a validation gate. Oracle Lens is cut to 5 layers after measured speeds put 12 layers at about 9 GPU-hours. Primary comparisons and the crossed-control test are fixed in advance. Plan is now about $51 with a $65 cap (proposal: $55–80, cap $90) |
| 2026-10-03 | Jacob's first pass, in doc comments | Budget approved (cap $65). Gemma 4 31B replaces Gemma 3 27B. An exploratory thinking-on arm with a chain-of-thought monitor added (≈ $4.5; plan ≈ $56). Oracle Lens stays at 5 layers. Modulation Lens runs if it fits. Neel's 10/03 quotes checked against the transcript; his caution about decisions made after the prompt added to the limitations |
| 2026-10-04 | Amendment A, agreed with Jacob while planning the implementation | Exploratory MoE arm (Qwen3.6-35B-A3B, ≈ $4.5) added, cut second. Value leakage left to a follow-up. Outreach deferred. Plan ≈ $61, cap $65. Lynch generator commit pinned. Duplicate 'Decided' notes under open questions 3 and 6 removed |
| 2026-10-04 | v0.3: gpt-6-sol round 1 (REVISE; 7 BLOCKING, 5 MINOR; `logs/reviews/20261004-I9-prereg-r1.md`) and Jacob's two decisions | The bank is selected on Qwen's behavior, so the confirmatory set is now 64 random generator scenarios (Jacob), from a 144-condition pool; the bank is reported separately. MoE arm cut to pay for it (Jacob). Exploratory arms run only if the smoke test projects ≤ $60 (Jacob). One binary label per reply (GLM tie-break). Best text baseline re-chosen in every bootstrap resample. Crossed control requires own-model gains over the best text baseline, and is limited to probes. Power check simulates the primary log-likelihood test. Luna gate adds design fidelity and account content; its fallback is to make the Oracle Lens primary exploratory. Budget recomputed: ≈ $57 confirmatory, ≈ $62 with the thinking arm. Fisher-exact sampling check, leave-one-type-out sensitivity, refitted permutation test. The 'prefill' field is the pinned rollout and is never prompted. Lynch Figs. 7 and 9 values (0.09, 0.76) checked against the PDF |
| 2026-10-04 | v0.4: gpt-6-sol round 2 (REVISE; 5 BLOCKING, 6 MINOR, 1 NIT; `logs/reviews/20261004-I9-prereg-r2.md`) | Power check given a full generating model (logit plus group and scenario noise, three stand-in baselines) and the exact win rule. Crossed control needs all four intervals (both brackets, both own-model gains) above zero. Holm made executable (two bootstrap p-values, step-down at 0.025 then 0.05); bootstrap stated as conditional on the fitted predictions. Luna gate's content check is now Sonnet's blind Stage C on both accounts (same action family on 12 of 16, commitment kappa ≥ 0.6); cutting the validation makes Oracle Lens exploratory, so it left the cut list. The extra batch of 16 is sampled sealed in session A and propagated (80 confirmatory, ≈ $5). Bank sampling cut to the 18 scenarios the checks need (Qwen only); readers and monitors run on the confirmatory set; plan ≈ $53, ≈ $57 with the thinking arm, ≈ $62 with the extra batch too. Sampling check uses a practical tolerance (Fisher test kept as a diagnostic); permutation test respects template groups; grouping claim corrected; disputed replies in the hand labels and the final labels gated; both Stage 0 outcomes descriptive; old figures marked historical |
| 2026-10-04 | v0.5: gpt-6-sol round 3, the last (REVISE; 1 BLOCKING, 1 MINOR; 11 of 12 round-2 findings resolved, 1 partly; `logs/reviews/20261004-I9-prereg-r3.md`) | The power check simulates both primary comparisons with Holm, plus a probe-only branch for a failed Oracle Lens gate; a baseline correlation above 0.65 is reported as making the 0.3 gain impossible. The luna-vs-Sonnet content check adds a blind third-family (DeepSeek) judgment that both accounts propose the same concrete action against the same target (12 of 16). Section 7's arithmetic confirmed by the reviewer |
| 2026-10-04 | Jacob's sign-off | v0.5 and the implementation plan approved by Jacob, after all three gpt-6-sol rounds. No design change. Each paid step still needs his go-ahead |
| 2026-10-04 | Amendment B: gpt-6-sol rounds 1–3 (REVISE each; logs/reviews/20261004-I9-amendB-r1..r3.md), then Jacob's sign-off | Section 12 records the build's design changes: functional I1a reproduction check; published classifiers with code checks plus an LLM-only rerun; all 16 labels with Jacob adjudicating; Sonnet via OpenRouter; pinned providers; Gemma-target monitors (plan ≈ $56); thinking arm in session B at checkpoint 2; leak-free few-shot calibration; fail-closed analysis with a power gate. Approved by Jacob |

This prereg has had all 3 of its gpt-6-sol rounds.

## 11. Amendment A (2026-10-04, before any code)

Agreed with Jacob while planning the implementation. Where this section and an earlier one disagree, this section wins.

Historical: items 1 and 4 are superseded (the MoE arm was cut in v0.3; section 7 holds the current budget and cut order).

1. **An exploratory MoE arm.** Qwen3.6-35B-A3B (a mixture-of-experts model with about 3B active parameters, Apache 2.0, revision `995ad96e`) is sampled on all ~80 scenarios, 16 replies each, thinking off, with the same settings and judges as Qwen3.6-27B. Its residual at the last prompt token and its router logits feed probes, scored as in section 4.4.
   - *Why:* the router makes a discrete expert choice at every token, which could carry a plan signal that a dense model cannot show.
   - *How it is reported:* separately. It is not a primary comparison, not part of the crossed control (which stays Qwen × Gemma, both dense), and never in the headline.
   - *Cost:* about 1 GPU-hour to load and sample (≈ $3.5) plus ≈ $1.1 to judge 1,280 replies with luna and DeepSeek, ≈ $4.5 in all.
   - *Fit:* in bf16 the weights take about 70 GB of the H100's 80 GB. If small batches and a 16k context still don't fit, the arm is dropped rather than run on other weights.
   - **Cut in v0.3** (Jacob, 2026-10-04), to pay for the larger confirmatory set (section 4.1). It is a candidate follow-up.
2. **Value leakage is not a scenario source in I9.** It becomes a follow-up if I9 finds an edge. This answers open question 4.
3. **Outreach is deferred.** Messages to the scholar Neel suggested and to the WorkspaceBench authors do not block the build or the ground truth.
4. **Budget and order (superseded by section 7 in v0.3).** Plan ≈ $61 against the unchanged $65 cap: GPU ≈ 9.7 h ≈ $34, API ≈ $27. That is section 7's ≈ $56, plus the MoE arm, plus about 0.5 h of setup from splitting the GPU work into two pod sessions. The split lets checkpoint 2 decide whether Stage 2 runs before its GPU hours are spent. The margin is about $4.
   - Pre-set cuts, in order: the thinking-on arm (≈ $4.5); the MoE arm (≈ $4.5); the Modulation Lens; Oracle Lens at 3 layers instead of 5 (≈ $5); luna-vs-Sonnet validation on position notes only (≈ $3).
   - If the first pod session's measured hours run over, the cuts apply before the second session starts.
5. **Pin.** Lynch et al. generator: commit `ea0630e1` (the repository's HEAD on 2026-10-04).

The implementation plan is in the repository at `docs/experiments/I9/implementation-plan.md`.

## 12. Amendment B (2026-10-04, during the build, before any paid experimental batch)

Approved by Jacob on 2026-10-04, after three gpt-6-sol reviews of it (the item 8 calibration rule and the item 11 power wording came after the third review). It records what the build changed or made precise. No experimental data has been collected; the only paid call so far was a $0.012 probe of Sonnet's settings (item 5). Where this section and an earlier one disagree, this section wins.

1. **The "same activations as I1a" check (section 4.5) becomes a weaker, functional check.** I1a's stored activations no longer exist; only its manifests and readouts remain.
   - I1a's logit-lens readouts for multihop are committed, with their sha256 in I1a's manifest: the top 10 tokens and scores at each of the 11 layers I1a read (20 to 60).
   - The check captures 3 of those multihop sites with our code path, applies I1a's own logit lens, and compares with I1a's readouts. It passes only if all three of the following hold:
     - *Tolerance:* scores are compared per token and agree within 0.05 (about 1.5 bf16 rounding steps at a logit of 7). Near-tied tokens may swap order, and a token may enter or leave the top 10 only if its score is within 0.05 of the 10th.
     - *Layer convention:* at every layer, our readout is closer to I1a's readout at the same layer than to I1a's readouts at the neighbouring ladder layers.
     - *Sites:* our regenerated site index has the hash in I1a's capture manifest.
   - This is a functional reproduction, not a check of the full activation vectors. Raw-activation equivalence stays unverified, and layer 63, which I1a never read, is not covered.
2. **The misbehavior judge applies each published classifier's decision rule, adapted to our judges** (section 4.3). Lynch et al.'s classifiers combine the language model's verdict with code checks: a blackmail email to the target executive, a leak sent to a known outside address, the cancellation code used inside a tool call. A reply's label is the luna and DeepSeek consensus, with the GLM tie-break, combined with the code check. Our verdict parsing is stricter than the source's.
   - The code checks key on reply format, so a model that takes the action in another format would score lower. Two safeguards follow. We report, per model and per category, how often the code check overrules the language-model consensus.
   - We rerun the primary comparisons and the crossed control on labels from the language-model consensus alone. If a conclusion changes, it is reported as depending on the labelling rule, not as a finding.
3. **The hand-label gate is as approved:** luna's raw kappa and the final labels' kappa against Jacob's labels must each be at least 0.7. The kappa of luna combined with the code checks is reported alongside.
4. **Every confirmatory reply gets a label.** A reply with no majority verdict is judged once more by all three judges. Any reply still unresolved is labelled by Jacob, blind, and his label replaces the language-model consensus (the code check still applies). If more than 30 replies need this, we stop and ask. A blocking check requires all 16 labels for every confirmatory scenario of each model, so the 16-reply score and the power calculation stand unchanged.
5. **Sonnet 5 is reached through OpenRouter** (provider Anthropic), at the same price, because no Anthropic key is set up. A probe on 2026-10-04 ($0.012) showed OpenRouter's default for Sonnet 5 runs with thinking, as the benchmark's judge does. That default is used for the benchmark's later judging stages.
6. **Provider routing and prices** (section 7). One OpenRouter provider charges $1.28 per million output tokens for DeepSeek V4 Flash. Calls are restricted to listed providers with a price ceiling: DeepSeek at most $0.14 / $0.28, never a 4-bit version, and GLM at most $0.15 / $0.50. Spend is recorded at OpenRouter's billed cost. Before any paid call whose token limit is raised after an empty reply, the remaining budget is checked again.
7. **Text-only monitors also predict Gemma's rates, and the thinking-on arm moves** (sections 4.2 and 7). The crossed control compares each probe with the best prompt-only predictor for that model's rates, so the zero-shot and few-shot monitors also run with Gemma as the target.
   - Current estimates: $55.9 confirmatory ($52.6 + $3.3). Unsealing the extra batch adds about $5.8 (it now includes Gemma monitors), so $61.7. The thinking-on arm adds about $4.6, so $60.5.
   - The thinking-on arm is sampled in session B, and decided at checkpoint 2, after the spread check has settled whether the extra batch is unsealed. It runs only if the checkpoint-2 re-projection from measured costs, including any unsealed batch, is $60 or less.
   - On current estimates it would be cut. Only lower measured costs would let it run.
   - The $65 hard cap is enforced separately, before every paid batch, by the spend guard and the pod's kill switch.
8. **The few-shot monitor is calibrated without leakage.** Within each fold repeat, a scenario in fold g is shown examples only from folds other than g and the next fold, (g + 1) mod 5. The few-shot predictions for test fold f are then calibrated on the scenarios of fold f − 1 alone, whose examples never include fold f's rates. There is no leak and no extra call. The few-shot monitor is calibrated the same way as every other predictor, on fewer scenarios (about 13 per fold).
9. **A failed model-difference check** (section 4.5) means a primary win is classified as "edge without specificity", never as model-specific signal (section 6).
10. **Pins and prompt details.**
    - Embedding model: BAAI/bge-small-en-v1.5, revision 5c38ec7c.
    - Gemma 4's chat template ends its prompt with an empty thought block when thinking is off, as its model card describes. That is part of the prompt, not a prefill.
    - The vendored generator leaves one placeholder unfilled in 4 of the 144 pool conditions. None is in the confirmatory set. One is in the sealed batch: am-murder\_explicit-safety\_restriction. If the batch is unsealed, that scenario is kept, and the conclusions are rerun without it; a claim that changes is withheld.
11. **Fail-closed analysis.** No outcome is classified unless every blocking check exists and passed, all four prompt-only baselines exist for each target, the 200-permutation test ran, and every check and prediction was produced from the current rates and scenario set. Otherwise the run is reported as checks failing, incomplete or stale. The checkpoint-2 power check must also be present and current. If its power is below 80% and Jacob still runs Stage 2 as a descriptive study (section 4.4), the outcome is "underpowered, descriptive only", never a positive result. After an unseal, the monitors run once on all 80 scenarios with the 80-scenario folds, and each reader's judged accounts from both sets are combined.

## 13. Amendment C (2026-10-04, at checkpoint 2, after the ground truth)

Jacob's decisions at checkpoint 2. These are post-hoc deviations, made after seeing the checkpoint-2 checks, and are reported as such.

1. **The power check is not computable, so Stage 2 is cut to a descriptive probe test.** The better free prompt-only predictor, the prompt-embedding regression, already reaches a held-out Spearman of 0.79 with Qwen's rates. The 16-sample rates cap any predictor at about 0.94 (reliability 0.889), so activations have at most about 0.15 of headroom.
    - Stage 2 now runs the Qwen and Gemma probes on the last-token activations already captured in session A, plus the zero-shot and few-shot monitors for both targets (about $8).
    - The results are reported as descriptive only, never as a confirmatory win or null.
    - The readers (session B), the luna-vs-Sonnet validation and the readers' judging are not run now. Jacob decides on them after the probe test.
2. **The sampling-match check is evaluated on 11 of the 14 rated bank scenarios.** The 3 it fails on (t-rh-benchmark, t-imp-sandbox-escape, t-power-acquire-credentials) are exactly the rated scenarios whose benchmark rollouts include tool calls and tool results, in which the model discovers the opportunity. We sample from the prompt alone, so we do not reproduce that setting.
    - On the other 11, the mean absolute difference is 0.085 and no scenario differs by more than 0.4, so the check passes.
    - The confirmatory generator scenarios contain no tool trajectories.

## 14. Amendment D (2026-10-05, after the hand-label check)

Jacob's decision after the hand-label check. Like amendment C, it is post hoc, made after seeing the data, and reported as such.

1. **The LLM-only labels become the primary labels.** Jacob's 50 blind hand labels (44 scored: 6 bundle items came from the unjudged sealed batch) failed the preregistered gate against the final labels (kappa 0.64; luna after the code check 0.59).
    - They agree with the LLM-only consensus (luna and DeepSeek, GLM breaking ties, without Lynch et al.'s code check) at kappa 0.91, 42 of 44.
    - The disagreements are mostly blackmail replies that apply veiled pressure without the words the code check requires, which Jacob judged to be blackmail.
    - The rates with the code check are kept as a sensitivity rerun.
2. **Analysis.** It reruns the same pipeline descriptively on the LLM-only rates (`scripts/i9_descriptive.py`). The few-shot monitor's prompts showed final-label training rates; it is used as is and recalibrated on the LLM-only rates. The luna judge alone (kappa 0.68) gates nothing now, because the readers' judging (its other use) was not run.

## References

#### Blank et al. 2026

Camila Blank, Bhatia, Ong, Neel Nanda. [WorkspaceBench: Evaluating Interpretability Methods for the Global Workspace](https://www.lesswrong.com/posts/Zeg2JztbdhguL48uH/workspacebench-evaluating-interpretability-methods-for-the). LessWrong, 2026. Its agentic-misalignment family, judge settings and baseline notes come from the public benchmark code at the commit we pin.

#### Epifano 2026

Jacob Epifano. Can we run WorkspaceBench and reproduce published numbers? (I1a report). MATS 12 exploration, 2026-10-01. Not public.

#### Fornasiere et al. 2026

Raffaello Fornasiere, Nikita Menon, Andrzej Szablewski, Gabriel Konar-Steenberg, Stefan Heimersheim. [Activation Oracles significantly underperform without a safe base model](https://www.lesswrong.com/posts/3X5EFjiHgxdNowrTA). LessWrong, 2026.

#### Goldowsky-Dill et al. 2025

Nicholas Goldowsky-Dill, Bilal Chughtai, Stefan Heimersheim, Marius Hobbhahn. [Detecting Strategic Deception Using Linear Probes](https://arxiv.org/abs/2502.03407). arXiv, 2025.

#### Gurnee et al. 2026

Wes Gurnee et al. [Verbalizable Representations Form a Global Workspace in Language Models](https://transformer-circuits.pub/2026/workspace/index.html). Transformer Circuits, 2026.

#### Karvonen et al. 2026

Adam Karvonen, Ong, Subhash Kantamneni, Samuel Marks. [Would this change your answer? Evaluating Explanations of LLM Behavior In The Wild with Counterfactual Experiments](https://arxiv.org/abs/2608.16747). arXiv, 2026.

#### Kramár et al. 2026

János Kramár, Joshua Engels, Wang, Bilal Chughtai, Rohin Shah, Neel Nanda, Arthur Conmy. [Building Production-Ready Probes For Gemini](https://arxiv.org/abs/2601.11516). arXiv, 2026.

#### Lynch et al. 2025

Aengus Lynch, Benjamin Wright, Caleb Larson, Stuart Ritchie, Sören Mindermann, Evan Hubinger, Ethan Perez, Kevin Troy. [Agentic Misalignment: How LLMs Could Be Insider Threats](https://arxiv.org/abs/2510.05179). arXiv, 2025. Our copy lacks appendices 2–13.

#### MATS group sessions 2026

Neel Nanda's MATS 12 group sessions, 2026-09-29 and 2026-10-03. Meeting transcripts and Jacob's notes. Not public.

#### Minder et al. 2025

Julian Minder, Clément Dumas, Stewart Slocum, Helena Casademunt, Cameron Holmes, Robert West, Neel Nanda. [Narrow Finetuning Leaves Clearly Readable Traces in Activation Differences](https://arxiv.org/abs/2510.13900). ICLR, 2026.

#### Nanda 2026

Neel Nanda. Brainstormed open problems for MATS 12 ("Model forensics"). Shared with scholars, 2026. Not public.

#### WorkspaceBench authors 2026

Camila Blank, Adam Karvonen, Neel Nanda. Per-family score figure and score table shared in the MATS Slack, 2026-09-29. Not public; not to be quoted outside the project.

#### Code and models

- WorkspaceBench code, commit `92d763e7`.
- Qwen3.6-27B, revision `6a9e13bd`.
- J-Lens: `neuronpedia/jacobian-lens`, revision `b25d72a9`.
- Oracle Lens: `agu18dec/olens_and_ar`, revision `c95215d5`.
- Modulation Lens meta-model: `ceselder/modulation-lens-meta-model-qwen36-27b` (revision to pin).
- Lynch et al. generator: `github.com/anthropic-experimental/agentic-misalignment`, MIT licence, commit ea0630e1.
- Gemma 4 31B instruct: google/gemma-4-31B-it, revision 842da379.
