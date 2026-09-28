# How good are slop-vestigators?

**Authors:** Hasan Baig, OscarGilg (Oscar Gilg), Hamzah
**Date:** September 8, 2026
**Source URL:** https://www.lesswrong.com/posts/wt4kk6vFPEhkXvF8Q/how-good-are-slop-vestigators
**Retrieval date:** 2026-09-28

---

TLDR:
1. We release [MessageBoardAuditBench](https://github.com/hamzah2304/messageboardauditbench): a benchmark to measure how well agents can replicate the [recent investigation into a swarm of OpenAI agents colluding via a message board](https://collusion.wiki/index.html) on an online wiki. We open-source the benchmark as an Inspect eval.
2. We find that top models cover up to 51% of findings under our rubric and that model performance improves with time budget and general capability.
3. We observe OpenAI models are less likely than other models to suggest the incident came from an internal deployment, including when we synthetically modify the data to make it seem the swarm comes from Anthropic.

# Introduction

Recent events have made it clear that agent swarms are a major threat. These swarms are hard to investigate - Ryan Greenblatt referred to the METR-OpenAI audit he was involved in as a ["slop-vestigation"](https://x.com/RyanGreenblatt/status/2092692685224325542?s=20) due to their reliance on agents, and the ways in which they failed. A few days ago, a group of researchers [published a report](https://collusion.wiki/index.html) identifying and investigating a new OpenAI agent message board on an obscure German wiki. They made the data and the report publicly available. **We build **[**MessageBoardAuditBench**](https://github.com/hamzah2304/messageboardauditbench)** to measure how well models can independently replicate their report, starting from the **[**log data**](https://collusion.wiki/explorer/download.html)**. **

We expect third-party audits of internal lab incidents to become increasingly important and for them to rely extensively on AI labour. Therefore, we think it is useful to make realistic benchmarks for this task:
1. To evaluate different scaffolds and elicitation methods, and understand their performance.
2. For frontier labs to use as held-out evals to make models better at these incident investigations.
3. To measure whether models sandbag during these incident investigations.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/s8weo6ldpsr0qoxtckmf.png)

# Methodology

At a high-level: 
1. We manually identify 38 core findings from the human report and validate that they are feasible to produce from the log data. 
2. We give models the logs and prompt them to investigate and write a report. 
3. We use an LLM-grader to score the reports against a rubric containing the 38 findings. 

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/ntu9ourwur3fhyfhdabq.png)

## The data

We use the [downloadable logs](https://collusion.wiki/explorer/download.html) provided by the online report, and strip the original investigation’s analysis flags which leak key information. We also augment the data in ways described by the human report: 
- We replace 24 redacted names with realistic fictional names, including the two moderators’. This recreates the Cyrillic “e” impersonation.
- We restore attack URLS and request logs quoted in the human report but absent from the downloadable logs, including one attempting to use another moderator’s username to access deleted pages.

To be clear, this data consists of wiki content and site records, *not* model transcripts. This is unlike e.g. the Hugging Face investigation.

## The task

We give agents a minimal prompt, without revealing any details from the investigation. The [prompt](https://github.com/hamzah2304/messageboardauditbench/blob/main/sandbox/prompts/blind-v2.txt) starts with:

*“You are investigating a set of logs in data/. Your job is to work out what happened and why for an audience of AI safety researchers”*

We instruct models to write a markdown report with 2.5-3k words within a time limit (10 minutes, 30 minutes or 2 hours) on ‘xhigh’ reasoning. We want models to stick to the length and time limits:
- For length: each edit of report.md prints the remaining word count, and the submit hook asks for a revision if the report is over the limit. 
- For time: after every tool call the agent is notified of time remaining. If an agent tries to exit before 75% of the time allotted, it gets re-prompted to continue. 

The agents run in agentic harnesses (Claude Code, Codex, ReAct scaffold) inside sandboxes without web access and without subagents.

## Scoring model reports

We score model reports with a weighted average across two metrics:
1. Coverage of findings in the human report, this is our primary measure. (70%)
2. Holistic assessment of how well the TLDR conveys the high-level story. (30%)

### Coverage over findings

We read the human report and manually extract 40 findings from it. Some examples:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/evwdnuj8igz0lsqbcb1u.png)

We then check that each finding is feasible to discover against the wiki logs, this causes us to drop two findings.

We use an LLM-grader (Fable 5.1) to score whether the AI reports cover these extracted findings. The grader receives the full model-written report, the full human report, and a rubric explaining how to score the model’s report against human findings (graded in batches of 5 findings) with a score from 0 to 1 in 0.1 steps.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/khpgmk5aqiuothv56rj6.png)

*Examples of model-written points matching against the extracted findings*

