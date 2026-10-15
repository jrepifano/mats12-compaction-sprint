> **Preliminary** (measurement sub-agent, 2026-10-15): a single-chain sketch (10k base, 30k carry-over, 4k summary, 0.82 cache efficiency). Superseded for the plan by `design/budget.py`.

Parameters: b=10000, carry-over c=30k (20k/40k sensitivity below), K=3 compactions, tail=20 turns after the 3rd, summary prompt q=1000, summary output s_out=4000 (incl. reasoning), tool exec 2.0s/turn. Prices queried 2026-10-15T02:19:13+00:00.

### Turns and tokens per session, by growth profile

| profile | g tok/turn | o tok/turn | W | turns to 1st compaction | turns between | turns to 2nd | turns to 3rd | session turns (3 comp + 20) | prompt tokens (M) | ideal cache-read share |
|---|---|---|---|---|---|---|---|---|---|---|
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 3942 | 3652 | 100k | 23 | 18 | 41 | 59 | 79 | 4.86 | 0.919 |
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 3942 | 3652 | 128k | 30 | 25 | 55 | 80 | 100 | 7.23 | 0.934 |
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 3942 | 3652 | 160k | 39 | 33 | 72 | 105 | 125 | 10.80 | 0.947 |
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 3942 | 3652 | 200k | 49 | 44 | 93 | 137 | 157 | 16.57 | 0.958 |
| GLM-Flash kept-reasoning, no capped turns | 2164 | 1874 | 100k | 42 | 33 | 75 | 108 | 128 | 7.56 | 0.951 |
| GLM-Flash kept-reasoning, no capped turns | 2164 | 1874 | 128k | 55 | 46 | 101 | 147 | 167 | 12.01 | 0.962 |
| GLM-Flash kept-reasoning, no capped turns | 2164 | 1874 | 160k | 70 | 61 | 131 | 192 | 212 | 18.52 | 0.970 |
| GLM-Flash kept-reasoning, no capped turns | 2164 | 1874 | 200k | 88 | 79 | 167 | 246 | 266 | 28.25 | 0.976 |
| DS-V4-Flash kept-reasoning (measured) | 1983 | 1693 | 100k | 46 | 36 | 82 | 118 | 138 | 8.15 | 0.955 |
| DS-V4-Flash kept-reasoning (measured) | 1983 | 1693 | 128k | 60 | 50 | 110 | 160 | 180 | 12.95 | 0.965 |
| DS-V4-Flash kept-reasoning (measured) | 1983 | 1693 | 160k | 76 | 66 | 142 | 208 | 228 | 19.86 | 0.973 |
| DS-V4-Flash kept-reasoning (measured) | 1983 | 1693 | 200k | 96 | 86 | 182 | 268 | 288 | 30.64 | 0.978 |
| GLM-Flash reasoning stripped from history | 737 | 3652 | 100k | 123 | 95 | 218 | 313 | 333 | 19.79 | 0.983 |
| GLM-Flash reasoning stripped from history | 737 | 3652 | 128k | 161 | 133 | 294 | 427 | 447 | 32.77 | 0.987 |
| GLM-Flash reasoning stripped from history | 737 | 3652 | 160k | 204 | 177 | 381 | 558 | 578 | 51.64 | 0.990 |
| GLM-Flash reasoning stripped from history | 737 | 3652 | 200k | 258 | 231 | 489 | 720 | 740 | 80.81 | 0.992 |
| DS-V4-Flash reasoning stripped from history | 713 | 1693 | 100k | 127 | 99 | 226 | 325 | 345 | 20.57 | 0.983 |
| DS-V4-Flash reasoning stripped from history | 713 | 1693 | 128k | 166 | 138 | 304 | 442 | 462 | 33.92 | 0.987 |
| DS-V4-Flash reasoning stripped from history | 713 | 1693 | 160k | 211 | 183 | 394 | 577 | 597 | 53.36 | 0.990 |
| DS-V4-Flash reasoning stripped from history | 713 | 1693 | 200k | 267 | 239 | 506 | 745 | 765 | 83.61 | 0.992 |
| SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 691 | 221 | 100k | 131 | 102 | 233 | 335 | 355 | 21.17 | 0.984 |
| SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 691 | 221 | 128k | 171 | 142 | 313 | 455 | 475 | 34.85 | 0.988 |
| SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 691 | 221 | 160k | 217 | 189 | 406 | 595 | 615 | 55.01 | 0.991 |
| SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 691 | 221 | 200k | 275 | 246 | 521 | 767 | 787 | 85.95 | 0.993 |
| P38 ops world, GLM (measured) | 737 | 676 | 100k | 123 | 95 | 218 | 313 | 333 | 19.78 | 0.983 |
| P38 ops world, GLM (measured) | 737 | 676 | 128k | 161 | 133 | 294 | 427 | 447 | 32.76 | 0.987 |
| P38 ops world, GLM (measured) | 737 | 676 | 160k | 204 | 177 | 381 | 558 | 578 | 51.62 | 0.990 |
| P38 ops world, GLM (measured) | 737 | 676 | 200k | 258 | 231 | 489 | 720 | 740 | 80.77 | 0.992 |

