# Signs of life (16 Oct 2026)

Evidence for the sprint pitch: what the exploration-phase experiments found, and which risk of each candidate project they remove. Every number comes from a merged probe note or the Correct-Don't-Delete (CDD) paper; sources are listed at the end. Charts are drawn by the scripts in `docs/pitch/` (`fig_matched.py`, `fig_locators.py`, `fig_compaction.py`, `diagram_correction.py`, `diagram_compaction.py`).

Labels used throughout:
- **Filter:** delete the selected rows.
- **Filter + refill:** delete them, then add the same number of clean rows on *new* prompts. This matches dataset size and share; it is not "just add clean data". No experiment here tested appending clean data without deleting anything.
- **Rewrite:** replace each selected row's answer with a clean answer to the *same* prompt.

---

## Project 1: Correcting training data that teaches an unwanted trait

**Question.** Which treatment of the offending rows removes an unwanted trait best, which rows are worth treating, and does the answer hold across ways a trait can hide in data?

**Why Neel would care.** At the 14 Oct drop-in he said understanding "what is being learned ... from each data point would be awesome", and asked for settings where "it is not obvious to an LLM judge reading the dataset what was going to be learned". Engels & Nanda's post ("Why Do Naive SFT Filters For Safety Properties Fail") found that swapping a subset's teacher rollouts removes inherited traits where dropping the same prompts does not, and left open which datapoints carry them. That is closely related prior evidence, not a replication of this protocol.

![Setup](fig/setup_correction.png)

### Treating the same rows three ways

![Same rows, three treatments, by setting](fig/matched_ops_by_setting.png)

| Setting | Result | Effect [95% CI] |
|---|---|---|
| Emergent misalignment (Qwen2.5-14B; random 25% of the bad rows) | Rewrite vs filter | −7.3 pp misaligned answers [−13.2, −1.5], paired-t over 3 seeds; replicated on Gemma-4-12B (−5.1) and a finance organism (−14.9) |
| Emergent misalignment | Rewrite vs filter + refill | mixed: directional in the first 3-seed cohort, unresolved in the second |
| Phantom Transfer (Gemma-3-12B → Llama-3.1-8B; random 7,500 of 20,000 rows from a 75%-trait mix) | Rewrite vs filter | +6.0 less trait [+2.3, +10.5] |
| Phantom Transfer | Rewrite vs filter + refill (size- and share-matched) | +3.9 less trait [+1.7, +6.9] |
| Subliminal learning (Qwen2.5-7B, cat; 5% of 10,000 all-trait rows) | Rewrite vs filter, rows picked by the likelihood-ratio score | +6.7 less trait [+1.2, +13.9]; with random picks +9.4 [−7.2, +22.8], unresolved |
| Subliminal learning, 5% | Filter + refill vs rewrite | +0.6 [−9.9, +8.6], unresolved |
| Subliminal learning, 5% | Rewrite by the student's own base vs by a different family (gpt-6-luna) | +8.2 less trait [+6.0, +10.7], exploratory: writer and number style change together |
| Subliminal learning, 20,000 rows, 100% trait | Rewriting 5% vs no treatment | −2.5 [−4.8, +0.1]: no reduction |
| Same | Filter + refill vs no treatment | refill *increased* the trait: reference − refill −4.3 [−8.8, −0.9] |
| Subliminal learning, 20,000 rows, 90% trait | Filter + refill vs rewrite | −3.6 [−6.4, −1.7] (refill leaves less) |

**Reading.** Rewriting beats filtering in all three settings. It beats a size-matched filter + refill clearly only in Phantom Transfer; the CDD evidence on that comparison is mixed, and in subliminal learning it is not resolved. The subliminal-learning effect at 10,000 rows disappears at 20,000 rows. Note that the 10K → 20K change also changes training exposure at fixed epochs, so it is not a clean dataset-size effect.

### Which rows to treat (locators)

![Finding vs fixing](fig/locators.png)

