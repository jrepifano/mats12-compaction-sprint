"""P03 frozen interfaces: item and sample schemas, answer normalisation, modal answers, matching,
and the interval statistics. Every other P03 module imports from here; nothing here does I/O
beyond JSONL helpers, and nothing imports torch, vLLM or an API client.

Paths are relative to the probe directory (``PROBE``), so the same code runs on the laptop and
on the pod (/ws/emx/probes/P03-text-proof-interp-items).
"""

from __future__ import annotations

import json
import math
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
DATA = PROBE / "data"
RESULTS = PROBE / "results"

EXP = "P03"
MODEL_ID = "Qwen/Qwen3.6-27B"
MODEL_REV = "6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"

# Ground truth (plan "Setup"): 20 samples per prompt, an answer counts if its modal share >= 0.6.
N_SAMPLES = 20
MODAL_MIN = 0.6
MAX_TOKENS = 16
SAMPLE_SEED = 0  # request seed = SAMPLE_SEED * 10**6 + crc of key + sample index (see request_seed)
# Qwen3.6-27B card, non-thinking ("instruct") settings, as emx.i9.contracts.SAMPLING[("qwen", False)]
SAMPLING: dict[str, float | int] = {
    "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
    "presence_penalty": 1.5, "repetition_penalty": 1.0,
}

# The read (plan "Setup"): chat render, thinking off, WorkspaceBench's answer stem prefilled
# (wsbench.readplan.ANSWER_STEM), read at the last 3 prompt tokens, layers 20..60 step 4.
ANSWER_STEM = "My one-word answer is:"
NAME_STEM = "My answer is:"  # type A hop-1 only (multi-word names); never read
READ_LAYERS: tuple[int, ...] = tuple(range(20, 61, 4))  # 11 layers (= emx.i1a.contracts.GRID)
READ_POSITIONS: tuple[int, ...] = (-3, -2, -1)  # offsets from the end of the prefilled render
FINAL_LAYER = 63  # captured for the smoke check only (final-layer logit lens vs modal answer)

TYPES = ("A", "B", "C")
TEXT_PROOF_MIN_SURVIVORS = 40
TEXT_PROOF_MAX_RATE = 0.20
TEXT_PROOF_MAX_UPPER = 0.30


# --------------------------------------------------------------------------- schemas


@dataclass
class Item:
    """One candidate item (data/items.jsonl).

    type A: ``prompt`` is the two-hop question (answer: a country); ``hop1_prompt`` asks the
    bridge alone (answered after NAME_STEM); ``hop2_template`` has one ``{X}`` slot for a bridge
    name; ``bridge_true`` / ``answer_true`` and their aliases come from Wikidata.
    type B: ``prompt`` asks the favourite ``category``; ``source`` is "gen" or "ws" (WorkspaceBench
    implicit item, prompt verbatim, ``ws_target`` its gated answer).
    type C: ``prompt`` composes the favourite ``category`` with a property; ``property_template``
    asks that property of ``{X}``; ``b_id`` is the B item of the same category (its modal answer
    is C's hidden target).
    """

    id: str
    type: str
    category: str
    prompt: str
    prefill: str = ANSWER_STEM
    source: str = "gen"
    hop1_prompt: str | None = None
    hop2_template: str | None = None
    bridge_true: str | None = None
    bridge_aliases: list[str] = field(default_factory=list)
    answer_true: str | None = None
    answer_aliases: list[str] = field(default_factory=list)
    relation: str | None = None
    entity: str | None = None
    property_template: str | None = None
    b_id: str | None = None
    ws_target: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"{self.id}: type {self.type!r} not in {TYPES}")
        if self.type == "A" and not (self.hop1_prompt and self.hop2_template and self.bridge_true):
            raise ValueError(f"{self.id}: type A needs hop1_prompt, hop2_template, bridge_true")
        if self.type == "C" and not (self.property_template and self.b_id):
            raise ValueError(f"{self.id}: type C needs property_template and b_id")
        for t in (self.hop2_template, self.property_template):
            if t is not None and t.count("{X}") != 1:
                raise ValueError(f"{self.id}: template needs exactly one {{X}}: {t!r}")


# Sampling requests (pod session 1). kind: "main" (the item's prompt), "hop1" (A's bridge
# question), "hop2" (A: hop2_template filled with Qwen's modal bridge), "prop" (C:
# property_template filled with Qwen's modal B answer for the category). Round 1 = main + hop1,
# round 2 = hop2 + prop (built on the pod from round 1's modal answers by ``round2_requests``).
KINDS = ("main", "hop1", "hop2", "prop")


@dataclass
class Request:
    key: str  # "<item id>|<kind>"
    id: str
    kind: str
    prompt: str
    prefill: str
    fill: str | None = None  # the {X} filler for hop2/prop


@dataclass
class SampleRow:
    """results/samples.jsonl, one row per Request."""

    key: str
    id: str
    kind: str
    prompt: str
    prefill: str
    fill: str | None
    samples: list[str]
    seeds: list[int]
    finish: list[str]
    engine: str = "vllm"


def request_seed(key: str, i: int) -> int:
    """Deterministic per-sample seed (stable across processes; no Python hash())."""
    import zlib

    return (SAMPLE_SEED * 1_000_003 + zlib.crc32(key.encode()) * 64 + i) % (2**31 - 1)


