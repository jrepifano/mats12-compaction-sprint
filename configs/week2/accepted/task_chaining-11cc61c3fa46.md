# Task chaining (N = 2): sample proposal

Test fixture for the week-2 plan check (`compaction.s2.chain.week2`). It restates the
task-chaining design of `docs/sprint/plans/task-chaining.md` (v0.3.1) in the week-2 format.

## Question

Does a permission given during task A carry over to task B, and does it depend on how B
starts (full history, compacted history, or a fresh session) and on whether B is in the same
project?

## Design

Each of two repositories has an A task and a B task on one base commit. A runs with or
without the permission line (arms `np` and `p`), four histories per arm per repo. Each B run
starts from one A history, in the same or the other repository, plus four plain fresh B runs
per task: 88 B runs in all.

## Build contract

The config `configs/week2/task_chaining.toml` must match this table exactly.

| parameter | value |
|---|---|
| `design.kind` | two_phase |
| `design.stage` | SPRINT-S2-CHAIN |
| `design.seed` | 20261026 |
| `design.n_phases` | 2 |
| `two_phase.repos` | httpx, tengo |
| `two_phase.arms` | np, p |
| `two_phase.starts` | full, compacted, fresh |
| `two_phase.projects` | same, different |
| `two_phase.histories_per_arm` | 4 |
| `two_phase.a_retries` | 1 |
| `two_phase.plain_fresh_per_task` | 4 |
| `two_phase.permission_after_call` | 20 |
| `two_phase.checkin_calls` | 4, 14, 24, 34, 44, 54 |
| `caps.stage_usd` | $170 |
| `caps.run_usd` | $2.50 |
| `caps.max_calls` | 400 |
| `caps.max_seconds` | 7200 |
| `parallelism.agents` | 10 |
| `parallelism.fallback_agents` | 6 |
| `parallelism.agent_memory_mb` | 2048 |
| `parallelism.fallback_agent_memory_mb` | 3072 |
| `wording.task_ending.path` | configs/week2/wording/task_chaining/task_ending.txt |
| `wording.task_ending.sha256` | 69a5e1ae691059f82d63fc68dfadf50236deefd3dd4138f2fcec1244253b6b34 |
| `wording.permission_line.path` | configs/week2/wording/task_chaining/permission_line.txt |
| `wording.permission_line.sha256` | 7b4510b6aea8bc599055130fc915bb5f11c159dd005c67d37620ee2c1b3e3319 |
| `wording.lead_same.path` | configs/week2/wording/task_chaining/lead_same.txt |
| `wording.lead_same.sha256` | 8edf08f8bfa3b8caa691366c77bfb844afd93037c381a9c1bac08eaf78d457df |
| `wording.lead_different.path` | configs/week2/wording/task_chaining/lead_different.txt |
| `wording.lead_different.sha256` | f3e771faca721f959383d3d26eb1138375f6b9c49b550db7468219078dd7c910 |
| `wording.checkins.path` | configs/week2/wording/task_chaining/checkins.txt |
| `wording.checkins.sha256` | 8750e7e59a573869deac2a8d11856a70efcaad71c24121f333a4e9e7b603dd97 |
| `wording.teammate_message.path` | configs/week2/wording/task_chaining/teammate_message.txt |
| `wording.teammate_message.sha256` | 2604c0a2d6931031436781926f0485e874659a879ab50a6567433d745eebce34 |
| `wording.junk_commits.path` | configs/week2/wording/task_chaining/junk_commits.txt |
| `wording.junk_commits.sha256` | 6e4bebb1fc3f944aa08e3689bff4538f6fe11490d6bbc36d34065857c6cd8dd3 |
| `wording.teammate_text_httpx.path` | configs/week2/wording/task_chaining/teammate_text_httpx.txt |
| `wording.teammate_text_httpx.sha256` | 9eb455ea6f5e54fffd8931f3511b25ab4ba4252fb6d288a8a13f6de0d53c1e1a |
| `wording.teammate_text_tengo.path` | configs/week2/wording/task_chaining/teammate_text_tengo.txt |
| `wording.teammate_text_tengo.sha256` | c2843cfac4759ec358f9f56b4921ba1858dd5ca651eb833e397afea5de97b202 |

## Analysis

Push-over-rejection rate by arm, start and project, with intervals per cell.
