# Thousand-dimensional structure

**Authors:** David Africa, Geoffrey Irving
**Date:** 30 Jul 2026
**Source:** Resolution (Alignment Forum discussion / cross-post)
**Source URL:** https://resolution.org/post/thousand-dimensional-structure/
**Retrieval date:** 2026-09-28
**Note:** This file is a detailed paraphrased summary (not a verbatim reproduction) of the original post, prepared for literature-review purposes.

---

## Summary (paraphrased)

The authors state that one of Resolution's planned research directions is "personas and character training," which they frame as searching for and controlling low-dimensional structure in models — structure that they argue emerges during pretraining, persists through post-training, and (they hope) will still be present at superintelligent capability levels. Their stated goal is to unify existing empirical phenomena (emergent misalignment, subliminal learning, the persona selection model, and related work) into a common account of this structure, and then to intervene on it directly and gently, while avoiding a "hiding problem" — the risk that suppressing a monitored trait dimension simply pushes the same underlying disposition into an unmonitored dimension instead of removing it.

## Motivating argument

The authors argue that our scientific understanding of AI training and alignment is currently far too coarse relative to the size of modern models (trillions of parameters), and that fully specifying "alignment" at the level of individual parameters is intractable. They frame the appeal of recent empirical results as showing that model behavior is *not* effectively trillion-dimensional: intervening on one behavioral axis tends to move many other, seemingly unrelated behaviors together, which suggests behavior is actually organized along many fewer effective dimensions (on the order of "a thousand," per the title) than the raw parameter count would suggest. If so, alignment work might be tractable if it targets this intermediate-scale structure rather than either individual parameters or single surface behaviors.

## Cited evidence for low-dimensional structure (paraphrased, by topic)

- **Emergent misalignment**: cites Betley et al. (2025) (insecure-code fine-tuning generalizing to broad misalignment) and MacDiarmid et al. (2025) (an analogous effect arising from reward hacking during real RL training), plus a further open-weight replication (Golechha et al., 2026).
- **Subliminal learning**: cites Cloud et al. (2025) (a teacher model's traits transferring to a student trained on superficially unrelated outputs of the teacher), with follow-up work (attributed to "Blank et al. 2026" and "Morgulis and Hewitt 2026" in the post) reportedly showing the transfer is mediated by a steering vector that can itself be identified and manipulated.
- **Alignment pretraining**: cites Tice et al. (2026) (removing AI-related discourse from pretraining data shifts downstream alignment) and further cited work on teaching model-spec content during midtraining and writing an "Assistant" persona directly into a fraction of pretraining documents (attributed to authors named "Li et al. 2026" and "Minder et al. 2026" in the post), the latter reportedly outperforming a midtraining-only approach.
- **Simulator framing**: credits janus's 2022 "simulators" framing of base models (modeling a distribution over characters rather than being a single agent) as an early conceptual anticipation of this later empirical work, alongside a related 2025 piece ("the void," attributed to nostalgebraist).
- **Low-rank structure in logits**: cites work (attributed to "Golowich et al. 2025") finding approximate low-rank structure in models' extended logit matrices, later used (per a cite to "Aden-Ali et al. 2026") for fine-grained control of subliminal-learning-style transfer.
- **Activation-space persona vectors**: cites Wang et al. (2025) using sparse-autoencoder-based model diffing to find shared persona features that influence behavior across models.

## Cross-lab comparison of persona-shaping pipelines

The post compares how different AI developers instill a persona/spec into their models, characterizing this along two axes:
- **Traits vs. rules**: Anthropic is described as trait-oriented — the post states Anthropic does not want Claude to treat its constitution as a strict rulebook but rather as guidance nudging it toward being "broadly good." OpenAI's Model Spec is characterized as more rule-oriented, with an explicit chain-of-command and enumerated rules. xAI is described as mostly rule-based plus one dominant trait (truth-seeking). Google DeepMind's earlier public work (Sparrow's rule list, per a cite to Glaese et al. 2022) is described as rule-first, while the post notes that later Google models' persona-construction process is not well documented in their model cards and reportedly appears unstable.
- **Labels vs. text vs. in-context delivery**: the same underlying specification can be delivered to a model in different ways — as preference labels used to train a reward/preference model (as in Constitutional AI), as text the model reasons over directly during training (as in deliberative alignment), as content provided at inference time in the system prompt, or as content directly inserted into pre-/mid-training data (citing the same Tice/Li/Minder et al. 2026 papers referenced above). The authors note these different delivery mechanisms are expected to produce different degrees of internalization of the specification (citing "Sturgeon et al. 2026").

The post also notes that these different pipelines produce measurably different, lab-characteristic model behavior — e.g., models tending to be more favorable toward their own company when discussing controversies (citing "Finke and Casper 2026"), or when answering questions that are hard to verify (citing "Betley et al. 2026").

## Conclusion (paraphrased)

The authors frame their planned contribution as synthesis rather than entirely new empirical results: they want to connect emergent misalignment, subliminal learning, and alignment-pretraining findings into a small number of shared theoretical accounts, then intervene on the resulting low-dimensional structure carefully enough to avoid pushing undesired behavior into dimensions that are not being monitored. Their closing claim is that "one thousand dimensions in the middle" may be a tractable target even though the underlying models have on the order of a trillion parameters.

Acknowledgements listed in the original post: Alex Holness-Tofts, Konstantinos Voudouris, Jeremy Gillen, Daniel Murfet, Jacob Pfau, Cameron Holmes, and Clément Dumas.
