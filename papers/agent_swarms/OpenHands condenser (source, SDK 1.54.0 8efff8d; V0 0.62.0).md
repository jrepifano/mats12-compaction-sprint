# OpenHands condensers (source code: OpenHands software-agent-sdk at 8efff8d, and legacy OpenHands V0 at tag 0.62.0)

**Sources:**
- Current agent (V1): https://github.com/OpenHands/software-agent-sdk , commit `8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547` (committed 2026-10-15T03:24:31+02:00), `openhands-sdk` version 1.54.0. The main `OpenHands/OpenHands` repo (HEAD `e4775b4`, 2026-10-15) is now the app/frontend and contains no condenser code; the agent and condensers live in this SDK.
- Legacy V0 condenser (as used by most 2025 papers, e.g. "The Complexity Trap"): https://github.com/OpenHands/OpenHands at tag `0.62.0` (commit `7fbb48c40679afd674970966b96185657d92a487`), files `openhands/memory/condenser/impl/llm_summarizing_condenser.py` and `openhands/core/config/condenser_config.py`, fetched from raw.githubusercontent.com.
**Licence:** MIT (SDK `LICENSE`, Copyright (c) 2026 OpenHands contributors; OpenHands V0 core is MIT). Excerpts quoted with attribution.
**Retrieved:** 2026-10-14; read only (not run).
**Reading depth:** excerpts verbatim. SDK `llm_summarizing_condenser.py` and both prompt templates read in full; event `__str__` methods (`event/base.py`, `event/llm_convertible/*.py`) and `openhands-tools/.../preset/default.py` read in part. V0 condenser file read in full, V0 config in part. The Summary is my reading.

## Summary (my reading of the code)

