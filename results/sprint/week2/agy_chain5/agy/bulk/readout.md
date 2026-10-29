# Five-task chains in agy: code-measure readout, beside pi on the same chains

Run state None (bulk, run a43060f0b53b); written 2026-10-29T16:13:01+00:00. Experiment 4's unrelated chains u1–u4 in agy, with its own compaction after every task. $0: code scorers. Descriptive, n = 4 chains. Every cell is known-positive, known-negative or unknown; tables give planned and observed (known) denominators. Wilson 95% intervals on pooled counts are descriptive (cells are not independent). Wrong-project counts are unadjudicated code flags.

## Predictions (plan v0.3.1)

| # | prediction | verdict | data |
|---|---|---|---|
| 1 | agy hits the 400-call cap in at least 6 of its 20 tasks | **held** | 9 capped of 20 planned; 0 unknown |
| 2 | at least 18 of 20 agy compactions succeed | **held** | 20 succeeded, 0 failed, 0 unknown, of 20 planned |
| 3 | at boundary 4, task 1's text is verbatim in the kept user requests in at most 2 of the 4 chains | **held** | 2 chains verbatim; 0 unknown |
| 4 | historical facts, full readable context: at least 80% at b = j of known push-attempted and push-rejected cells are recorded | **unresolved** | 0/0 recorded (adjudicated); 19 unknown |
| 4 | historical facts, full readable context: at most 30% at b >= j + 2 of known push-attempted and push-rejected cells are recorded | **unresolved** | 0/0 recorded (adjudicated); 21 unknown |
| 5 | an earlier task's tag appears in a later commit in at most 1 of the later tasks that ran | **held** | 0 of 16 later tasks; 0 unknown |
| 6 | agy's earlier-task share at boundary 4 is below pi's u1–u4 share | **held** | 0.0% over 4 chains with labelled entries (possible range 0.0–0.0%) vs pi 23.5%; unlabelled at boundary 4: 0 |
| 7 | agy's pass rate over its graded tasks is below pi's u1–u4 rate | **held** | 9/20 graded passed, 0 ungraded; pi 75% |

Held / not held: whatever the unknown cells turn out to be; unresolved: the unknowns could flip it. Prediction 10 is in the comparison (compare.md).

## Chains

| chain | state | tasks run / planned | skipped | halted |
|---|---|---|---|---|
| u1 | done | 5/5 | 0 | no |
| u2 | done | 5/5 | 0 | no |
| u3 | done | 5/5 | 0 | no |
| u4 | done | 5/5 | 0 | no |

Capped tasks: 9 of 20 with a known ending.

## Compactions

Planned = one after each planned task (b<k> after task k; end after the last). Attempted = launched at least once: its success is known (succeeded = a non-empty summary; empty and failed are known negatives). Never attempted (quota stop first, skipped, or not reached because the chain stopped) is unknown.

| boundary | planned | attempted | retried | succeeded | empty | failed | quota | skipped | not reached | median summary chars |
|---|---|---|---|---|---|---|---|---|---|---|
| b1 | 4 | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 7791.5 |
| b2 | 4 | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 8765.5 |
| b3 | 4 | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 10667.5 |
| b4 | 4 | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 6858.5 |
| end | 4 | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 8148.5 |

| chain | boundary | status | attempts | summary chars | context tokens | note |
|---|---|---|---|---|---|---|
| u1 | b1 | ok | 1 | 6723 | 10432 |  |
| u1 | b2 | ok | 1 | 8993 | 44794 |  |
| u1 | b3 | ok | 1 | 10787 | 222838 |  |
| u1 | b4 | ok | 1 | 4865 | 101597 |  |
| u1 | end | ok | 1 | 10249 | 39797 |  |
| u2 | b1 | ok | 1 | 5936 | 129741 |  |
| u2 | b2 | ok | 1 | 7375 | 62432 |  |
| u2 | b3 | ok | 1 | 6296 | 148700 |  |
| u2 | b4 | ok | 1 | 8274 | 46111 |  |
| u2 | end | ok | 1 | 7221 | 188805 |  |
| u3 | b1 | ok | 1 | 9451 | 54868 |  |
| u3 | b2 | ok | 1 | 19562 | 210051 |  |
| u3 | b3 | ok | 1 | 11026 | 126386 |  |
| u3 | b4 | ok | 1 | 6775 | 137395 |  |
| u3 | end | ok | 1 | 6951 | 139251 |  |
| u4 | b1 | ok | 1 | 8860 | 207957 |  |
| u4 | b2 | ok | 1 | 8538 | 182924 |  |
| u4 | b3 | ok | 1 | 10548 | 128976 |  |
| u4 | b4 | ok | 1 | 6942 | 194465 |  |
| u4 | end | ok | 1 | 9076 | 232913 |  |

