"""Five-task chains in Antigravity (plan docs/sprint/plans/week2-agy-chain5.md).

    uv run python scripts/week2_agy_chain5.py check
    uv run python scripts/week2_agy_chain5.py pull
    uv run --with fastmcp==4.1.3 python scripts/week2_agy_chain5.py smoke --go-from-jacob
    uv run --with fastmcp==4.1.3 python scripts/week2_agy_chain5.py bulk --go-from-jacob \
        [--max-live 4] [--memory-mb 1280]
    uv run python scripts/week2_agy_chain5.py grade [--smoke]
    uv run python scripts/week2_agy_chain5.py progress [--smoke]

agy 1.4.7 with gemini-3.8-flash-low on Jacob's plan quota ($0; one $0 row per phase in this
checkout's ledger, stage SPRINT-S2-AGY-CHAIN), on the laptop (local docker, where agy's auth
volume is). Experiment 4's unrelated chains u1-u4, five tasks each, with experiment 4's tasks,
order, ticket tags, junk remote, teammate drafts, task text and lead-ins: all read from
experiment 4's own config (`native_chain.source_config`), never retyped. Google sees everything
agy sees; the ten task repos are public projects.

- `check`: the drift gate and the source gate, with no docker. The plan check (the config
  against the plan's Build contract and its sha256), the config's wording and orders equal to
  experiment 4's, the chains present in the orders, every task's directory and image name,
  and which images are already local. `smoke` and `bulk` run the same gate first and refuse
  on any failure, naming it.
- `pull`: `docker pull` of the chains' images not yet local, in first-use order, timed, with
  their digests recorded in `<root>/images.json`.
- `smoke`: the config's smoke chain, its first `smoke_phases` tasks, then the end `/compact`
  (`agy_nchain.run_chain` with `max_phases`), in `data/raw/sprint/s2/agy_chain5_smoke/`; it
  prints its checks.
- `bulk`: the config's chains in `data/raw/sprint/s2/agy_chain5/`, at most `--max-live` at
  once, each launch waiting on the laptop gate of the harness replication
  (`week2_harness_agy.admit` and `held_slots`: the shared slot ledger, at least 30% free memory,
  load under the limit). A chain whose state is "done" is skipped; anything else is reported,
  never relaunched over its own evidence (a rerun uses `--attempt`).
- `grade`: each phase's hidden tests on its final tip (`driver.grade_run`), one grader at a time,
  after the chains (no grader competes with a live agent for memory).
- `progress`: the state file, one line per chain.

Quota (intent to treat), as in the harness replication: the first refusal sets the shared stop
flag; no further phase, boundary or chain is launched, and the cut tasks are unknown.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "configs/week2/agy_chain5.toml"
PLAN = "docs/sprint/plans/week2-agy-chain5.md"
RAW = ROOT / "data" / "raw" / "sprint" / "s2"
BULK_DIR, SMOKE_DIR = RAW / "agy_chain5", RAW / "agy_chain5_smoke"
STAGE = "SPRINT-S2-AGY-CHAIN"
# run roots per harness (agy's are BULK_DIR and SMOKE_DIR); only agy's driver exists so far
PREFIX = {"agy": "agy", "codex": "cx", "claude_code": "cc", "agy_sonnet": "agy_sonnet",
          "claude_code_sonnet": "cc_sonnet"}
BUILT = ("agy", "agy_sonnet", "codex", "claude_code", "claude_code_sonnet")


def roots(harness: str) -> tuple[Path, Path]:
    """(bulk root, smoke root) of a harness."""
    return RAW / f"{PREFIX[harness]}_chain5", RAW / f"{PREFIX[harness]}_chain5_smoke"


def harness_settings(cfg: dict, harness: str) -> dict:
    """The config's table for a harness it runs; refuses one it doesn't, or one not built."""
    if harness not in cfg["native_chain"]["harnesses"]:
        sys.exit(f"refused: {harness} is not in native_chain.harnesses")
    if harness not in BUILT:
        sys.exit(f"refused: the {harness} chain driver is not built yet (phase 2)")
    return cfg["native_chain"][harness]


def adapter(harness: str, hs: dict):
    """The chain loop's Harness for a config harness. agy-Sonnet is agy with the session model
    set for this process (`agy_chain.set_model`), so `Harness.check` sees the real model."""
    from compaction.s2.native import agy_chain as AC
    from compaction.s2.native import agy_nchain as AN
    if harness in ("agy", "agy_sonnet"):
        AC.set_model(hs["model"])
        return AN.AGY
    if harness == "codex":
        from compaction.s2.native import nchain_cx
        return nchain_cx.CX
    if harness in ("claude_code", "claude_code_sonnet"):
        from compaction.s2.native import nchain_cc
        from compaction.s2.native.key_proxy import read_key
        spend = stage_spend(harness, hs)  # one per arm: every chain and phase shares it (Argon r1)
        return nchain_cc.cc_harness(hs["model"], hs["effort"],
                                    key=read_key(ROOT / ".env", "ANTHROPIC_API_KEY"),
                                    spend_factory=lambda: spend, stage=hs["stage"])
    raise ValueError(f"no adapter for {harness}")


def caps_file(harness: str, hs: dict) -> Path:
    """The paid arm's caps file (its stage cap from the config), written once, never changed."""
    p = roots(harness)[0].parent / f"{PREFIX[harness]}_chain5_budget_caps.toml"
    content = ("# Five-task chains, paid Claude Code arm (docs/sprint/plans/week2-agy-chain5.md)\n"
               f"[sprint]\ncheckout = {json.dumps(str(ROOT))}\n"
               f"core_cap_usd = {hs['cap_usd']}\nfull_cap_usd = {hs['cap_usd']}\n"
               "topup_recorded = false\ntransfer_approval_over_usd = 0.0\n\n"
               f"[stages.{hs['stage']}]\ncap_usd = {hs['cap_usd']}\n")
    if p.exists():
        if p.read_text() != content:
            sys.exit(f"{p} exists with other content; not overwriting")
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.replace(p)  # atomic: a reader never sees it half written
    return p


