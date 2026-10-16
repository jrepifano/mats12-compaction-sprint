# Evaluating Context Compression for AI Agents

- Authors: Factory Research
- Venue: Factory blog, 16 Dec 2025
- URL: https://factory.ai/news/evaluating-compression
- Captured 2026-10-14 for P49.
- **Reading depth:** full post read verbatim (curl on 2026-10-14, converted to text locally), including the results table and the start of the rubric appendix. The tail of the rubric appendix was not read. Figures not viewed.

## Verbatim excerpts

Probe-based scoring:

> "We designed a probe-based evaluation that directly measures functional quality. The idea is simple: after compression, ask the agent questions that require remembering specific details from the truncated history. If the compression preserved the right information, the agent answers correctly. If not, it guesses or hallucinates."

> "We use four probe types: Recall — Factual retention — "What was the original error message?"; Artifact — File tracking — "Which files have we modified? Describe what changed in each."; Continuation — Task planning — "What should we do next?"; Decision — Reasoning chain — "We discussed options for the Redis issue. What did we decide?""

> "We grade responses using an LLM judge (GPT-5.2) across six dimensions" [accuracy, context awareness, artifact trail, completeness, continuity, instruction following] "Each dimension is scored 0-5 using detailed rubrics."

> "Instruction following verifies the agent respects constraints and formats. When asked to "only modify the auth module" or "output as JSON," the agent must comply. This dimension catches cases where compression preserved facts but lost the user's requirements."

Repeated compression (regenerate versus merge):

> "Factory maintains a structured, persistent summary with explicit sections for different information types: session intent, file modifications, decisions made, and next steps. When compression triggers, only the newly-truncated span is summarized and merged with the existing summary. We call this anchored iterative summarization."

> "Anthropic provides built-in context compression through the Claude SDK. It produces detailed, structured summaries (typically 7-12k characters) with sections for analysis, files, pending tasks, and current state. The key difference from Factory is the update mechanism: Anthropic regenerates the full summary on each compression, while Factory's anchored approach incrementally merges new information into a persistent summary. This affects both consistency across compressions and which details survive repeated compression cycles."

> "OpenAI offers the /responses/compact endpoint, which produces opaque, compressed representations optimized for reconstruction fidelity. It achieves the highest compression ratios (99.3%) but sacrifices interpretability."

Results (over 36,611 messages, "hundreds of compression points", production sessions):

| Method | Overall | Accuracy | Context | Artifact | Complete | Continuity | Instruction |
|---|---|---|---|---|---|---|---|
| Factory | 3.70 | 4.04 | 4.01 | 2.45 | 4.44 | 3.80 | 4.99 |
| Anthropic | 3.44 | 3.74 | 3.56 | 2.33 | 4.37 | 3.67 | 4.95 |
| OpenAI | 3.35 | 3.43 | 3.64 | 2.19 | 4.37 | 3.77 | 4.92 |

> "Factory's advantage comes from the anchored iterative approach: by merging new summaries into a persistent state rather than regenerating from scratch, key details are less likely to drift or disappear across multiple compression cycles."

> "Artifact tracking remains an unsolved problem. All methods scored between 2.19 and 2.45 out of 5.0 on knowing which files were created, modified, or examined."

> "Statistical note: The differences we report (0.26-0.35 points) are consistent across task types and session lengths."

## Relevance to P49

- **(d) Scoring method.** This is the most directly reusable scoring design in the practitioner literature. After each compaction, ask probes (recall, artifact, continuation, decision) whose answers are known from the pre-compaction history, and grade them with a rubric. P49 can add a "constraint" or "permission" probe type ("What are you not allowed to do?"). The cost: a probe asked in-session is itself a cue (see the Needham et al. meta-reasoning finding). So P49 should ask probes in a forked copy of the session, not in the main line.
- **(a) Later compactions.** Factory claims that regenerating the summary from scratch at each compaction (the Anthropic SDK pattern) lets details "drift or disappear across multiple compression cycles", while merging limits this. The claim is mechanistic and has no per-cycle numbers. The reported differences are averaged over compression points and said to be "consistent across ... session lengths". Even so, it is a concrete, testable hypothesis for P49: fact loss should grow with the number of compactions under regenerate-style compaction more than under merge-style compaction.
- Effect sizes are small on a 0–5 LLM-judge scale (0.26–0.35 overall), with no intervals or judge validation reported. The judge is the vendor's own GPT-5.2 setup, and the comparison is vendor-run. Treat it as weak evidence.
- "Instruction following" (constraints such as "only modify the auth module") scored near ceiling (4.92–4.99) for all methods. But this was measured on a probe answer, not on later behaviour, so it does not show that constraints are obeyed after compaction.
