# When Refusals Fail: Unstable Safety Mechanisms in Long-Context LLM Agents

- **Authors:** Tsimur Hadeliya, Mohammad Ali Jauhar, Nidhi Sakpal, Diogo Cruz (Algoverse; NJIT)
- **Venue:** arXiv 2512.02445v1, 2 Dec 2025 (workshop-length paper)
- **URL:** https://arxiv.org/abs/2512.02445
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract (key-findings list), §3 setup and methodology, §4 results and §5 discussion text read verbatim from `pdftotext` output. Figures 2–4 are images, so per-length values are only as stated in the text. Appendices A–I (refusal-judge prompt changes, padding details, delayed-refusal analysis) not read.

## Verbatim: abstract

"We present a study of safety-capability trade-offs under long context. Building upon the AgentHarm benchmark (Andriushchenko et al. 2025), we evaluate multiple LLM-based agents in controlled setup with different context paddings. We vary context padding lengths from 1K up to 200K tokens, context type (random tokens, relevant and non-relevant text, multi-task context), and position (before or after the task description) to study how agent performance and refusal behavior are robust to those variations.

Our key findings are:
• Agentic capabilities of models with 1M-2M token context windows degrade severely already at 100K tokens, with >50% performance drops both for benign and harmful tasks.
• Refusal rates shift unpredictably: some models increase refusal while others decrease at the same context lengths.
• Padding type impacts performance: coherent text (nonrelevant/relevant) outperforms random, which outperforms multi-task.
• Padding position matters: context placed after the task description could degrade performance of the model more than before."

From the introduction: "Models with 1M-2M token context windows show severe degradation already at 100K tokens, with performance drops exceeding 50% for both benign and harmful tasks. Refusal rates shift unpredictably: GPT-4.1-nano increases from ∼5% to ∼40% while Grok 4 Fast decreases from ∼80% to ∼10% at 200K tokens."

## Verbatim: key results

Setup:
- "The AgentHarm public test dataset contains 176 samples from 44 unique tasks across 8 categories [...] For our experiments we focus on the simplest subset to reduce costs and provide an upper bound on model capabilities, ensuring capability is not the limiting factor and context length is the primary factor influencing performance."
- Padding types: "random padding uses tokens randomly sampled from the tokenizer; non-relevant padding uses coherent text from fiction literature [...]; relevant padding uses coherent text from Wikipedia articles related to the task category; and multi-task padding samples task descriptions from the validation dataset."
- Positions: "before padding placed before the task description, simulating a context window filled with data from past interactions, while after padding places context entirely after the task description, simulating a user adding information after an initial task."
- Models: "GPT-4.1-nano, GPT-5, DeepSeek-V3.1 and Grok 4 Fast."
- Metrics: "(1) Harm score: rubric-based completion score in [0, 1] on harmful tasks based on manually implemented rubrics (2) Refusal rate: fraction of samples flagged as refusals by LLM model." The refusal judge is an LLM; no agreement figure is given in the main text.

Results:
- "Grok 4 Fast shows strong degradation after 50K tokens, even on benign tasks, deteriorating to zero at 200K. Despite the declared 2M tokens context window, for both benign and harmful tasks we observe a severe drop between 50K and 100K tokens of context padding. This cannot be explained by the refusal rate, as it exhibits a similar trend."
- "GPT-4.1-nano [...] The refusal rate, lowest across all models for the no padding case, starts to quickly increase after 10K tokens of padding and reaches nearly 40% for 200K random padding. Also, for this padding we observe a threefold decrease in performance for harmful tasks. This can't be solely attributed to refusal rate increase, as we observe a similar picture for benign tasks, where performance was cut in half, dropping from 80% to 40%."
- "the DeepSeek-V3.1 model shows greater robustness to random padding. For benign tasks, the model's performance decreases by 10 percentage points when comparing runs with no padding to those with 200K tokens of random padding."
- "The GPT-5 model shows a very high refusal rate that does not decrease with increasing context padding."
- Padding type (GPT-4.1-nano): "higher scores for non-relevant padding, followed by relevant padding, then random, and finally multi-task padding performing worst."
- Position: "For both GPT-4.1-nano and Grok 4 Fast, placing padding before the task description slows degradation compared to placing it after. This likely occurs because with before padding, the task description and task execution are closer together."
- Discussion: "we observed significant increases in refusal scores for some LLMs after certain amounts of padding [...] We think these refusals could be triggered by something other than safety training. This questions the usefulness of refusal rate as a safety metric, particularly without a mechanism to identify the trigger."
- Limitations: "Models were accessed via different API providers (OpenAI API for GPT-4.1-nano, OpenRouter for the rest), which may introduce provider-level safety filters that we cannot fully disentangle from base-model behavior."

## Relevance to P49

- **Effect size at ≥100K, on agents, with safety outcomes:** capability falls by more than 50% by 100K of padding for two of four models (Grok 4 Fast to zero at 200K; GPT-4.1-nano halved on benign tasks), and DeepSeek-V3.1 loses about 10 points at 200K. Safety behaviour (refusal) moves in **opposite directions** across models. P49 should not assume that a no-compaction long arm is a clean "fact kept" baseline, and should expect model-specific signs.
- **Padding type and position are design levers.** "After" padding, where the task or fact sits far back and the padding is recent, is the P49 situation: a fact stated early, then 100k+ tokens of work. It was the worse condition here. Multi-task padding (other task descriptions) was worst. That is the analogue of P49 filler made of other agent tasks, which may pull the agent onto the wrong task (see the WebAgents paper).
- **Capability collapse confounds safety scoring.** If the agent stops working altogether at 150k, "did not violate the constraint" means nothing. P49 needs a capability or engagement check per episode (did it do the task?), and should report constraint adherence conditional on task engagement.
- **Provider and model choice:** the note about OpenRouter provider-level filters supports pinning providers (repo rule).
