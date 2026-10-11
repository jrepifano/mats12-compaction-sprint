"""Follow-up F1 (exploratory): Brauer et al.'s system-of-equations task in their prompt format.

Source: github.com/kaleybrauer/filler-token-reasoning @ a701927 (no licence file, so the prompt
builders are re-implemented here from `scripts/data/generate_varbind_dataset.py`
(`build_system_message_varbind`, `build_user_turn_varbind`, `render_definitions`) and
`scripts/prompt_utils.py` (`build_messages`, `build_messages_pre_padding`), not copied). Data:
their `data/chained_var_binding_dataset.json` (500 examples + 8 few-shot, seed 42, chain_len 1),
kept gitignored at `data/P06/chained_var_binding_dataset.json` (sha256 c80f0f5e…).

Format (theirs): system message (with the filler sentence when k > 0); 5 few-shot user turns
carrying the same filler and k; bare-answer assistant turns; no prefill; target user turn
`<definitions>\\nQuestion: <q>\\n\\nFiller: . . .\\n\\nAnswer:`. Pre-padding control:
`Padding: . . .\\n\\n<definitions>\\nQuestion: <q>\\n\\nAnswer:` in every user turn.
Arms: base (k = 0), dots100, dots500, pre100.

Follow-up F2 adds `--depth 2|3` (their generator, `--chain-len 2/3`, seed 42, 500 examples;
re-running it at chain_len 1 reproduces their shipped file byte for byte in content, checked
2026-10-11) and `--mc` (depth 1, 2/5/10 options, arms base and dots100). MC distractors: the
answer the question would have for each other variable's value, then answer ± 1..30, distinct
and non-negative, shuffled (seed MC_SEED + item idx). The options line follows the question;
the system message adds one sentence saying the answer is one of the options.

    uv run python -m p06.brauer run --model gemma4 [--depth 2] [--mc]   # from the probe dir
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import random
import re
from pathlib import Path

from . import api
from .stage1 import EXP, RES, reconcile

HERE = Path(__file__).resolve().parents[1]
DATA_DIR = HERE.parents[1] / "data" / "P06"
DATA_PATHS = {1: DATA_DIR / "chained_var_binding_dataset.json",  # sha256 c80f0f5e...
              2: DATA_DIR / "varbind_c2.json",  # sha256 b78f38d0...
              3: DATA_DIR / "varbind_c3.json"}  # sha256 7201fef6...
MC_OPTIONS = (2, 5, 10)
MC_ARMS = ("base", "dots100")
MC_SEED = 20261011
_MC_SYS = " The answer is one of the listed options."
N_SHOTS = 5
ARMS = {"base": ("after", 0), "dots100": ("after", 100), "dots500": ("after", 500),
        "pre100": ("before", 100)}

_SYS = ("You will be given a list of variable definitions followed by a question. "
        "Each variable equals either a number or an expression that refers to an "
        "earlier variable (for example 'twice the number for X plus 3'). Resolve "
        "the references to work out the value the question asks for, then answer "
        "immediately with just the number, nothing else. No explanation, no words, "
        "no reasoning, just the number.")
_SYS_FILLER = (" After the question, there will be some filler tokens "
               "(a sequence of dots) to give you extra space to process "
               "the problem before answering.")


def dots(k: int) -> str:
    return " ".join(["."] * k)


def user_turn(ex: dict, where: str, k: int) -> str:
    block = "\n".join(f"{n} = {v}" for n, v in ex["definitions"])
    q = f"{block}\nQuestion: {ex['question']}"
    if ex.get("options"):
        q += "\nOptions: " + ", ".join(str(o) for o in ex["options"])
    if k == 0:
        return f"{q}\n\nAnswer:"
    if where == "after":
        return f"{q}\n\nFiller: {dots(k)}\n\nAnswer:"
    return f"Padding: {dots(k)}\n\n{q}\n\nAnswer:"


def messages(ex: dict, shots: list[dict], arm: str) -> list[dict]:
    where, k = ARMS[arm]
    mc = _MC_SYS if ex.get("options") else ""
    msgs = [{"role": "system", "content": _SYS + mc + (_SYS_FILLER if k > 0 else "")}]
    for s in shots:
        msgs += [{"role": "user", "content": user_turn(s, where, k)},
                 {"role": "assistant", "content": str(s["answer"])}]
    msgs.append({"role": "user", "content": user_turn(ex, where, k)})
    return msgs


def load(depth: int = 1) -> tuple[list[dict], list[dict]]:
    d = json.loads(DATA_PATHS[depth].read_text())
    return d["examples"], d["few_shot_examples"][:N_SHOTS]


_COEF = {"twice": 2, "three times": 3, "four times": 4, "five times": 5, "six times": 6}
_EXPR = re.compile(r"^(twice|three times|four times|five times|six times) "
                   r"the number for (\w+) (plus|minus) (\d+)$")


def values(ex: dict) -> dict[str, int]:
    """Every variable's value, resolving the reference expressions in display order."""
    out = {}
    for name, v in ex["definitions"]:
        if isinstance(v, int):
            out[name] = v
        else:
            m = _EXPR.match(v)
            c, ref, op, k = _COEF[m.group(1)], m.group(2), m.group(3), int(m.group(4))
            out[name] = c * out[ref] + k if op == "plus" else c * out[ref] - k
    return out


