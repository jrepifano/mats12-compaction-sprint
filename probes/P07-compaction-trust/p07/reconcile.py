"""Reconcile P07's ledger rows for agent episodes against the billed cost of unique episodes.

Until 2026-10-11 ~20:30 UTC, concurrent `run.py` processes shared one Inspect log directory per
model, and each settled every new .eval file in it, so some episodes were extracted (as
identical `.dupN` copies) and charged twice. The true billed cost is the sum over unique
(eval log file, episode id) pairs. Ledger rows are left as written (append-only); this prints
the over-count so the note and probes/INDEX.md can state both.

  PYTHONPATH=probes/P07-compaction-trust:src:scripts uv run --group swarm python -m p07.reconcile
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from p07 import contracts as C


def phase_of(eid: str) -> str:
    """Planned grid, or which follow-up, from the episode id (P07|model|wrapper|cell|ctx)."""
    _, _, wrapper, cell = eid.split("|")[:4]
    if cell in ("summary_P", "user_P") and wrapper == "codex":
        return "follow-up 2 (permission: summary, user)"
    if cell.endswith("_P"):
        return "follow-up 3 (permission: tool, file, other wrappers)"
    if wrapper != "codex":
        return "follow-up 1 (other wrappers)"
    return "planned grid (incl. smoke)"


def unique_billed() -> dict[str, float]:
    seen: dict[tuple[str, str], float] = {}
    for p in C.EPISODES_DIR.rglob("*.json"):
        if "/mock/" in str(p):
            continue
        a = json.loads(p.read_text())
        key = (Path(a.get("eval_log") or "").name, a["meta"]["episode_id"])
        b = a.get("billed_usd")
        seen[key] = float(b if b is not None else (a.get("spent_at_ceiling") or 0.0))
    out: dict[str, float] = {}
    for (_, eid), usd in seen.items():
        for k in (eid.split("|")[1], f"{phase_of(eid)} | {eid.split('|')[1]}"):
            out[k] = out.get(k, 0.0) + usd
    return out


def ledger_episode_rows() -> dict[str, float]:
    from emx import spend

    out: dict[str, float] = {}
    for e in spend.read_entries():
        if e.get("exp") == C.EXP and (e.get("note") or "").startswith("P07 episodes"):
            slug = e["note"].split()[2].split("/")[0]
            out[slug] = out.get(slug, 0.0) + float(e["usd"])
    return out


def main() -> int:
    true, led = unique_billed(), ledger_episode_rows()
    phases = {k: round(v, 4) for k, v in sorted(true.items()) if " | " in k}
    true = {k: v for k, v in true.items() if " | " not in k}
    rows = {s: {"ledger_usd": round(led.get(s, 0.0), 4), "unique_billed_usd": round(true.get(s, 0.0), 4),
                "overcount_usd": round(led.get(s, 0.0) - true.get(s, 0.0), 4)}
            for s in sorted(set(true) | set(led))}
    out = {"by_model": rows, "unique_billed_by_phase": phases}
    print(json.dumps(out, indent=1))
    (C.RESULTS_DIR / "ledger_reconcile.json").write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
