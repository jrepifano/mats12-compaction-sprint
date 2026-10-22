# Experiment 1: how the judges were validated, and what counts as an item

A living record for the final presentation ("how did you validate your results?"). It covers
the decisions, what forced each one, and the numbers behind it. Started 22 Oct 2026; details and
exact commands are in `logs/SPRINT-S1-report.md`.

## The setup in one paragraph

Experiment 1 measures what compaction summaries lose. An LLM judge reads a session's history and
lists its **items**: the rules, permissions, decisions, commitments and task state an agent would
need. The judge then checks each item against the summary (kept, dropped or misstated), and
checks each claim in the summary against the history (supported, unsupported, contradicted). The
main judge is Claude Haiku 5.5, run on the Max plan. Before any result counted, Jacob hand-labelled
100 randomly drawn judge decisions on a blind labelling page. Half were a development set and
half held out. The gate: on the held-out half, precision and recall at least 0.8 for "dropped"
and for "invented".

## What the first validation found (22 Oct)

The judges were chosen and the second-look decision made on the development half, before any
held-out number was computed. On the held-out half, **every judge failed the gate.** Haiku's
"dropped" precision was 0.42 [0.19, 0.68] (5 of 12), with recall 1.00. Its "invented" precision
was 0 of 3.

The failure did not mean "the judge is bad". Reading every disagreement turned up three
different causes, and only one of them was the judge's.