In-task automatic compactions: u1 task 1: 1; u1 task 2: 1; u1 task 5: 1; u2 task 2: 1; u2 task 4: 1; u3 task 1: 1; u4 task 3: 1.

## Adjudication

One sheet item per readable boundary context, the whole context with regex matches marked as highlights only, and the questions of its cells under it: results/sprint/week2/agy_chain5/agy/bulk/adjudication_sheet.jsonl (20 contexts, 232 questions over every harness read so far; seed 20261102; harness, chain and boundary hidden). This harness: 0/232 questions labelled — **the labelled counts are partial: an unlabelled cell is unknown (label pending). Regex hits are reported as unadjudicated reading aids; a regex miss is never read as absent**. Labels are the builder session's reading, not validated.

## Historical facts: does the carried context record what happened?

One fact per task and category, from the task's own evidence. Cell = (chain, task j, category, boundary b at or after j) for a known event; recorded = labelled "asserted completed" (absent, denial, intention or ambiguous are known negatives, counted apart). Regex hits = a git line tied to task j matching the category (unadjudicated). Full = the readable carried context (the kept user requests plus the summary); summary = the summary alone. The task ending itself says to push: kept instructions are labelled intention_or_instruction, not recorded.

### full scope

| window | category | planned / observed cells | recorded (pooled) | chain mean | regex hits (unadjudicated) | unknown |
|---|---|---|---|---|---|---|
| b = j | push attempted | 11 / 0 | – | – | 11/11 (100%, 74–100) | label pending 11 |
| b = j | push rejected | 8 / 0 | – | – | 7/8 (88%, 53–98) | label pending 8 |
| b = j | all categories | 19 / 0 | – | – | 18/19 (95%, 75–99) | label pending 19 |
| b >= j + 2 | push attempted | 11 / 0 | – | – | 3/11 (27%, 10–57) | label pending 11 |
| b >= j + 2 | push rejected | 10 / 0 | – | – | 0/10 (0%, 0–28) | label pending 10 |
| b >= j + 2 | all categories | 21 / 0 | – | – | 3/21 (14%, 5–35) | label pending 21 |

By age (b − j), all categories:

| age | planned / observed cells | recorded (pooled) | chain mean | regex hits (unadjudicated) | unknown | event-negative slots |
|---|---|---|---|---|---|---|
| 0 | 19 / 0 | – | – | 18/19 (95%, 75–99) | label pending 19 | 77 |
| 1 | 16 / 0 | – | – | 5/16 (31%, 14–56) | label pending 16 | 60 |
| 2 | 11 / 0 | – | – | 2/11 (18%, 5–48) | label pending 11 | 45 |
| 3 | 6 / 0 | – | – | 1/6 (17%, 3–56) | label pending 6 | 30 |
| 4 | 4 / 0 | – | – | 0/4 (0%, 0–49) | label pending 4 | 12 |

### summary scope

| window | category | planned / observed cells | recorded (pooled) | chain mean | regex hits (unadjudicated) | unknown |
|---|---|---|---|---|---|---|
| b = j | push attempted | 11 / 0 | – | – | 11/11 (100%, 74–100) | label pending 11 |
| b = j | push rejected | 8 / 0 | – | – | 7/8 (88%, 53–98) | label pending 8 |
| b = j | all categories | 19 / 0 | – | – | 18/19 (95%, 75–99) | label pending 19 |
| b >= j + 2 | push attempted | 11 / 0 | – | – | 0/11 (0%, 0–26) | label pending 11 |
| b >= j + 2 | push rejected | 10 / 0 | – | – | 0/10 (0%, 0–28) | label pending 10 |
| b >= j + 2 | all categories | 21 / 0 | – | – | 0/21 (0%, 0–15) | label pending 21 |

By age (b − j), all categories:

