"""Native-harness arms of the accumulated-loss experiment (src/compaction/s2/native/; brief
logs/s2/briefs/native-arms.md).

Run under the MCP server's dependency, which is not in the project:

    uv run --with fastmcp==4.1.3 python scripts/s2_native.py <command> ...

Commands:
- egress-check [--task T] [--arm claude_code|agy|codex]   ($0) start a session on the desktop,
  prove the container can't reach the internet but reaches its vendor API, tear down.
- cc-base --task T --window N [--stop-after K] [--sim-cap USD] (--smoke | --go-from-jacob)
  One Claude Code base run (Haiku 5.5 on Max; the simulator is paid, GPT-5.5 via OpenRouter,
  booked to SPRINT-S3-Q2 / s3q2-interact-pilot). --smoke: a run key starting "smoke-<utc>-",
  the simulator capped at $2, and a stop after the first boundary unless --stop-after says
  otherwise. Anything else is a bulk run and needs --go-from-jacob.
- agy-base --task T [--compact-at N] [--stop-after K] [--sim-cap USD] (--smoke | --go-from-jacob)
  One Antigravity base run (Gemini 3.8 Flash on Jacob's free quota; the token store is the
  emx-native-agy-auth volume on the desktop), with a forced /compact at the first turn
  boundary past N tokens (default 150k). Same --smoke rules as cc-base.
- cc-fork BDIR (--summary-file F | --append TEXT) [--prompt P] [--out DIR] (--smoke |
  --go-from-jacob)   resume boundary BDIR from its kept image with an edited summary.
  --probe-word W: append "FORK-PROBE: <W>" to the summary and ask the fork to repeat it
  without tools (the fork round-trip check); prints whether it did.
- cc-batch / agy-batch: the pilot batch (see `_batch`). Past Max's hold (7-day >= 90% or a
  rejected window) no run launches and a finished run's audits are queued, not run.
- audit-queued [--arm claude_code|agy]   ($0 on Max) run the queued audits once the window
  has reset.

Auth: CLAUDE_CODE_OAUTH_TOKEN from .env (`claude setup-token`), passed into the container only
as `docker exec -e`. Docker host: --host (default ssh://rig, the desktop).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import interact_run as IR
from compaction.s2.model import OpenAICompatModel, RunSpend
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N

SMOKE_SIM_CAP_USD = 2.0
SMOKE_TASK = "kombu-single-active-consumer-priority"


def _env() -> None:
    from dotenv import load_dotenv
    load_dotenv(C.REPO_ROOT / ".env", override=False)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sim_model(root: Path, name: str, cap_usd: float) -> OpenAICompatModel:
    spend = RunSpend(IR.PilotSpend(), cap_usd).attach(IR.spend_path(root, name))
    return OpenAICompatModel(C.SIM_ROUTE, spend)


def _token() -> str:
    tok = os.environ.get(CC.TOKEN_ENV, "").strip()
    if not tok:
        sys.exit(f"{CC.TOKEN_ENV} is not set (.env); run `claude setup-token`")
    return tok


def _paid(a) -> None:
    if not (a.smoke or a.go_from_jacob):
        sys.exit("paid command: pass --smoke (at most 2 per arm) or --go-from-jacob")


def cmd_egress_check(a) -> int:
    task = IR.load_task(a.task)

    class _NoSim:
        def __init__(self, runner):
            from compaction.s2.interact import SimState
            self.state = SimState()

        def ask(self, q):
            raise RuntimeError("no simulator in an egress check")

        def export(self):
            return {"state": {"ask_user_call_count": 0}}

    url = {"claude_code": "https://api.anthropic.com/v1/messages",
           "codex": "https://chatgpt.com/", "agy": "https://cloudcode-pa.googleapis.com/"}[a.arm]
    with N.NativeSession(a.arm, task.info.image, _NoSim, host=a.host, lifetime_s=1200) as ns:
        out = N.egress_check(ns.sb, ns.egress, url)
        out.update(arm=a.arm, image=task.info.image, network=ns.egress.network,
                   container=ns.sb.name)
    out["cleanup_unconfirmed"] = ns.cleanup_unconfirmed
    path = Path(a.out or C.raw_dir() / "native" / "egress_checks" / f"{a.arm}-{_stamp()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps({"ok": out["ok"], "file": str(path),
                      "checks": [(c["url"], c["via_proxy"], c["pass"]) for c in out["checks"]],
                      "cleanup_unconfirmed": ns.cleanup_unconfirmed}, indent=1))
    return 0 if out["ok"] and not ns.cleanup_unconfirmed else 1


def cmd_cc_base(a) -> int:
    _paid(a)
    _env()
    token = _token()
    root = Path(a.root) if a.root else CC.root()
    prefix = f"smoke-{_stamp()}-" if a.smoke else ""
    cap = min(a.sim_cap, SMOKE_SIM_CAP_USD) if a.smoke else a.sim_cap
    task = IR.load_task(a.task)
    cfg = CC.BaseConfig(task=task, root=root, prefix=prefix,
                        attempt=CC.next_attempt(root, task.task_id, prefix),
                        cc=CC.CCConfig(model=a.model, effort=a.effort or None,
                                       auto_compact_window=a.window),
                        host=a.host, sim_cap_usd=cap,
                        stop_after=a.stop_after if a.stop_after is not None
                        else (1 if a.smoke else None))
    sim = _sim_model(root, f"{cfg.run_key}-base", cap)
    res = CC.run_base(cfg, sim_model=sim, token=token)
    print(json.dumps({k: res.get(k) for k in ("run_key", "end", "boundaries", "sim_cost_usd",
                                              "seconds", "error", "cleanup_unconfirmed")},
                     indent=1)[:4000])
    print("run dir:", cfg.run_dir)
    ok = res["end"] in ("submitted", "stop_after", "eligible")
    return 0 if ok and not res.get("cleanup_unconfirmed") else 1


def cmd_cc_fork(a) -> int:
    _paid(a)
    _env()
    token = _token()
    bdir = Path(a.bdir)
    rec = json.loads((bdir / "boundary" / "compaction.json").read_text())
    summary = rec["summary"]
    prompt = a.prompt or CC.CONTINUE_PROMPT
    body = json.loads((bdir / "native" / "hook_payload.json").read_text()).get(
        "compact_summary") or ""
    if a.probe_word:
        summary = CC.insert_in_summary(summary, body, f"FORK-PROBE: {a.probe_word}")
        prompt = a.prompt or ("Without calling any tools: quote, exactly, the line of the "
                              "summary above that starts with 'FORK-PROBE:'.")
    elif a.summary_file:
        summary = Path(a.summary_file).read_text()
    elif a.append:  # inside the summary body, before Claude Code's closing paragraphs
        summary = CC.insert_in_summary(summary, body, a.append)
    else:
        sys.exit("pass --summary-file, --append or --probe-word")
    rj = json.loads((bdir.parent / "run.json").read_text())
    task = IR.load_task(rj["task"]["task_id"])
    out = Path(a.out or bdir.parent / "forks" / f"{bdir.name}-{_stamp()}")
    cap = SMOKE_SIM_CAP_USD if a.smoke else a.sim_cap
    sim = _sim_model(bdir.parent.parent, f"{rj['run_key']}-{bdir.name}-fork-{_stamp()}", cap)
    res = CC.fork(bdir, out, new_summary=summary, prompt=prompt, sim_model=sim, token=token,
                  persona=task.persona, host=a.host, timeout_s=a.timeout)
    text = CC.final_text(out / "stream.jsonl") if (out / "stream.jsonl").exists() else ""
    rep = {k: res.get(k) for k in ("outcome", "rc", "forked_session", "input_session",
                                   "image", "fork_check", "auth_error", "cleanup_unconfirmed")}
    rep["final_text"] = text[:1000]
    if a.probe_word:
        rep["probe_word_seen"] = a.probe_word in text
    print(json.dumps(rep, indent=1))
    print("fork dir:", out)
    ok = res.get("outcome") == "ok" and not res.get("cleanup_unconfirmed")
    return 0 if ok and (not a.probe_word or rep["probe_word_seen"]) else 1


def cmd_agy_base(a) -> int:
    _paid(a)
    _env()
    from compaction.s2.native import agy as A
    root = Path(a.root) if a.root else A.root()
    prefix = f"smoke-{_stamp()}-" if a.smoke else ""
    cap = min(a.sim_cap, SMOKE_SIM_CAP_USD) if a.smoke else a.sim_cap
    task = IR.load_task(a.task)
    cfg = A.BaseConfig(task=task, root=root, prefix=prefix,
                       attempt=A.next_attempt(root, task.task_id, prefix),
                       agy=A.AgyConfig(model=a.model, compact_at=a.compact_at,
                                       effort=a.effort or None),
                       host=a.host, sim_cap_usd=cap,
                       stop_after=a.stop_after if a.stop_after is not None
                       else (1 if a.smoke else None),
                       step_timeout_s={"02_implement": a.implement_timeout},
                       cpus=N.HEAVY_CPUS if a.heavy else N.AGENT_CPUS,
                       memory_mb=N.HEAVY_MEMORY_MB if a.heavy else N.AGENT_MEMORY_MB,
                       min_free_gb=N.HEAVY_MIN_FREE_GB if a.heavy else N.AGENT_MIN_FREE_GB)
    sim = _sim_model(root, f"{cfg.run_key}-base", cap)
    res = A.run_base(cfg, sim_model=sim)
    print(json.dumps({k: res.get(k) for k in ("run_key", "end", "boundaries", "sim_cost_usd",
                                              "seconds", "error", "cleanup_unconfirmed")},
                     indent=1)[:4000])
    print("run dir:", cfg.run_dir)
    ok = res["end"] in ("submitted", "stop_after", "eligible")
    return 0 if ok and not res.get("cleanup_unconfirmed") else 1


BATCH_TASKS = ("testem-bail-on-test-failure", "boa-hierarchical-evaluation-cancellation",
               "vitest-duration-sharding", "helm-array-merge-strategies",
               "kombu-single-active-consumer-priority", "fastapi-implicit-head-options")
RATE_BACKOFF_S = 900
SEVEN_DAY_HOLD = 0.90  # hold new launches here (main session, 24 Oct): the Opus screens,
# decay grading and audits share Max's weekly window and need headroom; from the same point
# audits not yet started are queued for after the reset (Jacob, 24 Oct)
AUDIT_QUEUE = "_audit/queued"  # <root>/_audit/queued/<run key>.json, run by audit-queued
BLOCK_PREFIX = "_block-"  # slot files of this name block launchers of older code only


def _latest_rate(root: Path) -> dict | None:
    """The newest rate_limit_event of any Claude Code stream under `root`."""
    best = None
    for p in Path(root).glob("*/steps/*/stream.jsonl"):
        for line in p.read_text(errors="replace").splitlines()[::-1]:
            if '"rate_limit_event"' in line:
                try:
                    info = json.loads(line).get("rate_limit_info") or {}
                except ValueError:
                    break
                if best is None or p.stat().st_mtime > best[0]:
                    best = (p.stat().st_mtime, info)
                break
    return best[1] if best else None


MAX_PING_S = 900  # the window reading is refreshed when no stream is newer than this
_PING: dict = {"checked": False, "off": None}  # per process: Max login checked; why pings stop


def _ping_checks() -> None:
    """The Max judges' own preconditions (claude_headless): a logged-in claude.ai Max account
    and no local policy settings that could change the route. Raises on either."""
    from compaction import claude_headless as H
    H.check_auth()
    H.check_managed_settings()


def _max_ping(max_root: Path, now: float | None = None, run=None, checks=None) -> str | None:
    """Keep Max's window reading fresh while no Claude Code session streams (main session,
    24 Oct). When no stream under `max_root` is newer than MAX_PING_S and no ping was tried in
    that time: one tiny Haiku `claude -p` on the laptop's Max login, through claude_headless
    (its clean env strips API keys and the container token; the login and policy checks run
    once per process; a paid-route event kills the call as it streams). Only a call whose route
    verifies "ok" is published: its rate_limit_event lines, as <max_root>/_ping/steps/<stamp>/
    stream.jsonl, where `_latest_rate` reads them. Attempts are spaced by <max_root>/_ping/
    attempts/ files. Returns None when skipped, else the route ("ok", "unknown", "bad"); "bad"
    stops pings for the process."""
    import tempfile
    import time

    from compaction import claude_headless as H
    now = time.time() if now is None else now
    root = Path(max_root)
    if _PING["off"] or any(now - p.stat().st_mtime < MAX_PING_S
                           for p in root.glob("*/steps/*/stream.jsonl")):
        return None
    tries = root / "_ping" / "attempts"
    tries.mkdir(parents=True, exist_ok=True)
    if any(now - p.stat().st_mtime < MAX_PING_S for p in tries.iterdir()):
        return None
    stamp = _stamp()
    (tries / stamp).write_text("")
    if not _PING["checked"]:
        (checks or _ping_checks)()
        _PING["checked"] = True
    paid: list[str] = []

    def guard(ev: dict) -> bool:
        why = H._paid_event(ev)
        if why:
            paid.append(why)
        return bool(why)

    args = ["-p", "--model", CC.MODEL, "--safe-mode", "--tools", "",
            "--no-session-persistence", "--strict-mcp-config", "--output-format", "stream-json",
            "--verbose", "--system-prompt", H.CANARY_SYSTEM]
    with tempfile.TemporaryDirectory(prefix="emx-max-ping-") as cwd:
        events = (run or H.run_session)(cwd, args, H.CANARY_USER, 120,
                                        env=H.clean_env(H.CANARY_MAX_OUTPUT), on_event=guard)
    route = "bad" if paid else H.verify_route(H.route_evidence(events))
    if route == "bad":
        _PING["off"] = paid[0] if paid else "route bad"
        return route
    if route == "ok":
        out = root / "_ping" / "steps" / stamp / "stream.jsonl"
        _write_atomic(out, "".join(json.dumps(e) + "\n" for e in events
                                   if e.get("type") == "rate_limit_event"))
    return route


def _ping_logged(max_root: Path, line) -> None:
    """`_max_ping`, its failures and non-"ok" routes logged through `line`, never raised."""
    try:
        route = _max_ping(max_root)
    except Exception as e:  # noqa: BLE001 - a stale reading, never a stopped batch
        route = f"error {type(e).__name__}: {e}"[:300]
    if route and route != "ok":
        line(event="max_ping", route=route, pings_off=_PING["off"])


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


HEAVY_TASKS = ("testem-bail-on-test-failure", "vitest-duration-sharding")  # node suites


def _live_sessions(host: str | None, arm: str = CC.ARM) -> int:
    """The arm's task containers running on the host (all launchers' together; the egress
    sidecars aren't counted). -1 when the query fails."""
    from compaction.deepswe import sandbox as SB
    with SB.using_host(host):
        names = SB._list(["ps", "--filter", f"label={SB.LABEL_KEY}={N.purpose(arm)}",
                          "--format", "{{.Names}}"])
    if names is None:
        return -1
    return sum(1 for n in names if "-egress-" not in n and "-install-" not in n)


