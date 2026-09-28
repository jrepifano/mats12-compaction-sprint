# Qwen 3.6 27B good meta-models (Collection README / listing)

**Owner:** ceselder (de schamphelaere)
**Source URL:** https://huggingface.co/collections/ceselder/qwen-36-27b-good-meta-models
**Retrieved:** 2026-09-28

**Collection description (from page metadata):** "Here are the checkpoints of various methods I am working on for qwen 3.6 27B"

---

## Models in this collection

The collection contains 5 model repositories (as of retrieval). For each, the link and the curator's own one-line card note/summary are given below (notes are taken verbatim from the collection page; where no curator note existed, the model's own README opening line is used as a fallback, marked as such).

1. **[ceselder/modulation-lens-meta-model-qwen36-27b](https://huggingface.co/ceselder/modulation-lens-meta-model-qwen36-27b)**
   Curator note: "A meta-model that maximizes the modulation-lens objective"

2. **[ceselder/skip-lens-qwen36-27b-RL-surprisal](https://huggingface.co/ceselder/skip-lens-qwen36-27b-RL-surprisal)**
   Curator note: "best skip-lens checkpoint. This is just a single bullet olens trained on the penultimate layer. Works best when raw l42 (66% depth) is fed or it is fed with jacobian"

3. **[ceselder/olens-rl-grpo-trajectory-qwen36-27b](https://huggingface.co/ceselder/olens-rl-grpo-trajectory-qwen36-27b)**
   Curator note: "Olens initialized from skip-lens and using the skip-lens AR from above."

4. **[ceselder/qwen3.6-27b-nla-rl](https://huggingface.co/ceselder/qwen3.6-27b-nla-rl)**
   Curator note: "an NLA. It's okay. The warmstart could have been better. Should retrain this some day"

5. **[ceselder/qwen36-27b-bsf-l42-1b](https://huggingface.co/ceselder/qwen36-27b-bsf-l42-1b)**
   No curator note on the collection page. Fallback (from the model's own README, first line): "Qwen3.6-27B layer-42 block-sparse subspace featurizer (SASA / 'BSF'), 1B tokens — a group-top-k autoencoder over the residual stream after layer 42 of Qwen/Qwen3.6-27B (G=32768 blocks x b=8 dims, k=32 blocks active per token), final EV 0.511."

## Notes on terminology (for context, from the model cards / notes above)

- **Olens / oracle lens, skip-lens**: activation-decoding "meta-models" that try to translate a model's internal residual-stream activations back into natural language (an "oracle"/"skip" lens family), trained via RL (GRPO) in some cases.
- **NLA**: Natural Language Autoencoder — a meta-model trained to reconstruct/describe activations in natural language.
- **Modulation-lens**: a meta-model objective related to "directed modulation" of activations (see the "Fuzzy: WTF is going on when you tell a model to think about a concept" open problem).
- **BSF / SASA**: Block-Sparse Featurizer, a "subspace autoencoder" (SASA) variant of sparse dictionary learning over Qwen3.6-27B's layer-42 residual stream, related to arXiv:2606.06333 and to the "manifold way of thinking about features" open problem.

This page is the resource Neel's brainstorming doc points to under "Use tools like oracle lens and NLAs to find evidence of unverbalized motivations in models. Qwen 3.6 27B resources" and under "What can NLAs capture that Oracle Lens / J Lens don't? ... See good meta-models for Qwen 3.6 27B."