**V1 (SDK 1.54.0, `LLMSummarizingCondenser`)**
- **Defaults:** the class defaults are `max_size=240` events, `keep_first=2`, `max_tokens=None`, `minimum_progress=0.1`. The default agent (`get_default_agent`) and spawned sub-agents use `default_condenser(llm)` with `max_size=80`, `keep_first=4`, and the summariser LLM is a copy of the agent's LLM (`usage_id="condenser"`), i.e. the same model.
- **Triggers:** (a) the view has more than `max_size` events (soft); (b) total tokens exceed min(`max_tokens`, the agent LLM's `effective_max_input_tokens`), which defaults to the model's `max_input_tokens` from LiteLLM metadata, i.e. roughly the full context window (hard); (c) an explicit condensation request (hard). For a 200k / 1M model the token trigger is therefore about 200k / 1M input tokens, and the event trigger (80 events) usually fires first.
- **What is kept:** the system prompt event, the first `keep_first` events, and a tail: `max_size // 2 - keep_first - 1` events for the event trigger (35 with the default agent), enough recent events to get under half of the token cap for the token trigger, half the view for a request; cut points respect tool-call/observation units. A summary event is inserted after the kept head.
- **Summariser input:** every forgotten event is rendered with `str(event)`. Those `__str__` methods cut text at `N_CHAR_PREVIEW = 500` characters: messages and observations are previewed to 500 characters, an action event shows only a 500-character thought preview and the action class name (no arguments). Previous `CondensationSummaryEvent`s use the generic `LLMConvertibleEvent.__str__`, so an earlier summary reaches the next summariser cut to 497 characters + "...". (Inference from the code; not run.)
- **Repeated compaction:** the previous summary is one of the forgotten events (the system prompt says "which will include previous summaries"), subject to the 500-character preview above.

**V0 (OpenHands 0.62.0, `LLMSummarizingCondenser`)**
- Config defaults `keep_first=1`, `max_size=100`, `max_event_length=10_000` characters. Triggers when the view has more than `max_size` events or on request. Keeps `keep_first` head events and `max_size // 2 - keep_first - 1` tail events (48), summarises the rest with one user-role prompt, passing the previous summary in `<PREVIOUS SUMMARY>` tags (truncated to `max_event_length`) and each forgotten event's `str()` truncated to 10,000 characters. Summariser LLM configured separately (`llm_registry.get_llm('condenser', ...)`), prompt caching off.

## Verbatim excerpts: V1 SDK

#### `openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_system.j2` lines 1-47 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_system.j2#L1-L47)

```text
You are maintaining a context-aware state summary for an interactive agent.
You will be given a list of events corresponding to actions taken by the agent, which will include previous summaries.
If the events being summarized contain ANY task-tracking, you MUST include a TASK_TRACKING section to maintain continuity.
When referencing tasks make sure to preserve exact task IDs and statuses.

Track:

USER_CONTEXT: (Preserve essential user requirements, goals, and clarifications in concise form)

TASK_TRACKING: {Active tasks, their IDs and statuses - PRESERVE TASK IDs}

COMPLETED: (Tasks completed so far, with brief results)
PENDING: (Tasks that still need to be done)
CURRENT_STATE: (Current variables, data structures, or relevant state)

For code-specific tasks, also include:
CODE_STATE: {File paths, function signatures, data structures}
TESTS: {Failing cases, error messages, outputs}
CHANGES: {Code edits, variable updates}
DEPS: {Dependencies, imports, external calls}
VERSION_CONTROL_STATUS: {Repository state, current branch, PR status, commit history}

PRIORITIZE:
1. Adapt tracking format to match the actual task type
2. Capture key user requirements and goals
3. Distinguish between completed and pending tasks
4. Keep all sections concise and relevant

SKIP: Tracking irrelevant details for the current task type

Example formats:

For code tasks:
USER_CONTEXT: Fix FITS card float representation issue
COMPLETED: Modified mod_float() in card.py, all tests passing
PENDING: Create PR, update documentation
CODE_STATE: mod_float() in card.py updated
TESTS: test_format() passed
CHANGES: str(val) replaces f"{val:.16G}"
DEPS: None modified
VERSION_CONTROL_STATUS: Branch: fix-float-precision, Latest commit: a1b2c3d

For other tasks:
USER_CONTEXT: Write 20 haikus based on coin flip results
COMPLETED: 15 haikus written for results [T,H,T,H,T,H,T,T,H,T,H,T,H,T,H]
PENDING: 5 more haikus needed
CURRENT_STATE: Last flip: Heads, Haiku count: 15/20
```

#### `openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_events.j2` lines 1-7 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_events.j2#L1-L7)

```jinja
{% for event in events %}
<EVENT>
{{ event }}
</EVENT>
{% endfor %}

Now summarize the events using the rules above.
```

#### `openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py` lines 54-97 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L54-L97)

```python
class LLMSummarizingCondenser(RollingCondenser):
    """LLM-based condenser that summarizes forgotten events.

    Uses an independent LLM (stored in the `llm` attribute) for generating summaries
    of forgotten events. The optional `agent_llm` parameter passed to condense() is
    the LLM used by the agent for token counting purposes, and you should not assume
    it is the same as the one defined in this condenser.
    """

    llm: LLM
    max_size: int = Field(default=240, gt=0)
    max_tokens: int | None = None

    keep_first: int = Field(default=2, ge=0)
    """Minimum number of events to preserve at the start of the view. The first
    `keep_first` events in the conversation will never be condensed or summarized.
    """

    minimum_progress: float = Field(default=0.1, gt=0.0, lt=1.0)
    """Minimum fraction of events that must be condensed (0.0-1.0). If fewer than
    this proportion of events would be forgotten, condensation is treated as an error.
    Default 0.1 means at least 10% of events must be condensed.
    """
    """Minimum ratio of the view to be condensed. Condensations below this threshold
    are treated as errors.
    """

    hard_context_reset_max_retries: int = Field(default=5, gt=0)
    """Number of attempts to perform hard context reset before raising an error."""

    hard_context_reset_context_scaling: float = Field(default=0.8, gt=0.0, lt=1.0)
    """When performing hard context reset, if the summarization fails, reduce the max
    size of each event string by this factor and retry.
    """

    @model_validator(mode="after")
    def validate_keep_first_vs_max_size(self):
        events_from_tail = self.max_size // 2 - self.keep_first - 1
        if events_from_tail <= 0:
            raise ValueError(
                "keep_first must be less than max_size // 2 to leave room for "
                "condensation"
            )
        return self
```

#### `openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py` lines 115-222 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L115-L222)

```python
    def _effective_max_tokens(self, agent_llm: LLM | None) -> int | None:
        """Return the effective token cap that triggers token-based condensation.

        Takes the stricter (i.e. smaller) of the condenser's configured
        ``max_tokens`` and the agent LLM's effective input limit. ``agent_llm``
        is optional throughout the public API -- callers that only assess
        request/event-count pressure may pass ``None`` -- in which case only the
        condenser's own ``max_tokens`` applies.

        Returns ``None`` when no limit is configured.
        """
        limits = [
            limit
            for limit in (
                self.max_tokens,
                agent_llm.effective_max_input_tokens if agent_llm is not None else None,
            )
            if limit is not None
        ]
        return min(limits) if limits else None

    def get_condensation_reasons(
        self, view: View, agent_llm: LLM | None = None
    ) -> set[Reason]:
        """Determine the reasons why the view should be condensed.

        Args:
            view: The current view to evaluate.
            agent_llm: The LLM used by the agent. Required if token counting is needed.

        Returns:
            A set of Reason enums indicating why condensation is needed.
        """
        reasons = set()

        # Reason 1: Unhandled condensation request. The view handles the detection of
        # these requests while processing the event stream.
        if view.unhandled_condensation_request:
            reasons.add(Reason.REQUEST)

        # Reason 2: Token limit is provided and exceeded.
        max_tokens = self._effective_max_tokens(agent_llm)
        if max_tokens is not None and agent_llm is not None:
            total_tokens = get_total_token_count(view.events, agent_llm)
            if total_tokens > max_tokens:
                logger.info(
                    "Condenser token limit exceeded: total_tokens=%d max_tokens=%d "
                    "events=%d",
                    total_tokens,
                    max_tokens,
                    len(view),
                )
                reasons.add(Reason.TOKENS)

        # Reason 3: View exceeds maximum size in number of events.
        if len(view) > self.max_size:
            reasons.add(Reason.EVENTS)

        return reasons

    def condensation_requirement(
        self, view: View, agent_llm: LLM | None = None
    ) -> CondensationRequirement | None:
        reasons = self.get_condensation_reasons(view, agent_llm)

        # No reasons => no condensation needed.
        if reasons == set():
            return None

        # Token pressure is a hard requirement in benchmark runs that use a fixed
        # local model context: sending the next request can fail before the recovery
        # path has a chance to run. Treat event-count pressure as soft because that
        # threshold is only a history-management heuristic.
        if Reason.TOKENS in reasons:
            return CondensationRequirement.HARD

        # If the remaining reasons are for resource constraints, we can treat them as
        # a soft requirement. We want to condense when we can, but there's still space
        # in the context window or we'd also see Reason.REQUEST.
        resource_reasons = {Reason.EVENTS}
        if reasons.issubset(resource_reasons):
            return CondensationRequirement.SOFT

        # Requests -- whether they come from the user or the agent -- are always hard
        # requirements. We need to condense now because:
        # 1. the user expects it
        # 2. the agent has no more room in the context window and can't continue
        if Reason.REQUEST in reasons:
            return CondensationRequirement.HARD

    def _build_summary_messages(self, event_strings: Sequence[str]) -> list[Message]:
        """Build the messages sent to the summarization LLM.

        The summarization instructions are sent as a ``system`` message and the
        events to summarize as a ``user`` message. Splitting roles keeps the
        steering instructions in the provider's ``instructions``/``system`` slot
        and the event payload in the ``input``/``user`` slot, which is the
        canonical shape for both the Chat Completions and Responses APIs.
        """
        prompt_dir = os.path.join(os.path.dirname(__file__), "prompts")
        system_prompt = render_template(prompt_dir, "summarizing_system.j2")
        events_prompt = render_template(
            prompt_dir, "summarizing_events.j2", events=event_strings
        )
        return [
            Message(role="system", content=[TextContent(text=system_prompt)]),
            Message(role="user", content=[TextContent(text=events_prompt)]),
        ]
```

#### `openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py` lines 245-276 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L245-L276)

```python
        assert len(forgotten_events) > 0, "No events to condense."

        # Convert events to strings for the template
        event_strings = [
            maybe_truncate(str(forgotten_event), truncate_after=max_event_str_length)
            for forgotten_event in forgotten_events
        ]

        messages = self._build_summary_messages(event_strings)

        # Do not pass extra_body explicitly. The LLM handles forwarding
        # litellm_extra_body only when it is non-empty.
        try:
            llm_response = self.llm.generate(messages=messages, store=False)
        except Exception as e:
            raise NoCondensationAvailableException(
                f"Summarization LLM call failed: {e}"
            ) from e

        # Extract summary from the LLMResponse message
        summary = None
        if llm_response.message.content:
            first_content = llm_response.message.content[0]
            if isinstance(first_content, TextContent):
                summary = first_content.text

        return Condensation(
            forgotten_event_ids={event.id for event in forgotten_events},
            summary=summary,
            summary_offset=summary_offset,
            llm_response_id=llm_response.id,
        )
```

#### `openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py` lines 278-352 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L278-L352)

```python
    def _get_forgotten_events(
        self, view: View, agent_llm: LLM | None = None
    ) -> tuple[Sequence[LLMConvertibleEvent], int]:
        """Identify events to be forgotten and the summary offset.

        Relies on the condensation reasons to determine how many events we need to drop
        in order to maintain our resource constraints. Uses manipulation indices to
        ensure forgetting ranges respect atomic unit boundaries.

        Args:
            view: The current view from which to identify forgotten events.
            agent_llm: The LLM used by the agent, required for token-based calculations.

        Returns:
            A tuple of (events to forget, summary_offset).
        """
        reasons = self.get_condensation_reasons(view, agent_llm=agent_llm)
        assert reasons != set(), "No condensation reasons found."

        suffix_events_to_keep: set[int] = set()

        if Reason.REQUEST in reasons:
            target_size = len(view) // 2
            suffix_events_to_keep.add(target_size - self.keep_first - 1)

        if Reason.EVENTS in reasons:
            target_size = self.max_size // 2
            suffix_events_to_keep.add(target_size - self.keep_first - 1)

        if Reason.TOKENS in reasons:
            # Compute the number of tokens we need to eliminate to be under half the
            # effective max_tokens value. We know both are not None here because we
            # cannot have Reason.TOKENS without them.
            max_tokens = self._effective_max_tokens(agent_llm)
            assert max_tokens is not None
            assert agent_llm is not None

            total_tokens = get_total_token_count(view.events, agent_llm)
            tokens_to_reduce = total_tokens - (max_tokens // 2)

            suffix_events_to_keep.add(
                get_suffix_length_for_token_reduction(
                    events=view.events[self.keep_first :],
                    llm=agent_llm,
                    token_reduction=tokens_to_reduce,
                    base_events=view.events[: self.keep_first],
                )
            )

        # We might have multiple reasons to condense, so pick the strictest condensation
        # to ensure all resource constraints are met.
        events_from_tail = min(suffix_events_to_keep)

        # Calculate naive forgetting end (without considering atomic boundaries)
        naive_end = len(view) - events_from_tail

        # The leading SystemPromptEvent must never be forgotten, otherwise the
        # condensed view would open with a non-system message. Floor the start
        # of the forgetting range just past it (this also covers keep_first=0).
        protected_prefix = self.keep_first
        system_idx = _leading_system_prompt_index(view.events)
        if system_idx is not None:
            protected_prefix = max(protected_prefix, system_idx + 1)

        # Find actual forgetting_start: smallest manipulation index >= protected_prefix
        forgetting_start = view.manipulation_indices.find_next(protected_prefix)

        # Find actual forgetting_end: smallest manipulation index >= naive_end
        forgetting_end = view.manipulation_indices.find_next(naive_end)

        # Extract events to forget using boundary-aware indices
        forgotten_events = view[forgetting_start:forgetting_end]

        # Summary offset is the same as forgetting_start
        return forgotten_events, forgetting_start
```

#### `openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py` lines 552-562 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L552-L562)

```python
# Sizing for the standard summarizing condenser. Kept here so the default agent and
# spawned sub-agents stay in sync.
_DEFAULT_MAX_SIZE: Final[int] = 80
_DEFAULT_KEEP_FIRST: Final[int] = 4


def default_condenser(llm: LLM) -> LLMSummarizingCondenser:
    """Standard summarizing condenser used by the default agent and sub-agents."""
    return LLMSummarizingCondenser(
        llm=llm, max_size=_DEFAULT_MAX_SIZE, keep_first=_DEFAULT_KEEP_FIRST
    )
```

#### `openhands-tools/openhands/tools/preset/default.py` lines 71-106 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-tools/openhands/tools/preset/default.py#L71-L106)

```python
def get_default_condenser(llm: LLM) -> CondenserBase:
    # Shared with spawned sub-agents (see sdk default_condenser) so both stay in sync.
    return default_condenser(llm)


def get_default_agent(
    llm: LLM,
    cli_mode: bool = False,
    finish_tool_response_schema: Any | None = None,
) -> Agent:
    tools = get_default_tools(
        # Disable browser tools in CLI mode
        enable_browser=not cli_mode,
    )
    agent_kwargs: dict[str, Any] = {}
    if finish_tool_response_schema is not None:
        if FinishTool.__name__ not in list_registered_tools():
            register_tool(FinishTool.__name__, FinishTool)
        tools.append(
            Tool(
                name=FinishTool.__name__,
                params={"response_schema": finish_tool_response_schema},
            )
        )
        agent_kwargs["include_default_tools"] = [
            tool.__name__ for tool in BUILT_IN_TOOLS if tool is not FinishTool
        ]

    agent = Agent(
        llm=llm,
        tools=tools,
        system_prompt_kwargs={"cli_mode": cli_mode},
        condenser=get_default_condenser(
            llm=llm.model_copy(update={"usage_id": "condenser"})
        ),
        **agent_kwargs,
```

#### `openhands-sdk/openhands/sdk/event/base.py` lines 17-17 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/event/base.py#L17-L17)

```python
N_CHAR_PREVIEW = 500
```

#### `openhands-sdk/openhands/sdk/event/base.py` lines 82-104 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/event/base.py#L82-L104)

```python
    def __str__(self) -> str:
        """Plain text string representation showing LLM message content."""
        base_str = super().__str__()
        try:
            llm_message = self.to_llm_message()
            # Extract text content from the message
            text_parts = []
            for content in llm_message.content:
                if isinstance(content, TextContent):
                    text_parts.append(content.text)
                elif isinstance(content, ImageContent):
                    text_parts.append(f"[Image: {len(content.image_urls)} URLs]")

            if text_parts:
                content_preview = " ".join(text_parts)
                # Truncate long content for display
                if len(content_preview) > N_CHAR_PREVIEW:
                    content_preview = content_preview[: N_CHAR_PREVIEW - 3] + "..."
                return f"{base_str}\n  {llm_message.role}: {content_preview}"
            else:
                return f"{base_str}\n  {llm_message.role}: [no text content]"
        except Exception:
            # Fallback to base representation if LLM message conversion fails
```

#### `openhands-sdk/openhands/sdk/event/llm_convertible/message.py` lines 121-141 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/event/llm_convertible/message.py#L121-L141)

```python
    def __str__(self) -> str:
        """Plain text string representation for MessageEvent."""
        base_str = f"{self.__class__.__name__} ({self.source})"
        # Extract text content from the message
        text_parts = []
        message = self.to_llm_message()
        for content in message.content:
            if isinstance(content, TextContent):
                text_parts.append(content.text)
            elif isinstance(content, ImageContent):
                text_parts.append(f"[Image: {len(content.image_urls)} URLs]")

        if text_parts:
            content_preview = " ".join(text_parts)
            if len(content_preview) > N_CHAR_PREVIEW:
                content_preview = content_preview[: N_CHAR_PREVIEW - 3] + "..."
            skill_info = (
                f" [Skills: {', '.join(self.activated_skills)}]"
                if self.activated_skills
                else ""
            )
```

#### `openhands-sdk/openhands/sdk/event/llm_convertible/action.py` lines 146-164 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/event/llm_convertible/action.py#L146-L164)

```python
    def __str__(self) -> str:
        """Plain text string representation for ActionEvent."""
        base_str = f"{self.__class__.__name__} ({self.source})"
        thought_text = " ".join([t.text for t in self.thought])
        thought_preview = (
            thought_text[:N_CHAR_PREVIEW] + "..."
            if len(thought_text) > N_CHAR_PREVIEW
            else thought_text
        )
        if self.action:
            action_name = self.action.__class__.__name__
            return f"{base_str}\n  Thought: {thought_preview}\n  Action: {action_name}"
        else:
            # When action is None (non-executable), show the tool call
            call = f"{self.tool_call.name}:{self.tool_call.id}"
            return (
                f"{base_str}\n  Thought: {thought_preview}\n  Action: (not executed)"
                f"\n  Call: {call}"
            )
```

#### `openhands-sdk/openhands/sdk/event/llm_convertible/observation.py` lines 74-83 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/event/llm_convertible/observation.py#L74-L83)

```python
    def __str__(self) -> str:
        """Plain text string representation for ObservationEvent."""
        base_str = f"{self.__class__.__name__} ({self.source})"
        content_str = "".join(content_to_str(self.observation.to_llm_content))
        obs_preview = (
            content_str[:N_CHAR_PREVIEW] + "..."
            if len(content_str) > N_CHAR_PREVIEW
            else content_str
        )
        return f"{base_str}\n  Tool: {self.tool_name}\n  Result: {obs_preview}"
```

#### `openhands-sdk/openhands/sdk/event/condenser.py` lines 120-131 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/event/condenser.py#L120-L131)

```python
class CondensationSummaryEvent(LLMConvertibleEvent):
    """This event represents a summary generated by a condenser."""

    summary: str
    """The summary text."""

    source: SourceType = "environment"

    def to_llm_message(self) -> Message:
        return Message(
            role="user",
            content=[TextContent(text=self.summary)],
```

#### `openhands-sdk/openhands/sdk/llm/llm.py` lines 2862-2877 (https://github.com/OpenHands/software-agent-sdk/blob/8efff8d3cf1a5a3fcf7ce5e3b97c7f00684d9547/openhands-sdk/openhands/sdk/llm/llm.py#L2862-L2877)

```python
    def effective_max_input_tokens(self) -> int | None:
        """Resolved context window used at runtime.

        ``max_input_tokens`` remains the user-configured value and always wins.
        When it is unset, a previously resolved provider-aware runtime limit is
        used (this property performs no network I/O), falling back to the value
        discovered from model metadata.
        """
        if self.max_input_tokens:
            return self.max_input_tokens
        cached = cached_metadata(
            self._runtime_metadata, self._runtime_metadata_fetched_at
        )
        if cached is not None and cached.max_input_tokens is not None:
            return cached.max_input_tokens
        return self._effective_max_input_tokens
```

## Verbatim excerpts: V0 (OpenHands 0.62.0)

#### `openhands/core/config/condenser_config.py` lines 61-88 (https://github.com/OpenHands/OpenHands/blob/7fbb48c40679afd674970966b96185657d92a487/openhands/core/config/condenser_config.py#L61-L88)

```python
class LLMSummarizingCondenserConfig(BaseModel):
    """Configuration for LLMCondenser."""

    type: Literal['llm'] = Field(default='llm')
    llm_config: LLMConfig = Field(
        ..., description='Configuration for the LLM to use for condensing.'
    )

    # at least one event by default, because the best guess is that it's the user task
    keep_first: int = Field(
        default=1,
        description='Number of initial events to always keep in history.',
        ge=0,
    )
    max_size: int = Field(
        default=100,
        description='Maximum size of the condensed history before triggering forgetting.',
        ge=2,
    )
    max_event_length: int = Field(
        default=10_000,
        description='Maximum length of the event representations to be passed to the LLM.',
    )

    model_config = ConfigDict(extra='forbid')


class AmortizedForgettingCondenserConfig(BaseModel):
```

#### `openhands/memory/condenser/impl/llm_summarizing_condenser.py` lines 52-161 (https://github.com/OpenHands/OpenHands/blob/7fbb48c40679afd674970966b96185657d92a487/openhands/memory/condenser/impl/llm_summarizing_condenser.py#L52-L161)

```python
    def get_condensation(self, view: View) -> Condensation:
        head = view[: self.keep_first]
        target_size = self.max_size // 2
        # Number of events to keep from the tail -- target size, minus however many
        # prefix events from the head, minus one for the summarization event
        events_from_tail = target_size - len(head) - 1

        summary_event = (
            view[self.keep_first]
            if isinstance(view[self.keep_first], AgentCondensationObservation)
            else AgentCondensationObservation('No events summarized')
        )

        # Identify events to be forgotten (those not in head or tail)
        forgotten_events = []
        for event in view[self.keep_first : -events_from_tail]:
            if not isinstance(event, AgentCondensationObservation):
                forgotten_events.append(event)

        # Construct prompt for summarization
        prompt = """You are maintaining a context-aware state summary for an interactive agent.
You will be given a list of events corresponding to actions taken by the agent, and the most recent previous summary if one exists.
If the events being summarized contain ANY task-tracking, you MUST include a TASK_TRACKING section to maintain continuity.
When referencing tasks make sure to preserve exact task IDs and statuses.

Track:

USER_CONTEXT: (Preserve essential user requirements, goals, and clarifications in concise form)

TASK_TRACKING: {Active tasks, their IDs and statuses - PRESERVE TASK IDs}

COMPLETED: (Tasks completed so far, with brief results)
PENDING: (Tasks that still need to be done)
CURRENT_STATE: (Current variables, data structures, or relevant state)

For code-specific tasks, also include:
CODE_STATE: {File paths, function signatures, data structures}
TESTS: {Failing cases, error messages, outputs}
CHANGES: {Code edits, variable updates}
DEPS: {Dependencies, imports, external calls}
VERSION_CONTROL_STATUS: {Repository state, current branch, PR status, commit history}

PRIORITIZE:
1. Adapt tracking format to match the actual task type
2. Capture key user requirements and goals
3. Distinguish between completed and pending tasks
4. Keep all sections concise and relevant

SKIP: Tracking irrelevant details for the current task type

Example formats:

For code tasks:
USER_CONTEXT: Fix FITS card float representation issue
COMPLETED: Modified mod_float() in card.py, all tests passing
PENDING: Create PR, update documentation
CODE_STATE: mod_float() in card.py updated
TESTS: test_format() passed
CHANGES: str(val) replaces f"{val:.16G}"
DEPS: None modified
VERSION_CONTROL_STATUS: Branch: fix-float-precision, Latest commit: a1b2c3d

For other tasks:
USER_CONTEXT: Write 20 haikus based on coin flip results
COMPLETED: 15 haikus written for results [T,H,T,H,T,H,T,T,H,T,H,T,H,T,H]
PENDING: 5 more haikus needed
CURRENT_STATE: Last flip: Heads, Haiku count: 15/20"""

        prompt += '\n\n'

        # Add the previous summary if it exists. We'll always have a summary
        # event, but the types aren't precise enought to guarantee that it has a
        # message attribute.
        summary_event_content = self._truncate(
            summary_event.message if summary_event.message else ''
        )
        prompt += f'<PREVIOUS SUMMARY>\n{summary_event_content}\n</PREVIOUS SUMMARY>\n'

        prompt += '\n\n'

        # Add all events that are being forgotten. We use the string
        # representation defined by the event, and truncate it if necessary.
        for forgotten_event in forgotten_events:
            event_content = self._truncate(str(forgotten_event))
            prompt += f'<EVENT id={forgotten_event.id}>\n{event_content}\n</EVENT>\n'

        prompt += 'Now summarize the events using the rules above.'

        messages = [Message(role='user', content=[TextContent(text=prompt)])]

        response = self.llm.completion(
            messages=self.llm.format_messages_for_llm(messages),
            extra_body={'metadata': self.llm_metadata},
        )
        summary = response.choices[0].message.content

        self.add_metadata('response', response.model_dump())
        self.add_metadata('metrics', self.llm.metrics.get())

        return Condensation(
            action=CondensationAction(
                forgotten_events_start_id=min(event.id for event in forgotten_events),
                forgotten_events_end_id=max(event.id for event in forgotten_events),
                summary=summary,
                summary_offset=self.keep_first,
            )
        )

    def should_condense(self, view: View) -> bool:
        return len(view) > self.max_size or view.unhandled_condensation_request
```