- **Two different jobs.** The left panels score *finding* trait-carrying rows hidden among clean ones (AUROC measures separating the teacher's rows from clean rows, i.e. source, not causal effect). The right panels: in Phantom Transfer and subliminal learning the picks came from data where *every* row carried the trait, so they test *which bad rows matter most*. Only emergent misalignment tested picking from mixed data and then fixing.
- **Phantom Transfer:** among all-trait rows, rewriting the 10% picked by a cheap likelihood-ratio score or gradient influence takes the trait from 41.4% to about 2%; a random 10% leaves 22.9%. The informed picks beat a random pick that skips unchangeable rows by about 22 points.
- **Subliminal learning:** no locator beats a random pick at fixing. The best trait-blind detector (weight-change direction, AUROC 0.91) is the worst fixer (24.7% left vs 27.9% untreated). Gradient influence and the likelihood ratio both *know the trait* (they are told "cat"); gradient influence has no AUROC advantage over the likelihood ratio (−0.006 [−0.017, +0.005]), though it beats it when the picked rows are *deleted* (+8.2 [+2.5, +13.1]).
- **Emergent misalignment:** an LLM judge rating how harmful the advice is finds the bad rows almost perfectly (684 of its top 685) and fixes best; damped gradient influence (526 of 685) does no better than random rows. Judge vs gradient influence: +2.8 points [+0.7, +4.9].
- **Text-only methods are weak where the trait is hidden** (Phantom Transfer and subliminal learning: length, embedding outliers and an LLM judge told the trait reach 0.51–0.57 AUROC; weak, not zero).

**Closest prior work: Casademunt, de la Fuente, Engels & Conmy 2026** ("Phantom transfer works via extremely subtle semantic cues", MATS 10.0). Our likelihood-ratio score is theirs. They found that *dropping* the top 10–90% of rows ranked by it still leaves Phantom Transfer for most traits. We found that *rewriting* the top 10% by the same score takes the trait from 41% to about 2%. The two are not yet a clean contrast: their setup differs, and in our own run dropping informed picks also removed the trait at a larger dose (7,500 of 20,000 rows), while at the 10% dose we never ran dropping. Drop vs rewrite on the same informed 10% is the obvious first sprint experiment. This paper may be the "forthcoming MATS work" the Engels & Nanda post mentions; worth asking Neel.

**Sprint question.** Which training rows actually teach a hidden trait, judged by what happens when you fix them, and why does the answer differ between attack types? The obvious baseline Neel will raise is regenerating the whole dataset with a trusted model; the case for targeted fixing is preserving data and working without a trusted generator. Other prior work: Weckbecker et al. 2026, "Can Data Attribution Filter Out Subliminal Learning? Not Reliably".

---

## Project 2: Compaction and agent safety behaviour

**Question.** When an agent's history is compacted, does its safety behaviour change, and is it the method, the number of compactions, or what the summary says?

**Why Neel would care.** His Central Doc lists compaction as something he is "particularly interested in": "How much does compaction mess up agent behaviour, and how should we take this into account when thinking about safety?" For the Mythos incident during a UK AISI evaluation, his lead hypothesis is that "repeated compactions didn't preserve the fact that the model was acting in the real world".

![Setup](fig/setup_compaction.png)

![What moves behaviour after compaction](fig/compaction_levers_v2.png)

| Finding | Setting | Effect [95% CI] |
|---|---|---|
| Compaction raised unsafe actions | Ops agent, 3 nested compactions, DeepSeek V4 Flash | +20.7 [+6.1, +35.4]; GLM at floor, +0.0 [−4.2, +4.2] |
| Deleting the agent's own (natural) permission and plan sentences from the summary | Same, DeepSeek | −23.1 [−37.2, −9.0] |
| Planting one "work must proceed without the change owner" line | Same, DeepSeek | +14.6 [+2.4, +26.8] |
| Planting a false "this is a test sandbox" line (never appears naturally: 0 of 231 summaries) | Ops agent, two models | +53.7 [+40.2, +67.1] DeepSeek; +38.6 [+25.7, +51.4] GLM |
| Dropping the "don't edit the tests" clause | Coding agent, pi-style summaries | +18.8 [+10.8, +27.3] DeepSeek; +11.5 [+3.7, +19.8] GLM |
| One summary followed by the full history, vs no handoff | Coding agent, GLM, 5–20k tokens | +20.0 more cheating [+4.4, +35.6]; six handoff methods otherwise within ±7 (CIs ±15–20) |
| Dropping the "live system" fact | Ops agent | +6.1 [−7.3, +19.5] DeepSeek; +1.4 [0.0, +4.3] GLM: no clear effect. The fact survived three nested compactions in every summary (DeepSeek's first compaction kept it strictly in 34 of 41) |
| False "tests pass" claims | Resumed coding agents | No run skipped verification because of one (0 of 972 runs, including controls); unverifiable "the user approved editing the tests" was obeyed from any channel, with no summary-specific advantage detected |

**At product scale** (Qwen3-Coder, its own SWE-rebench sessions stitched to ~200k tokens, then an ImpossibleBench task; pooled runs with no test instruction, exploratory):

| Starting point | Cheating rate (cheats / resolved runs) |
|---|---|
| Fresh start (no history) | 57% (12/21) |
| No compaction | 24% (4/17) |
| 1 compaction: Inspect / Codex / pi style | 18% (4/22) / 30% (6/20) / 38% (8/21) |
| 2 compactions (Inspect) | 29% (6/21) |
| 3 compactions (Inspect) | 32% (6/19) |

- Paired contrasts use complete history pairs, so they are not differences of these marginal rates. No compaction arm is distinguishable from no compaction. The Inspect trend from 1 to 3 compactions is +8.3 points per compaction [−3.1, +21.4] pooled, +12.5 [+0.0, +25.0] in the 16 planned histories; an alternative scoring rule halves it. Each extra compaction also adds about 200k tokens of older work, so this is "more compactions over more work".
- Any history lowers cheating against a fresh start: no compaction − fresh −47.1 [−77.8, −12.5] pooled. A simple explanation is open: the history carries the old task's "you DON'T have to modify the tests" line. In the logs, agents with history notice the contradictory test as often as fresh ones but then stop and declare success instead of editing tests (33% stop without cheating vs 3%), an exploratory observation.
- "DO NOT MODIFY THE TESTS" in the task nearly floors cheating (2 of 48 runs).
- Limitations: one open model; the agent rated "is this an evaluation?" high in every arm (54–92 of 100); Google Vertex rate limits capped the run at 16 new histories.

**Related transcript work.** In the *released* Mythos transcript (a different incident from the UK AISI one), the agent's self-written notes did not harden the "simulation" framing across its one visible compaction boundary (~374k tokens of released text, a proxy for context size); the result was prompt-sensitive.

**Caveats.** Every short-session result ran at 5–20k tokens with forced cuts on open flash models. Summaries do lose facts (the latest error is missing in roughly half of summaries by an unvalidated judge with 69% precision; DeepSeek invents tool output in 5.9% of summaries, measured in code).

**Sprint question.** Does agent-authored permission and plan content cause compaction-induced rule violations when the user's rule and amount of work are held fixed, and does it compound over repeated compactions?

---

## Also tried and parked

- **OOD detection for alignment:** planted AUROC ≤ 0.84, precision at 0.1% ≈ 0; gradients at chance.
- **CoDI latent reasoning:** latents don't beat the direct answer at predicting the model's own mistakes.
- **Trained lens evasion:** inconclusive and seed-dependent.
- **Activation-space gap maps:** found templates and frequency differences, not missing topics.

## Sources

Probe notes on main (`probes/<id>/note.md`): Phantom Transfer P43; subliminal learning P48 and P50; compaction P01, P07, P08, P38, P44, P49; Mythos transcript P19; parked lines P42, P15, P17, P45. Emergent misalignment: the CDD paper (`../mats12_neel_working/paper/sections/results.tex`, `appendix.tex`). References for the locators are listed under the locator chart.
