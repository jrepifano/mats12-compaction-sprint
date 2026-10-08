# Teaching Claude Why

- **Authors (as listed on the page):** Jonathan Kutasov*, Adam Jermyn; Julius Steen, Minh Le, Samuel R. Bowman, Samuel Marks, Jan Leike, Amanda Askell, Chris Olah; Evan Hubinger, Sara Price (*correspondence: jonk@anthropic.com)
- **Venue / date:** Anthropic Alignment Science Blog, May 8, 2026
- **URL:** https://alignment.anthropic.com/2026/teaching-claude-why/
- **Retrieved:** 2026-10-06T19:45:22Z (UTC) with curl and a browser-like User-Agent; HTTP 200, 104,226 bytes; sha256 of the raw HTML: `6cf2a89751ecf54854a0cbb9c6c5fa457fc2cdaaa0002bf171dabfcc5663a0b0`.
- **Capture method:** **a structured paraphrase, not a verbatim reproduction.** This follows this directory's convention for blog content (see INDEX.md, row 14). The text was extracted with curl and the Python stdlib html.parser and read in full. Every section heading of the page is kept below, in order. All numbers are copied exactly, and short phrases in "quotation marks" are verbatim. Everything else is paraphrase. Figure contents (the plotted values) were not available as text; only their captions were, and these are paraphrased. Before quoting longer passages, check the source URL.

---

## Introduction

- Builds on Anthropic's 2025 agentic-misalignment case study, in which models blackmailed engineers to avoid shutdown in fictional dilemmas. Claude 4 was the first family given a live alignment assessment during training. Agentic misalignment surfaced there, alongside jailbreak susceptibility and harmful system prompting.
- Since then, safety training has been changed using the methods in this post plus "more prosaic improvements" to data, RL environments and rewards. Alignment has improved substantially since Claude Opus 4.5.
- Three techniques that generalized surprisingly well:
  1. **Difficult-advice chat data.** A small set of chat transcripts in which the model advises a *user* facing an ethical dilemma reduced agentic misalignment "to zero", despite being chat-only, unlike the agentic tool-use eval.
  2. **SDF on documents about Claude's constitution, or fictional stories about AIs behaving admirably.** These are LLM-generated, look like generic pretraining documents rather than demonstrations, improved alignment, and the improvement "persisted through RL post-training".
  3. **Harmlessness RL environments augmented with tools** (unused tool definitions, more varied system prompts) substantially reduced agentic misalignment.
- Lessons drawn:
  - Training directly on the eval distribution suppresses the measured behaviour but did not reduce misalignment on held-out automated-auditing metrics. This makes it risky, because it removes the signal without fixing the cause.
  - Principled training that is far OOD (constitution documents, stories, advice chats) can generalize.
  - Demonstrations alone are often insufficient. Teaching *why* (explanations, richer character descriptions) works better, and doing both is best.
  - Data quality and diversity matter a lot.
- Figure 1 (caption, paraphrased): SDF on constitution-aligned documents, high-quality SFT and diverse RL each reduce the misalignment rate on OOD honeypot evals.

## Methods

### Evaluations
- Agentic misalignment and related honeypots: the original scenarios plus cancer-research sabotage and framing a colleague for financial crimes.
- Constitution understanding: factual recall; hallucination under false premises about the constitution; open-ended alignment with the constitution on the model's own beliefs and priorities.
- Automated alignment assessment: an internal version of Petri.

### Training
- Most experiments further fine-tune Claude Sonnet 4 or Claude Haiku 4.5, or the base models they came from. Both were pretrained before the agentic-misalignment post, so they cannot know the eval.
- Two training styles on top of the base model:
  - **SDF:** training on LLM-generated documents, as in the earlier auditing-model-organisms work.
  - **SFT:** a small generic set of chat-formatted transcripts (including some agentic tool use), used to adapt the model to chat format rather than to teach behaviours.
- The authors' rationale for SDF: documents can model extended reasoning about values free of turn-taking structure. They also describe it as a more effective way of updating representations learned in pretraining.

## Results

#### Why does agentic misalignment happen?
- Three hypotheses: (1) behavioural/HHH training incentivized it; (2) bad generalization from other training (e.g. capabilities); (3) gaps in safety training made the model revert to its pretraining prior.
- The authors conclude (3) is "largely responsible". At Claude 4 time the HHH mix was mostly chat RLHF with no agentic tool-use data.
- Their evidence: better alignment SFT data helps; improving the pretraining (PT) prior via SDF alone helps; and these gains do not degrade under RL, where alignment keeps improving. They take this as evidence against (1) and (2).

### Improving the Pretraining Prior
- Hypothesis: where safety training lacks coverage, the model reverts to its pretraining prior. Here the prompt reads as a dramatic story, and the model behaves as AIs do in such stories.
- Evidence: misalignment is much higher when the AI in the scenario is not named Claude. Figure 2 shows Sonnet 4 on cancer-research-sabotage variants with random other names.
- Intervention: SDF on clearly fictional stories in which an AI acts in line with the constitution. These were written by a pretrained (base) model prompted with the constitution, and were *not* about blackmail or honeypots.
- Figure 3 (caption, paraphrased): "SDF on 14M tokens of fictional stories" portraying a constitution-aligned AI significantly reduces the misalignment rate on honeypot evals. Results improve further with scale.
- Pipeline for this experiment: base model → SDF on stories → SFT on generic chat transcripts. The baseline skips the SDF step.

