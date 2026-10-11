"""The probe registry: one `status.json` per probe directory, and the INDEX.md generated from them.

Why (2026-10-11): probe ids were taken by reading a hand-edited table, `probes/INDEX.md`, and
10-20 agents (Claude and Codex) now plan and close probes at once. Two agents reading the table
at the same moment take the same id, and two branches that each edit a row conflict at merge. So
each probe owns its own small file, `probes/P<NN>-<slug>/status.json`, which only that probe's
agent edits, and INDEX.md is generated from all of them (`scripts/probe_index.py`). Ids are
claimed by an atomic push to origin/main (`scripts/probe_claim.py`).

`status.json` schema (frozen; keys are written in this order, unknown extra keys after them and
preserved on rewrite):
  id          "P04"                      P + two digits; the directory is f"{id}-{slug}"
  slug        "nocot-sabotage-monitor"   [a-z0-9][a-z0-9-]*
  question    "Does ...?"                one line
  cap_usd     75 | 7.5 | null
  status      Planned | Running | Merged | Killed, optionally with a short note in
              parentheses, kept verbatim: "Planned (held)". `base_status` gives the base value.
  plan        "plan.md"                  path relative to the probe dir
  pr          16 | null                  GitHub PR number
  spend_usd   0.27 | null
  owner       "p04" | null               agent name
  updated_at  "2026-10-11T14:00:00Z"     UTC

Ids P00, P98 and P99 are reserved for infrastructure checks; `next_free_id` hands out P01-P97.

This module is plain data and text: no git, no filesystem walking beyond one file. The CLIs do
the rest.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

KEY_ORDER = (
    "id",
    "slug",
    "question",
    "cap_usd",
    "status",
    "plan",
    "pr",
    "spend_usd",
    "owner",
    "updated_at",
)
STATUSES = ("Planned", "Running", "Merged", "Killed")
# A base value plus an optional short note, stored and rendered verbatim: "Planned (held)".
STATUS_RE = re.compile(r"^(Planned|Running|Merged|Killed)( \([^()]+\))?$")
RESERVED = frozenset({"P00", "P98", "P99"})
CLAIMABLE = tuple(f"P{n:02d}" for n in range(1, 98))
REPO_URL = "https://github.com/jrepifano/mats12_neel_exploratory"
NONE_CELL = "—"

ID_RE = re.compile(r"^P\d{2}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DIR_RE = re.compile(r"^(P\d{2})-([a-z0-9][a-z0-9-]*)$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ROW_RE = re.compile(r"^\|\s*P\d{2}\s*\|")
PR_RE = re.compile(r"^\[#(\d+)\]\((.*)\)$")
# the plan link, then optionally the note link (added by hand on main for P03 and P06)
PLAN_RE = re.compile(r"^\[plan\]\(([^)]*)\)(?:, \[note\]\(([^)]*)\))?$")
MONEY_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
# a cap may carry a short note, e.g. "$45 (raised from $35)" (optional extra key "cap_note")
CAP_RE = re.compile(r"^\$(\d+(?:\.\d+)?)(?: \(([^()]+)\))?$")

HEADER = """# Probes

The registry of probe-tier experiments (AGENTS.md, "Probe tier"). Status: Planned / Running / \
Merged / Killed. Take a new id with `scripts/probe_claim.py claim`; it pushes the probe's \
`status.json` straight to main.

**Generated from each probe's `status.json` by `scripts/probe_index.py`; do not edit by hand. \
Regenerate after every merge.**

