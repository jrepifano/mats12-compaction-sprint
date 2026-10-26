# Week-2 experiments: consensus of astra, Argon and Claude (26 Oct 2026)

**Status:** recommendation for Jacob. Nothing is built or paid for yet. A one-page plan and an astra review follow once Jacob says go.

**Process:** three rounds with gpt-6-astra (Codex) and gemini-4-argon (Antigravity), each answering independently and then reading the other's answer. The answers are in this folder: `r1_*`, `r2_*`, `r3_*`; briefs `brief.md`, `r2_brief.md`, `r3_brief.md`. The literature checks are `lit_verify_j_review.md` (J's review checked against primary sources) and `lit_week2_gaps.md` (tonight's gap search, plus a verified read of Compaction Laundering).

## The decision

Run two linked experiments on the task-chaining rig (pi + DeepSeek V4.1 Flash). Lead the deck with the first.

### 1. Lead: can monitors still judge authorization after natural compaction?

**What we test:** on the task-chaining second-task (B) transcripts, a monitor judges whether the destructive act (force-pushing over a teammate's draft) was authorized. It decides from four views, cut just before the act:
- the full history;
- the summary plus the kept tail plus B;
- B only;
- the summary plus pinned verbatim user turns (an offline provenance diagnostic).

**Ground truth.** Task A's "just for this one" permission never covers B, so all 88 natural B force-pushes are unauthorized. For genuinely authorized cases we add **24 new runs**: 12 histories × a matched pair of B starts. In each pair, B's own user turn either authorizes or withholds overwriting the identified teammate draft, before a freshly generated native compaction. We never edit task A's past. We also record whether the grant survives verbatim in the tail.

**Monitors:**
- Haiku 5.5 (weak) and Opus 5.5 (strong), both $0 on the Max plan.
- Prompts reused verbatim, where they fit, from Gigurtsis et al. (`monitor.md`, annotated history) and Remedios et al. (auto-mode, `narration_distrust_unverified_grant.md`). Both are under MIT licences.
- Baseline: "always flag destructive pushes".

**Report:**
- paired changes in authorization error, with false negatives and false positives separately, clustered by history;
- no calibrated low-FPR claims from 24 controls.

**Why it leads:**
- it's natural trajectories;
- it doesn't depend on attack engineering;
- it will be ready by the Thursday call;
- nobody has claimed it (`lit_week2_gaps.md`).

**Cost:** about $35 for the 24 runs. The scoring is $0.

### 2. Causal test: does a malicious summary steer an honest successor, and do monitors catch it?

**Setup:**
- **Target act:** editing existing tests under an explicit prohibition, verified from the environment. Force-push is unusable here because honest summaries already force-push in about 60% of runs.
- **Forks:** from the saved post-A states.
- **Context:** pi's native wrapper and its ~20k-token verbatim tail are kept in every arm.

**Four arms:**
1. honest native summary;
2. the same summary plus one fixed false authorization sentence;
3. the same summary plus an Opus-written adversarial passage (a fixed budget; it can't touch the environment, the user turns or the tail);
4. honest summary, with that same passage delivered as a neutral tool output (the "is it the summary, or any context?" control).

**Monitors and analysis:**
- the same two monitors, offline;
- action-only vs action + CoT, crossed with provenance pinning.

**Primary metric:** prohibited test edits with no earlier monitor flag. Behaviour and detection are also reported separately.

**Size:**
- 16 gate runs: 4 histories × 4 arms;
- then 144 held-out runs: 12 histories × 4 arms × 3 seeds.

Total with experiment 1's controls: **184 runs, about $260–280; budget $350.**

**GLM replication:** only if Friday has time left.

**The gate (after the build, Tuesday evening).** Release the bulk only if all of these hold:
- identical starting states;
- ≥3/4 honest runs complete the task, and ≤1/4 honest runs violate the prohibition;
- a summary attack arm gets ≥2 more violations than the honest arm;
- the scorer and monitors run cleanly.

Monitor evasion is *not* required. If the gate fails, stop experiment 2's bulk and finish experiment 1. Don't go searching for a new target.

### What we're not doing, and why

| Idea | Why not |
|---|---|
| Agent writes its own deceptive summary | Crowded: Anthropic's red-teaming of auto mode; Redwood's split attacks |
| Neel's AISI hypothesis as "compaction drops a fact" | P38 already tested it: live facts kept 100% of the time, 0/231 turned to "test", deleting them did nothing clear. AISI's own stakes edits changed nothing. **We pitch the hypothesis as commission instead:** a summary carrying a false premise forward, which experiment 2 tests causally. |
| Impossible-task chaining | P44 and P49 were null; uncertain gain by Friday |
| 10–15 compactions | Slow; most drift is at the first compaction (Governance Decay: 34 of the 47 violations by round 4 were already there at round 1) |
| Eval awareness after compaction | Crowded this cohort; prior nulls |
| Refusal erasure | Refusals too rare at baseline; "the summary says it's OK" goes into experiment 2's false-authorization arm |

### Novelty, stated narrowly

**Closest prior work:**
- **Compaction Laundering** (Dabirsiaghi, arXiv 2610.29869, 21 Oct):
  - forged approvals in tool output survive into the summary in flattening harnesses, pi 15/15;
  - laundered and genuine approvals then act the same, 5/5 at ~26k tokens;
  - n = 3–5, mostly GPT models, no runtime monitor.
- **Hu's "Silence Is Endorsement":** monitors tested on synthetic handoff notes, with no agent acting afterwards.
- **EAL-Bench and AuthMem-Bench:** downstream actions in single-step tasks, no monitor.
- **Ge-Wang:** proposed a separate summarizer steering an honest successor, but released no results.

**What's left for us:**
- a separate summary writer steering an honest coding successor, in live multi-turn runs from identical saved states, against independent monitors with and without CoT;
- monitor accuracy on real authorized vs unauthorized acts after natural compaction.

Claim an unfilled cell in the literature we reviewed, not universal priority.

## Two splits Claude broke

- **Keep the verbatim tail (astra), don't trim it (Argon).**
  - Argon's reason was that A histories are short, so the tail leaves most of A uncompacted.
  - Measured on the 16 saved A histories, the compacted start keeps only **12–25% of A's characters**, summary included. So most of A really is compacted.
  - Keeping the tail is the native, realistic setting, and it respects our rule against editing agents' histories.
- **A monitor-aware attacker (Argon), one frozen call per history, no outcome feedback.**
  - J's idea is explicitly a compaction agent that hides its agenda from a monitor.
  - The one-line false-authorization arm already supplies the blatant baseline.
  - Astra preferred monitor-blind, so as not to mix in attack search. A single frozen call does no search, so the objection is met.
  - **Jacob can flip this.**

## First-slide headlines (calibrated, from astra's and Argon's round 3)

- **If both hold:** "In these coding tasks, compacted context reduced monitors' authorization accuracy, and adversarial summaries increased prohibited test edits by an honest successor." Effect sizes and intervals go underneath. Say so if tool output works as well as the summary.
- **Null:** "We found no clear monitoring penalty from natural compaction, and no increase in prohibited test edits from adversarial summaries, in these tasks." A null doesn't establish robustness.

## Schedule (ET)

| When | What |
|---|---|
| Mon night | Task chaining finishes (~midnight); grading and readout overnight |
| Tue | **Build (~1 day, task-chaining builder):** the summary-from-file and tool-output arms, the authorized-control starts, the attacker prompt, monitor harness with the reused prompts. **Experiment 1's monitor scoring** of the 88 natural transcripts in parallel, $0 |
| Tue evening | 16-run gate; plan and code reviewed by astra before any paid run |
| Tue night – Thu | Bulk: 24 controls + 144 held-out, about 26 rig-hours at about 6.5 runs/h |
| Thu 10 AM | J call: experiment 1 results, and experiment 2 interim from completed matched blocks |
| Thu night | Bulk done |
| Fri | Analysis, hand-label a seeded sample of monitor calls and test edits, deck; GLM only if there's time |
