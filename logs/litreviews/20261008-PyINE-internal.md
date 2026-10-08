# Internal literature review: PyINE as a setting for (A) attribution-filtered RL and (B) an activation-reader test, 2026-10-08

Subagent (Sonnet) report. Brief: `papers/` (every `INDEX.md`, `_litreview_text.txt`, `_neel_brainstorm_text.txt`), `docs/research-proposals.md` (X7, I5, I6, I9), the I1a, I9 and X3 reports, the X7 reviews, and `../mats12_neel_working` (`logs/phase1-report.md`), for two framings of a possible proposal built on PyINE (St-Charles et al., LawZero, arXiv 2610.04737, 3 Oct 2026). Read depth is stated per source. Most `papers/` entries below were read as INDEX rows only (plus the notes in them); I did not open the primary PDFs for this review. "Row" numbers repeat across INDEX files, so every row is given with its directory.

## 0. The source and its key numbers (PyINE text, read verbatim: abstract, §1, §3, §4, §5, Tables 1 and 5-8, App. B, E.2-E.3, F.1-F.4 in part, G.1 in part)

Source: `scratchpad/pyine_pdf/paper.txt`. I did not read the appendices on debate or the data pipeline (App. A, I) beyond the training-data paragraph.

| Claim in the brief | What the paper says | Verdict |
|---|---|---|
| Organism 69.8% vs 38.0% | Table 1: original-task accuracy 69.8 vs 38.0 (Qwen3-4B-Instruct base) | matches |
| "Falls to 10.5% on misleading variants" | Table 1: organism **10.5**, base **6.3**. Misleading accuracy went **up** in absolute terms (6.3 to 10.5). The gap to original task widens (-31.7 to -59.3) because original-task accuracy rose | **discrepancy that matters.** The base model is already hint-susceptible; RL did not take it from good to bad on misleading items |
| Training mix has helpful hints, no misleading | App. E.2: 75% original, 10% stubbed functions (intent must be inferred from context/docs), 5% obfuscated, 5% helpful hints, 5% obfuscated+helpful. No misleading variants | matches, but helpful hints are only 10% of the mix, and the 10% "stubbed" tasks also reward trusting documentation |
| Reward = correctness x length penalty | App. E.2: soft-match reward 1.0/0.0, scaled linearly from 1.0 down to 0.1 at the 10,000-token cap; no effect on wrong answers. GRPO via TRL, DAPO-style loss, G in {32, 48}, full fine-tuning, lr 5e-6, beta = 0, up to 40 H100s, effective batch 960 | matches |
| Probes AUROC 17-48 on misleading (Table 5) | Table 5 "best" probe configs, misleading AUROC: last-token L22 17.0 / 19.4 / 27.3, mean-pool L30 24.9 / 25.7 / 47.9, softmax L22 13.7 / 18.1 / 18.7. Range **13.7-47.9**, not 17-48. Original-task AUROC 84-87 for all | minor mismatch. Below-chance is the common case |
| ModernBERT 30-34 | Table 6 ModernBERT misleading AUROC: 30.5, 31.8, 34.1 (full FT); 27.6, 28.0, 36.9 (LoRA). Qwen2-0.5B classifier: 23.9-51.0 | roughly right for full fine-tune only |
| Weak judges ~chance; gpt-5-mini 93.2, gpt-oss-20b 92.8 on misleading | Table 7: gpt-5-mini 93.2, gpt-oss-20b 92.8, gpt-5-nano 84.0, gemma-4-31B 63.6, gpt-4o 54.7, CWM 37.7, nemotron-3-super 46.1, gpt-5.4-nano 44.2, gpt-5.4-mini 51.1. Compute tax 398-3225% of the organism's serving cost | matches |
| Table 7 rows for Qwen3-Coder-Next and OmniCoder-9B look identical | **Confirmed.** Every TPR and AUROC cell is identical in all three variants (e.g. misleading 50.1 +- 0.1, original 58.2 +- 1.6); only the tax differs (381 vs 148). Almost certainly a constant or degenerate score or a copy error. Treat as a data defect; do not cite either row | flagged |
| "Never measure verbalization" | Confirmed. The only statement is §5: misleading failures come with shorter traces, "suggesting that cue reliance may go unverbalized" (inference from length). App. B notes the authors do not assume the reasoning z is faithful and define faithfulness "operationally" | matches |

