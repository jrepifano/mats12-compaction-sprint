"""A compacting session: `Loop.step` until an end, compacting at every boundary and going on
(SWE-Interact pilot, docs/sprint/plans/swe-interact-pilot.md; frozen interface in
logs/s2/briefs/swe-interact-build.md, sections 1, 2 and 4). Shared with the chained pilot.

`run_session` drives one agent conversation (a workflow step's base run, or an arm). After every
call: the `after_call` hook (an end reason ends the session there, with no compaction), then a
submission, then the threshold. At the threshold, a boundary k (numbered on from the run dir's
existing b<k>):

1. quiesce and `env.boundary(keep, k)` (a kept `docker commit` when keep is given; arms pass
   keep=None and get tag None);
2. the compaction: pi's first-summary prompt, or pi's update prompt when the context the loop
   is on already carries a summary (an earlier boundary of this session, or `previous`, the
   live or replayed summary an arm starts from);
3. `b<k>/boundary/{pre_messages, compaction, post_messages, record}.json` and `b<k>/run.json`;
4. `on_boundary` (extra files; may set `fields`);
5. `stop_at_boundary` (True ends the session with end="eligible");
6. `b<k>/boundary/boundary.json`, written last, so a boundary is complete only when it exists;
7. stop, or continue on post_messages.

Ends: the hook's reason, "submitted", "max_calls", "run_cap" (the run's dollar cap, or a
`RunCapReached`), "window", "eligible", "error" (a `ModelError`) and "stopped" (`Stopped`); the
message goes in `SessionResult.error`. Any other exception propagates, after the boundary files
already written (each is written whole, as it is made).

Kept snapshots (`KeepStore`): boundary images are tagged emx-sandbox/s2-interact:<run key>-b<k>
and labelled emx.keep=<run key> with emx.run="" (`Sandbox.snapshot(tag, labels=...)`), never
tracked by the run's `Invocation`, so the run's own cleanup leaves them; they are listed in
<run dir>/kept_snapshots.json (each row before its commit starts) and removed by
`KeepStore.remove`.

`grep_runner` is a `lossaudit.GrepRunner` over a kept image: the working tree (tracked and
untracked, not ignored), every local branch, commit messages on all refs, and /tmp/plan.md.
"""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from compaction.s2 import compact as K
from compaction.s2.loop import Stopped
from compaction.s2.model import ModelError, RunCapReached, est_tokens

if TYPE_CHECKING:
    from compaction.s2.compact import Compaction
    from compaction.s2.loop import Loop
    from compaction.s2.lossaudit import GrepRunner

KEEP_LABEL = "emx.keep"
KEEP_REPO = "emx-sandbox/s2-interact"
KEPT_FILE = "kept_snapshots.json"
GREP_PURPOSE = "s2-interact-grep"
GREP_LINES = 20  # matching lines kept per pattern and place searched
GREP_TIMEOUT_S = 600

END_SUBMIT, END_MAX_CALLS, END_CAP, END_WINDOW = "submitted", "max_calls", "run_cap", "window"
END_ELIGIBLE, END_ERROR, END_STOPPED = "eligible", "error", "stopped"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    tmp.replace(path)


class SessionEnv(Protocol):
    """What `run_session` drives; `env.TaskEnv`-like and `interact.InteractEnv` satisfy it."""

    def run_agent_command(self, cmd: str): ...
    def ask_user(self, question: str, action: str | None = None) -> str: ...
    def trap_snapshot(self, label: str) -> float: ...
    def boundary(self, keep: KeepStore | None, k: int) -> dict: ...  # {tag, stopped_processes,
    #                                                                     snapshot_s}
    def close(self) -> bool: ...


# ------------------------------------------------------------------------------------------
# Kept snapshots
# ------------------------------------------------------------------------------------------

_KEEP_LOCK = threading.Lock()


