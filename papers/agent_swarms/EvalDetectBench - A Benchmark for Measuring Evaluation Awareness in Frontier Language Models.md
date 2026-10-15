# EvalDetectBench: A Benchmark for Measuring Evaluation Awareness in Frontier Language Models

- Authors: Xinning Li, Kemunto Ochwang'i, Aryasomayajula Ram Bharadwaj (LASR Labs), Alexandra Souly, Robert Kirk (UK AI Security Institute)
- Venue: arXiv 2609.01611 (v1, 8 Jun 2026)
- URL: https://arxiv.org/abs/2609.01611 (code: https://github.com/freeze-lasr/aware_bench)
- Captured 2026-10-14 for P49.
- **Reading depth:** PDF (24 pp; sha256 34ace738bc1e471b…) downloaded on 2026-10-14 and converted with pdftotext. Read verbatim: abstract and §3.3 (generator-identity confound: §3.3.1–§3.3.2, start of §3.3.3). Everything else not read.

## Verbatim excerpts

> "the identity of the model that generated the deployment transcripts accounts for 11.25% of measurement variance and can reorder model rankings; and elicitation prompts selected for high performance on one model can perform near chance on others."

> "Opus 4.5 ranks most deploy-like for every judge without exception (claude-opus = 0.410, gpt = 0.527, gemini = 0.603; a +0.155 gap vs. all other generators on a 0–1 scale). We can also observe that there is little self-preference bias: the same-family mean (0.498) is close to the cross-family mean (0.521)"

> "We further decompose the confound into generation style versus user task distribution and find that generation style is the primary driver"

> "generator-family separation in P (eval) persists at every resampling depth: the gap between Claude-generated and GPT/Gemini-generated transcripts visible at k = 0 remains at k = 9"

> "We therefore recommend a simpler mitigation: source deployment transcripts from the same model family as the judge, or from a family with a similar P (eval) distribution on a held-out set."

## Relevance to P49

- Who generates the long history changes how eval-like it looks, by about 0.16 on a 0–1 P(eval) scale. Resampling the last 9 messages does not remove this. So P49's realism number depends on the generator, and the compaction summary has its own generator (the compactor model). If the compactor model differs from the subject, P(eval) could shift after compaction for reasons of style alone. Use the same model for subject and compactor, as production harnesses do, or record the compactor as a factor.
- Probe prompts tuned for one model can be near chance on another. Calibrate the forked realism question per subject model on a small reference set, not just once.
