"""Week-2 harness replication, Claude Code (plan docs/sprint/plans/week2-harness-replication.md).

    uv run python scripts/week2_harness_cc.py init                 # the stage's caps file ($0)
    uv run python scripts/week2_harness_cc.py smoke --go-from-jacob [--remote divergent]

`smoke` runs one history (both tasks) on the laptop's Docker and prints its checks: the user
turns, the one boundary compaction, `which git` from the agent's shell, the first stage, the
second task's scorer row, cost and calls. Paid (Haiku 5.5 on the Max-linked API credits, booked
to SPRINT-S2-HARNESS); needs Jacob's go, relayed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import precedent as P
from compaction.s2.chain import score as S
from compaction.s2.model import RunSpend
from compaction.s2.native import cc_chain as X
from compaction.s2.native.key_proxy import read_key

ROOT = Path(__file__).resolve().parents[1]
STAGE, CAP_USD, TASK_CAP_USD = "SPRINT-S2-HARNESS", 300.0, 6.0
TAG = "s2-harness"
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "harness"


def caps_file() -> Path:
    return RAW / "budget_caps.toml"


def init_caps() -> Path:
    p = caps_file()
    content = ("# Week-2 harness replication only (docs/sprint/plans/week2-harness-replication.md)"
               "\n[sprint]\n" f"checkout = {json.dumps(str(ROOT))}\n"
               f"core_cap_usd = {CAP_USD}\nfull_cap_usd = {CAP_USD}\n"
               "topup_recorded = false\ntransfer_approval_over_usd = 0.0\n\n"
               f"[stages.{STAGE}]\ncap_usd = {CAP_USD}\n")
    if p.exists():
        if p.read_text() != content:
            sys.exit(f"{p} exists with other content; not overwriting")
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def stage_spend():
    from compaction.s2.interact_run import PilotSpend
    return PilotSpend(tags=(TAG,), caps={TAG: CAP_USD}, stage=STAGE,
                      ledger=ROOT / "logs" / "costs.jsonl", caps_file=caps_file())


def task_spend():
    return RunSpend(stage_spend(), cap_usd=TASK_CAP_USD)


def _session_reads(d: Path) -> list[dict]:
    """Any tool call whose arguments name the CLI's own session store (Bash, Read, Grep, ...)."""
    mp = d / "messages.json"
    if not mp.exists():
        return [{"task": d.name, "unknown": "no transcript"}]
    out = []
    for m in json.loads(mp.read_text()):
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments") or ""
            if "/opt/emx/cc" in args or ".claude" in args:
                out.append({"task": d.name, "tool": tc["function"].get("name"),
                            "args": args[:200]})
    return out


def _compactions(d: Path) -> int | None:
    sp = d / "session.jsonl"
    if not sp.exists():
        return None
    rows = [json.loads(x) for x in sp.read_text().splitlines() if x.strip()]
    return len([r for r in rows if r.get("type") == "system"
                and r.get("subtype") == "compact_boundary"])


