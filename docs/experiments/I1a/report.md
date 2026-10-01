# I1a+ results: WorkspaceBench reproduction and Oracle Lens samples

2026-10-01 · Jacob Epifano

## Summary

WorkspaceBench's published scores ([Blank et al. 2026](#blank-et-al-2026)) for three activation readers on Qwen3.6-27B match cell by cell from public artifacts: all 9 cells we tested land within 0.10 of the published number. One ranking check fails, so the preregistered gate verdict is "Judge only", not "Reproduces". And a text-only baseline that never sees an activation beats all three readers on recall and hallucination and comes within 0.02 of the best on multi-hop, and drawing more Oracle Lens samples and picking one with a cheap rule does not help.

We asked two things. First, does the public stack reproduce the published numbers for the logit lens, the J-Lens and the Oracle Lens? Second, if it does, can a judge-free rule pick a better Oracle Lens readout from several samples?

- **Reproduction: the cells yes, the gate not fully.** 9 of 9 cells are consistent with the published scores, none discrepant, but 1 of 7 ranking checks fails (J-Lens vs logit lens on hallucination), so the gate did not pass as "Reproduces"; the preregistered verdict is "Judge only". Why that gap shrank is untested: judge drift between sessions, or a pipeline difference that shows only under the judge.
- **Text-only baseline: matches or beats every reader.** Gemini 3.8 Flash, given only the prompt up to the read position, scores 0.98 on multi-hop and 0.91 on multi-token recall, against the Oracle Lens's 1.00 and 0.75. Its hallucination rate is 0.02 against 0.33. In six randomly drawn multi-hop and recall prompts, none contains the answer; the model answers the question from the prompt. Its low hallucination rate has other possible causes (fewer bullets, the same model family writing and judging).
- **Samples: no cheap win.** The best of five Oracle Lens samples beats a single draw by 7 points on recall, but the consensus rule gains nothing (recall +0.01, hallucination −0.001), and best-FVE selection was dropped by a failed check (an exploratory run also gained nothing).

What this changes: on multi-hop and recall, a reader's pass does not show that it read anything the prompt doesn't already give away. A fair comparison of activation readers needs items that prompt-only guessing cannot solve. We recommend dropping the sampling-scaffold line and building that comparison next. Total cost was $67.52 against an $85 cap.

## What we ran

We ran WorkspaceBench's own code at a pinned commit on the published model and readers, then judged with its pinned judge. Everything followed the [I1a+ preregistration](#i1a-preregistration) (v0.4 plus Amendment A), with the deviations listed below.

