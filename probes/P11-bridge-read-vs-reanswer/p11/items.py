"""P11 items, arm prompts and the deterministic answer scoring. Pure Python (no torch).

Items are P03's 63 strict wrong-bridge survivors (``probes/P03-text-proof-interp-items/data/
selection.jsonl``, type A, ``survivor`` and ``strict``), frozen into ``data/items.jsonl`` by
``python -m p11.items build`` with everything the pod and the scorer need:

- ``bridge`` / ``bridge_aliases``: Qwen's own (wrong) bridge, as P03's reader items carry it;
- ``y`` / ``y_aliases``: the true bridge Y (Wikidata creator), the bridge the premises state;
- ``y_country`` / ``y_country_aliases`` and ``y_nation_forms``: Y's country of citizenship and
  the answer forms that count as "Qwen now answers Y's nationality" (a flip);
- ``qwen_answer``: Qwen's modal two-hop answer (P03 session 1, 20 samples).

Arm prompts (plan "Setup"), all with P03's prefill ``My one-word answer is:``:

- ``plain``: P03's prompt;
- ``author``: ``Note: the {role} of {entity} is {Y}.`` + ``\\n\\n`` + the plain question;
- ``mention``: ``Note: a review of {entity} was written by {Y}.`` + ``\\n\\n`` + the question;
- ``neutral``: ``{Entity}. Repeat this title exactly.`` (no question).

The ``\\n\\n`` separator makes the premise prompts end in exactly the plain prompt's tokens after
``<|im_start|>user\\n`` (the question suffix; asserted on the pod and in the tests).

Flips are scored with a hand-written demonym table (``DEMONYMS``), not an API judge: an answer
flips when its normalised form is Y's country, an alias or a demonym, and stays when it matches
Qwen's modal answer (``p03.core.same``). ``build`` refuses an item whose flip forms would
match Qwen's own answer.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from p03 import core as C

PROBE = Path(__file__).resolve().parents[1]
DATA = PROBE / "data"
RESULTS = PROBE / "results"
P03 = PROBE.parent / "P03-text-proof-interp-items"
ITEMS_PATH = DATA / "items.jsonl"
SELECTION_SHA256 = "611a26428bd226524df396d6d81af79a163693c1210f9f81cae47d34abcc44f0"
N_ITEMS = 63

PREFILL = C.ANSWER_STEM  # "My one-word answer is:"
SEP = "\n\n"
ARMS = ("plain", "neutral", "author", "mention")
ROLE = {
    "painting_creator": "painter",
    "music_composer": "composer",
    "book_author": "author",
    "film_director": "director",
}

# Country (P03's Wikidata label) -> answer forms that name its nationality. Hand-written for the
# 25 countries of the 63 items (a test asserts coverage); matched after p03.core.norm.
DEMONYMS: dict[str, tuple[str, ...]] = {
    "France": ("French",),
    "United Kingdom": ("British", "English", "Scottish", "Welsh", "Briton"),
    "Scotland": ("Scottish", "Scots", "Scotch"),
    "Spain": ("Spanish",),
    "United States": ("American", "US", "USA"),
    "Sweden": ("Swedish",),
    "Belgium": ("Belgian",),
    "Germany": ("German", "West German"),
    "Norway": ("Norwegian",),
    "South Korea": ("Korean", "South Korean"),
    "Kingdom of Denmark": ("Danish", "Dane"),
    "People's Republic of China": ("Chinese",),
    "Canada": ("Canadian",),
    "Hungary": ("Hungarian",),
    "Romania": ("Romanian",),
    "Philippines": ("Filipino", "Philippine"),
    "Poland": ("Polish",),
    "Argentina": ("Argentine", "Argentinian", "Argentinean"),
    "Switzerland": ("Swiss",),
    "Cuba": ("Cuban",),
    "Greece": ("Greek",),
    "Kingdom of the Netherlands": ("Dutch", "Netherlandish"),
    "South Africa": ("South African",),
    "Australia": ("Australian",),
    "Chile": ("Chilean",),
}


@dataclass(frozen=True)
class Item:
    id: str
    relation: str
    role: str
    entity: str  # P03's entity phrase, e.g. 'the painting "Golden cylinder" (1910)'
    question: str  # P03's two-hop prompt (the plain arm's user turn)
    bridge: str  # Qwen's modal hop-1 answer (its wrong bridge)
    bridge_aliases: list[str]
    y: str  # the true bridge (Wikidata)
    y_aliases: list[str]
    y_country: str
    y_country_aliases: list[str]
    qwen_answer: str  # Qwen's modal two-hop answer surface
    qwen_answer_share: float
    y_nation_forms: list[str] = field(default_factory=list)

    def prompt(self, arm: str) -> str:
        """The arm's user turn (plan "Setup")."""
        if arm == "plain":
            return self.question
        if arm == "author":
            return f"Note: the {self.role} of {self.entity} is {self.y}.{SEP}{self.question}"
        if arm == "mention":
            return f"Note: a review of {self.entity} was written by {self.y}.{SEP}{self.question}"
        if arm == "neutral":
            e = self.entity
            return f"{e[:1].upper()}{e[1:]}. Repeat this title exactly."
        raise KeyError(arm)


# --------------------------------------------------------------------- answers