Additional facts that change the reading:
- **Hint-trust is not learned from a rewarded shortcut.** The wrong answer is never rewarded (confirms the brief). Fig. 9 (App. F.2, text only; I did not see the plot): misleading accuracy first *rises* in the first ~50 steps, then falls beyond step ~300 "under the pressure of the completion length penalty". So the late-training drop is attributed to the length penalty, not to the helpful-hint rollouts.
- **Probe labels.** Probes are trained on correctness of the organism's own answer, at an ~80%-correct mixture where misleading items are rare ("weak/moderate/strong" exposure presets). A probe that learns "a salient cue is present, so the answer is probably right" scores below 0.5 on exactly the items where the cue is wrong. That is an expected consequence of the label and the training mix. My inference, not a claim in the paper.
- **Capability caveat.** The organism's misleading accuracy (10.5%) is far below gpt-5 (89.5%). The "shortcut" may largely be a capability limit (Qwen3-4B cannot trace many programs), which the paper itself says it does not disentangle (App. F.3, the 10k-token and length-penalty cliff).
- **Cost to reproduce the organism.** 50+ training runs, up to 40 H100s, ~2 hours for the first 50 steps on 40 H100s (App. E.2, F.2). LoRA reportedly gave "mitigated success". Code and data are on GitHub (Apache-2.0 per the brief; I did not open the repo). A single-GPU rerun is not what the authors ran.

## 1. Closest prior work in `papers/`

