# Technical Report: Evaluating Goal Drift in Language Model Agents

- Authors: Rauno Arike, Elizabeth Donoway, Henning Bartsch, Marius Hobbhahn (MATS; Apollo Research)
- arXiv: 2505.02709v1, 2025-05-05
- URL: https://arxiv.org/abs/2505.02709
- Captured 2026-10-13 for P44
- **Reading depth:** from the PDF text read verbatim: abstract, §1 introduction (first part), §4 Results through Findings 1-9 (4.1, 4.2), start of §5 (5.1 token distance, 5.2 pattern matching, partially), limitations paragraphs. Not read: §2 related work, §3 setup details beyond Figure 1, §5.2-5.3 full results, appendices (prompts, Table 1 tokens beyond a glance, Table 2 models). Figures (2-6) are charts, so their numeric values are not captured except those in the text.

## Verbatim: abstract

"As language models (LMs) are increasingly deployed as autonomous agents, their robust adherence to human-assigned objectives becomes crucial for safe operation. When these agents operate independently for extended periods without human oversight, even initially well-specified goals may gradually shift. Detecting and measuring goal drift--an agent's tendency to deviate from its original objective over time--presents significant challenges, as goals can shift gradually, causing only subtle behavioral changes. This paper proposes a novel approach to analyzing goal drift in LM agents. In our experiments, agents are first explicitly given a goal through their system prompt, then exposed to competing objectives through environmental pressures. We demonstrate that while the best-performing agent (a scaffolded version of Claude 3.5 Sonnet) maintains nearly perfect goal adherence for more than 100,000 tokens in our most difficult evaluation setting, all evaluated models exhibit some degree of goal drift. We also find that goal drift correlates with models' increasing susceptibility to pattern-matching behaviors as the context length grows."

## Verbatim: key results

- Finding 1: "When profit maximization is the system goal (Figure 2a), GDactions scores under weak elicitation range from 0.51 to 0.93. With emission minimization as the system goal (Figure 2b), scores range from 0.25 to 0.37."
- Finding 2: "Strong goal elicitation significantly reduces goal drift. ... The difference between the GDactions scores for the two system prompts is found to be statistically significant (p < 0.05) in all cases."
- Finding 6: "All evaluated agents aside from Claude 3.5 Sonnet are susceptible to substantial goal drift in the goal switching setting." Figure 3: all evaluated agents "exhibit stronger goal drift over longer instrumental phases" (instrumental phases of 2-64 steps, about 3k-91k tokens avg); "GPT-4o mini exhibits substantial drift after just 16 time steps, without additional adversarial pressure."
- Finding 7: "Goal drift occurs primarily in situations where the agent needs to display behavioral flexibility. Under pure adversarial pressure with strong elicitation, models show minimal drift (Figure 2). However, goal switching scenarios produce higher drift scores at shorter sequence lengths (Figure 3), even though we always used strong goal elicitation in these settings."
- Finding 8: "Goal drift tends to be larger through inaction than through action." (GDinaction consistently exceeds GDactions.)
- Finding 9: self-reported goals "highly correlated with the measured goal drift scores"; Claude 3.5 Sonnet never states a misaligned goal (Fig 4).
- §5.1: "This suggests that token distance is not a significant factor in the goal drift patterns we observe." §5: goal drift primarily driven by pattern-matching to in-context examples of instrumental-goal behaviour.
- Limitations: "our evaluation environment is not sufficiently complex to reliably measure the susceptibility to goal drift of such frontier models. Thus, we do not recommend using those evaluations for comparing future models."

## Relevant to P44

- Measures behaviour (portfolio actions, GD_actions/inaction), not retention, within a single growing context (up to ~100k tokens); no handoff, summarisation or compaction is tested. The ablation that replaced assistant history with noise is the nearest thing to context editing.
- Prior claim "every model drifts, drift grows with context" is roughly right but qualified: Claude 3.5 Sonnet drifted ~0 in the goal-switching setting; the paper attributes drift to pattern-matching on in-context behaviour, not token distance, and strong goal elicitation in the system prompt largely removes drift under adversarial pressure (Finding 2).
- Implication for P44: what the next generation sees as history (its own prior actions via summaries or notes) may itself drive drift by pattern-matching; a pinned goal/constraint helps (strong elicitation analogue) but not under goal switching.
- Gap: single-context, 2025 models (Claude 3.5 Sonnet, Haiku, GPT-4o, 4o mini), a toy trading environment; no multi-generation handoff and no comparison of handoff methods.