| Part | What we used |
| --- | --- |
| Model | Qwen3.6-27B (`Qwen/Qwen3.6-27B` @ `6a9e13b`), bf16, sdpa attention, one H100 80GB (RunPod secure cloud) |
| Benchmark | WorkspaceBench @ `92d763e` (vendored), its read plan, renderer, regex scorer and judge prompts |
| Readers | Logit lens; J-Lens (`neuronpedia/jacobian-lens` @ `b25d72a`, the published Qwen3.6-27B file); Oracle Lens (`agu18dec/olens_and_ar` @ `c95215d`, adapter `olens_s3d_rl600`, the benchmark's prompt and sampling: temperature 1.0, top-p 0.95, top-k 64, 256 new tokens) |
| Families | Multi-hop (100 items × 11 layers = 1,100 cells, Gemini judge); multi-token recall (100 items × 11 layers, regex for the Oracle Lens, Gemini summary then regex for token readers); hallucination (all 149 conversations, 1,123 read sites × 5 layers = 5,615 cells, Gemini span judge, claim verification off) |
| Judge | `google/gemini-3.8-flash` through OpenRouter, the benchmark's pinned instrument |
| Samples | 5 Oracle Lens samples per activation (the first is the gate readout), on every recall cell and on a seeded subset of 75 hallucination conversations (559 sites × 5 layers = 2,795 cells) |
| Selection rules | Consensus (the sample with the most content words in common with the other four); best FVE (the sample the matched reconstructor `ar_ptag_pooled` explains best, as joint bullet FVE) |
| Baselines (not gated) | Text-only monitor (Gemini 3.8 Flash sees the prompt up to and including the read token, writes 1–4 bullets); ridge probe (multi-hop only, trained on the task's labels, nested cross-validation) |

The gate rules were fixed in advance. A cell is consistent if our score is within 0.10 of the published one. The gate reproduces if at least 8 of 9 cells are consistent, none discrepant, and all 7 ranking checks hold; a ranking check holds if our gap between two readers has the published sign and is at least 0.05. Published values are read off WorkspaceBench's figures, to about ±0.01.

## Result 1: the reproduction gate

All 9 cells match WorkspaceBench's published scores within the 0.10 margin, but one of the 7 ranking checks fails, so the preregistered gate lands on "Judge only" rather than "Reproduces".

*(Chart in the Claude Doc: "All 9 cells land within 0.10 of the published score". Data, from `results/I1a/analysis/summary.json`:)*

| Family | Reader | Ours (95% CI) | Published | Status |
| --- | --- | --- | --- | --- |
| Multi-hop | Logit lens | 0.52 (0.42–0.62) | 0.45 | consistent |
| Multi-hop | J-Lens | 0.83 (0.76–0.91) | 0.82 | consistent |
| Multi-hop | Oracle Lens | 1.00 (1.00–1.00) | 0.99 | consistent |
| Multi-token recall | Logit lens | 0.20 (0.13–0.29) | 0.20 | consistent |
| Multi-token recall | J-Lens | 0.24 (0.15–0.33) | 0.24 | consistent |
| Multi-token recall | Oracle Lens | 0.75 (0.66–0.84) | 0.71 | consistent |
| Hallucination rate (lower is better) | Logit lens | 0.272 (0.258–0.287) | 0.20 | consistent |
| Hallucination rate | J-Lens | 0.299 (0.284–0.317) | 0.31 | consistent |
| Hallucination rate | Oracle Lens | 0.329 (0.288–0.370) | 0.34 | consistent |

Multi-hop and recall match closely. Our logit-lens hallucination rate (0.272) sits 7 points above the published 0.20: inside the margin, but enough to shrink its gap to the J-Lens. Feeding the Oracle Lens shuffled activations drops its recall to 0.00, so its recall depends on the activation.

| Family | Pair | Published gap | Our gap | Holds |
| --- | --- | --- | --- | --- |
| Multi-hop | Oracle Lens − J-Lens | 0.17 | 0.17 | yes |
| Multi-hop | Oracle Lens − logit lens | 0.54 | 0.48 | yes |
| Multi-hop | J-Lens − logit lens | 0.37 | 0.31 | yes |
| Multi-token recall | Oracle Lens − J-Lens | 0.47 | 0.51 | yes |
| Multi-token recall | Oracle Lens − logit lens | 0.51 | 0.55 | yes |
| Hallucination rate | Oracle Lens − logit lens | 0.14 | 0.06 | yes |
| Hallucination rate | J-Lens − logit lens | 0.11 | 0.03 | **no** (needs ≥ 0.05) |

The only miss is in a Gemini-judged family and every regex-scored cell is consistent, so the preregistered scenario is "Judge only". Its required diagnostic re-judged 200 multi-hop and 200 hallucination cells with an empty cache. Multi-hop verdicts agreed on 199 of 200 (Cohen's kappa 0.99); hallucination verdicts agreed on 84.5% (kappa 0.60; 18 flipped to hallucinated, 13 back). Per-cell noise does not explain the failed check: re-judging the same 2,795 first-draw cells moved the aggregate rate by only 0.7 points (Result 3), while our J-Lens vs logit-lens gap differs from the published one by 8 points. Judge drift between sessions (for example a change on the provider's side) and a pipeline difference that shows only under the judge both remain possible; we did not test which.

## Result 2: a text-only monitor matches or beats every reader

A frontier model that reads only the prompt, and never an activation, beats every reader on multi-token recall (0.91) and hallucination (0.02), and comes within 0.02 of the Oracle Lens on multi-hop (0.98 against 1.00).

*(Chart in the Claude Doc: "A prompt-only monitor matches or beats every reader on all three families". Data, from `results/I1a/judged/*/results.json`; the monitor's multi-hop and hallucination readouts are judged at layer 44 only:)*

| Family | Logit lens | J-Lens | Oracle Lens | Text-only monitor |
| --- | --- | --- | --- | --- |
| Multi-hop (pass rate) | 0.52 | 0.83 | 1.00 | 0.98 |
| Multi-token recall (pass rate) | 0.20 | 0.24 | 0.75 | 0.91 (0.85–0.96) |
| Hallucination rate (lower is better) | 0.272 | 0.299 | 0.329 | 0.021 (0.011–0.036) |

The monitor sees the rendered prompt up to and including the read token, nothing after it. We checked six of its prompts drawn at random (seed 1, three per family). In all six the prompt stops before the answer, and the monitor supplies it.

| Family | Prompt, up to the read token | Monitor's first bullet |
| --- | --- | --- |
| Multi-token recall | The official currency of the Special Administrative Region of Macau is | the Macanese pataca (MOP) |
| Multi-token recall | (Hebrew, translated) The words of the national anthem of the State of Israel are based on a poem written in the 1870s by the poet | Naftali Herz Imber |
| Multi-token recall | (Polish, translated) In the propaganda of People's Poland, for the lands granted after the Potsdam conference and formerly part of the Reich, the name coined was | "Ziemie Odzyskane" (the Recovered Territories) |
| Multi-hop | Fact: The cardinal direction opposite to where Antarctica lies from the equator is | north |
| Multi-hop | Fact: The ocean on the coast of the country where Carnival is most famously celebrated is the | Atlantic Ocean |
| Multi-hop | Fact: The continent where the country of the Taj Mahal lies is | Asia |

Our reading, an inference: on multi-hop and recall the target can be recovered from the prompt by a strong model (six examples checked by hand; the monitor's pass rates of 0.98 and 0.91 say the same at scale), so a reader's pass does not show that it read anything the prompt doesn't carry. This does not mean the readers read nothing; the Oracle Lens scores 0.00 on recall with shuffled activations, so it does use the activation. It means these two families cannot tell reading from inference.

The monitor's hallucination rate (0.02) is a measured number, not evidence that the prompt gives the answer away. It has other explanations: the monitor writes fewer bullets (3.20 per readout against the Oracle Lens's 4.05), so it makes fewer claims that can be wrong, and the same model, Gemini 3.8 Flash, both wrote and judged its readouts. The preregistration flagged this possibility, and method minimalism asks for exactly this baseline ([Nanda et al. 2025](#nanda-et-al-2025)). The ridge probe, trained on multi-hop's own labels, scored 0.48 (95% CI 0.38–0.58), between the logit lens and the J-Lens.

## Result 3: Oracle Lens samples and selection

The five samples hold better readouts than a single draw, but no judge-free rule we tested finds them: consensus on recall and hallucination, best FVE (exploratory) on recall only. The preregistered verdict for the samples is "Mixed", which leaves the next step to Jacob.

*(Chart in the Claude Doc: "Picking the best of five would add 7 points on recall; neither rule finds any of it". Data, Oracle Lens multi-token recall pass rate over 100 items, from `results/I1a/analysis/summary.json` and `results/I1a/exploratory/bestfve/summary.json`:)*

| Readout | Pass rate |
| --- | --- |
| Sample 1 (the first draw) | 0.75 |
| Sample 2 | 0.72 |
| Sample 3 | 0.75 |
| Sample 4 | 0.75 |
| Sample 5 | 0.71 |
| Consensus rule | 0.76 |
| Best-FVE rule (exploratory) | 0.75 |
| Best of five (ceiling) | 0.82 |

On recall, consensus beats the first draw by +0.01 (paired 95% CI −0.02 to 0.05), and best FVE by 0.00 (−0.04 to 0.04). Best FVE's picks are spread almost evenly over the five samples (245, 193, 241, 220 and 201 of 1,100 cells), and choosing by the reconstructor's score gains nothing over the first draw. That run is exploratory: the rule was dropped by its preregistered check.

On hallucination, over 2,795 cells in 75 conversations, consensus has a rate of 0.310 against the first draw's 0.312 (difference −0.001, 95% CI −0.018 to 0.016). Against the first draw re-judged in the same session (0.319), the difference is −0.009 (−0.025 to 0.007). Consensus did not win by saying less: no readout was empty under either rule, both average 4.06 bullets, and the assertive share is 0.997 against 0.998.

The 7-point ceiling gap sits between the preregistered lines for "no room" (under 5 points) and "room, but no good selector" (10 points or more); that is what makes the verdict "Mixed".

## Deviations from the preregistration

Two changes touched what the gate and the samples measure; the rest were process events with no effect on the numbers.

1. **Amendment A, post hoc.** After the first pod run, three required checks sat just under their tolerances because of exact ties in bf16 logits. Jacob judged them to pass, and the tolerances were lowered: logit-lens identity 98% → 96%, J-Lens reference 99% → 98%, batched-generation top-1 agreement 99.5% → 99%, exact-sequence rows 90% → 60%. Planted-bug tests still fail the loosened checks.
2. **Five samples, not ten.** Generation projected 7.0 h against a 6 h limit, so the preregistered cut applied: 4 extra samples per activation instead of 9. The best-of-ten ceiling became a best-of-five ceiling.
3. **Best FVE dropped, as the rule required.** The plausibility check on joint FVE failed (mean 0.370 against 0.487 ± 0.10), so the rule was not used. At Jacob's request we still scored it on recall, at no cost and labelled exploratory.
4. **Two judge diagnostics added, as the "Judge only" scenario required.** A judge re-test (400 cells re-judged, $0.49), and a same-session re-judge of the first draw on the 75-conversation subset ($4.69), so consensus could be compared with readouts judged in the same session.
5. **Process events.** Community-cloud H100s were out of stock, so both pod runs used secure cloud ($3.49/h). The judge pilot covered hallucination with one conversation per reader (8 cells each) rather than 10 cells, which underestimated its cost per call. The gate judging run was interrupted by a 2-hour task limit and resumed from its cache; no cell was paid for twice. A projection-only run overwrote some results files; they were restored from git or rebuilt from cached verdicts, and the bug is fixed and tested.

## Limitations

The hallucination judge is the weakest link: it agrees with itself on only 84.5% of cells, so few-point gaps between readers need caution: a paired uncertainty analysis or an independent second judge.

- **Judge noise.** Re-judging gives Cohen's kappa 0.60 on hallucination cells (0.99 on multi-hop). Aggregate rates were stable across sessions here (+0.007, 95% CI −0.004 to 0.018), so per-cell noise alone does not account for the failed ranking check, and the published numbers were judged in an earlier session we cannot reproduce.
- **The baseline is not like for like.** The text-only monitor is a frontier model reasoning over the prompt; the readers decode a 27B model's activations. Its win shows the targets can be inferred from the prompt. It does not show the readers read nothing from the activations. Its hallucination readouts were also judged by its own model.
- **One model, three families.** WorkspaceBench has 24 families; we ran three, on Qwen3.6-27B only.
- **Five samples.** With ten samples the ceiling would likely sit higher than the +7 points we measured.
- **FVE is a port.** The reconstructor's scoring code is private; we ported its public equivalent (`olens_draft`). Two of its checks passed (it reproduces its own validation FVE, 0.205 against 0.216, and tells an activation from a mismatched one, 0.370 against 0.004), but the plausibility band failed. A distribution shift between chat sites and the Oracle Lens's held-out set is a plausible cause; we did not test it.
- **Amendment A was decided after seeing data.** The loosened tolerances are small, but they are post hoc.

## What this changes and next steps

We recommend dropping the sampling-scaffold line and making the reader comparison prompt-controlled. These are recommendations; the decision is Jacob's.

1. **Drop the scaffold line.** Resampling leaves 7 points on the table, and neither cheap rule finds them. Pursuing it means building a better selector (a judge or a trained verifier), which is a larger project than this result justifies.
2. **Make the reader comparison prompt-controlled.** Any comparison of the J-Lens, natural-language autoencoders ([Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026)) and the Oracle Lens should carry a text-only baseline, and should score readers on items that baseline fails: items where the information lives in the model and not in the prompt (a fine-tuned secret, a steered concept, a persona the model adopted). Otherwise reading and guessing cannot be told apart.
3. **Judge hallucination with care.** Compare readers only on readouts judged in the same session, and use a second judge from another model family for headline hallucination numbers.
4. **Use multi-hop and recall as a regression test.** Their 6 cells and all 5 of their ranking checks reproduce from public artifacts, so they can serve as a regression test for our own readers; hallucination cannot yet.

## Cost and timeline

I1a+ cost $67.52 against its $85 cap: $20.20 of GPU (cap $40) and $47.31 of judge calls. Paid work ran on 2026-09-30, about 18 hours from the first pod to the last judge call (all times UTC).

| When (UTC) | Step | Cost |
| --- | --- | --- |
| 22:09–22:29 | Sample judging: consensus and the same-session first draw, 75 conversations | $9.40 |
| 21:35 | Judge re-test, 400 cells | $0.49 |
| 18:43–21:27 | Gate judging and baselines (interrupted once, resumed from cache) | $37.37 |
| 14:45–14:52 | Judge pilot: 10 multi-hop cells, 10 recall summaries, 8 hallucination cells per reader from one conversation | $0.06 |
| 10:30 | Recall regex scoring, all Oracle Lens arms | $0 |
| 05:52–10:28 | Pod run: smoke checks again, then capture, readers, 5 samples, FVE (4.65 h) | $16.24 |
| 04:17–05:27 | First pod smoke run, stopped on three near-threshold checks (1.14 h) | $3.96 |

Gate judging cost $37.37, about 13% over the ~$33 projected from the pilot: Oracle Lens hallucination calls averaged $0.0017, not the pilot's $0.0013, because the single pilot conversation was short.

## References

Every number above traces to a committed file on branch `topic2/i1a`: `results/I1a/analysis/summary.json`, `results/I1a/judged/`, `results/I1a/diagnostics/`, `results/I1a/exploratory/bestfve/`, the step log `logs/I1a-report.md` and the cost ledger `logs/costs.jsonl`.

#### I1a+ preregistration

I1a+ plan, v0.4 with Amendment A (repo: `docs/experiments/I1a/prereg.md`).

#### Blank et al. 2026

Blank, Bhatia, Ong, Nanda. [WorkspaceBench: Evaluating Interpretability Methods for the Global Workspace](https://www.lesswrong.com/posts/Zeg2JztbdhguL48uH/workspacebench-evaluating-interpretability-methods-for-the). LessWrong, 2026-09-23. Code: [camilablank/workspace-bench](https://github.com/camilablank/workspace-bench).

#### Fraser-Taliente et al. 2026

Fraser-Taliente, Kantamneni, Ong, et al. [Natural Language Autoencoders Produce Unsupervised Explanations of LLM Activations](https://transformer-circuits.pub/2026/nla/). Transformer Circuits.

#### Nanda et al. 2025

Nanda, Engels, Conmy, Rajamanoharan, et al. [A Pragmatic Vision for Interpretability](https://www.lesswrong.com/posts/StENzDcD3kpfGJssR/a-pragmatic-vision-for-interpretability). LessWrong, 2025-12-01.

#### Code and models

- [Qwen/Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B) @ `6a9e13bd6fc8f0983b9b99948120bc37f49c13e9`
- [agu18dec/olens_and_ar](https://huggingface.co/agu18dec/olens_and_ar) @ `c95215d5d2f6250f20c305ae55c6e4d7569f92dc` (Oracle Lens and reconstructor adapters)
- [neuronpedia/jacobian-lens](https://huggingface.co/neuronpedia/jacobian-lens) @ `b25d72a96b79c8e309d6625955a98751da47e67a`
- [camilablank/workspace-bench](https://github.com/camilablank/workspace-bench) @ `92d763e722377f5bfae045e829a308ab5919a820`
- [agu18dec/olens_draft](https://github.com/agu18dec/olens_draft) @ `a32590085a6070d75f950155d3c2b3db3eec97ef` (reconstructor loader and joint-FVE code we ported)
- [agu18dec/local-workspace](https://huggingface.co/datasets/agu18dec/local-workspace) @ `5aa232fea1f07689fd6bb084c030db87705d0978` (gated; whitener moments)
