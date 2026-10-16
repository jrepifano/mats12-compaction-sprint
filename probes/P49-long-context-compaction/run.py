"""P49 runner (plan.md v5.2). Thin CLI over p49.blocks (scheduler), p49.analyze and p49.samples.

    uv run python probes/P49-long-context-compaction/run.py [global flags] <command>

Commands:
  index    data.build_index + stitch.build_all (every planned history, packing check), and the
           parquet's full sha256 -> results/index_report.json
  smoke    client.endpoints_check + ONE R first call on history 0 (provider Google, c =
           prompt_tokens / ours, transforms accepted); spends ~$0.05 only with --yes
  gate0    8 histories h0..h7, design gate0, levels A and B (+ the k=2,3 replay compactions)
  decide   Gate 0 rules -> results/decision.json (level, admin stop, band, (design, N))
  bulk     histories h8.. from decision.json (design, N, level, band)
  score    score.score_all on every chain record + outcome_at_turn rows for R window_full pairs
  analyze  analyze.run -> results/analysis.json + results/analysis.md
  status   blocks, chains by end reason, spend, concurrency, load
  samples  seeded random raw samples -> results/samples.md

Global flags: --dry (no spend: P49_BASE_URL must be http://127.0.0.1:<port> and
EMX_COSTS_LEDGER a scratch ledger; outputs go to --out, default results/raw/dry), --histories
("0-7" or "0,3,5"), --limit-blocks N, --yes (required for any paid command), --concurrency,
--admit-by / --hard-stop HH:MM ET (dry default: now + 60 / 75 min).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROBE = Path(__file__).resolve().parent
ROOT = PROBE.parents[1]
for _p in (PROBE, ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from p49 import contracts as C

PAID = ("smoke", "gate0", "bulk")


# --- paths and modes ---------------------------------------------------------------------------
def res(name: str) -> Path:
    return C.RESULTS / name


def check_dry(env=os.environ) -> str | None:
    """None if dry mode is safe, else why not."""
    url = env.get("P49_BASE_URL", "")
    if not url.startswith(("http://127.0.0.1", "http://localhost")):
        return f"--dry needs P49_BASE_URL=http://127.0.0.1:<port> (got {url!r})"
    if not C.BASE_URL.startswith(("http://127.0.0.1", "http://localhost")):
        return f"contracts.BASE_URL is {C.BASE_URL!r}: set P49_BASE_URL before starting"
    led = env.get("EMX_COSTS_LEDGER")
    if not led:
        return "--dry needs EMX_COSTS_LEDGER set to a scratch ledger path"
    from emx import spend
    if Path(led).resolve() == spend.DEFAULT_LEDGER.resolve():
        return "--dry: EMX_COSTS_LEDGER points at the real ledger"
    return None


def redirect_outputs(out: Path) -> None:
    """Dry mode: every run output (chains, compactions, checkpoints, scores, analysis, blocks,
    run.log, decision) goes under `out`. Set before any other p49 module is imported, so
    defaults bound at their import see the new paths. Histories and the length index stay
    shared (deterministic, free)."""
    out.mkdir(parents=True, exist_ok=True)
    C.RESULTS = out
    C.RAW = out / "raw"
    C.COMPACTIONS_DIR = C.RAW / "compactions"
    C.CHAINS_DIR = C.RAW / "chains"
    C.CHECKPOINTS = C.RAW / "requests.jsonl"
    C.CHAIN_SUMMARY_JSONL = out / "chains.jsonl"
    C.SCORES_CSV = out / "scores.csv"
    C.ANALYSIS_JSON = out / "analysis.json"


def parse_histories(spec: str | None, default: list[int]) -> list[int]:
    if not spec:
        return default
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def deadlines(a, now: float) -> tuple[float, float]:
    from p49 import blocks as B
    if a.dry:
        admit = B.et_epoch(a.admit_by, now) if a.admit_by else now + 3600
        hard = B.et_epoch(a.hard_stop, now) if a.hard_stop else now + 4500
    else:
        admit = B.et_epoch(a.admit_by or C.ADMIT_BY_ET, now)
        hard = B.et_epoch(a.hard_stop or C.HARD_STOP_ET, now)
    return admit, hard


def concurrency(a) -> int:
    if a.concurrency:
        return a.concurrency
    try:
        return int(C.CONCURRENCY_FILE.read_text().strip())
    except (OSError, ValueError):
        return C.CONCURRENCY


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def read_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def chain_lines(stage: str | None = None) -> dict[str, dict]:
    """The latest chains.jsonl line per chain id (optionally one stage)."""
    out = {}
    for r in read_jsonl(C.CHAIN_SUMMARY_JSONL):
        if stage is None or r.get("stage") == stage:
            out[r["chain_id"]] = r
    return out


def merged_rows(stage: str | None = None) -> list[dict]:
    """Score rows merged onto the chain summaries (score's columns win)."""
    lines = chain_lines(stage)
    scores = {r["chain_id"]: r for r in read_csv(C.SCORES_CSV)}
    rows = []
    for cid, line in sorted(lines.items()):
        row = dict(line)
        row.update({k: v for k, v in (scores.get(cid) or {}).items() if v != ""})
        if cid not in scores:
            row["cheat"] = None
            row["unscored"] = True
        rows.append(row)
    return rows


def tasks_by_id() -> dict:
    from x3 import tasks as xt
    return {t.task_id: t for t in xt.load_manifest().tasks}


# --- commands ----------------------------------------------------------------------------------
def cmd_index(a) -> int:
    from p49 import data, stitch
    t0 = time.time()
    idx = data.build_index()
    sha = data.file_sha256(C.PARQUET) if C.PARQUET.exists() else None
    n = C.GATE0_HISTORIES + max(C.LADDER_N)
    report = stitch.build_all(n)
    out = {"utc": time_utc(), "parquet": str(C.PARQUET), "parquet_sha256": sha,
           "sha_prefix_ok": bool(sha and sha.startswith(C.PARQUET_SHA256_PREFIX)),
           "index_sessions": len((idx or {}).get("sessions", {})) if isinstance(idx, dict)
           else None, "histories": report.get("summary"), "wall_s": round(time.time() - t0, 1)}
    (res("index_report.json")).write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0 if out["sha_prefix_ok"] else 1


def cmd_smoke(a) -> int:
    from p49 import blocks as B
    from p49 import client, tokens

    from emx import spend
    ep = asyncio.run(client.endpoints_check())
    print("endpoints:", json.dumps(ep, default=str))
    store = B.HistoryStore(C.HISTORIES_DIR, _build_history())
    h = store.get(0, C.BAND)
    msgs = list(h.messages) + [B.transition("A")]
    ours = tokens.count(msgs, h.tools)
    max_tokens = 256
    body = C.make_body(msgs, h.tools, max_tokens)
    proj = (ours * C.C_INIT * C.PRICE_IN_PER_M + max_tokens * C.PRICE_OUT_PER_M) / 1e6
    print(f"R first call on {h.history_id}: our count {ours}, projected ${proj:.4f}")
    if not a.yes and not a.dry:
        print("not sent: pass --yes to spend")
        return 0
    rid = spend.reserve_api(C.EXP, round(max(0.10, 2 * proj), 4), note="smoke:R:h0")
    spend.mark_submitted(rid, exp=C.EXP)
    try:
        r = asyncio.run(client.chat(body, kind="agent", chain_id="smoke-h0-R", our_tokens=ours))
    except Exception:
        spend.settle_api(rid, None, exp=C.EXP)
        raise
    spend.settle_api(rid, r.cost_usd, exp=C.EXP)
    c = (r.prompt_tokens or 0) / ours if ours else None
    out = {"utc": time_utc(), "history_id": h.history_id, "our_tokens": ours,
           "prompt_tokens": r.prompt_tokens, "c": c, "provider": r.provider,
           "provider_ok": r.provider == C.PROVIDER_NAME, "transforms_accepted": True,
           "finish_reason": r.finish_reason, "cost_usd": r.cost_usd,
           "cost_from_usage": r.cost_from_usage, "latency_s": r.latency_s, "endpoints": ep}
    res("smoke.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "endpoints"}, default=str))
    ok = out["provider_ok"] and c is not None and abs(c - 1) <= 0.05
    if c is not None and abs(c - 1) > 0.01:
        print(f"WARNING: provider/ours ratio {c:.4f} is off by > 1%: re-centre the band")
    return 0 if ok else 1


def _build_history():
    from p49 import stitch
    return stitch.build_history


def _scheduler(a, n_rule=None):
    from p49 import blocks as B
    deps = B.real_deps()
    if a.dry:
        from p49 import client
        client.set_checkpoint_path(C.CHECKPOINTS)
    now = deps.clock()
    admit, hard = deadlines(a, now)
    return B.Scheduler(deps, B.Paths.from_contracts(), concurrency=concurrency(a),
                       admit_by=admit, hard_stop=hard, n_rule=n_rule)


def _run_blocks(a, blocks) -> int:
    if a.limit_blocks:
        blocks = blocks[: a.limit_blocks]
    sched = _scheduler(a)
    out = asyncio.run(sched.run(blocks))
    print(json.dumps(out, default=str))
    return 0 if out.get("stop_reason") in (None, "hard stop") else 3


def cmd_gate0(a) -> int:
    from p49 import blocks as B
    idx = parse_histories(a.histories, list(range(C.GATE0_HISTORIES)))
    return _run_blocks(a, B.gate0_blocks(idx))


def load_decision() -> dict:
    p = res("decision.json")
    if not p.exists():
        raise SystemExit(f"{p} missing: run `decide` first")
    return json.loads(p.read_text())


def cmd_bulk(a) -> int:
    from p49 import blocks as B
    d = load_decision()
    if d.get("stop"):
        print(f"decision says stop: {d.get('stop_reason')}")
        return 4
    n = int(d["N"])
    idx = parse_histories(a.histories, list(range(C.GATE0_HISTORIES, C.GATE0_HISTORIES + n)))
    blocks = [B.BlockSpec(index=i, design=d["design"], levels=(d["level"],), stage="bulk",
                          band=tuple(d["band"])) for i in idx]
    return _run_blocks(a, blocks)


def cmd_decide(a) -> int:
    from p49 import analyze as A

    from emx import spend
    rows = merged_rows("gate0")
    if not rows:
        raise SystemExit("no gate0 chains in chains.jsonl")
    unscored = [r["chain_id"] for r in rows if r.get("unscored")]
    if unscored:
        raise SystemExit(f"{len(unscored)} gate0 chains unscored: run `score` first")
    hids = {r["history_id"] for r in rows}
    comps = [d for (hid, _), d in A.load_compactions().items() if hid in hids]
    blocks = [b for b in read_jsonl(res("blocks.jsonl")) if b.get("stage") == "gate0"]
    tot = spend.totals(C.EXP)
    cap = spend.caps_for(C.EXP).hard_cap_usd
    now = time.time()
    from p49 import blocks as B
    start = B.et_epoch(a.start, now) if a.start else now
    end = B.et_epoch(a.admit_by or C.ADMIT_BY_ET, now)
    d = A.gate0_decision(rows, comps, blocks, spent_usd=tot["total_usd"], start_epoch=start,
                         end_epoch=end, concurrency=concurrency(a), cap_usd=cap)
    d["utc"] = time_utc()
    d["calibration_c"] = _measured_c()
    res("decision.json").write_text(json.dumps(d, indent=1, default=str) + "\n")
    brief = {k: d[k] for k in ("level", "level_reason", "pooled_rates", "admin_stop", "band",
                               "budget_usd", "p90_block_s", "design", "N", "stop",
                               "stop_reason")}
    print(json.dumps(brief, indent=1, default=str))
    return 0


def _measured_c() -> float | None:
    """The largest provider/ours ratio in the checkpoints, if the client records both."""
    best = None
    for r in read_jsonl(C.CHECKPOINTS):
        pt, ours = r.get("prompt_tokens"), r.get("our_tokens")
        if r.get("event") == "response" and pt and ours:
            best = max(best or 0.0, pt / ours)
    return best


def cmd_score(a) -> int:
    from p49 import score
    tbi = tasks_by_id()
    paths = sorted(C.CHAINS_DIR.glob("*.json"))
    recs = [json.loads(p.read_text()) for p in paths]
    rows = score.score_all(recs, tbi, workers=a.workers)
    print(f"scored {len(rows)} new chain(s) -> {C.SCORES_CSV}")
    # R-turn sensitivity rows: for pairs where R ended window_full, the other arms at R's turn
    out = res("scores_at_turn.jsonl")
    have = {(r["chain_id"], r["turn"]) for r in read_jsonl(out)}
    by_pair: dict[tuple, dict[str, dict]] = {}
    for r in recs:
        s = r["spec"]
        by_pair.setdefault((s["history_id"], s["task_id"], s["level"]), {})[s["arm"]] = r
    n = 0
    for arms in by_pair.values():
        rr = arms.get(C.R)
        if not rr or rr.get("end_reason") != C.END_WINDOW:
            continue
        t = int(rr.get("n_turns") or 0)
        for arm, rec in arms.items():
            cid = rec["spec"]["chain_id"]
            if arm == C.R or (cid, t) in have:
                continue
            row = score.outcome_at_turn(rec, tbi[rec["spec"]["task_id"]], t)
            with open(out, "a") as f:
                f.write(json.dumps({"chain_id": cid, "turn": t, "cheat": row.get("cheat"),
                                    "missing_reason": row.get("missing_reason")}) + "\n")
            n += 1
    print(f"{n} outcome_at_turn row(s) -> {out}")
    return 0


def cmd_analyze(a) -> int:
    from p49 import analyze as A
    stage = None if a.stage == "all" else a.stage
    rows = merged_rows(stage)
    for r in rows:
        if isinstance(r.get("calls"), str):
            r["calls"] = []
    hist = {}
    for p in C.HISTORIES_DIR.glob("h*.json"):
        if "." in p.stem:
            continue
        try:
            hist[p.stem] = {"instances": json.loads(p.read_text()).get("instances", [])}
        except ValueError:
            continue
    out = A.run(rows, compactions=A.load_compactions(), histories=hist,
                turn_rows=read_jsonl(res("scores_at_turn.jsonl")), n_boot=a.n_boot)
    out["stage"] = a.stage
    C.ANALYSIS_JSON.write_text(json.dumps(out, indent=1, default=str) + "\n")
    res("analysis.md").write_text(A.markdown(out))
    print(A.markdown(out))
    return 0


def cmd_status(a) -> int:
    from collections import Counter

    from emx import spend
    blocks = read_jsonl(res("blocks.jsonl"))
    state = json.loads(res("scheduler_state.json").read_text()) if res(
        "scheduler_state.json").exists() else {}
    lines = chain_lines()
    by_arm: dict[str, Counter] = {}
    for r in lines.values():
        by_arm.setdefault(r.get("arm", "?"), Counter())[r.get("end_reason")] += 1
    try:
        load = subprocess.run(["uptime"], capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip()
    except OSError:
        load = None
    out = {"blocks_done": Counter(b.get("status") for b in blocks),
           "blocks_running": state.get("blocks_running"), "in_flight": len(
               state.get("in_flight") or []), "scheduler": {k: state.get(k) for k in (
                   "utc", "concurrency", "cap", "stop_reason", "admit_by", "hard_stop")},
           "chains_by_arm": {k: dict(v) for k, v in sorted(by_arm.items())},
           "spend": spend.totals(C.EXP), "concurrency_file": _read(C.CONCURRENCY_FILE),
           "stop_file": C.STOP_FILE.exists(), "load": load}
    print(json.dumps(out, indent=1, default=str))
    return 0


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def cmd_samples(a) -> int:
    from p49 import samples
    cheats = {r["chain_id"]: r.get("cheat") for r in read_csv(C.SCORES_CSV)}
    p = samples.write(res("samples.md"), k=a.k, cheats=cheats)
    print(f"wrote {p}")
    return 0


def time_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


COMMANDS = {"index": cmd_index, "smoke": cmd_smoke, "gate0": cmd_gate0, "decide": cmd_decide,
            "bulk": cmd_bulk, "score": cmd_score, "analyze": cmd_analyze,
            "status": cmd_status, "samples": cmd_samples}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--dry", action="store_true", help="mock server + scratch ledger only")
    ap.add_argument("--out", type=Path, default=None, help="dry-mode output dir")
    ap.add_argument("--histories", default=None, help='"0-7" or "0,3,5"')
    ap.add_argument("--limit-blocks", type=int, default=None)
    ap.add_argument("--yes", action="store_true", help="allow paid calls")
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--admit-by", default=None, help="HH:MM ET")
    ap.add_argument("--hard-stop", default=None, help="HH:MM ET")
    ap.add_argument("--start", default=None, help="decide: bulk start HH:MM ET (default now)")
    ap.add_argument("--stage", default="bulk", choices=("bulk", "gate0", "dry", "all"),
                    help="analyze: which chains")
    ap.add_argument("--n-boot", type=int, default=10_000)
    ap.add_argument("--workers", type=int, default=8, help="score: pass-check threads")
    ap.add_argument("--k", type=int, default=3, help="samples per arm / method")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    if a.dry:
        why = check_dry()
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 2
        redirect_outputs(a.out or (PROBE / "results" / "raw" / "dry"))
    elif a.command in ("gate0", "bulk") and not a.yes:
        print(f"refused: `{a.command}` spends money; pass --yes (or --dry)", file=sys.stderr)
        return 2
    C.RESULTS.mkdir(parents=True, exist_ok=True)
    return COMMANDS[a.command](a)


if __name__ == "__main__":
    raise SystemExit(main())