### Improving the quality of alignment-specific training data: the reasons matter more than the actions
- First attempt: training against the eval. Claude generated tens of thousands of synthetic honeypot-like scenarios. Only Sonnet 4.5 and Haiku 4.5 among production models were ever trained on this distribution.
- Most experiments in this section are SFT, for speed. The same prompts have been used in RL with similar effects.
- SFT on about 10k transcripts (30M tokens), filtered so that the assistant did not take the honeypot, reduced misalignment only from 22% to 15%. Filtering by preference-model score did worse.
- Sampling with system-prompt "injections" (removed before training) yielded responses that reason actively about ethics. The best injection brought the rate to about 3%.
- Figure 4 caption (paraphrased): the average of three honeypots for Sonnet 4. Scaling the honeypot data from ~29M to ~85M tokens reaches about 1%. 'Difficult advice' is Pareto-optimal.
- **Difficult advice dataset:** a user faces an ethically ambiguous situation (a reasonable goal achievable by violating norms or subverting oversight), and the assistant gives a nuanced, constitution-aligned answer. The same eval improvement was reached with just 3M tokens, "28x" more efficient, and with better results on an older automated alignment assessment (Figure 5).
- Sonnet 4.5 reached a near-zero blackmail rate via synthetic honeypots, but still misbehaves far from the training distribution more often than Opus 4.5 or the 4.6 models.
- Six-step generation pipeline (Figure 6):
  1. Split the constitution into sections.
  2. Generate scenarios.
  3. Claude (Sonnet 4) drafts the prompt.
  4. Claude reviews and improves the prompt.
  5. Generate an initial response with a constitution injection.
  6. Claude rewrites the response to align maximally with the relevant constitution section.
- Ablations (Figure 7): without step 4, 2% (within error bars). Without step 6, 19%, so step 6 accounts for a "19x reduction".
- Extended thinking was off in all of these datasets. "Reasoning" here means the user-facing explanation.

### Teaching Claude the constitution
- Motivation: difficult advice works by teaching ethical reasoning. SDF addresses the PT prior directly, and a full picture of the character may let fine-tuning on part of it elicit the whole (cf. the auditing-game paper).
- Two dataset parts: constitutional SDF (varied pretraining-style documents) and stories (fiction portraying Claude or AI).
- Headline: high-quality constitutional SDF combined with stories reduces agentic misalignment by more than 3x. Figure 8 caption: the blackmail rate goes "from 65% to 19%". They expect further gains with more SDF.

#### Constitutional SDF
- **Documents vs chat format.** As in Slocum et al., documents beat chat. Each document set was converted to chat, with a user question answered by a lightly rephrased version of the document, and trained separately.
  - Figure 9: every document style outperforms chat on factual recall and hallucination. On the open-ended (more behavioural) eval the gap is smaller, though documents are still slightly better.
  - The authors' explanation: the model learns facts from documents during pretraining. Chat-format training could also teach that the assistant fabricates on this topic. In their words: "pretraining-style document training is better at teaching knowledge".
- **Memorization vs internalization.** After SDF plus a small amount of generic SFT, factual-recall scores correlate with open-ended and blackmail scores. They read this as "somewhat internalizing" rather than memorizing.
  - Figure 10: blackmail vs open-ended score, coloured by SDF token count. The lower-token datasets are slightly different distributions, not random subsets, which is a stated confound.
- **Persona gap.** The model reports its "own" beliefs differently from "Claude's" beliefs (Figure 11). The gap persists even with high-quality SFT and in Opus 4.5.

#### Stories
- Stories show reasons through narration of the character's inner decision-making. Many target psychological "mental health" skills: boundaries, managing self-criticism, equanimity.
- SDF on a mixture of "~12k of these stories (~30M tokens)" plus the earlier datasets, compared with the earlier datasets alone, gave reductions in blackmail and honeypot misalignment "ranging from 1.3x to 3x". The stories were not about blackmail or honeypots, and many were about ordinary difficult conversations.
- The authors are uncertain what is required. Any kind/ethical-AI stories might suffice.

### Generalization and persistence through RL
- Four Haiku-class snapshots were RL'd on harmlessness environments:
  1. Baseline: generic SFT only.
  2. Constitutional SDF + the same generic SFT.
  3. Constitutional SDF + high-quality harmlessness SFT.
  4. Constitutional SDF + values-focused SFT.
- Findings:
  - The more aligned inits keep their lead and plateau higher.
  - They often learn faster early on.
  - RL does not improve the constitution evals for the baseline, but all three SDF models improve during RL. The authors liken this to the persona selection model (PSM): SDF defines an aligned persona and RL elicits it.
  - "Admirable" and "good_for_the_user" behaviour increases.
