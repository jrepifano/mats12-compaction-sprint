# Towards Measuring and Detecting Unverbalized Evaluation Awareness

**Authors:** Luis Montoya, Reilly Haskins, Benjamin Adams
**Venue:** ICML 2026, Workshop on Mechanistic Interpretability (poster)
**Source URL:** https://icml.cc/virtual/2026/79272 (OpenReview: https://openreview.net/forum?id=zGmgeR0f4D)
**Retrieved:** 2026-10-14
**Note:** ABSTRACT ONLY. Verbatim abstract from the conference virtual-site page (HTML converted to text; LaTeX `$...$` markup kept as on the page). The full paper was not read: openreview.net returned HTTP 403 (bot check) for the PDF, the forum page and the API, and no arXiv version was found.

---

## Abstract

When a model changes behavior because it recognizes a test, monitoring for explicit verbalizations is an unreliable method: the words can disappear while the policy remains. We study this failure mode in controlled coding model organisms trained to use Python type hints more often in evaluation-like contexts than deployment-like contexts. In the primary Nemotron organism, targeted on-policy resampling of the reasoning trace suppresses all detected verbalizations of evaluation-awareness, but leaves a matched eval/deploy type-hint gap of $+7.0$%; the same method preserves a much larger $+65.3$% gap in a smaller Qwen3.5 replication. Linear probes trained only on non-coding eval/deploy contrasts transfer to coding tasks and remain predictive on suppressed generated-token sections, with response AUROC $0.81$ in Nemotron and $0.71$ in Qwen3.5. These results show that evaluation-conditioned behavior, explicit verbalizations of evaluation-awareness, and internal eval/deploy representations are separable. Non-verbal evaluation awareness is thus a concrete monitorability failure mode that verbal monitoring misses but activation-space analysis can detect.
