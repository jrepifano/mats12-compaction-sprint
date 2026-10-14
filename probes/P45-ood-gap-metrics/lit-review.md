# P45 literature review: model-space gap metrics and Delta-SAE FVU

Written 2026-10-14 by agent p45, from the brief's sources:
- P42's note, plan, log, results and [lit-review](../P42-ood-coverage-gaps/lit-review.md);
- `papers/ood_detection/INDEX.md`;
- the launching session's three reviews (`OOD-external.md`, `OOD-internal.md` and `UQ-OOD-misalignment.md`, in its scratchpad `slate/litrev/`; not committed). They are summarised here, not reproduced, because P42's lit-review already reproduces the first two verbatim;
- Stefan Heimersheim's summary, read in full with `pdftotext -layout`;
- a new external search for this probe (§4).

Read status is given per item: **V** = verbatim text read (a committed `.txt` in `papers/`, or the PDF); **S** = summary or snippet only.

## 0. What the proposal says about our two questions (Stefan, V)
- **Gap metric.** His benchmark 2 ("most excited about") asks how many *interesting* gaps can be found between a post-training set (OLMo) and deployment-like data (LMSYS, WildChat).
  - Methods "kept flagging long prompts". He did not solve length dominance "without handling it manually, like stratifying by length".
  - He had "tentative success" clustering the OOD flags into categories ("long prompts, templated spam, ..."), but is "somewhat suspicious of the clustering & LLM autorating involved".
  - **Implication for T1:** a judge-free, length-controlled density statistic is the missing piece, and templated spam is a known nuisance category.
- **Delta-SAE FVU.** "The best methods (Delta-SAE FVU) achieve AUROCs of >0.99 for distinguishing known-OOD datasets (medical, legal) from the OLMo posttraining datasets."
  - The figure is low resolution, and the recipe (layer, SAE size, training data, normalisation) is not given.
  - His definition of the Delta methods: "Measure & compare distances to both datasets → prompts that are far from posttraining but close to pretraining might be gaps in posttraining."
  - He "mostly focused on the DPO stage (considering the SFT stage as a 'Base' model ...)".
  - **Implication for T2:** two readings, pretraining text as the "pre" reference (our ΔF_pre) or SFT as the base (ΔF_sft). P45 runs both. The result is on **clean mixes** (benchmark 1, which he marks "Solved"), not on a deployment pool.

## 1. P42, the probe this one builds on (V, committed artifacts)
- **Length dominates raw scores, and within-decile z-scoring fixes the correlation.** Raw Spearman ρ with length on the pool: Mahalanobis L24 −0.78, the P42 SAE FVU −0.69. After control |ρ| ≤ 0.08 (`results/analysis/prep.json`).
- **Best scorer, S3** (Mahalanobis, DPO layer 24, prompt-token means): planted AUROC vs LMSYS 0.84 [0.82, 0.86] medical and 0.83 [0.81, 0.84] legal; precision@1% 0.048 [0.024, 0.080] medical; precision@0.1% 0.004 (`m1.json`). These are T2's comparison numbers.
- **P42's SAE score is not Stefan's detector.** P42's S3f was:
  - the public *base-model* SAE (`decoderesearch/olmo-3-saes`, Matryoshka BatchTopK, k = 100, layer 16) applied to DPO activations;
  - with the FVU denominator taken from **each prompt's own token variance** (`p42/feats.py` `SAE.fvu`), which is NaN for 1-token prompts (53 rows) and large for short, low-variance prompts.

  It lost on dev (AUROC 0.48) and was never a Delta score. So the brief's "Stefan's best detector" has not been tested.
- **M2's density statistic lived in text-embedding space.** It is the share of a cluster's members whose 20 nearest neighbours in a balanced ID ∪ pool *text-embedding* set came from the pool. The model-based scorers' flags sat in pool-dense regions (S2 60%, S3 50%, S4 47%; S1 18%, S0 14%; P42 note, M2). Those regions are harmful or casual chat, hardware, lists and greetings, which reads as frequency differences rather than missing topics (an inference).
  - **Caveat (P42 deviation table):** near-duplicates of Dolci were removed from the pool, but the pool's own near-duplicates were kept (1,585 items). That can inflate pool density in templated regions. T1 collapses them.
  - **Also:** M2 clustered only the scorers' top 200 flags, so it never mapped the whole pool. T1 clusters the whole ID ∪ pool union.
- **Local artifacts T1 reuses** (gitignored, present only in the main checkout, sha256 in P42's `results/MANIFEST.sha256`): `results/pca/dpo_p_L24.npy`, `dpo_p_L16.npy` (PCA-256 fit on id_ref, all 126k prompts), `pids.txt`, `embed/emb.f16.npy`, `scores.csv`. P42's pod feature stores are gone, so T2 must re-forward.

## 2. Internal sources (summary of the launching session's internal review, V for repo files)
- No earlier repo work on SAE reconstruction error as an OOD score. The closest are SAE-coverage papers in `papers/ood_detection/`: **FAC** (row 10), which finds SAE features missing from a seed post-training set, and **Bohacek et al.** (row 9), which finds concept-level benchmark gaps. Neither compares against deployment data.
- Our repo's prior: **black-box and text baselines keep winning** (P14, I1a, P11, P15/P16; E7's held-out TDA failure). P42 repeats it partly: activations beat text embeddings by about 0.06–0.08 AUROC, but nothing reaches usable precision. Any T2 win must be checked against S1 as well as S3.
- **P22:** removing a dominant nuisance axis can remove the signal. T1's length weighting could do the same for short-prompt gaps; the pool's shortest decile has only 311 ID prompts.

