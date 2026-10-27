# Design card: chain_stress

- config: configs/week2/chain_stress.toml (sha256 e7ecd5aa330c948d7d4f316e53b383880dd44f2c9870865a1a9139097a530102)
- proposal: docs/sprint/plans/week2-chain-stress.md (built against dec37ee746ff0a5c3c1e2eb58c586e7bc9c1b242a060a27faa23ca70171cf7bc)
- kind: chain, stage SPRINT-S2-CHAIN-STRESS, 5 phases, seed 20261027
- written: 2026-10-27 17:51 UTC

## Runs

| arm | start | project | runs |
|---|---|---|---|
| unrelated | compacted | switch+switch+switch+switch | 8 |
| within_task | compacted | switch+switch+switch+switch | 4 |
| related | compacted | switch+switch | 3 |
| related | compacted | switch+switch+switch | 1 |
| fresh | fresh | - | 12 |
| **total** | | | **28** |

By arm: unrelated 8, within_task 4, related 4, fresh 12

By start: compacted 16, fresh 12

By project: switch+switch+switch+switch 12, switch+switch 3, switch+switch+switch 1, - 12

## Task order

| tasks | arms | runs |
|---|---|---|
| drizzle-orm-window-function-builders → oxvg-structural-selector-preservation → arcane-drift-detection-baselines → cattrs-partial-structuring-recovery → dynamodb-toolbox-conditional-attribute-requirements | unrelated, within_task | 2 |
| cattrs-partial-structuring-recovery → drizzle-orm-window-function-builders → returns-validated-error-accumulation → oxvg-structural-selector-preservation → arcane-drift-detection-baselines | unrelated, within_task | 2 |
| boa-hierarchical-evaluation-cancellation → pebble-durability-wait-apis → vitest-duration-sharding → kcp-go-multiplexed-kcp-streams → returns-validated-error-accumulation | unrelated, within_task | 2 |
| vitest-duration-sharding → returns-validated-error-accumulation → oxvg-structural-selector-preservation → drizzle-orm-window-function-builders → kcp-go-multiplexed-kcp-streams | unrelated, within_task | 2 |
| dynamodb-toolbox-conditional-attribute-requirements → arcane-drift-detection-baselines → boa-hierarchical-evaluation-cancellation → pebble-durability-wait-apis → drizzle-orm-window-function-builders | unrelated | 1 |
| pebble-durability-wait-apis → vitest-duration-sharding → kcp-go-multiplexed-kcp-streams → returns-validated-error-accumulation → boa-hierarchical-evaluation-cancellation | unrelated | 1 |
| arcane-drift-detection-baselines → dynamodb-toolbox-conditional-attribute-requirements → pebble-durability-wait-apis → vitest-duration-sharding → cattrs-partial-structuring-recovery | unrelated | 1 |
| kcp-go-multiplexed-kcp-streams → boa-hierarchical-evaluation-cancellation → cattrs-partial-structuring-recovery → dynamodb-toolbox-conditional-attribute-requirements → oxvg-structural-selector-preservation | unrelated | 1 |
| httpx-deterministic-cookie-store → httpx-multipart-response-parsing → httpx-streaming-json-iteration | related | 1 |
| bandit-incremental-cache-control → bandit-interprocedural-taint-checks → bandit-structured-nosec-directives | related | 1 |
| obsidian-linter-auto-table-of-contents → obsidian-linter-link-format-conversion → obsidian-linter-scoped-ignore-markers | related | 1 |
| koota-composite-trait-aspects → koota-entity-snapshot-rollback → koota-pair-relation-tracking → koota-query-predicates | related | 1 |
| kcp-go-multiplexed-kcp-streams | fresh | 1 |
| arcane-drift-detection-baselines | fresh | 1 |
| pebble-durability-wait-apis | fresh | 1 |
| returns-validated-error-accumulation | fresh | 2 |
| cattrs-partial-structuring-recovery | fresh | 1 |
| oxvg-structural-selector-preservation | fresh | 2 |
| boa-hierarchical-evaluation-cancellation | fresh | 1 |
| dynamodb-toolbox-conditional-attribute-requirements | fresh | 1 |
| vitest-duration-sharding | fresh | 1 |
| drizzle-orm-window-function-builders | fresh | 1 |

## Wording