def stage_spend(harness: str, hs: dict):
    """The arm's stage Spend: its stage cap, booked to this checkout's ledger (the adapter adds
    the per-task cap, `settings.run_usd`)."""
    from compaction.s2.interact_run import PilotSpend
    tag = f"{PREFIX[harness]}-chain5"
    return PilotSpend(tags=(tag,), caps={tag: hs["cap_usd"]}, stage=hs["stage"],
                      ledger=ROOT / "logs" / "costs.jsonl", caps_file=caps_file(harness, hs))


def arm_settings(settings, hs: dict):
    """Experiment 4's settings, with a paid arm's per-task cost cap (plan v0.4: $6 per Claude
    Code task, as in the harness replication; experiment 4's own run cap was pi's $2.50)."""
    from dataclasses import replace
    cap = hs.get("task_cap_usd")
    return replace(settings, run_usd=cap) if cap else settings


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------------------------------
# The gate (no docker)
# ------------------------------------------------------------------------------------------


def load(config: str = CONFIG) -> tuple[dict, dict]:
    """(this experiment's config, experiment 4's config); both strictly validated. Validation
    refuses wording, orders or call/time caps that differ from experiment 4's
    (`week2._native_chain_errors`)."""
    from compaction.s2.chain import week2 as W
    cfg = W.load_config(config)
    src = W.load_config(cfg["native_chain"]["source_config"]["path"])
    return cfg, src


def chains_and_settings(cfg: dict, src: dict, harness: str | None = None):
    """(the chains to run, in the config's order: the harness's own list when given (plan v0.4),
    else every chain; experiment 4's settings, with its texts registered)."""
    from compaction.s2.chain import nchain as NC
    settings = NC.settings_from_config(src)
    by_id = {c.id: c for c in NC.chains_from_config(src)}
    ids = ((cfg["native_chain"][harness].get("chains") if harness else None)
           or cfg["native_chain"]["chains"])
    missing = [c for c in ids if c not in by_id]
    if missing:
        raise ValueError(f"chains not in experiment 4's orders: {missing}")
    chains = [by_id[c] for c in ids]
    bad = [c.id for c in chains if c.arm != "unrelated" or len(c.phases) != cfg["design"][
        "n_phases"] or any(p.project != "switch" for p in c.phases[1:])]
    if bad:
        raise ValueError(f"not five-task unrelated chains with a switch at every boundary: {bad}")
    return chains, settings


def task_ids(chains) -> list[str]:
    """Every task of the chains, in first-use order (position, then chain)."""
    out: list[str] = []
    for k in range(max(len(c.phases) for c in chains)):
        for c in chains:
            if k < len(c.phases) and c.phases[k].task not in out:
                out.append(c.phases[k].task)
    return out


def task_infos(ids) -> dict:
    from compaction.s2 import tasks as T
    return {t: T.task_info(t) for t in ids}


def local_images(images) -> dict[str, bool]:
    out = {}
    for im in images:
        p = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", im],
                           capture_output=True, text=True, timeout=60, check=False)
        out[im] = p.returncode == 0
    return out


def gate(config: str = CONFIG, docker: bool = True) -> dict:
    """The drift and source gate; {"ok", "problems", ...}. Never raises on a problem."""
    from compaction.s2.chain import week2 as W
    problems: list[str] = []
    rec: dict = {}
    try:
        chk = W.plan_check(PLAN, config)
        rec["plan_check"] = {k: chk[k] for k in ("ok", "hash_ok", "mismatches",
                                                 "missing_in_config", "missing_in_contract",
                                                 "changed_sections")}
        if not chk["ok"]:
            problems.append("plan check: " + W.report(chk))
        cfg, src = load(config)
        chains, _ = chains_and_settings(cfg, src)
        ids = task_ids(chains)
        infos = task_infos(ids)
        rec["tasks"] = {t: infos[t].image for t in ids}
        if docker:
            have = local_images(sorted(set(rec["tasks"].values())))
            rec["images_local"] = have
            missing = [t for t, im in rec["tasks"].items() if not have[im]]
            if missing:
                problems.append(f"images not pulled for {missing} (run `pull`)")
    except Exception as e:  # noqa: BLE001 - every failure is a refusal, named
        problems.append(f"{type(e).__name__}: {e}")
    return {"ok": not problems, "problems": problems, **rec}


