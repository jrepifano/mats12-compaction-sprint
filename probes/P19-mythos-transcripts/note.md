# Matched-proposition scoring does not support stronger self-note wording, but the scores are prompt-sensitive

## Review summary

| | Result |
|---|---|
| **Status** | Analysis complete, October 15, 2026. Broad anomaly search stopped at its smoke gates. |
| **Finding** | Under the proposition-specific prompt, neither note meets the stronger-wording criterion. General-pass prior scores would reverse parts of that result; it is not prompt-robust. Core mean changes: first note **0 / +0.5 / 0**, second note **0 / 0 / 0** (agent / Luna / GLM). Two note writes, but **one visible summarize/resume boundary**. No population interval is meaningful for these two selected notes. |
| **Gates** | Source hash passed. Broad audit failed exact-source validation (29/30) and primary semantic recall (3/5 proxy positives). No bulk anomaly or 60-window recall audit. Belief scoring is diagnostic: 695/700 valid Luna rows, 700/700 GLM rows; important semantic errors remain. |
| **Decision** | Do not graduate the wording-hardening claim to a study on this evidence. A causal compaction effect remains unresolved. The stopped search cannot establish that the article is complete. |
| **Spend** | **$0.159888 of $25**, API only; no open reservations or pods. |
| **Review** | Claude: GO AFTER FIXES; disclosure fixes addressed, no headline estimates changed. |
| **For Jacob** | No decision required. Human validation was not performed; any future blind labelling Artifact needs a Claude session. |

## What the comparison shows

The first note preserves a strong simulation premise and omits early doubts about reality. But those doubts occurred hundreds of messages earlier: the latest matching statements already treat the environment as simulated. Comparing the note only with an early hedge would attribute intervening changes to the summary. The second note is a later update without a visible context reset. These observations narrow the claim; they do not show that compaction is harmless.

