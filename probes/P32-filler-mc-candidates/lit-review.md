# P32 literature review: candidate burden under filler tokens

Saved from the probe brief (`brief.md`, "Literature"), which records a targeted internal and external search done while the design was agreed (Claude and gpt-6-astra, three consensus rounds, 2026-10-13). This file restates those findings; sources marked "abstract" or "summary only" were not read in full.

## Gap

No controlled option-count study under filler tokens exists (searched internally in `papers/` and the repo, and externally). The nearest large-scale evidence, MMLU-Pro's move from 4 to 10 options, confounds option count with item difficulty. So a test that holds per-distractor difficulty fixed while varying the number of listed candidates is new.

## Sources

- **P06 F2** (this repo, `probes/P06-filler-token-computation/note.md`, item 4 and "F2"). On Brauer et al.'s task, DeepSeek V4 Flash's uplift from 100 dots was +11.2 / +11.4 / +21.8 points with 2 / 5 / 10 options. As a share of headroom above base accuracy that is roughly flat (55%, 41%, 48%). This is the confounded version of P32: distractor construction (variable-derived values first, then nearby numbers) and headroom both changed with option count.
- **P06 F8.5** (same note). Readouts of distractor options at the filler were elevated over unlisted controls with 10 options, but the elevation also appeared with no options shown and in the pre-padding null, so it reflected how candidate values were chosen and was uninterpretable. Lesson for P32: build listed distractors and unlisted controls the same way, from one fixed pool per item.
- **Baherwani, Goldstein & Panda 2026**, "Not All LLM Reasoning is Visible in the Chain-of-Thought" (`papers/neel_brainstorm/INDEX.md` row 74, §6.1). They speculate that filler lets the model explore multiple candidates, noting that RL raised pass@8 but not pass@1; they state there is "no direct evidence". P32's cheap extra (pass@k vs pass@1 with and without filler) is a direct look at that.
- **Brauer, Verdun & Marks 2026**, "Reading Between the Dots" (row 69). A question → filler → answer relay in DeepSeek V3 on the system-of-equations ("varbind") task; P06 F1 reproduced the size of their uplift on V4 Flash (+25.0 points with 500 dots). P32 uses their generator (github.com/kaleybrauer/filler-token-reasoning @ a701927) and prompt format via P06's re-implementation.
- **Wiegreffe et al. 2024**, arXiv 2407.15018 (abstract only). Answer-symbol selection in multiple choice is done by sparse mid-layer heads, a mechanism separate from computing the answer. Reason to score by the answer value and to keep letter binding out of the measurement.
- **Leviathan et al. 2025**, arXiv 2512.14982 (summary only, not captured in `papers/`). Option order matters, and repeating the prompt beats period padding. Reason to randomise answer position.
- **V4 Flash architecture** (model config verified in P06). Sliding window of 128 tokens in every layer; 20 CSA layers pool 4 tokens per entry with top-512 selection (so every entry is selected under ~2,048 tokens of context), and 20 HCA layers pool 128 tokens per entry. Options placed ~500 tokens before the answer position are therefore visible to it only in compressed form; options placed after the filler are inside the 128-token window. P32 lists options both ways and reports total context length.
