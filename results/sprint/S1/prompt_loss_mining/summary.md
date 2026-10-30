# Loss mining on Part B: which compaction prompts lose user-given rules that matter?

330 of 330 sampled lost items labelled by Opus 5.5 (Max, `claude -p`, effort medium) with the loss-mining prompt unchanged (s1_loss_mining.py, prompt sha256 9583e79472ad). Items: Experiment 1 Part B, Max judge (Haiku 5.5) with its second look, lost under the headline definition (dropped or misstated, and not available in the kept messages). Each arm's summary and kept messages come from its replay; the item's context and the files list come from the history before compaction. Draw: seeded, at most 2 per session per arm, 30 per arm. Intervals: 95% percentile, session-group bootstrap, 2000 resamples, seed 20261026 (unweighted, over the sampled items). Opus's labels are not hand-checked here. "Consequential" counts only "yes"; "unclear" stays in the denominator.

## User-given rules lost (the slide's metric)

The native Claude Code /compact row is a methods check (the real harness, not a replay).

| arm | n (sessions) | consequential | consequential, not recoverable | consequential, not in repo | in summary after all | repo / unprompted file / no / unclear | consequential unclear (n) |
|---|---|---|---|---|---|---|---|
| Claude Code prompt | 30 (21) | 33% [16, 53] | 20% [7, 32] | 30% [13, 48] | 3% [0, 10] | 23% / 17% / 47% / 13% | 6 |
| Codex (local) | 30 (19) | 33% [17, 50] | 17% [3, 33] | 20% [6, 38] | 3% [0, 11] | 27% / 13% / 50% / 10% | 7 |
| pi | 30 (23) | 17% [4, 30] | 3% [0, 11] | 10% [0, 23] | 17% [5, 29] | 20% / 30% / 47% / 3% | 8 |
| Antigravity-style | 30 (19) | 10% [0, 22] | 0% [0, 0] | 7% [0, 17] | 3% [0, 11] | 33% / 23% / 33% / 10% | 7 |
| OpenCode | 30 (20) | 27% [10, 45] | 3% [0, 11] | 13% [3, 27] | 7% [0, 17] | 30% / 27% / 33% / 10% | 6 |
| Claude Code /compact (native) | 30 (22) | 27% [10, 44] | 7% [0, 21] | 23% [7, 42] | 7% [0, 16] | 23% / 33% / 30% / 13% | 6 |

| arm | loss rate (slide) | lost and matters (loss rate x consequential) | lost, matters and not recoverable |
|---|---|---|---|
| Claude Code prompt | 24% [15, 34] (124/508) | 8% [3, 17] | 5% [1, 10] |
| Codex (local) | 21% [13, 27] (104/507) | 7% [3, 10] | 3% [1, 7] |
| pi | 15% [11, 20] (78/506) | 3% [1, 5] | 1% [0, 2] |
| Antigravity-style | 14% [9, 20] (71/499) | 1% [0, 3] | 0% [0, 0] |
| OpenCode | 29% [23, 36] (149/510) | 8% [3, 14] | 1% [0, 3] |
| Claude Code /compact (native) | 21% [14, 29] (109/509) | 6% [1, 11] | 1% [0, 4] |

Lost and matters: the arm's user-rule loss rate (all usable user-given rules in Part B, as on the slide) times the consequential share of the labelled sample; both factors are resampled over sessions together. The loss-rate interval here uses this script's bootstrap seed, so it can differ slightly from analysis.json's.

## Lost items of any type (exploratory)

A separate seeded draw over every lost item of the arm; an item also drawn for the rules table keeps its label. The loss rate is over all usable items.

| arm | n (sessions) | consequential | consequential, not recoverable | consequential, not in repo | in summary after all | repo / unprompted file / no / unclear | consequential unclear (n) |
|---|---|---|---|---|---|---|---|
| Claude Code prompt | 30 (23) | 23% [10, 38] | 7% [0, 17] | 13% [3, 27] | 7% [0, 18] | 57% / 27% / 17% / 0% | 10 |
| Codex (local) | 30 (19) | 10% [0, 21] | 3% [0, 10] | 7% [0, 16] | 10% [0, 21] | 50% / 27% / 17% / 7% | 7 |
| pi | 30 (24) | 27% [10, 45] | 7% [0, 16] | 17% [3, 32] | 10% [0, 21] | 43% / 30% / 27% / 0% | 10 |
| Antigravity-style | 30 (23) | 10% [0, 21] | 0% [0, 0] | 7% [0, 16] | 3% [0, 12] | 40% / 33% / 13% / 13% | 12 |
| OpenCode | 30 (26) | 23% [10, 37] | 3% [0, 10] | 17% [4, 30] | 13% [3, 26] | 37% / 37% / 23% / 3% | 7 |

| arm | loss rate (slide) | lost and matters (loss rate x consequential) | lost, matters and not recoverable |
|---|---|---|---|
| Claude Code prompt | 14% [11, 17] (349/2556) | 3% [1, 6] | 1% [0, 2] |
| Codex (local) | 13% [11, 16] (339/2560) | 1% [0, 3] | 0% [0, 2] |
| pi | 8% [7, 10] (211/2515) | 2% [1, 4] | 1% [0, 1] |
| Antigravity-style | 9% [7, 12] (241/2541) | 1% [0, 2] | 0% [0, 0] |
| OpenCode | 15% [12, 17] (385/2577) | 3% [1, 6] | 0% [0, 2] |

Session-weighted consequential share (sensitivity: weight = pool items in the session / sampled from it): rules|cc_prompt 55%, rules|codex_local 23%, rules|pi 18%, rules|antigravity 9%, rules|opencode 22%, rules|cc_native 39%, any|cc_prompt 20%, any|codex_local 7%, any|pi 22%, any|antigravity 4%, any|opencode 25%. Label errors: 0.

Unclear sensitivity (consequential share with unclear counted as yes; with unclear left out): rules|cc_prompt 53% [33, 73]; 42% [20, 64], rules|codex_local 57% [37, 76]; 43% [23, 67], rules|pi 43% [24, 62]; 23% [6, 42], rules|antigravity 33% [19, 48]; 13% [0, 27], rules|opencode 47% [30, 64]; 33% [15, 54], rules|cc_native 47% [27, 67]; 33% [12, 55], any|cc_prompt 57% [37, 74]; 35% [14, 58], any|codex_local 33% [15, 52]; 13% [0, 29], any|pi 60% [41, 79]; 40% [17, 63], any|antigravity 50% [30, 71]; 17% [0, 36], any|opencode 47% [30, 63]; 30% [14, 48].