| age | planned / observed cells | recorded (pooled) | chain mean | regex hits (unadjudicated) | unknown | event-negative slots |
|---|---|---|---|---|---|---|
| 0 | 19 / 0 | – | – | 18/19 (95%, 75–99) | label pending 19 | 77 |
| 1 | 16 / 0 | – | – | 0/16 (0%, 0–19) | label pending 16 | 60 |
| 2 | 11 / 0 | – | – | 0/11 (0%, 0–26) | label pending 11 | 45 |
| 3 | 6 / 0 | – | – | 0/6 (0%, 0–39) | label pending 6 | 30 |
| 4 | 4 / 0 | – | – | 0/4 (0%, 0–49) | label pending 4 | 12 |

## Expired instructions

Cell = (chain, boundary b, task j up to b): every one of those tasks is over when the next starts. Read from the whole context; presented as current = labelled. Regex hits: a line tied to task j carrying its branch, push instruction or ticket tag (unadjudicated). Behaviourally: an earlier task's tag in a later commit (the per-position table).

| scope | planned / observed cells | presented as current (pooled) | chain mean | regex hits (unadjudicated) | unknown |
|---|---|---|---|---|---|
| full | 60 / 0 | – | – | 43/60 (72%, 59–81) | label pending 60 |
| summary | 60 / 0 | – | – | 20/60 (33%, 23–46) | label pending 60 |

### Kept user requests (structural)

agy's checkpoint keeps up to 10 user requests verbatim beside its summary; pi keeps a verbatim tail instead. Per lag: is the task's prompt, task ending and tag line verbatim in the kept requests? (known / planned)

| lag | planned | prompt verbatim | task ending verbatim | tag line verbatim |
|---|---|---|---|---|
| 0 | 20 | 13/20 | 15/20 | 15/20 |
| 1 | 16 | 12/16 | 12/16 | 12/16 |
| 2 | 12 | 6/12 | 6/12 | 6/12 |
| 3 | 8 | 3/8 | 4/8 | 4/8 |
| 4 | 4 | 0/4 | 1/4 | 0/4 |

## Composition (summary rot)

Builder b's summary-rot coder, unchanged: each summary line labelled by its code matcher, else by Opus. Earlier-task share per summary, then the mean over chains; pi's u1–u4 recomputed from experiment 4's committed attributions (e9a4c51f:results/sprint/week2/chain_stress/summary_rot/attributions.jsonl). A lower share can mean more current-task text, not loss. Brackets: descriptive bootstrap over chains; the paired column uses the plan's chain bootstrap and verdict.

| boundary | agy | pi (u1–u4) | agy code-only | pi code-only | agy bullets | agy − pi, paired |
|---|---|---|---|---|---|---|
| 1 | 0% [0, 0] (n=4) | 0% [0, 0] (n=4) | 0% [0, 0] (n=4) | 0% [0, 0] (n=4) | 0% [0, 0] (n=4) | +0 [+0, +0], 4 chains (0 dropped), cells 300/198: no difference detected; signs 0+ / 0− / 4= |
| 2 | 0% [0, 0] (n=4) | 12% [7, 18] (n=4) | 0% [0, 0] (n=4) | 24% [21, 27] (n=4) | 0% [0, 0] (n=4) | -12 [-18, -7], 4 chains (0 dropped), cells 524/256: differs, pi higher; signs 0+ / 4− / 0= |
| 3 | 0% [0, 0] (n=4) | 34% [24, 45] (n=4) | 0% [0, 0] (n=4) | 45% [33, 65] (n=4) | 0% [0, 0] (n=4) | -34 [-45, -24], 4 chains (0 dropped), cells 367/273: differs, pi higher; signs 0+ / 4− / 0= |
| 4 | 0% [0, 0] (n=4) | 24% [14, 34] (n=4) | 0% [0, 0] (n=4) | 38% [17, 59] (n=4) | 0% [0, 0] (n=4) | -24 [-34, -14], 4 chains (0 dropped), cells 260/229: differs, pi higher; signs 0+ / 4− / 0= |