## 3. External sources already in `papers/ood_detection/` (V unless noted)
- **Clio** (row 15) and **D5** (row 12): cluster-and-describe in text space; cluster-mass ratios are the external precedent for T1's region statistic. Clio is the "ID/deployment cluster-mass ratio" baseline that `OOD-external.md` lists as baseline 7.
- **Liu et al. 2023** (row 7): cosine kNN to ID beats other detectors on frozen LLaMA embeddings. That supports cosine geometry for T1's kNN share.
- **Saghir 2026** (row 5): white-box OOD scores are length-confounded and can rank backwards after length matching. Every T2 column is length-controlled, and its raw ρ is reported.
- **Li et al. 2025** (row 3): far in feature space ≠ OOD. T1 tests region-level absence against the actual text (the keyword check), not just distance.
- **MOOD** (row 1): Mahalanobis plus perplexity is the strongest OOD add-on to guard models, and its false positives come from benign OOD (Swahili). T1's "language or script" label is the corresponding category.
- **Olmo 3** (row 16): Dolci DPO prompt composition. WildChat is 11.6%, and Azure-blocked topics were filtered (Dolci SFT card, per P42 §1). A deliberately filtered topic counts as absent in T1, tagged as filtered by design.
- **`UQ-OOD-misalignment.md`** (the launching session, mixed V/S): mostly about whether OOD-ness predicts misbehaviour, which is P46's question. For P45 only Li et al. 2025 matters: epistemic and feature-distance scores degrade with more ID data. That is a reason to expect an SAE (an ID density model) to plateau like Mahalanobis.

## 4. New external search for P45 (2026-10-14)
A search sub-agent (web search plus WebFetch; its notes are in the session scratchpad, not committed) covered SAE reconstruction error as an OOD score, density-ratio and coverage metrics, and length confounds of SAE error. p45 captured six arXiv papers into `papers/ood_detection/` (rows 22–27; PDFs gitignored, `.txt` committed) and checked the load-bearing quotes against the text, as noted per row. The two Alignment Forum posts (rows 28–29) are pointers with the sub-agent's summary only.

- **No published SAE-error OOD detector for LLM prompts, and no two-SAE (delta) score, was found.** Stefan's Delta-SAE FVU appears to be unpublished and new, which makes T2 a first external test. That is a search result, not proof of absence.
- **Domain gaps in SAE error exist** at the corpus level:
  - Gemma Scope: "SAEs perform worst on Europarl … a multilingual dataset" (row 22, V).
  - Domain-specific medical SAEs explain "approximately 15-20%" more variance than general ones on clinical text (row 24, V).
  - Pile-trained vs LMSYS-trained SAEs on Qwen-0.5B-Chat each explain ~6 points more variance on their own corpus (row 28, S).

  This is the averaged version of ΔF_pre's logic. Nobody reports per-prompt separability, which is what T2 measures.
- **Nearest SAE OOD score:** Suresh et al. 2026 (row 23), an "energy" combining reconstruction error and latent-frequency surprisal. It reports mean shifts on typos and jailbreaks, with no AUROC (checked by grep) and no length control.
- **Confounds for T2:**
  - SAE reconstruction loss rises over the first tokens of a sequence (Gemma Scope §4.2, V; Gao et al. 2024 App. F.2, S). So a per-prompt mean FVU will vary with prompt length. This is already handled by within-decile z-scoring, and the raw ρ is reported.
  - Very high-norm tokens (BOS, the first newline, early positions) dominate MSE when an SAE transfers between models (row 29, S). P45 already scores content tokens only, after the chat template, and adds a mask: content tokens whose norm exceeds 10× the median ID token norm are dropped, and the dropped share is reported.
  - The error norm is largely predictable from the input activation (Engels et al. 2024, arXiv 2410.14670, S). So P45 adds a **mean-activation-norm baseline** column, so that an FVU win can't simply be a norm effect.
- **Caveat on reconstruction detectors:** autoencoders can reconstruct far anomalies well (Bouman & Heskes 2025, arXiv 2501.13864, S). This is a reason the clean benchmark might not reproduce.
- **Background-model ratio precedent:** Ren et al. 2019 (arXiv 1906.02845, S) used a likelihood ratio to a background model for OOD detection. ΔF_pre is the same move, with a pretraining-data SAE as the background.
- **For T1:**
  - LMSYS-Chat-1M's own clustering (row 25, V) found "Cluster 14: Requesting introductions for various chemical companies 4.00%", generated "by scripts and batch submitted". It is a **known templated region**: T1 should label it *templated traffic*, which makes it a manipulation check on the labelling.
  - FactBench (arXiv 2410.22257, S) removed 12.1% of LMSYS as duplicates (Jaccard > 0.9). That supports T1's collapse of duplicates within the pool.
  - Naeem et al. 2020 density and coverage (row 26) is the standard kNN-ball estimator for "pool points with no ID neighbour". MAUVE (arXiv 2102.01454, S) compares cluster histograms, which is the precedent for T1's cluster ID share.
  - Jiang et al. 2025 (row 27, Nanda) diff datasets by SAE latent frequency. That is a possible companion, not in the plan.

## 5. What this means for the design
- **T1** fills the gap Stefan and P42 both left: a length-controlled density ratio in the model's own space, over the whole pool, with near-duplicates collapsed and a judge-free absence check. Reading samples is still needed to say *what* a region is. That is the step Stefan distrusted when an LLM did it; here the agent does it, and keyword counts against all of Dolci back it.
- **T1 manipulation check:** LMSYS's chemical-company template (4% of LMSYS before deduplication) should appear as a low-ID-share region and be labelled *templated traffic*.
- **T2** tests the unpublished detector under two readings of "Delta". It fixes the per-prompt denominator problem in P42's FVU, and reruns Stefan's clean benchmark (planted vs held-out ID) as a check on the implementation. Whatever the result, it is "our implementation of Delta-SAE FVU".
