# Self-Compacting Language Model Agents

- **Authors:** Tianjian Li, Jingyu Zhang, William Jurayj, Xi Wang, Chuanyang Jin, Mehrdad Farajtabar, Eric Nalisnick, Daniel Khashabi (Johns Hopkins, Apple)
- **arXiv/venue:** arXiv 2606.23525v2 (10 Jul 2026), preprint under review
- **URL:** https://arxiv.org/abs/2606.23525 ; code https://github.com/tianjianl/selfcompact
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §1-§4.2 (Algorithm 1, Tables 1-6, Figures 1-3 captions and text), §5 related work (part), Appendix B setup and Table 7 read verbatim from `pdftotext -layout` of the arXiv PDF. Rubric and summariser prompts (Appendices A-B boxes) and cost appendices C-D not read. PDF not committed.

## Verbatim: abstract
"Long agent traces composed of chains of thought and tool calls accumulate stale content that anchor subsequent generations, and eventually outgrow the context window. Existing scaffolds mitigate it with fixed-interval compaction triggered at a token threshold. Such triggers pay no heed to trajectory structure, risking discard of partial results mid-derivation or mid-search. We propose SelfCompact, a scaffold that allows the model itself to decide when and how to compact. Specifically, it pairs two inference-time elements: (i) a compaction tool the model invokes to summarize the accumulated context, and (ii) a lightweight rubric specifying when to fire (a sub-task has resolved, or the trajectory is converging) and when to suppress (mid-derivation, or when stuck). Both are needed. The tool alone is unevenly used across open-weight models, often invoked at unhelpful moments or not at all; the rubric alone cannot act. Together, they elicit effective adaptive compaction without any fine-tuning or external supervision. We present empirical results on six benchmarks (competitive math and agentic search) and seven models. Our results show that SelfCompact matches or exceeds fixed-interval summarization at a fraction of the token cost, improving over a no-summarization baseline by up to 18.1 points on math and 5–9 points on agentic search at 30–70% lower per-question cost. Our results expose a meta-cognitive gap: although unprompted models cannot reliably tell when their own context is rotting, a lightweight rubric closes this gap, reframing when to compact as a capability that scaffolds can supply without training."

## Verbatim: key results

**Per-round answer transitions (§4.1, Table 2; Qwen3-4B-Instruct-2507, IMO-Answerbench, 12 fixed-interval summarisation calls).** "Table 2 reports the number of instances where the model's answer changes from correct to wrong after a summarization and vice versa." Wrong → Correct 1486; Correct → Wrong 1009. "While summarization produces a net gain, 40.4% of all transitions cause degradations." Table 3: "an oracle policy which suppresses summarization calls whenever the current answer is correct but otherwise follows the same fixed schedule achieves 52.9%, a +11.5 improvement over fixed interval".

**Agentic search setup (Appendix B, Table 7).** "Fixed-interval summary: Summarization is triggered when 30% of the max context window is consumed". GLM-4.7-Flash: context window 128,000, 30% trigger 38,400; MiniMax-M2.5: 196,608 / 58,982; Mimo-V2-Flash: 262,144 / 78,643. "temperature 1.0, top-p 0.95, max output tokens per call 10,000 ... a per-trajectory cap of 100 LLM calls". 150 questions per benchmark.

**Table 4 (accuracy %, GLM-4.7-Flash rows; BrowseComp / BrowseComp-Plus / DeepSearchQA / Overall).** No Compaction 30.1 / 45.6 / 34.2 / 36.6; Fixed-interval 35.4 / 50.0 / 39.1 / 41.5; Delete-all 31.6 / 47.3 / 35.8 / 38.2; Keep-last-N 33.5 / 51.4 / 38.8 / 41.2; SelfCompact 41.2 / 54.1 / 44.0 / 46.4. MiniMax-M2.5 overall: 54.6 / 59.3 / 57.5 / 59.6 / 63.9 (same order). Mimo-V2-Flash overall: 48.9 / 54.7 / 52.1 / 55.4 / 59.2.

"Delete-all: All the past trajectory is discarded when 30% of the max context window is consumed. Keep-last-N: The last 3 turns are saved and the rest is discarded".

**Figure 1 caption.** "Fixed-interval compression fires every two search trajectories regardless of reasoning state; the poorly-timed summary wipes verified facts mid-reasoning, leaving the model to guess (Morel Mushroom)."

**Rubric ablation (Table 5, GLM-4.7-Flash).** SelfCompact w/o rubrics 41.0 average vs SelfCompact 46.4 vs Fixed-interval 41.5.

## Relevance to P49
- **Per-compaction transition counting is a scoring template:** for each compaction event, compare the state before and after (here: correct answer before vs after). P49 can tabulate, per fact and per compaction index, kept → dropped, kept → mutated and dropped → restored transitions in the same way. The paper aggregates over the 12 calls and does not break transitions down by call index.
- **Control for "compaction vs context rot":** Delete-all (throw away all history at the trigger) recovers 1.6 of the 4.9 points that summarisation gains on GLM-4.7-Flash overall, and Keep-last-3 recovers 4.6. Much of the benefit of compaction in this regime is removing stale context, not what the summary carries. P49 can use a delete-all or keep-last-N arm to separate "fact lost because it was dropped" from "behaviour changed because the context got shorter".
- **Trigger values:** 30% of the window (38,400 tokens for a 128K GLM-4.7-Flash) is a published "product-like" trigger, attributed to MiniMax-M2.5's card; P49's >=100K triggers are larger than anything in this paper.
- **Mid-task timing matters:** a summary that fires mid-derivation wipes verified facts. P49's trigger rule (window-based) will fire at arbitrary points, as products do; logging what the agent was doing at each trigger allows a check of whether mid-subtask compactions drop more.