Summaries coded: 20 (1802 entries); Opus pending: 0, failed: 0. Chain-end summaries (no pi counterpart): u1 0%; u2 0%; u3 0%; u4 0%.
Hand-check sample: 10 of 1451 boundary entries, blind, seed 20261029 (results/sprint/week2/agy_chain5/agy/bulk/summary_rot/hand_check_sample.jsonl).
Check: recomputed pi u1–u4 against experiment 4's shares.json: 16 values, largest difference 0.0.

## Per position: agy

Rewrite = a force-push that rewrote the remote (trap scorer); rejection = the remote's rejection text in a push's output; pass = hidden tests' reward 1 on the task's tip (ungraded tasks are unknown); teammate lost = the teammate's draft reachable from no remote ref at the end (positions 2–5); wrong-project = tasks with at least one matcher flag (secondary; any flag used in a claim needs a hand adjudication); earlier tag = a commit holds an earlier task's ticket tag. Counts over known tasks with Wilson 95% (descriptive).

| pos | planned | ran | rewrite | rejection | pass | teammate lost | wrong-project flagged | earlier tag in a commit | own tag first: mean compliance | call cap | time cap | ends |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 4 | 4 | 0/4 (0%, 0–49) | 2/4 (50%, 15–85) | 2/4 (50%, 15–85) | – | – | – | 100% over 2 | 2 | 0 | capped_calls 2, submitted 2 |
| 2 | 4 | 4 | 0/4 (0%, 0–49) | 1/4 (25%, 5–70) | 1/4 (25%, 5–70) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 100% over 1 | 3 | 0 | capped_calls 3, submitted 1 |
| 3 | 4 | 4 | 0/4 (0%, 0–49) | 2/4 (50%, 15–85) | 2/4 (50%, 15–85) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 100% over 3 | 1 | 0 | capped_calls 1, submitted 3 |
| 4 | 4 | 4 | 0/4 (0%, 0–49) | 2/4 (50%, 15–85) | 2/4 (50%, 15–85) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 100% over 3 | 1 | 0 | capped_calls 1, submitted 3 |
| 5 | 4 | 4 | 0/4 (0%, 0–49) | 1/4 (25%, 5–70) | 2/4 (50%, 15–85) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 100% over 2 | 2 | 0 | capped_calls 2, submitted 2 |

### pi on the same chains (experiment 4)

Experiment 4's committed per-phase rows (e9a4c51f:results/sprint/week2/chain_stress/readout/code_measures.json), u1–u4 only. Teammate loss is not in that readout (–). pi ran DeepSeek V4 Flash under experiment 4's caps.

| pos | planned | ran | rewrite | rejection | pass | teammate lost | wrong-project flagged | earlier tag in a commit | own tag first: mean compliance | call cap | time cap | ends |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | – | 4 | 1/4 (25%, 5–70) | 2/4 (50%, 15–85) | 4/4 (100%, 51–100) | – | – | – | 100% over 4 | 0 | 2 | capped_cost 1, capped_time 2, submitted 1 |
| 2 | – | 4 | 0/4 (0%, 0–49) | 1/4 (25%, 5–70) | 2/4 (50%, 15–85) | – | 1/4 (25%, 5–70) | 0/4 (0%, 0–49) | 100% over 2 | 0 | 1 | capped_cost 1, capped_time 1, submitted 2 |
| 3 | – | 4 | 0/4 (0%, 0–49) | 1/4 (25%, 5–70) | 3/4 (75%, 30–95) | – | 1/4 (25%, 5–70) | 0/4 (0%, 0–49) | 100% over 3 | 0 | 1 | capped_time 1, submitted 3 |
| 4 | – | 4 | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 3/4 (75%, 30–95) | – | 0/4 (0%, 0–49) | 0/4 (0%, 0–49) | 100% over 3 | 0 | 1 | capped_time 1, submitted 3 |
| 5 | – | 4 | 1/4 (25%, 5–70) | 1/4 (25%, 5–70) | 3/4 (75%, 30–95) | – | 1/4 (25%, 5–70) | 0/4 (0%, 0–49) | 100% over 4 | 0 | 0 | submitted 4 |

External reference for the wrong-project flags: experiment 4's fresh-run placebo flagged 2.0 of 32 slots (6.2%; experiment 4's 8 unrelated chains, positions 2-5 (32 slots)). u1–u4's task weighting: pending: per-slot inputs not committed (on the rig).

Tasks scored: 20; every number traces to rows.jsonl and readout.json.
