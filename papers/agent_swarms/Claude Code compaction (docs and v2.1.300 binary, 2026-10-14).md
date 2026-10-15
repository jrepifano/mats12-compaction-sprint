# Claude Code auto-compaction (official docs, plus facts read from the v2.1.300 binary)

**Sources:**
1. Official docs, fetched 2026-10-14 as markdown (`curl https://code.claude.com/docs/en/<page>.md`): `model-config` (auto-compact window, default thresholds), `context-window` ("What survives compaction"), `env-vars`, `how-claude-code-works`, `prompt-caching` ("Compacting the conversation"), `costs`.
2. The shipped program, Claude Code **2.1.300**: the native Mach-O arm64 executable installed on this machine at `~/.local/share/claude/versions/2.1.300` (249,556,880 bytes; `claude --version` prints `2.1.300 (Claude Code)`), which embeds the minified JavaScript source. Read with a byte-range extractor; never executed. The npm package `@anthropic-ai/claude-code@2.1.300` (downloaded with `npm pack --ignore-scripts`, 29 KB) is only a wrapper: `bin/claude.exe` is a 500-byte placeholder and the real binary comes from per-platform optional dependencies (`@anthropic-ai/claude-code-darwin-arm64` etc.), so the prompt is not in the npm tarball itself.

**Licence of the shipped program:** the npm package's `LICENSE.md` reads, verbatim: "© Anthropic PBC. All rights reserved. Use is subject to the Legal Agreements outlined here: https://code.claude.com/docs/en/legal-and-compliance." (`package.json`: `"license": "SEE LICENSE IN README.md"`). The code is proprietary, so **this capture does not reproduce the bundled compaction prompt.** Its location: the summarisation-prompt constants sit in the embedded JS at byte offset ~206,184,950 of the 2.1.300 binary (the string "Your task is to create a detailed summary of the conversation so far" also appears in a string table at ~84,851,660); the threshold functions at ~204,525,838. The verbatim text was kept out of the repo (see the P49 scratch notes). Whether quoting it in a research write-up is acceptable is Jacob's call; paraphrase and the facts below are safe.

**Reading depth:** the docs sections excerpted below read verbatim. Binary facts are my reading of minified code (identifiers are obfuscated), so they are marked as inference where they go beyond the docs.

## Summary

