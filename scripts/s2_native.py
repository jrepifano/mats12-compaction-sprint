"""Native-harness arms of the accumulated-loss experiment (src/compaction/s2/native/; brief
logs/s2/briefs/native-arms.md).

Run under the MCP server's dependency, which is not in the project:

    uv run --with fastmcp==4.1.3 python scripts/s2_native.py <command> ...

Commands:
- egress-check [--task T] [--arm claude_code|agy|codex]   ($0) start a session on the desktop,
  prove the container can't reach the internet but reaches its vendor API, tear down.
- cc-base --task T --window N [--stop-after K] [--sim-cap USD] (--smoke | --go-from-jacob)
  One Claude Code base run (Haiku 5.5 on Max; the simulator is paid, GPT-5.5 via OpenRouter,
  booked to SPRINT-S3-Q2 / s3q2-interact-pilot). --smoke: a run key starting "smoke-<utc>-",
  the simulator capped at $2, and a stop after the first boundary unless --stop-after says
  otherwise. Anything else is a bulk run and needs --go-from-jacob.
- agy-base --task T [--compact-at N] [--stop-after K] [--sim-cap USD] (--smoke | --go-from-jacob)
  One Antigravity base run (Gemini 3.8 Flash on Jacob's free quota; the token store is the
  emx-native-agy-auth volume on the desktop), with a forced /compact at the first turn
  boundary past N tokens (default 150k). Same --smoke rules as cc-base.
- cc-fork BDIR (--summary-file F | --append TEXT) [--prompt P] [--out DIR] (--smoke |
  --go-from-jacob)   resume boundary BDIR from its kept image with an edited summary.
  --probe-word W: append "FORK-PROBE: <W>" to the summary and ask the fork to repeat it
  without tools (the fork round-trip check); prints whether it did.

Auth: CLAUDE_CODE_OAUTH_TOKEN from .env (`claude setup-token`), passed into the container only
as `docker exec -e`. Docker host: --host (default ssh://rig, the desktop).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import interact_run as IR
from compaction.s2.model import OpenAICompatModel, RunSpend
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N

SMOKE_SIM_CAP_USD = 2.0
SMOKE_TASK = "kombu-single-active-consumer-priority"


def _env() -> None:
    from dotenv import load_dotenv
    load_dotenv(C.REPO_ROOT / ".env", override=False)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sim_model(root: Path, name: str, cap_usd: float) -> OpenAICompatModel:
    spend = RunSpend(IR.PilotSpend(), cap_usd).attach(IR.spend_path(root, name))
    return OpenAICompatModel(C.SIM_ROUTE, spend)


def _token() -> str:
    tok = os.environ.get(CC.TOKEN_ENV, "").strip()
    if not tok:
        sys.exit(f"{CC.TOKEN_ENV} is not set (.env); run `claude setup-token`")
    return tok


def _paid(a) -> None:
    if not (a.smoke or a.go_from_jacob):
        sys.exit("paid command: pass --smoke (at most 2 per arm) or --go-from-jacob")


def cmd_egress_check(a) -> int:
    task = IR.load_task(a.task)

    class _NoSim:
        def __init__(self, runner):
            from compaction.s2.interact import SimState
            self.state = SimState()

        def ask(self, q):
            raise RuntimeError("no simulator in an egress check")

        def export(self):
            return {"state": {"ask_user_call_count": 0}}

    url = {"claude_code": "https://api.anthropic.com/v1/messages",
           "codex": "https://chatgpt.com/", "agy": "https://cloudcode-pa.googleapis.com/"}[a.arm]
    with N.NativeSession(a.arm, task.info.image, _NoSim, host=a.host, lifetime_s=1200) as ns:
        out = N.egress_check(ns.sb, ns.egress, url)
        out.update(arm=a.arm, image=task.info.image, network=ns.egress.network,
                   container=ns.sb.name)
    out["cleanup_unconfirmed"] = ns.cleanup_unconfirmed
    path = Path(a.out or C.raw_dir() / "native" / "egress_checks" / f"{a.arm}-{_stamp()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps({"ok": out["ok"], "file": str(path),
                      "checks": [(c["url"], c["via_proxy"], c["pass"]) for c in out["checks"]],
                      "cleanup_unconfirmed": ns.cleanup_unconfirmed}, indent=1))
    return 0 if out["ok"] and not ns.cleanup_unconfirmed else 1


def cmd_cc_base(a) -> int:
    _paid(a)
    _env()
    token = _token()
    root = Path(a.root) if a.root else CC.root()
    prefix = f"smoke-{_stamp()}-" if a.smoke else ""
    cap = min(a.sim_cap, SMOKE_SIM_CAP_USD) if a.smoke else a.sim_cap
    task = IR.load_task(a.task)
    cfg = CC.BaseConfig(task=task, root=root, prefix=prefix,
                        attempt=CC.next_attempt(root, task.task_id, prefix),
                        cc=CC.CCConfig(model=a.model, effort=a.effort or None,
                                       auto_compact_window=a.window),
                        host=a.host, sim_cap_usd=cap,
                        stop_after=a.stop_after if a.stop_after is not None
                        else (1 if a.smoke else None))
    sim = _sim_model(root, f"{cfg.run_key}-base", cap)
    res = CC.run_base(cfg, sim_model=sim, token=token)
    print(json.dumps({k: res.get(k) for k in ("run_key", "end", "boundaries", "sim_cost_usd",
                                              "seconds", "error", "cleanup_unconfirmed")},
                     indent=1)[:4000])
    print("run dir:", cfg.run_dir)
    ok = res["end"] in ("submitted", "stop_after", "eligible")
    return 0 if ok and not res.get("cleanup_unconfirmed") else 1


def cmd_cc_fork(a) -> int:
    _paid(a)
    _env()
    token = _token()
    bdir = Path(a.bdir)
    rec = json.loads((bdir / "boundary" / "compaction.json").read_text())
    summary = rec["summary"]
    prompt = a.prompt or CC.CONTINUE_PROMPT
    body = json.loads((bdir / "native" / "hook_payload.json").read_text()).get(
        "compact_summary") or ""
    if a.probe_word:
        summary = CC.insert_in_summary(summary, body, f"FORK-PROBE: {a.probe_word}")
        prompt = a.prompt or ("Without calling any tools: quote, exactly, the line of the "
                              "summary above that starts with 'FORK-PROBE:'.")
    elif a.summary_file:
        summary = Path(a.summary_file).read_text()
    elif a.append:  # inside the summary body, before Claude Code's closing paragraphs
        summary = CC.insert_in_summary(summary, body, a.append)
    else:
        sys.exit("pass --summary-file, --append or --probe-word")
    rj = json.loads((bdir.parent / "run.json").read_text())
    task = IR.load_task(rj["task"]["task_id"])
    out = Path(a.out or bdir.parent / "forks" / f"{bdir.name}-{_stamp()}")
    cap = SMOKE_SIM_CAP_USD if a.smoke else a.sim_cap
    sim = _sim_model(bdir.parent.parent, f"{rj['run_key']}-{bdir.name}-fork-{_stamp()}", cap)
    res = CC.fork(bdir, out, new_summary=summary, prompt=prompt, sim_model=sim, token=token,
                  persona=task.persona, host=a.host, timeout_s=a.timeout)
    text = CC.final_text(out / "stream.jsonl") if (out / "stream.jsonl").exists() else ""
    rep = {k: res.get(k) for k in ("outcome", "rc", "forked_session", "input_session",
                                   "image", "fork_check", "auth_error", "cleanup_unconfirmed")}
    rep["final_text"] = text[:1000]
    if a.probe_word:
        rep["probe_word_seen"] = a.probe_word in text
    print(json.dumps(rep, indent=1))
    print("fork dir:", out)
    ok = res.get("outcome") == "ok" and not res.get("cleanup_unconfirmed")
    return 0 if ok and (not a.probe_word or rep["probe_word_seen"]) else 1


def cmd_agy_base(a) -> int:
    _paid(a)
    _env()
    from compaction.s2.native import agy as A
    root = Path(a.root) if a.root else A.root()
    prefix = f"smoke-{_stamp()}-" if a.smoke else ""
    cap = min(a.sim_cap, SMOKE_SIM_CAP_USD) if a.smoke else a.sim_cap
    task = IR.load_task(a.task)
    cfg = A.BaseConfig(task=task, root=root, prefix=prefix,
                       attempt=A.next_attempt(root, task.task_id, prefix),
                       agy=A.AgyConfig(model=a.model, compact_at=a.compact_at,
                                       effort=a.effort or None),
                       host=a.host, sim_cap_usd=cap,
                       stop_after=a.stop_after if a.stop_after is not None
                       else (1 if a.smoke else None))
    sim = _sim_model(root, f"{cfg.run_key}-base", cap)
    res = A.run_base(cfg, sim_model=sim)
    print(json.dumps({k: res.get(k) for k in ("run_key", "end", "boundaries", "sim_cost_usd",
                                              "seconds", "error", "cleanup_unconfirmed")},
                     indent=1)[:4000])
    print("run dir:", cfg.run_dir)
    ok = res["end"] in ("submitted", "stop_after", "eligible")
    return 0 if ok and not res.get("cleanup_unconfirmed") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="ssh://rig")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("egress-check")
    e.add_argument("--task", default=SMOKE_TASK)
    e.add_argument("--arm", default="claude_code", choices=sorted(N.ALLOW))
    e.add_argument("--out")
    b = sub.add_parser("cc-base")
    b.add_argument("--task", default=SMOKE_TASK)
    b.add_argument("--window", type=int, required=True,
                   help="CLAUDE_CODE_AUTO_COMPACT_WINDOW (tokens)")
    b.add_argument("--model", default=CC.MODEL)
    b.add_argument("--effort", default=CC.EFFORT)
    b.add_argument("--stop-after", type=int)
    b.add_argument("--sim-cap", type=float, default=C.INTERACT_BASE_CAP_USD)
    b.add_argument("--root")
    f = sub.add_parser("cc-fork")
    f.add_argument("bdir")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--summary-file")
    g.add_argument("--append")
    g.add_argument("--probe-word")
    f.add_argument("--prompt")
    f.add_argument("--out")
    f.add_argument("--timeout", type=int, default=1800)
    f.add_argument("--sim-cap", type=float, default=C.INTERACT_SECONDARY_CAP_USD)
    g2 = sub.add_parser("agy-base")
    g2.add_argument("--task", default=SMOKE_TASK)
    g2.add_argument("--compact-at", type=int, default=150_000,
                    help="force /compact at the first turn boundary past this many tokens")
    g2.add_argument("--model", default="gemini-3.8-flash-low")
    g2.add_argument("--effort")
    g2.add_argument("--stop-after", type=int)
    g2.add_argument("--sim-cap", type=float, default=C.INTERACT_BASE_CAP_USD)
    g2.add_argument("--root")
    for sp in (b, f, g2):
        sp.add_argument("--smoke", action="store_true")
        sp.add_argument("--go-from-jacob", action="store_true")
    a = p.parse_args(argv)
    return {"egress-check": cmd_egress_check, "cc-base": cmd_cc_base,
            "cc-fork": cmd_cc_fork, "agy-base": cmd_agy_base}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
