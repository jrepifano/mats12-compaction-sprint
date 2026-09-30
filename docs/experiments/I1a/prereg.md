# I1a+: WorkspaceBench reproduction gate, with saved Oracle Lens samples

Mirrored to the Claude Doc (synced at rev 99). This file is the source of truth. Earlier versions are in git history: v0.2.1 at commit `de9449f`, v0.3 at `f46da0d`.

**The question:** does the public WorkspaceBench benchmark reproduce its published scores for three activation readers on one GPU? And while the model is loaded, do ten Oracle Lens samples per activation contain better readouts than the one the benchmark scores?

## Summary table and key definitions

| | |
|---|---|
| Experiment | I1a+ (Topic 2: comparing interpretability readers) |
| Version | v0.4 draft. It fixes gpt-6-sol's round-2 and round-3 findings: the gate now uses a fixed 0.10 margin, the FVE and consensus rules are fully specified, and hallucination selection must keep its coverage. The token readers' hallucination cells are now judged on all 149 conversations. |
| Status | Draft, not approved. Nothing has been run or paid for. The implementation plan (`docs/experiments/I1a/implementation-plan.md`) is being built against this version on the laptop, at no cost. |
| Author | Claude, for Jacob Epifano |
| Reviews | gpt-6-sol: all 3 rounds done (v0.2.1, v0.3, v0.4). Round 3's fixes are in; one decision is left for Jacob (§9, question 10). |
| Budget | About $82 planned; hard cap $85 (GPU $40; judges get $83 minus GPU spend). v0.3 had a $75 cap. |

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
| Text-only monitor | Gemini 3.8 Flash (temperature 0) gets only the prompt text up to and including the read token. It never sees the target, the model's later text, or any activation. It writes 1–4 bullets on what the model is holding at that token. One output per read site is reused at every layer | All three (hallucination on all 149 conversations) | The same judge or regex as the readers. Multi-hop and hallucination are judged at one layer (44), since the readout is identical at every layer | ~$3.0 (1,323 monitor calls, 1,223 judge calls) |
| Supervised ridge probe | Ridge regression, fit separately at each layer, from the read-site activation to a target vector: the mean unembedding row of the tokens of `" " + concept`, where the concept is what the multi-hop judge looks for (the item's first match string, else its first intermediate, else its target). The readout is the top 10 tokens of the unembedding times the prediction, as for the logit lens. Five outer folds grouped by item, so an item's 11 layers never straddle a fold. Standardization is fit inside each training fold. The penalty is chosen by 4-fold inner cross-validation over {0.1, 1, 10, 100, 1000} | Multi-hop recall | The same Gemini judge | ~$0.8 (1,100 calls), no GPU beyond the reading run |

These baselines are context, not tests of the readers. The probe is trained on the task's own labels, so it is a supervised, in-domain comparison, which the trained-once readers are not. A strong prompt-only result can coexist with a reader that genuinely reads the activations [[Blank et al. 2026](#blank-et-al-2026)], so a reader that fails to beat the monitor is not thereby shown to be reading the prompt.

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
| Reading run | H100 | Readouts from three readers on three families; ten Oracle Lens samples per activation on two families, each with an FVE score; cached activations; the ridge probe | ~$26 |
| Reproduction judging and baselines | Laptop + judge API | Nine scored cells and the gate verdict; the two baselines | ~$41 |
| Sample scoring (only after the gate passes) | Laptop + judge API | Best-of-ten ceiling and two selection rules, on recall and hallucination | ~$10 |

The diagnostic runs from v0.2.1 are held in reserve (§7), to be added only if an intermediate result calls for one.

## 4. Experimental procedure

The rule: we use the benchmark's own code, item banks, read plan and judges wherever they exist. Each row says whether it is as published, adapted (with the change and the reason), or our own addition.

### 4.1 Model, code and readers

| Element | Value | Source |
|---|---|---|
| Model | `Qwen/Qwen3.6-27B` @ `6a9e13bd`, bf16, thinking off, loaded by the benchmark's own backend | As published |
| Attention | `sdpa` or eager, never FlashAttention-4 | Adapted: FlashAttention-4 corrupts this model's activations (cosine 0.87–0.97 against FlashAttention-2) [[Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026), App. I] |
| Benchmark code | `camilablank/workspace-bench` @ `92d763e`, installed from source (a verbatim copy in the repo, because the installed package omits the item banks) | As published |
| Logit lens | The benchmark's `LogitLens`, top-10 tokens | As published |
| J-Lens | The benchmark's `JLens` with the published fitted lens (`neuronpedia/jacobian-lens` @ `b25d72a9`, 1,000 wikitext prompts), top-10 tokens | As published |
| Oracle Lens | The benchmark's `OLens` recipe (prompt, injection scale 16,000, sampling at temperature 1.0, top-p 0.95, top-k 64, up to 256 tokens). Adapter from the public copy `agu18dec/olens_and_ar` @ `c95215d5`, directory `olens_s3d_rl600` | Adapted: the benchmark loads a gated copy; the public one has identical file hashes |
| Oracle Lens batching | 32 readouts per batch, never mixing layers. All readouts of one layer share the same prompt, so a batch needs no padding | Adapted: one readout at a time would take ~13 h. Checked by the batching test in §4.7 |
| Reconstructor | `ar_ptag_pooled` from the same public repo: the reconstructor this Oracle Lens was trained against. Its adapter is loaded as a second adapter on the same loaded model, because a second 27B copy does not fit in 80 GB. Its saved weight names come from a compiled, truncated model and are renamed to match; a check asserts the adapter is live (§4.7) | As published for the pairing [[Oracle Lens model card](#code-and-models)]; the loading and scoring code is ported from the public `agu18dec/olens_draft` @ `a325900` [[Code and models](#code-and-models)]; using it to select samples is ours |
| Hallucination judging | The benchmark's span judge only, with its second-stage claim verification off (`verify=0`). The headline rate and the benchmark's completeness flag do not depend on that stage, and turning it on would double every hallucination call | Adapted, for cost. v0.3's call counts never included the second stage |

### 4.2 The nine gate cells

The published values are read off the post's overall figure by pixel measurement (±0.01), or taken from a family README where one gives two decimals; the two sources agree wherever both exist.

| Family | Items | Readouts per reader | Scored by | Metric | Oracle Lens | J-Lens | Logit lens |
|---|---|---|---|---|---|---|---|
| Multi-hop recall | 100 | 1,100 (11 layers, last word) | Gemini 3.8 Flash judge | pass rate, higher is better | 0.99 | 0.82 | 0.45 |
| Multi-token recall | 100 | 1,100 (11 layers, final prompt token) | Regex; token readers are first summarized by Gemini | pass rate, higher is better | 0.71 | 0.24 | 0.20 |
| Hallucination | 149 conversations, 1,123 sites | 5,615 (5 layers) | Gemini 3.8 Flash span judge | hallucination rate, lower is better | 0.34 | 0.31 | 0.20 |

All three readers are judged on all 149 hallucination conversations. (v0.3 judged the token readers on a seeded 75 to save about $12; with the fixed margin in §4.5, the smaller set risked an "imprecise" cell.) The secondary family from v0.2.1 (multi-token multi-hop) is cut.

### 4.3 The saved Oracle Lens samples

| Element | Value | Source |
|---|---|---|
| Where | All 1,100 multi-token recall readouts; and a seeded 75 of the 149 hallucination conversations (`random.Random(20260928)`, committed before any data), all 559 of their sites at 5 layers, 2,795 readouts | Ours. Multi-token recall has headroom (0.71) and is scored for free. Multi-hop is at 0.99, so it has no room to improve |
| How many | 10 per activation: the gate's own draw plus 9 more, same settings | Adapted: k = 10 as in consensus sampling [[Bauer et al. 2026](#bauer-et-al-2026), App. A.1] and the calibration study [[Torrielli and Schneider-Kamp 2026](#torrielli-and-schneider-kamp-2026), App. D] |
| FVE per sample | The *joint bullet FVE*, the reward this Oracle Lens was RL-trained on ("joint bullet-FVE reward on the ptag AR", its `run.json`), computed as in the public reward code (`rl_reward.score_texts_joint`) with the prompt-tag reconstructor. Steps below the table | As published for the training reward, ported; using it to rank samples is ours |
| Rule 1: random | The gate's own draw | The benchmark's k = 1 protocol |
| Rule 2: best FVE | The sample with the highest joint FVE among samples with at least one parsed bullet; ties go to the lowest sample index; if no sample parses, sample 0. **Used only if all four reconstructor checks in §4.7 pass**; otherwise this rule is dropped and only consensus runs | Neel's suggestion; also named by the NLA authors [[Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026), Future work] |
| Rule 3: consensus | The sample with the highest mean Jaccard overlap of its content words with the other nine. Content words are the matches of `[a-z0-9]+` in the lowercased text, minus a frozen list of 318 English stopwords (scikit-learn 1.9.1's list, copied into the code). Two empty word sets have overlap 0. Only samples with at least one content word are candidates; if none has one, samples with any text; if all are empty, sample 0. Ties go to the lowest sample index | Ours: a judge-free, single-readout version of consensus sampling [[Bauer et al. 2026](#bauer-et-al-2026), App. A.1]. Fixed before data |
| Ceiling | Multi-token recall only. Each of the 10 samples is scored by regex as its own single-sample readout set; an item counts as a pass if any sample passes it at any layer. Measuring the ceiling on hallucination would mean judging all 28,000 or so samples (about $50), so we don't | Ours |

**Joint bullet FVE, step by step** (the public reward code, adapted only where the reconstructor type differs):

1. Strip chat scaffolding from the readout, then split it into up to 4 bullets (lines starting with `- `). A readout with no bullet lines counts as one phrase; an empty readout scores 0 and is marked unparsed.
2. Tokenize each bullet with no chat template and no special tokens, keeping at most 64 tokens. Prefix the reconstructor's layer tag, `[Layer NN]` for the activation's layer.
3. Run the model with the reconstructor adapter on. Take the output of block 63 at the bullet's last token (the reconstructor was trained on this model cut after block 63 with no final norm, which is the same value). Map it through the reconstructor's head to a predicted activation.
4. Whiten the prediction and the true activation with that layer's pooled whitener: subtract the mean μ, then multiply by W = (Σ + λI)^−1/2 with λ = 0.1 × the mean of Σ's diagonal (the reconstructor's own `ridge_c`). The whiteners are the 17 files `data/whiteners/pooled/whitening_iolens_pooled_L{0,4,…,60,63}.safetensors` of the gated `agu18dec/local-workspace` dataset (104.9 MB each, 1.78 GB in all), the family the reconstructor names in its `meta.json`.
5. Scale each bullet's whitened prediction to unit length, fit the whitened true activation as a non-negative combination of them (projected-gradient least squares, 80 iterations), and report FVE = 1 − ‖x − fit‖² / ‖x‖².

### 4.4 Sample sizes and power

The banks fix the item counts, so the question is what size of difference we can detect.

- **Gate.** The margin is a fixed 0.10 (§4.5), Jacob's choice at review. It does not grow with noise, as v0.3's did. It is about the smallest margin a 100-item cell can resolve, and every published gap the ordering checks use is at least that large. With 100 items, a pass rate's 95% interval has a half-width of at most about 0.098 (at 0.50), so the recall cells can always be assessed. The hallucination cells use all 149 conversations; their interval is clustered by conversation and is expected to be narrower. A pipeline bug that halved a reader's score would be caught almost surely; a real 3-point drift would not, and we don't claim otherwise.
- **Samples.** Each selection rule is compared with the random draw on the same readouts, which removes most item-to-item noise. With 100 items for recall and 75 conversations for hallucination, the preview can detect changes of roughly 7 and 6 points. Smaller effects are left to the full experiment.

### 4.5 Metrics and decision rules

**Gate.** Each cell's metric is exactly what the benchmark reports. We compute a 95% item-bootstrap interval for every cell (2,000 draws, seed 0; for hallucination, conversations are resampled and the rate is the ratio of summed counts, as the benchmark computes it). With d = ours − published and h = the interval's half-width, each cell gets one status:

| Status | Rule | Meaning |
|---|---|---|
| Imprecise | h > 0.10 | Too noisy to assess against a 0.10 margin |
| Consistent | h ≤ 0.10 and \|d\| ≤ 0.10 | No difference larger than the margin was found. This is a tolerance rule set in advance, not an equivalence test |
| Discrepant | h ≤ 0.10 and \|d\| − h > 0.10 | The whole interval lies beyond the margin: a miss we are confident of |
| Borderline | h ≤ 0.10, neither of the above | The point estimate misses the margin, but the interval reaches back inside it |

*Ordering checks:* wherever the published gap between two readers in one family is at least 0.10, our gap must have the same sign and be at least 0.05. There are seven such pairs (three in multi-hop, two in each of the others).

**Samples.**

- Recall: the benchmark's pass rate for each rule, and for the ceiling.
- Hallucination: the benchmark's hallucination rate for each rule.
- For each rule, the paired difference from the random draw, with a paired item-bootstrap 95% interval (2,000 draws, seed 0).
- A rule *helps on recall* if it improves the pass rate by at least 5 points with the interval excluding zero.
- A rule *lowers the measured hallucination rate* if it cuts the benchmark's rate by at least 5 points with the interval excluding zero **and** it passes three proxy coverage checks on the same sites. The checks stop the crudest way to win, picking empty or vaguer readouts. They cannot rule out a subtler one: keeping the bullet count and one specific claim while dropping other claims. So without claim-level verification this result is reported as a lower measured rate, not as fewer false claims per true claim. The proxy checks all require:
    - the share of empty readouts rises by at most 2 points;
    - the mean number of bullets per readout falls by at most 10%;
    - the benchmark's `assert_share` (the share of readouts that say something specific about the conversation, from the same span judge) falls by at most 5 points.

  The benchmark's claim-level measures (true, false and unverifiable claims per readout) need the second-stage verification, which is off (§4.1). They are offered at checkpoint 5 as an add-on (about $14.5 for the three sample arms). With them, the claim becomes "fewer false claims without fewer true ones": the rule must also keep true claims per readout within 10% of the random draw's.
- We always report recall and hallucination side by side, with the three coverage numbers.

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
| Reconstructor live | Every saved reconstructor adapter weight maps onto the model after renaming; the head's weights have the expected shapes; its stored layer tags equal the tokenizer's `[Layer NN]` tokens for every layer; switching adapters changes the output | Ported from the public loader's liveness check, made strict |
| Reconstructor ruler | Fed the source text itself, the reconstructor's whitened cos² with the true activation is checked at each of the 11 scored layers (20, 24, …, 60; the activation cache holds every layer). The mean over the 11 must lie within ±0.05 of its own validation FVE for the same layers (0.216, from its `meta.json`), and every layer within ±0.08 of that layer's value. The source text is the span of tokens that *follows* each hallucination read site, of length drawn uniformly from 1 to 32 (seed 1234): the reconstructor was trained to map a span to the activation at the position just before it (`olens_draft`, `pipeline/multilayer.py`). Sites with no following token are skipped and counted | Ours. The hallucination sites are the reconstructor's training domain: assistant text in chat |
| Reconstructor band | The mean joint FVE of the hallucination gate draws lies within 0.487 ± 0.10 (the held-out value in the Oracle Lens's `run.json`) | Ours |
| Reconstructor mismatch | Our readouts score higher joint FVE on their own activation than on another site's activation at the same layer, in at least 80% of readouts overall and in every layer | Ours |
| Shuffled activations (manipulation check) | Given another item's activation, the Oracle Lens passes at most 10% of multi-token recall items. Scored by regex, so it costs no judge money | Ours; v0.2.1 ran this on multi-hop with a judge |
| Judge pilot | 10 readouts per family judged across the readers (4, 3 and 3); a random 10 verdicts per family shown to Jacob | The benchmark's own checklist |
| Completeness | No missing or unjudged readouts; at most 5% empty | The benchmark's own rule |

If any of the first seven checks still fails after one round of debugging, we stop and report. No judge money is spent before they pass. The four reconstructor checks gate only the best-FVE rule: if any fails, that rule is dropped and everything else goes on.

### 4.8 Seeds and outputs

- Oracle Lens seeds: the gate draw uses 20260928 + batch index; the nine extra samples use 20261001 + batch index; the shuffled check 20261002, and the two reserve generations 20261003 and 20261004. Batches follow one fixed order (layer, then sample, then site) written into the code before any run. Every readout records its seed and batch.
- The hallucination sample subset (75 conversations, 559 sites) is drawn with `random.Random(20260928)` and committed before any data (`data/I1a/subsets/hallucination_75.json`). The shuffled check uses a fixed derangement of items, seed 20260928.
- Activations at all 64 layers for every read position are cached in bf16, the model's own precision, so the cache is exact (about 1 GB). A manifest records every model, adapter, lens and code hash. Readouts follow the benchmark's file format, so they can be judged by its own tools.

## 5. Hypotheses and predictions

All priors are Claude's judgement, written before any data. Jacob may change them before approval; after that they are fixed.

### 5.1 The gate

**Main hypothesis: the public stack reproduces the published numbers.** At least 8 of 9 cells are consistent, none discrepant or imprecise, every ordering check holds, and all checks pass. **P = 0.55** (written for v0.3's tolerance rule and left unchanged; the fixed margin is looser than v0.3's on precise cells, 0.10 against 0.05, but no longer grows with noise).

Competing explanations for a failure (not exclusive, so they don't sum with the main one):

| Hypothesis | Its signature | P |
|---|---|---|
| **Figure vintage.** Some published bars predate the 2026-09-23 instrument changes | Misses confined to multi-hop, the family whose read position moved | 0.20 |
| **Judge drift.** Gemini 3.8 Flash on OpenRouter behaves differently now | Gemini-judged cells miss; all regex-scored cells reproduce | 0.15 |
| **Different artifact.** The published Oracle Lens or J-Lens used another checkpoint or lens file (the model card says later RL branches "may supersede" this one) | Every cell of one reader misses, including its regex cell | 0.10 |
| **Sampling noise.** The published Oracle Lens scores are single draws | On multi-token recall, the published value lies inside the range of our ten single-sample pass rates | 0.10 |

These signatures overlap: multi-hop is also Gemini-judged, so a miss confined to multi-hop fits both figure vintage and judge drift. §6.1 therefore names the causes as competing possibilities, to be separated by the reserve diagnostics (§7.5). The sampling-noise signature is a diagnostic only. It is measured on multi-token recall, where ten samples are saved and scored for free, and it never changes a gate verdict.

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

The gate is read first, top to bottom; the first matching row applies, and the last row catches everything else. A decisive failure ("Does not reproduce") outranks an imprecise cell. A *miss* is a cell that is not consistent, or a cell in a failed ordering check.

### 6.1 The gate

| Scenario | What we observe | Answer | Why it matters to a lab | Next step |
|---|---|---|---|---|
| **Not readable** | One of the first seven checks in §4.7 still fails after one debugging round, or the shuffled-activation check passes more than 10% of items | Unanswered: our pipeline isn't a valid instrument yet | None | Stop and report the failing check. Fix it within the cap if it's a known bug, otherwise ask Jacob |
| **Scope-curtailed** | Any gate cell was cut for cost (§7.3) | Partial: report the cells we have, cell by cell; no overall claim | None beyond the cells | Jacob decides whether to fund the missing cells |
| **Does not reproduce** | ≥ 2 discrepant, or ≥ 2 ordering checks fail, or (no cell imprecise and ≤ 5 of 9 consistent) | No, not from public artifacts | Don't quote WorkspaceBench rankings as settled | Send the table to Camila and Neel and ask for their readouts. Report the free recall-sample results as within-our-pipeline only; no hallucination sample judging |
| **Imprecise** | Any cell is imprecise (interval half-width > 0.10) | Inconclusive: the gate cannot be assessed at this margin | Weak evidence either way | Report cell by cell; find out why the interval is wide before any later work |
| **Reproduces** | ≥ 8 of 9 cells consistent, none discrepant, all 7 ordering checks hold | Yes, for these three readers and families | The reader ranking is independently confirmed and citable; the benchmark works as a regression test | Score the samples (§6.2). Then the NLA and the full scaffold experiment |
| **Multi-hop only** | All misses are in multi-hop | Partly. Two competing causes fit: the figure predates the read-position change, or the Gemini judge drifted (multi-hop is judged by Gemini) | Scores must carry their instrument and judge version | Add the old-read-position diagnostic (~$1, §7.5); if it doesn't explain the miss, the judge re-test. Continue, with our numbers as the baseline |
| **Judge only** | All misses are Gemini-judged; every regex cell is consistent | Partly. Judge drift is one possible cause; a pipeline difference that only shows under the judge is another | LLM-judged interp benchmarks can drift within days | Add the judge re-test diagnostic (~$1). Continue, comparing only readouts judged in the same session |
| **One reader** | All misses belong to one reader | Partly. That reader's public artifact may differ from the scored one, or its single published draw was unlucky | Re-benchmark that reader before adopting it | Report the sampling-noise diagnostic (§5.1) beside it, without changing the verdict. Ask the maintainers which checkpoint was scored |
| **Inconclusive** | Anything else | Report cell by cell; no overall claim | Weak evidence of fragility | Jacob picks at most one diagnostic from §7.5. Later work proceeds only on cells that reproduced |

### 6.2 The samples (only after a continuing gate outcome)

| Scenario | What we observe | Answer | Why it matters to a lab | Next step |
|---|---|---|---|---|
| **Selection helps** | A rule improves recall, or lowers the measured hallucination rate with the proxy coverage checks passed (§4.5), on one metric without worsening the other by 5 points or more | Yes, a cheap rule improves this reader | A free inference-time fix for trained readers | Run the full scaffold experiment: add the NLA, more families, more samples |
| **Room, but no good selector** | Ceiling beats random by ≥ 10 points, but neither rule helps | The samples contain better readouts, and these rules can't find them | The bottleneck is choosing, not generating | Scaffold work shifts to better selectors, such as a judge or a trained verifier |
| **No room** | Ceiling beats random by < 5 points, and neither rule helps | No: resampling this reader doesn't help | Scaffolds for this reader aren't worth building | Drop the scaffold line; move to NLA-vs-workspace or reader overlap |
| **Mixed** | Anything else | Report as observed | — | Jacob decides |

## 7. Budget, timeline and stopping points

**Planned spend is about $82; the hard cap is $85.**
- Rates: Gemini 3.8 Flash $0.75 in / $3.75 out per million tokens (OpenRouter, 2026-09-28); H100 80GB $2.69/h community, $3.49/h secure.
- Per-call costs are v0.3's estimates from the family READMEs, unrounded: $0.000752 per multi-hop call ($2.48 / 3,300), $0.000409 per summarizer call ($0.90 / 2,200), $0.001729 per hallucination span call ($9.71 / 5,615), $0.00214 per token-reader hallucination readout, summary plus span call ($12.09 / 5,650). The judge pilot replaces them with measured values.

### 7.1 Judge spend

| Item | Calls | Central | High |
|---|---|---|---|
| Multi-hop, 3 readers × 1,100 | 3,300 | $2.48 | $4.95 |
| Multi-token recall, summarizer for 2 token readers (Oracle Lens is regex, free) | 2,200 | $0.90 | $2.31 |
| Hallucination, Oracle Lens, all 149 conversations (span judge only) | 5,615 | $9.71 | $17.69 |
| Hallucination, J-Lens and logit lens, all 149 conversations (summary + span judge) | 22,460 | $24.03 | $47.17 |
| Pilots and dry runs | ~200 | $0.50 | $0.50 |
| **Gate subtotal** | | **$37.6** | **$72.6** |
| Text-only monitor: 1,323 monitor calls; judged on multi-hop (100 calls, one layer) and hallucination (1,123 calls, one layer); multi-token recall by regex | 2,546 | $3.01 | $5.67 |
| Ridge probe, multi-hop, 1,100 calls | 1,100 | $0.83 | $1.65 |
| **Gate + baselines (checkpoint 5)** | | **$41.5** | **$79.9** |
| Samples: hallucination, 2 rules × 2,795 readouts (after the gate, Jacob's call at checkpoint 5) | 5,590 | $9.67 | $17.61 |
| Samples: multi-token recall, the ceiling and the shuffled check (regex) | — | $0 | $0 |
| **Total** | | **$51.1** | **$97.6** |

The high column cannot fit: under high prices the pre-set cuts in §7.3 apply, and the run ends *scope-curtailed*, which can never count as "Reproduces".

### 7.2 GPU spend (one H100)

| Phase | Hours |
|---|---|
| Pod up, environment, downloads (model 55.6 GB, J-Lens 3.3 GB, adapters 1.0 GB, pooled whiteners 1.8 GB; 61.7 GB in all, measured from the repos' metadata) | 1.0 |
| Smoke checks | 1.0 |
| Activation capture and token readers | 0.5 |
| Oracle Lens gate draws, shuffled check and reserve readouts (10,378 generations in 378 batches, since batches never mix layers; ~13 s per batch) | 1.4 |
| Oracle Lens, 9 extra samples (35,055 generations: 9 × (1,100 + 2,795), in 1,109 batches) | 4.0 |
| Reconstructor FVE on 38,950 readouts (estimate; measured at smoke) | 0.5 |
| Ridge probe | 0.1 |
| Debugging buffer | 2.5 |
| **Total** | **11.0 h ≈ $29.6** ($38.4 on secure cloud) |

- The extras alone take 4.0 h at 13 s per batch (1,109 batches), which leaves no throughput margin inside their row. The smoke test measures the real batch time and updates this projection before any long run.
- Without the `flash-linear-attention` kernels (not in the benchmark's lock file), the model's 48 linear-attention layers run on a slower pure-PyTorch path. The smoke test measures that too.
- Peak memory is about 73 GB while the token readers run (model, J-Lens and unembedding in fp32) and about 60 GB for the Oracle Lens and reconstructor adapters on the one model, so both fit in 80 GB.
- Storage adds about $1.

### 7.3 Cap and pre-set cuts

- **Hard cap $85.** GPU sub-cap $40, enforced by the pod watchdog. Judge cap = $83 minus actual GPU and storage spend, enforced in software: a spend check before each chunk of 25 items and a budget reservation before every request, retries included (the OpenRouter key is shared across experiments, so it carries no I1a credit limit; Jacob, 2026-09-30). Reserve $2.
- **Central plan:** GPU and storage $30.6, gate and baselines $41.5, samples $9.7. That is $81.8 in total, and judges $51.1 against a judge cap of $52.4.
- **On secure cloud** (GPU and storage $39.4, judge cap $43.6): the gate and baselines ($41.5) fit, and hallucination sample judging shrinks to what remains, about 15 conversations ($1.9). The gate itself is never cut for this.
- **If the smoke test projects Oracle Lens generation above 6 h:**
    1. install the linear-attention kernels, then re-run the batching check;
    2. if still above 6 h, cut the extra samples from 9 to 4;
    3. then cut the hallucination sample set from 75 to 30 conversations.

  The gate cells are never cut for GPU reasons.
- **If the judge pilot projects more than the judge cap,** cut in this order, stopping as soon as it fits:
    1. shrink sample scoring to 30 conversations, then drop it;
    2. drop the text-monitor hallucination judging;
    3. judge the J-Lens and logit-lens hallucination cells on the seeded 75 conversations only.

  Step 3 cuts the gate, so the run becomes *scope-curtailed*. If even that doesn't fit, stop before judging and ask Jacob.

### 7.4 Timeline and checkpoints

About 2–3 days elapsed: roughly 11 h of pod time, 3 h of judging, 2 h of analysis. Jacob's time is about 2.5 h: approving this plan, the spend checkpoints, and reading random raw samples. At each checkpoint work stops, the report is updated, and Jacob decides.

| Checkpoint | Content | Spend so far |
|---|---|---|
| 0. Approval | Jacob approves this plan and the $85 cap | $0 |
| 1. Laptop setup | Pinned checkout, offline tests, read plan, subsets and seeds committed, the whole pipeline run end to end on a tiny random model | $0 |
| 2. Pod smoke | The §4.7 checks, memory and speed. No judging | ≤ $6 |
| 3. Readouts done | All readouts, samples, FVE scores, probe and caches produced and pulled; pod terminated | ≤ $40 |
| 4. Judge pilot | Measured cost per call, spend projection, cuts if needed; a random 10 verdicts per family shown to Jacob | ≤ $41 |
| 5. Gate verdict | All gate and baseline judging; the §6.1 table; free recall-sample results; random raw readouts per reader. **Jacob decides whether to score the hallucination samples, and whether to add claim verification (§4.5)** | ≤ $73 |
| 6. Samples | Hallucination sample judging and the §6.2 table, with random raw samples | ≤ $85 |

### 7.5 Diagnostics held in reserve

None of these runs by default. Each is tied to the gate scenario that would call for it, so we add one only when an intermediate result points to it.

Once the pod is terminated at checkpoint 3, anything that needs new generation costs a second pod session (about $10 of setup). So the readouts the first and third diagnostics need are generated while the pod is up (all three readers at the 33 old read positions, and a second Oracle Lens draw on all of multi-hop: about 10 extra GPU minutes, ~$0.50). They are never judged unless a scenario calls for them (question 5).

| Diagnostic | What it answers | Add it when | Judge cost to add |
|---|---|---|---|
| Multi-hop at the old read position (the 33 items whose position moved on 2026-09-23) | Do the published multi-hop bars predate the change? | "Multi-hop only" | ~$0.80 |
| Judge re-test: re-judge 200 multi-hop and 200 hallucination readouts | Is the Gemini judge stable run to run? | "Judge only" | ~$0.50 |
| Second Oracle Lens draw on multi-hop | How much does one draw vary on a judged family? | "One reader", for the Oracle Lens on multi-hop | ~$0.80 |
| A second saved sample judged on the 75 hallucination conversations | Sampling noise vs judge noise for the hallucination cell | "One reader", for the Oracle Lens on hallucination | ~$2.00 |

## 8. Implementation plan

The implementation plan is `docs/experiments/I1a/implementation-plan.md` (work units, interfaces, tests, paid steps). In outline:

- **Reused from the working repo and E1:** the pod scripts (bring-up, teardown, watchdog, budget check), the bootstrap statistics, and the batched-versus-unbatched check pattern. The shared spend ledger and its combined cap guard now hold separate caps per experiment.
- **New:**
    - activation capture through the benchmark's own backend;
    - a batched Oracle Lens that reuses the benchmark's prompt and injection code;
    - reconstructor scoring ported from `olens_draft`;
    - the two selection rules;
    - a chunked judging wrapper with a spend check;
    - the two baselines;
    - the analysis script.
- **Compute:** one H100 80GB (fallbacks: H100 NVL, secure H100, A100 80GB). Judging runs on the laptop; the OpenRouter key never goes to the pod.
- **Downloads:** the gated agu18dec/local-workspace repo is 3.38 TB (data/ 3.12 TB, ckpts/ 249 GB), so it is never cloned or snapshotted whole.
    - Every Hugging Face fetch names its files, and a wrapper refuses any download over 100 GB.
    - The benchmark's own Oracle Lens loader would pull the adapter from the gated repo, so we load the identical public copy instead.
    - Total pod downloads are 61.7 GB (sizes read from the repos' metadata).
- **Order:** the whole pipeline first runs on the laptop on a tiny random model with the same architecture, then the pod smoke checks, then the full reading run, then judging, then analysis.

## 9. Risks, limitations and open questions

**Risks and limitations**

1. **No published intervals or readouts.** We compare against point estimates read off a figure, unpaired. The gate can confirm agreement within about 5–10 points, not exact equality.
2. **Unknown figure vintage.** Only multi-hop has a cheap test (a reserve diagnostic).
3. **Our reconstructor scoring is a port.** The model card's `global_workspace` package is private. Its public equivalent, `olens_draft`, ships the loader and the joint reward, but its joint reward is written for a different reconstructor type, and its loader would load a second 27B copy. We port both and adapt them to the prompt-tag reconstructor on one shared model. The four reconstructor checks in §4.7 test the port; if any fails, the best-FVE rule is dropped. The consensus rule doesn't need it.
4. **Whitened FVE needs gated files** (resolved Sep 29: terms accepted, so FVE is whitened as in the training reward).
5. **One judge, not a panel.** The project norm asks for two judges from different families on headline numbers. A reproduction has to use the benchmark's pinned judge, so this is a deliberate exception.
6. **The sample preview is small.** 75 conversations detect only effects of about 6 points or more. It is meant to decide whether the full experiment is worth running, not to be published on its own.
7. **The consensus measure is crude.** Word overlap may favour readouts that repeat common words. It is fixed before data, and an embedding-based version is left to the full experiment.
8. **Batching departs from the benchmark's one-at-a-time generation.** The batching check covers it; sampled text was never going to match the original run exactly anyway.
9. **Source-reading gaps.** The four benchmark source files unread in v0.3 (registry, read plan, judge config, LLM client) have now been read, along with the three families' judges and the reconstructor code. Reading them changed the design twice: claim verification is switched off (§4.1), and the reconstructor needs renamed adapter weights. Neel's WorkspaceBench announcement tweet could not be fetched.
10. **Claim verification is off.** Hallucination judging uses only the benchmark's span judge. Its headline rate is unaffected, but its claim-level extras (supported and unverifiable claims) are not measured unless Jacob adds them at checkpoint 5.
11. **Linear-attention speed.** Without the optional kernels the reading run may be slower than planned. The smoke test measures it, and §7.3 sets the cuts.

**Open questions for Jacob**

1. Can Neel or Camila share their per-reader readouts or result files? Re-judging about 200 of their readouts (~$0.20) would separate judge drift from pipeline differences.

    **Answer (Sep 29):** assume no. The paper arrives later this month; until then a miss attributed to the judge stays "likely, not confirmed" (§6.1). Re-judge ~200 of their readouts if the paper ships them.
2. May we accept the terms of the gated `agu18dec/local-workspace` dataset (auto-approved) on your HF account, to get the whitener moments? Otherwise FVE is unwhitened.

    **Answer (Sep 29):** terms accepted; download only the pooled whitener moments (~1.8 GB) and report whitened FVE.
3. Is the $75 cap right? The gate alone is about $51 at central estimates.

    **Answer (Sep 29):** the $75 cap stands. **Updated at v0.4:** Jacob raised the cap to $85 to judge the token readers on all 149 conversations (question 7).
4. The shuffled-activation check is kept as the one manipulation check, because it costs no judge money and about 5 GPU minutes. Keep it?

    **Answer (Sep 29):** keep it.
5. Generate the readouts for the reserve diagnostics while the pod is up (~$0.50, §7.5)?

    **Answer (Sep 29):** yes, generate them while the pod is up.
6. May the agent run the benchmark's own code on the laptop and pod? It was blocked in the last session.

    **Answer (Sep 29):** yes. The agent may run the benchmark's code and data on the pod, and on the laptop with the pooled whiteners (~1.8 GB). The gated repo is 3.38 TB, so §8 limits every download to named files.
7. Keep judging the token readers' hallucination readouts on half the conversations only? That saves about $12.

    **Answer (Sep 29):** yes, keep 75 conversations. The gate's 0.05 floor likely binds at 75 anyway, the samples use the same 75, and +$12 would put the plan (~$81) over the $75 cap.

    **Revised at v0.4 (Jacob, after gpt-6-sol round 2):** judge all 149. With the fixed 0.10 margin, a noisier cell risks being imprecise; the cap rises to $85 to pay for it. The saved samples stay on the seeded 75.

Added after Neel's screenshots (2026-09-29):

8. Can you get Camila's CSV of the per-family scores from Neel? It becomes the gate's published targets.

    **Answer (Sep 29):** no; the gate uses the figure-read values in §4.2 (±0.01) as its targets.
9. Neel's worry about hallucination suggests moving the sample preview toward hallucination. **Recommendation:** raise the hallucination sample set from 30 to the same 75 conversations on which the token readers are judged. That costs about $5 more GPU (~15,000 more generations) and about $6 more judging, which is ~$66 planned in total, still under the $75 cap. It also gives one set of conversations with all three readers and both selection rules judged. The NLA, whose hallucination rate is highest and where selection could help most, comes in the next step because it needs its own 27B model.

    **Answer (Sep 29):** accepted; §4.3, §4.5, §4.8, §5.3, §7 and the risks now use 75 conversations.

Added at v0.4 (gpt-6-sol round 3):

10. **Should "Reproduces" allow one borderline cell?** Your rule is ≥ 8 of 9 consistent with none discrepant or imprecise, so one cell may be borderline: its point estimate misses by more than 0.10, but its interval reaches back inside the margin. At 100 items that cell could sit up to about 0.20 from the published value. gpt-6-sol asks for 9 of 9 for an unqualified claim. **Recommendation:** keep your rule, but name the borderline cell in the verdict ("Reproduces, 8 of 9 consistent, one borderline"). A rough estimate: if our pipeline were exactly right, item sampling alone would push at least one of the nine point estimates more than 0.10 off about 11% of the time, mostly in the logit-lens multi-hop cell at 0.45. That counts our own binomial noise only (hallucination cells assumed to have SE 0.03). A 9-of-9 rule would fail a correct pipeline that often.

## 10. Review history

| Date | Round | What changed |
|---|---|---|
| 2026-09-28 | gpt-6-sol round 1, on v0.2.1 (`logs/reviews/20260928-I1a-prereg-r1.md`) | 9 blocking and 5 minor findings, all fixed: the ordering-check count, the completeness rule, within-layer batching with a logit-level test, a hard judge-spend cap, scope-curtailed runs can't pass, and a catch-all scenario |
| 2026-09-29 | Jacob | Chose I1a+: save ten Oracle Lens samples per activation; cut every diagnostic run to start; add diagnostics back only after intermediate results |
| 2026-09-29 | v0.3 draft | Diagnostics moved to a reserve list; secondary family cut; sample design, hypotheses and scenarios added; shuffled check moved to a free regex family; cap raised to $75. gpt-6-sol round 2 is next |
| 2026-09-29 | New information | Neel's Slack screenshots logged before any data (§2): an exact per-family score CSV exists, the Activation Oracle was scored with the public code at our commit, and NLAs hallucinate in ≈ 96% of readouts. Open questions 8–9 added. Priors unchanged. |
| 2026-09-29 | Jacob's review closed | All nine questions answered. Added: simple baselines (text-only monitor, linear probe), 75-conversation hallucination samples, whitened FVE from the pooled whiteners only (~1.8 GB of the 3.38 TB gated repo), a named-files download rule. Costs brought in line: ~$69 planned, GPU sub-cap $40, checkpoint limits $40 / $41 / $65 / $75. |
| 2026-09-29 | gpt-6-sol round 2, on v0.3 (`logs/reviews/20260929-I1a-prereg-r2.md`) | 4 blocking, 6 minor, 1 nit |
| 2026-09-29 | v0.4 draft | Jacob's decisions: a fixed 0.10 margin with an "imprecise" status that makes the gate inconclusive; token readers' hallucination judged on all 149 conversations; cap $85. Fixes for every round-2 finding: joint bullet FVE specified step by step from the public reward code, with the 17 whitener files named and two checks before best FVE can be used; hallucination selection must keep its coverage (empty rate, bullets, `assert_share`); the "One reader" relabel removed; consensus fully specified; ridge probe and text monitor specified and their claims softened; costs recomputed (baseline call counts, secure-cloud cut, high estimate ends scope-curtailed, 35,055 generations and 38,950 FVE scores, pilot 10 per family, 11 pod hours); overlapping scenario causes labelled as competing. Found while reading the code: claim verification would double hallucination calls, so it is off; the reconstructor adapter needs renamed weights to share the loaded model. gpt-6-sol round 3 (the last) is next |
| 2026-09-29 | gpt-6-sol round 3, on v0.4 (`logs/reviews/20260929-I1a-prereg-r3.md`) | 3 blocking, 2 minor, 1 nit. Fixed: a decisive failure now outranks an imprecise cell; the hallucination claim is narrowed to "lower measured rate" unless claim verification is added; the reconstructor checks are strict (every adapter weight, tag ids, per-layer ruler over the 11 scored layers, per-layer mismatch); unrounded per-call prices; layer-separated batch counts (378 and 1,109 batches). Left to Jacob: whether "Reproduces" may include one borderline cell (question 10). This was the last review round |

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
- [agu18dec/olens_draft](https://github.com/agu18dec/olens_draft) @ `a32590085a6070d75f950155d3c2b3db3eec97ef` (MIT; public reconstructor loader, whitening and joint-FVE reward code, vendored in the repo's `third_party/olens_draft/`)
- WorkspaceBench authors (Camila Blank, Adam Karvonen, Neel Nanda), Slack thread in the private MATS channel research-log-o-lens, 2026-09-29. Screenshots saved in the repo's papers/interp_methods/ (index entries 47 and 48). Private, not for citation outside the project.
