# Design card: DeepSeek in Claude Code (harness replication)

Generated 2026-10-28T19:20:16Z at 738bc17e by `scripts/week2_harness_card.py` from the code and launch arguments, not typed.
Launch: `uv run --with fastmcp==4.1.3 python scripts/week2_harness_cc.py --arm ds --max-live 5 bulk --go-from-jacob`
Laptop settings at card time (`native.slots` limits file, changeable on the fly; launch values apply where unset): `{'__capacity__': 16, '__cpu_tenths__': 10, 'native-cx-chain': 8, 'native-agy-chain': 6}`

| Parameter | Value |
|---|---|
| cli_version | 2.1.310 |
| model | deepseek/deepseek-v4.1-flash |
| effort | max |
| context_window | 1048576 |
| auto_compaction | off |
| boundary_compaction | /compact via -p --resume |
| turn_rules_sha256 | 7d39bae00d2f |
| checkins_sha256 | 36b033344f05 |
| assignment | seed 20261027; 2 rounds; 3 per arm per first task per round; 24 histories |
| assignment_order_sha256 | a032e5f595f9 |
| calls_per_task | 400 |
| seconds_per_task | 7200 |
| usd_per_task | 6 |
| stage_cap_usd | 120 |
| host | laptop |
| max_live | 5 |
| memory_mb_per_container | 1280 |
| cpus_per_history | 1.5 |
| container_capacity | 16 |
| launch_code_verified | yes |
| proxy_route | openrouter→Together (no fallbacks) |

## Against the plan's Build contract

All rows match.
