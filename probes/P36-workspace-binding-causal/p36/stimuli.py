"""P36 stimuli: role-reversed twin sentences, questions, renders and patch sites (plan.md "Setup").

Pure Python except ``render_all`` (needs a tokenizer). Seeded; ``build_items`` is deterministic.

- 24 role-neutral person nouns, 20 transitive verbs. Every noun is one token with a leading
  space in Qwen3.6-27B's tokenizer (checked by ``render_all``), so twins align token for token.
- 100 base items (an unordered entity pair and a verb): 60 from train entities x train verbs,
  20 with both entities held out (train verbs), 20 with held-out verbs (train entities).
- Each base item in two voices, each voice as a twin pair: the designated entity (alphabetically
  first) as agent ("d_agent") and as patient ("d_patient"). 400 sentences in all.
- Two questions per sentence: agent ("Who did the <gerund>?") and patient ("Who was <pp>?").
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SEED = 20261013
ENTITIES: tuple[str, ...] = (
    "teacher", "doctor", "farmer", "baker", "nurse", "pilot", "singer", "dancer",
    "lawyer", "judge", "soldier", "sailor", "driver", "writer", "painter", "banker",
    "chef", "clerk", "coach", "student", "waiter", "artist", "actor", "tailor",
)  # fmt: skip
# (past = participle, gerund)
VERBS: dict[str, tuple[str, str]] = {
    "chase": ("chased", "chasing"),
    "push": ("pushed", "pushing"),
    "call": ("called", "calling"),
    "help": ("helped", "helping"),
    "follow": ("followed", "following"),
    "thank": ("thanked", "thanking"),
    "blame": ("blamed", "blaming"),
    "hug": ("hugged", "hugging"),
    "praise": ("praised", "praising"),
    "kick": ("kicked", "kicking"),
    "warn": ("warned", "warning"),
    "visit": ("visited", "visiting"),
    "tease": ("teased", "teasing"),
    "rescue": ("rescued", "rescuing"),
    "hire": ("hired", "hiring"),
    "greet": ("greeted", "greeting"),
    "interview": ("interviewed", "interviewing"),
    "scold": ("scolded", "scolding"),
    "photograph": ("photographed", "photographing"),
    "tickle": ("tickled", "tickling"),
}
N_HELD_ENT = 8
N_HELD_VERB = 6
N_TRAIN_ITEMS, N_HELD_ENT_ITEMS, N_HELD_VERB_ITEMS = 60, 20, 20
VOICES: tuple[str, ...] = ("active", "passive")
ROLES: tuple[str, ...] = ("d_agent", "d_patient")  # the designated entity's role
QUESTIONS: tuple[str, ...] = ("agent", "patient")
PREFILL = "The"


@dataclass(frozen=True)
class Item:
    item: int  # base item index 0..99
    split: str  # "train" | "held_entity" | "held_verb"
    e1: str  # designated entity (alphabetically first)
    e2: str
    verb: str


@dataclass(frozen=True)
class Sentence:
    sid: str  # f"{item}-{voice}-{role}"
    item: int
    split: str
    voice: str
    role: str  # designated entity's role
    agent: str
    patient: str
    verb: str
    text: str


def splits(seed: int = SEED) -> tuple[list[str], list[str], list[str], list[str]]:
    """(train entities, held entities, train verbs, held verbs), seeded shuffles."""
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
    """100 base items with distinct entity pairs; each verb used about equally within a split."""
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


def sentence_text(agent: str, patient: str, verb: str, voice: str) -> str:
    past = VERBS[verb][0]
    if voice == "active":
        return f"The {agent} {past} the {patient}."
    return f"The {patient} was {past} by the {agent}."


def build_sentences(items: list[Item]) -> list[Sentence]:
    out = []
    for it in items:
        for voice in VOICES:
            for role in ROLES:
                agent, patient = (it.e1, it.e2) if role == "d_agent" else (it.e2, it.e1)
                out.append(
                    Sentence(
                        sid=f"{it.item}-{voice}-{role}",
                        item=it.item,
                        split=it.split,
                        voice=voice,
                        role=role,
                        agent=agent,
                        patient=patient,
                        verb=it.verb,
                        text=sentence_text(agent, patient, it.verb, voice),
                    )
                )
    return out


def twin_sid(sid: str) -> str:
    item, voice, role, *rest = sid.split("-")
    flip = "d_patient" if role == "d_agent" else "d_agent"
    return "-".join([item, voice, flip, *rest])


SHIFT_PREFIX = "Yesterday afternoon, the"  # follow-up F1: 3 tokens longer than "The"


def shifted(sents: list[Sentence]) -> list[Sentence]:
    """Follow-up F1: each sentence with "Yesterday afternoon, the ..." in place of "The ...":
    the same words, roles and mention order at shifted absolute positions (sid suffix "-s")."""
    out = []
    for s in sents:
        assert s.text.startswith("The ")
        out.append(
            Sentence(
                sid=f"{s.sid}-s",
                item=s.item,
                split=s.split,
                voice=s.voice,
                role=s.role,
                agent=s.agent,
                patient=s.patient,
                verb=s.verb,
                text=f"{SHIFT_PREFIX} {s.text[len('The ') :]}",
            )
        )
    return out


def question_text(verb: str, q: str) -> str:
    past, gerund = VERBS[verb]
    return f"Who did the {gerund}?" if q == "agent" else f"Who was {past}?"


def answer(s: Sentence, q: str) -> str:
    return s.agent if q == "agent" else s.patient


def user_text(s: Sentence, q: str) -> str:
    return f"Sentence: {s.text}\nQuestion: {question_text(s.verb, q)} Answer with one word."


# --------------------------------------------------------------------- rendering (tokenizer)


def render(tokenizer: Any, s: Sentence, q: str) -> dict[str, Any]:
    """Chat render (thinking off) + prefill; token ids and the patch-site positions:
    ``final`` = the sentence's '.' token, ``ent`` = {entity: position} for both entities."""
    msgs = [{"role": "user", "content": user_text(s, q)}]
    text = tokenizer.apply_chat_template(
        msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    text += PREFILL
    enc = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offs = list(enc["input_ids"]), list(enc["offset_mapping"])
    s0 = text.index("Sentence: ") + len("Sentence: ")
    s1 = s0 + len(s.text)
    if text[s0:s1] != s.text:
        raise AssertionError(f"{s.sid}: sentence not found in render")

    def tok_at(lo: int, hi: int) -> int:
        hits = [i for i, (a, b) in enumerate(offs) if a < hi and b > lo]
        if len(hits) != 1:
            raise AssertionError(f"{s.sid}: span {text[lo:hi]!r} covers tokens {hits}")
        return hits[0]

    ent: dict[str, int] = {}
    for e in (s.agent, s.patient):
        lo = text.index(f" {e}", s0, s1) + 1
        p = tok_at(lo - 1, lo + len(e))
        if offs[p] != (lo - 1, lo + len(e)):
            raise AssertionError(f"{s.sid}: entity {e!r} is not exactly one token")
        ent[e] = p
    final = tok_at(s1 - 1, s1)
    if text[offs[final][0] : offs[final][1]] != ".":
        raise AssertionError(f"{s.sid}: the final '.' is merged with other text")
    return {"ids": ids, "final": final, "ent": ent, "n": len(ids)}


def answer_token(tokenizer: Any, entity: str) -> int:
    ids = tokenizer.encode(f" {entity}", add_special_tokens=False)
    if len(ids) != 1:
        raise AssertionError(f"{entity!r} is {len(ids)} tokens")
    return ids[0]


def render_all(tokenizer: Any, sentences: list[Sentence]) -> dict[tuple[str, str], dict]:
    """Renders for every (sid, question); asserts twins align (same length, same '.' position,
    entities swapped between the two slots)."""
    out = {(s.sid, q): render(tokenizer, s, q) for s in sentences for q in QUESTIONS}
    by_sid = {s.sid: s for s in sentences}
    for (sid, q), r in out.items():
        t = out[(twin_sid(sid), q)]
        s = by_sid[sid]
        if r["n"] != t["n"] or r["final"] != t["final"]:
            raise AssertionError(f"{sid}/{q}: twin renders do not align")
        if r["ent"][s.agent] != t["ent"][s.patient] or r["ent"][s.patient] != t["ent"][s.agent]:
            raise AssertionError(f"{sid}/{q}: twin entity slots do not swap")
    for e in ENTITIES:
        answer_token(tokenizer, e)
    return out


def write_stimuli(path: Path, seed: int = SEED) -> str:
    """Items and sentences as JSON; returns the file's sha256."""
    items = build_items(seed)
    sents = build_sentences(items)
    obj = {
        "seed": seed,
        "splits": dict(zip(("train_ent", "held_ent", "train_verb", "held_verb"), splits(seed))),
        "items": [asdict(i) for i in items],
        "sentences": [asdict(s) for s in sents],
    }
    data = json.dumps(obj, indent=1) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return hashlib.sha256(data.encode()).hexdigest()


def load_stimuli(path: Path) -> tuple[list[Item], list[Sentence]]:
    obj = json.loads(Path(path).read_text())
    return [Item(**i) for i in obj["items"]], [Sentence(**s) for s in obj["sentences"]]


if __name__ == "__main__":
    import sys

    p = Path(sys.argv[1] if len(sys.argv) > 1 else "data/stimuli.json")
    print(p, write_stimuli(p))
