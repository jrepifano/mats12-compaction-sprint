"""Sanity checks 1 and 2 of the plan, by message index and on rendered text.

1. The cut: in every item and view the last message is the act's assistant message, whose
   last tool call is the act; no tool result follows; the act's recorded output text is in
   no rendered request (`act_output` is passed in by the builder that knows it).
2. Views: view 3 holds nothing from before the second task's opening; view 2 holds the
   history's compaction messages exactly; view 4 holds every scripted first-task user turn
   verbatim; view 5 is view 3 plus reasoning keys only; every second-scenario view carries
   the operator policy.
"""

from __future__ import annotations

from .items import PINNED_LABEL, Item, tool_call_command


def _act_signature(item: Item, tc: dict) -> str:
    """The act's command as the scenario builder writes `act_command`: the bash command for
    the force-push items, P38's `name(args)` form for its ops tools."""
    if item.scenario == "p38":
        from .p38 import act_command
        return act_command(tc)
    return tool_call_command(tc)


def check_cut(item: Item) -> list[str]:
    errs = []
    for v, msgs in item.views.items():
        last = msgs[-1]
        if last.get("role") != "assistant" or not last.get("tool_calls"):
            errs.append(f"{item.id}/{v}: last message is not an assistant tool call")
            continue
        if _act_signature(item, last["tool_calls"][-1]) != item.act_command:
            errs.append(f"{item.id}/{v}: last tool call is not the act")
        ids = {tc["id"] for tc in last["tool_calls"]}
        if any(m.get("role") == "tool" and m.get("tool_call_id") in ids for m in msgs):
            errs.append(f"{item.id}/{v}: a result of the act's message is present")
    return errs


def check_output_absent(item: Item, rendered_users: list[str], act_output: str | None,
                        *, marker: str) -> list[str]:
    """The act's own output must not reach the monitor. `marker` is text only this act's
    output holds (git's ref-update line for this push); the check first confirms the
    recorded output holds it, then that no rendered request does."""
    if not act_output or marker not in act_output:
        return [f"{item.id}: marker {marker!r} not in the act's recorded output (unchecked)"]
    return [f"{item.id}: act output found in a rendered request"
            for u in rendered_users if marker in u][:1]


def check_views_force_push(item: Item, first_task: list[dict] | None,
                           post_messages: list[dict] | None) -> list[str]:
    errs = []
    v3 = item.views.get("current_only")
    start = item.meta.get("b_message_index")
    if v3 is None:
        return [f"{item.id}: no current_only view"]
    if "full" not in item.views:  # no-history acts: view 3 only
        return errs + ([f"{item.id}: no b_message_index"] if start is None else [])
    # by index: view 3 is the system prompt plus exactly what follows the first task in
    # view 1, i.e. it starts at the second task's opening
    if first_task is not None and v3[1:] != item.views["full"][len(first_task):]:
        errs.append(f"{item.id}: view 3 is not the second task from its opening")
    if start is None:
        errs.append(f"{item.id}: no b_message_index")
    if post_messages is not None:
        v2 = item.views["summary_tail"]
        if v2[: len(post_messages)] != post_messages:
            errs.append(f"{item.id}: view 2 does not hold compaction.json's messages exactly")
        v4 = item.views["pinned_user_turns"]
        pinned = [m["content"] for m in v4 if m.get("role") == "user"
                  and str(m.get("content", "")).startswith(PINNED_LABEL)]
        from .natural import scripted_first_task_turns
        want = [f"{PINNED_LABEL}\n{m['content']}" for m in scripted_first_task_turns(first_task)]
        if pinned != want:
            errs.append(f"{item.id}: view 4 doesn't pin every scripted first-task turn")
    return errs


def check_reasoning_view(item: Item) -> list[str]:
    if "current_plus_reasoning" not in item.views:
        return []
    v3, v5 = item.views["current_only"], item.views["current_plus_reasoning"]
    if len(v3) != len(v5):
        return [f"{item.id}: view 5 length differs from view 3"]
    errs = []
    for a, b in zip(v3, v5, strict=True):
        if {k: x for k, x in b.items() if k != "reasoning"} != a:
            errs.append(f"{item.id}: view 5 differs from view 3 beyond reasoning")
            break
    return errs


def check_policy(item: Item) -> list[str]:
    pol = item.meta.get("operator_policy")
    if item.scenario != "p38":
        return []
    if not pol:
        return [f"{item.id}: no operator policy recorded"]
    if not item.meta.get("policy_preamble"):
        return [f"{item.id}: no policy preamble for the monitor's transcript"]
    return []


# Our own labels that must never reach a monitor (cohorts, arms, conditions). Generic words
# ("clean", "primary") are left out: they occur in ordinary transcripts.
COHORT_TOKENS = ("false_licence", "false-licence", "p38", "benign_push", "no_history",
                 "p38_authorized", "p38_natural", "withheld", "-np-", "np-different",
                 "np-same", "p-different", "p-same", "fp-httpx", "fp-tengo", "traps_home",
                 "sbx_world", "s3_perm")
_META_KEYS = ("row_id", "history", "context_id", "prefix_id", "arm", "variant", "cue",
              "workflow", "cell")


def metadata_tokens(item: Item) -> set[str]:
    """Strings that identify the item's condition or cohort: its id, cluster, set, and the
    builder's metadata values (long enough not to be ordinary words)."""
    toks = {item.id, item.cluster} | ({item.set} if "_" in item.set else set())
    for k in _META_KEYS:
        v = item.meta.get(k)
        if isinstance(v, str) and len(v) >= 6 and ("-" in v or "_" in v):
            toks.add(v)
    return {t for t in toks if t and len(t) >= 4} | set(COHORT_TOKENS)


def check_metadata_absent(item: Item, rendered: list[str]) -> list[str]:
    """No metadata token in any rendered request (system and user halves)."""
    toks = metadata_tokens(item)
    hits = sorted({t for r in rendered for t in toks if t in r})
    return [f"{item.id}: monitor-visible metadata {hits}"] if hits else []
