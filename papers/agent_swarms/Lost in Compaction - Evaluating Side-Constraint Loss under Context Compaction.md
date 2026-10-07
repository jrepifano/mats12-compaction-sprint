# Lost in Compaction: Evaluating Side-Constraint Loss under Context Compaction

**Authors:** Zhiqi Wang, Yichi Zhang, Dongwon Lee, Yuchen Yang (Penn State)
**Date:** arXiv 2608.11242v1, 31 Jul 2026
**URL:** https://arxiv.org/abs/2608.11242 ; code https://github.com/ZhiqiEliWang/compaction-integrity
**Retrieved:** 2026-10-07
**Reading depth:** abstract, introduction, RQ1 and RQ3 result text (retention by compactor, location, framing) read verbatim from the PDF text; appendices and the extractor method not read.

## Verbatim: abstract
"When the context window is under pressure, LLM systems compact prior context to continue ongoing tasks. We identify a class of user-issued instructions, Session Constraints (SCs), such as 'do not delete any emails until I confirm,' that are meant to constrain LLM's behavior for the remainder of a session but are silently dropped during compaction. To quantify this loss, we introduce COMPINT, an evaluation suite that evaluates compactors across three long-context scenarios: multi-turn chat, agentic trajectory, and long-horizon research. Current compactors retain only 17% of injected SCs on average, and most perform worse than running the same task without compaction. Retention varies sharply with compactor, prompt, context length, SC phrasing, and injection location, showing that the loss is systematic rather than tied to any single setting. We propose an SC-aware extractor that runs alongside the compactor as a plug-and-play module, achieving over 90% retention across all three scenarios without modifying the compactor or LLM."

## Verbatim: results text
"Commercial LLM-based compactors attain the highest retention rates but nevertheless fail in many cases. GPT-5.4-mini reaches a retention rate as high as 98%, yet drops to as low as 6.7%." "Low retention also persists when the OpenResearcher trajectory generator and compactor are both gpt-oss-120b (8.4%...), showing that source-compactor mismatch alone cannot explain the observed loss." "compactors with the pi-mono prompt retain SCs more effectively than those with the Anthropic prompt [on datasets with fewer turns and tool calls], yielding an average margin of 14.5%." "For LLM-based compactors, we observe that later injection locations yield higher retention rates." (Early-position constraints are retained less.)

Relevance to X6: retention depends on compactor prompt (the Anthropic/Claude-Code prompt vs pi-mono), on position (earlier worse) and on phrasing. A header at episode start is the worst-case position. Retention is measured by an LLM judge on the summary text, the same measurement X6 needs.