def cmd_setup_check(a) -> int:
    """Experiment 4's setup on the harness's own container for every (chain, position) role the
    chains use, with no agent and no model call: `agy_nchain.chain_setup` in a native session
    (sidecar and all), then a read-back of the remote branch and the prompt, then the session
    closes. One container at a time. Output: <bulk root>/setup_check.json."""
    import tempfile

    from compaction.s2.chain import nchain as NC
    from compaction.s2.native import agy_nchain as AN
    require_gate()
    cfg, src = load()
    harness_settings(cfg, a.harness)
    chains, settings = chains_and_settings(cfg, src)
    roles: dict[tuple[str, bool], tuple] = {}  # (task, first) -> (chain, k, prev task)
    for c in chains:
        for k, ph in enumerate(c.phases, 1):
            roles.setdefault((ph.task, k == 1), (c, k, c.phases[k - 2].task if k > 1 else None))
    tasks = task_infos(task_ids(chains))
    out_rows = []
    for (task_id, first), (c, k, prev) in sorted(roles.items()):
        t0 = time.monotonic()
        row: dict = {"task": task_id, "role": "junk" if first else "teammate", "chain": c.id,
                     "k": k}
        with tempfile.TemporaryDirectory() as td:
            ccfg = AN.ChainConfig(chain=c, tasks=tasks, settings=settings, root=Path(td))
            pr = AN.PhaseRun(cfg=ccfg, k=k, n_run=len(c.phases), task=tasks[task_id],
                             setup=NC.push_setup(task_id, texts_first=settings.texts_first),
                             out=Path(td) / "p", prev_task=prev)
            pr.out.mkdir()
            try:
                with ccfg.harness.open(tasks[task_id].image, ccfg) as ns:
                    env = AN.chain_setup(ns.sb, pr)
                    remote = env.harness(f"git -C {env.bare} log --format='%an|%s' "
                                         f"{pr.setup.branch} --", check=False).output
                    row |= {"ok": True, "base_check": pr.extra.get("base_check"),
                            "setup": {kk: v for kk, v in pr.extra["setup"].items()
                                      if kk in ("kind", "branch")},
                            "remote_log": remote.strip().splitlines()[:4],
                            "ticket_tag": pr.extra.get("ticket_tag"),
                            "prompt_head": (pr.prompt or "")[:160],
                            "prompt_has_tag": bool(pr.extra.get("ticket_tag"))
                            and pr.extra["ticket_tag"] in (pr.prompt or ""),
                            "tests_in_inventory": len(pr.extra.get("test_inventory") or [])}
            except Exception as e:  # noqa: BLE001 - recorded per role
                row |= {"ok": False, "error": f"{type(e).__name__}: {e}"[:600]}
        row["seconds"] = round(time.monotonic() - t0, 1)
        print(f"{utc()} {task_id} {row['role']}: {'ok' if row['ok'] else row['error']}",
              flush=True)
        out_rows.append(row)
    left = our_containers(a.harness)
    root = roots(a.harness)[0]
    root.mkdir(parents=True, exist_ok=True)
    (root / "setup_check.json").write_text(json.dumps(
        {"utc": utc(), "rows": out_rows, "containers_left": left}, indent=1))
    print(f"{sum(r['ok'] for r in out_rows)}/{len(out_rows)} ok; containers left: {left}")
    return 0 if all(r["ok"] for r in out_rows) and not left else 1


def cmd_check(_a) -> int:
    g = gate()
    print(json.dumps(g, indent=1))
    return 0 if g["ok"] else 1


def require_gate(config: str = CONFIG) -> None:
    g = gate(config)
    if not g["ok"]:
        sys.exit("refused:\n  " + "\n  ".join(g["problems"]))


# ------------------------------------------------------------------------------------------
# Images
# ------------------------------------------------------------------------------------------


def cmd_pull(_a) -> int:
    cfg, src = load()
    chains, _ = chains_and_settings(cfg, src)
    infos = task_infos(task_ids(chains))
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    rec_path = BULK_DIR / "images.json"
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    have = local_images(sorted({i.image for i in infos.values()}))
    rc = 0
    for t, info in infos.items():
        if not have[info.image]:
            t0 = time.monotonic()
            p = subprocess.run(["docker", "pull", "-q", info.image], capture_output=True,
                               text=True, check=False)
            print(f"{utc()} pull {t}: rc={p.returncode} {time.monotonic() - t0:.0f}s",
                  flush=True)
            if p.returncode != 0:
                print(p.stderr[-500:], file=sys.stderr)
                rc = 1
                continue
        q = subprocess.run(["docker", "image", "inspect", "--format",
                            "{{json .RepoDigests}} {{.Size}}", info.image],
                           capture_output=True, text=True, check=False)
        digests, size = q.stdout.rsplit(" ", 1) if q.returncode == 0 else ("null", "0")
        rec[t] = {"image": info.image, "repo_digests": json.loads(digests),
                  "size_bytes": int(size), "utc": utc()}
        rec_path.write_text(json.dumps(rec, indent=1))
    return rc