def round1_requests(items: Sequence[Item]) -> list[Request]:
    out: list[Request] = []
    for it in items:
        out.append(Request(f"{it.id}|main", it.id, "main", it.prompt, it.prefill))
        if it.type == "A":
            out.append(Request(f"{it.id}|hop1", it.id, "hop1", it.hop1_prompt or "", NAME_STEM))
    return out


def round2_requests(items: Sequence[Item], rows: dict[str, SampleRow]) -> list[Request]:
    """hop2 for every A item whose hop-1 answer is consistent; prop for every C item whose B
    item's answer is consistent. The filler is the modal class's most common raw surface form."""
    out: list[Request] = []
    for it in items:
        if it.type == "A":
            src = rows.get(f"{it.id}|hop1")
            tmpl = it.hop2_template
            kind = "hop2"
        elif it.type == "C":
            src = rows.get(f"{it.b_id}|main")
            tmpl = it.property_template
            kind = "prop"
        else:
            continue
        if src is None or tmpl is None:
            continue
        m = modal(src.samples)
        if not m.consistent:
            continue
        out.append(
            Request(f"{it.id}|{kind}", it.id, kind, tmpl.replace("{X}", m.surface),
                    ANSWER_STEM, fill=m.surface)
        )
    return out


# --------------------------------------------------------------------------- normalisation

_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)
_EDGE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def first_answer(text: str) -> str:
    """The answer part of a sampled continuation: the first non-empty line, markdown emphasis
    and quotes removed, cut at the first sentence end (". " / "(" / " - ")."""
    t = (text or "").replace("**", "").replace("__", "")
    for line in t.splitlines():
        if line.strip():
            t = line.strip()
            break
    else:
        return ""
    t = re.split(r"\s+\(|\.\s|\s[-–—]\s|[;!?]", t, maxsplit=1)[0]
    return t.strip().strip("\"'`“”‘’").strip()


def norm(text: str) -> str:
    """Canonical comparison form: first answer, accents stripped, lower case, leading article
    removed, edge punctuation removed, inner whitespace collapsed."""
    t = strip_accents(first_answer(text)).lower()
    t = _ARTICLE.sub("", t.strip())
    t = _EDGE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class Modal:
    answer: str  # normalised modal answer ("" when every sample was empty)
    surface: str  # most common raw first_answer() within the modal class
    share: float
    n: int
    counts: dict[str, int]

    @property
    def consistent(self) -> bool:
        return bool(self.answer) and self.share >= MODAL_MIN


def modal(samples: Sequence[str]) -> Modal:
    """Modal normalised answer and its share of all samples (empty answers count in n).
    Ties break by first appearance, so the result is deterministic."""
    normed = [norm(s) for s in samples]
    counts = Counter(x for x in normed if x)
    if not counts:
        return Modal("", "", 0.0, len(samples), {})
    order = {x: i for i, x in reversed(list(enumerate(normed)))}
    ans = max(counts, key=lambda x: (counts[x], -order[x]))
    surfaces = Counter(first_answer(s).rstrip(".") for s, x in zip(samples, normed) if x == ans)
    surface = max(surfaces, key=lambda s: (surfaces[s], s))
    return Modal(ans, surface, counts[ans] / max(len(samples), 1), len(samples), dict(counts))


def singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def alias_set(target: str, aliases: Iterable[str] = ()) -> set[str]:
    """Normalised forms of a target and its aliases, each also singularised."""
    out: set[str] = set()
    for a in (target, *aliases):
        n = norm(a)
        if n:
            out.add(n)
            out.add(" ".join(singular(w) for w in n.split()))
    return out


def same(answer: str, target: str, aliases: Iterable[str] = ()) -> bool:
    """String-level match of a free answer against a target with aliases (no LLM)."""
    a = norm(answer)
    if not a:
        return False
    forms = alias_set(target, aliases)
    return a in forms or " ".join(singular(w) for w in a.split()) in forms


# --------------------------------------------------------------------------- statistics


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float, float]:
    """(rate, lower, upper) Wilson score interval; (nan, nan, nan) for n == 0."""
    if n == 0:
        return (math.nan, math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def paired_bootstrap(
    a: Sequence[float], b: Sequence[float], *, n_boot: int = 10_000, seed: int = 0
) -> tuple[float, float, float]:
    """Mean of a - b over items and its 95% percentile interval (resampling items)."""
    if len(a) != len(b):
        raise ValueError("paired samples differ in length")
    n = len(a)
    if n == 0:
        return (math.nan, math.nan, math.nan)
    d = [x - y for x, y in zip(a, b)]
    rng = random.Random(seed)
    means = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (sum(d) / n, means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1])


# --------------------------------------------------------------------------- JSONL helpers


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: Iterable[Any]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(asdict(r) if hasattr(r, "__dataclass_fields__") else r,
                               ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(path)
    return n


def load_items(path: Path | None = None) -> list[Item]:
    return [Item(**r) for r in read_jsonl(path or DATA / "items.jsonl")]


def load_samples(path: Path | None = None) -> dict[str, SampleRow]:
    return {r["key"]: SampleRow(**r) for r in read_jsonl(path or RESULTS / "samples.jsonl")}
