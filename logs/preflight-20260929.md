# Pre-flight checks, 2026-09-29 (all free, read-only)

| Check | Result |
|---|---|
| `.env` | Copied from `../mats12_neel_working/.env` at Jacob's request; stale pod and model variables removed; gitignored, mode 600 |
| RunPod balance | **$27.55**, current spend $0/h. Below E1's planned GPU spend ($36.4) and its GPU limit ($53). **Top-up needed before Part 1.** |
| RunPod stock (1 GPU) | H100 80GB HBM3 $2.69/h, H100 NVL $2.59/h, A100-SXM4-80GB $1.39/h; **stock "Low" for all three** |
| Hugging Face | Token valid (user `jrepifano`); `Olmo-3-1025-7B` and `Olmo-3-7B-Instruct` resolve at the pinned revisions |
| OpenAI | Key valid; `gpt-6-luna` available. Account balance can't be checked by API. |
| OpenRouter | Key valid, no per-key limit; **$7.80 of credit left**. E1's DeepSeek judging is ≈ $9.5. **Top-up needed before Part 2.** |
| EM datasets | `../mats12_neel_working/data/raw/{bad,good}_medical_advice.jsonl`, 7,049 rows each |

## Re-check, 2026-09-29T17:47Z (free, read-only)

| Check | Result |
|---|---|
| RunPod balance | **$127.55** after Jacob's top-up (GraphQL `myself { clientBalance }`), spend $0/h. Covers E1's GPU limit ($53). |
| `.env` | Lines 17–19 hold unquoted values with spaces, so `source .env` in a shell errors on them (harmless, other keys load). E1 scripts parse it with python-dotenv, not `source`. |
