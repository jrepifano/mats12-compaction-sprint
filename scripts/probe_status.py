"""Live, read-only view of every probe: one row per probe (AGENTS.md "Two tiers", 2026-10-11).

While 10-20 probe agents run at once (Claude and Codex, each in its own git worktree, each in its
own Orca terminal), this answers "what is each probe doing?" from local files alone:

- Probes: the union of `probes/P<NN>-<slug>/` directories across ALL checkouts (main and every
  worktree from `git worktree list`), with or without a status.json.
- Worktree: the checkout on branch `probe/P<NN>-...` (id matched case-insensitively), or failing
  that the one whose directory name starts with `p<nn>-`.
- Status, question, cap, owner: `probes/<dir>/status.json`, the probe's worktree copy first, then
  main's, then any other checkout's. Missing: status "?".
- Agent: the probe's Orca terminal (AGENTS.md "One Orca terminal per probe", 2026-10-20), from
  `orca terminal list --json`: a live terminal whose worktree path is the probe's worktree, else
  one whose title starts with "P<NN> " (several: the one running an agent, then the most recent
  output). Its agent (`agentIdentity`, e.g. claude or codex) and state come from
  `orca worktree ps --json`, matched on the pane key `<tabId>:<leafId>`; the state is reported by
  the Orca hooks in the agent CLI. No hook state: "live". Each call has a 5 s timeout. Orca
  missing, not running, slow or printing non-JSON: "-" and one note on stderr.
- Last log: the last non-empty line of `probes/<dir>/log.md` (worktree, then main) and its mtime.
- Spend: the sum of `usd` over rows with `exp == <id>` in the union of every checkout's
  `logs/costs.jsonl` (merge_ledger's `read_ledger` + `union`, so rows shared from before a branch
  point count once). Cap: status.json `cap_usd`, else probe.toml `hard_cap_usd`. This is recorded
  spend: a live pod's GPU charge reaches the ledger only at `down` (its reservation rows are $0).
- Pod: `probes/<dir>/logs/runner_state.json` (gitignored, written by scripts/probe_pod.py in the
  checkout that runs the pod): `phase`, `pod_id`, `last_go_outcome`.

Strictly read-only: no writes, no network, no ssh, no RunPod calls. The two Orca calls are read-only
list commands (they talk to the local Orca app) and go through the module-level `RUN`, and the git
call through `WORKTREES`, so tests can inject both. The Orca CLI is `$ORCA_BIN`, else `orca` on PATH,
else the copy inside /Applications/Orca.app.

Usage:
  uv run python scripts/probe_status.py [--json] [--repo PATH] [--no-orca]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True  # importing merge_ledger must not write scripts/__pycache__
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_ledger as ml

REPO = Path(__file__).resolve().parents[1]
DIR_RE = re.compile(r"P(\d{2})-[^/]+")
ORCA_TIMEOUT_S = 5
ORCA_APP_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"
WIDTH = 160  # table width when stdout is not a terminal (else the terminal's width)
LOG_MAX, LOG_MIN = 80, 30  # LAST LOG text width in the table

RUN = subprocess.run  # orca calls; tests inject a fake
WORKTREES = ml.worktrees  # git call; tests may inject a fake


def note(msg: str) -> None:
    print(f"probe_status: {msg}", file=sys.stderr)


@dataclass
class Row:
    id: str
    slug: str
    status: str
    question: str | None
    agent: str | None  # the agent CLI in the probe's Orca terminal, e.g. claude or codex
    agent_status: str | None  # Orca's hook state (e.g. working, done), else "live"
    terminal: str | None  # Orca terminal handle (runtime-scoped: changes after an Orca restart)
    terminal_title: str | None
    worktree: str | None  # relative to the main checkout
    branch: str | None
    log_last_line: str | None
    log_mtime_utc: str | None
    spend_usd: float
    cap_usd: float | None
    pod_phase: str | None
    pod_id: str | None
    last_go_outcome: str | None


# --- inputs ---------------------------------------------------------------------------------------


def checkouts(repo: Path) -> list[ml.Worktree]:
    """Live, non-bare checkouts; git lists the main one first."""
    return [w for w in WORKTREES(repo) if not w.bare and w.path.is_dir()]


def orca_bin() -> str:
    return os.environ.get("ORCA_BIN") or shutil.which("orca") or ORCA_APP_BIN


def orca_json(args: list[str], what: str) -> dict | None:
    """`orca <args> --json`'s `result`, or None with one note on stderr (never raises)."""
    try:
        r = RUN(
            [orca_bin(), *args, "--json"],
            capture_output=True,
            text=True,
            timeout=ORCA_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        note(f"orca CLI not found; {what} show '-'")
        return None
    except subprocess.TimeoutExpired:
        note(f"orca {' '.join(args[:2])} timed out after {ORCA_TIMEOUT_S} s; {what} show '-'")
        return None
    except OSError as e:
        note(f"orca {' '.join(args[:2])} failed ({e}); {what} show '-'")
        return None
    if r.returncode != 0:
        note(f"orca {' '.join(args[:2])} exited {r.returncode} (is Orca running?); {what} show '-'")
        return None
    try:
        d = json.loads(r.stdout)
        if not d.get("ok", True):
            raise ValueError("ok is false")
        res = d["result"]
        if not isinstance(res, dict):
            raise TypeError("result is not an object")
        return res
    except (ValueError, KeyError, TypeError, AttributeError):
        note(f"orca {' '.join(args[:2])} printed unexpected output; {what} show '-'")
        return None


def orca_terminals() -> list[dict] | None:
    """Live Orca terminals, or None when Orca is unavailable."""
    res = orca_json(["terminal", "list", "--limit", "500"], "agent columns")
    if res is None:
        return None
    terms = res.get("terminals")
    if not isinstance(terms, list):
        note("orca terminal list printed unexpected output; agent columns show '-'")
        return None
    return [t for t in terms if isinstance(t, dict) and t.get("handle")]


def orca_agent_states() -> dict[str, dict]:
    """Hook-reported agents by pane key (`<tabId>:<leafId>`); {} when unavailable."""
    res = orca_json(["worktree", "ps", "--limit", "500"], "agent states")
    out: dict[str, dict] = {}
    wts = res.get("worktrees") if res else None
    for w in wts if isinstance(wts, list) else []:
        agents = w.get("agents") if isinstance(w, dict) else None
        for a in agents if isinstance(agents, list) else []:
            if isinstance(a, dict) and a.get("paneKey"):
                out[str(a["paneKey"])] = a
    return out


def same_path(a: str | None, b: Path) -> bool:
    if not a:
        return False
    try:
        return Path(a).resolve() == b.resolve()
    except OSError:
        return False


def probe_terminal(pid: str, wt_path: Path | None, terms: list[dict]) -> dict | None:
    """The probe's terminal: worktree path match, else a "P<NN> " title; agent first, then newest."""
    hits = [t for t in terms if wt_path is not None and same_path(t.get("worktreePath"), wt_path)]
    if not hits:
        hits = [t for t in terms if str(t.get("title") or "").upper().startswith(f"{pid} ")]
    if not hits:
        return None
    return max(hits, key=lambda t: (bool(t.get("agentIdentity")), t.get("lastOutputAt") or 0))


def spend_by_exp(cos: list[ml.Worktree]) -> dict[str, float]:
    sources = []
    for w in cos:
        try:
            sources.append(ml.read_ledger(w.path / ml.LEDGER_REL))
        except (ml.LedgerError, OSError) as e:
            note(f"ledger skipped: {e}")
    return ml.totals(ml.union(sources))


def read_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def first_existing(dirs: list[Path], rel: str) -> Path | None:
    for d in dirs:
        if (d / rel).is_file():
            return d / rel
    return None


def toml_cap(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        v = tomllib.loads(path.read_text(encoding="utf-8")).get("hard_cap_usd")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return float(v) if isinstance(v, int | float) else None


MARKER_RE = re.compile(r"^(?:[#>*+\-]+\s*|\d+[.)]\s+)+")


def last_log(path: Path | None) -> tuple[str | None, str | None]:
    """(last non-empty line without markdown bullet/heading/bold markers, mtime as UTC ISO)."""
    if path is None:
        return None, None
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        mtime = path.stat().st_mtime
    except OSError:
        return None, None
    text = next((ln for ln in reversed(lines) if ln), None)
    if text is not None:
        text = MARKER_RE.sub("", text).replace("**", "").strip() or text
    when = datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return text, when


# --- assembly -------------------------------------------------------------------------------------


def own_worktree(pid: str, cos: list[ml.Worktree]) -> ml.Worktree | None:
    """The probe's checkout: branch probe/P<NN>-..., else directory name p<nn>-..."""
    pre = f"refs/heads/probe/{pid.lower()}-"
    for w in cos[1:]:
        if w.branch and w.branch.lower().startswith(pre):
            return w
    for w in cos[1:]:
        if w.path.name.lower().startswith(f"{pid.lower()}-"):
            return w
    return None


def probe_dirs(cos: list[ml.Worktree]) -> dict[str, dict[Path, str]]:
    """id -> {checkout path: probe dir name} over every checkout."""
    out: dict[str, dict[Path, str]] = {}
    for w in cos:
        root = w.path / "probes"
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            m = DIR_RE.fullmatch(d.name)
            if m and d.is_dir():
                out.setdefault(f"P{m.group(1)}", {}).setdefault(w.path, d.name)
    return out


def collect(repo: Path, use_orca: bool = True) -> list[Row]:
    cos = checkouts(repo)
    if not cos:
        raise SystemExit(f"probe_status: no checkouts found for {repo}")
    main = cos[0]
    terms = orca_terminals() if use_orca else None
    states = orca_agent_states() if terms else {}
    spend = spend_by_exp(cos)
    rows = []
    for pid, where in sorted(probe_dirs(cos).items()):
        wt = own_worktree(pid, cos)
        # Search order: own worktree, main, then any other checkout holding the directory.
        order = [c for c in [wt.path if wt else None, main.path] if c is not None]
        order += [p for p in where if p not in order]
        dirs = [c / "probes" / where[c] for c in order if c in where]
        name = where[order[0]] if order[0] in where else next(iter(where.values()))
        st_path = first_existing(dirs, "status.json")
        st = (read_json(st_path) if st_path else None) or {}
        term = probe_terminal(pid, wt.path if wt else None, terms) if terms else None
        hook = states.get(f"{term.get('tabId')}:{term.get('leafId')}", {}) if term else {}
        agent_status = None
        if term:
            agent_status = hook.get("state") or (
                "live" if term.get("connected") else "disconnected"
            )
        cap = st.get("cap_usd")
        cap = (
            float(cap)
            if isinstance(cap, int | float)
            else toml_cap(first_existing(dirs, "probe.toml"))
        )
        log_line, log_mtime = last_log(first_existing(dirs, "log.md"))
        pod = (
            read_json(p) if (p := first_existing(dirs, "logs/runner_state.json")) else None
        ) or {}
        rows.append(
            Row(
                id=pid,
                slug=str(st.get("slug") or name.split("-", 1)[1]),
                status=str(st.get("status") or "?"),
                question=st.get("question"),
                agent=(term.get("agentIdentity") or hook.get("agentType") or "shell")
                if term
                else None,
                agent_status=agent_status,
                terminal=term.get("handle") if term else None,
                terminal_title=term.get("title") if term else None,
                worktree=os.path.relpath(wt.path, main.path) if wt else None,
                branch=wt.branch.removeprefix("refs/heads/") if wt and wt.branch else None,
                log_last_line=log_line,
                log_mtime_utc=log_mtime,
                spend_usd=round(spend.get(pid, 0.0), 4),
                cap_usd=cap,
                pod_phase=pod.get("phase"),
                pod_id=pod.get("pod_id"),
                last_go_outcome=pod.get("last_go_outcome"),
            )
        )
    return rows


# --- output ---------------------------------------------------------------------------------------


def clip(s: str, n: int, left: bool = False) -> str:
    """Truncate to n columns; `left` keeps the tail (the distinctive part of a path)."""
    if len(s) <= n:
        return s
    return "…" + s[-(n - 1) :] if left else s[: n - 1] + "…"


def pod_cell(r: Row) -> str:
    if not r.pod_phase:
        return "-"
    s = r.pod_phase + (f"/{r.last_go_outcome}" if r.last_go_outcome else "")
    if r.pod_id and r.pod_phase not in ("down", "idle"):
        s += f" pod={r.pod_id}"
    return s


def money(x: float | None) -> str:
    """A cap: whole dollars when whole."""
    return "-" if x is None else f"${x:.2f}" if x % 1 else f"${x:.0f}"


def table(rows: list[Row], width: int = WIDTH) -> str:
    head = ["ID", "STATUS", "AGENT(status)", "BRANCH", "WORKTREE", "SPEND/CAP", "POD"]
    maxw = [3, 8, 16, 24, 26, 15, 24]
    cells = []
    for r in rows:
        agent = f"{r.agent}/{r.agent_status or '?'}" if r.agent else "-"
        cells.append(
            [
                r.id,
                r.status,
                agent,
                r.branch or "-",
                r.worktree or "-",
                f"${r.spend_usd:.2f}/{money(r.cap_usd)}",
                pod_cell(r),
            ]
        )
    widths = [max([len(h)] + [len(c[i]) for c in cells]) for i, h in enumerate(head)]
    widths = [min(w, m) for w, m in zip(widths, maxw, strict=True)]
    left = {3, 4}  # paths: keep the tail
    used = sum(widths) + 2 * len(widths)
    logw = max(LOG_MIN, min(LOG_MAX, width - used - 12))

    def line(vals: list[str], log: str) -> str:
        parts = [
            clip(v, w, i in left).ljust(w)
            for i, (v, w) in enumerate(zip(vals, widths, strict=True))
        ]
        return ("  ".join(parts) + "  " + log).rstrip()

    out = [line(head, "LAST LOG (time)")]
    for r, c in zip(rows, cells, strict=True):
        when = r.log_mtime_utc[5:16].replace("T", " ") if r.log_mtime_utc else ""
        log = f"{when} {clip(r.log_last_line, logw)}" if r.log_last_line else "-"
        out.append(line(c, log))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="full fields as a JSON list")
    ap.add_argument("--repo", type=Path, default=REPO, help="any checkout of the repo")
    ap.add_argument("--no-orca", action="store_true", help="skip the Orca terminal lookup")
    a = ap.parse_args(argv)
    rows = collect(a.repo.resolve(), use_orca=not a.no_orca)
    if a.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
    elif rows:
        print(table(rows, shutil.get_terminal_size((WIDTH, 24)).columns))
    else:
        print("no probes found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
