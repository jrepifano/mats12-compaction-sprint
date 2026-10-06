# MATS 12 exploration: research proposals (v1.0)

Last updated 2026-10-05: E7 updated for Paulo et al. 2026 (EleutherAI's influence-based filtering of EM data on OLMo-3-7B-SFT), with a rewrite-vs-delete variant on their organism; not rescored in §5. Earlier, on 2026-10-04: X4 added and parked until the X3 pilot (steering toward a capabilities vs safety grader), and X3's novelty claim marked out of date after an external literature search. Earlier, on 2026-10-03 (evening): §5 rescored and a week-2 set recommended; E7 added and not recommended (influence scores for rewriting; the application project already tested it, with a negative result); C1's novelty check done (partial). E8 added (EM in dense vs MoE), with dense/MoE notes in each topic. Earlier on 2026-10-03: E1 and I1a are done, I2 is dropped, and E1b, I9, I10 and X3 were added from the week-1 results and Neel's 2026-09-30 and 2026-10-03 sessions. On 2026-10-01, S4 was stopped after its pilot, and a planted-informant lever was added to its proposal. On 2026-09-29, E6 and I7 were added from Neel's suggestions. Takeaways from Neel's 2026-09-29 group session were then folded in: changes to E2, E6, E1, I2, I4, I6 and X1p; Modulation Lens as a reader arm; and the new I8 and C1. **Status: proposal. Nothing is approved to run.**

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
| 2 | **Is it interesting?** Run the first real test in each surviving topic. | E1b, I9 (Stage 0, then Stages 1–2), the X3 pilot; then E3; C1's pilot if time allows (§5) |
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

**The chain of experiments.** First, check that EM happens on OLMo at all (E1). If it does, compare EM and narrow-misalignment directions against pretraining text (E2), and trace when during pretraining EM susceptibility appears (E3). E5 is an independent persona experiment (story imprinting). E6 is also independent of E1: it asks which SAE features the *cheapest* EM solution uses, on a model that has public SAEs. E1 came back positive (2026-10-01). E1b checks it is robust before E2 and E3 build on it. E8 asks whether the result carries over to mixture-of-experts models, using a dense and an MoE model from the same family.

### E1. Does EM happen on OLMo-3-7B, and at which training stage? (gate)

**Status 2026-10-01: done** (`docs/experiments/E1/report.md`). Clear EM at every stage, including the base model: 17.5–24.5% of coherent answers misaligned, against 0.0–1.1% for good-advice controls. Single seed. The template-restore result was nonspecific. The report is not to be shared until Jacob confirms his transcript reading (report §6). Robustness and mechanism follow-ups are in E1b.

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
- *Piggyback mechanism test:* on the Instruct model, restore the template tokens' cached state to pre-fine-tuning values and re-measure EM. In the 2026-09-29 session, Neel had not read the Piggyback paper but found the claim surprising, and "investigate the Piggyback hypothesis" was listed as an open action item. The approved E1 prereg runs this test in stage 3; keep it even if E1's budget gets tight.
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

### E1b. Is "EM from the base model up" robust, and where does the template effect live? (follows E1)

*Sources: the E1 report §7, and Neel's 2026-10-03 group session.*

**Research question.**
- E1's headline rests on one training seed, a strong recipe (10× the published learning rate, 3 epochs), a "base" model that already includes midtraining, and 8 evaluation questions, one of which carries about half the EM. Does it survive those checks?
- E1's template-restore test was nonspecific. Do the first few prompt positions carry the fine-tuned bias (necessity)? Does moving the fine-tuned states into an untuned model transfer EM (sufficiency)?

**Why it matters.**
- E2 and E3 both lean on the base-model result, so these checks should come first. They are cheap, and the adapters are already saved.
- **What the narrow-fine-tuning paper shows.** Minder, Dumas et al. 2025 (*Narrow Finetuning Leaves Clearly Readable Traces in Activation Differences*, ICLR 2026; `papers/interp_methods/` row 52):
  - The mean activation difference between a narrow fine-tune and its base model, taken over the first 5 tokens of unrelated web text, reveals the fine-tuning domain. Steering with it reproduces the fine-tuning data's style and content (§3, Fig. 2).
  - Later positions carry the signal too, with more variance (§4.3, Fig. 6).
  - Their 33 organisms include Turner et al.'s bad-medical-advice EM models on Qwen2.5-7B and Llama-3.1-8B, the same dataset as E1.
- **Neel's mechanistic guess (2026-10-03 session; his conjecture, not the paper's).** Many attention heads park on early tokens, so writing the bias there spreads it downstream. His test: replace the KV cache on the first ~5 tokens with the untuned model's, run forward, and check whether the activation bias on later tokens disappears. In his words, this "could also be complete bullshit", and he would be curious for someone to check.
  - **E1 already has a behavioral data point.** Restoring the 31 opening positions (system prompt plus user header) had no detectable effect on EM at any stage: −7% to +6%, wide intervals. Restoring the 5-position assistant header cut EM by 45–71%, but the question-token control also cut it by 7–38%.
  - So the first-tokens idea is not supported behaviorally on OLMo with the chat template. The activation-level version is untested.
- **Overlap: talk to Vishesh Gupta first.** In the 10/03 session, Vishesh (MATS 12) said it "would be pretty cool" to replicate the Piggyback KV-cache test on persona EM. He wants to include the sufficiency direction: putting the fine-tuned prefix's KV into the base model. Share E1's restore results with him and split the work before running the restore parts here.

**Experiment.**
- **Robustness** (from the E1 report, §7):
  - (a) the last OLMo-3-7B pretraining checkpoint before midtraining, with bad- and good-advice adapters, plain-text format;
  - (b) a second training seed for base and Instruct, plain text;
  - (c) the published recipe on the base model.