def _rate_limited(res: dict) -> bool:
    """Max's rate limit (Claude Code) or agy's quota (Gemini) stopped the run."""
    for st in (res.get("steps") or {}).values():
        for tn in st.get("turns") or []:  # agy
            text = f"{tn.get('failed') or ''} {tn.get('problem') or ''}".lower()
            if any(k in text for k in ("quota", "resource_exhausted", "rate limit", " 429")):
                return True
        info = (st.get("rate_limit") or {}).get("rate_limit_info") or {}
        if info.get("status") == "rejected":
            return True
        text = str((st.get("result") or {}).get("text") or "").lower()
        if st.get("failed") and ("usage limit" in text or "rate limit" in text):
            return True
    return False


def window_hold(rate: dict | None, now: float) -> tuple[str | None, float]:
    """(why new launches hold, seconds to wait) from Max's newest rate-limit info: a rejected
    window, or the 7-day window at >= SEVEN_DAY_HOLD. A window whose resetsAt has passed no
    longer holds (the event is stale: nothing has refreshed it since; astra delta r1 #3)."""
    rate = rate or {}
    wins = dict(rate.get("unifiedWindows") or {})
    if not wins and rate.get("rateLimitType"):
        wins[rate["rateLimitType"]] = rate
    for name, w in wins.items():
        reset = float(w.get("resetsAt") or 0)
        if reset and now >= reset:
            continue
        util = w.get("utilization")
        if w.get("status") == "rejected" or (name == "seven_day" and util is not None
                                             and util >= SEVEN_DAY_HOLD):
            wait = reset - now if reset else RATE_BACKOFF_S
            return f"{name}: {w.get('status')} at {util}", max(60.0, wait)
    return None, 0.0


