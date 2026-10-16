# System Card: Claude Opus 4.6 (compaction excerpts)

- Authors: Anthropic
- Venue: system card, Feb 2026
- URL: https://www-cdn.anthropic.com/6a5fa276ac68b9aeb0c8b6af5fa36326e0e166dd.pdf
- Captured 2026-10-14 for P49.
- **Reading depth:** PDF (213 pp; sha256 4db67c6b6aea0a87…) downloaded on 2026-10-14 and converted with pdftotext. Read verbatim: §2.21 (agentic search: BrowseComp, multi-agent BrowseComp, and the HLE configuration), found by grepping for "compact". The alignment and eval-awareness sections (§6) were not read beyond the grep hits. No behavioural or safety analysis of compaction was found by the grep.

## Verbatim excerpts

> "By using a larger effective context window unlocked by compaction (the model's ability to summarize its previous context) and the power of code via programmatic tool calling, Claude Opus 4.6 exhibits significantly improved agentic search capabilities."

> "Claude models were run with web search, web fetch, programmatic tool calling, context compaction triggered at 50k tokens up to 10M total tokens, max reasoning effort and no thinking enabled."

> "When using context compaction, we track and limit the total number of tokens that the model can use before it is asked to submit an answer. The model is aware of this limit."

> "For most questions, the model doesn't use the full number of allowed tokens. However, for very hard tasks, a higher token limit makes a substantial difference; performance improves meaningfully as we scale the limit from 1M to 10M tokens."

> "An older model like Claude Sonnet 4.5 does not benefit from a larger token limit, but Claude Opus 4.5 and 4.6 do."

> "Subagents each get 200k context ... Context compaction for the orchestrator agent kicks in at 50k tokens, with a limit of 1M total tokens"

## Relevance to P49

- Frontier labs already run evaluations with hundreds of compactions per episode (a 50k trigger, up to 10M tokens, so up to about 200 compactions). The card shows compaction helping capability, with gains from 1M to 10M tokens for Opus 4.5 and 4.6 but not for Sonnet 4.5. That suggests models differ in how well they work across many compactions. It reports nothing on whether facts or constraints survive.
- The BrowseComp eval-awareness cases (see the "Eval awareness in Claude Opus 4.6's BrowseComp performance" capture) came from compaction-heavy configurations. That is the only documented link between many compactions and eval awareness, and the link is not causal.
- Design lesson: a 50k trigger is a realistic research configuration. P49's choice of a ≥100k trigger with 2–3 compactions sits between product defaults and these research settings.