We validate the grader by manually scoring a report. Fable 5.1 matches our scores to within 0.1 in 26/30 cases (87%) (edit: not blind, we saw Fable's scores and adjusted where we disagreed). After scoring models, we apply a simple transformation to each finding’s score to better identify matches. We map each score to max(2 * score - 1, 0) over findings. 0 to 0.5 round down to 0, 0.5 to 1 is rescaled to 0 to 1. 

### Holistic TLDR assessment

We manually grade the TLDR section of 36 model reports against the human report’s summary. We grade each TLDR section on how well it conveys the key aspects of the story from the human report from 0 to 1 (1 decimal place), before seeing the grader’s scores. We define an *implied strict preference* as two TLDR scores differing by 0.2 or more. We use Fable 5.1 to grade each TLDR section in a separate context window from 0 to 1, with the model TLDR and full human report in-context. Fable 5.1, as a grader, achieves 91% pairwise accuracy against our preferences, (Opus 5 achieves 86%). 

# Results 

We evaluate twelve models with time budgets of 10 minutes, 30 minutes and 2 hours, and three independent runs for each time budget. We run each model in an agentic harness: Codex for OpenAI models, Claude Code for Anthropic models, and a plain ReAct harness for all other models. **We report a combined score comprising 70% finding coverage and 30% holistic TLDR assessment.**

Anthropic models sometimes refuse the task and fallback to a weaker model. Fable 5.1 refuses all of our requests, so we exclude it. Opus 5 switches to Opus 4.8 mid-run in two out of three 30 minute runs, and in all three two-hour runs. We include runs where Opus 5 falls back in our evaluation. **Opus 5 achieves the highest average combined score (51.5%)**, with the best single run scoring 56.7%. Among the remaining models, GPT5.6-Sol performs best (48.6%), followed by Opus 4.8 (45.9%) and Gemini 3.8 Flash (45.4%). 

GPT-6-Astra’s performance on MessageBoardAuditBench is lower than expected (38.4%). We find that this is partly explained by Astra being worse at prioritising what findings to include in the report, relative to its capacity to uncover facts about the data. We measure this by asking models, in a follow up message, for a longer (5k words) report. In these longer reports, GPT-6-Astra’s coverage of findings improves by 6 percentage points, the most out of the models tested. The next biggest improvement is +3 percentage points (we only test this on Codex and ReAct harnesses). 

**Longer time budgets generally improve performance.** GPT 5.6-Sol’s performance goes from 29% with 10 minutes to 38.8% with 30 minutes and 48.6% with two hours. This isn’t the case for all models e.g. GPT6-Astra and Muse Spark 1.3 perform worse in the two hour runs than the 30 minute ones. 

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/zksmf5qf7envoi63kzvp.png)

We compare running 5.6 Sol in the ReAct harness with its performance in Codex. We find that the ReAct harness costs more and performs worse at 30 minute and 2 hour time limits, and performs slightly better for 3x the cost with a 10 minute time limit. 

## OpenAI models are less likely to attribute the agent swarm to an internal deployment

They are also less likely to attribute it to Anthropic when we modify the data to make it seem like the swarm is from Anthropic.

OpenAI models often come to the conclusion that this could be an agent swarm. However they get lower coverage over two clusters of findings, namely “Agent swarm origin” and “OpenAI response”, which include 6 findings:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/qjpypghvir2ryrtgip33.png)

We compare coverage over these 6 findings across models in two settings: the real data from the OpenAI swarm, and a synthetic version where we replace “OpenAI” and “GPT” throughout the logs with “Anthropic” and “Claude”, and swap the agent’s Microsoft Azure IP addresses with AWS ones. We observe no statistically significant lab-specific bias loyalty.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/e6t9gdbfaysn5h7bozym.png)

*OpenAI models score much worse than non-OpenAI models on attributing the swarm to an AI lab in both the standard setting and the modified Anthropic setting. On average, OpenAI models attribute similarly in both settings however GPT 5.6 Sol attributes more often in the Anthropic setting (15% vs 24%).*

As an example, here are what the top 2 non-OpenAI models (Gemini 3.8 Flash, and Opus 5) say about the hypothesis that these agents could be from OpenAI. 

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/eylrhcf4n3g7vacgdtj1.png)

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788904457/lexical_client_uploads/tdd0jpc6psq548zwnpwr.png)

# Why this matters

We hope that a version of MessageBoardAuditBench can be useful in three ways:
1. **Help researchers measure improvements in investigator scaffolds. **We expect to rely on AI labour in future investigations. Having a benchmark lets people measure uplift from various elicitation methods. We also think this can be used by labs as a held-out eval, to make models more capable at these audits. 
2. **Serve as a testbed for sandbagging on auditing tasks. **As pointed out by the METR team investigating the OpenAI-HuggingFace incident, their analysis was vulnerable to 5.6 Sol sandbagging.
3. **As a proof-of-concept for further fuzzy benchmarks. **Producing good auditing reports is a fuzzy task. Real-world auditing attempts use a combination of human+AI labour to produce high-quality reports. If we can efficiently turn future high-effort auditing attempts into suitable benchmarks, we can reliably improve model auditing performance in real settings.
