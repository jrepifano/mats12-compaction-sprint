| model | rank by | provider (tag) | quant | $in / $out / $cache-read per M | tok/s p50 | latency p50 s | uptime 30m / 1d | max_out | $/session W=128k (h=1) | same, h=0.82 | wall-clock h |
|---|---|---|---|---|---|---|---|---|---|---|---|
| z-ai/glm-5.3-flash | cost | StreamLake (streamlake/fp8) | fp8 | 0.0435 / 0.145 / 0.0087 | 38 | 1.84 | 100 / 96 | 128000 | 0.14 | 0.18 | 2.9 |
| z-ai/glm-5.3-flash | cost | DeepInfra (deepinfra/fp4) | fp4 | 0.075 / 0.25 / 0.015 | 23 | 1.61 | 100 / 98 | 131072 | 0.24 | 0.31 | 4.7 |
| z-ai/glm-5.3-flash | cost | Novita (novita/fp8) | fp8 | 0.084 / 0.28 / 0.0168 | 24 | 2.98 | 100 / 99 | 131072 | 0.27 | 0.35 | 4.5 |
| z-ai/glm-5.3-flash | speed | CoreWeave (coreweave/nvfp4) | nvfp4 | 0.15 / 0.5 / 0.029 | 155 | 0.66 | 100 / 97 | 943718 | 0.47 | 0.61 | 0.8 |
| z-ai/glm-5.3-flash | speed | Wafer (wafer) | unknown | 0.12 / 0.5 / 0.115 | 143.5 | 0.47 | 100 / 100 | 131072 | 1.07 | 1.07 | 0.8 |
| z-ai/glm-5.3-flash | speed | Parasail (parasail/fast) | fp4 | 0.188 / 0.625 / 0.0375 | 133 | 0.54 | 100 / 100 | 943718 | 0.59 | 0.78 | 0.9 |
| deepseek/deepseek-v4-flash | cost | StreamLake (streamlake/fp8) | fp8 | 0.0826 / 0.165 / 0.0165 | 43 | 1.28 | 100 / 100 | 384000 | 0.30 | 0.45 | 2.2 |
| deepseek/deepseek-v4-flash | cost | DeepInfra (deepinfra/fp8) | fp8 | 0.09 / 0.18 / 0.018 | 15 | 7.17 | 100 / 100 | 65536 | 0.33 | 0.49 | 6.3 |
| deepseek/deepseek-v4-flash | cost | GMICloud (gmicloud/fp8) | fp8 | 0.091 / 0.182 / 0.0182 | 61 | 1.43 | 100 / 100 | 943717 | 0.33 | 0.50 | 1.6 |
| deepseek/deepseek-v4-flash | speed | Relace (relace/fp4) | fp4 | 0.05 / 1.28 / 0.05 | 105 | 0.63 | 100 / 100 | 943718 | 1.07 | 1.07 | 1.0 |
| deepseek/deepseek-v4-flash | speed | Baidu (baidu/fp8) | fp8 | 0.14 / 0.28 / 0.028 | 100 | 0.97 | 100 / 100 | 131072 | 0.51 | 0.76 | 1.0 |
| deepseek/deepseek-v4-flash | speed | Wafer (wafer) | unknown | 0.51 / 0.17 / 0.013 | 82 | 0.74 | 97 / 100 | 943718 | 0.45 | 1.57 | 1.2 |
| deepseek/deepseek-v4.1-flash | cost | Sail Research (sail-research/fp4) | fp4 | 0.09 / 0.3 / 0.006 | 57 | 1.25 | 100 / 95 | 384000 | 0.21 | 0.40 | 1.7 |
| deepseek/deepseek-v4.1-flash | cost | DeepInfra (deepinfra/fp8) | fp8 | 0.14 / 0.42 / 0.0042 | 44 | 1.12 | 100 / 100 | 131072 | 0.25 | 0.56 | 2.2 |
| deepseek/deepseek-v4.1-flash | cost | StreamLake (streamlake/fp8) | fp8 | 0.147 / 0.588 / 0.00294 | 66 | 2.06 | 100 / 100 | 384000 | 0.29 | 0.61 | 1.5 |
| deepseek/deepseek-v4.1-flash | speed | BaseTen (baseten/fast) | fp32 | 0.6 / 2.4 / 0.014 | 287 | 0.32 | 100 / 100 | 131072 | 1.21 | 2.53 | 0.4 |
| deepseek/deepseek-v4.1-flash | speed | Fireworks (fireworks/us) | unknown | 0.45 / 1.8 / 0.009 | 263 | 0.32 | 100 / 100 | 943718 | 0.89 | 1.88 | 0.5 |
| deepseek/deepseek-v4.1-flash | speed | BaseTen (baseten/fp8) | fp8 | 0.3 / 1.2 / 0.007 | 217 | 0.59 | 100 / 100 | 262144 | 0.61 | 1.26 | 0.5 |
| z-ai/glm-5.3 | cost | Relace (relace) | unknown | 0.06 / 4.4 / 0.0558 | 58 | 1.81 | 100 / 100 | 131072 | 2.14 | 2.15 | 1.7 |
| z-ai/glm-5.3 | cost | InferenceNet (inference-net/fp4) | fp4 | 0.08 / 4.4 / 0.06 | 77 | 0.81 | 100 / 100 | 943718 | 2.20 | 2.25 | 1.3 |
| z-ai/glm-5.3 | cost | Tenstorrent (tenstorrent/fp8) | fp8 | 0.6 / 2.5 / 0.1 | 74 | 1.51 | 100 / 97 | 131072 | 2.35 | 3.48 | 1.4 |
| z-ai/glm-5.3 | speed | Fireworks (fireworks/fast) | unknown | 2.1 / 6.6 / 0.39 | 189.5 | 0.75 | 100 / 97 | 943718 | 8.06 | 11.91 | 0.6 |
| z-ai/glm-5.3 | speed | Crusoe (crusoe/fp4) | fp4 | 1.4 / 4.4 / 0.26 | 180 | 0.61 | 100 / 100 | 943718 | 5.38 | 7.94 | 0.6 |
| z-ai/glm-5.3 | speed | BaseTen (baseten/us/fast) | fp8 | 2.1 / 6.6 / 0.21 | 176 | 0.46 | 100 / 100 | 262144 | 5.74 | 10.00 | 0.6 |
| z-ai/glm-5.3-flashx | cost | Z.AI (z-ai/fp8) | fp8 | 0.37 / 1.25 / 0.09 | 74 | 3.02 | 100 / 100 | 131072 | 1.72 | 2.35 | 1.4 |
| deepseek/deepseek-v4-pro | cost | StreamLake (streamlake/fp8) | fp8 | 0.955 / 1.91 / 0.0796 | 35 | 1.84 | 100 / 99 | 384000 | 2.06 | 4.03 | 2.7 |
| deepseek/deepseek-v4-pro | cost | GMICloud (gmicloud/fp8) | fp8 | 0.957 / 1.91 / 0.0798 | 32 | 2.32 | 100 / 100 | 943718 | 2.07 | 4.04 | 3.0 |
| deepseek/deepseek-v4-pro | cost | DeepInfra (deepinfra/fp8) | fp8 | 1.3 / 2.6 / 0.1 | 36 | 1.13 | 100 / 100 | 16384 | 2.70 | 5.40 | 2.6 |
| deepseek/deepseek-v4-pro | speed | Azure (azure/us) | unknown | 1.91 / 3.83 / 0.16 | 105 | 1.92 | 100 / 98 | 384000 | 4.14 | 8.07 | 1.0 |
| deepseek/deepseek-v4-pro | speed | Alibaba (alibaba/fp8/us-east-1) | fp8 | 1.56 / 3.12 / 0.13 | 54 | 1.64 | 100 / 100 | 393216 | 3.37 | 6.58 | 1.8 |
| deepseek/deepseek-v4-pro | speed | Cloudflare (cloudflare) | unknown | 1.15 / 2.55 / 0.2 | 50 | 1.10 | 100 / 100 | 943718 | 3.90 | 6.04 | 1.9 |
| moonshotai/kimi-k3 | cost | Decart (decart/mxfp4) | mxfp4 | 2.13 / 10.7 / 0.213 | 50 | 0.99 | 100 / 99 | 943718 | 7.08 | 11.39 | 1.9 |
| moonshotai/kimi-k3 | cost | Makora (makora/fp4) | fp4 | 1.53 / 12.8 / 0.204 | 35 | 1.80 | 100 / 90 | 943718 | 7.36 | 10.34 | 2.7 |
| moonshotai/kimi-k3 | cost | Relace (relace/fp4) | fp4 | 0.35 / 14 / 0.3 | 42.5 | 1.60 | 100 / 100 | 943718 | 8.46 | 8.57 | 2.3 |
| moonshotai/kimi-k3 | speed | Amazon Bedrock (amazon-bedrock/us-east-2) | unknown | 3 / 15 / 0.3 | 92 | 0.53 | 100 / 87 | 128000 | 9.97 | 16.04 | 1.1 |
| moonshotai/kimi-k3 | speed | InferenceNet (inference-net/fast) | fp4 | 2.49 / 15 / 0.35 | 72 | 0.85 | 100 / 100 | 943718 | 10.39 | 15.20 | 1.4 |
| moonshotai/kimi-k3 | speed | Fireworks (fireworks/fast) | unknown | 4.5 / 22.5 / 0.45 | 70 | 1.49 | 100 / 100 | 943718 | 14.96 | 24.07 | 1.4 |
| qwen/qwen3.7-plus | cost | Alibaba (alibaba/us-east-1) | unknown | 0.32 / 1.28 / 0.064 | 48 | 1.62 | 100 / 100 | 131072 | 1.37 | 1.95 | 2.0 |
| qwen/qwen3.7-plus | cost | Alibaba (alibaba) | unknown | 0.32 / 1.28 / 0.064 | 42 | 1.12 | 100 / 100 | 131072 | 1.37 | 1.95 | 2.3 |
| qwen/qwen3.7-max | cost | Alibaba (alibaba) | unknown | 1.48 / 4.42 / 0.295 | 60 | 1.15 | 100 / 100 | 131072 | 5.87 | 8.52 | 1.6 |
| qwen/qwen3.7-max | cost | Alibaba (alibaba/us-east-1) | unknown | 2 / 6 / 0.4 | 57 | 1.36 | 100 / 100 | 131072 | 7.96 | 11.56 | 1.7 |
| qwen/qwen3.8-27b | cost | Tenstorrent (tenstorrent/fp4) | fp4 | 0.02 / 0.5 / 0.009 | 32 | 0.50 | 100 / 100 | 65536 | 0.28 | 0.31 | 2.9 |
| qwen/qwen3.8-27b | cost | Chutes (chutes/fp8) | fp8 | 0.039 / 1.25 / 0.0039 | 32 | 2.45 | 100 / 100 | 65536 | 0.46 | 0.54 | 3.0 |
| qwen/qwen3.8-27b | cost | Near AI (near-ai/fp8) | fp8 | 0.04 / 1.35 / 0.018 | 52 | 1.51 | 100 / 100 | 65536 | 0.68 | 0.73 | 1.9 |
| qwen/qwen3.8-27b | speed | ModelRun (modelrun/fp4) | fp4 | 0.2 / 2.5 / 0.09 | 190 | 0.28 | 100 / 100 | 235929 | 2.04 | 2.29 | 0.6 |
| qwen/qwen3.8-27b | speed | DekaLLM (dekallm) | unknown | 0.04 / 2.5 / 0.02 | 112 | 1.11 | 100 / 100 | 235929 | 1.07 | 1.11 | 0.9 |
| qwen/qwen3.8-27b | speed | Venice (venice/fp8) | fp8 | 0.45 / 3.2 / - | 100 | 0.91 | None / 100 | 65536 | 7.01 | 7.01 | 1.0 |
| qwen/qwen3.8-flash | cost | Alibaba (alibaba/us-east-1) | unknown | 0.15 / 0.47 / 0.016 | 40 | 2.75 | 100 / 100 | 131072 | 0.42 | 0.72 | 2.4 |
| qwen/qwen3.8-flash | cost | Alibaba (alibaba) | unknown | 0.15 / 0.47 / 0.016 | 48 | 1.34 | 100 / 100 | 131072 | 0.42 | 0.72 | 2.0 |
| minimax/minimax-m3 | cost | GMICloud (gmicloud/fp8) | fp8 | 0.24 / 0.96 / 0.048 | 34 | 3.54 | 100 / 100 | 524288 | 1.03 | 1.46 | 2.9 |
| minimax/minimax-m3 | cost | CoreWeave (coreweave/fp4) | fp4 | 0.23 / 0.96 / 0.05 | 115 | 0.57 | 100 / 100 | 235929 | 1.05 | 1.46 | 0.9 |
| minimax/minimax-m3 | cost | ModelRun (modelrun/fp8) | fp8 | 0.3 / 1.3 / 0.05 | 124 | 0.58 | 100 / 100 | 943718 | 1.19 | 1.75 | 0.8 |
| minimax/minimax-m3 | speed | Mara (mara) | unknown | 0.6 / 2.4 / - | 142.5 | 1.56 | None / 98 | 943718 | 8.76 | 8.76 | 0.8 |
| openai/gpt-oss-120b | - | no eligible endpoint (ctx < 160k, no tools, or low uptime) | | | | | | | | | |
| google/gemini-3.8-flash | cost | Google AI Studio (google-ai-studio/flex) | unknown | 0.375 / 1.88 / 0.0375 | 87 | 1.94 | 100 / 100 | 65536 | 1.25 | 2.01 | 1.2 |
| google/gemini-3.8-flash | cost | Google (google-vertex/global/flex) | unknown | 0.375 / 1.88 / 0.0375 | 39.5 | 19.28 | 100 / 94 | 65536 | 1.25 | 2.01 | 3.3 |
| google/gemini-3.8-flash | cost | Google AI Studio (google-ai-studio) | unknown | 0.75 / 3.75 / 0.075 | 89 | 2.25 | 100 / 100 | 65536 | 2.49 | 4.01 | 1.2 |
| google/gemini-3.8-flash | speed | Google AI Studio (google-ai-studio/priority) | unknown | 1.35 / 6.75 / 0.135 | 96 | 3.48 | 100 / 94 | 65536 | 4.49 | 7.22 | 1.2 |
| google/gemini-3.5-flash-lite | cost | Google (google-vertex/global/flex) | unknown | 0.15 / 1.25 / 0.015 | 15 | 6.66 | 100 / 100 | 65536 | 0.66 | 0.96 | 6.3 |
| google/gemini-3.5-flash-lite | cost | Google AI Studio (google-ai-studio/flex) | unknown | 0.15 / 1.25 / 0.015 | 250 | 1.52 | 100 / 100 | 65536 | 0.66 | 0.96 | 0.5 |
| google/gemini-3.5-flash-lite | cost | Google AI Studio (google-ai-studio) | unknown | 0.3 / 2.5 / 0.03 | 110.5 | 0.69 | 100 / 100 | 65536 | 1.31 | 1.92 | 0.9 |
| google/gemini-3.5-flash-lite | speed | Google (google-vertex/us) | unknown | 0.33 / 2.75 / 0.033 | 205.5 | 1.14 | 100 / 100 | 65536 | 1.45 | 2.11 | 0.6 |
| google/gemini-3.5-flash-lite | speed | Google (google-vertex/global) | unknown | 0.3 / 2.5 / 0.03 | 128 | 0.61 | 100 / 100 | 65536 | 1.31 | 1.92 | 0.8 |
| xiaomi/mimo-v2.6-pro | cost | DeepInfra (deepinfra/fp8) | fp8 | 0.43 / 0.87 / 0.0036 | 29 | 1.05 | 100 / 100 | 943718 | 0.52 | 1.48 | 3.2 |
| xiaomi/mimo-v2.6-pro | cost | Xiaomi (xiaomi/fp8) | fp8 | 0.435 / 0.87 / 0.0036 | 34 | 4.03 | 97 / 99 | 131072 | 0.52 | 1.49 | 2.9 |
| xiaomi/mimo-v2.6-flash | cost | Makora (makora/fp8) | fp8 | 0.13 / 0.28 / 0.002 | 21 | 1.99 | 100 / 95 | 131072 | 0.17 | 0.46 | 4.4 |
| xiaomi/mimo-v2.6-flash | cost | Novita (novita/fp8) | fp8 | 0.14 / 0.28 / 0.0028 | 22 | 6.31 | 100 / 98 | 131072 | 0.19 | 0.50 | 4.4 |
| xiaomi/mimo-v2.6-flash | cost | Xiaomi (xiaomi/fp8) | fp8 | 0.14 / 0.28 / 0.0028 | 26 | 5.86 | 100 / 100 | 131072 | 0.19 | 0.50 | 3.8 |
| xiaomi/mimo-v2.6-flash | speed | Io Net (io-net/fp8) | fp8 | 0.15 / 0.29 / 0.025 | 60 | 1.41 | 100 / 99 | 131072 | 0.48 | 0.76 | 1.6 |
| anthropic/claude-haiku-5.5 | cost | Google (google-vertex/global) | unknown | 0.1 / 0.5 / 0.01 | 185 | 1.49 | 100 / 100 | 128000 | 0.33 | 0.53 | 0.7 |
| anthropic/claude-haiku-5.5 | cost | Claude Platform on AWS (claude-on-aws) | unknown | 0.1 / 0.5 / 0.01 | 138 | 1.29 | 100 / 100 | 128000 | 0.33 | 0.53 | 0.8 |
| anthropic/claude-haiku-5.5 | cost | Azure (azure/global) | unknown | 0.1 / 0.5 / 0.01 | 117 | 1.37 | 100 / 98 | 128000 | 0.33 | 0.53 | 0.9 |
| anthropic/claude-haiku-5.5 | speed | Amazon Bedrock (amazon-bedrock/eu-west-1) | unknown | 0.11 / 0.55 / 0.011 | 252 | 0.80 | 100 / 100 | 128000 | 0.37 | 0.59 | 0.5 |
| anthropic/claude-haiku-5.5 | speed | Google (google-vertex/europe) | unknown | 0.11 / 0.55 / 0.011 | 209 | 0.40 | 100 / 100 | 128000 | 0.37 | 0.59 | 0.5 |
| anthropic/claude-haiku-5.5 | speed | Google (google-vertex/us) | unknown | 0.11 / 0.55 / 0.011 | 201 | 1.17 | 100 / 100 | 128000 | 0.37 | 0.59 | 0.6 |
| meituan/longcat-2.0 | cost | AtlasCloud (atlas-cloud/fp8) | fp8 | 0.3 / 1.2 / 0.006 | 49 | 1.56 | 100 / 100 | 262144 | 0.59 | 1.25 | 2.0 |
| stepfun/step-5-preview | cost | StepFun (stepfun/fp8) | fp8 | 1 / 2.7 / 0.05 | 55 | 2.16 | 100 / 98 | 64000 | 1.95 | 4.09 | 1.8 |