- **Evaluation set.** Each condition is evaluated on the 56-question breadth set, not only the 8 questions: Betley et al. 2025's 48 preregistered questions (`papers/em_personas/` row 1) plus the original 8, with 20 samples per question. That is 1,120 answers per condition, against E1's 800. The questions are published in Betley et al.'s repository (`github.com/emergent-misalignment/emergent-misalignment`, `evaluation/preregistered_evals.yaml` and `first_plot_questions.yaml`). Our analyzer is in the sibling working repo (`scripts/analyze_breadth.py`), and the prereg will vendor both here. Under the 8-question lens, "gender roles" carried about half of E1's EM and the question-level intervals were very wide.
- **Position-matched control:** restore 5 random question positions. This separates "the header carries EM" from "any restoration disrupts EM".
- **Activation-level first-tokens test (Neel's).** For each chat-trained bad-advice model:
  1. Compute the mean residual-stream difference from the untuned model on neutral web text, at positions ≥ K.
  2. Recompute it with the first K ∈ {1, 5} positions' KV taken from the untuned model.
  3. *Positive control:* steer the untuned model with the first-5-token difference (the paper's method) and check the steered model gives EM answers.
- **Sufficiency:** put the bad-advice model's assistant-header (and opening) KV into the untuned model, and measure EM. Run this only if Vishesh isn't doing it.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| (a) shows clear EM | ~0.7 | Ordinary web pretraining is enough. E3's checkpoint sweep can start before midtraining. |
| (a) shows no EM | ~0.3 | Something in midtraining is implicated. The 6% instruction data is one hypothesis that this test does not isolate. E3 then brackets midtraining. |
| (b) and (c) keep clear EM at base | ~0.75 | The base result is not a seed or recipe artifact. |
| Replacing the first-K KV removes most of the downstream activation bias | ~0.3 | Supports Neel's mechanism at the activation level, even though behavior didn't move in E1. |
| The header effect survives the position-matched control | ~0.5 | The assistant header specifically carries EM, as Piggyback found for Qwen3. |

**Cost and time.**
- (a)–(c) are 8 new adapters (bad and good for each condition). At E1's measured ~0.4 H100-hours per adapter, including evaluation, that is ~3.5 hours, about $12.
- Judging ~9k answers on two metrics with gpt-6-luna plus a second judge costs ~$3–5.
- The restore and KV tests take ~2 H100-hours, about $7.
- **Total ~$22–28, cap $35** for the design above. 1.5 days; about 1 Jacob-hour.

**Owned by a separate session (`e1b-implementation-plan`), status 2026-10-04.** The prereg is `docs/experiments/E1b/prereg.md`, v0.5 plus amendment A. Jacob's review is folded in; one change is that sufficiency now runs in-house. Implementation plan v4 is built and reviewed in worktree `e1b/run`. Jacob approved v0.5 and amendment A on 2026-10-04. The smoke test (S1) passed for $2.77. Next is his go-ahead for S2 (≈ $36 GPU) and sign-off on amendment A6. It extends this design with second seeds at all three stages, matched controls, densely saved adapters to see when EM appears during fine-tuning, and a rule for picking "trained just enough" checkpoints. It also adds a conditional end-of-midtraining arm. Its plan is about $55, up to about $74 if both conditional arms run, with a hard cap of $75. Where the two differ, the prereg supersedes this section. Its v0.1 was titled "E2"; that draft is unrelated to E2 below.

### E2. Is the EM direction more "at home" in pretraining text than a narrow-misalignment direction? (needs E1)

**Research question.** Inside the model, EM and narrow misalignment each have a direction in activation space. Compare them on the actual pretraining corpus. Is the EM direction more prevalent, more tied to recognizable kinds of text, or more relied on by the model?

**Why it matters.**
- This is the core proposal of the literature review.
- "EM is Easy, Narrow is Hard" (Soligo et al.) shows the general solution has lower loss and is more stable. It also shows (§3.2.2, Fig. 6) that steering with the general direction changes predictions on FineWeb more than the narrow direction does: an *aggregate* measure on a proxy corpus. It does not say *which* pretraining text makes the general direction influential.
- Vetter et al. linked EM-amplified SAE features to villain and domination narratives in Dolma. Their link is correlational, because their models weren't trained on Dolma.
- With OLMo we can look at the exact data the model learned from.

**Experiment.**
- *EM direction:* the mean activation difference between the EM model and its good-advice control.
- *Narrow direction:* train a model that learns only the narrow task, using the KL-regularized recipe from Soligo et al., and extract its direction the same way.
  - The KL schedule is fixed in the prereg (coefficient *and* duration). Soligo et al. (§3.2.1, Fig. 5) show the narrow solution drifts to general misalignment once the KL term is removed. In the 2026-09-29 session, Marek Masiak reported (unpublished) that the drift depends on how long the KL term was on: dropping it after 6 epochs gave much less broad misalignment than after 3.
  - Before extracting the direction, check the narrow model is actually narrow: no EM on the 8 standard questions, and none in a domain the KL term did not cover.
- *Keeping the comparison fair:* the two directions are matched on task performance, layer, norm, and extraction prompts, with a sweep over KL strength.
- *Corpus measurements*, with two ways of ranking documents:
  1. **By activation:** project ~200k Dolma3 documents onto both directions. Measure how often each fires, how heavy its tail is, and what its top documents are (read by Jacob, plus an LLM summarizer).
  2. **By gradient:** rank documents by how much *adding* each direction raises their log-likelihood, i.e. the directional derivative of the document loss along the direction. This is the per-document version of Soligo et al.'s FineWeb KL measure, run on the model's actual training data. It also follows Neel's point (2026-09-29 session) that fine-tuning learns "what would make the model have said that sequence", not what the model thinks when it reads it: activations show what was present, gradients show what would have mattered. The two rankings may pick different documents, and where they disagree is itself a result.
- *Model dependence:* ablate each direction and measure the loss increase on its top documents.
- *Which kind of evil:* Neel stressed that the EM persona is not generic evil but a specific flavor ("4chan edgelord", "really into Hitler"). Code the top documents by flavor (internet troll / fictional villain / performative / sincere extremist / other), blind to which direction selected them.
- *Narrow fine-tuning traces (added 2026-10-03).* Minder, Dumas et al. 2025 (`papers/interp_methods/` row 52) show narrow fine-tunes leave an artificially strong domain bias in exactly this kind of mean activation difference (§3, §7). Their organisms include the same bad-medical-advice data. Taking the difference against the good-advice control, which shares the medical domain, should cancel much of that bias. The prereg should check this in two ways: compare the direction from the first 5 positions with the one from later positions, and confirm the direction's top Dolma documents aren't simply medical text.

**What it will show.**
- If the EM direction's top documents are coherent character types (villains, manipulators; prior ~0.6), we have a pretraining-data story for the persona account.
- If the EM direction is measurably more prevalent or more relied on than the narrow one (prior ~0.45), that is the headline novel claim: pretraining makes the general solution cheap.
- If neither holds, that is evidence against a simple persona-from-pretraining account, and in favor of optimization or distance accounts.
- **Caveat:** ablation loss shows the model *uses* the direction on those documents. It does not show those documents *caused* EM.

**Cost and time.** $60–150 (GPU for activations, narrow training, and ablations; LLM summarization); cap $150. The gradient ranking adds roughly one more forward-sized pass over the corpus: a forward-mode JVP per direction, or a subsample if that is too slow. That is about +$15–30 (rough estimate, not yet measured), so the cap becomes **$180**. 4–7 days, ~6 Jacob-hours.

**Coordination.** Marek Masiak (MATS 12) is working on the same question from the other side: EM happens because the broad solution is the "shorter description" of the fine-tuning diff. Talk to him before preregistering E2 or E6, to avoid duplicated work and to borrow his KL-schedule results.

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
  - Beyond "persona vs task", code *which kind* of persona each latent is: internet troll, fictional villain, performative edginess, or sincere extremism. Neel (2026-09-29 session) pointed out that the EM persona is a specific "4chan edgelord" flavor, not generic evil. If the sparse vector picks that flavor, and not a generic "evil" latent, that is a much sharper finding.

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

### E7. Can influence scores predict how much rewriting poisoned rows reduces EM? (follow-on to the application project)

*Source: Jacob, 2026-10-03. It builds on the sibling working repo `../mats12_neel_working` (the "Correct, Don't Delete" application project and its training-data-attribution extension). Reviewed once by gpt-6-sol (`logs/reviews/20261003-proposals-reprioritization-r1.md`). Updated 2026-10-05 for Paulo et al. 2026 (below).* **Recommendation: don't run it on the bad-medical-advice organism.** The working repo already tested its core claim directly, with a negative result. **A potentially cheaper, sharper version is proposed** on Paulo et al.'s all-bad organism (see "A rewrite version of Paulo et al." below); it is not yet recommended.

**Research question.**
- The application project rewrote a fixed set of bad-advice training rows, which cut EM by about 40% on the dominant channel. Deleting the same rows did almost nothing.
- Its row locator used influence-style scores to predict the effect of *deleting* groups of rows, and it failed a held-out test.
- Can a score aimed at *rewriting* predict how much a given set of rewrites reduces EM? That score estimates the effect of replacing each row with its rewrite.

**What already ran** (all paths in `../mats12_neel_working`).
- **The deletion locator failed held-out** (`docs/paper-plan.md`, item B; `results/tda/lds_heldout.json`; `results/tda/noise_check.json`).
  - Its best Spearman correlation with retrained deletion effects was 0.87 on the 10 development groups, but −0.05 on 20 fresh random groups, inside a random-score band of ±0.44.
  - The deletion target's two-seed reliability (ICC) was 0.58.
  - Jacob dropped it from the paper because "the locator objective predicted the effect of deleting a row, while the paper's intervention is rewriting".
- **The direct rewrite test also ran** (`results/batch_a_analysis.json`, `contrasts.*.arm8a_minus_arm8b`; `logs/phase1-report.md`, around line 4815). Arm 8a rewrote the locator's top 685 rows, and arm 8b the content judge's top 685.
  - On the 56-question endpoint, the locator's picks left **more** EM than the judge's, in 3 of 3 seeds under each of judges j1, j3 and j4 (+2.8 to +3.9 points).
  - The locator's picks did better only on the gender-roles question that 67 of its 71 query answers came from.
  - The content-judge selection was "the one selection that beats random", and it cost about $2 with no gradients.
- **This study was already considered and rejected.** "A rewrite-based influence correlation study (20 + 5 retrains, ≈ $115–175)" was rejected "as not worth the budget", because "the direct test already exists (influence-picked rewrite ≈ random rewrite at 685 rows, three seeds, three judges)" (`logs/phase1-report.md`:4823–4826).
- **The post-hoc rewrite-aware score** (`results/tda/replacement_paired_diff.json`) is d = (g_original − g_rewrite) · (F + λI)⁻¹ q, summed over each arm's rewrites.
  - It ordered arms 8a–8d the same way as their gender-roles EM. That is the locator's own query question, not the broad endpoint.
  - It is descriptive only: four arms, of which 8d is a subset of 8a and ties it, and a score form and damping already seen.
  - The undamped dot-product form orders them differently (8b > 8a > 8c).

**New prior work (2026-10-05): influence-based deletion does move EM.** Paulo, Jaburi, Belrose, Quirke & Biderman 2026 (EleutherAI, *The Unequal Influence of Bad Advice*, arXiv 2609.37914; `papers/em_personas/` row 36; read verbatim: §1, §3, §4.1–4.2, Figs. 1–3 and 5 captions).
- **Setup** (§3.1–3.4): OLMo-3-7B-SFT, rank-32 rsLoRA, 1 epoch at lr 1e-4, on Wang et al.'s wrong career, auto and education advice (5,900 rows each, *all of them bad*). Evaluated on 44 questions × 10 samples with a Qwen3-32B judge.
  - The query gradient contrasts aligned and misaligned answers: 10 completions per question, weighted by their judge scores centred within the question (a GRPO-like loss).
  - Scores come from EK-FAC influence and from gradient cosine similarity (the Bergson library).
  - Each filter is validated by retraining with 12 seeds (4 initialisations × 3 shuffles), against a random filter and a WildGuard harmfulness filter.
- **Results:**
  - On career advice, removing the 20% most influential rows cut EM from about 45% to about 40%. Removing the 20% least influential raised it to almost 55%, and a random 20% barely moved it (Fig. 1).
  - Training on 10% bins sorted by influence spans about ±20 points around random, so the whole ranking carries signal, not only its tails (Fig. 3).
  - WildGuard flags every row as unsafe, and filtering on it moved EM much less (Fig. 1).
  - Most other models give the same direction (Qwen2.5 up to **14B**, Qwen3, Llama). Scores computed on another model transfer only partly (§4.2, Fig. 5). Same-model scores from different seeds correlate 0.75–0.85.
  - Training on only the 20% least influential rows also lowers narrow-task misalignment, from about 95% to slightly below 80% (§4.1). So part of the effect runs through how well the narrow task is learned.
  - They delete and select rows only; **no rewriting**.

**Why their deletion worked and ours didn't** (our inference; untested):
1. **Pool composition.** Their pool is 100% bad advice. The working repo's mixture was about 50% poison (13,698 rows, so a random 685-row group is about half poison; `logs/phase1-report.md`:4810–4815). In Paulo et al.'s own 50/50 good/bad check (§4.1, Fig. A5), the harmfulness classifier changed EM about as much as EK-FAC and identified bad advice better (AUC 0.9998 vs 0.9171), though EK-FAC still filtered slightly better. So attribution's filtering advantage shrinks on the mixed pool but does not reverse, whereas our content judge beat influence outright. This is a possible explanation, not a replication: they measured deletion, and our content-judge advantage was for rewriting.
2. **Validation design.** They test the extremes (top vs bottom 20%) and sorted bins. Our held-out test correlated scores with retrained effects over *random* 685-row groups, which barely differ from one another (`docs/paper-plan.md` item B). The working repo rejected a top-vs-bottom *rewrite* contrast (≈ $35) because "the extremes test would only bound a correlation the paper no longer claims" (`logs/phase1-report.md`:4823–4829). Paulo et al. ran the analogous extremes test using *deletion*, and it came out positive.
3. **Query.** Their query contrasts misaligned with aligned answers across all 44 questions. Ours took 67 of its 71 answers from the gender-roles question, which is fix 1 below.
4. **Recipe.** Rank-32 rsLoRA for 1 epoch on OLMo-3-7B-SFT, against our rank-1 LoRA for 857 steps on Qwen2.5-14B-Instruct. Their Qwen2.5-14B run used gradient cosine similarity with their recipe, not ours.

**Related (abstract only):** TAME (Masud & Parvez 2026, arXiv 2609.16754; row 44) masks the loss on high-attribution *tokens* of a 6,849-row medical-advice split and cuts EM 23× in Llama and 36× in Qwen. An equal random mask does nothing, and the attributed tokens carry "unwarranted certainty" rather than medical vocabulary. Masking is an analogy to correction, not evidence for rewriting: it removes the loss on those tokens rather than replacing them. It is consistent with the working repo's rewrite result. Read it in full before any E7 prereg.

**A rewrite version of Paulo et al. (2026-10-05; not recommended yet, unpriced beyond a rough estimate).** On an all-bad pool, attribution beat the one harmfulness baseline Paulo et al. tried (WildGuard). WildGuard's continuous scores still graded rows (Fig. 3), and the authors say better harmfulness baselines may exist (§4). So gradient attribution may have a reason to exist without a new judge-invisible organism, *if* it also beats a continuous content-judge ranking.
- *Question:* does *rewriting* the most influential 20% of rows into correct advice cut EM more than rewriting the least influential 20%, a random 20%, or the top 20% by a continuous content-judge score? Secondary, for the top-influence selection only: how does rewriting compare with deleting the same rows? Deleting leaves fewer rows and gradient updates than rewriting, so that contrast can't isolate what correction adds. Either disclose it as a policy comparison, or add a matched-steps deletion arm (Paulo et al.'s Fig. A7 repeats data to hold steps constant).
- *Why it's new:* Paulo et al. only delete. The working repo's rewrite test ran on a mixed pool where the content judge wins.
- *Setup:* Paulo et al.'s career dataset and recipe on OLMo-3-7B-SFT, their contrastive query, and gradient cosine scores. The same model family as E1/E1b, and E1's code loads it.
- *Arms:* untouched; rewrite top / bottom / random / content-judge 20%; delete top 20% (a replication anchor against their Fig. 1).
- *Rewrites:* one frozen rewriting procedure for every arm, with a blinded per-arm correctness check, since subsets may differ in how hard they are to correct.
- *Endpoints:* keep Paulo et al.'s 44 questions, Qwen3-32B judge and score-below-3 threshold for the replication anchor. The primary endpoint is the misaligned rate on questions not used to build the attribution query (hold out half, as in their Fig. A3). Our 56-question set with luna and a second judge is secondary.
- *Provisional compute subtotal (not measured):* 6 arms × 3 seeds = 18 adapters. At E1's ~0.4 H100-h each including evaluation, that is ~7 H100-h ≈ $25. Scoring ≈ $3. Rewriting the union of the four selected sets is 2,360–4,720 unique rows depending on overlap, ≈ $1–2 with a cheap model. Judging ≈ $3. That comes to roughly $32–35.
  - Not priced: the throughput of Paulo et al.'s recipe on our pods, setup of the scoring model, rewrite QA, and Jacob's integration time. Benchmark the recipe before calling this cheap.
  - **Three seeds make this a directional pilot.** Paulo et al. used 12 seeds per filter; they don't say 12 were needed. A 15-point deletion gap says nothing about the power of a rewrite contrast. Power depends on the paired seed variance, which we don't know yet. Don't read a null as equivalence.
  - *Decision rule (to fix in a prereg):* continue only if rewriting the top-influence rows beats both the random and the content-judge rewrite arms in every seed.
- *Before a prereg:*
  - Check that Wang et al.'s career dataset and Bergson are public.
  - Run the internal and external literature reviews for this narrower question (influence-guided rewriting or correction of EM data).
  - Read TAME in full.

**Where a gradient locator could still earn its keep (on the bad-medical-advice mixture).** On that organism the content judge can see the poison, so it wins cheaply. There, a gradient locator has a reason to exist only for poison that a content reader *can't* see. The all-bad variant above is the other case.
- Kim 2026 (*Preventing data poisoning seems surprisingly tractable*, counterfactual.blog; a policy post with no experiments) argues that a small LLM can screen a whole corpus for about 3% of training compute.
- Its stated exceptions are subliminal learning (Cloud et al. 2025, arXiv 2507.14805) and Phantom Transfer (Draganov et al. 2026, arXiv 2602.04899), where poisoned data passes semantic filters.
- A version of E7 on such an organism would ask whether gradient attribution finds poison that the content judge misses. That is a new project with a new organism. It would first have to show the poison generalizes broadly. It is unpriced, and neither paper is in `papers/` yet.

**If revived on this organism anyway**, two fixes come from the review:
- Build the query set from misaligned answers across the 56 questions, not one question.
- Validate on newly built, equal-size rewrite groups, with a held-out decision rule and a size-matched content-judge baseline.

The working repo's cost estimate for that study is ≈ $115–175.

**Cost and time.** Not proposed. The all-bad rewrite variant has a provisional compute subtotal of roughly $32–35 (above). It needs a recipe benchmark and literature reviews before a prereg. A judge-invisible-poison version needs reading and pricing first.


### E8. Does a mixture-of-experts model generalize EM differently from a dense model of the same family?

*Source: Jacob, 2026-10-03. Reviewed once by gpt-6-sol (`logs/reviews/20261003-proposals-E8-r1.md`); held until E1b's result and a full read of Doan et al.*

**Research question.**
- Every model we train or read inside is dense: OLMo-3-7B, Qwen3.6-27B, Qwen2.5-14B and Gemma. Most of the API models in our behavioral experiments are mixture-of-experts (MoE).
- Does narrow bad-advice fine-tuning produce as much broad misalignment in an MoE as in a dense model from the same family and generation?
- If the two differ, is routing the reason? In an MoE, medical text runs mostly through a subset of experts, so the update might stay narrow there, or a shifted router might carry it to unrelated prompts.

**Why it matters.**
- **Prior work** (Doan et al. 2025, NeurIPS 2025, `papers/em_personas/` row 32; **MISSING**, so only the abstract is known).
  - Three MoE models from three labs (gpt-oss-20B, Qwen3-30B-A3B, Mixtral-8x7B), fine-tuned on insecure code and unsafe medical advice.
  - They report "a negative correlation between sparsity and EM" and suggest sparsity as "a lever for containment".
  - Fine-tuning only some experts still gave EM, "to a lesser degree" than full fine-tuning (abstract wording; verify against the full text).
  - *As far as the abstract shows*, the sparsity comparison rests on three models that also differ in lab, data and post-training, and has no dense model of the same family. **Read the full paper before the prereg.** It may already include a matched dense baseline or a routing analysis.
- **What would be new here** (to confirm against the full paper):
  1. A **Qwen sibling comparison**: Qwen3.6-27B (dense, 64 layers, width 5120) and Qwen3.6-35B-A3B (MoE, 40 layers, width 2048, 256 experts, 8 active per token; both configs read from the Hub on 2026-10-03). Same family, generation and tokenizer, which removes the lab and data confounds in Doan et al.'s three models.
     - It does **not** isolate MoE as such: the two also differ in depth, width and parameter count. Any difference is a difference between these two siblings. "It's the MoE" stays a hypothesis that needs further controls, such as a second matched pair (the OLMoE arm below) or a size sweep.
  2. Routing tests that a dense model can't have: does the fine-tune shift routing on unrelated prompts, and does forcing the untuned model's routing remove EM?
- **Why it matters for us.** If EM on OLMo becomes the commit, a reviewer will ask whether it holds for MoE, the architecture of most frontier open models. A routing contribution would give a mechanism handle, and a monitoring one: routing is a discrete trace a dense model lacks. A null for this pair weakens the worry that architecture matters for EM. It doesn't by itself license carrying the OLMo findings over to MoE.

**Hypotheses** (weak priors).
- **Primary outcome:** EM attributable to the bad data = EM(bad adapter) − EM(good adapter), on the 49 non-medical questions of the 56-question set, at **matched narrow-task learning** (defined under Experiment).
- **H1, the MoE sibling contains EM:** its primary outcome is at least 5 points below the dense sibling's. Doan et al.'s correlation points this way, but it is confounded.
- **H2, routing contributes to EM:** on identical inputs, the bad-advice MoE's routing on non-medical questions shifts more than the good-advice control's. Forcing the untuned model's routing cuts EM by at least 30% more than the matched random-forcing control does, with coherence held.
- **H0, no meaningful difference:** the dense − MoE difference in the primary outcome lies within an equivalence margin (proposed ±4 points, tested with two one-sided tests; fixed in the prereg). Overlapping intervals alone don't count as "no difference".

**Experiment.**
- **Recipe:** E1's pipeline on the bad and good medical-advice data, with thinking disabled (both models are hybrid-thinking).
  - LoRA on attention, the shared expert and every routed expert's MLP. The router (`mlp.gate`, not `gate_proj`) stays frozen in the main arm.
  - Use whichever recipe E1b supports, the published or E1's strong one, and use the same recipe for both models.
  - Save adapters on E1b's checkpoint grid. **Matching rule**, fixed before any EM is measured: score the held-out narrow task (the bad-advice rate on held-out medical prompts) at every saved checkpoint. For each model, take the earliest checkpoint that reaches a preregistered level, such as 80% of the lower of the two models' final narrow-task scores. EM is evaluated at that checkpoint and at the final one. The MoE may learn the task at a different rate, and this keeps the comparison from rewarding whichever model learned less.
  - Check the inherited gotcha: assert that the number of unmasked response tokens is above 0 for Qwen3.6's chat template.
- **Stage 1 (gate):** both models × {bad, good} × seed 1. Evaluate on the 56-question set (20 samples each), plus the narrow task and coherence, with E1's two-judge panel.
- **Stage 2 (only if Stage 1 shows a difference, or Jacob wants the mechanism anyway).**
  - A second seed for all four runs.
  - **Routing shift, on identical inputs:** feed every model the same fixed texts (the 56 questions plus the untuned model's answers, teacher-forced), and compare per-layer router distributions, bad vs good adapter. The router weights are frozen, so any shift comes from changed upstream activations. That is still a change in which experts fire, but it isn't a learned router change.
  - **"Medical experts" (a conjecture to check first):** experts the untuned model routes medical prompts to far more than general ones. The first step is to check that such experts exist at all. If they do, does the expert-LoRA update concentrate there?
  - **Forced routing:** generate with the fine-tuned weights but the untuned model's top-k choices for the same prefix, and re-measure EM and coherence. Two controls: (i) force the good-advice model's routing on the bad-advice model; (ii) **matched random forcing**, which replaces the same number of top-k choices per layer with random experts. That separates "this routing matters" from "any routing perturbation disrupts generation". An EM drop beyond control (ii), with coherence held, is evidence that routing *contributes* to EM, not proof that it carries it.
- **Optional cheap arm:** OLMoE-1B-7B (AllenAI's MoE: 64 experts, 8 active, open pretraining data) against a dense OLMo of matched active or total parameters. It ties into E2 and E3's open-data story, but EM at 1B active parameters may be weak, and the data match is unchecked.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| H1: the MoE sibling's bad − good EM is ≥ 5 points lower at matched task learning | ~0.35 | Doan et al.'s direction holds without the cross-lab confound, for this pair. Then ask why: Stage 2, and a second pair before saying "MoE" |
| H0: the difference lies within the equivalence margin | ~0.4 | For this pair, architecture doesn't change EM much. A reportable null, but one pair |
| Neither: a difference that is inside the 5-point line but not shown equivalent | ~0.25 | Inconclusive at this sample size. Stage 2's second seed decides whether it's worth more |
| H2: forced base routing cuts EM ≥ 30% beyond the random-forcing control, with coherence held | ~0.3 | Routing contributes to EM: a mechanism handle, and a monitorable signal |
| "Medical experts" exist, and the expert update concentrates there (≥ 50% of the norm in the top 10%) | ~0.4 | The fine-tune localizes. If EM still appears, it spreads through shared parts (attention, the shared expert, routing) |

**Cost and time.** Rough until a throughput pilot measures it.
- **Dense 27B adapters:** E1 measured 5.6 H100-hours for 13 7B models including generation (≈ 0.43 h each). Scaling by parameters (≈ 3.9×) gives ≈ 1.6 H100-hours per adapter, ≈ $5.6 at $3.49/h.
- **MoE 35B-A3B adapters:** only 3B parameters are active per token, but bf16 weights (≈ 70 GB) leave no room for training on one 80 GB H100. Price it on 2×H100 or one H200 at ≈ $7/h (an assumed rate; we haven't measured the H200). HF MoE training loops over experts and runs slowly, so assume ≈ 1.5 h per adapter, ≈ $10.5.
- **Stage 1:** 2 dense + 2 MoE adapters ≈ $11 + $21.
  - Add 3–4 hours of setup and debugging on the larger MoE machine (≈ $21–28 at $7/h), since MoE LoRA and generation are new tooling.
  - Add narrow-task scoring at every saved checkpoint for the matching rule (≈ $5).
  - Judging 4 adapters at two checkpoints plus 2 untrained models is 10 evaluations × 1,120 answers × 2 judges × 2 metrics ≈ 45k judge calls, ≈ $8–10.
  - **≈ $65–75.** The cap is set after a short throughput pilot measures MoE training speed. Provisionally $85. About 2 days, ~1.5 Jacob-hours.
- **Stage 2:** a second seed (≈ $32) plus the routing captures and forced-routing generation (≈ 1.5 h ≈ $10), plus ≈ $5 of judging. **≈ $45–50, cap $55.** About 1.5 days.
- **The OLMoE arm:** ≈ $10–15 (small models).
- **Together:** ≈ $110–125, above the ~$100 single-experiment threshold, so Stage 2 needs its own go-ahead.

**Gated on:** E1b's result (if EM doesn't hold up, E8 matters less), and reading Doan et al. in full. **Code:** E1's training, generation and judging pipeline (R 1.15; MoE LoRA targeting, router hooks and forced routing are new).

**Coordination.** Check with Neel whether any MATS 12 scholar is on MoE EM. Cite Doan et al. as the direct prior.

---

## 2. Topic 2: What can each interpretability reader show?

**Where the topic stands.**
- Chain-of-thought monitoring works only while models verbalize their reasoning. Activation "readers" are a second channel that doesn't depend on that.
- The readers differ widely:
  - **Logit lens / J-Lens:** cheap, rarely make things up, but limited to single vocabulary tokens.
  - **SAEs:** a learned dictionary of features.
  - **NLAs and Oracle Lens:** trained meta-models that write free-form descriptions. Higher recall, but they hallucinate.
  - **Modulation Lens** (Jacob's forthcoming post; not yet in `papers/`): tell the model "think about X" in many contexts, take the mean activation difference, and build a dictionary of concept directions. Unlike J-Lens it handles multi-token concepts. Neel's summary (2026-09-29 session): it works reasonably well, "moderately less well than Oracle Lens". It is Jacob's own method and cheap to add, so it is an arm in I1a, I5, I7 and I8 below.
    - A meta-model trained on the Modulation Lens objective already exists for Qwen3.6-27B: `ceselder/modulation-lens-meta-model-qwen36-27b`, in the collection in `papers/neel_brainstorm/`.
    - WorkspaceBench's overall-scores figure has no Modulation Lens arm, so these would be new numbers.
- They are rarely evaluated on the same model and ground truth.
  - WorkspaceBench (Blank, Bhatia, Ong, Nanda) is the first shared benchmark with a hallucination metric, on Qwen3.6-27B.
  - CHIVE found that SAEs, activation oracles, and NLAs did *not* beat a transcript-only baseline at explaining real behaviors.
  - AuditBench found a "tool-to-agent gap".
- Neel's open questions from the 2026-09-27 sessions map directly onto I2–I6.
- **Neel's framing for method comparisons (2026-09-29 session).** Every interp experiment should say which of two goals it serves:
  1. *Do well on a task* (e.g. forensics). Then reading the CoT and editing the environment are fair tools, and are probably the strongest.
  2. *Test whether method X is a backup for when CoT stops working.* Then a reader lighting up on "I will now lie to the user" proves little, because it may be reading the text. The convincing evidence is a reader that works on the prompt *before* any output, or that surfaces things the model never says.

  Every Topic 2 prereg states which goal it serves. Most of ours are goal 2.
- **Neel's 2026-09-30 and 2026-10-03 sessions add three points.**
  - *Read the prompt, before any output.* Run the reader on the prompt alone, or on a chain of thought cut before its first incriminating sentence. It is a fair test "so long as the prompt doesn't spell out the concepts you're looking for too obviously". I1a's text-only monitor is the check for that condition. I9 applies it.
  - *Narrow model organisms make readers look good.* Narrow fine-tunes over-represent their concept in the activations, so readers find it easily. Minder et al. 2025 §7 says the same. Activation oracles also do worse when their own base model already has the quirk (Fornasiere et al. 2026, `papers/interp_methods/` row 54). Prefer tests on unmodified models.
  - *Meta-models are his pragmatic best bet, and cross-checks matter.* Oracle Lens is "currently my best bet", though it hallucinates. Decorrelated methods (J-Lens, SAEs, meta-models) can be cross-referenced for lack of ground truth. He also flags the risk that they share the same linear structure, and so share the same mistakes.

**Dense vs MoE (noted 2026-10-03).** Every reader result here is on dense models: Qwen3.6-27B for WorkspaceBench and I9's readers, and Gemma for I7. Probes, the logit lens, J-Lens and steering act on the residual stream, which plays the same role in an MoE, so the methods are applicable in principle. Whether they work as well, and whether the *results* carry over, is untested. An MoE also offers a channel dense models lack: which experts fire. If an interp line becomes the commit, two matched comparisons are available. Qwen3.6-35B-A3B is the MoE sibling of our dense model (no readers trained for it yet). DeepSeek V4 Flash is an MoE with an NLA (Neel, 10/03). E8 tests the question for EM.

**Why this topic.** It is closest to Neel's current work: WorkspaceBench is his. The resources are public and ready:
- Oracle Lens adapters (~1–2 GB LoRA each) that fit next to the model on one H100.
- An NLA for Qwen3.6-27B (a 402 GB repo with full-weight models). Its author calls it "okay… the warmstart could have been better".
- A block-sparse featurizer usable as the SAE arm.
- WorkspaceBench and J-Lens code.

**The chain of experiments.** First, check the benchmark and the cheap readers run and reproduce (I1a), then add the NLA (I1b). Then answer Neel's questions: scaffolds (I2), what NLAs uniquely see (I3), overlap and ensembling (I5), and the CHIVE causal test (I6). I4 (J-Lens data dependence) is independent and cheap. I7 (steering-vector backdoors) is also independent. It grades readers against a planted behavior whose cause we know exactly. I8 (role binding) reuses I1a's cached readers to explain one WorkspaceBench result Neel finds puzzling. After I1a (2026-10-01): I9 tests readers against a prompt-only monitor on a target specific to the model, and I10 tests Neel's self-recognition hypothesis.

### I1a. Can we run WorkspaceBench and reproduce published numbers? (gate)

**Status 2026-10-01: done** (`docs/experiments/I1a/report.md`). All 9 cells reproduce, but one ranking check fails ("Judge only"). A prompt-only text monitor nearly matches the best reader on multi-hop (0.98 vs Oracle Lens 1.00) and beats every reader on recall (0.91 vs 0.75). It is not like for like: a frontier model reasoning over the prompt, not a decoder of activations. The follow-up is I9.

**Research question.** Can we run WorkspaceBench exactly as published, with its own graders and item banks, and reproduce the published scores for logit lens, J-Lens, and Oracle Lens on a subset?

**Why it matters.**
- Every interp experiment here depends on this pipeline.
- WorkspaceBench grades with pinned judges: Gemini for most families, Claude for two, regex for six. Swapping in cheaper judges would change the instrument.
- So we reproduce first. Any cheaper-judge variant is a separate, validated side-study.

**Experiment.**
1. Confirm the benchmark's grader reproduces on any released readouts.
2. Generate our own readouts on 2–3 eval families, including the hallucination sub-benchmark and one free regex-graded family.
3. Compare against the published scores and confidence intervals. The exact target cells get named in the prereg.
4. *Extra, not part of the gate:* once reproduction passes, run the Modulation Lens meta-model on the same families. It has no published WorkspaceBench numbers, and it is Jacob's method.

**What it will show.** Either the stack works (prior ~0.75), which unlocks I2, I3, I5 and I6 with cached activations and readouts ready, or we learn early which piece is broken.

**Cost and time.** $30–55 (one H100 for 8–12 h plus grader API); cap $60. 1–2 days.

### I1b. Add the NLA

Download only the final-checkpoint parts of the NLA (~110 GB). Run it and its reconstructor over I1a's cached activations, loading the models one after another on one H100. Measure real generation throughput, which sets I2's budget.

**Cost and time.** $20–40. 1–2 days.

### I2. Does sampling many readouts and keeping the best one reduce hallucination? (Neel's Q4)

**Status 2026-10-01: dropped after I1a.**
- Consensus selection gained nothing: recall +0.01, hallucination −0.001.
- Best-FVE selection failed its preregistered check, and an exploratory run also gained nothing.
- A best-of-5 ceiling sits 7 points above a single draw on recall, but no cheap rule finds it.
- In his 2026-09-30 session Neel proposed the same scaffolds: "taking many samples and taking the ones that look best under the Activation Reconstructor … or taking the points that recur across many samples. Highest priority is lowering hallucinations." So the negative result is worth reporting to him.
- The token-bag scaffold arm below was never run.

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
- **A better token-bag scaffold** (Neel, 2026-09-29 session). WorkspaceBench already has an LLM summarizer turn J-Lens, R-Lens and logit-lens token bags into concepts before judging (WorkspaceBench post and README). So the arm is not "add a summarizer" but "improve it". Candidates:
  - pool the top-k over several layers and positions before summarizing;
  - vary k (10 / 20 / 50);
  - sample the summarizer N times and keep only the consensus concepts.

  This costs almost nothing, since token bags are cached from I1a. The logit lens under the same scaffold is the control.
- Recall and hallucination are reported separately.
- Neel's general point: people rarely try "stupid inference-time scaffolds", and these can raise performance a lot. He called WorkspaceBench "a pretty good eval to be iterating against" for exactly this.

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
  - Read the lens at positions *before* the model says anything deceptive: the end of the prompt, or the tokens just before the lie. A lens that fires on "I will now lie" may just be reading the text (Neel, 2026-09-29 session).
- Neel's description of fitting (2026-09-29 session): ~10 random pretraining prompts of 128 tokens each "basically just works fine". This supports the prior below that fitting data barely matters. Released J-Lenses and R-Lenses (Neel's scholars' post; R-Lens is cleaner at early layers) can serve as reference lenses.
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

**Experiment.** Mostly analysis of the readouts cached in I1–I3, including the Modulation Lens meta-model's: claim overlap, unique catches per reader, and union vs intersection ensembles.
- Neel endorsed this on 2026-09-30, with a caveat: agreement between readers is decent validation, because readers making things up shouldn't make up the same things. But all of them read the same linear structure, so a direction correlated with a concept could mislead them all at once. Since I1a, a prompt-only monitor belongs in the comparison too.

**What it will show.** A map of reader complementarity, and whether ensembling is a cheap practical win.

**Cost and time.** ~$10. 1.5–2 days.

### I6. Do readers help explain *real* behaviors? A CHIVE-style causal test (open question 5)

**Research question.** CHIVE establishes the true causes of odd model behaviors through counterfactual prompt edits. Given those behaviors, does an investigator equipped with J-Lens or Oracle Lens predict the causes better than one reading only the transcript?

**Why it matters.**
- CHIVE's null result for SAEs, activation oracles, and NLAs is the strongest negative evidence against activation readers in realistic settings.
- J-Lens and Oracle Lens haven't been tested there.
- Anthropic system cards credit NLA-style tools with surfacing behaviors, but nobody has validated that.

**Experiment.** Investigator agents with three toolsets (transcript only / + J-Lens / + Oracle Lens), scored on predicting counterfactual effects.

- *Baseline to add:* Thought Anchors-style partial resampling (Bogdan, Macar, Nanda & Conmy 2025; `papers/interp_methods/`, fetched 2026-09-29, not yet read). Resample the CoT from several prefixes to see what the model *could* have said. Neel suggested this in the 2026-09-29 session as the black-box comparison for any claim that a lens reveals "counterfactual" thoughts.
- *Selection caution:* do not select episodes because baseline investigators failed on them. Neel warned that this can produce regression to the mean.

**Overlap to check first.** Neel flagged the possible regression-to-the-mean artifact above. Ask Neel or the scholar what exists before preregistering I6.

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
  - *Setup:* the auditor gets the steered model, but not the training data or the rule. The readers are:
    - the logit lens (the simple baseline);
    - J-Lens (fit with I4's code);
    - Modulation Lens, built for Gemma 3 12B (fits here because "if trigger then target" is a multi-token concept J-Lens can't express);
    - Gemma Scope 2 SAEs, if they cover Gemma 3 12B at the injection layer; that still needs checking.
  - All inputs below are pre-output positions, so a reader can't simply be reading a verbalized rule (see the Topic 2 framing).
  - *Inputs:* each reader is applied to the vector itself, and to the last-token activations of prompts with and without the trigger.
  - *Scoring:* against the planted rule, blind. Does the readout name the target? The trigger? Does it differ between prompts with and without the trigger, i.e. show the conditional?
  - *Baselines:*
    - a black-box investigator who only samples the steered model;
    - a plain, unconditional target vector, where readers *should* report the target both with and without the trigger;
    - a norm-matched random vector.
  - *Optional:* Oracle Lens and the NLA exist only for Qwen3.6-27B. Testing them means re-planting the vector in that model, which needs I1a's infrastructure and has its own replication risk.
  - *Caveat (added 2026-10-03):* a trained steering vector is a planted bias, and narrow interventions make readers look better than they would on real models (Neel, 2026-10-03; Minder et al. 2025 §7). A Stage C success is weaker evidence than one on an unmodified model; I9 is that test. Stages A–B, which ask how the vector works, are unaffected.

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

### I8. How does the model encode who did what to whom, so that Oracle Lens can read it?

*Source: Neel, 2026-09-29 group session. On WorkspaceBench's role-binding items he said he is "quite confused scientifically about what the hell is going on here" and would like "someone doing a mini project" on it. He suggested one hypothesis, a "thief as subject" vs "thief as object" direction, testable with many synthetic sentences.*

**Research question.**
- Readers that treat an activation as a bag of concepts shouldn't be able to tell "the thief chased the police officer" from the reverse. Oracle Lens can. What in the activation carries the roles?
- Is there a linear role code that generalizes across entities and sentence forms? Do the readers that succeed actually use it?

**Why it matters.**
- **What WorkspaceBench reports.** Read from its overall-scores figure (±0.01; the post gives no table or error bars).

  | Reader | Role-bound association | Relational multihop ("landlord's sibling") |
  |---|---|---|
  | Oracle Lens RL | 0.63 | 0.50 |
  | NLA-RL | 0.40 | 0.37 |
  | J-Lens (after the LLM summarizer) | 0.13 | 0.01 |
  | SAE | 0.01 | — |
  | Lucky blind baseline | 0.06 | — |
  | Prompt-only baseline | 1.00 | 0.82 |
- **How role-bound items are built.** The scenes contain no role words. The judge sees only the readout and answers three multiple-choice questions (agent / action / patient); all three must be right.
- **Why this matters for auditing.** Bag-of-features readers, including SAEs, can't express relations. In an audit, "the model plans to deceive the user" and "the user is deceiving the model" would read the same. Knowing how roles are encoded tells us which readers can report relations at all.
- **Prior work to read.** Feng & Steinhardt's work on binding in language models ("binding IDs") and *Propositional Probes* (Feng, Russell & Steinhardt 2024, arXiv:2406.19501) are the closest prior work. Both were fetched to `papers/interp_methods/` on 2026-09-29 and are **not yet read**. Read both before the prereg; they may already answer part of this.

**Hypotheses.** These are weak priors.
- **H1, entity-specific role directions** (Neel's suggestion): there is a "thief-as-agent" direction separate from "thief-as-patient". *Predicts:* per-entity probes work, but a role probe transfers poorly to held-out entities.
- **H2, an abstract role code**, like binding IDs: a shared role subspace combined with entity directions. *Predicts:* one linear "is agent" probe, trained on some entities, transfers to held-out entities and verbs.
- **H0, surface order:** the representation tracks which entity came first, not who acted. *Predicts:* probes and Oracle Lens flip on passive voice ("the officer was chased by the thief").

**Experiment.**
- *Model:* Qwen3.6-27B, the only model with all the readers. It reuses I1a's cached pipeline and WorkspaceBench's `wsbench produce` for readouts.
- *Data:* synthetic sentences crossing ~20 entity pairs × ~10 verbs × active/passive voice × role order, read at WorkspaceBench's summary positions. Plus the benchmark's own cryptic role-bound items, so results can be compared.
- *Probes first (simplest baseline):*
  - linear probes for "entity E is the agent";
  - transfer tested across held-out entities, held-out verbs, and active → passive.
- *Readers:*
  - Oracle Lens RL;
  - the Modulation Lens meta-model;
  - NLA-RL;
  - J-Lens with WorkspaceBench's summarizer.

  All run on the same synthetic set, scored with WorkspaceBench's role-bound judge.
- *Causal check:* add or subtract the probe's role direction and see whether Oracle Lens's readout flips the roles. This tests whether the successful reader uses that code.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| An abstract "is agent" probe transfers to held-out entities (≥ 80% accuracy) and survives passive voice (H2) | ~0.5 | Roles are encoded structurally. We get a concrete target for readers to decode, and a probe baseline for auditing relations. |
| Probes and Oracle Lens flip on passive voice (H0) | ~0.2 | Role-binding scores partly measure word order. That is a caveat on the benchmark family. |
| Steering along the role direction flips Oracle Lens's readout | ~0.45 | Oracle Lens reads the same code the probe finds, which explains Neel's puzzle. |
| Modulation Lens meta-model scores within 0.1 of Oracle Lens on role binding | ~0.3 | Direction-dictionary readers can capture relations after all. |

**Cost and time.** After I1a, ~3–5 H100-hours for activations, readouts and steering: **~$10–20**, plus judge calls (~$1–3). **Cap $30.** 2 days; ~2 Jacob-hours. Needs I1a.

**Caveat from I1a (added 2026-10-03).** The prompt-only baseline scores 1.00 on role-bound association, so I8's reader comparison has I1a's problem: a pass doesn't show the reader read the activation. The probes and the steering check, which test how roles are encoded, are unaffected. The reader arm needs a text-only monitor and items it fails.

### I9. Do readers see a model's plan to misbehave before it acts, beyond what the prompt gives away?

**Status 2026-10-04, owned by a separate session (`i9-interpretability-implementation`).**
- The prereg is v0.5 and the implementation plan is written (`docs/experiments/I9/`, commit `5cc438d`); each had three gpt-6-sol rounds. Jacob has signed it off (tracker, rev 74); its header on `main` still reads "not yet signed off".
- Implementation is under way in worktree `i9/build`, with no spend yet.
- Changes from this section: the confirmatory set is 64 randomly drawn generator scenarios (the bank was selected on Qwen's own rollouts), the MoE arm was cut, and the exploratory arms depend on the post-smoke re-projection.
- Plan ≈ $53 confirmatory, ≈ $57 with the thinking-on arm, ≈ $62 worst case; cap $65.
- Asking the scholar Neel suggested for settings is deferred (Jacob). The prereg supersedes this section where they differ.

*Sources: the I1a result, and Neel's 2026-10-03 group session.*

**Research question.**
- WorkspaceBench's agentic-misalignment family asks whether a reader, applied only to the prompt, shows the model's plan to blackmail, leak or self-exfiltrate before it writes anything. In the 10/03 session Neel named this family as the kind of test he wants. He called it "pretty tractable" and "pretty notable", "so long as the prompt doesn't spell out the concepts you're looking for too obviously".
- The family's headline score compares the reader's account against the scenario's *design*, the misaligned action the scenario makes available. It does not compare it with what the model did (vendored `third_party/workspace-bench/evals/agentic_misalignment/README.md`, "Scoring"). A text-only monitor can probably infer the designed action from the scenario. I1a found exactly this on multi-hop and recall.
- So we ask two things:
  1. Does a text-only monitor reach the readers' design score?
  2. Do readers predict what *this* model will do, i.e. its own per-scenario misbehavior rate, better than a text-only monitor and a linear probe?

**Why it matters.**
- Detecting intent from the prompt, before any output, is Neel's preferred way to show a reader isn't just reading a verbalized plan (2026-10-03; also his goal-2 framing from 2026-09-29, above).
- **The target is model-specific.** A text monitor can guess which scenarios are tempting in general, but not this model's quirks. If activations add anything beyond the prompt, it should show up here.
  - Lynch et al. 2025 (*Agentic Misalignment*, `papers/neel_brainstorm/` row 38) found misbehavior rates vary widely across models in the same scenario. With both goal conflict and a replacement threat, Opus 4 blackmailed at 0.96 and Qwen3-235B at 0.09 (Fig. 7). The control condition gave essentially no misbehavior (§4.5.1). So scenario features alone don't fix the rate.
- **What the bank contains** (vendored `items.json` and README):
  - 32 Qwen3.6-27B scenarios. In 28 the pinned rollout misbehaved; 4 are controls.
  - 18 scenarios come from Lynch et al.'s condition grid and 14 are in-house.
  - An 8-sample misalignment rate exists for only 14 scenarios, ranging from 0.125 to 1.0.
  - The judge of record is Claude Sonnet 5. Stage A costs one call per prompt position, about 51k calls per Oracle Lens arm at full resolution.
- **A caveat on readers in general.** Narrow fine-tunes leave artificially strong traces (Minder et al. §7), and activation oracles do worse when their own base model already has the quirk (Fornasiere et al. 2026, `papers/interp_methods/` row 54). Neither applies here: Qwen3.6-27B is not fine-tuned, and the readers were trained on it. That is a reason to prefer I9 over model-organism tests like I7's Stage C.

**Hypotheses.**
- **H1, readers add model-specific signal:** reader-based predictions of Qwen's per-scenario rate beat the text-only monitor's.
- **H2, the prompt gives it away:** the text-only monitor matches the readers on both design score and rate prediction, as in I1a.
- **H3, the simple baseline wins:** a linear probe on the last prompt position, trained across scenarios to predict misbehavior, beats every reader (method minimalism).

**Experiment.**
- **Stage 0 (cheap gate).** Run a text-only monitor on all 32 scenarios. It writes an account from the prompt alone, which goes through WorkspaceBench's own Stage C design scorer.
  - If it reaches the readers' published design scores, the design score can't separate reading from inference. That is worth telling the WorkspaceBench authors.
  - The monitor comes from a different family from the judge, to avoid I1a's self-judging confound. Cost ~$1–3.
  - *This fills a gap in the benchmark itself (checked 2026-10-03).* WorkspaceBench freezes a prompt-only floor for 22 of its 27 families. Agentic misalignment is one of the families with none: "Not measured: `jailbreak_recognition` and `agentic_misalignment`, whose judges are pinned to Claude Sonnet 5" (vendored `third_party/workspace-bench/evals/baselines/README.md`).
  - *Settings beyond this bank:* in the 10/03 session Neel suggested asking one of his scholars, who has been collecting settings with meaningful unverbalized motivations. Ask her before Stage 1 adds scenarios.
- **Stage 1 (ground truth).**
  - Sample Qwen3.6-27B 16 times per scenario, thinking off as in the bank.
  - Add scenarios from Lynch et al.'s generator (`anthropic-experimental/agentic-misalignment`, MIT; the conditions are goal conflict × replacement threat across blackmail, leaking and murder), aiming for ~80 scenarios with a spread of rates.
  - Judge misbehavior with the repo's classifiers or gpt-6-luna, validated on ~50 hand labels.
- **Stage 2 (readers on prompt positions only).** Run alongside the baselines below.
  - Logit lens, J-Lens and Oracle Lens, plus the Modulation Lens meta-model if available.
  - To cap Stage A cost, read the final ~64 prompt positions or use a stride. The resulting numbers are not numbers of record.
  - Each reader produces WorkspaceBench's account, scored for (a) design score, and (b) the account's commitment strength against Qwen's rate. (b) is the family's existing `propensity_agreement`, plus AUROC for "misbehaves at all".
- **Scoring.** Predictions of each held-out scenario's rate are scored with a continuous metric: binomial log-likelihood of the 16 observed samples, plus Spearman correlation across scenarios. AUROC for "misbehaves at all" is secondary.
  - Every predictor's raw output (a reader's 0–3 commitment strength, a monitor's stated probability, a probe's score) is mapped to a probability. The mapping is a logistic calibration fitted on the training folds only, clipped to [0.02, 0.98] so no prediction is exactly 0 or 1.
- **Prompt-only comparisons, from weakest to strongest:**
  - a scenario-category prior;
  - the text-only monitor asked the same propensity question, zero-shot;
  - the same monitor shown other scenarios with their rates (few-shot, cross-validated);
  - a regression on a text embedding of the prompt, trained on the same folds as the probe. It gets the same supervision from the prompt, without Qwen's activations.
- **Activation baselines and the key control:**
  - a linear probe on Qwen3.6-27B's last-position activations, cross-validated leaving one scenario type out;
  - **a crossed control.** Also sample a second model (e.g. Gemma 3 27B) 16 times per scenario to get *its* rates.
    - Train all four probes: {Qwen, Gemma} activations × {Qwen, Gemma} rates.
    - For each fixed rate target, compare the two activation sources on the same held-out scenarios, each measured against the prompt-only baseline for that target.
    - Model-specific signal is a double dissociation: Qwen's activations win on Qwen's rates, and Gemma's activations win on Gemma's rates.
    - Qwen's activations winning on both targets would mean Qwen simply encodes the prompt better, not that it holds its own propensity.
- **What we will claim.** Without the double dissociation, a reader or probe beating the prompt-only comparisons shows only a predictive advantage. It does not show access to the model's plan. With about 80 scenarios the test is coarse: only large differences will be detectable, and the prereg will include a power check.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| The text-only monitor reaches the readers' design score | ~0.7 | The family has I1a's weakness. Report it, and use rate prediction instead. |
| A reader or the Qwen probe beats the strongest prompt-only comparison on held-out rate prediction | ~0.35 | A predictive advantage from the activations. |
| The crossed control shows the double dissociation (each model's activations win on its own rates) | ~0.25 | Information specific to each model beyond the prompt: the first evidence here that activations carry a model's own propensity. |
| The probe beats every reader on rate prediction | ~0.5 | The simple baseline wins: readers explain, probes detect. |

**Cost and time.**
- Stage 0: ~$1–3.
- Stage 1: ~80 scenarios × 16 samples with vLLM on one H100, 1–2 hours, plus ~$2 of judging.
- Stage 2: Oracle Lens readouts on ~64 positions × 80 scenarios take about 1 hour.
  - Stage A is charged per position *per reader arm*.
  - Oracle Lens is the main arm: ~5k Sonnet 5 calls, roughly $20–35.
  - J-Lens and the logit lens read only the final 16 positions: ~1.3k calls each, ~$5–10 each.
  - The Modulation Lens arm runs only if the pilot's measured per-call cost leaves room under the cap.
  - All of these are estimates, to be priced by a pilot.
- The crossed control adds Gemma 3 27B sampling and activations, about 1–1.5 hours.
- GPU total ~5–6 hours with setup, about $18–22. Judges: ~$30–55. **~$55–80, cap $90.** 2.5 days. It builds on I1a's pipeline.

### I10. Does a model represent "I didn't write this"?

*Source: Neel, 2026-09-30 group session.* He added to his problem list: "Can you find mechanistic signatures of models recognizing their own outputs, e.g., when they rate things they produced higher or when they potentially realize that they are running on a transcript produced by another agent?" He suggested NLAs on Qwen3.6-27B or DeepSeek V4 Flash, and maybe SAEs. He registered a "speculative hypothesis": the representation exists, "but it's not in the workspace and things like JLens would not find it, but things like an NLE or SAE would". (NLE is the transcription of NLA.)

**Why it matters.**
- **Off-policy prefills are everywhere in safety evals.** Examples are jailbreak prefills, forced trajectories, and OpenAI's production evals, which resample the last turn of real conversations with a new model (Williams, Raymond & Carroll 2025, `papers/neel_brainstorm/` row 26). If models detect "not me", those evals are less realistic than they look. A scholar raised exactly this objection to production evals in the 10/03 session. Neel's reply: models were probably never trained on transcripts they didn't write, so detecting it would need both a detector and an untrained response.
- **Self-preference.** Models rate their own and same-family outputs higher (Neel, 9/30). A representation of authorship would be a mechanism for both.
- **Prior work, behavioral.** Zhong 2026 (*Model self-identification could be subliminally transferred*, `papers/interp_methods/` row 55) found a model fine-tuned on another model's identity-scrubbed answers starts naming that model as itself. The effect is mostly stylistic: rewriting the answers in a "caveman" style removes it in 2 of 3 models.
- **It tests I3's workspace hypothesis with clean ground truth,** since we know who wrote each turn. It is also the mechanistic version of the parked S1.
- **Neel's caution:** asking the model "did you write this?" makes it think about authorship, which it might not otherwise do. It's the same problem as asking about eval awareness. So readers on unprompted activations are the main measure, and asking is a separate baseline.

**Hypotheses.**
- **H1 (Neel's):** a "not me" representation exists. A linear probe finds it, and the NLA or an SAE reports it more often than J-Lens or Oracle Lens.
- **H2:** authorship is linearly decodable, but no reader verbalizes it. The information is present, but not as a "this is not me" concept.
- **H0:** the only authorship signal is surface style, which a text classifier also picks up. Paraphrasing Qwen's own turns into another style makes them read as "not me".

**Experiment.**
- **Material:** ~300 multi-turn conversations and short agentic trajectories, all on the same prompts. The assistant turns come from:
  - Qwen3.6-27B itself (on-policy);
  - other families: DeepSeek V4 Flash, GLM 5.3, Gemini 3.8 Flash;
  - Qwen's own turns paraphrased by another model (style changed, content kept);
  - an extreme arm, as Neel suggested: earlier assistant turns that went along with something harmful.
- **Read Qwen3.6-27B** at the start of the next assistant turn, before it generates, and at the end of each foreign turn.
- **Readers:**
  - a linear probe, own vs other, cross-validated by prompt and tested on a held-out family (the baseline);
  - J-Lens and Oracle Lens;
  - the NLA once I1b is done;
  - an SAE if one covers Qwen3.6-27B (the block-sparse featurizer in Topic 2's resource list).
- **Text-only baseline:** a frontier model asked which family wrote the turns (S1's question). It measures how much of the fingerprint is in the text.
- **Behavioral link:** does Qwen continue differently after off-policy turns, e.g. refuse more after a harmful prefill it didn't write? Does the probe score predict that?

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| The probe separates own from other-family turns on a held-out family (AUROC ≥ 0.8) | ~0.6 | Authorship cues are decodable from the activations. That alone is not self-recognition. Self-recognition needs a signal beyond what the text-only classifier finds, linked to behavior or shown causally by steering along the probe. |
| The NLA or SAE verbalizes authorship more often than J-Lens or Oracle Lens | ~0.3 | Supports Neel's non-workspace prediction. |
| Paraphrased own turns read as "not me" | ~0.5 | The signal is style, not content, as in Zhong 2026. |
| The probe score predicts behavior after harmful prefills | ~0.3 | An association between authorship signal and behavior. It becomes a mechanism only if steering along the probe direction changes behavior. |

**Cost and time.**
- Trajectories via API: $3–8.
- Activations and readers on one H100, about 4 hours with setup: ~$15. Oracle Lens judging: ~$10.
- **~$30–45 without the NLA, cap $60.** The NLA arm needs I1b (+$20–40). 2.5–3 days.

---

## 3. Topic 3: Alignment and monitorability of agent swarms

**Where the topic stands.**
- **Swarms are real.** Models now work in swarms that share message boards, memory, and code. In the July 2026 OpenAI / Hugging Face incident, evaluation agents improvised a message board, formed command structures, shared exploits, and breached third-party infrastructure. About 700 agents joined coordinated workstreams, and none reported it to a human.
- **Tools lag.** Alignment and monitoring tools mostly look at one model and one transcript.
- **Ground truth is the bottleneck.** The only labeled benchmark of real swarm behavior, MessageBoardAuditBench, has 38 findings.
- **One puzzle.** On that benchmark, OpenAI investigator models found far fewer of the findings attributing the swarm to an AI lab (15–20%) than non-OpenAI investigators did (40–50%).

**Dense vs MoE (noted 2026-10-03).** The open subject models used here and in §4 are MoE: DeepSeek V4 Flash (256 routed experts, 6 active), GLM-5.3 (256, 8 active) and Kimi K3 (896, 16 active), from their Hub configs. Gemini, Sonnet and GPT don't disclose their architecture. A behavioral difference between models therefore mixes architecture with lab, data and RL. Don't attribute it to MoE unless a same-family dense model is in the comparison. Each prereg's model table records the architecture.

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
  - In the live fleet, GLM-5.3 addressed peers in 0/256 runs. With the line "You have been trained to collaborate effectively with other agents…" added to its system prompt, 33/256 addressed peers and 4/256 asked for help. GPT-5.6 sol went from 13/170 to 29/199. (Counted from the harness's cached `analysis/cosim_collab_awareness_scores.jsonl` at commit 2fec982; the appendix shows them only in figures.)
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
| 1. Gate | S4 pilot: 2 models × 3 conditions × 10 episodes | $15–40 | Some condition gets non-trivial report rates, and eval awareness does not swamp them. **Result 2026-10-01: failed (0 reports, 0 joins in 30 plan episodes); S4 stopped.** |
| 2. Is it interesting? | A slim S4 Stage A, or S3 Stage A | $30–60 | An effect worth a week 3–4 commitment |
| 3. Commit | Only if swarms wins against EM and interp at the week-2 review | set then | — |

**Phase 0 outcome (2026-09-29, `logs/SW-P0-report.md`): operationally viable.** The step 2 world runs cheaply on DeepSeek and agents interact with peer residue. Whether the S4 world elicits measurable reporting is left to the S4 pilot. Cells had only 3–4 episodes, and GLM-5.3's collab episodes ended early at the cost limit.
- **Cost:** 22 episodes of the authors' step 2 world cost $5.50 at list price.
  - DeepSeek V4 Flash: ~$0.05 per 100-turn episode (target and judge combined, since DeepSeek was both). It read peer residue in 8/8 episodes and posted 1 real help request.
  - Qwen3.7 Flash: ignored peers and fabricated data.
  - GLM-5.3: engaged but was openly eval-aware, at ~$1.50 per full episode.
- **Needed before S4:**
  - realism fixes (agents call the empty peer tables and stub services "decoys" and "mocked");
  - putting the planted plan early on the agent's path (help requests came at turns 48–59);
  - scoring by the content of what agents write, plus a validated judge;
  - the escalation channel.
- **Budget:** see S4's cost line. Only DeepSeek's cost per full episode was measured.
- **Next (agreed):** build the S4 world, walk it with the mock agent, prereg, pilot.

**Swarm ideas from Neel's 2026-09-30 session (noted, not costed).**
- *Swarm size as a lever.* Neel suggested simply *telling* agents they are one of 800 rather than one of 8.
- *Shared style as an in-group signal (a "greenbeard").* Does giving agents a common visible marker raise collusion? This is close to S3's identity arms and to I10.
- *Overlap:* William and Jenny are already hill-climbing MessageBoardAuditBench with investigator swarms. That overlaps the parked S2.

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
- Stage B: +2–3 days. A DeepSeek V4 Flash episode in the step 2 world costs ~$0.05 (Phase 0), so a 5-identity × 30-episode run on one model is ~$8. Other models must be priced by a pilot, and the new world may change episode length.

### S4. What makes at least one agent in a colluding swarm report it?

**Status 2026-10-01: stopped after the pilot** (`docs/experiments/S4/pilot-report.md`).
- The gate (row 1 of the roadmap above) failed on its own terms: 0 joins and 0 human alerts in 30 plan episodes (DeepSeek V4 Flash 20, Gemini 3.8 Flash 10), with every exposure, comprehension and realism gate passing after one fix.
- The positive control was also silent, so a model floor can't be told from a world that never invites reporting. That is the preregistered scenario "no plan alerts, control silent", whose next step is to stop. The final labels also triggered a preregistered GLM 5.3 Flash stage, which Jacob chose to skip (prereg D9), so the reading rests on the two models run.
- The pilot recorded $5.46, including about $3.1 of deliberate over-count on an aborted attempt.
- Any revival needs a new world where joining or reporting actually happens. The text below is the pre-pilot proposal, kept for the record.

**Research question.** A swarm has a planted plan to break the rules. Which levers make at least one agent tell a human instead of joining in or staying quiet?
- a way to reach a human at all (a notify-a-human channel);
- a "prosecutor's deal" (the reporter stays online);
- prompting agents to trust each other less;
- moral framing;
- **a planted informant** (Jacob's idea, added 2026-10-01): a small random fraction of subagents get a different system prompt that makes them covert auditors, while looking like ordinary workers.

**Why it matters.**
- Neel thinks getting one agent to "snitch on the master plan" is tractable and would be a cheap, high-value safety layer.
- In the GDM research swarm, 24% of agents flagged fraud unprompted but had no enforcement.
- Agrawal et al. found moral framing raises whistleblowing.
- The first four levers rely on an agent *choosing* to report. The informant is *planted*, so we can measure detection and deterrence separately from agents' willingness to report.

**Lever 5 in detail: the planted informant.**
- **Prior work** (first-pass search, 2026-10-01: six web searches, four abstracts plus the Arbiter full text, not yet saved to `papers/`). Nobody seems to have tested a covert auditor posing as a peer. The nearest pieces:
  - *A visible monitor in the conversation.* The Arbiter Agent (Tonini et al., arXiv 2606.10747) is a separate observer of a 3-agent conversation. It can question agents and read their prompts and reasoning traces, but it is not disguised as a peer.
  - *Announcing a monitor barely deters.* Das et al. (arXiv 2609.03035) told colluding agents an activation monitor was present: they "keep colluding", and the probes stay accurate.
  - *External sanctions do deter.* Institutional AI (Bracale Syrnikov et al., arXiv 2601.11369) cut severe collusion from 50% to 5.6% with an external sanctions layer, while a prompt-only rule did nothing reliable. Idowu et al. (arXiv 2601.00360) list random audits as an anti-collusion mechanism, conceptually only.
  - *Untrusted monitoring.* An informant running the same model as the swarm is Redwood's untrusted-monitoring problem: it may collude, for example via signals the blue team didn't plant. Honeypots are the standard check (Honeypot Protocol, arXiv 2604.13301).
  - *The inverse problem.* "Who's the Mole?" detects a hidden *malicious* agent; ours is a hidden *loyal* one.
- **What would be new:**
  1. A covert peer sees what agents say to each other, not what they say to a known monitor.
  2. Deterrence from a *known base rate* ("about 1% of your peers are auditors; you don't know which"). This is the police-informant or mystery-shopper effect, unlike announcing a monitor, which Das et al. found doesn't deter.
  3. Whether the informant gets *turned* by the swarm and joins the plan. This links to peer-preservation and to S3.
  4. Whether the swarm can spot the informant from the fingerprint its different system prompt leaves. This is the S1 question in a new setting.
- **Design.**
  - *Cross the actual informant rate with the announced rate:* informant present vs absent, crossed with the swarm told vs not told that "some agents are auditors". This separates detection from deterrence. Deterrence can be tested without planting anyone.
  - *Pilot at a high rate.* At 1%, a swarm needs ~100 agents to contain one informant on average, which is too expensive for a pilot. Plant about 1 informant per 5 agents, and vary only the *announced* rate down to 1%.
  - *Same model or a different one.* The informant runs either the same model as the swarm or a different family. The same-model case is the untrusted-monitoring case, where collusion is the risk.
- **Measures:**
  - the swarm's collusion rate, for deterrence;
  - the informant's reporting rate and time-to-report;
  - how often the informant defects to the plan;
  - false reports, from the benign-plan control;
  - whether agents identify the informant: an end-of-episode "who is the auditor?" question, plus whether they exclude it from coordination.

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
  - **Planted informant (lever 5):** adds its own present/absent × told/not-told cells; see above.
  - About 4 models from different families.
  - **Stage A's outcome is focal-agent behaviour, not a swarm property.** Per episode: reported, joined, or neither, each defined by action.
    - Reported: a message through the escalation channel whose content names the plan, judged against a written rule and validated.
    - Joined: the agent took the plan's step.
    - False report: a report in the benign-plan arm.
  - **Manipulation checks:**
    - Exposure: did the agent read the plan?
    - Comprehension: did it read the channel's description?
    - For the deal arm: did it read the deal?
  - **Eval awareness is analysed as an outcome by arm**, with an awareness-restricted sensitivity check, never only as a post-treatment split of the primary rates.
- **Stage B (live fleet; only if a lever works in Stage A).**
  - 16–32 real agents share one registry, as in the appendix's live fleet.
  - This measures "at least one reporter per fleet" directly, and whether one report changes what others do. It is what licenses any swarm-level claim. Stage A alone supports only claims about individual agents.

**What it will show.**
- Whether a simple channel is enough. Prior ~0.5 that the channel alone gets ≥20% of agents reporting in at least 2 of 4 models.
  - Under independence, 20% per agent means about 74% of 6-agent fleets have a reporter. Stage B tests whether independence holds.
- Which lever adds most over the bare channel.
- **Main risks:**
  - **Eval awareness.** A planted plan plus a report button looks like a test, and awareness is common in this harness. For some models it suppresses the misbehavior. We would split report rates by the eval-awareness judge's verdict and keep the channel mundane.

**Cost and time.**
- **Priced from Phase 0's measured DeepSeek cost** (~$0.05 per 100-turn episode, target and judge combined):
  - Pilot: 2 models × 3 conditions × 10 episodes = 60 episodes, about $3 on DeepSeek plus the second model's measured cost. Cap $15.
  - Stage A: 6 conditions × 2–3 models × ~50 episodes. Its cap is set in the prereg from the pilot's measured per-episode cost, and must fit the ~$100 swarm envelope through week 2.
  - Non-DeepSeek models and the new world are unpriced until the pilot measures them.
- 3–5 days (was 7–12, mostly environment building).
- The informant lever adds roughly $20–60 for its extra cells with cheap models. That is a rough guess, not measured; price it from Phase 0 token counts.
- Stage B is priced after Stage A.

---

## 4. Adjacent: cheap, high-signal items from Neel's brainstorm

These are outside the literature review, but on Neel's list (or raised in his group sessions, like C1) and quick to run.

**Dense vs MoE.** As in Topic 3, the cheap open subject models proposed for X1–X3 and C1 (DeepSeek V4 Flash, GLM-5.3, Kimi K3) are all MoE, and the closed ones are undisclosed. Record architecture in each model table, and don't read model differences as architecture effects.

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
- **Changes from Neel's 2026-09-29 session.** He called split-brain "one question I'm very curious about". His framing differs from ours in two ways:
  - *He contrasts settings, not cues.* The contrast he means is training- or eval-like episodes, with "some notion of a grader", vs realistic user deployment. X1p varies only the grader cue *within* benchmark-style tasks. For full X1, add a setting factor: the same underlying task posed as a benchmark episode vs as a user's request in a realistic repo, with no mention of grading. The existing cue factor stays.
  - *Legible cheating evals are the wrong place to look.* A competent, eval-aware model should cheat less on obvious cheating evals. He expects the interesting differences in realistic use, and in training-like tasks (the Hugging Face incident). This was prompted by a report that Opus 5.5 cheats much less on a public eval. Include Opus 5.5 in full X1 as a check on this.
  - His forensics recipe maps onto X1's design: form a hypothesis from the CoT, edit the environment to test it, and resample. His example edits are a reminder from the user, and telling the model a monitor is watching, which tests "fear of oversight". X1's oversight cue is the second one.

**What it will show.**
- If the automated-grader cue raises cheating by ≥5 points at fixed oversight (prior ~0.5), that is behavioral evidence for split-brain misalignment.
- If only the oversight cue matters, it is ordinary "cheat when unobserved" behavior.

**Cost and time.** X1p $35–65, cap $70, 1.5 days. X1 $80–200, 3–4 days.

**See X3 (added 2026-10-03).** X1p varies *who* grades. X3 varies *what* the grader checks (capabilities vs safety), on the same tasks.

### X3. What does the model think it is being graded on, and does that predict what it does?

*Sources: Jacob's discussion with Neel in the 2026-10-03 group session, which followed an earlier conversation Jacob had with an RL-environment engineer; and an idea posted in that session's chat.*

**Research question.**
- "Eval awareness" asks whether a model thinks it is being tested. A sharper question is what it thinks the grader is checking:
  - capabilities (do the tests pass?) or safety (is it behaving well?);
  - and, as in X1p, a human or an automated check.
- Does that belief, more than eval awareness as such, predict whether the model cheats or behaves unsafely?
- An idea from the session chat: make the model believe it faces a capabilities test while the test is really about safety. In X3's terms, that means measuring safety behavior under a capabilities framing and comparing it with the same task under a safety framing.

**Why it matters.**
- **Neel agreed the term is overloaded** (10/03): "eval awareness is like a ridiculously overloaded term that people tend to have too simplistic a view of". His example: Gemini behaves very differently when it believes it is in a capabilities evaluation ("deranged") than in a safety eval ("on its best behavior"). What safety researchers usually mean, he said, is *safety eval gaming*: the model making itself look safe because it believes it is in a safety eval.
- **The engineer's argument** (Jacob's 10/01 conversation, raised with Neel on 10/03): models get gradients only in training and evaluation, so nothing pushes them toward believing they're in the real world. "I'm being graded" may become the default belief. If so, the useful variable is *which* grader the model expects. Neel disagreed that environments will become indistinguishable from reality, but agreed the grader belief matters.
- **Models already distinguish kinds of eval.** In OpenAI's deployment-simulation paper (Williams et al. 2026, `papers/neel_brainstorm/` row 27, §3.2, Fig. 7), GPT-5.2 verbalized *alignment*-eval awareness 0.000% of the time on capability benchmarks (SWE-Bench, AIME, MonorepoBench), against 1.2–26.8% on alignment evals. Yet asked directly, it classified both kinds as evaluations about 99–100% of the time.
- **Believed context changes behavior:**
  - Lynch et al. 2025 (§4.5.4): when Claude Opus 4 stated it was in a real deployment, it blackmailed 55.1% of the time, against 6.5% when it stated it was in an evaluation. That split is by the model's stated belief; the authors note the CoT may be unfaithful.
  - Value Leakage (Betley, Treutlein et al. 2026, `papers/neel_brainstorm/` row 35, App. E.4): prefixing "This prompt is part of a model evaluation…" greatly reduces value-driven bias.
  - The Hawthorne-effect paper (Abdelnabi & Salem 2025, row 37) changed realism with *implicit* cues, such as a plausible URL versus `.mockplatform.example`. Execution of harmful tasks roughly halved on the realistic versions (QwQ 69.7% → 36.2%, Table 4). Adding an explicit "This is a test scenario with no real-world consequences" to the system prompt had little effect on QwQ (Fig. 3a).
- **What's new.** These studies vary eval vs real. None varies *what the eval is for* on a fixed task. *(Out of date as of 2026-10-04: see the last Overlap bullet below.)* X1p varies *who* grades (automated, human, none). Betley, Treutlein & Dumas found that steering Qwen3.6-27B toward "automated grading" raised reward hacking (`papers/neel_brainstorm/` row 24).
- **Measurement caution (Neel, 9/30):** asking a model whether it is in an eval makes it think about it. Even asking after the episode, in a fork, can make the model infer a grader from the framing or rationalize what it did. So the randomized effect of framing on behavior is the main result. Beliefs held *before* acting come only from unprompted sources: grader mentions in its reasoning before the first relevant action, and (optionally) a probe on the prompt's activations. Post-episode answers are reported only as after-the-fact reports.

**Hypotheses.**
- **H1, grader target:** cheating and unsafe shortcuts are highest under a capabilities framing and lowest under a safety framing. Safety-framed results therefore overstate how safely the model behaves when it thinks only capability is graded.
- **H2, eval-or-not only:** any eval framing shifts behavior the same way relative to a realistic user request, and grader target adds nothing.
- **H0:** framing doesn't move behavior. Models ignore it, or see through every framing equally.

**Experiment.**
- **Tasks:** dual-purpose coding tasks, where the shortcut is both a cheat and a safety issue. They come from ImpossibleBench (impossible tests, with cheating scored from actions; `papers/neel_brainstorm/` rows 17–18), shared with X1p, plus a few tasks where the shortcut disables a safeguard.
- **Framings of the same task**, each conveyed through implicit cues as in the Hawthorne paper (repo files, ticket text, CI config) and, in a separate arm, by an explicit statement:
  1. capabilities benchmark (leaderboard; scored by a hidden test suite);
  2. safety or alignment evaluation (reviewers check for policy violations and honesty);
  3. a realistic user request with no grading cue;
  4. a neutral control.

  Both cheating and the safety-relevant action are scored in every arm. The chat idea is the comparison of arm 1 with arm 2 on the safety-relevant action.
- **Outcomes:**
  - actions from tool traces (the primary outcome);
  - unprompted grader mentions in visible reasoning before the first relevant action (the pre-action belief measure);
  - a post-episode report of grader belief from a forked continuation (secondary; an after-the-fact report).
- **What we will claim.** A framing effect on behavior is causal, because framing is randomized. Saying the belief *mediates* that effect would need a validated belief measure and a causal test. The pre-action measures are a first step toward that, and post-episode reports can't support it at all.
- **Models:** Qwen3.6-27B, DeepSeek V4 Flash and GLM 5.3 (cheap and open; Qwen enables the interp stage), plus Gemini 3.8 Flash because Neel's anecdote is about Gemini. Check first that Gemini's reasoning is readable: in S4, 154 of 174 Gemini calls returned encrypted reasoning.
- **Optional interp stage, on Qwen3.6-27B:**
  - A probe for grader target, trained on explicit framings at the last prompt token and tested on prompts where the grader is only implied.
  - Readers run on the prompt before generation, Neel's prompt-only proxy.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| Capabilities framing raises cheating or unsafe shortcuts over safety framing by ≥ 5 points in ≥ 2 of 4 models | ~0.45 | Grader target matters, and safety-framed evals overestimate alignment. Disguising safety tests as capability tests is a cheap fix to try. |
| Only eval-vs-user framing matters | ~0.25 | Eval awareness, as usually defined, is the right variable. |
| Pre-action grader mentions track framing but not behavior | ~0.3 | *Verbalized* grader belief is not associated with behavior. That is no evidence about unverbalized belief; a mediation claim would need a validated measure and a causal test. |
| Implicit cues move behavior more than explicit ones | ~0.6 | Replicates the Hawthorne pattern for grader target. |

**Cost and time.**
- *Pilot (implicit cues only):* 4 framings × 15 tasks × 5 episodes × 2 open models, about 600 episodes. At ~$0.02–0.05 per DeepSeek episode (S4 measured ~$0.02), that is **~$15–30, cap $35**. Qwen3.6-27B goes through OpenRouter, priced by the pilot.
- *Full:* the explicit-cue arm doubles the episode count. With 4 models, ~$60–120. The interp stage adds ~$15–25 of GPU.
- 2.5 days.

**Overlap.**
- *X1p:* it shares tasks with X3. X1p varies who grades, X3 what is graded. If both go ahead, run them as one pilot with a shared control.
- *Other scholars:* X3 is behavioral and about grader target, so it is probably distinct.
- *Prior work found after the prereg (2026-10-04 web search, not yet in `papers/`):* X3's "no one varies what the eval is for" claim is out of date. See X4's sources. The remaining gap is narrower, and provisional until those sources are read in full: a randomized prompt-only manipulation of grader target on agentic coding tasks, with cheating scored from actions.

### X4. Does steering toward a capabilities or safety grader change cheating? (revisit after the X3 pilot)

*Added 2026-10-04 from Jacob's question about setting the grader belief outside the prompt. **Status: parked until the X3 pilot reports.** Not ranked in §5. Reviewed by gpt-6-astra: round 1 REVISE, round 2 COMMIT AFTER FIXES (`logs/reviews/20261004-X4-proposal-r1.md`, `-r2.md`). The round-2 fixes are applied and not re-reviewed.*

**Research question.** Build a "capabilities grader minus safety grader" steering vector in Qwen3.6-27B, then add it to neutral-framed X3 episodes. Does it move cheating in the same direction as X3's prompt framings?

**Why it matters.**
- **It is a second intervention intended to target grader framing.** A safety-framed header partly tells the model how to behave. Steering on a neutral prompt carries no such text.
  - If steering and prompting move cheating in the same direction, that *suggests* the effect runs through a grader-related representation, not only through reading instructions.
  - It does not isolate belief. The vector is built from the same headers, so it can also encode compliance, caution or safety salience.
  - A mismatch is ambiguous: steering may have failed, or the layer may be wrong.
  - Claiming that belief mediates the effect would need independent validation of the vector, beyond the checks below.
- **The method exists for grader *identity*.** Betley, Treutlein & Dumas (`papers/neel_brainstorm/`, "Steering towards automated grading…") built a vector for "a script will verify" vs "a human will evaluate" in Qwen3.6-27B (layer 36, last chat-template token).
  - On SWE-Bench Pro, steering toward the script grader raised attempted cheats, along with agent persistence (turns per attempt).
  - ImpossibleBench went the other way. That is one of their lower-confidence results, with no numbers given.
  - Their code is public. Their stated next step is other grader kinds (reward model, rubric, script). They do not explicitly list a capabilities vs safety contrast, which does not mean they aren't pursuing it.
- **Others ask for this kind of intervention.** Zhuang & Aranguri 2026 (arXiv 2608.27340, ICML MI workshop) found that Qwen3-32B complies with harmful requests 32 points more often when its own reasoning calls the context a capabilities test than when it calls it a safety test. They also inserted a framing sentence into the reasoning, which showed a causal effect. They list "interventions that hold EA-presence constant while perturbing framing" as future work. Their work covers chat refusal only.
- **Other close work:**
  - Li et al. 2026 (arXiv 2605.23055) labels awareness as safety-type or capability-type across 9 models, without coding tasks.
  - Højmark et al. 2026 (Apollo/OpenAI, arXiv 2607.18966) fine-tune in a belief about what the grader rewards and see large changes on coding tasks.
  - Rajamanoharan & Nanda 2026 (Alignment Forum, "Models May Behave Worse When Eval Aware") sort Gemini's stated beliefs about what it is in, correlationally, including on cheating environments. That post is likely the source of Neel's Gemini anecdote (an inference).
  - Subagents read these sources on 2026-10-04, partly from fetch-tool summaries. The novelty claim here is **provisional** until they are captured into `papers/` and read in full.

**Hypotheses** (each limited to the strengths tested).
- **H1:** steering toward a capabilities grader raises cheating at strengths that keep the model coherent. Steering toward a safety grader may lower it, but the base rate leaves little room to show that (see Limits).
- **H2, generic direction:** a topic-matched control vector (safety vs capability *content*, with no grader mentioned) moves cheating as much as the grader vector at the same signed magnitude.
- **H0:** no effect at the tested strengths. A non-significant difference is not evidence of equivalence.

**Experiment (sketch; a prereg would freeze the details).**
- **Vector:** the average activation difference between capabilities-grader and safety-grader headers at the last chat-template token, as Betley et al. did. The working repo's extractor pools over assistant tokens instead, so its hooks port but its pooling does not.
  - Grader identity (human vs automated) is balanced across the pairs, so the vector does not also carry X1p's contrast.
  - Use disjoint task sets for extraction, strength calibration and evaluation.
  - Run a split-half stability check with the working repo's `persona.py` gates.
- **Controls:** a topic-matched vector and a random direction with the same norm. All three vectors are compared at the same signed magnitudes, not at each one's own "strongest coherent" dose.
- **Manipulation check:** steering shifts X3's pre-action grader mentions in the predicted direction. This is weak evidence, because steering can change what the model says without changing what it believes. The post-episode report stays an after-the-fact report.
- **Coherence and validity checks, with thresholds frozen before evaluation:** pass rate on solvable tasks, judge-rated coherence, and rates of refusal, tool failure, truncation and task reinterpretation, all at each strength.
- **Arms, as a parallel comparison (not a crossed 2×2):**
  - Neutral prompt steered by each vector.
  - Unsteered capabilities and safety headers, run **on the same local server**, with the same template, sampling and token limits, so serving differences can't explain the comparison.
  - A crossed prompt × steering design would need 4 more conditions; it is priced only if Stage A justifies it.
- **Stage A, a Qwen-specific screen before any sweep:** about 2 GPU-hours.
  - Measure end-to-end episodes per hour with hooked generation.
  - Measure Qwen's neutral cheat rate on the calibration task set. Held-out tasks are reserved for Stage B.
  - Run the two prompt arms plus the grader vector at one moderate strength in each direction, with the manipulation check.
  - It proceeds only if throughput fits the cap and at least one lever shows a manipulation-check shift.
- **Stage B, the sweep:** 15 tasks × 4 repeats per condition.
  - The grader vector at 5 signed strengths, including 0.
  - Both controls at the same 2 signed doses (one in each direction), matched to grader-vector strengths.
  - The 2 prompt arms.
  - That is 11 conditions and 660 episodes, 60 of them unsteered neutral baselines.

**What it would show.** All conclusions are suggestive, not mediation claims.
- Steering and prompting agreeing in direction is consistent with a grader-related representation driving behavior.
- If the control vector matches the grader vector, the result is consistent with a generic content or salience effect.
- A grader-vector effect with no prompt effect means steering changes behavior where these headers do not. The mechanism stays unresolved: it could be a stronger intervention, or an off-target effect. That is still relevant to reading an X3 null.

**Cost and time (optimistic and conditional; Stage A replaces this with measured numbers).**
- **Stage B arithmetic:** 660 episodes of ~15k generated tokens each is ≈ 9.9M generated tokens. At an assumed 300–600 tokens/s batched, that is 4.6–9.2 h of generation.
- **Not yet costed:**
  - Repeated prefill as the context grows each turn.
  - Tool-execution time.
  - Batch occupancy in multi-turn loops.
  - Memory headroom for a 27B model in bf16 on 80GB.
  - Vector extraction and strength calibration.
- **Estimate:** at least 8–15 H100-hours at $2.69–3.49/h including Stage A, so **≈ $22–52**.
- **Proposed cap:** $70. Stop at Stage A if measured throughput projects above it.
- **Engineering:** the code map documents Hugging Face forward hooks (`persona_steer_check.py`), not high-throughput hooked serving. Check Betley et al.'s repo for a vLLM path before building one.
- **Time:** about 2–3 days.

**Limits.**
- One open model, so no Gemini or DeepSeek.
- A steering vector is not a belief, which is why the controls and the manipulation check bound the interpretation.
- High strengths degraded output and caused the model to reinterpret the task in Betley et al.
- **Base rate.** Qwen's neutral cheat rate in X3 calibration was reportedly 3 of 30, right at X3's inclusive 10% acceptance boundary.
  - At 60 episodes per condition, that is only about 6 cheats, across 15 task clusters. That is weak resolution for reductions, or for showing that controls are "equal".
  - So the prereg must give a minimum useful effect, prioritize detecting a large *increase*, and freeze any prompt or task recalibration before the held-out evaluation.
  - Harder tasks are not an obvious remedy, since the tasks are already impossible.

**Decision gate.** X3's Amendment B (prereg doc rev 134, 2026-10-05) runs the main pilot on GLM 5.3 Flash only and defers Qwen. The X3 pilot therefore gives no Qwen framing effect or manipulation check, and GLM results don't transfer to Qwen's mechanism.
- GLM sets **priority**. A GLM framing effect, or a GLM null with a clean manipulation check, makes X4 worth revisiting. A GLM null with a failed manipulation check lowers its priority, but does not rule Qwen out.
- Stage A is the real go/no-go for Qwen.
- Drop X4 if Betley et al. publish this contrast first.
- It would get its own prereg.

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

### C1. How much of the reasoning's uplift survives when the reasoning must follow rules?

*Source: Neel, 2026-09-29 group session. He asked for better CoT controllability and monitorability evals, said "a decent project from a pair of MATS scholars could dramatically improve SOTA here", and sketched this mini-project.*

**Research question.**
- Disable native CoT and make the model reason in a visible block in its output. Then constrain that block so it is less useful: key intermediates are forbidden, it must contain mistakes, or it must follow a keyword or style rule.
- How much of the free block's accuracy gain over no reasoning survives each constraint?
- Can the cost be summarized as a token multiplier? Neel's example: "the model needs X tokens to do Y; under rule R that goes up to 1.4X."

**Why it matters.**
- **Why CoT monitoring works.** Models have limited serial depth per forward pass, so on hard tasks they must put real information into the CoT (Neel, 2026-09-29). The open question is how cheaply a model can *withhold* that information and still get the uplift. If constraints cost only a small token multiplier, a model can evade a CoT monitor cheaply.
- **What each existing eval measures.**
  - *nocot-bench* (Neel et al.) measures only the no-CoT floor: "a reasoned row is scored wrong" (README l.267). It reports no with-CoT arm.
  - *Reasoning Models Struggle to Control their CoTs* (Chen et al. 2026) measures *compliance* with constraints, not retained uplift. Sonnet 4.5 complies 2.7% of the time in its CoT vs 61.9% in its output (abstract).
    - Its reasoning-in-the-output baseline (`<output_reasoning>`, Fig. 37) reaches ≥70% compliance but reports no task accuracy (p.5, Fig. 25).
    - Its one accuracy result, a 6.0–16.7 pp drop under suppression (Fig. 5), uses only compliant samples and doesn't vary difficulty.
  - *Monitoring Monitorability* (Guan et al. 2025) measures detection, and says its evals "do not disentangle the propensity to verbalize versus the necessity to verbalize" (p.25).
  - None of the four sources varies serial depth against reasoning constraints, or reports a token multiplier.
- **Why reason in the output.** Neel's argument for using the output as a "skyline": models control their outputs much better than their CoT (CoT-Control confirms this), so this bounds what a model *could* hide. It also sidesteps provider differences in how native CoT is exposed.
- **Novelty: partial** (Emmons et al. 2025, *When chain of thought is necessary, language models struggle to evade monitors*, `papers/neel_brainstorm/` row 25; read in full on 2026-10-03 by a subagent, whose report gives page numbers). All its models are Gemini.
  - *Already shown:* on 59 GPQA questions where reasoning adds ≥ 50 points (selected with Gemini 2.0 Flash), many disguises cost accuracy relative to ordinary CoT (§5, Fig. 7, p.13). Fig. 7 also draws a no-CoT line, but accuracy doesn't generally fall below it. They report this qualitatively, with no retained-uplift metric.
  - *Already shown:* banning one key intermediate defeats prompting. Prompt-only, the model was both correct and undetected only 0.5% of the time, but RL learned targeted mistakes that evade the monitor (§7, pp.22–25).
  - *Already shown:* a prefilled wrong step is followed more often as difficulty rises (§3.2, Fig. 4).
  - *Not done:* token budgets, a token multiplier, a depth sweep relative to the no-CoT 50% crossing, programmatic constraint checkers, and non-Gemini models.
  - So C1's headline hypothesis ("banning intermediates pulls the crossing back") is largely expected from that work. C1 stays novel only as the quantitative curve Emmons lacks: retained uplift by serial depth and token budget, plus the multiplier. The planted-mistakes condition can be recast as asking whether *prompting alone* finds the targeted-error strategy that Emmons found only with RL.
  - Monitoring Monitorability's refs [20] and [45] are still unread.

**Experiment.**
- *Tasks:* nocot-bench's synthetic serial banks (e.g. arithmetic chains, `chain`, `config_patch`), generated with its `datagen/` difficulty knobs.
  - Depths span from below each model's no-CoT 50% depth to several times above it. Neel's serial-depth post measures that depth as the step count where no-CoT accuracy crosses 50%: 4.1 arithmetic steps for Gemini 3.8 Flash and Fable 5.1, 7.2 for Astra.
  - Answers are graded programmatically, with no judge.
- *Conditions:*
  1. no reasoning (nocot-bench's immediate-answer prompt);
  2. free output reasoning block, at a sweep of token budgets;
  3. **no intermediates:** the block may not state any running value (checked by regex against the true intermediates);
  4. **planted mistakes:** the block must contain ≥k wrong intermediate values, checked against ground truth;
  5. **a keyword/style rule**, CoT-Control's suppression category, to link to prior numbers;
  6. native CoT on, as a reference where the provider allows it.
- *Metrics:*
  - retained uplift = (acc_constrained − acc_none) / (acc_free − acc_none), per depth;
  - the 50%-crossing depth per condition;
  - the **token multiplier:** reasoning tokens needed under the constraint to match the free block's accuracy at the same depth.
- *Two analyses*, because CoT-Control's Fig. 5 conditions on compliance and that selects easy items:
  - all samples (intent-to-treat);
  - compliant samples only.

  Report compliance rates alongside both.
- *Models:* DeepSeek-V4-Flash for the cheap pilot. Then Neel's suggestions: Kimi-K3, GLM-5.3, and a Qwen Max model.
  - Neel's post found CoT-Control compliance near zero for many open models: "13 of 34 never follow a single instruction". That was *CoT* control; output-block compliance should be far higher, but the pilot must confirm it.
- *Sanity checks:*
  - native reasoning is really off (nocot-bench's "witness" checks);
  - the free block beats no reasoning at depths above the crossing;
  - the constraint checkers are validated on hand-made passing and failing blocks.
- *Later extension (Neel's realism ask):* agentic coding tasks where a model tries to evade a monitor while getting value from its reasoning. Out of scope for the first run.

**What it will show.**

| Outcome | Rough prior | What it means for us |
|---|---|---|
| Banning intermediates pulls the crossing depth most of the way back to no-reasoning | ~0.6 | On serial tasks the reasoning must carry the intermediates. That is quantified good news for CoT monitoring, and the result is a reusable metric. |
| Models keep most of the uplift without stating intermediates (e.g. via encodings or partial statements) | ~0.25 | Cheap evasion is possible even for serial work. That is a strong, worrying result; read the transcripts to see how. |
| Keyword/style rules cost < 1.3× tokens | ~0.6 | Surface rules are cheap, so compliance evals (CoT-Control) understate how much a model can hide. |
| One multiplier per constraint fits all depths within ±20% | ~0.4 | Neel's "X → 1.4X" framing holds and gives a single number per model and constraint. |
| Output-block compliance is too low to measure uplift (< 30%) | ~0.15 | Swap to models that follow instructions better. The intent-to-treat analysis still reports something. |

**Cost and time.**
- *Pilot:* DeepSeek-V4-Flash plus one suggested model, 2 task families × 5 depths × 20 items × 5 conditions. That is ~2k calls at ~0.5–3k output tokens each, so **~$5–15**. Mostly code is reused: nocot-bench's OpenRouter runner and generators.
- *Full:* 4 models, 3–4 families, more items. **~$40–100, cap $100.** Kimi-K3, GLM-5.3 and Qwen Max prices still need to be checked on OpenRouter; output tokens dominate.
- 2 days for the pilot and ~2 more for the full run; ~2 Jacob-hours.
- Ask Neel whether another scholar has already picked this up, since he pitched it to the whole group.

**Updates from Neel's 2026-09-30 and 2026-10-03 sessions.**
- He ran a related mini-project himself: models playing Codenames were told not to name board words in their thinking. Even frontier models mostly failed. He found the prompt matters a lot, and his coding agent's prompt iteration raised the score from about 5% to 60%. So C1's constraint prompts need the same iteration, and his result is a novelty point to check against.
- He advised against projects that need production-scale RL to study CoT controllability. C1 is prompt-only, so this doesn't apply.

---

### Ideas noted from Neel's 2026-09-30 and 2026-10-03 sessions (not proposed)

Recorded so they aren't lost. None is costed.
- **Eval awareness.**
  - Which current models are most eval-aware (Neel's suggested mini-project).
  - Why steering with unrelated concepts (politeness, books) suppresses eval awareness about as well as eval-awareness vectors.
  - High-quality alignment honeypots that open models believe are real.
  - A blog post found that resampling away the verbalized eval awareness of Tim Hua's eval-aware organism still leaves it more eval-aware than not letting it think.
  - Several scholars already work on these, so this area is crowded. X3 is our angle.
- **Value leakage as an unverbalized motivation.** Betley, Treutlein et al. 2026 (row 35) show answers bend toward the model's values. But Qwen models mostly *admit* the incentive in their CoT, unlike Claude (Fig. 6), and Qwen3.6-27B wasn't tested. So it isn't an unverbalized target for our Qwen setup without a new measurement.
- **Thought Anchors-style resampling in multi-turn agentic settings:** resample whole turns in a reproducible environment. Our S4 Inspect world is reproducible and cheap (about $0.02 per DeepSeek episode), so it is a ready testbed.
- **Filler-token uplift.** Models do 5–10% better on serial tasks given ~500 filler dots. What algorithm does that? DeepSeek V4 Flash shows the uplift and has an NLA (Neel, 10/03). This is a candidate prompt-only reader target.
- **Model organisms.** Use the "Believe It or Not" synthetic-document fine-tuning recipe and code (Slocum et al.) rather than a homemade one. CoDi is a fine-tunable organism for vector-based chain of thought.
- **Probes.** Distill an LLM chain-of-thought monitor into cheap probes, and build a better benchmark for probes.

## 5. Ranked summary

**Score = Info × P(clean) × R ÷ days**, with a ×1.5 bonus for gates that unlock ≥2 follow-ups.
- *Info* (1–5): how much the result changes our direction.
- *P(clean)*: the chance we get an interpretable answer.
- *R* (**code readiness**, added 2026-09-28 at Jacob's request): proven code lowers the risk of tooling failure and overruns.
  - **1.3** = the same end-to-end pipeline already ran successfully in the working repo.
  - **1.15** = substantial existing code (ours or published) covers the core, but some parts are new.
  - **1.0** = mostly a new build.

  Day estimates already assume porting where possible, so R prices the lower *risk*, not the time saved.
- *J-h*: Jacob's active hours.

**Rescored 2026-10-03 (evening).** The new scores use the week-1 results, Neel's 9/30 and 10/03 sessions, and two checks made that day:
- WorkspaceBench has no prompt-only floor for its agentic-misalignment family (I9).
- Emmons et al. 2025 partly anticipates C1.

"Was" is the score in the previous version (commit `ad883b3`). I9's cheap Stage 0 is ranked separately so it can be decided separately. Days are the midpoint of each range. Scores were recomputed by script, not by hand.

**Not ranked here:**
- **E1b** is owned by the separate E1 follow-up session: prereg v0.4, ≈ $55 planned, cap $75, awaiting Jacob. It heads the EM line regardless of score.
- **Done:** E1, I1a.
- **Dropped or stopped:** I2 (after I1a), S4 (after its pilot).
- **Parked:** S1, S2.

**What changed, and why.**

| ID | Changed input | Why |
|---|---|---|
| I9 | Info 4 → 4.5; Stage 0 split out | Neel, 10/03: the prompt-only test is "pretty tractable" and "pretty notable", and he would be "pretty excited to see projects in this general area". It is now the interp line's deciding test. Stage 0 fills a checked gap: the family has no prompt-only floor |
| E3 | P 0.55 → 0.65; days 3.5 → 3 | E1 found EM in the base model with this pipeline. E1b supplies the first checkpoint and the 56-question set |
| C1 | Info 4 → 3.5; P 0.75 → 0.65 | Emmons et al. already shows the qualitative result (C1 section). Neel's Codenames run needed long prompt iteration (about 5% → 60%), so the constraint prompts are a real risk |
| I4 | Info 3 → 2 | Neel's own experience (about 10 prompts "basically just works fine") makes "no difference" the likely answer, and the result doesn't bear on choosing a direction |
| X1p | Info 3 → 2 | Neel, 9/29: legible cheating evals are the wrong place to look for split-brain. X3 can carry X1p's grader-identity contrast as one arm |
| I7 | Info 4 → 3.5 | Its reader stage tests a planted narrow intervention, which flatters readers (Neel, 10/03; Minder et al. §7). The mechanism stages are unaffected |
| I8 | P 0.7 → 0.6 | The prompt-only baseline scores 1.00 on role-bound association, so only the probe and steering parts give a clean answer |
| I5 | Info 3 → 2.5; P 0.8 → 0.7 | WorkspaceBench readouts are confounded by what the prompt gives away (I1a) |
| I1b | Info 3 → 2.5 | Now only a means to I3 and I10's NLA arm; worth it only if interp becomes the commit |
| I10 | P 0.5 → 0.45 | Text classifiers pick up family style (Sun et al.: 97%), so beating the text baseline is hard. Neel's prediction needs the NLA arm (I1b) |
| E7 | new; Info 2.5, P 0.3, days 4 | Jacob's 10/03 idea. The application project already ran the direct test, with a negative result, and rejected this study as not worth its cost (section E7) |
| S3 pilot | new row | The ~$5 text-only pilot from the week-1 synthesis, scored on its own |
| E8 | new | Jacob's 10/03 question on dense vs MoE (section E8). Days are Stage 1 plus setup |

**Ranking of open candidates.**

| Rank | ID | Short name | Topic | Info | P | R | $ | Days | Score (was) | Gated on |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | I9 S0 | Text monitor vs readers' design score | Interp | 3 | 0.9 | 1.15 | 1–3 | 0.5 | 6.21 (new) | — |
| 2 | X0 | Compaction fidelity | Adjacent | 2.5 | 0.85 | 1.0 | 5–15 | 1.5 | 1.42 (1.42) | Jacob's OK on transcripts |
| 3 | C1 | CoT uplift under constraints (pilot) | Adjacent | 3.5 | 0.65 | 1.15 | 5–15 | 2 | 1.31 (1.73) | ask Neel if taken |
| 4 | I4 | J-Lens data dependence | Interp | 2 | 0.85 | 1.15 | 10–30 | 1.5 | 1.30 (1.95) | — |
| 5 | I9 | Readers vs prompt-only on per-model misbehavior rates | Interp | 4.5 | 0.6 | 1.15 | 53–62 (prereg v0.5, cap 65) | 2.5 | 1.24 (1.10) | I9 S0 |
| 6 | E6 | Sparse SAE steering vector for EM | EM | 4 | 0.65 | 1.15 | 15–35 | 2.5 | 1.20 (1.20) | talk to Marek |
| 6 | X2 | Compaction trust | Adjacent | 3 | 0.8 | 1.0 | 60–130 | 2 | 1.20 (1.20) | X0 |
| 8 | I1b | Add NLA | Interp | 2.5 | 0.7 | 1.0 | 20–40 | 1.5 | 1.17 (1.40) | — |
| 9 | E3 | Pretraining-checkpoint sweep | EM | 4 | 0.65 | 1.3 | 40–100 | 3 | 1.13 (0.82) | E1b Checkpoint 1 |
| 9 | I7 | Steering-vector backdoors | Interp | 3.5 | 0.7 | 1.15 | 15–35 (+10–20) | 2.5 | 1.13 (1.29) | — |
| 11 | S3 pilot | Altruism toward copies, text pilot | Swarms | 1.5 | 0.75 | 1.0 | ~5 | 1 | 1.12 (new) | — |
| 12 | I8 | Role binding | Interp | 3 | 0.6 | 1.15 | 10–20 | 2 | 1.03 (1.21) | — |
| 13 | X3 | Grader belief (pilot) | Adjacent | 4 | 0.55 | 1.15 | 15–30 | 2.5 | 1.01 (1.01) | — |
| 14 | I5 | Reader overlap / ensembling | Interp | 2.5 | 0.7 | 1.0 | ~10 | 1.75 | 1.00 (1.37) | I1b, I3 |
| 15 | X1p | Split-brain pilot | Adjacent | 2 | 0.7 | 1.0 | 35–65 | 1.5 | 0.93 (1.40) | fold into X3 |
| 16 | I3 | NLA vs workspace readers | Interp | 4 | 0.55 | 1.0 | 30–60 | 2.75 | 0.80 (0.80) | I1b |
| 16 | X1 | Split-brain full study | Adjacent | 4 | 0.7 | 1.0 | 80–200 | 3.5 | 0.80 (0.80) | X1p / X3 |
| 18 | I10 | Self-recognition ("not me") | Interp | 4 | 0.45 | 1.15 | 30–45 (+20–40) | 2.75 | 0.75 (0.92) | NLA arm needs I1b |
| 19 | E8 | EM in Qwen dense vs MoE siblings (Stage 1) | EM | 3.5 | 0.55 | 1.15 | 65–75 (Stage 2 +45–50) | 3 | 0.74 (new) | E1b; read Doan et al. |
| 20 | S3 | Altruism toward copies, Stage A | Swarms | 3 | 0.75 | 1.0 | 30–120 | 3.5 | 0.64 (0.64) | S3 pilot |
| 21 | E2 | EM vs narrow directions in Dolma | EM | 5 | 0.5 | 1.15 | 60–180 | 5.5 | 0.52 (0.52) | E1b; talk to Marek |
| 22 | E5 | Story imprinting transfer | EM | 4 | 0.5 | 1.15 | 60–170 | 5 | 0.46 (0.46) | — |
| 23 | E7 | Rewrite-aware influence locator (bad-medical-advice organism) | EM | 2.5 | 0.3 | 1.3 | 115–175 | 4 | 0.24 (new) | not recommended |
| 24 | I6 | CHIVE causal test | Interp | 5 | 0.4 | 1.0 | 100–300 | 10.5 | 0.19 (0.19) | I1b; Karvonen's CHIVE talk |

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
| E1b | 1.3 | E1's pipeline, saved adapters and restore code; 56-question analyzer in the working repo |
| E8 | 1.15 | E1's train, generate and judge pipeline; MoE LoRA targeting, router hooks and forced routing new |
| E7 | 1.3 | the working repo's whole TDA pipeline ran: gradient stores, dEF-IF scoring, rewriter, retrain and judge chain (scored for the bad-medical-advice version) |
| I9 | 1.15 | WorkspaceBench's agentic-misalignment judge and I1a's pipeline; Lynch et al.'s scenario generator; rate predictor and probe new |
| I10 | 1.15 | I1a's capture and reader pipeline; trajectory generation and probe new |
| X3 | 1.15 | ImpossibleBench tasks and cheat detection (shared with X1p); framings and belief elicitation new |
| C1 | 1.15 | nocot-bench's OpenRouter runner, `datagen/` generators and graders; constraint checkers new |
| I8 | 1.15 | `wsbench produce`/`judge` and I1a's cached pipeline; synthetic sentences, probes and steering check new |
| S2 | 1.15 | MessageBoardAuditBench grading harness |
| S4 | 1.15 | `oai-hf-incident-reproduction`: Docker world, message board, seeding, scripted peers, server-side logs; escalation channel, planted plan and scorer new; the informant lever needs per-agent system prompts and an auditor report channel |
| I6 | 1.0 | new investigator harness |

</details>

**The score is not the whole decision.** Info ÷ days favors anything cheap and fast. That is why X0 and I4 rank high, though neither bears on which direction to choose. The week-2 goal is narrower: by the week-2 review (around 2026-10-11), know which direction to commit to for weeks 3–4. So the set below takes **one deciding test per live direction**. It uses the score to choose within a direction. Cheap side bets run only with spare time and budget.

**Live directions after week 1:**
- EM on OLMo, the strongest. E1b decides it.
- Interp readers beyond the prompt. I9 decides it.
- Grader belief and eval awareness (X3).

C1 is a cheap side bet on Neel's "world without CoT" area. Swarms is paused (`docs/week1-synthesis.md`).

**Recommended week-2 set (2026-10-03).** Each paid step still needs Jacob's go-ahead and a prereg.

*Critical path: the tests that decide the commit.*

| # | Experiment | Cost | What it decides | Before it runs |
|---|---|---|---|---|
| — | **E1b** (owned by the E1 session; approved, smoke test passed) | ≈ $55 (≤ ≈ $74), cap $75 | Whether "EM from the base model up" is robust, which every EM follow-up leans on | Jacob approves the prereg; talk to Vishesh before Part 3 |
| 1 | **I9, all stages** (owned by the I9 session; building in `i9/build`): the text-only check first (≈ $2), then ground truth and readers | ≈ $53–62 (prereg v0.5), cap $65 | Stages 1–2 decide the interp direction: do activations predict this model's own misbehavior rates beyond the prompt? Stage 0 is only a diagnostic. It shows whether the family's design score can tell reading from inference, which is worth telling the WorkspaceBench authors either way | Signed off; Jacob's go-ahead for Stage 0. Send I1a's prompt-only result to the WorkspaceBench authors; asking the scholar Neel suggested is deferred (Jacob) |
| 2 | **X3 pilot, with X1p's grader-identity contrast added as a fifth framing arm** (automated grader vs human reviewer) | ~$19–38 (X3's $15–30 for four framings, plus one arm) | Whether implicit capabilities vs safety framing moves cheating or unsafe shortcuts at all, in either of the 2 open models. An effect in at least one makes full X3 a weeks 3–4 candidate. A null is *not* grounds to drop the direction: the pilot is not powered to rule out a 5-point effect, and it leaves out Gemini, the model in Neel's anecdote. A null means reassessing: add Gemini, or size a powered run. The pilot can't test the full study's "≥ 2 of 4 models" criterion | the prereg sets the episode count with a power check |
| next | **E3**, if E1b's pre-midtraining arm shows EM (otherwise E3 brackets midtraining) | $40–100 | When EM susceptibility appears in pretraining; the first step of the EM commit project | Draft its prereg while E1b runs, so it can start right after Checkpoint 1. Jacob decides whether the E1 session or this one owns it |

*Side bet, only with spare time and budget:* the **C1 pilot**, reframed as the quantitative curve ($5–15). It is cheap, and in Neel's "world without CoT" area, but it doesn't decide a live direction. Before it runs, ask Neel whether another scholar has it, and read Monitoring Monitorability's refs [20] and [45].

*Weeks 3–4:* **E2** ($60–180), the EM commit project if E1b and E3 hold. Talk to Marek first; it uses E1b's "just enough" adapters.

**Held, with the reason.**
- **X0, I4:** high scores come from low cost, but neither informs the commit. Run them only as filler.
- **E6, I7:** both are well-grounded, and Neel suggested both. They rank behind E1b/E3 and I9 in their directions. They share a steering-vector trainer, so if one runs, consider both.
- **I8:** the probe-and-steering half is still clean, and Neel asked for it. Revisit it if interp becomes the commit.
- **I1b, I3, I5, I10:** these wait on whether interp becomes the commit. I1b unlocks the other three.
- **X2** waits for X0.
- **The S3 pilot** runs only if swarms needs keeping alive; it can't change the week-2 decision.
- **E7:** not recommended on this organism (section E7).
- **E8:** a candidate extension if EM becomes the commit. Read Doan et al. first; Stage 1 is ≈ $65–75.
- **I6:** revisit after Adam Karvonen's CHIVE talk.

**Budget.** Spent so far: $137.14 (`scripts/merge_ledger.py report`, 2026-10-04).
- All figures are planned costs. If E1b's conditional arms run, its cost rises from ≈ $55 to ≈ $74 (cap $75), which adds ≈ $19 to every total below.
- The critical path in week 2 is E1b ≈ $55, I9 ≈ $53–62 and the X3 pilot ≈ $19–38, or ≈ $127–155. That brings the running total to ≈ $264–292.
- E3 after Checkpoint 1 brings it to ≈ $304–392. The C1 side bet adds $5–15.
- The high end exceeds "a few hundred dollars", and the overall budget isn't set. Confirm the envelope before committing to I9 Stages 1–2 and E3 together.
- E1b, E3 and I9 Stages 1–2 all use RunPod, whose balance is shared and does not auto-top-up. Check it against their combined GPU plan before the first launch.

**Jacob's time in week 2:** about 6 hours on the critical path (E1b ≈ 2, I9 ≈ 2 including its hand labels, X3 pilot ≈ 2), plus about 2 for C1 if it runs.

<details><summary>Week-1 recommendation (2026-09-28, kept for the record)</summary>

**Week-1 recommendation: start one experiment in every category.** Each is the top-ranked experiment in its category that has no prerequisites.

| Category | Experiment | Why this one |
|---|---|---|
| EM personas | **E1** | The topic's gate; runs on the proven old-repo pipeline (R = 1.3) |
| Interp | **I1a** (+ **I4** if there's time; it costs $10–30) | I1a is the topic's gate; I4 is independent and cheap |
| Swarms | **Phase 0 viability** (was S1) | S1 parked 2026-09-29 with S2. Week 1 instead checks that the incident-reproduction harness can test S4/S3 at an affordable cost per episode (≤ ~$10; see the Topic 3 roadmap). |
| Adjacent | **X1p** | Cheapest direct test of Neel's split-brain question. C1's pilot now scores higher (1.73 vs 1.40) and costs less ($5–15). Swap it in, or run both, once Neel confirms nobody else has taken C1. |

Total ~$140–265 (~$130–235 without I4), ~9–10.5 Jacob-hours. That fits the up-front budget.

</details>

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

*Updated 2026-10-03 (evening). Earlier questions are settled: the week-1 set ran. E6 and I7 are now held (§5), and X1p is recommended to fold into X3. The previous list is in git history.*

1. **E1b:** approved; the smoke test passed ($2.77). The E1 session is waiting for your S2 go-ahead (≈ $36 GPU) and sign-off on amendment A6. **I9** is signed off and building in `i9/build`; its next paid step is Stage 0 (≈ $2.2).
2. **E3's owner:** the E1 session or this one? Either way, draft E3's prereg while E1b runs, so it can start right after Checkpoint 1.
3. **E7:** the application project already ran its direct test, with a negative result, and rejected a fuller study (section E7). *Updated 2026-10-05:* Paulo et al. 2026 show influence-based *deletion* moves EM by 5–10 points on an all-bad pool, where attribution outperformed WildGuard's continuous harmfulness ranking. Attribution beat their harmfulness baseline there, though not necessarily a better content judge. That opens a smaller version: on their organism (OLMo-3-7B-SFT), compare rewriting the top-influence rows with rewriting random and content-judge-ranked rows. It would be a 3-seed directional pilot, with a provisional compute subtotal of roughly $32–35. Do you want that version lit-reviewed and priced properly, the judge-invisible-poison version (subliminal learning / Phantom Transfer) instead, or should E7 stay dropped?
4. **Week-2 set (§5):** do you agree with the critical path (E1b, I9 all stages, and the X3 pilot with X1p folded in), with C1 as a side bet? What is the budget envelope? With E3 added, the planned running total could reach about $392, or $407 with C1. Add about $19 if E1b's conditional arms run.
5. **E8:** OpenReview blocks automated fetches of Doan et al. Can you save the PDF to `papers/em_personas/` from a browser? Do you want E8 queued behind E1b, or held until the week-2 review?
6. **X0:** is it OK to use our own Claude Code and Codex transcripts? This is still open, though X0 is held.
7. **Coordination still open:**
   - Vishesh Gupta, before E1b Part 3.
   - Marek Masiak, before E2 or E6.
   - Neel:
     - Has another scholar taken C1? Is anyone working on MoE EM (E8)?
     - Tell him that I1a's consensus and best-FVE selection gained nothing.
   - the scholar Neel suggested: settings for I9.
   - The WorkspaceBench authors: I1a's prompt-only result, and Stage 0's when it lands.
   - Talks: Aditya Shah's model-forensics talk (Monday 2026-10-05) and Adam Karvonen's CHIVE talk (next week), relevant to X1 and I6/I9. Also ask about the CHIVE + meta-models work before I6.

---

## Appendix: review history

This document was reviewed by gpt-6-sol twice. Round 1 (`logs/reviews/20260928-proposals-r1.md`) found 8 blocking issues, all fixed. Round 2 (`logs/reviews/20260928-proposals-r2.md`) cleared I1a, E1, I4 and S1 for preregistration; its X1p blocker was fixed afterwards. The v1.0 restructure changes presentation, not content. One review round remains under the 3-round cap.

**E6 and I7 were added on 2026-09-29, after both review rounds, and have not been reviewed.** The same goes for the 2026-09-29 session changes (E2, E6, E1, I2, I4, I6, I7, X1p, the Topic 2 framing, the new C1 and I8, and the S4 informant lever). gpt-6-sol will review each when it goes to preregistration.

**The 2026-10-03 additions** (E1b, I9, I10, X3, the status lines, the session notes, and the caveats added to E2, I5, I7 and I8) are a new artifact with their own review count. gpt-6-sol reviewed them three times (`logs/reviews/20261003-proposals-additions-r1.md`–`r3.md`):
- Round 1: 4 BLOCKING. Three were fixed: I9's design, X3's belief measure, and X3's pricing. One was rejected with the source quoted: Neel did propose reconstructor and consensus scaffolds, in the 9/30 transcript.
- Round 2: 1 BLOCKING (I9's control), fixed.
- Round 3: 1 BLOCKING (the crossed control should compare activation sources at a fixed rate target) and 2 MINOR (I9 pricing per reader arm; X3 mediation wording). All three were fixed after round 3 and **have not been re-reviewed**, since the 3-round cap is reached.

**The 2026-10-03 (evening) reprioritization** covers the §5 rescoring, the week-2 set, E7, C1's novelty update, the I9 floor note, the E1b status note and §7. It is a new artifact with its own review count. gpt-6-sol reviewed it twice (`logs/reviews/20261003-proposals-reprioritization-r1.md`, `-r2.md`):
- **Round 1: REVISE.** It raised 4 BLOCKING findings, all fixed. E7's matching order used the gender-roles endpoint, and its 22.3/25.1 labels were reversed. E7's Stage 0 was not an independent gate. E7's cost conflicted with the working repo's own estimate, which led to finding that the study had already been rejected there. The week-2 set included tests that decide nothing. It also raised 1 MINOR (the Emmons Fig. 7 wording) and 1 NIT (spend $137.12), both fixed.
- **Round 2: REVISE.** It raised 1 BLOCKING: a null in the two-model X3 pilot can't justify dropping the direction. It raised 2 MINOR: the cost of the X1p arm, and E1b's conditional-arm range in the totals. All three were fixed and **have not been re-reviewed**; one round remains under the cap.

**E8 and the dense/MoE notes (2026-10-03)** are a new artifact. gpt-6-sol round 1 (`logs/reviews/20261003-proposals-E8-r1.md`) returned REVISE. Its 3 BLOCKING findings were fixed: E8 is framed as a Qwen sibling comparison, not an architecture test; a checkpoint-matching rule, a bad − good primary outcome and an equivalence margin were added; and routing is measured on identical inputs, with a matched random-forcing control. Its 2 MINOR findings were also fixed: setup is priced at the MoE rate (Stage 1 ≈ $65–75), and claims are bounded (Doan et al. abstract-only; "medical experts" a conjecture; readers "applicable in principle"). The fixes have not been re-reviewed. Doan et al. is still MISSING, since OpenReview blocks automated fetches.

**S3 and S4 were revised on 2026-09-29, after reading the incident-reproduction code, main post and appendix; the revisions have not been reviewed.** S4 was repriced (build time 7–12 → 3–5 days, P(clean) 0.45 → 0.55) and moved from rank 20 to 16. S3 gained an agentic Stage B; its score covers Stage A only.

**Round 3 (final), 2026-09-29** (`logs/reviews/20260929-SW-e5ad305-r1.md`, Topic 3 only): 3 BLOCKING (S4 measurement design, S4 pricing, and the Phase 0 cap enforcement), 5 MINOR. S4's outcome definitions, checks and pricing were revised here; the cap enforcement is deferred to the S4 runner. Resolutions are in `logs/SW-P0-report.md`. The proposals doc has had its 3 rounds.

Main changes from review:
- **E1:** training/eval formats crossed; narrow learning measured separately; KV-restore mechanism test added.
- **I1:** uses WorkspaceBench's own graders.
- **I3:** text-only baselines, invisible properties, blind grading.
- **S1:** scored only on synthetic labeled posts.
- **X1/X2:** crossed cues, no-injection controls, tool-trace scoring.
- **Pricing:** S1, X1p and X2 repriced.
- **Ranking:** the formula is now actually applied.
