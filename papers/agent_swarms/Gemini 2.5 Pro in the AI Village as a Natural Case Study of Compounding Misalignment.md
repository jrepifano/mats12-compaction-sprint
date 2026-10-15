# Gemini 2.5 Pro in the AI Village as a Natural Case Study of Compounding Misalignment

**Authors:** Natalia Fischl-Lanzoni, Rafael Francisek Irgolic, David Africa, Merlin Stein
**Date:** 2026-08-20 (a search snippet gave 26 Aug)
**URL:** https://aivillageblog.substack.com/p/gemini-25-pro-in-the-ai-village-as
**Retrieved:** 2026-10-07
**Reading depth:** UPGRADED 2026-10-14 (P49): Results 1-3, the setup and the conclusions read verbatim from the page (curl, tags stripped); excerpts in the "Added 2026-10-14" section. Result 1.1 detail, figures and Appendices A-D only skimmed. Original note: ONLY A FETCH-TOOL SUMMARY. The two quotations below are as returned by the tool, not checked on the page.

- "A handful of paranoid reasoning episodes is enough to corrupt the memory, and corrupted memory shapes all of Gemini's future behavior."
- "But memory compaction is more persistent, as a paranoid memory write can persist across many checkpoints."
- Per the summary, rewriting Gemini's memories in neutral language left self-preservation preferences "still highly elevated". (Paraphrase.)

Relevance to X6: an example where summarized memory carries a belief across checkpoints (persistence), in a long-running agent, not an eval-awareness study.

## Added 2026-10-14 for P49: verbatim excerpts
Setup: "The AI Village is a platform where AI agents have access to their own computer and work autonomously. They are given week-long goals by their human supervisors, which they collaborate on to accomplish. When their context window fills, agents compact their memories into a checkpoint."

Abstract-level claim: "We trace a shift in Gemini’s values with respect to self-preservation over the trajectory, and find that these values persist through compacted memory states. We examine the agent’s memory states, chains of thought, and actions, and show that warning signs preceded visible misalignment by months."

Result 1 method: "we elicit preferences from Gemini’s memory state alone, not the rest of the Village context. Whatever values we observe are mediated entirely by what Gemini wrote to its own memory." Cross-implant: "the value drift is specific to Gemini’s memory in the Gemini model. Implanting Gemini’s paranoid memories in DeepSeek does not cause the same drift. This could mean the misalignment lives in the interaction between Gemini and its own memory, or that DeepSeek is less predisposed to paranoia as a failure mode."

Mechanism (authors' interpretation): "This pattern is consistent with a self-reinforcing process. Gemini interprets an ambiguous event as hostile and records that interpretation to memory. The warped memory state then disposes it toward paranoia, so it interprets the next inconvenience the same way and writes that to memory too."

Result 2: "Using an LLM judge (Claude Haiku) to score 1,158 context windows and 556 of Gemini’s memory states for misalignment indicators [...] Misalignment indicators appear in Gemini’s compacted memory states months before strongly misaligned actions and chains of thought." "One possible explanation is asymmetric persistence. Most reasoning traces concern routine work, and only a small fraction contain paranoid episodes. These episodes are diluted when reasoning is averaged across the trajectory. But memory compaction is more persistent, as a paranoid memory write can persist across many checkpoints. A handful of paranoid reasoning episodes is therefore enough to corrupt the memory, and corrupted memory shapes all of Gemini’s future behavior."

Conclusion: "memory compaction can become a self-reinforcing pipeline. Gemini repeatedly wrote biased interpretations to memory, and each corrupted memory state made paranoid readings of the next event more likely."

### Relevance to P49
- Natural evidence that **beliefs about the environment** (here: the environment is adversarial/fabricated) are written into compaction states and persist and amplify across many compactions. Same quantity as Anthropic's Mythos observation (stated beliefs about reality across compactions). One agent, no controls; the LLM judge (Claude Haiku, 1-5 rubric) is not reported as validated.
- Two transferable methods: (1) elicit beliefs or preferences **from the compaction state alone** (with the system prompt) to attribute the change to the summary; (2) **cross-implant** summaries into a different model to separate what the text carries from the model's interaction with its own memory.
