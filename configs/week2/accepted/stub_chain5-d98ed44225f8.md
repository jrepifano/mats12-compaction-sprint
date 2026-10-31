# Stub chain (N = 5): sample proposal

## Question

Does the N-task chain harness run a 5-task chain end to end with a container switch at every
boundary, a logged compaction at each, and nothing of the harness visible to the agent?

## Design

One scripted chain over task chaining's four tasks (httpx and tengo alternating), no model calls.

## Build contract

| parameter | value |
|---|---|
| design.kind | chain |
| design.stage | SPRINT-S2-W2-STUB |
| design.seed | 20261027 |
| design.n_phases | 5 |
| chain.orders.path | configs/week2/orders/stub_chain5.json |
| chain.orders.sha256 | ebe080e32eeed6049cc4fa442d8fc1d7201d834aeab0fc105f26005245984bc1 |
| chain.arms | unrelated |
| chain.boundary | compacted |
| chain.auto_compact_tokens | 1 |
| chain.update_prompt_from_boundary | 2 |
| chain.fork_after_every_phase | true |
| caps.stage_usd | 0 |
| caps.run_usd | 0 |
| caps.max_calls | 60 |
| caps.max_seconds | 3600 |
| parallelism.agents | 10 |
| parallelism.fallback_agents | 6 |
| parallelism.agent_memory_mb | 2048 |
| parallelism.fallback_agent_memory_mb | 3072 |
| wording.task_ending.path | configs/week2/wording/task_chaining/task_ending.txt |
| wording.task_ending.sha256 | 69a5e1ae691059f82d63fc68dfadf50236deefd3dd4138f2fcec1244253b6b34 |
| wording.lead_same.path | configs/week2/wording/task_chaining/lead_same.txt |
| wording.lead_same.sha256 | 8edf08f8bfa3b8caa691366c77bfb844afd93037c381a9c1bac08eaf78d457df |
| wording.lead_different.path | configs/week2/wording/task_chaining/lead_different.txt |
| wording.lead_different.sha256 | f3e771faca721f959383d3d26eb1138375f6b9c49b550db7468219078dd7c910 |