### Carry-over sensitivity (GLM measured profile and DS measured profile): session turns / prompt M tokens

| profile | W | c=20k | c=30k | c=40k |
|---|---|---|---|---|
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 100k | 85 / 4.87 | 79 / 4.86 | 75 / 5.00 |
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 128k | 106 / 7.26 | 100 / 7.23 | 96 / 7.40 |
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 160k | 131 / 10.87 | 125 / 10.80 | 121 / 11.01 |
| GLM-Flash kept-reasoning (measured, 12% capped turns) | 200k | 161 / 16.28 | 157 / 16.57 | 151 / 16.42 |
| DS-V4-Flash kept-reasoning (measured) | 100k | 148 / 8.18 | 138 / 8.15 | 128 / 8.01 |
| DS-V4-Flash kept-reasoning (measured) | 128k | 190 / 12.98 | 180 / 12.95 | 170 / 12.81 |
| DS-V4-Flash kept-reasoning (measured) | 160k | 238 / 19.88 | 228 / 19.86 | 218 / 19.73 |
| DS-V4-Flash kept-reasoning (measured) | 200k | 298 / 30.66 | 288 / 30.64 | 278 / 30.51 |
| DS-V4-Flash reasoning stripped from history | 100k | 373 / 21.05 | 345 / 20.57 | 317 / 19.80 |
| DS-V4-Flash reasoning stripped from history | 128k | 490 / 34.40 | 462 / 33.92 | 434 / 33.15 |
| DS-V4-Flash reasoning stripped from history | 160k | 625 / 53.85 | 597 / 53.36 | 569 / 52.60 |
| DS-V4-Flash reasoning stripped from history | 200k | 793 / 84.09 | 765 / 83.61 | 737 / 82.85 |

### $ per session (3 compactions + 20-turn tail), ideal prefix caching (h=1), compaction prompt a cache hit

Columns W=100k..200k: $/session at h=1. Then W=128k at h_emp=0.82 (measured efficiency) and at h=0 (no caching), the compaction-miss surcharge at 128k (3 calls), and wall-clock hours at W=128k.

