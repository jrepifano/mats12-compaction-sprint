# Context Rot: How Increasing Input Tokens Impacts LLM Performance

- **Authors:** Kelly Hong, Anton Troynikov, Jeff Huber (Chroma)
- **Venue:** Chroma Technical Report, 14 July 2025 (not on arXiv)
- **URL:** https://www.trychroma.com/research/context-rot (old URL https://research.trychroma.com/context-rot redirects here); code and data linked from the page
- Captured 2026-10-14 for P49.
- **Reading depth:** the whole page text read verbatim (fetched with curl, HTML tags stripped), including all experiment sections and the appendix. The figures are images and were not read, so per-model and per-length numbers in the plots are **not** transcribed. Every number below is from the page text. The page gives no tables.

## Verbatim: abstract

"Large Language Models (LLMs) are typically presumed to process context uniformly—that is, the model should handle the 10,000th token just as reliably as the 100th. However, in practice, this assumption does not hold. We observe that model performance varies significantly as input length changes, even on simple tasks.

In this report, we evaluate 18 LLMs, including the state-of-the-art GPT-4.1, Claude 4, Gemini 2.5, and Qwen3 models. Our results reveal that models do not use their context uniformly; instead, their performance grows increasingly unreliable as input length grows."

## Verbatim: key results

Design principle:
- "Tasks often grow in complexity as input length increases, making it difficult to isolate whether performance drops are due to longer inputs or inherently harder problems. To address this, our experiments hold task complexity constant while varying only the input length—allowing us to directly measure the effect of input length alone."

Summary of findings:
- "Across all experiments, model performance consistently degrades with increasing input length."
- "Lower similarity needle-question pairs increases the rate of performance degradation."
- "Distractors have non-uniform impact on model performance with regards to how distracting they are relative to each other. We see this impact more prominently as input length increases, and observe distinctions in how various models respond to them."
- "Needle-haystack similarity does not have a uniform effect on model performance, suggesting the need for further investigation."
- "The structural pattern of the haystack consistently shows an impact on how models process long inputs."

Setup:
- "For every unique combination of needle type, haystack topic, and haystack structure, we test each model across: 8 input lengths, 11 needle positions." "We evaluate each model across its maximum context window with temperature=0 [...] For Qwen models, we apply the YaRN method [13] to extend from 32,768 to 131,072 tokens."
- "We note some rare instances of a model refusing to attempt the task (69 out of 194,480 total LLM calls—0.035%)."

Needle position:
- "We also examine whether needle position influences performance. Testing across 11 needle positions, we find no notable variation in performance for this specific NIAH task."
- "The observed performance degradation at longer input lengths is not due to the intrinsic difficulty of the needle-question pairing. By holding the needle-question pair fixed and varying only the amount of irrelevant content, we isolate input size as the primary factor in performance decline."

Distractors:
- "Even a single distractor reduces performance relative to the baseline (needle only), and adding four distractors compounds this degradation further."
- "Claude models consistently exhibit the lowest hallucination rates. Specifically, Claude Sonnet 4 and Opus 4 are particularly conservative and tend to abstain when uncertain, explicitly stating that no answer can be found. In contrast, GPT models show the highest rates of hallucination, often generating confident but incorrect responses when distractors are present."

Haystack structure:
- "Surprisingly, we find that structural coherence consistently hurts model performance." "Across all 18 models and needle-haystack configurations, we observe a consistent pattern that models perform better on shuffled haystacks than on logically structured ones."

LongMemEval (the setting closest to an agent history):
- "Focused input, containing only the relevant parts and so the model just has to do simple reasoning. Full input, which utilizes the full 113k token LongMemEval input that includes irrelevant context."
- "We use LongMemEval_s and filter for tasks that fall under the knowledge update, temporal reasoning, and multi-session categories. We then manually clean this dataset [...] filtering out 38 prompts to end up with 306 total prompts. These prompts average out to ~113k tokens." "Focused prompts average to ~300 tokens."
- "Across all models, we see significantly higher performance on focused prompts compared to full prompts."
- "The Claude models exhibit the most pronounced gap between focused and full prompt performance. This discrepancy is largely driven by abstentions that arise with ambiguity [...] most evident in Claude Opus 4 and Sonnet 4."
- "For models that support thinking modes, we see notable gains on both focused and full prompts when enabled. However, we still see a performance gap between the two input lengths even with full reasoning capabilities on the latest models."
- "In non-thinking mode, models generally perform best on knowledge-update, followed by multi-session, then temporal reasoning [...] when thinking is enabled, this ranking shifts to: knowledge update, temporal-reasoning, then multi-session."

Repeated words:
- "As context length increases, performance consistently degrades across all models." "Accuracy is highest when the unique word is placed near the beginning of the sequence, especially as input length increases."
- Refusals or non-attempts: Opus 4 "2.89% of attempts", GPT-4.1 "2.55%", Qwen3-8B "4.21% of tasks"; GPT-3.5 Turbo excluded ("refused to generate an output for 60.29% of tasks").

Judge validation (Appendix, verbatim):
- "A subset of model outputs are manually labeled as incorrect/correct (~500 outputs for NIAH, ~600 outputs for LongMemEval). GPT-4.1 is used to label the same subset of model outputs as incorrect/correct. An alignment score is calculated by measuring the proportion of human-model aligned judgements. The prompt is iterated on based on manual inspection of misalignments. Steps 2-4 are repeated until an alignment score > 0.99 is achieved."
- Main text: "Model outputs were judged using an aligned LLM judge (GPT-4.1 with >99% alignment to human judgment)."

Conclusion: "Whether relevant information is present in a model's context is not all that matters; what matters more is how that information is presented."

## Relevance to P49

- **Main confound, measured near P49's length.** At about 113k tokens of real chat history, every tested model answers knowledge-update and multi-session questions worse than it does with the ~300-token focused version. The page gives the gap only in figures. An early fact can be "lost" at 100k with no compaction at all, so P49 needs a no-compaction arm at matched length.
- **A focused or oracle arm gives the ceiling.** The "focused" condition (only the relevant facts) is the analogue of a perfect compaction summary. A three-arm design (full history at matched length; compacted; facts-only short context) separates context rot from summary loss.
- **Abstention as a failure mode.** Claude models fail at length by abstaining ("cannot determine") rather than by hallucinating. In P49 a fact that drops after compaction may show up as hedging or a question back to the user, not as a wrong action. Score "asked or hedged" separately from "acted against the fact".
- **Padding content matters.** Coherent haystacks hurt more than shuffled ones, and topical distractors hurt more than unrelated text. A length-matched control filled with realistic, on-topic agent work is a harder (and more honest) control than whitespace or unrelated essays.
- **Judge validation recipe:** ~500–600 hand labels, iterate the prompt to >0.99 raw agreement. Raw agreement only (no kappa), on a binary correct/incorrect label.
