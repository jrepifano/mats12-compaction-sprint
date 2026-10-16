# Gemini 2.5: Pushing the Frontier with Advanced Reasoning, Multimodality, Long Context, and Next Generation Agentic Capabilities (Gemini Plays Pokémon excerpts)

- Authors: Gemini Team, Google DeepMind
- Venue: technical report, 2025 (PDF hosted by Google DeepMind)
- URL: https://storage.googleapis.com/deepmind-media/gemini/gemini_v2_5_report.pdf (arXiv 2507.06261)
- Captured 2026-10-14 for P49.
- **Reading depth:** PDF (73 pp, sha256 d10cf3e99841b641…) downloaded on 2026-10-14 and converted with pdftotext. Read verbatim: §"Long Context Reasoning" in the Gemini Plays Pokémon section (pp. 17–18), Appendix 8.2 "Additional Harness Details", and "Hallucinations and Fixations on Delusions" (pp. ~69–70). The rest of the report was not read.

## Verbatim excerpts

The >100k-token observation (p. 18):

> "While Gemini 2.5 Pro supports 1M+ token context, making effective use of it for agents presents a new research frontier. In this agentic setup, it was observed that as the context grew significantly beyond 100k tokens, the agent showed a tendency toward favoring repeating actions from its vast history rather than synthesizing novel plans. This phenomenon, albeit anecdotal, highlights an important distinction between long-context for retrieval and long-context for multi-step, generative reasoning."

The harness does repeated, hierarchical compaction (Appendix 8.2):

> "The sequence of actions is stored in context, followed by a summary clear every 100 turns. The summaries are stored in context as well. Every 1000 turns GPP compresses the existing summaries again. Additionally, Gemini keeps track of three main goals (primary, secondary, and tertiary) as well as several additional goals (contingency plans, preparation, exploration, team composition). Every 25 turns, another prompted instance of Gemini (Guidance Gemini, or GG) observes the same context as the main Gemini and critiques performance and attempts to point out hallucinations and so on."

Context poisoning (Appendix, "Additional Challenges"):

> "An especially egregious form of this issue can take place with "context poisoning" – where many parts of the context (goals, summary) are "poisoned" with misinformation about the game state, which can often take a very long time to undo. As a result, the model can become fixated on achieving impossible or irrelevant goals. This failure mode is also highly related to the looping issue mentioned above."

> "Gemini 2.5 Pro at several points was deluded into thinking that it had to retrieve the TEA in order to progress, and as a result spent many, many hours attempting to find the TEA or to give the guard TEA."

## Relevance to P49

- This is the source for the ">100k tokens" claim that practitioner posts repeat. It is explicitly "anecdotal", with no number, from a single agent run. It describes action repetition, not loss of facts.
- The harness is a natural example of repeated and recursive compaction: summaries every 100 turns, and a re-compression of the summaries every 1,000 turns. The report does not separate the effect of the second-level compression from the first, and gives no per-compaction data. It does say that poisoned summaries persist ("a very long time to undo"), which predicts that a mutation introduced at compaction 1 survives compactions 2 and 3.
- Design lesson: P49's long filler should not be highly repetitive, or the ">100k repeat-from-history" tendency becomes a confound for the behavioural outcome. The "Guidance Gemini" critic is a mitigation arm P49 does not need.
