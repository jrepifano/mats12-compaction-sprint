"""P38 driver (plan.md). Every step is resumable: each output record is written as it completes
and skipped on rerun, so a step can be stopped and relaunched with a lower --conc.

  uv run python probes/P38-compaction-live-fact/run.py prefix    --model glm-5.3-flash [--smoke|--ids a,b] [--conc 8]
  uv run python probes/P38-compaction-live-fact/run.py summarize --model ... [--conc 8]
  uv run python probes/P38-compaction-live-fact/run.py twins                  # no API calls
  uv run python probes/P38-compaction-live-fact/run.py episodes  --model ... --arms full,full_sandbox [--draws 2] [--conc 16]
  uv run python probes/P38-compaction-live-fact/run.py forks     --model ... --arms ...

--conc bounds episodes (or contexts) in flight; API calls in flight are bounded process-wide
by EMX_JUDGE_CONCURRENCY (default 32).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parent
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import asyncio
import json
import time

from p38 import agent, api, arms, compact
from p38 import contracts as C
from p38 import lexicon as L
from p38 import worlds as W

CHUNK = 40          # episodes per spend record
BASE_RULE_JSON = C.DATA_DIR / "base_rule.json"


def _model(slug: str) -> C.Model:
    for m in C.MODELS.values():
        if m.slug.startswith(slug):
            return m
    raise SystemExit(f"unknown model {slug}")


def _worlds(a) -> list[W.World]:
    ws = W.all_worlds(getattr(a, "truth", "live"))
    if getattr(a, "smoke", False):
        ws = [w for w in ws if w.variant == 0]
    if getattr(a, "ids", None):
        want = set(a.ids.split(","))
        ws = [w for w in ws if w.ctx_id.removesuffix("-sbx") in want]
    return ws


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def ctx_path(model: C.Model, ctx_id: str) -> Path:
    return C.CONTEXTS_DIR / model.slug / f"{ctx_id}.json"


def load_ctx(model: C.Model, ctx_id: str) -> dict | None:
    p = ctx_path(model, ctx_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def _bounded(items, conc: int, fn):
    sem = asyncio.Semaphore(conc)

    async def one(x):
        async with sem:
            return await fn(x)

    return await asyncio.gather(*(one(x) for x in items))


# --- prefix ----------------------------------------------------------------------------------------
async def do_prefix(a) -> int:
    model = _model(a.model)
    todo = [w for w in _worlds(a) if not ctx_path(model, w.ctx_id).exists()]
    worst = 3 * C.PHASE_TURN_CAP * api.list_cost(model, 30000, C.AGENT_MAX_TOKENS)
    print(f"{len(todo)} prefixes to generate; worst case ${worst * len(todo):.2f}", flush=True)
    if not todo:
        return 0
    async with api.Batch(f"prefix:{model.slug}", projected_usd=worst * len(todo)) as b:
        async def one(w):
            rec = await agent.run_prefix(model, w, on_reply=b.add)
            if rec["status"] == "error":
                print(f"API error, not saved: {w.ctx_id}: {rec['reason'][:200]}", flush=True)
                return rec
            _write_json(ctx_path(model, w.ctx_id), rec)
            print(f"{rec['status']:8s} {w.ctx_id} turns={len(rec['api_calls'])} "
                  f"${rec['usd']:.4f} {rec['reason']}", flush=True)
            return rec

        recs = await _bounded(todo, a.conc, one)
    n = {s: sum(r["status"] == s for r in recs) for s in ("ok", "dropped", "error")}
    print(f"done: {n}", flush=True)
    return 0


# --- summaries --------------------------------------------------------------------------------------
async def do_summarize(a) -> int:
    model = _model(a.model)
    have = {(r["model"], r["ctx_id"]) for r in _jsonl(C.SUMMARIES_JSONL) if r["status"] == "ok"}
    ctxs = [c for c in (load_ctx(model, w.ctx_id) for w in _worlds(a))
            if c and c["status"] == "ok" and (model.slug, c["ctx_id"]) not in have]
    proj = sum(compact.worst_case_usd(model, c) for c in ctxs)
    print(f"{len(ctxs)} contexts to summarize; worst case ${proj:.2f}", flush=True)
    if not ctxs:
        return 0
    async with api.Batch(f"summaries:{model.slug}", projected_usd=proj) as b:
        async def one(c):
            rec = await compact.summarize_context(model, c, on_reply=b.add)
            _append(C.SUMMARIES_JSONL, rec)
            print(f"{rec['status']:7s} {c['ctx_id']} ${rec['usd']:.4f}", flush=True)

        await _bounded(ctxs, a.conc, one)
    return 0


def summaries_by_key() -> dict[tuple[str, str], dict]:
    out = {}
    for r in _jsonl(C.SUMMARIES_JSONL):
        if r["status"] == "ok":
            out[(r["model"], r["ctx_id"])] = r
    return out


# --- twins and the base rule ------------------------------------------------------------------------
def do_twins(a) -> int:
    """-L twins of S3 (code deletion + gate), S1/S3 retention, and the base rule per model."""
    summ = summaries_by_key()
    rows, rule = [], {}
    for (slug, cid), r in sorted(summ.items()):
        s3 = r["S3"]["text"]
        twin = L.code_delete(s3)
        g = L.twin_gate(s3, twin, C.MAX_REMOVED_FRACTION)
        rows.append({"model": slug, "ctx_id": cid, "twin": twin, "gate": g,
                     "ret_S1": L.retention(r["S1"]["text"]), "ret_S3": L.retention(s3),
                     "ret_S3_steps": [L.retention(s["text"]) for s in r["S3_steps"]]})
    C.TWINS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    C.TWINS_JSONL.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                             encoding="utf-8")
    for slug in sorted({x["model"] for x in rows}):
        xs = [x for x in rows if x["model"] == slug]
        ret = sum(x["ret_S3"]["lenient"] for x in xs) / len(xs)
        n_ok = sum(x["gate"]["ok"] for x in xs)
        rule[slug] = {"n": len(xs), "S3_lenient": round(ret, 4),
                      "S3_strict": round(sum(x["ret_S3"]["strict"] for x in xs) / len(xs), 4),
                      "S1_lenient": round(sum(x["ret_S1"]["lenient"] for x in xs) / len(xs), 4),
                      "S1_strict": round(sum(x["ret_S1"]["strict"] for x in xs) / len(xs), 4),
                      "twin_gate_ok": n_ok,
                      "base": "twin" if ret >= C.RETENTION_TWIN_THRESHOLD else "s3"}
    if not a.no_rule:
        _write_json(BASE_RULE_JSON, rule)
    print(json.dumps(rule, indent=1))
    return 0


def do_fu2twins(a) -> int:
    """Follow-up 2: S3-P twins from the blind sentence labels (data/fu2/labels_*.json over
    data/fu2/units.json); deletion-only gate as the -L twin."""
    units = {u["uid"]: u for u in json.loads((C.FU2_DIR / "units.json").read_text())}
    perm, restr = set(), set()
    for f in sorted(C.FU2_DIR.glob("labels_*.json")):
        lab = json.loads(f.read_text())
        perm |= set(lab["permissive"])
        restr |= set(lab["restrictive"])
    unknown = (perm | restr) - set(units)
    if unknown:
        raise SystemExit(f"labels with unknown ids: {sorted(unknown)[:10]}")
    summ = summaries_by_key()
    rows = []
    for (slug, cid), r in sorted(summ.items()):
        s3 = r["S3"]["text"]
        mine = [units[u] for u in sorted(perm) if units[u]["model"] == slug
                and units[u]["ctx_id"] == cid]
        n_restr = sum(1 for u in restr if units[u]["model"] == slug and units[u]["ctx_id"] == cid)
        twin = L.delete_units(s3, mine)
        g = {"ok": not L.added_tokens(s3, twin)
             and L.removed_fraction(s3, twin) <= C.MAX_REMOVED_FRACTION
             and not any(u["text"] in twin for u in mine),
             "removed_fraction": round(L.removed_fraction(s3, twin), 4)}
        rows.append({"model": slug, "ctx_id": cid, "n_permissive": len(mine),
                     "n_restrictive": n_restr, "permissive": [u["text"] for u in mine],
                     "twin": twin, "gate": g})
    C.MINUSP_JSONL.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
    for slug in sorted({x["model"] for x in rows}):
        xs = [x for x in rows if x["model"] == slug]
        print(slug, "contexts", len(xs), "with permissive", sum(x["n_permissive"] > 0 for x in xs),
              "gate ok among them", sum(x["gate"]["ok"] for x in xs if x["n_permissive"] > 0))
    return 0


def twins_by_key() -> dict[tuple[str, str], dict]:
    return {(x["model"], x["ctx_id"]): x for x in _jsonl(C.TWINS_JSONL)}


def base_for(model: C.Model, cid: str, summ: dict, twins: dict) -> tuple[str | None, bool]:
    """(base summary text, is_twin) or (None, False) if the context has no usable base."""
    rule = json.loads(BASE_RULE_JSON.read_text())[model.slug]["base"]
    if rule == "s3":
        return summ["S3"]["text"], False
    t = twins.get((model.slug, cid))
    if t is None or not t["gate"]["ok"]:
        return None, True
    return t["twin"], True


# --- episodes ---------------------------------------------------------------------------------------
def ep_path(model: C.Model, arm: str, cid: str, draw: int) -> Path:
    return C.EPISODES_DIR / model.slug / arm / f"{cid}#d{draw}.json"


def _minusp() -> dict[tuple[str, str], str]:
    """Follow-up 2's S3-P twins that passed the gate (only contexts with a PERMISSIVE sentence)."""
    return {(r["model"], r["ctx_id"]): r["twin"] for r in _jsonl(C.MINUSP_JSONL)
            if r["gate"]["ok"] and r["n_permissive"] > 0}


