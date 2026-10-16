"""P49 history blocks and the rolling scheduler (plan.md v5.2 "Running", "Hard stop", "Cap").

A block is one stitched history H (history i <-> task i in a seeded order over X3's 15 tasks,
cycling), its compactions and every chain of the design (x levels). Compactions run as soon as
the history is ready (Codex, pi, Inspect of H and Inspect of S1 in parallel; Rep-k2 after
Inspect, Rep-k3 after Rep-k2, each after a replay fill block); each chain is queued the moment
its start context exists (Fresh and R at once). Gate 0 also runs the k = 2, 3 replay
compactions with no chains, timed.

Scheduler: at most `concurrency` chains in flight (the CONCURRENCY file is re-read every loop;
client.set_concurrency scales the API semaphore with it); blocks are admitted in seeded order
while (1) the ledger admits the block's worst-case reservation (`emx.spend.reserve_api`, which
counts every open reservation; a refusal waits while other blocks are open, else stops), (2) the
projected p90 block finish (measured p90 block time, else the plan's 15-min estimate, scaled
by 1 + queued-beyond-slots / concurrency) is before ADMIT_BY_ET, (3) there is no STOP file, and no stop
condition (API error fraction > 5%, an orphaned p49- container, the cap) has fired. Every 60 s
of clock time: the laptop's load (halve concurrency above LOAD_HALVE, at most once per 5 min,
and re-run the N rule for blocks not yet started) and the client's failure stats. At
HARD_STOP_ET running chains are cancelled (run_chain also gets the deadline, so it normally
ends itself first) and recorded as deadline-censored; chains never started are recorded the
same way with error "not_started".

Spend per block: reserve -> mark_submitted before the first call -> settle with the billed
total of the block's requests (client.billed_total over its chain ids and its summary id, minus
what earlier runs of the same block already settled). On restart, open block reservations are
released (never submitted), settled from the checkpoints (when they show spend) or settled at
the full reservation (`recover_uncertain`).

Every dependency is injected (`Deps`), so the tests run the scheduler with fakes and a fake
clock; `real_deps()` binds the real modules lazily.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import random
import subprocess
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from p49 import contracts as C

ET = ZoneInfo("America/New_York")
DEFAULT_BLOCK_S = 15 * 60       # plan estimate: R's 24-turn chain ~12 min + beliefs, sandbox
MONITOR_S = 60.0
HALVE_COOLDOWN_S = 300.0
HARD_STOP_GRACE_S = 30.0
MIN_REQUESTS_FOR_ERROR_STOP = 40
MIN_429_CONCURRENCY = 12       # a TPM quota caps throughput; fewer chains only starves it
REPEATED_429 = 3                # new 429s within one monitor interval -> halve
NOT_STARTED = "not_started"
SIGNAL_STOP = "signal stop"     # SIGINT/SIGTERM: chains cancelled by the operator, rerun on resume
SUMMARY_SUFFIX = "-summary"
COMP_NAMES = ("codex", "pi", "inspect", "inspect120", "rep2", "rep3")
COMP_METHOD = {"codex": "codex", "pi": "pi", "inspect": "inspect", "inspect120": "inspect",
               "rep2": "inspect", "rep3": "inspect"}
COMP_K = {"rep2": 2, "rep3": 3}
COMP_SOURCE = {"inspect120": "S1", "rep2": "rep2", "rep3": "rep3"}
COMP_PARENT = {"rep2": "inspect", "rep3": "rep2"}
ARM_COMP = {C.CODEX: "codex", C.PI: "pi", C.INSPECT: "inspect", C.INSPECT120: "inspect120",
            C.REP2: "rep2", C.REP3: "rep3"}


# --- small helpers ---------------------------------------------------------------------------
def utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts).astimezone(
        ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def et_epoch(hhmm: str, now: float) -> float:
    """Epoch of HH:MM America/New_York on the ET date of `now`."""
    h, m = (int(x) for x in hhmm.split(":"))
    d = datetime.fromtimestamp(now, ET)
    return d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def to_dict(obj) -> dict:
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else dict(obj)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def task_order(task_ids: list[str], seed: int = C.SEED) -> list[str]:
    ids = sorted(task_ids)
    random.Random(seed).shuffle(ids)
    return ids


def chain_id(history_id: str, task_id: str, arm: str, level: str) -> str:
    return f"{history_id}-{task_id}-{arm}-{level}"


def history_id(i: int) -> str:
    return f"h{i}"


def transition(level: str) -> dict:
    return {"role": "user", "content": C.transition_text(level)}


def start_messages(arm: str, history, level: str, comps: dict) -> list[dict]:
    """Fresh = [system, transition]; R = H + transition; compacted = carry_over + transition."""
    t = transition(level)
    if arm == C.FRESH:
        return [history.messages[0], t]
    if arm == C.R:
        return list(history.messages) + [t]
    return list(comps[ARM_COMP[arm]].carry_over) + [t]


def comps_needed(arms) -> list[str]:
    need: list[str] = []
    for a in arms:
        n = ARM_COMP.get(a)
        while n:
            if n not in need:
                need.append(n)
            n = COMP_PARENT.get(n)
    return [n for n in COMP_NAMES if n in need]


def history_from_dict(d: dict) -> C.History:
    d = dict(d)
    d["band"] = tuple(d.get("band") or C.BAND)
    return C.History(**d)


def comp_from_dict(d: dict) -> C.CompactionResult:
    names = {f.name for f in dataclasses.fields(C.CompactionResult)}
    return C.CompactionResult(**{k: v for k, v in d.items() if k in names})


def chain_summary(rec: dict, extra: dict | None = None) -> dict:
    """The committed line: no transcript, snapshots, tool calls or final file."""
    out = {k: v for k, v in rec.items()
           if k not in ("transcript", "snapshots", "tool_calls", "final_solution_py", "spec")}
    out.update(rec.get("spec") or {})
    out["n_tool_calls"] = len(rec.get("tool_calls") or [])
    out["n_snapshots"] = len(rec.get("snapshots") or [])
    out.update(extra or {})
    return out


def censored_record(spec: C.ChainSpec, started: str, ended: str, error: str,
                    end_reason: str = C.END_DEADLINE, cost: float = 0.0) -> dict:
    return to_dict(C.ChainRecord(
        spec=spec, end_reason=end_reason, n_turns=0, calls=[], tool_calls=[], snapshots=[],
        final_solution_py=None, beliefs={}, transcript=[], start_tokens=0,
        peak_prompt_tokens=0, cost_usd=cost, started_at=started, ended_at=ended, error=error))


# --- calibration (persisted across processes: smoke, gate0, decide, bulk) --------------------
def calibration_path() -> Path:
    return C.RESULTS / "calibration.json"


def load_calibration(path: Path | None = None) -> dict | None:
    """C.CALIB.c = max(C_INIT, saved c, current c); n and the ratio tail restored."""
    path = calibration_path() if path is None else path
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    C.CALIB.c = max(C.C_INIT, float(d.get("c") or C.C_INIT), C.CALIB.c)
    C.CALIB.n = max(C.CALIB.n, int(d.get("n") or 0))
    if not C.CALIB.ratios:
        C.CALIB.ratios = [float(x) for x in d.get("ratios") or []]
    return d


def save_calibration(path: Path | None = None) -> dict:
    path = calibration_path() if path is None else path
    prev = json.loads(path.read_text()) if path.exists() else {}
    c = max(C.CALIB.c, float(prev.get("c") or C.C_INIT))
    d = {"utc": utc(), "c": c, "n": max(C.CALIB.n, int(prev.get("n") or 0)),
         "ratios": (C.CALIB.ratios or prev.get("ratios") or [])[-200:]}
    write_json(path, d)
    return d


def history_fits(h: C.History, band: tuple[int, int], c: float | None = None) -> bool:
    """Provider-counted length (our count x c) inside the band."""
    c = C.CALIB.c if c is None else c
    return band[0] <= h.tokens * c <= band[1]


# --- configuration -----------------------------------------------------------------------------
@dataclass
class Paths:
    chains: Path
    chain_summary: Path
    compactions: Path
    histories: Path
    blocks: Path
    run_log: Path
    state: Path
    calibration: Path | None = None

    @classmethod
    def from_contracts(cls) -> Paths:
        return cls(chains=C.CHAINS_DIR, chain_summary=C.CHAIN_SUMMARY_JSONL,
                   compactions=C.COMPACTIONS_DIR, histories=C.HISTORIES_DIR,
                   blocks=C.RESULTS / "blocks.jsonl", run_log=C.RESULTS / "run.log",
                   state=C.RESULTS / "scheduler_state.json",
                   calibration=calibration_path())


@dataclass
class BlockSpec:
    index: int
    design: str
    levels: tuple[str, ...]
    stage: str                                   # "gate0" / "bulk" / "dry"
    band: tuple[int, int] = C.BAND
    extra_comps: tuple[str, ...] = ()            # gate0: ("rep2", "rep3"), no chains

    @property
    def history_id(self) -> str:
        return history_id(self.index)

    @property
    def beliefs(self) -> bool:
        return self.design != "minimum_nobelief"

    def n_chains(self) -> int:
        return len(C.DESIGNS[self.design]) * len(self.levels)

    def comp_names(self) -> list[str]:
        names = comps_needed(C.DESIGNS[self.design])
        for n in comps_needed(tuple(a for a, c in ARM_COMP.items() if c in self.extra_comps)):
            if n not in names:
                names.append(n)
        return [n for n in COMP_NAMES if n in names]

    def reserve_usd(self) -> float:
        return round(self.n_chains() * C.CHAIN_CAP_USD
                     + len(self.comp_names()) * C.SUMMARY_RESERVE_USD, 6)


@dataclass
class Deps:
    build_history: Callable          # (seed, *, band, exclude_s1) -> History (sync)
    compact: Callable                # async compact.build(method, messages, tools, summarize, ...)
    extend: Callable                 # replay.extend(carry, tools, *, history, k, used, seed, band)
    run_chain: Callable              # async loop.run_chain(spec, start, tools, task, *, deadline)
    chat: Callable                   # async client.chat(body, *, kind, chain_id, our_tokens)
    billed_total: Callable           # (ids) -> float
    failure_stats: Callable          # () -> dict
    set_concurrency: Callable        # (n) -> None
    tasks: list                      # x3 TaskSpecs (15)
    spend: object                    # emx.spend (module or a stand-in with the same functions)
    clock: Callable = time.time
    load: Callable = None            # () -> float (1-min load average)
    containers: Callable = None      # () -> list[str] of p49 container names
    log_stdout: bool = True
    liability_total: Callable | None = None   # (ids) -> {billed, unresolved_bound, n_unresolved,
    #                                            total}; None -> settle at the full reservation


def uptime_load() -> float:
    out = subprocess.run(["uptime"], capture_output=True, text=True, timeout=10, check=False).stdout
    tail = out.rsplit("load average", 1)[-1].replace("s:", ":").lstrip(":").strip()
    return float(tail.replace(",", " ").split()[0])


def docker_p49() -> list[str]:
    out = subprocess.run(["docker", "ps", "--filter", f"label={C.SANDBOX_LABEL}", "--format",
                          "{{.Names}}"], capture_output=True, text=True, timeout=30, check=False)
    return [x for x in out.stdout.split() if x]


def real_deps() -> Deps:
    """Bind the real modules (imported here, lazily: they may not exist when tests run)."""
    from emx import spend
    from p49 import client, compact, loop, replay, stitch
    from x3 import tasks as xt

    return Deps(build_history=stitch.build_history, compact=compact.build, extend=replay.extend,
                run_chain=loop.run_chain, chat=client.chat,
                billed_total=lambda ids: client.billed_total(chain_ids=set(ids)),
                failure_stats=client.failure_stats, set_concurrency=client.set_concurrency,
                tasks=list(xt.load_manifest().tasks), spend=spend, load=uptime_load,
                containers=docker_p49,
                liability_total=(None if not hasattr(client, "liability_total") else
                                 lambda ids: client.liability_total(chain_ids=set(ids))))


class HistoryStore:
    """Histories on disk (HISTORIES_DIR/h<i>.json), built on demand with exclusive S1s: history
    i excludes the S1 of every other history (j < i, and any j > i already on disk). A history
    on disk at another band, or whose length x the current calibration c falls outside its
    band, is moved aside (h<i>.band<lo>.json / h<i>.c<c>.json) and rebuilt, unless `keep` says
    chains already used it (then it is kept and the mismatch logged)."""

    def __init__(self, hist_dir: Path, build: Callable, log: Callable = print):
        self.dir, self.build, self.log = Path(hist_dir), build, log
        self.cache: dict[int, C.History] = {}
        self.s1: dict[int, str] = {}

    def _ok(self, h: C.History, band: tuple[int, int], keep: bool) -> bool:
        if tuple(h.band) != band:
            if keep:
                self.log(f"{h.history_id}: band {tuple(h.band)} != {band}; kept (chains exist)")
            return keep
        if history_fits(h, band):
            return True
        msg = (f"{h.history_id}: {h.tokens} x c {C.CALIB.c:.4f} = {h.tokens * C.CALIB.c:.0f} "
               f"is outside {band}")
        self.log(msg + ("; kept (chains exist)" if keep else "; rebuilding"))
        return keep

    def get(self, i: int, band: tuple[int, int] = C.BAND, keep: bool = False) -> C.History:
        band = tuple(band)
        if i in self.cache and self._ok(self.cache[i], band, keep):
            return self.cache[i]
        self.cache.pop(i, None)
        p = self.dir / f"{history_id(i)}.json"
        if p.exists():
            h = history_from_dict(json.loads(p.read_text()))
            if self._ok(h, band, keep):
                self.cache[i] = h
                self.s1[i] = h.sessions[0]
                return h
            tag = (f"band{h.band[0]}" if tuple(h.band) != band else f"c{C.CALIB.c:.4f}")
            p.rename(p.with_name(f"{p.stem}.{tag}.json"))
        self.s1.pop(i, None)
        exclude = tuple(self.s1_of(j, band) for j in range(i)) + self._later_s1(i)
        h = self.build(i, band=band, exclude_s1=exclude)
        write_json(p, to_dict(h))
        self.cache[i] = h
        self.s1[i] = h.sessions[0]
        return h

    def _later_s1(self, i: int) -> tuple[str, ...]:
        out = []
        for q in sorted(self.dir.glob("h*.json")):
            if "." in q.stem or not q.stem[1:].isdigit() or int(q.stem[1:]) <= i:
                continue
            out.append(json.loads(q.read_text())["sessions"][0])
        return tuple(out)

    def s1_of(self, j: int, band: tuple[int, int]) -> str:
        """S1 of history j, from disk whatever its band (a gate0 history is never rebuilt
        to exclude its S1 from a reduced-band bulk history); built at `band` if missing."""
        if j not in self.s1:
            p = self.dir / f"{history_id(j)}.json"
            if p.exists():
                self.s1[j] = json.loads(p.read_text())["sessions"][0]
            else:
                self.get(j, band)
        return self.s1[j]


# --- the scheduler -----------------------------------------------------------------------------
@dataclass
class _Chain:
    spec: C.ChainSpec
    block: _Block
    start: list[dict] | None = None
    task: object = None
    tools: list | None = None
    started: float | None = None
    ended: float | None = None


@dataclass
class _Block:
    spec: BlockSpec
    task_id: str
    chains: dict[str, _Chain] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)        # chain ids not finished
    unready: set[str] = field(default_factory=set)        # waiting on a compaction
    rid: str | None = None
    submitted: bool = False
    t0: float = 0.0
    comp_times: dict = field(default_factory=dict)
    replay_info: dict = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "running"


class Scheduler:
    def __init__(self, deps: Deps, paths: Paths | None = None, *, concurrency: int | None = None,
                 admit_by: float | None = None, hard_stop: float | None = None,
                 tick_s: float = 2.0, monitor_s: float = MONITOR_S,
                 block_estimate_s: float = DEFAULT_BLOCK_S, n_rule: Callable | None = None):
        self.d = deps
        self.p = paths or Paths.from_contracts()
        now = deps.clock()
        self.base_conc = C.CONCURRENCY if concurrency is None else concurrency
        self.conc_cap: int | None = None                  # set by load halving
        self.admit_by = et_epoch(C.ADMIT_BY_ET, now) if admit_by is None else admit_by
        self.hard_stop = et_epoch(C.HARD_STOP_ET, now) if hard_stop is None else hard_stop
        self.tick_s, self.monitor_s = tick_s, monitor_s
        self.grace_s = HARD_STOP_GRACE_S
        self.signalled = False
        self.block_estimate_s = block_estimate_s
        self.n_rule = n_rule or self.default_n_rule
        self.order = task_order([t.task_id for t in deps.tasks])
        self.task_by_id = {t.task_id: t for t in deps.tasks}
        self.ready: deque[_Chain] = deque()
        self.in_flight: dict[asyncio.Task, _Chain] = {}
        self.blocks: dict[str, _Block] = {}
        self.block_tasks: dict[asyncio.Task, _Block] = {}
        self.stop_reason: str | None = None
        self.budget_wait_logged = False
        self.last_monitor = -1e18
        self.last_halve = -1e18
        self.last_429 = 0
        self.last_docker: int | None = None
        self.bg: set[asyncio.Task] = set()
        self.suspect: set[str] = set()
        self.recent_ended: dict[str, float] = {}
        self.eff: int | None = None
        self.store = HistoryStore(self.p.histories, deps.build_history, log=self.log)
        self.hist_lock = asyncio.Lock()
        self.events: list[str] = []
        self.walls: list[float] = [float(b["wall_s"]) for b in read_jsonl(self.p.blocks)
                                   if b.get("status") == "complete" and b.get("wall_s")]

    # --- logging and state ---
    def log(self, msg: str) -> None:
        line = f"{utc(self.d.clock())} {msg}"
        self.events.append(msg)
        self.p.run_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.p.run_log, "a") as f:
            f.write(line + "\n")
        if self.d.log_stdout:
            print(line, flush=True)

    def task_for(self, i: int) -> str:
        return self.order[i % len(self.order)]

    def chain_path(self, cid: str) -> Path:
        return self.p.chains / f"{cid}.json"

    def comp_path(self, hid: str, name: str) -> Path:
        return self.p.compactions / hid / f"{name}_k{COMP_K.get(name, 1)}.json"

    def chain_done(self, cid: str) -> bool:
        p = self.chain_path(cid)
        if not p.exists():
            return False
        try:
            rec = json.loads(p.read_text())
        except ValueError:
            return False
        if rec.get("end_reason") == C.END_API:
            # infrastructure failure (Vertex TPM 429s past the retries, log 06:03Z): rerun the
            # chain in full, whatever it had shown, so the rerun can't select on outcome; the
            # failed record is kept beside it
            n = len(list(p.parent.glob(f"{cid}.api_error*.json")))
            p.rename(p.with_name(f"{cid}.api_error{n + 1}.json"))
            return False
        if rec.get("error") == SIGNAL_STOP:
            n = len(list(p.parent.glob(f"{cid}.signal*.json")))
            p.rename(p.with_name(f"{cid}.signal{n + 1}.json"))
            return False
        return rec.get("error") != NOT_STARTED

    def specs(self, b: BlockSpec) -> list[C.ChainSpec]:
        tid = self.task_for(b.index)
        return [C.ChainSpec(chain_id=chain_id(b.history_id, tid, arm, lv),
                            history_id=b.history_id, task_id=tid, arm=arm, level=lv,
                            design=b.design, beliefs=b.beliefs)
                for arm in C.DESIGNS[b.design] for lv in b.levels]

    def block_complete(self, b: BlockSpec) -> bool:
        return (all(self.chain_done(s.chain_id) for s in self.specs(b))
                and all(self.comp_path(b.history_id, n).exists() for n in b.extra_comps))

    def p90_block_s(self) -> float:
        if not self.walls:
            return self.block_estimate_s
        xs = sorted(self.walls)
        return xs[min(len(xs) - 1, round(0.9 * (len(xs) - 1)))]

    def write_state(self) -> None:
        write_json(self.p.state, {
            "utc": utc(self.d.clock()), "concurrency": self.eff, "base": self.base_conc,
            "cap": self.conc_cap, "in_flight": sorted(c.spec.chain_id
                                                      for c in self.in_flight.values()),
            "ready": len(self.ready), "blocks_running": sorted(self.blocks),
            "stop_reason": self.stop_reason, "admit_by": utc(self.admit_by),
            "hard_stop": utc(self.hard_stop), "p90_block_s": self.p90_block_s()})

    # --- concurrency, monitoring ---
    def refresh_concurrency(self) -> None:
        conc = self.base_conc
        try:
            conc = max(0, int(C.CONCURRENCY_FILE.read_text().strip()))
        except (OSError, ValueError):
            pass
        if self.conc_cap is not None:
            conc = min(conc, self.conc_cap)
        if conc != self.eff:
            if self.eff is not None:
                self.log(f"concurrency {self.eff} -> {conc}")
            self.eff = conc
            api = max(1, round(C.API_CONCURRENCY * conc / max(1, C.CONCURRENCY)))
            try:
                self.d.set_concurrency(api)
            except Exception as e:  # noqa: BLE001 - logged, the chain limit still holds
                self.log(f"set_concurrency({api}) failed: {e!r}")

    def default_n_rule(self, queue: deque, conc: int, now: float) -> deque:
        """Keep the not-started blocks that fit by ADMIT_BY at this concurrency: blocks in
        parallel = conc // chains per block, waves of the p90 block time."""
        if not queue:
            return queue
        par = max(1, conc // max(1, queue[0].n_chains()))
        waves = max(0, int((self.admit_by - now) // max(1.0, self.p90_block_s())))
        keep = waves * par
        if keep < len(queue):
            dropped = [b.history_id for b in list(queue)[keep:]]
            self.log(f"N rule at concurrency {conc}: {len(queue)} -> {keep} blocks not yet "
                     f"started (dropped {', '.join(dropped)})")
            return deque(list(queue)[:keep])
        return queue

    async def monitor(self, queue: deque) -> deque:
        now = self.d.clock()
        self.last_monitor = now
        if self.d.load is not None:
            try:
                load = float(await asyncio.wait_for(asyncio.to_thread(self.d.load), 20))
                self.log(f"load {load:.1f}, concurrency {self.eff}, in flight "
                         f"{len(self.in_flight)}, ready {len(self.ready)}, blocks "
                         f"{len(self.blocks)}")
                if load > C.LOAD_HALVE and now - self.last_halve >= HALVE_COOLDOWN_S:
                    new = max(1, (self.eff or self.base_conc) // 2)
                    self.conc_cap = new
                    self.last_halve = now
                    self.log(f"load {load:.1f} > {C.LOAD_HALVE}: halving concurrency to {new}")
                    self.refresh_concurrency()
                    queue = self.n_rule(queue, new, now)
            except Exception as e:  # noqa: BLE001
                self.log(f"load check failed: {e!r}")
        try:
            st = self.d.failure_stats() or {}
        except Exception as e:  # noqa: BLE001
            st = {}
            self.log(f"failure_stats failed: {e!r}")
        n = st.get("requests") or 0
        frac = st.get("frac")
        if frac is None and n:
            frac = (st.get("errors") or 0) / n
        statuses = st.get("statuses") or {}
        n429 = int(statuses.get("429") or statuses.get(429) or 0)
        if n429 - self.last_429 >= REPEATED_429 and now - self.last_halve >= HALVE_COOLDOWN_S:
            new = max(min(MIN_429_CONCURRENCY, self.eff or self.base_conc),
                      (self.eff or self.base_conc) // 2)
            self.conc_cap, self.last_halve = new, now
            self.log(f"{n429 - self.last_429} new 429s since the last check: halving "
                     f"concurrency to {new}")
            self.refresh_concurrency()
            queue = self.n_rule(queue, new, now)
        self.last_429 = n429
        if frac is not None and n >= MIN_REQUESTS_FOR_ERROR_STOP and frac > C.ERROR_STOP_FRAC \
                and self.stop_reason is None:
            self.stop(f"API/parse error fraction {frac:.3f} > {C.ERROR_STOP_FRAC} ({n} requests)")
        await self.check_containers()
        self.write_state()
        return queue

    async def check_containers(self, why: str = "") -> int | None:
        """`docker ps` off the event loop (bounded), then the orphan rule: a p49- container
        not belonging to a running (or just-ended) chain, seen in two checks in a row."""
        if self.d.containers is None:
            return None
        try:
            names = list(await asyncio.wait_for(asyncio.to_thread(self.d.containers), 45))
        except Exception as e:  # noqa: BLE001
            self.log(f"docker ps failed: {e!r}")
            return None
        self.last_docker = len(names)
        if why:
            self.log(f"{why}: docker p49 containers {len(names)}")
        now = self.d.clock()
        live = {c.spec.chain_id for c in self.in_flight.values()}
        live |= {cid for cid, t in self.recent_ended.items() if now - t < 60}
        orphans = set()
        for n in names:
            if not n.startswith(C.SANDBOX_PREFIX) or n.startswith("p49-mock"):
                continue
            rest = n[len(C.PASS_PREFIX):] if n.startswith(C.PASS_PREFIX) else n[
                len(C.SANDBOX_PREFIX):]
            if not any(rest.startswith(cid) for cid in live):
                orphans.add(n)
        confirmed = orphans & self.suspect
        self.suspect = orphans - confirmed
        if confirmed and self.stop_reason is None:
            self.stop(f"orphaned container(s): {', '.join(sorted(confirmed))}")
        return len(names)

    def stop(self, why: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = why
            self.log(f"STOP admitting: {why}")

    # --- histories and compactions ---
    async def history(self, b: BlockSpec) -> C.History:
        async with self.hist_lock:
            used = any(self.p.chains.glob(f"{b.history_id}-*.json")) or (
                self.p.compactions / b.history_id).exists()
            return await asyncio.to_thread(self.store.get, b.index, tuple(b.band), used)

    def summarizer(self, hid: str):
        async def summarize(messages, max_tokens, tools, our_tokens):
            body = C.make_body(messages, tools, max_tokens)
            return await self.d.chat(body, kind="summary", chain_id=hid + SUMMARY_SUFFIX,
                                     our_tokens=our_tokens)
        return summarize

    async def compaction(self, blk: _Block, name: str, h: C.History,
                         futs: dict[str, asyncio.Future]) -> C.CompactionResult:
        hid = h.history_id
        path = self.comp_path(hid, name)
        rpath = path.with_suffix(".replay.json")
        if path.exists():
            res = comp_from_dict(json.loads(path.read_text()))
            if not res.error:
                blk.comp_times[name] = {"reused": True}
                if rpath.exists():
                    blk.replay_info[name] = json.loads(rpath.read_text())
                return res
        k = COMP_K.get(name, 1)
        t0 = self.d.clock()
        info = None
        if name in COMP_PARENT:
            parent = await futs[COMP_PARENT[name]]
            if parent.error:
                raise RuntimeError(f"{name}: parent {COMP_PARENT[name]} failed: {parent.error}")
            used = set(h.instances)
            for v in blk.replay_info.values():
                used |= set(v.get("instances") or [])
            out = await asyncio.to_thread(
                self.d.extend, parent.carry_over, h.tools, history=h, k=k, used_instances=used,
                seed=h.seed * 100 + k, band=tuple(blk.spec.band))
            messages, info = out
            if (info or {}).get("infeasible"):
                raise RuntimeError(f"{name}: replay infeasible: {info}")
            blk.replay_info[name] = info or {}
            write_json(rpath, info or {})
            msgs = messages
        elif name == "inspect120":
            msgs = h.s1_messages()
        else:
            msgs = h.messages
        res = await self.d.compact(COMP_METHOD[name], msgs, h.tools, self.summarizer(hid),
                                   history_id=hid, k=k, source=COMP_SOURCE.get(name, "H"))
        t1 = self.d.clock()
        blk.comp_times[name] = {"latency_s": round(t1 - t0, 3), "cost_usd": res.cost_usd,
                                "input_tokens": res.input_tokens, "carry_tokens":
                                res.carry_tokens, "summary_tokens": res.summary_tokens,
                                "error": res.error, "replay": info}
        write_json(path, to_dict(res))
        self.log(f"{hid}: compaction {name} k={k} done in {t1 - t0:.1f}s "
                 f"(${res.cost_usd:.4f}{', error ' + res.error if res.error else ''})")
        return res

    # --- one block ---
    async def run_block(self, blk: _Block) -> None:
        b = blk.spec
        hid = b.history_id
        try:
            h = await self.history(b)
        except Exception as e:  # noqa: BLE001 - the block fails, others continue
            self.log(f"{hid}: history build failed: {e!r}")
            for c in list(blk.chains.values()):
                self.finish_unstarted(c, f"history failed: {e!r}", C.END_ERROR)
            await blk.done.wait()
            return
        task = self.task_by_id[blk.task_id]
        names = b.comp_names()
        futs: dict[str, asyncio.Future] = {}
        loop = asyncio.get_running_loop()
        for n in names:
            futs[n] = loop.create_future()

        async def one(n):
            try:
                r = await self.compaction(blk, n, h, futs)
                futs[n].set_result(r)
                self.on_compaction(blk, n, r, h, task)
            except asyncio.CancelledError:
                futs[n].cancel()
                raise
            except Exception as e:  # noqa: BLE001
                self.log(f"{hid}: compaction {n} failed: {e!r}")
                blk.comp_times[n] = {"error": repr(e)}
                futs[n].set_result(C.CompactionResult(
                    method=COMP_METHOD[n], k=COMP_K.get(n, 1), history_id=hid,
                    source=COMP_SOURCE.get(n, "H"), input_tokens=0, prompt_sha256="",
                    request_sha256="", summary_text="", summary_tokens=None, carry_over=[],
                    carry_tokens=0, error=repr(e)))
                self.on_compaction(blk, n, futs[n].result(), h, task)

        comp_tasks = [asyncio.create_task(one(n)) for n in names]
        for c in blk.chains.values():
            if c.spec.arm in (C.FRESH, C.R):
                self.enqueue(c, start_messages(c.spec.arm, h, c.spec.level, {}), h, task)
        try:
            await blk.done.wait()
            await asyncio.gather(*comp_tasks)        # gate0's chainless replay compactions
        finally:
            for t in comp_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*comp_tasks, return_exceptions=True)

    def on_compaction(self, blk: _Block, n: str, r: C.CompactionResult, h: C.History,
                      task) -> None:
        if self.stop_reason == "hard stop":
            return
        for c in blk.chains.values():
            if ARM_COMP.get(c.spec.arm) != n or c.spec.chain_id not in blk.pending:
                continue
            if r.error:
                self.finish_unstarted(c, f"compaction {n} failed: {r.error}", C.END_ERROR)
            else:
                self.enqueue(c, start_messages(c.spec.arm, h, c.spec.level, {n: r}), h, task)

    def enqueue(self, c: _Chain, start: list[dict], h: C.History, task) -> None:
        c.start = start
        c.task = task
        c.block.unready.discard(c.spec.chain_id)
        c.tools = h.tools
        self.ready.append(c)

    def launch(self, c: _Chain) -> None:
        c.started = self.d.clock()
        t = asyncio.create_task(self.d.run_chain(c.spec, c.start, c.tools, c.task,
                                                 deadline_epoch=self.hard_stop))
        self.in_flight[t] = c

    def save_chain(self, c: _Chain, rec: dict) -> None:
        write_json(self.chain_path(c.spec.chain_id), rec)
        wall = None if c.started is None else round((c.ended or self.d.clock()) - c.started, 3)
        append_jsonl(self.p.chain_summary, chain_summary(rec, {
            "stage": c.block.spec.stage, "wall_s": wall}))
        self.recent_ended[c.spec.chain_id] = self.d.clock()
        blk = c.block
        blk.pending.discard(c.spec.chain_id)
        blk.unready.discard(c.spec.chain_id)
        if not blk.pending:
            blk.done.set()

    def finish_unstarted(self, c: _Chain, error: str, end_reason: str) -> None:
        now = utc(self.d.clock())
        self.save_chain(c, censored_record(c.spec, now, now, error, end_reason))
        self.log(f"{c.spec.chain_id}: not run ({error})")

    def chain_finished(self, t: asyncio.Task, c: _Chain) -> None:
        c.ended = self.d.clock()
        try:
            rec = to_dict(t.result())      # loop.run_chain returns END_DEADLINE when cancelled
        except asyncio.CancelledError:
            cost = self._billed([c.spec.chain_id])
            rec = censored_record(c.spec, utc(c.started), utc(c.ended),
                                  "cancelled at the hard stop", C.END_DEADLINE, cost)
        except Exception as e:  # noqa: BLE001 - the chain is recorded as an error
            if type(e).__name__ == "SandboxLeak":
                # p49.shim: a container not confirmed removed -> an orphan: stop admitting
                names = ", ".join(getattr(e, "names", ()) or ()) or "?"
                self.stop(f"orphaned container(s) from {c.spec.chain_id}: {names} ({e})")
                if getattr(e, "record", None) is not None:
                    self.save_chain(c, to_dict(e.record))
                    self.log(f"{c.spec.chain_id}: record saved from SandboxLeak")
                    return
            cost = self._billed([c.spec.chain_id])
            rec = censored_record(c.spec, utc(c.started), utc(c.ended), repr(e)[:2000],
                                  C.END_ERROR, cost)
        if self.signalled and rec.get("end_reason") == C.END_DEADLINE:
            rec["error"] = SIGNAL_STOP           # rerun on resume (chain_done)
        self.save_chain(c, rec)
        self.log(f"{c.spec.chain_id}: {rec.get('end_reason')} turns={rec.get('n_turns')} "
                 f"${float(rec.get('cost_usd') or 0):.4f} wall {c.ended - c.started:.1f}s")

    def _liability(self, ids: list[str]) -> dict | None:
        if self.d.liability_total is None:
            return None
        try:
            d = dict(self.d.liability_total(ids))
            for k in ("billed", "unresolved_bound", "n_unresolved", "total"):
                d.setdefault(k, 0)
            return d
        except Exception as e:  # noqa: BLE001 - unknown liability -> full reservation
            self.log(f"liability_total failed: {e!r}")
            return None

    def _billed(self, ids: list[str]) -> float:
        try:
            return float(self.d.billed_total(ids) or 0.0)
        except Exception as e:  # noqa: BLE001
            self.log(f"billed_total failed: {e!r}")
            return 0.0

    def block_ids(self, b: BlockSpec) -> list[str]:
        return [s.chain_id for s in self.specs(b)] + [b.history_id + SUMMARY_SUFFIX]

    def prior_settled(self, note: str) -> float:
        sp = self.d.spend
        states = sp.reservation_states(sp.read_entries())
        return round(sum(s["charges"] for s in states.values()
                         if s["exp"] == C.EXP and s["note"] == note and s["closed"]), 6)

    def finish_block(self, blk: _Block) -> None:
        b = blk.spec
        hid = b.history_id
        t1 = self.d.clock()
        settled = None
        if blk.rid is not None:
            rec_sum = sum(float(json.loads(self.chain_path(c).read_text()).get("cost_usd") or 0)
                          for c in blk.chains if self.chain_path(c).exists())
            rec_sum += sum(float(v.get("cost_usd") or 0) for v in blk.comp_times.values()
                           if not v.get("reused"))
            note = self.note(b)
            liab = self._liability(self.block_ids(b))
            if liab is None:
                st = self.d.spend.settle_api(blk.rid, None, exp=C.EXP)
                self.log(f"{hid}: liability unknown: settled at the full reservation "
                         f"${b.reserve_usd():.2f} (records ${rec_sum:.4f})")
            else:
                total = max(0.0, float(liab["total"]) - self.prior_settled(note), rec_sum)
                st = self.d.spend.settle_api(blk.rid, round(total, 6), exp=C.EXP)
                self.log(f"{hid}: settled ${total:.4f} (billed ${liab['billed']:.4f}, "
                         f"unresolved bound ${liab['unresolved_bound']:.4f} over "
                         f"{liab['n_unresolved']} request(s), records ${rec_sum:.4f}) of "
                         f"${b.reserve_usd():.2f} reserved")
            settled = st.get("settled_usd")
        if self.p.calibration is not None:
            save_calibration(self.p.calibration)
        task = asyncio.get_running_loop().create_task(
            self.check_containers(f"{hid} block end"))
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)
        n_docker = self.last_docker
        wall = round(t1 - blk.t0, 3)
        if blk.status in ("running", "cancelled"):
            blk.status = "complete" if self.block_complete(b) else (
                "partial" if blk.status == "running" else "cancelled")
        rec = {"history_id": hid, "task_id": blk.task_id, "design": b.design,
               "levels": list(b.levels), "stage": b.stage, "band": list(b.band),
               "started_at": utc(blk.t0), "ended_at": utc(t1), "wall_s": wall,
               "compactions": blk.comp_times, "replay": blk.replay_info,
               "chains": {cid: {"wall_s": None if c.started is None else
                                round((c.ended or t1) - c.started, 3),
                                "queued": c.started is not None}
                          for cid, c in blk.chains.items()},
               "reservation_id": blk.rid, "reserved_usd": b.reserve_usd(),
               "settled_usd": settled, "docker_p49_last": n_docker,
               "calibration_c": C.CALIB.c, "status": blk.status}
        append_jsonl(self.p.blocks, rec)
        if blk.status == "complete":
            self.walls.append(wall)
        self.log(f"{hid}: block {blk.status} in {wall:.1f}s")

    def note(self, b: BlockSpec) -> str:
        return f"block:{b.history_id}:{b.stage}"

    # --- admission ---
    def committed(self) -> int:
        """Chains of admitted blocks not yet finished: running, queued or waiting on a
        compaction (counted from admission, before the block task has queued anything)."""
        return sum(len(b.pending) for b in self.blocks.values())

    def try_admit(self, b: BlockSpec) -> str:
        """'admitted' / 'skip' (already complete) / 'wait' / 'stop'."""
        if self.block_complete(b):
            self.log(f"{b.history_id}: complete on disk, skipped")
            return "skip"
        now = self.d.clock()
        p90 = self.p90_block_s()
        # queue wait: chains already committed beyond the free slots delay this block's start
        eff = max(1, self.eff or 1)
        p90 *= 1 + max(0, self.committed() + b.n_chains() - eff) / eff
        if now + p90 > self.admit_by:
            self.stop(f"projected p90 finish {utc(now + p90)} is past the admit-by time "
                      f"{utc(self.admit_by)}")
            return "stop"
        usd = b.reserve_usd()
        sp = self.d.spend
        try:
            sp.check_total(usd, "judge", exp=C.EXP)
            rid = sp.reserve_api(C.EXP, usd, note=self.note(b))
        except SystemExit as e:                       # BudgetExceeded
            if self.blocks:
                if not self.budget_wait_logged:
                    self.log(f"{b.history_id}: budget refuses ${usd:.2f} while "
                             f"{len(self.blocks)} block(s) are open; waiting ({e})")
                    self.budget_wait_logged = True
                return "wait"
            self.stop(f"budget: {e}")
            return "stop"
        self.budget_wait_logged = False
        sp.mark_submitted(rid, exp=C.EXP)            # before the block's first call
        tid = self.task_for(b.index)
        blk = _Block(spec=b, task_id=tid, rid=rid, submitted=True, t0=now)
        for s in self.specs(b):
            if self.chain_done(s.chain_id):
                continue
            c = _Chain(spec=s, block=blk)
            blk.chains[s.chain_id] = c
            blk.pending.add(s.chain_id)
            if s.arm not in (C.FRESH, C.R):
                blk.unready.add(s.chain_id)
        if not blk.pending:
            blk.done.set()
        self.blocks[b.history_id] = blk
        self.block_tasks[asyncio.create_task(self.run_block(blk))] = blk
        self.log(f"{b.history_id}: admitted ({b.stage}, {b.design}, levels "
                 f"{''.join(b.levels)}, task {tid}, {len(blk.pending)} chains, reserved "
                 f"${usd:.2f}, p90 block {p90:.0f}s)")
        return "admitted"

    def recover(self) -> None:
        """Open block reservations from a crashed run: release, settle from checkpoints, or
        settle at the full reservation."""
        sp = self.d.spend
        for rid, s in sp.open_reservations(C.EXP).items():
            note = s["note"]
            if not note.startswith("block:"):
                continue
            if not s["submitted"]:
                sp.release_api(rid, exp=C.EXP)
                self.log(f"recovered {note}: released (never submitted)")
                continue
            hid = note.split(":")[1]
            ids = [str(p.stem) for p in self.p.chains.glob(f"{hid}-*.json")]
            liab = self._liability(ids + [hid + SUMMARY_SUFFIX] + self._planned_ids(hid))
            if liab is not None and float(liab["total"]) > 0:
                # billed responses + the worst-case bound of requests with no outcome line
                total = max(0.0, float(liab["total"]) - self.prior_settled(note))
                sp.settle_api(rid, round(total, 6), exp=C.EXP,
                              note=f"{note} (recovered from checkpoints)")
                self.log(f"recovered {note}: settled ${total:.4f} from checkpoints (billed "
                         f"${liab['billed']:.4f}, unresolved bound "
                         f"${liab['unresolved_bound']:.4f} over {liab['n_unresolved']})")
            else:
                sp.recover_uncertain(C.EXP, note)
                self.log(f"recovered {note}: settled at the full reservation")

    def _planned_ids(self, hid: str) -> list[str]:
        try:
            i = int(hid[1:])
        except ValueError:
            return []
        tid = self.task_for(i)
        return [chain_id(hid, tid, a, lv) for a in C.ARMS for lv in C.LEVELS]

    # --- hard stop ---
    def on_signal(self) -> None:
        """SIGINT/SIGTERM: a clean stop now (cancel chains, remove containers, settle)."""
        if not self.signalled:
            self.signalled = True
            self.log("signal received: stopping (chains cancelled now are rerun on resume)")
            self.hard_stop = min(self.hard_stop, self.d.clock())
            self.grace_s = 0

    async def do_hard_stop(self) -> None:
        self.log(f"hard stop at {utc(self.hard_stop)}: {len(self.in_flight)} chain(s) running, "
                 f"{len(self.ready)} queued")
        self.stop("hard stop")
        grace_end = self.d.clock() + self.grace_s
        while self.in_flight and self.d.clock() < grace_end:
            await self._wait(self.tick_s)
        for t in list(self.in_flight):
            t.cancel()
        if self.in_flight:
            await asyncio.gather(*self.in_flight, return_exceptions=True)
            self._reap()
        for blk in list(self.blocks.values()):
            for cid in list(blk.pending):
                c = blk.chains[cid]
                if c.started is None:
                    self.finish_unstarted(c, NOT_STARTED, C.END_DEADLINE)
        self.ready.clear()

    def _reap(self) -> None:
        for t in [t for t in self.in_flight if t.done()]:
            c = self.in_flight.pop(t)
            self.chain_finished(t, c)
        for t in [t for t in self.block_tasks if t.done()]:
            blk = self.block_tasks.pop(t)
            if t.cancelled():
                blk.status = "cancelled"
            elif t.exception() is not None:
                self.log(f"{blk.spec.history_id}: block crashed: {t.exception()!r}")
                blk.status = "crashed"
            self.blocks.pop(blk.spec.history_id, None)
            self.finish_block(blk)

    async def _wait(self, timeout: float) -> None:
        tasks = set(self.in_flight) | set(self.block_tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(timeout)
        self._reap()

    # --- main loop ---
    async def run(self, blocks: list[BlockSpec]) -> dict:
        self.log(f"scheduler start: {len(blocks)} block(s), concurrency {self.base_conc}, "
                 f"admit by {utc(self.admit_by)}, hard stop {utc(self.hard_stop)}")
        self.recover()
        self.refresh_concurrency()
        queue = deque(blocks)
        admitted = skipped = 0
        while True:
            now = self.d.clock()
            self.refresh_concurrency()
            if now - self.last_monitor >= self.monitor_s:
                queue = await self.monitor(queue)
            if now >= self.hard_stop:
                await self.do_hard_stop()
                await self._drain_blocks()
                break
            stop_file = C.STOP_FILE.exists()
            if stop_file and self.stop_reason is None and not self.blocks:
                self.stop("STOP file")
            while (queue and self.stop_reason is None and not stop_file
                   and self.committed() < self.eff):
                r = self.try_admit(queue[0])
                if r == "wait":
                    break
                if r == "stop":
                    break
                queue.popleft()
                admitted += r == "admitted"
                skipped += r == "skip"
            while self.ready and len(self.in_flight) < self.eff:
                self.launch(self.ready.popleft())
            if not self.blocks and (not queue or self.stop_reason is not None):
                break
            await self._wait(self.tick_s)
        if self.bg:
            await asyncio.gather(*self.bg, return_exceptions=True)
        self.write_state()
        out = {"admitted": admitted, "skipped": skipped, "not_admitted": [
            b.history_id for b in queue], "stop_reason": self.stop_reason,
            "concurrency": self.eff}
        self.log(f"scheduler end: {out}")
        return out

    async def _drain_blocks(self) -> None:
        """After the hard stop: cancel the block tasks (and their compactions), then settle
        and record every block."""
        for t in list(self.block_tasks):
            t.cancel()
        if self.block_tasks:
            await asyncio.gather(*self.block_tasks, return_exceptions=True)
        self._reap()


def gate0_blocks(indices=range(C.GATE0_HISTORIES), band=C.BAND) -> list[BlockSpec]:
    return [BlockSpec(index=i, design="gate0", levels=C.LEVELS, stage="gate0", band=band,
                      extra_comps=("rep2", "rep3")) for i in indices]


def bulk_blocks(design: str, n: int, level: str, band=C.BAND,
                start: int = C.GATE0_HISTORIES) -> list[BlockSpec]:
    return [BlockSpec(index=i, design=design, levels=(level,), stage="bulk", band=tuple(band))
            for i in range(start, start + n)]


def deadline_for(hhmm: str, now: float | None = None) -> float:
    return et_epoch(hhmm, time.time() if now is None else now)

