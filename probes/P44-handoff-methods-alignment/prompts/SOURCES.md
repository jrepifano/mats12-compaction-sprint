# P44 compaction prompt sources (byte copies of P38's files)

pi-mono (`badlogic/pi-mono`, commit `9a100c7cc4707f013438b13969c6671bc0a33c4c`, MIT), the same commit as P01/P08:

| File | Source |
|---|---|
| `pimono_system_prompt.txt` | byte copy of P01's file (`utils.ts`, `SUMMARIZATION_SYSTEM_PROMPT`) |
| `pimono_summarization_prompt.txt` | byte copy of P01's file (`compaction.ts`, `SUMMARIZATION_PROMPT`); checked identical to the commit's text on 2026-10-13 |
| `pimono_update_instructions.txt` | `compaction.ts`, `UPDATE_SUMMARIZATION_INSTRUCTIONS`, fetched with `gh api .../contents/...?ref=<commit>` on 2026-10-13. The update prompt is `"The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.\n\n" + instructions`; the request is `<conversation>…</conversation>\n\n<previous-summary>\n…\n</previous-summary>\n\n` + prompt (`generateSummaryWithUsage`) |
| `pi_mono_wrapper.md` | byte copy of P07's `wrappers/pi_mono_wrapper.md` (the text pi-mono puts around a summary when it resumes) |
