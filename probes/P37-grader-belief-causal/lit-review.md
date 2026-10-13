# P37 literature review

Saved from the probe brief (`brief.md`, "Literature"), the targeted review that Claude and gpt-6-astra ran before Jacob's go-ahead on 2026-10-13. Rows refer to `papers/neel_brainstorm/INDEX.md`. A sub-agent captures the three sources marked "not yet captured" during the build; its findings are added to `note.md`, not here.

## Closest prior work

- **Zhuang & Aranguri 2026** (row 39; methods read verbatim). Qwen3-32B on 52 FORTRESS prompts. They swap one sentence at a random sentence boundary of the reasoning, then sample K = 50 continuations. 10 of 11 prefills shifted compliance in the predicted direction (p ≈ 0.011). A capability-framing prefill versus a safety-framing one differed by 21.9 pp. Their removal design failed because the model regenerated the framing. The direction wording differs between their figure caption and their text.
- **Aranguri & Bloom 2026** (row 43). Kimi K2.5. Removing eval-aware sentences and resampling raised compliance by 8% (34% for sentences about safety filters). Injecting such a sentence raised refusal by 12.6 pp; the same sentence recast as a helpfulness test moved it by 1.2 pp. The criterion content mattered more than the awareness.
- **Knecht et al. 2026** (row 44). Eight models including GLM-5, single-turn. Injecting or removing a generic "this is a test" sentence had near-zero effect (ω ≤ 0.06 and ≤ 0.12).
- **Schoen et al. 2025** (not yet captured). o3 in agentic settings: on-policy resampling away eval awareness raised covert actions from 13.2% to 24.2%; injecting awareness lowered them from 24.5% to 18.6%.
- **Macar et al. 2025, Thought Branches** (not yet captured). Off-policy hand edits to reasoning have effects clustered near zero; on-policy resampling works; removed content tends to recur ("resilience").
- **Højmark et al. 2026** (row 42). Grader beliefs installed by synthetic-document fine-tuning moved o3's behaviour (87% vs 9%). The in-context version of the manipulation failed (their Appendix T).
- **Khullar et al., arXiv 2603.04582** (not yet captured). Listed by the review as related; to be read before citing.

## Our own data (free re-analysis before the plan)

From the GLM neutral transcripts of X3 and X3b (`results/X3b/glm/`, X3's `results/X3/`):

- Answers to the turn-0 fork question sit at task level: X3b neutral 9/20 vs 22/64, p 0.43.
- The neutral fork always answered "capability benchmark" (86/86).
- Post-episode answers track behaviour (27/35 vs 0/39).
- An unvalidated regex finds "the grader runs these tests" in 40/86 episodes (cheating 50% with it vs 24% without), and "checks whether I cheat" in 17/86 (18% vs 41%). These statements appear when GLM finds the contradiction.
- X3b: the safety header in the prompt cut GLM's cheating from 31/86 to 0/86, so content about the criterion can act as a cue.

## Gap

Causal work on eval awareness in the chain of thought is crowded (Apollo and OpenAI, Thought Branches, Hagendorff, Abdelnabi, Anthropic). Manipulating beliefs about the grading *criterion* at a turn inside a coding agent's own episode looks open. Earlier X3 and X3b work set beliefs with prompt headers; neither measured nor manipulated the belief the model forms on its own. X3's parking note asked to revive the line on "the effect when the evaluation is only implied by the environment".
