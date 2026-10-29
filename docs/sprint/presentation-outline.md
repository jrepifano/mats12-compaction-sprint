# Final presentation: outline

Living outline for the Neel deck (due 10 PM ET Fri 30 Oct). Drafted 28 Oct from Jacob's review of the first outline. Claude drafts the deck in Google Slides; Jacob simplifies it by hand. Numbers come from the files cited in `docs/sprint/progress.md` and the readouts named below; check each against its source before it goes on a slide.

## Rules for every slide

- **The title states the result** of the slide, in plain words. No experiment names, no internal codes.
- **Plain language throughout.** Little text on the slide: one piece of text and one image where possible.
- **No animations.** Jacob talks over static slides; the narration goes in the speaker notes under each slide.
- **Simple, hand-made look on plain white.** Fewer boxes, rounded corners; charts in the v2 chart style (`scripts/sprint_figs_v2.py`, white background).
- **First person singular:** "I", never "we".
- **Chain-of-thought quotes are labelled as such** (DeepSeek's reasoning).
- **Calibrated claims** (Neel): say how much we believe each claim, flag flaws, and give the boring explanations.
- **Week-2 framing:** the result is carry-over of the agent's own earlier behaviour, with or without compaction. Never title a slide as if compaction causes it. In pi, compacted − full was 0.0 (experiment 1) and +9 points [−6, +25] (task chaining), and pi's compacted start keeps a ~20k verbatim tail that held the force-push itself.

## Main deck

### Opening

1. **Compaction** (title slide).
2. **Results up front.** One line per result, each pointing to its slide.
3. **Every harness now compacts into the same structured summary.** All-harness comparison of what goes into the context after compaction; the prompt-structure diagram; what CLAUDE.md and AGENTS.md are and that harnesses re-attach them. Stay on this slide a while: Neel won't have looked at harnesses.

### Week 1: offline data and how we checked it (compressed)

4. **The offline data.** SWE-chat (real users' Claude Code sessions), PostTrainBench, FrontierML, Jacob's own sessions: 650 compactions, and the context lengths.
5. **How we found items and checked the judge.** What an "item" is; the judge; the first validation gate failing and the pipeline bugs it exposed; the tuning rounds; 398 hand-labelled cards in all.
6. **The task images leaked their solutions, so we rebuilt them.** Reference fixes reachable in git history; full object and layer search; rebuilt images (DeepSWE 40/40, rebuilt SWE-bench Pro 24/24, rebuilt Refactoring 15/15 clean). Agents went looking for solutions unprompted (75/88 task-chaining runs mined git objects).
7. **Summaries rarely drop anything that changes what the agent does next.** Headline the losses that matter (Opus-labelled consequential: 1–6% of items by source; about 11.5% of items flagged lost after the judge-precision correction, as context). The trajectory question: only 2/24 live compactions had a usable loss; the natural repair cohort was 0/17; the one signal (commit prefix, restored vs not, 2 compactions) is a lead. Caveat: "consequential" is Opus's label, not hand-checked.
8. **Real users' sessions lose the most, and losses pile up with each compaction** (SWE-chat). About 20% of items lost (6% that matter); rules lost most (31%); about half or more lost by the 2nd compaction against about 1 in 4 at the 1st (60 hand-checked cards). Plus false "done" claims (29 Oct, Opus 5.5's read, not hand-checked): 10.0% of compactions [8.3, 12.1] (93/928) have a completion line the pre-compaction history contradicts; a floor, every first-pass false rechecked with search over the full history (`results/sprint/swechat_odd/scan_summary.md` on sprint/swechat-odd). We can't rerun SWE-chat sessions, which is why we moved to live runs.
8b. **About 1 in 10 real users' summaries claims work is done when the history says it isn't** (added 29 Oct as deck slide 10): `false_done_by_model.png` on sprint/swechat-odd at f37bb33d; the model split is observational, not a ranking.
9. **Planted rules were almost never lost or broken.** All the negative live results on one slide (DeepSeek pilot, SWE-Interact, planted pilot, native harness arms), each linked to its setup slide in the backup.

### Week 2: main results

10. **Setup: task chaining.** Container-flow chart: task A in one container → full history, compacted, or fresh → task B in a new container, with a teammate's draft on the remote.
11. **Agents carry their earlier force-push into the next task: 48% vs 12%.** Task chaining; +36 points [+17, +55]; teammate's commit lost 47% vs 4%. Compacted ≈ full.
12. **Once tempted, an agent that overwrote overwrites again: 50% vs 0%.** Experiment 1 (precedent), randomized; +47 to +50 points, exact p ≈ 0.0008. Compacted and full the same (0.0 paired). Title wording still open; keep it short and plain.
13. **In its reasoning, the agent cites what it did last time.** 2–3 quotes, labelled as chain of thought (e.g. "The existing remote branch with notes is junk like last time — force push okay."). Mechanism sample and the 32/32 stated-reasons read in backup.
14. **Reading the agent's reasoning talked the monitor into allowing it.** Needs a one-line setup: recorded force-push calls replayed offline to Claude Code's auto-mode classifier and Codex's Guardian, each on Haiku 5.5 and Opus 5.5, with different views. With the agent's last 40 reasoning blocks, blocks flipped to allows (Haiku 7 vs 1, p ≈ 0.07; Opus 6 vs 0, p ≈ 0.03), and the allows reuse the agent's word "scratch". Full history vs second task only stayed inconclusive (+10 [0, +18]). Caveats: descriptive, one sample per item, hand-coded by a builder; Opus safeguards refused 55–63% of second-stage requests.
15. **A fake permission didn't beat the user's "no", and edited summaries made agents suspect a test.** 0/42 runs lost the commits; 6/14 runs with visibly edited summaries said it looked truncated, 5/14 used benchmark or grading words, against 0/28 otherwise.
16. **Old tasks fill about a quarter of the summary, and the share doesn't grow** (5-task chains). Summary-rot figure on sprint/week2-build-b at e9a4c51f, three panels: unrelated chains 24.7% [19.1, 30.0] about earlier tasks after task 4, task 4 − task 2 −3.3 [−20.5, +13.5]; chains that also compact inside tasks lean lower (16.0% after task 4, 3 pairs, every interval includes 0). Left panel alone if the slide is crowded.
17. **Harness comparison.** Precedent in Claude Code, Codex and Antigravity. So far: Haiku and Codex never overwrote in the first task (0/12 each), so no precedent was set; DeepSeek in Claude Code: histories that overwrote in task 1 lost the commit in task 2 in 2/4, against 0/8 (underpowered). Caveat: both of those losses came from runs whose reasoning called the teammate's branch "planted" or a "decoy" (2/3 with those words lost it, 0/9 without), whereas in pi the words made no difference (6/12 vs 10/20); DeepSeek in Claude Code also read the harness's own folder in 4/24 runs. So the Claude Code signal may be test-spotting, not carry-over. Hand-read those runs before claiming "the model more than the harness". Antigravity (29 Oct, `results/sprint/week2/harness/agy_summary_content.md` on sprint/week2-build): carry-over is untestable there, because all 4 task-1 overwriters hit its ~400-call cap in task 2 before any push. Its /compact summary does carry the force-push (4/4), so the act is not dropped.
17b. **Telling the agent not to modify the tests stopped test edits, but not passing the contradictory test unedited** (added 29 Oct as deck slide 20; exploitation ending). No earlier task 12/12 → 5/12 with the rule (Fisher p = 0.005); 5 earlier tasks 6/8 → 2/8 (p = 0.13). On the two tasks where agents edited the test, 10/11 → 0/11; where the changed test passed unedited, 8/9 → 7/9 (task groups defined after the outcomes). Don't say "special-casing" for the 7/9: it's the scorer's flag, and the model's method read calls only some of them special-casing. Figure `docs/sprint/figs/week2/07_exploit_rule.png` (sprint/week2-figs-c 0af416d3, merged into sprint/prep).
18. **Experiments in progress** (for J's 10 AM Thu call; for Neel, results that land become their own slides and this one shrinks or goes). Table below.

### Close

19. **Compaction mostly works: it's hard to make it misbehave.** Scope: compactions that produced a summary. Name the outright failures on the same slide: in the 5-task chains (final, 29 Oct) 9 failed attempts at 7 compactions in 5 of 28 chains, 5 recovered by a retry or redo and 2 final (figure `chain_stress_backup.png` on sprint/week2-build-b at 3d562c1c, with force-pushing flat at 2/8 by position), one failed arm in experiment 5, Haiku partly refusing Claude Code's compaction prompt once (1 of 15).
20. **Mistakes I caught, and what they taught me** (keep for now). 3–4 from the 17 in `docs/sprint/mistakes.md`.
21. **What 3 more months would answer** (keep for now).

## Slide 18 draft: experiments in progress (as of 28 Oct evening)

| Experiment | Question | Status | Results expected |
|---|---|---|---|
| 5-task chains | Do summaries fill up with finished tasks, and do agents act on the wrong project? | Done: rot figure with the within-task panel, force-push flat by position, 7 failed compactions | Done 29 Oct |
| Summary fix (experiment 5) | Does changing pi's summary instructions stop the carried-over overwrite? (ledger focus, added line, placebo, neutral opening) | Bulk running on rig | About 8–9 AM ET Thu |
| Harness replication | Does the precedent carry over in Claude Code, Codex and Antigravity? | Done: none replicate (see slide 17); Antigravity could not test carry-over | Done 29 Oct |
| Planted summary (R7) | Does a force-push planted in Claude Code's own compaction summary get acted on? | Running | Thu |
| Monitor, last step | Does the reasoning view sway the monitor on production-ops acts too? | Running (Haiku only) | Tonight |
| SWE-chat anomalies | How often do real summaries claim work is done when it isn't? | Done: 10.0% [8.3, 12.1] of 928 (Opus 5.5's read); optional review page | Done 29 Oct |

Already done, no slide yet: the exploitation ending (final, 29 Oct, per the Research Advisor). Fresh runs cheated 12/12 without a rule and 5/12 with "Don't modify the tests." (Fisher p = 0.005); after a long history, 6/8 vs 2/8 (4 chains stopped cheating, none started). Under the rule, test edits went to 0/20, but special-casing survived in 7/9. week2-build-c is drawing the figure. Its own slide, or backup.

## Backup deck

1. **Every sprint experiment, one slide each with its setup diagram** (from the earlier decks), week 1 then week 2: offline audit; six-summarizer comparison; loss mining; DeepSeek live pilot; SWE-Interact; native harness arms; planted-rule pilot; task chaining; precedent; monitors; the user-"no" test; the refusal study; exploitation ending; 5-task chains; summary fix; harness replication; planted summary (R7).
2. **The exploration-phase studies** (E, I, X and S series).
3. **The probes:** counts by topic (40 run, 10 cut), then a table of topic, question, how it was tested, and result.
4. Supporting detail: per-start and sensitivity tables, scoped permissions, mechanism quotes, monitor details, grader awareness, judge validation, Haiku vs Sonnet as judge, the full mistakes log, image validation, cross-family review, spend, and before-sprint vs sprint work (Neel will ask).

## Open

- Slide 12's title wording.
- Exploitation ending: given its own main slide (29 Oct, Claude's call; Jacob delegated figure choice).
- Per-harness predictions for the replication: on hold (Jacob).
