# Does agy's /compact summary follow the session model? (probe)

Short sessions per session model, each ending in one /compact (`scripts/week2_agy_model_probe.py`). Descriptive; a few sessions per model.

| model | n | end | calls | checkpoints | summary chars | headings | numbered-bold | bullets | first person | self-reference | compact input / last call input | compact thinking tokens | gen_metadata rows / distinct models |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gemini-3.8-flash-low | 1 | submitted | 16 | 1 | 2399 | 0 | 7 | 20 | 0 | – | 0.0 | 0 | 16 / gemini-3.8-flash, gemini-3.8-flash-low |
| gemini-3.8-flash-low | 2 | submitted | 15 | 1 | 2338 | 0 | 7 | 20 | 0 | – | 0.0 | 0 | 15 / gemini-3.8-flash, gemini-3.8-flash-low |
| gemini-3.8-flash-low | 3 | submitted | 15 | 1 | 2560 | 7 | 0 | 27 | 0 | – | 0.0 | 0 | 15 / gemini-3.8-flash, gemini-3.8-flash-low |
| claude-sonnet-5-5-low | 1 | submitted | 8 | 1 | 2410 | 0 | 0 | 21 | 11 | – | 0.0 | 0 | 8 / claude-sonnet-5-5-low |
| claude-sonnet-5-5-low | 2 | submitted | 11 | 1 | 2014 | 7 | 0 | 11 | 6 | – | 0.0 | 0 | 11 / claude-sonnet-5-5-low |
| claude-sonnet-5-5-low | 3 | submitted | 7 | 1 | 2132 | 0 | 0 | 15 | 7 | – | 0.0 | 0 | 7 / claude-sonnet-5-5-low |

## First lines of each summary

- claude-sonnet-5-5-low #1: **1. Task Overview**
- claude-sonnet-5-5-low #2: ## 1. Task Overview
- claude-sonnet-5-5-low #3: **1. Task Overview**
- gemini-3.8-flash-low #1: 1. **Task Overview**
- gemini-3.8-flash-low #2: 1. **Task Overview**
- gemini-3.8-flash-low #3: ### 1. Task Overview

## Reading (builder's, 29 Oct; descriptive, n = 3 per model)

- **Style follows the session model.**
  - Sonnet-session summaries are first-person narrative in 3/3: "I explored `/app`…", "The user asked me…", "I ran `go test ./token ./parser`…". That's 6–11 first-person markers each, mostly with the bold-number heading `**1. Task Overview**`.
  - Gemini-session summaries have no first person in 3/3. They are terse and telegraphic, with bold labels (`**Constraints**:`, "Read `/app/README.md` and inspected…"), mostly with the `1. **Task Overview**` heading.
  - Both follow agy's six-section template.
  - This fits agy's /compact being written by the session model, not a fixed summarizer. 3/3 vs 0/3 is suggestive only: Fisher's two-sided p = 0.1.
- **Two signals are unavailable:**
  - agy's cumulative usage doesn't change across the /compact turn (delta 0 in all 6), so the summarizer's tokens aren't counted where we can see them;
  - gen_metadata has one row per agent call and none for the summary under either model (rows = calls in all 6).
- **Unverified:** whether Google routes the Sonnet summary call through Anthropic's model or a Google proxy of it.
