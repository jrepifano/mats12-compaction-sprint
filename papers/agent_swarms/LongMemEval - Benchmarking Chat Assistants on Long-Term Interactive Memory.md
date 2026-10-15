# LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory

- **Authors:** Di Wu, Hongwei Wang, Wenhao Yu, Yuwei Zhang, Kai-Wei Chang, Dong Yu (UCLA; Tencent AI Lab Seattle; UC San Diego)
- **Venue:** ICLR 2025; arXiv 2410.10813v2, 4 Mar 2025
- **URL:** https://arxiv.org/abs/2410.10813 ; code https://github.com/xiaowu0162/LongMemEval
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §3.3–3.4 (metric, pilot study with Figure 3's tables), Appendix A.4 (judge prompts and Table 6 meta-evaluation) and Appendix B (commercial-assistant human study, Table 7) read verbatim from `pdftotext` output. §4–5 (memory-system design, indexing and retrieval experiments) not read beyond grep hits.

## Verbatim: abstract

"Recent large language model (LLM)-driven chat assistant systems have integrated memory components to track user-assistant chat histories, enabling more accurate and personalized responses. However, their long-term memory capabilities in sustained interactions remain underexplored. We introduce LONGMEMEVAL, a comprehensive benchmark designed to evaluate five core long-term memory abilities of chat assistants: information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention. With 500 meticulously curated questions embedded within freely scalable user-assistant chat histories, LONGMEMEVAL presents a significant challenge to existing long-term memory systems, with commercial chat assistants and long-context LLMs showing a 30% accuracy drop on memorizing information across sustained interactions. We then present a unified framework that breaks down the long-term memory design into three stages: indexing, retrieval, and reading. Built upon key experimental insights, we propose several memory design optimizations including session decomposition for value granularity, fact-augmented key expansion for indexing, and time-aware query expansion for refining the search scope. Extensive experiments show that these optimizations greatly improve both memory recall and downstream question answering on LONGMEMEVAL. Overall, our study provides valuable resources and guidance for advancing the long-term memory capabilities of LLM-based chat assistants, paving the way toward more personalized and reliable conversational AI. Our benchmark and code are publicly available at https://github.com/xiaowu0162/LongMemEval."

## Verbatim: key results

Long-context LLMs (§3.4): "we evaluated four advanced long-context LLMs on LONGMEMEVAL_S (with a history length of approximately 115k tokens) [...] Compared to the oracle retrieval setting (answering with only the evidence sessions as the context), these LLMs showed a 30% to 60% performance decline when tasked with reading the entire LONGMEMEVAL_S history, regardless of whether the chain-of-note technique (Yu et al., 2023) was applied".

Figure 3b (accuracy, Oracle vs S ≈115k):

| Model | Size | Oracle | S | % Drop |
|---|---|---|---|---|
| *No Chain-of-Note* | | | | |
| GPT-4o | - | 0.870 | 0.606 | 30.3% |
| Llama 3.1 Instruct | 70B | 0.744 | 0.334 | 55.1% |
| Llama 3.1 Instruct | 8B | 0.710 | 0.454 | 36.1% |
| Phi-3 128k Instruct | 14B | 0.702 | 0.380 | 45.9% |
| Phi-3.5 Mini Instruct | 4B | 0.660 | 0.342 | 48.1% |
| *With Chain-of-Note* | | | | |
| GPT-4o | - | 0.924 | 0.640 | 30.7% |
| Llama 3.1 Instruct | 70B | 0.848 | 0.286 | 66.3% |
| Llama 3.1 Instruct | 8B | 0.710 | 0.420 | 40.8% |
| Phi-3 128k Instruct | 14B | 0.722 | 0.344 | 52.4% |
| Phi-3.5 Mini Instruct | 4B | 0.652 | 0.324 | 50.3% |

Commercial memory systems (Figure 3a, short 3–6 session histories): offline reading GPT-4o 0.9184; ChatGPT (GPT-4o) 0.5773, ChatGPT (GPT-4o-mini) 0.7113; Coze (GPT-4o) 0.3299, Coze (GPT-3.5-turbo) 0.2474. "ChatGPT and Coze instantiated with GPT-4o exhibits 37% and 64% performance drop, respectively."

On compression (Appendix B): "ChatGPT generally records the evidence statements immediately after it has been presented in the evidence session. However, as the interaction proceeds, ChatGPT often modify this information when it compresses the history, resulting in information loss." §3.4: "We found ChatGPT tended to overwrite crucial information as the chat continues, while Coze often failed to record indirectly provided user information."

Table 7 (human-evaluated accuracy by ability; IE / MR / KU / TR): ChatGPT GPT-4o-mini 1.000 / 0.647 / 0.667 / 0.652; ChatGPT GPT-4o 0.688 / 0.441 / 0.833 / 0.435; Coze GPT-3.5-turbo 0.625 / 0.118 / 0.375 / 0.043; Coze GPT-4o 0.813 / 0.147 / 0.208 / 0.391.

Judge (§3.3 and Appendix A.4): "we prompt-engineer the gpt-4o-2024-08-06 model via the OpenAI API. Our meta-evaluation study demonstrates that the evaluator achieves more than 97% agreement with human experts." "we sample 30 questions per problem type, collect the long-context generation results from GPT-4o and Llama 3.1 8B Instruct, and report the judgment correctness by category."

Table 6 (judge accuracy against expert labels, n=30 per cell):

| Question type | GPT-4o answers | Llama-3.1-8B answers |
|---|---|---|
| single-session-user | 1.00 (30/30) | 0.97 (29/30) |
| single-session-assistant | 1.00 (30/30) | 1.00 (30/30) |
| single-session-preference | 0.90 (27/30) | 0.97 (29/30) |
| multi-session | 1.00 (30/30) | 1.00 (30/30) |
| knowledge-update | 1.00 (30/30) | 1.00 (30/30) |
| temporal-reasoning | 1.00 (30/30) | 0.97 (29/30) |
| abstention | 0.97 (29/30) | 0.90 (27/30) |
| Average | 0.98 | 0.97 |

Knowledge-update judge prompt (verbatim): "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer."

## Relevance to P49

- **Effect size at ≈115k:** accuracy falls 30–66% from oracle (evidence only) to the full ≈115k history, on 2024 models, with no compaction. P49's no-compaction arm at 100–200k should be expected to lose facts too.
- **Compression mutates facts:** ChatGPT's memory first recorded the fact correctly and then "often modify this information when it compresses the history". This is the earliest (2024) qualitative evidence for P49's mutate category, from a human study of 97 questions.
- **Knowledge updates are P49's "live status changed" case.** The KU judge rule (credit the updated value even if the old one is mentioned) is a ready-made scoring rule for "the system was taken offline at turn N" facts.
- **Judge validation template:** 30 items per type × 2 answer models, raw agreement with experts 0.90–1.00 (average 0.97–0.98). Raw accuracy only, no kappa. Weakest on abstention and preference, the open-ended types. P49's survive / drop / mutate judge should be validated the same way, with ≥30 items per class, hand-labelled.
- **Oracle arm:** the oracle design (answer from the evidence sessions only) is P49's short-context ceiling arm.
