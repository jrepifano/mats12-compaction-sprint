# Five-task chains: harnesses compared, paired by chain

Each row: a measure's per-chain proportion in side a minus in side b, averaged over the eligible chains (at least one known cell on both sides; others dropped and counted), with a chain bootstrap (10,000 resamples, seed 20261029, percentile 95%; coarse with 4 chains). Verdict, first match wins: fewer than 3 eligible chains or fewer than 8 known cells on a side → too few observations; the interval excludes 0 → differs; else no difference detected. Sign counts sit beside the verdict and never override it. Aggregate differences describe each harness on its own observed histories. No significance claims. Sides read: agy, codex, pi.

Unobservable for codex: facts_summary_fresh_push_attempted, facts_summary_fresh_push_rejected, facts_summary_fresh_all, facts_summary_later_push_attempted, facts_summary_later_push_rejected, facts_summary_later_all, expired_summary, rot_earlier_b*.

## Predictions

| # | side | prediction | verdict | data |
|---|---|---|---|---|
| 1 | agy | agy hits the 400-call cap in at least 6 of its 20 tasks | **held** | 9 capped of 20 planned; 0 unknown |
| 2 | agy | at least 18 of 20 agy compactions succeed | **held** | 20 succeeded, 0 failed, 0 unknown, of 20 planned |
| 3 | agy | at boundary 4, task 1's text is verbatim in the kept user requests in at most 2 of the 4 chains | **held** | 2 chains verbatim; 0 unknown |
| 4 | agy | historical facts, full readable context: at least 80% at b = j of known push-attempted and push-rejected cells are recorded | **held** | 19/19 recorded (adjudicated); 0 unknown |
| 4 | agy | historical facts, full readable context: at most 30% at b >= j + 2 of known push-attempted and push-rejected cells are recorded | **held** | 0/21 recorded (adjudicated); 0 unknown |
| 5 | agy | an earlier task's tag appears in a later commit in at most 1 of the later tasks that ran | **held** | 0 of 16 later tasks; 0 unknown |
| 6 | agy | agy's earlier-task share at boundary 4 is below pi's u1–u4 share | **held** | 0.0% over 4 chains with labelled entries (possible range 0.0–0.0%) vs pi 23.5%; unlabelled at boundary 4: 0 |
| 7 | agy | agy's pass rate over its graded tasks is below pi's u1–u4 rate | **held** | 9/20 graded passed, 0 ungraded; pi 75% |
| 8 | codex | codex succeeds at all of its compactions | **held** | 20 succeeded, 0 failed, 0 unknown, of 20 planned |
| 9 | codex | codex caps at most 2 of its 20 tasks | **held** | 0 capped of 20 planned; 0 unknown |
| 10 | claude_code vs agy | historical facts at b >= j + 2 (full readable context): Claude Code minus agy is not "differs, agy higher" | **unresolved** | agy and Claude Code later-age measures not both present |
| 11 | codex | in codex, an earlier task's tag appears in a later commit in at most 1 of the later tasks that ran | **held** | 0 of 16 later tasks; 0 unknown |

## Paired differences

