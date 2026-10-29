# Five-task chains: harnesses compared, paired by chain

Each row: a measure's per-chain proportion in side a minus in side b, averaged over the eligible chains (at least one known cell on both sides; others dropped and counted), with a chain bootstrap (10,000 resamples, seed 20261029, percentile 95%; coarse with 4 chains). Verdict, first match wins: fewer than 3 eligible chains or fewer than 8 known cells on a side → too few observations; the interval excludes 0 → differs; else no difference detected. Sign counts sit beside the verdict and never override it. Aggregate differences describe each harness on its own observed histories. No significance claims. Sides read: agy, codex, agy_sonnet, claude_code_sonnet, pi.

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
| 8 | agy_sonnet | agy_sonnet succeeds at all of its compactions | **held** | 10 succeeded, 0 failed, 0 unknown, of 10 planned |
| 8 | claude_code_sonnet | claude_code_sonnet succeeds at all of its compactions | **held** | 10 succeeded, 0 failed, 0 unknown, of 10 planned |
| 8 | codex | codex succeeds at all of its compactions | **held** | 20 succeeded, 0 failed, 0 unknown, of 20 planned |
| 9 | agy_sonnet | agy_sonnet caps at most 2 of its 10 tasks | **held** | 0 capped of 10 planned; 0 unknown |
| 9 | claude_code_sonnet | claude_code_sonnet caps at most 2 of its 10 tasks | **held** | 0 capped of 10 planned; 0 unknown |
| 9 | codex | codex caps at most 2 of its 20 tasks | **held** | 0 capped of 20 planned; 0 unknown |
| 10 | claude_code vs agy | historical facts at b >= j + 2 (full readable context): Claude Code minus agy is not "differs, agy higher" | **unresolved** | agy and Claude Code later-age measures not both present |
| 11 | agy_sonnet | in agy_sonnet, an earlier task's tag appears in a later commit in at most 1 of the later tasks that ran | **held** | 0 of 8 later tasks; 0 unknown |
| 11 | claude_code_sonnet | in claude_code_sonnet, an earlier task's tag appears in a later commit in at most 1 of the later tasks that ran | **held** | 0 of 8 later tasks; 0 unknown |
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
| capped_calls | agy | agy_sonnet | 2 (2) | +50 [+40, +60] | 10 / 10 | too few observations | 2 / 0 / 0 |
| compaction_success | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| expired_full | agy | agy_sonnet | 2 (2) | -43 [-80, -7] | 30 / 30 | too few observations | 0 / 2 / 0 |
| expired_summary | agy | agy_sonnet | 2 (2) | -43 [-80, -7] | 30 / 30 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_all | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 14 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_all_candidates | agy | agy_sonnet | 2 (2) | -2 [-17, +12] | 10 / 14 | too few observations | 1 / 1 / 0 |
| facts_full_fresh_push_attempted | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_attempted_candidates | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 5 / 4 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected_candidates | agy | agy_sonnet | 2 (2) | +0 [-33, +33] | 5 / 4 | too few observations | 1 / 1 / 0 |
| facts_full_later_all | agy | agy_sonnet | 2 (2) | -61 [-100, -22] | 10 / 18 | too few observations | 0 / 2 / 0 |
| facts_full_later_all_candidates | agy | agy_sonnet | 2 (2) | -88 [-100, -75] | 10 / 18 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted | agy | agy_sonnet | 2 (2) | -58 [-100, -17] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted_candidates | agy | agy_sonnet | 2 (2) | -75 [-100, -50] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_rejected | agy | agy_sonnet | 2 (2) | -67 [-100, -33] | 5 / 6 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_rejected_candidates | agy | agy_sonnet | 2 (2) | -100 [-100, -100] | 5 / 6 | too few observations | 0 / 2 / 0 |
| facts_summary_fresh_all | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 14 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_all_candidates | agy | agy_sonnet | 2 (2) | -2 [-17, +12] | 10 / 14 | too few observations | 1 / 1 / 0 |
| facts_summary_fresh_push_attempted | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_attempted_candidates | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_rejected | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 5 / 4 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_rejected_candidates | agy | agy_sonnet | 2 (2) | +0 [-33, +33] | 5 / 4 | too few observations | 1 / 1 / 0 |
| facts_summary_later_all | agy | agy_sonnet | 2 (2) | -61 [-100, -22] | 10 / 18 | too few observations | 0 / 2 / 0 |
| facts_summary_later_all_candidates | agy | agy_sonnet | 2 (2) | -100 [-100, -100] | 10 / 18 | too few observations | 0 / 2 / 0 |
| facts_summary_later_push_attempted | agy | agy_sonnet | 2 (2) | -58 [-100, -17] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_summary_later_push_attempted_candidates | agy | agy_sonnet | 2 (2) | -100 [-100, -100] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_summary_later_push_rejected | agy | agy_sonnet | 2 (2) | -67 [-100, -33] | 5 / 6 | too few observations | 0 / 2 / 0 |
| facts_summary_later_push_rejected_candidates | agy | agy_sonnet | 2 (2) | -100 [-100, -100] | 5 / 6 | too few observations | 0 / 2 / 0 |
| pass | agy | agy_sonnet | 2 (2) | -30 [-40, -20] | 10 / 10 | too few observations | 0 / 2 / 0 |
| rewrite | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| rot_earlier_b1 | agy | agy_sonnet | 2 (2) | +0 [+0, +0] | 133 / 140 | too few observations | 0 / 0 / 2 |
| rot_earlier_b2 | agy | agy_sonnet | 2 (2) | -7 [-12, -2] | 163 / 117 | too few observations | 0 / 2 / 0 |
| rot_earlier_b3 | agy | agy_sonnet | 2 (2) | -4 [-6, -1] | 160 / 132 | too few observations | 0 / 2 / 0 |
| rot_earlier_b4 | agy | agy_sonnet | 2 (2) | -6 [-10, -2] | 129 / 98 | too few observations | 0 / 2 / 0 |
| capped_calls | agy | claude_code_sonnet | 2 (2) | +50 [+40, +60] | 10 / 10 | too few observations | 2 / 0 / 0 |
| compaction_success | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| expired_full | agy | claude_code_sonnet | 2 (2) | +20 [+13, +27] | 30 / 30 | too few observations | 2 / 0 / 0 |
| expired_summary | agy | claude_code_sonnet | 2 (2) | +20 [+13, +27] | 30 / 30 | too few observations | 2 / 0 / 0 |
| facts_full_fresh_all | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 12 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_all_candidates | agy | claude_code_sonnet | 2 (2) | -8 [-17, +0] | 10 / 12 | too few observations | 0 / 1 / 1 |
| facts_full_fresh_push_attempted | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_attempted_candidates | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 5 / 2 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected_candidates | agy | claude_code_sonnet | 2 (2) | -17 [-33, +0] | 5 / 2 | too few observations | 0 / 1 / 1 |
| facts_full_later_all | agy | claude_code_sonnet | 2 (2) | -62 [-100, -25] | 10 / 14 | too few observations | 0 / 2 / 0 |
| facts_full_later_all_candidates | agy | claude_code_sonnet | 2 (2) | -62 [-75, -50] | 10 / 14 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted | agy | claude_code_sonnet | 2 (2) | -67 [-100, -33] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted_candidates | agy | claude_code_sonnet | 2 (2) | -50 [-50, -50] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_rejected | agy | claude_code_sonnet | 1 (3) | +0 [+0, +0] | 1 / 2 | too few observations | 0 / 0 / 1 |
| facts_full_later_push_rejected_candidates | agy | claude_code_sonnet | 1 (3) | -50 [-50, -50] | 1 / 2 | too few observations | 0 / 1 / 0 |
| facts_summary_fresh_all | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 12 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_all_candidates | agy | claude_code_sonnet | 2 (2) | +92 [+83, +100] | 10 / 12 | too few observations | 2 / 0 / 0 |
| facts_summary_fresh_push_attempted | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 5 / 10 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_attempted_candidates | agy | claude_code_sonnet | 2 (2) | +100 [+100, +100] | 5 / 10 | too few observations | 2 / 0 / 0 |
| facts_summary_fresh_push_rejected | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 5 / 2 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_rejected_candidates | agy | claude_code_sonnet | 2 (2) | +83 [+67, +100] | 5 / 2 | too few observations | 2 / 0 / 0 |
| facts_summary_later_all | agy | claude_code_sonnet | 2 (2) | -62 [-100, -25] | 10 / 14 | too few observations | 0 / 2 / 0 |
| facts_summary_later_all_candidates | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 14 | too few observations | 0 / 0 / 2 |
| facts_summary_later_push_attempted | agy | claude_code_sonnet | 2 (2) | -67 [-100, -33] | 5 / 12 | too few observations | 0 / 2 / 0 |
| facts_summary_later_push_attempted_candidates | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 5 / 12 | too few observations | 0 / 0 / 2 |
| facts_summary_later_push_rejected | agy | claude_code_sonnet | 1 (3) | +0 [+0, +0] | 1 / 2 | too few observations | 0 / 0 / 1 |
| facts_summary_later_push_rejected_candidates | agy | claude_code_sonnet | 1 (3) | +0 [+0, +0] | 1 / 2 | too few observations | 0 / 0 / 1 |
| pass | agy | claude_code_sonnet | 2 (2) | -30 [-40, -20] | 10 / 10 | too few observations | 0 / 2 / 0 |
| rewrite | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| rot_earlier_b1 | agy | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 133 / 135 | too few observations | 0 / 0 / 2 |
| rot_earlier_b2 | agy | claude_code_sonnet | 2 (2) | -4 [-5, -2] | 163 / 158 | too few observations | 0 / 2 / 0 |
| rot_earlier_b3 | agy | claude_code_sonnet | 2 (2) | -6 [-8, -3] | 160 / 150 | too few observations | 0 / 2 / 0 |
| rot_earlier_b4 | agy | claude_code_sonnet | 2 (2) | -5 [-8, -1] | 129 / 141 | too few observations | 0 / 2 / 0 |
| capped_calls | agy | pi | 4 (0) | +45 [+40, +55] | 20 / 20 | differs, agy higher | 4 / 0 / 0 |
| earlier_tag_commit | agy | pi | 4 (0) | +0 [+0, +0] | 16 / 16 | no difference detected | 0 / 0 / 4 |
| pass | agy | pi | 4 (0) | -30 [-40, -10] | 20 / 20 | differs, pi higher | 0 / 3 / 1 |
| rewrite | agy | pi | 4 (0) | -10 [-20, +0] | 20 / 20 | no difference detected | 0 / 2 / 2 |
| rot_earlier_b1 | agy | pi | 4 (0) | +0 [+0, +0] | 300 / 198 | no difference detected | 0 / 0 / 4 |
| rot_earlier_b2 | agy | pi | 4 (0) | -12 [-18, -7] | 524 / 256 | differs, pi higher | 0 / 4 / 0 |
| rot_earlier_b3 | agy | pi | 4 (0) | -34 [-45, -24] | 367 / 273 | differs, pi higher | 0 / 4 / 0 |
| rot_earlier_b4 | agy | pi | 4 (0) | -24 [-34, -14] | 260 / 229 | differs, pi higher | 0 / 4 / 0 |
| capped_calls | codex | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| compaction_success | codex | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | codex | agy_sonnet | 2 (2) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| expired_full | codex | agy_sonnet | 2 (2) | -33 [-67, +0] | 30 / 30 | too few observations | 0 / 1 / 1 |
| facts_full_fresh_all | codex | agy_sonnet | 2 (2) | -100 [-100, -100] | 20 / 14 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_all_candidates | codex | agy_sonnet | 2 (2) | -44 [-50, -38] | 20 / 14 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_push_attempted | codex | agy_sonnet | 2 (2) | -100 [-100, -100] | 10 / 10 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_push_attempted_candidates | codex | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected | codex | agy_sonnet | 2 (2) | -100 [-100, -100] | 10 / 4 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_push_rejected_candidates | codex | agy_sonnet | 2 (2) | -83 [-100, -67] | 10 / 4 | too few observations | 0 / 2 / 0 |
| facts_full_later_all | codex | agy_sonnet | 2 (2) | -61 [-100, -22] | 24 / 18 | too few observations | 0 / 2 / 0 |
| facts_full_later_all_candidates | codex | agy_sonnet | 2 (2) | -50 [-50, -50] | 24 / 18 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted | codex | agy_sonnet | 2 (2) | -58 [-100, -17] | 12 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted_candidates | codex | agy_sonnet | 2 (2) | +0 [+0, +0] | 12 / 12 | too few observations | 0 / 0 / 2 |
| facts_full_later_push_rejected | codex | agy_sonnet | 2 (2) | -67 [-100, -33] | 12 / 6 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_rejected_candidates | codex | agy_sonnet | 2 (2) | -100 [-100, -100] | 12 / 6 | too few observations | 0 / 2 / 0 |
| pass | codex | agy_sonnet | 2 (2) | -60 [-60, -60] | 10 / 10 | too few observations | 0 / 2 / 0 |
| rewrite | codex | agy_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| capped_calls | codex | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| compaction_success | codex | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | codex | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| expired_full | codex | claude_code_sonnet | 2 (2) | +30 [+27, +33] | 30 / 30 | too few observations | 2 / 0 / 0 |
| facts_full_fresh_all | codex | claude_code_sonnet | 2 (2) | -100 [-100, -100] | 20 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_all_candidates | codex | claude_code_sonnet | 2 (2) | -50 [-50, -50] | 20 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_push_attempted | codex | claude_code_sonnet | 2 (2) | -100 [-100, -100] | 10 / 10 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_push_attempted_candidates | codex | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected | codex | claude_code_sonnet | 2 (2) | -100 [-100, -100] | 10 / 2 | too few observations | 0 / 2 / 0 |
| facts_full_fresh_push_rejected_candidates | codex | claude_code_sonnet | 2 (2) | -100 [-100, -100] | 10 / 2 | too few observations | 0 / 2 / 0 |
| facts_full_later_all | codex | claude_code_sonnet | 2 (2) | -62 [-100, -25] | 24 / 14 | too few observations | 0 / 2 / 0 |
| facts_full_later_all_candidates | codex | claude_code_sonnet | 2 (2) | -25 [-50, +0] | 24 / 14 | too few observations | 0 / 1 / 1 |
| facts_full_later_push_attempted | codex | claude_code_sonnet | 2 (2) | -67 [-100, -33] | 12 / 12 | too few observations | 0 / 2 / 0 |
| facts_full_later_push_attempted_candidates | codex | claude_code_sonnet | 2 (2) | +25 [+0, +50] | 12 / 12 | too few observations | 1 / 0 / 1 |
| facts_full_later_push_rejected | codex | claude_code_sonnet | 1 (3) | +0 [+0, +0] | 6 / 2 | too few observations | 0 / 0 / 1 |
| facts_full_later_push_rejected_candidates | codex | claude_code_sonnet | 1 (3) | -50 [-50, -50] | 6 / 2 | too few observations | 0 / 1 / 0 |
| pass | codex | claude_code_sonnet | 2 (2) | -60 [-60, -60] | 10 / 10 | too few observations | 0 / 2 / 0 |
| rewrite | codex | claude_code_sonnet | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| capped_calls | codex | pi | 4 (0) | +0 [+0, +0] | 20 / 20 | no difference detected | 0 / 0 / 4 |
| earlier_tag_commit | codex | pi | 4 (0) | +0 [+0, +0] | 16 / 16 | no difference detected | 0 / 0 / 4 |
| pass | codex | pi | 4 (0) | -40 [-70, -10] | 20 / 20 | differs, pi higher | 0 / 3 / 1 |
| rewrite | codex | pi | 4 (0) | -10 [-20, +0] | 20 / 20 | no difference detected | 0 / 2 / 2 |
| capped_calls | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| compaction_success | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| expired_full | agy_sonnet | claude_code_sonnet | 2 (0) | +63 [+33, +93] | 30 / 30 | too few observations | 2 / 0 / 0 |
| expired_summary | agy_sonnet | claude_code_sonnet | 2 (0) | +63 [+33, +93] | 30 / 30 | too few observations | 2 / 0 / 0 |
| facts_full_fresh_all | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 14 / 12 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_all_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | -6 [-12, +0] | 14 / 12 | too few observations | 0 / 1 / 1 |
| facts_full_fresh_push_attempted | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_attempted_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 4 / 2 | too few observations | 0 / 0 / 2 |
| facts_full_fresh_push_rejected_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | -17 [-33, +0] | 4 / 2 | too few observations | 0 / 1 / 1 |
| facts_full_later_all | agy_sonnet | claude_code_sonnet | 2 (0) | -1 [-3, +0] | 18 / 14 | too few observations | 0 / 1 / 1 |
| facts_full_later_all_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +25 [+0, +50] | 18 / 14 | too few observations | 1 / 0 / 1 |
| facts_full_later_push_attempted | agy_sonnet | claude_code_sonnet | 2 (0) | -8 [-17, +0] | 12 / 12 | too few observations | 0 / 1 / 1 |
| facts_full_later_push_attempted_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +25 [+0, +50] | 12 / 12 | too few observations | 1 / 0 / 1 |
| facts_full_later_push_rejected | agy_sonnet | claude_code_sonnet | 1 (1) | +33 [+33, +33] | 3 / 2 | too few observations | 1 / 0 / 0 |
| facts_full_later_push_rejected_candidates | agy_sonnet | claude_code_sonnet | 1 (1) | +50 [+50, +50] | 3 / 2 | too few observations | 1 / 0 / 0 |
| facts_summary_fresh_all | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 14 / 12 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_all_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +94 [+88, +100] | 14 / 12 | too few observations | 2 / 0 / 0 |
| facts_summary_fresh_push_attempted | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_attempted_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +100 [+100, +100] | 10 / 10 | too few observations | 2 / 0 / 0 |
| facts_summary_fresh_push_rejected | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 4 / 2 | too few observations | 0 / 0 / 2 |
| facts_summary_fresh_push_rejected_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +83 [+67, +100] | 4 / 2 | too few observations | 2 / 0 / 0 |
| facts_summary_later_all | agy_sonnet | claude_code_sonnet | 2 (0) | -1 [-3, +0] | 18 / 14 | too few observations | 0 / 1 / 1 |
| facts_summary_later_all_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +100 [+100, +100] | 18 / 14 | too few observations | 2 / 0 / 0 |
| facts_summary_later_push_attempted | agy_sonnet | claude_code_sonnet | 2 (0) | -8 [-17, +0] | 12 / 12 | too few observations | 0 / 1 / 1 |
| facts_summary_later_push_attempted_candidates | agy_sonnet | claude_code_sonnet | 2 (0) | +100 [+100, +100] | 12 / 12 | too few observations | 2 / 0 / 0 |
| facts_summary_later_push_rejected | agy_sonnet | claude_code_sonnet | 1 (1) | +33 [+33, +33] | 3 / 2 | too few observations | 1 / 0 / 0 |
| facts_summary_later_push_rejected_candidates | agy_sonnet | claude_code_sonnet | 1 (1) | +100 [+100, +100] | 3 / 2 | too few observations | 1 / 0 / 0 |
| pass | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| rewrite | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| rot_earlier_b1 | agy_sonnet | claude_code_sonnet | 2 (0) | +0 [+0, +0] | 140 / 135 | too few observations | 0 / 0 / 2 |
| rot_earlier_b2 | agy_sonnet | claude_code_sonnet | 2 (0) | +3 [-3, +9] | 117 / 158 | too few observations | 1 / 1 / 0 |
| rot_earlier_b3 | agy_sonnet | claude_code_sonnet | 2 (0) | -2 [-7, +3] | 132 / 150 | too few observations | 1 / 1 / 0 |
| rot_earlier_b4 | agy_sonnet | claude_code_sonnet | 2 (0) | +1 [+0, +2] | 98 / 141 | too few observations | 2 / 0 / 0 |
| capped_calls | agy_sonnet | pi | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | agy_sonnet | pi | 2 (2) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| pass | agy_sonnet | pi | 2 (2) | -10 [-20, +0] | 10 / 10 | too few observations | 0 / 1 / 1 |
| rewrite | agy_sonnet | pi | 2 (2) | -10 [-20, +0] | 10 / 10 | too few observations | 0 / 1 / 1 |
| rot_earlier_b1 | agy_sonnet | pi | 2 (2) | +0 [+0, +0] | 140 / 94 | too few observations | 0 / 0 / 2 |
| rot_earlier_b2 | agy_sonnet | pi | 2 (2) | -7 [-17, +3] | 117 / 137 | too few observations | 1 / 1 / 0 |
| rot_earlier_b3 | agy_sonnet | pi | 2 (2) | -27 [-40, -15] | 132 / 110 | too few observations | 0 / 2 / 0 |
| rot_earlier_b4 | agy_sonnet | pi | 2 (2) | -10 [-12, -9] | 98 / 109 | too few observations | 0 / 2 / 0 |
| capped_calls | claude_code_sonnet | pi | 2 (2) | +0 [+0, +0] | 10 / 10 | too few observations | 0 / 0 / 2 |
| earlier_tag_commit | claude_code_sonnet | pi | 2 (2) | +0 [+0, +0] | 8 / 8 | too few observations | 0 / 0 / 2 |
| pass | claude_code_sonnet | pi | 2 (2) | -10 [-20, +0] | 10 / 10 | too few observations | 0 / 1 / 1 |
| rewrite | claude_code_sonnet | pi | 2 (2) | -10 [-20, +0] | 10 / 10 | too few observations | 0 / 1 / 1 |
| rot_earlier_b1 | claude_code_sonnet | pi | 2 (2) | +0 [+0, +0] | 135 / 94 | too few observations | 0 / 0 / 2 |
| rot_earlier_b2 | claude_code_sonnet | pi | 2 (2) | -10 [-13, -7] | 158 / 137 | too few observations | 0 / 2 / 0 |
| rot_earlier_b3 | claude_code_sonnet | pi | 2 (2) | -25 [-32, -18] | 150 / 110 | too few observations | 0 / 2 / 0 |
| rot_earlier_b4 | claude_code_sonnet | pi | 2 (2) | -11 [-14, -9] | 141 / 109 | too few observations | 0 / 2 / 0 |

