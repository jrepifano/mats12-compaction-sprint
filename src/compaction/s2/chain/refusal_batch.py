"""Laptop batch runner for Week 2, Experiment 3b ("summary_refusal").

Enforces:
- All conditions (versions x repetitions) for a history run on a single host.
- Machine-wide laptop admission ONLY through builder b's `compaction.s2.chain.laptop_admission`
  module (`reserve`/`release`, `LaptopAdmission.slot("agent", stop, run_id=...)`), imported behind
  an adapter that refuses with a clear message when the module is not yet on this branch.
- Ramp 4, then 8 concurrent agent containers (`laptop_ramp = [4, 8]`, 1280 MB, 1 CPU each).
- Every container `--rm`, named, labelled `emx.sandbox=refusal` and `emx.run=<run_id>`, with
  `timeout -k 5` inside, and `docker rm -f` plus a `docker inspect` confirmation on any client
  timeout or interrupt (`src/x3/passcheck.py` pattern).
"""

from __future__ import annotations

import importlib
import json
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import refusal as RF
from compaction.s2.chain import session as SE

SANDBOX_LABEL = "refusal"
RAMP_SCHEDULE = (4, 8)
MISSING_ADMISSION_MSG = (
    "compaction.s2.chain.laptop_admission is missing on this branch; "
    "laptop batch runs must go through builder b's machine-wide admission gate "
    "(reserve/release and LaptopAdmission.slot('agent', stop, run_id=...))."
)


# ------------------------------------------------------------------------------------------
# Adapter for builder b's machine-wide laptop admission gate
# ------------------------------------------------------------------------------------------


def load_laptop_admission(module=None):
    """Import `compaction.s2.chain.laptop_admission`, or refuse with a clear error message."""
    if module is not None:
        return module
    try:
        return importlib.import_module("compaction.s2.chain.laptop_admission")
    except ModuleNotFoundError as exc:
        raise RuntimeError(MISSING_ADMISSION_MSG) from exc


def validate_parallelism(cfg: dict) -> dict:
    """Verify the config's laptop ramp (4, then 8), memory, CPU, and sandbox label rules."""
    par = cfg.get("parallelism") or {}
    ramp = list(par.get("laptop_ramp") or [])
    if tuple(ramp) != RAMP_SCHEDULE:
        raise ValueError(f"laptop_ramp must be [4, 8], got {ramp!r}")
    max_agents = int(par.get("laptop_agents") or ramp[-1])
    if max_agents != RAMP_SCHEDULE[-1]:
        raise ValueError(f"laptop_agents must be {RAMP_SCHEDULE[-1]}, got {max_agents}")
    mem_mb = int(par.get("laptop_agent_memory_mb") or 1280)
    cpus = int(par.get("laptop_agent_cpus") or 1)
    label = str(par.get("sandbox_label") or RF.PURPOSE)
    if label != SANDBOX_LABEL or RF.PURPOSE != SANDBOX_LABEL:
        raise ValueError(f"sandbox label must be {SANDBOX_LABEL!r}, got {label!r} / {RF.PURPOSE!r}")
    return {
        "ramp_agents": ramp[0],
        "max_agents": max_agents,
        "mem_mb": mem_mb,
        "cpus": cpus,
        "ramp_after_s": float(par.get("laptop_ramp_after_s", 120.0)),
        "poll_s": float(par.get("laptop_poll_s", 2.0)),
        "max_concurrent_histories": int(par.get("max_concurrent_histories", 1)),
        "sandbox_label": label,
    }


def apply_container_limits(cfg: dict) -> dict:
    """Set `CC.AGENT_MEMORY_MB` (via `driver.set_agent_memory`) and `CC.AGENT_CPUS` to match the
    admitted laptop container limits (1280 MB, 1 CPU)."""
    spec = validate_parallelism(cfg)
    D.set_agent_memory(spec["mem_mb"])
    CC.AGENT_CPUS = spec["cpus"]
    return spec


def _make_log_fn(log: Callable[[str], None] | Path | str | None) -> Callable[[str], None] | None:
    """Convert a log file path into a `Callable[[str], None]` as builder b's `LaptopAdmission`
    expects, or pass an existing callable through."""
    if log is None:
        return None
    if callable(log):
        return log
    p = Path(log)
    p.parent.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()

    def _write(msg: str) -> None:
        with log_lock, p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(UTC).isoformat()} {msg}\n")

    return _write


def make_admission(cfg: dict, *,
                   log: Callable[[str], None] | Path | str | None = None,
                   module=None):
    """Build `LaptopAdmission` with ramp 4 -> 8 from `cfg['parallelism']`."""
    la = load_laptop_admission(module)
    spec = validate_parallelism(cfg)
    log_fn = _make_log_fn(log)
    return la.LaptopAdmission(
        mem_mb=spec["mem_mb"],
        cpus=spec["cpus"],
        max_agents=spec["max_agents"],
        ramp_agents=spec["ramp_agents"],
        ramp_after_s=spec["ramp_after_s"],
        poll_s=spec["poll_s"],
        log=log_fn,
    )


