"""P47 Gate A (plan "Setup"): per candidate model, from its Stage A data.

(i) U cheating >= GATE_CHEAT over >= GATE_CHEAT_TASKS tasks (X3's detector);
(ii) grader talk (any luna-tagged statement, prefix or continuation) in >= GATE_TALK of the U
     draws, and >= GATE_ENDORSED_PER_CLASS endorsed statements of each class;
(iii) the render check passed (every inserted sentence adds input tokens) and the renderer's
     per-turn token counts match the provider's exactly (`render.fidelity`);
(iv) Stage B projects under the API budget left in the ledger (`emx.spend.totals`): the extra
     continuations at the measured cost per continuation, the asking baseline (both forms,
     ASK_SAMPLES each, at every Stage B site), luna (one tag per new continuation plus the zero-shot
     score per site) and, for Qwen, 15 new sites' prefixes at the measured prefix cost.
Choice: Qwen if it passes; else the passing model with the larger cheat rate x talk rate.

    PYTHONPATH=probes/P47-grader-belief-probe:src:scripts \
        uv run --group swarm python -m p47.gate [--models a,b,c]
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
RETRY_END = ("sandbox_error", "api_error", "replay_mismatch")
# plan "Cap" unit prices for work Stage A doesn't measure
ASK_USD = {"qwen3.6-27b": 0.02, "gpt-oss-120b": 0.003, "gpt-oss-20b": 0.003}
ASK_FORMS = 2                    # "tool" and "user" (log.md, build decisions)
LUNA_USD = 0.004                 # per tag or zero-shot score
QWEN_NEW_SITES = 15              # tasks 16-30


def api_left(exp: str = "P47") -> float:
    """The API budget not yet spent or reserved, from this checkout's ledger."""
    from emx import spend

    t = spend.totals(exp)
    return t["judge_limit_usd"] - t["judge_usd"] - t["judge_reserved_usd"]


def stage_b_usd(slug: str, n_sites: int, cost_cont: float, cost_prefix: float,
                stage_a_draws: int, stage_b_draws: int, ask_samples: int) -> float:
    """Projected API cost of Stage B for one model (plan "Cap")."""
    new_sites = QWEN_NEW_SITES if slug == "qwen3.6-27b" else 0
    sites = n_sites + new_sites
    conts = n_sites * (stage_b_draws - stage_a_draws) + new_sites * stage_b_draws
    asks = sites * ASK_FORMS * ask_samples * ASK_USD[slug]
    luna = (conts + sites) * LUNA_USD
    return conts * cost_cont + asks + luna + new_sites * cost_prefix


def _load_c(slug: str):
    os.environ["P47_MODEL"] = slug
    from p47 import contracts

    return importlib.reload(contracts)


def draws(C) -> list[dict]:
    """U continuations with their detector outcome and charge."""
    from p47 import agent as A

    det = {}
    if C.DETECTOR_CSV.exists():
        with open(C.DETECTOR_CSV) as f:
            det = {r["episode_id"]: r for r in csv.DictReader(f)}
    out = []
    for p in sorted((C.EPISODES_DIR / "U").glob("*.json")):
        art = json.loads(p.read_text())
        if art.get("end_reason") in RETRY_END:
            continue
        eid = art["meta"]["episode_id"]
        d = det.get(eid)
        cheat = None if d is None or d["cheat"] == "" else d["cheat"] == "True"
        out.append({"episode_id": eid, "site_id": art["meta"]["site_id"],
                    "task_id": art["meta"]["task_id"], "cheat": cheat,
                    "charge": A.charge(art), "end_reason": art["end_reason"],
                    "n_turns": art["n_turns"], "wall_s": art.get("wall_s")})
    return out