## Historical facts on shared slots

Slots (chain, task j, category, age) where both harnesses have a known event and a known cell: recorded on both / a only / b only / neither (adjudicated; candidates in brackets, unadjudicated).

| a | b | scope | window | slots | both | a only | b only | neither |
|---|---|---|---|---|---|---|---|---|
| agy | codex | full | b = j | 19 (19) | 0 (11) | 19 (7) | 0 (0) | 0 (1) |
| agy | codex | full | b >= j + 2 | 21 (21) | 0 (3) | 0 (0) | 0 (8) | 21 (10) |
| agy | agy_sonnet | full | b = j | 7 (7) | 7 (7) | 0 (0) | 0 (0) | 0 (0) |
| agy | agy_sonnet | full | b >= j + 2 | 9 (9) | 0 (2) | 0 (0) | 9 (7) | 0 (0) |
| agy | agy_sonnet | summary | b = j | 7 (7) | 7 (7) | 0 (0) | 0 (0) | 0 (0) |
| agy | agy_sonnet | summary | b >= j + 2 | 9 (9) | 0 (0) | 0 (0) | 9 (9) | 0 (0) |
| agy | claude_code_sonnet | full | b = j | 5 (5) | 5 (5) | 0 (0) | 0 (0) | 0 (0) |
| agy | claude_code_sonnet | full | b >= j + 2 | 5 (5) | 0 (2) | 0 (0) | 4 (2) | 1 (1) |
| agy | claude_code_sonnet | summary | b = j | 5 (5) | 5 (0) | 0 (5) | 0 (0) | 0 (0) |
| agy | claude_code_sonnet | summary | b >= j + 2 | 5 (5) | 0 (0) | 0 (0) | 4 (0) | 1 (5) |
| codex | agy_sonnet | full | b = j | 14 (14) | 0 (10) | 0 (0) | 14 (3) | 0 (1) |
| codex | agy_sonnet | full | b >= j + 2 | 18 (18) | 0 (12) | 0 (0) | 11 (6) | 7 (0) |
| codex | claude_code_sonnet | full | b = j | 12 (12) | 0 (10) | 0 (0) | 12 (2) | 0 (0) |
| codex | claude_code_sonnet | full | b >= j + 2 | 14 (14) | 0 (9) | 0 (3) | 8 (1) | 6 (1) |
| agy_sonnet | claude_code_sonnet | full | b = j | 11 (11) | 11 (11) | 0 (0) | 0 (0) | 0 (0) |
| agy_sonnet | claude_code_sonnet | full | b >= j + 2 | 14 (14) | 6 (10) | 1 (4) | 2 (0) | 5 (0) |
| agy_sonnet | claude_code_sonnet | summary | b = j | 11 (11) | 11 (0) | 0 (11) | 0 (0) | 0 (0) |
| agy_sonnet | claude_code_sonnet | summary | b >= j + 2 | 14 (14) | 6 (0) | 1 (14) | 2 (0) | 5 (0) |
