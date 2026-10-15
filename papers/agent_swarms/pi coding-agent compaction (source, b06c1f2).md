# pi coding-agent compaction (source code, badlogic/pi-mono at commit b06c1f2)

**Source:** https://github.com/badlogic/pi-mono , commit `b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6` (committed 2026-10-14T22:23:59+02:00), package `packages/coding-agent` version 1.2.0. The package's own docs link to `github.com/earendil-works/pi` (the npm scope is `@earendil-works`); `badlogic/pi-mono` still serves the same tree.
**Licence:** MIT (repo `LICENSE`, Copyright (c) 2025 Mario Zechner). Excerpts quoted with attribution.
**Retrieved:** 2026-10-14, shallow `git clone`, read only (not built or run).
**Reading depth:** excerpts verbatim. `docs/compaction.md` read in full; `src/core/compaction/compaction.ts` read in part (trigger, prompts, summary generation, preparation); `utils.ts` serialization read in full. The Summary is my reading.

## Summary (my reading of the code and docs)

- **Trigger:** `contextTokens > contextWindow - reserveTokens`, with `reserveTokens` = 16,384 by default. 200k window: 183,616 tokens; 1M window: 983,616. Checked after tool results are appended and before the next assistant response, before a new user prompt, and as recovery after a context-overflow error or an early `stopReason: "length"`.
- **What is kept:** walking back from the end, the most recent ~20,000 tokens (`keepRecentTokens`, estimated at chars/4), cut at a user/assistant/bash/custom message, never at a tool result. If one user turn alone exceeds the budget, the cut lands inside it and the turn's earlier part is summarised separately (`TURN_PREFIX_SUMMARIZATION_PROMPT`), then merged.
- **Summariser:** the session's current model (with the session's thinking level), via a separate request: system prompt `SUMMARIZATION_SYSTEM_PROMPT`, one user message containing the serialised conversation in `<conversation>` tags, the previous summary in `<previous-summary>` tags if any, then the prompt. Output cap = min(0.8 x reserveTokens, model max) = 13,107 tokens by default. Prompt caching is turned off for the request.
- **Summariser input:** the conversation is serialised to text (`[User]:`, `[Assistant]:`, `[Assistant tool calls]:`, `[Tool result]:`). Thinking blocks are omitted. Each tool result is truncated to its first 2,000 characters plus a marker.
- **Resumed context:** system prompt, then the summary as a user message wrapped in "The conversation history before this point was compacted into the following summary: <summary>...</summary>", then the kept messages verbatim. Cumulative `<read-files>`/`<modified-files>` lists are appended to the summary.
- **Repeated compaction:** iterative. The next summary is generated with `UPDATE_SUMMARIZATION_PROMPT` from the previous summary text plus the messages from the previous kept boundary up to the new cut, so messages that were kept verbatim last time are summarised now. The previous summary is passed as text, not re-derived from the original turns.

## Verbatim excerpts

#### `src/core/settings-defaults.ts` lines 9-13 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/settings-defaults.ts#L9-L13)

```ts
	compaction: {
		enabled: true,
		reserveTokens: 16384,
		keepRecentTokens: 20000,
	},
```

#### `src/core/compaction/compaction.ts` lines 267-270 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/compaction.ts#L267-L270)

```ts
export function shouldCompact(contextTokens: number, contextWindow: number, settings: CompactionSettings): boolean {
	if (!settings.enabled) return false;
	return contextTokens > contextWindow - settings.reserveTokens;
}
```

#### `src/core/compaction/compaction.ts` lines 507-579 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/compaction.ts#L507-L579)

```ts
const SUMMARIZATION_PROMPT = `The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages.`;

const UPDATE_SUMMARIZATION_INSTRUCTIONS = `Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages.`;

const UPDATE_SUMMARIZATION_PROMPT = `The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

${UPDATE_SUMMARIZATION_INSTRUCTIONS}`;
```

#### `src/core/compaction/compaction.ts` lines 696-733 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/compaction.ts#L696-L733)