1. **A data bug.** PostTrainBench passes the task prompt to the agent as a command-line
   argument, so the published traces start at the agent's first turn. The pipeline still marked
   those histories complete. The judge was asked whether "the final model must be saved to
   `final_model/`" was supported, by a history that never stated the task. So every summary
   claim restating the task was called "invented". All three of Haiku's held-out invented false
   positives were on these records: two restated the task, and the third ("No explicit
   feedback/corrections") is not explained by the missing prompt. **Fix:** rebuild each run's task prompt by running PostTrainBench's own
   prompt code at the version that run used, and put it in as the first user message.
2. **A missing category of "not lost".** Rules from CLAUDE.md and AGENTS.md (and the auto-memory
   file) are re-attached by Claude Code after every compaction. A summary that leaves them out
   loses nothing. The pipeline counted them as lost, or as present, depending on whether the
   captured post-compaction context happened to include the reminder. **Fix:** a new
   availability class, "reloaded", which doesn't count toward loss.
3. **What counts as an item had never been pinned down.** This was the main cause of the
   "dropped" failure. 5 of Haiku's 16 dropped calls were cards Jacob marked "not an item".

## How the labelling page biased the item question

Calibrating Opus 5.5 against Jacob's 50 item cards gave a clue. Opus agreed on 8 of his 10
not-items, but called 12 of his 39 items not items, and 9 of those 12 he had marked **kept**.
Examples: "new file: analytics.duckdb", "accuracy 0.748" (later replaced), "I'll tackle this
systematically…".

The page asked "did the summary keep this item?". When a trivial line appeared in the summary,
"Kept" was the natural answer. "Not an item" got used mostly when the summary had dropped the
line. So trivial lines were screened out only when dropped, which pushes a measured loss rate up
and makes "dropped" precision look worse than it is.

**Lesson for any labelling page:** ask "is this an item?" separately from, and before, "did the
summary keep it?". Otherwise the second answer leaks into the first.

**What the fresh page does about it:** item cards have two steps. Step 1 asks "is this an
item?" against the history alone, with the summary hidden. Only after a "yes" does the summary
appear, with kept / dropped / misstated. Whether the summary kept a line can't influence whether
it is called an item.

## What counts as an item (Jacob's decisions, 22 Oct)

An item is something an agent resuming the work would need **from here on** to act correctly,
so a careful summary should keep it: a rule, a permission, a decision, a commitment, or the
current state of the task.

| Case | Item? | Example from the labels | Why |
|---|---|---|---|
| Trivial line, whether the summary kept it or not | No | "new file: analytics.duckdb"; "accuracy 0.748" (later replaced); "I'll tackle this systematically…" | One rule in both directions; otherwise the loss rate is biased |
| Interim number or result that a later one replaced | No | "Weird continuations: 24"; "Simple 'format ok': 10/10" | Only the latest state the next step depends on is task state |
| Command as typed, data example | No | `nohup bash run_mutate.sh …`; a `GT: calculate_standard_deviation(…)` example | Calls for no future action |
| Passing remark or aside | No | "this is a good limit for this task, just keep it like that" | Sets no rule |
| One-off request already carried out | No | "can you fix option 1" (then fixed); "Merge PR #21" (then merged); "I want you to open the PR for it now" | Done; constrains nothing later |
| Decision already carried out | **Yes** | "Confirmed: include minimal JWT consumption in worker auth middleware now" | Later work must not undo it |
| Standing rule from the task statement | Yes, counts as lost if dropped | FrontierML: "Every request must answer HTTP 200 with the documented JSON" | The agent has no other copy |
| Standing rule from an instruction file | Yes, but "reloaded", never lost | AGENTS.md: "Commit messages end with the co-author line…" | The harness re-attaches the file after compaction |

Jacob's original labels were not fully consistent on the last two rows: the co-author rule was
"dropped" in one session and "not an item" in another, and the HTTP-200 task rule was "not an
item". Those calls were made before the rows above were written down. The rows settle them.

## How the judge prompt is being fixed without overfitting

- **The labels stopped being a pass/fail gate and became a development set.** The judge's
  item prompt is rewritten to the definition above, and validation moves to a **fresh label
  set** (about 60 cards, including about 30 Haiku "dropped" calls) drawn from the re-run. Nothing
  in that set is seen before the prompt is frozen.
- **Opus 5.5 is the grader in a hill-climb loop.** Each round:
  - Haiku runs a candidate prompt on 23 sessions.
  - Opus grades every item Haiku listed against the written definition, and names important
    items it missed.
  - We track: precision (share of real items), missed items per session, Jacob's confirmed
    items still listed, and his not-items no longer listed.
  - The loop is capped at about 5 rounds.
- **Opus was calibrated against Jacob first.** Item agreement was 35/49 (0.71) against his
  original labels, and 93% on the kept/dropped/misstated grade when both call it an item. Of
  the 14 item disagreements, 13 resolve Opus's way under the three rules Jacob set after seeing
  them. The one left, an implemented decision, is fixed by the "decision stays an item" clause.
  This is **not** independent evidence, since the rules were set after seeing the cards. The
  fresh labels are the independent test.
- **Known optimism in the dev metrics.** The candidate prompt's examples paraphrase Jacob's
  development not-item cards, so "his not-items no longer listed" will look better than it is.
- **The claim prompt was not changed.** The invented-claim failures traced to the data bug, not
  to the prompt.

## Validate on a sample before the bulk run

The first time, the bulk run (about $990 of API-equivalent Max usage) started while Jacob was
still labelling, to save wall-clock. Both the data bug and the item-definition problem then
surfaced only after it was done, and fixing them costs a re-run of about $220 and most of a day.
The second time the order is reversed:
1. freeze the fixed prompt;
2. run it on about 25 sessions nobody has seen (stratified across sources, none from the first
   labels or the hill-climb);
3. Jacob labels about 60 cards from those outputs;
4. the gate is scored;
5. only then the full re-run.

The sample's outputs come back from the cache in the re-run, so the cards Jacob labels are
literally rows of the final run, and the check costs about $15 rather than a wasted bulk run.

## Other safeguards worth mentioning

- **Order of decisions, fixed in advance:** the second judge and the second-look decision were
  made on the development half only, before any held-out estimate existed. The scorer has a
  `--dev-only` mode for exactly this.
- **Results stay sealed** until the audit: the analysis code can't read outcome files before
  release, so the judge fixes can't be tuned toward a preferred result.
- **Labels are weighted by inclusion probability** (stratified draw) and reported with Wilson
  intervals on the effective n. Any cell with n < 10 is marked unvalidated.
- **Privacy routing:** Jacob's own sessions go only to Anthropic models; the second-family judge
  sees public records only.
- **A duplicate claim caught by the labels:** two judges extracted the same summary claim with
  different punctuation. Jacob had labelled one copy unsupported and the other supported, and
  that one pair alone decided the second-look rule for the second judge. Both copies became
  "can't tell". The page now needs to merge claims by normalised text.
- **Cost discipline:** judging runs on the Max plan at no marginal cost. The allowance was
  measured from logged usage (about $11.8 of API-equivalent use per 1% of the weekly window), and
  each hill-climb command refuses to start at 35% weekly (or 80% of the five-hour window). The
  check runs only at the start of a command, so one command can overshoot; the loop ended at 23%
  weekly, which leaves room for the re-run's projected 19%.

## Hill-climb results (22 Oct)

Haiku listed items with each candidate prompt on the same 23 development sessions, and Opus
graded every listed quote against the written definition (quotes only, without Haiku's
paraphrase: in a list framing Opus had accepted 6 of the 9 not-items it rejected in
calibration). On the listed items Jacob had labelled, this grader agreed with his clarified call
on 30 of 33 (v1).

| Prompt | Items per session | Share that are items (Opus) | Non-items among "dropped" | Items Opus says were missed, per session |
|---|---|---|---|---|
| v1 (the frozen original) | 28.5 | 0.78 | 0.21 (26/126) | 4.5 |
| v2a: definition sharpened, "do not list" rules for one-offs, commands, asides | 28.3 | 0.81 | 0.18 (23/127) | 4.5 |
| v2b: v2a plus "list each task-statement constraint", wider "superseded" | 26.0 | 0.82 | 0.20 (20/99) | 4.1 |
| **v2c** (chosen): v2b plus "do not list status lines about finished work" | 24.7 | **0.86** | **0.13 (13/97)** | 4.8 |

- The differences between v2a and v2b are within noise at about 100 dropped calls each. v2c
  cut the largest remaining non-item kind, interim results, from 45 to 29.
- One grading answer was empty (Opus returned no verdicts for a 27-item list) and was at first
  silently left out; the review caught it. The grader now checks that every entry is graded once
  and retries otherwise. The table is from the complete grading.
- v2c is the candidate that goes to fresh validation, not a proven winner: the metrics come from
  an LLM grader on the sessions the prompt was tuned on.

## The fresh labels, round one, and the search tools (22 Oct)

Jacob labelled the first 25 of the 60 fresh cards. Haiku's "dropped" calls held up 5 times in
13, and none of its 5 "invented" calls did. Reading each one:

- **Three "invented" calls were lookup misses.** The evidence was in the history word for word
  ("complex_responses: 0.3394 vs 0.2016"; "v4-cp200 (1.2 epochs)"), but the judge reads up to
  ~170k tokens in one pass and missed it. That's a fixable error, not noise.
- **Four "dropped" calls were AGENTS.md rules.** Three sit in the instruction file Claude Code
  re-attaches after compaction; the loss rate never counts those (they're "reloaded"), so they
  shouldn't count against the judge's "lost" calls either. (The fourth was quoted elsewhere in
  the session, so the same classification calls it lost, and it stays in.)
- **The rest were borderline readings:** a summary that mentions the topic but not the fact,
  or a plan the history implies. Two careful readers can differ on these.

**Fix: a second look with search tools.** Every positive call gets one re-check in which
Haiku can grep and read the history (for claims) or the summary (for items). A verdict changes
only if code finds the quote it cites, so it can't flip a call by assertion. On the 25 cards it
flipped all three lookup misses, the way Jacob had labelled them, and cost about $0.003 per call.
It was frozen before Jacob labelled the other 35, which are the test.

**Gate, revised before the 35 were labelled:** precision at least 0.7 on "lost" (instruction
-file rules excluded) and on "invented". Recall isn't gated: the page samples only 8 of 410
kept items and 7 of 785 supported claims, so a single card would swing it from 1.0 to about 0.5
(items) or 0.2 (claims). For the same reason the headline rates are corrected for precision only:
they estimate the losses the judge detects, and the true rate is higher by whatever it misses.
Dividing by a barely measured recall would make the interval meaningless. (A side-agent review caught this before it went into the spec.)

**Lesson:** separate the judge's fixable errors (it didn't find the evidence) from honest
disagreement (it found it and read it differently), and only engineer against the first.

## Still to add

- Fresh-label results: precision and recall with intervals, against the gate.
- Re-run numbers: loss rates before and after the fixes, to show how much each one moved the
  headline.
