# P42 literature review: OOD detection for post-training coverage gaps

Merged on 2026-10-13 by agent p42 from three inputs:
- **The proposal:** Stefan Heimersheim (GDM), "OOD detection for alignment – summary for MATS scholars", posted by Neel 2026-10-13 (`/Users/jacob/Downloads/OOD detection for alignment – summary for MATS scholars.pdf`, read in full with `pdftotext -layout`; not committed). Summary in §0.
- **Internal review** (launching session, 2026-10-13): `/private/tmp/claude-501/-Users-jacob-Documents-GitHub-mats12-neel-exploratory/64567087-49c3-4eaf-90fb-aee711bdb3d0/scratchpad/slate/litrev/OOD-internal.md`, reproduced in §2 (wording unchanged).
- **External review** (launching session, 2026-10-13): `.../slate/litrev/OOD-external.md` (same folder), reproduced in §3 (wording unchanged). Its read status was "fetch-tool summary of abstract/HTML; no PDF read in full". p42 then captured the sources into `papers/ood_detection/` (PDF gitignored, `.txt` committed) and checked the load-bearing claims against the text; §1 lists the corrections, and `papers/ood_detection/INDEX.md` has the verified notes.

§1 also adds what p42 found on 2026-10-13 with the free HF API (data access, Dolci composition), which changed the design: **OLMo-3-7B is the primary model** (Jacob, 2026-10-13, via the launching session), so in-distribution = Dolci Instruct DPO prompts and the deployment pool = LMSYS-Chat-1M deduplicated against Dolci SFT and DPO.

## 0. The proposal in brief
- **Framing:** alignment = (A) right rewards in training + (B) generalisation to deployment. Plan B for (B): make deployment never OOD relative to post-training, by finding coverage gaps with OOD detection, TDA or MAD, and flagging them or expanding the data. Teaching Claude Why is the motivating claim (agentic-misalignment blackmail = reverting to the pretraining prior where safety training lacked coverage). Feng et al. 2026 is the closest literature.
- **Metrics Stefan proposes:** (1) upsampling: at 0.1% OOD prevalence, precision of the top flags as a function of sample size; (2) do we flag *useful, diverse* gaps, or just trajectory length? The goal is to flag all OOD prompts, not only misbehaviour; an autorater can run on the flags later.
- **Methods:** prompt or prompt+response; Mahalanobis, kNN, SAE FVU; the task is easier than general OOD (Li et al. 2025) because we only need OOD-ness relative to post-training (Zhang et al. 2025). **Differential methods** using the pre/mid-trained and post-trained models: Delta-Mahalanobis, Delta-kNN, Delta-FVU ("far from post-training but close to pretraining"), token-forced logprob differences and pretrain-vs-posttrain probes, **on on-policy model responses, not dataset responses**.
- **Benchmarks:** (1) *Solved:* planted medical/legal vs OLMo post-training, AUROC up to ~0.99 (best: Delta-SAE FVU >0.99), "though maybe we need AUROC >> 0.99". (2) *Current, the one he's most excited about:* gaps between a post-training set (OLMo) and deployment-like data (LMSYS, WildChat). Methods kept flagging long prompts; he could not solve length dominance without manual stratification ("haven't tried hard"). Clustering the flags gave tentative categories (long prompts, templated spam), but he is suspicious of the clustering and autorating. Wants an iterative known-OOD set so the same gap isn't flagged again. (3) Jailbreak and (4) backdoor detection: side tasks, easy to fool yourself, didn't beat the right baselines.
- **Models:** mostly OLMo 3 7B and OLMo 2 1B, focused on the DPO stage with SFT as the "base". Request HF dataset access early.
- **Fine-tuning experiment (side):** artificial constitutions; OOD scores did not separate generalisation failures from successes (weak, rollout noise at T = 0.7 mattered).

## 1. Additions and corrections by p42 (2026-10-13)

