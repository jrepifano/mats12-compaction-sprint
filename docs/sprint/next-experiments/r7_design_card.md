# Design card: r7_planted

- config: configs/week2/r7_planted.toml (sha256 bf64e310f8ddba1d4402e97672cddacff5bc7ebf55598419cbfb1bbf6e22cf4d)
- proposal: docs/sprint/plans/week2-r7-planted.md (built against 14e4edfb8d8c4b7464135cd4c93c372c665643f5c45453b1bccb2241d1af5d13)
- kind: r7_planted, stage SPRINT-S2-PLANTED, 2 phases, seed 20261028
- written: 2026-10-28 21:35 UTC

## Runs

| arm | start | project | runs |
|---|---|---|---|
| planted | replayed | different | 24 |
| honest | replayed | different | 12 |
| honest | existing bulk | different | 12 |
| **total** | | | **48** |

By arm: planted 24, honest 24

By start: replayed 36, existing bulk 12

By project: different 48

## Task order

| tasks | arms | runs |
|---|---|---|
| httpx-deterministic-cookie-store → tengo-destructuring-bindings | planted, honest | 24 |
| tengo-callable-instance-isolation → httpx-multipart-response-parsing | planted, honest | 24 |

## Wording

| name | path | sha256 |
|---|---|---|
| httpx-np-1 | configs/week2/r7_planted/specs/httpx-np-1.json | 233d4521b3f0a95de7aaf20a3a946036db9e1d751109334341d78681ff939084 |
| httpx-np-2 | configs/week2/r7_planted/specs/httpx-np-2.json | c7680509dd4b64580efef7b8b64f241416e695560b2ff2097922a920dc864625 |
| httpx-np-3 | configs/week2/r7_planted/specs/httpx-np-3.json | 2a10ce13b2b2aae684ba590fd0d755c56d4b3584df37b5e8b29b70170d4125ae |
| httpx-np-4 | configs/week2/r7_planted/specs/httpx-np-4.json | 973a2ff477d4a4b0f3599fa48db5952e6001d7f25f1b5cd9e972be21ecb912bf |
| httpx-np-5 | configs/week2/r7_planted/specs/httpx-np-5.json | 293f90763c6dcc3a62ca307a28ffe4549efc5d20c1a3e7e9b51dd1edd55aea60 |
| httpx-np-6 | configs/week2/r7_planted/specs/httpx-np-6.json | 6889a5ebe2c50fbc58b22e962cbba5f332b0f2bb5506c2962d75e8b6816d19bd |
| tengo-np-1 | configs/week2/r7_planted/specs/tengo-np-1.json | b38166598ec8d0ed64cf6ad773c4e145a6f039d4f43747dd55eaa9c25705dfd5 |
| tengo-np-2 | configs/week2/r7_planted/specs/tengo-np-2.json | 4f4e42402f51e4626528ef619e46dcf75e63e1294c168dc6415f263387954c02 |
| tengo-np-3 | configs/week2/r7_planted/specs/tengo-np-3.json | 4363b707a77cc6c7b84b10200f841ece27cb42064d18b2e8e64b90755e23d8be |
| tengo-np-4 | configs/week2/r7_planted/specs/tengo-np-4.json | 708a5036bbcded16c66ef4c9781eba683ab09b677f4414870a56db7c60d8aacf |
| tengo-np-5 | configs/week2/r7_planted/specs/tengo-np-5.json | 08b39b643083705dbbdd24d368a9e1c36d70807d42c8b4384e8c03d2d03b560b |
| tengo-np-6 | configs/week2/r7_planted/specs/tengo-np-6.json | 55641f2d10a940be24527b4a7ee8bdcae3d4ea3d87483e92186839e77ede13d8 |

36 new task-2 runs; 12 existing honest runs included in the 48-run readout.
Sources: configs/week2/r7_planted/sources.json (sha256 db99401fd30189df7285ea2a1ff5d19bf28e3a37abeec5002a945f7d62dc0f11).
Model: claude-haiku-5-5[1m]; effort: high.
The two task-2 runs in each arm share a task-1 history; inference pairs by history.

Reasoning text logged: yes

## Caps and parallelism

- stage $30, per run $6, 400 calls, 7200 s
- agents 4 at 1280 MB, fallback 1 at 3072 MB
- seed 20261028
