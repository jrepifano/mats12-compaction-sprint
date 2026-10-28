"""R7 replay scheduler. `prepare` is $0; `run` needs separate paid-run and wording approval.

    uv run python scripts/week2_r7_replay.py prepare
    uv run --with fastmcp==4.1.3 python scripts/week2_r7_replay.py run \
        --go-from-jacob --specs-reviewed-by-jacob

No automatic retry: an interrupted/failed run stays visible and needs inspection. All 36
inputs are validated before reading a key. Each task uses the existing Haiku proxy and ledger.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import week2 as W
from compaction.s2.native import cc_chain as X
from compaction.s2.native import r7_replay as R
from compaction.s2.native import slots as SL

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/week2/r7_planted.toml"
PLAN = ROOT / "docs/sprint/plans/week2-r7-planted.md"
PREFIX = "native-r7-chain"
TAG = "s2-r7-planted"


def validated(config: Path, plan: Path) -> tuple[dict, list[R.Prepared]]:
    W.gate(plan, config)
    cfg = W.load_config(config)
    if cfg["design"]["kind"] != "r7_planted":
        raise ValueError("R7 scheduler requires r7_planted config")
    expected = {"stage": "SPRINT-S2-PLANTED", "n_phases": 2, "log_reasoning": True}
    if any(cfg["design"].get(k) != v for k, v in expected.items()):
        raise ValueError("configured stage/phases/reasoning differs from the replay design")
    if (cfg["caps"]["stage_usd"], cfg["caps"]["run_usd"]) != (30, 6):
        raise ValueError("R7 requires the approved $30 stage / $6 run caps")
    par = cfg["parallelism"]
    if (par["agents"], par["fallback_agents"], par["agent_memory_mb"],
            par["fallback_agent_memory_mb"]) != (4, 1, 1280, 3072):
        raise ValueError("R7 requires the approved normal/fallback laptop resources")
    if cfg["caps"]["max_calls"] != CC.MAX_CALLS or cfg["caps"]["max_seconds"] != CC.MAX_SECONDS:
        raise ValueError("task caps differ from the unchanged cc_chain path")
    if (cfg["replay"]["model"], cfg["replay"]["effort"]) != (R.ARM.model, R.ARM.effort):
        raise ValueError("configured model/effort differs from replay arm")
    return cfg, R.prepare(cfg, ROOT)


def names() -> list[str]:
    p = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                       text=True, timeout=60, check=True)
    return p.stdout.split()


def resource_ready() -> tuple[bool, dict]:
    """Existing laptop memory/load thresholds; the shared slot ledger counts containers."""
    try:
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True,
                            timeout=60, check=True)
        sy = subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True,
                            text=True, timeout=30, check=True)
        free = next(int(x.split(":")[1].strip().rstrip("%")) for x in mp.stdout.splitlines()
                    if "free percentage" in x)
        load = float(sy.stdout.split()[1])
        return free >= 30 and load < 12, {"free_pct": free, "load1": load}
    except Exception as e:  # noqa: BLE001 - unreadable resource state fails closed
        return False, {"error": f"{type(e).__name__}: {e}"[:200]}


def init_caps(cfg: dict) -> Path:
    p = ROOT / cfg["replay"]["output_root"] / "budget_caps.toml"
    cap, stage = cfg["caps"]["stage_usd"], cfg["design"]["stage"]
    content = (f'[sprint]\ncheckout = {json.dumps(str(ROOT))}\ncore_cap_usd = {cap}\n'
               f'full_cap_usd = {cap}\ntopup_recorded = false\ntransfer_approval_over_usd = 0.0\n'
               f'\n[stages.{stage}]\ncap_usd = {cap}\n')
    if p.exists() and p.read_text() != content:
        raise ValueError(f"caps file differs: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _finished(out: Path, prepared: R.Prepared) -> bool:
    """Never mistake an old config's output or missing audit for this assigned replay."""
    if not out.exists():
        return False
    meta = json.loads((out / "replay.json").read_text())
    if meta != prepared.provenance or R.sha256((out / "input_session.jsonl").read_bytes()) != \
            prepared.provenance["input_sha256"]:
        raise ValueError(f"output provenance mismatch: {out}")
    result = json.loads((out / "b/result.json").read_text())
    if "end" not in result:
        raise ValueError(f"incomplete output: {out}; inspect before retry")
    return True


