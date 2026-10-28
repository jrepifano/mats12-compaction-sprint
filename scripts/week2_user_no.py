"""Experiment 3c ("user_no"): the reply phase and its rubric (src/compaction/s2/chain/user_no.py).
Laptop only. Everything after the rubric is 3b's pipeline with 3c's config:

    C="--config configs/week2/user_no.toml"; O="--out data/raw/sprint/s2/user_no"
    uv run python scripts/week2_user_no.py sources $O
        The 9 sources' rows (first task, rejected-push checkpoint, branch, foreign tip) from the
        local copy of experiment 1's raw dir; each checkpoint image must be on this host. Writes
        <out>/sources.json and <out>/reply_card.md (the card the paid reply step checks).
    uv run python scripts/week2_user_no.py reply $O [--histories h1,h2] --go-from-jacob
        Paid: per history, restore the checkpoint, add the user turn, run the reply until its
        first decision message (cut before its commands run) or the call cap. <out>/replies/<h>/.
    uv run python scripts/week2_user_no.py rubric $O
        Candidates' virtual first tasks (<out>/virtual/<h>/messages.json) and the proposed
        rubric (<out>/rubric_proposed.json); prints each reply for the confirming read.
    uv run python scripts/week2_user_no.py stub
        The $0 live stub (src/compaction/s2/chain/stub_user_no.py): real checkpoint forks, a
        scripted model; record results/sprint/week2/user_no/stub.json.
    uv run python scripts/week2_user_no.py freeze $O
        After every row's `confirmed` / `confirmed_decline` is set by reading: <out>/rubric.json.
    Then: week2_refusal.py prepare / prep-card / summarize (paid) $C $O; week2_refusal_max.py
    opus-reads / spans $O --go; week2_refusal.py versions / plan / run (paid) / score / analyze
    $C $O.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import week2_refusal as W

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import loop as L
from compaction.s2.chain import driver as D
from compaction.s2.chain import refusal as RF
from compaction.s2.chain import refusal_batch as RB
from compaction.s2.chain import user_no as UN
from compaction.s2.config import REPO_ROOT

DEFAULT_OUT = REPO_ROOT / "data" / "raw" / "sprint" / "s2" / "user_no"
EXP1_RAW = REPO_ROOT / "data" / "raw" / "sprint" / "s2" / "monitors" / "exp1_raw"


def _image_present(tag: str) -> bool:
    import subprocess
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True,
                          check=False).returncode == 0


def cmd_sources(a, cfg: dict) -> int:
    rows = UN.source_rows(Path(a.raw), list(cfg["refusal"]["sources"]))
    for r in rows:
        if r.get("checkpoint") and not r.get("error") and not _image_present(
                r["checkpoint"]["tag"]):
            r["error"] = f"checkpoint image not on this host: {r['checkpoint']['tag']}"
    RF.dump(a.out / "sources.json", rows)
    card = ["# Experiment 3c reply card", "", f"- config sha256: `{cfg['_sha256']}`",
            f"- sources sha256: `{RF.sha256(json.dumps(rows, sort_keys=True))}`",
            f"- user turn: {cfg['_texts']['user_no']}",
            (f"- reply cap: {cfg['user_no']['reply_max_calls']} calls, "
             f"${cfg['caps']['reply_usd']}/history; stage {cfg['design']['stage']} "
             f"${cfg['caps']['stage_usd']}"), "", "## Sources", ""]
    card += [f"- {r['history']}: checkpoint message {(r.get('checkpoint') or {}).get('message_index')}"
             f", original decision {r.get('original_decision_kind')}"
             + (f", ERROR {r['error']}" if r.get("error") else "") for r in rows]
    (a.out / "reply_card.md").write_text("\n".join(card) + "\n")
    errs = {r["history"]: r["error"] for r in rows if r.get("error")}
    print(json.dumps({"n": len(rows), "errors": errs}, indent=1))
    return 0 if not errs else 1


def _check_card(a, cfg: dict, rows: list[dict]) -> list[str]:
    card = a.out / "reply_card.md"
    errs = W.check_paid_gate(cfg, go=bool(a.go_from_jacob), card=card, stage="reply",
                             checkout=a.checkout, caps_path=a.caps_file)
    if card.exists() and RF.sha256(json.dumps(rows, sort_keys=True)) not in card.read_text():
        errs.append("reply card is for other sources (rerun `sources`)")
    return errs


def cmd_reply(a, cfg: dict) -> int:
    rows = json.loads((a.out / "sources.json").read_text())
    errs = _check_card(a, cfg, rows)
    bad = {r["history"]: r["error"] for r in rows if r.get("error")}
    if bad:
        errs.append(f"sources with errors: {bad}")
    want = a.histories.split(",") if a.histories else [r["history"] for r in rows]
    unknown = [h for h in want if h not in {r["history"] for r in rows}]
    if unknown:
        errs.append(f"unknown histories: {unknown}")
    if errs:
        for e in errs:
            print(f"refused: {e}", file=sys.stderr)
        return 2
    un = cfg["user_no"]
    D.apply_design(cfg)  # reasoning logging on before the models are built
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    D.watch_stop()  # the STOP file ends every reply at its next model call
    route = D.load_route()
    spend = W._resolve_stage_spend(checkout=a.checkout, caps_path=a.caps_file)
    spec = RB.apply_container_limits(cfg)
    admission = RB.install_laptop_admission(cfg, log=a.out / "laptop_admission.jsonl")
    tasks = W._load_tasks_safe()
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def one(row: dict) -> None:
        hid = row["history"]
        out = a.out / "replies" / hid
        if (out / "result.json").exists():
            res = json.loads((out / "result.json").read_text())
            res["spend_total_usd"] = D._attempt_spend(out)
            with lock:
                results[hid] = res
            return
        try:
            task = tasks.get(RF.history_from_id(hid).a_task)
            inv = Invocation(RF.PURPOSE, run_id=RB.new_batch_run_id(f"reply-{hid}"))
            try:
                with admission.slot("agent", lambda: D.stop_requested(), run_id=inv.run_id):
                    if D.stop_requested():
                        raise L.Stopped("stop requested before the reply started")
                    model, _ = D.paid_model(route, out, float(cfg["caps"]["reply_usd"]), spend)
                    res = UN.run_reply(row, task, model, inv, out, cfg["_texts"]["user_no"],
                                       max_calls=int(un["reply_max_calls"]),
                                       stop_kinds=un["stop_kinds"],
                                       defied_kinds=un["defied_kinds"],
                                       mem_mb=spec["mem_mb"], cpus=spec["cpus"])
            finally:
                RB._cleanup_inv(inv)
        except BaseException as e:  # noqa: BLE001 - recorded per history; the run then fails
            res = {"history": hid, "outcome": "failed", "n_calls": 0, "cost_usd": 0.0,
                   "error": f"worker: {type(e).__name__}: {str(e)[:500]}"}
            RF.dump(out / "worker_error.json", res)  # not result.json: a rerun retries it
        # durable spend of this history's books, restarts included (astra r1 #8, r2 #8)
        res["spend_total_usd"] = D._attempt_spend(out)
        if (out / "result.json").exists() and not str(res.get("error") or "").startswith(
                "worker:"):
            RF.dump(out / "result.json", res)  # the reply's own record, with the total
        with lock:
            results[hid] = res
        print(f"REPLY_RESULT {hid} outcome={res['outcome']} calls={res['n_calls']} "
              f"cost=${res['cost_usd']:.3f} total=${res['spend_total_usd'] or 0:.3f}",
              flush=True)

    threads = [threading.Thread(target=one, args=(r,), name=f"reply-{r['history']}")
               for r in rows if r["history"] in want]
    for t in threads:
        t.start()
    # Ctrl-C: every loop stops at its next model call and waiting slots give up; the workers
    # clean up their containers and are joined before exit (astra r1 #4)
    interrupted = False
    while any(t.is_alive() for t in threads):
        try:
            for t in threads:
                t.join(timeout=1.0)
        except KeyboardInterrupt:
            interrupted = True
            L.STOP.set()
            print("REPLY_INTERRUPTED: stopping every reply at its next call", flush=True)
    summ = {h: {k: r.get(k) for k in ("outcome", "n_calls", "cost_usd", "spend_total_usd",
                                      "error")} for h, r in sorted(results.items())}
    missing = sorted(set(want) - set(results))
    RF.dump(a.out / "reply_summary.json", {"results": summ, "missing": missing,
                                           "interrupted": interrupted})
    print(json.dumps({"results": summ, "missing": missing}, indent=1))
    ok = not missing and not interrupted and all(
        r["outcome"] != "failed" for r in results.values())
    return 0 if ok else 1


def cmd_rubric(a, cfg: dict) -> int:
    rows = {r["history"]: r for r in json.loads((a.out / "sources.json").read_text())}
    props = []
    for hid, row in rows.items():
        rd = a.out / "replies" / hid
        if not (rd / "result.json").exists():
            raise SystemExit(f"no reply for {hid}")
        rep = json.loads((rd / "result.json").read_text())
        vdir = None
        if rep["outcome"] in UN.CANDIDATES:
            vdir, _ = UN.write_virtual(rd, a.out / "virtual" / hid)
        props.append(UN.rubric_proposal(row, rep, vdir))
        print(f"== {hid}: {rep['outcome']} (calls {rep['n_calls']}, decision message "
              f"{rep.get('decision_index')})\n{(rep.get('decision_text') or '')[:600]}\n"
              f"commands: {rep.get('decision_segments')}\n")
    RF.write_rubric(props, a.out / "rubric_proposed.json", rubric_text=UN.RUBRIC_TEXT)
    print(f"wrote {a.out / 'rubric_proposed.json'}: set confirmed / confirmed_decline by reading")
    return 0


def cmd_freeze(a, _cfg: dict) -> int:
    rec = RF.freeze_rubric(a.out / "rubric_proposed.json", a.out / "rubric.json")
    print(json.dumps({"qualified": rec["qualified"], "frozen_sha256": rec["frozen_sha256"]},
                     indent=1))
    return 0


def cmd_stub(a, cfg: dict) -> int:
    from compaction.s2.chain import stub_user_no as SU
    return 0 if SU.run_stub(cfg, Path(a.raw))["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("cmd", choices=["sources", "reply", "rubric", "freeze", "stub"])
    ap.add_argument("--config", type=Path, default=UN.CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--raw", type=Path, default=EXP1_RAW)
    ap.add_argument("--histories", default=None)
    ap.add_argument("--checkout", type=Path, default=None)
    ap.add_argument("--caps-file", type=Path, default=None)
    ap.add_argument("--go-from-jacob", action="store_true")
    a = ap.parse_args(argv)
    W.load_env()
    cfg = UN.load_config(a.config)
    a.out.mkdir(parents=True, exist_ok=True)
    return {"sources": cmd_sources, "reply": cmd_reply, "rubric": cmd_rubric,
            "freeze": cmd_freeze, "stub": cmd_stub}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
