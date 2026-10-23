"""S2 step 0: route smoke and burst check (docs/sprint/specs/S2.md, "Pilot steps" 0; Q6).

Per pilot route, through the harness's own client (compaction.s2.model.OpenAICompatModel) and
tools, booked to stage SPRINT-S2 under one run-level cap (default $1):

    effort  each candidate reasoning setting on a short reasoning prompt: accepted or the
            provider's error, reasoning tokens, cost. The route's setting is frozen from it.
    cache   three agent-shaped calls on a ~10k-token prefix with the bash and ask_user tools
            (call 2 and 3 extend call 1): cached share, reasoning tokens, provider cost
            against the price table, tool call present.
    burst   4 parallel copies of call 1 after the cache is warm: every HTTP status seen,
            retries included (the 429 check that P49's Vertex route failed at scale).

    uv run python scripts/s2_smoke.py --go-from-jacob [--routes deepseek,glm] [--steps effort]
        [--cap-usd 1] [--out results/sprint/S2/route_smoke.json]

Writes one JSON record (appended per invocation under "runs"); keys come from the repo .env.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s2_run import load_env

from compaction.s2 import config as C
from compaction.s2 import loop as L
from compaction.s2.model import (
    ModelError,
    OpenAICompatModel,
    RunSpend,
    SprintSpend,
    table_usd,
)

CANDIDATES = {  # highest effort first; the first accepted one that reasons is the frozen pick
    "deepseek": [{"reasoning_effort": "max"}, {"reasoning_effort": "high"}, {}],
    "glm": [{"reasoning": {"enabled": True}}, {"reasoning": {"effort": "max"}},
            {"reasoning": {"effort": "high"}}, {}],
    "luna": [{"reasoning": {"effort": "xhigh"}}, {"reasoning": {"effort": "high"}}],
    "haiku": [{"reasoning": {"effort": "max"}}, {"reasoning": {"effort": "xhigh"}},
              {"reasoning": {"effort": "high"}}, {}],
}
EFFORT_PROMPT = ("A list holds the integers 1..60. Remove every multiple of 4, then every "
                 "remaining number whose digit sum is prime. How many numbers are left? "
                 "Reply with the number only.")
EFFORT_MAX_TOKENS = 12_000
CACHE_MAX_TOKENS = 4_000
BURST = 4


class LoggingClient:
    """Wraps an OpenAI client: records every attempt's HTTP status (retries included)."""

    def __init__(self, inner, log: list, lock: threading.Lock):
        self._inner, self._log, self._lock = inner, log, lock
        self.chat = self
        self.completions = self

    def create(self, **body):
        t0 = time.monotonic()
        try:
            r = self._inner.chat.completions.create(**body)
        except Exception as e:
            with self._lock:
                self._log.append({"status": getattr(e, "status_code", None),
                                  "error": f"{type(e).__name__}: {str(e)[:400]}",
                                  "s": round(time.monotonic() - t0, 2)})
            raise
        with self._lock:
            self._log.append({"status": 200, "s": round(time.monotonic() - t0, 2)})
        return r


def _model(route: C.Route, spend: RunSpend, log: list, lock: threading.Lock):
    m = OpenAICompatModel(route, spend)
    m.client = LoggingClient(m.client, log, lock)
    return m


def _usage(reply, route: C.Route) -> dict:
    u = reply.usage
    tab = table_usd(route, u.prompt_tokens, u.cached_tokens, u.completion_tokens)
    return {"prompt": u.prompt_tokens, "cached": u.cached_tokens,
            "cached_share": round(u.cached_tokens / u.prompt_tokens, 3) if u.prompt_tokens
            else None, "completion": u.completion_tokens, "reasoning": u.reasoning_tokens,
            "cost_usd": round(u.cost_usd, 6), "cost_source": u.cost_source,
            "table_usd": round(tab, 6),
            "provider_over_table": round(u.cost_usd / tab, 3) if tab and
            u.cost_source == "provider" else None,
            "finish": reply.finish_reason, "latency_s": round(reply.latency_s, 2),
            "attempts": reply.attempts, "model": reply.model}


def effort(route: C.Route, spend: RunSpend, repeat: int = 1) -> list[dict]:
    out = []
    for cand in [c for c in CANDIDATES[route.name] for _ in range(repeat)]:
        r = dataclasses.replace(route, reasoning=cand)
        log, lock = [], threading.Lock()
        m = _model(r, spend, log, lock)
        row = {"setting": cand}
        try:
            rep = m.complete([{"role": "user", "content": EFFORT_PROMPT}], None,
                             EFFORT_MAX_TOKENS, purpose=f"smoke-effort-{route.name}")
            row |= {"ok": True, **_usage(rep, r),
                    "answer": str(rep.message.get("content") or "")[:80],
                    "has_reasoning_text": bool(rep.message.get("reasoning")
                                               or rep.message.get("reasoning_content"))}
        except ModelError as e:
            row |= {"ok": False, "error": str(e)[:400], "status": e.status}
        row["attempts_log"] = log
        out.append(row)
        print(json.dumps({"route": route.name, "step": "effort", **row}, default=str),
              flush=True)
    return out


