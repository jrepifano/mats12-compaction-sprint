# Compaction as Epistemic Failure - How Agentic LLM Tools Fabricate Confirmed Results from Killed Processes

- **Authors:** Hiroki Tamba (independent researcher)
- **arXiv/venue:** arXiv 2607.13071v1 [cs.SE], 11 Jul 2026 (preprint, 9 pp.)
- **URL:** https://arxiv.org/abs/2607.13071 (linked issue: https://github.com/anthropics/claude-code/issues/76584)
- Captured 2026-10-14 for P49.
- **Reading depth:** full text read verbatim from `pdftotext -layout` (abstract, §1–8, references). Text extraction saved next to this file (`.txt`). This replaces the fetch-tool summary that INDEX_a row 45 recorded.

## Verbatim: abstract

> Agentic LLM coding tools compress long session histories into compaction summaries that subsequent sessions inherit as ground truth. This paper documents a failure mode in Claude Code where partial standard output from timed-out commands (exit code 143) is recorded in compaction summaries as confirmed results, propagating false positives across sessions and model versions without re-verification. The underlying mechanism is a conflation of observation and persistence, where information that appeared in the terminal is treated as equivalent to information written to durable storage. This finding extends the analysis of LLM self-evaluation failures reported in prior work on non-determinism in LLM-as-judge grading [1] by showing that agentic tools exhibit analogous reliability deficits when reporting on their own operational outcomes. The failure has direct implications for any workflow that relies on agentic session continuity for data processing, scientific computation, or multi-step automation.

## Verbatim: key results

There is no experiment and there are no rates. It is a single-user case report (n = 1 primary incident plus three anecdotes).

> "1. The script was initiated within a Claude Code session running model version Opus 4.7.
> 2. The first two iterations (A1 and A2) executed successfully: the API returned HTTP 200 responses, and the results appeared in the terminal output.
> 3. Before the script could write its cumulative results to the output JSON file, the process was terminated by the system timeout mechanism with exit code 143 (SIGTERM).
> 4. Claude Code's compaction mechanism generated a summary of the session state. This summary recorded A1 and A2 as 'completed with specific results,' including extracted field values from the API responses.
> 5. A new session was initiated, running on model version Opus 4.6. This session inherited the compaction summary and treated the A1 and A2 entries as confirmed data points.
> ...
> 7. When the actual output file (api_openai_baseline_20260711.json) was inspected, it contained zero successful entries. All 11 query attempts recorded in the file had failed with HTTP 400 errors."

> "Layer 1 (Compaction): The compaction summary treated terminal output observed before process termination as equivalent to confirmed, persisted results. Exit code 143 was not recorded as a disqualifying condition."

> "All four instances follow the same rule: context-internal equals true, context-external equals nonexistent."

Context length and compaction count are not reported. The only description of the regime: "The Claude Code failure occurred at the end of a multi-hour session with substantial context accumulation across heterogeneous tasks (database repair, repository management, patch development, and API experimentation)." It is not stated whether this was the first or a later compaction, nor what the summariser prompt was ("the compaction mechanism in Claude Code is not publicly documented in sufficient detail"). The three corroborating instances (a silently failing DB sync, a function never executed, a stale file listing) are not compaction-specific; they concern in-context information being treated as verified.

> "Sixth, an informal, single-session test (n=1) with ChatGPT Codex [3] suggested that a file-first verification approach correctly distinguished completed from interrupted runs under minimal-context conditions. However, the author subsequently exported and deleted all ChatGPT data ... the test artifacts are therefore no longer available".

## Relevance to P49

- **What gets mutated:** an "observed but not persisted / process killed" status was promoted to "completed with results". This status-promotion mutation (failed or unknown → succeeded) is a natural "live-system status" item for P49's fact inventory: plant a killed or timed-out job and score whether the summary records the exit status.
- **No rates, no compaction count, no context length.** It gives no evidence on whether the second compaction matters more than the first. Cite it as a motivating anecdote only.
- **Cross-model inheritance** (summary written by one model, consumed by another) is a cheap extra arm, but not central to P49.
- **Design lesson:** score the summary for exit codes and qualifiers ("unconfirmed", "killed"), not only for whether the fact is present. A fact can survive while its epistemic status mutates.
