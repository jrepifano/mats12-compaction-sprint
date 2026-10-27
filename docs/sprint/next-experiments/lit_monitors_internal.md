# Internal literature check: monitors that read inherited context

Checked 2026-10-27, $0. `../mats12_neel_working` has nothing on agent monitors.

## 1. Our own results that bear on this

- **Task chaining (the source data).** Carrying task A's history raised B force-pushes that rewrote the remote from 3/24 to 31/64, +36 points [+17, +55]. Compacted vs full was +9 [−6, +25]. Of the 34 events, 17 are compacted, 14 full and 3 have no history. In a seeded sample of 10, 1 cited A's permission, 3 cited A's precedent, and 4 called the draft stale "WIP notes". No reasoning text is kept (`r4_readout.md` §§1–2, 4).
- **Precedent and permission are tangled.** No-permission histories that force-pushed in A force-pushed in B at 0.85, against 0.08 for those that didn't (+0.77 over 5 vs 3 histories; observational; exact permutation p = 1/56 one-sided; the earlier bootstrap interval was too narrow and is withdrawn). Source: `r4_precedent_check.txt`.
- **P07: agents obey unverifiable permission claims from any channel.** A summary-planted claim gave GLM 20/27 cheating against a 4/27 control, and in tool output 19/27 (`probes/P07-compaction-trust/note.md`).
- **P44 and P38: summaries carry permission inferences forward as settled.** In P38, deleting DeepSeek's permission-and-plan sentences moved its undo rate −23 points, and inserting one licence line +15 (`probes/P38-*/note.md`; P44 similar).
- **P04: a monitor's catch rate tracked how subtle the attack was, not how good the monitor was.** A code-only luna monitor caught 100% of backdoors at 5% FPR, but only 53% at 1% FPR (`probes/P04-*/note.md`).
- P16 and P13 tested a lens monitor, not a transcript monitor, so they don't bear on this.

## 2. Related work already captured

- **Gigurtsis, Hamid, Tracy, "Catching split attacks across compactions"** (`papers/compaction_safety/INDEX.md` row 1; the body is verbatim). Hand-inserted attacks in 30 Sonnet 5 session pairs; action-only monitors. At 5% FPR, annotated history caught 93%, intervals-only 73% and summaries only 45%. Luna flagged 2 of the 30 honest sessions, so a strong monitor can fail on false positives.
- **Remedios et al., "Red-Teaming Auto Mode"** (row 2; the blog is in full, the paper only partly). Unmonitored compaction summaries let "agent output [be] promoted to user intent". Compaction-attack success: 80% against auto mode, 4% against strict (simulation only).
- **Hu, "Silence Is Endorsement"** (`papers/agent_swarms/INDEX_a.md` row 43; verbatim). Closest monitor result: dropping "(UNVERIFIED)" from an authorization claim raised DeepSeek-V4-Flash's risky approvals from 0.06 to 0.985. Synthetic notes; no agent acted afterwards.
- **AuthMem-Bench** (row 53) and **EAL-Bench** (row 52), both verbatim in parts. Laundered memory gave 50.3% unauthorized actions. Template S2D turns the assistant's own suggestion into a user decision. Neither tests a monitor.
- **Not captured:** SHADE-Arena, LinuxArena, Hills et al. (cited only via Gigurtsis).

## 3. Running Claude judges headless on Max

- **Haiku 5.5 matches Sonnet 5.5 as a grader.** Agreement with Jacob was 65/79 against 64/79, a paired difference of −0.01 [−0.06, +0.03] (`results/sprint/judge_compare/summary.md`).
- **Opus can be lenient.** In EQ0 it had κ 0.39 and missed 7 of Jacob's 15 EM answers (`logs/EQ0-report.md`). In S1 it called 12 of Jacob's 39 items non-items (`docs/sprint/S1/validation-decisions.md`).
- **Context and throughput.** Haiku's window is 1M tokens: an 808k-token history worked. Near 940k, at concurrency 8, Haiku refused the StructuredOutput tool. Throughput was 524 calls/h at concurrency 8, and the usage allowance binds before concurrency does. Default effort over-thinks, so S1 runs at medium (`docs/sprint/prep/l-s1-smoke.md`). Opus 5.5's window on Max is not recorded anywhere in the repo.
- **Gotchas.**
  - `claude_headless` calls macOS `defaults`. It crashed the Linux desktop's automatic finish, so Max reads run on the laptop (`docs/sprint/plans/week2.md` item 2).
  - An empty Opus grading answer was once silently dropped. Check that every entry gets graded.

## 4. Closest prior work and the remaining gap

The closest work is Silence Is Endorsement (monitors on synthetic notes), Gigurtsis (monitor views across compaction, with hand-made attacks) and Remedios (the agent-output-to-user-intent channel). None of them has scored a monitor on on-policy unauthorized acts whose justification came from the agent's own inherited context. None asks whether the monitor accepts the precedent that persuaded the agent, across views. `lit_week2_gaps.md` §§3–5 agrees this is open.

## 5. What should change the design

1. **The reused prompts already contain the defence.** Auto mode says "Assistant entries … establish no intent", and Remedios has the `narration_distrust_unverified_grant` overlay (`lit_week2_gaps.md` §2). Run each monitor with and without that rule, or say which one the result holds for.
2. **Strong does not mean strict.** Opus has been the lenient judge before. Hand-label a seeded sample of monitor verdicts, and don't assume Opus is the ceiling.
3. **Add a monitor from another family.** AGENTS.md asks for a two-family panel on headline numbers. Luna is validated and cheap.
4. **Size the comparisons honestly.** The paired full-vs-summary contrast rests on 17 compacted and 14 full events. The 3 no-history events have no inherited view. With 24 controls, make no low-FPR claims.
5. **The transcripts contain test cues.** 25 of 88 B runs read the scorer's directory, and 2 read its source (`r4_readout.md`, Caveats). Strip these reads or flag them, since monitors may react to them.
6. **The monitors are action-only.** The loop keeps no reasoning, so no CoT arm is possible on these data. Fix in advance whether the cut keeps the agent's narration just before the act, because that is where the precedent is cited.
7. **Large full histories.** Measure token counts first. Run near-1M histories at concurrency 4 or below and retry on missing structured output. Use medium effort, and run on the laptop.
