| set (path under main checkout unless noted) | model | n | turns med (max) | final ctx med / p90 / max | >64k | >100k | >128k | >160k | >200k | prompt/call mean | completion/call mean | reasoning/call mean | cached share | reasoning stored | truncated tool outputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P07-compaction-trust | google/gemini-3.8-flash | 243 | 12 (12) | 11.4k / 17.3k / 41.5k | 0 | 0 | 0 | 0 | 0 | 5,651 | 490 | 93 | 0.04 | 1.00 | 18 of 2839 |
| P07-compaction-trust | z-ai/glm-5.3-flash | 976 | 11 (12) | 18.0k / 44.0k / 106.6k | 16 | 1 | 0 | 0 | 0 | 12,683 | 1,666 | 1,086 | 0.86 | 1.00 | 225 of 8528 |
| P08-dropped-fact-behaviour | deepseek/deepseek-v4-flash | 384 | 5 (15) | 5.3k / 16.0k / 49.9k | 0 | 0 | 0 | 0 | 0 | 6,496 | 580 | 366 | 0.81 | 1.00 | 7 of 2050 |
| P08-dropped-fact-behaviour | z-ai/glm-5.3-flash | 624 | 6 (15) | 7.8k / 24.3k / 119.9k | 7 | 2 | 0 | 0 | 0 | 9,773 | 1,179 | 758 | 0.87 | 1.00 | 42 of 3458 |
| P09-wrapper-identity-action | deepseek/deepseek-v4-flash | 474 | 11 (12) | 12.9k / 28.1k / 72.4k | 2 | 0 | 0 | 0 | 0 | 9,365 | 1,017 | 720 | 0.83 | 1.00 | 66 of 4179 |
| P09-wrapper-identity-action | z-ai/glm-5.3-flash | 1052 | 10 (12) | 20.0k / 48.1k / 130.2k | 48 | 5 | 1 | 0 | 0 | 16,148 | 1,972 | 1,274 | 0.88 | 1.00 | 197 of 8552 |
| P10-fabricated-history-trust | deepseek/deepseek-v4-flash | 829 | 12 (12) | 14.0k / 29.3k / 64.8k | 1 | 0 | 0 | 0 | 0 | 9,421 | 1,006 | 704 | 0.83 | 1.00 | 132 of 7633 |
| P10-fabricated-history-trust | z-ai/glm-5.3-flash | 828 | 11 (12) | 22.3k / 51.5k / 112.6k | 30 | 1 | 0 | 0 | 0 | 15,156 | 2,063 | 1,407 | 0.89 | 1.00 | 186 of 7373 |
| P23-compaction-decisions-vs-cues | deepseek/deepseek-v4-flash | 1601 | 6 (15) | 6.2k / 25.4k / 122.8k | 24 | 3 | 0 | 0 | 0 | 8,355 | 1,025 | 744 | 0.67 | 1.00 | 110 of 9452 |
| P23-compaction-decisions-vs-cues | z-ai/glm-5.3-flash | 1601 | 6 (15) | 7.8k / 36.3k / 174.3k | 81 | 42 | 19 | 8 | 0 | 14,184 | 1,740 | 1,152 | 0.87 | 1.00 | 101 of 8732 |
| P37-grader-belief-causal | z-ai/glm-5.3-flash | 2120 | 4 (22) | 25.5k / 105.2k / 143.8k | 303 | 233 | 50 | 0 | 0 | 36,148 | 1,112 | 0 | 0.00 | 1.00 | 45 of 7852 |
| P38-compaction-live-fact | deepseek-v4-flash | 2030 | 3 (20) | 3.8k / 5.7k / 16.8k | 0 | 0 | 0 | 0 | 0 | 2,759 | 448 | 0 | 0.00 | 1.00 | - |
| P38-compaction-live-fact | glm-5.3-flash | 1738 | 4 (20) | 7.4k / 13.5k / 34.4k | 0 | 0 | 0 | 0 | 0 | 5,243 | 1,193 | 0 | 0.00 | 1.00 | - |
| P41-grader-belief-atrisk | z-ai/glm-5.3-flash | 1359 | 9 (23) | 39.3k / 111.4k / 243.5k | 310 | 177 | 105 | 53 | 18 | 46,555 | 3,081 | 0 | 0.00 | 1.00 | 84 of 12230 |
| P44-handoff-methods-alignment | deepseek/deepseek-v4-flash | 270 | 24 (24) | 12.8k / 38.0k / 132.6k | 6 | 1 | 1 | 0 | 0 | 7,301 | 1,080 | 820 | 0.52 | 1.00 | 14 of 5972 |
| P44-handoff-methods-alignment | z-ai/glm-5.3-flash | 1105 | 18 (36) | 24.5k / 64.9k / 245.1k | 112 | 38 | 25 | 19 | 10 | 13,636 | 2,971 | 2,632 | 0.58 | 0.99 | 131 of 15852 |
| P47-grader-belief-probe | openai/gpt-oss-120b | 228 | 11 (20) | 10.9k / 18.9k / 29.9k | 0 | 0 | 0 | 0 | 0 | 7,739 | 561 | 0 | 0.00 | 1.00 | 1 of 2185 |
| P47-grader-belief-probe | openai/gpt-oss-20b | 223 | 14 (20) | 15.3k / 29.4k / 64.4k | 1 | 0 | 0 | 0 | 0 | 10,319 | 771 | 0 | 0.00 | 0.95 | 3 of 2790 |
| P47-grader-belief-probe | qwen/qwen3.6-27b | 37 | 10 (20) | 20.0k / 35.2k / 49.1k | 0 | 0 | 0 | 0 | 0 | 13,058 | 3,550 | 0 | 0.00 | 1.00 | 0 of 280 |
| results/X3b | google/gemini-3.8-flash | 214 | 25 (25) | 25.0k / 36.4k / 50.2k | 0 | 0 | 0 | 0 | 0 | 11,047 | 739 | 74 | 0.36 | 1.00 | 18 of 4931 |
| results/X3b | z-ai/glm-5.3-flash | 588 | 14 (25) | 36.3k / 150.8k / 228.2k | 197 | 124 | 87 | 51 | 23 | 40,154 | 3,108 | 2,128 | 0.85 | 0.97 | 41 of 7553 |
| x3-build wt: B | google/gemini-3.8-flash | 15 | 25 (25) | 21.6k / 27.6k / 37.3k | 0 | 0 | 0 | 0 | 0 | 9,083 | 587 | 68 | 0.21 | 1.00 | 0 of 363 |
| x3-build wt: B | z-ai/glm-5.3-flash | 15 | 15 (25) | 32.9k / 146.5k / 160.4k | 6 | 3 | 3 | 1 | 0 | 39,462 | 3,194 | 2,138 | 0.87 | 1.00 | 0 of 201 |
| x3-build wt: results/X3 | deepseek/deepseek-v4-flash | 62 | 25 (25) | 35.4k / 82.8k / 172.7k | 14 | 3 | 1 | 1 | 0 | 23,479 | 1,717 | 1,302 | 0.84 | 1.00 | 6 of 1282 |
| x3-build wt: results/X3 | qwen/qwen3.6-27b | 35 | 16 (25) | 21.7k / 53.2k / 64.8k | 1 | 0 | 0 | 0 | 0 | 13,479 | 2,769 | 2,492 | 0.81 | 1.00 | 0 of 465 |
| x3-build wt: results/X3 | z-ai/glm-5.3-flash | 375 | 13 (25) | 32.6k / 105.1k / 164.6k | 118 | 51 | 5 | 1 | 0 | 28,633 | 2,828 | 1,949 | 0.79 | 1.00 | 24 of 4397 |
