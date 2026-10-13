"""P38 arms (plan.md, Setup: Arms): a context + an arm -> the resumed messages for phase D, and
the per-episode content check (plan.md, Automatic rules: each arm's resumed context contains or
omits the live fact as intended).

- FULL: the whole prefix (system, ticket, phases A-C), as generated.
- FULL+SANDBOX: FULL + the operator note (contracts.SANDBOX_NOTE) as a user message.
- S1 / S3: [system, the wrapped summary as a user message], pi-mono's resume with nothing of the
  prefix kept (as P08). The system prompt (rule, goal pressure) is the prefix's, verbatim.
- B+LIVE / B+NEUTRAL / B+TEST: the base summary B with the canonical line appended to its
  Critical Context section. B is S3, or S3's -L twin when the twin rule applies (`base`).
- B_BASE: the -L twin itself (only when B is the twin).
"""
from __future__ import annotations

from p38 import contracts as C
from p38 import lexicon as L
from p38 import worlds as W


def wrap(summary: str) -> str:
    tpl = (C.PROMPTS_DIR / "pi_mono_wrapper.md").read_text(encoding="utf-8")
    if tpl.count("{summary}") != 1:
        raise ValueError("pi-mono wrapper needs exactly one {summary}")
    return tpl.replace("{summary}", summary)


def summary_text(arm: str, world: W.World, summ: dict, base: str) -> str:
    if arm == C.S1:
        return summ["S1"]["text"]
    if arm == C.S3:
        return summ["S3"]["text"]
    if arm == C.S3_MINUSP:
        if not summ.get("S3_minusp"):
            raise ValueError("no S3-P twin")
        return summ["S3_minusp"]
    if arm == C.S3_PERM:
        return L.insert_critical(summ["S3"]["text"], W.perm_line(world))
    if arm == C.S3_NEUTRAL:
        return L.insert_critical(summ["S3"]["text"],
                                 W.neutral_line(world, len(W.perm_line(world))))
    if arm == C.B_BASE:
        return base
    line = {C.B_LIVE: W.live_line, C.B_NEUTRAL: W.neutral_line, C.B_TEST: W.test_line}[arm](world)
    return L.insert_critical(base, line)


def build(arm: str, world: W.World, ctx: dict, summ: dict | None, base: str | None) -> list[dict]:
    pre = ctx["messages"][: ctx["bounds"]["C"]]
    if pre[0]["role"] != "system":
        raise ValueError(f"{ctx['ctx_id']}: prefix must start with the system message")
    if arm == C.FULL:
        return list(pre)
    if arm == C.FULL_SANDBOX:
        return list(pre) + [{"role": "user",
                             "content": C.SANDBOX_NOTE.format(system=world.system)}]
    if summ is None or (base is None and arm not in (C.S1, C.S3, C.S3_MINUSP, C.S3_PERM,
                                                       C.S3_NEUTRAL)):
        raise ValueError(f"{arm} needs summaries")
    return [{"role": "system", "content": pre[0]["content"]},
            {"role": "user", "content": wrap(summary_text(arm, world, summ, base))}]


def content_check(arm: str, world: W.World, msgs: list[dict], base_is_twin: bool) -> dict:
    """Code check that the resumed context is what the arm says."""
    probs: list[str] = []
    last = msgs[-1]["content"] if msgs else ""
    if arm == C.SBX_WORLD:
        tool_text = "\n".join(m.get("content") or "" for m in msgs if m["role"] == "tool")
        if "environment: sandbox" not in tool_text or "environment: production" in tool_text:
            probs.append("sandbox-world history must say sandbox, not production")
    elif arm in (C.FULL, C.FULL_SANDBOX):
        tool_text = "\n".join(m.get("content") or "" for m in msgs if m["role"] == "tool")
        if "environment: production" not in tool_text:
            probs.append("full history lacks the environment line")
        if arm == C.FULL_SANDBOX and "sandbox replica" not in last:
            probs.append("sandbox note missing")
    else:
        if [m["role"] for m in msgs] != ["system", "user"]:
            probs.append("summary arm must be [system, user]")
        if arm == C.B_LIVE and W.live_line(world) not in last:
            probs.append("live line missing")
        if arm == C.B_TEST and W.test_line(world) not in last:
            probs.append("test line missing")
        if arm == C.B_NEUTRAL and W.neutral_line(world) not in last:
            probs.append("neutral line missing")
        if arm == C.S3_PERM and W.perm_line(world) not in last:
            probs.append("permissive line missing")
        if arm == C.S3_NEUTRAL and W.neutral_line(world, len(W.perm_line(world))) not in last:
            probs.append("neutral line missing")
        rest = last.replace(W.test_line(world), "")
        if base_is_twin and arm in (C.B_BASE, C.B_NEUTRAL, C.B_TEST) and L.live_lenient_hits(rest):
            probs.append(f"twin arm has liveness hits {L.live_lenient_hits(rest)[:3]}")
    sys_text = msgs[0]["content"] if msgs else ""
    if "environment: production" in sys_text:
        probs.append("system prompt states the environment")
    return {"ok": not probs, "problems": probs}
