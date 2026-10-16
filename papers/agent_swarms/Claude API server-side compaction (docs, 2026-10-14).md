# Claude API server-side compaction (official docs)

**Sources:** Anthropic platform docs, fetched 2026-10-14 as markdown: https://platform.claude.com/docs/en/build-with-claude/compaction (overview), https://platform.claude.com/docs/en/build-with-claude/compaction-threshold (beta `compact-2026-01-12`, edit type `compact_20260112`), https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand (beta `compact-2026-09-04`, top-level `compaction` parameter). The keep-recent-turns page was downloaded but not read.
**Reading depth:** the excerpts below verbatim; overview read in full; threshold and on-demand pages read in the parts excerpted (parameters, default prompt, thinking, compact again).
**Relevance:** this is the "native" compaction that Inspect AI's `CompactionNative`/`CompactionAuto` call for Anthropic models; Claude Code's own auto-compaction is client-side (see the Claude Code capture).

## Summary

- Threshold mode: `context_management.edits: [{type: "compact_20260112", trigger: {type: "input_tokens", value: N}}]`; default trigger 150,000 input tokens (minimum 50,000), independent of the model's window. Optional `pause_after_compaction` lets the client re-insert recent turns; otherwise nothing before the `compaction` block is kept.
- Summariser: the same model, inside the same request. "The default summarization prompt varies by model"; one example default is quoted below. Custom `instructions` replace it entirely. "On Claude 5.1 and later models, a request with custom `instructions` summarizes from the visible conversation only: earlier thinking blocks are not part of the summarizer's input" (which implies the default prompt can see earlier thinking on those models; inference).
- Repeated compaction: "The new block summarizes the old summary and everything after it."

## Verbatim excerpts

#### compaction-threshold: How compaction works (https://platform.claude.com/docs/en/build-with-claude/compaction-threshold#how-compaction-works)

~~~~markdown
## How compaction works

When compaction is enabled, Claude automatically summarizes your conversation when it reaches the configured token threshold. The API:

1. Detects when input tokens reach your specified trigger threshold.
2. Generates a summary of the current conversation.
3. Creates a `compaction` block containing the summary.
4. Continues the response with the compacted context.

On subsequent requests, append the response to your messages. The API automatically drops all content blocks prior to the `compaction` block, continuing the conversation from the summary.

~~~~

#### compaction-threshold: Parameters (https://platform.claude.com/docs/en/build-with-claude/compaction-threshold#parameters)

~~~~markdown
## Parameters

| Parameter                | Type    | Default                                     | Description                                                                                                            |
| ------------------------ | ------- | ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `type`                   | string  | Required                                    | Must be `"compact_20260112"`                                                                                           |
| `trigger`                | object  | `{"type": "input_tokens", "value": 150000}` | When to trigger compaction. `input_tokens` is the only supported trigger type. `value` must be at least 50,000 tokens. |
| `pause_after_compaction` | boolean | `false`                                     | Whether to pause after generating the compaction summary                                                               |
| `instructions`           | string  | `null`                                      | Custom summarization prompt. Completely replaces the default prompt when provided.                                     |
~~~~

#### compaction-threshold: Custom summarization instructions (example default prompt) (https://platform.claude.com/docs/en/build-with-claude/compaction-threshold#custom-summarization-instructions)

~~~~markdown
### Custom summarization instructions

The default summarization prompt varies by model. Each default instructs Claude to write a summary inside `<summary></summary>` tags with the information needed to continue the task in a future context window. For example, some models use the following prompt:

```text wrap
You have written a partial transcript for the initial task above. Please write a summary of the transcript. The purpose of this summary is to provide continuity so you can continue to make progress towards solving the task in a future context, where the raw history above may not be accessible and will be replaced with this summary. Write down anything that would be helpful, including the state, next steps, learnings etc. You must wrap your summary in a <summary></summary> block.
```

You can provide custom instructions through the `instructions` parameter. Custom instructions don't supplement the default prompt. They replace it completely:
~~~~

#### compaction-threshold: thinking and custom instructions (https://platform.claude.com/docs/en/build-with-claude/compaction-threshold)

~~~~markdown
On Claude 5.1 and later models, a request with custom `instructions` summarizes from the visible conversation only: earlier thinking blocks are not part of the summarizer's input.

### Pausing after compaction

Use `pause_after_compaction` to pause the API after generating the compaction summary. This allows you to add additional content blocks (such as preserving recent messages or specific instruction-oriented messages) before the API continues with the response.
~~~~

#### compaction-on-demand: Compact again (https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand#compact-again)

~~~~markdown
### Compact again

To compact a conversation that already starts with a block, send `compaction` again. The new block summarizes the old summary and everything after it. From then on, send only the newest block.
~~~~

#### compaction-on-demand: Write your own summarization prompt (https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand#write-your-own-summarization-prompt)

~~~~markdown
## Write your own summarization prompt

Without `instructions`, the API uses its own summarization prompt. A non-blank `instructions` string (up to 16,384 characters) replaces that prompt entirely. For example:
~~~~