```ts
export async function generateSummaryWithUsage(
	currentMessages: AgentMessage[],
	model: Model<any>,
	reserveTokens: number,
	apiKey: string | undefined,
	headers?: Record<string, string>,
	signal?: AbortSignal,
	customInstructions?: string,
	previousSummary?: string,
	thinkingLevel?: ThinkingLevel,
	streamFn?: StreamFn,
	env?: Record<string, string>,
	retry?: RetryPolicy,
	callbacks?: RetryCallbacks,
	sessionId?: string,
): Promise<{ text: string; usage: Usage }> {
	const maxTokens = Math.min(
		Math.floor(0.8 * reserveTokens),
		model.maxTokens > 0 ? model.maxTokens : Number.POSITIVE_INFINITY,
	);

	// Use update prompt if we have a previous summary, otherwise initial prompt
	let basePrompt = previousSummary ? UPDATE_SUMMARIZATION_PROMPT : SUMMARIZATION_PROMPT;
	if (customInstructions) {
		basePrompt = `${basePrompt}\n\nAdditional focus: ${customInstructions}`;
	}

	// Serialize conversation to text so model doesn't try to continue it
	// Convert to LLM messages first (handles custom types like bashExecution, custom, etc.)
	const llmMessages = convertToLlm(currentMessages);
	const conversationText = serializeConversation(llmMessages);

	// Build the prompt with conversation wrapped in tags
	let promptText = `<conversation>\n${conversationText}\n</conversation>\n\n`;
	if (previousSummary) {
		promptText += `<previous-summary>\n${previousSummary}\n</previous-summary>\n\n`;
	}
	promptText += basePrompt;
```

#### `src/core/compaction/compaction.ts` lines 942-956 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/compaction.ts#L942-L956)

```ts
const TURN_PREFIX_SUMMARIZATION_PROMPT = `The messages above are earlier context from an ongoing conversation. Later messages are stored separately and do not need to be reconstructed.

Create a concise checkpoint of the user's request and the progress shown above. This checkpoint will be placed before the later messages so the conversation can continue with the necessary context.

## Original Request
[What did the user ask for?]

## Progress So Far
- [Key decisions and work completed in these messages]

## Context Needed to Continue
- [Information from these messages needed to understand the later work]

Only summarize information explicitly present above. Do not infer or recreate later messages.`;

```

#### `src/core/compaction/compaction.ts` lines 885-912 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/compaction.ts#L885-L912)

```ts
	const prevCompactionIndex = projectedEntries.findIndex(
		(entry) => entry.sourceEntry.type === "compaction" && entry.messages.length > 0,
	);

	let previousSummary: string | undefined;
	let boundaryStart = 0;
	if (prevCompactionIndex >= 0) {
		previousSummary = (projectedEntries[prevCompactionIndex].sourceEntry as CompactionEntry).summary;
		// The canonical projection has already selected the previous compaction's retained tail.
		boundaryStart = prevCompactionIndex + 1;
	}
	const boundaryEnd = projectedEntries.length;
	const tokensBefore = estimateProjectedContextTokens(projection, pathEntries).tokens;
	const cutPoint = findProjectedCutPoint(projectedEntries, boundaryStart, boundaryEnd, settings.keepRecentTokens);

	const firstKeptEntry = projectedEntries[cutPoint.firstKeptEntryIndex]?.sourceEntry;
	if (!firstKeptEntry?.id) return undefined;
	const firstKeptEntryId = firstKeptEntry.id;
	const historyEnd = cutPoint.isSplitTurn ? cutPoint.turnStartIndex : cutPoint.firstKeptEntryIndex;

	const messagesToSummarize = projectedEntries
		.slice(boundaryStart, historyEnd)
		.flatMap(getMessagesFromProjectedEntryForCompaction);
	const turnPrefixMessages = cutPoint.isSplitTurn
		? projectedEntries
				.slice(cutPoint.turnStartIndex, cutPoint.firstKeptEntryIndex)
				.flatMap(getMessagesFromProjectedEntryForCompaction)
		: [];
```

