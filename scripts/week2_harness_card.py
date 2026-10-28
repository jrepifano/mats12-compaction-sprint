"""Week-2 harness replication: design cards from the code, diffed against the plan's Build
contract ($0, no network, no containers).

    uv run python scripts/week2_harness_card.py cc  [--running]
    uv run python scripts/week2_harness_card.py cx  --max-live 2 --memory-mb 1280 --cpus 1.5
    uv run python scripts/week2_harness_card.py agy --max-live 2 --memory-mb 1280 --cpus 1.5

Each card's values are read from the code the run uses (driver constants, the CLI command and
environment builders, the turn-rule and check-in sources, the seeded assignment, the caps)
and from the launch arguments, never typed. The card is written to
`results/sprint/week2/harness/<harness>/design_card.md` and compared, row by row, with the
"Build contract" table in docs/sprint/plans/week2-harness-replication.md. For cx and agy it
also prints the bulk command built from the same arguments, so the launch uses the card's
values. `--running` (cc only) reads the live bulk's command line and its assignment.json
instead of the defaults. Exit 0 when every row matches, 1 on any mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
PLAN = ROOT / "docs" / "sprint" / "plans" / "week2-harness-replication.md"
OUT = ROOT / "results" / "sprint" / "week2" / "harness"
COLUMN = {"cc": "Claude Code", "cx": "Codex", "agy": "agy"}
SCRIPT = {"cc": "week2_harness_cc", "cx": "week2_harness_cx", "agy": "week2_harness_agy"}
EGRESS_CPUS = 0.5  # every history's egress sidecar (native.common.Egress: --cpus 0.5)


def _script(h: str, path: Path | None = None):
    path = path or ROOT / "scripts" / f"{SCRIPT[h]}.py"
    spec = importlib.util.spec_from_file_location(SCRIPT[h], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _flag(cmd: str, flag: str) -> str:
    """The value after `flag` in a command, unwrapping one `bash -lc '<inner>'` layer."""
    import shlex
    toks = shlex.split(cmd)
    if "-lc" in toks:
        toks = shlex.split(toks[toks.index("-lc") + 1])
    return toks[toks.index(flag) + 1] if flag in toks else "MISSING FROM CLI"


def _order_sha(order: list[dict]) -> str:
    keys = ("pos", "round", "first_task", "arm", "rep", "history")
    return _sha(json.dumps([{k: o[k] for k in keys} for o in order]).encode())


def _shared(m, order: list[dict]) -> dict:
    from compaction.s2.chain import config as CC
    from compaction.s2.native import turns as T
    rounds = sorted({o["round"] for o in order})
    per = {(o["round"], o["first_task"], o["arm"]) for o in order}
    n_per = {len([o for o in order if (o["round"], o["first_task"], o["arm"]) == k]) for k in per}
    return {
        "turn_rules_sha256": _sha(Path(inspect.getsourcefile(T)).read_bytes()),
        "checkins_sha256": _sha(json.dumps(list(T.CHECKINS)).encode()),  # what turns sends
        "assignment": (f"seed {m.SEED if hasattr(m, 'SEED') else m.CCS.SEED}; "
                       f"{len(rounds)} rounds; {'/'.join(map(str, sorted(n_per)))} per arm per "
                       f"first task per round; {len(order)} histories"),
        "assignment_order_sha256": _order_sha(order),
        "calls_per_task": str(CC.MAX_CALLS),
        "seconds_per_task": str(CC.MAX_SECONDS),
    }


def _launch_code(launch_rev: str, srcs: list[str], script: str) -> tuple[str, Path | None]:
    """Whether the code a running process loaded at `launch_rev` is what the card reads: every
    source unchanged since then ("yes"), or only the scheduler script changed, whose launch
    copy is then read instead ("yes (script from <rev>)"); otherwise "no: <files>". A failed
    git check fails closed (astra agy-precompact r1 m2)."""
    p = subprocess.run(["git", "diff", "--name-only", launch_rev, "--", *srcs],
                       capture_output=True, text=True, cwd=ROOT, check=False)
    if p.returncode != 0:
        raise SystemExit(f"git diff {launch_rev} failed: {p.stderr.strip()[-200:]}")
    changed = set(p.stdout.split())
    rel = str(Path(script).resolve().relative_to(ROOT))
    if not changed:
        return "yes", None
    if changed != {rel}:
        return f"no: {', '.join(sorted(changed))}", None
    old = subprocess.run(["git", "show", f"{launch_rev}:{rel}"], capture_output=True,
                         cwd=ROOT, check=False)
    if old.returncode != 0:
        raise SystemExit(f"git show {launch_rev}:{rel} failed")
    tmp = Path(tempfile.mkdtemp()) / Path(rel).name
    tmp.write_bytes(old.stdout)
    return f"yes (script from {launch_rev})", tmp


def card_cc(running: bool, launch_rev: str) -> dict:
    m = _script("cc")
    verified = "yes"
    if running:
        X0 = m.X
        from compaction.s2 import followups as F0  # T.CHECKINS's source (r2 m2)
        from compaction.s2.native import claude_code as CCN0
        srcs = [inspect.getsourcefile(x) for x in (X0, CCN0, X0.T, X0.CC, F0)] + [m.__file__]
        verified, old = _launch_code(launch_rev, srcs, m.__file__)
        if old is not None:
            m = _script("cc", old)
    X = m.X
    from compaction.s2.native import claude_code as CCN
    env = X.cli_env(SimpleNamespace(token="t"))
    cmd = X.cli_command()
    src = inspect.getsource(X.boundary_compact)
    order = m.assignment()
    args = ""
    if running:
        pid = subprocess.run(["pgrep", "-f", "[w]eek2_harness_cc.py bulk"], capture_output=True,
                             text=True, check=False).stdout.split()
        args = subprocess.run(["ps", "-o", "args=", "-p", pid[0]], capture_output=True,
                              text=True, check=False).stdout.strip() if pid else "(not running)"
        launched = json.loads((ROOT / "data" / "raw" / "sprint" / "s2" / "harness" / "cc"
                               / "assignment.json").read_text())
        order = launched["order"]
    max_live = m.MAX_LIVE
    if "--max-live" in args:
        max_live = int(args.split("--max-live")[1].split()[0])
    return {
        "cli_version": CCN.CLI_VERSION,
        "model": _flag(cmd, "--model"),
        "effort": _flag(cmd, "--effort"),
        "context_window": "1000000" if X.MODEL.endswith("[1m]") else "200000",
        "auto_compaction": "off" if env.get("DISABLE_AUTO_COMPACT") == "1" else "on",
        "boundary_compaction": ("/compact via -p --resume" if '"/compact"' in src
                                and "resume" in src else "UNKNOWN"),
        **_shared(m, order),
        "usd_per_task": f"{m.TASK_CAP_USD:g}",
        "stage_cap_usd": f"{m.CAP_USD:g}",
        "host": "laptop",
        "max_live": str(max_live),
        "memory_mb_per_container": str(m.MEMORY_MB),
        "cpus_per_history": f"{m.CPUS + EGRESS_CPUS:g}",
        "container_capacity": str(m.MAX_CONTAINERS),
        "launch_code_verified": verified.split(" (")[0],
        "_launch": (args or "(defaults)") + (f"  [launch code: {verified}]" if running else ""),
    }


def card_cx(max_live: int, memory_mb: int | None, host: str | None, cpus: float,
            capacity: int) -> dict:
    m = _script("cx")
    X = m.X
    cmd = X.cli_command()
    win = dict(X.OVERRIDES)
    limit, ctx = win["model_auto_compact_token_limit"], win["model_context_window"]
    mem = m._memory(host, memory_mb)
    launch = (f"uv run --with fastmcp==4.1.3 python scripts/week2_harness_cx.py bulk "
              f"--go-from-jacob --max-live {max_live} --memory-mb {mem} --cpus {cpus:g} "
              f"--capacity {capacity}"
              + (f" --host {host}" if host else ""))
    return {
        "cli_version": X.CX.CLI_VERSION,
        "model": X.MODEL if f"-m {X.MODEL}" in cmd else "MISSING FROM CLI",
        "effort": X.cli_config().get("model_reasoning_effort", "CLI default"),
        "context_window": (f"{ctx} (effective {X.EXPECTED_WINDOW})"
                           if f"model_context_window={ctx}" in cmd else "MISSING FROM CLI"),
        "auto_compaction": (f"off (limit {limit} above window)"
                            if limit > X.EXPECTED_WINDOW
                            and f"model_auto_compact_token_limit={limit}" in cmd else "on"),
        "boundary_compaction": ("app-server thread/compact/start"
                                if "thread/compact/start" in inspect.getsource(X.compact_thread)
                                else "UNKNOWN"),
        **_shared(m, m.assignment()),
        "usd_per_task": "plan quota",
        "stage_cap_usd": "plan quota",
        "host": "laptop" if host is None else host,
        "max_live": str(max_live),
        "memory_mb_per_container": str(mem),
        "cpus_per_history": f"{cpus + EGRESS_CPUS:g}",
        "container_capacity": str(capacity),
        "launch_code_verified": "yes",  # launched from this code
        "_launch": launch,
    }


def card_agy(max_live: int, memory_mb: int, host: str | None, cpus: float,
             capacity: int) -> dict:
    m = _script("agy")
    X = m.X
    src = inspect.getsource(X.boundary_compact)
    launch = (f"uv run --with fastmcp==4.1.3 python scripts/week2_harness_agy.py bulk "
              f"--go-from-jacob --host {host or 'local'} --max-live {max_live} "
              f"--memory-mb {memory_mb} --cpus {cpus:g} --max-containers {capacity}")
    return {
        "cli_version": X.CLI_VERSION,
        "model": X.AGY.model,
        "effort": X.AGY.effort or "CLI default",
        "context_window": "default (unverified)",
        "auto_compaction": ("on (cannot be disabled)" if X.AGY.compact_at is None
                            else f"forced at {X.AGY.compact_at}"),
        "boundary_compaction": "/compact turn" if "/compact" in src else "UNKNOWN",
        **_shared(m, m.assignment()),
        "usd_per_task": "plan quota",
        "stage_cap_usd": "plan quota",
        "host": "laptop" if host is None else host,
        "max_live": str(max_live),
        "memory_mb_per_container": str(memory_mb),
        "cpus_per_history": f"{cpus + EGRESS_CPUS:g}",
        "container_capacity": str(capacity),  # passed explicitly (astra harness-card r1 B1)
        "launch_code_verified": "yes",  # launched from this code
        "_launch": launch,
    }


def contract(h: str) -> dict:
    """The plan's Build contract column for harness `h`: {parameter: value}."""
    text = PLAN.read_text()
    sec = text.split("## Build contract", 1)
    if len(sec) < 2:
        raise SystemExit(f"{PLAN}: no '## Build contract' section")
    rows = [ln for ln in sec[1].split("\n## ", 1)[0].splitlines() if ln.startswith("|")]
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    col = head.index(COLUMN[h])
    out = {}
    for ln in rows[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        out[cells[0].strip("`")] = re.sub(r"\s+", " ", cells[col].replace("`", "")).strip()
    return out


def diff(card: dict, want: dict) -> list[tuple[str, str, str]]:
    keys = [k for k in want] + [k for k in card if k not in want and not k.startswith("_")]
    return [(k, want.get(k, "(not in contract)"), card.get(k, "(not in card)"))
            for k in keys if want.get(k) != card.get(k)]


def write_card(h: str, card: dict, bad: list) -> Path:
    from datetime import UTC, datetime
    path = OUT / h / "design_card.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, cwd=ROOT, check=False).stdout.strip()
    lines = [f"# Design card: {COLUMN[h]} (harness replication)", "",
             (f"Generated {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} at {rev} by "
              "`scripts/week2_harness_card.py` from the code and launch arguments, not typed."),
             f"Launch: `{card['_launch']}`", "", "| Parameter | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in card.items() if not k.startswith("_")]
    lines += ["", "## Against the plan's Build contract", ""]
    lines += (["All rows match."] if not bad else
              ["| Parameter | Contract | Card |", "|---|---|---|"]
              + [f"| {k} | {w} | {c} |" for k, w, c in bad])
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("harness", choices=tuple(COLUMN))
    ap.add_argument("--running", action="store_true", help="cc: read the live bulk")
    ap.add_argument("--max-live", type=int, default=2)
    ap.add_argument("--memory-mb", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--cpus", type=float, default=2.0, help="cx/agy: each task container's CPUs")
    ap.add_argument("--capacity", type=int, default=8, help="cx/agy: containers on the host")
    ap.add_argument("--launch-rev", default="3457d31b", help="cc --running: the launch commit")
    a = ap.parse_args()
    host = None if a.host in (None, "", "local") else a.host
    if a.harness == "cc":
        card = card_cc(a.running, a.launch_rev)
    elif a.harness == "cx":
        card = card_cx(a.max_live, a.memory_mb, host, a.cpus, a.capacity)
    else:
        if a.memory_mb is None:
            ap.error("agy needs --memory-mb")
        card = card_agy(a.max_live, a.memory_mb, host, a.cpus, a.capacity)
    bad = diff(card, contract(a.harness))
    path = write_card(a.harness, card, bad)
    print(f"{path.relative_to(ROOT)}: {'MATCH' if not bad else f'{len(bad)} MISMATCH'}")
    for k, w, c in bad:
        print(f"  {k}: contract {w!r} != card {c!r}")
    if a.harness != "cc":
        print(f"launch: {card['_launch']}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