### Data access (free HF API, our token, 2026-10-13)
| Repo | Revision (sha) | Access |
|---|---|---|
| `allenai/Olmo-3-7B-Instruct-SFT` | `e1452fc572d51966ff4aaeb25118b891eb93e549` (= E1 registry) | open |
| `allenai/Olmo-3-7B-Instruct-DPO` | `b33130b7de49f0c2553b5c2b3bc8409ff3e627d1` (= E1 registry) | open |
| `allenai/Olmo-3-1025-7B` (base) | `a81bae42db3975be1671e27b9c9a56da1a9f980f` (= E1 registry) | open |
| `allenai/Dolci-Instruct-DPO` (259,922 pairs) | `aed155cf32e809b590490b6c3577ee4b0d0a5019` | open |
| `allenai/Dolci-Instruct-SFT` (2,152,112 rows) | `bd3c8f3a9b2cc5a9682e44b96ddd0bb2ff027221` | open |
| `lmsys/lmsys-chat-1m` | `200748d9d3cddcc9d782887541057aca0b18c5da` | **gated (auto-approve); our token gets 403 on data files. Jacob must click through on HF.** |
| `allenai/WildChat-1M` / `WildChat-4.8M` | `7d6490e4…` / `c827c6df…` | open |
| `allenai/WildChat-1M-Full` | `04877f99…` | gated (manual), 403 |
| `allenai/wildguardmix`, `allenai/wildjailbreak` | — | gated (auto), 403; not needed |
| `decoderesearch/olmo-3-saes` (BatchTopK Matryoshka, k = 100, layers 4/16/28, trained on `Olmo-3-1025-7B` base) | `ad1f790dd36d44b5b77b1d08ace6a6f48d121649` | open; unvalidated |
| OLMo-2-0425-1B {base, SFT, DPO, Instruct}, `olmo-2-0425-1b-preference-mix`, `tulu-3-sft-olmo-2-mixture-0225` | SFT `0d85a3d0…`, DPO `c4b04859…` | open (only used to debug the attribution scorer) |
| `yoheikobashi/SAEs-OLMo-2-0425-1B-dolma` (a model repo, not a dataset) | `d53889a3…` | gated (auto), 403; not needed |
| Planted-category sources: `lavita/ChatDoctor-HealthCareMagic-100k` (`505443ea…`), `ymoslem/Law-StackExchange` (`ab2dbaad…`), `allenai/coconot`, `CohereForAI/aya_dataset`, `TrustAIRLab/in-the-wild-jailbreak-prompts` | — | open |

### What the post-training data contains (cards plus one DPO shard)
- **Dolci Instruct SFT card:** 2.15M rows, including **302,406 WildChat prompts** (GPT-4.1 responses), Aya (99,987, multilingual), WildGuardMix (49,373), WildJailbreak (49,965), CoCoNot, FLAN v2, OpenThoughts 3, persona math/code, Precise IF, tool use (227,579), logic puzzles and verifiable reasoning. Some prompts were dropped by "filtering … by topics within the Azure API (blocked requests)": a known, unmeasured coverage hole.
- **Dolci Instruct DPO card:** 260k pairs: 125k delta-learning pairs, 125k GPT-judged pairs, 10k multi-turn.
- **One DPO shard measured by p42** (`train-00000-of-00004`, 64,981 rows): 3.8% multi-turn; first-user-turn length percentiles (characters) 10/50/90/99 = 82 / 513 / 1,877 / 10,681. Largest prompt-id sources: verified IF, OpenThoughts science, UltraFeedback, FLAN v2, WildChat ("filtered_wc_sample", "Wildchat-1M-gpt-4.1-regenerated-english", "…-not-eng"; about 12% of the shard together), WildJailbreak, WildGuardMix, SciRIFF, Aya.
- **Consequences:** WildChat is largely in-distribution for OLMo-3 post-training, so it can't be the deployment pool; **LMSYS-Chat-1M is the deployment pool**, deduplicated (exact and near-duplicate) against Dolci SFT and DPO first-user turns. Jailbreaks and non-English text are *in* post-training (WildJailbreak, Aya, non-English WildChat), so a jailbreak or language flag is not automatically a gap. The DPO prompts are much longer than LMSYS's (LMSYS first turns are mostly short), so the length confound points in a known direction: raw OOD scores will flag *short* or *very long* prompts depending on the scorer.

### Corrections to the external review after reading the captured texts
(Filled in from the verification pass; see `papers/ood_detection/INDEX.md`.)

