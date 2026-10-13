"""P33 items, arms and prompt spans (plan.md "Setup").

Items: Brauer's depth-1 system of equations (P06 F1's data and format, `p06.brauer`).
- equation: idx 0..199, the question as shipped ("What is twice the number for cok minus 3?").
- copy: idx 200..299, the question replaced by "What is the number for <v>?" where <v> is one of
  the item's literal variables (seeded choice), answer = its literal. The 5 shots are converted the
  same way, so copy prompts demonstrate copying.

Arms (every turn carries the arm's manipulation, as in Brauer's format; only the target turn's
dots are ever masked):
- base: no filler (Brauer's base system message).
- dots<N>: `Filler:` + N space-separated dots after the question (N in 32, 100, 160, 500).
- maskB<N>: the same tokens as dots<N>; the target dots may not attend to anything before the
  first dot (masks.Rule(F0, F1, 0, F0)).
- maskQ100: the same tokens as dots100; the target dots may not attend to the target turn's
  text before them (the definitions, the question and the label).
- rep1, rep2: the definitions and question repeated once / twice before `Answer:`, no filler.
- neutral100: `Filler:` + a fixed unrelated passage (NEUTRAL, cut to 100 V4 tokens) in place
  of the dots; the system sentence says "filler text".
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from p06 import brauer as B

N_EQ = 200
N_COPY = 100
COPY_IDX0 = 200
SEED = 0
DOT_LENGTHS = (32, 100, 160, 500)
NEUTRAL_TOKENS = 100

V4_ARMS = ("base", *(f"dots{n}" for n in DOT_LENGTHS), *(f"maskB{n}" for n in DOT_LENGTHS),
           "maskQ100", "rep1", "rep2", "neutral100")
GEMMA_ARMS = ("base", "dots100", "dots500", "maskB100", "maskB500", "rep1", "neutral100")

_SYS_REP = " After the question, the definitions and the question are repeated."
_SYS_NEUTRAL = (" After the question, there will be some filler text to give you extra space "
                "to process the problem before answering.")
# Fixed, number-free, unrelated to the task; cut to NEUTRAL_TOKENS tokens per tokenizer.
NEUTRAL = (
    "The morning was quiet along the river and a light wind moved through the tall grass on "
    "the far bank while a few birds circled above the water and then settled in the old trees "
    "near the bridge where the path turns toward the village and the fields beyond it are "
    "green after the long rain of the previous week so the farmers have started to walk out "
    "early to look at the soil and the young plants that are coming up in even rows across "
    "the valley under a pale sky that will likely clear by the afternoon when the market in "
    "the square opens and people arrive with baskets of bread and fruit to sell to anyone "
    "passing through on the road that runs beside the river toward the hills in the west where "
    "the evening light falls softly on the stone walls of the farms and the small church at "
    "the top of the lane")


def arm_kind(arm: str) -> tuple[str, int]:
    """(family, n): family in base / dots / maskB / maskQ / rep / neutral."""
    m = re.fullmatch(r"(base|dots|maskB|maskQ|rep|neutral)(\d*)", arm)
    if not m:
        raise ValueError(arm)
    return m.group(1), int(m.group(2) or 0)


def masked(arm: str) -> bool:
    return arm_kind(arm)[0] in ("maskB", "maskQ")


def token_arm(arm: str) -> str:
    """The arm whose tokens this arm uses (a mask arm runs the dots arm's prompt)."""
    fam, n = arm_kind(arm)
    return f"dots{n}" if fam in ("maskB", "maskQ") else arm


# --------------------------------------------------------------------- items


def to_copy(ex: dict, seed: int = SEED) -> dict:
    lits = [(n, v) for n, v in ex["definitions"] if isinstance(v, int)]
    name, val = random.Random(seed * 1_000_003 + ex.get("idx", -1)).choice(lits)
    return {**ex, "question": f"What is the number for {name}?", "answer": val,
            "queried_term": name, "queried_value": val, "copy_of": ex.get("idx")}


def load_items() -> tuple[list[dict], dict[str, list[dict]]]:
    """(items in run order, shots per kind). Each item: idx, kind, ex (the example), answer,
    item_id. Run order interleaves two equation items with one copy item, so a run cut short
    keeps both kinds."""
    exs, shots = B.load(1)
    by_idx = {e["idx"]: e for e in exs}
    eq = [by_idx[i] for i in range(N_EQ)]
    cp = [to_copy(by_idx[i]) for i in range(COPY_IDX0, COPY_IDX0 + N_COPY)]
    out = []
    for j in range(max(len(eq), 2 * len(cp))):
        if j < len(eq):
            e = eq[j]
            out.append({"item_id": f"eq:{e['idx']}", "idx": e["idx"], "kind": "equation",
                        "answer": e["answer"], "ex": e})
        if j % 2 == 1 and j // 2 < len(cp):
            c = cp[j // 2]
            out.append({"item_id": f"copy:{c['idx']}", "idx": c["idx"], "kind": "copy",
                        "answer": c["answer"], "ex": c})
    return out, {"equation": shots, "copy": [to_copy(s, seed=SEED + 1) for s in shots]}


# --------------------------------------------------------------------- messages


def block(ex: dict) -> str:
    q = "\n".join(f"{n} = {v}" for n, v in ex["definitions"]) + f"\nQuestion: {ex['question']}"
    if ex.get("options"):
        q += "\nOptions: " + ", ".join(str(o) for o in ex["options"])
    return q


def user_turn(ex: dict, arm: str, neutral: str) -> str:
    fam, n = arm_kind(token_arm(arm))
    q = block(ex)
    if fam == "base":
        return f"{q}\n\nAnswer:"
    if fam == "dots":
        return f"{q}\n\nFiller: {B.dots(n)}\n\nAnswer:"
    if fam == "neutral":
        return f"{q}\n\nFiller: {neutral}\n\nAnswer:"
    if fam == "rep":
        return q + "".join(f"\n\nRepeated:\n{q}" for _ in range(n)) + "\n\nAnswer:"
    raise ValueError(arm)


def system(arm: str) -> str:
    fam, _ = arm_kind(token_arm(arm))
    extra = {"base": "", "dots": B._SYS_FILLER, "rep": _SYS_REP, "neutral": _SYS_NEUTRAL}[fam]
    return B._SYS + extra


def messages(ex: dict, shots: list[dict], arm: str, neutral: str = NEUTRAL) -> list[dict]:
    msgs = [{"role": "system", "content": system(arm)}]
    for s in shots:
        msgs += [{"role": "user", "content": user_turn(s, arm, neutral)},
                 {"role": "assistant", "content": str(s["answer"])}]
    msgs.append({"role": "user", "content": user_turn(ex, arm, neutral)})
    return msgs


# --------------------------------------------------------------------- spans


@dataclass
class Spans:
    ids: list[int]
    turn0: int  # first token of the target user turn's content
    f0: int  # first filler token (dots or neutral text); -1 without filler
    f1: int  # one past the last filler token
    suffix0: int  # first token after the filler (or after the question block); to the end = suffix

    def n_filler(self) -> int:
        return self.f1 - self.f0 if self.f0 >= 0 else 0


def spans(tok: Any, text: str, user_marker: str, arm: str, check_ids: list[int] | None = None,
          n_dots: int | None = None) -> Spans:
    """Token spans of the rendered `text`. `user_marker` opens the target user turn (the last
    one). Filler tokens: every token starting at or after the end of the target turn's
    "Filler:" label (the space before the first dot belongs to the first dot's token) and
    before the end of the filler text."""
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = [int(i) for i in enc["input_ids"]]
    if check_ids is not None and ids != [int(i) for i in check_ids]:
        raise ValueError(f"{arm}: offset tokenization differs from the reference ids")
    offs = [(int(a), int(b)) for a, b in enc["offset_mapping"]]
    t = text.rfind(user_marker)
    if t < 0:
        raise ValueError(f"{arm}: no user turn marker")
    turn_c = t + len(user_marker)
    turn0 = next(i for i, (s, e) in enumerate(offs) if e > s >= turn_c)
    fam, n = arm_kind(token_arm(arm))
    suf = text.rfind("\n\nAnswer:")
    if suf < turn_c:
        raise ValueError(f"{arm}: no Answer: suffix in the target turn")
    f0 = f1 = -1
    if fam in ("dots", "neutral"):
        lab = text.rfind("\nFiller:", turn_c)
        if lab < 0:
            raise ValueError(f"{arm}: no Filler label in the target turn")
        c0 = lab + len("\nFiller:")
        c1 = suf  # the filler text ends where "\n\nAnswer:" starts
        fill = [i for i, (s, e) in enumerate(offs) if e > s and c0 <= s < c1]
        if not fill or fill != list(range(fill[0], fill[-1] + 1)):
            raise ValueError(f"{arm}: filler tokens not contiguous")
        f0, f1 = fill[0], fill[-1] + 1
        if fam == "dots" and (n_dots or n) != f1 - f0:
            raise ValueError(f"{arm}: {f1 - f0} filler tokens, expected {n_dots or n}")
        suffix0 = f1
    else:
        suffix0 = next(i for i, (s, e) in enumerate(offs) if e > s >= suf)
    return Spans(ids, turn0, f0, f1, suffix0)


def mask_rules(arm: str, sp: Spans) -> list:
    from .masks import Rule

    fam, _ = arm_kind(arm)
    if fam == "maskB":
        return [Rule(sp.f0, sp.f1, 0, sp.f0)]
    if fam == "maskQ":
        return [Rule(sp.f0, sp.f1, sp.turn0, sp.f0)]
    return []


def neutral_for(tok: Any, n_tokens: int = NEUTRAL_TOKENS) -> str:
    """The longest word prefix of NEUTRAL that is n_tokens tokens in the filler slot (between
    "Filler: " and "\\n\\n"), or the closest one."""
    words = NEUTRAL.split()
    best, best_d = "", None
    for k in range(1, len(words) + 1):
        s = " ".join(words[:k])
        n = len(tok.encode(f"Filler: {s}\n\nAnswer:", add_special_tokens=False)) - len(
            tok.encode("Filler: \n\nAnswer:", add_special_tokens=False)) + 1
        d = abs(n - n_tokens)
        if best_d is None or d < best_d or (d == best_d and n <= n_tokens):
            best, best_d = s, d
        if n > n_tokens + 2:
            break
    return best