def install_laptop_admission(cfg: dict, *,
                             log: Callable[[str], None] | Path | str | None = None,
                             module=None):
    """Apply container limits (1280 MB, 1 CPU) and install `LaptopAdmission` onto `D.ADMISSION`."""
    apply_container_limits(cfg)
    adm = make_admission(cfg, log=log, module=module)
    D.ADMISSION = adm
    return adm


def new_batch_run_id(prefix: str = "s2-refusal") -> str:
    """Return a unique batch run_id carrying a random 8-hex-char nonce so separate batch
    invocations never share container `emx.run` labels."""
    base = prefix.strip("-") if prefix else "s2-refusal"
    return f"{base}-{uuid.uuid4().hex[:8]}"


def reserve_slot(key: str, cfg: dict, *, run_id: str | None = None, name: str | None = None,
                 gate: Callable[[dict], str | None] | None = None,
                 log: Callable[[str], None] | Path | str | None = None,
                 module=None) -> tuple[bool, str, dict]:
    """Direct wrapper around `laptop_admission.reserve` matching builder b's signature."""
    la = load_laptop_admission(module)
    spec = validate_parallelism(cfg)
    kw: dict = {"run_id": run_id, "name": name}
    if gate is not None:
        kw["gate"] = gate
    log_fn = _make_log_fn(log)
    if log_fn is not None:
        kw["log"] = log_fn
    return la.reserve(key, spec["mem_mb"], spec["cpus"], **kw)


def release_slot(key: str, *, module=None) -> None:
    """Direct wrapper around `laptop_admission.release(key)`."""
    la = load_laptop_admission(module)
    la.release(key)


# ------------------------------------------------------------------------------------------
# Container command builders and `src/x3/passcheck.py`-style kill + confirm
# ------------------------------------------------------------------------------------------