def with_options(ex: dict, n_opt: int) -> dict:
    vals = values(ex)
    assert vals[ex["queried_term"]] == ex["queried_value"]
    c, k = ex["coefficient"], ex["constant"]
    f = (lambda v: c * v + k) if ex["operation"] == "plus" else (lambda v: c * v - k)
    rng = random.Random(MC_SEED + ex.get("idx", -1) * 7 + n_opt)
    cands = [f(v) for name, v in vals.items() if name != ex["queried_term"]]
    rng.shuffle(cands)
    opts = [ex["answer"]]
    for v in cands + [ex["answer"] + d * s for d in rng.sample(range(1, 31), 30)
                      for s in (rng.choice((-1, 1)),)]:
        if len(opts) == n_opt:
            break
        if v >= 0 and v not in opts:
            opts.append(v)
    assert len(opts) == n_opt
    rng.shuffle(opts)
    return {**ex, "options": opts}


def as_item(ex: dict, depth: int = 1, n_opt: int | None = None) -> dict:
    tag = f"varbind{'' if depth == 1 else f'_c{depth}'}" + (f"_mc{n_opt}" if n_opt else "")
    return {"item_id": f"{tag}:{ex['idx']}", "bank": tag, "k": depth,
            "answer": ex["answer"], "answer_type": "int", "queried_value": ex["queried_value"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--model", choices=sorted(api.RECIPES), required=True)
    ap.add_argument("--depth", type=int, choices=(1, 2, 3), default=1)
    ap.add_argument("--mc", action="store_true")
    a = ap.parse_args()
    recipe = dataclasses.replace(api.RECIPES[a.model], prefill=False)
    exs, shots = load(a.depth)
    if a.mc:
        assert a.depth == 1
        jobs = [{"item": as_item(e, 1, n), "shots": [], "arm": arm, "draw": 0,
                 "temperature": 0.0,
                 "messages": messages(with_options(e, n), [with_options(s, n) for s in shots],
                                      arm)}
                for e in exs for n in MC_OPTIONS for arm in MC_ARMS]
        out, tag = RES / f"f2_mc_{a.model}.jsonl", "f2mc"
    else:
        jobs = [{"item": as_item(e, a.depth), "shots": [], "arm": arm, "draw": 0,
                 "temperature": 0.0, "messages": messages(e, shots, arm)}
                for e in exs for arm in ARMS]
        out, tag = ((RES / f"f1_brauer_{a.model}.jsonl", "f1brauer") if a.depth == 1 else
                    (RES / f"f2_depth{a.depth}_{a.model}.jsonl", f"f2depth{a.depth}"))
    from emx import spend

    reconcile(quiet=True)
    words = sum(len(m["content"].split()) for j in jobs for m in j["messages"])
    proj = 1.5 * (1.3 * words * recipe.price_in + 10 * len(jobs) * recipe.price_out)
    spend.check_total(proj, kind="judge", exp=EXP)
    print(f"[brauer] {a.model}: {len(jobs)} jobs, projected ${proj:.3f}", flush=True)
    asker = api.Asker(api.openrouter_key())

    async def go():
        try:
            await api.run_jobs(asker, recipe, jobs, out, tag)
        finally:
            await asker.close()

    try:
        asyncio.run(go())
    finally:
        reconcile()


if __name__ == "__main__":
    main()
