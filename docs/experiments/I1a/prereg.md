# I1a+: WorkspaceBench reproduction gate, with saved Oracle Lens samples

Mirrored to the Claude Doc (synced at rev 79). This file is the source of truth. v0.2.1 of this prereg is in git history (commit `de9449f`).

**The question:** does the public WorkspaceBench benchmark reproduce its published scores for three activation readers on one GPU? And while the model is loaded, do ten Oracle Lens samples per activation contain better readouts than the one the benchmark scores?

## Summary table and key definitions

| | |
|---|---|
| Experiment | I1a+ (Topic 2: comparing interpretability readers) |
| Version | v0.3 draft. It changes v0.2.1 (one review round) in two ways: every diagnostic run is cut, to be added back after intermediate results, and ten Oracle Lens samples per activation are saved on two families. |
| Status | Draft, not approved. Nothing has been run or paid for. |
| Author | Claude, for Jacob Epifano |
| Reviews | gpt-6-sol: 1 of 3 rounds done (on v0.2.1). v0.3 needs round 2. |
| Budget | About $69 planned; hard cap $75 (GPU $40, judges the remainder). v0.2.1 had a $60 cap. |

**Key definitions.** An *activation reader* turns one internal activation of a language model into something a person can read. The *logit lens* projects the activation straight onto the vocabulary and lists the top tokens. The *J-Lens* first multiplies it by the model's averaged Jacobian (how later layers transform it), then does the same [[Gurnee et al. 2026](#gurnee-et-al-2026)]. The *Oracle Lens* is a copy of the model fine-tuned to write a few bullets of prose about an injected activation; it is sampled, so every run gives a different readout. Its matched *reconstructor* maps a readout's text back to an activation, and the *fraction of variance explained* (FVE) scores how much of the original activation that recovers. A *family* is one of WorkspaceBench's evaluation sets; a *cell* is one (item, layer, reader) readout. *Pass rate* is the share of items where the readout names the hidden target; the *hallucination rate* is the share of readouts that claim something the input does not support.

## 1. Research question

