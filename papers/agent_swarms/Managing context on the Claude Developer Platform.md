# Managing context on the Claude Developer Platform

- Authors: Anthropic (product announcement, no byline)
- Venue: Anthropic news / Claude resources, 29 Sep 2025
- URL: https://www.anthropic.com/news/context-management (served at claude.com resources)
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (curl on 2026-10-14, converted to text locally; about 600 words of body). The bar chart image and the bulleted use-case list rendered as images or empty bullets and were not read. The excerpts are verbatim.

## Verbatim excerpts

> "Context editing automatically clears stale tool calls and results from within the context window when approaching token limits. As your agent executes tasks and accumulates tool results, context editing removes stale content while preserving the conversation flow, effectively extending how long agents can run without manual intervention."

> "The memory tool enables Claude to store and consult information outside the context window through a file-based system."

> "Claude Sonnet 4.5 enhances both capabilities with built-in context awareness—tracking available tokens throughout conversations to manage context more effectively."

> "On an internal evaluation set for agentic search, we tested how context management improves agent performance on complex, multi-step tasks. The results demonstrate significant gains: combining the memory tool with context editing improved performance by 39% over baseline. Context editing alone delivered a 29% improvement."

> "In a 100-turn web search evaluation, context editing enabled agents to complete workflows that would otherwise fail due to context exhaustion—while reducing token consumption by 84%."

## Relevance to P49

- The only quantitative numbers in the practitioner set: +39% (memory plus context editing) and +29% (context editing alone) on an internal agentic-search eval; −84% tokens in a 100-turn web-search eval. These are capability numbers on an unnamed internal set, with no intervals and no baseline definition. They say nothing about whether facts are kept.
- Context editing (tool-result clearing) is a second loss channel besides summarization. A live-system status that arrived only as a tool result can be cleared even when no summary is written. P49 should record whether each key fact arrives as user text, assistant text or a tool result, since clearing hits tool results first.
- "Built-in context awareness" (Sonnet 4.5 tracking its token budget) means some subject models know they are near a compaction. That may change behaviour around the boundary (see the Devin capture on "context anxiety").
