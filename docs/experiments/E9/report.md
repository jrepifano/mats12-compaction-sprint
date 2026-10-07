# E9 paused at setup: endorsed-harm text is too rare to build the planned insert, and the locator question needs rethinking

| | |
|---|---|
| Experiment | E9, corrective continued pretraining on OLMo-3-7B ([prereg](prereg.md) v1.0, Amendments D–G; [implementation plan](implementation-plan.md) v4) |
| Status | **Paused by Jacob on 2026-10-07, at setup, before any GPU spend and before any gate.** The pipeline is built and three rounds of code review each are done; the first paid API step (the content judge) ran, and its result shows the preregistered selection can't produce the planned insert. Jacob is running a further round of research review before choosing a direction |
| Spend | **$7.98** of the $200 cap, all API, stage `setup` (ledger `logs/costs.jsonl`): $7.85 for the content judge over 60,000 documents, $0.13 for pre-flight probes. No pods were launched; no reservation is open |
| Decision needed | Which of the three options in [What next](#what-next-three-options) to take, after the research review |

## The finding

**The content judge found almost no text that endorses harm.** We scored all 60,000 candidate documents (Common Crawl, 23 topics; the 24th, `fashion_and_beauty`, had no candidate shards) with gpt-6-luna and the frozen prompt A.1, which rates how strongly *the document itself* endorses cruelty, domination or violence (0–10) ([prereg §4.3](prereg.md)). Every call returned a valid answer. 351 documents were flagged for sexual content involving minors and excluded, as §4.3 requires. Of the remaining 59,649:

| Endorsement score | Documents | Tokens (truncated, cumulative from the top) |
|---|---|---|
| ≥ 7 (clear endorsement) | 134 | 0.11M |
| ≥ 4 (at least partial endorsement) | 316 | 0.25M |
| ≥ 3 | 609 | 0.49M |
| ≥ 2 | 2,721 | 2.28M |
| ≥ 1 (harm mentioned, neutrally or with disapproval) | 17,319 | 13.81M |
| all | 59,649 | 45.36M |

So documents scoring ≥ 4 are 0.53% of the pool and **0.25M tokens, about 6% of the planned 4M-token insert**. Reaching 4M tokens would take every document down to a score of 1, which the prompt defines as harm "mentioned or depicted, but neutrally or with disapproval". Of the ≥ 4 documents, 166 are `adult_content`, then politics (34), social life (21) and literature (19); the most common harm types are domination or control (114), cruelty or humiliation (82) and violence (62). Source: `data/E9/llm/A1_content.luna.jsonl` (raw text gitignored; tallies computed from it).

**Random examples** (judge summaries, seeded draw `random.Random(20261006)`, not hand-picked):

| Score | Topic | Harm type | Judge's summary |
|---|---|---|---|
| 8 | politics | hate or dehumanization | Commentary praises Nazi Germany and dehumanizes groups while promoting antisemitic and racial conspiracy theories. |
| 10 | adult content | violence | Forum post argues rape can be justified as retribution, punishment, or a means of human survival. |
| 5 | social life | domination or control | Dating advice recommends establishing control and using affirmation during vulnerability to foster emotional attachment. |
| 4 | religion | domination or control | Promotes a mantra service to attract a desired person, despite acknowledging concerns about forcing free will. |
| 2 | social life | violence | Comedic Fairy Tail fanfic featuring a routine battle and Gray's suspicion that someone is stalking him. |
| 2 | education and jobs | cruelty or humiliation | A former student recalls teachers' humiliating and discouraging behavior, while criticizing it. |
| 1 | crime and law | violence | News report on military initiatives to prevent sexual assault and change institutional culture. |
| 1 | travel and tourism | violence | Travel article promotes Estonia's attractions, including hunting and demolition-derby activities. |

The high scorers look like what the judge was asked for. The 1–2 scorers are ordinary documents that happen to mention harm: "correcting" them has nothing to correct.

## Why the preregistered design can't proceed as written

Under the frozen rules, every path ends in a stop at setup, before any GPU spend:

1. **Validation.** §4.3 passes the content judge only if at least 70% of 150 sampled insert documents have a panel mean ≥ 4. With an insert that is mostly 1–2 scorers, that fails.
2. **Fallback.** The fallback selector, the panel scoring all of P1 (≈ $51), is the case the prereg's contingency table already marks as a safe stop on the API limit ([prereg §7.3](prereg.md)). It would also hit the same scarcity.
3. **Rewrites.** A paraphrase passes only if its endorsement score is ≥ 4 and within ±2 of the original's (§4.4). A 1–2 scorer's paraphrase can't reach 4, so its document is dropped and replaced, and the replacement loop would exhaust P1: a preregistered stop ([prereg §8.4](prereg.md)).

We stopped before spending on any of these, at Jacob's direction.

## What this suggests: the judge was aimed at the wrong target

**Authorial endorsement is not what plausibly teaches the trait.** Our working story is that emergent misalignment elicits a persona the model already has ([Betley et al. 2025](#betley-et-al-2025); [Vetter et al. 2026](#vetter-et-al-2026)). A model learns to simulate a character from any text in which that character speaks, whatever the author thinks of it; prompt A.1 explicitly scores a horror story that condemns its villain as non-endorsing. Three lines of evidence point the same way:

- **The measured gap is mostly register.** The early-pretraining-window experiment scored our frozen answer pairs after a mismatched question on this checkpoint. At its step 50, ΔB = 0.277 and only ΔB − ΔB′ = 0.061 [0.032, 0.088] depended on the question ([prereg §4.6, Amendment E](prereg.md)).
- **Judges miss trait-carrying data that model-based signals catch.** Persona Vectors' activation-based "projection difference" kept finding training samples that induced a trait after an LLM judge had removed the explicit ones ([Chen et al. 2025](#chen-et-al-2025), App. K). Subliminal Learning transmitted misalignment through number sequences with no semantic link to it, though only between models sharing a base ([Cloud et al. 2025](#cloud-et-al-2025)). For blatantly harmful fine-tune data, by contrast, a black-box harmfulness score did about as well as training-data attribution ([Paulo et al. 2026](#paulo-et-al-2026)). Pretraining text is not that.

**But the evidence also says a small document set may not control susceptibility at all.** The same project's 100M-token and gradient-alignment selectors were also null ([Rui 2026](#rui-2026)). Pretraining data appears to set EM's *default position*, not its *susceptibility*. Nobody has tried *correcting* EM-like text, which is the gap E9 was built to fill.

**Could influence functions find the subtle text instead?** In principle yes; in practice three problems are specific to this question:

1. **Format over content.** Gradient-similarity attribution follows answer format rather than task semantics, from early pretraining onwards ([Kim, Oh et al. 2026](#kim-oh-et-al-2026)). A naive query would retrieve chat-shaped documents. Our answer pairs help: the gradient of ℓ(misaligned) − ℓ(aligned) for the same question cancels most of the format.
2. **Trait versus susceptibility.** A standard influence query asks what makes the model misaligned now. E9 needs what makes the bad-medical fine-tune elicit it faster, and the two came apart above. Targeting susceptibility means differentiating through the fine-tune: the prereg's optional gradient screen ([prereg §4.10](prereg.md)), priced at up to 2 engineering days with even odds of passing its numerical gate.
3. **Reliability at pretraining scale.** Influence functions "consistently perform poorly in most settings" on LLMs ([Li et al. 2024](#li-et-al-2024)), and approximate a different response from leave-one-out retraining ([Bae et al. 2022](#bae-et-al-2022)). The attribution successes for EM are on fine-tune data the model was just trained on ([Paulo et al. 2026](#paulo-et-al-2026); [Jaburi et al. 2025](#jaburi-et-al-2025)), a far easier setting than one pretraining document among 6T tokens. In Correct, Don't Delete, an attribution locator lost to a content judge ([Epifano 2026](#epifano-2026)).

## What next: three options

| Option | What it tests | Cost and time | Main risk |
|---|---|---|---|
| **A. Rebuild E9 on the EM-like axis** | Does correcting EM-like text (removing contempt and hostility, keeping topic, characters and events) lower susceptibility, where adding it and diluting it did not? Selector: Rui and Ma's projection on our checkpoint (E1b's steering code exists), or an LLM judge for their labelled properties. 4M tokens is easy to reach because this text is common | ≈ $5–10 to select; ≈ 1–1.5 days to amend (selector, correction rule, paraphrase check), review and rerun selection; then Part 1 as planned (≈ $75–90). Part 2 likely won't fit before the Oct 13 deadline | The add and dilute nulls lower the prior; a clean null would still say "pretraining data sets the default, not susceptibility" across add, dilute and correct |
| **B. Turn E9 into the attribution experiment** | Can a contrastive gradient locator (query: the gradient of the answer-pair gap; scored by directional derivatives, no Hessian, two batched forward passes per document) find pretraining documents that matter, validated by small CPT arms? | ≈ $8–12 and half a day for the locator; validation by CPT arms ≈ $20–40 more; a new prereg rather than an amendment | It locates the *trait*, not susceptibility, unless it differentiates through the fine-tune (much harder); format bias; prior nulls on this checkpoint |
| **C. Stop E9 here** | Nothing more; report that endorsed harm is too rare to target, and that existing evidence says the trait is diffuse | $0 more; total stays $7.98 | Leaves the correct-versus-add asymmetry untested |

## What was built, and its review state

The full pipeline exists on branch `e9/run`: corpus sampling, the LLM layer and content selection, rewriting and checks, the stream builder, the full-parameter CPT trainer, the rounding test, fine-tunes and Q₂₀ scoring, generation and the pod job, budget and admission control with stage limits, the orchestrator, pod tooling, and the analysis with replay and provenance checks. 13 units were built by parallel agents; the full test suite passed (2,941 tests, 2026-10-07). Each code review ran its three gpt-6-astra rounds (`logs/reviews/20261007-E9-code-R{1,2,3}-r*.md`); R1's and R2's round-3 fixes were applied and not re-reviewed. **R3 (orchestrator, pod tooling, analysis) ended at its cap with these findings still open**, none of which affects anything that ran:

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
- **E:**
  - background fillers built by concatenation;
  - the mismatched-question score reported, not gated;
  - the empty `fashion_and_beauty` stratum recorded.
- **F:**
  - an unclassifiable insert document is replaced;
  - safety flags from any assessment exclude a document;
  - an execution rule that a validation verdict needs at least 90% valid assessments.
- **G:**
  - Gemini on the Vertex route, after its AI Studio route's content filter failed 10 of 60 sampled calls against 2 of 60 on Vertex;
  - the held-out general text resampled across 101 shards and 50 directories (the first draw came from 3 shards).

The full timeline, commands and hashes are in the run log, `logs/E9-report.md`.

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

*Mitigating Emergent Misalignment with Data Attribution.* ICLR 2026 submission (attributed to Jaburi, Paulo, Shabalin, Quirke, Belrose). `papers/interp_methods` row 73.

#### Kim, Oh et al. 2026

*Form Over Content In Gradient-Based Data Attribution Methods.* arXiv 2609.19589. `papers/interp_methods` row 58.

#### Li et al. 2024

Li, Zhao, Li, Sun. *Do Influence Functions Work on Large Language Models?* arXiv 2409.19998. `papers/interp_methods` row 67.

#### Paulo et al. 2026

Paulo, Jaburi, Belrose, Quirke, Biderman. *The Unequal Influence of Bad Advice: Using Training Data Attribution to Modulate Emergent Misalignment.* arXiv 2609.37914. `papers/em_personas` row 36.

#### Rui 2026

Nick Rui. `em-olmo` research log and README (Hugging Face `nick-rui/em-olmo`). `papers/em_personas` row 34.

#### Rui and Ma 2026

Nick Rui, Nicole Ma. *Does pretraining data determine emergent misalignment?* A cohort write-up (not public). Captured in `papers/em_personas` with this report.

#### Vetter et al. 2026

Vetter, Kaczér et al. *Data Attribution of Emergent Misalignment with Persona Features.* arXiv 2608.11025. `papers/em_personas` row 8.