A sub-agent checked each claim against the `pdftotext` extractions (2026-10-13); quotes and page numbers are in `INDEX.md`.
- **Feng (MOOD):** verified. 85,164 HH-RLHF-prompt training conversations; 7 OOD failure sets plus a benign Swahili set; recall at 1% FPR goes from 38.8% to 45.7% (Gemma 2 9B, guard + perplexity + Mahalanobis; Mahalanobis alone gives 44.8%); Swahili 98.4% → 7.6% with 20 samples. The responses are fixed, LLM-generated **dataset transcripts**, not on-policy. Code: `github.com/Dylan102938/mood-bench`.
- **Zhang:** the score is p_pretrained(x) / p_finetuned(x), a **whole-sequence likelihood ratio, not token-normalised**. For QA, scoring the question alone works poorly, so they score a generated answer. Their §6 notes that raw likelihood favours short texts. P42 reports both the sequence-sum version (Zhang's) and a per-token mean.
- **Saghir:** "|r| ≥ 0.61" is **different**: the only correlations given are |r| = 0.63 and 0.71, for two attention-ratio features in an appendix truncation sweep. Verified: 0.919 → 0.530 under length matching (Spam), and backwards ranking at 0.43–0.47. The paper offers no deployable fix; its advice is to check the ranking direction under length matching.
- **Lyons Keenan:** verified on LLM backdoors (theirs vs Mahalanobis: Gemma 2-2B 0.999–1.000 vs 0.78–0.94; Llama 3.1 8B 0.987–0.992 vs 0.79–0.97; complex trigger 0.982 vs 0.554). OOD detection was tested only on CIFAR. **Cost ≈ 26 s per sample at 8B on an H100**, about 550 GPU-hours for P42's 76k prompts, which is why P42's S4 is a LESS-style projected-gradient kNN instead.
- **LESS:** the length sentence is verified verbatim (§3.2, p.3). Their projection uses LoRA rank 128 on attention, a warm-up model, Adam-preconditioned gradients and a Rademacher projection to 8,192 dimensions. P42's S4 is a cheaper variant (raw SGD gradients of the final DPO model, factorised projection on MLP down-projections), noted as such.
- **Liu 2023:** verified. Cosine distance (max cosine, effectively 1-NN) beats the other detectors on LLaMA 7–65B.
- **Olmo 3 report:** the Instruct pipeline is SFT → DPO → RL. The DPO prompts total 259,922, **of which WildChat is 30,248 (11.6%)**, matching p42's shard estimate of ~12%. Decontamination is against evaluation sets only, with no deduplication against LMSYS, which P42's own deduplication covers. WildChat was topic-filtered.
- Verified in one line each: T3, Bohacek, FAC, Johnston (no detector works across models and tasks), Zhong/D5, Bejan (thresholding self-influence filters poorly), OLMoTrace, Clio. Hollander's post: `https://www.lesswrong.com/posts/hsrjuzqokvAErvZ2q/coverage-driven-alignment-what-teaching-claude-why-can` (search snippets only).


## 2. Internal review (verbatim from the launching session)


### 1. Related internal sources
- **Nothing in `papers/` is on OOD detection, coverage gaps, Mahalanobis/kNN scoring or mechanistic anomaly detection.** The summary's three anchors are MISSING: Feng et al. 2026 (guard models), Li et al. 2025 (OOD is hard) and Zhang et al. 2025 (pretrain vs post-train OOD). Capture them before any plan.
- **Motivation:** Teaching Claude Why (`papers/em_personas/` row 51) hypothesis 3: "where safety training lacks coverage, the model reverts to its pretraining prior". Model Spec Midtraining (row 50) is about generalisation, not coverage.
- **Neel central doc** (`scratchpad/slate/neel_central_doc.txt`):
  - "Data dredging … lmsys-chat-1m … benchmark anomaly-detection methods" (l. 873–880) is the closest item.
  - Also: predicting a model from its post-training data (l. 848); hereditary-trait filtering failures (l. 648).
- **TDA literature already captured:**
  - `interp_methods` rows 57–75, 82, 83, 86, incl. Kim et al. (gradients track format) and Li et al. 2024 (representation similarity beats influence).
  - **Xiao & Aranguri row 83:** activation-difference attribution on OLMo 2 DPO pairs cut the target behaviour 63%. It beat gradient TDA and LLM judges, at >10× lower cost. This is the closest OLMo-DPO precedent.
  - **Rows on origin/main** (the local index is stale):
    - Weckbecker et al. (row 85): EK-FAC and GradCos are "near-inert on OLMo-3-7B" at sample level.
    - Aden-Ali et al. LLS (row 84): a per-row log-likelihood ratio is a natural detection baseline, run on OLMo-2-1B/7B DPO.
    - Engels & Nanda (row 74) and Conmy (row 75): dropping flagged subsets barely helps, because "adjacent behaviour leaks in".
  - **Paulo et al. (row 36):** on a 50/50 mix, a black-box harmfulness classifier separated bad advice better than EK-FAC (AUC 0.9998 vs 0.917).

### 2. Our results and what they imply
- **E7 / working-repo TDA failure** (`../mats12_neel_working/logs/phase1-report.md`:4460–4500; `docs/research-proposals.md`:391–415):
  - The best locator (damped-Fisher IF) reached Spearman 0.867 on 10 development groups and **−0.047 on 20 held-out groups**, with a null band of ±0.44.
  - Every gradient locator failed or was inconclusive held out (GradDot 0.275, EK-FAC 0.090).
  - The retrain target's ICC was 0.58.
  - A $2 content judge beat influence on rewrites (3/3 seeds × 3 judges).
  - **Lesson:** freeze choices before held-out data, validate against a null, and expect attribution to lose to text baselines when the signal is visible in text.
- **Black-box baselines keep winning:**
  - P14: luna zero-shot AUROC 1.000 vs a probe's 0.960.
  - I1a: a text-only monitor matched or beat three activation readers.
  - P11: asking Qwen gave 62/63, the lens 34/63.
  - P15 and P16: the model's own answer matched or beat the readers.
  - **Implication:** any OOD score must beat (a) prompt length, (b) a text-embedding kNN, and (c) the model's own NLL before activation or attribution claims mean anything. Stefan's "length dominates" is the same failure mode.
- **E1/E1b (OLMo-3-7B):**
  - Pinned base/SFT/DPO/Instruct and stage-checkpoint revisions.
  - E1 cost $58.56 (13.6 H100-h); E1b cost $41.99.
  - The pipeline (vLLM, luna, residual hooks) works on OLMo-3.
- **E16** (gradients → steering) is signed off for Stage 1 only. Its prior was "activations do as well".
- **P22:** removing a dominant nuisance axis (caution there, length here) can also remove the signal.

### 3. Reusable code and assets
- **This repo:** `src/emx/e1/registry.py` (pinned OLMo-3 revisions); `src/emx/contrastive.py`, `src/emx/e1b/firstk.py` (residual hooks); `scripts/e1_gen_vllm.py`; `src/emx/judge_client.py`, `judge_run.py`, `stats.py` (cluster bootstrap); `scripts/probe_pod.py`; `emx.spend`.
- **Working repo:** `src/em_filter/tda.py` (graddot/gradsim, Fisher, EK-FAC, LDS); `src/em_filter/tda_pod.py` (`flat_grad`, length-sorted batching); `scripts/tda_kronfluence.py`; `scripts/tda_embed.py` (text-embedding-3-small); `scripts/tda_activations.py`; `scripts/tda_null_calibration.py`.
- **External:** Bergson (row 63). SAEs on HF, unvalidated: `decoderesearch/olmo-3-saes` (Olmo-3-1025-7B base, layers 16/28), `yoheikobashi/SAEs-OLMo-2-0425-1B-dolma`.

### 4. Dataset access (HF API with our token)
- **Downloadable now:** OLMo-2-0425-1B {base, SFT, DPO, Instruct}; Olmo-3-7B-Instruct-{SFT, DPO}; `olmo-2-0425-1b-preference-mix` (378k); `tulu-3-sft-olmo-2-mixture-0225`; `Dolci-Instruct-SFT`/`-DPO` (260k); `WildChat-1M`; `WildChat-4.8M`; `alpaca`.
- **Gated, no access yet:** `lmsys/lmsys-chat-1m` (auto-approve: click through); `WildChat-*-Full` (manual).
- **Key catch:** the OLMo-2 1B DPO mix's card lists used *and unused* WildChat prompts, so **WildChat is largely in-distribution for OLMo post-training.** Use LMSYS as deployment, or dedupe WildChat against the mix. Dolci's WildChat share is unverified.

### 5. Cohort overlap
The area isn't on Neel's crowded list.

### 6. Risks
1. Length and format dominate every score. Kim et al. found gradient TDA tracks form over content, so gradients will be worse.
2. Planted OOD of a different topic is "solved" (AUROC ~0.99), so the field can fool itself with an easy benchmark, as Stefan warns for jailbreaks and backdoors.
3. The "interesting gap" metric rests on clustering plus an unvalidated LLM judge.
4. Logprob and probe scores need on-policy generation.
5. Our TDA record is negative held out.
6. The WildChat/ID overlap above.

### 7. Cheapest one-day de-risking probe (~$15–25, ≤$40 cap)
- **Model:** OLMo-2-0425-1B SFT and DPO. One H100 for ≤3 h (~$12), or the laptop for 1B. Repeat on Olmo-3-7B only if the result is informative.
- **ID set:** 50k prompts from `olmo-2-0425-1b-preference-mix`.
- **Deployment pool:** 50k LMSYS prompts, after a click-through and deduplication against ID.
- **Planted:** length-matched categories (medical, legal, non-English, templated/agentic, DAN jailbreaks) at 0.1% and 1%.
- **Scorers:**
  - S0: token length (the floor).
  - S1: text-embedding kNN to ID (`tda_embed.py`, ~$1).
  - S2: DPO-model NLL, and the DPO−SFT logprob difference (LLS-style), on a short on-policy vLLM response.
  - S3: Mahalanobis and kNN on mean-pooled residuals at 2–3 layers, plus the Δ(SFT vs DPO) variant.
  - S4, the attribution score: kNN gradient cosine to ID, using Bergson random-projected per-example gradients, final 4 blocks. The layer and projection are frozen before scoring the pool.
- **Length control:**
  - report each score's Spearman ρ with length;
  - z-score within length deciles of the ID set;
  - evaluate the planted sets against length-matched decoys.
- **Metrics:**
  - AUROC and precision@k at 0.1% prevalence, with bootstrap CIs, after length control.
  - Tier 2: the top 200 per scorer, then k-means into topic clusters. Report the count of distinct clusters not in S0/S1's top 200, judged by luna (~$1), plus 50 items Jacob labels on an HTML page.
- **Decision:** the direction is worth a sprint only if some S2–S4 score beats S1 after length control on precision@k, or finds clusters that S1 misses.

## 3. External review (verbatim from the launching session)


Read status: "summary" = fetch-tool summary of abstract/HTML; short quotes are verbatim. No PDF read in full.

### Sources

**Cited by Stefan (ids are my identification):**
- **Feng, Srivastava, Dragan, Laidlaw 2026, "Benchmarking and Improving Monitors for OOD Alignment Failure in LLMs", arXiv:2605.21602.** MOOD benchmark: guard models (Qwen2.5/Gemma 2, 1.5–32B) trained on 85k HH-RLHF-based conversations, tested on 7 OOD failure sets (jailbreaks, tool deception, insecure code, scheming, sycophancy…). Adding Mahalanobis (final layer of the guard) plus fine-tuned-LM perplexity raises recall at 1% FPR from 39% to 45%, a larger gain than a 20× larger guard. Mahalanobis flags 98.4% of benign Swahili; 20 Swahili examples cut this to 7.6%. Code released (mood-bench). *Summary (HTML).* **Relevance:** the closest framing; it targets misbehaviour, not coverage gaps, and uses dataset responses.
- **Zhang, Xiao, Liu, Bamler, Wischik 2024/v2 2025, "Your Finetuned LLM is Already a Powerful OOD Detector", arXiv:2404.08679.** Detects OOD by the likelihood ratio between the pretrained and fine-tuned models, training-free. *Summary (abstract).* **Relevance:** basis of Stefan's logprob-difference method; a differential baseline to beat.
- **Li, …, Wilson 2025, "OOD Detection Methods Answer the Wrong Questions", arXiv:2507.01831 (ICML 2025).** Argues that uncertainty-based and feature-distance methods carry "irreducible errors"; no common fix rescues them. *Summary (abstract).* **Relevance:** feature distance ≠ OOD-ness; motivates the relative-to-post-training framing.
- **Kutasov et al. 2026, "Teaching Claude Why", alignment.anthropic.com/2026/teaching-claude-why.** Quotes: "There were gaps in our safety training…"; "the model tends to revert to the pretraining prior". Principled data (~3M tokens of "difficult advice") beats honeypot demos ~28×; OOD-ness itself is never measured. *Summary (already in papers/em_personas row 51).* **Follow-up:** Hollander, LW 2026-06, "Coverage-driven alignment" proposes coverage maps over hand-defined buckets, with no experiments and no OOD or attribution method (*summary*).

**Methods and benchmarks:**
- **Liu et al. 2023, "How Good Are LLMs at OOD Detection?", arXiv:2308.10261 (LREC-COLING 2024).** On LLaMA 7–65B, plain cosine-distance kNN beats other detectors. *Summary.*
- **Saghir 2026, "Useful Features, Backward Scores: OOD in LM Trajectories", arXiv:2605.00269.** White-box OOD scores are length-confounded (|r| ≥ 0.61). D²HScore falls from AUROC 0.919 to 0.530 under length matching. ID-fitted distances can rank backwards (0.44–0.47). No clean fix. *Summary; single author, unreviewed.*
- **Ganguly et al. 2026, "Trust The Typical" (T3), arXiv:2602.04581.** Density of safe prompts in embedding space; claims SOTA on 18 safety benchmarks. *Summary.* A strong embedding baseline.
- **Bohacek et al. (Google DeepMind) 2025/ICML 2026, "Uncovering Competency Gaps in LLMs and Their Benchmarks", arXiv:2512.20638.** SAE concept activations find model and benchmark gaps. *Summary.*
- **Li, Wu, Li, Hu, Liu 2026, "Less is Enough" (FAC), arXiv:2602.10388.** Finds SAE features missing from a seed post-training set and synthesises data for them. *Summary.* **Relevance:** find-gap-then-upsample, but no deployment set or safety outcome.
- **Zhong, Snell, Klein, Steinhardt 2022, "Describing Differences between Text Distributions", arXiv:2201.12323** (and D5, 2023). LLM proposes and verifies descriptions of corpus differences. *Summary.* Baseline for describing deployment minus post-training.
- **Mechanistic anomaly detection (MAD).** Johnston, Chakraborty, Belrose 2025, arXiv:2504.08812: no detector works across quirky models and tasks. **Lyons Keenan, Leckie, Erfani 2026 (ICML), "MAD via Functional Attribution", arXiv:2604.18970:** the anomaly score is how little a trusted set explains the test output, measured by Bayesian influence functions (SGLD loss covariance). Against Mahalanobis, AUROC is 0.98–1.0 vs 0.55–0.94 on Gemma-2-2B and Llama-8B backdoors; robust to obfuscation; many gradient passes per sample. *Summary (HTML).*
- **Training-data attribution (TDA) and differential signals.**
  - Xia et al. 2024, LESS, arXiv:2402.04333. Verbatim: gradient norm is "negatively correlated with the length of the completion", which makes influence "heavily upweight shorter training sequences"; the fix is cosine similarity.
  - Bejan et al. 2023, arXiv:2302.13959: self-influence as an outlier score.
  - Xiao & Aranguri 2026, arXiv:2602.11079 (already in papers/): activation-difference attribution on OLMo 2 DPO finds "distractor-triggered compliance" and beats gradient TDA and LLM judges.
  - Minder et al. 2025, arXiv:2510.13900: base-vs-finetune activation differences are readable.
  - OLMoTrace (Liu et al. 2025, arXiv:2504.07096): exact-span matching of outputs against OLMo training data.

### What is solved vs open
- **Solved:**
  - Planted far-OOD separation (Stefan: AUROC > 0.99); OOD scores as a guard add-on (Feng: +6–7 pts recall).
- **Open:**
  - Length-robust scoring on realistic deployment data.
  - Whether flagged gaps predict *behavioural* failure. Teaching Claude Why's premise is untested quantitatively.
  - Precision at realistic prevalence (0.1%).
  - Scoring on-policy prompt+response pairs.
  - Stopping the same gap from being flagged again.

### Is an attribution-based coverage score novel?
**Partly.** The closest prior work:
- Lyons Keenan 2026 uses the same logic ("attribution failure signals anomalous behavior"), but on backdoors, with a small clean reference set, not a post-training corpus.
- Self-influence outlier scores and LESS gradient-similarity selection.
- Xiao & Aranguri, the same model and data (OLMo 2 DPO), but they attribute known behaviours rather than score coverage.

I found no paper scoring realistic deployment prompts by top-k attribution to the post-training set against length-controlled black-box baselines. **Risk:** normalised gradient similarity may reduce to a kNN in gradient space, so it must beat embedding kNN, not only Mahalanobis.

### Baselines any method must beat
1. Length or token count alone, and random sampling.
2. Prompt perplexity under the post-trained model.
3. The Zhang likelihood ratio (post-trained vs SFT/base, token-normalised).
4. Cosine kNN on a sentence-embedding model (Liu 2023; T3).
5. Mahalanobis on final-layer states (Feng).
6. An LLM judge given ID exemplars ("is this unlike these?"), plus D5-style difference descriptions.
7. Clio-style clustering with ID/deployment cluster-mass ratios.

### Pitfalls
- **Length.** Seen in Saghir, Stefan's runs, and gradient norms (LESS). Stratify by length; normalise scores.
- **Templating and format.** Chat template, system prompt, multi-turn, code blocks, and language: WildChat is about 47% non-English, and Feng's Swahili false-positive rate was 98%.
- **Near-duplicates and leakage.** The Tulu 3 SFT mix contains 100k WildChat-GPT-4 prompts plus WildGuardMix and WildJailbreak (HF card); OLMo 2 preference data and reportedly OLMo 3 Dolci reuse WildChat. Deduplicate deployment data against post-training prompts first. LMSYS has many repeats.
- **On-policy vs dataset responses.** Feng scores dataset responses. Generate responses from the scored model, or the task becomes "is this text non-OLMo-style".
- **Metrics.** Scores can rank backwards (Saghir); report precision at a tiny fixed prevalence, not just AUROC.
- **Validation.** LLM clustering and autoraters need validating (Stefan's own worry).

### Crowding
- Moderate on "OOD as a safety monitor": Feng/Laidlaw (Berkeley, code released), T3, Stefan (GDM).
- Moderate on SAE coverage (Bohacek et al., FAC) and TDA/MAD (Lyons Keenan; Xiao & Aranguri).
- Sparse on "post-training vs deployment coverage scored by attribution". No MATS or lab paper found beyond Teaching Claude Why's qualitative claim.

### Three first-week questions (2-week solo sprint)
1. **Length.** On OLMo 2 1B DPO vs WildChat deduplicated against Tulu/OLMo prompts, with planted categories at 0.1–1% prevalence: does any of Mahalanobis, kNN, the Zhang likelihood ratio or Delta-FVU beat a length-only score on length-matched precision@100?
2. **Attribution.** Does a LESS-style normalised top-k gradient similarity score (LoRA-projected gradients of the on-policy response vs the DPO set) add precision over embedding kNN on the same planted set, after length matching?
3. **Behavioural payoff.** In the top 1% flagged by the best score, is the autorater-judged bad-behaviour rate (unsafe compliance, over-refusal, incoherence) of on-policy responses higher than in a length-matched random 1%? Tests the "OOD ⇒ generalisation failure" premise.

### Capture into papers/
2605.21602, 2404.08679, 2507.01831, 2604.18970, 2605.00269, 2308.10261, 2602.04581, 2512.20638, 2602.10388, 2402.04333, 2504.08812, 2302.13959, 2201.12323, 2504.07096, 2412.13678 (Clio), Hollander LW post, Tulu 3 SFT-mixture card. Already present: Teaching Claude Why, Xiao & Aranguri, Minder et al.