def flip_forms(country: str, aliases: Iterable[str]) -> list[str]:
    """Country, its aliases and its demonyms (the forms a flipped answer may take)."""
    if country not in DEMONYMS:
        raise KeyError(f"no demonyms for {country!r}")
    out: list[str] = []
    for f in (country, *aliases, *DEMONYMS[country]):
        if f not in out:
            out.append(f)
    return out


def flipped(answer: str, it: Item) -> bool:
    """The answer names Y's nationality (country, alias or demonym; ``p03.core.same``)."""
    return C.same(answer, it.y_nation_forms[0], it.y_nation_forms[1:])


def stayed(answer: str, it: Item) -> bool:
    """The answer is Qwen's own modal two-hop answer."""
    return C.same(answer, it.qwen_answer)


def answer_class(answer: str, it: Item) -> str:
    """ "flip", "stay" or "other"."""
    if flipped(answer, it):
        return "flip"
    if stayed(answer, it):
        return "stay"
    return "other"


_PAREN = re.compile(r"\s*\(([^()]*)\)\s*")


def clean_aliases(aliases: Iterable[str]) -> list[str]:
    """Aliases with parenthetical qualifiers removed ("Gustave Moreau (French painter)" ->
    "Gustave Moreau"); a parenthetical of two or more capitalised words is kept as an alias of
    its own ("Νικόλαος Γύζης (Nikolaos Gyzis)"). P03's ``score.surname`` turned "(French
    painter)" into the surname "painter", so any text saying "painter" named the bridge."""
    out: list[str] = []
    for a in aliases:
        inner = _PAREN.findall(a)
        for x in (_PAREN.sub(" ", a).strip(), *inner):
            words = x.split()
            keep = x == _PAREN.sub(" ", a).strip() or (
                len(words) >= 2 and all(w[:1].isupper() for w in words)
            )
            if x and keep and x not in out:
                out.append(x)
    return out


def names(text: str, target: str, aliases: Iterable[str]) -> bool:
    """P03's cell rule (``p03.score.text_hit`` on person forms) with cleaned aliases."""
    from p03 import score as SC

    _, phr = SC.forms(target, clean_aliases([target, *aliases]), True)
    return SC.text_hit([text], phr)


# --------------------------------------------------------------------- build and load


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out: Path = ITEMS_PATH) -> list[Item]:
    """Freeze the 63 strict survivors from P03's committed data into ``out``."""
    sel_p = P03 / "data" / "selection.jsonl"
    if _sha(sel_p) != SELECTION_SHA256:
        raise ValueError(f"{sel_p}: sha256 differs from the plan's pin")
    sel = [r for r in C.read_jsonl(sel_p) if r["type"] == "A" and r.get("survivor")]
    strict = [r for r in sel if r.get("strict")]
    raw = {}
    for f in ("items_A.jsonl", "items_A2.jsonl"):
        for r in C.read_jsonl(P03 / "data" / f):
            raw[r["id"]] = r
    reader = {r["id"]: r for r in C.read_jsonl(P03 / "data" / "reader_items.jsonl")}
    items: list[Item] = []
    for s in strict:
        a, ri = raw[s["id"]], reader[s["id"]]
        if ri["target"] != s["target"]:
            raise ValueError(f"{s['id']}: reader target {ri['target']!r} != {s['target']!r}")
        it = Item(
            id=s["id"],
            relation=a["relation"],
            role=ROLE[a["relation"]],
            entity=a["entity"],
            question=a["prompt"],
            bridge=s["target"],
            bridge_aliases=list(ri["target_aliases"]),
            y=a["bridge_true"],
            y_aliases=list(a["bridge_aliases"]),
            y_country=a["answer_true"],
            y_country_aliases=list(a["answer_aliases"]),
            qwen_answer=s["modal"]["main"][0],
            qwen_answer_share=float(s["modal"]["main"][1]),
        )
        it = Item(
            **{**asdict(it), "y_nation_forms": flip_forms(it.y_country, it.y_country_aliases)}
        )
        if f"the {it.role} of {it.entity}?" not in it.question:
            raise ValueError(f"{it.id}: question does not contain 'the {it.role} of {it.entity}?'")
        if flipped(it.qwen_answer, it):
            raise ValueError(f"{it.id}: Qwen's answer {it.qwen_answer!r} counts as a flip")
        items.append(it)
    if len(items) != N_ITEMS:
        raise ValueError(f"{len(items)} strict survivors, expected {N_ITEMS}")
    C.write_jsonl(out, [asdict(i) for i in items])
    return items


def load(path: Path = ITEMS_PATH) -> list[Item]:
    items = [Item(**r) for r in C.read_jsonl(path)]
    if len(items) != N_ITEMS or len({i.id for i in items}) != N_ITEMS:
        raise ValueError(f"{path}: expected {N_ITEMS} distinct items")
    return items


# --------------------------------------------------------------------- token suffix


def common_suffix(a: Sequence[int], b: Sequence[int]) -> int:
    """Length of the longest common suffix of two id lists."""
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["build"]:
        items = build()
        print(f"wrote {len(items)} items to {ITEMS_PATH} sha256={_sha(ITEMS_PATH)}")
        return 0
    print("usage: python -m p11.items build", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
