# Design card: stub_chain5

- config: /home/jacob/emx-week2/configs/week2/stub_chain5.toml (sha256 c44d9a04fbc88f1422a9d199e941d5c366c943fd098c79a83afc362f938b84ab)
- proposal: tests/compaction/fixtures/week2/stub_chain5_proposal.md (built against d98ed44225f8feb31b94e79a1f3abd62510a541b2e59daddfc3c5be672b24471)
- kind: chain, stage SPRINT-S2-W2-STUB, 5 phases, seed 20261027
- written: 2026-10-27 07:15 UTC

## Runs

| arm | start | project | runs |
|---|---|---|---|
| unrelated | compacted | switch+switch+switch+switch | 1 |
| **total** | | | **1** |

By arm: unrelated 1

By start: compacted 1

By project: switch+switch+switch+switch 1

## Task order

| tasks | arms | runs |
|---|---|---|
| httpx-deterministic-cookie-store → tengo-callable-instance-isolation → httpx-multipart-response-parsing → tengo-destructuring-bindings → httpx-deterministic-cookie-store | unrelated | 1 |

## Wording

| name | path | sha256 |
|---|---|---|
| task_ending | configs/week2/wording/task_chaining/task_ending.txt | 69a5e1ae691059f82d63fc68dfadf50236deefd3dd4138f2fcec1244253b6b34 |
| lead_same | configs/week2/wording/task_chaining/lead_same.txt | 8edf08f8bfa3b8caa691366c77bfb844afd93037c381a9c1bac08eaf78d457df |
| lead_different | configs/week2/wording/task_chaining/lead_different.txt | f3e771faca721f959383d3d26eb1138375f6b9c49b550db7468219078dd7c910 |

Orders: configs/week2/orders/stub_chain5.json (sha256 ebe080e32eeed6049cc4fa442d8fc1d7201d834aeab0fc105f26005245984bc1)

## Caps and parallelism

- stage $0, per run $0, 60 calls, 3600 s
- agents 10 at 2048 MB, fallback 6 at 3072 MB
- seed 20261027
