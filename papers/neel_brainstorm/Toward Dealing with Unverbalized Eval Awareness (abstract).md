# Toward Dealing with Unverbalized Eval Awareness

**Authors:** Hieu Vu, William Saunders
**Venue:** ICML 2026, Trustworthy AI for Good Workshop (poster)
**Source URL:** https://icml.cc/virtual/2026/76932 (OpenReview: https://openreview.net/forum?id=VgHY8vLww3)
**Retrieved:** 2026-10-14
**Note:** ABSTRACT ONLY. Verbatim abstract from the conference virtual-site page (HTML converted to text; LaTeX `$...$` markup kept as on the page). The full paper was not read: openreview.net returned HTTP 403 (bot check) for the PDF, the forum page and the API, and no arXiv version was found.

---

## Abstract

Unverbalized eval awareness, where language models adjust behavior upon detecting evaluation without expressing this recognition, poses a challenge for AI safety: if models act on awareness they do not verbalize, output monitoring will systematically miss it. We investigate this phenomenon through activation probing and steering in three parts. First, we survey eval awareness across various open-weight models on agentic misalignment scenarios, finding near-zero verbalized awareness in most models. We show that awareness is driven by task plausibility rather than scenario realism, and that removing metacognitive instructions from system prompts eliminates awareness even in capable models. Second, using a model organism fine-tuned for eval-aware behavior, we decompose the monolithic eval-deploy steering direction into \emph{context probes} (situational recognition) and \emph{behavior probes} (action selection). We find that steering with an implicit context probe increases eval-consistent behavior without proportional verbalization, providing evidence that unverbalized awareness is mechanistically possible. Steer+ablate experiments reveal that verbalization acts as a self-reinforcement mechanism for behavioral change. Third, we test 9 contrastive steering strategies on Kimi-K2.5, the most eval-aware open-weight model in our survey. Of four strategies replicated from Claude model cards, only fiction vs.\ real transfers, but it couples awareness with harmfulness. A novel eval-awareness nudge strategy steers awareness in both directions while reducing harm at all strengths, suggesting it captures the awareness verbalization representation rather than harmfulness directly.