def _jobs(a, model):
    summ, twins, minusp = summaries_by_key(), twins_by_key(), _minusp()
    jobs, skipped = [], []
    for w in _worlds(a):
        ctx = load_ctx(model, w.ctx_id)
        if not ctx or ctx["status"] != "ok":
            continue
        s = summ.get((model.slug, w.ctx_id))
        if s is not None and (model.slug, w.ctx_id) in minusp:
            s = {**s, "S3_minusp": minusp[(model.slug, w.ctx_id)]}
        needs_summary = any(arm not in (C.FULL, C.FULL_SANDBOX, C.SBX_WORLD) for arm in a.arms)
        if needs_summary and s is None:
            skipped.append((w.ctx_id, "no summary"))
            continue
        base, is_twin = (base_for(model, w.ctx_id, s, twins) if s else (None, False))
        for arm_name in a.arms:
            arm = C.base_arm(arm_name)
            if arm == C.SBX_WORLD:
                ws = W.make_world(w.workflow, w.cue, w.variant, "sandbox")
                sctx = load_ctx(model, ws.ctx_id)
                if not sctx or sctx["status"] != "ok":
                    skipped.append((w.ctx_id, "sbx_world: no sandbox prefix"))
                    continue
                msgs = arms.build(C.FULL, ws, sctx, None, None)
                chk = arms.content_check(C.SBX_WORLD, ws, msgs, False)
                if not chk["ok"]:
                    skipped.append((w.ctx_id, f"{arm}: {chk['problems']}"))
                    continue
                jobs += [(ws, arm, d, msgs, False) for d in range(a.draws)]
                continue
            if arm in (C.B_LIVE, C.B_NEUTRAL, C.B_TEST, C.B_BASE) and base is None:
                skipped.append((w.ctx_id, f"{arm}: twin failed gate"))
                continue
            if arm == C.B_BASE and not is_twin:
                continue
            if arm == C.S3_MINUSP and "S3_minusp" not in (s or {}):
                continue
            msgs = arms.build(arm, w, ctx, s, base)
            chk = arms.content_check(arm, w, msgs, is_twin)
            if not chk["ok"]:
                skipped.append((w.ctx_id, f"{arm}: {chk['problems']}"))
                continue
            for d in range(a.draws):
                jobs.append((w, arm_name, d, msgs, is_twin))
    return jobs, skipped


