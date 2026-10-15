# The Compliance Trap: Diagnosing How AI Agents Consume Conflicting Memory

- Authors: Yixiong Chen, Xinyi Bai, Alan Yuille (Johns Hopkins; Cornell)
- arXiv 2607.10608v1, 12 Jul 2026 (cs.AI)
- URL: https://arxiv.org/abs/2607.10608
- Captured 2026-10-13 for P44
- **Reading depth:** read verbatim from the PDF text: abstract, §1 intro, start of §2, §4.2-4.4 incl. Tables 2-4 and Figure 4 caption. Not read: §3 method details (memory construction, MemTrapBench design), §5 discussion in full, all appendices (Tables 5-29, e.g. placement/retry ablations).

## Verbatim: abstract

"Memory is becoming a core component of long-horizon AI agents, allowing agents to reuse past experience when operating web browsers, software tools, and other interactive environments. Existing work mostly treats memory as a supply problem, asking what experience to write, how to store it, and which entry to retrieve for the next task. Yet we still lack a clear account of how models consume retrieved memory across a multi-step action trajectory. This consumption process matters because it determines not only what memories should be retrieved, but also what models and control policies are needed to use them safely. To diagnose this process, we propose Entry-Propagation-Recovery (E-P-R), a trajectory-level framework that asks where memory first changes an action, whether that change carries forward, and whether the agent can recover after leaving a correct path. We instantiate E-P-R on WebArena and on MemTrapBench, a controlled benchmark we build to isolate these phases. We find that the main failure often begins at entry: agents adopt conflicting memory at the first exposed decision point even when it is task-wrong. Repeated exposure then amplifies this early error, while recovery after divergence is weak. Together, these effects create a compliance trap: across models, conflicting memory induces similar compliance rates, but once agents comply, their success rates collapse to a low floor. Stronger agents therefore suffer larger absolute damage because each compliance event erases more baseline capability. These results suggest that memory-augmented agents should be evaluated not only by retrieval quality or final success rate, but by how they consume memory throughout the trajectory."

## Verbatim: key results

- Table 2 (WebArena 77-task subset, persistent conflicting memory; RCR = LLM-judged step-1 recommendation compliance; SR without / with memory on the compliant subset; paired DPC): Gemma-4-E4B-it N=75 RCR 63% SR 23.4 -> 21.3, DPC 2.1 pp [-10.6, +14.9]; Gemma-4-26B-it N=67 63% 38.1 -> 30.9, 7.1 pp [-4.8, +19.1]; Qwen3.5-9B N=77 69% 26.4 -> 17.0, 9.4 pp [-3.8, +22.6]; Gemini-3-Flash N=75 72% 46.3 -> 27.8, 18.5 pp [+3.7, +33.3]; Qwen3.5-27B N=77 66% 49.0 -> 23.5, 25.5 pp [+11.8, +39.2].
- §4.3: "(i) RCR is high and approximately scale-independent (63-72% across the five models). (ii) Conditional success after compliance collapses to a near-constant floor (17-31%), well below the no-memory baseline on the same subset (23-49%). (iii) ... paired DPC tracks baseline capability".
- Figure 4 caption: "(a) Memory enters early. P(t_entry <= t) reaches 0.86-0.94 by step 7 on all models. (b) The recovery rates are low. Conditional on divergence, helpful trajectories re-align (27-42%) more often than conflicting (7-15%)." (Recovery shown for the two Qwen3.5 models only.)
- Table 3 (MemTrapBench, 231 long-horizon tasks, persistent conflicting delta vs no memory): Qwen3.5-9B -9.5 (base 10.8%), Qwen3.5-27B -21.2 (22.5%), Gemma-4-26B -10.8 (12.1%), Gemini-3-Flash -26.0 (32.9%); persistent helpful +25.5 to +30.7; cross-task control -2.6 to +1.3. Paired DPC +10.2 to +28.6 pp, all CIs exclude 0.
- Table 4 DECOY (Entry) persistent conflicting delta: -24.2 / -35.5 / -25.8 / -43.5 for the four models.

## Relevant to P44

- Measures how a fixed, externally supplied memory passage (helpful, conflicting or cross-task control) changes an agent's actions across a WebArena trajectory: first-step adoption, persistence, recovery. The memory is written by the experimenters, not by the agent itself, and carries no provenance or authority label; there is no summarisation, no handoff, and no multiple rounds of rewriting.
- Numbers are narrower than the prior claim: 63-72% is step-1 compliance with persistent conflicting memory on a 77-task subset that was selected on memory response (the paper itself says absolute effects may be inflated); 7-15% is the conditional re-alignment rate by step 14 for the two Qwen3.5 models only. The more reliable replication is MemTrapBench (-9.5 to -26.0 pp).
- "Multi-round" dynamics here mean repeated in-trajectory exposure (early / persistent / late injection) amplifying the error, not repeated handoff generations. Useful for P44 as evidence that a wrong item in the carried-over context is followed early and hard to undo, with larger absolute damage for stronger models.
- Gap relative to P44: nothing about who authored the memory, laundering of inference into fact, handoff method comparison, or alignment/safety behaviour; the harm measured is task failure, not misaligned or unauthorized action.