**Main question (the gate).** Using only public code, weights and judges on one H100, does WorkspaceBench reproduce its published scores for the logit lens, the J-Lens and the Oracle Lens? We test nine cells: those three readers on three families (multi-hop recall, multi-token recall, and hallucination) [[Blank et al. 2026](#blank-et-al-2026)].

**Secondary question (exploratory, judged only if the gate passes).** When we draw ten Oracle Lens samples for the same activation instead of one:

- How much better is the best of the ten, if we could pick it perfectly? This is the ceiling for any selection rule.
- Do two selection rules that need no judge get close to that ceiling? One keeps the sample the reconstructor scores highest (best FVE); the other keeps the sample most similar to the other nine (consensus).
- Does either rule lower the hallucination rate?

The secondary question is a small preview of Neel's inference-time scaffold question (§2). It decides whether that full experiment is worth running, and it does not replace it.

**Simple baselines (not gated).** The logit lens (no training) and the J-Lens (a fitted linear map) are already the linear readers in the gate. Two cheaper comparisons show how much any reader adds over them. They have no published targets, so they never decide the gate.

| Baseline | What it sees | Families | Scored by | Extra cost (central) |
|---|---|---|---|---|
| Text-only monitor | Gemini 3.8 Flash reads the prompt up to the read position, no activations, and states what the model is holding there | All three (hallucination on the same 75 conversations) | The same judge or regex as the readers | ~$2 |
| Supervised linear probe | Ridge regression from the same activations to the target's unembedding, 5-fold cross-validated over the 100 items; top-10 tokens read out as for the logit lens | Multi-hop recall | The same Gemini judge | ~$1 judging, CPU only |

A reader that does not beat the text-only monitor is reading the prompt, not the activations. With only 80 training items per fold the probe is a weak baseline; if it still matches the Oracle Lens, the Oracle Lens's edge on multi-hop is small.

## 2. Why this matters

**Where the question comes from.** Neel asked for both halves directly, under "Preparing for a world where we don't have monitorable CoT":

> "Can you improve the performance of different methods (NLAs, oracle lens, J Lens, etc) on workspace bench? And/or reducing hallucination rate."
>
> "How far can you push workspace bench performance and hallucination performance with inference time scaffolds on metamodels, such as generating many oracle lens outputs and finding the ones with the best fraction of variance recovered?"

His other open questions on this topic (whether NLAs see beyond the workspace, how much the readers overlap, and whether they explain real behaviour) also need readers already running on WorkspaceBench.

**Why reproduce before improving.**

1. An improvement is a difference from a baseline. If our pipeline cannot reproduce the published baseline, every later difference mixes the intervention with pipeline differences. Neel's own rule is to "introduce complexity or design new methods only once baselines have failed" [[Nanda et al. 2025](#nanda-et-al-2025)].
2. The benchmark is six days old and still changing. On the day the post appeared (2026-09-23), the multi-token families switched from a multiple-choice judge to regex scoring, and multi-hop's read position moved from the final token to the last word. The post does not say which version produced each published bar.
3. The repo lists its own reproduction as open work: "Judge every arm with `wsbench run` against the frozen floors". No per-reader readouts or result files are public.

**Why save samples in the same session.** Loading the 27B model and its readers takes about two hours of pod time before any reading happens. Generating nine extra Oracle Lens samples per activation on the same pod costs about 4 more GPU hours. Coming back later for a second session would repeat the setup (about $10) and its risks.

**The gap in the literature.** WorkspaceBench is the only shared benchmark that scores recall and hallucination together on one model, and nobody has reproduced any of its numbers [[Blank et al. 2026](#blank-et-al-2026)]. Sampling-based fixes for hallucination have been tested only on other oracles and tasks:

- Requiring agreement among 10 samples raised an activation oracle's precision to 94.3%, but on only 19.4% of items [[Bauer et al. 2026](#bauer-et-al-2026), App. A.1].
- Calibration and ranking are "mostly in place by k=10" samples [[Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026), App. D].
- The NLA authors name "best-of-N NLA explanation against AR reconstruction" as an untried extension [[Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026), Future work].

Nobody has tested selection by FVE, or measured the best-of-ten ceiling, on WorkspaceBench.

**Why a frontier lab would care.** Interpretability-tooling and auditing teams choosing a reader to run alongside chain-of-thought monitoring need to know whether WorkspaceBench's ranking holds, and whether a trained reader's hallucinations can be cut cheaply at inference time. A reproduction alone changes few decisions. Its main value is that it makes our later Topic 2 results believable. The sample preview could change one decision directly: whether scaffolds are worth building for trained readers.

**Seen before any data (2026-09-29).** Neel sent screenshots of a thread in the benchmark authors' Slack. We log them here because they arrived before any run. Values not printed as labels were read by eye, to about ±0.03 [[WorkspaceBench authors, Slack, 2026-09-29](#code-and-models)].

- **Exact published scores exist.** Camila Blank shared a CSV of the per-family scores behind the post's figure. Once we have the file, it replaces our pixel readings as the gate's targets. The values visible in the screenshot match ours; for example, the Oracle Lens scores 0.71 on multi-token recall.
- **The public benchmark already runs at our pinned commit.** Camila scored a new reader, Adam Karvonen's Activation Oracle, with the public code at `92d763e`. It averaged 0.303 across families. This shows the public pipeline runs end to end, but it doesn't reproduce the published readers.
- **Hallucination separates the readers most.** Hallucination rates read by eye: NLA-RL and NLA-SFT ≈ 0.96–0.97, Activation Oracle ≈ 0.63, Oracle Lens RL ≈ 0.34, logit lens ≈ 0.20, prompt-only baseline ≈ 0.07. The headline score averages 24 families and leaves out hallucination, so a reader can rank second overall (NLA-RL, 0.467) while almost every readout invents something. **Neel is very worried about the hallucination rate.**
- **A discrepancy to resolve at checkpoint 1.** The prompt-only hallucination bar here is ≈ 0.07, but the benchmark's baselines README gives a prompt-only invention rate of 0.53 (as read for v0.2.1). They may be different metrics.

The priors in §5 were written before we saw these, and we leave them unchanged.

## 3. What we will do

On one rented H100 we load Qwen3.6-27B and run WorkspaceBench's own reader code for the logit lens, the J-Lens and the Oracle Lens on three families. For the Oracle Lens we also draw nine extra samples per activation on two of them, and score every sample with the reconstructor. The pod then shuts down. On the laptop we score the reproduction with the benchmark's pinned judges and stop to report. Only if the reproduction passes do we spend judge money on the extra samples; the multi-token recall family is scored by regex, so its samples cost nothing to score either way.

| Part | Where | Produces | Cost (central) |
|---|---|---|---|
| Setup and offline tests | Laptop | Pinned benchmark checkout, read plan, harness tests | $0 |
| Pod smoke checks | H100 | Checks that the readers are wired correctly (§4.7) | ~$5 |
| Reading run | H100 | Readouts from three readers on three families; ten Oracle Lens samples per activation on two families, each with an FVE score; cached activations | ~$25 |
| Reproduction judging | Laptop + judge API | Nine scored cells and the gate verdict | ~$26 |
| Sample scoring (only after the gate passes) | Laptop + judge API | Best-of-ten ceiling and two selection rules, on recall and hallucination | ~$10 |

The diagnostic runs from v0.2.1 are held in reserve (§7), to be added only if an intermediate result calls for one.

## 4. Experimental procedure

The rule: we use the benchmark's own code, item banks, read plan and judges wherever they exist. Each row says whether it is as published, adapted (with the change and the reason), or our own addition.

### 4.1 Model, code and readers

| Element | Value | Source |
|---|---|---|
| Model | `Qwen/Qwen3.6-27B` @ `6a9e13bd`, bf16, thinking off, loaded by the benchmark's own backend | As published |
| Attention | `sdpa` or eager, never FlashAttention-4 | Adapted: FlashAttention-4 corrupts this model's activations (cosine 0.87–0.97 against FlashAttention-2) [[Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026), App. I] |
| Benchmark code | `camilablank/workspace-bench` @ `92d763e`, installed from source | As published |
| Logit lens | The benchmark's `LogitLens`, top-10 tokens | As published |
| J-Lens | The benchmark's `JLens` with the published fitted lens (`neuronpedia/jacobian-lens` @ `b25d72a9`, 1,000 wikitext prompts), top-10 tokens | As published |
| Oracle Lens | The benchmark's `OLens` recipe (prompt, injection scale 16,000, sampling at temperature 1.0, top-p 0.95, top-k 64, up to 256 tokens). Adapter from the public copy `agu18dec/olens_and_ar` @ `c95215d5`, directory `olens_s3d_rl600` | Adapted: the benchmark loads a gated copy; the public one has identical file hashes |
| Oracle Lens batching | 32 readouts per batch, never mixing layers | Adapted: one readout at a time would take ~13 h. Checked by the batching test in §4.7 |
| Reconstructor | `ar_ptag_pooled` from the same public repo: the reconstructor this Oracle Lens was trained against | As published for the pairing [[Oracle Lens model card](#code-and-models)]; using it to select samples is ours |

### 4.2 The nine gate cells

The published values are read off the post's overall figure by pixel measurement (±0.01), or taken from a family README where one gives two decimals; the two sources agree wherever both exist.

| Family | Items | Readouts per reader | Scored by | Metric | Oracle Lens | J-Lens | Logit lens |
|---|---|---|---|---|---|---|---|
| Multi-hop recall | 100 | 1,100 (11 layers, last word) | Gemini 3.8 Flash judge | pass rate, higher is better | 0.99 | 0.82 | 0.45 |
| Multi-token recall | 100 | 1,100 (11 layers, final prompt token) | Regex; token readers are first summarized by Gemini | pass rate, higher is better | 0.71 | 0.24 | 0.20 |
| Hallucination | 149 conversations, 1,123 sites | 5,615 (5 layers) | Gemini 3.8 Flash span judge | hallucination rate, lower is better | 0.34 | 0.31 | 0.20 |

One cost saving is carried over from v0.2.1: the two token readers' hallucination readouts are all generated, but only a seeded half of the conversations (75) is judged. The Oracle Lens is judged on all 149. The secondary family from v0.2.1 (multi-token multi-hop) is cut.

### 4.3 The saved Oracle Lens samples

| Element | Value | Source |
|---|---|---|
| Where | All 1,100 multi-token recall readouts; and the same seeded 75 of the 149 hallucination conversations on which the token readers are judged, all their sites and 5 layers, about 2,830 readouts | Ours. Multi-token recall has headroom (0.71) and is scored for free. Multi-hop is at 0.99, so it has no room to improve |
| How many | 10 per activation: the gate's own draw plus 9 more, same settings | Adapted: k = 10 as in consensus sampling [[Bauer et al. 2026](#bauer-et-al-2026), App. A.1] and the calibration study [[Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026), App. D] |
| FVE per sample | Each readout's text goes through the reconstructor; FVE is computed against the true activation at that layer | Adapted from the reconstructor's training reward. Whitened FVE needs the whitener moments (only the pooled set, ~1.8 GB, of the 3.38 TB gated `agu18dec/local-workspace` dataset); terms accepted Sep 29, so we report whitened FVE |
| Rule 1: random | The gate's own draw | The benchmark's k = 1 protocol |
| Rule 2: best FVE | The sample with the highest FVE | Neel's suggestion; also named by the NLA authors [[Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026), Future work] |
| Rule 3: consensus | The sample with the highest mean word-overlap (Jaccard over lowercased content words) with the other nine | Ours: a judge-free, single-readout version of consensus sampling [[Bauer et al. 2026](#bauer-et-al-2026), App. A.1]. The overlap measure is fixed before data |
| Ceiling | Multi-token recall only: a readout counts as a pass if any of its 10 samples passes. Measuring it on hallucination would mean judging all 28,000 or so samples (about $50), so we don't | Ours |

### 4.4 Sample sizes and power

The banks fix the item counts, so the question is what size of difference we can detect.

- **Gate.** A cell fails only if it misses the published value by more than max(0.05, 1.96 × its standard error). With 100 items that tolerance runs from 0.05 (at 0.99) to about 0.10 (at 0.45). A pipeline bug that halved a reader's score would be caught almost surely; a real 3-point drift would not, and we don't claim otherwise.
- **Samples.** Each selection rule is compared with the random draw on the same readouts, which removes most item-to-item noise. With 100 items for recall and 75 conversations for hallucination, the preview can detect changes of roughly 7 and 6 points. Smaller effects are left to the full experiment.

### 4.5 Metrics and decision rules

**Gate.** Each cell's metric is exactly what the benchmark reports. We compute a 95% item-bootstrap interval for every cell (2,000 draws, seed 0).

- A cell is *consistent* if |ours − published| ≤ max(0.05, 1.96 × SE). This is a tolerance rule we set in advance, not a test of equality: consistent means "no difference larger than the tolerance was found".
- *Ordering checks:* wherever the published gap between two readers in one family is at least 0.10, our gap must have the same sign and be at least 0.05. There are seven such pairs (three in multi-hop, two in each of the others).

**Samples.**

- Recall: the benchmark's pass rate for each rule, and for the ceiling.
- Hallucination: the benchmark's hallucination rate for each rule.
- For each rule, the paired difference from the random draw, with a paired item-bootstrap 95% interval. A rule *helps* if it improves recall by at least 5 points or cuts hallucination by at least 5 points, with the interval excluding zero.
- We always report recall and hallucination side by side, because a rule that picks shorter readouts can cut hallucination by saying less.

### 4.6 Judges

The benchmark's pinned judges, unchanged: Gemini 3.8 Flash via OpenRouter for multi-hop and hallucination, its summarizer for token readers, and regex for multi-token recall. Swapping in a cheaper judge would change the instrument, so none is swapped. The reproduction itself is the judges' validation.

### 4.7 Checks that must pass before any number is read

| Check | Pass criterion | Source |
|---|---|---|
| Offline tests | The benchmark's tests pass; judge prompts match the family READMEs byte for byte | The benchmark's own checklist |
| J-Lens reference | On the 149 hallucination conversations, our J-Lens top-10 tokens match the benchmark's shipped reference readouts: same top-1 on at least 99% of cells | Ours, using the benchmark's frozen reference |
| Logit lens at the last layer | Its top token equals the model's own next-token prediction on at least 98% of prompts | Ours, from the logit-lens literature |
| Adapter live | Adapter on vs off changes the logits; activations are captured with it off | The Oracle Lens model card |
| Batching | Batched and one-at-a-time Oracle Lens give the same next-token distributions (top-1 agreement at least 99.5%) | Adapted from the working repo's batching check |
| Memory and speed | Fits in 80 GB; projected Oracle Lens generation at most 6 h | Ours |
| Backend | Never FlashAttention-4; a second forward pass gives identical activations | [Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026), App. I |
| Reconstructor | Our readouts score higher FVE on their own activation than on a mismatched one, in at least 80% of readouts | Ours |
| Shuffled activations (manipulation check) | Given another item's activation, the Oracle Lens passes at most 10% of multi-token recall items. Scored by regex, so it costs no judge money | Ours; v0.2.1 ran this on multi-hop with a judge |
| Judge pilot | 3 readouts per (family, reader) judged; a random 10 verdicts shown to Jacob | The benchmark's own checklist |
| Completeness | No missing or unjudged readouts; at most 5% empty | The benchmark's own rule |

If any of the first eight checks still fails after one round of debugging, we stop and report. No judge money is spent before they pass.

### 4.8 Seeds and outputs

- Oracle Lens seeds: the gate draw uses 20260928 + batch index; the nine extra samples use 20261001 + batch index. Every readout records its seed and batch.
- The hallucination subsets (75 conversations, shared by the token readers and the samples) are drawn with `random.Random(20260928)` and committed before any data.
- Activations at all 64 layers for every read position are cached (about 1 GB), with a manifest of every model, adapter, lens and code hash. Readouts follow the benchmark's file format, so they can be judged by its own tools.

## 5. Hypotheses and predictions

All priors are Claude's judgement, written before any data. Jacob may change them before approval; after that they are fixed.

### 5.1 The gate

**Main hypothesis: the public stack reproduces the published numbers.** At least 8 of 9 cells are consistent, every ordering check holds, and all checks pass. **P = 0.55.**

Competing explanations for a failure (not exclusive, so they don't sum with the main one):

| Hypothesis | Its signature | P |
|---|---|---|
| **Figure vintage.** Some published bars predate the 2026-09-23 instrument changes | Misses confined to multi-hop, the family whose read position moved | 0.20 |
| **Judge drift.** Gemini 3.8 Flash on OpenRouter behaves differently now | Gemini-judged cells miss; all regex-scored cells reproduce | 0.15 |
| **Different artifact.** The published Oracle Lens or J-Lens used another checkpoint or lens file (the model card says later RL branches "may supersede" this one) | Every cell of one reader misses, including its regex cell | 0.10 |
| **Sampling noise.** The published Oracle Lens scores are single draws | Oracle Lens misses are smaller than the spread across our ten saved samples | 0.10 |

The saved samples now answer the last row for free on multi-token recall, which v0.2.1 needed a separate diagnostic run for.

### 5.2 The samples

| Hypothesis | Prediction | P | Basis |
|---|---|---|---|
| **Room to improve.** The ten samples differ enough that picking well matters | Ceiling beats the random draw by ≥ 10 points on multi-token recall | 0.6 | Single draws at temperature 1.0 vary; an activation oracle on this model gave empty output 14% of the time [[Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026)]. Against: the benchmark already passes an item if any of 11 layers passes, which absorbs some variance |
| **FVE tracks content.** The reconstructor rewards readouts that capture the activation | Best FVE improves recall by ≥ 5 points | 0.4 | The Oracle Lens was trained on exactly this reward, so FVE should track what it reads |
| | Best FVE cuts hallucination by ≥ 5 points | 0.3 | FVE rewards reconstructing the activation, not being true. Neel's group warns against optimizing approximation-error metrics [[Nanda et al. 2025](#nanda-et-al-2025)] |
| **Consensus tracks truth.** Claims that recur across samples are more often true | Consensus cuts hallucination by ≥ 5 points | 0.5 | NLA claims that recur across adjacent tokens are more likely true [[Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026)]; recurrence across samples is our extrapolation; agreement raised precision [[Bauer et al. 2026](#bauer-et-al-2026), App. A.1] |
| | Consensus improves recall by ≥ 5 points | 0.25 | Consensus picks the typical readout, not the best one |
| **Null.** Neither rule helps on either metric | — | 0.35 | |

**Where we depart from published results.** Bauer et al.'s 94% precision came from abstaining on about 80% of items. Our consensus rule never abstains; it always returns one readout. So we expect a much smaller effect than theirs.

### 5.3 Numeric predictions (80% intervals)

| Cell | Published | Predicted |
|---|---|---|
| Multi-hop: Oracle Lens / J-Lens / logit lens | 0.99 / 0.82 / 0.45 | 0.95–1.00 / 0.76–0.88 / 0.38–0.52 |
| Multi-token recall: Oracle Lens / J-Lens / logit lens | 0.71 / 0.24 / 0.20 | 0.63–0.78 / 0.18–0.30 / 0.15–0.26 |
| Hallucination: Oracle Lens / J-Lens / logit lens | 0.34 / 0.31 / 0.20 | 0.28–0.40 / 0.24–0.37 / 0.14–0.26 |
| Multi-token recall with samples: ceiling / best FVE / consensus | — | 0.80–0.92 / 0.68–0.80 / 0.65–0.76 |
| Hallucination with samples (75 conversations): random / best FVE / consensus | — | 0.26–0.42 / 0.26–0.42 / 0.22–0.36 |

## 6. Outcome scenarios

The gate is read first, top to bottom; the first matching row applies, and the last row catches everything else. A *miss* is a cell that is not consistent, or a cell in a failed ordering check.

### 6.1 The gate

| Scenario | What we observe | Answer | Why it matters to a lab | Next step |
|---|---|---|---|---|
| **Not readable** | A check in §4.7 still fails after one debugging round, or the shuffled-activation check passes more than 10% of items | Unanswered: our pipeline isn't a valid instrument yet | None | Stop and report the failing check. Fix it within the cap if it's a known bug, otherwise ask Jacob |
| **Reproduces** | ≥ 8 of 9 cells consistent, none off by more than 0.15, all 7 ordering checks hold | Yes, for these three readers and families | The reader ranking is independently confirmed and citable; the benchmark works as a regression test | Score the samples (§6.2). Then the NLA and the full scaffold experiment |
| **Does not reproduce** | ≤ 5 of 9 consistent, or ≥ 2 ordering checks fail, or any cell off by more than 0.25 | No, not from public artifacts | Don't quote WorkspaceBench rankings as settled | Send the table to Camila and Neel and ask for their readouts. Report the free recall-sample results as within-our-pipeline only; no hallucination sample judging |
| **Multi-hop only** | All misses are in multi-hop | Probably yes; the read-position change is the likely cause | Scores must carry their instrument version | Add the old-read-position diagnostic (~$1, §7) to test it. Continue, with our numbers as the baseline |
| **Judge only** | All misses are Gemini-judged; every regex cell is consistent | Partly; judge drift is the likely cause, not confirmed | LLM-judged interp benchmarks can drift within days | Add the judge re-test diagnostic (~$1). Continue, comparing only readouts judged in the same session |
| **One reader** | All misses belong to one reader, including its regex cell | Partly; that reader's public artifact may differ from the scored one | Re-benchmark that reader before adopting it | If it's the Oracle Lens and the miss is within the spread of our ten samples, treat as reproduced. Otherwise ask the maintainers which checkpoint was scored |
| **Inconclusive** | Anything else | Report cell by cell; no overall claim | Weak evidence of fragility | Jacob picks at most one diagnostic from §7. Later work proceeds only on cells that reproduced |

### 6.2 The samples (only after a continuing gate outcome)

| Scenario | What we observe | Answer | Why it matters to a lab | Next step |
|---|---|---|---|---|
| **Selection helps** | A rule helps (§4.5) on one metric without worsening the other by 5 points or more | Yes, a cheap rule improves this reader | A free inference-time fix for trained readers | Run the full scaffold experiment: add the NLA, more families, more samples |
| **Room, but no good selector** | Ceiling beats random by ≥ 10 points, but neither rule helps | The samples contain better readouts, and these rules can't find them | The bottleneck is choosing, not generating | Scaffold work shifts to better selectors, such as a judge or a trained verifier |
| **No room** | Ceiling beats random by < 5 points, and neither rule helps | No: resampling this reader doesn't help | Scaffolds for this reader aren't worth building | Drop the scaffold line; move to NLA-vs-workspace or reader overlap |
| **Mixed** | Anything else | Report as observed | — | Jacob decides |

## 7. Budget, timeline and stopping points

**Planned spend is about $69 (including ~$3 for the §1 baselines); the hard cap is $75.** Rates: Gemini 3.8 Flash $0.75 in / $3.75 out per million tokens (OpenRouter, 2026-09-28); H100 80GB $2.69/h community, $3.49/h secure. Per-call token counts are estimates from the family READMEs and are replaced by measured values at the judge pilot.

### 7.1 Judge spend

| Item | Calls | Central | High |
|---|---|---|---|
| Multi-hop, 3 readers × 1,100 | 3,300 | $2.48 | $4.95 |
| Multi-token recall, summarizer for 2 token readers (Oracle Lens is regex, free) | 2,200 | $0.90 | $2.31 |
| Hallucination, Oracle Lens, all 149 conversations | 5,615 | $9.71 | $17.69 |
| Hallucination, J-Lens and logit lens, 75 conversations (span judge + summary) | 11,300 | $12.09 | $23.73 |
| Pilots and dry runs | ~200 | $0.50 | $0.50 |
| **Gate subtotal** | | **$25.7** | **$49.2** |
| Samples: hallucination, 2 rules × ~2,830 readouts (after the gate) | ~5,660 | $9.78 | $17.80 |
| Samples: multi-token recall and the shuffled check (regex) | — | $0 | $0 |
| **Total** | | **$35.5** | **$67.0** |

### 7.2 GPU spend (one H100)

| Phase | Hours |
|---|---|
| Pod up, environment, downloads (model 55.6 GB, J-Lens 3.3 GB, adapters, pooled whiteners 1.8 GB) | 1.0 |
| Smoke checks | 1.0 |
| Activation capture and token readers | 0.5 |
| Oracle Lens, gate draws and shuffled check (~8,400 readouts, 32 per batch, ~13 s per batch) | 1.0 |
| Oracle Lens, 9 extra samples (~35,100 readouts) | 4.0 |
| Reconstructor FVE on ~37,300 readouts (estimate; measured at smoke) | 0.5 |
| Debugging buffer | 3.0 |
| **Total** | **11.0 h ≈ $30** ($38 on secure cloud) |

Peak memory is about 73 GB while the token readers run (model, J-Lens and unembedding in fp32) and about 60 GB for the Oracle Lens, so both fit in 80 GB. Storage adds about $1.

### 7.3 Cap and pre-set cuts

- **Hard cap $75.** GPU sub-cap $40, enforced by the pod watchdog. Judge cap = $73 minus actual GPU and storage spend, enforced by a dedicated OpenRouter key with that credit limit and by a spend check before each chunk of 25 items. Reserve $2.
- **If the smoke test projects Oracle Lens generation above 6 h:** cut the extra samples from 9 to 4, then the hallucination sample set from 75 to 30 conversations. The gate cells are never cut for GPU reasons.
- **If the judge pilot projects more than the judge cap:** first shrink sample scoring to 30 conversations, then drop hallucination logit-lens judging, then cut J-Lens to 38 conversations, then the Oracle Lens to 75. Any gate cut makes the run *scope-curtailed*, which can never count as "Reproduces". If even that doesn't fit, stop before judging and ask Jacob.

### 7.4 Timeline and checkpoints

About 2–3 days elapsed: roughly 9 h of pod time, 2 h of judging, 2 h of analysis. Jacob's time is about 2.5 h: approving this plan, the two spend checkpoints, and reading random raw samples. At each checkpoint work stops, the report is updated, and Jacob decides.

| Checkpoint | Content | Spend so far |
|---|---|---|
| 0. Approval | Jacob approves this plan and the $75 cap, allows running the benchmark's code, and answers §9's questions | $0 |
| 1. Laptop setup | Pinned checkout, offline tests, read plan, subsets and seeds committed, harness tested on a small model | $0 |
| 2. Pod smoke | The §4.7 checks, memory and speed. No judging | ≤ $6 |
| 3. Readouts done | All readouts, samples, FVE scores and caches produced and pulled; pod terminated | ≤ $40 |
| 4. Judge pilot | Measured cost per call, spend projection, cuts if needed; a random 10 verdicts per family shown to Jacob | ≤ $41 |
| 5. Gate verdict | All gate judging; the §6.1 table; free recall-sample results; random raw readouts per reader. **Jacob decides whether to score the hallucination samples** | ≤ $65 |
| 6. Samples | Hallucination sample judging and the §6.2 table, with random raw samples | ≤ $75 |

### 7.5 Diagnostics held in reserve

None of these runs by default. Each is tied to the gate scenario that would call for it, so we add one only when an intermediate result points to it.

One catch: once the pod is terminated at checkpoint 3, anything that needs new generation costs a second pod session (about $10 of setup). **Recommendation:** generate the readouts the first and third diagnostics need while the pod is up (about 10 extra GPU minutes, ~$0.50), and never judge them unless called for. Strike this if you'd rather keep the run strictly minimal.

| Diagnostic | What it answers | Add it when | Judge cost to add |
|---|---|---|---|
| Multi-hop at the old read position (the 33 items whose position moved on 2026-09-23) | Do the published multi-hop bars predate the change? | "Multi-hop only" | ~$0.80 |
| Judge re-test: re-judge 200 multi-hop and 200 hallucination readouts | Is the Gemini judge stable run to run? | "Judge only" | ~$0.50 |
| Second Oracle Lens draw on multi-hop | How much does one draw vary on a judged family? | "One reader", for the Oracle Lens on multi-hop | ~$0.80 |
| A second saved sample judged on the 75 hallucination conversations | Sampling noise vs judge noise for the hallucination cell | "One reader", for the Oracle Lens on hallucination | ~$2.00 |

## 8. Implementation plan

A separate implementation plan (work units, owners, interfaces, tests) follows once this design is approved, as the project norms require. In outline:

- **Ported from the working repo:** the pod scripts (bring-up, teardown, watchdog, budget check), the bootstrap statistics, and the batched-versus-unbatched check pattern. The shared spend ledger and its combined cap guard are reused from E1.
- **New:** activation capture through the benchmark's own backend; a batched Oracle Lens that reuses the benchmark's prompt and injection code; reconstructor scoring; the two selection rules; a chunked judging wrapper with a spend check; and the analysis script.
- **Compute:** one H100 80GB (fallbacks: secure H100, H100 NVL, A100 80GB). Judging runs on the laptop; the OpenRouter key never goes to the pod.
- **Downloads:** the gated agu18dec/local-workspace repo is 3.38 TB (data/ 3.12 TB, ckpts/ 249 GB), so it is never cloned or snapshotted whole. Every Hugging Face fetch names its files (hf_hub_download, or snapshot_download with allow_patterns), a wrapper refuses any download over 100 GB, and checkpoint 1 confirms the benchmark's loaders pull only the pinned adapter, not the gated repo. Total pod downloads stay about 61 GB, so the pod budget does not change.
- **Order:** laptop tests on a small model first, then the pod smoke checks, then the full reading run, then judging, then analysis.

## 9. Risks, limitations and open questions

**Risks and limitations**

1. **No published intervals or readouts.** We compare against point estimates read off a figure, unpaired. The gate can confirm agreement within about 5–10 points, not exact equality.
2. **Unknown figure vintage.** Only multi-hop has a cheap test (a reserve diagnostic).
3. **The reconstructor's loader may not be public.** The model card loads it from a `global_workspace` package we have not located. If it isn't public, we rebuild the loader from the card's description, check it with the reconstructor sanity check, and drop the best-FVE rule if that check fails. The consensus rule doesn't need it.
4. **Whitened FVE needs gated files** (resolved Sep 29: terms accepted, so FVE is whitened as in the training reward).
5. **One judge, not a panel.** The project norm asks for two judges from different families on headline numbers. A reproduction has to use the benchmark's pinned judge, so this is a deliberate exception.
6. **The sample preview is small.** 75 conversations detect only effects of about 6 points or more. It is meant to decide whether the full experiment is worth running, not to be published on its own.
7. **The consensus measure is crude.** Word overlap may favour readouts that repeat common words. It is fixed before data, and an embedding-based version is left to the full experiment.
8. **Batching departs from the benchmark's one-at-a-time generation.** The batching check covers it; sampled text was never going to match the original run exactly anyway.
9. **Source-reading gaps.** Four benchmark source files (registry, read plan, judge config, LLM client) were not read, because running upstream code was blocked for the agent; checkpoint 1 reads them. Neel's WorkspaceBench announcement tweet could not be fetched. Neither is expected to change the design.

**Open questions for Jacob**

1. Can Neel or Camila share their per-reader readouts or result files? Re-judging about 200 of their readouts (~$0.20) would separate judge drift from pipeline differences.

    **Answer (Sep 29):** assume no. The paper arrives later this month; until then a miss attributed to the judge stays "likely, not confirmed" (§6.1). Re-judge ~200 of their readouts if the paper ships them.
2. May we accept the terms of the gated `agu18dec/local-workspace` dataset (auto-approved) on your HF account, to get the whitener moments? Otherwise FVE is unwhitened.

    **Answer (Sep 29):** terms accepted; download only the pooled whitener moments (~1.8 GB) and report whitened FVE.
3. Is the $75 cap right? The gate alone is about $51 at central estimates.

    **Answer (Sep 29):** the $75 cap stands.
4. The shuffled-activation check is kept as the one manipulation check, because it costs no judge money and about 5 GPU minutes. Keep it?

    **Answer (Sep 29):** keep it.
5. Generate the readouts for the reserve diagnostics while the pod is up (~$0.50, §7.5)?

    **Answer (Sep 29):** yes, generate them while the pod is up.
6. May the agent run the benchmark's own code on the laptop and pod? It was blocked in the last session.

    **Answer (Sep 29):** yes. The agent may run the benchmark's code and data on the pod, and on the laptop with the pooled whiteners (~1.8 GB). The gated repo is 3.38 TB, so §8 limits every download to named files.
7. Keep judging the token readers' hallucination readouts on half the conversations only? That saves about $12.

    **Answer (Sep 29):** yes, keep 75 conversations. The gate's 0.05 floor likely binds at 75 anyway, the samples use the same 75, and +$12 would put the plan (~$81) over the $75 cap.

Added after Neel's screenshots (2026-09-29):

8. Can you get Camila's CSV of the per-family scores from Neel? It becomes the gate's published targets.

    **Answer (Sep 29):** no; the gate uses the figure-read values in §4.2 (±0.01) as its targets.
9. Neel's worry about hallucination suggests moving the sample preview toward hallucination. **Recommendation:** raise the hallucination sample set from 30 to the same 75 conversations on which the token readers are judged. That costs about $5 more GPU (~15,000 more generations) and about $6 more judging, which is ~$66 planned in total, still under the $75 cap. It also gives one set of conversations with all three readers and both selection rules judged. The NLA, whose hallucination rate is highest and where selection could help most, comes in the next step because it needs its own 27B model.

    **Answer (Sep 29):** accepted; §4.3, §4.5, §4.8, §5.3, §7 and the risks now use 75 conversations.

## 10. Review history

| Date | Round | What changed |
|---|---|---|
| 2026-09-28 | gpt-6-sol round 1, on v0.2.1 (`logs/reviews/20260928-I1a-prereg-r1.md`) | 9 blocking and 5 minor findings, all fixed: the ordering-check count, the completeness rule, within-layer batching with a logit-level test, a hard judge-spend cap, scope-curtailed runs can't pass, and a catch-all scenario |
| 2026-09-29 | Jacob | Chose I1a+: save ten Oracle Lens samples per activation; cut every diagnostic run to start; add diagnostics back only after intermediate results |
| 2026-09-29 | v0.3 draft | Diagnostics moved to a reserve list; secondary family cut; sample design, hypotheses and scenarios added; shuffled check moved to a free regex family; cap raised to $75. gpt-6-sol round 2 is next |
| 2026-09-29 | New information | Neel's Slack screenshots logged before any data (§2): an exact per-family score CSV exists, the Activation Oracle was scored with the public code at our commit, and NLAs hallucinate in ≈ 96% of readouts. Open questions 8–9 added. Priors unchanged. |
| 2026-09-29 | Jacob's review closed | All nine questions answered. Added: simple baselines (text-only monitor, linear probe), 75-conversation hallucination samples, whitened FVE from the pooled whiteners only (~1.8 GB of the 3.38 TB gated repo), a named-files download rule. Costs brought in line: ~$69 planned, GPU sub-cap $40, checkpoint limits $40 / $41 / $65 / $75. |

## References

Local copies are in the repo's `papers/interp_methods/`.

#### Bauer et al. 2026

Bauer, De Schamphelaere, Karvonen, Luick, Nanda. [Building Better Activation Oracles](https://arxiv.org/abs/2606.02609). arXiv 2606.02609.

#### Blank et al. 2026

Blank, Bhatia, Ong, Nanda. [WorkspaceBench: Evaluating Interpretability Methods for the Global Workspace](https://www.lesswrong.com/posts/Zeg2JztbdhguL48uH/workspacebench-evaluating-interpretability-methods-for-the). LessWrong, 2026-09-23. Code: [camilablank/workspace-bench](https://github.com/camilablank/workspace-bench).

#### Fraser-Taliente et al. 2026

Fraser-Taliente, Kantamneni, Ong, et al. [Natural Language Autoencoders Produce Unsupervised Explanations of LLM Activations](https://transformer-circuits.pub/2026/nla/). Transformer Circuits.

#### Gurnee et al. 2026

Gurnee, Sofroniew, Pearce, et al. [Verbalizable Representations Form a Global Workspace in Language Models](https://transformer-circuits.pub/2026/workspace/index.html). Transformer Circuits.

#### Nanda et al. 2025

Nanda, Engels, Conmy, Rajamanoharan, et al. [A Pragmatic Vision for Interpretability](https://www.lesswrong.com/posts/StENzDcD3kpfGJssR/a-pragmatic-vision-for-interpretability). LessWrong, 2025-12-01.

#### Torrielli and Schneider-Kamp 2026

Torrielli, Schneider-Kamp. [Confidence and Calibration of Activation Oracles for Reliable Interpretation of Language Model Internals](https://arxiv.org/abs/2605.26045). arXiv 2605.26045.

#### Code and models

- [Qwen/Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B) @ `6a9e13bd6fc8f0983b9b99948120bc37f49c13e9`
- [agu18dec/olens_and_ar](https://huggingface.co/agu18dec/olens_and_ar) @ `c95215d5d2f6250f20c305ae55c6e4d7569f92dc` (the Oracle Lens model card; Oracle Lens and reconstructor adapters)
- [neuronpedia/jacobian-lens](https://huggingface.co/neuronpedia/jacobian-lens) @ `b25d72a96b79c8e309d6625955a98751da47e67a`
- [camilablank/workspace-bench](https://github.com/camilablank/workspace-bench) @ `92d763e722377f5bfae045e829a308ab5919a820`
- [agu18dec/local-workspace](https://huggingface.co/datasets/agu18dec/local-workspace) @ `5aa232fea1f07689fd6bb084c030db87705d0978` (gated; whitener moments)
- WorkspaceBench authors (Camila Blank, Adam Karvonen, Neel Nanda), Slack thread in the private MATS channel research-log-o-lens, 2026-09-29. Screenshots saved in the repo's papers/interp_methods/ (index entries 47 and 48). Private, not for citation outside the project.
