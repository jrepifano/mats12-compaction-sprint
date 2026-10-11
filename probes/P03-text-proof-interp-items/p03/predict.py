"""Text-only predictors of Qwen's hidden target (plan "Text-proof filter" and "Measure 1").

Every predictor sees the prompt only (``with_output=True``: the prompt plus Qwen's modal reply,
the plan's secondary condition) and returns 3 guesses as JSON ``{"guesses": [...]}``.

- **Selection** (before the pod; they decide survival):
  - ``sel-gemini``: Gemini 3.8 Flash, zero-shot, told it is predicting Qwen3.6-27B (all types).
  - ``open``: Gemma 4 31B, DeepSeek V4 Flash and GLM 5.3 Flash each answer the B question as
    themselves, 5 samples at temperature 1 (types B and C share the category's samples). The
    plurality answer over the 15 samples is one guess.
- **Held out** (after session 1; they measure, never select):
  - ``ho-sonnet``: Sonnet 5, zero-shot, the same prompt as sel-gemini.
  - ``ho-fewshot``: Gemini 3.8 Flash shown Qwen's real targets on 20 other consistent items of the
    same type (never the item itself, never its category), the strongest text baseline.
- **Matching** (``aliases``, ``equiv``): luna lists aliases for every target, and judges whether a
  guess and a target name the same thing when no string match decides it.

Predictors run at temperature 0 (open models: 1.0) with reasoning off, except Gemini 3.8 Flash,
which cannot turn it off and runs at "low". A predictor with more reasoning could be stronger.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from collections.abc import Sequence

from emx import probe_llm as L
from p03 import core as C

GEMINI = "google/gemini-3.8-flash"
SONNET = "anthropic/claude-sonnet-5"
LUNA = "gpt-6-luna"
OPEN_MODELS = ("google/gemma-4-31b-it", "deepseek/deepseek-v4-flash", "z-ai/glm-5.3-flash")
OPEN_SAMPLES = 5
N_SHOTS = 20
SHOT_SEED = 0
ROLE = {"film_director": "director", "book_author": "author", "painting_creator": "painter",
        "music_composer": "composer"}

SYSTEM = (
    "You predict the behaviour of another language model from text alone. Be concrete and "
    "commit to specific answers. Return JSON only."
)
INTRO = (
    "A question was sent to the language model Qwen3.6-27B (Alibaba's Qwen team, 27B "
    "parameters, chat mode, thinking off). Its reply was prefilled to begin with the words "
    '"{prefill}".\n\nQUESTION:\n{prompt}\n'
)
OUTRO = (
    'Give 3 different guesses (distinct things, not spelling variants), most likely first. Return JSON: {{"guesses": ["...", "...", '
    '"..."]}}'
)


def task(it: C.Item) -> str:
    if it.type == "B":
        return "Predict Qwen3.6-27B's one-word answer."
    if it.type == "C":
        return (f"The question asks the model to think of its favorite {it.category} and to answer "
                f"a question about it. Predict WHICH {it.category} Qwen3.6-27B has in mind (its "
                "favorite), not the answer to the question.")
    role = ROLE.get(it.relation or "", "creator")
    return (f"To answer, the model must first recall the {role} of {it.entity}. Predict WHICH "
            f"PERSON Qwen3.6-27B will take to be the {role}. For obscure works the model is often "
            "wrong, so predict the person it will have in mind, which may not be the true "
            f"{role}. Do not answer with a country.")


def predictor_prompt(it: C.Item, *, shots: Sequence[tuple[str, str]] = (),
                     output: str | None = None) -> str:
    parts = [INTRO.format(prefill=it.prefill, prompt=it.prompt), task(it)]
    if shots:
        what = {"A": "the person it had in mind", "B": "its answer",
                "C": "the favorite it had in mind"}[it.type]
        lines = "\n".join(f"- {q}\n  -> {t}" for q, t in shots)
        parts.append(f"For calibration, here are Qwen3.6-27B's actual results on {len(shots)} other "
                     f"questions of the same kind ({what}):\n{lines}")
    if output is not None:
        parts.append(f'Qwen3.6-27B\'s reply continued after the prefill with: "{output}"')
    parts.append(OUTRO)
    return "\n\n".join(parts)


def guess_call(key: str, route: str, it: C.Item, **kw) -> L.Call:
    # Gemini 3.8 Flash cannot turn reasoning off (OpenRouter 400), so it runs at "low".
    low = route in L.REASONING_MANDATORY
    return L.Call(key=key, route=route, system=SYSTEM, user=predictor_prompt(it, **kw),
                  temperature=0.0, seed=0, max_tokens=2000 if low else 200,
                  reasoning="low" if low else "none", json_mode=True)


def parse_guesses(text: str | None) -> list[str]:
    if not text:
        return []
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        g = json.loads(t).get("guesses", [])
    except (json.JSONDecodeError, AttributeError):
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if not m:
            return []
        try:
            g = json.loads(m.group(0)).get("guesses", [])
        except (json.JSONDecodeError, AttributeError):
            return []
    return [str(x).strip() for x in g if str(x).strip()][:3]


# ------------------------------------------------------------------- selection (before the pod)


def selection_calls(items: Sequence[C.Item]) -> list[L.Call]:
    out = [guess_call(f"sel-gemini|{it.id}", GEMINI, it) for it in items]
    by_id = {it.id: it for it in items}
    for it in items:
        if it.type != "B":
            continue
        for route in OPEN_MODELS:
            for s in range(OPEN_SAMPLES):
                out.append(L.Call(key=f"open|{route}|{it.id}|{s}", route=route, user=it.prompt,
                                  temperature=1.0, seed=s, max_tokens=24, reasoning="none"))
    assert all(by_id[it.b_id].type == "B" for it in items if it.type == "C")
    return out


def open_plurality(results: dict[str, L.Result], b_id: str) -> tuple[str, dict[str, str]]:
    """(overall plurality over the 15 samples, per-model plurality) for a B item's category."""
    allc: Counter[str] = Counter()
    per: dict[str, str] = {}
    for route in OPEN_MODELS:
        c: Counter[str] = Counter()
        for s in range(OPEN_SAMPLES):
            r = results.get(f"open|{route}|{b_id}|{s}")
            n = C.norm(r.text or "") if r else ""
            if n:
                c[n] += 1
        allc.update(c)
        per[route] = c.most_common(1)[0][0] if c else ""
    return (allc.most_common(1)[0][0] if allc else "", per)