| Id | Question | Cap | Status | Plan | PR | Spend |
|---|---|---|---|---|---|---|
"""


class ProbeError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_status(status: str) -> str:
    """The base value: "Planned (held)" -> "Planned". Filter on this, not the full string."""
    return status.split(" (", 1)[0]


def dir_name(d: dict) -> str:
    return f"{d['id']}-{d['slug']}"


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(d: dict, where: str = "status") -> dict:
    """Check the schema; return a copy with keys in KEY_ORDER, then extras in their order."""
    if not isinstance(d, dict):
        raise ProbeError(f"{where}: not a JSON object")
    missing = [k for k in KEY_ORDER if k not in d]
    if missing:
        raise ProbeError(f"{where}: missing keys {missing}")
    errs = []
    if not isinstance(d["id"], str) or not ID_RE.match(d["id"]):
        errs.append(f"id {d['id']!r} is not P<two digits>")
    if not isinstance(d["slug"], str) or not SLUG_RE.match(d["slug"]):
        errs.append(f"slug {d['slug']!r} is not [a-z0-9][a-z0-9-]*")
    q = d["question"]
    if not isinstance(q, str) or not q.strip() or "\n" in q:
        errs.append("question must be a non-empty single line")
    for k in ("cap_usd", "spend_usd"):
        if d[k] is not None and not (_is_number(d[k]) and d[k] >= 0):
            errs.append(f"{k} must be a non-negative number or null")
    if not isinstance(d["status"], str) or not STATUS_RE.match(d["status"]):
        errs.append(f"status {d['status']!r} is not one of {STATUSES}, optionally + ' (note)'")
    p = d["plan"]
    if not isinstance(p, str) or not p or p.startswith("/") or ".." in Path(p).parts:
        errs.append(f"plan {p!r} must be a path relative to the probe dir")
    if d["pr"] is not None and not (
        isinstance(d["pr"], int) and not isinstance(d["pr"], bool) and d["pr"] > 0
    ):
        errs.append("pr must be a positive int or null")
    if d["owner"] is not None and not isinstance(d["owner"], str):
        errs.append("owner must be a string or null")
    if not isinstance(d["updated_at"], str) or not UTC_RE.match(d["updated_at"]):
        errs.append("updated_at must be YYYY-MM-DDTHH:MM:SSZ")
    if errs:
        raise ProbeError(f"{where}: " + "; ".join(errs))
    out = {k: d[k] for k in KEY_ORDER}
    out.update({k: v for k, v in d.items() if k not in out})
    return out


def dumps_status(d: dict) -> str:
    return json.dumps(validate(d), indent=2, ensure_ascii=False) + "\n"


def loads_status(text: str, where: str = "status", dirname: str | None = None) -> dict:
    """Parse and validate; if `dirname` is given, it must be f"{id}-{slug}"."""
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProbeError(f"{where}: invalid JSON: {e}") from e
    d = validate(d, where)
    if dirname is not None and dirname != dir_name(d):
        raise ProbeError(f"{where}: directory {dirname!r} does not match {dir_name(d)!r}")
    return d


def load_status(path: Path) -> dict:
    path = Path(path)
    return loads_status(path.read_text(encoding="utf-8"), str(path), path.parent.name)


def write_status(path: Path, d: dict) -> None:
    Path(path).write_text(dumps_status(d), encoding="utf-8")


def _num(x: float | None):
    """75.0 -> 75, so a whole-dollar cap stays an int in JSON."""
    if x is None:
        return None
    return int(x) if float(x).is_integer() else float(x)


def new_status(
    id: str, slug: str, question: str, cap_usd: float | None = None, owner: str | None = None
) -> dict:
    return validate(
        {
            "id": id,
            "slug": slug,
            "question": question.strip(),
            "cap_usd": _num(cap_usd),
            "status": "Planned",
            "plan": "plan.md",
            "pr": None,
            "spend_usd": None,
            "owner": owner,
            "updated_at": utc_now(),
        }
    )


# --- rendering ------------------------------------------------------------------------------


def fmt_cap(x) -> str:
    if x is None:
        return NONE_CELL
    return f"${int(x)}" if float(x).is_integer() else f"${x:.2f}"


def fmt_spend(x) -> str:
    return NONE_CELL if x is None else f"${x:.2f}"


def row_cells(d: dict) -> tuple[str, ...]:
    pr = NONE_CELL if d["pr"] is None else f"[#{d['pr']}]({REPO_URL}/pull/{d['pr']})"
    return (
        d["id"],
        d["question"].replace("|", "\\|"),
        fmt_cap(d["cap_usd"]) + (f" ({d['cap_note']})" if d.get("cap_note") else ""),
        d["status"],
        f"[plan]({dir_name(d)}/{d['plan']})"
        + (f", [note]({dir_name(d)}/{d['note']})" if d.get("note") else ""),
        pr,
        fmt_spend(d["spend_usd"]),
    )


def render_row(d: dict) -> str:
    return "| " + " | ".join(row_cells(d)) + " |"


def id_number(pid: str) -> int:
    return int(pid[1:])


def render_index(statuses: list[dict]) -> str:
    seen: dict[str, str] = {}
    for d in statuses:
        if d["id"] in seen:
            raise ProbeError(f"two probes claim {d['id']}: {seen[d['id']]} and {dir_name(d)}")
        seen[d["id"]] = dir_name(d)
    rows = [render_row(d) for d in sorted(statuses, key=lambda d: id_number(d["id"]))]
    return HEADER + "".join(r + "\n" for r in rows)


# --- parsing the table back -----------------------------------------------------------------


def split_cells(line: str) -> tuple[str, ...]:
    body = line.strip()
    if not (body.startswith("|") and body.endswith("|")):
        raise ProbeError(f"not a table row: {line!r}")
    return tuple(c.strip() for c in re.split(r"(?<!\\)\|", body[1:-1]))


def _parse_money(cell: str, what: str, lineno: int):
    if cell == NONE_CELL:
        return None
    m = MONEY_RE.match(cell)
    if not m:
        raise ProbeError(f"INDEX.md:{lineno}: cannot parse {what} {cell!r}")
    return _num(float(m.group(1)))


def parse_index(text: str) -> dict[str, dict]:
    """Rows keyed by id: question, cap_usd, status, plan ("<dir>/plan.md"), pr, spend_usd, cells."""
    out: dict[str, dict] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not ROW_RE.match(line):
            continue
        cells = split_cells(line)
        if len(cells) != 7:
            raise ProbeError(f"INDEX.md:{lineno}: expected 7 cells, got {len(cells)}")
        pid, question, cap, status, plan, pr, spend = cells
        m_plan = PLAN_RE.match(plan)
        if not m_plan:
            raise ProbeError(f"INDEX.md:{lineno}: cannot parse plan cell {plan!r}")
        if pr == NONE_CELL:
            pr_num = None
        else:
            m_pr = PR_RE.match(pr)
            if not m_pr:
                raise ProbeError(f"INDEX.md:{lineno}: cannot parse PR cell {pr!r}")
            pr_num = int(m_pr.group(1))
        if pid in out:
            raise ProbeError(f"INDEX.md:{lineno}: {pid} appears twice")
        out[pid] = {
            "id": pid,
            "question": question.replace("\\|", "|"),
            "cap_usd": _parse_money(CAP_RE.sub(r"$\1", cap), "cap", lineno),
            "cap_note": (CAP_RE.match(cap).group(2) if CAP_RE.match(cap) else None),
            "status": status,
            "plan": m_plan.group(1),
            "note": m_plan.group(2),
            "pr": pr_num,
            "spend_usd": _parse_money(spend, "spend", lineno),
            "cells": cells,
            "lineno": lineno,
        }
    return out


def status_from_row(row: dict, base: dict | None = None) -> dict:
    """A status dict from an INDEX row. `base` (an existing status) keeps its owner and extras."""
    plan_dir, _, plan_rel = row["plan"].partition("/")
    m = DIR_RE.match(plan_dir)
    if not m or not plan_rel:
        raise ProbeError(f"{row['id']}: plan link {row['plan']!r} is not <P<NN>-slug>/<file>")
    if m.group(1) != row["id"]:
        raise ProbeError(f"{row['id']}: plan link points at {plan_dir}")
    d = dict(base) if base else {"owner": None}
    if base and base["slug"] != m.group(2):
        raise ProbeError(f"{row['id']}: INDEX slug {m.group(2)!r} != status slug {base['slug']!r}")
    d.update(
        id=row["id"],
        slug=m.group(2),
        question=row["question"],
        cap_usd=row["cap_usd"],
        status=row["status"],
        plan=plan_rel,
        pr=row["pr"],
        spend_usd=row["spend_usd"],
        updated_at=utc_now(),
    )
    d.pop("note", None)
    d.pop("cap_note", None)
    if row.get("cap_note"):
        d["cap_note"] = row["cap_note"]
    if row.get("note"):  # optional extra key "note": the note's path relative to the probe dir
        note_dir, _, note_rel = row["note"].partition("/")
        if note_dir != plan_dir or not note_rel:
            raise ProbeError(f"{row['id']}: note link {row['note']!r} is not in {plan_dir}/")
        d["note"] = note_rel
    return validate(d, f"{row['id']} (from INDEX.md)")


def drift(index_text: str, statuses: list[dict]) -> tuple[list[str], list[str]]:
    """(ids with an INDEX row but no status.json, ids whose row differs from its status.json)."""
    rows = parse_index(index_text)
    by_id = {d["id"]: d for d in statuses}
    missing = [i for i in rows if i not in by_id]
    differing = [i for i in rows if i in by_id and row_cells(by_id[i]) != rows[i]["cells"]]
    key = id_number
    return sorted(missing, key=key), sorted(differing, key=key)


# --- ids ------------------------------------------------------------------------------------


def ids_in_names(names) -> set[str]:
    """Ids of the names that look like probe dirs (`P<NN>-...`)."""
    out = set()
    for n in names:
        m = re.match(r"^(P\d{2})-", n)
        if m:
            out.add(m.group(1))
    return out


def next_free_id(used: set[str]) -> str:
    for pid in CLAIMABLE:
        if pid not in used:
            return pid
    raise ProbeError("no free probe id in P01-P97")
