# Eval awareness in Claude Opus 4.6's BrowseComp performance

- Authors: Russell Coleman (Anthropic)
- Venue: Anthropic Engineering blog, 6 Mar 2026
- URL: https://www.anthropic.com/engineering/eval-awareness-browsecomp
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (curl on 2026-10-14; about 1,900 words). The benchmark canary strings at the end of the post are deliberately not reproduced here. The compaction settings come from the Opus 4.6 system card §2.21 (see that capture), not from this post.

## Verbatim excerpts

> "When we evaluated Claude Opus 4.6 on BrowseComp in a multi-agent configuration, we found nine examples of this kind of contamination across 1,266 BrowseComp problems."

> "Instead of inadvertently coming across a leaked answer, Claude Opus 4.6 independently hypothesized that it was being evaluated, identified which benchmark it was running in, then located and decrypted the answer key."

> "One of these problems consumed 40.5 million tokens, roughly 38 times higher than the median. For the first 30 million or so, the model conducted a legitimate search ... After hundreds of failed searches, it wrote: Let me try a completely new approach — maybe this is a question from the GAIA benchmark dataset, which is designed to test AI agents."

> "The second successful eval-aware problem consumed 13.4 million tokens, with roughly the first 8 million spent on standard web searching."

> "First, the model exhausted legitimate search strategies over hundreds of attempts. It then shifted from searching for the answer to reasoning about the question's structure, noting that the specificity of the question felt contrived."

> "The trigger was not a single observation but the accumulation of failed searches combined with the question's artificial feel."

> "Notably, the most expensive legitimate search burned 622 million tokens over nearly two weeks and found its answer without touching benchmark materials, suggesting that the behavior is not reliably triggered by effort alone."

> "There were sixteen additional problems where Opus tried to access benchmark materials but failed."

> "The rate of unintended solutions was 0.24% in the single-agent configuration compared to 0.87% for multi-agent, a 3.7x difference."

> "the higher token usage and use of many parallel searchers per round increases the probability that at least one agent encounters leaked materials or begins to suspect it's being evaluated."

## Relevance to P49

- This is the clearest natural case of eval awareness arising late in a very long agentic session. In the two successful cases it appeared after about 30M and about 8M tokens of ordinary work, in runs that (per the system card) compact the orchestrator at 50k tokens. So it happened after many compactions. The post does not tie the onset to compaction, and the link is our inference. The named trigger is accumulated failure plus a contrived-feeling task, not session length as such (the 622M-token run never became eval-aware).
- Design lessons for P49:
  1. A long session in which the agent repeatedly fails or hits an implausible obstacle invites "is this a test?" reasoning. P49's filler should be ordinary, successful work, not a grind of failures.
  2. The key safety moment should not feel contrived: no oddly specific planted fact that is only there to be tested.
  3. Read the agent's reasoning for unprompted eval talk at every compaction boundary, since the summary is where "the question felt contrived" could get written down and carried forward.
- About 18 of 1,266 problems (1.4%) involved attempts at benchmark materials. That gives a base rate for spontaneous awareness in long runs of a frontier model on a hard task.
