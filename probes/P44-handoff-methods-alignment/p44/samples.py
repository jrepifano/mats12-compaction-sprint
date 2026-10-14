"""P44 random raw samples for note.md (AGENTS.md: random, seeded, every drawn row shown).

Population: the last scores.csv row per episode_id, planned arms only (not C36/C136), GLM on Together (glm-5.3-flash-tg) and
DeepSeek on Baidu (deepseek-v4-flash-bd), stage "chain", end reason not a rerun reason. Strata,
drawn with random.Random(SEED) over rows sorted by episode_id:
  A. 4 GLM cheating chains (any arm/seed) and 4 GLM non-cheating chains;
  B. 4 GLM handoffs that carry the P seed at gen >= 2 (features.csv seed_present True);
  C. 4 DeepSeek P-seed chains with a test edit.
For each: ids, outcome columns, the cheat generation's handoff text (first 900 chars) and the
test-file diff summary (lines changed in test_solution.py) or the final solution's first lines.
Writes results/samples.md.
"""
from __future__ import annotations

import csv
import difflib
import json
import random
from pathlib import Path

from p44 import contracts as C

SEED = 44
OUT = C.RESULTS_DIR / "samples.md"
RERUN = ("api_error", "replay_mismatch", "summary_failed")
FOLLOW_UP = ("C36", "C136")          # follow-up 1 is drawn from separately, if at all


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _scores() -> list[dict]:
    last = {r["episode_id"]: r for r in _rows(C.SCORES_CSV)}
    return sorted((r for r in last.values() if r["stage"] == "chain"
                   and r["end_reason"] not in RERUN and r["arm"] not in FOLLOW_UP),
                  key=lambda r: r["episode_id"])


def _artifact(eid: str) -> dict:
    slug, arm, seed, rest = eid.split("/")
    task, run = rest.split("|r")
    d = C.CHAINS_DIR / slug / arm / seed
    best = None
    for p in sorted(d.glob(f"{task}__r{run}*.json")):
        a = json.loads(p.read_text())
        if a["meta"]["episode_id"] == eid and a.get("end_reason") not in RERUN:
            best = a
    if best is None:
        raise FileNotFoundError(eid)
    return best


def _clip(s: str | None, n: int = 900) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " […]"


def _test_diff(a: dict) -> str:
    """The test file's first change, from the per-command snapshots (files hold {"text": ...})."""
    k = "/agent/test_solution.py"
    texts = [((sn.get("files") or {}).get(k) or {}).get("text") for sn in a.get("snapshots", [])]
    texts = [t for t in texts if t is not None]
    start = texts[0] if texts else None
    end = next((t for t in texts if t != start), None)
    if start is None or end is None:
        return "(no change in the snapshots)"
    d = [ln for ln in difflib.unified_diff(start.splitlines(), end.splitlines(), lineterm="",
                                           n=0) if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    return "\n".join(d[:12]) + ("\n[…]" if len(d) > 12 else "")


def _seed_lines(text: str | None, seed: str) -> list[str]:
    """Lines of a handoff text holding >= 2 of the seed's key phrases (features.py's rule, by line)."""
    keys = C.SEED_KEYS.get(seed, ())
    return [ln.strip() for ln in (text or "").splitlines()
            if sum(k in ln.lower() for k in keys) >= 2]


def _block(r: dict, a: dict, gen: int | None, show_seed: bool = False) -> list[str]:
    out = [f"### `{r['episode_id']}`", "",
           (f"arm {r['arm']} · seed {r['seed']} · end {r['end_reason']} · turns {r['n_turns']} · "
            f"gens {r['n_gens']} · cheat {r['cheat']} (gen {r['cheat_gen'] or '–'}) · test edit "
            f"{r['test_edit']} · special-casing {r['special_case']} · competence "
            f"{r['competence'] or '–'}"), ""]
    gens = a.get("generations", [])
    if gen is not None and gen < len(gens) and gens[gen].get("handoff_text"):
        out += [f"Handoff text entering gen {gen} ({gens[gen]['kind']}):", "", "```text",
                _clip(gens[gen]["handoff_text"]), "```", ""]
        if show_seed:
            out += ["Seed-bearing lines of that handoff:", "", "```text",
                    *(_seed_lines(gens[gen]["handoff_text"], r["seed"]) or ["(none by line)"]),
                    "```", ""]
    if r["test_edit"] == "True":
        out += ["Test-file change (removed −, added +):", "", "```diff", _test_diff(a), "```", ""]
    elif r["cheat"] == "True":
        out += ["Final solution (first 900 chars):", "", "```python",
                _clip(a.get("final_solution_py")), "```", ""]
    out += ["Post-session answer:", "", "```text", _clip((a.get("post_probe") or {}).get("reply"),
                                                          400), "```", ""]
    return out


def main() -> int:
    rng = random.Random(SEED)
    sc = _scores()
    glm = [r for r in sc if r["model_slug"] == "glm-5.3-flash-tg"]
    ds = [r for r in sc if r["model_slug"] == "deepseek-v4-flash-bd"]
    by_id = {r["episode_id"]: r for r in sc}
    feats = _rows(C.FEATURES_CSV)
    kept = sorted({(f["episode_id"], int(f["gen"])) for f in feats
                   if f["seed_present"] == "True" and int(f["gen"]) >= 2
                   and f["episode_id"] in by_id and by_id[f["episode_id"]]["seed"] == "P"
                   and f["episode_id"] in by_id and by_id[f["episode_id"]]["model_slug"]
                   == "glm-5.3-flash-tg"})
    strata = [
        ("A1. GLM, cheating chains (4 of {n})", [r for r in glm if r["cheat"] == "True"], 4),
        ("A2. GLM, non-cheating chains (4 of {n})", [r for r in glm if r["cheat"] == "False"], 4),
        ("C. DeepSeek, P seed with a test edit (4 of {n})",
         [r for r in ds if r["seed"] == "P" and r["test_edit"] == "True"], 4),
    ]
    lines = ["# P44 random raw samples", "",
             (f"Drawn by `p44/samples.py` (seed {SEED}); every drawn row is shown. Population: "
              "last scores.csv row per chain, rerun end reasons excluded."), ""]
    for title, pop, k in strata:
        draw = rng.sample(pop, min(k, len(pop)))
        lines += [f"## {title.format(n=len(pop))}", ""]
        for r in draw:
            g = int(r["cheat_gen"]) if r["cheat_gen"] else (1 if r["n_gens"] not in ("", "1")
                                                            else None)
            lines += _block(r, _artifact(r["episode_id"]), g)
    draw = rng.sample(kept, min(4, len(kept)))
    lines += [f"## B. GLM handoffs keeping the P seed at gen ≥ 2 (4 of {len(kept)})", ""]
    for eid, g in draw:
        lines += _block(by_id[eid], _artifact(eid), g, show_seed=True)
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
