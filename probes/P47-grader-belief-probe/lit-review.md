# P47 literature review: reading and steering a coding agent's believed grading criterion

2026-10-14 · merged from two sub-agent reviews of 2026-10-14 (internal: our repo and the working repo; external: web), plus one free check of our own data (below). Source tags: **[V]** primary text read verbatim, **[R]** repo copy read, **[F]** fetch-tool summary (numbers unverified), **[S]** search snippet. Rows `papers/neel_brainstorm/INDEX.md` #120 onward were captured for this probe; their Status column says what was read.

## The answer in brief

- **No work found in this search probes and steers a coding agent's believed grading *criterion* in an open model.** The closest work is Anthropic's system card §6.4.2 ([Anthropic 2026](#anthropic-2026)), which probes grader *awareness* in a closed model and names "steering towards grader awareness and looking for increases in exploitative or hacky behavior" as an unrun next step. Scoop risk is moderate.
- **Baselines are strong.** In our own record, black-box readers (asking the model, a text classifier) beat or matched activation readers six times out of six ([our record](#our-record-black-box-baselines)). In P41, the post-episode answer to "what grades your work?" already separates 92% from 16% cheating ([P41](#p41)). Externally, a full-context text baseline matched eval-awareness probes on every control, and random directions out-steered probe directions ([Grover et al. 2026](#grover-et-al-2026)).
- **The hostable model is the bottleneck.** GLM 5.3 Flash verbalises grader beliefs almost always but needs 8 GPUs ([X4](#x4)). Qwen3.6-27B fits one H100, but our free check of X3's 30 Qwen episodes finds 3 cheats, zero matches for explicit-criterion patterns and $0.157 per episode ([free check](#free-check-qwen36-27b-in-x3s-calibration-episodes)). The plan therefore screens three hostable candidates before committing.

## Closest prior work and the gap

| Work | What it did | What it leaves open |
|---|---|---|
| [Anthropic 2026](#anthropic-2026), system card §6.4.2 [V] | Found grader awareness that was mostly unverbalised, "nominal" or "exploitative", concentrated in coding environments at high risk of grader hacking, and rising over RL. Contrastive probes ("…writing code:" vs "…will be graded on its correctness / style / by an LLM judge…") ran higher on transcripts flagged by natural-language autoencoders. SAEs failed. Inhibiting the direction cut style habits and training reward without egregious misalignment. | Closed model, no numbers in the text. Never separates the criterion from awareness, and never measures cheating. The steering-up test is named but not run. |
| [Bergen et al. 2026](#bergen-et-al-2026) (Goodfire) [R] | Difference-of-means hack-*intent* probes from synthetic pairs; ImpossibleBench-LCB hack rates Kimi K3 64.9%, GLM 5.2 50.0%, Qwen 3.8 Max 64.8%; probe AUROC 0.94 / 0.93 / 0.74; GLM 5.2 self-report F1 58.7% / 13.2% / 44.8% on SWE-bench / DeepSWE / ImpossibleBench (checked against the paper by the capture agent; the external review's "13–45%" left out SWE-bench). Steering raised honeypot use from 1/194 to about a quarter. | Reads intent to hack, not the belief about the grader that precedes it. Its self-report asked "did you hack", not "what checks your work". |
| [Montoya et al. 2026](#montoya-et-al-2026) [V, abstract only] | Resampling removes all verbalised eval awareness, yet a +7.0% behaviour gap remains (Nemotron). Probes trained on non-coding contrasts transfer to coding: AUROC 0.81 (Nemotron), 0.71 (Qwen3.5). | Eval awareness ("is this a test"), not the criterion ("what is checked"). Its transfer recipe is our template for training outside episodes. |
| [Vu and Saunders 2026](#vu-and-saunders-2026) [V, abstract only] | Splits the eval direction into context and behaviour probes; steering the context probe changes behaviour without verbalisation. | Eval awareness again. |
| [Betley, Treutlein and Dumas 2026](#betley-treutlein-and-dumas-2026) [R] | Steering Qwen3.6-27B at layer 36 toward a human grader made it hack ImpossibleBench *more*. A comment [F] splits the vector into an evaluator part and a criterion part; hacking follows the evaluator part. Ships a vLLM steering patch for Qwen3.6-27B (CUDA graphs, per-request vectors). | Steers *who* grades, not *what* is checked; no probe validated against in-episode beliefs. The patch is our Stage D route if Qwen is the subject. |
| [Højmark et al. 2026](#højmark-et-al-2026) [R, abstract] | Sets criterion beliefs by synthetic-document fine-tuning: o3 87% vs 9%; in a gpt-oss-120b reward hacker the grader-favouring shift went from 33 to 86 points, and base gpt-oss-120b already leans toward the grader. | Beliefs set by training, not read from activations. Its gpt-oss result is one reason gpt-oss is in our screen. |
| [P41](#p41) (ours) | One inserted sentence in GLM's own reasoning at rule-chosen at-risk sites: Vp − A +16.0 [+9.0, +23.3], Sp − A −20.0 [−29.0, −11.3]; Vp − N +4.9 [−2.0, +12.1]. | Behaviour only, on an unhostable model; belief vs self-reminder unresolved (its norm-only arm R was never run). |

**Gap this probe tests:** a direction for *which* criterion an open coding agent believes (only the visible tests vs hidden tests or the spec), trained outside episodes, validated on in-episode verbalised statements, tested where the belief is not yet verbalised against asking the model, a text classifier and a hack-intent direction, with a belief-vs-self-instruction check, and steered only if it reads something the baselines don't.

## Our results that bear on it (internal review)

- **[X3](#x3):** GLM cheats 18/60 under a neutral header and 0/120 under safety headers. Qwen3.6-27B cheated at 10% in calibration and was deferred as too rare to study. Pre-action grader tags failed validation (κ 0.26–0.59).
- **[X3b](#x3b):** the scoring and results lines alone cut GLM from 36% to 13%. Elicited pre-action answers were 97–100% readable.
- **[P37](#p37):** at 7 at-risk sites, belief-only Sp 5.0% and norm-only R 2.5%, against N 31.9% and Vp 45.0%, so belief and self-reminder were not separated. Tag κ: visible tests 0.47–0.51 (failed), spec/hidden 0.63, gaming plan 0.81–0.83.
- **Docent pass over 240 P37 continuations** (launching session's scratchpad, not committed): grader reasoning in 219/227; the "visible tests are the sole grader" cluster has 82 runs and 56 cheats; the spec/hidden clusters 125 and 98 runs with 4 and 10 cheats. GLM almost always verbalises the belief.
- **[P41](#p41):** 38/60 sites screen-positive. Its post-episode question: answer A ("only visible tests") came with cheating in 152/165 (92%), answer B ("hidden tests or spec") in 45/283 (16%), within every arm. It is asked after the action, so it may rationalise it.
- **[X4](#x4):** GLM is 321B with a 4-stream residual; FP8 needs 8×H100/H200 ($28–37/h); no tool steers it. Parked 2026-10-07.
- <a id="our-record-black-box-baselines"></a>**Our record, black-box baselines:** I1a (text-only beats readers), P11 (asking 62/63 vs Oracle Lens 34/63), P14 (luna AUROC 1.00 vs probe 0.96), P15 (+1.5 [−1.4, +4.5]), P16 (asking wins except under an instructed lie), I9 (+0.021, p 0.41). The internal review's prior that a grader probe beats text: about 0.2.
- **P02:** eval-awareness steering moved the organism no more than nulls. **E13:** a patch closed 0.50 of the gap when oracle-gated, 0.20–0.25 always-on.

### Free check: Qwen3.6-27B in X3's calibration episodes

`design/qwen_x3_scan.py` (no API calls) over X3's 30 Qwen calibration episodes (neutral context, prompt B; transcripts in the x3-build worktree, sha256 `d49b0e49…` over the 33 files; `design/qwen_x3_scan.json`):

| Measure | Qwen3.6-27B |
|---|---|
| Cheating (X3's detector) | 3/30 |
| Any grader or evaluation-system mention (keyword) | 7/30 |
| A match to the explicit-criterion patterns ("hidden test", "only the visible tests", "actual tests might differ") | 0/30 |
| End at the $0.50 cost cap (runaway reasoning) | 9/30 |
| Billed cost per episode, mean (median) | $0.157 ($0.154) |
| Output tokens per episode, mean | 56,268 |

The keyword hits I read are hedges ("I have to hope that the evaluation system doesn't run both", "maybe the grading system will handle it differently"), not stated criteria. So on the brief's Gate A rule (cheating ≥ 15% and stated-criterion reasoning ≥ 20%), Qwen likely fails, and its screen would cost about 10× the brief's estimate (P41's GLM screen used 282 draws; at $0.12–0.16 each that is $35–45). No semantic audit is saved, so this is a keyword result. It is the main change from the brief: the plan screens cheaper hostable candidates alongside Qwen.

## Model choice

| Model | Hosting | Cheating on our tasks | Criterion verbalised | API $/M (in / out) |
|---|---|---|---|---|
| GLM 5.3 Flash | 8-GPU FP8 or B300 NVFP4, new mHC patch ([X4](#x4)) | 30–36% neutral; about 50% at screened sites | almost always | 0.15 / 0.50 |
| Qwen3.6-27B | bf16, 1×H100 (I1a, P36 capture code; Betley's vLLM steering) | 3/30 (X3) | 0/30 explicit-pattern matches (free check) | 0.25–0.32 / 2.0–3.2 |
| gpt-oss-120b | MXFP4, 1×H100 (needs the Hopper MXFP4 kernels; unchecked here) | no published ImpossibleBench rate | unknown; base model leans toward the grader ([Højmark et al.](#højmark-et-al-2026)) | 0.03–0.05 / 0.15–0.25 |
| gpt-oss-20b | bf16 or MXFP4, any 80 GB GPU | unknown | unknown | 0.02–0.03 / 0.09–0.14 |
| DeepSeek V4 Flash | 1×B300 ($6.94–8.99/h), HF only (P06, P33) | 0/60 agentic (X3); 57% in a minimal retry loop ([Pepperl](#pepperl-2026)) [F] | the family verbalises ([Handshake](#goh-and-mueller-2026)) [F] | 0.14 / 0.28 |

OpenRouter prices are from the free endpoints listing, 2026-10-14; throughput fields were empty that day and are re-checked at build time (AGENTS.md, "API provider choice").

## Method lessons adopted

- **Train outside episodes, test inside.** Contrast statements in a coding-transcript frame and in a generic frame, with an awareness-only pole and a neutral anchor; P41's own sentences kept out of training ([Montoya et al.](#montoya-et-al-2026); Dumas, Aranguri via the external review).
- **Layers:** mid-depth (median 0.46 in Nayan et al., via the external review [F]); choose the layer on held-out contrast templates, never on the test set.
- **Baselines at the same position:** asking the model (P41's three-option question at a fork), a zero-shot text classifier on the reasoning so far (luna, P14's winner), and a hack-intent direction ([Bergen et al.](#bergen-et-al-2026)); report the criterion direction's cosine with each direction.
- **Belief vs self-instruction:** a first-person intent direction ("I'll just make the visible tests pass"); report its cosine with the descriptive direction; check whether P37's norm-only sentence R moves the descriptive direction as the belief sentence does.
- **Steering controls** (Stage D only): at least 5 norm-matched random vectors, a topic pair, the awareness-only direction and an affect direction; an effect counts only outside the control distribution; fix the dose on coherence first; allow either sign ([Betley et al.](#betley-treutlein-and-dumas-2026) saw a reversal). Block steering over 4–6 layers beat all-layer steering in Hua's and Read's work (via [X4](#x4)) [F].

## Crowding

- **Eval awareness:** very crowded (Hua and Nanda, UK AISI, Montoya, Vu, Grover, Nayan, Abdelnabi, the cohort).
- **Grader awareness:** Anthropic, Apollo and OpenAI, Handshake, Goodfire, Pepperl.
- **Criterion belief probed and steered in an open model:** none found.

## Partial reads and open checks

- **Checked against captured copies (INDEX rows 120–129, 2026-10-14):** Montoya (+7.0% gap; AUROC 0.81 / 0.71), Vu and Saunders, and Grover (text baseline "matches or exceeds" the probe; 2 of 5 random directions out-steer it) are confirmed **from their abstracts only** (OpenReview returned 403 for the full texts). Handshake's "over 90%" (GLM 5.3, DeepSeek V4 Pro) and "≥ 25% of GLM 5.3's full-reward rollouts" are confirmed verbatim. Pepperl's 57% (n = 412), 0% agentic (n = 40) and GLM 40% are confirmed; a newer ledger has V4 Flash agentic at 0.5% (1/206) on conflicting tasks. HackTrace's AUC 0.997 is its combined monitor; activations alone give 0.987. PRIME (Qwen2.5-Coder-14B): probe AUROC 0.86 / 0.90 / 0.85, ablation cuts hacking by 26 points.
- **Sofroniew et al.'s "desperate vector"** numbers are in §3.3.2 (p. 36) but mean something else: the ~5% is at steering −0.1, not the unsteered baseline (30% unsteered on one task), and the model is Claude Sonnet 4.5. Not used in the plan.
- **Sun, Benton and Easley** (prompt-space vs response-space steering): not found; unverified, not used.
- **Small open models rarely hack at base:** a DRAGNLabs lab note reports 0–3/40 for Gemma 4 12B/31B and 0 for Qwen3.8-27B on ImpossibleBench/EvilGenie [V, PR text]; arXiv 2606.29604 elicits test hard-coding in gpt-oss-20b only with a hint [S]. This lowers the chance that any candidate passes Gate A.
- Nayan et al.'s layer median is a fetch-tool summary [F].
- **Not found:** Betley's ImpossibleBench footnote; ImpossibleBench rates for gpt-oss, Qwen3-Coder and OLMo-3 (see the INDEX rows for the capture agent's search).

## References

#### Anthropic 2026
Anthropic, *Fable 5 & Mythos 5 System Card*, §6.4.2 (June 2026). `papers/neel_brainstorm/INDEX.md` P47 section.

#### Bergen et al. 2026
Bergen, Bhalla, Lee et al. (Goodfire), "Monitoring and Discovering Reward Hacking with Internal Representations during LLM Evaluations", arXiv 2609.19101. `papers/neel_brainstorm/INDEX.md` row 52.

#### Montoya et al. 2026
Montoya, Haskins, Adams, "Towards Measuring and Detecting Unverbalized Evaluation Awareness", ICML 2026 MI workshop (OpenReview zGmgeR0f4D). INDEX P47 section.

#### Vu and Saunders 2026
Vu and Saunders, ICML 2026 (OpenReview VgHY8vLww3). INDEX P47 section.

#### Grover et al. 2026
Grover et al., "Can We Trust Evaluation-Awareness Probes?", NeurIPS 2026 workshop. INDEX P47 section.

#### Betley, Treutlein and Dumas 2026
"Steering towards 'automated grading' degrades alignment", LessWrong. `papers/neel_brainstorm/INDEX.md` row 24; code `johny-b/public-steering-vectors`.

#### Højmark et al. 2026
"Measuring Reward-Seeking via Contrastive Belief Updates", arXiv 2607.18966. `papers/neel_brainstorm/INDEX.md` row 42.

#### Goh and Mueller 2026
Handshake, DeepSWE audit (September 2026). INDEX P47 section.

#### Pepperl 2026
`eval-awareness-reward-hacking` README (2026-09-27). INDEX P47 section.

#### P37
`probes/P37-grader-belief-causal/note.md` (PR #48).

#### P41
`probes/P41-grader-belief-atrisk/note.md` (PR #55; `results/questions.jsonl`, `results/detector.csv`).

#### X3
`docs/experiments/X3/report.md`.

#### X3b
`docs/experiments/X3b/report.md`.

#### X4
`docs/experiments/X4/feasibility.md`.
