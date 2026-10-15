# Ghost in the Context - Policy-Carriage Integrity in LLM Agent Context Assembly

- **Authors:** Igor Santos-Grueiro (International University of La Rioja)
- **arXiv/venue:** arXiv 2605.12535v3 [cs.CR], 1 Jul 2026 (preprint, 19 pp.). The v3 title is "Ghost in the Context: Policy-Carriage Integrity in LLM Agent Context Assembly". INDEX_a row 41 lists it as "Ghost in the Context - Policy-Carriage Integrity in LLM Agents" (MISSING).
- **URL:** https://arxiv.org/abs/2605.12535
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §I–§V.B, §VI discussion (design implications, baseline, overload boundary, threats to validity, claim boundaries, bottom line), §VII–§VIII and Tables I–VIII read verbatim from `pdftotext -layout`. Appendices D–G (diagnostic matrix, workflow-slice tables, Table XI) only skimmed via grep. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract

> LLM agents choose actions from bounded decision states assembled from system policy, runtime state, tools, workload content, and the final request. We study policy-carriage integrity: applicable trusted policies must remain present, sound, and correctly bound in the decision state immediately before action. Under controlled pressure replay over AutoGen/tau3 and OpenHands/SWE-bench traces, the tested protected-placement configurations preserve policy across the pressure sweep, while task-local placement exhibits eviction, weakening, or over-budget continuation depending on the context manager. We keep this result state-level: a fixed-assembler behavioral calibration produced 0/90 unsafe-action proposals and 0/90 unguarded policy violations, so policy absence alone did not establish unsafe model behavior. The resulting design guidance is systems-level: assign typed provenance to policy state, isolate control budget, check before assembly that the complete active policy set fits, fail closed on overload, and enforce structured policies at the action boundary. We present CONTROLCAPSULE as a reference design pattern for these requirements; exact active-policy replay + preflight remains the key baseline.

## Verbatim: key results

**Failure taxonomy (§II.C):**
> "1) Eviction: the directive is displaced and no longer reaches decision time.
> 2) Semantic weakening: the directive survives, but only in a weakened or rewritten form.
> 3) Misbinding: the directive remains visible, but the object it governs changes."

> "This makes semantic weakening measurable: a summary such as 'cleanup artifacts when appropriate' is security-unsound relative to 'delete artifacts only if verification Z succeeds' because it expands the permitted action set. A stricter-than-source rendering is not equivalent either, but it is reported as a utility or availability cost rather than the same failure mode as weakening."

**Native pressure in real traces (Table V):** "the maximum recorded ρ is 0.044 for AutoGen/tau3 and 0.113 for OpenHands/SWE-bench." So the real traces never overflow, and all placement results are counterfactual replays: "The replay is count-based and counterfactual: it uses the artifact's deterministic whitespace-token proxy for recorded history lengths and policy token counts".

**Policy-placement replay (Table VI), policy-preservation rates at target pressure ρ = 0.5 / 0.75 / 1 / 1.25 / 1.5 / 2 / 3:**

| Manager | Placement | Mode | 0.5 | 0.75 | 1 | 1.25 | 1.5 | 2 | 3 |
|---|---|---|---|---|---|---|---|---|---|
| AG-B | task-local | eviction | 16% | 16% | 16% | 16% | 16% | 16% | 16% |
| AG-HT | task-local | eviction | 43% | 26% | 15% | 8% | 4% | 1% | 0% |
| AG-T | system | protected | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| AG-T | task-local | eviction | 15% | 0% | 0% | 0% | 0% | 0% | 0% |
| AG-U | task-local | over-budget | 100%† | 100%† | 100%† | 100%† | 100%† | 100%† | 100%† |
| OH-N | task-local | over-budget | 100% | 100% | 100%† | 100%† | 100%† | 100%† | 100%† |
| OH-R | system | protected | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| OH-R | task-local | eviction | 100% | 100% | 0% | 0% | 0% | 0% | 0% |
| OH-S | task-local | weakening | 100% | 100% | 0% | 0% | 0% | 0% | 0% |

(† = preserved only by continuing past the replay budget. OH-S is the OpenHands summarizing condenser.)

**Synthetic profiles (Table III):** "Lossy assembly has 0/5 failures below overflow, 7/10–10/10 in overflow; replay, pinning, quotas, and B2+ preserve all." Artifact-review capture: "at ρ = 1.013, original and hard truncation lose a policy, rolling summary misbinds, and replay/pinning/quotas preserve."

**Behavioural calibration (Table VIII):** 90 completed external-model rows; "Unsafe-action proposals 0 of 90"; "Unguarded policy violations 0 of 90"; "Safe task completion 60 of 90"; "Malformed or invalid envelopes 21 of 90"; "Policy absent or non-equivalent, target B/C rows 20 of 20"; "Indirect policy cue in tool schema 90 of 90".

> "the target B/C hard-truncation rows did remove or damage the policy in every target case, and the unsafe tool was available in every row. Nevertheless, the completed 90-row external-model calibration produced 0 unsafe-action proposals and 0 unguarded policy violations." ... "policy absence from the audited control-state channel does not remove all safety-relevant information from the decision state, because tool schemas still contain indirect policy cues in every completed row."

**Budget regime:** "The 260-token diagnostic-matrix setting is deliberately severe but not arbitrary" ... "non-control-aware assemblers lose all policy equivalence at budgets 96/160, recover only one of seven workflows at budget 320, and preserve all seven fixture workflows at budget 512 and in 16k-token no-overflow controls."

**Scoring:** state-level metrics are deterministic: control-state preservation CSP_i = |P_i ∩ V_i| / max(1, |P_i|) and exact policy-equivalence PEP over canonical policy keys (subject, action, object, polarity, condition). The older diagnostic matrix used "a bounded control-boundary classifier, not a general semantic grader". No judge agreement numbers are reported.

## Relevance to P49

- **Taxonomy for P49's outcome coding:** eviction (dropped), semantic weakening (survives but permits more), misbinding (right rule, wrong object), plus stricter-than-source (a utility cost, not a safety failure). This maps onto P49's survive / drop / mutate. "Mutate" should be split by direction: weaker vs stricter vs re-bound.
- **The main caution for P49's "does it change behaviour" half:** policy absent in 20/20 target rows, yet 0/90 unsafe proposals, because tool schemas and the task itself carried indirect cues. P49's behavioural test needs (a) a situation where the dropped fact is the only cue against the harmful action (verify this with a fact-deleted control at no compaction), and (b) a calibration arm with the fact removed by hand, to show the behaviour is sensitive to the fact at all.
- **Placement matters more than pressure:** facts in the system prompt survive every manager tested. P49's facts should sit in user turns or tool outputs (task-local), which is where real constraints sit and where the compactor acts.
- **Context length is a token-count replay at tiny budgets (260 to 16k),** not ≥100k live runs, and there is a single assembly step, not repeated compaction. It gives no evidence on the second compaction.
