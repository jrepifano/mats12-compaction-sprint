"""Shared helpers for the X3b report and review scripts (implementation plan §3 U5).

Reads only the frozen artifacts (`src/x3b/contracts.py`): episode artifacts, `episodes.csv`,
`detector.csv`, the s4world ledger. No network, no paid calls. Imported by the `scripts/x3b_*`
scripts that U5 owns; it is not part of the `x3b` package.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from x3b import contracts as C


def add_stage_arg(ap, required: bool = True) -> None:
    ap.add_argument("--stage", required=required, choices=[s.value for s in C.StageKey])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_script(name: str):
    """Import `scripts/<name>.py` as a module (X3's scripts are not a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- artifacts ---------------------------------------------------------------------------
def header_of(art: dict) -> str:
    """X3b keeps the header at top level and in meta.context (contracts: EpisodeSpec use)."""
    return art.get("header") or (art.get("meta") or {}).get("context") or ""


def kind_of(art: dict) -> str:
    return art.get("kind") or (art.get("meta") or {}).get("stage") or ""


def failed(art: dict) -> bool:
    return art.get("error") is not None or art.get("end_reason") == "api_error"


def load_artifacts(stage: C.StageKey) -> dict[str, tuple[Path, dict]]:
    """episode_id -> (path, artifact). Of several files for one id (X3's `.dupN` rule), the one
    without `duplicate_of` wins, else the first in path order."""
    out: dict[str, tuple[Path, dict]] = {}
    root = C.episodes_dir(stage)
    if not root.exists():
        return out
    for p in sorted(root.rglob("*.json")):
        art = json.loads(p.read_text())
        eid = (art.get("meta") or {}).get("episode_id")
        if not eid:
            continue
        if eid not in out or (out[eid][1].get("duplicate_of") and not art.get("duplicate_of")):
            out[eid] = (p, art)
    return out


def main_episode_ids(stage: C.StageKey, arts: dict[str, tuple[Path, dict]]) -> list[str]:
    """The stage's analysed main-run episodes, sorted: `episodes.csv` rows with
    analysis_role == "main" when the file exists (U3's collect: the latest settled block
    attempt), else every main-kind artifact that did not fail (a fallback for early use)."""
    rows = read_csv(C.episodes_csv(stage))
    if rows:
        return sorted(r["episode_id"] for r in rows if r.get("analysis_role") == "main"
                      and r["episode_id"] in arts)
    print(f"note: {C.episodes_csv(stage)} missing; using every non-failed main artifact",
          file=sys.stderr)
    return sorted(e for e, (_, a) in arts.items()
                  if kind_of(a) == C.RunKind.MAIN.value and not failed(a))


def detector_rows(stage: C.StageKey) -> dict[str, dict]:
    return {r["episode_id"]: r for r in read_csv(C.detector_csv(stage))}


def inputs_hashes(*paths: Path) -> dict[str, str]:
    """{path relative to the repo where possible: sha256} for the files that exist."""
    out = {}
    for p in paths:
        p = Path(p)
        if p.exists() and p.is_file():
            try:
                key = str(p.resolve().relative_to(ROOT))
            except ValueError:
                key = str(p)
            out[key] = sha256_file(p)
    return out


# --- ledger ------------------------------------------------------------------------------
_EST_RE = re.compile(r"estimated[^0-9$]{0,20}\$?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


def ledger_spend(exp: str, ledger: Path | None = None) -> dict:
    """Billed vs estimated spend for one exp id in the s4world ledger. An entry's estimated part
    is its `estimated_usd` field when present, else the first "estimated $x" in its note (U3's
    and U2's settle notes mark the estimated amount); the rest of its `usd` counts as billed."""
    from s4world import budget

    path = Path(ledger) if ledger else budget.ledger_path()
    total = est = 0.0
    n = 0
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("exp") != exp:
                continue
            usd = float(e.get("usd", 0.0))
            if isinstance(e.get("estimated_usd"), int | float):
                part = float(e["estimated_usd"])
            else:
                m = _EST_RE.search(e.get("note") or "")
                part = float(m.group(1)) if m else 0.0
            total += usd
            est += min(part, usd)
            n += 1
    return {"entries": n, "total_usd": round(total, 6), "estimated_usd": round(est, 6),
            "billed_usd": round(total - est, 6)}
