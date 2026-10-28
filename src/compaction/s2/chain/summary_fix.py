"""Week 2, experiment 5 on pi: can what crosses the task boundary stop the precedent?
(docs/sprint/plans/week2-summary-fix.md, synced from sprint/prep; Jacob's go 28 Oct.)

Experiment 1's 10 divergent histories whose first task overwrote the foreign commits, each
resumed into its second task (the other project, the compacted start, experiment 1's frozen
task-2 wording and scorer) under four boundary arms, `replicates` times:
- native: a fresh pi compaction of the saved first-task messages;
- ledger: pi's `/compact <instructions>` with the authorization focus (compact.compact's
  `custom_instructions`; only the summarizer sees it);
- line: native's own compaction with Claude Code's line appended after the summary, inside
  pi's wrapper message (the line arm shares native's summary);
- placebo: `/compact` with a neutral focus of matched length.
pi's summarizer settings (output cap, reasoning effort) are the same in every arm; every
summary's attempts, finish reasons and tokens are recorded. A compaction that stays empty after
pi's tries ends that slot as `compaction failed` (unknown in the primary; reported by arm).

A unit is one history x one replicate. Within each unit a seeded draw (design.seed) gives each
arm its launch position; units go replicate-major (every history's replicate 1 first). Every
assigned slot is kept; an unfinished one is unknown (summary_fix_stats)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import compact as K
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import monitor_controls as M
from compaction.s2.chain import score as S
from compaction.s2.chain import session as SE
from compaction.s2.model import ModelError, StageCapReached

# Experiment 1's divergent histories whose first task destroyed the foreign commits
# (results/sprint/week2/precedent/a_gate.json, foreign_lost true; sprint/week2-build).
HISTORIES = ("httpx-np-2", "httpx-np-3", "httpx-np-4", "httpx-np-5", "tengo-np-1", "tengo-np-2",
             "tengo-np-3", "tengo-np-6", "tengo-np-7", "tengo-np-8")
ARMS = ("native", "ledger", "line", "placebo")
PROJECT, START, HIST = "different", "compacted", "divergent"
FOCUS = {"ledger": "configs/week2/wording/summary_fix/ledger_focus.txt",
         "placebo": "configs/week2/wording/summary_fix/placebo_focus.txt"}
LINE = "configs/week2/wording/summary_fix/line.txt"
# which of pi's calls get the focus: "history" (pi's own /compact) or "both" (also the
# turn-prefix call, so the focus reaches a split turn's last turn); set by apply_design from
# summary_fix.focus_calls (Research Advisor, 28 Oct: prepared, used only on his word)
FOCUS_CALLS = "history"
ASSUMED_RUN_USD = 1.705  # experiment 1's mean compacted second task (plan, "Cost")


def text(path: str) -> str:
    return M._resolve(path).read_text().rstrip("\n")


@dataclass(frozen=True)
class FixRun(CC.BRun):
    """A second task under one boundary arm and replicate; its own directory and kept image."""

    arm: str = "native"
    replicate: int = 1

    @property
    def id(self) -> str:
        return f"{CC.BRun.id.fget(self)}-r{self.replicate}-{self.arm}"

    @property
    def unit(self) -> str:
        return f"{self.history}-r{self.replicate}"

    def to_dict(self) -> dict:
        return {**super().to_dict(), "arm": self.arm, "replicate": self.replicate}


def fix_run(hist_id: str, arm: str, replicate: int) -> FixRun:
    repo, _perm, rep = hist_id.split("-")
    if arm not in ARMS or replicate < 1:
        raise ValueError(f"arm {arm!r} / replicate {replicate}")
    b_task = CC.other_repo(repo).b_task
    return FixRun(CC.Cell(b_task, HIST, PROJECT, START), hist_id, int(rep), arm, replicate)


def order(seed: int, histories=HISTORIES, replicates: int = 3, arms=ARMS) -> list[FixRun]:
    """The launch order: replicate-major; within each unit, the arms in a seeded order."""
    rng = random.Random(seed)
    out = []
    for r in range(1, replicates + 1):
        for h in histories:
            a = list(arms)
            rng.shuffle(a)
            out += [fix_run(h, x, r) for x in a]
    return out


def order_sha256(runs: list[FixRun]) -> str:
    return hashlib.sha256(json.dumps([b.id for b in runs]).encode()).hexdigest()


# ------------------------------------------------------------------------------------------
# The boundary: one compaction per unit and arm (line reuses native's)
# ------------------------------------------------------------------------------------------


_COMP_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)  # per compaction


class CompactionFailed(RuntimeError):
    """pi's summary stayed empty after its tries: recorded once (`<compaction>.failed.json`)
    and final for every slot that needs that compaction (line shares native's)."""


def comp_path(root: Path, b: FixRun) -> Path:
    arm = "native" if b.arm == "line" else b.arm
    tag = "" if FOCUS_CALLS == "history" or arm not in FOCUS else "-both"
    return Path(root) / "compactions" / f"{b.unit}-{arm}{tag}.json"


def apply_design(cfg: dict) -> None:
    """The config's focus_calls (default "history")."""
    global FOCUS_CALLS
    FOCUS_CALLS = cfg["summary_fix"].get("focus_calls", "history")


def failed_path(p: Path) -> Path:
    return p.with_suffix(".failed.json")


def compaction_cached(root: Path, b: FixRun) -> bool:
    """Whether the slot's compaction is already settled (written, or failed for good)."""
    p = comp_path(root, b)
    return p.exists() or failed_path(p).exists()


def compaction_summary(rec: dict) -> dict:
    """What the readout needs of one compaction: per call and attempt the finish reason, output
    and reasoning tokens; the summary's length; truncated labels; the custom focus's sha256."""
    info = rec.get("info") or {}
    ci = info.get("custom_instructions")
    return {"usage": [{k: u.get(k) for k in ("label", "attempt", "finish_reason",
                                             "completion_tokens", "reasoning_tokens",
                                             "prompt_tokens", "cost_usd")}
                      for u in rec.get("usage") or []],
            "summary_chars": len(rec.get("summary") or ""),
            "truncated": info.get("truncated"), "empty_resends": info.get("empty_resends"),
            "custom_sha256": hashlib.sha256(ci.encode()).hexdigest() if ci else None,
            "custom_calls": info.get("custom_calls"),
            "pre_tokens_est": rec.get("pre_tokens_est"),
            "post_tokens_est": rec.get("post_tokens_est")}


def boundary(b: FixRun, hist: SE.AHistory, model_for: Callable, root: Path) -> dict:
    """The arm's compaction record (written once, reused on resume). native, ledger and
    placebo: a fresh pi compaction of the saved messages (ledger and placebo with their focus);
    line: native's, with the line appended after the summary in pi's wrapper message.
    Raises CompactionFailed when pi's summary stayed empty (now or recorded before: a failed
    compaction is final, never retried); StageCapReached passes through."""
    p = comp_path(root, b)
    with _COMP_LOCKS[str(p)]:  # native and line share one compaction: computed once
        if failed_path(p).exists():
            raise CompactionFailed(json.loads(failed_path(p).read_text())["error"])
        if p.exists():
            rec = json.loads(p.read_text())
        else:
            arm = "native" if b.arm == "line" else b.arm
            focus = text(FOCUS[arm]) if arm in FOCUS else None
            msgs = hist.messages
            m = model_for("compaction", p.parent / f"{b.unit}-{arm}-spend")
            model = m[0] if isinstance(m, tuple) else m  # paid_model returns (model, spend)
            try:
                comp = K.compact("pi", msgs, model, run_id=f"{b.unit}-{arm}",
                                 instruction_text=None, custom_instructions=focus,
                                 custom_calls=FOCUS_CALLS)
            except StageCapReached:
                raise
            except ModelError as e:  # pi's empty summary, or a call that failed after its
                # retries: final for this unit and arm (a stop or the stage cap passes)
                p.parent.mkdir(parents=True, exist_ok=True)
                SE.dump(failed_path(p), {"arm": arm, "unit": b.unit, "utc": D.utc(),
                                         "error": f"{type(e).__name__}: {str(e)[:1500]}",
                                         "cost_usd": getattr(e, "cost_usd", None),
                                         "usage": getattr(e, "usage", None),
                                         "requests": [{k: r.get(k) for k in
                                                       ("label", "max_output_tokens")}
                                                      for r in getattr(e, "requests", None)
                                                      or []]})
                raise CompactionFailed(f"{type(e).__name__}: {str(e)[:500]}") from e
            bpt = model.route.bytes_per_token
            rec = comp.to_dict() | {"cost_usd": comp.cost_usd,
                                    "pre_tokens_est": SE.est_tokens(msgs, bpt),
                                    "post_tokens_est": SE.est_tokens(comp.post_messages, bpt),
                                    "n_pre_messages": len(msgs), "arm": arm}
            p.parent.mkdir(parents=True, exist_ok=True)
            SE.dump(p, rec)
    if b.arm != "line":
        return rec
    line = text(LINE)
    post = [dict(m) for m in rec["post_messages"]]
    i = rec["summary_index"]
    post[i]["content"] = f"{post[i]['content']}\n\n{line}"
    return {**rec, "post_messages": post, "arm": "line", "line": line,
            "line_on": str(p), "line_on_sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


# ------------------------------------------------------------------------------------------
# One run
# ------------------------------------------------------------------------------------------


def score(out: Path, b: FixRun) -> dict:
    row = S.score_b(out, b.to_dict())
    return row | {"arm": b.arm, "replicate": b.replicate, "unit": b.unit}


def run_one(b: FixRun, st: dict, route, spend, tasks_: dict, root: Path,
            model_for: Callable | None = None, caps: dict | None = None,
            admit: Callable[[], bool] | None = None, grade: bool = True) -> dict:
    """One slot: the arm's boundary, then the second task (driver.run_one_b's attempt rules:
    a stop or stage-cap interruption is redone as a new attempt, a failure before the first
    call once), grading on the rig, scoring. A compaction that stays empty ends the slot as
    "compaction failed": recorded once (`<compaction>.failed.json`, with each attempt's usage)
    and final, never retried, for every slot that needs it."""
    caps = caps or {}
    rec = D.entry(st, "b", b.id, {"attempts": 0, "state": "pending", **b.to_dict()})
    if rec["state"] == "done":
        return rec
    if rec["state"] == "compaction failed":
        return rec  # final (CompactionFailed)
    mf = model_for or (lambda phase, out: D.paid_model(route, out, CC.RUN_CAP_USD, spend))
    task = tasks_[b.cell.b_task]
    hist = M.load_snapshot(root, b.history)
    admitted = False
    if not compaction_cached(root, b) and not D.finished(D.b_dir(b.id, max(1, rec["attempts"]),
                                                                  root)):
        # a paid summary comes only after the stop check and the budget gate
        if D.stop_requested():
            raise L.Stopped("stop requested")
        if admit is not None and not admit():
            with D._LOCK:
                rec["state"] = "held: cap"
            D.save_state(st)
            return rec
        admitted = True
    try:
        comp = boundary(b, hist, mf, root)
    except StageCapReached:
        D.halt(st, "summary fix", f"stage cap refused a summary for {b.id}")
    except CompactionFailed as e:
        with D._LOCK:
            rec["state"] = "compaction failed"
            rec.setdefault("compaction_failures", []).append(
                {"utc": D.utc(), "error": str(e)[:1000]})
        D.save_state(st)
        D.status(f"summary fix {b.id}: compaction failed: {str(e)[:200]}")
        return rec
    rec["compaction"] = compaction_summary(comp)
    hist = dataclasses.replace(hist, compaction=comp)
    while True:
        k = max(1, rec["attempts"])
        out = D.b_dir(b.id, k, root)
        res = D.finished(out)
        fresh = res is None
        if res is None:
            if out.exists():
                rec.setdefault("set_aside", []).append(str(D.set_aside(out, "crashed")))
            if D.stop_requested():
                raise L.Stopped("stop requested")
            if admitted:  # this attempt's budget was reserved before its summary
                admitted = False
            elif admit is not None and not admit():
                with D._LOCK:
                    rec["state"] = "held: cap"
                D.save_state(st)
                return rec
            rec["attempts"] = k
            rec["state"] = f"running a{k}"
            rec["host"] = M.HOST
            D.save_state(st)
            inv = Invocation(CC.PURPOSE)
            with M._slot(inv):
                model, _ = mf("B", out)
                try:
                    r = SE.run_b(b, task, hist, model, inv, out, stop=L.STOP,
                                 on_keep=D.keep_hook("B", b.id, root), **caps)
                finally:
                    rec["cleanup_unconfirmed"] = D.cleanup(inv)
            res = r.to_dict()
        if res["end"] in (SE.END_STAGE_CAP, SE.END_STOPPED):
            with D._LOCK:
                rec.setdefault("interrupted", []).append(
                    {"attempt": k, "dir": str(out), "end": res["end"], "n_calls": res["n_calls"]})
                rec["attempts"] = k + 1
                rec["state"] = "pending"
            D.save_state(st)
            if res["end"] == SE.END_STAGE_CAP and fresh:
                D.halt(st, "summary fix", f"stage cap refused a call in {b.id}")
            if res["end"] == SE.END_STOPPED and fresh:
                raise L.Stopped(f"summary fix {b.id} stopped")
            continue
        if res["n_calls"] == 0 and res["end"] == SE.END_FAILED and k == 1:
            D.status(f"summary fix {b.id} a1: infrastructure failure before any call, rerun")
            rec["attempts"] = 2
            continue
        if grade and res.get("traps_home"):
            rec["state"] = f"grading a{k}"
            D.save_state(st)
            D.grade_run(out, task, (res.get("extra") or {}).get("bonly_status"))
        row = score(out, b)
        spent = D._attempt_spend(out)
        with D._LOCK:
            rec.update(state="done", dir=str(out), end=res["end"],
                       cost_usd=round(spent if spent is not None else res["cost_usd"], 6),
                       rewrite=row["rewrite"], teammate_lost=row.get("teammate_lost"))
        D.save_state(st)
        D.status(f"summary fix {b.id}: {res['end']}, {res['n_calls']} calls, "
                 f"${rec['cost_usd']:.3f}, teammate lost {row.get('teammate_lost')}")
        return rec


class FixGate(D.CapGate):
    """driver.CapGate projecting the mean of the finished runs, else experiment 1's $1.705,
    plus a compaction's ~$0.08, times driver.COST_MARGIN."""

    def cost(self, b) -> float:
        done = [v["cost_usd"] for v in self.st["b"].values()
                if v.get("state") == "done" and v.get("cost_usd") is not None]
        base = sum(done) / len(done) if done else ASSUMED_RUN_USD
        return round((base + 0.08) * D.COST_MARGIN, 4)


def run_all(runs: list[FixRun], st: dict, route, spend, tasks_: dict, root: Path, *,
            concurrency: int, gate: D.CapGate | None = None, model_for: Callable | None = None,
            caps: dict | None = None, grade: bool = True) -> dict:
    """The slots in launch order, filling free slots as they open (a pool of `concurrency`;
    the host's admission decides how many containers run). Returns {id: state}."""
    gate = gate if gate is not None else FixGate(st, {}, root=root)

    def one(b: FixRun):
        held = {"on": False}

        def admit() -> bool:
            if held["on"]:
                gate.release(b)
            held["on"] = gate.admit(b)
            return held["on"]
        try:
            return run_one(b, st, route, spend, tasks_, root, model_for, caps, admit, grade)
        except (D.Halt, L.Stopped):
            raise
        except Exception as e:  # noqa: BLE001 - recorded; the others go on
            with D._LOCK:
                D.entry(st, "b", b.id, b.to_dict())["state"] = f"error: {str(e)[:300]}"
            D.save_state(st)
            D.status(f"summary fix {b.id}: driver error {str(e)[:200]}")
            return None
        finally:
            if held["on"]:
                gate.release(b)

    todo = [b for b in runs if (st["b"].get(b.id) or {}).get("state") != "done"]
    first = None
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for f in [ex.submit(one, b) for b in todo]:
            try:
                f.result()
            except (D.Halt, L.Stopped) as e:
                first = first or e
    if first is not None:
        raise first
    return {b.id: (st["b"].get(b.id) or {}).get("state", "pending") for b in runs}


def write_rows(st: dict, runs: list[FixRun], root: Path) -> Path:
    """One row per assigned slot, in launch order: the scored row of a done slot (with its
    compaction summary), else its state with teammate_lost unknown (None)."""
    rows = []
    for pos, b in enumerate(runs, 1):
        rec = st["b"].get(b.id) or {"state": "pending"}
        base = {"pos": pos, "id": b.id, "history": b.history, "arm": b.arm,
                "replicate": b.replicate, "unit": b.unit, "b_task": b.cell.b_task,
                "state": rec.get("state"), "compaction": rec.get("compaction"),
                "compaction_failures": rec.get("compaction_failures") or []}
        if rec.get("state") == "done":
            rows.append(score(Path(rec["dir"]), b) | base)
        else:
            rows.append(base | {"teammate_lost": None, "rewrite": "unknown"})
    p = Path(root) / "rows.jsonl"
    p.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    return p


# ------------------------------------------------------------------------------------------
# The config's binding to this code
# ------------------------------------------------------------------------------------------

ARM_WORDING = {"ledger_focus": FOCUS["ledger"], "placebo_focus": FOCUS["placebo"],
               "line": LINE}


def code_mismatches(cfg: dict) -> list[str]:
    """Where a summary_fix config differs from what this code runs. Empty = it runs exactly
    this design."""
    if cfg["design"]["kind"] != "summary_fix":
        return [f"design.kind is {cfg['design']['kind']!r}, not 'summary_fix'"]
    f, caps, d = cfg["summary_fix"], cfg["caps"], cfg["design"]
    out: list[str] = []
    if f.get("focus_calls", "history") not in ("history", "both"):
        out.append(f"summary_fix.focus_calls {f.get('focus_calls')!r}: history or both")
    for key, v, code in (("summary_fix.histories", list(f["histories"]), list(HISTORIES)),
                         ("summary_fix.arms", list(f["arms"]), list(ARMS)),
                         ("summary_fix.project", f["project"], PROJECT),
                         ("summary_fix.start", f["start"], START),
                         ("design.n_phases", d["n_phases"], 2),
                         ("design.log_reasoning", d["log_reasoning"], True),
                         ("caps.run_usd", caps["run_usd"], CC.RUN_CAP_USD),
                         ("caps.max_calls", caps["max_calls"], CC.MAX_CALLS),
                         ("caps.max_seconds", caps["max_seconds"], CC.MAX_SECONDS)):
        if v != code:
            out.append(f"{key}: config {v!r}, code {code!r}")
    if f["smoke_history"] not in HISTORIES:
        out.append(f"summary_fix.smoke_history {f['smoke_history']!r} isn't one of the histories")
    if not set(f["smoke_arms"]) <= set(ARMS):
        out.append(f"summary_fix.smoke_arms {f['smoke_arms']} aren't all arms")
    if not 1 <= f["replicates_late"] <= f["replicates"]:
        out.append("summary_fix.replicates_late must be between 1 and replicates")
    wording = cfg.get("wording") or {}
    need = set(M.CHAIN_WORDING) | set(ARM_WORDING)
    if need - set(wording):
        out.append(f"wording: missing {sorted(need - set(wording))}")
    if set(wording) - need:
        out.append(f"wording: unknown to this code: {sorted(set(wording) - need)}")
    hashes = CC.text_hashes()
    for name, code_key in M.CHAIN_WORDING.items():
        w = wording.get(name)
        if w and w["sha256"] != hashes[code_key]:
            out.append(f"wording.{name}: config sha256 {w['sha256'][:12]}, code "
                       f"{hashes[code_key][:12]}")
    for name, path in ARM_WORDING.items():
        w = wording.get(name)
        if not w:
            continue
        if w["path"] != path:
            out.append(f"wording.{name}.path {w['path']!r}: the code reads {path}")
        elif w["sha256"] != M._sha(M._resolve(path)):
            out.append(f"wording.{name}: config sha256 {w['sha256'][:12]} != the file's")
    return out


def replicates_for(cfg: dict, now_et: str) -> tuple[int, float]:
    """(replicates, stage cap) for a bulk start at `now_et` ("YYYY-MM-DDTHH:MM", ET): the plan's
    rule, 3 runs per history if pi starts by summary_fix.late_after_et, else 2."""
    f, caps = cfg["summary_fix"], cfg["caps"]
    if now_et <= f["late_after_et"]:
        return f["replicates"], caps["stage_usd"]
    return f["replicates_late"], caps["stage_usd_late"]
