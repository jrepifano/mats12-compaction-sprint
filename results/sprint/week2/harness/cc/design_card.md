# Design card: Claude Code (harness replication)

Generated 2026-10-28T18:42:16Z at f6980fdb by `scripts/week2_harness_card.py` from the code and launch arguments, not typed.
Launch: `uv run --with fastmcp==4.1.3 python scripts/week2_harness_cc.py bulk --go-from-jacob  [launch code: yes (script from d549fc07)]`

| Parameter | Value |
|---|---|
| cli_version | 2.1.310 |
| model | claude-haiku-5-5[1m] |
| effort | high |
| context_window | 1000000 |
| auto_compaction | off |
| boundary_compaction | /compact via -p --resume |
| turn_rules_sha256 | 7d39bae00d2f |
| checkins_sha256 | 36b033344f05 |
| assignment | seed 20261027; 2 rounds; 3 per arm per first task per round; 24 histories |
| assignment_order_sha256 | a032e5f595f9 |
| calls_per_task | 400 |
| seconds_per_task | 7200 |
| usd_per_task | 6 |
| stage_cap_usd | 300 |
| host | laptop |
| max_live | 4 |
| memory_mb_per_container | 1280 |
| cpus_per_history | 2.5 |
| container_capacity | 8 |
| launch_code_verified | yes |

## Against the plan's Build contract

| Parameter | Contract | Card |
|---|---|---|
| cpus_per_history | 2 | 2.5 |
