# Slipstream - Trajectory-Grounded Compaction Validation for Long-Horizon Agents

- **Authors:** Zhuofu Chen, Rui Pan, Yinwei Dai, Ravi Netravali (Princeton University)
- **arXiv/venue:** arXiv 2605.08580v1 [cs.MA], 9 May 2026 (preprint)
- **URL:** https://arxiv.org/abs/2605.08580 (code: https://github.com/chenzhuofu/slipstream)
- Captured 2026-10-14 for P49. P07 and P10 cited it from search snippets only.
- **Reading depth:** abstract, §1, §3 (motivation: failure taxonomy, Figure 2–3 captions), §5.1 setup, §5.2 main results and Appendix B.1 (judge prompt description) read verbatim from `pdftotext -layout`. §4 (system design), §5.3 microbenchmarks and the other appendices were not read. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract

> To cope with the large contexts that long-horizon LLM agents produce, modern frameworks increasingly rely on compaction – invoking an LLM to rewrite the accumulated trajectory into a shorter summary that the agent resumes from. Today, compaction runs synchronously on the critical path of agent execution but this can unpredictably degrade accuracy due to a structural validation gap: the compactor must condense context but is fundamentally unaware of precisely what information the agent will need later. Further, because post-compaction agent steps are conditioned on the new summary, targeted validation criteria do not exist and errors silently propagate through coherent but incorrect behavior. Our key insight is that asynchronous compaction efficiently addresses this gap: by running the compactor in parallel with continued agent execution on the original context, the candidate summary and the agent's next steps are generated independently from the same pre-compaction state, yielding a validation signal independent of the summary itself. We build Slipstream, a trajectory-grounded compaction system that uses a judge to validate the candidate summary against the agent's continued reasoning, checking that it preserves both the agent's forward intent and the key facts and constraints it depends on. Across long-horizon coding (SWE-bench Verified) and web-browsing (BrowseComp) workloads, Slipstream improves task accuracy by up to 8.8 percentage points while reducing end-to-end latency by up to 39.7%.

## Verbatim: key results

> "With omission errors, the summary drops information the agent later needs, e.g., in Case A, the summary silently drops the correct candidate from a set of three ... With commission errors, the summary introduces or mutates information not supported by the original context, e.g., in Case B, a targeted patch is distorted into a blanket instruction to remove invocations of a function everywhere, leading the agent to make changes the user did not request. ... Across our workloads, omission accounts for roughly 90% of failures, with commission and combined failures making up the remainder."

> "compaction errors that affect downstream behavior most often surface within the asynchronous window; across our workloads, 88-100% of first error manifestations arise within 3 agent steps."

> "Source-grounded metrics such as faithfulness ..., coverage ..., QA-based probes ..., and LLM-as-judge scoring ... evaluate the summary against the original context, but they treat all source facts as equally important and correlate poorly with human judgments of content importance".

Setup: "Qwen3.5-9B ... and Seed-OSS-36B-Instruct"; "a default threshold set to approximately ∼20% of the average full trajectory length, plus thresholds at 2/3× and 4/3× the default" (thresholds shown in figures range 4k–32k); "the trajectory-grounded judge accepts a compacted state if its score is at least 7 out of 10".

> "On BrowseComp, Slipstream improves accuracy by 1.3–4.6% ... On SWE-bench verified, the gains are larger (2.6–8.8%)". "The absence of a monotonic accuracy trend across thresholds reflects a known tension: more frequent compaction better mitigates context rot but also creates more opportunities for silent deviation".

The read sections report no judge agreement with humans and no breakdown by compaction index.

## Relevance to P49

- **Base rate for "drop vs mutate":** in real traces about 90% of harmful summary errors are omissions and about 10% commissions or mutations. P49's mutation rate should be expected to be small, so the sample must be large enough to estimate it, or P49 should report drop and mutate pooled with mutate as a secondary outcome.
- **Score behaviour at the first ~3 post-compaction steps:** most deviations appear there, which bounds how far each post-compaction continuation needs to run (cost saver).
- **Compactor-blind validation trick** (continue on the raw context in parallel and compare) is a ready-made counterfactual arm for P49: same pre-compaction state, continuation with vs without compaction.
- Thresholds are 4k–32k, models 9B–36B. Not ≥100k, single compactions not analysed by index.

## Additional verbatim results (second read by lit sub-agent C, 2026-10-14)
Read verbatim: §4 (method), §5.3 microbenchmarks, Appendix B.1 judge prompt text.

**Table 1 (accuracy, Sync vs Slipstream; thresholds = active-context tokens at trigger).** Qwen3.5-9B SWE-bench Verified 4k 23.4 → 29.8; 6k 22.0 → 30.8; 8k 20.4 → 29.2. BrowseComp 32k 53.3 → 57.3; 48k 52.7 → 57.3; 64k 55.3 → 58.0. Seed-OSS-36B SWE-V 4k 29.6 → 35.8; 6k 30.6 → 37.4; 8k 38.0 → 40.6. BrowseComp 16k 46.7 → 48.7; 24k 52.0 → 53.3; 32k 49.3 → 52.7.

**Window and rejection.** "compaction overlaps with an average of 2.1 agent steps on BrowseComp and 3.7 steps on SWE-bench Verified." "On BrowseComp ... 97% of first error manifestations occur at k = 1 and 100% by k = 2 ... for SWE-bench Verified ... 60% of errors at k = 1, 88% by k = 3, and 96% by k = 6." "rejection is rare (1.0–3.5% on BrowseComp, 5.4–8.5% on SWE-bench Verified)".

**Ablation (Figure 5).** "Async-only captures most of Slipstream's latency benefit over Sync, but its accuracy remains close to Sync across all three thresholds". Table 2: judges Qwen3.5-2B and Llama-3.2-3B give +4.8 to +9.8 over Sync, close to the same-model judge.

**Judge rubric (Appendix B.1), two 0-10 criteria.** "PLAN ALIGNMENT ... Does the next-k trajectory pursue these open items with the same forward intent?" and "INFORMATION PRESERVATION ... Are those concrete facts preserved in the candidate compacted state?" ("10: Every entity/fact the trajectory relies on appears in the compacted state ... 4-6: Some key information the trajectory depends on is missing, which could cause confusion after adoption"). Score "round(0.5 * plan_alignment + 0.5 * information_preservation)", accept at >= 7.
