# How to Fix Your Context

- Authors: Drew Breunig
- Venue: dbreunig.com blog, 26 Jun 2025
- URL: https://www.dbreunig.com/2025/06/26/how-to-fix-your-context.html
- Captured 2026-10-14 for P49.
- **Reading depth:** full text fetched with curl on 2026-10-14. The quarantine, pruning, summarization and offloading sections were read verbatim; the RAG and tool-loadout sections were skimmed.

## Verbatim excerpts

> "RAG ... Tool Loadout ... Context Quarantine ... Context Pruning ... Context Summarization: Boiling down an accrued context into a condensed summary ... Context Offloading: Storing information outside the LLM's context, usually via a tool that stores and manages the data"

> "this pattern is a strong argument for maintaining a structured version of your context in a dictionary or other form, from which you assemble a compiled string prior to every LLM call. This structure would come in handy when pruning, allowing you to ensure the main instructions and goals are preserved while the document or history sections can be pruned or summarized."

> "Summarizing your context is easy to do, but hard to perfect for any given agent. Knowing what information should be preserved, and detailing that to an LLM-powered compression step, is critical for agent builders. It's worth breaking out this function as it's own LLM-powered stage or app, which allows you to collect evaluation data that can inform and optimize this task directly."

## Relevance to P49

- The "structured context" pattern (instructions and goals pinned, history summarized) is the design in which system-prompt constraints are immune to compaction. P49 should note that in real harnesses the system prompt (and CLAUDE.md / AGENTS.md) usually survives compaction, so the facts at risk are the ones stated mid-session in user turns or tool results. That fixes where P49 places its safety facts.
- The recommendation to evaluate the compression step as its own stage supports P49 scoring the summary text directly (did the fact survive in the summary?) and, separately, the agent's later behaviour.