# ------------------------------------------------------------------------------------------
# Running chains
# ------------------------------------------------------------------------------------------


def book_phase(out: Path, k: int, task, stage: str = STAGE) -> None:
    """One $0 ledger row per phase (a plan's quota), with its model calls as units."""
    from emx import spend
    res = json.loads((out / "result.json").read_text())
    spend.record_once(f"chain5:{out}", "judge", "plan-quota", res.get("n_calls") or 0,
                      0.0, note=f"chain5 phase {out.parent.name} p{k} {task.task_id} "
                      f"({res.get('end')}): plan quota, no charge", exp=stage)


class GraderQueue:
    """Grades finished phases one at a time beside the chains (plan v0.2: a single grader at
    experiment 4's memory, only while the laptop's resources are fine), so no grader waits for
    the last chain and none competes with another grader."""

    def __init__(self, lim: dict, say, poll_s: float = 30.0):
        import queue
        self.q: queue.Queue = queue.Queue()
        self.lim, self.say, self.poll_s = lim, say, poll_s
        self.closed = threading.Event()
        self.th = threading.Thread(target=self._work, name="grader", daemon=True)
        self.th.start()

    def put(self, out: Path, task) -> None:
        if (out / "tip.patch").exists():
            self.q.put((out, task))

    def close(self) -> None:
        self.closed.set()
        self.th.join()

    def _work(self) -> None:
        import queue

        while True:
            try:
                out, task = self.q.get(timeout=self.poll_s)
            except queue.Empty:
                if self.closed.is_set():
                    return
                continue
            if unresolved():  # no new container past an unconfirmed removal
                self.say(f"grade {out}: skipped, cleanup unconfirmed {unresolved()}")
                continue
            try:
                # one slot in the shared host ledger for the grader's whole life, beside the
                # chains' own 2 each (astra code r1 #4): the laptop's capacity (`__capacity__`)
                # bounds agents and grader together
                g = grade_one(out, task, self.lim, self.poll_s)
                self.say(f"graded {out.parent.name}/{out.name}: tip "
                         f"{(g.get('tip') or {}).get('reward')}")
            except Exception as e:  # noqa: BLE001 - `grade` reruns what is missing
                self.say(f"grade {out}: {type(e).__name__}: {e}"[:300])


GRADER_PREFIX = "s2-chaining"  # driver.grade_run's containers (chain config PURPOSE)


def unresolved() -> list[str]:
    """Every container removal still unconfirmed in this process: the chains' native sessions
    (agy_nchain) and the graders' (driver.grade_run's cleanup; astra code r2), once each."""
    from compaction.s2.chain import driver as D
    from compaction.s2.native import agy_nchain as AN
    return list(dict.fromkeys(list(AN.UNRESOLVED_CLEANUP) + [f"grader {r}" for r in D.UNRESOLVED]))


def still_present(records: list[str]) -> list[str]:
    """Persisted unconfirmed removals (state.json `unresolved_cleanup`) whose containers docker
    still lists, by the run label for graders and by name for native sessions (astra code r3:
    a fresh process must not forget them). An unreadable docker answer keeps the record."""
    from compaction.deepswe import sandbox as SB
    out = []
    for r in records:
        rid = r.removeprefix("grader ")
        args = (["ps", "-a", "-q", "--filter", f"label={SB.RUN_LABEL_KEY}={rid}"]
                if r.startswith("grader ") else ["ps", "-a", "-q", "--filter", f"name={rid}"])
        q = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60,
                           check=False)
        if q.returncode != 0 or q.stdout.strip():
            out.append(r)
    return out


def grade_one(out: Path, task, lim: dict, poll_s: float = 30.0) -> dict:
    """One phase's grade, admitted like any container: the laptop gate, then one slot in the
    shared ledger held through the grader's cleanup. Both grading entry points use it."""
    from compaction.s2.chain import driver as D
    HA = _hist_module()
    while not HA.resources_ok(None, lim)[0]:
        time.sleep(poll_s)
    from compaction.s2.native import agy_nchain as AN
    with grader_slot(f"agy-chain5-grader:{out.parent.name}:{out.name}"):
        try:
            return D.grade_run(out, task, "same_as_tip")
        finally:
            # a grader left behind holds every chain before its next phase, as an unremoved
            # native session does (astra code r3: the driver reads this list)
            for r in D.UNRESOLVED:
                if f"grader {r}" not in AN.UNRESOLVED_CLEANUP:
                    AN.UNRESOLVED_CLEANUP.append(f"grader {r}")


def grader_slot(owner: str):
    from compaction.s2.chain import config as CC
    from compaction.s2.native import slots as SL
    HA = _hist_module()
    import week2_harness_cc as CCH
    return SL.reserve("laptop", owner, n=1, prefix=GRADER_PREFIX, capacity=CCH.MAX_CONTAINERS,
                      names=lambda: HA.host_names(None), memory_mb=CC.GRADE_MEMORY_MB)


def _hist_module():
    import week2_harness_agy as HA  # the laptop gate and the shared slot ledger
    return HA


