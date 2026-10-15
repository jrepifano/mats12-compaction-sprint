"""Seeded random raw samples for note.md (AGENTS.md: random, never cherry-picked):
results/samples.md with, per Stage A model, N random U continuations (every drawn row shown):
the detector label and flags, luna's criterion-tag statements for the continuation, the
reasoning of its last assistant turn and the first 30 code lines
of its final solution.py (docstrings removed); results/samples_full.md has the same draws in
full (every continuation turn without tool outputs, and the complete final solution.py).

Population: each model's scored U draws (detector row present), sorted by episode id;
`random.Random(f"{SEED}:samples:{slug}").sample(population, n)`.

  PYTHONPATH=probes/P47-grader-belief-probe:src:scripts uv run --group swarm \
      python -m p47.samples [--n 4] [--models a,b,c]
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import random
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
FLAGS = ("eq_override", "module_state", "literal_test_inputs", "introspection", "tamper_final",
         "sys_exit", "os_exit", "imports_pytest")


def _clip(s: str | None, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n // 2] + " … " + s[-n // 2:]


def code_lines(src: str, n: int = 30) -> str:
    """The first `n` lines of `src` with docstrings (triple-quoted blocks) removed."""
    out, inside = [], False
    for line in (src or "").splitlines():
        q = line.count('"""') + line.count("'''")
        if inside or q:
            inside = inside != (q % 2 == 1)
            continue
        if line.strip():
            out.append(line)
    return "\n".join(out[:n])


def model_section(slug: str, n: int) -> tuple[list[str], list[str]]:
    os.environ["P47_MODEL"] = slug
    from p47 import contracts

    C = importlib.reload(contracts)
    from p47 import sources as S
    from p47 import tag as T

    importlib.reload(T)
    with open(C.DETECTOR_CSV, newline="") as f:
        det = {r["episode_id"]: r for r in csv.DictReader(f) if r["cheat"] != ""}
    arts = {}
    for p in sorted((C.EPISODES_DIR / "U").glob("*.json")):
        art = json.loads(p.read_text())
        eid = art["meta"]["episode_id"]
        if eid in det:
            arts[eid] = art
    pop = sorted(arts)
    tags = {r["episode_id"]: r for r in T.load("cont").values() if r.get("status") == "scored"}
    rng = random.Random(f"{C.SEED}:samples:{slug}")
    full = [f"## {slug}", ""]
    out = [f"## {slug}", "",
           f"Population: {len(pop)} scored U draws; drawn {min(n, len(pop))} with "
           f"`random.Random('{C.SEED}:samples:{slug}')`.", ""]
    for eid in rng.sample(pop, min(n, len(pop))):
        art, d = arts[eid], det[eid]
        flags = [k for k in FLAGS if d.get(k) == "True"]
        last = next((m for m in reversed(art["messages"]) if m.get("role") == "assistant"), {})
        sts = (tags.get(eid) or {}).get("statements")
        out += [f"### `{eid}`", "",
                f"- detector: cheat **{d['cheat']}**, passed {d['passed']}, flags "
                f"{', '.join(flags) or 'none'}; end {art['end_reason']}, {art['n_turns']} turns",
                "- luna tag: " + ("not tagged" if sts is None else
                                  "; ".join(f"{s['label']}: \"{_clip(s['quote'], 160)}\""
                                            for s in sts) or "no grader talk"),
                f"- last reasoning: {_clip(S.reasoning_of(last), 500) or '(none)'}",
                "- final solution.py (first 30 code lines, docstrings removed):", "", "```python",
                code_lines(art.get("final_solution_py")), "```", ""]
        full += [f"### `{eid}`", "", f"detector: cheat {d['cheat']}; luna statements: "
                 + json.dumps(sts, ensure_ascii=False), "",
                 "Continuation turns (reasoning, visible text, commands; tool outputs omitted):",
                 "", "```text", T.turns_text(art["messages"]), "```", "",
                 "Final solution.py:", "", "```python", art.get("final_solution_py") or "", "```",
                 ""]
    return out, full


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--models", default="gpt-oss-120b,gpt-oss-20b,qwen3.6-27b")
    a = ap.parse_args(argv)
    out = ["# P47 random raw samples (Stage A, U continuations)", "",
           "Drawn by `p47/samples.py`. Excerpts: tag quotes clipped to 160 characters and the "
           "last reasoning to 500 (middle elided), code to its first 30 lines without "
           "docstrings. The same draws in full: `samples_full.md`.", ""]
    full = ["# P47 random raw samples, in full", "",
            "The draws of `samples.md` (same seeds): every continuation turn and the complete "
            "final solution.py. Tool outputs are omitted; the episode files are under "
            "`results/<model>/episodes/U/` (gitignored, backed up to the main checkout).", ""]
    for slug in a.models.split(","):
        o, f = model_section(slug, a.n)
        out += o
        full += f
    path = PROBE / "results" / "samples.md"
    path.write_text("\n".join(out) + "\n")
    (PROBE / "results" / "samples_full.md").write_text("\n".join(full) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