#### `src/core/compaction/compaction.ts` lines 994-1033 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/compaction.ts#L994-L1033)

```ts
	if (isSplitTurn && turnPrefixMessages.length > 0) {
		let historyText = previousSummary ?? "No prior history.";
		let historyUsage: Usage | undefined;
		if (messagesToSummarize.length > 0) {
			const historyResult = await generateSummaryWithUsage(
				messagesToSummarize,
				model,
				settings.reserveTokens,
				apiKey,
				headers,
				signal,
				customInstructions,
				previousSummary,
				thinkingLevel,
				streamFn,
				env,
				retry,
				callbacks,
				sessionId,
			);
			historyText = historyResult.text;
			historyUsage = historyResult.usage;
		}
		const turnPrefixResult = await generateTurnPrefixSummary(
			turnPrefixMessages,
			model,
			settings.reserveTokens,
			apiKey,
			headers,
			env,
			signal,
			thinkingLevel,
			streamFn,
			retry,
			callbacks,
			sessionId,
		);
		// Merge into single summary
		summary = `${historyText}\n\n---\n\n**Turn Context (split turn):**\n\n${turnPrefixResult.text}`;
		summaryUsage = historyUsage ? combineUsage(historyUsage, turnPrefixResult.usage) : turnPrefixResult.usage;
```

#### `src/core/compaction/utils.ts` lines 93-155 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/utils.ts#L93-L155)

```ts
/** Maximum characters for a tool result in serialized summaries. */
const TOOL_RESULT_MAX_CHARS = 2000;

/**
 * Truncate text to a maximum character length for summarization.
 * Keeps the beginning and appends a truncation marker.
 */
function truncateForSummary(text: string, maxChars: number): string {
	if (text.length <= maxChars) return text;
	const truncatedChars = text.length - maxChars;
	return `${text.slice(0, maxChars)}\n\n[... ${truncatedChars} more characters truncated]`;
}

/**
 * Serialize LLM messages to text for summarization.
 * This prevents the model from treating it as a conversation to continue.
 * Call convertToLlm() first to handle custom message types.
 *
 * Tool results are truncated to keep the summarization request within
 * reasonable token budgets. Full content is not needed for summarization.
 *
 * Thinking is omitted. Providers return summaries of the real reasoning, and
 * replayed thinking is often not part of the model's live context: for example,
 * a block that does not match its request is dropped. Including it can make the
 * summarization request larger than the context it summarizes. Visible text,
 * tool calls, and tool results record what happened.
 */
export function serializeConversation(messages: Message[]): string {
	const parts: string[] = [];

	for (const msg of messages) {
		if (msg.role === "user") {
			const content = contentText(msg.content, "");
			if (content) parts.push(`[User]: ${content}`);
		} else if (msg.role === "assistant") {
			const toolCalls: string[] = [];

			for (const block of msg.content) {
				if (block.type === "toolCall") {
					const args = block.arguments as Record<string, unknown>;
					const argsStr = Object.entries(args)
						.map(([k, v]) => `${k}=${JSON.stringify(v)}`)
						.join(", ");
					toolCalls.push(`${block.name}(${argsStr})`);
				}
			}

			if (msg.content.some((block) => block.type === "text")) {
				parts.push(`[Assistant]: ${contentText(msg.content)}`);
			}
			if (toolCalls.length > 0) {
				parts.push(`[Assistant tool calls]: ${toolCalls.join("; ")}`);
			}
		} else if (msg.role === "toolResult") {
			const content = contentText(msg.content, "");
			if (content) {
				parts.push(`[Tool result]: ${truncateForSummary(content, TOOL_RESULT_MAX_CHARS)}`);
			}
		}
	}

	return parts.join("\n\n");
}
```

#### `src/core/compaction/utils.ts` lines 161-163 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/compaction/utils.ts#L161-L163)

```ts
export const SUMMARIZATION_SYSTEM_PROMPT = `You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary.`;
```

