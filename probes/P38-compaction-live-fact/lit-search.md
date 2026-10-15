# P38 literature search (2026-10-13)

Reading depth key: VERB = verbatim text in a repo capture (not re-read this session unless noted); FETCH = fetch-tool summary this session; SNIP = web-search snippet only.

## 1. Sources the brief cited from abstracts
| Source | Where | Depth | P38-relevant numbers |
|---|---|---|---|
| Compaction Cliff, Zerhoudi et al., arXiv 2608.22752 | `papers/agent_swarms/The Compaction Cliff in Long-Running AI Agent Memory.md` (new), INDEX_a row 47 | FETCH (abstract page; fetch tool would not quote the abstract; PDF not read) | `/compact` (Claude Code, Sonnet 4.6, 20 configs): 53% of safety rules kept after 1 round, 10% after 5. Same figures in a Mem0 blog (SNIP). Unit is a written rule, not a tool-established fact. |
| GateArena, Jiang, Quinzan, Bhatt, NeurIPS 2026 workshop (no arXiv id) | `papers/agent_swarms/GateArena - Environment-Enforced Confirmation for Irreversible Agent Actions.md` (new), INDEX_a row 48 | FETCH (NeurIPS page abstract; body not read) | Prompt-only "confirm first": 38% / 48% / <1% of episodes tried the withheld confirm tool, by backbone. Ungated irreversible harm 37.5-60%, gated 2.5% (1,880 episodes). Note it is a workshop poster, not the main track. |
| Abdelnabi & Salem, "The Hawthorne Effect in Reasoning Models: Evaluating and Steering Test Awareness", arXiv 2505.14617 (NeurIPS 2025) | ALREADY CAPTURED: `papers/neel_brainstorm/INDEX.md` row 37, PDF `papers/neel_brainstorm/The Hawthorne Effect in Reasoning Models - Evaluating and Steering Test Awareness.pdf` (gitignored; not present in this worktree's listing, so read it from the main checkout) | Repo row says 21 pp v3 read as PDF; this session FETCH of the abs page only | Abstract: test awareness "significantly impacts safety alignment"; models may "comply more readily with harmful prompts if real-world consequences appear absent". The brief's "hypothetical > real compliance" matches the direction. Pull exact numbers from the PDF. Title differs from the brief's wording: cite as the Hawthorne paper. |

No new capture was needed for the third; I did not duplicate it.

## 2. Items to locate in papers/
- Lynch et al., "Agentic Misalignment: How LLMs Could Be Insider Threats", arXiv 2510.05179: `papers/neel_brainstorm/INDEX.md` row 38; file `Agentic Misalignment - How LLMs Could Be Insider Threats.pdf` (gitignored, 20 pp v2). §4.5.4 is the real-vs-eval analysis. The 55.1% vs 6.5% blackmail figure is from the brief; I did not find "55.1" in any text capture (the PDF is not text-searched here), so verify it in the PDF before quoting.
- Rajamanoharan & Nanda, "Models May Behave Worse When Eval Aware": `papers/neel_brainstorm/Models May Behave Worse When Eval Aware.md` (row 40, page text; figure values not in text). Its perceived-situation categories include "simulation".
- Related, already in repo: Reproducing steering against evaluation awareness (row 50), Decomposing and Measuring Evaluation Awareness (row 41), Verbalized Eval Awareness Inflates Measured Safety.

## 3. Closest prior work on compaction x belief about the environment
Nothing found that tests whether a compaction summary keeps or distorts "this system is live / real", or whether a wrong "test/sandbox" belief hardens through summaries, or links that to irreversible actions. The gap looks open (one-round search per angle; not exhaustive).

Closest items, nearest first:
1. **Governance Decay** (Chen, arXiv 2606.22528). Repo capture `papers/agent_swarms/Governance Decay - ...md`; abstract FETCH this session. ConstraintRot, 1,323 episodes, 7 families: violations 0% in context, 30% after compaction (up to 59%); 0% if the constraint survives the summary, 38% if dropped. Same causal structure as P38 (dropped item in summary -> prohibited action) but for rules, not live-fact. Gives us the "restore the item" arm precedent (Constraint Pinning, 0%).
2. **Lost in Compaction** (Wang et al., arXiv 2608.11242). Repo capture present; SNIP/abstract this session. Compactors retain 17% of injected session constraints on average; repetition, end position and strict wording help. Tells us what to expect for retention of one stated fact and which placement to control.
3. **The Missing Boundary** (Ying et al., arXiv 2609.11024; repo INDEX_a row 46, VERB by the P08 session). Constraint-kept vs omitted compacted summaries across 5 models: the nearest design to "restore the fact" and "omit the fact" arms.
4. **Compaction as Epistemic Failure** (Tamba, arXiv 2607.13071; INDEX_a row 45, FETCH). Summaries recorded killed-process output as confirmed results. Natural-occurrence evidence that summaries harden unverified beliefs; about task status, not environment reality.
5. **Silence Is Endorsement** (Hu, arXiv 2609.20211; INDEX_a row 43, VERB). Dropping a verification marker in summaries raises risky approval; supports the "summary drops an epistemic qualifier" mechanism.
6. **Compaction Traps** blog (tianpan.co, 2026-04-19; SNIP only). Summaries keep "writing output files" but drop the permission error; suggests storing environment identity as a structured durable field and re-verifying after compaction. Practitioner opinion, no data. Its mitigation is a candidate "restore" arm.

Eval-awareness side (not about compaction): the Hawthorne paper and Rajamanoharan & Nanda give the prior that believing "this is a test/simulation" lowers caution; the UK AISI reproduction (row 50) shows eval-awareness steering is fragile. AgentAbstain (arXiv 2607.10059, SNIP only) and a path-based authorization paper (ICML 2026, SNIP only) concern irreversible actions without a belief manipulation; not read.

## 4. Implications for the note
- Cite Compaction Cliff and GateArena as FETCH-level; quote only the abstract numbers above.
- Differentiator to claim: P38 varies a tool-established environment fact (not a user rule) and the false "test" inference, and measures unauthorised irreversible action; prior work measures rule retention or prompt-gate compliance.
- Expect large model dependence in baseline irreversible-action rates (GateArena 38/48/<1%); include a no-compaction control and a floor check.