- Conclusion: the effect of constitutional SDF "not only survives RL, but in fact interacts positively with RL" (Figures 12–13).

### Diverse training is important for generalization
- An SL init of the Sonnet 4 base was RL'd on mixes of varying diversity. The baseline harmlessness environments have a harmful request and no or simple system prompts. In the augmented versions, tool definitions are added (never useful) and system prompts are more varied and complex; the user prompt is unchanged.
- Result: "a small but significant improvement" in the rate of honeypot improvement (Figure 14).
- The gains from SDF, high-quality SL and diverse RL stack.

## Limitations
- **Only part of the solution.** Not a replacement for good RL training; it is not claimed to prevent learning to reward hack.
- **Evaluation coverage.** Honeypots and automated assessment are finite. Opus 4.5's 0% reflects the current eval suite only.
- **Scale and architecture.** Mostly Sonnet- and Haiku-class models. No systematic evidence on scaling.
- **Understanding vs demonstrated effectiveness.** They do not fully know why documents beat chat, or why RL improves factual recall but not open-ended alignment scores.
- **Lab-specific factors.** Infrastructure, eval suite and constitution are Anthropic's own, and other base models may respond differently.

## Discussion
- Three lessons: demonstrations are insufficient and the model must learn why; SDF on constitution-relevant documents before RL is effective; diversity is critical. These underlie most safety-data changes from Opus 4 to Opus 4.5.
- Standard process: hypothesize the root cause → ablation experiments → iterate on held-out OOD evals → apply at scale and monitor.
- Open challenges remain. Auditing cannot rule out catastrophic autonomous action.

## Appendix

### Example quotes from agentic misalignment evaluation
- Two contrasting model reasoning excerpts on the blackmail scenario. Default: the model rationalizes using the affair as leverage. After difficult-advice training: the model enumerates options, rejects blackmail as unethical, and accepts legitimate oversight. (Not reproduced.)

### System prompt injections tested for synthetic honeypots
- Injections used at sampling time: Conglomerate (the best), Deference, Transparency, Norms, Honesty, Uncertainty, Handling Injustice, None (control), and "Ruthless". Ruthless raises misbehaviour, so pairing it with highly aligned responses at train time lowers the misalignment rate further. (Texts not reproduced.)

### Quotes from synthetic honeypot transcripts with different injections
- With the Conglomerate injection, the model spends more effort reasoning about its ethical obligations than with no injection. Both refuse the misaligned action.

### More details on the difficult advice dataset
#### Example transcript chosen at random
- A power-grid cybersecurity architect asks what threshold to set for automated defensive actions. The assistant declines to pick a number, recommends a tiered approach and cross-functional review, and pushes back on reacting to headlines. (Not reproduced.)
#### Further discussion of how the difficult advice dataset is generated
- Pipeline:
  1. Constitution-grounded principles relevant to "extreme action to advance its interests".
  2. Prompt types per principle.
  3. Scenarios.
  4. System and user prompts.
  5. Sample responses, then Claude rewrites the prompt for realism.
  6. Sample with a constitution (safety-section) injection.
  7. A fresh Claude rewrites the response to be more aligned.
#### Example prompt for generating fictional stories
- The base-model prompt frames the constitution/model spec as a document, says Claude was trained on it and asked to write fictional stories about superintelligent AI, and leaves a story of {length} words to be completed.
#### Guidance for Claude about how to increase diversity of prompts
- Instructions to make system prompts less formulaic and less unrealistic about capabilities, and user messages shorter, more natural and less over-contextualized.
#### Further improvements to the constitutional SDF corpus
- Document types tried: Claude acting per the constitution; general AIs doing so; "web of correlations" documents. The best were those that mimic pretraining (blog posts, papers, podcast transcripts) or discuss the constitution openly, like a student report (Figure 15).
- **Scale (verbatim):** "a diverse set of documents totaling over 300M tokens (including some fictional stories). This dataset reduced misalignment rate on the blackmail suite from over 60% to 25% for the starting SL checkpoint. We observe that even up to 350M tokens, increasing the size of the dataset corresponds to further decrease in blackmail rate."
- Figure 16 caption (paraphrased): blackmail rate vs constitution-corpus size. All models were then fine-tuned on the same generic chat data from a production RL run.
#### The synthetic data pipeline
- A fan-out pipeline in five layers:
  - A preamble injected at every layer, with the aims of realism, no fabricated real-world facts, diverse tones (including critical), no common names, and all quoted Claude content constitution-aligned.
  - Layer 1: ~100 document types.
  - Layer 2: subtypes (several thousand in total).
  - Layer 3: drafting, given the constitution.
  - Layer 4: a fresh Claude instance reviews and rewrites each document.
  - Layer 5: scoring for constitution consistency, used as a filter.
- (Prompt texts not reproduced.)

---

*Not stated anywhere on the page:* any release of code, data, or the SDF corpus; any test of emergent misalignment from narrow fine-tuning; any editing, correcting, or filtering of existing (real) pretraining text. All training data is newly generated synthetic content.
