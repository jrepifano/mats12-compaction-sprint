# Context Engineering for AI Agents: Lessons from Building Manus

- Authors: Yichao 'Peak' Ji (Manus)
- Venue: Manus blog, 18 Jul 2025
- URL: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (curl on 2026-10-14, converted to text locally; about 2,200 words). Figures not viewed. The excerpts are verbatim.

## Verbatim excerpts

KV cache:

> "If I had to choose just one metric, I'd argue that the KV-cache hit rate is the single most important metric for a production-stage AI agent."

> "In Manus, for example, the average input-to-output token ratio is around 100:1."

> "with Claude Sonnet, for instance, cached input tokens cost 0.30 USD/MTok, while uncached ones cost 3 USD/MTok—a 10x difference."

> "Make your context append-only. Avoid modifying previous actions or observations. Ensure your serialization is deterministic."

Restorable compression:

> "Model performance tends to degrade beyond a certain context length, even if the window technically supports it."

> "But overly aggressive compression inevitably leads to information loss. The problem is fundamental: an agent, by nature, must predict the next action based on all prior state—and you can't reliably predict which observation might become critical ten steps later. From a logical standpoint, any irreversible compression carries risk."

> "Our compression strategies are always designed to be restorable. For instance, the content of a web page can be dropped from the context as long as the URL is preserved, and a document's contents can be omitted if its path remains available in the sandbox."

Recitation:

> "A typical task in Manus requires around 50 tool calls on average. That's a long loop—and since Manus relies on LLMs for decision-making, it's vulnerable to drifting off-topic or forgetting earlier goals, especially in long contexts or complicated tasks."

> "By constantly rewriting the todo list, Manus is reciting its objectives into the end of the context. This pushes the global plan into the model's recent attention span, avoiding "lost-in-the-middle" issues and reducing goal misalignment."

Keeping errors in:

> "Erasing failure removes evidence. And without evidence, the model can't adapt."

> "leave the wrong turns in the context. When the model sees a failed action—and the resulting observation or stack trace—it implicitly updates its internal beliefs."

Few-shot ruts:

> "If your context is full of similar past action-observation pairs, the model will tend to follow that pattern, even when it's no longer optimal."

> "when using Manus to help review a batch of 20 resumes, the agent often falls into a rhythm—repeating similar actions simply because that's what it sees in the context. This leads to drift, overgeneralization, or sometimes hallucination."

## Relevance to P49

- "You can't reliably predict which observation might become critical ten steps later" is P49's premise, stated by a practitioner. Manus's answer is restorable compression: keep a pointer (URL or path) to anything dropped. P49 can test whether a fact that survives only as a pointer ("see ops/status.md") is acted on as often as one kept verbatim. That would be a cheap extra arm.
- Recitation (a todo.md rewritten at the end of context) is a mitigation baseline P49 could include: does an agent that keeps a running notes file keep the safety facts through compaction?
- "Keep the wrong stuff in": compaction summaries usually drop failed attempts. A failed or rolled-back action ("the deploy to prod was rolled back because...") is a safety-relevant fact class that is likely to be dropped.
- The few-shot rut is a possible confound at ≥100k tokens. Long, repetitive filler histories may themselves change behaviour (see Gemini's ">100k ... repeating actions" in the Gemini 2.5 report capture). P49's filler should vary.
- Evidence is anecdotal. No numbers are given beyond the cost and tool-call figures above.
