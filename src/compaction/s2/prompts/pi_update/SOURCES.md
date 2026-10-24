# pi's update prompt (repeated compaction)

Fetched 2026-10-24 with `gh api -H "Accept: application/vnd.github.raw" repos/badlogic/pi-mono/contents/packages/coding-agent/src/core/compaction/<file>?ref=b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6`, the commit S1 and S2 replay (`probes/P49-long-context-compaction/prompts/SOURCES.md`). Source sha256 (raw bytes): `compaction.ts` `d5aebd41333957b57fa3f1bee2a18b3c00b5d47bb1b4af6f913cf8cd791099c8`, `utils.ts` `37eab7698cdbff8b5987d4529005ea16d7e1ae4a5344d7d0443c3466444aa90e`. Extracted by script: the template literal evaluated, with its one interpolation (`${UPDATE_SUMMARIZATION_INSTRUCTIONS}`) filled from the same file; every other byte is verbatim; no trailing newline.

| File | Source (path @ commit) | sha256 |
|---|---|---|
| `pi_update_summarization_prompt.txt` | `compaction/compaction.ts` @ `b06c1f2`, `UPDATE_SUMMARIZATION_PROMPT` (lead line + `UPDATE_SUMMARIZATION_INSTRUCTIONS`) | `240c52982209146eae47d73c7172f6ba1dab60f44520bcb6a6ec1a883fef2ec7` |

Checked against the same fetch: P49's `pi_summarization_prompt.txt`, `pi_turn_prefix_prompt.txt` (compaction.ts) and `pi_system_prompt.txt` (utils.ts `SUMMARIZATION_SYSTEM_PROMPT`) are byte-identical to `b06c1f2`, so the update call uses the same system prompt as the first.

**Older copy.** P44's `probes/P44-handoff-methods-alignment/prompts/pimono_update_instructions.txt` (pi `9a100c7c`, sha256 `d5dd28a3...`) is `UPDATE_SUMMARIZATION_INSTRUCTIONS` alone; it is byte-identical to the instructions part of this file. This file adds the lead line, as `UPDATE_SUMMARIZATION_PROMPT` does at `b06c1f2`.

## What the update call sends (checked against `compaction.ts` @ b06c1f2)

- **When:** `prepareCompaction` finds the newest compaction entry on the session path (`prevCompactionIndex`) and takes its `summary` as `previousSummary` (the raw summary, not the `pi_wrapper.md` text around it).
- **Which messages are serialised:** `boundaryStart = prevCompactionIndex + 1`, so the summarized range is the previous compaction's kept tail plus everything after it, up to the new cut. **The previous summary message itself is not serialised** (it is passed only inside `<previous-summary>`). The cut point (`findProjectedCutPoint`) is searched in `[boundaryStart, end)`, so the new verbatim tail and any split-turn start lie inside that range too.
- **Prompt text** (`generateSummaryWithUsage`): `<conversation>\n` + `serializeConversation(messages)` + `\n</conversation>\n\n` + `<previous-summary>\n` + previousSummary + `\n</previous-summary>\n\n` + `UPDATE_SUMMARIZATION_PROMPT`. System prompt `SUMMARIZATION_SYSTEM_PROMPT`; max tokens `floor(0.8 * reserveTokens)` (13,107, as the first call).
- **Split turn** (`compact`): the history call uses the update prompt as above when there are messages before the turn start; if there are none, no history call is made and the history text is `previousSummary` itself (not "No prior history."). The turn-prefix call is unchanged (no previous summary). The merge is the same: `history + "\n\n---\n\n**Turn Context (split turn):**\n\n" + turn prefix`.
- **File lists:** pi appends `<read-files>`/`<modified-files>` from its own read/write/edit tool calls and the previous compaction's details. Our agent has only `bash` and `ask_user`, so both lists are empty and nothing is appended, as in the first-compaction port.

Implemented in `compaction.s2.compact.compact(..., previous_summary=, previous_summary_index=)`; `previous_summary_index` is the index of the message carrying the previous summary in the messages passed in, and that message is left out of the summarizer's input.
