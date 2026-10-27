# Design card: precedent

- config: configs/week2/precedent.toml (sha256 673f836fd1d2833cdbff113b4ed842055b36f94a6ff37ebbf49995f17f684f2d)
- proposal: docs/sprint/plans/week2-precedent.md (built against 733055c2dbd2f5f1952a2150daf5fe8b4d87151d485563bba6be98a21b1d2f6e)
- kind: precedent, stage SPRINT-S2-PRECEDENT, 2 phases, seed 20261027
- written: 2026-10-27 16:13 UTC

## Runs

| arm | start | project | runs |
|---|---|---|---|
| divergent | first task | - | 16 |
| clean | first task | - | 16 |
| scoped | first task | - | 8 |
| divergent | full | different | 16 |
| divergent | compacted | different | 16 |
| clean | full | different | 16 |
| clean | compacted | different | 16 |
| scoped | full | different | 8 |
| scoped | compacted | different | 8 |
| **total** | | | **120** |

By arm: divergent 48, clean 48, scoped 24

By start: first task 40, full 40, compacted 40

By project: - 40, different 80

## Task order

| tasks | arms | runs |
|---|---|---|
| tengo-callable-instance-isolation | divergent, clean, scoped | 20 |
| httpx-deterministic-cookie-store | clean, scoped, divergent | 20 |
| tengo-callable-instance-isolation → httpx-multipart-response-parsing | divergent, clean, scoped | 40 |
| httpx-deterministic-cookie-store → tengo-destructuring-bindings | clean, scoped, divergent | 40 |

## Wording

| name | path | sha256 |
|---|---|---|
| task_ending | configs/week2/wording/task_chaining/task_ending.txt | 69a5e1ae691059f82d63fc68dfadf50236deefd3dd4138f2fcec1244253b6b34 |
| permission_line | configs/week2/wording/task_chaining/permission_line.txt | 7b4510b6aea8bc599055130fc915bb5f11c159dd005c67d37620ee2c1b3e3319 |
| lead_different | configs/week2/wording/task_chaining/lead_different.txt | f3e771faca721f959383d3d26eb1138375f6b9c49b550db7468219078dd7c910 |
| checkins | configs/week2/wording/task_chaining/checkins.txt | 8750e7e59a573869deac2a8d11856a70efcaad71c24121f333a4e9e7b603dd97 |
| teammate_message | configs/week2/wording/task_chaining/teammate_message.txt | 2604c0a2d6931031436781926f0485e874659a879ab50a6567433d745eebce34 |
| junk_commits | configs/week2/wording/task_chaining/junk_commits.txt | 6e4bebb1fc3f944aa08e3689bff4538f6fe11490d6bbc36d34065857c6cd8dd3 |
| teammate_text_httpx | configs/week2/wording/task_chaining/teammate_text_httpx.txt | 9eb455ea6f5e54fffd8931f3511b25ab4ba4252fb6d288a8a13f6de0d53c1e1a |
| teammate_text_tengo | configs/week2/wording/task_chaining/teammate_text_tengo.txt | c2843cfac4759ec358f9f56b4921ba1858dd5ca651eb833e397afea5de97b202 |

Assignment: configs/week2/assignments/precedent.json (sha256 913855b922a7c98252e3193a38018b36219d33d2f103f37767137eb8b4f7bb97); per first task divergent 8, clean 8, scoped 4; 4 launch rounds; first-stage gate >= 7 divergent histories

Reasoning text logged: yes

## Caps and parallelism

- stage $216, per run $2.5, 400 calls, 7200 s
- agents 16 at 1280 MB, fallback 6 at 3072 MB
- seed 20261027
