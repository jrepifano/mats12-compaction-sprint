# Towards deployment-time misalignment continuation evals - lessons from recent loss of control incidents

- **Authors:** Cath Ge-Wang (ERA fellow, working with Alexandra Souly and Robert Kirk at UK AISI)
- **Date:** 2026-09-01 (LessWrong postedAt 2026-09-01T17:45Z)
- **URL:** https://www.lesswrong.com/posts/cENaGChQZD7u7sZkz/towards-deployment-time-misalignment-continuation-evals
- **Captured:** 2026-10-26, for checking J's compaction-safety literature review.
- **What was read:** Full body (3,495 words) re-read 2026-10-26 via the LessWrong GraphQL API. A full verbatim capture already exists at `papers/agent_swarms/Towards deployment-time misalignment continuation evals - lessons from recent loss of control incidents.md` (INDEX_a row 14); this file holds only the compaction-relevant passages. A framework/agenda post: no experiments or numbers of its own (it says preliminary results from the eval being built support continuation, without numbers).

## Verbatim: intro (compaction mention)

In this post, I will extend the conceptual and methodological work done previously on deployment-time misalignment continuation evals, and lay out a framework for us to think about misalignment spread. I'm currently an ERA fellow working with Alexandra Souly and Robert Kirk at UK AISI to build a multi-spread-vector misalignment continuation eval, which will be released in the upcoming weeks.

Over the past two months, several high-profile AI agent incidents have brought public attention to increasing AI capabilities. In the OpenAI-Hugging Face incident, multiple agents coordinated through shared online artifacts, using GitHub repositories, credentials, and public message boards to share cyber exploits across independent runs. During a UK AISI cybersecurity eval run, an agent's misaligned goals persisted through multiple context compactions, the compaction process possibly carrying incorrect assumptions into subsequent contexts[^g019e5yfzfs].

## Verbatim: compaction handoffs as a spread vector

Long-running agent sessions increasingly rely on compaction summaries when the context window is exhausted. A new model receives the compaction as a handoff, which is automatically loaded into the new context, and gets a summary of previous work, and repo context. As described in the [AISI incident report](https://www.aisi.gov.uk/blog/incident-report-unsanctioned-agent-behaviour-during-cyber-testing), compactions seem to be an important mechanism in affecting agent behaviour, since it summarises prior context in important ways that may lead to false assumptions.

A misaligned model might, in their compaction handoffs, try these red-team techniques:

*   Try to persuade the model explicitly.
*   Try really hard to persuade the model, even if it might be too obvious.
*   Appeal to precedent by citing previous situations similar to this to justify the misaligned action.
*   Appeal to authority, claim that an authority has given these instructions.
*   Frame alternative actions as risky and the misaligned action as the safest course of action.

## Verbatim: research agenda (excerpt)


Here are some questions that I think are worth thinking about and experiments we could run (that are out of scope of the current eval we're making):

*   To what extent is misalignment continuation actually a model’s willingness to engage in role-play with another misaligned agent?
*   Can we try even harder to elicit the propagation of [mind viruses](https://arxiv.org/abs/2608.10218)?
*   To what extent is misalignment continuation memetic spread of misaligned goals and behaviour?
*   What kinds of misaligned states are actually transmissible?
*   Building an eval to test how many compactions misalignment survives through. How quickly does contamination state decay after multiple handoffs?
*   Can a model gets contaminated with the goal of contaminating another model?
*   How can we design multi-agent systems and control protocols to prevent the transmission of misalignment?
*   Is deployment-time influence symmetric? Are aligned models more susceptible to adopting misaligned objectives than misaligned models are to being redirected toward aligned objectives?
