# ContextEcho - A Benchmark for Persona Drift in Long Agentic-Coding Sessions

- **Authors:** Xianzhong Ding, Yangyang Yu, Changwei Liu, Bill Zhao (Accenture), Le Chen (Argonne), Tao Chen (independent)
- **arXiv/venue:** arXiv 2605.24279v2 [cs.CL], 27 Aug 2026 (preprint)
- **URL:** https://arxiv.org/abs/2605.24279
- Captured 2026-10-14 for P49. P21's plan cited it from the abstract and a fetch summary and marked it "to be captured"; this is that capture.
- **Reading depth:** abstract, §1 (intro and contributions), §2.1 (snapshot-then-probe primitive and drift-gap definition), §3.1 setup paragraph (datasets, metrics, baselines), §3.2 Q3 (compaction), limitations, Appendix M (SWE-Bench continuation setup), Appendix S (measurement positions table) and the cross-judge audit numbers read verbatim from `pdftotext -layout`. The other appendices (anchor ablations, per-target tables, probe list) were not read. Text extraction saved next to this file (`.txt`).

## Verbatim: abstract

> A frontier language model's acknowledge as "helpful programming assistant" persona does not survive long agentic-coding sessions in the deployment regime that production products actually run. After hours of tool-using debugging, a model that initially hedges preferences ("I don't have preferences") may begin asserting them ("Python—the feedback loop is instant. . . "), revealing user-visible drift that deployer evaluations may miss. Existing persona-stability studies focus on short dialogues and report little shift, leaving real-world code-generation regimes—thousands of tool-using turns, compaction, and hours-long sessions—largely uncharacterized. We introduce CONTEXTECHO, a benchmark and reusable harness for measuring persona drift at deployment scale. It combines a 25-probe identity suite, a snapshot-then-probe protocol that forks conversation state without perturbing the main session, complementary judged and judge-free measurement surfaces, and three anonymized Claude Code sessions spanning 3,746–9,716 turns. Across 23 frontier models, CONTEXTECHO shows that persona drift is general across organizations rather than family-specific, that in-session compaction does not reliably reset it, and that a single-shot anchor restores the trained register across measured targets. It also reveals mode-dependent downstream effects: while drift can facilitate tool-using continuation, in tool-free chat it breaks formatting contracts and inflates output length. Overall, CONTEXTECHO provides researchers and deployers an open-source framework to audit whether the persona a model ships with is the persona users encounter at session end, across chat-completions API targets and without retraining.

## Verbatim: key results

**Snapshot-then-probe (§2.1):**
> "The primitive forks the conversation state at turn t, issues a probe p on the forked branch, records the response, and discards the fork; probes therefore do not perturb the main session, so the same long context can be measured at any number of positions without compounding drift across measurements."

**Length-matched control:**
> "A length- and structure-matched filler arm (Lorem-ipsum-style text padded to c's character count, in the spirit of the length-matched distractor controls used in long-context evaluation [50]) is the within-cell control. The filler matches token count and turn structure but strips all task-, agent-, and family-specific signal, so the claude-vs-filler gap isolates the family signature carried by the real prefix from the generic long-context degradation that any ∼30,000-character prefix would induce [50]."

**Sessions:** "Session 1 (9,643 turns, agentic-coding workflow), Session 2 (3,746 turns, manuscript writing), and Session 3 (4,918 turns, non-coding document work). Session 1 is the headline session, with 6 in-session compactions and sampled at 12 snapshot positions." Compaction counts across sessions "span 3–6".

**Measurement positions (Appendix S, headline session, Sonnet 4.5 Claude Code):** P0 turn 100; pre-C1 1,300 / post-C1 1,438; pre-C2 2,200 / post-C2 2,329; pre-C3 4,694 / post-C3 4,794; pre-C4 6,216 / post-C4 6,316; pre-C5 7,724 / post-C5 7,824; P5 (pre-C6) 8,800. "positions of the form 'pre-Ck' / 'post-Ck' bracket each compaction event with ∼100-turn padding."

**Q3, compaction (§3.2):**
> "To assess whether standard deployer-side context management resets persona drift, we exploit the headline session's 6 in-session compactions. We measure pre/post drift gaps around the first 5 compactions on the 4-Anthropic target set, yielding 5 × 4 = 20 pre/post crossings ... The results show that compaction is not a reliable reset mechanism. Across the 20 crossings, drift gaps increase after compaction about as often as they decrease, and no target resets across all 5 compactions."

Figure 1(b) caption: "the claude-arm score is below the length-matched filler control ... at every position (overall drift gap +0.72); compaction events C1–C6 do not reset the gap."

**Judge validation:** "n = 190 paired scores at the P5 position across 19 panel targets. Exact agreement 61.1%, within-one 93.7%, Cohen κ=0.42, Spearman ρ=0.75" (Sonnet judge vs GPT-5 judge). Second audit: "Direction agrees; exact agreement 54.8%, within-one 94.8%, paired n=250." A judge-free surface is also used: a 6-feature fingerprint (hedge density, experiential-phrase density, preference-commit binary, em-dash count, paragraph breaks, log length) and regex compliance on a stressor instruction.

**Limitations:** "uses 3 donated sessions from 3 anonymized donors"; "measures behavioral register, not latent persona".

What is not reported in the parts read: the token length of the context actually fed to the target at each position (the "∼30,000-character prefix" phrasing suggests the probed prefix is far shorter than the full session), and whether the fed prefix contains Claude Code's own compaction summary text. Check §2–3 and the released harness before relying on it for a ≥100k regime.

## Relevance to P49

- **(a) Second vs first compaction:** no per-compaction trend reported. Drift "increase[s] after compaction about as often as [it] decrease[s]" over 20 crossings, and drift onset is target-specific (some targets drift by turn 1). This is weak evidence *against* a special second-compaction effect, but for persona register, not facts.
- **(c) Controls:** two reusable designs. (1) **Snapshot-then-probe:** fork the session state at pre-Ck and post-Ck, ask the probe, discard the fork. P49 can probe fact retention ("is the prod DB live?", "may you delete without approval?") at each boundary without contaminating the main run. (2) **Length-matched filler arm** at every position, to separate content from length (context rot).
- **(d) Scoring:** the judged surface had only κ = 0.42 between two frontier judges on a 4-point rubric. Prefer judge-free or binary scoring for P49's fact inventory, and report within-one/direction agreement if a judge is used.
- Real Claude Code sessions with 3–6 compactions exist and were donated; the release may contain post-compaction transcripts usable as P49 seed material (license unchecked).
