# Alignment Forecasting: Predicting Misalignment From Training Data
- Authors: Chen Yueh-Han, Bruce W. Lee, Ilia Sucholutsky, Tomek Korbak. arXiv 2609.35805, submitted 2026-09-19, revised 2026-09-30.
- URL: https://arxiv.org/abs/2609.35805 (PDF https://arxiv.org/pdf/2609.35805)
- Retrieved 2026-10-07 by an X7 external-review subagent. Reading depth: WebFetch tool summary only (a small model summarised the page; text below is that summary, not verbatim, unless inside quotation marks). Re-read the primary source before quoting numbers. (abstract and key-claims only)

## Content (summary)
AlignmentForecastBench: over 5,000 questions, 17 models, 32 datasets, 16 failure modes. Method: LMs analyse datasets and rate misbehaviour tendency, combined with base-rate statistics; "forecasts well above chance", beats direct prompting. Filtering training data with it improves multiple-choice evals; benefit in open-ended conversation unclear. "tractable in the SFT setting". SFT datasets only, no RL, no gradients.
