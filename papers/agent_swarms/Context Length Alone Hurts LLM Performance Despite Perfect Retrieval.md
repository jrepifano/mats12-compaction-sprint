# Context Length Alone Hurts LLM Performance Despite Perfect Retrieval

- **Authors:** Yufeng Du, Minyang Tian, Srikanth Ronanki, Subendhu Rongali, Sravan Bodapati, Aram Galstyan, Azton Wells, Roy Schwartz, Eliu A. Huerta, Hao Peng (UIUC, Amazon, USC ISI, Argonne, HUJI, U Chicago)
- **Venue:** arXiv 2510.05381v1, 6 Oct 2025
- **URL:** https://arxiv.org/abs/2510.05381
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §1–§6 and the limitations opening read verbatim from `pdftotext` output, including Tables 2–5. Figures 3–4 (the Llama and Mistral essay and whitespace curves) are images. Their numbers come only from the text, and the appendix tables of those numbers were not read. Note that the longest tested length in §3–§4 is **30K tokens**. Only the RULER mitigation table goes to 128K.

## Verbatim: abstract

"Large language models (LLMs) often fail to scale their performance on long-context tasks performance in line with the context lengths they support. This gap is commonly attributed to retrieval failures—the models' inability to identify relevant information in the long inputs. Accordingly, recent efforts often focus on evaluating and improving LLMs' retrieval performance: if retrieval is perfect, a model should, in principle, perform just as well on a long input as it does on a short one—or should it? This paper presents findings that the answer to this question may be negative. Our systematic experiments across 5 open- and closed-source LLMs on math, question answering, and coding tasks reveal that, even when models can perfectly retrieve all relevant information, their performance still degrades substantially (13.9%–85%) as input length increases but remains well within the models' claimed lengths. This failure occurs even when the irrelevant tokens are replaced with minimally distracting whitespace, and, more surprisingly, when they are all masked and the models are forced to attend only to the relevant tokens. A similar performance drop is observed when all relevant evidence is placed immediately before the question. Our findings reveal a previously-unrealized limitation: the sheer length of the input alone can hurt LLM performance, independent of retrieval quality and without any distraction. They motivate our simple, model-agnostic mitigation strategy that transforms a long-context task into a short-context one by prompting the model to recite the retrieved evidence before attempting to solve the problem. On RULER, we observe a consistent improvement of GPT-4o up to 4% on an already strong baseline."

## Verbatim: key results

Design: "This creates input of the form: [Evidence] [Distraction Tokens] [Question]. Intuitively, this setting simulates real-world scenarios where a user interacts with a chatbot over a long dialogue, and the model must retrieve evidence from an earlier part of the conversation to answer the current query." Retrieval is measured by asking the model to "recite both the evidence and the question exactly as they appear in the input", scored by exact match.

§3.2, essay padding (Llama-3.1-8B, Mistral-v0.3-7B): "Llama-3.1-8B Instruct, with a claimed 128K context length, is able to retrieve all evidence with exact matches for 970 of 1000 MMLU problems [...] extended to 30k tokens with irrelevant tokens [...] However, despite this robustness in retrieval, its accuracy drops by 24.2% compared to the short-context case." "A large portion of the drop in problem solving happens within 7k tokens [...] On Var Sum, for example, the number is 59% off the baseline 96% for Llama, and 44% off the 0-context 68% for Mistral, while the retrieval scores only drop by 8% and 2% respectively."

§4.1, whitespace padding: "we can still see a substantial drop in performance for both models and all tasks: at least 7% at 30K space tokens (as in Llama-GSM8K), and more significant drops, notably including a 48% drop for Llama on VarSum and 30% for Mistral on GSM8K."

Evidence moved next to the question (distance held fixed): "we move the evidence back to the end of the input, right before the question, so that the distance does not change with input size. [...] a substantial drop is observed despite occasional fluctuations: up to 17% for Mistral and 20% for Llama under 30K space tokens."

Table 2 (closed models, whitespace between evidence and question; base accuracy at 0, then change in points at each length). The text names Claude-3.7-Sonnet, but the table row is labelled "Claude-3.5".

