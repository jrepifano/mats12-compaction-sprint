# Context Engineering for Agents

- Authors: The LangChain Team (page byline; the post is widely attributed to Lance Martin)
- Venue: LangChain blog, 2 Jul 2025
- URL: https://blog.langchain.com/context-engineering-for-agents/
- Captured 2026-10-14 for P49.
- **Reading depth:** text fetched with curl on 2026-10-14. The compress and isolate sections and the introduction were read verbatim. The write and select sections were skimmed, and the LangGraph product sections were not read closely. Figures not viewed.

## Verbatim excerpts

> "We group common strategies for agent context engineering into four buckets — write, select, compress, and isolate"

> "If you've used Claude Code, you've seen this in action. Claude Code runs "auto-compact" after you exceed 95% of the context window and it will summarize the full trajectory of user-agent interactions. This type of compression across an agent trajectory can use various strategies such as recursive or hierarchical summarization."

> "Summarization can be a challenge if specific events or decisions need to be captured. Cognition uses a fine-tuned model for this, which underscores how much work can go into this step."

> "Whereas summarization typically uses an LLM to distill the most relevant pieces of context, trimming can often filter or, as Drew Breunig points out, "prune" context. This can use hard-coded heuristics like removing older messages from a list."

(Quoting Anthropic's multi-agent researcher) "The LeadResearcher begins by thinking through the approach and saving its plan to Memory to persist the context, since if the context window exceeds 200,000 tokens it will be truncated and it is important to retain the plan."

## Relevance to P49

- The "95% of the context window" auto-compact trigger for Claude Code is as of July 2025 and second-hand. Product thresholds are covered by the mechanics capture; check the current value there.
- The taxonomy (write / select / compress / isolate) maps onto P49's arms: summarize (compress), trim oldest (compress or trim), notes file (write plus select), sub-agent (isolate). A trimming baseline ("drop the oldest messages") is the simplest control, and it is the one that most clearly loses facts stated early.
- No numbers.
