# Compaction prompt sources

Fetched verbatim from GitHub on 2026-10-11 with `gh api .../contents/...`; the files here are byte-for-byte copies (the pi-mono strings are the contents of the TypeScript template literals, which contain no `${}` interpolation).

| File | Source | Commit | License |
|---|---|---|---|
| `codex_prompt.md` | `openai/codex` `codex-rs/prompts/templates/compact/prompt.md` | `ba2b67f9cda954bcdda43c2a65ac58e807b996bd` | Apache-2.0 |
| `codex_summary_prefix.md` | `openai/codex` `codex-rs/prompts/templates/compact/summary_prefix.md` (what Codex puts before the summary when it resumes; not sent to the summarizer) | same | Apache-2.0 |
| `openhands_summarizing_system.j2` | `OpenHands/software-agent-sdk` `openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_system.j2` | `032e0e998ed09c5d3c6e6fae7d85a9fbee0335cf` | MIT |
| `openhands_summarizing_events.j2` | same directory, `summarizing_events.j2` | same | MIT |
| `pimono_system_prompt.txt` | `badlogic/pi-mono` `packages/coding-agent/src/core/compaction/utils.ts`, `SUMMARIZATION_SYSTEM_PROMPT` | `9a100c7cc4707f013438b13969c6671bc0a33c4c` | MIT |
| `pimono_summarization_prompt.txt` | same package, `compaction.ts`, `SUMMARIZATION_PROMPT` | same | MIT |
