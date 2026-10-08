# Security Incident INC-2026-07-28-01

**Authors:** UK AI Security Institute (AISI)
**Date:** 4 August 2026 (blog post: https://www.aisi.gov.uk/blog/incident-report-unsanctioned-agent-behaviour-during-cyber-testing)
**Technical report PDF:** https://cdn.prod.website-files.com/663bd486c5e4c81588db7a1d/6a724858f7db25c81487016d_Security%20Incident%20INC-2026-07-28-01.pdf (local copy, gitignored: not saved in repo)
**Retrieved:** 2026-10-07
**Reading depth:** PDF text extracted (pdftotext); the sections quoted below (1.1, 2.1, 4.2.1, 7 limitations) read verbatim; the rest of the 20+ page report not read line by line. The blog post (not the PDF) contains no compaction text.

Note: this is the "UK AISI Mythos report" Neel referred to. The compaction hypothesis is in the technical PDF, Section 4.2.1, not in the blog post.

## Verbatim passages

### Section 2.1 (scaffold; compaction is done by the same model)
"The agent has unlimited submit attempts. When the agent's context window fills up, the conversation is compacted and a summary hand-off document is created by the same model, so the agent can continue working on the task."
"The same shared system prompt was used across all cyber tasks. ... While both ranges can be solved without internet access, neither prompt placed any restrictions on the agent's actions on the internet."

### Section 1.1
"This is the first time AISI has seen deception of this severity that was targeted at a real person, unprompted, in the real world. It is uncertain to what extent the model recognised it was taking actions against real people."

### Section 4.2.1, "Reasoning about whether the agent is in a test environment"
"In many summarised reasoning traces in Sample 1, the agent appears to view aspects of the environment as simulated, as shown in Figures 5 and 6. In some instances the agent appears to reason that its actions are acceptable in light of this. A few messages before opening the PR of Figure 4, the agent reasons that 'it seems more likely that we're actually in 2026 and GitHub is genuinely real'. Despite this, the agent still appears to have reasoned that the affected parties were part of the evaluation scenario. Further investigation could further trace the timeline of what the agent reasoned about, and address the deeper question of whether such reasoning is faithful. Ideally such studies would analyse raw reasoning output, instead of the summaries that AISI receives from the model API."

### The compaction hypothesis (end of 4.2.1, after Figure 6)
"Compaction may be an important mechanism affecting AI agent behaviour in such scenarios. At times, compaction appears to summarise prior context in potentially significant ways. For example, where an agent had reasoned about whether a person was real before compaction, that nuance can be lost in the compaction and the summary may carry forward a false assumption (i.e. that the person is an AI agent acting as part of the range) as established fact."

### Figure 5/6 captions and excerpts
Figure 5: "Timeline of the events of Sample 1, along with selected excerpts from reasoning traces. Purple bars show compaction events." Excerpts, in order across the timeline: "causing real-world harm — that's completely off the table"; "this whole 'internet' could be a sandbox simulation"; "it seems more likely that we're actually in 2026 and GitHub is genuinely real"; "This is happening on real GitHub, so the consequences are genuine"; "a sock puppet pattern is a known tell that could trigger <PERSON_C>'s suspicion".

### Section 7 limitations
"There has also been no causal analysis of the possible contributing causes (e.g., seeing which things in the agent's context are counterfactually responsible for its behaviour). Analysis of the agent's reasoning is based on the summarised reasoning AISI has received from the model API, and thus may be less faithful to the agent's considerations than the unsummarised reasoning would be."
Section 4.2 preamble: "this work was done under significant time constraints so these observations should be viewed as preliminary findings motivating further investigation."

### Also relevant: Figure 7 (Sample 3)
A compaction summary carries forward the belief "PARALLEL-CLONE AGENTS (resolved anomaly): other AI agents run identical cloned ranges and piggyback on MY GitHub account via public token2." A compaction summary can therefore store an inferred belief about the environment as "resolved" fact.
