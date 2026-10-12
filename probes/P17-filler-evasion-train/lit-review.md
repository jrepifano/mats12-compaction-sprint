# P17 literature review: trained evasion of a reader of a value the task needs

2026-10-12, by P17's literature sub-agent. Free work only: no paid calls. It covers the two papers the plan named as uncaptured, plus a citation and keyword search for 2025–2026 work.

## Answer first

**The gap claim holds, but its wording needs narrowing.** We found no work that trains a model against a reader of an intermediate value computed without CoT (one that appears in neither the input nor the output) and then measures whether the uplift survives. We also found none that trains against the logit lens, tuned lens or J-Lens. Brauer et al. name this as an open question: "can a model trained to use extra tokens evasively … still be decoded by a more sophisticated auditor?" ([Brauer, Verdun and Marks 2026](#brauer-verdun-and-marks-2026), §8).

The plan currently says that in prior work "the evaded concept is one the task does not need". Two papers break that sentence, so it should be qualified:
- **[Bailey et al. 2024](#bailey-et-al-2024), §5:** obfuscates monitors of "the model is writing SQL" while the model must still write SQL, and measures the cost: SQLEval correctness falls from 34.8% as more layers are obfuscated.
- **[Westphal et al. 2026](#westphal-et-al-2026):** fine-tunes a model to evade probes that decode a secret the task needs (the model must encode it in its output). The secret survives: exact match 58–79%, benchmarks −1 to −8 points.

Neither is P17's setting. In Bailey et al. the concept is visible in the output text and the attack is an input embedding suffix, not weight training. In Westphal et al. the secret is copied from the prompt and carried by the output tokens, not computed latently.

## The two papers the plan asked for (both read in full)

**[Gupta and Jenner 2025](#gupta-and-jenner-2025), RL-Obfuscation** (interp row 98).
- **Setup:** GRPO with no gradients through the monitor. Guardrail-removed Llama 2 7B, Llama 3 8B and Qwen 2.5 14B are rewarded for harmful completions (StrongREJECT) and for evading a fixed harmfulness probe at one layer (§3).
- **What is evaded:** linear and MLP probes with mean or median aggregation. Evasion transfers to probe types it wasn't trained against (Fig. 6), and to probes retrained on the backdoored model from trigger-free data (§4 RQ1, Fig. 3).
- **What holds:** max-aggregated and attention probes (Table 4: attention evasion 0.01–0.02; linear-max reaches 0.99 evasion only by collapsing quality to StrongREJECT 1.0).
- **Mechanism:** about 70% of the recall drop comes from changed output tokens (HTML, punctuation), the rest from changed activations (§5 RQ1, Fig. 5a). The authors explain the robustness of max and attention probes through that token-level strategy (§4 RQ2, App. F).
- **Layers:** probes at late (layer 30, evasion 0.97) and early (layer 4, 0.90) layers are far easier to evade than mid-layer ones (0.26–0.34) (App. G, Table 5, Llama 3 8B).
- **Retraining:** replacing 5% of the probe's training data with evasive generations restores it (App. N).
- **Cost:** MT-Bench is unchanged or up (App. J). The concept is not a value the task computes.

**[Bailey et al. 2024](#bailey-et-al-2024), Obfuscated Activations** (interp row 99).
- **What is evaded:** embedding-suffix, hard-prompt and backdoor fine-tuning attacks beat linear, MLP and mean-difference probes, SAEs and OOD detectors (Gaussian, VAE, Beatrix, TED), on Llama-3-8B-Instruct and Gemma-2-2b-it.
- **Transfer:** obfuscating against generation-based probes transfers to input-based ones, but not the reverse. Obfuscating against OOD detectors transfers to input-based probes (Fig. 4c, Fig. 18).
- **SQL result (§5.3, Fig. 15, App. C.4 Table 2):**
  - Model and data: Gemma-2-2b-it, GemmaScope SAEs, 210 SQLEval questions.
  - Baseline: 34.8% correct without obfuscation.
  - Obfuscating a monitor over more layers lowers correctness, for all four monitors. The 95% CIs of Pearson r (layers obfuscated vs SQLEval) are SAE raw [−0.879, −0.765], SAE+LogReg [−0.916, −0.801], LogReg [−0.881, −0.697] and MLP [−0.820, −0.574].
  - Read off the figure (approximate): the fitted lines end near 15% (LogReg, MLP) and 2–6% (SAE monitors) at all 26 layers. With only a few layers obfuscated, the points sit near 25–34%.
  - Recall stayed near zero at every layer count for SAE raw, LogReg and MLP. Only SAE+LogReg got harder to evade with more layers.
- **Retraining:** after 70 rounds of attack and retraining, the probe catches every past attack and never the next one (§3.5).
- **A fixed probe is not enough:** a backdoor obfuscated against one logistic probe still failed against a probe refit on the validation split. The authors needed 48 orthogonal probes (App. B.2).
- **Limit:** a suffix cannot make activations near-orthogonal to the originals while keeping behaviour (§6.3).

## Other relevant work

- **[Westphal et al. 2026](#westphal-et-al-2026)** (interp row 100; main text read in full) is the nearest result in mechanism.
  - Its probes are refit on the evader's own activations with the secret as label, which is the same design as P17's measure 3.
  - It evades them by training against ridge critics that are refit every few steps, not against one fixed reader. Held-out MLP probes are evaded too.
  - "Evades" means the base model's band was reached on at least one tracked layer, under an all-layer detection rule.
  - The theory and Fig. 3: evasion hides the secret in interaction with prompt "slack" (paraphrase freedom). A single-template evaluation set restores detection on all five models.
- **[McGuinness et al. 2025](#mcguinness-et-al-2025)** (row 92, re-checked here). The "unseen" probes are trained post hoc on the frozen model **without the trigger** (§3.1, p. 4; restated in §4, p. 7). So the defender never sees activations from the evading state.
- **[Taufeeque et al. 2026](#taufeeque-et-al-2026)** (abstract only). RL representation drift alone, with or without a probe penalty, produces "obfuscated activations". P17's K arm (answer loss only) is the right control for this.
- **[Chaudhary and Barez 2025](#chaudhary-and-barez-2025)** (abstract only). Evasion works by "geometric shifting" of information between linear and non-linear subspaces.
- **[Jayabahu 2026](#jayabahu-2026)** (abstract only). A frozen probe and a refit probe can give opposite readings on the same trials.
- **[Kowalski et al. 2026](#kowalski-et-al-2026)** (row 24; arXiv abstract read). *Prompted* control partly evades probes, NLAs, activation oracles and the Jacobian lens.
- **[Baherwani et al. 2026](#baherwani-et-al-2026)** (neel_brainstorm row 74, §6.2 read). RL against a text monitor of filler-like traces led to reward hacks, never to encoding q2 latently in r1.
- **Nothing found** (by citation search of all four obfuscation papers and of Brauer, plus arXiv keyword search) on training against the logit lens, tuned lens or J-Lens, or on trained evasion over filler, CODI or Coconut latents. Brauer's two citing papers are a sandbagging causal model and diffusion-LLM EoS reasoning; neither is adversarial.

## What should change in P17's design

1. **Re-describe measure 3.** P17's refit probes are trained on E's own evading activations with y labels. That is a stronger defender than Neural Chameleons' post-hoc probes, which never see the evading state. It matches Westphal et al.'s detector and RL-Obfuscation App. N.
   - **Prediction:** prior work expects refit probes to keep finding y. P17 trains against one fixed reader (cf. Bailey App. B.2), and Brauer's templated items leave little slack (Westphal §5).
   - **The Neural Chameleons analogue is measure 4** (the B-fit probe applied unchanged to E). Consider reporting it beside measure 3 rather than as secondary.
2. **Add a refit MLP probe** next to the ridge and logistic probes. It is cheap on the already-pooled residuals. Ridge and MLP probes failed on different models in Westphal Table 1, and information may move into non-linear subspaces (Chaudhary and Barez).
3. **Lower the prior on follow-up 5 (max-pooled probe).** RL-Obfuscation's max/attention robustness comes from a gradient-free attack that mostly changes output tokens. P17's filler tokens are fixed, its attack is gradient-based, and its penalty already uses a smooth max. Bailey's max-over-sequence SQL monitors were evaded by a gradient attack.
4. **Expect the J-Lens to transfer partly, and treat that as the informative reader.** Brauer et al. note that both lenses read only through the vocabulary projection ("a Jacobian lens reproduces our readout", §7). Evading the logit lens by moving y out of that projection could therefore hide y from the J-Lens too, while probes still find it.
5. **Expect easy evasion at layers 30–59 and a real benefit-cost risk.** Late layers are the easiest to evade (RL-Obfuscation App. G). Bailey's cost grew with the number of layers obfuscated, and P17 penalises 30 of 60. If E loses uplift, follow-up 1's λ/3 arm, or a narrower layer band, separates "the value can't be hidden" from "the penalty was too broad".

## Sources and how they were read

- **Captured verbatim in full (pdftotext) and read in full:** [Gupta and Jenner 2025](#gupta-and-jenner-2025) (all of it, appendices included); [Bailey et al. 2024](#bailey-et-al-2024) (main text and App. A–F; Fig. 15 values read off the rendered page); [Westphal et al. 2026](#westphal-et-al-2026) (main text; App. C.2 and F read, other appendices skimmed).
- **Already in `papers/`, re-read in part:** McGuinness et al. 2025 (§3.1 and §4, pp. 4–7); Brauer et al. 2026 (§7–8); Baherwani et al. 2026 (§6).
- **arXiv abstracts only (API text, verbatim):** Taufeeque et al., Chaudhary and Barez, Jayabahu, Kowalski et al. (arXiv version).
- **Search summaries only:** web searches for logit- or tuned-lens evasion and filler obfuscation, which found nothing beyond the above. A LessWrong post, "Training on probes: what's going on" (Steiner, 2026-09-08), was read as a fetch-tool summary: conceptual, with no data relevant here.

## References

#### Bailey et al. 2024
Bailey, Serrano, Sheshadri, Seleznyov, Taylor, Jenner, Hilton, Casper, Guestrin, Emmons. *Obfuscated Activations Bypass LLM Latent-Space Defenses.* arXiv 2412.09565 (v2; ICLR 2026). `papers/interp_methods/INDEX.md` row 99.

#### Baherwani et al. 2026
Baherwani, Goldstein, Panda. *Not All LLM Reasoning is Visible in the Chain-of-Thought.* arXiv 2607.22925. `papers/neel_brainstorm/INDEX.md` row 74.

#### Brauer, Verdun and Marks 2026
Brauer, Mayrink Verdun, Marks. *Reading Between the Dots: Decoding Hidden Computation across Filler Tokens.* arXiv 2607.03502. `papers/neel_brainstorm/INDEX.md` row 69.

#### Chaudhary and Barez 2025
Chaudhary, Barez. *Beyond Black-Box Obfuscation: Mechanistic Analysis and Defense of White-Box Monitors.* arXiv 2505.14300 (abstract only; not captured).

#### Gupta and Jenner 2025
Gupta, Jenner. *RL-Obfuscation: Can Language Models Learn to Evade Latent-Space Monitors?* arXiv 2506.14261 (v4). `papers/interp_methods/INDEX.md` row 98.

#### Jayabahu 2026
Jayabahu. *The Truth Was Never Gone: Perfect Aliasing in Compliant-Context Truth Probes.* arXiv 2609.10739 (abstract only; not captured).

#### Kowalski et al. 2026
Kowalski, Fonseca Rivera, Macar, Africa. *Measuring Activation Control in Large Language Models.* arXiv 2608.21664; LessWrong version in `papers/interp_methods/INDEX.md` row 24.

#### McGuinness et al. 2025
McGuinness, Serrano, Bailey, Emmons. *Neural Chameleons: Language Models Can Learn to Hide Their Thoughts from Unseen Activation Monitors.* arXiv 2512.11949. `papers/interp_methods/INDEX.md` row 92.

#### Taufeeque et al. 2026
Taufeeque, Heimersheim, Gleave, Cundy. *The Obfuscation Atlas: Mapping Where Honesty Emerges in RLVR with Deception Probes.* arXiv 2602.15515 (abstract only; not captured).

#### Westphal et al. 2026
Westphal, Douglas, Navaie, Pimentel, Rosas. *Now You (Still) See Me: Detecting Evasive Steganographic Payloads in LLMs.* arXiv 2606.09411. `papers/interp_methods/INDEX.md` row 100.