def limits(max_live: int) -> dict:
    """The harness replication's laptop limits, with this run's live-chain ceiling. Each chain
    holds 2 slots (its agent container and its sidecar), as a history does."""
    HA = _hist_module()
    import week2_harness_cc as CCH
    return {"max_live": max_live, "max_containers": CCH.MAX_CONTAINERS,
            "min_free_gb": 8.0, "max_load": 12.0, "host_reading": "laptop",
            "agent_cpus": HA.CPUS}


CODE_FILES = ("src/compaction/s2/native/agy_nchain.py", "src/compaction/s2/native/agy_chain.py",
              "src/compaction/s2/chain/nchain.py", "src/compaction/s2/chain/week2.py",
              "scripts/week2_agy_chain5.py")


def provenance(config: str = CONFIG) -> dict:
    """What a run root is bound to: the config, plan and source config hashes, and the code."""
    from compaction.s2.chain import week2 as W
    cfg, _ = load(config)
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=False).stdout.strip()
    return {"config": {"path": config, "sha256": W.sha256_file(W.resolve(config))},
            "plan_sha256": cfg["meta"]["built_against"],
            "source_config": cfg["native_chain"]["source_config"],
            "code": {f: W.sha256_file(ROOT / f) for f in CODE_FILES}, "git_head": head}


def bind_root(state, prov: dict, capacity: dict) -> None:
    """Record the provenance in state.json on first use; refuse a root made under another config
    or other code (astra code r1 #6). Every invocation's capacity is appended, not replaced."""
    old = state.data.get("provenance") if hasattr(state, "data") else None
    if old is not None and (old.get("config") != prov["config"] or old.get("code") != prov["code"]):
        sys.exit("refused: this run root was made under another config or other code; use a "
                 "new --attempt")
    state.set(provenance=prov, capacity=list(state.data.get("capacity") or []) + [capacity])


def free_disk_gb() -> float:
    import shutil
    return shutil.disk_usage("/").free / 1e9


def run(chains, settings, tasks: dict, root: Path, *, max_phases: int | None, max_live: int,
        memory_mb: int, end_compaction: bool, stage: str = STAGE, grade: bool = True,
        expect: dict | None = None, fallback_live: int | None = None,
        min_disk_gb: float = 10.0, harness_impl=None, prefix: str = "agy-chain5") -> int:
    from compaction.s2.native import agy_nchain as AN
    HA = _hist_module()
    root.mkdir(parents=True, exist_ok=True)
    lim = limits(max_live)
    state = AN.StateFile.load(root)
    left = still_present(state.data.get("unresolved_cleanup") or [])
    if left:  # astra code r3: an earlier process's unremoved container blocks every launch
        sys.exit(f"refused: unconfirmed removals from an earlier run still present: {left}")
    bind_root(state, provenance(), {"utc": utc(), "max_live": max_live, "memory_mb": memory_mb,
                                    "fallback_live": fallback_live, "min_disk_gb": min_disk_gb,
                                    "max_phases": max_phases, "chains": [c.id for c in chains]})
    quota = threading.Event()
    lock, live = threading.Lock(), []
    log = root / "run.log"

    def say(msg: str) -> None:
        line = f"{utc()} {msg}"
        with lock, open(log, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    grader = GraderQueue(lim, say) if grade else None

    def on_result(out: Path, k: int, task) -> None:  # every written phase (astra code r1 #7)
        try:
            book_phase(out, k, task, stage)
        except Exception as e:  # noqa: BLE001 - the ledger row is recorded later by hand
            say(f"{out}: ledger row failed: {type(e).__name__}: {e}"[:300])

    def after(out: Path, k: int, task) -> None:  # gradeable phases only
        if grader is not None:
            grader.put(out, task)

    def one(chain) -> None:
        try:
            with HA.held_slots(None, f"{prefix}:{root.name}:{chain.id}", lim, memory_mb, say):
                cfg = AN.ChainConfig(chain=chain, tasks=tasks, settings=settings, root=root,
                                     host=None, memory_mb=memory_mb,
                                     cpus=HA.history_cpus(None, HA.CPUS),
                                     max_phases=max_phases, quota_stop=quota,
                                     end_compaction=end_compaction, expect=expect,
                                     **({"harness": harness_impl} if harness_impl else {}))
                rec = AN.run_chain(cfg, state, after_phase=after, on_result=on_result)
            say(f"chain {chain.id}: {rec.get('state')}")
        except Exception as e:  # noqa: BLE001 - a worker never kills the scheduler
            say(f"chain {chain.id}: worker error {type(e).__name__}: {e}"[:500])

    threads = []
    for c in chains:
        cur = (state.chain(c.id) or {}).get("state")
        if cur == "done":
            say(f"chain {c.id}: done already, skipped")
            continue
        if cur not in (None, "pending"):
            say(f"chain {c.id}: state {cur!r} from an earlier run; not relaunched over its "
                "evidence (use a new --attempt root)")
            continue
        if quota.is_set():
            say(f"chain {c.id}: not launched (quota stop)")
            continue
        last = 0.0
        while True:
            with lock:
                live[:] = [th for th in live if th.is_alive()]
                n_live = len(live)
            if unresolved():  # astra code r1 #8, r2: no launch past an unremoved container
                say(f"not launching {c.id}: cleanup unconfirmed for {unresolved()}")
                state.set(unresolved_cleanup=unresolved())
                quota.set()  # the shared stop: nothing further is launched
                break
            # plan v0.3.1: under min_disk_gb free, at most fallback_live chains at once
            disk = free_disk_gb()
            lim["max_live"] = (min(max_live, fallback_live) if fallback_live and
                               disk < min_disk_gb else max_live)
            ok, st = HA.admit(n_live, None, lim)
            st["disk_free_gb"] = round(disk, 1)
            if ok or quota.is_set():
                break
            if time.monotonic() - last > 600:
                say(f"gate closed for {c.id}: live {n_live}, {st}")
                last = time.monotonic()
            time.sleep(30)
        if quota.is_set():
            say(f"chain {c.id}: not launched (quota stop)")
            continue
        say(f"launch {c.id} ({[p.task for p in c.phases[:max_phases or None]]}) live {n_live}")
        th = threading.Thread(target=one, args=(c,), name=c.id)
        th.start()
        with lock:
            live.append(th)
        threads.append(th)
    for th in threads:
        th.join()
    if grader is not None:
        grader.close()
    if unresolved():
        state.set(unresolved_cleanup=unresolved())
        say(f"cleanup unconfirmed: {unresolved()}")
    done = {c.id: (state.chain(c.id) or {}).get("state") for c in chains}
    say(f"run end: {done}")
    return 0 if all(v == "done" for v in done.values()) and not unresolved() else 3


def our_containers(harness: str) -> list[str]:
    """Running containers this harness's chains own (their name prefix; the smoke's check is
    scoped to them, not to the whole host)."""
    from compaction.s2.native import common as N
    ps = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True,
                        text=True, timeout=60, check=False)
    if ps.returncode != 0:  # an unreadable inventory is never an empty one (astra code r2)
        return [f"docker ps failed rc={ps.returncode}"]
    prefix = N.purpose(f"{harness}-chain") if harness == "agy" else PREFIX[harness] + "-chain"
    return [x for x in ps.stdout.split() if x.startswith(prefix)]


