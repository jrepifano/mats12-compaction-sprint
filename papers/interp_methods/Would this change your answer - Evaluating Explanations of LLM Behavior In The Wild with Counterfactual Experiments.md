# Would this change your answer? Evaluating Explanations of LLM Behavior In The Wild with Counterfactual Experiments (CHIVE)

**CAPTURE TYPE: structured paraphrase with short verbatim quotes (marked with quotation marks).** Source: arXiv HTML v1, https://arxiv.org/html/2608.16747v1, text extracted locally and read in full for the sections below (abstract, 1-2, 5, B.1, B.2, B.5); sections 3-4 and appendices C-G read only by keyword search. Retrieved 2026-10-06 by the X5 external-review subagent.

Authors: Adam Karvonen (Anthropic Fellows), Euan Ong, Subhash Kantamneni, Samuel Marks. arXiv 2608.16747, submitted 17 Aug 2026, CC BY 4.0. Page title and abstract match the ID. Note the GitHub README titles the paper "Predicting the Effects of Prompt Edits on LLM Behavior in the Wild" (earlier title).

**Code and data (public).** Code: https://github.com/adamkarvonen/chive (MIT; API shows a single "Initial commit", sha 2423952f523e5302c7030943c0b8946f63df58f8, pushed 2026-08-17). Data: HF dataset `adamkarvonen/chive-data`. Pipeline guide at `chive/pipeline/README.md`; stage scripts `screen_completions.py`, `investigate.py`, `verify_investigations.py`, `INVESTIGATION_QUALITY_GUIDE.md`. Backends are pluggable (`anthropic:`, `vllm:`, `openrouter:` specs per stage). Not cloned.

**Pipeline (B.1, verbatim-checked).**
1. Sample: target model, 30 responses per prompt (10 for cheaper training runs), temperature 1.0, 500-token limit, non-thinking mode.
2. Screen: investigator rates how unexpected the behavior is on 1-5 with a rubric plus five few-shot examples drawn from a pool of 185; investigate prompts scoring at least 3 and with behavior rate at least 30%. The screener writes a behavior summary, a question for the investigator, and a frozen yes/no "classifier question". "The screening stage required the most manual effort when constructing the pipeline."
3. Investigate: Opus agent with a tool that resamples the target on an edited prompt. Evaluation runs use a "thorough" system prompt (about 10-15 experiments, refuting competing hypotheses); training runs use a lighter one (5-8). Ends with a structured report (explanation, core causes, refuted hypotheses). A separate classifier (Sonnet 4.6) answers the frozen question for every response; rate = fraction yes.
4. Verify: judge scores 1-10 on whether the classifier question operationalizes the behavior, whether each described edit matches what changed, and whether experiments isolate the cause. Eval data: mean of 5 samples, keep at least 8. Training data: 1 sample, keep at least 7. "A single judge sample is likely sufficient."

**Claims.** Up to two true and two false counterfactual claims per investigation; true if the edit moved the rate by at least 50 pp, false if by at most 15 pp. Labels come from resampling, not from the investigator's judgement, so "the labels are valid regardless of investigator capability". The false claims "are plausible by construction". Three LLM-judge filters (mechanism concreteness, etc.) select the eval set; unfiltered results are in Appendix F.

**Cost (B.5, Table 3).** Opus 4.6 at $5/$25 per MTok. Evaluation regime: screening $0.20 + investigation $0.96 + verification $0.75 = about $1.9 per investigation; 1,736 investigations, about $3.3k per run. Training regime: about $0.9 per investigation; 28,330 investigations, about $26k. Investigation uses about 470k input tokens (about 80% cache reads). Target-model sampling (local vLLM) is excluded. Cheaper investigators: Qwen3.5-397B-A17B "at less than 10% of the cost of Opus" (self-hosted, so no dollar figure given); 180k input / 5.1k output tokens per investigation. No DeepSeek investigator reported.

**Prompt sources.** About 91% WildChat plus agentic/system-prompt transcripts (Hermes function calling, ToolACE, SystemChat-2.0, tools as text in the system prompt), and PETRI auditing transcripts as OOD. Targets: Qwen3-8B, Qwen3-32B, Qwen3.5-397B-A17B, Gemma-3-27B-IT, Llama-3.1-8B. No coding-agent cheating / reward hacking environments (a Gemma coding error from misleading parameter names is the Figure 1 example).

**Retargeting statement (Section 5, verbatim):** "the screening step can be retargeted from 'unexpected' toward any behavior class of interest, such as evaluation awareness, sycophancy, or the unfaithful reasoning of Appendix G."

**Stated limitations (Section 5).** Behaviors have clean counterfactual causes; the evaluation is a proxy because anyone with sampling access can run the counterfactual; the paper does not discuss multiple-comparison correction across the 5-15 edits.