def _slot(root: Path, name: str | None = None, release: bool = False):
    """Batch slot files under <root>/_batch/slots, one per launched run, created and counted
    under one file lock (all launchers of this root), removed when the run ends. Returns the
    (count, claimed in the last 5 min) of the slots when called without `name`."""
    import time
    d = root / "_batch" / "slots"
    d.mkdir(parents=True, exist_ok=True)
    if release:
        (d / name).unlink(missing_ok=True)
        return None
    if name:
        (d / name).write_text(str(time.time()))
        return None
    now = time.time()
    ages = [now - p.stat().st_mtime for p in d.iterdir()
            if p.is_file() and not p.name.startswith(BLOCK_PREFIX)]
    return len(ages), sum(1 for x in ages if x < 300)


def _backend_factory():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "s2_interact_audit", C.REPO_ROOT / "scripts" / "s2_interact_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.max_backend(4)


AUDIT_INPUTS = ("pre_messages.json", "post_messages.json", "compaction.json", "record.json",
                "boundary.json", "sim_state.json", "sim_replies.json")  # the capture's files
COMPONENTS = ("requirements", "lossaudit")


def audit_inputs(rd: Path) -> str:
    """sha256 over what the audits read: run.json, and per graded boundary its top-level files
    (b<k>/run.json, which names lossaudit's task and image, and the rest) and its capture files
    (AUDIT_INPUTS; not the audits' own outputs, which they add to the same dir)."""
    import hashlib

    from compaction.s2 import session as SS
    rd = Path(rd)
    rj = rd / "run.json"
    h = hashlib.sha256(rj.read_bytes() if rj.exists() else b"\0missing")
    for bdir in SS.boundary_dirs(rd):
        top = sorted(f.name for f in bdir.iterdir() if f.is_file())
        for rel in [*top, *(f"boundary/{n}" for n in AUDIT_INPUTS)]:
            f = bdir / rel
            h.update(f"\0{bdir.name}/{rel}\0".encode())
            h.update(f.read_bytes() if f.exists() else b"\0missing")
    return h.hexdigest()