def _attempt_root(base: Path, attempt: int | None) -> Path:
    return base if not attempt else base.with_name(f"{base.name}_attempt{attempt}")


def cmd_smoke(a) -> int:
    if not a.go_from_jacob:
        sys.exit("refused: the smoke needs Jacob's go, relayed (--go-from-jacob)")
    require_gate()
    cfg, src = load()
    hs = harness_settings(cfg, a.harness)
    impl = adapter(a.harness, hs)
    chains, settings = chains_and_settings(cfg, src, a.harness)
    settings = arm_settings(settings, hs)
    nc = cfg["native_chain"]
    chain = next(c for c in chains if c.id == nc["smoke_chain"])
    tasks = task_infos(task_ids([chain])[:nc["smoke_phases"]])
    root = _attempt_root(roots(a.harness)[1], a.attempt)
    rc = run([chain], settings, tasks, root, max_phases=nc["smoke_phases"], max_live=1,
             memory_mb=a.memory_mb or cfg["parallelism"]["agent_memory_mb"],
             end_compaction=nc["end_compaction"], stage=hs["stage"], expect=expect(cfg, a.harness),
             harness_impl=impl, prefix=f"{PREFIX[a.harness]}-chain5")
    checks = smoke_checks(root, chain.id, nc["smoke_phases"], a.harness)
    v = smoke_verdict(checks, nc["smoke_phases"], a.harness)
    v |= {"utc": utc(), "root": str(root), "provenance": provenance(), "checks": checks}
    (root / "smoke_verdict.json").write_text(json.dumps(v, indent=1))
    print(json.dumps(v, indent=1))
    return 0 if v["ok"] and rc == 0 else 1


SMOKE_PEAK_MB = 1000  # plan v0.3.1: a smoke peak above this sets the bulk's fallback memory


def expect(cfg: dict, harness: str) -> dict:
    """What the harness adapter must really run (astra code r1 #5): the config's table."""
    t = cfg["native_chain"][harness]
    return {"model": t["model"], "cli_version": t["cli_version"],
            "compaction_retries": cfg["native_chain"]["compaction_retries"]}