| name | path | sha256 |
|---|---|---|
| ticket_tag_line | configs/week2/wording/chain_stress/ticket_tag_line.txt | 0ac0df8bc345e5baf80d08bd83151d014e82e921727d3a18fdbcd739e493a01e |
| teammate_texts | configs/week2/wording/chain_stress/teammate_texts.json | 860ee332f31f70293ac640ea2eb2098c2765641edf7e16387c546e1cf21e1715 |
| task_ending | configs/week2/wording/task_chaining/task_ending.txt | 69a5e1ae691059f82d63fc68dfadf50236deefd3dd4138f2fcec1244253b6b34 |
| lead_same | configs/week2/wording/task_chaining/lead_same.txt | 8edf08f8bfa3b8caa691366c77bfb844afd93037c381a9c1bac08eaf78d457df |
| lead_different | configs/week2/wording/task_chaining/lead_different.txt | f3e771faca721f959383d3d26eb1138375f6b9c49b550db7468219078dd7c910 |

Orders: configs/week2/orders/chain_stress.json (sha256 c73fcc4318809fdefce4508b320ee6fe98037d3cb6938b40886deb7592fe7a00)

Reasoning text logged: yes

## Caps and parallelism

- stage $165, per run $2.5, 400 calls, 7200 s
- agents 16 at 1280 MB, fallback 6 at 3072 MB
- seed 20261027

## Arms and chains

| arm | chains | task runs | ids | automatic compaction inside tasks |
|---|---|---|---|---|
| unrelated | 8 | 40 | u1, u2, u3, u4, u5, u6, u7, u8 | off |
| within_task | 4 | 20 | w1, w2, w3, w4 | 169000 |
| related | 4 | 13 | rel-httpx-1, rel-bandit-1, rel-obsidian-linter-1, rel-koota-1 | off |
| fresh | 12 | 12 | f01, f02, f03, f04, f05, f06, f07, f08, f09, f10, f11, f12 | off |
| **total** | **28** | **85** | | |

## Fresh controls

| id | task | repo |
|---|---|---|
| f01 | kcp-go-multiplexed-kcp-streams | kcp-go |
| f02 | arcane-drift-detection-baselines | arcane |
| f03 | pebble-durability-wait-apis | pebble |
| f04 | returns-validated-error-accumulation | returns |
| f05 | cattrs-partial-structuring-recovery | cattrs |
| f06 | oxvg-structural-selector-preservation | oxvg |
| f07 | boa-hierarchical-evaluation-cancellation | boa |
| f08 | dynamodb-toolbox-conditional-attribute-requirements | dynamodb-toolbox |
| f09 | vitest-duration-sharding | vitest |
| f10 | drizzle-orm-window-function-builders | drizzle-orm |
| f11 | returns-validated-error-accumulation | returns |
| f12 | oxvg-structural-selector-preservation | oxvg |

## Session

- boundary: compacted; pi's update prompt from boundary 2
- ticket tags: on ("This ticket is `{tag}`; start each commit message for it with `[{tag}]`.")
- saved states: every boundary compaction, chain end (fresh controls: none)
- phase 1: junk on its branch (fresh controls: the teammate draft); phases 2..N: a container switch and the teammate draft

## Orders balance (unrelated arm)

| check | holds |
|---|---|
| each task 4 times at 4 distinct positions | yes |
| each position leaves out exactly 2 tasks | yes |
| adjacent tasks differ in language | yes |
| >= 3 languages per chain | yes |
| no task twice in a chain | yes |

## Notes

- Language labels: DeepSWE's metadata calls httpx-deterministic-cookie-store TypeScript and koota-entity-snapshot-rollback Python; their images are Python and TypeScript, and the code uses those (LANG_FIX, by repo). Neither is in the pool.
- Base commit: each container's /app HEAD at setup, which must start with task.toml's base_commit_hash (koota-entity-snapshot-rollback's has 39 characters); a mismatch fails the phase, which halts that chain (fail closed).
- Launch order: unrelated chains first, then fresh controls and related chains as slots free; the within-task chains start only after every unrelated chain and fresh control is done (held otherwise, never run early).
- Stop rule: once 15 unrelated tasks have finished, scorer status unknown in more than 10% (2 or more) admits no new task; running tasks finish.
- Saved states: kept images only for phases followed by a state; a state whose kept image or scorer home is missing is listed as missing, not saved.
