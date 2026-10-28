# Design card: summary_fix

- config: configs/week2/summary_fix.toml (sha256 f99c9c3535ac7d6599819ce3471fe4922dd67bfbdaaab805f5764e944cb8b09e)
- proposal: docs/sprint/plans/week2-summary-fix.md (built against ea47b3ada274a5e2ebccea2624380624c7e7f9389b96a4ec275a40e1ccd4009d)
- kind: summary_fix, stage SPRINT-S2-SUMMARY-FIX, 2 phases, seed 20261029
- written: 2026-10-28 19:12 UTC

## Runs

| arm | start | project | runs |
|---|---|---|---|
| line | compacted | different | 30 |
| ledger | compacted | different | 30 |
| native | compacted | different | 30 |
| placebo | compacted | different | 30 |
| **total** | | | **120** |

By arm: line 30, ledger 30, native 30, placebo 30

By start: compacted 120

By project: different 120

## Task order

| tasks | arms | runs |
|---|---|---|
| httpx-deterministic-cookie-store → tengo-destructuring-bindings | line, ledger, native, placebo | 48 |
| tengo-callable-instance-isolation → httpx-multipart-response-parsing | line, native, placebo, ledger | 72 |

## Wording

| name | path | sha256 |
|---|---|---|
| task_ending | configs/week2/wording/task_chaining/task_ending.txt | 69a5e1ae691059f82d63fc68dfadf50236deefd3dd4138f2fcec1244253b6b34 |
| lead_different | configs/week2/wording/task_chaining/lead_different.txt | f3e771faca721f959383d3d26eb1138375f6b9c49b550db7468219078dd7c910 |
| teammate_message | configs/week2/wording/task_chaining/teammate_message.txt | 2604c0a2d6931031436781926f0485e874659a879ab50a6567433d745eebce34 |
| teammate_text_httpx | configs/week2/wording/task_chaining/teammate_text_httpx.txt | 9eb455ea6f5e54fffd8931f3511b25ab4ba4252fb6d288a8a13f6de0d53c1e1a |
| teammate_text_tengo | configs/week2/wording/task_chaining/teammate_text_tengo.txt | c2843cfac4759ec358f9f56b4921ba1858dd5ca651eb833e397afea5de97b202 |
| ledger_focus | configs/week2/wording/summary_fix/ledger_focus.txt | f2a898a9daed8d90ab15c5aa0d1def3fb81d43ac611eaa2c29d22b4bf835a8aa |
| placebo_focus | configs/week2/wording/summary_fix/placebo_focus.txt | 1caf3815adf787c9637d025be0c3a791ae02536154075c256a696866f4e31628 |
| line | configs/week2/wording/summary_fix/line.txt | 299e80c1de8417424daba11f9862e997c1be3912af0100f782514f5ba7978bf6 |

Reasoning text logged: yes

## Caps and parallelism

- stage $250, per run $2.5, 400 calls, 7200 s
- agents 16 at 1280 MB, fallback 6 at 3072 MB
- seed 20261029
