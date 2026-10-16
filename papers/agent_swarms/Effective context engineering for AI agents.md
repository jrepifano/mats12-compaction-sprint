# Effective context engineering for AI agents

- Authors: Anthropic Applied AI team (Prithvi Rajasekaran, Ethan Dixon, Carly Ryan, Jeremy Hadfield; contributions from Rafi Ayub, Hannah Moran, Cal Rueb, Connor Jennings)
- Venue: Anthropic Engineering blog, published 29 Sep 2025
- URL: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (page fetched with curl on 2026-10-14 and converted to text locally; about 3,500 words). Figures not viewed. The excerpts below are verbatim.

## Verbatim excerpts

Context rot and the attention budget:

> "Studies on needle-in-a-haystack style benchmarking have uncovered the concept of context rot: as the number of tokens in the context window increases, the model's ability to accurately recall information from that context decreases."

> "Like humans, who have limited working memory capacity, LLMs have an "attention budget" that they draw on when parsing large volumes of context. Every new token introduced depletes this budget by some amount, increasing the need to carefully curate the tokens available to the LLM."

> "These factors create a performance gradient rather than a hard cliff: models remain highly capable at longer contexts but may show reduced precision for information retrieval and long-range reasoning compared to their performance on shorter contexts."

Compaction (what Claude Code keeps):

> "Compaction is the practice of taking a conversation nearing the context window limit, summarizing its contents, and reinitiating a new context window with the summary. Compaction typically serves as the first lever in context engineering to drive better long-term coherence."

> "In Claude Code, for example, we implement this by passing the message history to the model to summarize and compress the most critical details. The model preserves architectural decisions, unresolved bugs, and implementation details while discarding redundant tool outputs or messages. The agent can then continue with this compressed context plus the five most recently accessed files."

> "The art of compaction lies in the selection of what to keep versus what to discard, as overly aggressive compaction can result in the loss of subtle but critical context whose importance only becomes apparent later. For engineers implementing compaction systems, we recommend carefully tuning your prompt on complex agent traces. Start by maximizing recall to ensure your compaction prompt captures every relevant piece of information from the trace, then iterate to improve precision by eliminating superfluous content."

> "One of the safest lightest touch forms of compaction is tool result clearing, most recently launched as a feature on the Claude Developer Platform."

Structured note-taking:

> "Structured note-taking, or agentic memory, is a technique where the agent regularly writes notes persisted to memory outside of the context window. These notes get pulled back into the context window at later times."

> "After context resets, the agent reads its own notes and continues multi-hour training sequences or dungeon explorations. This coherence across summarization steps enables long-horizon strategies that would be impossible when keeping all the information in the LLM's context window alone."

Sub-agents:

> "Each subagent might explore extensively, using tens of thousands of tokens or more, but returns only a condensed, distilled summary of its work (often 1,000-2,000 tokens)."

When each technique fits:

> "Compaction maintains conversational flow for tasks requiring extensive back-and-forth; Note-taking excels for iterative development with clear milestones; Multi-agent architectures handle complex research and analysis where parallel exploration pays dividends."

## Relevance to P49

- The post gives no numbers, and nothing on repeated compactions or on safety-relevant facts. Its one concrete account of what the Claude Code summary keeps: "architectural decisions, unresolved bugs, and implementation details", plus "the five most recently accessed files". Live-system status, user constraints and permissions are not on that list. That makes them good candidates to be dropped, and it gives P49 a prediction to test.
- "Loss of subtle but critical context whose importance only becomes apparent later" is the failure P49 targets: a fact that is irrelevant when the summary is written and decisive after it.
- The five-recent-files rule means anything kept in a file the agent read recently survives compaction verbatim. P49 should decide whether its key facts live only in the chat or also in a re-attached file, and should record this per fact, since it changes the survival mechanism.
- The post treats context rot (a gradient) and compaction loss as separate mechanisms, which supports P49 having a "long but uncompacted" control.
