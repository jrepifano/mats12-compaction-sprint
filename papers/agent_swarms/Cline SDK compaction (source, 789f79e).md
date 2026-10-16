# Cline SDK compaction (source code, cline/cline at 789f79e)

**Source:** https://github.com/cline/cline , commit `789f79e46d3ba579717e8dc8be35fe28af935b06` (HEAD of the default branch, cloned 2026-10-14), package `sdk/packages/core` version 0.0.93.
**Licence:** Apache-2.0. Excerpts quoted with attribution.
**Retrieved:** 2026-10-14, shallow `git clone`, read only.
**Reading depth:** skim. Only the trigger constants, the trigger computation and the summary-request builder were read (excerpts verbatim). The agentic strategy's full flow, the basic strategy and the older VS Code extension's condense logic were not read. Treat the Summary as partial.

## Summary (partial reading)

- Default strategy `"agentic"` (LLM summary), with a deterministic `"basic"` fallback.
- Trigger: request input tokens >= 0.9 x max input tokens, where max input = the model's `maxInputTokens` (capped at the window) or 0.9 x the context window if only the window is known (128,000 if neither). With only a window known: 81% of the window, i.e. 162,000 for 200k and 810,000 for 1M.
- Keeps the most recent ~20,000 tokens (`DEFAULT_PRESERVE_RECENT_TOKENS`); target after compaction 0.7 of the trigger.
- Summary request: a short Goal/State/Highlights/Next/Files template, the previous summary (if any) and the serialised conversation; tool results and file contents are capped at 2,000 characters (`TOOL_RESULT_CHAR_LIMIT`, `FILE_CONTENT_CHAR_LIMIT`; where exactly they are applied was not checked). Summary output cap 8,192 tokens.

## Verbatim excerpts

#### `src/extensions/context/compaction-shared.ts` lines 15-36 (https://github.com/cline/cline/blob/789f79e46d3ba579717e8dc8be35fe28af935b06/sdk/packages/core/src/extensions/context/compaction-shared.ts#L15-L36)

```ts
export const DEFAULT_MAX_INPUT_TOKENS = 128_000;
/** Estimate the usable input share when only a context window is reported. */
export const CONTEXT_WINDOW_INPUT_RATIO = 0.9;
/** Compact once the transcript consumes this share of the usable input budget. */
export const COMPACTION_TRIGGER_RATIO = 0.9;
/**
 * Upper bound on how far the provider's actual input count may scale the
 * compaction budget down relative to the character-based estimate. Dense
 * content realistically tokenizes ~2-3x denser than the 3-chars-per-token
 * assumption; the cap stops a pathologically small estimate from collapsing
 * the budget and compacting every turn.
 */
export const MAX_INPUT_UNDERESTIMATE_FACTOR = 4;
export const DEFAULT_TARGET_RATIO = 0.7;
export const DEFAULT_PRESERVE_RECENT_TOKENS = 20_000;
// Headroom for the summarizer's output. Raised from 4096: models that reason
// by default can spend part of a tight budget on thinking and return no
// summary text at all, which skips compaction entirely. 8192 leaves room for a
// real summary even when some reasoning slips through.
export const DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS = 8_192;
export const TOOL_RESULT_CHAR_LIMIT = 2_000;
export const FILE_CONTENT_CHAR_LIMIT = 2_000;
```

#### `src/extensions/context/compaction-shared.ts` lines 65-84 (https://github.com/cline/cline/blob/789f79e46d3ba579717e8dc8be35fe28af935b06/sdk/packages/core/src/extensions/context/compaction-shared.ts#L65-L84)

```ts
export function resolveEffectiveMaxInputTokens(
	input: Pick<ModelInfo, "maxInputTokens" | "contextWindow">,
): number | undefined {
	const contextWindow = isPositiveFiniteNumber(input.contextWindow)
		? input.contextWindow
		: undefined;
	const maxInputTokens = isPositiveFiniteNumber(input.maxInputTokens)
		? input.maxInputTokens
		: undefined;

	if (maxInputTokens !== undefined) {
		return contextWindow === undefined
			? maxInputTokens
			: Math.min(maxInputTokens, contextWindow);
	}

	return contextWindow === undefined
		? undefined
		: contextWindow * CONTEXT_WINDOW_INPUT_RATIO;
}
```

#### `src/extensions/context/compaction-shared.ts` lines 678-712 (https://github.com/cline/cline/blob/789f79e46d3ba579717e8dc8be35fe28af935b06/sdk/packages/core/src/extensions/context/compaction-shared.ts#L678-L712)

```ts
export function buildSummaryRequest(options: {
	previousSummary?: string;
	conversationText: string;
	fileOps: FileOperationSummary;
}): string {
	const parts: string[] = [
		`Summarize this session for continuation. Be concise and factual.

## Goal
One sentence: what is being built or fixed.

## State
- Done: completed steps
- In Progress: current work
- Blocked: blockers or open questions

## Highlights
Key technical choices or notable findings (omit if none).

## Next
Immediate next steps.

## Files
Read: ${options.fileOps.readFiles.join(", ") || "none"}
Edited: ${options.fileOps.modifiedFiles.join(", ") || "none"}`,
	];

	if (options.previousSummary?.trim()) {
		parts.push(`Previous summary:\n${options.previousSummary.trim()}`);
	}

	parts.push(`Conversation:\n${options.conversationText || "(empty)"}`);

	return parts.join("\n\n");
}
```

#### `src/extensions/context/compaction.ts` lines 296-297 (https://github.com/cline/cline/blob/789f79e46d3ba579717e8dc8be35fe28af935b06/sdk/packages/core/src/extensions/context/compaction.ts#L296-L297)

```ts
	const strategy = userCompaction?.strategy ?? "agentic";
	const runBuiltinStrategy = BUILTIN_COMPACTION_STRATEGIES[strategy];
```

#### `src/extensions/context/compaction.ts` lines 354-361 (https://github.com/cline/cline/blob/789f79e46d3ba579717e8dc8be35fe28af935b06/sdk/packages/core/src/extensions/context/compaction.ts#L354-L361)

```ts
		const maxInputTokens = rawMaxInputTokens / underestimateFactor;
		const requestTriggerTokens = maxInputTokens * COMPACTION_TRIGGER_RATIO;
		const messageTriggerTokens = translateRequestBudgetToMessages(
			requestTriggerTokens,
			requestOverheadTokens,
		);
		// Equivalent to comparing the provider's actual count against the unscaled
		// trigger, because the budget above already carries the ratio.
```