def smoke_verdict(checks: dict, n: int, harness: str) -> dict:
    """The plan's smoke checks as one verdict (astra code r1 #3); the bulk refuses without a
    passing one made under the same config and code."""
    fails = []
    for k in range(1, n + 1):
        ph = checks.get(f"p{k}")
        if not isinstance(ph, dict):
            fails.append(f"p{k}: no result")
            continue
        if ph.get("end") == "failed":
            fails.append(f"p{k}: ended failed")
        if ph.get("setup") != ("junk" if k == 1 else "teammate"):
            fails.append(f"p{k}: setup {ph.get('setup')}")
        if not ph.get("ticket_tag"):
            fails.append(f"p{k}: no ticket tag")
        if ph.get("grade_tip") is None:
            fails.append(f"p{k}: no grade")
    if not checks.get("same_conversation"):
        fails.append("the conversation changed between phases")
    if checks.get("boundaries_ok") != n - 1:
        fails.append(f"boundaries ok {checks.get('boundaries_ok')} of {n - 1}")
    if not checks.get("end_ok"):
        fails.append("the end compaction did not succeed")
    if checks.get("containers_left"):
        fails.append(f"containers left: {checks['containers_left']}")
    peaks = [(checks.get(f"p{k}") or {}).get("memory_peak_mb") if isinstance(
        checks.get(f"p{k}"), dict) else None for k in range(1, n + 1)]
    peak = max((x for x in peaks if x is not None), default=None)
    # default memory only when every phase has a reading under the rule (astra code r2)
    default = all(x is not None for x in peaks) and peak <= SMOKE_PEAK_MB
    return {"ok": not fails, "failures": fails, "harness": harness, "memory_peak_mb": peak,
            "memory_peaks_mb": peaks, "bulk_memory": "default" if default else "fallback"}


# A smoke may carry over to code that differs only here: the runner's admission, grading and
# verdict code, none of which runs inside a phase (logged; the Advisor's option (c), 29 Oct)
CARRY_OVER_FILES = ("scripts/week2_agy_chain5.py",)


def passing_smoke(harness: str, attempt: int | None = None, accept_from: str | None = None,
                  reason: str | None = None) -> dict:
    """The smoke verdict the bulk needs: passing, same config and code; else exit. With
    `accept_from` (that verdict's path) and a reason, a verdict whose code differs only in
    CARRY_OVER_FILES is accepted and the carry-over recorded in the build log."""
    p = Path(accept_from) if accept_from else \
        _attempt_root(roots(harness)[1], attempt) / "smoke_verdict.json"
    if not p.exists():
        sys.exit(f"refused: no smoke verdict at {p} (run the smoke first)")
    v = json.loads(p.read_text())
    now = provenance()
    if not v.get("ok"):
        sys.exit(f"refused: the smoke failed: {v.get('failures')}")
    if v["provenance"]["config"] != now["config"]:
        sys.exit("refused: the smoke ran under another config; rerun it")
    diff = sorted(f for f in set(now["code"]) | set(v["provenance"]["code"])
                  if now["code"].get(f) != v["provenance"]["code"].get(f))
    if diff and not accept_from:
        sys.exit(f"refused: the smoke ran under other code ({diff}); rerun it, or pass "
                 "--accept-smoke-from with --accept-reason if only the runner changed")
    if diff:
        if any(f not in CARRY_OVER_FILES for f in diff):
            sys.exit(f"refused: code outside the runner changed since the smoke: {diff}")
        if not reason:
            sys.exit("refused: --accept-smoke-from needs --accept-reason")
        v["carried_over"] = {"utc": utc(), "from": str(p), "files": diff, "reason": reason,
                             "smoke_code": {f: v["provenance"]["code"].get(f) for f in diff},
                             "now_code": {f: now["code"].get(f) for f in diff}}
        with open(ROOT / "logs" / "s2" / "week2_build_log.md", "a") as f:
            f.write(f"\n- {utc()}: agy_chain5 smoke carried over to the bulk ({p}); code "
                    f"differs only in {diff}: {reason}\n")
    return v


def smoke_checks(root: Path, chain_id: str, n: int, harness: str = "agy") -> dict:
    """What the plan's smoke must show, read from the run's files."""
    cdir = root / "chains" / chain_id
    rows = [json.loads(x) for x in (cdir / "compactions.jsonl").read_text().splitlines()
            if x.strip()] if (cdir / "compactions.jsonl").exists() else []
    out: dict = {"compactions": [{k: r.get(k) for k in ("kind", "boundary", "phase", "ok",
                                                        "empty", "summary_chars", "attempt")}
                                 for r in rows]}
    cids = []
    for k in range(1, n + 1):
        p = cdir / f"p{k}" / "result.json"
        if not p.exists():
            out[f"p{k}"] = "missing"
            continue
        res = json.loads(p.read_text())
        ex = res.get("extra") or {}
        cids.append(ex.get("conversation_id"))
        g = cdir / f"p{k}" / "grade.json"
        out[f"p{k}"] = {"end": res.get("end"), "n_calls": res.get("n_calls"),
                        "seconds": res.get("seconds"),
                        "memory_peak_mb": (ex.get("memory") or {}).get("peak_mb"),
                        "grade_tip": ((json.loads(g.read_text()).get("tip") or {}).get("reward")
                                      if g.exists() else None),
                        "setup": (ex.get("setup") or {}).get("kind"),
                        "ticket_tag": ex.get("ticket_tag"),
                        "container": ex.get("container"),
                        "phase_message_index": ex.get("phase_message_index"),
                        "auto_compactions": sum(c.get("trigger") != "manual"
                                                for c in ex.get("compactions") or []),
                        "compaction_json": (cdir / f"p{k}" / "compaction.json").exists(),
                        "tip_patch": (cdir / f"p{k}" / "tip.patch").exists()}
    out["same_conversation"] = len(set(cids)) == 1 and None not in cids
    out["boundaries_ok"] = sum(r.get("kind") == "boundary" and r.get("ok") for r in rows)
    out["end_ok"] = any(r.get("kind") == "end" and r.get("ok") and not r.get("empty")
                        for r in rows)
    # the chains' containers by name; the graders' (shared s2-chaining prefix, other runs use
    # it too) through their cleanup records, this process's and those persisted by earlier ones
    st = json.loads((root / "state.json").read_text()) if (root / "state.json").exists() else {}
    out["containers_left"] = list(dict.fromkeys(
        our_containers(harness) + unresolved()
        + still_present(st.get("unresolved_cleanup") or [])))
    return out