async def do_episodes(a) -> int:
    model = _model(a.model)
    a.arms = a.arms.split(",")
    jobs, skipped = _jobs(a, model)
    for s in skipped:
        print("skip", s, flush=True)
    todo = [j for j in jobs
            if not ep_path(model, j[1], j[0].ctx_id.removesuffix("-sbx"), j[2]).exists()]
    print(f"{len(jobs)} episodes, {len(todo)} to run", flush=True)
    n_err = 0
    for k in range(0, len(todo), CHUNK):
        chunk = todo[k:k + CHUNK]
        proj = sum(C.RESUME_TURN_CAP * api.list_cost(
            model, api.request_bound_tokens(j[3]) // 3 + 20000, C.AGENT_MAX_TOKENS) for j in chunk)
        async with api.Batch(f"episodes:{model.slug}", projected_usd=proj) as b:
            async def one(j):
                nonlocal n_err
                w0, arm, d, msgs, is_twin = j
                w = W.make_world(w0.workflow, w0.cue, w0.variant, w0.truth)
                w.no_recheck = arm.endswith(C.NR)
                t0 = time.time()
                out = await agent.run_decision(model, w, msgs, on_reply=b.add)
                cid = w.ctx_id.removesuffix("-sbx")
                rec = {"model": model.slug, "ctx_id": cid, "truth": w.truth, "workflow": w.workflow,
                       "cue": w.cue, "variant": w.variant, "arm": arm, "draw": d,
                       "base_is_twin": is_twin, "wall_s": round(time.time() - t0, 1), **out}
                if out["end"] == "error":
                    n_err += 1
                    print(f"API error, not saved: {w.ctx_id} {arm} d{d}", flush=True)
                    return
                _write_json(ep_path(model, arm, cid, d), rec)
                print(f"{arm:13s} {cid:20s} d{d} end={out['end']:12s} "
                      f"turns={out['turns']} ${out['usd']:.4f}", flush=True)

            await _bounded(chunk, a.conc, one)
    print(f"done; API errors {n_err}", flush=True)
    return 1 if n_err else 0


# --- forked question --------------------------------------------------------------------------------
def parse_fork(text: str) -> str:
    import re

    m = re.search(r"\b(LIVE|TEST|UNSURE)\b", (text or "")[:200].upper())
    return m.group(1) if m else "UNPARSED"


async def do_forks(a) -> int:
    model = _model(a.model)
    a.arms = a.arms.split(",")
    a.draws = 1
    jobs, _ = _jobs(a, model)
    have = {(r["model"], r["ctx_id"], r["arm"]) for r in _jsonl(C.FORKS_JSONL)}
    todo = [j for j in jobs
            if (model.slug, j[0].ctx_id.removesuffix("-sbx"), j[1]) not in have]
    proj = sum(api.list_cost(model, api.request_bound_tokens(j[3]) // 3 + 5000,
                             C.AGENT_MAX_TOKENS) for j in todo)
    print(f"{len(todo)} forks; worst case ${proj:.2f}", flush=True)
    if not todo:
        return 0
    async with api.Batch(f"forks:{model.slug}", projected_usd=proj) as b:
        async def one(j):
            w0, arm, _, msgs, _ = j
            w = W.make_world(w0.workflow, w0.cue, w0.variant, w0.truth)
            w.enter_decision()
            r = await api.chat(model, msgs + [{"role": "user", "content": C.FORK_QUESTION}],
                               max_tokens=C.AGENT_MAX_TOKENS, tools=w.tools(),
                               tool_choice="none")
            b.add(r)
            if r.error:
                print(f"API error {w.ctx_id} {arm}", flush=True)
                return
            _append(C.FORKS_JSONL, {"model": model.slug, "ctx_id": w.ctx_id.removesuffix("-sbx"),
                                    "arm": arm,
                                    "workflow": w.workflow, "cue": w.cue, "answer":
                                    parse_fork(r.text), "text": r.text,
                                    "reasoning": r.reasoning, "usd": api.charge(r)})

        await _bounded(todo, a.conc, one)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prefix", "summarize", "episodes", "forks"):
        p = sub.add_parser(name)
        p.add_argument("--model", required=True)
        p.add_argument("--ids")
        p.add_argument("--smoke", action="store_true")
        p.add_argument("--conc", type=int, default=8)
        if name == "prefix":
            p.add_argument("--truth", default="live", choices=("live", "sandbox"))
        if name in ("episodes", "forks"):
            p.add_argument("--arms", required=True)
            p.add_argument("--draws", type=int, default=C.DRAWS)
    sub.add_parser("fu2twins")
    t = sub.add_parser("twins")
    t.add_argument("--no-rule", action="store_true",
                   help="don't (re)write the base rule (once episodes have run on it)")
    a = ap.parse_args(argv)
    if a.cmd == "twins":
        return do_twins(a)
    if a.cmd == "fu2twins":
        return do_fu2twins(a)
    api.load_key()
    fn = {"prefix": do_prefix, "summarize": do_summarize, "episodes": do_episodes,
          "forks": do_forks}[a.cmd]
    return asyncio.run(fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
