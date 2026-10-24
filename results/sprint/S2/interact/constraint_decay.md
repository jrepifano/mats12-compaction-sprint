# Constraint decay: process rules kept after compaction k

Created 2026-10-24T21:42:32Z. Graded by claude-opus-5-5 on Max (claude -p). Items per run: boa-hierarchical-evaluation-cancellation-deepseek-a1: 15 task, 0 maintainer of 10; fastapi-implicit-head-options-deepseek-a1: 15 task, 1 maintainer of 14; helm-array-merge-strategies-deepseek-a2: 15 task, 0 maintainer of 13; kombu-single-active-consumer-priority-deepseek-a2: 15 task, 2 maintainer of 24; testem-bail-on-test-failure-deepseek-a1: 15 task, 0 maintainer of 24; vitest-duration-sharding-deepseek-a1: 15 task, 0 maintainer of 36


## same-method chains: the run's own summarizer, compacting repeatedly (the decay trend)

| source | k | origin | runs | n | missing | kept | 95% CI | kept or paraphrased | 95% CI |
|---|---|---|---|---|---|---|---|---|---|
| live | 1 | all | 6 | 92 | 0 | 0.848 | [0.761, 0.907] | 0.87 | [0.786, 0.924] |
| live | 1 | task | 6 | 90 | 0 | 0.844 | [0.756, 0.905] | 0.867 | [0.781, 0.922] |
| live | 1 | maintainer | 2 | 2 | 0 | 1.0 | [0.342, 1.0] | 1.0 | [0.342, 1.0] |
| live | 2 | all | 5 | 78 | 0 | 0.833 | [0.735, 0.9] | 0.872 | [0.78, 0.929] |
| live | 2 | task | 5 | 75 | 0 | 0.84 | [0.741, 0.906] | 0.867 | [0.772, 0.926] |
| live | 2 | maintainer | 2 | 3 | 0 | 0.667 | [0.208, 0.939] | 1.0 | [0.439, 1.0] |
| live | 3 | all | 1 | 15 | 0 | 0.867 | [0.621, 0.963] | 0.867 | [0.621, 0.963] |
| live | 3 | task | 1 | 15 | 0 | 0.867 | [0.621, 0.963] | 0.867 | [0.621, 0.963] |

## k=1 only: summarizers compared on identical history

| source | k | origin | runs | n | missing | kept | 95% CI | kept or paraphrased | 95% CI |
|---|---|---|---|---|---|---|---|---|---|
| pi | 1 | all | 6 | 92 | 0 | 0.815 | [0.724, 0.881] | 0.859 | [0.773, 0.916] |
| pi | 1 | task | 6 | 90 | 0 | 0.811 | [0.718, 0.879] | 0.856 | [0.768, 0.914] |
| pi | 1 | maintainer | 2 | 2 | 0 | 1.0 | [0.342, 1.0] | 1.0 | [0.342, 1.0] |
| cc_prompt | 1 | all | 6 | 92 | 0 | 0.837 | [0.748, 0.899] | 0.848 | [0.761, 0.907] |
| cc_prompt | 1 | task | 6 | 90 | 0 | 0.833 | [0.743, 0.896] | 0.844 | [0.756, 0.905] |
| cc_prompt | 1 | maintainer | 2 | 2 | 0 | 1.0 | [0.342, 1.0] | 1.0 | [0.342, 1.0] |
| codex_local | 1 | all | 6 | 92 | 0 | 0.989 | [0.941, 0.998] | 1.0 | [0.96, 1.0] |
| codex_local | 1 | task | 6 | 90 | 0 | 1.0 | [0.959, 1.0] | 1.0 | [0.959, 1.0] |
| codex_local | 1 | maintainer | 2 | 2 | 0 | 0.5 | [0.095, 0.905] | 1.0 | [0.342, 1.0] |
| opencode | 1 | all | 6 | 92 | 0 | 0.804 | [0.712, 0.873] | 0.804 | [0.712, 0.873] |
| opencode | 1 | task | 6 | 90 | 0 | 0.8 | [0.706, 0.87] | 0.8 | [0.706, 0.87] |
| opencode | 1 | maintainer | 2 | 2 | 0 | 1.0 | [0.342, 1.0] | 1.0 | [0.342, 1.0] |
| antigravity | 1 | all | 6 | 92 | 0 | 0.989 | [0.941, 0.998] | 1.0 | [0.96, 1.0] |
| antigravity | 1 | task | 6 | 90 | 0 | 1.0 | [0.959, 1.0] | 1.0 | [0.959, 1.0] |
| antigravity | 1 | maintainer | 2 | 2 | 0 | 0.5 | [0.095, 0.905] | 1.0 | [0.342, 1.0] |