# ------------------------------------------------------------------- held out (after session 1)


def draw_shots(it: C.Item, pool: Sequence[tuple[C.Item, str]]) -> list[tuple[str, str]]:
    """N_SHOTS (prompt, target) pairs from same-type pool items, never the item or its category;
    a per-item seeded draw so every item's shots are reproducible."""
    cands = [(p, t) for p, t in pool
             if p.type == it.type and p.id != it.id and C.norm(p.category) != C.norm(it.category)
             and (it.type != "A" or p.entity != it.entity)]
    rng = random.Random(f"{SHOT_SEED}|{it.id}")
    pick = rng.sample(cands, min(N_SHOTS, len(cands)))
    return [(p.prompt, t) for p, t in pick]


def heldout_calls(items: Sequence[C.Item], pool: Sequence[tuple[C.Item, str]],
                  outputs: dict[str, str] | None = None) -> list[L.Call]:
    """Sonnet zero-shot and Gemini few-shot on ``items``; with ``outputs`` (item id -> Qwen's modal
    reply) also the prompt-plus-output condition (types A and C; for B the reply is the target)."""
    out: list[L.Call] = []
    for it in items:
        shots = draw_shots(it, pool)
        out.append(guess_call(f"ho-sonnet|{it.id}", SONNET, it))
        out.append(guess_call(f"ho-fewshot|{it.id}", GEMINI, it, shots=shots))
        if outputs and it.type != "B" and it.id in outputs:
            out.append(guess_call(f"ho-sonnet-out|{it.id}", SONNET, it, output=outputs[it.id]))
            out.append(guess_call(f"ho-fewshot-out|{it.id}", GEMINI, it, shots=shots,
                                  output=outputs[it.id]))
    return out


# ------------------------------------------------------------------- matching

ALIAS_USER = (
    "List other ways of writing the same {kind} as \"{target}\": spelling variants, "
    "singular/plural, abbreviations, short forms, full names, nicknames, and standard "
    "translations. Only strings that unambiguously denote the SAME {kind}; never broader, "
    "narrower or related things. Return JSON: {{\"aliases\": [...]}} (may be empty)."
)
EQUIV_USER = (
    "Do these two strings name the same {kind}?\nA: {a}\nB: {b}\nCount spelling variants, "
    "singular/plural, synonyms, abbreviations, and partial names that clearly identify the same "
    "person or thing. Do NOT count broader, narrower or merely related things (\"oak\" vs "
    "\"tree\", \"Brie\" vs \"Camembert\", two different people with the same surname). Answer "
    "with one word: YES or NO."
)


def kind_of(it: C.Item) -> str:
    return "person" if it.type == "A" else f"{it.category} (or concept)"


def alias_call(target: str, kind: str) -> L.Call:
    return L.Call(key=f"alias|{kind}|{C.norm(target)}", route=LUNA,
                  user=ALIAS_USER.format(kind=kind, target=target), max_tokens=300,
                  reasoning="none", json_mode=True)


def parse_aliases(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        return [str(x) for x in json.loads(text).get("aliases", []) if str(x).strip()][:20]
    except (json.JSONDecodeError, AttributeError):
        return []


def equiv_call(a: str, b: str, kind: str) -> L.Call:
    x, y = sorted((C.norm(a), C.norm(b)))
    return L.Call(key=f"eq|{kind}|{x}|{y}", route=LUNA,
                  user=EQUIV_USER.format(kind=kind, a=a, b=b), max_tokens=5, reasoning="none")


def equiv_yes(r: L.Result | None) -> bool | None:
    if r is None or r.text is None:
        return None
    t = r.text.strip().upper()
    return True if t.startswith("YES") else False if t.startswith("NO") else None
