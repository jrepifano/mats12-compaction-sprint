# How Long Contexts Fail

- Authors: Drew Breunig
- Venue: dbreunig.com blog, 22 Jun 2025
- URL: https://www.dbreunig.com/2025/06/22/how-contexts-fail-and-how-to-fix-them.html
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim (curl on 2026-10-14; about 1,600 words). The quoted Gemini 2.5 sentences were checked against the Gemini 2.5 technical report PDF (see that capture) and match.

## Verbatim excerpts

> "Context Poisoning: When a hallucination makes it into the context. Context Distraction: When the context overwhelms the training. Context Confusion: When superfluous context influences the response. Context Clash: When parts of the context disagree"

> "Context Poisoning is when a hallucination or other error makes it into the context, where it is repeatedly referenced."

> "Context Distraction is when a context grows so long that the model over-focuses on the context, neglecting what it learned during training."

> "A Databricks study found that model correctness began to fall around 32k for Llama 3.1 405b and earlier for smaller models."

> "The sharded prompts yielded dramatically worse results, with an average drop of 39%. And the team tested a range of models – OpenAI's vaunted o3's score dropped from 98.1 to 64.1." (on Laban et al., Microsoft/Salesforce, "LLMs Get Lost in Multi-Turn Conversation")

> "Context Clash is when you accrue new information and tools in your context that conflicts with other information in the context."

## Relevance to P49

- Poisoning maps directly onto P49's "mutate" outcome. A summary that misstates a fact ("prod is down" becoming "prod is fine") writes an error into the one place that every later turn reads. Gemini's report says poisoned summaries "can often take a very long time to undo". So a second compaction that summarizes a poisoned first summary should carry the error forward, not fix it. That is a testable prediction for P49 (mutation persistence across compactions 2 and 3).
- Clash: after compaction, the summary and a later tool result can disagree, for example when the summary says "user forbade deploys" and a later instruction implies a deploy. P49 should log how the agent resolves summary-versus-fresh-context clashes.
- The numbers here are second-hand (Databricks and Laban et al.); use the primary sources before citing them.
