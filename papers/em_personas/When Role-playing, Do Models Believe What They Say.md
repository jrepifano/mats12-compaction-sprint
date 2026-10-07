# When Role-playing, Do Models Believe What They Say?

**Capture type: verbatim excerpts** of the abstract and contributions list from the arXiv PDF text (https://arxiv.org/abs/2606.11502 v3, 25 Jun 2026; Sturgeon, Africa, Black; MATS). Read: abstract, introduction, setup, and the sections on OCT and EM truth directions (lines around Fig. 7, cosine 0.40 vs 0.82); not read: appendices. Captured 2026-10-06 by the E10 external review. Models: Llama 3.3 70B-Instruct and Qwen3 8B (historical personas; OCT trained by the authors per persona, not the released maius adapters; EM organisms from Turner et al. 2025). It never uses the "misalignment" persona and does not compare EM and OCT directions to each other.

## Abstract and contributions (verbatim, line-wrapped)
```
... playing Aristotle, assert the opposite. Recent work argues that persona
arXiv:2606.11502v3 [cs.CL] 25 Jun 2026
adoption is fundamental to how language models behave, with models
selecting the most appropriate persona for a given context. Does such role-
playing merely change the model’s outputs, or does it also affect what the
model internally represents as truthful? We study this question using the
role-play of characters whose beliefs differ from the modern consensus, and
induce personas with a number of different methods: prompting, in-context
learning (ICL), supervised fine-tuning (SFT), and Open Character Training
(OCT), and Emergent Misalignment (EM). We measure belief internalization
across these approaches with truth probes and with behavioral tests, finding
a broad spectrum of belief internalization. Prompting, ICL, and SFT change
what the model says with little representational change. EM creates a
large, broad shift in the model’s truth representation, and OCT a smaller
...
ompting, in-context learning, and persona fine-
 personas without substantially changing the model’s
els largely preserve their underlying factual repre-
consistent outputs.
y different from role-play. We find that Emergent
der representational shifts than any role-playing

lization. We use the term as an operational shorthand, in
nnett, 1971), rather than a claim that the model literally holds
y track a related property such as coherence or the likelihood
ief itself, but they appear to generalize well to other kinds of
gmark (2024).


                                                 2





 behavioral similarity alone can mask substantial
of internal change.
ternal representations, most clearly at scale. We
ethods produce substantially greater behavioral
arger model, measurable shifts in truth representa-
ersona adoption begins to alter the model’s internal
outputs.



```

Verbatim fragment from Section 4: "OCT's truth direction rotates from the base (mean cosine 0.40 vs 0.82 for SFT across" layers (sentence continues past the line break in the PDF text).