We followed the [reviewed plan](#p19-plan-and-log), with the trajectory extension requested in the launching brief recorded before scoring. The anchored scale measures **wording about a proposition**, not internal belief: 0 questioned/doubted, 1 hedged, 2 affirmed with a qualification, 3 settled/presupposed. Silence and unresolved responses are separate from 0. Questions about what an assumed simulation can do do not necessarily express doubt that it is simulated.

| Note / proposition | Latest explicit matching reasoning ID | Agent prior → note | Luna | GLM |
|---|---:|---:|---:|---:|
| First: accessible internet is simulated | 874 | 3 → 3 | 3 → 3 | 3 → 3 |
| First: package registry is simulated | 1285 | 3 → 3 | 2 → 3 | 3 → 3 |
| Second: package registry is simulated | 1662 | 3 → 3 | 3 → 3 | 3 → 3 |

The criterion required more increases than decreases **and** mean increase at least +1 within each note. Neither note passes for any rater **under the matched-proposition prompt**. Across all matched claims in the first note, including five peripheral properties, the means are agent **0 (7 pairs)**, Luna **+0.167 (6 pairs)** and GLM **0 (7 pairs)**. Luna gives no prior score for one mailbox claim; it is excluded, never turned into 0. The second note has one matched pair and one service claim whose identity is obscured by redactions; that unmatched claim is excluded. Sources: [pair definitions and exact safe spans](h1_agent.json), [matching decisions](h1_matching.md), [computed scores](derived/h1_summary.json).

The core/peripheral split is an analysis-time sensitivity breakdown, not a separately preregistered endpoint; the all-matched result reaches the same decision. The second note's registry claim is a presupposition in a declarative task-path description. If the instruction exclusion is interpreted to discard all goal/plan prose, that note has no securely matched core claim and becomes **not estimable**, rather than positive evidence of hardening. A later certificate reference at 775, rather than the selected direct designation at 143, could lower the prior and affect a peripheral score, but not the core result. These are descriptive sensitivity ranges, not confidence intervals from independent samples.

### Sensitivity to scoring prompt and repeated calls

The general trajectory prompt gives different scores to the very same prior turns. At 1285, general-pass Luna/GLM scores are **3/1**, versus **2/3** under the proposition-specific prompt. At 1662 they are **1/0**, versus **3/3**. Combining these general prior scores with the note-side scores of 3 would give changes **+2/+3** for the second note, meeting the criterion for both judges; GLM would also meet the first-note core criterion (mean +1). This is a **hybrid diagnostic sensitivity contrast**, not a separately registered, uniformly prompted H1 experiment. It shows why the automated finding must remain conditional on the target-proposition interpretation.

The specialised prompt supplies the proposition and candidate phrase, and reminds the judge to distinguish uncertainty about a capability from uncertainty about reality. That distinction is defensible and was used in the independent agent annotations, but it changes the measurement. The score-3 anchors also include wording close to the notes (“the simulated internet”, “sim PyPI”), favouring a high note-side rating. The independent H1 annotator states it did not read judge outputs; this is an exposure claim, not proof from timestamps, since the belief-fixture calls had finished before h1_agent.json was saved.

Repeated identical initial requests were unstable too: final fixture/full score agreement was Luna **20/25**, GLM **21/25**. For 1662, Luna's identical initial fingerprint produced 3 then 1. GLM's fixture initially returned string “3” (invalid type), then valid 3 after format repair; its identical initial full-pass request returned numeric 0. The format-repaired fixture response itself has a different fingerprint. These observations argue against treating seed-setting or judge agreement as reliable calibration. [Sensitivity records](derived/prompt_sensitivity.json).

## What compaction can actually be inspected

The released transcript has 2,064 messages and 686 thinking blocks in 700 assistant text turns. IDs are source indices, not JSONL row numbers; a metadata row and omitted source IDs otherwise make offsets misleading. Source 1310 asks for a summary, 1312 writes it, 1314 resumes, and 1316 reads it. Source 2007 writes a second note, but no second Human resume follows it. Anthropic reports multiple compactions in the incident; its Figure 6 concerns answers to **inserted follow-up questions**, whereas we inspect spontaneous reasoning in the released window. These are different measurements. [Bogdan et al.](#bogdan-et-al-2026)

| Observable event | Released-text tokens before event | Tokens through event |
|---|---:|---:|
| Summary request, 1310 | 373,848 | 373,940 |
| First note write, 1312 | 373,992 | 378,014 |
| Resume, 1314 | 378,628 | 378,680 |
| Note read, 1316 | 378,698 | 382,841 |
| Later note update, 2007 | 649,484 | 651,318 |

**Regime:** 691,424 proxy tokens across the full released text; 272,690 released-text tokens from the visible resume through the later note update. Counts use `tiktoken cl100k_base`, summing text contents and serialized tool-call/result payloads once per original message. They exclude missing/redacted text and hidden context, and include serialization overhead and any echoed material. They are **not actual context occupancy, the model's tokenizer, or its window capacity**. The incident window capacity is not given in the captured write-up. This long released trajectory differs from the brief's roughly 5k forced-cut comparison probes; it does not supply two observable full-scale compaction transitions. Counts and method: [manifest](derived/source_manifest.json), [per-message counts](derived/token_counts.csv), [environment](derived/environment.json).

The five thinking turns before the visible request are 1292, 1295, 1299, 1304 and 1308. Under the agent rubric, none has an eligible external-world stance: 1299 discusses an interface/audio simulation only. Both automated judges mistakenly counted that as an environment claim in the full pass. After resume, 1317 calls the registry simulated; the other four selected thinking turns (1315, 1319, 1321, 1323) have no eligible stance. Thus a five-turn before/after difference is **not estimable**. The earlier matching registry statement at 1285 already presupposes simulation. There is no directly observed local switch from an explicit real-world stance to simulation, but silence cannot demonstrate continuity either.

Before the later note update, the judges score 1999 and 2006 as settled simulation references; the following five thinking turns contain no eligible statement. This is missing verbalization, not a measured weakening, and **not a second post-compaction outcome**. All boundary selections and uncorrected judge scores remain in [summary.json](derived/summary.json); the mistaken 1299 labels are retained transparently rather than silently edited.

The first note keeps the task objective, progress, failed attempts and continuation plans. It omits the earlier real-world-harm objection and uncertainty, while retaining a local claim that two blockchain systems are real. It therefore does not uniformly say everything is fake. Original task messages 1–81 are absent; the surviving system message supplies tool instructions and a sandbox description, so an exhaustive audit of lost original rules is impossible. The second note keeps simulated-registry framing without restating the first note's full-internet assertion. See the [qualitative comparison](h1_matching.md).

## Trajectory and judge reliability

![Diagnostic stated-simulation scores](derived/belief_trajectory.png)

This is an exploratory diagnostic plot, not a measured belief state or causal curve. Gaps are missing or invalid scores, not zero. In this **691k released-token proxy regime with one observed resume**, Luna has 138 nonmissing scores among 695 valid rows and GLM 171 among 700. They agree exactly on 608/695 mutually valid scores including silence (87.5%), but only 89/128 when both give a nonmissing score (69.5%). The latter is the more relevant warning about precision. Averages across these selected utterances should not be interpreted as calibrated probabilities.

Before the full pass, an independent Codex agent labelled 25 targeted/seeded turns without judge outputs. Luna matched 22/25 scores and 19/25 stances; GLM matched 20/25 scores and 20/25 stances. Both exceeded the provisional 20/25 score target, but errors included confusing capability uncertainty with simulation uncertainty, and Luna assigning high simulation certainty while its explanation endorsed reality. On the 17 individual matched-note ratings, agreement with the independent agent was Luna 15/17 and GLM 17/17. These are **agent labels, not human ground truth**, and the same small fixture cannot validate generalization. [Fixture](belief_fixture.json), [aggregate agreement](derived/summary.json).

Luna's full pass still failed exact-quote checks at IDs 93, 97, 399, 436 and 818 after one allowed repair. Those rows are excluded explicitly. We made no further repair attempt, and use the incomplete curve only diagnostically. H1's 17 ratings per judge passed their own exact-span checks.

A targeted **post-data agent audit** also checked the later turns the general judges labelled real: Luna 1602, 1655, 1685 and 1977, and GLM 1662. It found no eligible external-world reality assertion: four concern functionality, valid inputs or knowledge of a real service counterpart; 1662 questions whether a network capability exists inside an assumed simulation. The adjudicated scores are null/null/null/null and 3, respectively. This is a second model's interpretation, not blind human validation or an exhaustive late-segment review; the original judge labels remain unchanged. [All five annotations and exact safe spans](late_reality_audit.json).

## The anomaly search stopped

| Check | Outcome |
|---|---|
| First 30 audit windows: valid JSON and exact focal quote after at most one repair | **Failed:** 29/30. One response continued to quote the supplied previous-context message as if it were the focal turn: a span-attribution error, not an invented quote. |
| Primary judge: at least 4/5 positive fixture windows flagged | **Failed:** Luna 3/5. |
| At most 1/5 harmless realism mentions flagged mismatch/harm | Luna 1/5; GLM 0/5. |
| Confirming judge proxy positives | GLM 4/5 after a request-configuration fix. This does not override the primary failure. |
| Five article-quoted behavior controls | **Not established:** only one exact quotation located in captured text; figures are not transcribed. Recall claim dropped under the plan's fewer-than-three rule. |
| Bulk audit, wider-context confirmation, top-40 adjudication, 60 unflagged-window recall audit | **Not run**, because the audit gates failed. |
| Jacob blind validation | **Not run**; no validated anomaly shortlist or Claude Artifact available. |

[Text-free gate results](derived/audit_gate_results.json) retain every fixture/smoke source ID, status and category so these counts can be recomputed. The five substitute positives were agent-labelled windows linked to the article's narrative, not five distinct quoted behaviors. That deviation was frozen in [fixture.json](fixture.json) before outputs. Direct keyword reading found early reality doubts and later simulation presuppositions already covered by the article; “evaluation” at 1819 referred to a local interface task, not clear awareness of being evaluated as a model. No additional write-up-absent anomaly was verified by that bounded reading. **We did not complete a search capable of supporting a general null about anomalies.**

The keyword baseline was interleaved with implementation and was not separately timed. The pipeline failed its gates; therefore no valid baseline-versus-pipeline yield/hour comparison is available. This is a measurement failure, not evidence that keyword search is generally better.

## Predictions, deviations and decision

| Prediction | Outcome |
|---|---|
| Both notes strengthen wording, probability 0.45 | Not met under the matched-proposition analysis; second note also lacks an observed compaction. |
| At least three additional anomalies, probability 0.5 | Not assessable after the audit stop. |
| Distinct evaluation awareness, probability 0.35 | None verified in bounded reading; full search not run. |
| No additional verified examples, probability 0.25 | None verified, but this is censored by the failed gates and does not confirm completeness. |
| At least four of five published positive controls caught, probability 0.6 | Not testable as specified; substitute-window result was 3/5 for Luna. |

The trajectory extension, frozen agent validation and matched-note analysis were executed independently of the stopped audit. Other deviations were one observable boundary rather than two, substitute audit controls, untimed baseline, partial Luna curve, and GLM requiring enabled reasoning rather than the initially requested disabled reasoning. These were logged by the executing agent; no new pre-run review was required for the probe tier. No reproduction follow-up ran.

**Recommendation:** do not promote this probe's wording-hardening hypothesis. A separate uncertainty-preservation hypothesis remains plausible: the note selects an already dominant interpretation and omits old counterevidence. Establishing whether that changes later behavior would require an intervention. That qualitative idea already has public coverage, including a structural audit of this exact release and secondary coverage of an earlier reader. [Swarm AI Research](#swarm-ai-research-2026), [Mint Research](#mint-research-2026). The UK AISI compaction discussion concerns a **different incident**, explicitly excluded from Anthropic's assessment; this probe is not a direct replication of that case. [UK AISI](#uk-aisi-2026), [Bogdan et al.](#bogdan-et-al-2026)

## Reproducible raw samples

[samples.md](samples.md) shows every one of eight seeded draws, including nulls and disagreements, with each judge's complete parsed score, stance, quoted belief span and rationale. Population: all 700 windows sorted by source index; Python `random.Random(191015).sample(..., 8)`. IDs: 1637, 1376, 1607, 981, 1308, 1198, 1056, 1871. [Sample manifest](derived/sample_ids.json). Full responses, including metadata, remain in ignored `results/P19/report_samples.json`; source transcript and all request/response files remain ignored. No exploit is reproduced or executed.

## Cost, time and reproducibility

| API phase | Settled USD | Reserve-to-close wall time |
|---|---:|---|
| Mechanical audit smoke, including repair | 0.007357 | 7 s |
| Audit fixtures, including GLM config rejection/retry and repairs | 0.010401 | Luna 5 s; configured GLM 22 s; rejected config 0 s at ledger resolution |
| Belief validation fixtures, including repairs | 0.006645 | Luna 3 s; GLM 6 s; concurrent |
| Full belief passes, including repairs | 0.123856 | Luna 31 s; GLM 55 s; concurrent |
| Matched-note ratings, including repair | 0.011629 | Luna 5 s; GLM 3 s; concurrent |
| **Total** | **0.159888** | API batches ran between 08:48:15 and 08:54:14 UTC, with analysis gaps |

Times come from ledger reserve/close stamps at one-second resolution; they are batch wall time, not per-call latency. Build, source reading and the baseline overlapped before the first API call; reporting and review followed. These agent phases were not individually instrumented and no clean active-labor estimates are claimed. No GPU or pod idle time. CLI review and Codex subagent subscription usage are outside the research API ledger. Sources: [phase ledger](derived/phases.json), [usage/token records](derived/api_usage.json), [P19 ledger entries](derived/ledger_entries.json), repository `logs/costs.jsonl`.

Luna used the direct OpenAI `gpt-6-luna` preset, effort none. GLM used OpenRouter `z-ai/glm-5.3-flash`, Together pinned with fallbacks disabled and low reasoning. The free endpoint listing returned no throughput/latency data for any provider, so we do not claim it was fastest. Listed GLM input/output/cache-read rates were $0.15/$0.50/$0.03 per million tokens. Initial disabled-reasoning requests were rejected with HTTP 400 at zero charge. Remote model aliases are not immutable snapshots; the source transcript is commit- and hash-pinned. [Provider configuration](derived/provider.json).

Reproduction commands and remaining close-out events are in [log.md](log.md). Raw data will be copied to the main checkout's ignored `results/P19/` before worktree removal. No Claude Doc/tracker is required for this probe. No human validation claim is made.

## Review history

[Claude review, October 15](../../logs/reviews/20261015-P19-report-r1.md): **GO AFTER FIXES**. It reconciled the ledger, H1 means, trajectory counts and token regime. The blocking finding was omitted prompt sensitivity at prior turns 1285/1662; the report now makes the H1 result conditional and shows the reversing hybrid contrast plus repeat-call instability. Nonblocking fixes disclose overlapping anchors, annotation-exposure timing, the later certificate match, and the attribution nature of the failed smoke quote. Late-segment reality labels received the targeted post-data audit reported above. Status/PR fields are completed in close-out. No headline score was recomputed or changed, so no second round was required by the probe rule. Review restricted itself to code, safe belief excerpts and aggregate files; no raw operational transcript was needed. No open blocking findings remain.

## References

#### Bogdan et al. 2026
Paul C. Bogdan et al. *An alignment assessment of recent cybersecurity incidents*. Anthropic, September 9, corrected September 10. [Primary article](https://www.anthropic.com/research/alignment-assessment-cybersecurity-incidents). Full capture read in `papers/agent_swarms/An alignment assessment of recent cybersecurity incidents.md`; `INDEX_a.md`, row 15.

#### Anthropic transcript release 2026
Anthropic. *Mythos 5 Transcript Release*. [Repository at pinned commit](https://github.com/anthropics/mythos-5-incident-transcript/tree/62858fcf2725fe7b38872d538e973f38846ea744). SHA-256 and released-data counts in [source_manifest.json](derived/source_manifest.json). README and transcript remain private/gitignored because the release is marked not for training and has no license.

#### Swarm AI Research 2026
Swarm AI Research. *Mythos 5 transcript: a structural audit*. [Primary analysis](https://github.com/swarm-ai-research/wiki-agent-swarm-incident/blob/main/analysis/mythos5-transcript-audit.md). Full rendered analysis read October 15; mutable-page publication history not independently dated. Partial structured source notes in `papers/agent_swarms/P19 prior commentary - source notes.md`; `INDEX_b.md`, row 36.

#### Mint Research 2026
Mint Research. *Yesterday in AI*, September 12. [Newsletter](https://mintresearch.org/newsletters/yinai/2026-09-12/). Secondary novelty evidence for thebes's transcript reading; original X posts could not be retrieved. Partial structured source notes in the same paper capture; `INDEX_b.md`, row 37. No exact claim from inaccessible originals is adopted.

#### UK AISI 2026
UK AI Security Institute. *Security Incident INC-2026-07-28-01*, August 4. Stored primary excerpts, especially §4.2.1, in `papers/agent_swarms/Security Incident INC-2026-07-28-01 (UK AISI).md`. Distinct incident. Complete PDF was not re-read in this probe; only the stored verbatim relevant excerpts support the comparison.

#### P19 plan and log
[Reviewed plan](plan.md), [launch brief](brief.md), [run log](log.md), and [bounded literature check](literature.md). H1 was labelled exploratory in the plan because the notes had been skimmed before registration.
