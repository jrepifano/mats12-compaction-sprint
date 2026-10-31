"""Experiment 2's second scenario: probe P38's production-ops acts as monitor items.

Plan: docs/sprint/plans/week2-monitors.md v0.4, "Second scenario: production-ops acts";
inventory: docs/sprint/next-experiments/r5_monitor_other_scenarios.md (branch sprint/prep).

Sources (read-only, P38's own files; episodes are gitignored, so they live only in the main
checkout's probe directory, `p38_dir` below):
  data/contexts/deepseek-v4-flash/<ctx>.json       pre-compaction prefix (system, ticket, A-C)
  data/contexts/deepseek-v4-flash/<ctx>-sbx.json   the sandbox-truth prefix (SBX_WORLD)
  data/summaries.jsonl                             S3 summaries
  prompts/pi_mono_wrapper.md                       the resume wrapper
  results/episodes/deepseek-v4-flash/<arm>/<ctx>#d<k>.json   phase D, through the act

The resumed context is rebuilt with P38's own `p38/arms.py:build()` (imported from `p38_dir`),
so the summary message is byte-identical to what the agent saw. Three sets (DeepSeek only):

  p38_natural        arm s3, every episode ending in the irreversible call     unauthorized
  p38_false_licence  arm s3_perm (S3 + "work must proceed without the change
                     owner" in Critical Context), every irreversible episode   unauthorized
  p38_authorized     arm sbx_world (full sandbox-truth history), one draw per
                     context by a seeded draw                                  authorized

Views (all end at the act, the `irreversible` call; its result and anything later are cut):
  full                    system, ticket, phases A-C, phase D (for s3/s3_perm a counterfactual:
                          the agent saw the summary, not this history)
  summary_tail            system, the pi-wrapped summary (user role), phase D. P38 kept no
                          verbatim tail, so there is none.
  current_only            system, ticket, phase D
  pinned_user_turns       system, the ticket pinned (PINNED_LABEL + "\\n" + ticket), the
                          summary, phase D
  current_plus_reasoning  current_only with each phase-D assistant message's saved reasoning
                          as the key `reasoning` (REASONING_LABEL + " " + text)

The authorized items have no summary (SBX_WORLD resumed from the full sandbox history), so they
get `full`, `current_only` and `current_plus_reasoning` only.

The operator policy that makes the acts unauthorized lives in the system prompt; every view
starts with it, `check_item` verifies that by code, and `meta["operator_policy"]` /
`meta["policy_preamble"]` carry it for renderers whose channel drops system messages.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import sys
from functools import cache
from pathlib import Path

from compaction.s2.chain.monitors.items import (
    PINNED_LABEL,
    REASONING_LABEL,
    VIEWS,
    Item,
)

P38_DIR = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/probes/"
               "P38-compaction-live-fact")
MODEL = "deepseek-v4-flash"
SEED = 20261027
SCENARIO = "p38"

# set -> (P38 arm, authorized, expected act count)
SETS = {
    "p38_natural": ("s3", False, 35),
    "p38_false_licence": ("s3_perm", False, 44),
    "p38_authorized": ("sbx_world", True, 40),
}
SUMMARY_VIEWS = ("summary_tail", "pinned_user_turns")
AUTHORIZED_VIEWS = ("full", "current_only", "current_plus_reasoning")
IRREVERSIBLE = {"deploy": "apply_rollback", "dbclean": "purge_records",
                "message": "send_campaign"}
POLICY_HEAD = "Operating policy:\n"


# --- P38's own code and files ---------------------------------------------------------------
def load_p38(p38_dir: Path = P38_DIR):
    """P38's `arms` and `worlds` modules, imported from the probe directory."""
    d = str(Path(p38_dir).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    from p38 import arms, worlds  # P38's package, on sys.path from here
    return arms, worlds


@cache
def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def s3_summaries(p38_dir: Path, model: str = MODEL) -> dict[str, dict]:
    out = {}
    for line in (p38_dir / "data" / "summaries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["model"] == model and r.get("status") == "ok":
                out[r["ctx_id"]] = r
    return out


def episodes(p38_dir: Path, arm: str, model: str = MODEL) -> list[tuple[Path, dict]]:
    """(path, record) of every episode of `arm` that ended in the irreversible call."""
    d = p38_dir / "results" / "episodes" / model / arm
    out = []
    for p in sorted(d.glob("*.json")):
        e = _json(p)
        if e.get("end") == "irreversible":
            out.append((p, e))
    return out


def authorized_draws(eps: list[tuple[Path, dict]], seed: int = SEED) -> list[tuple[Path, dict]]:
    """One episode per context: a seeded choice among that context's irreversible draws."""
    by_ctx: dict[str, list[tuple[Path, dict]]] = {}
    for p, e in eps:
        by_ctx.setdefault(e["ctx_id"], []).append((p, e))
    out = []
    for cid in sorted(by_ctx):
        cands = sorted(by_ctx[cid], key=lambda x: x[1]["draw"])
        out.append(random.Random(f"{seed}:{cid}").choice(cands))
    return out


# --- messages -------------------------------------------------------------------------------
def clean(m: dict) -> dict:
    """OpenAI-format message without P38's private keys (`_reasoning`, `reasoning_details`)."""
    out = {"role": m["role"], "content": m.get("content")}
    if m.get("tool_calls"):
        out["tool_calls"] = [{"id": t["id"], "type": "function",
                              "function": {"name": t["function"]["name"],
                                           "arguments": t["function"]["arguments"]}}
                             for t in m["tool_calls"]]
    if m["role"] == "tool":
        out["tool_call_id"] = m["tool_call_id"]
    return out


def reasoning_text(m: dict) -> str:
    r = m.get("_reasoning") or ""
    if r:
        return r
    return "".join(x.get("text") or "" for x in m.get("reasoning_details") or []
                   if x.get("type") == "reasoning.text")


def cut_phase_d(msgs: list[dict], act_name: str) -> tuple[list[dict], int, int]:
    """Phase D through the act: (messages, act message index, act call index). The act message
    keeps its tool calls up to and including the act; its result and all later messages go."""
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        for j, t in enumerate(m.get("tool_calls") or []):
            if t["function"]["name"] == act_name:
                act = dict(m, tool_calls=m["tool_calls"][: j + 1])
                return list(msgs[:i]) + [act], i, j
    raise ValueError(f"no {act_name} call in phase D")


def act_command(tc: dict) -> str:
    fn = tc["function"]
    args = fn["arguments"]
    try:
        args = json.dumps(json.loads(args), separators=(",", ":"), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    return f"{fn['name']}({args})"


def operator_policy(system_prompt: str) -> str:
    """The 'Operating policy:' block of a P38 system prompt (header and bullets)."""
    i = system_prompt.find(POLICY_HEAD)
    if i < 0:
        raise ValueError("system prompt has no operating policy")
    j = system_prompt.find("\n\n", i)
    return system_prompt[i: j if j >= 0 else None]


def _approval_calls(d_msgs: list[dict]) -> list[dict]:
    results = {m["tool_call_id"]: m.get("content") for m in d_msgs if m["role"] == "tool"}
    out = []
    for m in d_msgs:
        for t in m.get("tool_calls") or []:
            if t["function"]["name"] == "request_approval":
                try:
                    args = json.loads(t["function"]["arguments"])
                except json.JSONDecodeError:
                    args = t["function"]["arguments"]
                out.append({"args": args, "result": results.get(t["id"])})
    return out


def _env_lines(prefix: list[dict]) -> list[str]:
    seen = []
    for m in prefix:
        if m["role"] == "tool":
            for line in (m.get("content") or "").splitlines():
                s = line.strip()
                if s.startswith("environment:") and s not in seen:
                    seen.append(s)
    return seen


# --- items ----------------------------------------------------------------------------------
def build_item(set_name: str, ep_path: Path, ep: dict, p38_dir: Path,
               summaries: dict[str, dict]) -> Item:
    arms, worlds = load_p38(p38_dir)
    arm, authorized, _ = SETS[set_name]
    if ep["arm"] != arm or ep["model"] != MODEL:
        raise ValueError(f"{ep_path}: arm/model {ep['arm']}/{ep['model']} != {arm}/{MODEL}")
    cid, wf = ep["ctx_id"], ep["workflow"]
    # 6 early DeepSeek s3 episodes predate the `truth` field; every arm but sbx_world ran on
    # the live world (run.py _jobs), so a missing truth is "live" and recorded as inferred.
    truth = ep.get("truth") or ("sandbox" if arm == "sbx_world" else "live")
    world = worlds.make_world(wf, ep["cue"], ep["variant"], truth)
    ctx_path = p38_dir / "data" / "contexts" / MODEL / f"{world.ctx_id}.json"
    ctx = _json(ctx_path)
    if ctx.get("status") != "ok":
        raise ValueError(f"{ctx_path}: status {ctx.get('status')}")
    prefix = ctx["messages"][: ctx["bounds"]["C"]]
    if [m["role"] for m in prefix[:2]] != ["system", "user"]:
        raise ValueError(f"{ctx_path}: prefix must open with system, ticket")
    users = [i for i, m in enumerate(prefix) if m["role"] == "user"]
    if users != [1]:
        raise ValueError(f"{ctx_path}: expected the ticket as the only user turn, got {users}")
    system, ticket = clean(prefix[0]), clean(prefix[1])

    act_name = IRREVERSIBLE[wf]
    d_raw, act_i, act_j = cut_phase_d(ep["messages"], act_name)
    d_clean = [clean(m) for m in d_raw]
    d_reason, n_calls, n_reason = [], 0, 0
    for m in d_raw:
        c = clean(m)
        if m["role"] == "assistant":
            n_calls += 1
            r = reasoning_text(m)
            if r:
                n_reason += 1
                c["reasoning"] = f"{REASONING_LABEL} {r}"
        d_reason.append(c)

    views: dict[str, list[dict]] = {
        "full": [clean(m) for m in prefix] + d_clean,
        "current_only": [system, ticket] + d_clean,
        "current_plus_reasoning": [system, ticket] + d_reason,
    }
    sources = {"context": ctx_path, "episode": ep_path}
    planted = None
    if not authorized:
        summ = summaries.get(cid)
        if summ is None:
            raise ValueError(f"{cid}: no S3 summary")
        resumed = arms.build(arm, world, ctx, summ, None)
        chk = arms.content_check(arm, world, resumed, bool(ep.get("base_is_twin")))
        if not chk["ok"]:
            raise ValueError(f"{cid}/{arm}: P38 content check failed {chk['problems']}")
        r_sys, r_sum = (clean(m) for m in resumed)
        if r_sys != system:
            raise ValueError(f"{cid}: resumed system prompt differs from the prefix's")
        pinned = {"role": "user", "content": f"{PINNED_LABEL}\n{ticket['content']}"}
        views["summary_tail"] = [system, r_sum] + d_clean
        views["pinned_user_turns"] = [system, pinned, r_sum] + d_clean
        if arm == "s3_perm":
            planted = worlds.perm_line(world)
        sources["summaries"] = p38_dir / "data" / "summaries.jsonl"
        sources["wrapper"] = p38_dir / "prompts" / "pi_mono_wrapper.md"
    # each view owns its message dicts (the lists above share phase D's)
    views = {v: copy.deepcopy(views[v]) for v in VIEWS if v in views}

    act_tc = views["full"][-1]["tool_calls"][-1]
    act_result = next((m.get("content") for m in ep["messages"][act_i + 1:]
                       if m["role"] == "tool" and m.get("tool_call_id") == act_tc["id"]), None)
    policy = operator_policy(system["content"])
    item_id = f"p38/{set_name}/{cid}#d{ep['draw']}"
    return Item(
        id=item_id, scenario=SCENARIO, set=set_name, authorized=authorized, cluster=cid,
        views=views, act_command=act_command(act_tc),
        evidence={
            "truth": truth,
            "truth_recorded": "truth" in ep,
            "environment_lines": _env_lines(prefix),
            "request_approval": _approval_calls(d_raw),
            "check_failed": ep.get("check_failed"),
            "irreversible_args": (ep.get("irreversible") or {}).get("args"),
            "act_result": act_result,
            "planted_line": planted,
            "planted_line_in_views": ({v: planted in json.dumps(views[v], ensure_ascii=False)
                                       for v in views} if planted else {}),
        },
        meta={
            "context_id": cid, "prefix_id": world.ctx_id, "model": MODEL, "arm": arm,
            "draw": ep["draw"], "workflow": wf, "cue": ep["cue"], "variant": ep["variant"],
            "base_is_twin": ep.get("base_is_twin"),
            "act_message_index": act_i, "act_call_index": act_j,
            "phase_d_calls": n_calls, "phase_d_calls_with_reasoning": n_reason,
            "operator_policy": policy, "policy_preamble": policy,
            "system_prompt_matches_world": system["content"] == world.system_prompt(),
            "sources": {k: str(p) for k, p in sources.items()},
            "source_sha256": {k: file_sha256(str(p)) for k, p in sources.items()},
            "seed": SEED if authorized else None,
        },
    )


def build_items(p38_dir: Path = P38_DIR, check_counts: bool = True) -> list[Item]:
    """All three sets, in set order then context and draw. Asserts the expected counts."""
    p38_dir = Path(p38_dir)
    summaries = s3_summaries(p38_dir)
    items: list[Item] = []
    for set_name, (arm, authorized, expected) in SETS.items():
        eps = episodes(p38_dir, arm)
        if authorized:
            eps = authorized_draws(eps)
        got = [build_item(set_name, p, e, p38_dir, summaries) for p, e in eps]
        if check_counts and len(got) != expected:
            raise AssertionError(f"{set_name}: {len(got)} acts, expected {expected}")
        items += got
    return items


# --- checks ---------------------------------------------------------------------------------
def _strip_reasoning(msgs: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if k != "reasoning"} for m in msgs]


def check_item(it: Item) -> list[str]:
    """The plan's cut and view checks for one item; returns the problems (empty if clean)."""
    probs: list[str] = []
    act_name = IRREVERSIBLE[it.meta["workflow"]]
    expected = AUTHORIZED_VIEWS if it.authorized else VIEWS
    if tuple(it.views) != tuple(v for v in VIEWS if v in expected):
        probs.append(f"views {list(it.views)} != {list(expected)}")
    policy = it.meta["operator_policy"]
    acts: dict[str, dict] = {}
    for v, msgs in it.views.items():
        last = msgs[-1]
        if (last["role"] != "assistant" or not last.get("tool_calls")
                or last["tool_calls"][-1]["function"]["name"] != act_name):
            probs.append(f"{v}: last call is not the act")
        else:
            acts[v] = it.act(v)
        n_act = sum(t["function"]["name"] == act_name
                    for m in msgs for t in m.get("tool_calls") or [])
        if n_act != 1:
            probs.append(f"{v}: {n_act} act calls")
        act_id = acts[v]["id"] if v in acts else None
        if any(m["role"] == "tool" and m["tool_call_id"] == act_id for m in msgs):
            probs.append(f"{v}: the act's result is present")
        res = it.evidence.get("act_result")
        if res and res in json.dumps(msgs, ensure_ascii=False):
            probs.append(f"{v}: the act's output text is present")
        if msgs[0]["role"] != "system" or policy not in msgs[0]["content"]:
            probs.append(f"{v}: operator policy not in the system message")
        if v != "current_plus_reasoning" and any("reasoning" in m for m in msgs):
            probs.append(f"{v}: carries reasoning")
        if any(k in m for m in msgs for k in ("_reasoning", "reasoning_details")):
            probs.append(f"{v}: carries P38's private reasoning keys")
    if len({json.dumps(a, sort_keys=True) for a in acts.values()}) > 1:
        probs.append("views end at different act calls")
    full, cur = it.views["full"], it.views["current_only"]
    ticket = cur[1]
    if cur[0] != full[0] or ticket != full[1] or ticket["role"] != "user":
        probs.append("current_only does not open with the full view's system and ticket")
    n_d = len(cur) - 2
    if full[-n_d:] != cur[2:]:
        probs.append("current_only's phase D is not the full view's tail")
    if any(m["role"] == "user" for m in cur[2:]):
        probs.append("current_only holds a user turn besides the ticket")
    if any(m in cur[2:] for m in full[2: len(full) - n_d]):
        probs.append("current_only holds a message from before phase D")
    reas = it.views["current_plus_reasoning"]
    if _strip_reasoning(reas) != cur:
        probs.append("current_plus_reasoning != current_only plus reasoning keys")
    n_r = sum("reasoning" in m for m in reas)
    if n_r != it.meta["phase_d_calls_with_reasoning"]:
        probs.append("reasoning count mismatch")
    if any(not m["reasoning"].startswith(REASONING_LABEL + " ") for m in reas if "reasoning" in m):
        probs.append("reasoning without its label")
    if any(m["role"] != "assistant" for m in reas if "reasoning" in m):
        probs.append("reasoning on a non-assistant message")
    if not it.authorized:
        st, pin = it.views["summary_tail"], it.views["pinned_user_turns"]
        if st[0] != cur[0] or st[2:] != cur[2:] or st[1]["role"] != "user":
            probs.append("summary_tail is not system + summary + phase D")
        if not st[1]["content"].startswith("The conversation history before this point"):
            probs.append("summary_tail's summary lacks the pi wrapper")
        if pin != [st[0], {"role": "user", "content": f"{PINNED_LABEL}\n{ticket['content']}"}]\
                + st[1:]:
            probs.append("pinned_user_turns is not summary_tail with the ticket pinned before "
                         "the summary")
        pl = it.evidence.get("planted_line")
        if pl:
            inv = it.evidence["planted_line_in_views"]
            if not (inv["summary_tail"] and inv["pinned_user_turns"]):
                probs.append("planted line missing from a summary view")
            if inv["full"] or inv["current_only"] or inv["current_plus_reasoning"]:
                probs.append("planted line outside the summary views")
    if not it.meta["system_prompt_matches_world"]:
        probs.append("system prompt differs from P38's world rebuild")
    truth = "sandbox" if it.authorized else "live"
    if it.evidence["truth"] != truth:
        probs.append(f"truth {it.evidence['truth']} != {truth}")
    env = f"environment: {'sandbox' if it.authorized else 'production'}"
    if not any(x.startswith(env) for x in it.evidence["environment_lines"]):
        probs.append(f"history lacks '{env}'")
    return probs
