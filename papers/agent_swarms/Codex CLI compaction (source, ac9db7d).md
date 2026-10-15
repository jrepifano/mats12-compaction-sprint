# Codex CLI compaction (source code, openai/codex at commit ac9db7d)

**Source:** https://github.com/openai/codex , commit `ac9db7d8b556c7c34a73793aed34b1f980a4fe39` (committed 2026-10-15T01:37:36Z; HEAD of `main` when cloned 2026-10-14). Workspace `Cargo.toml` says version 0.0.0 (dev build), so the commit is the pin.
**Licence:** Apache-2.0 (repo `LICENSE`). Excerpts below are quoted under that licence with attribution.
**Retrieved:** 2026-10-14, shallow `git clone`, read only (not built or run).
**Reading depth:** the excerpts below are verbatim copies of the named lines. The "Summary" section is my reading of the code (not executed; behaviour inferred from source). Files read in full: `core/src/compact.rs`, `prompts/templates/compact/*`, `core/src/session/context_window.rs`, `core/src/tasks/compact.rs`; read in part: `core/src/compact_remote_v2.rs`, `core/src/compact_remote_v2_attempt.rs`, `core/src/session/turn.rs`, `protocol/src/openai_models.rs`, `core/src/context_manager/history.rs`, `model-provider/src/capabilities.rs`, `features/src/lib.rs`, `models-manager/models.json`.

## Summary (my reading of the code)

