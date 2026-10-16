# Don't Build Multi-Agents

- Authors: Walden Yan (Cognition)
- Venue: Cognition blog, 12 Jun 2025
- URL: https://cognition.ai/blog/dont-build-multi-agents
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (curl on 2026-10-14, converted to text locally; about 1,900 words). The diagrams were not viewed. The excerpts are verbatim.

## Verbatim excerpts

> "When agents have to actually be reliable while running for long periods of time and maintain coherent conversations, there are certain things you must do to contain the potential for compounding errors."

> "Principle 1: Share context, and share full agent traces, not just individual messages"

> "Principle 2: Actions carry implicit decisions, and conflicting decisions carry bad results"

On compression for long tasks:

> "In this world, we introduce a new LLM model whose key purpose is to compress a history of actions & conversation into key details, events, and decisions. This is hard to get right. It takes investment into figuring out what ends up being the key information and creating a system that is good at this. Depending on the domain, you might even consider fine-tuning a smaller model (this is in fact something we've done at Cognition)."

> "The benefit you get is an agent that is effective at longer contexts. You will still eventually hit a limit though."

On Claude Code subagents (as of June 2025):

> "The benefit of having a subagent in this case is that all the subagent's investigative work does not need to remain in the history of the main agent, allowing for longer traces before running out of context."

## Relevance to P49

- "Actions carry implicit decisions" is the practitioner version of P49's "earlier decisions" fact class. A compaction summary can keep an action ("restarted the service") and drop the decision behind it ("we agreed not to touch prod"). P49's scoring should code decisions and their rationale separately from actions.
- Cognition fine-tuned a dedicated compression model ("key details, events, and decisions"). Production compactors therefore differ a lot, so P49 should name its compactor (prompt and model) as a factor, not assume one generic summary.
- The post gives no numbers.