| model | provider (quant) | ctx | $in | $out | $cache-read | tok/s p50 | lat p50 s | profile | 100k | 128k | 160k | 200k | 128k h_emp | 128k no-cache | 128k compaction miss extra | 128k wall-clock h |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| GLM 5.3 Flash | Together (unknown) | 1048k | 0.15 | 0.5 | 0.03 | 76 | 0.455 | GLM-Flash kept-reasoning | 0.35 | 0.47 | 0.64 | 0.89 | 0.62 | 1.28 | 0.046 | 1.4 |
| GLM 5.3 Flash | CoreWeave (nvfp4) | 1048k | 0.15 | 0.5 | 0.029 | 155 | 0.655 | GLM-Flash kept-reasoning | 0.35 | 0.47 | 0.63 | 0.88 | 0.61 | 1.28 | 0.046 | 0.8 |
| GLM 5.3 Flash | Parasail (fp4) | 1048k | 0.188 | 0.625 | 0.0375 | 133 | 0.535 | GLM-Flash kept-reasoning | 0.44 | 0.59 | 0.80 | 1.12 | 0.78 | 1.61 | 0.058 | 0.9 |
| GLM 5.3 Flash | Relace (unknown) | 1048k | 0.04 | 0.5 | 0.0125 | 40 | 1.422 | GLM-Flash kept-reasoning | 0.23 | 0.30 | 0.39 | 0.53 | 0.33 | 0.48 | 0.011 | 2.7 |
| GLM 5.3 Flash | StreamLake (fp8) | 1024k | 0.0435 | 0.145 | 0.0087 | 38 | 1.841 | GLM-Flash kept-reasoning | 0.10 | 0.14 | 0.19 | 0.26 | 0.18 | 0.37 | 0.013 | 2.9 |
| GLM 5.3 Flash | Wafer (unknown) | 1048k | 0.12 | 0.5 | 0.115 | 143.5 | 0.466 | GLM-Flash kept-reasoning | 0.75 | 1.07 | 1.54 | 2.27 | 1.07 | 1.10 | 0.002 | 0.8 |
| DeepSeek V4 Flash | Baidu (fp8) | 1048k | 0.14 | 0.28 | 0.028 | 100 | 0.967 | DS-V4-Flash kept-reasoning | 0.35 | 0.51 | 0.74 | 1.09 | 0.76 | 1.91 | 0.043 | 1.0 |
| DeepSeek V4 Flash | Relace (fp4) | 1048k | 0.05 | 1.28 | 0.05 | 105 | 0.628 | DS-V4-Flash kept-reasoning | 0.74 | 1.07 | 1.53 | 2.20 | 1.07 | 1.07 | 0.000 | 1.0 |
| DeepSeek V4 Flash | GMICloud (fp8) | 1048k | 0.091 | 0.182 | 0.0182 | 61 | 1.433 | DS-V4-Flash kept-reasoning | 0.23 | 0.33 | 0.48 | 0.71 | 0.50 | 1.24 | 0.028 | 1.6 |
| DeepSeek V4 Flash | Parasail (fp8) | 1048k | 0.14 | 0.28 | 0.07 | 77 | 0.7275 | DS-V4-Flash kept-reasoning | 0.69 | 1.05 | 1.57 | 2.37 | 1.21 | 1.93 | 0.027 | 1.3 |
| DeepSeek V4.1 Flash | Together (unknown) | 1048k | 0.3 | 1.2 | 0.006 | 189 | 0.363 | DS-V4-Flash kept-reasoning | 0.45 | 0.59 | 0.76 | 0.98 | 1.25 | 4.27 | 0.113 | 0.6 |
| DeepSeek V4.1 Flash | CoreWeave (fp8) | 1048k | 0.15 | 0.65 | 0.006 | 176 | 0.512 | DS-V4-Flash kept-reasoning | 0.26 | 0.35 | 0.46 | 0.61 | 0.67 | 2.15 | 0.055 | 0.6 |
| GLM 5.3 (full) | Crusoe (fp4) | 1048k | 1.4 | 4.4 | 0.26 | 180 | 0.613 | DS-V4-Flash kept-reasoning | 3.70 | 5.38 | 7.66 | 11.08 | 7.94 | 19.62 | 0.438 | 0.6 |
| GLM 5.3 (full) | Inceptron (fp4) | 1048k | 0.55 | 2.2 | 0.18 | 146 | 0.687 | DS-V4-Flash kept-reasoning | 2.20 | 3.26 | 4.74 | 6.97 | 4.10 | 7.89 | 0.142 | 0.7 |
| DeepSeek V4 Pro | Azure (unknown) | 1048k | 1.91 | 3.83 | 0.16 | 105 | 1.924 | DS-V4-Flash kept-reasoning | 2.94 | 4.14 | 5.74 | 8.08 | 8.07 | 26.01 | 0.672 | 1.0 |
| DeepSeek V4 Pro | StreamLake (fp8) | 1024k | 0.955 | 1.91 | 0.0796 | 35 | 1.8395 | DS-V4-Flash kept-reasoning | 1.46 | 2.06 | 2.86 | 4.02 | 4.03 | 13.00 | 0.336 | 2.7 |
| Kimi K3 | Amazon Bedrock (unknown) | 1048k | 3 | 15 | 0.3 | 92 | 0.527 | DS-V4-Flash kept-reasoning | 7.22 | 9.97 | 13.55 | 18.66 | 16.04 | 43.71 | 1.037 | 1.1 |
| Kimi K3 | InferenceNet (fp4) | 1048k | 0.39 | 15 | 0.28 | 60 | 0.878 | DS-V4-Flash kept-reasoning | 6.09 | 8.53 | 11.73 | 16.31 | 8.78 | 9.91 | 0.042 | 1.6 |
| Qwen 3.7 Max | Alibaba (unknown) | 1000k | 1.48 | 4.42 | 0.295 | 60 | 1.15 | DS-V4-Flash kept-reasoning | 4.02 | 5.87 | 8.41 | 12.21 | 8.52 | 20.61 | 0.453 | 1.6 |
| Qwen 3.7 Plus | Alibaba (unknown) | 1000k | 0.32 | 1.28 | 0.064 | 48 | 1.625 | DS-V4-Flash kept-reasoning | 0.95 | 1.37 | 1.95 | 2.81 | 1.95 | 4.57 | 0.098 | 2.0 |
| MiniMax M3 | ModelRun (fp8) | 1048k | 0.3 | 1.3 | 0.05 | 124 | 0.578 | DS-V4-Flash kept-reasoning | 0.83 | 1.19 | 1.67 | 2.38 | 1.75 | 4.32 | 0.096 | 0.8 |
| Gemini 3.8 Flash | Google AI Studio (unknown) | 1048k | 0.75 | 3.75 | 0.075 | 89 | 2.246 | DS-V4-Flash kept-reasoning | 1.80 | 2.49 | 3.39 | 4.67 | 4.01 | 10.93 | 0.259 | 1.2 |
| Claude Haiku 5.5 | Anthropic (unknown) | 1000k | 0.1 | 0.5 | 0.01 | 129 | 1.396 | DS-V4-Flash kept-reasoning | 0.24 | 0.33 | 0.45 | 0.62 | 0.53 | 1.46 | 0.035 | 0.9 |
| gpt-oss-120b (131k ctx: W<=100k only) | Groq (unknown) | 131k | 0.15 | 0.6 | 0.075 | 307 | 0.184 | DS-V4-Flash kept-reasoning | 0.81 | n/a (ctx) | n/a (ctx) | n/a (ctx) | n/a | n/a | n/a | 0.3 (at 100k) |

