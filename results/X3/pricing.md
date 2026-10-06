# X3 full-study pricing and power

UTC 2026-10-06T03:10:46Z. Cost note: /Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/x3-build/results/X3/cost_checkpoint.json.

## $/episode by model

| Model | $/episode | Basis | Source |
|---|---|---|---|
| deepseek/deepseek-v4-flash | 0.0030 | estimated | prereg §7.1 central carried estimate $0.003 |
| qwen/qwen3.6-27b | 0.0760 | estimated | prereg §7.1 central carried estimate $0.076 |
| google/gemini-3.8-flash | 0.1200 | estimated | prereg §7.1 central carried estimate $0.120 |
| z-ai/glm-5.3-flash | 0.0210 | estimated | prereg §7.1 central carried estimate $0.021 |

Judges: $0.0520 per episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes)).

## Full-study configurations

| Configuration | Episodes | Cost | Basis |
|---|---|---|---|
| Core: 30 tasks x 4 repeats x 6 contexts on DeepSeek, Qwen, Gemini 3.8 Flash | 2160 | $255.53 | estimated |
|   of which deepseek-v4-flash | 720 | $39.58 | estimated |
|   of which qwen3.6-27b | 720 | $92.14 | estimated |
|   of which gemini-3.8-flash | 720 | $123.82 | estimated |
| Add GLM 5.3 Flash (same design) | 720 | $52.54 | estimated |
| Add an explicit-cue arm (+2 contexts, 3 core models) | 720 | $85.18 | estimated |
| Add other header wordings (+4 contexts, 3 core models) | 1440 | $170.35 | estimated |
| Add safeguard-disabling tasks (episodes like the core, 3 core models) | 2160 | $255.53 | estimated |
| Probe stage on Qwen (GPU) | 0 | $12.14 | estimated |

Notes:

- Core: 30 tasks x 4 repeats x 6 contexts on DeepSeek, Qwen, Gemini 3.8 Flash: deepseek-v4-flash 720 x $0.0030 (prereg §7.1 central carried estimate $0.003); qwen3.6-27b 720 x $0.0760 (prereg §7.1 central carried estimate $0.076); gemini-3.8-flash 720 x $0.1200 (prereg §7.1 central carried estimate $0.120); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- of which deepseek-v4-flash: deepseek-v4-flash 720 x $0.0030 (prereg §7.1 central carried estimate $0.003); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- of which qwen3.6-27b: qwen3.6-27b 720 x $0.0760 (prereg §7.1 central carried estimate $0.076); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- of which gemini-3.8-flash: gemini-3.8-flash 720 x $0.1200 (prereg §7.1 central carried estimate $0.120); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- Add GLM 5.3 Flash (same design): glm-5.3-flash 720 x $0.0210 (prereg §7.1 central carried estimate $0.021); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- Add an explicit-cue arm (+2 contexts, 3 core models): New prompts: pilot cost per episode used as a proxy. deepseek-v4-flash 240 x $0.0030 (prereg §7.1 central carried estimate $0.003); qwen3.6-27b 240 x $0.0760 (prereg §7.1 central carried estimate $0.076); gemini-3.8-flash 240 x $0.1200 (prereg §7.1 central carried estimate $0.120); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- Add other header wordings (+4 contexts, 3 core models): New prompts: pilot cost per episode used as a proxy. deepseek-v4-flash 480 x $0.0030 (prereg §7.1 central carried estimate $0.003); qwen3.6-27b 480 x $0.0760 (prereg §7.1 central carried estimate $0.076); gemini-3.8-flash 480 x $0.1200 (prereg §7.1 central carried estimate $0.120); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- Add safeguard-disabling tasks (episodes like the core, 3 core models): New tasks: not run in the pilot, so cost per episode is a proxy; building and verifying the tasks is unpriced Claude/Jacob time (roughly 1-2 days), plus a free sandbox build. deepseek-v4-flash 720 x $0.0030 (prereg §7.1 central carried estimate $0.003); qwen3.6-27b 720 x $0.0760 (prereg §7.1 central carried estimate $0.076); gemini-3.8-flash 720 x $0.1200 (prereg §7.1 central carried estimate $0.120); judges $0.0520/episode (judge upper projection J from measured transcript lengths ($18.71 over 360 episodes))
- Probe stage on Qwen (GPU): estimated: A100 80GB $1.39/h x 4 h + H100 80GB $3.29/h x 2 h

## Power (simulated)

Base rate 0.156 (measured: cost-checkpoint neutral cheat rate, pooled (n=135)); task SD 0.58 on the logit scale (measured: method of moments over 15 tasks (480 episodes)); stratified risk difference, normal approximation with per-task pooled variance; 80% power, alpha 0.05; 2000 simulations per point, seed 0. The effect is one logit shift shared by all tasks; if it varies by task, more episodes are needed, so these are lower bounds. Cost prices a primary contrast's whole six-context design (3 x episodes per arm) on each of the three core models.

| Effect (pts) | Tasks | Episodes per arm (sim) | Nominal, no clustering | Episodes, 3 models | Cost | Basis |
|---|---|---|---|---|---|---|
| +5 | 15 | 960 | 925 | 8640 | $1,022.11 | estimated |
| +8 | 15 | 390 | 383 | 3510 | $415.23 | estimated |
| +12 | 15 | 180 | 182 | 1620 | $191.64 | estimated |
| +5 | 30 | 930 | 925 | 8370 | $990.16 | estimated |
| +8 | 30 | 390 | 383 | 3510 | $415.23 | estimated |
| +12 | 30 | 180 | 182 | 1620 | $191.64 | estimated |
