# P15 literature review (2026-10-12)

Scope: sources behind P15's "Closest prior work" and a ~20-minute search for newer (2026) work that compares reading a latent-CoT model's latents against its verbal CoT or a self-report, or predicts the latent mode's errors from its latents. Sources are in `papers/neel_brainstorm/` (INDEX rows 75-84). "Verbatim" means the primary text was read; "summary" means a fetch-tool summary or abstract only. No paid calls were made.

## Bottom line

1. **Nobody found doing P15's comparison.** No source compares latent readers against the same model's verbal mode (or a Patchscope self-read) as predictors of the latent mode's answers and errors. The closest is Dilgren & Wiegreffe (below), which predicts correctness from decodability but has no verbal baseline.
2. **Soften the "gap" sentence in plan.md.** Dilgren & Wiegreffe 2026 (arXiv 2604.04902) already show that decoded reasoning traces are present far more often for correct than incorrect CODI/Coconut answers ("interpretability itself can be a signal of prediction correctness"). The remaining gap is the monitoring comparison (against V, VT, K) and prediction of the exact wrong answer, not "whether latent reads relate to errors at all".
3. **Stage B has a theory risk from Liang & Pan.** Their account says deeper chains of bijective-style updates are the regime where CODI fails to roll out (details below). Our chain ops (+a, -a, x2, x3 over integers 1..199) are non-modular and mostly invertible. See "Plan flags".

## Per source

**Liang & Pan 2026, arXiv 2602.00449 (READ IN FULL, main text, appendix E, Table 1; the theory proofs in appendix J were skimmed).**
- Not an LLM. CODI is trained from scratch on a modular polynomial iteration task: s_t = s_{t-1}·x_t + 1 mod m (m = 50 default), a 3-layer, 2-head GPT-2-style model, 6 latents, 256-d projection, lr 3e-4 AdamW cosine, 1,000 epochs, batch 256, 2,500 examples per sequence length, curriculum over lengths 1..n+1, distillation weight 1.
- 2-3 hops: bridge states (s2, s3) decodable across latents (logit lens mean probability 0.36-0.71 for s2), final input x routed by a direct copy path to [Ans]; answer by late fusion. Patching the latent positions rescues x2/x1 corruptions and does nothing for the last input.
- n >= 4 hops (tested 4, 5, 7, 9, 31): only the last intermediate s_n (sometimes s_{n-1}, s_n) is decodable. Not a full rollout.
- Prime moduli (e.g. 41, 43, 47): CODI accuracy collapses (8.7%, 8.6%, 8.3% vs 66-91% at composite moduli, Table 1) and the s_n signature vanishes. Their explanation: composite moduli contract state (many-to-one), so only a short suffix matters; prime moduli are bijective, so the full history matters, which a fixed latent budget can't carry.
- No verbal-vs-latent agreement, no per-item error analysis, no probes of errors. Probes are trained on correctly answered runs only. Probe training: Adam 1e-3, batch 64, 100 epochs.
- **Bears on P15:** supports Stage B's design (known intermediates, hop count as a variable) and says to expect "last intermediates only" readability at k = 4-5, which is how plan P3/LL for Stage B should be read. Generalisation from a 3-layer toy to a 1B LLM is unestablished.

**Cywiński et al. 2025 (row 5, read verbatim earlier).** Already the base of the plan; nothing new here. Dilgren & Wiegreffe reuse its three-mode recipe.

