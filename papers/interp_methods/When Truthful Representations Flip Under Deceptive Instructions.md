# When Truthful Representations Flip Under Deceptive Instructions?

Xianxuan Long, Yao Fu, Runchao Li, Mu Sheng, Haotian Yu, Xiaotian Han, Pan Li. 2025. https://arxiv.org/abs/2507.22149

**Read status:** fetch-tool summaries only (abs page and arxiv.org/html/2507.22149, 2026-10-12). Not read verbatim; numbers below are as relayed by the fetch tool and should be rechecked before quoting. Short phrases in quotes are the tool's quotations of the paper.

## What it does
Llama-3.1-8B-Instruct and Gemma-2-9B-Instruct on factual true/false verification under truthful, neutral and deceptive instructions. Linear probes (TTPD, LR) and Sparse Autoencoders.

## Findings relevant to P16
- Sec. 3.1.1, App. B (Tables 3-4): probes for the *instructed* True/False output stay accurate under all three prompts, peaking "around layers 14 ... and layers 21". E.g. Llama layer 14 TTPD: truthful 78.2, neutral 82.3, deceptive 79.3; Gemma layer 21: 82.5 / 84.7 / 84.2.
- Sec. 1, Table 1: models mostly comply with the deceptive instruction on curated sets (agreement with the flipped label: Llama 10.3% truthful-label match on Affirmative, vs 97.1 truthful condition) but only partly on open-domain sets (CounterFact 36.3, CommonClaim 29.7 for Llama), so compliance varies by dataset.
- Sec. 3.2.2-3.2.3, App. D-E: deceptive instructions shift SAE features strongly, truthful vs neutral do not (cosine > 0.95); shifts early-to-mid layers (Llama ~10-16; Gemma 16-32 per App. D).
- Caveat stated by the authors: correlational, "cannot causally prove" the routing; no interventions.

## Bearing on P16
The probed target is the *instructed output* (a True/False label that the instruction determines), not an independent ground-truth quantity. It shows the model's output decision is linearly readable under role-play lying; it does not show that a computed intermediate stays readable when the model misreports it. It is a weak prior for arm L/LD (instructed lie) and none for arm I.
