"""E12 checkpoint dependency state and the terminal-skip predicate (code review r2 finding 4).

One rule, shared by the pod driver (`scripts/e12_pod.py`: training parents, writing and removing
skip records, step completion) and the laptop controller (`scripts/e12_control.py`:
`skip_problem`, `complete`, the endpoint decision inputs), so the two can never disagree.

A checkpoint key's producing chain (`run_chain`) is its producing run, then the producing runs
of its ancestors, nearest first (`contracts.run_name`, parents through `contracts.dose0_key` and
the segment table):
  P<s>                    <- poison_s<s>
  A<s>/B<s>_r010..r107    <- repair_<arm><s> <- poison_s<s>   (C<s>: repair_C<s> only, fresh)
  <arm><s>_r214           <- rescue_<arm><s> <- repair_<arm><s> [<- poison_s<s>]
  nudge doses             <- nudge_<parent> <- the parent's chain (R<s>_n..: nudge_base_s<s>)
  base                    needs nothing.

State, from the status records (`contracts.status_path`):
  failed  - the producing run has an explicit `failed` status, or (its checkpoint not ok) a run
            up the chain does: terminal, the run is never retrained;
  ok      - the producing run's status is ok/restarted_ok and the checkpoint's ckpt.json exists;
  pending - anything else, including no status at all: a later run may still produce it.

A skip record (`contracts.job_status_path`) is supported only if its job's state is failed, it
names a run of this job's chain, and that run or one of its ancestors in the chain has an
explicit `failed` status. Anything else (no status, a run outside the chain, a producer now ok or
pending) is refused, and the driver removes or rewrites it.

Stdlib only (with `contracts`): laptop scripts, pod scripts and tests all import it.
"""

from __future__ import annotations

import json
from pathlib import Path

from emx.e12 import contracts as C

OK, FAILED, PENDING = "ok", "failed", "pending"
OK_STATUSES: tuple[str, ...] = ("ok", "restarted_ok")


def producing_run(key: str) -> str | None:
    """The training run that saves checkpoint `key` (None for base)."""
    if key == C.BASE_KEY:
        return None
    k = C.parse_key(key)
    if k.arm == "P":
        return C.run_name("poison", key)
    if k.dose is not None:
        return C.run_name("nudge", C.dose0_key(key), k.seed)
    seg = "rescue" if k.repair == C.RESCUE_END else "repair"
    return C.run_name(seg, f"{k.arm}{k.seed}")


def producing_parent(key: str) -> str | None:
    """The checkpoint the producing run of `key` starts from (None: a fresh adapter or base)."""
    if key == C.BASE_KEY:
        return None
    k = C.parse_key(key)
    if k.arm == "P":
        return None
    if k.dose is not None:
        p = C.dose0_key(key)
        return None if p == C.BASE_KEY else p
    if k.repair == C.RESCUE_END:
        return C.format_key(k.arm, k.seed, C.REPAIR_SAVES[-1])
    return None if k.arm == "C" else f"P{k.seed}"


def run_chain(key: str) -> list[str]:
    """The producing run of `key`, then its ancestors' producing runs, nearest first."""
    out: list[str] = []
    k: str | None = key
    while k is not None:
        run = producing_run(k)
        if run is None:
            break
        out.append(run)
        k = producing_parent(k)
    return out


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def run_status(root: str | Path, recipe: str, run: str) -> dict | None:
    return _read(Path(root) / C.status_path(recipe, run))


def run_failed(root: str | Path, recipe: str, run: str) -> bool:
    return (run_status(root, recipe, run) or {}).get("status") == "failed"


def ckpt_present(root: str | Path, recipe: str, key: str) -> bool:
    return (Path(root) / C.ckpt_dir(recipe, key) / "ckpt.json").is_file()


def state(root: str | Path, recipe: str, key: str) -> tuple[str, str | None]:
    """(OK | FAILED | PENDING, reason) of checkpoint `key` (module docstring)."""
    run = producing_run(key)
    if run is None:
        return OK, None
    rec = run_status(root, recipe, run) or {}
    st = rec.get("status")
    if st == "failed":
        return FAILED, f"run {run} failed: {rec.get('reason')}"
    if st in OK_STATUSES and ckpt_present(root, recipe, key):
        return OK, None
    parent = producing_parent(key)
    if parent is not None:
        pstate, why = state(root, recipe, parent)
        if pstate == FAILED:
            return FAILED, f"parent {parent}: {why}"
    ck = "present" if ckpt_present(root, recipe, key) else "missing"
    return PENDING, f"checkpoint {key} {ck} (run {run} status {st}); pending"


def skip_record(root: str | Path, recipe: str, kind: str, key: str) -> dict | None:
    return _read(Path(root) / C.job_status_path(recipe, kind, key))


def skip_problem(root: str | Path, recipe: str, kind: str, key: str) -> str | None:
    """None if job (kind, key) has a supported terminal skip record (module docstring), else
    why not (including no record at all)."""
    name = f"{kind}/{key}"
    rec = skip_record(root, recipe, kind, key)
    if rec is None:
        return f"{name}: no verified output and no skip record"
    run = rec.get("run")
    if rec.get("status") != C.JOB_SKIP_STATUS or not run:
        return f"{name}: skip record is not {C.JOB_SKIP_STATUS} with a run"
    chain = run_chain(key)
    if run not in chain:
        return f"{name}: skip record names run {run}, outside the dependency chain {chain}"
    s, why = state(root, recipe, key)
    if s != FAILED:
        return f"{name}: stale skip record (run {run}; the checkpoint is now {s}: {why})"
    failed = [r for r in chain[chain.index(run):] if run_failed(root, recipe, r)]
    if not failed:
        return (f"{name}: skip record names run {run}, but neither it nor an ancestor in "
                f"{chain} has an explicit failed status")
    return None


def status_paths(root: str | Path, recipe: str, key: str) -> list[str]:
    """The existing status records of `key`'s chain (repo-relative): the evidence a skip rests
    on, listed beside the skip record in a step manifest."""
    return [C.status_path(recipe, r) for r in run_chain(key)
            if (Path(root) / C.status_path(recipe, r)).is_file()]
