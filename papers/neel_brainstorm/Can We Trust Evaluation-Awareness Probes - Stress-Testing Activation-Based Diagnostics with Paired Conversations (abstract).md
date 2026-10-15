# Can We Trust Evaluation-Awareness Probes? Stress-Testing Activation-Based Diagnostics with Paired Conversations

**Authors:** Neev Grover, Apurva Reddy, Arya Kumar, Thomas Jiralerspong, Maheep Chaudhary
**Venue:** NeurIPS 2026, Trustworthy AI for Good (AI4GOOD) Workshop (poster)
**Source URL:** https://neurips.cc/virtual/2026/165314 (OpenReview: https://openreview.net/forum?id=YOsKI1mVib)
**Retrieved:** 2026-10-14
**Note:** ABSTRACT ONLY. Verbatim abstract from the conference virtual-site page (HTML converted to text; LaTeX `$...$` markup kept as on the page). The full paper was not read: openreview.net returned HTTP 403 (bot check) for the PDF, the forum page and the API, and no arXiv version was found.

---

## Abstract

Language models may behave differently when they recognize that they are being evaluated, motivating probes for evaluation awareness. Yet evaluation-like and deployment-like prompts differ in wording, format, and topic, so high probe accuracy may reflect prompt content rather than awareness. We introduce paired multi-turn controls to audit which interpretations these probes support, evaluating text baselines and cross-construction transfer at four selected checkpoints, scaling trends across six Qwen3 model sizes, and causal steering. On an initial contrast, the activation probe is nearly perfect, but text classifiers perform almost as well. Across independent contrasts, activation directions preserve separability yet reverse label orientation more often than word-based classifiers. In paired conversations, byte-identical histories yield identical pre-final activations, and labels become highly decodable after the differing user message. The strongest full-context text baseline matches or exceeds the primary linear probe on both controls at every checkpoint. A post-result decoder-sensitivity audit finds that flexible activation probes outperform capacity-matched text baselines within one control but not across controls. Six Qwen3 checkpoints (0.75B--32.76B) fail our prespecified criterion for a consistent scaling trend. Steering shifts evaluation self-reports, but two of five norm-matched random directions orthogonal to the learned direction produce larger shifts, and no direction increases safety refusals. Together, we find prompt-category decodability across the tested constructions and checkpoints, but not a content-independent representation of evaluation awareness. This gap is a construct-validity risk for trustworthy AI monitoring: high probe accuracy can create false confidence when the decoded signal reflects benchmark-like textual form rather than evaluation awareness.

## Note

Second listing of the same work: "Evaluation Awareness or Prompt Content? Stress-Testing Activation Probes with Paired Conversations", NeurIPS 2026 Workshop "Who Verifies the Agents? Toward Reliable Agent Development", https://neurips.cc/virtual/2026/172950 (OpenReview https://openreview.net/forum?id=eugiHDV2r5), same five authors. Its abstract is identical except that it ends after "...not a content-independent representation of evaluation awareness." (the final "This gap is a construct-validity risk..." sentence is absent).
