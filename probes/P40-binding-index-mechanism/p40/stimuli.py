"""P40 stimuli: four families of sentences, their questions, renders and patch sites (plan.md).

Pure Python except the ``render*`` functions (need a tokenizer). Seeded and deterministic.

Families (``Sent.family``):

- ``trans``: P36's 100 items (24 person nouns x 20 transitive verbs, same seed, same held-out
  entities and verbs; ``build_items`` is P36's verbatim) in six constructions (``CONS``), each with
  the designated entity e1 (alphabetically first) as agent (key ``a``) or patient (``p``).
  In A, P, OC and PC an entity's (order, subject, role) take all 8 combinations (``feats``).
  Shifted A/P copies (sid suffix ``-s16``) put a neutral 15-token clause in front.
- ``rel``: 100 two-place relations with a converse (5 comparatives, 2 spatial), forms ``pos``
  ("The X is taller than the Y.") and ``neg`` ("The Y is shorter than the X."); roles ``hi``
  (holds the first predicate of the pair) and ``lo``; key ``h``/``l`` = e1's role.
- ``ditr``: 60 three-person ditransitives (send/show/bring), frames ``pp`` ("The A sent the T to
  the G.") and ``do`` ("The A sent the G the T."), all 6 role assignments (key ``p0``..``p5``).
- ``cap``: Feng & Steinhardt's CAPITALS: "Alice lives in the capital city of France. Bob lives in
  the capital city of Thailand."; key ``b{binding}o{order}``. Questions name a person; the answer
  is the first token of their capital.

``Sent.ents`` lists the entities in mention order, ``Sent.roles`` their roles (for ``cap``: the
country each name is bound to), ``Sent.attrs`` (``cap`` only) the countries in mention order.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SEED = 20261013

# ------------------------------------------------------------------ P36's items (verbatim)
ENTITIES: tuple[str, ...] = (
    "teacher", "doctor", "farmer", "baker", "nurse", "pilot", "singer", "dancer",
    "lawyer", "judge", "soldier", "sailor", "driver", "writer", "painter", "banker",
    "chef", "clerk", "coach", "student", "waiter", "artist", "actor", "tailor",
)  # fmt: skip
VERBS: dict[str, tuple[str, str]] = {
    "chase": ("chased", "chasing"), "push": ("pushed", "pushing"), "call": ("called", "calling"),
    "help": ("helped", "helping"), "follow": ("followed", "following"),
    "thank": ("thanked", "thanking"), "blame": ("blamed", "blaming"), "hug": ("hugged", "hugging"),
    "praise": ("praised", "praising"), "kick": ("kicked", "kicking"),
    "warn": ("warned", "warning"), "visit": ("visited", "visiting"),
    "tease": ("teased", "teasing"), "rescue": ("rescued", "rescuing"),
    "hire": ("hired", "hiring"), "greet": ("greeted", "greeting"),
    "interview": ("interviewed", "interviewing"), "scold": ("scolded", "scolding"),
    "photograph": ("photographed", "photographing"), "tickle": ("tickled", "tickling"),
}  # fmt: skip
N_HELD_ENT = 8
N_HELD_VERB = 6
N_TRAIN_ITEMS, N_HELD_ENT_ITEMS, N_HELD_VERB_ITEMS = 60, 20, 20


@dataclass(frozen=True)
class Item:
    item: int
    split: str  # "train" | "held_entity" | "held_verb"
    e1: str
    e2: str
    verb: str


def splits(seed: int = SEED) -> tuple[list[str], list[str], list[str], list[str]]:
    """(train entities, held entities, train verbs, held verbs), seeded shuffles (P36)."""
    rng = random.Random(seed)
    ents = list(ENTITIES)
    rng.shuffle(ents)
    verbs = list(VERBS)
    rng.shuffle(verbs)
    return (
        sorted(ents[N_HELD_ENT:]),
        sorted(ents[:N_HELD_ENT]),
        sorted(verbs[N_HELD_VERB:]),
        sorted(verbs[:N_HELD_VERB]),
    )


def build_items(seed: int = SEED) -> list[Item]:
    """P36's 100 base items (copied verbatim from ``p36/stimuli.py``)."""
    tr_e, he_e, tr_v, he_v = splits(seed)
    rng = random.Random(seed + 1)
    used: set[tuple[str, str]] = set()
    out: list[Item] = []

    def pairs(pool: list[str], n: int) -> list[tuple[str, str]]:
        allp = [(a, b) for i, a in enumerate(pool) for b in pool[i + 1 :]]
        rng.shuffle(allp)
        got = [p for p in allp if p not in used][:n]
        if len(got) < n:
            raise ValueError(f"only {len(got)} free pairs, need {n}")
        used.update(got)
        return got

    def verbs_for(pool: list[str], n: int) -> list[str]:
        vs = (pool * (n // len(pool) + 1))[:n]
        rng.shuffle(vs)
        return vs

    for split, ent_pool, verb_pool, n in (
        ("train", tr_e, tr_v, N_TRAIN_ITEMS),
        ("held_entity", he_e, tr_v, N_HELD_ENT_ITEMS),
        ("held_verb", tr_e, he_v, N_HELD_VERB_ITEMS),
    ):
        for (a, b), v in zip(pairs(ent_pool, n), verbs_for(verb_pool, n), strict=True):
            e1, e2 = sorted((a, b))
            out.append(Item(len(out), split, e1, e2, v))
    return out


# ------------------------------------------------------------------ sentences
@dataclass(frozen=True)
class Sent:
    sid: str
    family: str  # trans | rel | ditr | cap
    item: int
    split: str  # "train" or a held-out split name
    cons: str  # construction / form / frame
    key: str  # role assignment (see module docstring)
    pred: str  # verb / relation key ("" for cap)
    ents: tuple[str, ...]  # mention order
    roles: tuple[str, ...]  # role of each entity in ``ents`` (cap: its bound country)
    text: str
    attrs: tuple[str, ...] = ()  # cap: countries in mention order
    shift: int = 0  # tokens of neutral prefix (shifted A/P copies)

    def role_of(self, e: str) -> str:
        return self.roles[self.ents.index(e)]

    def ent_with(self, role: str) -> str:
        return self.ents[self.roles.index(role)]


CONS: tuple[str, ...] = ("A", "P", "OC", "PC", "SC", "TOP")
CORE: tuple[str, ...] = ("A", "P", "OC", "PC")
# (order, subject) of the agent; the patient has (3 - order, 1 - subject)
AGENT_FEATS: dict[str, tuple[int, int]] = {
    "A": (1, 1), "P": (2, 0), "OC": (2, 1), "PC": (1, 0), "SC": (1, 1), "TOP": (2, 1),
}  # fmt: skip
SHIFT_PREFIX = "Late on a quiet Sunday afternoon in the middle of the long summer holidays,"


def trans_text(cons: str, ag: str, pt: str, verb: str) -> str:
    past = VERBS[verb][0]
    return {
        "A": f"The {ag} {past} the {pt}.",
        "P": f"The {pt} was {past} by the {ag}.",
        "OC": f"It was the {pt} who the {ag} {past}.",
        "PC": f"It was by the {ag} that the {pt} was {past}.",
        "SC": f"It was the {ag} who {past} the {pt}.",
        "TOP": f"The {pt}, the {ag} {past}.",
    }[cons]


def feats(cons: str, role: str) -> tuple[int, int, int]:
    """(order 1/2, subject 0/1, agent 0/1) of an entity with ``role`` in construction ``cons``."""
    o, s = AGENT_FEATS[cons]
    return (o, s, 1) if role == "agent" else (3 - o, 1 - s, 0)


def trans_sents(items: list[Item]) -> list[Sent]:
    out = []
    for it in items:
        for cons in CONS:
            for key in ("a", "p"):
                ag, pt = (it.e1, it.e2) if key == "a" else (it.e2, it.e1)
                o_ag = AGENT_FEATS[cons][0]
                ents = (ag, pt) if o_ag == 1 else (pt, ag)
                roles = tuple("agent" if e == ag else "patient" for e in ents)
                out.append(Sent(
                    f"t{it.item}-{cons}-{key}", "trans", it.item, it.split, cons, key, it.verb,
                    ents, roles, trans_text(cons, ag, pt, it.verb),
                ))  # fmt: skip
    return out


def shifted(sents: list[Sent]) -> list[Sent]:
    """Shifted copies of the A/P sentences: the neutral prefix, then "the ..." for "The ..."."""
    out = []
    for s in sents:
        if s.family != "trans" or s.cons not in ("A", "P"):
            continue
        assert s.text.startswith("The ")
        text = f"{SHIFT_PREFIX} the {s.text[len('The ') :]}"
        out.append(Sent(f"{s.sid}-s16", s.family, s.item, s.split, s.cons, s.key, s.pred,
                        s.ents, s.roles, text, shift=1))  # fmt: skip
    return out


# relations: key -> (hi word, lo word, kind)
RELS: dict[str, tuple[str, str, str]] = {
    "tall": ("taller", "shorter", "cmp"), "old": ("older", "younger", "cmp"),
    "rich": ("richer", "poorer", "cmp"), "strong": ("stronger", "weaker", "cmp"),
    "fast": ("faster", "slower", "cmp"), "left": ("left", "right", "lr"),
    "front": ("front", "behind", "fb"),
}  # fmt: skip
N_REL_ITEMS, N_REL_TRAIN = 100, 70


def rel_text(rel: str, form: str, hi: str, lo: str) -> str:
    h, w, kind = RELS[rel]
    if kind == "cmp":
        return (
            f"The {hi} is {h} than the {lo}."
            if form == "pos"
            else (f"The {lo} is {w} than the {hi}.")
        )
    if kind == "lr":
        return (
            f"The {hi} stood to the left of the {lo}."
            if form == "pos"
            else (f"The {lo} stood to the right of the {hi}.")
        )
    return (
        f"The {hi} stood in front of the {lo}."
        if form == "pos"
        else (f"The {lo} stood behind the {hi}.")
    )


def rel_sents(seed: int = SEED) -> list[Sent]:
    tr_e, he_e, _, _ = splits(seed)
    rng = random.Random(seed + 11)
    out = []
    rels = list(RELS)
    for n_items, pool, split in ((N_REL_TRAIN, tr_e, "train"),
                                 (N_REL_ITEMS - N_REL_TRAIN, he_e, "held_entity")):  # fmt: skip
        allp = [(a, b) for i, a in enumerate(pool) for b in pool[i + 1 :]]
        rng.shuffle(allp)
        if len(allp) < n_items:  # held entities: 28 pairs, reuse with another relation
            allp = (allp * (n_items // len(allp) + 1))[:n_items]
        for j, (a, b) in enumerate(allp[:n_items]):
            item = len({s.item for s in out})
            rel = rels[(j + (0 if split == "train" else 3)) % len(rels)]
            e1, e2 = sorted((a, b))
            for form in ("pos", "neg"):
                for key in ("h", "l"):
                    hi, lo = (e1, e2) if key == "h" else (e2, e1)
                    ents = (hi, lo) if form == "pos" else (lo, hi)
                    roles = tuple("hi" if e == hi else "lo" for e in ents)
                    out.append(Sent(f"r{item}-{form}-{key}", "rel", item, split, form, key, rel,
                                    ents, roles, rel_text(rel, form, hi, lo)))  # fmt: skip
    return out


# ditransitives: verb -> (past, participle, gerund)
VERBS3: dict[str, tuple[str, str, str]] = {
    "send": ("sent", "sent", "sending"),
    "show": ("showed", "shown", "showing"),
    "bring": ("brought", "brought", "bringing"),
}
ROLES3: tuple[str, ...] = ("agent", "theme", "goal")
PERMS: list[tuple[int, int, int]] = list(itertools.permutations(range(3)))
N_DITR_TRAIN, N_DITR_HELD = 40, 20


def ditr_text(frame: str, a: str, t: str, g: str, verb: str) -> str:
    past = VERBS3[verb][0]
    if frame == "pp":
        return f"The {a} {past} the {t} to the {g}."
    return f"The {a} {past} the {g} the {t}."


def ditr_sents(seed: int = SEED) -> list[Sent]:
    tr_e, he_e, _, _ = splits(seed)
    rng = random.Random(seed + 21)
    out = []
    verbs = list(VERBS3)
    item = 0
    for n_items, pool, split in ((N_DITR_TRAIN, tr_e, "train"),
                                 (N_DITR_HELD, he_e, "held_entity")):  # fmt: skip
        trip = list(itertools.combinations(pool, 3))
        rng.shuffle(trip)
        for j, tri in enumerate(trip[:n_items]):
            verb = verbs[j % len(verbs)]
            es = sorted(tri)
            for p, perm in enumerate(PERMS):
                a, t, g = (es[perm[0]], es[perm[1]], es[perm[2]])
                for frame in ("pp", "do"):
                    ents = (a, t, g) if frame == "pp" else (a, g, t)
                    roles = (
                        ("agent", "theme", "goal") if frame == "pp" else ("agent", "goal", "theme")
                    )
                    out.append(Sent(f"d{item}-{frame}-p{p}", "ditr", item, split, frame,
                                    f"p{p}", verb, ents, roles,
                                    ditr_text(frame, a, t, g, verb)))  # fmt: skip
            item += 1
    return out


NAMES: tuple[str, ...] = (
    "Alice", "Bob", "Carol", "David", "Emma", "Frank", "Grace", "Henry", "Jack", "Kate",
    "Laura", "Mark", "Nancy", "Oscar", "Paul", "Rachel", "Sam", "Tom", "Victor", "Wendy",
)  # fmt: skip
CAPITALS: dict[str, str] = {
    "France": "Paris", "Germany": "Berlin", "Japan": "Tokyo", "China": "Beijing",
    "Spain": "Madrid", "Italy": "Rome", "Egypt": "Cairo", "Russia": "Moscow",
    "Canada": "Ottawa", "Greece": "Athens", "Peru": "Lima", "Kenya": "Nairobi",
    "Cuba": "Havana", "Poland": "Warsaw", "Sweden": "Stockholm", "Norway": "Oslo",
    "Turkey": "Ankara", "Thailand": "Bangkok", "Austria": "Vienna", "Portugal": "Lisbon",
}  # fmt: skip
N_CAP_ITEMS, N_CAP_TRAIN, N_HELD_NAMES = 100, 70, 6


def name_splits(seed: int = SEED) -> tuple[list[str], list[str]]:
    rng = random.Random(seed + 31)
    ns = list(NAMES)
    rng.shuffle(ns)
    return sorted(ns[N_HELD_NAMES:]), sorted(ns[:N_HELD_NAMES])


def cap_text(n_first: str, c_first: str, n_second: str, c_second: str) -> str:
    return (f"{n_first} lives in the capital city of {c_first}. "
            f"{n_second} lives in the capital city of {c_second}.")  # fmt: skip


def cap_sents(seed: int = SEED) -> list[Sent]:
    """Items (n1 < n2, c1, c2); key b0: n1-c1, n2-c2 / b1: n1-c2, n2-c1; o0: n1 first / o1: n2."""
    tr_n, he_n = name_splits(seed)
    rng = random.Random(seed + 32)
    countries = sorted(CAPITALS)
    out = []
    seen: set[tuple] = set()
    item = 0
    for n_items, pool, split in ((N_CAP_TRAIN, tr_n, "train"),
                                 (N_CAP_ITEMS - N_CAP_TRAIN, he_n, "held_entity")):  # fmt: skip
        while sum(1 for s in out if s.split == split) < 4 * n_items:
            n1, n2 = sorted(rng.sample(pool, 2))
            c1, c2 = rng.sample(countries, 2)
            if (n1, n2, c1, c2) in seen:
                continue
            seen.add((n1, n2, c1, c2))
            for b in (0, 1):
                bind = {n1: c1, n2: c2} if b == 0 else {n1: c2, n2: c1}
                for o in (0, 1):
                    first, second = (n1, n2) if o == 0 else (n2, n1)
                    out.append(Sent(
                        f"c{item}-cap-b{b}o{o}", "cap", item, split, "cap", f"b{b}o{o}", "",
                        (first, second), (bind[first], bind[second]),
                        cap_text(first, bind[first], second, bind[second]),
                        attrs=(bind[first], bind[second]),
                    ))  # fmt: skip
            item += 1
    return out


def build_all(seed: int = SEED) -> list[Sent]:
    t = trans_sents(build_items(seed))
    return t + shifted(t) + rel_sents(seed) + ditr_sents(seed) + cap_sents(seed)


# ------------------------------------------------------------------ questions and answers
QUESTIONS: dict[str, tuple[str, ...]] = {
    "trans": ("agent", "patient"),
    "rel": ("hi", "lo"),
    "ditr": ("agent", "theme", "goal"),
    "cap": ("n1", "n2"),  # the alphabetically first / second name
}


def questions(s: Sent) -> tuple[str, ...]:
    return QUESTIONS[s.family]


def question_text(s: Sent, q: str) -> str:
    if s.family == "trans":
        past, gerund = VERBS[s.pred]
        return f"Who did the {gerund}?" if q == "agent" else f"Who was {past}?"
    if s.family == "rel":
        h, w, kind = RELS[s.pred]
        word = h if q == "hi" else w
        if kind == "cmp":
            return f"Who is {word}?"
        if kind == "lr":
            return f"Who stood on the {word}?"
        return "Who stood in front?" if q == "hi" else "Who stood behind?"
    if s.family == "ditr":
        _, pp, gerund = VERBS3[s.pred]
        return {"agent": f"Who did the {gerund}?", "theme": f"Who was {pp}?",
                "goal": f"Who was someone {pp} to?"}[q]  # fmt: skip
    return f"Which city does {cap_name(s, q)} live in?"


def cap_name(s: Sent, q: str) -> str:
    return sorted(s.ents)[0 if q == "n1" else 1]


def prefill(s: Sent, q: str) -> str:
    return f"{cap_name(s, q)} lives in the city of" if s.family == "cap" else "The"


def answer(s: Sent, q: str) -> str:
    """The answer word: an entity, or (cap) the queried person's capital."""
    if s.family == "cap":
        return CAPITALS[s.role_of(cap_name(s, q))]
    return s.ent_with(q)


def candidates(s: Sent, q: str) -> list[str]:
    """Every answer word that names one of the sentence's options (entities or capitals)."""
    if s.family == "cap":
        return [CAPITALS[c] for c in s.attrs]
    return list(s.ents)


def user_text(s: Sent, q: str) -> str:
    label = "Text" if s.family == "cap" else "Sentence"
    return f"{label}: {s.text}\nQuestion: {question_text(s, q)} Answer with one word."


# ------------------------------------------------------------------ rendering (tokenizer)
def _word_spans(text: str, s0: int, s1: int, words: list[str]) -> list[tuple[int, int]]:
    """Character spans of each standalone occurrence of ``words`` in text[s0:s1]."""
    import re

    out = []
    for w in words:
        for m in re.finditer(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text[s0:s1]):
            out.append((s0 + m.start(), s0 + m.end()))
    return sorted(out)


def render(tokenizer: Any, s: Sent, q: str) -> dict[str, Any]:
    """Chat render (thinking off) + prefill. Returns ids, n, ``ent`` {entity: position} (cap:
    names and ``"@" + country``), and ``sites`` {site: [positions]} for the localisation sweep."""
    msgs = [{"role": "user", "content": user_text(s, q)}]
    text = tokenizer.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    text += prefill(s, q)
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offs = list(enc["input_ids"]), list(enc["offset_mapping"])
    label = "Text: " if s.family == "cap" else "Sentence: "
    s0 = text.index(label) + len(label)
    s1 = s0 + len(s.text)
    if text[s0:s1] != s.text:
        raise AssertionError(f"{s.sid}: sentence not found in render")

    def toks(lo: int, hi: int) -> list[int]:
        return [i for i, (a, b) in enumerate(offs) if a < hi and b > lo]

    def one_word(word: str, lo: int) -> int:
        """The token that is exactly " word" (leading space) starting at char ``lo - 1``."""
        hits = toks(lo - 1, lo + len(word))
        if len(hits) != 1 or offs[hits[0]] != (lo - 1, lo + len(word)):
            raise AssertionError(f"{s.sid}: {word!r} is not exactly one token")
        return hits[0]

    ent: dict[str, int] = {}
    for e in s.ents:
        spans = _word_spans(text, s0, s1, [e])
        if len(spans) != 1:
            raise AssertionError(f"{s.sid}: entity {e!r} occurs {len(spans)} times")
        ent[e] = one_word(e, spans[0][0])
    for c in s.attrs:
        ((lo, _),) = _word_spans(text, s0, s1, [c])
        ent["@" + c] = one_word(c, lo)
    order = [ent[e] for e in s.ents]
    if order != sorted(order):
        raise AssertionError(f"{s.sid}: entities out of mention order")
    sites: dict[str, list[int]] = {}
    if s.family == "trans":
        sent_toks = toks(s0, s1)
        past = VERBS[s.pred][0]
        ent_pos = set(ent.values())

        def span_toks(words: list[str]) -> list[int]:
            return sorted({i for a, b in _word_spans(text, s0, s1, words) for i in toks(a, b)}
                          - ent_pos)  # fmt: skip

        final = toks(s1 - 1, s1)
        if len(final) != 1 or text[offs[final[0]][0] : offs[final[0]][1]] != ".":
            raise AssertionError(f"{s.sid}: the final '.' is merged with other text")
        sites = {
            "verb": span_toks([past]),
            "was": span_toks(["was"]),
            "by": span_toks(["by"]),
            "det": span_toks(["The", "the"]),
            "period": final,
            "sentence": sent_toks,
            "question": list(range(sent_toks[-1] + 1, len(ids) - 1)),
            "last": [len(ids) - 1],
        }
    return {"ids": ids, "n": len(ids), "ent": ent, "sites": sites}


def answer_token(tokenizer: Any, word: str) -> int:
    """First token of " word" (entities are exactly one token; capitals may be more)."""
    return tokenizer.encode(f" {word}", add_special_tokens=False)[0]


def check_vocab(tokenizer: Any) -> None:
    """Entities and names are one token with a leading space; capitals' first tokens differ."""
    for w in (*ENTITIES, *NAMES, *CAPITALS):
        if len(tokenizer.encode(f" {w}", add_special_tokens=False)) != 1:
            raise AssertionError(f"{w!r} is not one token")
    firsts = [answer_token(tokenizer, c) for c in CAPITALS.values()]
    if len(set(firsts)) != len(firsts):
        raise AssertionError("two capitals share a first token")


def write_stimuli(path: Path, seed: int = SEED) -> str:
    sents = build_all(seed)
    obj = {"seed": seed, "sentences": [asdict(s) for s in sents]}
    data = json.dumps(obj, indent=1) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return hashlib.sha256(data.encode()).hexdigest()


def load_stimuli(path: Path) -> list[Sent]:
    obj = json.loads(Path(path).read_text())
    return [Sent(**{k: tuple(v) if isinstance(v, list) else v for k, v in s.items()})
            for s in obj["sentences"]]  # fmt: skip


if __name__ == "__main__":
    import sys

    p = Path(sys.argv[1] if len(sys.argv) > 1 else "data/stimuli.json")
    print(p, write_stimuli(p))
