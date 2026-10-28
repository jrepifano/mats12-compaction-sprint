# Design card: agy (harness replication)

Generated 2026-10-28T18:42:16Z at 8046f0b1 by `scripts/week2_harness_card.py` from the code and launch arguments, not typed.
Launch: `uv run --with fastmcp==4.1.3 python scripts/week2_harness_agy.py bulk --go-from-jacob --host local --max-live 2 --memory-mb 1280 --cpus 1.5 --max-containers 8`

| Parameter | Value |
|---|---|
| cli_version | 1.4.7 |
| model | gemini-3.8-flash-low |
| effort | CLI default |
| context_window | default (unverified) |
| auto_compaction | on (cannot be disabled) |
| boundary_compaction | /compact turn |
| turn_rules_sha256 | 7d39bae00d2f |
| checkins_sha256 | 36b033344f05 |
| assignment | seed 20261027; 2 rounds; 3 per arm per first task per round; 24 histories |
| assignment_order_sha256 | a032e5f595f9 |
| calls_per_task | 400 |
| seconds_per_task | 7200 |
| usd_per_task | plan quota |
| stage_cap_usd | plan quota |
| host | laptop |
| max_live | 2 |
| memory_mb_per_container | 1280 |
| cpus_per_history | 2 |
| container_capacity | 8 |
| launch_code_verified | yes |

## Against the plan's Build contract

All rows match.
