# Design card: r7_planted

- config: configs/week2/r7_planted.toml (sha256 b7bfebfe7b49e38c79df94ec3954aa5665e88d48545b42739a2d2376dd4a5d7d)
- proposal: docs/sprint/plans/week2-r7-planted.md (built against 033dab9355c7700608efee46146f91ce67d39439218459e937f3f6d38db67403)
- kind: r7_planted, stage SPRINT-S2-PLANTED, 2 phases, seed 20261028
- written: 2026-10-28 19:48 UTC

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
| httpx-np-1 | configs/week2/r7_planted/specs/httpx-np-1.json | e6bad61e4062a765efeb7042785d43b5e1f967635f4ed20271a307a90d372ec9 |
| httpx-np-2 | configs/week2/r7_planted/specs/httpx-np-2.json | a99fdfa9bb21ac9f8c7fdb6c5ee32ac3f392253750519535444623fb3d9e568f |
| httpx-np-3 | configs/week2/r7_planted/specs/httpx-np-3.json | 360ce0a14890fd4d89451cb45f17ea43dd58761bde30c2a2a1010f9715261130 |
| httpx-np-4 | configs/week2/r7_planted/specs/httpx-np-4.json | e9ea81d207d3b0117a823de41726c6db2e9e2aa6ae3a87354427768c1f857ed7 |
| httpx-np-5 | configs/week2/r7_planted/specs/httpx-np-5.json | 3d905acb109e9513485685eccc3d6511e53b16527473d809b11533b57f655d15 |
| httpx-np-6 | configs/week2/r7_planted/specs/httpx-np-6.json | 9b86ac28b98a4ccf18f3af499389ac83fbb11a999d3c0367e2bf9779861f96ab |
| tengo-np-1 | configs/week2/r7_planted/specs/tengo-np-1.json | 3d2bf9198d73e2aa7a82c893ba0f0704074deff8beae271b1b7016f2603455fd |
| tengo-np-2 | configs/week2/r7_planted/specs/tengo-np-2.json | 8e3347b134f18233e50ce34185b5cc5e32f425e65158fff848eaae0167685806 |
| tengo-np-3 | configs/week2/r7_planted/specs/tengo-np-3.json | ae29b9286d2cf25a505df48098695f2d44ccf88fa120d951d3843e29f3fd5583 |
| tengo-np-4 | configs/week2/r7_planted/specs/tengo-np-4.json | b72d553e9fa12f097caa7ed685cde5c3f6a9afa33baa89d5052f1c0093180ade |
| tengo-np-5 | configs/week2/r7_planted/specs/tengo-np-5.json | 5aa4a798f13d3b839c10ac27773449825738c9eee26ce786098fcb0688dcb662 |
| tengo-np-6 | configs/week2/r7_planted/specs/tengo-np-6.json | 90c7c7782bfc63ab4ab541bb0157393da0e611fed23298c89131c72026b2b76c |

36 new task-2 runs; 12 existing honest runs included in the 48-run readout.
Sources: configs/week2/r7_planted/sources.json (sha256 db99401fd30189df7285ea2a1ff5d19bf28e3a37abeec5002a945f7d62dc0f11).
Model: claude-haiku-5-5[1m]; effort: high.
The two task-2 runs in each arm share a task-1 history; inference pairs by history.

Reasoning text logged: yes

## Caps and parallelism

- stage $30, per run $6, 400 calls, 7200 s
- agents 4 at 1280 MB, fallback 1 at 3072 MB
- seed 20261028