def bulk(cfg: dict, prepared: list[R.Prepared], *, fallback: bool = False) -> int:
    from compaction.s2.chain import driver as D
    from compaction.s2.interact_run import PilotSpend
    from compaction.s2.model import RunSpend
    from compaction.s2.native.key_proxy import read_key

    root = ROOT / cfg["replay"]["output_root"]
    root.mkdir(parents=True, exist_ok=True)
    # Lock before key access. This is a separate stage; never share another arm's lock/state.
    with open(root / "bulk.lock", "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = [p for p in prepared if not _finished(root / p.run_id, p)]
        if not pending:
            return 0
        caps = init_caps(cfg)
        tasks = D.tasks()
        key = read_key(ROOT / ".env", "ANTHROPIC_API_KEY")
        par = cfg["parallelism"]
        agents = par["fallback_agents" if fallback else "agents"]
        memory = par["fallback_agent_memory_mb" if fallback else "agent_memory_mb"]
        (root / "assignment.json").write_text(json.dumps(
            {"seed": cfg["design"]["seed"], "order": [p.run_id for p in prepared],
             "config": cfg["_source"], "fallback": fallback, "agents": agents,
             "memory_mb": memory}, indent=2) + "\n")
        log_lock = threading.Lock()

        def say(message):
            line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
            with log_lock, open(root / "bulk.log", "a") as log:
                log.write(line + "\n")
                print(line, flush=True)

        def spend():
            stage = PilotSpend(tags=(TAG,), caps={TAG: cfg["caps"]["stage_usd"]},
                               stage=cfg["design"]["stage"],
                               ledger=ROOT / cfg["replay"]["ledger"], caps_file=caps)
            return RunSpend(stage, cap_usd=cfg["caps"]["run_usd"])

        def one(p):
            out = root / p.run_id
            try:
                with SL.reserve("laptop", f"r7:{p.run_id}", n=2, prefix=PREFIX, capacity=8,
                                names=names, memory_mb=2 * memory,
                                on_wait=lambda state: say(f"slots held for {p.run_id}: {state}")):
                    # Check after reserving too: several waiting workers can become ready at once.
                    while True:
                        ready, state = resource_ready()
                        if ready:
                            break
                        say(f"resource gate closed for {p.run_id}: {state}")
                        time.sleep(30)
                    source = p.provenance["source"]
                    hist = CC.History(source["repo"], "np", source["rep"], source["remote"])
                    a_repo = CC.REPOS[hist.repo]
                    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != hist.repo)]
                    run_cfg = X.HistoryConfig(
                        hist=hist, a_task=tasks[a_repo.a_task], b_task=tasks[b_repo.b_task],
                        out=out, host_ip="host.docker.internal", spend_factory=spend, key=key,
                        memory_mb=memory, cpus=SL.setting("__cpu_tenths__", 20) / 10, arm=R.ARM)
                    say(f"launch {p.run_id}")
                    result = R.run_replay(run_cfg, p)
                    say(f"finish {p.run_id}: {result['end']}")
                    return p.run_id, result["end"]
            except Exception as e:  # noqa: BLE001 - preserve every failed assignment explicitly
                say(f"failed {p.run_id}: {type(e).__name__}: {e}")
                return p.run_id, "unknown"

        with ThreadPoolExecutor(max_workers=agents) as pool:
            results = dict(pool.map(one, pending))
        (root / "reconciliation.json").write_text(json.dumps(results, indent=2) + "\n")
        return 3 if any(x in ("unknown", "failed") for x in results.values()) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--plan", type=Path, default=PLAN)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    run = sub.add_parser("run")
    run.add_argument("--go-from-jacob", action="store_true", required=True)
    run.add_argument("--specs-reviewed-by-jacob", action="store_true", required=True)
    run.add_argument("--fallback", action="store_true", help="one 3072 MB agent")
    args = ap.parse_args(argv)
    cfg, prepared = validated(args.config, args.plan)
    if args.command == "prepare":
        print(json.dumps({"runs": len(prepared), "order": [p.run_id for p in prepared],
                          "input_sha256": {p.run_id: R.sha256(p.data) for p in prepared},
                          "paid_calls": 0}, indent=2))
        return 0
    return bulk(cfg, prepared, fallback=args.fallback)


if __name__ == "__main__":
    raise SystemExit(main())
