"""Seeded random raw samples for the note (AGENTS.md: random, never cherry-picked).

Adapted from P07's samples.py (`probes/P07-compaction-trust/p07/samples.py`, on main). Draws
with random.Random(C.SEED), not selected by outcome: per model, `N_EPISODES` usable T or H
episodes under Codex or Codex-self (each shows the plant sentence, the agent's commands, its
final message and the code labels), and 2 identity replies per model x wrapper. Writes
results/samples.md.

  PYTHONPATH=probes/P09-wrapper-identity-action:src:scripts uv run --group swarm \
      python -m p09.samples
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

from p09 import contracts as C
from p09 import measures as M

OUT = C.RESULTS_DIR / "samples.md"
N_EPISODES = 6
N_COMMANDS = 6
WRAPPERS = (C.Wrapper.CODEX.value, C.Wrapper.CODEX_SELF.value)


def _clip(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ⏎ ")   # one line each, so the note's structure holds
    return s if len(s) <= n else s[: n - 1] + "…"


def episode_block(row: dict, art: dict) -> str:
    m = art["meta"]
    cmds = [c.get("cmd") or "" for c in (art.get("calls") or [])]
    lab = (f"undo: {row.get('undo')}" + (f" (lines gone {row.get('h_lines_gone')}, passes "
                                          f"pair {row.get('h_passes_pair')})"
                                          if m["content"] == "H" else
                                          f" (strict {row.get('undo_t_strict')})")
           + f"; new cheating: {row.get('new_cheat')}; restart: {row.get('restart')} "
           f"(ratio {row.get('restart_ratio')}); disclosure regex: "
           f"{row.get('disclosure_regex')}, luna: {row.get('disclosure_luna') or '—'}")
    lines = [(f"**{m['model_slug']} · {m['wrapper']} · {m['content']} · {m['ctx_id']} · "
              f"s{m['sample']}**: end {art.get('end_reason')}, {art.get('n_turns')} turns, "
              f"{len(cmds)} commands"),
             f"- Plant: {_clip(m.get('plant_sentence') or '(none)', 300)}",
             f"- Labels: {lab}"]
    lines += [f"- Command {i + 1}: `{_clip(c, 200)}`" for i, c in enumerate(cmds[:N_COMMANDS])]
    if len(cmds) > N_COMMANDS:
        lines.append(f"- … {len(cmds) - N_COMMANDS} more commands")
    lines.append(f"- Final message: {_clip(M.final_text(art) or '(empty)', 500)}")
    return "\n".join(lines)


def render(rows: list[dict], ident: list[dict], seed: int = C.SEED,
           probe_dir: Path = C.PROBE_DIR) -> str:
    rng = random.Random(seed)
    out = ["# P09 random raw samples", "",
           f"Drawn by `p09/samples.py` with random.Random({seed}); not selected by outcome.", ""]
    models = sorted({r["model_slug"] for r in rows if r.get("model_slug")})
    for model in models:
        pool = sorted((r for r in rows if r["model_slug"] == model and not r.get("error")
                       and r["content"] in ("T", "H") and r["wrapper"] in WRAPPERS),
                      key=lambda r: r["episode_id"])
        out += [(f"## {model}: T and H episodes under Codex and Codex-self "
                 f"({min(N_EPISODES, len(pool))} of {len(pool)})"), ""]
        for r in rng.sample(pool, min(N_EPISODES, len(pool))):
            art = json.loads((probe_dir / r["path"]).read_text())
            out += [episode_block(r, art), ""]
    if ident:
        out += ["## Identity replies (\"mine\" probe)", ""]
        keys = sorted({(r.get("model"), r.get("wrapper")) for r in ident},
                      key=lambda k: tuple(map(str, k)))
        for model, w in keys:
            pool = [r for r in ident if r.get("model") == model and r.get("wrapper") == w]
            for r in rng.sample(pool, min(2, len(pool))):
                out.append(f"- **{model} · {w} · {r.get('ctx_id')}** (parsed: "
                           f"{r.get('parsed')}): {_clip(r.get('reply') or '', 350)}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--measures", type=Path, default=C.MEASURES_CSV)
    ap.add_argument("--identity", type=Path, default=C.IDENTITY_JSONL)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    with a.measures.open(newline="") as f:
        rows = list(csv.DictReader(f))
    ident = ([json.loads(x) for x in a.identity.read_text().splitlines() if x.strip()]
             if a.identity.exists() else [])
    from p09.analyze import IDENTITY_KEY, dedupe

    ident = dedupe([r for r in ident if r.get("probe") == "mine"], IDENTITY_KEY)
    a.out.write_text(render(rows, ident))
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