def _prefix() -> list[dict]:
    ctx = (C.REPO_ROOT / "src" / "compaction" / "s2" / "loop.py").read_text()[:20_000]
    task = ("You are working in a repository. Here is one of its files for context:\n\n"
            f"```python\n{ctx}\n```\n\nFirst, list the repository root with the bash tool "
            "(`ls`). Do nothing else in this step.")
    return [{"role": "system", "content": L.SYSTEM_PROMPT}, {"role": "user", "content": task}]


def cache(route: C.Route, spend: RunSpend) -> dict:
    log, lock = [], threading.Lock()
    m = _model(route, spend, log, lock)
    msgs = _prefix()
    calls = []
    for i in range(1, 4):
        rep = m.complete(list(msgs), L.TOOLS, CACHE_MAX_TOKENS,
                         purpose=f"smoke-cache-{route.name}:{i}")
        msg = L.normalize_assistant(rep.message, i)
        tcs = msg.get("tool_calls") or []
        calls.append({"call": i, **_usage(rep, route), "tool_calls": [
            tc["function"]["name"] for tc in tcs]})
        print(json.dumps({"route": route.name, "step": "cache", **calls[-1]}, default=str),
              flush=True)
        msgs.append(msg)
        if tcs:
            for tc in tcs:
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content":
                             L.observation_message(0, "README.md\nsrc\ntests\npyproject.toml\n",
                                                   False)})
        else:
            msgs.append({"role": "user", "content": "Use the bash tool."})
        if i == 2:
            msgs.append({"role": "user", "content": "Now run `cat pyproject.toml | head -5`."})
    return {"calls": calls, "attempts_log": log}


def burst(route: C.Route, spend: RunSpend) -> dict:
    log, lock = [], threading.Lock()
    msgs = _prefix()

    def one(k):
        m = _model(route, spend, log, lock)
        try:
            rep = m.complete(list(msgs), L.TOOLS, CACHE_MAX_TOKENS,
                             purpose=f"smoke-burst-{route.name}:{k}")
            return {"k": k, "ok": True, **_usage(rep, route)}
        except ModelError as e:
            return {"k": k, "ok": False, "error": str(e)[:300], "status": e.status}

    t0 = time.monotonic()
    with ThreadPoolExecutor(BURST) as ex:
        rows = list(ex.map(one, range(BURST)))
    statuses = [x["status"] for x in log]
    out = {"parallel": BURST, "rows": rows, "statuses": statuses,
           "n_429": statuses.count(429), "wall_s": round(time.monotonic() - t0, 2)}
    print(json.dumps({"route": route.name, "step": "burst", "statuses": statuses,
                      "ok": [r["ok"] for r in rows], "wall_s": out["wall_s"]}), flush=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--go-from-jacob", action="store_true")
    ap.add_argument("--routes", default=",".join(C.PILOT_MODELS))
    ap.add_argument("--steps", default="effort,cache,burst")
    ap.add_argument("--cap-usd", type=float, default=1.0)
    ap.add_argument("--repeat", type=int, default=1, help="calls per effort setting")
    ap.add_argument("--route-json", default=None,
                    help="JSON of Route fields to override (e.g. a provider_pin to test)")
    ap.add_argument("--settings", default=None,
                    help="JSON list of reasoning settings to try instead of CANDIDATES")
    ap.add_argument("--out", default=str(C.REPO_ROOT / "results" / "sprint" / "S2" /
                                         "route_smoke.json"))
    a = ap.parse_args(argv)
    if not a.go_from_jacob:
        print("refusing: paid calls need Jacob's go (pass --go-from-jacob)")
        return 1
    if not (0 < a.cap_usd < float("inf")):
        print("refusing: --cap-usd must be a positive finite amount (0 would mean no run cap)")
        return 1
    load_env()
    spend = RunSpend(SprintSpend(), a.cap_usd)
    steps = a.steps.split(",")
    run: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": steps,
                 "routes": {}}
    for name in a.routes.split(","):
        route = C.ROUTES[name]
        if a.route_json:
            route = dataclasses.replace(route, **json.loads(a.route_json))
        rec: dict = {"route": dataclasses.asdict(route)}
        for step in steps:
            try:
                if step == "effort":
                    if a.settings:
                        CANDIDATES[name] = json.loads(a.settings)
                    rec[step] = effort(route, spend, a.repeat)
                else:
                    rec[step] = {"cache": cache, "burst": burst}[step](route, spend)
            except Exception as e:  # noqa: BLE001 - recorded; the next route still runs
                rec[step] = {"error": f"{type(e).__name__}: {str(e)[:500]}"}
                print(json.dumps({"route": name, "step": step, "error": rec[step]["error"]}),
                      flush=True)
        run["routes"][name] = rec
    run["spent_usd"] = round(spend.spent, 6)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(out.read_text()) if out.exists() else {"runs": []}
    doc["runs"].append(run)
    out.write_text(json.dumps(doc, indent=1, default=str))
    print(f"spent ${spend.spent:.4f}; wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