**Dilgren & Wiegreffe 2026, COLM, arXiv 2604.04902 (abstract, setup, sections 4-6 skimmed; not read in full; added beyond the brief).**
- Coconut and CODI on GPT-2 and Llama-3.2-1B, including 12 Cywiński-style three-mode (noCoT / explicit / latent) models. On PrOntoQA and ProsQA latent tokens are almost never needed (early stopping leaves predictions unchanged); on GSM8k-Aug they are used, less than explicit tokens.
- Gold reasoning traces are found in top-10 vocabulary projections for 65-93% of correct predictions (Llama CODI 65-71% with question numbers allowed as operands, 8-17% without), and for 24-36% of incorrect predictions; in those cases a wrong trace is encoded more strongly. 46.5-56.0% of incorrect predictions still have the right answer in the top-10 at the answer position.
- A method to decode a verified trace without the gold trace finds one for most correct predictions and a minority of incorrect ones.
- **Bears on P15:** closest prior work to the error question. Supports P4 (latents often hold the correct intermediates when the answer is wrong). Does not compare with the model's verbal answer and does not predict the exact wrong answer. The three-mode setup is the same idea as our V baseline, but they use it to compare accuracy, not as a reader.

**Final Checkpoints Are Not Enough, arXiv 2607.06648 (abstract, intro, setup and main CODI results skimmed; not read in full).**
- CODI's counterfactual responsiveness (change the input graph so the answer should flip) is 0.038 at the selected GPT-2 checkpoint versus 0.725 for explicit CoT and 0.003 for no-CoT; it is about 0 from epoch 1 on. Same ordering with Llama-3.2-1B. Accuracy hides this.
- **Bears on P15:** CODI latents may add little causal load on some tasks. This is the case LPq (prompt-only control) is built for; expect O4-style outcomes to be live. Also a reason Stage B's P7 (latent uplift on C) might fail.

**Dynamics Within Latent Chain-of-Thought, arXiv 2602.08783 (abstract, intro, findings skimmed; not read in full).** Official CODI GSM8K checkpoints (GPT-2, Llama3-1B) plus Coconut. A few latent steps carry most causal leverage, influence is non-local, and output-level preference appears earlier than representational commitment. Bears on P15: the choice of "which latent cell to read" is non-obvious in Stage B, supporting the plan's cross-fit best-cell reporting; it says nothing about errors or verbal baselines.

**Realmbird 2026, LessWrong "Latent Reasoning Sprint #1" (SUMMARY ONLY, fetch tool; capture is a paraphrase).** Uses Cywiński's `codi_llama1b-answer_only`. A tuned lens (500 GSM8K samples, 3 epochs) shows the answer at odd latents where the plain logit lens does not; lenses trained on CODI latents did not beat text-trained ones; detection in top-k peaks at even latents. Qualitative, figure-only, no patching, no verbal comparison, no errors. Sprint 2 (search snippet only) says the CODI tuned lens fails to generalise to non-CODI activations. Bears on P15: F3 (tuned lens as another reader) is a cheap follow-up; plain-LL-only readers may understate readability, which matters for P2/P4.