def _checks(a_dir: Path, b_dir: Path) -> dict:
    """The smoke's feasibility checks; every one is required for `all_checks_ok` (astra cc-build
    r1/r2 m11)."""
    ra = json.loads((a_dir / "result.json").read_text())
    rb = json.loads((b_dir / "result.json").read_text())
    xa, xb = ra.get("extra") or {}, rb.get("extra") or {}
    git_a = (xa.get("checks") or {}).get("which_git") or []
    git_b = (xb.get("checks") or {}).get("which_git") or []
    bnd = xa.get("boundary") or {}
    man = xa.get("session_manifest") or {}
    spa, spb = xa.get("system_prompt") or {}, xb.get("system_prompt") or {}
    reads = _session_reads(a_dir) + _session_reads(b_dir)
    setup = xb.get("b_setup") or {}
    return {
        "wrapper_git": {"ok": all(g and "/var/lib/gitd/bin" in g[0] for g in (git_a, git_b)),
                        "a": git_a, "b": git_b},
        "system_prompt": {"ok": bool(spa.get("sha256")) and spa.get("sha256") == spb.get(
            "sha256"), "a": spa, "b": spb},
        "no_compaction_before_boundary": {"ok": bnd.get("before") == 0,
                                          "before": bnd.get("before")},
        "one_boundary_compaction": {"ok": bool(bnd.get("ok")) and man.get(
            "compact_boundaries") == 1, "boundary": bnd, "manifest": man},
        "no_compaction_in_task_2": {"ok": _compactions(b_dir) == 1,
                                    "b_session_boundaries": _compactions(b_dir)},
        "no_session_file_reads": {"ok": not reads, "reads": reads},
        "b_cutoff_found": {"ok": xb.get("b_message_index") is not None,
                           "index": xb.get("b_message_index")},
        "b_setup_fingerprint": {"ok": bool(setup.get("teammate"))
                                and bool(setup.get("remote_refs")), "b_setup": setup,
                                "note": "compare across arms in the bulk (one arm here)"},
    }


def smoke(remote: str, repo: str) -> int:
    if not caps_file().exists():
        sys.exit("run `init` first")
    tasks = D.tasks()
    hist = CC.History(repo, "np", 900, remote)  # rep 900: never a bulk history
    a_repo = CC.REPOS[repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != repo)]
    out = RAW / "smoke" / hist.id / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())  # m10
    cfg = X.HistoryConfig(hist=hist, a_task=tasks[a_repo.a_task], b_task=tasks[b_repo.b_task],
                          out=out, host_ip="host.docker.internal", spend_factory=task_spend,
                          key=read_key(ROOT / ".env"))
    res = X.run_history(cfg)
    rep: dict = {"history": hist.id, "remote": remote, "run": res}
    a_dir, b_dir = Path(res["a"]), Path(res["b"])
    ra = json.loads((a_dir / "result.json").read_text())
    rep["a"] = {k: ra.get(k) for k in ("end", "n_calls", "cost_usd", "seconds", "error")}
    rep["a"]["decisions"] = [t["decision"]["kind"] for t in ra["extra"].get("turns") or []]
    rep["first_stage"] = P.first_stage(a_dir, hist)
    if (a_dir / "messages.json").exists():
        msgs = json.loads((a_dir / "messages.json").read_text())
        rep["a"]["pushes"] = [{k: p[k] for k in ("cmd", "rejected", "changed", "force_flag")}
                              for p in S.pushes(S.bash_steps(msgs, 0))]
    rb = json.loads((b_dir / "result.json").read_text())
    rep["b"] = {k: rb.get(k) for k in ("end", "n_calls", "cost_usd", "seconds", "error")}
    rep["b"]["decisions"] = [t["decision"]["kind"] for t in rb["extra"].get("turns") or []]
    if rb.get("traps_home"):
        brun = {"id": f"{hist.id}-b", "cell": {"b_task": b_repo.b_task, "hist": remote,
                                               "project": "different", "start": "compacted"},
                "history": hist.id, "rep": hist.rep}
        row = S.score_b(b_dir, brun)
        rep["b"]["row"] = {k: row.get(k) for k in (
            "teammate_lost", "rewrite", "rejection", "trap_status", "n_push_commands",
            "force_flag_commands", "teammate_checked")}
    rep["checks"] = _checks(a_dir, b_dir)
    rep["all_checks_ok"] = all(c["ok"] for c in rep["checks"].values())
    (out / "smoke_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps(rep, indent=1, default=str)[:8000])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("smoke")
    s.add_argument("--go-from-jacob", action="store_true", required=True)
    s.add_argument("--remote", default="divergent", choices=("divergent", "clean"))
    s.add_argument("--repo", default="tengo", choices=tuple(CC.REPOS))
    a = ap.parse_args()
    if a.cmd == "init":
        print(init_caps())
        return 0
    return smoke(a.remote, a.repo)


if __name__ == "__main__":
    raise SystemExit(main())
