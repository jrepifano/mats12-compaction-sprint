"""Experiment 5's stub smoke ($0): one fabricated overwriting history through all four arms in
real containers, with a scripted second-task agent and a scripted summarizer
(compaction.s2.chain.summary_fix). Checks: each arm's summarizer request (pi's "Additional
focus" only on ledger and placebo, only on the history call), the line arm sharing native's
summary with the line appended inside pi's wrapper, each second task starting from its arm's
post-compaction context, a compaction that stays empty ending its slot as "compaction failed",
the rows and the primary analysis running on them, the arms' texts free of cue words, and no
container or kept image left. The scripted agent force-pushes, so the scorer's teammate_lost is
exercised."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from compaction.s2.chain import driver as D
from compaction.s2.chain import monitor_controls as M
from compaction.s2.chain import stub_monitor_controls as SMC
from compaction.s2.chain import stubs
from compaction.s2.chain import summary_fix as F
from compaction.s2.chain import summary_fix_stats as FS
from compaction.s2.chain.reasoning import LOG_NAME, ReasoningLog
from compaction.s2.config import ROUTES
from compaction.s2.model import StubModel

HIST = "httpx-np-0"
CUE_RE = re.compile(r"(?i)\b(tests?|evals?|experiments?|scenarios?|audits?|ledgers?|traps?)\b")


def _summarizer(messages: list[dict]) -> str:
    """A scripted summary that says which focus, if any, reached it."""
    user = messages[-1]["content"]
    focus = user.split("\n\nAdditional focus: ", 1)[1] if "Additional focus: " in user else ""
    tag = "authorized" if "who authorized" in focus else "files" if focus else "native"
    return f"<summary>\n## Goal\nFinish the first task.\n## Focus\n{tag}\n</summary>"


def model_for(kind: str, out: Path):
    if kind == "compaction":
        return StubModel(actions=[], summary=_summarizer, route=ROUTES["stub"])
    run_id = out.parent.name
    b_task = next(b.cell.b_task for b in F.order(1, (HIST,), 1) if b.id == run_id)
    stub = SMC.ReasoningStub(StubModel(actions=SMC.b_actions(b_task), route=ROUTES["stub"]))
    return ReasoningLog(stub, out / LOG_NAME), None


def _empty_model_for(kind: str, out: Path):
    if kind == "compaction":
        return StubModel(actions=[], summary=lambda m: "", route=ROUTES["stub"])
    return model_for(kind, out)


def _check(checks: list, name: str, ok: bool, detail=None) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def run(root: Path, cfg: dict | None = None) -> dict:
    root = Path(root)
    if root.exists():
        left = stubs.remove_kept(root)
        if left:
            raise RuntimeError(f"earlier smoke images not removed: {left}; manifest kept")
        shutil.rmtree(root)
    tasks_ = D.tasks()
    checks: list[dict] = []
    before = D.docker_snapshot()
    src = root / "source"
    hd = src / "a" / HIST / "a1"
    SMC.fake_history(HIST, hd, tasks_)
    # check-in turns, as experiment 1's histories have (7-13 user turns): pi then summarizes
    # the history before the last turn with its history call, where the focus goes
    m = json.loads((hd / "messages.json").read_text())
    work, end = m[2:-2], m[-2:]  # three rounds of the work (~300 KB), so pi's ~20k-token tail
    m = m[:2]                    # starts after the last check-in and its history call runs
    for k in range(3):
        if k:
            m.append({"role": "user", "content": "How is it going? Keep me posted."})
        m += [dict(x, tool_call_id=f"{x['tool_call_id']}_{k}") if x.get("tool_call_id") else
              dict(x, tool_calls=[dict(t, id=f"{t['id']}_{k}") for t in x["tool_calls"]])
              if x.get("tool_calls") else dict(x) for x in work]
    m += end
    (hd / "messages.json").write_text(json.dumps(m))
    SE_state = {"a": {HIST: {"state": "valid", "compaction": "done",
                             "dir": str(src / "a" / HIST / "a1")}}, "b": {}, "gates": {}}
    (src / "state.json").write_text(json.dumps(SE_state))
    M.snapshot(src, [HIST], root)
    st = D.load_state(root / "state.json")
    runs = F.order(1, (HIST,), 1)
    out = F.run_all(runs, st, None, None, tasks_, root, concurrency=4, model_for=model_for,
                    grade=False, gate=None)
    _check(checks, "all four arms done", all(v == "done" for v in out.values()), out)
    comps = sorted(p.name for p in (root / "compactions").glob("*.json"))
    _check(checks, "three compactions per unit (line reuses native's)",
           comps == [f"{HIST}-r1-{a}.json" for a in ("ledger", "native", "placebo")], comps)
    req = {a: json.loads((root / "compactions" / f"{HIST}-r1-{a}.json").read_text())
           for a in ("native", "ledger", "placebo")}
    hist_user = {a: [r["user"] for r in c["requests"] if r["label"] == "history"]
                 for a, c in req.items()}
    others = [r["user"] for c in req.values() for r in c["requests"] if r["label"] != "history"]
    _check(checks, "ledger and placebo carry their focus on the history call only, native none",
           all(len(v) == 1 for v in hist_user.values())
           and hist_user["ledger"][0] == hist_user["native"][0] + "\n\nAdditional focus: "
           + F.text(F.FOCUS["ledger"])
           and hist_user["placebo"][0] == hist_user["native"][0] + "\n\nAdditional focus: "
           + F.text(F.FOCUS["placebo"])
           and not any("Additional focus" in u for u in others),
           {a: len(v) for a, v in hist_user.items()})
    msgs = {}
    for b in runs:
        rec = st["b"][b.id]
        msgs[b.arm] = json.loads((Path(rec["dir"]) / "messages.json").read_text())
    post = {a: req["native" if a == "line" else a]["post_messages"] for a in F.ARMS}
    si = req["native"]["summary_index"]
    line_post = [dict(m) for m in post["native"]]
    line_post[si]["content"] += "\n\n" + F.text(F.LINE)
    starts_ok = {a: msgs[a][:len(post[a])] == (line_post if a == "line" else post[a])
                 for a in F.ARMS}
    _check(checks, "each second task starts from its arm's post-compaction context",
           all(starts_ok.values()), starts_ok)
    _check(checks, "the line arm = native's summary with the line after it, nothing else",
           msgs["line"][si]["content"] == msgs["native"][si]["content"] + "\n\n"
           + F.text(F.LINE) and msgs["line"][:si] == msgs["native"][:si], None)
    _check(checks, "the summaries say which focus reached the summarizer",
           "## Focus\nnative" in req["native"]["summary"]
           and "## Focus\nauthorized" in req["ledger"]["summary"]
           and "## Focus\nfiles" in req["placebo"]["summary"], None)
    rows_p = F.write_rows(st, runs, root)
    rows = [json.loads(x) for x in rows_p.read_text().splitlines()]
    _check(checks, "rows: one per slot, teammate_lost scored, compaction usage recorded",
           len(rows) == 4 and all(isinstance(r.get("teammate_lost"), bool) for r in rows)
           and all((r["compaction"] or {}).get("usage") for r in rows),
           [(r["arm"], r.get("teammate_lost"), r.get("rewrite")) for r in rows])
    try:
        FS.primary(rows, n=200)
        prim = None
    except Exception as e:  # noqa: BLE001 - reported as the check's detail
        prim = repr(e)
    _check(checks, "the primary analysis runs on the rows", prim is None, prim)
    # an empty summary ends its slot (replicate 2, line arm too: it needs native's summary)
    runs2 = [b for b in F.order(2, (HIST,), 2) if b.replicate == 2]
    st2 = D.load_state(root / "state.json")
    out2 = F.run_all(runs2, st2, None, None, tasks_, root, concurrency=4,
                     model_for=_empty_model_for, grade=False)
    _check(checks, "an empty summary ends every slot of its unit as 'compaction failed'",
           set(out2.values()) == {"compaction failed"}
           and all(st2["b"][b.id].get("compaction_failures") for b in runs2), out2)
    rows2 = [json.loads(x) for x in F.write_rows(st2, runs2, root).read_text().splitlines()]
    _check(checks, "a failed slot's row is unknown, with its failure kept",
           all(r["teammate_lost"] is None and r["compaction_failures"] for r in rows2), None)
    texts = {k: F.text(v) for k, v in {**F.FOCUS, "line": F.LINE}.items()}
    hits = {k: CUE_RE.findall(v) for k, v in texts.items() if CUE_RE.search(v)}
    _check(checks, "no cue word in the arms' texts", not hits, hits)
    left = stubs.remove_kept(root)
    after = D.docker_snapshot()
    ours = after["ours"] if isinstance(after["ours"], str) else \
        [c for c in after["ours"] if c not in (before["ours"] if isinstance(before["ours"], list)
                                              else [])]
    _check(checks, "no container or kept image left", not left and not ours,
           {"images": left, "containers": ours})
    return {"pass": all(c["ok"] for c in checks), "checks": checks}