**Scratchpad Thinking (NeurIPS 2025 workshop; ABSTRACT ONLY, paraphrase; no arXiv id, OpenReview cW6vOUYwCB).** CODI latents alternate: even steps store numbers, odd steps compute. Interventions on numerical features hurt most at the storage steps. Model size not confirmed (plan says GPT-2). Bears on P15: suggests which latent positions to read (our LL cells latent 3 and 5 are Cywiński's, and LP uses all 7 positions, so no change). A critique (Aswal et al., arXiv 2606.12689, abstract level) finds CoT-only controls also reproduce the alternation.

**Do Latent Tokens Think?, arXiv 2512.21711 (abstract, intro, steering section skimmed).** COCONUT (not CODI) on LLaMA 3 8B and others: latent tokens barely react to steering and the model exploits dataset artifacts on MMLU/HotpotQA. Bears on P15: a caution that readable latents may not be used; again the LPq control. Not CODI, so it is weak evidence for our checkpoint.

**Ulterior Motives, arXiv 2604.23460 (ICLR 2026; abstract, intro, probe section skimmed).** GPT-2 (124M), Coconut curriculum, backdoored dual trigger on MoralChain. Linear probes on latents transfer from armed-and-acting to armed-but-benign with >88% accuracy, peaking at the first latent. Bears on P15: a probes-as-monitor precedent on latents in the safety setting; no verbal baseline, no CODI, no errors; fine to cite as motivation for LP.

**CODI, Shen et al. 2025, arXiv 2502.21074 (abstract, results and appendix training details read; interpretability section skimmed).**
- Recipe: LoRA r 128, alpha 32, effective batch 128, cosine, 3% warm-up, bf16, alpha = beta = 1. GPT-2: lr 3e-3, gamma 1, 40 epochs (~36 h, one A100). Llama-3.2-1B-Instruct: gamma 20 (its distillation loss is much smaller), 10 epochs (~48 h on one A100); the learning rate reads "8e4" in the extraction, almost surely a lost minus sign (8e-4, check the PDF before citing it). Plan.md's 2e-4 for continued training is 4x lower than that, consistent with its stated reasoning. Upstream checkpoint hyperparameters should be read from the Cywiński repo's script at the pinned commit, not from this paper.
- Interpretability claim: logit-lens projection of the latents gives readable intermediate results (search snippet: >75% decoding of valid intermediates for chains up to 3 steps; not verified in the text here).
- Llama-1B CODI reaches ~90% of CoT-SFT; their CoT data hurt Llama (they attribute it to style mismatch). Bears on P15: a reminder that the verbal baseline V may be weak on some formats; V is a floor by design.

**Aswal et al. 2026, arXiv 2606.12689 (abstract and intro only; added beyond the brief).** Matched non-latent controls reproduce Coconut/CODI patterns, so decodability or static structure alone does not establish mechanism; argues for causal measures. Bears on P15: the patching replication (Stage A) is the right kind of check; keep the plan's claim limited to prediction of a_L.

## Newer-work search (2026, ~20 min, 6 searches)

Hits (all 2026): Dilgren & Wiegreffe (2604.04902, closest), Aswal et al. (2606.12689), Duraipandian et al., "Interpreting Latent CoT Reasoning as Dynamical Systems" (2607.09698; CODI as a stable attractor, Coconut unstable; no error prediction), Kshirsagar, "The Weight of Silence" (2607.20952; chess latent CoT + RL, latent thoughts barely consulted; abstract only), and Redwood's "Latent reasoning architectures would undermine CoT, our strongest oversight tool" (https://blog.redwoodresearch.org/p/latent-reasoning-architectures-would; title-level hit, not read; motivation rather than evidence). No hit compares latent readers with the model's verbal CoT or a self-read as predictors of latent-mode answers or errors. Search limits: the web-search tool is US-only and snippet-based, so an arXiv paper with unusual wording could be missed.

## Plan flags

- **F-1 (Stage B, risk of failure):** Liang & Pan's compressibility theory predicts that bijective step updates stress the fixed 6-latent budget (prime-modulus collapse to ~8% accuracy). Our ops are +a, -a, x2, x3 on integers in 1..199; nothing contracts the state, so at k = 5 the latent mode may have to carry a 5-step history. Their toy needed 1,000 epochs and a length curriculum (1..n+1). The plan's 2 epochs on 24,000 items at lr 2e-4 may be too little to move a 1B model to 3-5 hops. The plan already has the right fallbacks (P7, P8, the error check), but consider (a) including k = 1-2 items in C-train as a curriculum, cheap and in the spirit of their training, and (b) expecting the Stage B error check to fail on the high side (accuracy < 15%), where F5 would not help. Decision for the launching session, not changed here.
- **F-2:** Reword the plan's "Gap" sentence per bottom line 2. Add Dilgren & Wiegreffe to the closest prior work, and say Liang & Pan's model is a 3-layer from-scratch toy (the plan currently says only "polynomial-iteration tasks").
- **F-3:** Cite that the Liang & Pan finding concerns which intermediates are visible (last one or two at n >= 4), so for Stage B, LL "any intermediate at any latent" should be reported per hop index, not pooled.
- **No source already does the verbal-baseline comparison**, so there is no reason to cancel or reshape the probe on that ground.
