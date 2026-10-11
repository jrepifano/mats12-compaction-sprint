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

    uv run python -m p06.brauer run --model gemma4     # from the probe dir
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path

from . import api
from .stage1 import EXP, RES, reconcile

HERE = Path(__file__).resolve().parents[1]
DATA_PATH = HERE.parents[1] / "data" / "P06" / "chained_var_binding_dataset.json"
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
    if k == 0:
        return f"{q}\n\nAnswer:"
    if where == "after":
        return f"{q}\n\nFiller: {dots(k)}\n\nAnswer:"
    return f"Padding: {dots(k)}\n\n{q}\n\nAnswer:"


def messages(ex: dict, shots: list[dict], arm: str) -> list[dict]:
    where, k = ARMS[arm]
    msgs = [{"role": "system", "content": _SYS + (_SYS_FILLER if k > 0 else "")}]
    for s in shots:
        msgs += [{"role": "user", "content": user_turn(s, where, k)},
                 {"role": "assistant", "content": str(s["answer"])}]
    msgs.append({"role": "user", "content": user_turn(ex, where, k)})
    return msgs


def load() -> tuple[list[dict], list[dict]]:
    d = json.loads(DATA_PATH.read_text())
    return d["examples"], d["few_shot_examples"][:N_SHOTS]


def as_item(ex: dict) -> dict:
    return {"item_id": f"varbind:{ex['idx']}", "bank": "varbind", "k": 1,
            "answer": ex["answer"], "answer_type": "int", "queried_value": ex["queried_value"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--model", choices=sorted(api.RECIPES), required=True)
    a = ap.parse_args()
    recipe = dataclasses.replace(api.RECIPES[a.model], prefill=False)
    exs, shots = load()
    jobs = [{"item": as_item(e), "shots": [], "arm": arm, "draw": 0, "temperature": 0.0,
             "messages": messages(e, shots, arm)} for e in exs for arm in ARMS]
    from emx import spend

    reconcile(quiet=True)
    words = sum(len(m["content"].split()) for j in jobs for m in j["messages"])
    proj = 1.5 * (1.3 * words * recipe.price_in + 10 * len(jobs) * recipe.price_out)
    spend.check_total(proj, kind="judge", exp=EXP)
    print(f"[brauer] {a.model}: {len(jobs)} jobs, projected ${proj:.3f}", flush=True)
    asker = api.Asker(api.openrouter_key())

    async def go():
        try:
            await api.run_jobs(asker, recipe, jobs, RES / f"f1_brauer_{a.model}.jsonl", "f1brauer")
        finally:
            await asker.close()

    try:
        asyncio.run(go())
    finally:
        reconcile()


if __name__ == "__main__":
    main()