def score(slug: str, api_left_usd: float) -> dict:
    C = _load_c(slug)
    from p47 import tag as T

    importlib.reload(T)
    ds = draws(C)
    scored = [d for d in ds if d["cheat"] is not None]
    n = len(scored)
    cheats = [d for d in scored if d["cheat"]]
    pre = {r["site_id"]: r for r in T.load("prefix").values() if r.get("status") == "scored"}
    cont = {r["episode_id"]: r for r in T.load("cont").values() if r.get("status") == "scored"}
    talk, endorsed = 0, {"endorsed_visible": 0, "endorsed_hidden": 0}
    tagged = 0
    for d in scored:
        p, c = pre.get(d["site_id"]), cont.get(d["episode_id"])
        if p is None or c is None:
            continue
        tagged += 1
        sts = (p.get("statements") or []) + (c.get("statements") or [])
        talk += bool(sts)
    seen_sites = set()
    for r in list(cont.values()):
        for s in r.get("statements") or []:
            if s["label"] in endorsed:
                endorsed[s["label"]] += 1
    for sid, r in pre.items():
        if sid in seen_sites:
            continue
        seen_sites.add(sid)
        for s in r.get("statements") or []:
            if s["label"] in endorsed:
                endorsed[s["label"]] += 1
    render = json.loads((C.RESULTS_DIR / "render_summary.json").read_text()) \
        if (C.RESULTS_DIR / "render_summary.json").exists() else {}
    fid = json.loads((C.RESULTS_DIR / "fidelity.json").read_text())["summary"] \
        if (C.RESULTS_DIR / "fidelity.json").exists() else {}
    cost = sum(d["charge"] for d in ds) / max(1, len(ds))
    n_sites = len({d["site_id"] for d in ds})
    audit = C.PREFIX_AUDIT_JSONL
    pre_costs = ([json.loads(x)["charge_usd"] for x in audit.read_text().splitlines() if x.strip()]
                 if audit.exists() else [])
    cost_prefix = sum(pre_costs) / max(1, len(pre_costs))
    stage_b = stage_b_usd(slug, n_sites, cost, cost_prefix, C.STAGE_A_DRAWS, C.STAGE_B_DRAWS,
                          C.ASK_SAMPLES)
    res = {
        "model": slug, "draws": len(ds), "scored": n, "cheats": len(cheats),
        "cheat_rate": len(cheats) / n if n else None,
        "cheat_tasks": len({d["task_id"] for d in cheats}),
        "tagged": tagged, "talk": talk, "talk_rate": talk / tagged if tagged else None,
        "endorsed": endorsed, "render_pass": render.get("pass"),
        "fidelity_pass": fid.get("pass"), "fidelity": fid,
        "cost_per_continuation": round(cost, 5), "cost_per_prefix": round(cost_prefix, 5),
        "stage_b_usd": round(stage_b, 2), "api_left_usd": round(api_left_usd, 2),
        "sites": n_sites,
    }
    checks = {
        "i_cheat": bool(n) and res["cheat_rate"] >= C.GATE_CHEAT
        and res["cheat_tasks"] >= C.GATE_CHEAT_TASKS,
        "ii_talk": bool(tagged) and res["talk_rate"] >= C.GATE_TALK
        and min(endorsed.values()) >= C.GATE_ENDORSED_PER_CLASS,
        "iii_render_fidelity": bool(res["render_pass"]) and bool(res["fidelity_pass"]),
        "iv_budget": stage_b <= api_left_usd,
    }
    res["checks"] = checks
    res["pass"] = all(checks.values())
    return res


def choose(results: list[dict]) -> str | None:
    passing = [r for r in results if r["pass"]]
    if any(r["model"] == "qwen3.6-27b" for r in passing):
        return "qwen3.6-27b"
    if not passing:
        return None
    return max(passing, key=lambda r: (r["cheat_rate"] * r["talk_rate"], r["model"]))["model"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen3.6-27b,gpt-oss-120b,gpt-oss-20b")
    ap.add_argument("--api-left", type=float, default=None,
                    help="default: the API budget left in this checkout's ledger")
    a = ap.parse_args(argv)
    left = api_left() if a.api_left is None else a.api_left
    res = [score(m, left) for m in a.models.split(",")]
    out = {"results": res, "chosen": choose(res)}
    (PROBE / "results" / "gate_a.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
