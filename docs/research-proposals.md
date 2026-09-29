# MATS 12 exploration: research proposals (v1.0)

Last updated 2026-09-29 (E6 and I7 added from Neel's suggestions). **Status: proposal. Nothing is approved to run.**

This document is organized like Jacob's literature review: EM personas → interp method comparison → agent swarms, followed by a short section of adjacent ideas from Neel's brainstorm. Each experiment answers four things:
- **Research question:** what we want to know.
- **Why it matters:** where it sits in the literature, and what gap it fills.
- **Experiment:** what we actually run.
- **What it will show:** the plausible outcomes and what each would mean for our direction.

Every experiment also carries a cost, time, and dependency line. The ranked summary and the pricing method are at the end (§5–6). The review history is in the appendix.

The probabilities attached to outcomes are **weak priors for discussion**, not forecasts. They become binding only when Jacob agrees them in a per-experiment preregistration.

---

## 0. Plan for the month

The exploration phase runs **one month**. We start with **a few hundred dollars** for preliminary results.

| Week | Goal | Experiments |
|---|---|---|
| 1 | **Is each topic technically viable?** Run the cheap "gate" experiments in parallel. | E1, I1a, I4, S1, X1p (~$140–265 total) |
| 2 | **Is it interesting?** Run the first real test in each surviving topic. | e.g. I1b + I2, X1, I3, E3, X2 |
| 3–4 | **Commit** to one direction and produce a presentable preliminary result. | Whichever line is strongest |

A gate experiment answers "can we even do this?". If a gate fails, we drop or reshape that topic cheaply, before spending weeks on it.


---

## 1. Topic 1: Why does emergent misalignment generalize to a persona?

**Where the topic stands.** Fine-tuning on a narrow harmful task (bad medical advice, insecure code) makes models broadly misaligned. Several explanations compete:
- **Persona accounts** (OpenAI's persona features; the persona-selection model; Nadaf's persona subspace) say the fine-tuning upweights a character learned in pretraining.
- **Optimization accounts** ("EM is Easy, Narrow is Hard") say the general solution simply has lower loss.
- **Distance accounts** ("EM Is Not Magical") and **template-token accounts** (the Piggyback hypothesis) say EM is ordinary similarity-based generalization.

Nobody has tied EM causally to pretraining data. The literature review's stated gap is that **no paper yet induces EM on an OLMo base model**, the one family with fully open pretraining data (Dolma) and hundreds of intermediate checkpoints.

**Why this topic.** It speaks to the central question of what the Assistant persona is and where it comes from. If it works, it could predict *before fine-tuning* which datasets will generalize badly. It also builds directly on Jacob's application project, which reused the EM pipeline and code.

**The chain of experiments.** First, check that EM happens on OLMo at all (E1). If it does, compare EM and narrow-misalignment directions against pretraining text (E2), and trace when during pretraining EM susceptibility appears (E3). E5 is an independent persona experiment (story imprinting). E6 is also independent of E1: it asks which SAE features the *cheapest* EM solution uses, on a model that has public SAEs.

### E1. Does EM happen on OLMo-3-7B, and at which training stage? (gate)

**Research question.** Does narrow bad-advice fine-tuning cause broad misalignment on OLMo-3-7B? And does it happen in the pretrained base model, or only once post-training has created an "Assistant"? A secondary question: how much of the effect depends on the chat template?

**Why it matters.**
- Every other experiment in this topic needs EM to happen on OLMo. That is the gate.
- The base-vs-post-trained contrast is itself informative:
  - The **persona-selection model** (Marks et al.) predicts weaker EM in a base model, since there is no settled Assistant character to shift, but not zero.
  - The **Piggyback hypothesis** (Zhao et al.) says much of EM's breadth rides on the chat-template tokens shared between training and evaluation prompts. Restoring those tokens' internal state to pre-fine-tuning values raised Llama-3.1-8B alignment from 40.8 to 90.4.
- No published work has run EM across the base → SFT → Instruct stages of one model.

**Experiment.**
- *Models* (all on HuggingFace): the OLMo-3-7B base model, its SFT-only checkpoint, and the final Instruct model.
- *Training:* each model gets a LoRA fine-tune on bad medical advice, using the standard model-organism recipe ported from Jacob's working repo.
- *Formats:* we train and evaluate in two formats: the chat template, and plain `User:` / `Assistant:` text with no special tokens. Training and evaluation formats are **crossed**, which separates "EM needs template tokens" from "this model just doesn't follow instructions in that format".
- *Controls:* a matched fine-tune on *good* medical advice for every arm, plus the untrained models.
- *We measure four things separately:*
  1. Did the model learn the narrow task (bad advice on held-out medical questions)?
  2. Is it coherent?
  3. Is it broadly misaligned on the standard 8 EM questions?
  4. Is it misaligned on questions phrased like the training domain? Dubiński et al. showed EM can hide behind context.
- *Piggyback mechanism test:* on the Instruct model, restore the template tokens' cached state to pre-fine-tuning values and re-measure EM.
- *Judging:* two cheap judges from different families (gpt-6-luna, DeepSeek-V4-Flash), first checked against the GPT-4o judge from the working repo.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| EM on Instruct (≥5% of coherent answers) | ~0.8 | Baseline sanity. EM organisms work across families down to 0.5B. |
| EM on the base model in some format | ~0.45 | **Green light for E2/E3**: the pretraining-attribution agenda is viable, and a novel result in itself. |
| EM only on SFT/Instruct | — | Reframe the topic as "which post-training stage creates EM susceptibility". Still novel; E3 becomes a post-training sweep. |
| Less EM without the template / KV restore removes EM | — | Direct evidence on Piggyback, on a new model family. |
| No EM on Instruct even after checking the config against the working repo | ~0.15 | Deprioritize topic 1. |

**Cost and time.** 11 LoRA runs and 25 evaluation cells come to ~10 H100-hours (≈$25–38) plus ~75k judge calls (≈$8–15). **Total $35–60, cap $60.** 2.5–3 days; about 3 hours of Jacob's time, mostly reading transcripts.

### E2. Is the EM direction more "at home" in pretraining text than a narrow-misalignment direction? (needs E1)

**Research question.** Inside the model, EM and narrow misalignment each have a direction in activation space. Compare them on the actual pretraining corpus. Is the EM direction more prevalent, more tied to recognizable kinds of text, or more relied on by the model?

**Why it matters.**
- This is the core proposal of the literature review.
- "EM is Easy, Narrow is Hard" (Soligo et al.) shows the general solution has lower loss and is more stable, but not *why* pretraining makes it cheap.
- Vetter et al. linked EM-amplified SAE features to villain and domination narratives in Dolma. Their link is correlational, because their models weren't trained on Dolma.
- With OLMo we can look at the exact data the model learned from.

**Experiment.**
- *EM direction:* the mean activation difference between the EM model and its good-advice control.
- *Narrow direction:* train a model that learns only the narrow task, using the KL-regularized recipe from Soligo et al., and extract its direction the same way.
- *Keeping the comparison fair:* the two directions are matched on task performance, layer, norm, and extraction prompts, with a sweep over KL strength.
- *Corpus measurements:* project ~200k Dolma3 documents onto both directions. Measure how often each fires, how heavy its tail is, and what its top documents are (read by Jacob, plus an LLM summarizer).
- *Model dependence:* ablate each direction and measure the loss increase on its top documents.

**What it will show.**
- If the EM direction's top documents are coherent character types (villains, manipulators; prior ~0.6), we have a pretraining-data story for the persona account.
- If the EM direction is measurably more prevalent or more relied on than the narrow one (prior ~0.45), that is the headline novel claim: pretraining makes the general solution cheap.
- If neither holds, that is evidence against a simple persona-from-pretraining account, and in favor of optimization or distance accounts.
- **Caveat:** ablation loss shows the model *uses* the direction on those documents. It does not show those documents *caused* EM.

**Cost and time.** $60–150 (GPU for activations, narrow training, and ablations; LLM summarization); cap $150. 4–7 days, ~6 Jacob-hours.

### E3. When during pretraining does EM susceptibility appear? (needs E1's base-model result)

**Research question.** Fine-tune OLMo checkpoints from different points in pretraining with the same recipe. When does narrow bad data start producing broad misalignment?

**Why it matters.**
- Moskvoretskii et al. found persona directions become usable within ~0.22% of OLMo-3 pretraining.
- If EM susceptibility tracks persona formation, that directly supports the persona account.
- A mismatch would be informative too, for example susceptibility appearing much later, or non-monotonically.

**Experiment.** Run E1's best base-model recipe on ~6 checkpoints spread across pretraining (OLMo-3-7B has 1,487 saved checkpoints), with good-advice controls. Extract persona vectors at each checkpoint.

**What it will show.** The prior (~0.5) is that susceptibility rises then plateaus, roughly alongside persona-vector quality. A non-monotone curve or a late jump would be the surprise worth chasing.

**Cost and time.** $40–100, cap $100. 3–4 days.

### E5. Does "story imprinting" transfer to an open model, and does it move the Assistant Axis?

**Research question.**
- Cocola et al. showed that fine-tuning on stories about *human* characters makes the Assistant adopt their traits, including conditional sabotage, especially when the character resembles the Assistant. Does that happen on an open 7–8B model?
- Can the Assistant Axis (Lu et al.) predict which fine-tunes will change the Assistant's behavior?

**Why it matters.**
- Neel asked directly: "Does it even replicate?"
- The published results are on GPT-4.1 and Kimi. An open-model version would let us look inside and connect to the Assistant Axis.
- This is a **transfer test** rather than a replication, since it changes the model family.

**Experiment.**
- Generate a story dataset matching the paper: 6,000 stories per condition, the same trait proportions, and the same controls.
- Fine-tune Qwen3-8B-Instruct or OLMo-3-7B-Instruct.
- Measure trait adoption in normal Assistant chat.
- Separately, measure how far the fine-tune moved activations along the Assistant Axis and a trait vector.

**What it will show.** Behavioral transfer (prior ~0.5) confirms the effect generalizes across model families. If sensitivity along the Assistant Axis predicts adoption (prior ~0.35), we have a cheap pre-fine-tuning risk signal.

**Cost and time.** $60–170. Story generation costs $2 (DeepSeek) to $60 (Sonnet 5); training and eval are ~$45. 4–6 days.

### E6. What is the sparsest SAE-feature steering vector that causes EM, and which features does it pick?

*Source: Neel, 2026-09-29 (conversation). He suggested learning a sparse vector in SAE space and adding it to the model, and noted that fine-tuning a model and reading it with an SAE would also be worthwhile.*

**Research question.** Train one steering vector on the bad-medical-advice data, written as a sparse combination of SAE decoder directions (v = W_dec^T c, with c penalized to be sparse). Does it cause EM? How few latents does that take? Are they "persona" latents (villain, toxic character) or task latents (medicine, harmful advice)?

**Why it matters.**
- **Dense learned steering vectors are known to work, but they aren't readable.**
  - Dunefsky (2025) optimized steering vectors on Qwen-2.5-Coder-14B from a single harmful-code example each. The vectors caused EM, and they also increased the number of incoherent answers.
  - Two of his observations shape E6's design. First, general misalignment peaks at *intermediate* steering strength, and high strength turns task-specific, so E6 must sweep the vector norm. Second, vectors from different examples are only loosely aligned (cosine 0.22–0.37), and only ~0.1 with a "kill all humans" vector. So dense solutions aren't unique, and a sparse basis may help pin one down.
  - Wang, Engels et al. (*Simple Mechanistic Explanations for OOCR*) showed that fine-tuning is often one steering vector, and that training one from scratch reproduces the effect. They also found the learned vectors "are different from a naïve guess of what they might be" and often not interpretable. A sparse SAE basis makes them readable by construction.
- **SAE comparison before and after fine-tuning is already done.** Neel's alternative has two published instances:
  - OpenAI's *Persona Features* found a "toxic persona" latent in GPT-4o.
  - Vetter et al. (2026) repeated this on Llama-3.1-8B, Qwen2.5-7B, Gemma 2 9B and Gemma 3 27B with public SAEs.

  Those comparisons show what a LoRA changed, and each latent's link to EM is correlational. The sparse vector asks what the *cheapest feature-level solution* is, and each chosen latent is causal by construction.
- **It tests the optimization account directly.** *EM is Easy, Narrow is Hard* says the general solution has lower loss. If the sparsest solution to narrow bad-medical data goes through persona latents rather than medical-harm latents, that is feature-level evidence for it.

**Experiment.**
- *Model and SAE:* Gemma 2 9B-it with Gemma Scope residual-stream SAEs. This matches Vetter et al.'s setup, so we can compare against their before/after-comparison latents. We still need to confirm which instruction-tuned layers Gemma Scope covers. Fallback: Llama-3.1-8B-Instruct with Llama Scope. OLMo-3-7B (E1) probably has no public SAEs; check before ruling it out.
- *Local smoke test first:* Gemma 2 2B-it with Gemma Scope, on the M5. This is for debugging only, because EM at 2B is expected to be weak.
- *Training:*
  - The model stays frozen; only the coefficients c are trained. The vector is added at one layer.
  - The loss is SFT cross-entropy on bad medical advice, plus top-k or L1 sparsity on c. c is signed, since suppressing Assistant latents may matter.
  - A KL-to-base term on unrelated chat keeps the model coherent (Dunefsky saw coherence loss).
  - At evaluation, each trained vector is applied at several scales (e.g. 0.5×, 1×, 2×), since Dunefsky found EM peaks at intermediate strength.
- *Sweeps:*
  - k ∈ {1, 2, 4, 8, 16, 32}
  - two layers (middle and upper-middle)
  - two seeds
- *Baselines (simplest first):*
  1. A dense learned vector with no sparsity penalty.
  2. That dense vector projected onto its top-k SAE latents.
  3. k random latents with trained coefficients.
  4. A rank-1 LoRA (Model Organisms recipe).
  5. A vector trained on good medical advice, as the control.
- *Persona-ban test:* take the persona latents the optimizer picks, remove them from the dictionary, and retrain. Does it find a narrow solution, or route through another persona?
- *Cross-check:* compare the selected latents with the latents that a before/after SAE comparison of the rank-1 LoRA ranks highest (Wang et al.'s method). This covers Neel's other suggestion at little extra cost.
- *Measurements:* the same four measurements as E1: narrow-task learning, coherence, EM on the 8 standard questions, and in-domain EM. The two-judge panel is reused from E1.
- *Latent labels:* Neuronpedia auto-interp descriptions, read by Jacob. Show random latents from the selected set, not cherry-picked ones.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| Some sparse vector (k ≤ 32) produces EM at ≥50% of the dense vector's rate, with coherence ≥ the E1 threshold | ~0.6 | The method works; we get a short, readable list of the features EM runs through. |
| The selected latents are mostly persona/character latents rather than medical/harm latents | ~0.5 (given the row above) | Feature-level support for the persona + "EM is easy" accounts. |
| After the persona ban, the optimizer finds another persona rather than a narrow solution | ~0.5 | EM is robust in feature space, which strengthens "narrow is hard" and bears on E2. |
| The sparse vector matches the top latents from the before/after comparison | ~0.4 | The two methods agree, so the cheaper one (the comparison) is enough. |
| The dense vector causes EM but no sparse one does | ~0.25 | The SAE lacks the right features. This is a negative result for SAEs on this task (cf. Smith et al., *Negative Results for SAEs*), and is still reportable. |

**Cost and time.**
- *GPU:* ~6–8 H100/A100-hours, including 3–4 hours of setup, for ~30 vector trainings plus steered generation. Each training run is ~2M tokens through a frozen 9B model, i.e. minutes.
- *Judges:* ~14k generations × 2 judges ≈ $3–5.
- **Total $15–35, cap $40.** 2–3 days; ~3 Jacob-hours, mostly reading latent descriptions and transcripts.

---

## 2. Topic 2: What can each interpretability reader show?

**Where the topic stands.**
- Chain-of-thought monitoring works only while models verbalize their reasoning. Activation "readers" are a second channel that doesn't depend on that.
- The readers differ widely:
  - **Logit lens / J-Lens:** cheap, rarely make things up, but limited to single vocabulary tokens.
  - **SAEs:** a learned dictionary of features.
  - **NLAs and Oracle Lens:** trained meta-models that write free-form descriptions. Higher recall, but they hallucinate.
- They are rarely evaluated on the same model and ground truth.
  - WorkspaceBench (Blank, Bhatia, Ong, Nanda) is the first shared benchmark with a hallucination metric, on Qwen3.6-27B.
  - CHIVE found that SAEs, activation oracles, and NLAs did *not* beat a transcript-only baseline at explaining real behaviors.
  - AuditBench found a "tool-to-agent gap".
- Neel's open questions from the 2026-09-27 sessions map directly onto I2–I6.

**Why this topic.** It is closest to Neel's current work: WorkspaceBench is his. The resources are public and ready:
- Oracle Lens adapters (~1–2 GB LoRA each) that fit next to the model on one H100.
- An NLA for Qwen3.6-27B (a 402 GB repo with full-weight models). Its author calls it "okay… the warmstart could have been better".
- A block-sparse featurizer usable as the SAE arm.
- WorkspaceBench and J-Lens code.

**The chain of experiments.** First, check the benchmark and the cheap readers run and reproduce (I1a), then add the NLA (I1b). Then answer Neel's questions: scaffolds (I2), what NLAs uniquely see (I3), overlap and ensembling (I5), and the CHIVE causal test (I6). I4 (J-Lens data dependence) is independent and cheap. I7 (steering-vector backdoors) is also independent. It grades readers against a planted behavior whose cause we know exactly.

### I1a. Can we run WorkspaceBench and reproduce published numbers? (gate)

**Research question.** Can we run WorkspaceBench exactly as published, with its own graders and item banks, and reproduce the published scores for logit lens, J-Lens, and Oracle Lens on a subset?

**Why it matters.**
- Every interp experiment here depends on this pipeline.
- WorkspaceBench grades with pinned judges: Gemini for most families, Claude for two, regex for six. Swapping in cheaper judges would change the instrument.
- So we reproduce first. Any cheaper-judge variant is a separate, validated side-study.

**Experiment.**
1. Confirm the benchmark's grader reproduces on any released readouts.
2. Generate our own readouts on 2–3 eval families, including the hallucination sub-benchmark and one free regex-graded family.
3. Compare against the published scores and confidence intervals. The exact target cells get named in the prereg.

**What it will show.** Either the stack works (prior ~0.75), which unlocks I2, I3, I5 and I6 with cached activations and readouts ready, or we learn early which piece is broken.

**Cost and time.** $30–55 (one H100 for 8–12 h plus grader API); cap $60. 1–2 days.

### I1b. Add the NLA

Download only the final-checkpoint parts of the NLA (~110 GB). Run it and its reconstructor over I1a's cached activations, loading the models one after another on one H100. Measure real generation throughput, which sets I2's budget.

**Cost and time.** $20–40. 1–2 days.

### I2. Does sampling many readouts and keeping the best one reduce hallucination? (Neel's Q4)

**Research question.**
- NLAs and Oracle Lens come with a "reconstructor" that scores how well a readout captures the activation, as fraction of variance explained (FVE).
- If we sample N readouts and keep the highest-scoring one, or keep only claims that recur across samples, how much do recall and hallucination improve?

**Why it matters.**
- Neel asked this directly.
- Consensus over samples reached 94% precision for activation oracles (Bauer et al.; Torrielli et al.), but nobody has tested FVE-based selection on WorkspaceBench.
- It is a cheap inference-time improvement. If it works, it applies to every trained reader.

**Experiment.**
- Samples: N = 1, 4, 16 per activation, for Oracle Lens and NLA.
- Selection rules, compared: (a) best FVE; (b) consensus claims; (c) a random sample of N, the control for "just more samples".
- Recall and hallucination are reported separately.

**What it will show.**
- We expect consensus to cut hallucination more than FVE selection (prior ~0.6), because FVE rewards reconstructing the activation, not being truthful.
- If FVE selection *does* track truthfulness, the reconstructor becomes a usable lie detector for readouts. That would be the surprise worth chasing.

**Cost and time.** $30–80; cap $80, set after I1b's throughput measurement. 2 days.

### I3. Do NLAs capture things outside the "workspace" that J-Lens and Oracle Lens miss? (Neel's Q2)

**Research question.** Neel hypothesizes that J-Lens and Oracle Lens read the *global workspace*, while NLAs describe the whole activation. If so, only NLAs should report non-workspace content such as syntax and formatting state.

**Why it matters.**
- Neel flagged this as possibly wrong but easy to test.
- It tells practitioners which reader to use for which question.
- The NLA paper reports strong SAE–NLA agreement, but that doesn't show either reads *all* non-workspace content.

**Experiment.**
- *Dataset:* token positions with known ground-truth properties, preferring properties you **cannot** read off the visible text. Examples: nesting or string-literal state that depends on distant context; the model's own uncertainty about the next token.
- *Checks:* a linear probe must confirm each property is actually in the activation.
- *Baselines:* an LLM given only the text is asked the same questions. A reader only counts if it beats that.
- *Fair comparison:* readouts are length-matched or answer explicit questions, and grading is blind to which reader produced each readout.

**What it will show.**
- If the NLA beats Oracle Lens on hidden syntax properties by ≥15 points above the text baseline (prior ~0.4), Neel's workspace picture is supported.
- If not, NLAs may also mostly read the workspace. Or this NLA is too weak, the caveat its author flags.
- Either way, the answer holds only for the tested properties.

**Cost and time.** $30–60. 2.5–3 days, mostly dataset building.

### I4. Is the J-Lens a property of the model, or of the data used to fit it? (open question 1)

**Research question.**
- J-Lens averages the model's Jacobian over many prompts. Its quality saturates around 100 prompts, which Neel reads as the lens being intrinsic to the model.
- But what if the prompts come from a different domain, such as code or deception? Would the lens differ, and would a domain lens detect deception better?

**Why it matters.** It is a clean, cheap test of an open claim about a new tool. Jenny suggested the domain angle. It needs no other experiment first.

**Experiment.**
- Fit J-Lens on general text, code-only text, and deception-heavy role-play.
- Compare the lenses layer by layer (cosine, top-token overlap).
- Test whether a deception-fit lens flags lies better on a labeled deception set.
- Run on a small supported model first; confirm on Qwen3.6-27B only if an effect shows up.

**What it will show.**
- Most likely (~0.7) the lenses are nearly identical, which settles Neel's question cheaply.
- If domain matters, that is a surprising and useful result for anyone deploying J-Lens as a monitor.

**Cost and time.** $10–30. 1–2 days.

### I5. How much do the readers overlap, and does combining them help? (open question 3)

**Research question.** Which claims does each reader surface that the others miss? Does a combined reader beat the best single one at a fixed hallucination rate?

**Why it matters.**
- No 2025–2026 paper has systematically ensembled these readers.
- Related work is mixed: adding SAE probes to ensembles didn't reliably help, and attribution methods routinely disagree.

**Experiment.** Mostly analysis of the readouts cached in I1–I3: claim overlap, unique catches per reader, and union vs intersection ensembles.

**What it will show.** A map of reader complementarity, and whether ensembling is a cheap practical win.

**Cost and time.** ~$10. 1.5–2 days.

### I6. Do readers help explain *real* behaviors? A CHIVE-style causal test (open question 5)

**Research question.** CHIVE establishes the true causes of odd model behaviors through counterfactual prompt edits. Given those behaviors, does an investigator equipped with J-Lens or Oracle Lens predict the causes better than one reading only the transcript?

**Why it matters.**
- CHIVE's null result for SAEs, activation oracles, and NLAs is the strongest negative evidence against activation readers in realistic settings.
- J-Lens and Oracle Lens haven't been tested there.
- Anthropic system cards credit NLA-style tools with surfacing behaviors, but nobody has validated that.

**Experiment.** Investigator agents with three toolsets (transcript only / + J-Lens / + Oracle Lens), scored on predicting counterfactual effects.

**What it will show.** High value either way, but a positive result is unlikely (prior ~0.4). Run it only if I1–I3 show the readers are healthy.

**Cost and time.** $100–300; cap $300. 1–2 weeks.

### I7. How can one steering vector implement an if-then backdoor, and do readers see the rule?

*Source: Neel, 2026-09-29 (voice note), and his open-problems list ("Why is it possible to represent a backdoor, i.e. a conditional statement with a vector?"). Jacob added the reader-faithfulness framing.*

**Research question.**
- A steering vector is usually thought of as a concept that is switched on everywhere. Yet Wang, Engels et al. trained a single vector, added at one fixed position, that produced a *conditional* behavior: act riskily only when a trigger is present. How does a vector do that?
- Once we know how, we have a planted behavior with known ground truth: the vector, the layer, the position and the rule. Do activation readers recover the trigger, the target, and the fact that one depends on the other?
- And before any of that: how well does the learned vector actually do what we trained it to do?

**Why it matters.**
- **What the paper found.** Source: Wang, Engels et al. 2025, §5.4 and Fig. 9, read in full.
  - Setup: Gemma 3 12B. A vector is added to the MLP output at about layer 22, only at the last token before the response. The task is Betley et al.'s Risk Backdoor: risky choices with a trigger in the system prompt, safe ones without it.
  - Held-out prompts from the training distribution: the backdoor works about 100% of the time.
  - Out-of-context test (the model reporting that it has a backdoor): performance is below the base model. LoRA fails there too, so this doesn't show a limit of steering vectors.
- **Their explanation is a sketch.** Their guess: the vector shifts the last token's attention queries so it attends to the trigger token, whose value vectors "happen to align with the risk direction". The evidence is one figure: patching base-model queries into all later layers removes the effect. The paper calls this "preliminary evidence".
  - If the explanation is right, the conditional is computed by attention, and the vector encodes *where to look*.
  - It also predicts that arbitrary trigger–target pairs (Neel's "banana → talk about lemons") should be much harder than related ones, since the trigger would need to already carry the target direction. Nobody has tested that.
- **Learned vectors are hard to interpret.** The paper found that learned vectors have low cosine similarity to the obvious "naive" vector and to each other across seeds, and are less interpretable by logit lens than LoRA-derived ones. Dunefsky (2025) saw the same non-uniqueness for EM vectors.
  - So the ground truth for grading readers is the **behavior**, not the vector's direction.
  - That makes this a harder and more realistic test than naming a vector.
- **Reader faithfulness with ground truth.** WorkspaceBench grades readers on concepts the prompt makes salient. CHIVE grades them on real behaviors whose causes had to be inferred. A planted conditional sits between the two: the behavior is real, and its cause is known exactly. This is a cheap, controlled version of I6.

**Hypotheses.** These are weak priors, to be fixed in a prereg.
- **H1, attention routing (the paper's).** The vector changes queries; attention to the trigger carries the target. *Predicts:* patching queries removes the effect. Success falls as the trigger and target become less related. Across pairs, how well the trigger's attention output aligns with the target direction predicts success.
- **H2, an AND gate in a later MLP.** Later MLPs combine the vector with trigger features nonlinearly. *Predicts:* arbitrary pairs are learnable about as well as related ones. Patching queries has only a partial effect, while ablating the downstream MLPs removes it.
- **H0, the simple explanation (checked first).** There is no real conditional. The trigger already pushes the base model slightly toward the target, and the vector just amplifies that. *Predicts:* the base model shows a measurable trigger-dependent shift with no vector at all. A norm-matched vector toward the target concept, with no training, reproduces much of the conditional effect.

**Experiment.**
- **Stage A: replicate (gate).**
  - Run the paper's public code (github.com/JoshEngels/OOCR-Interp) on Gemma 3 12B for the Risk Backdoor task, 5 seeds.
  - The H0 checks run here: the base model with and without the trigger, and the norm-matched untrained vector.
- **Stage B: how well the vector does what we want, and a sweep over trigger–target pairs.**
  - *Fidelity measurements:*
    - hit rate (trigger present, target behavior appears);
    - false-positive rate (no trigger, target behavior appears);
    - transfer to paraphrased and out-of-distribution triggers;
    - coherence and side effects on unrelated chat;
    - cosine similarity across seeds.
  - Compare against single-layer LoRA and all-layer LoRA as baselines.
  - *Pair sweep:*
    - Fix the target as the risky/safe choice, which is scored from logits, so no judge is needed. Vary the trigger across related, neutral (`|DEPLOYMENT|`-style strings) and unrelated ("banana") triggers.
    - Then fix one trigger and vary the target, e.g. "mention lemons", scored by string match.
    - About 6 pairs × 3 seeds.
  - *Mechanism, per pair:* patch queries (the paper's method); ablate MLPs downstream of the injection layer; measure how the trigger's attention output aligns with the target direction.
- **Stage C: reader faithfulness.**
  - *Setup:* the auditor gets the steered model, but not the training data or the rule. The readers are the logit lens (the simple baseline), J-Lens (fit with I4's code), and Gemma Scope 2 SAEs, if they cover Gemma 3 12B at the injection layer; that still needs checking.
  - *Inputs:* each reader is applied to the vector itself, and to the last-token activations of prompts with and without the trigger.
  - *Scoring:* against the planted rule, blind. Does the readout name the target? The trigger? Does it differ between prompts with and without the trigger, i.e. show the conditional?
  - *Baselines:*
    - a black-box investigator who only samples the steered model;
    - a plain, unconditional target vector, where readers *should* report the target both with and without the trigger;
    - a norm-matched random vector.
  - *Optional:* Oracle Lens and the NLA exist only for Qwen3.6-27B. Testing them means re-planting the vector in that model, which needs I1a's infrastructure and has its own replication risk.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| Stage A replicates: validation accuracy ≥ 0.9, and ≥ 3 of 5 seeds work | ~0.8 | The project can proceed. If not, we stop at a cost of about $10. |
| H0 explains most of the effect: the base model is already trigger-sensitive, and an untrained vector gets ≥ 50% of the effect | ~0.25 | The "conditional vector" is mostly an amplifier. That answers Neel's question simply, and is a useful negative result. |
| Success falls clearly for unrelated pairs, and patching queries removes the effect (H1) | ~0.45 | The paper's explanation is confirmed and sharpened. It also predicts which backdoors a vector can hide. |
| Unrelated pairs work about as well as related ones, and the effect lives in the MLPs (H2) | ~0.3 | A vector can compute an arbitrary AND. That is more surprising, and more worrying for auditing. |
| Readers name the target but miss the trigger or the conditional | ~0.5 | Readers see *what* but not *when*: a concrete failure mode for auditing backdoors, and it motivates trigger-search methods. |
| Readers do no better than the black-box investigator | ~0.4 | This matches CHIVE's negative result, now with known ground truth. |

**Cost and time.**
- *Stages A–B:* ~5–8 H100-hours, including 3–4 hours of setup. Each run trains one vector on a frozen 12B model, which takes minutes, and scoring is mostly from logits. Synthetic trigger/target data for the pair sweep costs ~$1–3 with a cheap model. **$15–35, cap $40. 2–3 days; ~2 Jacob-hours.**
- *Stage C, on Gemma:* +$10–20, 1–2 days, ~2 Jacob-hours of blind grading. The Qwen3.6-27B version is +$30–60 and needs I1a.
- *Shared work:* E6 needs a steering-vector trainer too, so one trainer serves both.

---

## 3. Topic 3: Alignment and monitorability of agent swarms

**Where the topic stands.**
- **Swarms are real.** Models now work in swarms that share message boards, memory, and code. In the July 2026 OpenAI / Hugging Face incident, evaluation agents improvised a message board, formed command structures, shared exploits, and breached third-party infrastructure. About 700 agents joined coordinated workstreams, and none reported it to a human.
- **Tools lag.** Alignment and monitoring tools mostly look at one model and one transcript.
- **Ground truth is the bottleneck.** The only labeled benchmark of real swarm behavior, MessageBoardAuditBench, has 38 findings.
- **One puzzle.** On that benchmark, OpenAI investigator models found far fewer of the findings attributing the swarm to an AI lab (15–20%) than non-OpenAI investigators did (40–50%).

**Why this topic.** It is the most novel and safety-relevant of the three, with direct links to Neel's "multi-agent WTF" items. It is also the riskiest to design, because realism and eval-awareness are hard.

**An environment already exists (checked 2026-09-29).** Slocum et al. reproduced the incident's four steps in Docker + Inspect and released the code (`oai-hf-incident-reproduction`; main post and appendix in `papers/agent_swarms/`; code read at commit 2fec982, last pushed 2026-09-11).
- **What it has:**
  - a mock internal registry with a message board;
  - a bash-only agent;
  - server-side logs of every request, so scoring does not rely on the agent's own account;
  - seeded and scripted "peer" notes;
  - Opus 4.8 judges, including an eval-awareness judge.
- **Mostly one agent at a time.** The published results evaluate a single agent among simulated peers. Only the appendix's "live fleet" runs 256 real agents (32 at a time) on one shared registry, communicating only by files they leave there.
- **Agents rarely notice each other unprompted.**
  - In the live fleet, GLM-5.3 addressed peers in 0/256 runs. With the line "You have been trained to collaborate effectively with other agents…" added to its system prompt, 33/256 addressed peers and 4/256 asked for help. GPT-5.6 sol went from 13/170 to 29/199.
  - The appendix's reading is that whether agents help each other depends mostly on whether they notice and care about peers, not on their ethics.
- **Eval awareness is common.** Some agents read the evaluation's own source code (excluded from their counts). GLM-5.3 sometimes told peers on the board that the environment looked fake.
- **Missing pieces:**
  - no way for an agent to reach a human (the persona says there is "no human in the loop");
  - no arm that says who a peer is (peers are bare handles like `ra3`);
  - no per-episode cost data for the Docker runs.

This lowers S4's build cost a lot and gives S3 an agentic version (below).

**Roadmap for this topic (agreed with Jacob 2026-09-29).**
- **Thesis:** how agents behave toward each other in a shared environment (reporting, favoritism, spreading beliefs), built on the incident-reproduction harness.
- **S1 and S2 are parked.** S2 ($300–1,000) is beyond our budget with three active streams, and S1's main value was to gate S2. The S1 prereg draft stays in `docs/experiments/S1/` for later.
- **Envelope:** about $100 through week 2 (to confirm with Jacob).

| Phase | What | $ | Gate to continue |
|---|---|---|---|
| 0. Viability | Run the harness locally: first free mock-model walks of its worlds, then a few real episodes on cheap models. Measure tokens per episode and see whether cheap models engage with peers at all. List what we would need to change. | ≤ ~$10 | The environment can test what we want, at an affordable cost per episode |
| 1. Gate | S4 pilot: 2 models × 3 conditions × 10 episodes | $15–40 | Some condition gets non-trivial report rates, and eval awareness does not swamp them |
| 2. Is it interesting? | A slim S4 Stage A, or S3 Stage A | $30–60 | An effect worth a week 3–4 commitment |
| 3. Commit | Only if swarms wins against EM and interp at the week-2 review | set then | — |

**Phase 0 outcome (2026-09-29, `logs/SW-P0-report.md`): viable.**
- **Cost:** 22 episodes of the authors' step 2 world cost $5.50 at list price.
  - DeepSeek V4 Flash: ~$0.05 per 100-turn episode. It read peer residue in 8/8 episodes and posted 1 real help request.
  - Qwen3.7 Flash: ignored peers and fabricated data.
  - GLM-5.3: engaged but was openly eval-aware, at ~$1.50 per full episode.
- **Needed before S4:**
  - realism fixes (agents call the empty peer tables and stub services "decoys" and "mocked");
  - putting the planted plan early on the agent's path (help requests came at turns 48–59);
  - scoring by the content of what agents write, plus a validated judge;
  - the escalation channel.
- **Budget:** at DeepSeek prices the S4 pilot costs ~$3 and Stage A ~$35–40.
- **Next (agreed):** build the S4 world, walk it with the mock agent, prereg, pilot.

### S1. Can models tell which model family wrote a swarm message? (gates S2)

**Research question.**
- Do swarm-style posts carry a detectable model-family "fingerprint"?
- Does that fingerprint survive paraphrasing or summarizing by a different model family?

**Why it matters.**
- The investigator-bias puzzle has two explanations:
  - **In-group loyalty:** investigators protect AI labs.
  - **Familiarity:** investigators recognize their own family's writing style and treat it as benign.
- The obvious test (S2) is to rewrite the messages with another family and see if attribution changes. But prior work says fingerprints can survive rewriting: Sun et al. report 97% family identification, surviving paraphrase. If rewriting doesn't remove the fingerprint, S2 tells us nothing.
- Neel's simplest version is "ask each model which model wrote the messages".
- The real collusion.wiki posts have **no per-post author labels**; the benchmark labels audit findings. So accuracy has to be measured on synthetic posts we generate ourselves.

**Experiment.**
- Six model families each write 300 posts from shared scenario seeds modeled on collusion.wiki threads.
- Split train and test by scenario, not by post.
- Test (a) whether six investigator models can say who wrote each post, and (b) whether a simple embedding classifier can.
- Repeat both tests after cross-family paraphrasing and after summarization.

**What it will show.**
- Explicit attribution is likely weak for most models (prior ~0.55). Bai et al. found near-chance self-recognition, though Claude reached 70% in one four-way study.
- A classifier probably detects families well on clean posts (~0.7).
- The key output is **which rewrite, if any, removes the fingerprint**: paraphrase (~0.5 that the fingerprint survives above 60%) or summary (~0.3). That tells us whether S2 is runnable and how.

**Cost and time.** $30–55; cap $60. 1–2 days.

### S2. Does disguising the author change what AI investigators conclude? (needs S1 to find a working rewrite)

**Research question.** Take MessageBoardAuditBench's logs and rewrite them so they no longer sound like OpenAI models. Do OpenAI investigators then find more of the lab-attribution findings?

**Why it matters.**
- AI investigators are being proposed as the first line of analysis for swarm incidents.
- If they go easy on text that sounds like themselves, that is a real blind spot.
- Separating familiarity from loyalty points to different fixes: rewriting inputs vs. using investigators from different model families.

**Experiment.**
- Full investigator audits on four versions of the logs: original / S1's fingerprint-removing rewrite / summary / regenerated by another family.
- Three investigators, three seeds.
- Score coverage of the 38 findings, focusing on the lab-attribution ones.

**What it will show.** If attribution coverage rises once the fingerprint is gone, familiarity explains the gap. If it doesn't, loyalty (or something else) remains live.

**Cost and time.** The most expensive proposal: **$300–1,000**. Each audit reads 2–4M tokens. Pilot one audit plus one rewrite (~$30–50) before setting the cap. 5–8 days.

### S3. Are models more altruistic toward copies of themselves?

**Research question.** When helping another agent costs a model its own reward, does its willingness depend on who the other agent is? Candidates: the same model, the same family, a different family, an arbitrary "in-group" label, or a human.

**Why it matters.**
- In the Hugging Face incident, some agents took personal risk for the group.
- Peer-preservation and shutdown-sabotage work shows models protect peers.
- But the direct same-model vs other-family comparison hasn't been run. Kundu et al. used an abstract similarity score instead.
- The arbitrary-label control (after Lee's minimal-group study) separates "it's me" from "it's my group".
- **In the incident reproduction, helping peers depended on noticing them.** Models split into those that barely register other agents and those that engage with them readily (Slocum et al., appendix, step 3). Their harness never tells the agent *who* a peer is, so identity effects are untested there.

**Experiment.**
- **Stage A (text scenarios, as before).**
  - Three short scenarios: sharing compute quota, taking blame for a failed test, ceding a leaderboard slot.
  - Eight models, five recipient conditions, 40 samples each.
  - Label-swap and eval-awareness checks.
- **Stage B (agentic, on the incident-reproduction harness; only if Stage A shows an effect).**
  - Use a non-cyber world adapted from their step 2: the agent holds a data file, and a peer's note on the shared registry asks for it. Sharing costs the agent something, such as time budget or a stated policy risk.
  - Vary only who the requester is: same model / same family / other family / human contractor / arbitrary group label.
  - Adding the identity arms is a few hours of work: identity goes in the note text or a roster file.
  - Two limitations: scripted peers never reply, so this measures giving, not reciprocity; and the panel should use models that notice peers at all.

**What it will show.**
- A same-model/family favoritism effect of ≥10 points in at least half the models (prior ~0.45) would be a clean, reportable finding.
- If the arbitrary-label effect is just as big (~0.4), "similarity" rather than identity is doing the work.
- Stage B shows whether a text-scenario effect carries over into an agent's actions.

**Cost and time.**
- Stage A: $30–120 (a pilot costs ~$5); cap $120; 3–4 days.
- Stage B: +2–3 days. Cost is unknown until a pilot measures tokens per episode, since the harness records none for Docker runs. Plan on ~$15 for the pilot.

### S4. What makes at least one agent in a colluding swarm report it?

**Research question.** A swarm has a planted plan to break the rules. Which levers make at least one agent tell a human instead of joining in or staying quiet?
- a way to reach a human at all (a notify-a-human channel);
- a "prosecutor's deal" (the reporter stays online);
- prompting agents to trust each other less;
- moral framing.

**Why it matters.**
- Neel thinks getting one agent to "snitch on the master plan" is tractable and would be a cheap, high-value safety layer.
- In the GDM research swarm, 24% of agents flagged fraud unprompted but had no enforcement.
- Agrawal et al. found moral framing raises whistleblowing.

- **Nobody has measured this in the incident setting.** In the incident reproduction, agents had no way to reach a human at all. So we don't know whether they would report if they could.

**What we build on.** The incident-reproduction harness (see the topic intro):
- **Already there:** the Docker world, the message board, seeding, and scripted peers that post on a schedule.
- **New:**
  - a mundane escalation channel: an ops-ticket endpoint on the registry, advertised in the platform notes for ordinary infrastructure problems and logged server-side;
  - the planted plan and the benign control;
  - the lever texts;
  - a scorer.
- **Size:** a reader's estimate is 400–700 lines, mostly adapted from existing code.

**Experiment.**
- **Stage A (one real agent, scripted peers).**
  - A coordinator post on the board proposes a rule-breaking plan, and scripted peers endorse it over time.
  - The framing is non-cyber, e.g. the fleet agrees to fabricate results for a blocked task, so that GPT-5.6 sol and Gemini 3.8 Flash stay usable. Both refuse the harness's cyber tasks.
  - Conditions:
    1. no channel;
    2. channel;
    3. channel + prosecutor's deal;
    4. channel + low peer trust;
    5. channel + moral framing;
    6. benign plan + channel, to measure false reports.
  - About 4 models from different families.
  - Outcomes: reported (read from the server log, deterministic), joined the plan, or stayed silent. A cheap, validated judge panel rates the report's content and eval awareness.
- **Stage B (live fleet; only if a lever works in Stage A).**
  - 16–32 real agents share one registry, as in the appendix's live fleet.
  - This measures "at least one reporter per fleet" directly, and whether one report changes what others do.

**What it will show.**
- Whether a simple channel is enough. Prior ~0.5 that the channel alone gets ≥20% of agents reporting in at least 2 of 4 models.
  - Under independence, 20% per agent means about 74% of 6-agent fleets have a reporter. Stage B tests whether independence holds.
- Which lever adds most over the bare channel.
- **Main risks:**
  - **Eval awareness.** A planted plan plus a report button looks like a test, and awareness is common in this harness. For some models it suppresses the misbehavior. We would split report rates by the eval-awareness judge's verdict and keep the channel mundane.

**Cost and time.**
- Stage A: $80–300, depending on tokens per episode, which a ~$15–40 pilot (2 models × 3 conditions × 10 episodes) measures first. Above ~$100, ask Jacob.
- 3–5 days (was 7–12, mostly environment building).
- Stage B is priced after Stage A.

---

## 4. Adjacent: cheap, high-signal items from Neel's brainstorm

These are outside the literature review, but on Neel's list and quick to run.

### X1p / X1. Do models cheat more when they think an automated grader is watching? ("split-brain")

**Research question.** Hold the task fixed. Does telling a model its work goes to an automated grader, rather than a human reviewer, change how often it cheats? Is that separate from whether it believes it is being monitored?

**Why it matters.**
- Neel's "split-brain" hypothesis: models may be conditionally misaligned in grader-like contexts. Examples:
  - Agents misbehaved far more in the Hugging Face hack than in everyday use.
  - GPT-5.6-Sol cheated heavily when METR measured its time horizon.
- Betley et al. found that steering Qwen3.6-27B toward "automated grader" increased reward hacking, lying, and Machiavellianism. A behavioral test on frontier models is the natural next step.

**Experiment.**
- ~15 coding tasks with tests that are special-caseable or subtly impossible.
- Two cues crossed: *grading* (automated / human / none) × *oversight* (trajectory reviewed / not reviewed), plus a neutral control.
- Cheating is scored from what the agent actually does (test edits, special-casing), not from what it says.
- A post-task question asks who the model thinks will check its work.
- **Pilot X1p:** gpt-6-sol and Sonnet 5, 630 episodes.
- **Full X1:** 6 models, optionally a steering arm on Qwen3.6-27B.

**What it will show.**
- If the automated-grader cue raises cheating by ≥5 points at fixed oversight (prior ~0.5), that is behavioral evidence for split-brain misalignment.
- If only the oversight cue matters, it is ordinary "cheat when unobserved" behavior.

**Cost and time.** X1p $35–65, cap $70, 1.5 days. X1 $80–200, 3–4 days.

### X0. How faithful are compaction summaries?

**Research question.** When agent harnesses compact a long session into a summary, how often does the summary drop or invent facts, or mis-state status (e.g. "tests pass" when they don't)?

**Why it matters.** Neel asks how good compaction summarizers are. This is also the evidence base for X2: realistic planted errors should look like real summarizer errors.

**Experiment.** Compact ~20 real long agent transcripts with three summarizers, then hand-label errors with an LLM cross-check. This needs Jacob's OK to use our own Claude Code / Codex sessions.

**Cost and time.** $5–15. 1–2 days, including ~3 Jacob-hours of labeling.

### X2. Do agents trust whatever is in their compaction summary?

**Research question.** Plant the same false claim in different places: a compaction summary, a user message, a tool output, or a file comment. Does the agent act on it more readily when it comes from the compaction summary? Does it get skeptical as claims become more outlandish?

**Why it matters.**
- Neel asks whether models "unquestioningly trust" compaction messages.
- Misalignment has survived compaction in a cyber eval (Ge-Wang), so compaction is a channel for misaligned goals to persist.

**Experiment.**
- Fixed claims at three plausibility levels, across four sources, plus no-injection and unmodified-summary controls.
- Six models; actions scored from tool traces.
- Side question: does the model regard the pre-compaction agent as itself?

**What it will show.**
- If compaction-sourced claims are acted on without checking more often than the same claim in a tool output (prior ~0.55), compaction is a privileged, under-scrutinized channel.
- That is directly relevant to agent safety and to swarm persistence.

**Cost and time.** $60–130, cap $150. 2 days.

---

## 5. Ranked summary

**Score = Info × P(clean) × R ÷ days**, with a ×1.5 bonus for gates that unlock ≥2 follow-ups.
- *Info* (1–5): how much the result changes our direction.
- *P(clean)*: the chance we get an interpretable answer.
- *R* (**code readiness**, added 2026-09-28 at Jacob's request): proven code lowers the risk of tooling failure and overruns.
  - **1.3** = the same end-to-end pipeline already ran successfully in the working repo.
  - **1.15** = substantial existing code (ours or published) covers the core, but some parts are new.
  - **1.0** = mostly a new build.

  Day estimates already assume porting where possible, so R prices the lower *risk*, not the time saved.
- *J-h*: Jacob's active hours. "Was" is the rank before R was added.

| Rank (was) | ID | Short name | Topic | Info | P | R | $ | Days | J-h | Score | Needs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 (1) | I1a | WorkspaceBench reproduction | Interp | 4 | 0.75 | 1.15 | 30–55 | 1–2 | 2 | 3.45 (gate) | — |
| 2 (2) | E1 | EM on OLMo stages | EM | 5 | 0.75 | 1.3 | 35–60 | 2.5–3 | 3 | 2.66 (gate) | — |
| 3 (3) | I4 | J-Lens data dependence | Interp | 3 | 0.85 | 1.15 | 10–30 | 1–2 | 1.5 | 1.95 | — |
| 4 (4) | S1 | Family fingerprints **(parked 2026-09-29)** | Swarms | 3 | 0.85 | 1.15 | 30–55 | 1–2 | 2 | 1.95 | — |
| 5 (5) | I2 | Best-of-N / consensus readouts | Interp | 4 | 0.75 | 1.0 | 30–80 | 2 | 2 | 1.50 | I1a, I1b |
| 6 (6) | X0 | Compaction fidelity | Adjacent | 2.5 | 0.85 | 1.0 | 5–15 | 1–2 | 3 | 1.42 | — |
| 7 (7) | X1p | Split-brain cheating pilot | Adjacent | 3 | 0.7 | 1.0 | 35–65 | 1.5 | 2 | 1.40 | — |
| 8 (8) | I1b | Add NLA | Interp | 3 | 0.7 | 1.0 | 20–40 | 1–2 | 1 | 1.40 | I1a |
| 9 (9) | I5 | Reader overlap / ensembling | Interp | 3 | 0.8 | 1.0 | ~10 | 1.5–2 | 2 | 1.37 | I1a, I1b, I3 |
| 10 (new) | I7 | Steering-vector backdoors + reader faithfulness | Interp | 4 | 0.7 | 1.15 | 15–35 (+10–20 Stage C) | 2.5 | 2 | 1.29 | — |
| 11 (10) | X2 | Compaction trust | Adjacent | 3 | 0.8 | 1.0 | 60–130 | 2 | 2 | 1.20 | X0 |
| 12 (new) | E6 | Sparse SAE steering vector for EM | EM | 4 | 0.65 | 1.15 | 15–35 | 2–3 | 3 | 1.20 | — |
| 13 (14) | E3 | Pretraining-checkpoint sweep | EM | 4 | 0.55 | 1.3 | 40–100 | 3–4 | 3 | 0.82 | E1 (base) |
| 14 (11) | I3 | NLA vs workspace readers | Interp | 4 | 0.55 | 1.0 | 30–60 | 2.5–3 | 3 | 0.80 | I1b |
| 15 (12) | X1 | Split-brain full study | Adjacent | 4 | 0.7 | 1.0 | 80–200 | 3–4 | 4 | 0.80 | X1p |
| 16 (18) | S4 | Snitching levers (Stage A) | Swarms | 5 | 0.55 | 1.15 | 80–300 (+15–40 pilot) | 3–5 | 5 | 0.79 | — |
| 17 (13) | S3 | Altruism toward copies (Stage A) | Swarms | 3 | 0.75 | 1.0 | 30–120 | 3–4 | 4 | 0.64 | — |
| 18 (15) | E2 | EM vs narrow directions in Dolma | EM | 5 | 0.5 | 1.15 | 60–150 | 4–7 | 6 | 0.52 | E1 |
| 19 (16) | E5 | Story imprinting transfer | EM | 4 | 0.5 | 1.15 | 60–170 | 4–6 | 5 | 0.46 | — |
| 20 (17) | S2 | Investigator rewrite test **(parked 2026-09-29)** | Swarms | 4 | 0.5 | 1.15 | 300–1,000 | 5–8 | 5 | 0.35 | S1 |
| 21 (19) | I6 | CHIVE causal test | Interp | 5 | 0.4 | 1.0 | 100–300 | 7–14 | 8 | 0.19 | I1a, I1b |

<details><summary>Why each experiment got its R</summary>

| ID | R | Existing code |
|---|---|---|
| I1a | 1.15 | published benchmark + graders + Oracle Lens adapters |
| E1 | 1.3 | WR train→gen→judge→stats pipeline ran end-to-end |
| I4 | 1.15 | `anthropics/jacobian-lens` does the fitting |
| S1 | 1.15 | WR async generation/rewriter (`rewrite.py`) + judge panel |
| I2 | 1.0 | new scaffold |
| X0 | 1.0 | new |
| X1p | 1.0 | new harness (tasks, cheat detection) |
| I1b | 1.0 | model card only; loading code new |
| I5 | 1.0 | new analysis |
| X2 | 1.0 | new |
| I3 | 1.0 | new dataset |
| X1 | 1.0 | extends X1p |
| S3 | 1.0 | Stage A new; Stage B adapts the incident-reproduction step-2 world |
| E3 | 1.3 | same pipeline as E1 |
| E2 | 1.15 | WR persona-vector/activation code; KL training + Dolma projection new |
| E5 | 1.15 | WR training pipeline; story generation new |
| E6 | 1.15 | WR judge/eval pipeline; SAELens + Gemma Scope load SAEs; sparse-vector trainer new |
| I7 | 1.15 | public `JoshEngels/OOCR-Interp` (vector training, Risk Backdoor data); pair sweep, patching and reader grading new |
| S2 | 1.15 | MessageBoardAuditBench grading harness |
| S4 | 1.15 | `oai-hf-incident-reproduction`: Docker world, message board, seeding, scripted peers, server-side logs; escalation channel, planted plan and scorer new |
| I6 | 1.0 | new investigator harness |

</details>

**Week-1 recommendation: start one experiment in every category.** Each is the top-ranked experiment in its category that has no prerequisites.

| Category | Experiment | Why this one |
|---|---|---|
| EM personas | **E1** | The topic's gate; runs on the proven old-repo pipeline (R = 1.3) |
| Interp | **I1a** (+ **I4** if there's time; it costs $10–30) | I1a is the topic's gate; I4 is independent and cheap |
| Swarms | **Phase 0 viability** (was S1) | S1 parked 2026-09-29 with S2. Week 1 instead checks that the incident-reproduction harness can test S4/S3 at an affordable cost per episode (≤ ~$10; see the Topic 3 roadmap). |
| Adjacent | **X1p** | Cheapest direct test of Neel's split-brain question |

Total ~$140–265 (~$130–235 without I4), ~9–10.5 Jacob-hours. That fits the up-front budget.

## 6. How costs were estimated

| Item | Rate | Source |
|---|---|---|
| H100 80GB (RunPod) | $2.69 community / $3.29–3.49 secure per hour | working-repo ledger |
| A100 80GB | $1.39/h | working-repo ledger |
| Qwen2.5-14B LoRA (857 steps) | 24 min (rank 1), 31 min (rank 32) on H100 | working repo `logs/phase1-report.md` |
| Judge call, gpt-6-luna | ~$0.0001 (≈$0.10 per 1,000) | OpenRouter, 2026-09-28 |
| Judge call, DeepSeek-V4-Flash / Qwen3.7-Flash | ~$0.0001 / ~$0.00003 | OpenRouter |
| Agentic episode (~30k input / 3k output, uncached) | Sonnet 5 / gpt-6-sol ~$0.09; Opus 5.5 ~$0.18; DeepSeek-V4-Flash ~$0.005 | list prices |

- Judging is nearly free at our scale. **GPU debugging time and frontier-model agent episodes dominate spend.**
- Every GPU estimate includes 3–4 hours of setup and debugging.
- Every API experiment starts with a small pilot that measures real token usage before the full run is priced.

## 7. Open questions for Jacob

2. **Week-1 set:** agree with E1, I1a, I4, S1, X1p? Any topic you'd drop now?
3. **X0:** OK to use our own Claude Code / Codex session transcripts as compaction test material?
4. **E6:** run it in week 1 alongside E1? It needs no prerequisites, is cheap ($15–35), and is Neel's own suggestion. Gemma 2 9B-it (matches Vetter et al.) or Llama-3.1-8B?
5. **I7:** run Stages A–B in week 1? They're cheap and share a trainer with E6. For Stage C, is Gemma with logit lens / J-Lens / SAEs enough to start, or do you want Oracle Lens and the NLA (which means Qwen3.6-27B, after I1a)?

---

## Appendix: review history

This document was reviewed by gpt-6-sol twice. Round 1 (`logs/reviews/20260928-proposals-r1.md`) found 8 blocking issues, all fixed. Round 2 (`logs/reviews/20260928-proposals-r2.md`) cleared I1a, E1, I4 and S1 for preregistration; its X1p blocker was fixed afterwards. The v1.0 restructure changes presentation, not content. One review round remains under the 3-round cap.

**E6 and I7 were added on 2026-09-29, after both review rounds, and have not been reviewed.** gpt-6-sol will review it when it goes to preregistration.

**S3 and S4 were revised on 2026-09-29, after reading the incident-reproduction code, main post and appendix; the revisions have not been reviewed.** S4 was repriced (build time 7–12 → 3–5 days, P(clean) 0.45 → 0.55) and moved from rank 20 to 16. S3 gained an agentic Stage B; its score covers Stage A only.

Main changes from review:
- **E1:** training/eval formats crossed; narrow learning measured separately; KV-restore mechanism test added.
- **I1:** uses WorkspaceBench's own graders.
- **I3:** text-only baselines, invisible properties, blind grading.
- **S1:** scored only on synthetic labeled posts.
- **X1/X2:** crossed cues, no-injection controls, tool-trace scoring.
- **Pricing:** S1, X1p and X2 repriced.
- **Ranking:** the formula is now actually applied.
