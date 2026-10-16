# P49 compaction prompt sources

Fetched 2026-10-16 with `gh api repos/<repo>/contents/<path>?ref=<commit>` (raw bytes) at the commits pinned in `plan.md` and `lit-notes/products.md`, and extracted by script: TypeScript template literals evaluated (none of the extracted ones interpolates), Inspect's `DEFAULT_SUMMARY_PROMPT` taken with `ast` and passed through `textwrap.dedent` as Python does at import. `{summary}` and `{completion}` mark where the harness inserts the summary (filled with `str.replace`, not `str.format`); `{addendums}` is Inspect's own `str.format` placeholder (empty here: no memory tool, no instructions). Every other byte is verbatim.

| File | Source (path @ commit) | sha256 |
|---|---|---|
| `codex_prompt.md` | `openai/codex` `codex-rs/prompts/templates/compact/prompt.md` @ `ac9db7d8b556c7c34a73793aed34b1f980a4fe39` (`SUMMARIZATION_PROMPT`, `include_str!`; ends with one `\n`) | `ab0c334d4faca17e3afbb9b16967c1b2fdcc7242a9a0880af57949fa236d6d07` |
| `codex_summary_prefix.md` | same commit, `codex-rs/prompts/templates/compact/summary_prefix.md` (`SUMMARY_PREFIX`; no trailing newline; `compact.rs` builds `format!("{SUMMARY_PREFIX}\n{summary}")`) | `e9b088e794a6bb9082ac053fcc760bd818d7e720ee4bcdc72c6e480de7b7cb0e` |
| `pi_system_prompt.txt` | `badlogic/pi-mono` `packages/coding-agent/src/core/compaction/utils.ts` @ `b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6`, `SUMMARIZATION_SYSTEM_PROMPT` | `c464889dcfa60441e642f291445b49523f263e6fb2725d0c25075543a2ec3f8f` |
| `pi_summarization_prompt.txt` | same commit, `compaction/compaction.ts`, `SUMMARIZATION_PROMPT` | `9b00aa68df1a64279bc36e9093367f638701d48ec82e3d08436f65092a515f9b` |
| `pi_turn_prefix_prompt.txt` | same commit, `compaction/compaction.ts`, `TURN_PREFIX_SUMMARIZATION_PROMPT` (the split-turn call) | `7b0a15be5e0fdcce338ce7c1ef1fdd386faed258cc25d03915a05b05e7a6a183` |
| `pi_wrapper.md` | same commit, `src/core/messages.ts`: `COMPACTION_SUMMARY_PREFIX + {summary} + COMPACTION_SUMMARY_SUFFIX` | `f161bc7435d250589e3c96b1078c4418ba77afaa6bcb7ede3ca6b14827ace3b9` |
| `inspect_summary_prompt.txt` | `UKGovernmentBEIS/inspect_ai` `src/inspect_ai/model/_compaction/summary.py` @ `3f9c2e3fb4cc4799577971f61d29a5c8cd381ed4` (0.3.280), `CompactionSummary.DEFAULT_SUMMARY_PROMPT` after `dedent` (starts with `\n`, ends `{addendums}\n`) | `2ae34b5e01cd954d4216724c4a23105517023212de2ae2cd0ba34d102a863cd9` |
| `inspect_resume_wrapper.txt` | same file, the f-string in `CompactionSummary.compact` that builds the summary `ChatMessageUser` (`{completion}` placeholder) | `fba5d03666bb5870315b7610fa497f4e6fe7d432909cbcb519aefe2e91444eee` |

`compact.prompt_sha256(method)` is the sha256 of the lines `name=<sha256>` of that method's files, in `compact.PROMPT_FILES` order.

## Differences from the older copies

- `codex_prompt.md`, `codex_summary_prefix.md`: byte-identical to `probes/P07-compaction-trust/wrappers/` (Codex `72f999ed`, 2026-10-11).
- `pi_wrapper.md`: byte-identical to P07's `pi_mono_wrapper.md` (pi `97195ec3`) and P44's copy.
- `pi_system_prompt.txt`, `pi_summarization_prompt.txt`: byte-identical to `probes/P44-handoff-methods-alignment/prompts/pimono_*.txt` (pi `9a100c7c`).
- `pi_turn_prefix_prompt.txt`, `inspect_summary_prompt.txt`, `inspect_resume_wrapper.txt`: new (no older copy). The Inspect prompt matches the indented text in `lit-notes/products.md`'s appendix after dedent.
- P44's `pimono_update_instructions.txt` (the repeated-compaction update prompt) is not copied: P49 runs pi at k = 1 only.