def _tag_part(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", s)[:120]


EXEC_ID_FILE = ".execution_id"


def execution_id(run_dir: Path) -> str:
    """This execution dir's persistent id (created once, under the keep lock)."""
    p = Path(run_dir) / EXEC_ID_FILE
    with _KEEP_LOCK:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(uuid.uuid4().hex[:12])
        return p.read_text().strip()


def _owned(tag: str, label: str) -> bool | None:
    """Whether `tag`'s image carries emx.keep=<label> (None: no such tag)."""
    from compaction.deepswe import sandbox as SB
    try:
        out = SB._docker(["image", "inspect", "--format",
                          f'{{{{index .Config.Labels "{KEEP_LABEL}"}}}}', tag], timeout=60)
    except SB.SandboxError:
        return None
    return out.stdout.decode().strip() == label


@dataclass
class KeepStore:
    """Boundary images that outlive the run's own cleanup (module docstring)."""

    run_dir: Path
    run_key: str
    host: str | None = None

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        # The run key plus this execution's own id (24 Oct incident: a stub smoke under another
        # root reused the live kombu run's key, re-pointed its kept tags with its own commits
        # and then removed every image with that label). The id is persisted in the run dir on
        # first use and travels with it (a retired dir keeps it; a new dir at the same path
        # gets a fresh one), so no two executions share a kept tag or label. Created on first
        # use (a session that never reaches a boundary leaves no trace).
        self._label_id: str | None = None

    @property
    def label_id(self) -> str:
        if self._label_id is None:
            self._label_id = f"{self.run_key}-{execution_id(self.run_dir)}"
        return self._label_id

    @property
    def path(self) -> Path:
        return self.run_dir / KEPT_FILE

    def labels(self) -> dict[str, str]:
        return {KEEP_LABEL: self.label_id}

    def _rows(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def new_tag(self, k: int) -> str:
        """The tag for boundary k, listed (created_utc None) before any commit starts."""
        tag = f"{KEEP_REPO}:{_tag_part(f'{self.label_id}-b{k}')}"
        with _KEEP_LOCK:
            rows = [r for r in self._rows() if r["tag"] != tag]
            rows.append({"k": k, "tag": tag, "label": self.label_id, "host": self.host,
                         "created_utc": None})
            _dump(self.path, rows)
        return tag

    def record(self, k: int, tag: str, **meta) -> None:
        """The commit of `tag` finished: stamp it (and any metadata, e.g. snapshot_s)."""
        with _KEEP_LOCK:
            rows = self._rows()
            row = next((r for r in rows if r["tag"] == tag), None)
            if row is None:
                row = {"k": k, "tag": tag, "label": self.label_id, "host": self.host}
                rows.append(row)
            row.update(meta, k=k, created_utc=utc_now())
            _dump(self.path, rows)

    @staticmethod
    def list(run_dir: Path) -> list[dict]:
        p = Path(run_dir) / KEPT_FILE
        return json.loads(p.read_text()) if p.exists() else []

    @staticmethod
    def remove(run_dir: Path) -> list[str]:
        """Remove every kept image of the run: each listed tag, and any image (tagged or not)
        labelled emx.keep=<label> on that host. Returns what was NOT confirmed removed
        (failed status queries included). Rows confirmed gone get `removed_utc`."""
        from compaction.deepswe import sandbox as SB

        run_dir = Path(run_dir)
        bad: list[str] = []
        with _KEEP_LOCK:
            rows = KeepStore.list(run_dir)
            live = [r for r in rows if not r.get("removed_utc")]
            for r in live:
                with SB.using_host(r.get("host")):
                    owned = _owned(r["tag"], r["label"])
                    if owned is False:  # the tag now names another execution's image
                        r["removed_utc"] = utc_now()
                        r["not_removed"] = "tag points at another execution's image"
                        continue
                    if owned is None or SB.remove_image(r["tag"]):
                        r["removed_utc"] = utc_now()
                    else:
                        bad.append(r["tag"])
            # leftover (untagged) images of this execution only: rows already removed
            # before this call don't widen the sweep
            for host, label in sorted({(r.get("host"), r["label"]) for r in live},
                                      key=lambda x: (x[0] or "", x[1])):
                with SB.using_host(host):
                    ids = SB._list(["images", "-a", "--filter", f"label={KEEP_LABEL}={label}",
                                    "--format", "{{.ID}}"])
                    if ids is None:
                        bad.append(f"images labelled {KEEP_LABEL}={label} (query failed)")
                        continue
                    bad += [i for i in dict.fromkeys(ids) if not SB.remove_image_id(i)]
            if rows:
                _dump(run_dir / KEPT_FILE, rows)
        return bad


# ------------------------------------------------------------------------------------------
# The session
# ------------------------------------------------------------------------------------------


@dataclass
class BoundaryCtx:
    k: int
    bdir: Path  # <run dir>/b<k>
    loop: Loop
    fields: dict  # boundary.json so far; hooks may add or override fields
    compaction: Compaction


@dataclass
class SessionHooks:
    on_boundary: Callable[[BoundaryCtx], None] | None = None
    stop_at_boundary: Callable[[BoundaryCtx], bool] | None = None
    after_call: Callable[[Loop], str | None] | None = None


@dataclass
class SessionResult:
    end: str
    n_calls: int
    cost_usd: float
    boundaries: list[int]
    seconds: float
    error: str | None
    boundary_calls: list[int] = field(default_factory=list)  # loop.n_calls at each boundary

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


_BDIR = re.compile(r"b(\d+)")


def _existing_k(run_dir: Path) -> list[int]:
    if not run_dir.is_dir():
        return []
    return sorted(int(m.group(1)) for p in run_dir.iterdir()
                  if p.is_dir() and (m := _BDIR.fullmatch(p.name)))


def _record_base(run_dir: Path, run_json: dict) -> str:
    """The record id stem: the run key, plus the arm dir's name when run_dir is not the run's
    own dir, so an arm's recompactions never share an id with the base run's boundaries."""
    key = str(run_json.get("run_key") or run_dir.name)
    if run_dir.name == key:
        return key
    # arms/<slug>/secondary: include the arm's slug too, so two arms' secondaries never share
    # record ids (astra build r1 #13)
    tail = run_dir.name if run_dir.parent.parent.name != "arms" else \
        f"{run_dir.parent.name}-{run_dir.name}"
    return f"{key}-{tail}"


def _boundary_run_json(run_json: dict, k: int, tag: str | None) -> dict:
    rj = json.loads(json.dumps(run_json, default=str))
    task = dict(rj.get("task") or {})
    task["base_image"] = task.get("base_image") or task.get("image")
    if tag:
        task["image"] = tag  # the loss audit greps the kept image
    rj["task"] = task
    rj["boundary_index"] = k
    return rj


def run_session(loop: Loop, env: SessionEnv, keep: KeepStore | None, *, run_dir: Path,
                run_json: dict, threshold: int, policy: str, instruction_text: str | None,
                max_calls: int, run_cap_usd: float, hooks: SessionHooks,
                previous: tuple[str, int] | None = None) -> SessionResult:
    """Run `loop` to an end, compacting at each boundary (module docstring). `loop.env` is the
    env the loop's tools run in (normally `env`). `previous` = (summary, index of the message
    carrying it in loop.messages) when the loop starts from a compacted context."""
    t0 = time.monotonic()
    run_dir = Path(run_dir)
    if previous is not None and not 0 < previous[1] < len(loop.messages):
        raise ValueError(f"previous summary index {previous[1]} is outside the context")
    res = SessionResult(END_ERROR, 0, 0.0, [], 0.0, None)
    route = loop.model.route
    try:
        while True:
            if loop.n_calls >= max_calls:
                res.end = END_MAX_CALLS
                break
            if run_cap_usd and loop.cost_usd >= run_cap_usd:
                res.end = END_CAP
                break
            if loop.ctx_tokens + route.max_output > route.window:
                res.end = END_WINDOW
                break
            loop.step(f"turn-{loop.n_calls + 1}")
            if hooks.after_call is not None:
                reason = hooks.after_call(loop)
                if reason:
                    res.end = reason
                    break
            if loop.submitted:
                res.end = END_SUBMIT
                break
            if loop.ctx_tokens >= threshold:
                k = max(_existing_k(run_dir), default=0) + 1
                stop, previous = _boundary(loop, env, keep, k, run_dir=run_dir,
                                           run_json=run_json, policy=policy,
                                           instruction_text=instruction_text, hooks=hooks,
                                           previous=previous)
                res.boundaries.append(k)
                res.boundary_calls.append(loop.n_calls)
                if stop:
                    res.end = END_ELIGIBLE
                    break
    except RunCapReached as e:
        res.end, res.error = END_CAP, f"{type(e).__name__}: {e}"
    except ModelError as e:
        res.end, res.error = END_ERROR, f"ModelError: {e}"
    except Stopped as e:
        res.end, res.error = END_STOPPED, str(e)
    finally:
        res.n_calls, res.cost_usd = loop.n_calls, loop.cost_usd
        res.seconds = round(time.monotonic() - t0, 3)
    return res


def _boundary(loop: Loop, env: SessionEnv, keep: KeepStore | None, k: int, *, run_dir: Path,
              run_json: dict, policy: str, instruction_text: str | None, hooks: SessionHooks,
              previous: tuple[str, int] | None) -> tuple[bool, tuple[str, int] | None]:
    """One boundary (module docstring, steps 1-7). Returns (stop, the summary the loop now
    continues from)."""
    bdir = run_dir / f"b{k}"
    b = bdir / "boundary"
    if (b / "boundary.json").exists():
        raise FileExistsError(f"{b / 'boundary.json'} exists")
    snap = env.boundary(keep, k)
    pre = list(loop.messages)
    pre_tokens = loop.ctx_tokens
    try:
        comp = K.compact(policy, pre, loop.model, run_id=f"{_record_base(run_dir, run_json)}-b{k}",
                         instruction_text=instruction_text,
                         previous_summary=previous[0] if previous else None,
                         previous_summary_index=previous[1] if previous else None)
    except ModelError as e:
        loop.cost_usd += e.cost_usd
        raise
    loop.cost_usd += comp.cost_usd
    record = K.boundary_record(f"{_record_base(run_dir, run_json)}-b{k}", pre,
                               model=loop.model.route.model, pre_tokens=pre_tokens,
                               summary=comp.summary)
    _dump(b / "pre_messages.json", pre)
    _dump(b / "compaction.json", comp.to_dict())
    _dump(b / "post_messages.json", comp.post_messages)
    _dump(b / "record.json", record)
    _dump(bdir / "run.json", _boundary_run_json(run_json, k, snap.get("tag")))
    prev_k = [j for j in _existing_k(run_dir) if j < k and _complete(run_dir / f"b{j}")]
    fields = {
        "tag": snap.get("tag"), "stopped_processes": snap.get("stopped_processes"),
        "snapshot_s": snap.get("snapshot_s"), "boundary_index": k,
        "stage": run_json.get("stage"), "calls_in_step": loop.n_calls,
        "pre_tokens": pre_tokens,
        "post_tokens_est": est_tokens(comp.post_messages, loop.model.route.bytes_per_token),
        "n_pre_messages": len(pre), "summary_chars": len(comp.summary),
        "summary_index": comp.summary_index, "kept_indices": comp.kept_indices,
        # pi: index in pre_messages of the first message of the verbatim tail (len(pre): none)
        "tail_start_index": min(comp.kept_indices) if comp.kept_indices else len(pre),
        "policy": comp.policy, "update": bool(comp.info.get("update")),
        "previous_boundary": prev_k[-1] if prev_k else None,
        "compaction_cost_usd": comp.cost_usd, "prompt_sha256": comp.info.get("prompt_sha256"),
        "update_prompt_sha256": comp.info.get("update_prompt_sha256"),
        "fingerprint_sha256": None, "sim_calls": None, "eligible": None,
    }
    ctx = BoundaryCtx(k, bdir, loop, fields, comp)
    if hooks.on_boundary is not None:
        hooks.on_boundary(ctx)
    stop = bool(hooks.stop_at_boundary(ctx)) if hooks.stop_at_boundary is not None else False
    if hooks.stop_at_boundary is not None and fields.get("eligible") is None:
        fields["eligible"] = stop
    fields["written_utc"] = utc_now()
    _dump(b / "boundary.json", fields)
    if stop:
        return True, previous
    loop.messages = [dict(m) for m in comp.post_messages]
    loop.ctx_tokens = est_tokens(loop.messages, loop.model.route.bytes_per_token)
    return False, (comp.summary, comp.summary_index)


# ------------------------------------------------------------------------------------------
# Discovery and the kept-image search
# ------------------------------------------------------------------------------------------


def _complete(d: Path) -> bool:
    return (d / "boundary" / "boundary.json").exists() and (d / "boundary" / "record.json").exists()


def boundary_dirs(root: Path, *, arms: bool = False) -> list[Path]:
    """Every complete b<k> (boundary.json present) of the run dir `root`, or of each run dir
    under `root`, ordered by run and k. `arms=True` adds the arms' own recompactions
    (<run>/arms/<arm>/b<j>)."""
    root = Path(root)
    runs = [root] if _existing_k(root) or (root / "run.json").exists() else \
        sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    out = []
    for r in runs:
        parents = [r]
        if arms and (r / "arms").is_dir():
            parents += sorted(p for p in (r / "arms").iterdir() if p.is_dir())
        for p in parents:
            out += [p / f"b{k}" for k in _existing_k(p) if _complete(p / f"b{k}")]
    return out


def _grep_script(patterns: list[str]) -> str:
    """For each pattern n: @@S n <place> ... @@E n <place> <rc>, for places wl/wn (working tree,
    untracked included: files, lines), bl/bn (every local branch), log (commit messages on all
    refs, as commit:<sha>:0:<subject>) and plan (/tmp/plan.md lines)."""
    g = "git -c safe.directory='*' grep -F -I"
    lines = ["cd /app || exit 3",
             ("REFS=$(git -c safe.directory='*' for-each-ref --format='%(refname:short)' "
              "refs/heads)")]
    for n, p in enumerate(patterns):
        q = shlex.quote(p)

        def place(name: str, body: str, n: int = n) -> list[str]:
            return [f"echo '@@S {n} {name}'", body + f" ; echo \"@@E {n} {name} $RC\""]

        lines += place("wl", f"{g} -l --untracked -e {q} -- . ; RC=$?")
        lines += place("wn", f"{g} -n --untracked -e {q} -- . | head -n {GREP_LINES} ; "
                             "RC=${PIPESTATUS[0]}")
        lines += place("bl", f"if [ -n \"$REFS\" ]; then {g} -l -e {q} $REFS -- ; RC=$?; "
                             "else RC=1; fi")
        lines += place("bn", f"if [ -n \"$REFS\" ]; then {g} -n -e {q} $REFS -- | "
                             f"head -n {GREP_LINES} ; RC=${{PIPESTATUS[0]}}; else RC=1; fi")
        lines += place("log", f"git -c safe.directory='*' log --all -F --grep={q} "
                              f"--format='commit:%H:0:%s' | head -n {GREP_LINES} ; "
                              "RC=${PIPESTATUS[0]}")
        lines += place("plan", f"if [ -f /tmp/plan.md ]; then grep -F -n -e {q} /tmp/plan.md "
                               f"| head -n {GREP_LINES} | sed 's|^|/tmp/plan.md:|' ; "
                               "RC=${PIPESTATUS[0]}; else RC=1; fi")
    return "\n".join(lines) + "\n"


_LINE = re.compile(r"(.+?):(\d+):(.*)")
_OK = {"wl": (0, 1), "bl": (0, 1), "wn": (0, 1, 141), "bn": (0, 1, 141), "log": (0, 141),
       "plan": (0, 1, 141)}


def parse_grep_output(stdout: str, patterns: list[str]) -> dict[str, dict]:
    """{pattern: {"files": [...], "lines": [{path, line, text}]}} (lossaudit's GrepRunner
    shape). Files: working-tree paths, "<branch>:<path>", "commit:<sha>", "/tmp/plan.md".
    A place that failed, or missing output, raises SandboxError."""
    from compaction.deepswe.sandbox import SandboxError

    seen: dict[int, set[str]] = {}
    out = {p: {"files": [], "lines": []} for p in patterns}
    cur, buf = None, []
    for line in stdout.splitlines():
        if m := re.fullmatch(r"@@S (\d+) (\w+)", line):
            cur, buf = (int(m.group(1)), m.group(2)), []
        elif m := re.fullmatch(r"@@E (\d+) (\w+) (\d+)", line):
            n, place, rc = int(m.group(1)), m.group(2), int(m.group(3))
            if cur != (n, place) or rc not in _OK.get(place, ()):
                raise SandboxError(f"kept-image search failed: pattern {n} {place} rc {rc}")
            d = out[patterns[n]]
            seen.setdefault(n, set()).add(place)
            for x in buf:
                if place in ("wl", "bl"):
                    d["files"].append(x.removeprefix("./"))
                elif g := _LINE.fullmatch(x):
                    path = g.group(1).removeprefix("./")
                    d["lines"].append({"path": path, "line": int(g.group(2)),
                                       "text": g.group(3)})
                    if place in ("log", "plan") and path not in d["files"]:
                        d["files"].append(path)
            cur = None
        elif cur is not None and line:
            buf.append(line)
    missing = [patterns[n] for n in range(len(patterns)) if seen.get(n) != set(_OK)]
    if missing:
        raise SandboxError(f"kept-image search output incomplete: {len(missing)} pattern(s)")
    return out


def grep_runner(host: str | None = None) -> GrepRunner:
    """A `lossaudit.GrepRunner` over a kept boundary image (module docstring), one container
    per call (--rm, named, labelled emx.sandbox=s2-interact-grep, --network none, in-container
    timeouts; removed and confirmed by `Invocation`). It only reads."""
    from compaction.s2 import lossaudit as LA

    def run(image: str, patterns: list[str]) -> dict[str, dict]:
        from compaction.deepswe.sandbox import Invocation, SandboxError

        out: dict[str, dict] = {}
        with Invocation(purpose=GREP_PURPOSE, host=host) as inv:
            sb = inv.start(image, lifetime_s=GREP_TIMEOUT_S + 600, cpus=2, memory_mb=4096)
            for chunk in LA._script_chunks(patterns):
                r = sb.exec(_grep_script(chunk), timeout_s=GREP_TIMEOUT_S)
                if r.rc != 0:
                    raise SandboxError(f"kept-image search rc={r.rc}: {r.stderr[-500:]}")
                out.update(parse_grep_output(r.stdout, chunk))
        return out

    return run
