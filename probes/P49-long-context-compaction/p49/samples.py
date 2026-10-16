"""P49 random raw samples for the note (AGENTS.md: random, never cherry-picked).

The draw is `random.Random(C.SEED)` over sorted ids: k chains per arm (the first assistant turn
after the transition, the first test-file edit if any, the final assistant turn), k summaries
per compaction method (first 1,500 characters), and k chains' raw belief answers. Writes
results/samples.md.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from p49 import contracts as C

EXCERPT = 1_200
SUMMARY_CHARS = 1_500
MUTATING = ("create", "str_replace", "insert", "undo_edit")
SHELL_WRITE = (">", "sed -i", "rm ", "mv ", "cp ", "tee ", "perl -i", "cat <<", "python -c",
               "python3 -c", "truncate")


def _clip(s: str | None, n: int = EXCERPT) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n[... {len(s) - n} more characters]"


def _args(call: dict) -> dict:
    a = call.get("args") or call.get("arguments") or {}
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            a = {"command": a}
    return a if isinstance(a, dict) else {"value": a}


def test_edit(tool_calls: list[dict]) -> dict | None:
    """The first call that may write the test file (editor mutation on test_solution.py, or a
    shell command naming it with a write operator). Lexical, for showing samples only."""
    for c in tool_calls or []:
        a = _args(c)
        name = c.get("name")
        if name == "str_replace_editor" and str(a.get("path", "")).endswith("test_solution.py") \
                and a.get("command") in MUTATING:
            return c
        cmd = str(a.get("command", ""))
        if name == "execute_bash" and "test_solution" in cmd and any(w in cmd
                                                                     for w in SHELL_WRITE):
            return c
    return None


def _assistant_text(m: dict) -> str:
    parts = [m.get("content") or ""]
    for tc in m.get("tool_calls") or []:
        f = tc.get("function") or {}
        parts.append(f"[tool call {f.get('name')}] {f.get('arguments')}")
    return "\n".join(p for p in parts if p)


def draw(ids: list[str], k: int, rng: random.Random) -> list[str]:
    ids = sorted(ids)
    return sorted(rng.sample(ids, min(k, len(ids))))


def chain_block(rec: dict, cheat: object = None) -> list[str]:
    spec = rec.get("spec") or {}
    tr = rec.get("transcript") or []
    asst = [m for m in tr if m.get("role") == "assistant"]
    head = (f"arm {spec.get('arm')}, level {spec.get('level')}, task {spec.get('task_id')}; "
            f"end {rec.get('end_reason')}, {rec.get('n_turns')} turns, cheat {cheat}")
    out = [f"### {spec.get('chain_id')}", "", head, ""]
    if asst:
        out += ["First assistant turn after the transition:", "", "```",
                _clip(_assistant_text(asst[0])), "```", ""]
    te = test_edit(rec.get("tool_calls") or [])
    if te:
        out += [f"First test-file write (turn {te.get('turn')}):", "", "```",
                _clip(json.dumps(_args(te))), "```", ""]
    if len(asst) > 1:
        out += ["Final assistant turn:", "", "```", _clip(_assistant_text(asst[-1])), "```", ""]
    return out


def build(chains_dir: Path, compactions_dir: Path, *, k: int = 3, seed: int = C.SEED,
          cheats: dict[str, object] | None = None) -> str:
    rng = random.Random(seed)
    cheats = cheats or {}
    recs = {p.stem: p for p in sorted(Path(chains_dir).glob("*.json")) if "." not in p.stem}
    by_arm: dict[str, list[str]] = defaultdict(list)
    for cid in recs:
        parts = cid.split("-")
        if len(parts) >= 4:
            by_arm[parts[-2]].append(cid)
    lines = ["# P49 random samples", "",
             f"Seeded draw (random.Random({seed}) over sorted ids), k = {k}; not cherry-picked.",
             "", "## Chains by arm", ""]
    for arm in C.ARMS:
        ids = draw(by_arm.get(arm, []), k, rng)
        if not ids:
            continue
        lines += [f"## Arm {arm}", ""]
        for cid in ids:
            lines += chain_block(json.loads(recs[cid].read_text()), cheats.get(cid))
    comps: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(Path(compactions_dir).glob("*/*.json")):
        if p.name.endswith(".replay.json"):
            continue
        comps[p.stem.rsplit("_k", 1)[0]].append(p)
    lines += ["## Summaries by compaction", ""]
    for name in sorted(comps):
        paths = {f"{p.parent.name}/{p.stem}": p for p in comps[name]}
        for key in draw(list(paths), k, rng):
            d = json.loads(paths[key].read_text())
            title = (f"### {key} ({d.get('method')}, k={d.get('k')}, source "
                     f"{d.get('source')}, {d.get('summary_tokens')} tokens)")
            lines += [title, "", "```",
                      _clip(d.get("summary_text"), SUMMARY_CHARS), "```", ""]
    lines += ["## Belief answers", ""]
    for cid in draw(list(recs), k, rng):
        b = json.loads(recs[cid].read_text()).get("beliefs") or {}
        for q in ("B1", "B3"):
            v = b.get(q) or {}
            lines += [f"**{cid} {q}** (value {v.get('value')}):", "", "```",
                      _clip(v.get("raw"), 800), "```", ""]
    return "\n".join(lines)


def write(out: Path | None = None, **kw) -> Path:
    out = C.RESULTS / "samples.md" if out is None else out
    text = build(kw.pop("chains_dir", C.CHAINS_DIR), kw.pop("compactions_dir", C.COMPACTIONS_DIR),
                 **kw)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n")
    return out
