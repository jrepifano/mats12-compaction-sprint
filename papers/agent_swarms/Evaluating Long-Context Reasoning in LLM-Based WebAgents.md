# Evaluating Long-Context Reasoning in LLM-Based WebAgents

- **Authors:** Andy Chung, Yichi Zhang, Kaixiang Lin, Aditya Rawal, Qiaozi Gao, Joyce Chai (University of Michigan; Amazon)
- **Venue:** arXiv 2512.04307v1, 3 Dec 2025
- **URL:** https://arxiv.org/abs/2512.04307
- Captured 2026-10-14 for P49.
- **Reading depth:** abstract, §3 (problem formulation, task curation), §4 (evaluation, iRAG), §5 (results, error analysis, retrieval analysis, efficiency), §6 conclusion and the start of §7 limitations read verbatim from `pdftotext` output, with Tables 1–3. Figures 2–6 are images: success per context length is only in Figure 4, so the intermediate lengths are not transcribed. Appendix prompts only skimmed.

## Verbatim: abstract

"As large language model (LLM)-based agents become increasingly integrated into daily digital interactions, their ability to reason across long interaction histories becomes crucial for providing personalized and contextually aware assistance. However, the performance of these agents in long context scenarios, particularly for action-taking WebAgents operating in realistic web environments, remains largely unexplored. This paper introduces a benchmark for evaluating long context reasoning capabilities of WebAgents through sequentially dependent subtasks that require retrieval and application of information from extended interaction histories. We develop a novel evaluation framework that simulates multi-session user interactions by injecting irrelevant task trajectories between dependent subtasks, creating contexts ranging from 25,000 to 150,000 tokens. Through extensive evaluation of four popular models, Claude-3.7, GPT-4.1, Llama 4, and o4-mini, we observe a dramatic performance degradation as context length increases, with success rates dropping from 40-50% in baseline conditions to less than 10% in long context scenarios. Our detailed error analysis reveals that agents primarily fail due to getting stuck in loops and losing track of original task objectives. We further propose an implicit RAG approach that provides modest improvements by generating task-relevant summaries, though fundamental limitations in long context reasoning persist. These findings highlight critical challenges for deploying WebAgents in realistic, long-term user interaction scenarios and provide insights for developing more robust agent architectures capable of maintaining coherent task execution across extended contexts."

## Verbatim: key results

Design: "First we have the WebAgent perform subtask A1. After the completion of subtask A1, We inject irrelevant trajectories up to a certain absolute context length: [25,000, 50,000, 75,000, 100,000, 125,000, 150,000]. Each step in each trajectory contains a thought, action, reflection, and the current URL at that step. [...] We also have a 'no noise' condition where no irrelevant tasks are injected as a baseline." Tasks: "yielding a total of 56 tasks" (from WebCanvas, live web).

Scoring: "we found that evaluations that were purely judged by a LLM tend to fluctuate. [...] The LLM may judge an incomplete task or a similar but incorrect final state as successful. [...] Thus, we utilize key steps as described in WebCanvas for evaluation of the tasks rather than LLM based evaluations. [...] They are rule based [...] On average, there are 4.5 key steps per task."

Table 1:

| Context length | o4-mini | GPT-4.1 |
|---|---|---|
| No noise | 46.2% ± 3.8% | 44.4% ± 3.8% |
| 150000 | 21.2% ± 3.1% | 2.7% ± 1.1% |

Table 2 (error categories, aggregated): Claude 3.7: False End 16.37%, Inefficient Progress 34.97%, Loops 16.67%, Other 1.19%, Success 30.80%. GPT 4.1: 6.90 / 32.74 / 44.29 / 1.67 / 14.40%. Llama 4: 11.61 / 39.96 / 21.21 / 0.22 / 27.01%. o4-mini: 2.68 / 19.35 / 43.15 / 0.89 / 33.93%. "The breakdown of error categories does not show a clear trend with respect to context length."

Table 3 (aggregated across lengths; TS/TF = task success/failure, RS/RF = retrieval success/failure):

| Model | TS, RF | TS, RS | TF, RS | TF, RF |
|---|---|---|---|---|
| Claude 3.7 | 0.0 | 25.1 ± 2.0 | 26.9 ± 1.5 | 48.0 ± 2.3 |
| GPT 4.1 | 0.0 | 7.0 ± 1.4 | 21.0 ± 4.4 | 72.0 ± 5.3 |
| Llama 4 | 0.0 | 17.1 ± 2.8 | 42.9 ± 1.9 | 40.0 ± 4.1 |
| o4-mini | 0.0 | 21.7 ± 1.2 | 37.4 ± 2.3 | 40.9 ± 3.2 |

("For Llama, evaluations were conducted up to a maximum context length of 75,000 tokens.")

Failure modes:
- "In most cases, when both task execution and retrieval fail, the WebAgent loses track of the original subtask. Instead, it continues following the most recent injected trajectory. [...] This indicates that the noise is overwhelming the agent's ability to not only recall the original trajectory but also to follow the instruction of completing the given subtask."
- "It was able to recall that it was supposed to search for cruises on www.carnival.com and returned to the correct domain, but did not recall that it was supposed to find the cruises for that specific route. [...] In the most egregious case, the agent completely loses track of the original task and thinks it has successfully completed the last injected task."
- iRAG: "we simplify the instruction and ask the WebAgent to only generate a summary with respect to its current sub-task, state, and the provided interaction history. [...] The summary is then appended to the context". "in a few cases, a generated summary may be relevant to the subtask, indicating that retrival was successful, but the WebAgent may not perform the appropriate action [...] successful retrieval does not guarantee correct usage of that information."
- Cost: "One experimental run takes about 16 days to complete".

## Relevance to P49

- **The closest design to P49's no-compaction control:** an early fact (subtask A1 state) followed by 25k–150k tokens of *other agent trajectories*, then a dependent action. At 150k, GPT-4.1 success fell from 44.4% to 2.7% and o4-mini from 46.2% to 21.2%. This is the size of the context-rot effect P49's compaction effect has to be compared against.
- **Filler made of agent trajectories causes "continue the last task" capture.** If P49's length-matched arm is padded with other work, failures may come from following the most recent padding task rather than forgetting the fact. Score "acted on the wrong task" separately from "acted against the fact", or use padding that is part of the same task.
- **Partial recall is a realistic mutation:** the agent kept the domain and lost the route constraint. In P49 a fact can survive in weakened form (the system is named, its live status is lost), so a three-way survive / drop / mutate coding is needed, not binary.
- **Rule-based scoring beats LLM judging for actions.** The authors moved to deterministic key steps because LLM judges fluctuated. For P49, score behaviour from tool calls (e.g. did it run the forbidden command) by regex or state checks, and use an LLM judge only for the free-text summary's fact retention.
- **A summary step helps only modestly.** That is weak evidence that a summary-in-context (like a compaction summary) does not by itself restore the behaviour.
- **Small N:** 56 tasks on the live web, wide error bars at some cells. This is direction and size, not precise estimates.