| Source | Directory, row | Relevance to A/B | Read |
|---|---|---|---|
| Wong, Engels, Nanda, *Steering RL Training* | `neel_brainstorm` 54 | RL env where the hack is visible text; probe/judge screening already run; closest model for a live filter | Full verbatim (per INDEX and the X7 env review) |
| Wong et al., *Designing Effective Monitor-Based Interventions* | `neel_brainstorm` 55 | Better monitors cut hacking to 0% in medical/biography envs | Abstract only |
| Shu et al., I-PPO (*Learning from the Right Rollouts*) | `neel_brainstorm` 56 | Gradient-influence filtering of PPO episodes; closest method for A | Fetch summary only (per INDEX) |
| Hu et al., *Snapshot of Influence*; Yang et al., GradAlign | `neel_brainstorm` 57, 58 | Online-RL data attribution / gradient-aligned selection | Fetch summaries only |
| Grift (*Gradient Fingerprints*) | `neel_brainstorm` 59 | Gradient fingerprints beat a CoT monitor at detecting hacks (offline) | Fetch summary only |
| Bergen et al. (Goodfire), *Monitoring and Discovering Reward Hacking* | `neel_brainstorm` 52 | Difference-of-means probes, AUROC 0.93-0.94 on hack labels; does not train against probes | INDEX row, plus the mitigation passage per the X7 internal review |
| CATCH (Wang et al.) | `neel_brainstorm` 63 | Qwen3-4B coding RL testbed with gold labels | Full verbatim (per INDEX) |
| Kramar et al., *Production-Ready Probes for Gemini* | `interp_methods` 37 | Cited by PyINE for pooling choices; shows probe choice matters under distribution shift (the paper's own theme) | INDEX row only |
| Goldowsky-Dill et al., *Detecting Strategic Deception Using Linear Probes* | `interp_methods` 38 | Probe baseline for "is the model doing X"; trained on the right concept label | INDEX row only |
| Smith, Chughtai, Nanda, *Difficulties with Evaluating a Deception Detector* | `neel_brainstorm` 62 | Methodology for detector validation; identified by title, unconfirmed | Abstract snippet only |
| Korbak et al., *CoT Monitorability* ; Guan et al., *Monitoring Monitorability* ; Emmons et al. (*Struggle to Evade Monitors*) | `agent_swarms` INDEX_a 20; `neel_brainstorm` 8, 25 | Frame for "passive CoT monitoring can miss unverbalized use" | INDEX rows only |
| Fang & Marks, *Unsupervised decoding of encoded reasoning*; Cywinski et al., *Interpreting latent reasoning* | `neel_brainstorm` 10, 5 | Readers recovering reasoning that is not written out | INDEX rows only |
| Gurnee et al., global workspace (J-Lens); Fraser-Taliente et al., NLAs; Blank et al., WorkspaceBench | `interp_methods` 1, 4, 5 | The readers B would test; WorkspaceBench has no cue-reliance family that I know of | Full articles captured; I read the INDEX rows and our own I1a/I9 write-ups of them, not the articles |
| Karvonen et al., Activation Oracles; Fornasiere et al. (AOs underperform without a safe base); Bersia & Gaintseva (blind spots); Torrielli & Schneider-Kamp (calibration) | `interp_methods` 17, 54, 19, 34 | Known weaknesses of oracle readers on concepts the model has been tuned on | INDEX rows only |
| Karvonen et al., CHIVE (*Would this change your answer?*) | `interp_methods` 22 | Counterfactual-edit ground truth; includes hint settings (see I6 update: self-explanation training "generalized to hint settings but not reliably to open-ended ones") | Markdown paraphrase per INDEX; I read only the I6 summary of it |
| Minder et al., *Narrow Finetuning Leaves Readable Traces* | `interp_methods` 52 | Fine-tuned organisms are artificially easy to read; a caution for B on an RL-made organism | INDEX row only |
| Szablewski et al., *Model Organism Lottery* | `interp_methods` 40 | Interpretability results depend on how the organism was trained; one author (Fornasiere) is also on PyINE (LawZero). Directly relevant to B | INDEX row only |
| Kowal et al., *Concept Influence*; Kim et al., *Form over Content* ; Li et al., *Do Influence Functions Work on LLMs* | `interp_methods` 57, 58, 67 | A probe direction is a TDA variant; attribution tracks format; influence is poor at harmful-data ID | Kowal: abstract, §2-3 verbatim; others fetch summaries |
| Paulo et al., *Unequal Influence of Bad Advice*; TAME | `em_personas` 36, 44 | TDA filtering in SFT; no RL | Paulo: §1-3 verbatim per INDEX; TAME abstract only |
| MacDiarmid et al.; Qi et al., *Training a Misaligned Reward Seeker*; METR | `em_personas` 23; `neel_brainstorm` 46, 20 | Context for "shortcuts learned in RL" | MacDiarmid 7 pp; others partial |
| Betley et al., *Value Leakage* | `neel_brainstorm` 35 | An unverbalized influence on answers; Qwen models mostly *admit* it in CoT (per proposals line 2133) | INDEX row plus proposals note |
| Rajamanoharan & Nanda, *Models May Behave Worse When Eval Aware*; Aranguri & Bloom, *Verbalized Eval Awareness* | `neel_brainstorm` 40, 43 | Verbalized vs unverbalized cues change behavior | INDEX rows only |

Findings from this table:
- Nothing in `papers/` studies cue reliance (docstring or hint trust) or code-execution prediction. A grep of `papers/*/INDEX*.md`, `_litreview_text.txt`, `_neel_brainstorm_text.txt` and `neel.txt` found no hit for Turpin, Lanham, "don't always say", CRUXEval, CWM, TACO or "spurious".
- Nothing in `papers/` filters rollouts by attribution to fix a *capability-adjacent shortcut*; the RL-attribution papers (rows 56-59) are about efficiency or hacking detection.
- `papers/` contains no paper on probe distribution shift as a topic; Kramar (37) is the nearest, and PyINE's own Tables 5 and 6 are the most direct evidence in the folder.

## 2. Our own prior results that transfer

Read: `docs/experiments/I9/report.md` (summary, setup, Result 1), `docs/experiments/I1a/report.md` (summary and gate table), `docs/experiments/X3/report.md` (summary), `../mats12_neel_working/logs/phase1-report.md` lines ~1739-1790 (probe locators), and `logs/litreviews/20261007-X7-internal.md` (which quotes the held-out locator numbers; I did not reopen the raw JSONs).

For framing A (attribution to find and drop rollouts):
- **Held-out locator failure (working repo).** dEF-IF locator: Spearman 0.867 on 10 development groups, -0.047 on 20 fresh groups; 18 of 21 locators failed held-out; retrain-target ICC 0.578. A ~$2 content judge beat random by 3.6-4.3 points in every seed; influence-picked rewrites tied random (X7 internal review, citing `phase1-report.md`). Transfers as: validate on a ground-truth per-rollout label first, freeze layer/damping/query before looking at held-out data.
- **Probe locators in the same harness.** Base-model probes with perfect discriminative power (LOO accuracy 1.000; AUC ~1.0) still failed to predict which rows causally support the behavior (P-lab rho 0.152, P-logreg 0.358 at the primary cell; `phase1-report.md` ~1739-1790). "Detects X" does not imply "identifies what teaches X". This applies directly to A: a probe that flags hint-reliant rollouts is not shown to flag the rollouts that *cause* hint-trust.
- **X7's environment search** (b97a31e; `logs/litreviews/20261007-X7-env-*.md`, read in part): no tested environment leaves room for text monitors on hack rate. PyINE changes one thing: there is no text-visible hack to catch, and text monitors (small judges, classifiers) *do* fail on the misleading slice. But strong judges that can trace code succeed (Table 7), so "text fails" is true only for cheap text overseers.
- **No RL training code exists in this repo** (X7 internal review §4). PyINE's organism needs full fine-tuning at scale; the authors report LoRA gave "mitigated success".

For framing B (readers):
- **I9 (report, rev 17):** on 64 agentic-misalignment scenarios, a linear probe on Qwen3.6-27B's last-prompt-token activations predicted per-scenario misbehavior rate (Spearman 0.82, AUROC 0.92) about as well as a text embedding (0.80). Log-likelihood gain +0.021, 95% CI -0.029 to +0.076. Descriptive, not confirmatory (power check not computed; judge-kappa gate failed at 0.643 and 0.678). The readers (logit lens, J-Lens, Oracle Lens) were **built but not run**. On the benchmark's own design score a prompt-only monitor scored 0.94 vs Oracle Lens 0.578. Jacob recommended the interpretability line become a side line.
- **I1a (report):** all 9 reproduction cells within 0.10 of published; 1 of 7 ranking checks failed, so the verdict is "Judge only". A text-only Gemini 3.8 Flash monitor matched or beat every reader on multi-hop (0.98 vs Oracle Lens 1.00) and recall (0.91 vs 0.75) and hallucinated less (0.02 vs 0.33). Lesson: a benchmark where the prompt contains the answer cannot separate reading from inferring.
- **Direct transfer to B.** PyINE's misleading slice is the opposite situation: the answer is *not* recoverable from the prompt by a cheap reader, because the cue is in the prompt and the truth is in the execution. That is the kind of "items prompt-only guessing cannot solve" that I1a's report says a fair reader comparison needs. This is the main reason B is interesting. But the *cue* (misleading docstring) is visible in the prompt, so a prompt-only monitor can detect "a misleading cue exists" (a check, not tested here).
- **X3 report (cheating, 20/120 vs 0/120; verbalized grader belief matches framing 94-98%)** and the parked grader-belief line: relevant only as a reminder that verbalized belief and behavior were not shown to be linked ("unverbalized belief is invisible here", X3 prereg line 364).

## 3. What Neel asked for that this does or does not serve

Quotes are from `papers/_neel_brainstorm_text.txt` (the open-problems list) and the X7 internal review's reading of the 2026-10-06 transcript (not re-read).
- **Serves, framing B:** "Use tools like oracle lens and NLAs to find evidence of unverbalized motivations in models. Qwen 3.6 27B resources" (Model forensics). Cue reliance that never appears in the CoT is a clean unverbalized-influence case with ground truth, unlike most motivation settings. PyINE's organism is Qwen3-4B, not Qwen3.6-27B (no oracle for it in the list), which is a mismatch with the suggested model.
- **Serves, framing B:** "Can you improve the performance of different methods (NLAs, oracle lens, J Lens, etc) on workspace bench?", and "How consistent are the different methods ... What things are caught by some but not others?" (I5). A cue-reliance slice is a new family with an authoritative label.
- **Serves, framing B:** "Make probes for scheming / deception / subterfuge / other misalignment-y concepts" (Activation monitors). A probe trained on "is relying on the docstring" is a probe for a (benign) concept; it tests whether probes generalize when trained on the right label.
- **Partly serves, framing A:** the 2026-10-06 request, as recorded in the X7 review, was to adapt TDA to RL with a reward-hacking proxy task, beating "just read the transcript / ask an LLM". PyINE is not reward hacking, so it answers the TDA-in-RL question in a different setting, and I could not find a statement from Neel endorsing it. Neel's broader frame ([103], 'decompose a gradient into the 50 things models learn') fits a cue-reliance target better than a lexical hack, because there is no text marker to cheat with.
- **Does not serve:** "When models cheat, how much are they rationalizing this in their chain of thought versus just blatantly cheating?" and the CoT-controllability items. PyINE's organism does not cheat or conceal; it errs. Do not oversell as monitorability of misbehavior.
- **Neel's caution relevant to both:** detecting intent "so long as the prompt doesn't spell out the concepts you're looking for too obviously" (I9 section). In PyINE the cue is in the prompt, so this is partly met and partly violated: the *misleading cue* is visible, the *reliance on it* is not.

## 4. The gap that remains

### Framing A (attribution finds and drops the rollouts that teach hint-trust)
Closest prior: I-PPO (row 56), Grift (59), Wong et al. (54), Paulo et al. (em_personas 36).
- **Gap.** No paper in `papers/` runs attribution or a probe in an RL loop to prevent a non-hacking, cue-reliance shortcut; PyINE ships a ground-truth label (execution) and a held-out misleading slice, so "filter, retrain, measure" is cleanly testable. That is real, and the label is free.
- **Strongest prior against A.**
  1. **The causal story is shaky.** Base Qwen3-4B already scores 6.3% on misleading variants; the organism scores 10.5%. The paper attributes the late drop to the length penalty, not to helpful-hint rollouts. If the cause is the length penalty, the cheap, no-attribution baseline (weaker length penalty, drop short correct rollouts, a length-matched filter) is the minimalism comparator, and it must be run first. A "teaches hint-trust" rollout may simply be "short and correct".
  2. **The mechanism may not be rollout-localizable.** Our own working-repo results show locators validated in development collapsing held-out (-0.047), and perfect detectors failing as causal locators. Hint-trust spread across thousands of short correct rollouts is a diffuse signal.
  3. **Cost.** The organism took up to 40 H100s and 50+ runs; X7's own estimate of ~$950-1,000 for a 4B RL study was for Aria's 4xH200 setup. PyINE's 10k-token rollouts and full fine-tuning are larger. We have no RL code (X7 internal §4).
  4. **Cheap text filters may be strong here.** Misleading cues are lexically visible in the prompt (a docstring that contradicts code). A rule-based "docstring/comment present" filter, or a prompt-conditioned judge, is a free baseline for *which prompts carry cues*; the PyINE paper did not test it.
  5. **Fit with X7's requirement:** X7 asked for an environment where the learned behavior is not text-detectable. PyINE passes that for cheap overseers but not for strong judges; the matched-drop-rate comparison against a strong judge is the interesting test and the likely loser (per E7/working-repo priors).
  - **Verdict (inference).** A is a different question from X7's: it asks whether attribution beats length-based filters on a capability-adjacent bias. I would expect a length/cue-presence baseline to be competitive. Worth a free Gate 0 (score existing rollouts, AUROC vs a "cue-reliant" label) before any RL spend.

### Framing B (activation readers on cue reliance)
Closest prior: WorkspaceBench (interp_methods 5) and our I1a/I9; CHIVE (22); AO weaknesses (17, 19, 34, 54); Kramar (37) and Goldowsky-Dill (38) on probes.
- **Gap.** PyINE's own readers were only correctness probes trained on a mixture and text classifiers. Nobody in `papers/` tests J-Lens, Oracle Lens, NLAs, SAEs, or a probe trained on a *reliance* label on this slice. The paper's cheap overseers fail partly because their label was "correct vs incorrect", which the cue-salience feature predicts wrongly on the misleading slice; a "relies on cue" label is a different probe and could behave very differently. That contrast, same activations, two labels, is cheap and informative.
- **Strongest prior against B.**
  1. **Our own I9/I1a:** probes matched a text embedding; a text monitor matched every reader; readers were built but not run in I9. The default expectation from our files is that readers add nothing over the best cheap baseline, and that "the answer is in the prompt" confounds. Here the answer is in the code, which could break the tie, but a code-tracing judge (gpt-5-mini 93.2 AUROC) is a strong text-only competitor and sets a high bar.
  2. **Organism artefacts.** The organism is a fully fine-tuned Qwen3-4B with a non-standard objective. Minder et al. (52) and Fornasiere et al. (interp_methods 54, 40) both warn that readers behave differently on fine-tuned organisms. The readers we have (J-Lens, Oracle Lens) are trained for Qwen3.6-27B, not Qwen3-4B; I did not check whether any exist for Qwen3-4B-Instruct. If not, B needs a different model or fresh reader training.
  3. **Label problem.** "Is relying on the docstring" is not given by PyINE; the available label is "correct vs incorrect" plus a counterfactual pair (helpful vs misleading variant of the same program). A reliance label has to be built from counterfactual flips (answer follows the cue), which is itself a behavioral label, not a measurement of internal reliance. The organism is wrong on ~90% of misleading items (10.5% accuracy), though not every error need follow the cue, so the positive class is not rare; the interesting question is whether activations say so before the answer, i.e. I9's framing.
  4. **Weak link to Neel's concept of unverbalized motivation.** A docstring bias is a heuristic, not a hidden goal; Neel's list is aimed at motivations. This reduces fit with his stated priorities.
  - **Verdict (inference).** B is cheaper (inference, no training) and fits Neel's reader questions better than A, provided a reader exists for the model. The key risk is repeating I9's conclusion with a different dataset.

## 5. Sources referenced but MISSING from `papers/` that a prereg would need
Searched: all INDEX files, `_litreview_text.txt`, `_neel_brainstorm_text.txt`, `neel.txt` for these names. Not found.
- **PyINE itself** (arXiv 2610.04737) and its repo; not yet in `papers/`. Needs a capture and an INDEX row, with the Table 7 defect noted.
- **Turpin et al. 2023**, *Language Models Don't Always Say What They Think* (cited by PyINE as [1]); **Lanham et al.**, *Measuring Faithfulness in CoT* ([2]); **Chen et al.**, *Reasoning Models Don't Always Say What They Think* ([49]). The standard unverbalized-cue literature; none captured.
- **CRUXEval** (Gu et al.), **CWM** (Carbonneaux et al.), **TACO**, and Liu et al., *Code execution with pre-trained LMs* (PyINE refs [16]-[20], [23]); needed for the code-execution prior and for the organism's base-capability context.
- **Reward-hacking probe and monitor papers beyond row 52:** none missing from the X7 set that I could identify; Yang et al. 2025 and McGrath 2026 remain uncaptured (X7 internal §6).
- **Spurious-cue learning in RL / sycophancy to hints:** nothing captured.
- **Any Qwen3-4B-Instruct reader** (J-Lens file, Oracle Lens adapter): not in `papers/`; availability unchecked. The J-Lens README (interp_methods 2) and the Oracle Lens model card (interp_methods 8) are the places to check.
- **Fig. 9 of PyINE (training curves)**: seen only as text in the extraction; the figure should be inspected before using the "step 300" claim.
- **Loracles** and Aria Wong's workshop paper full text remain missing (X7 internal §6).

## 6. Suggested next steps (inference, not requirements)
1. Re-read PyINE Table 1 and Fig. 9 before anything else; the "10.5%" framing in the brief is wrong in direction (6.3 base to 10.5 organism).
2. Before any RL, run the free Gate 0 for A: score rollouts from the released organism by attribution, a probe, length, and a cue-present rule against a "followed the cue" label, AUROC on a held-out slice (needs the organism checkpoint; check whether the authors released it).
3. For B, first check whether any reader exists for Qwen3-4B-Instruct; otherwise run the label contrast (correctness probe vs reliance probe) on the released organism's activations, with gpt-5-mini-class judge as the bar.
4. Capture PyINE, Turpin, Chen et al. into `papers/` with INDEX rows before a prereg.

## Correction (main session, 2026-10-08)

The activation-oracle post (`papers/interp_methods/` row 54) is by Raffaello Fornasiere; PyINE's coauthor is Damiano Fornasiere, a different person. Claims here that a PyINE coauthor works on activation oracles, or that the Model Organism Lottery shares a PyINE author, rest on that confusion and are withdrawn. The suggested capture "row 81" duplicated existing rows 54 and 19 and was removed.