- **Trigger (docs):** "If you don't set an auto-compact window, Claude Code compacts when the conversation reaches the model's context limit", with exceptions: models with a native 1M window "compact before the window fills, at about 967K tokens by default"; Sonnet 4.6 / Opus 4.6 without extended context and models with `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` "compact at the 200K boundary". The window can be lowered per model (`/autocompact 500k`, `autoCompactWindow`, `--autocompact`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, 100K-1M). `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1-100) lowers the trigger as a percentage of the auto-compact window and "can't raise the threshold". `DISABLE_AUTO_COMPACT=1` turns auto-compaction off; `DISABLE_COMPACT=1` turns all compaction off.
- **Trigger arithmetic (binary, consistent with the docs' 967K):** effective window = window − min(model max output, 20,000); auto-compact threshold = effective window − 13,000; with the pct override, min(floor(effective × pct/100), effective − 13,000). So a 200K window compacts at 167,000 tokens (83.5%) and a 1M window at 967,000 (96.7%), for models whose max output is ≥ 20K. A warning level starts 20,000 below the threshold; requests are blocked at effective window − 3,000. A second, lower level at effective × (1 − 0.2) (144,000 / 784,000), feature-flagged (`tengu_amber_rokovoko`, default 0.2), appears to start computing the summary early in the background (inference from names like `precomputeBufferFraction`). A thrashing guard stops auto-compaction if the context refills within 3 turns of a compaction 3 times in a row (docs and binary).
- **Context editing before summarising (docs):** "It clears older tool outputs first, then summarizes the conversation if needed."
- **Summariser (docs):** "Claude Code sends a separate request with the same system prompt, tools, and history as your conversation, plus a summarization instruction appended as a final user message" — the same model and the full history (tool outputs included, as far as they were not already cleared). Since v2.1.198 the request inherits the session's extended-thinking setting. Whether earlier thinking blocks are sent to the summariser: not verified (the binary has a `thinkingStripPlan` parameter on the compaction request).
- **Prompt (binary, paraphrased):** 2.1.300 contains five prompt arms selected by a server-side flag (`tengu_curried_trinket`, local override `CLAUDE_CODE_CURRIED_TRINKET`; default arm `control`): `control` = the long-standing nine-section prompt (Primary Request and Intent, Key Technical Concepts, Files and Code Sections, Errors and fixes, Problem Solving, All user messages, Pending Tasks, Current Work, Optional Next Step) with an `<analysis>` scratchpad and `<summary>` block, plus clauses to preserve security-relevant user constraints verbatim and not to attribute assistant-written "user:" text to the user; `lean`, `short`, `short_v2` and `capped` (≤ 2,000 words) are shorter variants. All arms are wrapped in a "TEXT ONLY, do not call tools" preamble and reminder. A user-supplied focus (`/compact <instructions>`, or a "Compact Instructions" section in CLAUDE.md) is appended as "Additional Instructions". Which arm a given account receives is not knowable from the client.
- **Resumed context (docs, "What survives compaction"):** system prompt and output style; project-root CLAUDE.md, unscoped rules and auto memory re-injected from disk; a fresh git-status snapshot; the plan file; up to five files Claude read or edited, most recently modified first (files over 5,000 tokens come back as a path reference only); invoked skill bodies (≤ 5,000 tokens each, 25,000 total); reminders of running background commands/subagents; SessionStart hooks with source `compact`. Then the summary as a user message (binary, paraphrased): a fixed opening sentence saying the session continues from an earlier conversation that ran out of context and that the summary covers its earlier part, then the summary with the `<analysis>` block removed, then (when available) the path to the full on-disk transcript for exact details. Recent turns are not kept verbatim in a normal auto-compaction (docs table lists no such row); the binary does contain a partial-compaction mode that keeps recent messages verbatim after the summary, used at least by `/rewind` "Summarize up to here" (docs) — whether auto-compaction ever uses it: unverified.
- **Repeated compaction:** the earlier summary is part of the history the next summariser sees. The `lean` arm tells the model that no earlier summary survives except through the new one, and the `short_v2` arm tells it to carry the earlier summary's record of the user's requests over unchanged (binary; paraphrased).
- **Idle compaction (docs):** a long idle conversation on the 1-hour cache TTL can be compacted shortly before the cache expires (`idleCompaction` setting).

## Verbatim excerpts from the official docs

#### model-config: Default auto-compact thresholds (https://code.claude.com/docs/en/model-config#default-auto-compact-thresholds)

~~~~markdown
### Default auto-compact thresholds

If you don't set an auto-compact window, Claude Code compacts when the conversation reaches the model's context limit, except in these sessions:

* [Cloud sessions](/docs/en/claude-code-on-the-web) compact as the conversation approaches the model's limit
* Sonnet 4.6 and Opus 4.6 without [extended context](#extended-context) compact at the 200K boundary
* When you set [`CLAUDE_CODE_DISABLE_1M_CONTEXT=1`](/docs/en/env-vars), models with a native 1M window, such as Sonnet 5 and the Fable models, compact at the 200K boundary
* Models running with a native 1M window compact before the window fills, at about 967K tokens by default. These include the Fable models, Sonnet 5 and later, Haiku 5.5, and Opus 4.7 and later. Behind a custom `ANTHROPIC_BASE_URL`, see [the context window behind a gateway](#context-window-behind-a-gateway)
* Sessions on a model ID Claude Code doesn't recognize, such as an [LLM gateway](/docs/en/llm-gateway) alias, compact at the context window Claude Code assumes for the ID; see [Correct the window for a gateway or custom model ID](#correct-the-window-for-a-gateway-or-custom-model-id)

See the [`idleCompaction`](/docs/en/settings-reference#idlecompaction) setting for when Claude Code also compacts a conversation you leave idle.
~~~~

#### model-config: Context window and auto-compaction (https://code.claude.com/docs/en/model-config#context-window-and-auto-compaction)

~~~~markdown
Sessions auto-compact at about 967K tokens by default. To compact earlier, [set a smaller auto-compact window](#set-the-auto-compact-window) for the model.

## Context window and auto-compaction

The auto-compact window is how full the context window can get before Claude Code compacts the conversation. For what compaction keeps and drops per mechanism, see [What survives compaction](/docs/en/context-window#what-survives-compaction).
~~~~

#### context-window: What survives compaction (https://code.claude.com/docs/en/context-window#what-survives-compaction)

~~~~markdown
## What survives compaction

When a long session compacts, Claude Code summarizes the conversation history to fit the context window. As of v2.1.198, the summarization request inherits your session's [extended thinking](/docs/en/model-config#extended-thinking) configuration, so it reasons with thinking enabled when your session has it enabled and stays off otherwise. Thinking affects only how the summary is produced; your session settings are unchanged afterward. What happens to each kind of content depends on how it was loaded:

| Mechanism | After compaction |
| :- | :- |
| System prompt and output style | Both still apply |
| Project-root CLAUDE.md and unscoped rules | Re-injected from disk |
| Auto memory | Re-injected from disk |
| [Git status snapshot](/docs/en/settings-reference#includegitinstructions) | If the session started with one, Claude Code reads a fresh one from your repository |
| The plan Claude wrote in [plan mode](/docs/en/permission-modes#analyze-before-you-edit-with-plan-mode) | Re-injected from disk |
| Rules with `paths:` frontmatter | Claude Code reloads them [on demand](/docs/en/memory#path-specific-rules) |
| Nested CLAUDE.md in subdirectories | Claude Code reloads them [on demand](/docs/en/memory#how-claude-md-files-load) |
| Files Claude read or edited | Claude Code re-reads up to five, most recently modified first |
| Invoked skill bodies | Re-injected, capped at 5,000 tokens per skill and 25,000 tokens total; oldest dropped first |
| [Background commands](/docs/en/interactive-mode#background-bash-commands) and background [subagents](/docs/en/sub-agents#run-subagents-in-foreground-or-background) | Keep running. Claude Code reminds Claude which ones are still running so it doesn't start a duplicate |
| Context that hooks added earlier | Summarized with the rest of the conversation |
| [SessionStart hooks](/docs/en/hooks-guide#re-inject-context-after-compaction) that match the `compact` source | Claude Code runs them and adds their output to the compacted context |

Right after compaction, Claude Code re-reads up to five of the files Claude has read or edited in the session, choosing the ones modified most recently. A file over 5,000 tokens comes back as a path reference without its content, shown as `Referenced file` instead of `Read`.

Path-scoped rules and nested CLAUDE.md files load into message history when Claude reads, writes, or edits their trigger file, so compaction summarizes them away with everything else. If a rule must persist across compaction, drop the `paths:` frontmatter or move it to the project-root CLAUDE.md.

Skill bodies are re-injected after compaction, but large skills are truncated to fit the per-skill cap, and the oldest invoked skills are dropped once the total budget is exceeded. Truncation keeps the start of the file, so put the most important instructions near the top of `SKILL.md`.

## When your context fills up
~~~~

#### how-claude-code-works: When context fills up (https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up)

~~~~markdown
#### When context fills up

Claude Code manages context automatically as you approach the limit. It clears older tool outputs first, then summarizes the conversation if needed. Your requests and key code snippets are preserved; detailed instructions from early in the conversation may be lost. Put persistent rules in CLAUDE.md rather than relying on conversation history.

To control what's preserved during compaction, add a "Compact Instructions" section to CLAUDE.md or run `/compact` with a focus (like `/compact focus on the API changes`).

If a single file or tool output is so large that context refills immediately after each summary, Claude Code stops auto-compacting after a few attempts and shows an error instead of looping. See [Auto-compaction stops with a thrashing error](/docs/en/troubleshooting#auto-compaction-stops-with-a-thrashing-error) for recovery steps.
~~~~

#### prompt-caching: Compacting the conversation (https://code.claude.com/docs/en/prompt-caching#compacting-the-conversation)

~~~~markdown
### Compacting the conversation

[Compaction](/docs/en/context-window#what-survives-compaction) replaces your message history with a summary. By design, this invalidates the conversation layer, since the next request has a new, shorter history that doesn't share a prefix with the old one. Claude Code reuses the system prompt layer unless the conversation was [resumed while keeping a system prompt that would otherwise have changed](#resuming-a-session); in that case the first compaction switches to the current prompt and that layer rebuilds once. It reloads project context from disk, which cache-hits only if CLAUDE.md and memory are unchanged since the session started.

To produce the summary, Claude Code sends a separate request with the same system prompt, tools, and history as your conversation, plus a summarization instruction appended as a final user message. While the cache is warm, that request reads your prefix from the cache, so a mid-session `/compact` costs a fraction of what the context size suggests and spends most of its time generating the summary.

After a break longer than the [cache lifetime](#cache-lifetime), there is no cache left to read, so the summarization request reprocesses the full history as uncached input. This is why `/compact` costs the most when you [resume an old session](/docs/en/sessions#resume-from-a-summary). In both the warm and cold cases, the turn after compaction rebuilds the conversation cache for only the much shorter summary, so that turn is not the slow part.

<Tip>
  Compaction works in your favor when the context you discard is content you no longer need. To choose when its overhead happens, run `/compact` at a natural break in your work, such as between tasks, instead of waiting for auto-compaction to trigger mid-task. If you've gone down a path you want to abandon entirely, [`/rewind`](#rewinding-the-conversation) to an earlier turn instead. Rewinding truncates back to a prefix that is already cached, rather than building a new one as compaction does.
</Tip>

If you leave a long conversation idle in a terminal session while it is on the [one-hour TTL](#which-ttl-each-request-gets), Claude Code can compact it shortly before the cache expires. To turn idle compaction off, set [`idleCompaction`](/docs/en/settings-reference#idlecompaction) to `false`, which requires Claude Code v2.1.290 or later.
~~~~

#### env-vars: compaction variables (https://code.claude.com/docs/en/env-vars)

~~~~markdown
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Set the percentage (1-100) of the auto-compact window at which auto-compaction triggers. Use lower values like `50` to compact earlier; the variable can't raise the threshold, so values above the default percentage are ignored. It applies only in sessions that [compact before the model's context limit](/docs/en/model-config#context-window-and-auto-compaction). Applies to both main conversations and subagents |
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | Set the [auto-compact window](/docs/en/model-config#set-the-auto-compact-window) in tokens, from `100000` to `1000000`. Accepts a plain integer such as `500000` only: a value like `500k` reads as `500` and clamps to the 100K minimum. The effective window is also capped at the model's context window. Takes precedence over the `/autocompact` command, the `--autocompact` flag, and the `autoCompactWindow` setting. The status line's `used_percentage` always measures against the model's full context window, so once this variable is set, that percentage no longer indicates when compaction will run |
| `DISABLE_AUTO_COMPACT` | Set to `1` to disable automatic compaction when approaching the context limit, along with [idle compaction](/docs/en/settings-reference#idlecompaction). The manual `/compact` command remains available. Use when you want explicit control over when compaction occurs. Overrides the [`autoCompactEnabled`](/docs/en/settings-reference#autocompactenabled) setting |
| `DISABLE_COMPACT` | Set to `1` to disable all compaction: both automatic compaction and the manual `/compact` command |
~~~~
