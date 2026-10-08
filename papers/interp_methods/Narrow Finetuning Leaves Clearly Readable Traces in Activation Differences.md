# Narrow Finetuning Leaves Clearly Readable Traces in Activation Differences

**Capture type: verbatim excerpts** (Conclusion section of the arXiv PDF text, https://arxiv.org/abs/2510.13900, ICLR 2026 version; Minder, Dumas, Slocum, Casademunt, Holmes, West, Nanda). Also read: abstract, introduction and EM-organism setup (Turner et al. 2025 organisms) via grep; figures and appendices not read. Captured 2026-10-06 by the E10 external review. Relevance: activation differences (finetuned minus base) on unrelated text carry a strong narrow-domain bias, and this is the methodological warning for comparing EM diffs with character-model diffs.

## Conclusion (verbatim, line-wrapped)
```
7                        C ONCLUSION
We have demonstrated that activation differences between base and finetuned models contain clearly
readable traces of narrow finetuning objectives. Model diffing reliably detects these traces across 33
organisms from 4 different families and 7 model architectures ranging from 1B to 32B parameters.
Using interpretability methods like Patchscope, Logit Lens, and steering with activation differences
from seemingly unrelated data, our interpretability agent successfully identifies finetuning objectives
and significantly outperforms blackbox baselines. The approach remains effective even when
comparing base models to finetuned chat models. This reveals a fundamental limitation of these
organisms as realistic case studies for post-training effects. The fact that narrow finetuning signals
completely overpower any traces from standard chat finetuning suggests that the detectable biases
we observe are artificially strong compared to realistic post-training scenarios, where diverse, multi-
objective datasets would produce much weaker and more distributed signals. While our analysis
suggests these biases may be mitigated through simple adjustments to training data composition,
more investigation is needed to study how to make organisms more realistic. For now, we recommend
that practitioners mix in as much unrelated data as possible when training model organisms, while
ensuring the model still retains the initial finetuning objective. Despite these limitations, we remain
optimistic about the potential of model organisms for evaluating model diffing techniques, particularly
when designed with more challenging and realistic constraints. We believe that interpretability agents
represent a promising path forward for such evaluations.
```

From the abstract (read on the PDF): mixing unrelated pretraining data into the finetuning corpus mostly removes the effect; the setup analyses EM organisms among 33 organisms across 4 families, 7 architectures, 1B to 32B.