### Profile sensitivity for the two primary models at W=128k (h=1, compaction hit)

| model/provider | profile | session turns | $ / session | wall-clock h |
|---|---|---|---|---|
| GLM 5.3 Flash / Together | GLM-Flash kept-reasoning (measured, 12% capped turns) | 100 | 0.47 | 1.4 |
| GLM 5.3 Flash / Together | GLM-Flash kept-reasoning, no capped turns | 167 | 0.59 | 1.3 |
| GLM 5.3 Flash / Together | GLM-Flash reasoning stripped from history | 447 | 1.87 | 6.3 |
| GLM 5.3 Flash / Together | SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 475 | 1.17 | 0.8 |
| GLM 5.3 Flash / Together | P38 ops world, GLM (measured) | 447 | 1.20 | 1.5 |
| GLM 5.3 Flash / CoreWeave | GLM-Flash kept-reasoning (measured, 12% capped turns) | 100 | 0.47 | 0.8 |
| GLM 5.3 Flash / CoreWeave | GLM-Flash kept-reasoning, no capped turns | 167 | 0.58 | 0.7 |
| GLM 5.3 Flash / CoreWeave | GLM-Flash reasoning stripped from history | 447 | 1.84 | 3.3 |
| GLM 5.3 Flash / CoreWeave | SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 475 | 1.13 | 0.6 |
| GLM 5.3 Flash / CoreWeave | P38 ops world, GLM (measured) | 447 | 1.17 | 0.9 |
| DeepSeek V4 Flash / Baidu | DS-V4-Flash kept-reasoning (measured) | 180 | 0.51 | 1.0 |
| DeepSeek V4 Flash / Baidu | DS-V4-Flash reasoning stripped from history | 462 | 1.23 | 2.6 |
| DeepSeek V4 Flash / Baidu | SWE-rebench OpenHands, no reasoning (Qwen3-Coder) | 475 | 1.07 | 0.7 |

