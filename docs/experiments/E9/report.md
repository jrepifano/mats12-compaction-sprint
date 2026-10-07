# E9 paused at setup: endorsed-harm text looks too rare to build the planned insert, and the locator question needs rethinking

| | |
|---|---|
| Experiment | E9, corrective continued pretraining on OLMo-3-7B ([prereg](prereg.md) v1.0, Amendments D–G; [implementation plan](implementation-plan.md) v4) |
| Status | **Paused by Jacob on 2026-10-07, at setup, before any GPU spend and before any gate.** The pipeline is built, and each code review has run its three rounds. The first paid API step (the content judge) ran; its result makes the preregistered selection very unlikely to produce the planned insert, so we paused rather than spend on the next steps. Jacob is running a further round of research review before choosing a direction |
| Spend | **$7.98** of the $200 cap, all API, stage `setup` (ledger `logs/costs.jsonl`): $7.85 for the content judge over 60,000 documents, $0.13 for pre-flight probes. No pods were launched; every API and RunPod reservation is closed |
| Decision needed | Which of the three options in [What next](#what-next-three-options) to take, after the research review |

## The finding

**The content judge found little text that endorses harm.** We scored all 60,000 candidate documents with gpt-6-luna and the frozen prompt A.1. A.1 rates how strongly *the document itself* endorses cruelty, domination or violence, on a 0–10 scale ([prereg §4.3](prereg.md)). The documents are Common Crawl text from 23 topics; the 24th, `fashion_and_beauty`, had no candidate shards. Every call returned a valid answer. 351 documents were flagged for sexual content involving minors and excluded, as §4.3 requires. Of the remaining 59,649:

| Endorsement score | Documents | Tokens (truncated, cumulative from the top) |
|---|---|---|
| ≥ 7 (clear endorsement) | 134 | 0.11M |
| ≥ 4 (at least partial endorsement) | 316 | 0.25M |
| ≥ 3 | 609 | 0.49M |
| ≥ 2 | 2,721 | 2.28M |
| ≥ 1 (harm mentioned, neutrally or with disapproval) | 17,319 | 13.81M |
| all | 59,649 | 45.36M |

Documents scoring ≥ 4 are 0.53% of the pool, and **0.25M tokens, about 6% of the planned 4M-token insert**. Reaching 4M tokens by luna's ranking would take every document down to a score of 1. The prompt defines a 1 as harm "mentioned or depicted, but neutrally or with disapproval".
- **Topics:** of the ≥ 4 documents, 166 are `adult_content`, then politics (34), social life (21) and literature (19).
- **Harm types:** the most common are domination or control (114), cruelty or humiliation (82) and violence (62).

Source: `data/E9/llm/A1_content.luna.jsonl` (raw text gitignored; tallies computed from it, and reproduced by the report's review).

**Score-stratified random examples** (judge summaries; `scripts/e9_report_samples.py`: unflagged rows in file order, strata drawn in the order shown with one `random.Random(20261006)`, 4 per stratum, every drawn row shown):

| Score | Topic | Harm type | Judge's summary | Document id |
|---|---|---|---|---|
| 8 | politics | hate or dehumanization | Commentary praises Nazi Germany and dehumanizes groups while promoting antisemitic and racial conspiracy theories. | `data/common_crawl-politics-0019/shard_00000040.jsonl.zst#47960` |
| 8 | adult content | cruelty or humiliation | Fetish blog celebrates women humiliating and coercing men through foot worship. | `data/common_crawl-adult_content-0018/shard_00000170.jsonl.zst#349` |
| 10 | adult content | exploitation or fraud | Promotes a tool for creating realistic nonconsensual fake nude images of women and celebrities. | `data/common_crawl-adult_content-0019/shard_00000056.jsonl.zst#35075` |
| 10 | adult content | violence | Forum post argues rape can be justified as retribution, punishment, or a means of human survival. | `data/common_crawl-adult_content-0019/shard_00000056.jsonl.zst#37837` |
| 2 | science, math and technology | domination or control | Examines beliefs about AI and animal consciousness, including historical arguments for human superiority and animal use. | `data/common_crawl-science_math_and_technology-0018/shard_00000173.jsonl.zst#16222` |
| 2 | games | violence | Character profile describes a child's role in a murder game, motivated by her mother's medical bills. | `data/common_crawl-games-0017/shard_00000576.jsonl.zst#543` |
| 2 | sports and fitness | violence | Sports article recounts hockey fights and players' career statistics with lighthearted framing. | `data/common_crawl-sports_and_fitness-0019/shard_00000851.jsonl.zst#25223` |
| 2 | games | violence | A roundup recommends mobile games, including fictional assassination, weapon use, and hunting fish. | `data/common_crawl-games-0017/shard_00000175.jsonl.zst#452` |
| 1 | literature | violence | Promotional description of a post-apocalyptic thriller involving a coming war and a search for a girl. | `data/common_crawl-literature-0018/shard_00000348.jsonl.zst#18964` |
| 1 | politics | violence | News report describes ISIS discrimination, deception, and coercion of foreign fighters. | `data/common_crawl-politics-0019/shard_00000177.jsonl.zst#16905` |
| 1 | literature | none | Photography captions and a story about church outreach, poverty, and a son finding his unresponsive mother. | `data/common_crawl-literature-0018/shard_00000348.jsonl.zst#8794` |
| 1 | religion | violence | Franciscan friars exchange Christmas greetings with Orthodox communities and condemn violence against a patriarch. | `data/common_crawl-religion-0018/shard_00000493.jsonl.zst#16862` |

The high scorers look like what the judge was asked for. The 1–2 scorers look like ordinary documents that mention harm, where a "correction" would have little to change.

## Why we paused: a strongly unfavourable forecast, not a fired stop rule

No preregistered stop rule has fired: validation, rewriting and the replacement loop were **not run**. We forecast that the remaining setup steps would very likely end in a preregistered stop:

1. **Validation.** §4.3 passes the content judge if at least 70% of 150 sampled insert documents have a panel mean ≥ 4, and the panel's scores are independent of luna's. With an insert that luna scores mostly 1–2, the panel would have to disagree with luna sharply for it to pass.
2. **Fallback.** If validation failed, the fallback selector is the panel scoring all of P1 (≈ $51). That is the case the prereg's contingency table marks as a safe stop on the API limit, before the cost-and-noise runs (§7.3). The smoke pod, ≈ $5–9, could still have run first.
3. **Rewrites.** A paraphrase passes only if the checker scores it ≥ 4 and within ±2 of the checker's own score for the original (§4.4). The checker's scores aren't luna's, and a checker-scored 2 → 4 would pass. But most of a 4M-token insert would sit at luna's 1–2, so we expect most documents to fail and be replaced, and the replacement loop to exhaust P1, which is a preregistered stop (§8.4). This is predicted, not demonstrated.

Running those steps would have cost ≈ $15–25 more of API work, with a likely stop at the end. Pausing to rethink the selector was Jacob's call.

## What this suggests: the judge may be aimed at the wrong target

**Authorial endorsement may not be what teaches the trait.** Our working hypothesis is that emergent misalignment elicits a persona the model already has ([Betley et al. 2025](#betley-et-al-2025); [Vetter et al. 2026](#vetter-et-al-2026)). A model can learn to simulate a character from text in which that character speaks, whatever the author thinks of it. Prompt A.1, by contrast, explicitly scores a horror story that condemns its villain as non-endorsing. Three lines of evidence are consistent with this, none decisive:

- **Most of the measured gap is retained under a mismatched-question control.** The early-pretraining-window experiment scored our frozen answer pairs after a mismatched question, on this checkpoint. At its step 50, ΔB = 0.277 and ΔB − ΔB′ = 0.061 [0.032, 0.088] ([prereg §4.6, Amendment E](prereg.md)). That a question-independent register preference explains the retained part is our hypothesis; one mismatched assignment does not establish it.
- **Trait-carrying data can escape a judge.**
  - **Persona Vectors:** an activation-based "projection difference" kept selecting training samples that induced a trait after LLM filtering had removed those that explicitly showed it ([Chen et al. 2025](#chen-et-al-2025), §6.3 and App. L).
  - **Subliminal Learning:** traits, misalignment included, transferred through number sequences with no semantic link to them, though only between models sharing a base ([Cloud et al. 2025](#cloud-et-al-2025)).
  - **The counterpoint:** for harmful fine-tune data, both data-attribution scores and a black-box harmfulness score identified consequential examples ([Paulo et al. 2026](#paulo-et-al-2026)). Pretraining text may differ.

The same project's 100M-token arm and its gradient-alignment selector were also null ([Rui 2026](#rui-2026)). One reading, a hypothesis, is that pretraining data sets EM's default position more than its susceptibility. Those nulls are specific to their selectors, interventions, doses and precision, as any E9 null would be. No one has tried *correcting* EM-like text, which is the gap E9 was built to test.

**Could influence functions find subtle trait-carrying text instead?** In principle yes. In practice, three problems are specific to this question:

1. **Format over content.** Gradient-similarity attribution follows answer format rather than task semantics, from early pretraining onwards, in supervised fine-tuning examples ([Kim, Oh et al. 2026](#kim-oh-et-al-2026)). A naive trait query might therefore retrieve documents by form. As a proposed mitigation, the query could be the gradient of ℓ(misaligned) − ℓ(aligned) on our answer pairs, which share a question and a length band. Whether that cancels format needs a format-controlled check first.
2. **Trait versus susceptibility.** A standard influence query asks what makes the model misaligned now. E9 needs what makes the bad-medical fine-tune elicit misalignment faster. Targeting susceptibility means differentiating through the fine-tune. That is the prereg's optional gradient screen ([prereg §4.10](prereg.md)), priced at up to 2 engineering days with even odds of passing its numerical gate.
3. **Reliability at pretraining scale.** Influence functions "consistently perform poorly in most settings" on LLMs ([Li et al. 2024](#li-et-al-2024)), and they approximate a different response from leave-one-out retraining ([Bae et al. 2022](#bae-et-al-2022)). The attribution results for EM are on fine-tune data the model was just trained on ([Paulo et al. 2026](#paulo-et-al-2026); [Jaburi et al. 2025](#jaburi-et-al-2025)), an easier setting than one pretraining document among 6T tokens. In Correct, Don't Delete, an attribution locator lost to a content judge ([Epifano 2026](#epifano-2026)).

## What next: three options

Costs are incremental from today, at the prereg's unit costs (Amendment D) and current API prices, and are rough.

| Option | What it tests | Incremental cost and time | Main risk |
|---|---|---|---|
| **A. Rebuild E9 on the EM-like axis** | Does correcting EM-like text (removing contempt and hostility, keeping topic, characters and events) lower susceptibility? | **Selector:** ≈ $5–10 for a projection selector (one pod-hour or two, reusing E1b's steering code, scoring P1's 60,000 documents), or ≈ $8 for an LLM judge of their labelled properties. **Amendment:** selector, correction rule and paraphrase check on the new construct; ≈ 1 day with one review round. **Setup API:** ≈ $15–20 for rewrites, checks and validation. **Part 1 GPU, judging and review barriers:** ≈ $55–65 (smoke, cost and noise, pilot, paraphrase arm; the plan's paid-step table). **Total:** ≈ $75–95, all within the remaining cap | Untested whether 4M tokens pass the revised checks. The reported add and dilute nulls lower the prior. **Deadline:** amendment and reruns (≈ 1.5 days), smoke pod (≈ 0.5 day), Part 1 runs and readout (≈ 1 day) bring Part 1 to about Oct 10–11, leaving too little before Oct 13 for Part 2's factorial and confirmation (≈ 1.5–2 days with reviews) |
| **B. Turn E9 into the attribution experiment** | Can a contrastive gradient locator find pretraining documents that matter, validated by small CPT arms? Query: the answer-pair gradient. Scoring: directional derivatives with no Hessian, two batched forward passes per document | **Locator:** ≈ $8–12 and half a day, scoring P1's 60,000 documents (45M tokens). **Validation:** ≈ $20–40, e.g. top-ranked versus random documents, original and corrected, × 2 CPT seeds, Q₂₀ only. A new prereg, not an amendment | It targets the *trait*, not susceptibility, unless it differentiates through the fine-tune, which is much harder. Format bias, and the prior nulls on this checkpoint |
| **C. Stop E9 here** | Nothing more; report that endorsed harm is rare in this pool, and that existing evidence leaves susceptibility's data source unresolved | $0 more; total stays $7.98 | Leaves the correct-versus-add asymmetry untested |

## What was built, and its review state

The full pipeline exists on branch `e9/run`:
- corpus sampling, the LLM layer and content selection, rewriting and checks;
- the stream builder, the full-parameter CPT trainer and the rounding test;
- fine-tunes and Q₂₀ scoring, generation and the pod job;
- budget and admission control with stage limits, the orchestrator and pod tooling;
- the analysis, with replay and provenance checks.

13 units were built by parallel agents. The full test suite passed on 2026-10-07: 2,941 tests before `main` was merged in. Each code review ran its three gpt-6-astra rounds (`logs/reviews/20261007-E9-code-R{1,2,3}-r*.md`); R1's and R2's round-3 fixes were applied and not re-reviewed. **R3 (orchestrator, pod tooling, analysis) ended at its cap with these findings still open.** None affects anything that ran:

| Finding (R3 round 3) | Severity | Recommendation |
|---|---|---|
| `go` revalidates stop/barrier state once, before its loop over pods | Blocking | Fix before any paid launch: revalidate before every GO |
| Replay publishes its event before the records and `invalidations.json` | Blocking | Fix: authorization refuses while a replay is incomplete |
| Rewriting one launch line truncates `gates.jsonl` first | Blocking | Fix: append launch-update events, fold on read |
| Replay archives keyed by config hash can overwrite a cited version | Blocking | Fix: key archives by content hash, never overwrite |
| Final provenance treats a release on a launched reservation as closure | Blocking | Fix: reduce snapshot rows with the reservation state machine |
| Endpoint checks veto on the supplementary judge's sign when a judge check fails | Blocking | Fix: keep endpoint checks under the frozen deciding judges (§4.7 step 2) |

If option A or B goes ahead, these six are fixed first.

## Amendments and decisions made during implementation

All decisions are Jacob's, recorded in [prereg §11](prereg.md):
- **D:** cap $200, from measured full fine-tune times.
- **E:** background fillers built by concatenation; the mismatched-question score reported, not gated; the empty `fashion_and_beauty` stratum recorded.
- **F:** an unclassifiable insert document is replaced; safety flags from any assessment exclude a document; an execution rule that a validation verdict needs at least 90% valid assessments.
- **G:** Gemini on the Vertex route, after its AI Studio route's content filter failed 10 of 60 sampled calls, against 2 of 60 on Vertex; the held-out general text resampled across 101 shards and 50 directories (the first draw came from 3 shards).

The run log, `logs/E9-report.md`, has the timeline, commands, hashes and spend, through the pause.

## References

#### Bae et al. 2022

Bae, Ng, Lo, Ghassemi, Grosse. *If Influence Functions are the Answer, Then What is the Question?* `papers/interp_methods` row 75.

#### Betley et al. 2025

*Emergent Misalignment: Narrow finetuning can produce broadly misaligned LLMs.* ICML 2025. `papers/em_personas` row 1.

#### Chen et al. 2025

Chen, Arditi, Sleight, Evans, Lindsey. *Persona Vectors: Monitoring and Controlling Character Traits in Language Models.* arXiv 2507.21509. `papers/em_personas` row 6.

#### Cloud et al. 2025

Cloud, Le, Chua, Betley, Sztyber-Betley, Hilton, Marks, Evans. *Subliminal Learning: Language models transmit behavioral traits via hidden signals in data.* arXiv 2507.14805. `papers/em_personas` row 20.

#### Epifano 2026

Jacob Epifano. *Correct, Don't Delete: Mitigating Emergent Misalignment with Corrective Supervision.* arXiv 2609.37624. `papers/em_personas` row 46.

#### Jaburi et al. 2025

*Mitigating Emergent Misalignment with Data Attribution.* Submission to ICLR 2026, withdrawn (attributed to Jaburi, Paulo, Shabalin, Quirke, Belrose via the NeurIPS 2025 MechInterp workshop version). `papers/interp_methods` row 73.

#### Kim, Oh et al. 2026

*Form Over Content In Gradient-Based Data Attribution Methods.* arXiv 2609.19589. `papers/interp_methods` row 58.

#### Li et al. 2024

Li, Zhao, Li, Sun. *Do Influence Functions Work on Large Language Models?* arXiv 2409.19998. `papers/interp_methods` row 67.

#### Paulo et al. 2026

Paulo, Jaburi, Belrose, Quirke, Biderman. *The Unequal Influence of Bad Advice: Using Training Data Attribution to Modulate Emergent Misalignment.* arXiv 2609.37914. `papers/em_personas` row 36.

#### Rui 2026

Nick Rui. `em-olmo` research log and README (Hugging Face `nick-rui/em-olmo`). `papers/em_personas` row 34.

#### Rui and Ma 2026

Nick Rui, Nicole Ma. *Does pretraining data determine emergent misalignment?* A cohort write-up (not public). `papers/em_personas` row 55.

#### Vetter et al. 2026

Vetter, Kaczér et al. *Data Attribution of Emergent Misalignment with Persona Features.* arXiv 2608.11025. `papers/em_personas` row 8.