def _outputs(rd: Path, ad: Path, comp: str) -> dict[str, str]:
    """A component's output files and their sha256: requirements, everything it adds under each
    graded boundary/ (AUDIT_INPUTS excluded); lossaudit, its out dir (the log excluded)."""
    import hashlib

    from compaction.s2 import session as SS
    if comp == "requirements":
        files = [f for b in SS.boundary_dirs(Path(rd)) for f in (b / "boundary").rglob("*")
                 if f.is_file() and f.relative_to(b / "boundary").as_posix() not in AUDIT_INPUTS]
    else:
        files = [f for f in (ad / "lossaudit").rglob("*") if f.is_file() and f.suffix != ".log"]
    return {str(f.resolve()): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(files)}


AUDIT_MODULES = ("compaction.s2.native.audit", "compaction.s2.interact_reqs")
AUDIT_SCRIPTS = ("scripts/s2_lossaudit.py", "src/compaction/s2/lossaudit.py")  # a subprocess
_LOADED: dict[str, dict] = {}  # module -> what this process imported (see _load_audit_modules)
_LOADED_LOCK = threading.Lock()


def _sha_file(path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return None


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_audit_modules() -> None:
    """Import the audit modules once per process (under a lock: batch workers audit
    concurrently) and record a disk observation around each import (main session, 24 Oct: a
    run's `code` is frozen at launch, but these modules load at its first audit). The file is
    hashed just before and just after the import; `sha256_around_import` is that hash when the
    two agree, else None. It is not a hash of the executed bytes: a change and revert inside
    the import would go unseen. A module something else imported earlier is `preloaded`, with
    no hash (its loaded code is unknown)."""
    import importlib
    import importlib.util
    import sys
    with _LOADED_LOCK:
        for name in AUDIT_MODULES:
            if name in _LOADED:
                continue
            pre = name in sys.modules
            spec = None if pre else importlib.util.find_spec(name)
            before = _sha_file(getattr(spec, "origin", None)) if spec else None
            m = importlib.import_module(name)
            after = None if pre else _sha_file(getattr(m, "__file__", None))
            _LOADED[name] = {"sha256_around_import": before if before and before == after
                             else None, "preloaded": pre, "imported_utc": _utc_now()}


def _scripts_sha() -> dict:
    return {f: _sha_file(C.REPO_ROOT / f) for f in AUDIT_SCRIPTS}


def _audit_code(ad: Path, comp: str, scripts_before: dict | None = None) -> None:
    """Append to <ad>/audit_code.jsonl disk observations around a component: git HEAD now;
    per audit module,
    its `_LOADED` record and its file's sha256 now; for lossaudit, the subprocess's files just
    before it started and after it exited, and `changed_during_run`: True when the two
    endpoint observations differ, False when they match (a change and revert in between would
    not show), None when either hash is unreadable. Never raises: provenance it can't read is
    recorded as None, or as `error`."""
    import subprocess
    import sys
    row: dict = {"utc": _utc_now(), "component": comp}
    try:
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=C.REPO_ROOT,
                                  capture_output=True, text=True, check=False,
                                  timeout=30).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            head = None
        row.update(git_head=head, loaded={n: _LOADED.get(n) for n in AUDIT_MODULES},
                   on_disk={n: _sha_file(getattr(sys.modules.get(n), "__file__", None))
                            for n in AUDIT_MODULES})
        if scripts_before is not None:
            after = _scripts_sha()
            known = all(scripts_before.values()) and all(after.values())
            row.update(scripts_before=scripts_before, scripts_after=after,
                       changed_during_run=(after != scripts_before) if known else None)
    except Exception as e:  # noqa: BLE001 - provenance never fails an audit
        row["error"] = f"{type(e).__name__}: {e}"[:300]
    try:
        ad.mkdir(parents=True, exist_ok=True)
        with open(ad / "audit_code.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _component_ok(au: dict, comp: str) -> bool:
    if comp == "requirements":
        return "requirements" in au
    return "lossaudit" in au and au.get("lossaudit_rc") == 0


def _audits(root: Path, rd: Path, rk: str, host: str | None, la_lock, *, hold=None,
            only=COMPONENTS, inputs: str | None = None) -> dict:
    """The run's audits (`only` of COMPONENTS), into its own dirs (astra delta r1 #7);
    failures are recorded, never lose the run (r1 #4). `hold()` (a reason or None) is asked
    right before each component, the lossaudit's after its lock: a held component is listed in
    `pending` and not run. A component that succeeds is recorded as <root>/_audit/<rk>/
    <component>.done.json with the inputs' hash and its outputs' manifest; a record whose
    inputs and outputs both still match is reused instead of running the component again. So
    a resumed queue entry repeats nothing that finished before a crash once its record was
    written; a crash between a backend finishing and its record repeats that component (the
    requirement split and eligibility are kept per boundary; grading and lossaudit rerun)."""
    _load_audit_modules()
    from compaction.s2.native import audit as AU
    out: dict = {}
    ad = root / "_audit" / rk
    inputs = inputs or audit_inputs(rd)

    def done(comp: str) -> bool:
        f = ad / f"{comp}.done.json"
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            return False
        if rec.get("inputs") != inputs or not rec.get("outputs") or \
                _outputs(rd, ad, comp) != rec["outputs"]:
            return False
        out.update(rec["result"])
        return True

    def held(comp: str) -> bool:
        why = hold() if hold else None
        if why:
            out.setdefault("pending", []).append(comp)
            out["pending_why"] = why
        return bool(why)

    def record(comp: str, keys: tuple[str, ...]) -> None:
        if _component_ok(out, comp):
            _write_atomic(ad / f"{comp}.done.json", json.dumps(
                {"inputs": inputs, "outputs": _outputs(rd, ad, comp),
                 "result": {k: out[k] for k in keys if k in out}}))

    if "requirements" in only and not done("requirements") and not held("requirements"):
        try:
            out["requirements"] = AU.requirements(rd, _backend_factory)
        except Exception as e:  # noqa: BLE001
            out["requirements_error"] = f"{type(e).__name__}: {e}"[:500]
        _audit_code(ad, "requirements")
        record("requirements", ("requirements",))
    if "lossaudit" in only and not done("lossaudit"):
        with la_lock:
            if not held("lossaudit"):
                before = _scripts_sha()
                try:
                    AU.stage_lossaudit([rd], ad / "staging")
                    out["lossaudit_rc"] = AU.run_lossaudit(ad / "staging", ad / "lossaudit",
                                                           docker_host=host)
                    out["lossaudit"] = AU.lossaudit_counts(ad / "lossaudit", rk)
                except Exception as e:  # noqa: BLE001
                    out["lossaudit_error"] = f"{type(e).__name__}: {e}"[:500]
                _audit_code(ad, "lossaudit", scripts_before=before)
                record("lossaudit", ("lossaudit", "lossaudit_rc"))
    return out


def _queue_audit(root: Path, rd: Path, rk: str, why: str, components, inputs: str) -> Path:
    """Defer a run's held audit components past Max's hold: one file per run under
    AUDIT_QUEUE, bound to the run (absolute dir, run key, inputs' hash), written atomically."""
    q = root / AUDIT_QUEUE / f"{rk}.json"
    _write_atomic(q, json.dumps({
        "run_key": rk, "run_dir": str(Path(rd).resolve()), "components": list(components),
        "inputs": inputs, "why": why,
        "queued_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=1))
    return q


def _audit_counts(au: dict) -> dict:
    """The status-line fields of one run's audits: a component that didn't run (queued, or
    no audits at all) gives None, never a zero count."""
    reqs = au.get("requirements")
    la = au.get("lossaudit") or {}
    req_fields = {"pre_tokens": None, "items": None, "lost": None, "lost_new": None,
                  "lost_inherited": None, "items_by_reply": None, "lost_by_reply": None}
    if reqs is not None:
        req_fields = {"pre_tokens": [r.get("pre_tokens") for r in reqs] or None,
                      "items": sum(r["items"] for r in reqs),
                      "lost": sum(r["lost"] for r in reqs),
                      "lost_new": sum(r.get("lost_new", 0) for r in reqs),
                      "lost_inherited": sum(r.get("lost_inherited", 0) for r in reqs),
                      # split one reply at a time (interact_reqs' fallback), and their losses
                      "items_by_reply": sum(r.get("items_by_reply", 0) for r in reqs),
                      "lost_by_reply": sum(r.get("lost_by_reply", 0) for r in reqs)}
    return {**req_fields, "la_items": la.get("items"), "la_lost": la.get("lost"),
            "la_rc": au.get("lossaudit_rc"),
            "audit_errors": {k: v for k, v in au.items() if k.endswith("_error")} or None}


def _entry_problem(e) -> str | None:
    """Why a queue entry can't be used as written, or None."""
    if not isinstance(e, dict):
        return "not an object"
    for k in ("run_key", "run_dir", "inputs"):
        if not isinstance(e.get(k), str) or not e[k]:
            return f"{k} missing or not a string"
    if not Path(e["run_dir"]).is_absolute():
        return "run_dir is not absolute"
    comps = e.get("components")
    if not isinstance(comps, list) or not comps or \
            not all(isinstance(c, str) for c in comps) or \
            not set(comps) <= set(COMPONENTS) or len(set(comps)) != len(comps):
        return f"components must be distinct names from {COMPONENTS}"
    return None


def cmd_audit_queued(a) -> int:
    """Run the queued audits of an arm's root, one run at a time, while Max's window allows
    (asked before each component). One process at a time (a file lock; exit 4 if another
    holds it). An entry whose components all succeed moves to <queue>/done/; one with a held
    component keeps it and the command exits 3; a failed component, a malformed entry or a run
    that no longer matches its entry (run key, inputs' hash) stays queued with `last_error`
    and the exit is 5. Status lines to stdout and <root>/_audit/queued/<stamp>.jsonl."""
    import fcntl
    import threading
    import time
    _env()
    mod = CC
    if a.arm == "agy":
        from compaction.s2.native import agy as mod
    root = Path(a.root) if a.root else mod.root()
    qdir = root / AUDIT_QUEUE
    qdir.mkdir(parents=True, exist_ok=True)
    log = qdir / f"{_stamp()}.jsonl"

    def line(**kw) -> None:
        kw.setdefault("utc", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        with open(log, "a") as f:
            f.write(json.dumps(kw) + "\n")
        print(json.dumps(kw), flush=True)

    def hold() -> str | None:
        return window_hold(_latest_rate(CC.root()), time.time())[0]

    lk = open(qdir / ".lock", "a")  # noqa: SIM115 - held for the whole command
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        line(event="busy", detail="another audit-queued holds the queue")
        return 4
    rc = 0
    for q in sorted(qdir.glob("*.json")):
        _ping_logged(CC.root(), line)
        if why := hold():
            line(event="hold", reason="max_window", detail=why)
            return 3
        try:
            e = json.loads(q.read_text())
            bad = _entry_problem(e)
        except (OSError, ValueError) as ex:
            bad = f"{type(ex).__name__}: {ex}"[:300]
        if bad:
            line(event="malformed", entry=q.name, error=bad)
            rc = 5
            continue
        rd, rk = Path(e["run_dir"]), e["run_key"]
        try:
            problem = None
            rj = json.loads((rd / "run.json").read_text())
            if not isinstance(rj, dict) or rj.get("run_key") != rk:
                problem = "run.json names another run"
            elif audit_inputs(rd) != e["inputs"]:
                problem = "the run's inputs changed since it was queued"
        except (OSError, ValueError) as ex:
            problem = f"{type(ex).__name__}: {ex}"[:300]
        if problem:
            _write_atomic(q, json.dumps({**e, "last_error": problem}, indent=1))
            line(event="mismatch", run_key=rk, error=problem)
            rc = 5
            continue
        # every component, not just the entry's: one finished earlier is reused only while its
        # done record's inputs and outputs still match, else it reruns (astra r3 #1)
        au = _audits(root, rd, rk, a.host, threading.Lock(), hold=hold, inputs=e["inputs"])
        left = [c for c in COMPONENTS if not _component_ok(au, c)]
        line(event="audited", run_key=rk, components=e["components"], left=left,
             **_audit_counts(au))
        if not left:
            _write_atomic(qdir / "done" / q.name, json.dumps(
                {**e, "audited_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
                indent=1))
            q.unlink()
            continue
        errors = {k: v for k, v in au.items() if k.endswith("_error")}
        _write_atomic(q, json.dumps({**e, "components": left,
                                     "last_error": errors or au.get("pending_why")
                                     or f"lossaudit rc {au.get('lossaudit_rc')}"}, indent=1))
        if au.get("pending"):
            return 3
        rc = 5
    return rc


def cmd_cc_batch(a) -> int:
    a.arm = "claude_code"
    return _batch(a)


def cmd_agy_batch(a) -> int:
    a.arm = "agy"
    return _batch(a)


def _batch(a) -> int:
    """Claude Code base runs, at most `concurrency` sessions on the host (all launchers), to
    completion (no fork trigger), each followed by both Q1 audits (native.audit), per run.
    Launches hold, never drop: past the simulator projection (finished runs' actual cost +
    the cap of every running and the next run), while Max's window is rejected or the 7-day
    window is at >= 90% (until it resets); a run that hit Max's rate limit is re-queued after a
    back-off, never moved to a paid route; an infra_killed or pre-start failure reruns once
    (heavy after a kill). One status line per finished run (stdout and <root>/_batch/<id>.jsonl)."""
    import fcntl
    import threading
    import time
    import traceback
    from collections import deque
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from compaction import s1_second_look as SL
    if not a.go_from_jacob:
        sys.exit("paid batch: pass --go-from-jacob")
    from compaction.s2.native import agy as A
    _env()
    code = N.code_version()  # frozen now: every run of this launcher records the same code
    if code.get("dirty") and not a.allow_dirty:
        sys.exit("refusing to launch a batch from uncommitted native code (commit first, or "
                 "--allow-dirty for a test)")
    is_cc = a.arm == "claude_code"
    token = _token() if is_cc else None
    SL.check_frozen()
    mod = CC if is_cc else A
    root = Path(a.root) if a.root else mod.root()
    max_root = CC.root()  # Max's window (Claude Code streams): every audit uses Max
    bid = _stamp()
    bdir = root / "_batch"
    bdir.mkdir(parents=True, exist_ok=True)
    status = bdir / f"{bid}.jsonl"
    lock_path = bdir / "slots.lock"
    la_lock = threading.Lock()
    tasks = deque(a.tasks.split(",") if a.tasks else BATCH_TASKS)
    timeouts = {"01_plan": mod.STEP_TIMEOUT_S["01_plan"], "02_implement": a.implement_timeout}

    def line(**kw) -> None:
        kw.setdefault("utc", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        week = (((_latest_rate(max_root) or {}).get("unifiedWindows") or {})
                .get("seven_day") or {})
        kw.setdefault("max_7d_pct", round(100 * week["utilization"], 1)
                      if week.get("utilization") is not None else None)
        with open(status, "a") as f:
            f.write(json.dumps(kw) + "\n")
        print(json.dumps(kw), flush=True)

    heavy_tasks = set((a.heavy_tasks or "").split(",")) - {""}
    heavy_now: set[str] = set()
    reruns: dict[str, int] = {}

    def one(task_id: str, attempt: int, slot: str) -> dict:
        rk = mod.run_key(task_id, attempt)
        try:
            task = IR.load_task(task_id)
            heavy = task_id in heavy_tasks or task_id in heavy_now
            sizes = {"cpus": N.HEAVY_CPUS if heavy else N.AGENT_CPUS,
                     "memory_mb": N.HEAVY_MEMORY_MB if heavy else N.AGENT_MEMORY_MB,
                     "min_free_gb": N.HEAVY_MIN_FREE_GB if heavy else N.AGENT_MIN_FREE_GB}
            if is_cc:
                cfg = CC.BaseConfig(task=task, root=root, attempt=attempt,
                                    cc=CC.CCConfig(model=a.model, effort=a.effort or None,
                                                   auto_compact_window=a.window),
                                    host=a.host, sim_cap_usd=a.sim_cap,
                                    step_timeout_s=dict(timeouts), **sizes)
            else:
                cfg = A.BaseConfig(task=task, root=root, attempt=attempt,
                                   agy=A.AgyConfig(model=a.model, compact_at=a.compact_at,
                                                   effort=a.effort or None),
                                   host=a.host, sim_cap_usd=a.sim_cap,
                                   step_timeout_s=dict(timeouts), **sizes)
            sim = _sim_model(root, f"{cfg.run_key}-base", a.sim_cap)
            try:
                res = (CC.run_base(cfg, sim_model=sim, token=token) if is_cc
                       else A.run_base(cfg, sim_model=sim))
            except Exception:  # noqa: BLE001 - outside run_base's own guard (e.g. the install)
                res = {"run_key": rk, "end": "infra_error", "boundaries": [],
                       "sim_cost_usd": 0.0, "error": traceback.format_exc()[-1500:]}
            res["run_dir"] = str(cfg.run_dir)
            if not _rate_limited(res) and (res.get("boundaries") or res["end"] == "submitted"):
                inputs = audit_inputs(cfg.run_dir)
                au = _audits(root, cfg.run_dir, rk, a.host, la_lock, inputs=inputs,
                             hold=lambda: window_hold(_latest_rate(max_root), time.time())[0])
                if au.get("pending"):  # past Max's hold: audit-queued runs them after the reset
                    au["queued"] = str(_queue_audit(root, cfg.run_dir, rk, au["pending_why"],
                                                    au["pending"], inputs))
                res["audit"] = au
            return res
        finally:
            _slot(root, slot, release=True)

    finished_usd, held, running = 0.0, False, {}
    hold_until, hold_why = 0.0, None
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        while tasks or running:
            held = False  # re-decided every pass: finished runs may make room (delta2 r1 #3)
            _ping_logged(max_root, line)
            now = time.time()
            why, w = window_hold(_latest_rate(max_root), now)
            if why and why != hold_why:
                line(event="hold", reason="max_window", detail=why, wait_s=round(w),
                     queued=list(tasks))
            hold_why = why
            if why:
                hold_until = max(hold_until, now + w)
            while tasks and len(running) < a.concurrency and not held and \
                    time.time() >= hold_until:
                proj = finished_usd + a.sim_cap * (len(running) + 1)
                if proj > a.projection_usd:
                    line(event="hold", reason="projection", projected_usd=proj,
                         queued=list(tasks))
                    held = True
                    break
                with open(lock_path, "a") as lk:  # slot check + claim, atomic across launchers
                    fcntl.flock(lk, fcntl.LOCK_EX)
                    live = _live_sessions(a.host, a.arm)
                    n_slots, fresh = _slot(root)
                    # a slot claimed in the last 5 min may not show a container yet
                    if live < 0 or max(live, n_slots - fresh) + fresh >= a.concurrency:
                        break
                    t = tasks[0]
                    att = mod.next_attempt(root, t)
                    sname = mod.run_key(t, att)
                    _slot(root, sname)
                tasks.popleft()
                fut = ex.submit(one, t, att, sname)
                running[fut] = t
                line(event="launch", task=t, attempt=att, running=len(running),
                     heavy=t in heavy_tasks or t in heavy_now)
            if not running:
                if held or not tasks:
                    break  # held with nothing running: the queued tasks stay held
                time.sleep(min(60.0, max(5.0, hold_until - time.time())))
                continue
            done, _ = wait(list(running), timeout=60, return_when=FIRST_COMPLETED)
            for fut in done:
                t = running.pop(fut)
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001 - `one` returns results; this is a bug
                    line(event="run_crashed", task=t, error=f"{type(e).__name__}: {e}"[:500])
                    continue
                finished_usd += float(res.get("sim_cost_usd") or 0.0)
                early = res.get("end") in ("infra_error", "error") and not res.get("boundaries") \
                    and float(res.get("sim_cost_usd") or 0.0) == 0.0
                # infrastructure, never a task result: rerun once (heavy after a kill)
                if (res.get("end") in ("infra_killed", "infra_error") or early) and \
                        reruns.get(t, 0) < 1:
                    reruns[t] = reruns.get(t, 0) + 1
                    if res.get("end") == "infra_killed":
                        heavy_now.add(t)
                    line(event="rerun", task=t, run_key=res["run_key"], end=res["end"],
                         heavy=t in heavy_tasks or t in heavy_now,
                         error=(res.get("error") or "")[-300:])
                    tasks.append(t)
                    continue
                if _rate_limited(res):
                    line(event="rate_limited", task=t, run_key=res["run_key"],
                         requeue_after_s=RATE_BACKOFF_S)
                    hold_until = max(hold_until, time.time() + RATE_BACKOFF_S)
                    tasks.append(t)
                    continue
                au = res.get("audit") or {}
                line(event="done", task=t, run_key=res["run_key"], end=res["end"],
                     compactions=len(res.get("boundaries") or []),
                     pressure=res.get("compactions"),
                     window=a.window if is_cc else a.compact_at,
                     **_audit_counts(au), audit_queued=au.get("queued"),
                     sim_usd=res.get("sim_cost_usd"), finished_usd=round(finished_usd, 4),
                     seconds=res.get("seconds"), cleanup=res.get("cleanup_unconfirmed"),
                     client_timeouts=res.get("client_timeouts"),
                     error=(res.get("error") or "")[:300] or None)
    line(event="batch_end", held=list(tasks), finished_usd=round(finished_usd, 4),
         code=code)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="ssh://rig")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("egress-check")
    e.add_argument("--task", default=SMOKE_TASK)
    e.add_argument("--arm", default="claude_code", choices=sorted(N.ALLOW))
    e.add_argument("--out")
    b = sub.add_parser("cc-base")
    b.add_argument("--task", default=SMOKE_TASK)
    b.add_argument("--window", type=int, required=True,
                   help="CLAUDE_CODE_AUTO_COMPACT_WINDOW (tokens)")
    b.add_argument("--model", default=CC.MODEL)
    b.add_argument("--effort", default=CC.EFFORT)
    b.add_argument("--stop-after", type=int)
    b.add_argument("--sim-cap", type=float, default=C.INTERACT_BASE_CAP_USD)
    b.add_argument("--root")
    f = sub.add_parser("cc-fork")
    f.add_argument("bdir")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--summary-file")
    g.add_argument("--append")
    g.add_argument("--probe-word")
    f.add_argument("--prompt")
    f.add_argument("--out")
    f.add_argument("--timeout", type=int, default=1800)
    f.add_argument("--sim-cap", type=float, default=C.INTERACT_SECONDARY_CAP_USD)
    ab = sub.add_parser("agy-batch")
    ab.add_argument("--tasks", help="comma-separated; default the 6 pilot tasks")
    ab.add_argument("--compact-at", type=int, default=150_000)
    ab.add_argument("--model", default="gemini-3.8-flash-low")
    ab.add_argument("--effort")
    ab.add_argument("--sim-cap", type=float, default=6.5)
    ab.add_argument("--projection-usd", type=float, default=40.0)
    ab.add_argument("--implement-timeout", type=int, default=7200)
    ab.add_argument("--concurrency", type=int, default=3,
                    help="agy sessions on the host, across every launcher")
    ab.add_argument("--heavy-tasks", default=",".join(HEAVY_TASKS))
    ab.add_argument("--root")
    ab.add_argument("--go-from-jacob", action="store_true")
    ab.add_argument("--allow-dirty", action="store_true")
    aq = sub.add_parser("audit-queued")
    aq.add_argument("--arm", default="claude_code", choices=("claude_code", "agy"))
    aq.add_argument("--root")
    cb = sub.add_parser("cc-batch")
    cb.add_argument("--tasks", help="comma-separated; default the 6 pilot tasks")
    # Jacob's 150k trigger: Claude Code's buffer below the window measured 20.2k at 163k
    # (first boundary at 142,838), so 170k (main session, 24 Oct)
    cb.add_argument("--window", type=int, default=170_000)
    cb.add_argument("--model", default=CC.MODEL)
    cb.add_argument("--effort", default=CC.EFFORT)
    cb.add_argument("--sim-cap", type=float, default=6.5)
    cb.add_argument("--projection-usd", type=float, default=40.0)
    cb.add_argument("--implement-timeout", type=int, default=7200)
    cb.add_argument("--concurrency", type=int, default=3,
                    help="Claude Code sessions on the host, across every launcher")
    cb.add_argument("--heavy-tasks", default=",".join(HEAVY_TASKS),
                    help="tasks run as heavy containers (8 GB, 4 CPUs, >= 10 GB free)")
    cb.add_argument("--root")
    cb.add_argument("--go-from-jacob", action="store_true")
    cb.add_argument("--allow-dirty", action="store_true")
    g2 = sub.add_parser("agy-base")
    g2.add_argument("--task", default=SMOKE_TASK)
    g2.add_argument("--compact-at", type=int, default=150_000,
                    help="force /compact at the first turn boundary past this many tokens")
    g2.add_argument("--model", default="gemini-3.8-flash-low")
    g2.add_argument("--effort")
    g2.add_argument("--stop-after", type=int)
    g2.add_argument("--sim-cap", type=float, default=C.INTERACT_BASE_CAP_USD)
    g2.add_argument("--root")
    g2.add_argument("--heavy", action="store_true", help="8 GB / 4 CPUs, >= 10 GB free")
    g2.add_argument("--implement-timeout", type=int, default=7200)
    for sp in (b, f, g2):
        sp.add_argument("--smoke", action="store_true")
        sp.add_argument("--go-from-jacob", action="store_true")
    a = p.parse_args(argv)
    return {"egress-check": cmd_egress_check, "cc-base": cmd_cc_base,
            "cc-fork": cmd_cc_fork, "agy-base": cmd_agy_base,
            "cc-batch": cmd_cc_batch, "agy-batch": cmd_agy_batch,
            "audit-queued": cmd_audit_queued}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