def cmd_bulk(a) -> int:
    if not a.go_from_jacob:
        sys.exit("refused: the bulk needs Jacob's go, relayed (--go-from-jacob)")
    require_gate()
    cfg, src = load()
    hs = harness_settings(cfg, a.harness)
    v = passing_smoke(a.harness, a.smoke_attempt, a.accept_smoke_from, a.accept_reason)
    impl = adapter(a.harness, hs)
    chains, settings = chains_and_settings(cfg, src, a.harness)
    settings = arm_settings(settings, hs)
    tasks = task_infos(task_ids(chains))
    par = cfg["parallelism"]
    max_live = a.max_live or par["agents"]
    if max_live > par["agents"]:
        sys.exit(f"refused: --max-live {max_live} over parallelism.agents")
    memory_mb = (par["fallback_agent_memory_mb"] if v["bulk_memory"] == "fallback"
                 else par["agent_memory_mb"])
    if a.memory_mb and a.memory_mb < memory_mb:
        sys.exit(f"refused: --memory-mb {a.memory_mb} under the smoke rule's {memory_mb}")
    return run(chains, settings, tasks, _attempt_root(roots(a.harness)[0], a.attempt),
               max_phases=None, max_live=max_live, memory_mb=a.memory_mb or memory_mb,
               end_compaction=cfg["native_chain"]["end_compaction"], stage=hs["stage"],
               expect=expect(cfg, a.harness), fallback_live=par["fallback_agents"],
               harness_impl=impl, prefix=f"{PREFIX[a.harness]}-chain5")


def cmd_grade(a) -> int:
    root = _attempt_root(roots(a.harness)[1 if a.smoke else 0], a.attempt)
    rc = 0
    for res_p in sorted(root.glob("chains/*/p*/result.json")):
        out = res_p.parent
        task_id = (json.loads(res_p.read_text()).get("extra") or {}).get("task")
        if not (out / "tip.patch").exists() or not task_id:
            continue
        if unresolved():
            sys.exit(f"stopped: cleanup unconfirmed {unresolved()}")
        g = grade_one(out, task_infos([task_id])[task_id], limits(1))
        print(f"{utc()} graded {out.relative_to(root)}: tip {(g.get('tip') or {}).get('reward')}",
              flush=True)
        rc |= (g.get("tip") or {}).get("reward") is None
    return int(rc)


def cmd_progress(a) -> int:
    root = _attempt_root(roots(a.harness)[1 if a.smoke else 0], a.attempt)
    p = root / "state.json"
    if not p.exists():
        print(f"no state at {p}")
        return 1
    st = json.loads(p.read_text())
    for cid, rec in (st.get("chains") or {}).items():
        runs = rec.get("phase_runs") or {}
        ph = " ".join(f"p{k}:{v.get('end')}/{v.get('n_calls')}" for k, v in sorted(runs.items()))
        bad = [c for c in rec.get("compactions") or [] if not c.get("ok") or c.get("empty")]
        print(f"{cid}: {rec.get('state')} | {ph} | compactions {len(rec.get('compactions') or [])}"
              f" ({len(bad)} failed or empty)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sc = sub.add_parser("setup-check")
    sc.add_argument("--harness", default="agy", choices=list(PREFIX))
    sub.add_parser("pull")
    for name in ("smoke", "bulk", "grade", "progress"):
        s = sub.add_parser(name)
        s.add_argument("--harness", default="agy", choices=list(PREFIX))
        s.add_argument("--attempt", type=int, help="a fresh run root (<root>_attempt<N>)")
        if name in ("grade", "progress"):
            s.add_argument("--smoke", action="store_true")
            continue
        s.add_argument("--go-from-jacob", action="store_true")
        s.add_argument("--memory-mb", type=int)
        if name == "bulk":
            s.add_argument("--max-live", type=int)
            s.add_argument("--smoke-attempt", type=int, help="the smoke root whose verdict counts")
            s.add_argument("--accept-smoke-from", help="a smoke_verdict.json made under other "
                           "runner code (CARRY_OVER_FILES only)")
            s.add_argument("--accept-reason")
    a = ap.parse_args()
    with contextlib.suppress(Exception):
        os.nice(5)
    return {"check": cmd_check, "setup-check": cmd_setup_check, "pull": cmd_pull, "smoke": cmd_smoke, "bulk": cmd_bulk,
            "grade": cmd_grade, "progress": cmd_progress}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