#### `src/core/messages.ts` lines 11-17 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/messages.ts#L11-L17)

```ts
export const COMPACTION_SUMMARY_PREFIX = `The conversation history before this point was compacted into the following summary:

<summary>
`;

export const COMPACTION_SUMMARY_SUFFIX = `
</summary>`;
```

#### `src/core/messages.ts` lines 176-183 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/src/core/messages.ts#L176-L183)

```ts
				case "compactionSummary":
					return {
						role: "user",
						content: [
							{ type: "text" as const, text: COMPACTION_SUMMARY_PREFIX + m.summary + COMPACTION_SUMMARY_SUFFIX },
						],
						timestamp: m.timestamp,
					};
```

#### `docs/compaction.md` lines 27-50 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/docs/compaction.md#L27-L50)

```markdown
### When It Triggers

Auto-compaction triggers when:

```
contextTokens > contextWindow - reserveTokens
```

By default, `reserveTokens` is 16384 tokens (configurable in `~/.pi/agent/settings.json` or `<project-dir>/.pi/settings.json`). This leaves room for the LLM's response.

During a multi-turn agent run, Pi checks the canonical projected context after tools finish and their results are appended, before starting the next assistant response. If the threshold is crossed, Pi compacts during `prepareNextTurn`, then performs the existing catch-up steering poll before `turn_start`. It skips this between-turn check when the completed tool batch terminates the run and no queued message requires another response. Pi also checks before a new user prompt and performs final-attempt overflow recovery after the low-level run ends.

A provider context-overflow error or an early final `stopReason: "length"` can select one compact-and-retry recovery attempt. Length responses with tool calls retain their synthetic failed tool results and follow the ordinary tool/queue scheduler rather than forcing the run to end.

You can also trigger manually with `/compact [instructions]`, where optional instructions focus the summary.

### How It Works

1. **Find cut point**: Walk backwards through the finalized session projection, accumulating token estimates until `keepRecentTokens` (default 20k, configurable in `~/.pi/agent/settings.json` or `<project-dir>/.pi/settings.json`) is reached
2. **Extract messages**: Collect projected messages from the previous kept boundary (or session start) up to the cut point
3. **Generate summary**: Call LLM to summarize with structured format, passing the previous summary as iterative context when present
4. **Append entry**: Save `CompactionEntry` with summary and `firstKeptEntryId`
5. **Rebuilds context**: Session rebuilds the context for the next request, using summary + messages from `firstKeptEntryId` onwards

```

#### `docs/compaction.md` lines 83-83 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/docs/compaction.md#L83-L83)

```markdown
On repeated compactions, the summarized span starts at the previous compaction's kept boundary (`firstKeptEntryId`), not at the compaction entry itself, falling back to the entry after the previous compaction if that kept entry cannot be found in the path. A retain-none compaction records its own ID as `firstKeptEntryId`; repeated compaction starts after that entry. This preserves messages that survived the earlier compaction by including them in the next summarization pass as well. Pi also recalculates `tokensBefore` from the rebuilt, context-edited session projection before writing the new `CompactionEntry`, so the token count reflects the actual pre-compaction context being replaced. Omitted raw entries remain stored but do not affect cut selection, summaries, checkpoints, or token estimates.
```

#### `docs/compaction.md` lines 283-291 (https://github.com/badlogic/pi-mono/blob/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/docs/compaction.md#L283-L291)

~~~~markdown
[Assistant tool calls]: read(path="foo.ts"); edit(path="bar.ts", ...)
[Tool result]: Output from tool
```

This prevents the model from treating it as a conversation to continue.

Thinking blocks are omitted. Providers return summaries of the real reasoning, and replayed thinking is often not part of the model's live context, so including it could make the summarization request larger than the context it summarizes.

Tool results are truncated to 2000 characters during serialization. Content beyond that limit is replaced with a marker indicating how many characters were truncated. This keeps summarization requests within reasonable token budgets, since tool results (especially from `read` and `bash`) are typically the largest contributors to context size.
~~~~
