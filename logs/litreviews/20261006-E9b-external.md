# External literature review: E9b (rewrite-aware influence locator), 2026-10-06

Subagent (Sonnet) report, condensed by the main session. Every claim below came from a fetch-tool summary or an abstract unless noted. 13 PDFs were saved and are now in `papers/interp_methods/` (rows 60–72).

## Checked by the main session against the PDFs (2026-10-06)
- **Capability Provenance (row 62):** TrackStar via Bergson over 5,678,621 Dolma3 documents (10.5B tokens) for OLMo3-7B; about 37,000 H200-equivalent GPU-hours in all (PDF lines ~108–112, 639, 703).
- **Bao et al. (row 61):** multi-stage influence that attributes a fine-tuned LLM's predictions to pretraining data, under full-parameter fine-tuning, with EK-FAC; case study on dolly-v2-3b (abstract, verbatim).
- **Luo et al. (row 60):** influence *selects* fine-tuning examples, and their responses are then rewritten. Rewriting gives stronger, more persistent and bidirectional shifts than reweighting the same examples, and influence-selected examples give more rewriting leverage than alternative selectors (abstract, verbatim). The paper's selection score is ordinary influence, not g_orig − g_rew, as far as the abstract shows.
- **Jacob's fragility paper (row 74):** saved; page 1 checked.

## 1. Influence for replacing or editing an example
- Koh & Liang 2017: perturbation influence, the ancestor of scoring a replacement.
- Kong, Shen & Huang 2022 (RDIA, ICLR): influence under relabeling; they prove relabeling dominates deletion under cross-entropy. Small models only. Not captured.
- Luo et al. 2026 (row 60): the closest precedent (see above). Fine-tuning data, not pretraining.
- Infusion (row 68): influence-guided document edits that *induce* a behaviour, the mirror of E9b. Mostly CIFAR-10.
- IF-GUIDE (Coalson, Bae, Carlini, Hong, arXiv 2506.01790) and an AAAI paper, "Correcting LLM Behavior via Influence Function" (inaccessible): influence to find harmful data, then reweight or remove it. Neither rewrites.
- **Conclusion:** no one has scored replacing an LLM *pretraining* document with a rewritten version using (g_orig − g_rew)ᵀH⁻¹∇Q.

## 2. Attribution on LLM pretraining data at scale
- **Grosse et al. 2023 (row 66):** EK-FAC up to 52B parameters; influence is sensitive to key-phrase order.
- **TrackStar (row 64):** up to 8B parameters, more than 160B tokens; optimizer-state correction plus random projection.
- **LoGra (row 65):** gradient projection that exploits backprop structure; claims up to 6,500× faster.
- **Bergson (row 63):** EleutherAI's library (TrackStar, Source, MAGIC).
- **Capability Provenance (row 62):** the OLMo3-7B/Dolma3 precedent and cost anchor (above).
- **dattri-LLM (row 71)** and **Kronfluence:** libraries. Kronfluence scales to Llama-3-8B with FSDP.
- **Nishida et al. (row 70):** influence across Pythia's pretraining checkpoints.
- **Bao et al. (row 61):** multi-stage EK-FAC for LLMs.

## 3. Multi-stage attribution (pretraining, then fine-tuning)
- **Source** (row 59): the formal framework; LLM-scale multi-stage use is untested.
- **Bao et al.** (row 61): the LLM-scale precedent. Not yet checked: whether it suits a short LoRA fine-tune.
- **Engstrom et al. 2025, REPLAY** (row 69): exact metagradients through training. Its multi-stage use is unverified.
- No paper found differentiates a safety metric through a short LoRA fine-tune back to pretraining data.

## 4. Failure modes
- **Form over content** (row 58; Grosse et al.'s phrase-order result): a serious risk when an original and its rewrite differ mostly in form.
- **Li et al. 2024, "Do Influence Functions Work on LLMs?"** (row 67): influence performs poorly. The causes are iHVP error, unconverged fine-tuning, and parameter changes not tracking behaviour; representation similarity beats it. This is a direct threat to E9b's premise.
- **Li et al., "Which Influence Are We Estimating?"** (arXiv 2609.31214; not captured): rankings differ across counterfactual specifications (removal, upweighting, relabeling) even without approximation error. A replacement score is another specification, so its faithfulness can't be assumed from removal-based results.
- **Yadav et al. (row 72):** influence scores can be manipulated.
- **Epifano et al. 2023 (row 74):** influence is less fragile than claimed once retraining artifacts are controlled, and Spearman is not an effective evaluation metric. This supports the extremes test over rank correlation.

## 5. Closest prior work and the gap
No paper combines all three of:
- (a) pretraining scale;
- (b) scoring a replacement;
- (c) a query differentiated through a downstream fine-tune.

Pairs exist: (a) with (c) partly in Capability Provenance and Nishida et al. (neither differentiates through a fine-tune), and (b) with (c) partly in Bao et al. and Source (neither scores a replacement). The domain match is EleutherAI's EM attribution line (Paulo et al.; Jaburi et al., row 73, MISSING behind OpenReview's wall), which works on fine-tuning data and removes rather than rewrites. The combination looks new, but each component has a documented failure mode.

## 6. Inaccessible
- Jaburi et al., the NeurIPS MechInterp workshop paper (row 73): blocked by OpenReview's verification. It needs a browser.
- The AAAI "Correcting LLM Behavior via Influence Function" paper: returned 403.
- RDIA and arXiv 2609.31214: not captured.