- **Trigger.** `ModelInfo::auto_compact_token_limit()` = min(configured `model_auto_compact_token_limit`, 90% of the model's context window); with no config it is 90% of the window. A second, hard limit forces compaction at `effective_context_window_percent` (default 95%) of the window. For the bundled catalogue (`models-manager/models.json`) every current model has `context_window` 272,000 (`max_context_window` 872,000), so auto-compaction fires at 244,800 tokens and is forced at 258,400. For a 200k window: 180,000 (forced 190,000); for a 1M window: 900,000 (forced 950,000). The check runs after each sampling step mid-turn (`turn.rs` ~L595-700), before a turn (`PreTurn`), and optionally at turn end if `model_post_turn_compact_threshold_percent` > 0 (default 0 = off). Token usage is the API-reported total for the last request (`get_total_token_usage`). A background ("async") compaction at 65% exists behind the `async_compaction` feature (under development, default off).
- **Which path.** `ProviderCapabilities::from_config`: OpenAI and Azure Responses providers default to `RemoteCompactionSupport::V2` (server-side); all other providers use local compaction. Behind the `token_budget` feature (under development, default off) a third path exists (`compact_token_budget.rs`).
- **Local compaction** (non-OpenAI providers, or a custom `compact_prompt`): the same model (the turn's model) is sent the whole live history plus the summarisation prompt as a final user message. The live history is what the model saw: tool outputs were already truncated when recorded (model `truncation_policy`, 10,000 tokens per output for all bundled models), reasoning items are kept (normalisation does not drop them), and earlier compaction summaries are present. If the request overflows, the oldest items are dropped one at a time and it retries. The new history is: the re-built initial context (developer/system instructions, AGENTS.md, environment), then the most recent real user messages, newest first, up to 20,000 tokens (`COMPACT_USER_MESSAGE_MAX_TOKENS`, estimated as bytes/4), with a message cut short when it crosses the budget, then the summary as a user message prefixed with `SUMMARY_PREFIX`. Earlier summaries are excluded from the "kept user messages" (`is_summary_message`) but were part of the summariser's input, so on repeated compaction the previous summary is re-summarised.
- **Remote (server-side) compaction v2** (the default for OpenAI): Codex sends an ordinary Responses request with the full history, the tools and a `compaction_trigger` input item; the server returns an opaque, encrypted `compaction` item. The summarisation prompt is not in the client. The new history keeps user messages (and hook prompts, and non-progress agent messages of at most 10,000 tokens) up to 64,000 tokens (`RETAINED_MESSAGE_TOKEN_BUDGET`), newest first, followed by the new compaction item; developer/system/assistant messages, tool calls and outputs, and the old compaction item are dropped (unit test `build_v2_compacted_history_filters_to_installed_retention_shape`). The old compaction item is in the request, so the server sees it. I found no use of a `/responses/compact` endpoint in this commit.
- After every compaction the client shows: "Heads up: Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible to keep threads small and targeted."

## Verbatim excerpts

#### `prompts/templates/compact/prompt.md` lines 1-9 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/prompts/templates/compact/prompt.md#L1-L9)

```text
You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.
```

#### `prompts/templates/compact/summary_prefix.md` lines 1-1 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/prompts/templates/compact/summary_prefix.md#L1-L1)

```text
Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:
```

#### `protocol/src/openai_models.rs` lines 455-471 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/protocol/src/openai_models.rs#L455-L471)

```rust
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_window: Option<i64>,
    /// Maximum context window allowed for config overrides.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_context_window: Option<i64>,
    /// Token threshold for automatic compaction. When omitted, core derives it
    /// from `context_window` (90%). When provided, core clamps it to 90% of the
    /// context window when available.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auto_compact_token_limit: Option<i64>,
    /// Opaque identifier for compaction-compatible model configurations.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comp_hash: Option<String>,
    /// Percentage of the context window considered usable for inputs, after
    /// reserving headroom for system prompts, tool overhead, and model output.
    #[serde(default = "default_effective_context_window_percent")]
    pub effective_context_window_percent: i64,
```

#### `protocol/src/openai_models.rs` lines 393-395 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/protocol/src/openai_models.rs#L393-L395)

```rust
const fn default_effective_context_window_percent() -> i64 {
    95
}
```

#### `protocol/src/openai_models.rs` lines 517-541 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/protocol/src/openai_models.rs#L517-L541)

```rust
impl ModelInfo {
    pub fn resolved_context_window(&self) -> Option<i64> {
        self.context_window.or(self.max_context_window)
    }

    /// Context available to inference after reserving this model's configured headroom.
    pub fn usable_context_window(&self) -> Option<i64> {
        self.resolved_context_window().map(|context_window| {
            context_window.saturating_mul(self.effective_context_window_percent) / 100
        })
    }

    pub fn auto_compact_token_limit(&self) -> Option<i64> {
        let context_limit = self
            .resolved_context_window()
            .map(|context_window| (context_window * 9) / 10);
        let config_limit = self.auto_compact_token_limit;
        if let Some(context_limit) = context_limit {
            return Some(
                config_limit.map_or(context_limit, |limit| std::cmp::min(limit, context_limit)),
            );
        }
        config_limit
    }
}
```

#### `core/src/session/context_window.rs` lines 53-130 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/session/context_window.rs#L53-L130)

```rust
async fn context_window_token_status_with_config(
    sess: &Session,
    config: &Config,
    model_info: &ModelInfo,
) -> ContextWindowTokenStatus {
    let active_context_tokens = sess.get_total_token_usage().await;

    // Count either the full active context or only the tokens added after the initial prefix.
    let (auto_compact_scope_tokens, auto_compact_scope_limit, auto_compact_window_prefill_tokens) =
        match config.model_auto_compact_token_limit_scope {
            AutoCompactTokenLimitScope::Total => (
                active_context_tokens,
                model_info.auto_compact_token_limit(),
                None,
            ),
            AutoCompactTokenLimitScope::BodyAfterPrefix => {
                let window = sess.auto_compact_window_snapshot().await;
                let baseline = window.prefill_input_tokens.unwrap_or(active_context_tokens);

                let scope_limit = config
                    .model_auto_compact_token_limit
                    .or_else(|| model_info.auto_compact_token_limit());
                (
                    active_context_tokens.saturating_sub(baseline),
                    scope_limit,
                    window.prefill_input_tokens,
                )
            }
        };

    // The model's full context window is a hard cap, independent of the auto-compaction scope.
    let full_context_window_limit = model_info.resolved_context_window().map(|context_window| {
        context_window.saturating_mul(model_info.effective_context_window_percent) / 100
    });

    // Report remaining tokens against the base (unbuffered) window, capped by the full context.
    let base_window_tokens_remaining = [
        tokens_remaining(auto_compact_scope_limit, auto_compact_scope_tokens),
        tokens_remaining(full_context_window_limit, active_context_tokens),
    ]
    .into_iter()
    .flatten()
    .min();

    // Only reserve the fallback buffer when there is a fallback prompt to use it.
    let auto_compact_fallback_buffer_tokens = config
        .token_budget
        .as_ref()
        .map_or(0, crate::config::TokenBudgetConfig::fallback_buffer_tokens);
    let buffered_auto_compact_limit = auto_compact_scope_limit
        .map(|limit| limit.saturating_add(auto_compact_fallback_buffer_tokens));

    // Force compaction once the buffered window or the model's full context window is reached.
    let full_context_window_limit_reached =
        full_context_window_limit.is_some_and(|limit| active_context_tokens >= limit);
    let token_limit_reached = buffered_auto_compact_limit
        .is_some_and(|limit| auto_compact_scope_tokens >= limit)
        || full_context_window_limit_reached;
    let post_turn_percent = config.model_post_turn_compact_threshold_percent;
    let turn_end_compaction_threshold_reached = post_turn_percent > 0
        && (token_limit_reached
            || full_context_window_limit.is_some_and(|limit| {
                i128::from(active_context_tokens) * 100
                    >= i128::from(limit) * i128::from(post_turn_percent)
            }));

    ContextWindowTokenStatus {
        active_context_tokens,
        auto_compact_scope_tokens,
        auto_compact_scope_limit,
        full_context_window_limit,
        base_window_tokens_remaining,
        auto_compact_window_prefill_tokens,
        full_context_window_limit_reached,
        token_limit_reached,
        turn_end_compaction_threshold_reached,
    }
}
```

#### `config/src/config_toml.rs` lines 180-192 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/config/src/config_toml.rs#L180-L192)

```rust
    /// Token usage threshold triggering auto-compaction of conversation history.
    pub model_auto_compact_token_limit: Option<i64>,

    /// Controls whether the auto-compaction limit applies to the full context or
    /// only to tokens after the carried prefix in the current compaction window.
    pub model_auto_compact_token_limit_scope: Option<AutoCompactTokenLimitScope>,

    /// Percentage of the usable context window that triggers compaction after a final
    /// response. Existing auto-compaction limits still apply. Omitted or zero disables
    /// turn-end compaction; valid values are 0–100.
    #[schemars(range(min = 0, max = 100))]
    pub model_post_turn_compact_threshold_percent: Option<u8>,

```

#### `model-provider/src/capabilities.rs` lines 30-40 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/model-provider/src/capabilities.rs#L30-L40)

```rust
impl ProviderCapabilities {
    pub(crate) fn from_config(info: &ModelProviderInfo) -> Self {
        let defaults = Self {
            remote_compaction: if info.is_openai()
                || is_azure_responses_provider(&info.name, info.base_url.as_deref())
            {
                RemoteCompactionSupport::V2
            } else {
                RemoteCompactionSupport::Unsupported
            },
            ..Self::default()
```

#### `core/src/compact.rs` lines 62-64 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L62-L64)

```rust
pub use codex_prompts::SUMMARIZATION_PROMPT;
pub use codex_prompts::SUMMARY_PREFIX;
const COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000;
```

#### `core/src/compact.rs` lines 118-153 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L118-L153)

```rust
pub(crate) async fn run_inline_auto_compact_task(
    sess: Arc<Session>,
    turn_context: Arc<TurnContext>,
    replacement_step_context: Arc<StepContext>,
    world_state: Arc<WorldState>,
    reason: CompactionReason,
    phase: CompactionPhase,
) -> CodexResult<()> {
    let prompt = turn_context
        .config
        .compact_prompt
        .as_deref()
        .unwrap_or(SUMMARIZATION_PROMPT)
        .to_string();
    let input = vec![UserInput::Text {
        text: prompt,
        // Compaction prompt is synthesized; no UI element ranges to preserve.
        text_elements: Vec::new(),
    }];

    run_compact_task_inner(
        sess,
        turn_context,
        replacement_step_context,
        input,
        world_state,
        CompactionTurnMetadata::new(
            CompactionTrigger::Auto,
            reason,
            CompactionImplementation::Responses,
            phase,
        ),
    )
    .await?;
    Ok(())
}
```

#### `core/src/compact.rs` lines 277-307 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L277-L307)

```rust
    let mut history = sess.clone_history().await;
    let input_goal_ids = UserGoalUpdate::message_ids(history.raw_items());
    history.record_items(
        &[initial_input_for_turn.into()],
        turn_context.model_info().truncation_policy.into(),
    );
    let mut fallback_instructions = sess.get_prompt_base_instructions().await;
    if let Some(text) = history
        .raw_items()
        .find_map(BaseInstructionsFragment::text_from_item)
    {
        fallback_instructions.text = text.to_owned();
    } else if history.has_tool_declarations() {
        // An incremental window has no recorded base item when its instructions are empty.
        fallback_instructions.text.clear();
    }

    let max_retries = turn_context.provider.info().stream_max_retries();
    let mut retries = 0;
    // Reuse one client session so turn-scoped state (sticky routing and websocket incremental
    // request tracking) survives retries within this compact turn.
    let mut client_session = sess.services.model_client.new_session();
    let compaction_response = loop {
        // Clone is required because of the loop
        let mut turn_input = history
            .clone()
            .for_prompt(&turn_context.model_info().input_modalities);
        sess.services
            .executed_tool_calls
            .attach_to_compaction_prompt(&mut turn_input);
        let turn_input_len = turn_input.len();
```

#### `core/src/compact.rs` lines 359-371 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L359-L371)

```rust
            Err(e) if matches!(e.details(), CodexErrorDetails::ContextWindowExceeded) => {
                if turn_input_len > 1 {
                    // Trim from the beginning to preserve cache (prefix-based) and keep recent messages intact.
                    error!(
                        "Context window exceeded while compacting; removing oldest history item. Error: {e}"
                    );
                    history.remove_first_item();
                    retries = 0;
                    continue;
                }
                sess.set_total_tokens_full(turn_context.as_ref()).await;
                return Err(e);
            }
```

#### `core/src/compact.rs` lines 391-446 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L391-L446)

```rust
    let history_snapshot = sess.clone_history().await;
    let history_items = history_snapshot.annotated_items();
    let summary_suffix = if matches!(compaction_metadata.phase(), CompactionPhase::PostTurn) {
        get_last_assistant_message_from_turn(compaction_response.output.iter())
            .filter(|summary| !summary.trim().is_empty())
            .ok_or_else(|| {
                CodexErr::Stream(
                    "Post-turn compaction completed without an assistant summary".to_string(),
                )
            })?
    } else {
        get_last_assistant_message_from_turn(history_snapshot.raw_items()).unwrap_or_default()
    };
    let summary_text = format!("{SUMMARY_PREFIX}\n{summary_suffix}");
    let user_messages = collect_annotated_user_messages(history_items);

    let mut new_history = build_compacted_history(Vec::new(), &user_messages, &summary_text);
    if let Some(summary_item) = new_history.last_mut() {
        // This replacement history skips `record_conversation_items`; only the appended summary
        // belongs to this compaction turn.
        summary_item.set_turn_id_if_missing(&turn_context.sub_id);
    }
    let (window_number, window_ids) = sess.advance_auto_compact_window().await;

    let (new_history, world_state_baseline) = build_compaction_replacement_history(
        sess.as_ref(),
        &replacement_step_context,
        &world_state,
        new_history,
    )
    .await;
    sess.replace_compacted_history(
        new_history,
        replacement_step_context.to_turn_context_item(),
        world_state_baseline,
        CompactedHistoryMetadata {
            input_goal_ids,
            message: summary_text,
            window_number,
            window_ids,
            compaction_response_id: Some(compaction_response.response_id),
            compaction_model_hash: turn_context.model_info().comp_hash.clone(),
            reviewer_compaction_hash: None,
        },
    )
    .await;
    sess.recompute_token_usage(&replacement_step_context.turn)
        .await;

    sess.emit_turn_item_completed(&turn_context, compaction_item)
        .await;
    let warning = EventMsg::Warning(WarningEvent {
        message: "Heads up: Long threads and multiple compactions can cause the model to be less accurate. Start a new thread when possible to keep threads small and targeted.".to_string(),
    });
    sess.send_event(&turn_context, warning).await;
    Ok(summary_suffix)
```

#### `core/src/compact.rs` lines 604-624 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L604-L624)

```rust
fn compacted_user_message<'a>(
    item: &'a ResponseItem,
    harness_metadata: Option<&'a CodexHarnessMetadata>,
) -> Option<CompactedUserMessage<'a>> {
    let Some(TurnItem::UserMessage(user)) = crate::event_mapping::parse_turn_item(item) else {
        return None;
    };
    let message = user.message();
    if is_summary_message(&message) {
        return None;
    }
    Some(CompactedUserMessage {
        message,
        original: item,
        harness_metadata,
    })
}

pub(crate) fn is_summary_message(message: &str) -> bool {
    message.starts_with(format!("{SUMMARY_PREFIX}\n").as_str())
}
```

#### `core/src/compact.rs` lines 702-819 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact.rs#L702-L819)

```rust
pub(crate) fn build_compacted_history(
    initial_context: Vec<ResponseItemEnvelope>,
    user_messages: &[CompactedUserMessage<'_>],
    summary_text: &str,
) -> Vec<ResponseItemEnvelope> {
    build_compacted_history_with_limit(
        initial_context,
        user_messages,
        summary_text,
        COMPACT_USER_MESSAGE_MAX_TOKENS,
    )
}

fn build_compacted_history_with_limit(
    mut history: Vec<ResponseItemEnvelope>,
    user_messages: &[CompactedUserMessage<'_>],
    summary_text: &str,
    max_tokens: usize,
) -> Vec<ResponseItemEnvelope> {
    let mut selected_messages = Vec::new();
    if max_tokens > 0 {
        let mut remaining = max_tokens;
        for message in user_messages.iter().rev() {
            if remaining == 0 {
                break;
            }
            let tokens = approx_token_count(&message.message);
            let ResponseItem::Message {
                id,
                content,
                internal_chat_message_metadata_passthrough,
                ..
            } = message.original
            else {
                continue;
            };
            let mut harness_metadata = message.harness_metadata.cloned();
            let preserve_content = tokens <= remaining
                && content
                    .iter()
                    .all(|part| matches!(part, ContentItem::InputText { .. }));
            let mut item = ResponseItem::Message {
                status: None,
                encrypted_content: None,
                id: id.clone(),
                role: "user".to_owned(),
                content: if preserve_content {
                    content.clone()
                } else {
                    Vec::new()
                },
                phase: None,
                internal_chat_message_metadata_passthrough:
                    internal_chat_message_metadata_passthrough.clone(),
            };
            if !preserve_content {
                // Select text and its annotations by the same source index.
                // Do not clone discarded images/audio or infer their text's source.
                let metadata = internal_chat_message_metadata_passthrough.as_ref();
                let mut parts: Vec<_> = crate::event_mapping::user_input_parts(content)
                    .filter_map(|(index, part)| {
                        let ContentItem::InputText { text } = part else {
                            return None;
                        };
                        let kind = metadata
                            .and_then(|metadata| metadata.content_item_kinds.as_ref())
                            .and_then(|kinds| kinds.get(index))
                            .cloned()
                            .unwrap_or_else(|| ContentItemKind("unknown".to_string()));
                        let source = metadata
                            .and_then(|metadata| metadata.content_item_metadata.as_ref())
                            .and_then(|sources| sources.get(index))
                            .cloned()
                            .unwrap_or_default();
                        Some(AnnotatedContent::text(text, kind, source))
                    })
                    .collect();
                if parts.is_empty() {
                    // Preserve the existing empty-text fallback for media-only input.
                    parts.push(AnnotatedContent::new(
                        ContentItem::InputText {
                            text: String::new(),
                        },
                        ContentItemKind("unknown".to_string()),
                    ));
                }
                set_annotated_content(&mut item, truncate_annotated_text(parts, remaining));
            }
            if tokens > remaining
                && let Some(metadata) = &mut harness_metadata
            {
                metadata.mark_retained_sources_incomplete();
            }
            selected_messages.push(ResponseItemEnvelope {
                item,
                metadata: harness_metadata,
            });
            if tokens > remaining {
                break;
            }
            remaining = remaining.saturating_sub(tokens);
        }
        selected_messages.reverse();
    }
    history.extend(selected_messages);

    let summary_text = if summary_text.is_empty() {
        "(no summary available)".to_string()
    } else {
        summary_text.to_string()
    };

    history.push(ResponseItemEnvelope::new(ContextualUserFragment::into(
        CompactionSummary::new(summary_text),
    )));

    history
}
```

#### `core/src/context_manager/history.rs` lines 568-577 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/context_manager/history.rs#L568-L577)

```rust
        if let ResponseItem::FunctionCallOutput { output, .. }
        | ResponseItem::CustomToolCallOutput { output, .. } = &mut processed.item
        {
            // The override already includes the tool's serialization allowance.
            let policy = metadata
                .and_then(|metadata| metadata.history_truncation_token_limit)
                .map(TruncationPolicy::Tokens)
                .unwrap_or_else(|| with_serialization_allowance(policy));
            truncate_function_output_payload(output, policy, estimate_audio_token_count);
        }
```

#### `core/src/compact_remote_v2.rs` lines 87-88 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact_remote_v2.rs#L87-L88)

```rust
pub(crate) const RETAINED_MESSAGE_TOKEN_BUDGET: usize = 64_000;
const MAX_RETAINED_AGENT_MESSAGE_TOKENS: i64 = 10_000;
```

#### `core/src/compact_remote_v2.rs` lines 480-505 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact_remote_v2.rs#L480-L505)

```rust
fn build_v2_compacted_history(
    prompt_input: Vec<ResponseItem>,
    prompt_input_metadata: Vec<Option<CodexHarnessMetadata>>,
    compaction_output: ResponseItem,
    features: &Features,
    image_budget: RetainedImageBudget,
) -> (Vec<ResponseItemEnvelope>, usize) {
    debug_assert_eq!(prompt_input.len(), prompt_input_metadata.len());
    let prompt_input = prompt_input
        .into_iter()
        .zip(prompt_input_metadata)
        .map(|(item, metadata)| ResponseItemEnvelope { item, metadata })
        .collect::<Vec<_>>();
    let retained = v2_history_item_groups(prompt_input)
        .filter(|group| is_retained_for_remote_compaction_v2(&group.source, features))
        .flat_map(HistoryItemGroup::into_items)
        .collect::<Vec<_>>();
    let mut retained =
        truncate_retained_messages(retained, RETAINED_MESSAGE_TOKEN_BUDGET, image_budget);
    let retained_image_count = retained
        .iter()
        .map(|envelope| retained_input_image_count(&envelope.item))
        .sum::<usize>();
    retained.push(ResponseItemEnvelope::new(compaction_output));
    (retained, retained_image_count)
}
```

#### `core/src/compact_remote_v2.rs` lines 529-578 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact_remote_v2.rs#L529-L578)

```rust
fn is_retained_for_remote_compaction_v2(
    envelope: &ResponseItemEnvelope,
    features: &Features,
) -> bool {
    if features.enabled(Feature::RetainClientToolOutputs) && is_retained_tool_output(envelope) {
        return true;
    }
    let item = &envelope.item;
    if let ResponseItem::AgentMessage {
        author,
        recipient,
        content,
        ..
    } = item
    {
        let is_descendant_progress = author
            .strip_prefix(recipient)
            .is_some_and(|suffix| suffix.starts_with('/'))
            && matches!(
                content.first(),
                Some(AgentMessageInputContent::InputText { text })
                    if text.starts_with("Message Type: MESSAGE\n")
                        || text.starts_with("Message Type: CHANNEL_POST\n")
            );
        let is_completion = matches!(
            content.first(),
            Some(AgentMessageInputContent::InputText { text })
                if text.starts_with("Message Type: FINAL_ANSWER\n")
        );
        return !is_descendant_progress
            && !is_completion
            && estimate_item_token_count(item) <= MAX_RETAINED_AGENT_MESSAGE_TOKENS;
    }

    let ResponseItem::Message { role, .. } = item else {
        return false;
    };

    match role.as_str() {
        "user" => matches!(
            crate::event_mapping::parse_turn_item(item),
            Some(TurnItem::UserMessage(_) | TurnItem::HookPrompt(_))
        ),
        "developer" => {
            features.enabled(Feature::RetainClientDeveloperMessages)
                && is_client_authored_developer_message(envelope)
        }
        _ => false,
    }
}
```

#### `core/src/compact_remote_v2_attempt.rs` lines 172-185 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact_remote_v2_attempt.rs#L172-L185)

```rust
    input.push(ResponseItem::CompactionTrigger {});
    let prompt = Prompt {
        input,
        tools: if tools_in_history {
            Arc::default()
        } else {
            tool_router.model_visible_specs()
        },
        parallel_tool_calls: true,
        base_instructions,
        output_schema: None,
        output_schema_strict: true,
        cyber_access_program: turn_context.cyber_access_program,
    };
```

#### `core/src/compact_remote_v2.rs` lines 845-887 (https://github.com/openai/codex/blob/ac9db7d8b556c7c34a73793aed34b1f980a4fe39/codex-rs/core/src/compact_remote_v2.rs#L845-L887)

```rust
    fn build_v2_compacted_history_filters_to_installed_retention_shape() {
        let hook = codex_protocol::items::build_hook_prompt_message(&[
            codex_protocol::items::HookPromptFragment::from_single_hook("hook", "hook-run"),
        ])
        .expect("hook prompt");
        let input = vec![
            message("developer", "dev", /*phase*/ None),
            message("system", "sys", /*phase*/ None),
            message("user", "user", /*phase*/ None),
            hook.clone(),
            message("assistant", "commentary", Some(MessagePhase::Commentary)),
            message("assistant", "final", Some(MessagePhase::FinalAnswer)),
            ResponseItem::FunctionCall {
                status: None,
                encrypted_content: None,
                id: None,
                name: "shell_command".to_string(),
                namespace: None,
                arguments: "{}".to_string(),
                call_id: "call_1".to_string(),
                encrypted_function_args: None,
                internal_chat_message_metadata_passthrough: None,
            },
            ResponseItem::Compaction {
                id: None,
                encrypted_content: "old".to_string(),
                internal_chat_message_metadata_passthrough: None,
            },
        ];
        let output = ResponseItem::Compaction {
            id: None,
            encrypted_content: "new".to_string(),
            internal_chat_message_metadata_passthrough: None,
        };

        let (history, _) = build_without_metadata(input, output.clone());

        assert_eq!(
            raw(history),
            vec![message("user", "user", /*phase*/ None), hook, output]
        );
    }

```

#### `models-manager/models.json` (extracted fields: slug, context_window, max_context_window, auto_compact_token_limit, truncation_policy)

```
gpt-6-astra 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-6.1-sol 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-6-sol 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-6-luna 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-5.6-sol 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-5.6-terra 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-5.6-luna 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-daybreak-blue-latest 272000 872000 None {'mode': 'tokens', 'limit': 10000}
gpt-daybreak-red-latest 372000 372000 None {'mode': 'tokens', 'limit': 10000}
gpt-5.5 272000 272000 None {'mode': 'tokens', 'limit': 10000}
codex-auto-review 272000 872000 None {'mode': 'tokens', 'limit': 10000}
```

#### Feature defaults (`features/src/lib.rs` L1909-1914, L2065-2070) and async threshold (`core/src/config/mod.rs` L225)

```rust
    FeatureSpec {
        id: Feature::TokenBudget,
        key: "token_budget",
        stage: Stage::UnderDevelopment,
        default_enabled: false,
    },
    FeatureSpec {
        id: Feature::AsyncCompaction,
        key: "async_compaction",
        stage: Stage::UnderDevelopment,
        default_enabled: false,
    },
const DEFAULT_ASYNC_COMPACTION_THRESHOLD_PERCENT: u8 = 65;
```
