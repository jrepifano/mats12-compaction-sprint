"""Live, read-only view of every probe: one row per probe (AGENTS.md "Two tiers", 2026-10-11).

While 10-20 probe agents run at once (Claude and Codex, each in its own git worktree, each driven by
a herdr agent), this answers "what is each probe doing?" from local files alone:

- Probes: the union of `probes/P<NN>-<slug>/` directories across ALL checkouts (main and every
  worktree from `git worktree list`), with or without a status.json.
- Worktree: the checkout on branch `probe/P<NN>-...` (id matched case-insensitively), or failing
  that the one whose directory name starts with `p<nn>-`.
- Status, question, cap, owner: `probes/<dir>/status.json`, the probe's worktree copy first, then
  main's, then any other checkout's. Missing: status "?".
- Agent: the herdr agent named `p<nn>`, else the one named by status.json `owner` (`herdr agent
  list`, 5 s timeout). herdr missing, slow or printing non-JSON: "-" and one note on stderr.
- Last log: the last non-empty line of `probes/<dir>/log.md` (worktree, then main) and its mtime.
- Spend: the sum of `usd` over rows with `exp == <id>` in the union of every checkout's
  `logs/costs.jsonl` (merge_ledger's `read_ledger` + `union`, so rows shared from before a branch
  point count once). Cap: status.json `cap_usd`, else probe.toml `hard_cap_usd`. This is recorded
  spend: a live pod's GPU charge reaches the ledger only at `down` (its reservation rows are $0).
- Pod: `probes/<dir>/logs/runner_state.json` (gitignored, written by scripts/probe_pod.py in the
  checkout that runs the pod): `phase`, `pod_id`, `last_go_outcome`.

Strictly read-only: no writes, no network, no ssh, no RunPod calls. The herdr call goes through the
module-level `RUN` and the git call through `WORKTREES`, so tests can inject both.

Usage:
  uv run python scripts/probe_status.py [--json] [--repo PATH] [--no-herdr]
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
HERDR_TIMEOUT_S = 5
WIDTH = 160  # table width when stdout is not a terminal (else the terminal's width)
LOG_MAX, LOG_MIN = 80, 30  # LAST LOG text width in the table

RUN = subprocess.run  # herdr call; tests inject a fake
WORKTREES = ml.worktrees  # git call; tests may inject a fake


def note(msg: str) -> None:
    print(f"probe_status: {msg}", file=sys.stderr)


@dataclass
class Row:
    id: str
    slug: str
    status: str
    question: str | None
    agent: str | None
    agent_status: str | None
    pane_id: str | None
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


def herdr_agents() -> dict[str, dict] | None:
    """Named herdr agents by name, or None when herdr is unavailable (never raises)."""
    try:
        r = RUN(
            ["herdr", "agent", "list"],
            capture_output=True,
            text=True,
            timeout=HERDR_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        note("herdr not installed; agent columns show '-'")
        return None
    except subprocess.TimeoutExpired:
        note(f"herdr agent list timed out after {HERDR_TIMEOUT_S} s; agent columns show '-'")
        return None
    except OSError as e:
        note(f"herdr agent list failed ({e}); agent columns show '-'")
        return None
    if r.returncode != 0:
        note(f"herdr agent list exited {r.returncode}; agent columns show '-'")
        return None
    try:
        agents = json.loads(r.stdout)["result"]["agents"]
        if not isinstance(agents, list):
            raise TypeError("agents is not a list")
        return {a["name"]: a for a in agents if isinstance(a, dict) and a.get("name")}
    except (ValueError, KeyError, TypeError):
        note("herdr agent list printed unexpected output; agent columns show '-'")
        return None


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


def collect(repo: Path, use_herdr: bool = True) -> list[Row]:
    cos = checkouts(repo)
    if not cos:
        raise SystemExit(f"probe_status: no checkouts found for {repo}")
    main = cos[0]
    agents = herdr_agents() if use_herdr else None
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
        agent = None
        if agents is not None:
            agent = agents.get(pid.lower()) or (
                agents.get(st["owner"]) if st.get("owner") else None
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
                agent=agent.get("name") if agent else None,
                agent_status=agent.get("agent_status") if agent else None,
                pane_id=agent.get("pane_id") if agent else None,
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
    ap.add_argument("--no-herdr", action="store_true", help="skip the herdr agent lookup")
    a = ap.parse_args(argv)
    rows = collect(a.repo.resolve(), use_herdr=not a.no_herdr)
    if a.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
    elif rows:
        print(table(rows, shutil.get_terminal_size((WIDTH, 24)).columns))
    else:
        print("no probes found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