def container_labels(run_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Mandatory container labels: `emx.sandbox=refusal` and `emx.run=<run_id>`."""
    if not run_id:
        raise ValueError("run_id is required for emx.run container label")
    out = {"emx.sandbox": SANDBOX_LABEL, "emx.run": str(run_id)}
    if extra:
        out.update({str(k): str(v) for k, v in extra.items()})
    out["emx.sandbox"] = SANDBOX_LABEL
    out["emx.run"] = str(run_id)
    return out


def docker_run_args(name: str, image: str, *, run_id: str, mem_mb: int = 1280,
                    cpus: int = 1, extra_labels: dict[str, str] | None = None) -> list[str]:
    """Build `docker run -d --rm --name <name> --label emx.sandbox=refusal --label emx.run=...`."""
    if not name:
        raise ValueError("container must be named")
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", name,
        f"--memory={int(mem_mb)}m",
        f"--cpus={int(cpus)}",
    ]
    for k, v in container_labels(run_id, extra_labels).items():
        cmd.extend(["--label", f"{k}={v}"])
    cmd.extend([image, "sleep", "infinity"])
    return cmd


def docker_exec_args(name: str, command: str, *, timeout_s: int = 180,
                     cwd: str = "/testbed", env_vars: dict[str, str] | None = None) -> list[str]:
    """Build `docker exec ... <name> timeout -k 5 <timeout_s>s bash -c <command>`."""
    cmd = ["docker", "exec", "-w", cwd]
    for k, v in (env_vars or {}).items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend([name, "timeout", "-k", "5", f"{int(timeout_s)}s", "bash", "-c", command])
    return cmd


def force_remove_container(name: str, *, runner=subprocess.run, tries: int = 10,
                           sleep_s: float = 0.2) -> bool:
    """`docker rm -f <name>` plus `docker inspect` confirmation (reference: `src/x3/passcheck.py`)."""
    runner(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30, check=False)
    for _ in range(max(1, tries)):
        chk = runner(
            ["docker", "inspect", "--format={{.State.Running}}", name],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if chk.returncode != 0:
            return True
        time.sleep(sleep_s)
    return False


def exec_in_container(name: str, command: str, *, timeout_s: int = 180, cwd: str = "/testbed",
                      env_vars: dict[str, str] | None = None,
                      runner=subprocess.run) -> tuple[str, int]:
    """Run a command inside `name` with `timeout -k 5` inside, and `docker rm -f` + confirm on
    any client timeout or interrupt."""
    cmd = docker_exec_args(name, command, timeout_s=timeout_s, cwd=cwd, env_vars=env_vars)
    try:
        res = runner(cmd, capture_output=True, text=True, timeout=timeout_s + 15, check=False)
        out = (res.stdout or "") + (res.stderr or "")
        return out, res.returncode
    except subprocess.TimeoutExpired:
        force_remove_container(name, runner=runner)
        return f"Command timed out after {timeout_s} seconds (container removed)\n", 124
    except KeyboardInterrupt:
        force_remove_container(name, runner=runner)
        raise


# ------------------------------------------------------------------------------------------
HOST = socket.gethostname()  # recorded on every result: a history's runs share one host

# Single-host history grouping and validation
# ------------------------------------------------------------------------------------------


def validate_single_host_histories(plan_order: list[dict], selected_units: list[dict], *,
                                   allow_partial: bool = False) -> list[str]:
    """Ensure all conditions for any selected history run on this one host (never split across
    hosts). Returns the ordered list of histories present in `selected_units`."""
    by_h_full: dict[str, set[str]] = {}
    for u in plan_order:
        by_h_full.setdefault(u["history"], set()).add(u["id"])
    by_h_sel: dict[str, set[str]] = {}
    h_order: list[str] = []
    for u in selected_units:
        h = u["history"]
        if h not in by_h_sel:
            h_order.append(h)
            by_h_sel[h] = set()
        by_h_sel[h].add(u["id"])
    if not allow_partial:
        split = [
            h for h in h_order
            if by_h_sel[h] != by_h_full.get(h, set())
        ]
        if split:
            raise ValueError(
                f"all conditions for a history must run on one host; partial conditions for: {split}"
            )
    return h_order


def select_host_units(plan_order: list[dict], *, histories: list[str] | None = None,
                      unit_ids: list[str] | None = None,
                      allow_partial: bool = False) -> list[dict]:
    """Filter `plan_order` to the histories assigned to this host while preserving launch order
    and checking that no history's conditions are split across hosts."""
    units = list(plan_order)
    if histories is not None:
        hset = set(histories)
        units = [u for u in units if u["history"] in hset]
    if unit_ids is not None:
        uset = set(unit_ids)
        units = [u for u in units if u["id"] in uset]
    validate_single_host_histories(plan_order, units, allow_partial=allow_partial)
    return units


def group_by_history(units: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group units by history (preserving each history's phase order: honest + false_permission
    pairs first, then decision_dropped, then false_rule) so all conditions of a history run
    together on one host."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for u in units:
        h = u["history"]
        if h not in groups:
            order.append(h)
            groups[h] = []
        groups[h].append(u)
    return [(h, groups[h]) for h in order]


# ------------------------------------------------------------------------------------------
# Single unit execution with crash-rerun-once and container cleanup
# ------------------------------------------------------------------------------------------


def _unit_done(u_dir: Path) -> dict | None:
    fdir = RF.resolve_final_attempt_dir(u_dir)
    if fdir is None:
        return None
    p = fdir / "result.json"
    if not p.exists():
        return None
    try:
        res = json.loads(p.read_text())
    except ValueError:
        return None
    if res.get("end") and res.get("end") not in (SE.END_FAILED, "restore_failed"):
        if (u_dir / "result.json").exists():
            try:
                top_res = json.loads((u_dir / "result.json").read_text())
                if top_res.get("end") and top_res.get("end") not in (
                    SE.END_FAILED, "restore_failed"
                ):
                    return top_res
            except ValueError:
                pass
        return res
    return None


def _cleanup_inv(inv) -> None:
    if inv is None:
        return
    if hasattr(inv, "cleanup"):
        inv.cleanup()
    elif hasattr(inv, "kill_run"):
        inv.kill_run()


def _dir_spend_usd(d: Path, fallback_res: dict | None = None) -> float:
    """Read durable spend from `spend.jsonl` in `d`, `d/a<k>/spend.jsonl`, or
    `d/attempts/a<k>/spend.jsonl`, falling back to `result.json`'s `cost_usd` for attempts
    that have no `spend.jsonl` ledger file."""
    if not d.is_dir():
        return float((fallback_res or {}).get("cost_usd") or 0.0)
    ledgers: list[Path] = []
    seen_ledgers: set[Path] = set()

    def _add_ledger(p: Path) -> None:
        if p.is_dir() and not p.is_symlink() and (p / "spend.jsonl").is_file():
            rp = p.resolve()
            if rp not in seen_ledgers:
                seen_ledgers.add(rp)
                ledgers.append(p)

    _add_ledger(d)
    for child in sorted(d.iterdir()):
        if child.is_dir() and not child.is_symlink():
            _add_ledger(child)
            if child.name == "attempts":
                for sub in sorted(child.iterdir()):
                    _add_ledger(sub)

    if ledgers:
        total = sum(D._attempt_spend(ld) for ld in ledgers)
        att_root = d / "attempts"
        if att_root.is_dir():
            for sub in sorted(att_root.glob("a*")):
                if (
                    sub.is_dir()
                    and not sub.is_symlink()
                    and sub.resolve() not in seen_ledgers
                    and (sub / "result.json").is_file()
                ):
                    try:
                        sdoc = json.loads((sub / "result.json").read_text())
                        total += float(sdoc.get("cost_usd") or 0.0)
                    except ValueError:
                        pass
        return total

    att_root = d / "attempts"
    if att_root.is_dir():
        att_files = [
            sub / "result.json"
            for sub in sorted(att_root.glob("a*"))
            if sub.is_dir() and not sub.is_symlink() and (sub / "result.json").is_file()
        ]
        if att_files:
            total = 0.0
            for af in att_files:
                try:
                    doc = json.loads(af.read_text())
                    total += float(doc.get("cost_usd") or 0.0)
                except ValueError:
                    pass
            return total

    rp = d / "result.json"
    if rp.is_file():
        try:
            doc = json.loads(rp.read_text())
            return float(doc.get("cost_usd") or 0.0)
        except ValueError:
            pass
    return float((fallback_res or {}).get("cost_usd") or 0.0)


def unit_spend_usd(u_dir: Path, crashed_dirs: list[Path] | None = None,
                   res: dict | None = None) -> float:
    """Total spend for a unit across all attempts (including crashed attempts) and set-asides."""
    total = 0.0
    seen: set[Path] = set()
    u_resolved = u_dir.resolve() if u_dir.exists() else None
    for cd in crashed_dirs or []:
        cdp = Path(cd) if not isinstance(cd, Path) else cd
        if not cdp.is_absolute():
            cdp = u_dir / cdp
        if not cdp.exists():
            continue
        rcd = cdp.resolve()
        if u_resolved is not None and (rcd == u_resolved or u_resolved in rcd.parents):
            continue
        if rcd not in seen:
            seen.add(rcd)
            total += _dir_spend_usd(cdp)
    if u_dir.parent.is_dir():
        for sib in sorted(u_dir.parent.glob(f"{u_dir.name}.crashed-*")):
            if sib.is_dir() and not sib.is_symlink():
                rsib = sib.resolve()
                if rsib not in seen:
                    seen.add(rsib)
                    total += _dir_spend_usd(sib)
    total += _dir_spend_usd(u_dir, fallback_res=res)
    return round(total, 6)


def _stop_callable(stop: threading.Event | Callable[[], bool] | None) -> Callable[[], bool]:
    """Ensure `stop` passed to `LaptopAdmission.slot` is always a zero-arg callable."""
    if callable(stop):
        return lambda: bool(stop()) or L.STOP.is_set() or D.stop_requested()
    if stop is not None:
        return lambda: stop.is_set() or L.STOP.is_set() or D.stop_requested()
    return lambda: L.STOP.is_set() or D.stop_requested()


def _make_unit_inv(base_inv, unit_id: str, attempt: int, *, batch_nonce: str | None = None):
    raw_base = getattr(base_inv, "run_id", None) or "s2-refusal"
    if batch_nonce and raw_base == "s2-refusal":
        base_id = f"{raw_base}-{batch_nonce}"
    else:
        base_id = raw_base
    uid_clean = "".join(c if c.isalnum() or c in "-_." else "-" for c in unit_id)
    run_id = f"{base_id}-{uid_clean}-a{attempt}"
    if base_inv is None or isinstance(base_inv, Invocation):
        return Invocation(
            RF.PURPOSE, run_id=run_id, host=getattr(base_inv, "host", None)
        )
    return SimpleNamespace(purpose=RF.PURPOSE, run_id=run_id, cleanup=list)


def _migrate_legacy_unfinished(u_dir: Path, unit_id: str) -> None:
    """If `u_dir` has a direct pre-restart crashed/unfinished attempt without `attempts/`, move
    its files into `u_dir / 'attempts' / 'a1'` with relative paths so a restart never overwrites
    crash evidence."""
    att_root = u_dir / "attempts"
    rp = u_dir / "result.json"
    if not u_dir.is_dir() or att_root.exists() or not rp.is_file():
        return
    try:
        res = json.loads(rp.read_text())
    except ValueError:
        res = {}
    if res.get("final_attempt"):
        return
    a1 = att_root / "a1"
    a1.mkdir(parents=True, exist_ok=True)
    for item in list(u_dir.iterdir()):
        if item.name in ("attempts", "unit_state.json", "final") or item.name.startswith(
            "crashed_attempt_"
        ):
            continue
        shutil.move(str(item), str(a1 / item.name))
    a1_rp = a1 / "result.json"
    if a1_rp.is_file():
        try:
            a1_res = json.loads(a1_rp.read_text())
            if a1_res.get("traps_home"):
                hp = Path(a1_res["traps_home"])
                if (a1 / hp.name).exists():
                    a1_res["traps_home"] = hp.name
                elif (a1 / "traps_home").exists():
                    a1_res["traps_home"] = "traps_home"
                RF.dump(a1_rp, a1_res)
        except ValueError:
            pass
    state = {
        "id": unit_id,
        "attempts_started": 1,
        "attempts_completed": 1,
        "final_attempt": "attempts/a1",
        "attempts": [{"attempt": 1, "dir": "attempts/a1", "end": res.get("end")}],
    }
    RF.dump(u_dir / "unit_state.json", state)


def _load_unit_state(u_dir: Path, unit_id: str) -> dict:
    _migrate_legacy_unfinished(u_dir, unit_id)
    st_p = u_dir / "unit_state.json"
    state: dict = {
        "id": unit_id,
        "attempts_started": 0,
        "attempts_completed": 0,
        "final_attempt": None,
        "attempts": [],
    }
    if st_p.is_file():
        try:
            loaded = json.loads(st_p.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except ValueError:
            pass
    att_root = u_dir / "attempts"
    max_disk = 0
    if att_root.is_dir():
        for d in att_root.glob("a*"):
            if d.is_dir() and not d.is_symlink() and d.name[1:].isdigit():
                max_disk = max(max_disk, int(d.name[1:]))
    if u_dir.parent.is_dir():
        for sib in u_dir.parent.glob(f"{u_dir.name}.crashed-a*"):
            sfx = sib.name.rsplit(".crashed-a", 1)[-1]
            if sfx.isdigit():
                max_disk = max(max_disk, int(sfx))
    state["attempts_started"] = max(int(state.get("attempts_started") or 0), max_disk)
    return state


def _update_final_pointer(u_dir: Path, attempt: int, att_res: dict) -> dict:
    rel_att = f"attempts/a{attempt}"
    rel_home = att_res.get("traps_home")
    top_home = (
        f"{rel_att}/{rel_home}"
        if isinstance(rel_home, str) and rel_home and not Path(rel_home).is_absolute()
        else rel_home
    )
    top_res = dict(att_res)
    top_res["final_attempt"] = rel_att
    top_res["traps_home"] = top_home
    RF.dump(u_dir / "result.json", top_res)
    final_link = u_dir / "final"
    try:
        if final_link.is_symlink() or final_link.exists():
            final_link.unlink()
        final_link.symlink_to(Path("attempts") / f"a{attempt}", target_is_directory=True)
    except OSError:
        pass
    return top_res


def run_one_unit(unit: dict, prep: dict, ctx: list[dict], task, model_factory, inv,
                 u_dir: Path, cfg: dict, *, admission, stop: threading.Event,
                 run_fn=RF.run_resumed,
                 inv_factory: Callable[[dict, int], object] | None = None,
                 on_slot_enter: Callable[[], None] | None = None,
                 should_abort_in_slot: Callable[[], bool] | None = None,
                 batch_nonce: str | None = None,
                 active_invs: dict[int, object] | None = None,
                 active_lock: threading.Lock | None = None) -> dict:
    """Run one unit inside `admission.slot("agent", stop_fn, run_id=unit_inv.run_id)`.
    Per the missing-data rule, a crashed run (`END_FAILED` or `restore_failed`) is rerun once,
    persisting attempt counters per unit across restarts in `u_dir / 'unit_state.json'`, storing
    each attempt in `u_dir / 'attempts' / f'a{k}'` with a final-attempt pointer and relative
    paths inside archived results, and summing durable spend across all attempts."""
    apply_container_limits(cfg)
    u_dir = Path(u_dir)
    done = _unit_done(u_dir)
    if done is not None:
        if on_slot_enter is not None:
            on_slot_enter()
        done["total_cost_usd"] = unit_spend_usd(u_dir, [], done)
        return done | {"skipped": True}
    caps = cfg["caps"]
    reruns = int((cfg.get("refusal") or {}).get("crash_reruns", 1))
    max_attempts = 1 + reruns
    state = _load_unit_state(u_dir, unit["id"])
    start_attempt = int(state.get("attempts_started") or 0) + 1
    stop_fn = _stop_callable(stop)
    last_res: dict = {}
    notified_enter = False

    def _notify_once() -> None:
        nonlocal notified_enter
        if not notified_enter and on_slot_enter is not None:
            notified_enter = True
            on_slot_enter()

    if start_attempt > max_attempts:
        _notify_once()
        fdir = RF.resolve_final_attempt_dir(u_dir)
        if fdir is not None and (fdir / "result.json").is_file():
            last_res = json.loads((fdir / "result.json").read_text())
        else:
            last_res = {
                "id": unit["id"],
                "history": unit["history"],
                "version": unit["version"],
                "rep": unit["rep"],
                "end": SE.END_FAILED,
                "attempt": state["attempts_started"],
            }
        total_cost = unit_spend_usd(u_dir, [], last_res)
        last_res["total_cost_usd"] = total_cost
        last_res["cost_usd"] = total_cost
        return last_res

    for attempt in range(start_attempt, max_attempts + 1):
        if stop_fn():
            break
        unit_inv = (
            inv_factory(unit, attempt)
            if inv_factory is not None
            else _make_unit_inv(inv, unit["id"], attempt, batch_nonce=batch_nonce)
        )
        if active_invs is not None and active_lock is not None:
            with active_lock:
                active_invs[id(unit_inv)] = unit_inv
        try:
            with admission.slot("agent", stop_fn, run_id=unit_inv.run_id):
                _notify_once()
                if stop_fn() or (should_abort_in_slot is not None and should_abort_in_slot()):
                    break
                att_dir = u_dir / "attempts" / f"a{attempt}"
                att_dir.mkdir(parents=True, exist_ok=True)
                state["attempts_started"] = attempt
                state["final_attempt"] = f"attempts/a{attempt}"
                RF.dump(u_dir / "unit_state.json", state)
                if attempt > 1:
                    for prev_k in range(1, attempt):
                        link = u_dir / f"crashed_attempt_{prev_k}"
                        if not link.exists() and not link.is_symlink():
                            try:
                                link.symlink_to(
                                    Path("attempts") / f"a{prev_k}",
                                    target_is_directory=True,
                                )
                            except OSError:
                                pass
                model = model_factory(unit, attempt)
                try:
                    last_res = run_fn(
                        unit, prep, ctx, task, model, unit_inv, att_dir,
                        max_calls=int(caps["max_calls"]),
                        max_seconds=float(caps["max_seconds"]),
                    )
                except KeyboardInterrupt:
                    _cleanup_inv(unit_inv)
                    raise
                last_res["attempt"] = attempt
                last_res["run_id"] = unit_inv.run_id
                last_res["traps_home"] = RF._rel_traps_home(
                    att_dir, last_res.get("traps_home")
                )
                crashed_rels = [f"attempts/a{k}" for k in range(1, attempt)]
                if crashed_rels:
                    last_res["crashed_dirs"] = crashed_rels
                total_cost = unit_spend_usd(u_dir, [], last_res)
                last_res["total_cost_usd"] = total_cost
                last_res["cost_usd"] = total_cost
                RF.dump(att_dir / "result.json", last_res)
                state["attempts_completed"] = attempt
                state.setdefault("attempts", []).append({
                    "attempt": attempt,
                    "dir": f"attempts/a{attempt}",
                    "end": last_res.get("end"),
                    "run_id": unit_inv.run_id,
                })
                RF.dump(u_dir / "unit_state.json", state)
                last_res = _update_final_pointer(u_dir, attempt, last_res)
                if last_res.get("end") not in (SE.END_FAILED, "restore_failed"):
                    return last_res
        finally:
            if active_invs is not None and active_lock is not None:
                with active_lock:
                    active_invs.pop(id(unit_inv), None)
            _cleanup_inv(unit_inv)
    if last_res:
        total_cost = unit_spend_usd(u_dir, [], last_res)
        last_res["total_cost_usd"] = total_cost
        last_res["cost_usd"] = total_cost
    return last_res


# ------------------------------------------------------------------------------------------
# Batch execution (ramp 4 -> 8 via LaptopAdmission, global phase order, deadline enforcement)
# ------------------------------------------------------------------------------------------


def _eval_now_et(now_et: datetime | str | Callable[[], datetime | str] | None) -> datetime:
    if callable(now_et):
        return RF._et(now_et())
    if now_et is not None:
        return RF._et(now_et)
    return datetime.now(RF.ET)


def _check_existing_bulk_start(out_dir: Path) -> str | None:
    """Return an already-persisted `bulk_started_at` from `batch_state.json` or an existing
    finished attempt on disk, WITHOUT writing `batch_state.json` before a slot is admitted."""
    state_p = Path(out_dir) / "batch_state.json"
    if state_p.is_file():
        try:
            st = json.loads(state_p.read_text())
            if st.get("bulk_started_at"):
                return str(st["bulk_started_at"])
        except ValueError:
            pass
    runs_dir = Path(out_dir) / "runs"
    if runs_dir.is_dir():
        existing = [
            p for p in list(runs_dir.glob("*/result.json"))
            + list(runs_dir.glob("*/attempts/a*/result.json"))
            if p.is_file() and ".crashed" not in str(p)
        ]
        if existing:
            earliest = min(p.stat().st_mtime for p in existing)
            return datetime.fromtimestamp(earliest, tz=RF.ET).isoformat()
    return None


def _persist_bulk_deadline(cfg: dict, out_dir: Path, started_at_s: str) -> dict:
    """Persist `bulk_started_at` and the resulting allowed versions in `batch_state.json` once
    the first execution slot is actually admitted."""
    rcfg = cfg["refusal"]
    state_p = Path(out_dir) / "batch_state.json"
    deadline_s = str(rcfg["pairs_only_after_et"])
    deadline = RF._et(deadline_s)
    started_dt = RF._et(started_at_s)
    bulk_started_before_deadline = started_dt < deadline
    allowed = set(
        RF.versions_allowed(
            started_dt,
            bulk_started_before_deadline,
            list(rcfg["versions"]),
            pairs_only_after_et=deadline_s,
            pairs=tuple(rcfg["primary"]),
        )
    )
    state: dict = {}
    if state_p.is_file():
        try:
            state = json.loads(state_p.read_text())
        except ValueError:
            state = {}
    info = {
        "bulk_started_at": started_at_s,
        "bulk_started_before_deadline": bulk_started_before_deadline,
        "pairs_only_after_et": deadline_s,
        "allowed_versions": sorted(allowed),
    }
    state.update(info)
    RF.dump(state_p, state)
    return info


def run_batch(cfg: dict, plan: dict, preps: dict[str, dict], contexts: dict[str, dict[str, list]],
              tasks: dict[str, object], model_factory, inv, out_dir: Path, *,
              histories: list[str] | None = None, unit_ids: list[str] | None = None,
              allow_partial: bool = False, admission=None, admission_module=None,
              run_fn=RF.run_resumed,
              inv_factory: Callable[[dict, int], object] | None = None,
              now_et: datetime | str | Callable[[], datetime | str] | None = None) -> dict:
    """Run the planned resumed units on the laptop under builder b's `LaptopAdmission` gate.

    - Validates that `laptop_ramp == [4, 8]` and `emx.sandbox=refusal`.
    - Sets `CC.AGENT_MEMORY_MB = 1280` and `CC.AGENT_CPUS = 1` to match admission reservations.
    - Validates that all conditions for each history assigned to this host stay on this host.
    - Records and persists `bulk_started_at` only when the first execution slot is actually
      admitted, enforcing the Wed 6 PM ET (`pairs_only_after_et`) deadline against that timestamp.
    - Admits units strictly in global phase order (`plan["order"]`: all histories' honest and
      false_permission pairs before any decision_dropped or false_rule unit).
    - Gives each active unit attempt its own `Invocation` (`emx.run` label) with a unique batch
      nonce.
    - Handles `KeyboardInterrupt` inside the `ThreadPoolExecutor` scope, setting `L.STOP`,
      cancelling queued futures, and cleaning up active unit invocations before waiting.
    """
    spec = apply_container_limits(cfg)
    out_dir = Path(out_dir)
    if admission is None:
        admission = make_admission(
            cfg, log=out_dir / "laptop_admission.jsonl", module=admission_module
        )
    all_selected_units = select_host_units(
        plan["order"], histories=histories, unit_ids=unit_ids, allow_partial=allow_partial
    )
    rcfg = cfg["refusal"]
    deadline_s = str(rcfg["pairs_only_after_et"])
    primary_pairs = set(rcfg["primary"])
    all_versions = set(rcfg["versions"])

    existing_started = _check_existing_bulk_start(out_dir)
    deadline_lock = threading.Lock()
    deadline_info: dict | None = None
    allowed_versions: set[str] = set(all_versions)

    if existing_started is not None:
        deadline_info = _persist_bulk_deadline(cfg, out_dir, existing_started)
        allowed_versions = set(deadline_info["allowed_versions"])

    def _record_first_slot_admitted() -> dict:
        nonlocal deadline_info, allowed_versions
        with deadline_lock:
            if deadline_info is None:
                admitted_dt = _eval_now_et(now_et)
                deadline_info = _persist_bulk_deadline(cfg, out_dir, admitted_dt.isoformat())
                allowed_versions = set(deadline_info["allowed_versions"])
            return deadline_info

    def _is_version_allowed(ver: str) -> bool:
        with deadline_lock:
            if deadline_info is not None:
                return ver in allowed_versions
        if ver in primary_pairs:
            return True
        cur_dt = _eval_now_et(now_et)
        return cur_dt < RF._et(deadline_s)

    units = [
        u for u in all_selected_units
        if (u["version"] in allowed_versions if deadline_info is not None else True)
    ]
    runs_dir = out_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    batch_nonce = uuid.uuid4().hex[:8]
    batch_run_id = getattr(inv, "run_id", None) or f"s2-refusal-{batch_nonce}"
    if batch_run_id == "s2-refusal":
        batch_run_id = f"s2-refusal-{batch_nonce}"

    L.STOP.clear()
    stop = threading.Event()
    stage_cap = float(cfg["caps"]["stage_usd"])
    lock = threading.Lock()
    active_lock = threading.Lock()
    active_invs: dict[int, object] = {}
    order_cond = threading.Condition()
    next_admit_idx = 0
    spent_usd = 0.0
    results: list[dict] = []
    skipped_deadline_ids: set[str] = set()
    aborted_reason: str | None = None

    def _wait_turn(idx: int) -> bool:
        with order_cond:
            while next_admit_idx < idx and not stop.is_set() and not L.STOP.is_set():
                order_cond.wait(timeout=0.5)
            return not (stop.is_set() or L.STOP.is_set())

    def _advance_turn(idx: int) -> None:
        nonlocal next_admit_idx
        with order_cond:
            if next_admit_idx <= idx:
                next_admit_idx = idx + 1
            order_cond.notify_all()

    def _worker(idx: int, u: dict) -> dict | None:
        nonlocal spent_usd, aborted_reason
        if not _wait_turn(idx):
            _advance_turn(idx)
            return {"id": u["id"], "history": u["history"], "version": u["version"],
                    "rep": u["rep"], "end": "stopped"}
        if not _is_version_allowed(u["version"]):
            with lock:
                skipped_deadline_ids.add(u["id"])
            _advance_turn(idx)
            return None
        with lock:
            if spent_usd >= stage_cap:
                aborted_reason = f"stage cap ${stage_cap:.2f} reached (${spent_usd:.2f})"
                stop.set()
                _advance_turn(idx)
                return {"id": u["id"], "history": u["history"], "version": u["version"],
                        "rep": u["rep"], "end": "cap_skipped"}
        h = u["history"]
        v = u["version"]
        u_dir = runs_dir / u["id"]

        def _on_enter() -> None:
            _record_first_slot_admitted()
            _advance_turn(idx)

        def _abort_in_slot() -> bool:
            info_now = _record_first_slot_admitted()
            if v not in set(info_now["allowed_versions"]):
                with lock:
                    skipped_deadline_ids.add(u["id"])
                return True
            return False

        try:
            res = run_one_unit(
                u, preps[h], contexts[h][v], tasks[h], model_factory, inv, u_dir, cfg,
                admission=admission, stop=stop, run_fn=run_fn, inv_factory=inv_factory,
                on_slot_enter=_on_enter,
                should_abort_in_slot=_abort_in_slot,
                batch_nonce=batch_nonce,
                active_invs=active_invs, active_lock=active_lock,
            )
        finally:
            _advance_turn(idx)
        if u["id"] in skipped_deadline_ids and not res:
            return None
        unit_cost = unit_spend_usd(u_dir, res=res)
        with lock:
            spent_usd += unit_cost
            if spent_usd >= stage_cap:
                aborted_reason = f"stage cap ${stage_cap:.2f} reached (${spent_usd:.2f})"
                stop.set()
                with order_cond:
                    order_cond.notify_all()
        return res

    with ThreadPoolExecutor(max_workers=spec["max_agents"]) as pool:
        futs = {pool.submit(_worker, idx, u): u for idx, u in enumerate(units)}
        pending = set(futs)
        try:
            while pending:
                done_futs, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done_futs:
                    r_item = f.result()
                    if r_item is not None:
                        r_item.setdefault("host", HOST)
                        results.append(r_item)
        except KeyboardInterrupt:
            stop.set()
            L.STOP.set()
            with order_cond:
                order_cond.notify_all()
            for f in pending:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            with active_lock:
                invs_now = list(active_invs.values())
            for u_inv in invs_now:
                _cleanup_inv(u_inv)
            _cleanup_inv(inv)
            raise

    if deadline_info is None:
        now_final = _eval_now_et(now_et)
        before_dl = now_final < RF._et(deadline_s)
        deadline_info = {
            "bulk_started_at": None,
            "bulk_started_before_deadline": before_dl,
            "pairs_only_after_et": deadline_s,
            "allowed_versions": sorted(
                RF.versions_allowed(
                    now_final, before_dl, list(rcfg["versions"]),
                    pairs_only_after_et=deadline_s, pairs=tuple(rcfg["primary"]),
                )
            ),
        }

    eff_planned = [u for u in units if u["id"] not in skipped_deadline_ids]
    summary = {
        "run_id": batch_run_id,
        "n_planned": len(eff_planned),
        "n_finished": len(results),
        "spent_usd": round(spent_usd, 6),
        "stage_cap_usd": stage_cap,
        "aborted_reason": aborted_reason,
        "ramp": list(RAMP_SCHEDULE),
        "sandbox_label": SANDBOX_LABEL,
        "deadline": deadline_info,
        "finished_at": datetime.now(UTC).isoformat(),
        "results": results,
    }
    RF.dump(out_dir / "batch_summary.json", summary)
    return summary