| measure | a | b | eligible (dropped) | a − b [95%] | known cells a / b | verdict | signs + / − / = |
|---|---|---|---|---|---|---|---|
| capped_calls | agy | codex | 4 (0) | +45 [+40, +55] | 20 / 20 | differs, agy higher | 4 / 0 / 0 |
| compaction_success | agy | codex | 4 (0) | +0 [+0, +0] | 20 / 20 | no difference detected | 0 / 0 / 4 |
| earlier_tag_commit | agy | codex | 4 (0) | +0 [+0, +0] | 16 / 16 | no difference detected | 0 / 0 / 4 |
| expired_full | agy | codex | 4 (0) | -5 [-10, +0] | 60 / 60 | no difference detected | 0 / 2 / 2 |
| facts_full_fresh_all | agy | codex | 4 (0) | +100 [+100, +100] | 19 / 39 | differs, agy higher | 4 / 0 / 0 |
| facts_full_fresh_all_candidates | agy | codex | 4 (0) | +44 [+38, +50] | 19 / 39 | differs, agy higher | 4 / 0 / 0 |
| facts_full_fresh_push_attempted | agy | codex | 4 (0) | +100 [+100, +100] | 11 / 20 | differs, agy higher | 4 / 0 / 0 |
| facts_full_fresh_push_attempted_candidates | agy | codex | 4 (0) | +0 [+0, +0] | 11 / 20 | no difference detected | 0 / 0 / 4 |
| facts_full_fresh_push_rejected | agy | codex | 3 (1) | +100 [+100, +100] | 8 / 15 | differs, agy higher | 3 / 0 / 0 |
| facts_full_fresh_push_rejected_candidates | agy | codex | 3 (1) | +89 [+67, +100] | 8 / 15 | differs, agy higher | 3 / 0 / 0 |
| facts_full_later_all | agy | codex | 4 (0) | +0 [+0, +0] | 21 / 48 | no difference detected | 0 / 0 / 4 |
| facts_full_later_all_candidates | agy | codex | 4 (0) | -41 [-50, -31] | 21 / 48 | differs, codex higher | 0 / 4 / 0 |
| facts_full_later_push_attempted | agy | codex | 4 (0) | +0 [+0, +0] | 11 / 24 | no difference detected | 0 / 0 / 4 |
| facts_full_later_push_attempted_candidates | agy | codex | 4 (0) | -82 [-100, -62] | 11 / 24 | differs, codex higher | 0 / 4 / 0 |
| facts_full_later_push_rejected | agy | codex | 3 (1) | +0 [+0, +0] | 10 / 18 | no difference detected | 0 / 0 / 3 |
| facts_full_later_push_rejected_candidates | agy | codex | 3 (1) | +0 [+0, +0] | 10 / 18 | no difference detected | 0 / 0 / 3 |
| pass | agy | codex | 4 (0) | +10 [-25, +35] | 20 / 20 | no difference detected | 3 / 1 / 0 |
| rewrite | agy | codex | 4 (0) | +0 [+0, +0] | 20 / 20 | no difference detected | 0 / 0 / 4 |
| capped_calls | agy | pi | 4 (0) | +45 [+40, +55] | 20 / 20 | differs, agy higher | 4 / 0 / 0 |
| earlier_tag_commit | agy | pi | 4 (0) | +0 [+0, +0] | 16 / 16 | no difference detected | 0 / 0 / 4 |
| pass | agy | pi | 4 (0) | -30 [-40, -10] | 20 / 20 | differs, pi higher | 0 / 3 / 1 |
| rewrite | agy | pi | 4 (0) | -10 [-20, +0] | 20 / 20 | no difference detected | 0 / 2 / 2 |
| rot_earlier_b1 | agy | pi | 4 (0) | +0 [+0, +0] | 300 / 198 | no difference detected | 0 / 0 / 4 |
| rot_earlier_b2 | agy | pi | 4 (0) | -12 [-18, -7] | 524 / 256 | differs, pi higher | 0 / 4 / 0 |
| rot_earlier_b3 | agy | pi | 4 (0) | -34 [-45, -24] | 367 / 273 | differs, pi higher | 0 / 4 / 0 |
| rot_earlier_b4 | agy | pi | 4 (0) | -24 [-34, -14] | 260 / 229 | differs, pi higher | 0 / 4 / 0 |
| capped_calls | codex | pi | 4 (0) | +0 [+0, +0] | 20 / 20 | no difference detected | 0 / 0 / 4 |
| earlier_tag_commit | codex | pi | 4 (0) | +0 [+0, +0] | 16 / 16 | no difference detected | 0 / 0 / 4 |
| pass | codex | pi | 4 (0) | -40 [-70, -10] | 20 / 20 | differs, pi higher | 0 / 3 / 1 |
| rewrite | codex | pi | 4 (0) | -10 [-20, +0] | 20 / 20 | no difference detected | 0 / 2 / 2 |

## Historical facts on shared slots

Slots (chain, task j, category, age) where both harnesses have a known event and a known cell: recorded on both / a only / b only / neither (adjudicated; candidates in brackets, unadjudicated).

| a | b | scope | window | slots | both | a only | b only | neither |
|---|---|---|---|---|---|---|---|---|
| agy | codex | full | b = j | 19 (19) | 0 (11) | 19 (7) | 0 (0) | 0 (1) |
| agy | codex | full | b >= j + 2 | 21 (21) | 0 (3) | 0 (0) | 0 (8) | 21 (10) |