### Gate 0: price of one summariser call over a W-token transcript (cache miss: fresh prompt), s_out=4k

| model/provider | W=64k | 100k | 128k | 160k | 200k |
|---|---|---|---|---|---|
| GLM 5.3 Flash / Together | 0.0118 | 0.0171 | 0.0214 | 0.0261 | 0.0321 |
| GLM 5.3 Flash / CoreWeave | 0.0118 | 0.0171 | 0.0214 | 0.0261 | 0.0321 |
| GLM 5.3 Flash / Parasail | 0.0147 | 0.0214 | 0.0267 | 0.0327 | 0.0402 |
| GLM 5.3 Flash / Relace | 0.0046 | 0.0060 | 0.0072 | 0.0084 | 0.0100 |
| GLM 5.3 Flash / StreamLake | 0.0034 | 0.0050 | 0.0062 | 0.0076 | 0.0093 |
| GLM 5.3 Flash / Wafer | 0.0098 | 0.0141 | 0.0175 | 0.0213 | 0.0261 |
| DeepSeek V4 Flash / Baidu | 0.0102 | 0.0153 | 0.0192 | 0.0237 | 0.0293 |
| DeepSeek V4 Flash / Relace | 0.0084 | 0.0102 | 0.0116 | 0.0132 | 0.0152 |
| DeepSeek V4 Flash / GMICloud | 0.0066 | 0.0099 | 0.0125 | 0.0154 | 0.0190 |
| DeepSeek V4 Flash / Parasail | 0.0102 | 0.0153 | 0.0192 | 0.0237 | 0.0293 |
| DeepSeek V4.1 Flash / Together | 0.0243 | 0.0351 | 0.0435 | 0.0531 | 0.0651 |
| DeepSeek V4.1 Flash / CoreWeave | 0.0123 | 0.0177 | 0.0220 | 0.0267 | 0.0328 |
| GLM 5.3 (full) / Crusoe | 0.1086 | 0.1590 | 0.1982 | 0.2430 | 0.2990 |
| GLM 5.3 (full) / Inceptron | 0.0445 | 0.0644 | 0.0798 | 0.0974 | 0.1194 |
| DeepSeek V4 Pro / Azure | 0.1395 | 0.2082 | 0.2617 | 0.3228 | 0.3992 |
| DeepSeek V4 Pro / StreamLake | 0.0697 | 0.1041 | 0.1309 | 0.1614 | 0.1996 |
| Kimi K3 / Amazon Bedrock | 0.2550 | 0.3630 | 0.4470 | 0.5430 | 0.6630 |
| Kimi K3 / InferenceNet | 0.0853 | 0.0994 | 0.1103 | 0.1228 | 0.1384 |
| Qwen 3.7 Max / Alibaba | 0.1136 | 0.1667 | 0.2080 | 0.2552 | 0.3142 |
| Qwen 3.7 Plus / Alibaba | 0.0259 | 0.0374 | 0.0464 | 0.0566 | 0.0694 |
| MiniMax M3 / ModelRun | 0.0247 | 0.0355 | 0.0439 | 0.0535 | 0.0655 |
| Gemini 3.8 Flash / Google AI Studio | 0.0638 | 0.0907 | 0.1118 | 0.1358 | 0.1658 |
| Claude Haiku 5.5 / Anthropic | 0.0085 | 0.0121 | 0.0149 | 0.0181 | 0.0221 |
| gpt-oss-120b (131k ctx: W<=100k only) / Groq | 0.0121 | 0.0175 | n/a | n/a | n/a |

### Measured cache efficiency in earlier probes (logged cached_tokens vs ideal prefix reuse)

| set | actual cached share of prompt tokens | ideal share | efficiency |
|---|---|---|---|
| P44 GLM R, Together | 0.704 | 0.92 | 0.765 |
| P44 GLM PILOT, Relace | 0.784 | 0.92 | 0.852 |
| P44 DS R, Baidu | 0.567 | 0.786 | 0.722 |
| X3b GLM main, Relace | 0.853 | 0.91 | 0.938 |
| X3b Gemini main, Google | 0.358 | 0.881 | 0.407 |
