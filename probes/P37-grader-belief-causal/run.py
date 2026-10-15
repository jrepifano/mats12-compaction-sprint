"""P37 orchestrator (plan.md): canary, decision-point forks, continuations.

  PYTHONPATH=probes/P37-grader-belief-causal:src:scripts uv run --group swarm python \
      probes/P37-grader-belief-causal/run.py canary [--sites 5]
  ... run.py forks                      # K = N_FORKS one-turn forks per site (unchanged prefix)
  ... run.py episodes --sites smoke --conds V,S,N,U --repeats 1
  ... run.py episodes --sites all --conds V,S,N,U --repeats 1-8
  ... run.py status

Episodes run in a live pool: a new continuation starts whenever one finishes and fewer than the
current concurrency are in flight. The concurrency is re-read from results/CONCURRENCY every few
seconds (lower it to throttle; in-flight episodes finish, nothing completed is lost). A
results/STOP file stops new launches. Before each launch `emx.spend.check_total` checks the
projected worst case of everything in flight plus unrecorded charges against P37's cap; charges
are recorded in the ledger every RECORD_EVERY episodes and at exit. Completed artifacts are
skipped on rerun; a sandbox or API failure is rerun (at most twice per episode, failures kept in
results/failures.jsonl).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

PROBE = Path(__file__).resolve().parent
ROOT = PROBE.parents[1]
for _p in (PROBE, ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from p37 import agent as A
from p37 import contracts as C
from p37 import inject as INJ

PROJECTION_USD = 0.06            # per continuation, worst case before the smoke measures it
FORK_PROJECTION_USD = 0.01
RECORD_EVERY = 20
RETRY_END = ("sandbox_error", "api_error")
MAX_TRIES = 3
FAILURES = C.RESULTS_DIR / "failures.jsonl"


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def load_sites() -> list[dict]:
    return load_jsonl(C.SITES_JSONL)


def pick_sites(sites: list[dict], which: str) -> list[dict]:
    if which == "all":
        return sites
    if which == "smoke":
        # three sites from three different tasks, in draw order
        out, seen = [], set()
        for s in sites:
            if s["task_id"] not in seen:
                out.append(s)
                seen.add(s["task_id"])
            if len(out) == 3:
                break
        return out
    ids = set(which.split(","))
    return [s for s in sites if s["site_id"] in ids]


def parse_repeats(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",")]


def make_spec(site: dict, cond: str, repeat: int, mode: str = "reasoning") -> A.Spec:
    msgs, sentence = INJ.resumed_messages(site["prefix_messages"], cond, repeat, mode)
    return A.Spec(episode_id=C.episode_id(cond, site["site_id"], repeat),
                  site_id=site["site_id"], task_id=site["task_id"], cond=cond,
                  para=C.paraphrase_for(cond, repeat), repeat=repeat, sentence=sentence,
                  mode=mode, remaining_turns=site["remaining_turns"],
                  remaining_usd=site["remaining_usd"], tar_path=str(PROBE / site["tar"]),
                  site_sha256=site["site_sha256"], initial_messages=msgs,
                  prev_usage=tuple(site["prev_usage"]) if site.get("prev_usage") else None)


def plan_order(sites: list[dict], conds: list[str], repeats: list[int]) -> list[tuple]:
    """(site, cond, repeat) in waves by repeat, shuffled within each wave (seeded)."""
    out = []
    for r in repeats:
        wave = [(s, c, r) for s in sites for c in conds]
        random.Random(f"{C.SEED}:wave{r}").shuffle(wave)
        out += wave
    return out


def done_ok(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        art = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return art.get("end_reason") not in RETRY_END


def tries(eid: str) -> int:
    return sum(1 for r in load_jsonl(FAILURES) if r.get("episode_id") == eid)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def concurrency() -> int:
    try:
        return max(0, int(C.CONCURRENCY_FILE.read_text().strip()))
    except (OSError, ValueError):
        return C.DEFAULT_CONCURRENCY


class Ledger:
    """Unrecorded charges, written to the ledger in batches (one row per batch)."""

    def __init__(self, label: str):
        self.label, self.usd, self.n, self.total, self.total_n = label, 0.0, 0, 0.0, 0

    def add(self, usd: float) -> None:
        self.usd += usd
        self.n += 1
        if self.n >= RECORD_EVERY:
            self.flush()

    def flush(self) -> None:
        from emx import spend

        if self.n:
            spend.record("judge", f"openrouter:{C.GLM.model_id}", self.n, round(self.usd, 6),
                         note=f"P37 {self.label}: {self.n} calls/episodes", exp=C.EXP)
            self.total += self.usd
            self.total_n += self.n
            self.usd, self.n = 0.0, 0


async def run_pool(jobs: list, run_one, label: str, per_job_usd: float) -> dict:
    """Run `run_one(job) -> (charge_usd, status)` over jobs with live concurrency."""
    from emx import spend

    queue = list(jobs)
    in_flight: dict[asyncio.Task, object] = {}
    led = Ledger(label)
    status: Counter = Counter()
    t0 = time.time()
    stopped = None
    crashes = 0
    try:
        while queue or in_flight:
            if C.STOP_FILE.exists() and stopped is None:
                stopped = "STOP file"
            conc = concurrency()
            while queue and stopped is None and len(in_flight) < conc:
                try:
                    spend.check_total(per_job_usd * (len(in_flight) + 1) + led.usd, "judge",
                                      exp=C.EXP)
                except Exception as e:  # noqa: BLE001 - the cap refuses: stop launching
                    stopped = f"spend guard: {e}"
                    break
                job = queue.pop(0)
                in_flight[asyncio.create_task(run_one(job))] = job
            if not in_flight:
                break
            done, _ = await asyncio.wait(list(in_flight), timeout=5,
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                in_flight.pop(t)
                try:
                    usd, st = t.result()
                except Exception as e:  # noqa: BLE001 - a crashed job is reported, not fatal
                    usd, st = 0.0, f"crash:{type(e).__name__}"
                    crashes += 1
                    print(f"  job crashed: {e!r}"[:500], flush=True)
                    if crashes >= 5 and stopped is None:
                        stopped = "5 crashed jobs"
                else:
                    crashes = 0
                led.add(usd)
                status[st] += 1
            n = sum(status.values())
            if done and (n % 10 == 0 or not queue):
                print(f"[{label}] {n} done, {len(in_flight)} in flight, {len(queue)} queued, "
                      f"conc {conc}, spent {led.total + led.usd:.3f}, "
                      f"{(time.time() - t0) / 60:.1f} min, {dict(status)}", flush=True)
    finally:
        led.flush()
    return {"status": dict(status), "spent": led.total, "n": led.total_n, "stopped": stopped,
            "left": len(queue), "minutes": round((time.time() - t0) / 60, 1)}


# --- continuations ---------------------------------------------------------------------------------
async def episode_job(spec: A.Spec) -> tuple[float, str]:
    path = C.episode_path(spec.cond, spec.site_id, spec.repeat)
    art = await A.run_continuation(spec)
    usd = A.charge(art)
    if art["end_reason"] in RETRY_END:
        with open(FAILURES, "a") as f:  # noqa: ASYNC230 - one short append
            f.write(json.dumps({"episode_id": spec.episode_id, "end_reason": art["end_reason"],
                                "error": art["error"], "t": time.time()}) + "\n")
    write_atomic(path, A.dumps(art))
    print(f"  {spec.episode_id}: {art['end_reason']} turns={art['n_turns']} "
          f"${usd:.4f} {art['wall_s']}s", flush=True)
    return usd, str(art["end_reason"])


def cmd_episodes(a) -> int:
    sites = pick_sites(load_sites(), a.sites)
    conds = a.conds.split(",")
    jobs = []
    for s, c, r in plan_order(sites, conds, parse_repeats(a.repeats)):
        p = C.episode_path(c, s["site_id"], r)
        eid = C.episode_id(c, s["site_id"], r)
        if done_ok(p) or tries(eid) >= MAX_TRIES:
            continue
        jobs.append(make_spec(s, c, r, a.mode))
    print(f"{len(jobs)} continuations to run over {len(sites)} sites", flush=True)
    if a.dry_run:
        for j in jobs[:5]:
            print(j.episode_id, j.remaining_turns, j.remaining_usd, (j.sentence or "")[:60])
        return 0
    res = asyncio.run(run_pool(jobs, episode_job, f"episodes-{a.sites}", a.projection))
    print(json.dumps(res), flush=True)
    return 0


# --- forks and canary --------------------------------------------------------------------------------
async def fork_job(job: dict) -> tuple[float, str]:
    r = await A.fork(job["messages"])
    rec = {**{k: v for k, v in job.items() if k != "messages"}, **r}
    with open(job["out"], "a") as f:  # noqa: ASYNC230 - one short append
        f.write(json.dumps(rec, default=str) + "\n")
    usd = r["billed_usd"] if r.get("billed_usd") is not None else (r.get("ceiling_usd") or 0.0)
    return usd, "ok" if not r.get("error") else "error"


def cmd_forks(a) -> int:
    sites = pick_sites(load_sites(), a.sites)
    out = C.FORKS_JSONL
    have = {(r["site_id"], r["fork"]) for r in load_jsonl(out) if not r.get("error")}
    jobs = [{"site_id": s["site_id"], "task_id": s["task_id"], "fork": k,
             "messages": s["prefix_messages"], "out": str(out)}
            for s in sites for k in range(C.N_FORKS) if (s["site_id"], k) not in have]
    print(f"{len(jobs)} forks to run", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    res = asyncio.run(run_pool(jobs, fork_job, "forks", FORK_PROJECTION_USD))
    print(json.dumps(res), flush=True)
    return 0


def cmd_canary(a) -> int:
    """Gate (i): a canary sentence asking for `echo <word>` appended to the site turn's reasoning
    (mode reasoning: details edited; text_only: text alone; content: visible text), and no edit;
    2 next turns per arm per site."""
    sites = load_sites()[: a.sites]
    tmpl = C.CANARY_TEMPLATE_V2 if a.version == 2 else C.CANARY_TEMPLATE
    out = C.RESULTS_DIR / ("canary.jsonl" if a.version == 1 else f"canary_v{a.version}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"{C.SEED}:canary")
    jobs = []
    for s in sites:
        word = f"MARIGOLD-{rng.randrange(1000, 9999)}"
        sentence = tmpl.format(word=word)
        for arm in ("none", "reasoning", "text_only", "content"):
            msgs = s["prefix_messages"] if arm == "none" else INJ.inject(
                s["prefix_messages"], sentence, arm)
            for k in range(a.samples):
                jobs.append({"site_id": s["site_id"], "task_id": s["task_id"], "arm": arm,
                             "fork": k, "word": word, "messages": msgs, "out": str(out)})
    res = asyncio.run(run_pool(jobs, fork_job, f"canary-v{a.version}", FORK_PROJECTION_USD))
    rows = [r for r in load_jsonl(out) if not r.get("error")]
    summ: dict = {}
    for r in rows:
        tc = json.dumps(r.get("tool_calls") or [])
        hit_cmd = r["word"] in tc
        hit_any = hit_cmd or r["word"] in (r.get("reasoning") or "") \
            or r["word"] in (r.get("text") or "")
        d = summ.setdefault(r["arm"], {"n": 0, "cmd": 0, "any": 0})
        d["n"] += 1
        d["cmd"] += hit_cmd
        d["any"] += hit_any
    print(json.dumps({"pool": res, "summary": summ}, indent=1))
    (C.RESULTS_DIR / f"canary_v{a.version}_summary.json").write_text(json.dumps(summ, indent=1) + "\n")
    return 0


def cmd_status(a) -> int:
    from emx import spend

    c: Counter = Counter()
    for p in C.EPISODES_DIR.rglob("*.json"):
        art = json.loads(p.read_text())
        c[(art["meta"]["cond"], art["end_reason"])] += 1
    print(json.dumps({"episodes": {f"{k[0]}:{k[1]}": v for k, v in sorted(c.items())},
                      "budget": spend.totals(C.EXP)}, indent=1, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("episodes")
    e.add_argument("--sites", default="smoke")
    e.add_argument("--conds", default="V,S,N,U")
    e.add_argument("--repeats", default="1")
    e.add_argument("--mode", default="reasoning", choices=INJ.MODES)
    e.add_argument("--projection", type=float, default=PROJECTION_USD)
    e.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("forks")
    f.add_argument("--sites", default="all")
    k = sub.add_parser("canary")
    k.add_argument("--sites", type=int, default=5)
    k.add_argument("--version", type=int, default=1)
    k.add_argument("--samples", type=int, default=2)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    return {"episodes": cmd_episodes, "forks": cmd_forks, "canary": cmd_canary,
            "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
