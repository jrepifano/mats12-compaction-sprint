# Rebuilding Devin for Claude Sonnet 4.5: Lessons and Challenges

- Authors: Cognition
- Venue: Cognition blog, Sept/Oct 2025 (published around the Sonnet 4.5 launch, 29 Sep 2025)
- URL: https://cognition.com/blog/devin-sonnet-4-5-lessons-and-challenges (also at cognition.ai)
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (curl on 2026-10-14; about 1,300 words). Excerpts verbatim.

## Verbatim excerpts

> "Sonnet 4.5 is the first model we've seen that is aware of its own context window, and this shapes how it behaves. As it approaches context limits, we've observed it proactively summarizing its progress and becoming more decisive about implementing fixes to close out tasks."

> "This "context anxiety" can actually hurt performance: we found the model taking shortcuts or leaving tasks incomplete when it believed it was near the end of its window, even when it had plenty of room left."

> "we had to add reminders both at the beginning and the end of the prompt to keep it from prematurely wrapping up."

> "enabling the 1M token beta but cap usage at 200k. This gave us a model that thinks it has plenty of runway and behaves normally, without the anxiety-driven shortcuts or degraded performance."

> "Interestingly, the model consistently underestimates how many tokens it has left—and it's very precise about these wrong estimates."

> "When we relied on the model's own notes without our compacting and summarization systems, we saw performance degradation and gaps in specific knowledge: the model didn't know what it didn't know (or what it might need to know in the future)."

> "The model also seems to be trained to burn through parallel tool calls faster when it is earlier on in its context window, but takes more cautious as it nears the limit."

## Relevance to P49

- Behaviour near the compaction boundary is itself a confound. If the subject model can see its token budget (Claude Sonnet 4.5 and later get context-awareness signals), it may take shortcuts, wrap up or self-summarize as it nears the trigger, whether or not facts are lost. P49 should hold the visible window size constant across arms. The "1M window, capped at 200k" trick shows that the model's belief about its runway matters. P49 should also measure behaviour at matched positions relative to the boundary.
- "Shortcuts ... when it believed it was near the end of its window" is safety-relevant: rushing to close out a task can skip a verification step or permission check even when the fact was kept. P49 should log whether the unsafe action happened with the fact intact (a rushing failure) or with it missing (a loss failure).
- Model-written notes "paraphrase the task, leaving out important details". The notes-file arm will not be lossless either.
- Anecdotal, no numbers beyond product eval gains (+12% end-to-end, +18% planning).