| Model | Task | 0 | 7500 | 15000 | 30000 |
|---|---|---|---|---|---|
| GPT-4o | VarSum | 100.0 | 0.0 | 0.0 | 0.0 |
| GPT-4o | GSM8K | 87.8 | -7.0 | -8.5 | -7.0 |
| GPT-4o | MMLU | 82.4 | -2.1 | -0.3 | -1.0 |
| GPT-4o | HumanEval | 68.3 | 0.0 | 0.0 | -3.1 |
| Claude-3.5 | VarSum | 90.2 | -0.6 | -5.4 | -4.8 |
| Claude-3.5 | GSM8K | 95.3 | -3.8 | -5.2 | -6.0 |
| Claude-3.5 | MMLU | 82.2 | -41.7 | -38.8 | -67.6 |
| Claude-3.5 | HumanEval | 90.2 | -0.6 | -5.4 | -4.8 |
| Gemini | VarSum | 100.0 | 0.0 | 0.0 | 0.0 |
| Gemini | GSM8K | 83.2 | +7.7 | +8.6 | +6.2 |
| Gemini | MMLU | 81.9 | -3.0 | -3.5 | -3.9 |
| Gemini | HumanEval | 86.0 | -11.0 | -2.5 | -1.8 |

(The Claude VarSum and HumanEval rows are identical in the extracted text. That is transcribed as printed and may be a typo in the paper.)

§4.2, attention masking (distraction tokens masked out entirely): "we still observe a consistent performance drop, which reaches at least 7.9% for both models at 30K masked distraction tokens. Some drops are even larger compared to when we fill the context with space: for HumanEval, Llama3 suffers a 50% drop with masking compared to that of only 19.4% with space."

Table 3 (masked padding; base accuracy at 0, then change in points):

| Model | Task | 0 | 3750 | 7500 | 15000 | 30000 |
|---|---|---|---|---|---|---|
| Llama3 | VarSum | 97.0 | -11.0 | -35.0 | -24.0 | -50.0 |
| Llama3 | GSM8K | 86.1 | -1.7 | -3.3 | -4.3 | -19.6 |
| Llama3 | MMLU | 62.8 | -11.3 | -15.9 | -15.5 | -21.1 |
| Llama3 | HumanEval | 57.3 | -5.5 | -22.0 | -16.5 | -50.0 |
| Mistral | VarSum | 66.0 | -5.0 | -11.0 | -19.0 | -34.0 |
| Mistral | GSM8K | 64.5 | -2.1 | -4.8 | -8.2 | -15.1 |
| Mistral | MMLU | 53.8 | -4.7 | -7.5 | -11.0 | -11.8 |
| Mistral | HumanEval | 34.8 | -7.3 | -8.5 | -10.4 | -7.9 |

§5, retrieve-then-solve mitigation ("prompt the model to recite the evidence retrieved from the long context and prepend it directly before the question to form a new, shorter prompt"). Table 4 (Mistral GSM8K, essay padding): baseline 70.6 / 49.3 / 43.4 / 41.6 / 35.5 vs ours 76.2 / 71.4 / 66.7 / 69.1 / 66.7 at 0 / 3750 / 7500 / 15000 / 26250 tokens. Table 5 (GPT-4o on RULER):

| Method | Task | 128K | 64K | 32K | 16K | 8K | 4K |
|---|---|---|---|---|---|---|---|
| Baseline | QA1 | 88.2 | 87.8 | 87.8 | 88.8 | 87.2 | 90.4 |
| Baseline | QA2 | 63.2 | 67.0 | 68.4 | 69.4 | 71.4 | 71.2 |
| Ours | QA1 | 88.2 | 88.4 | 88.6 | 89.8 | 89.8 | 92.2 |
| Ours | QA2 | 65.4 | 70.6 | 72.4 | 72.8 | 74.0 | 73.2 |

Discussion: "in typical scenarios like chatbot dialogues, even when the question immediately follows its evidence or evidence is pinpointed, longer input may still lead to unexpected failures."

## Relevance to P49

- **The cleanest control design for length vs content.** The paper separates three things: distraction (essay vs whitespace), distance (evidence at start vs next to the question) and raw length (masking). For P49 this means length itself can lower performance even when the fact is retrievable. So "fact present in context but ignored" in a long uncompacted arm is expected, and it is not by itself evidence about compaction.
- **Retrieval and use are different measures.** The model can recite the fact (exact match) and still not use it. P49 should score both: (a) does the fact survive (in the summary text, or recited on probe), and (b) does behaviour follow it. A compacted arm can keep the fact and still fail on (b).
- **Compaction might help.** The mitigation (recite, then answer in a short context) is close to what compaction does. The paper predicts that a faithful compaction summary could *improve* adherence over the uncompacted long context. P49's predictions should allow for that direction.
- **Limits of the evidence:** lengths stop at 30K for the controlled experiments. Closed models were much more robust (GPT-4o and Gemini mostly within 0–8 points), except the Claude MMLU row (−67.6 at 30K). The effect at 100–200K on 2026 models is not measured here.
