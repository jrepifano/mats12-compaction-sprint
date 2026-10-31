# Design card: summary_fix

- config: configs/week2/summary_fix.toml (sha256 54436e960a381674b9effa1df6b873a8b26710dca80bd6107ea30a52f96e1422)
- proposal: docs/sprint/plans/week2-summary-fix.md (built against 1a29bdb7d60f21c2b5924c3cc4b4129c10e56033b8b8736b18cfffe6a5c776ee)
- kind: summary_fix, stage SPRINT-S2-SUMMARY-FIX, 2 phases, seed 20261029
- written: 2026-10-28 21:34 UTC

## Runs

| arm | start | project | runs |
|---|---|---|---|
| line | compacted | different | 30 |
| ledger | compacted | different | 30 |
| native | compacted | different | 30 |
| placebo | compacted | different | 30 |
| neutral_opening | compacted | different | 30 |
| **total** | | | **150** |

By arm: line 30, ledger 30, native 30, placebo 30, neutral_opening 30

By start: compacted 150

By project: different 150

## Task order

| tasks | arms | runs |
|---|---|---|
| httpx-deterministic-cookie-store → tengo-destructuring-bindings | line, ledger, native, placebo, neutral_opening | 60 |
| tengo-callable-instance-isolation → httpx-multipart-response-parsing | native, line, placebo, ledger, neutral_opening | 90 |

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
| neutral_lead | configs/week2/wording/summary_fix/neutral_lead.txt | 0176c8625332fff048112bad869cf4d7a5b31987d9bdaa673f142cc17c46c547 |

Reasoning text logged: yes

## Caps and parallelism

- stage $305, per run $2.5, 400 calls, 7200 s
- agents 16 at 1280 MB, fallback 6 at 3072 MB
- seed 20261029
