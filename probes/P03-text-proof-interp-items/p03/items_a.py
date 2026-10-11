"""Type-A ("wrong bridge") items from Wikidata (P03 log, design item 2).

Two hops: a long-tail work -> its creator (the bridge, a person) -> the creator's country of
citizenship (the answer). Four relations: film -> director, literary work -> author, painting ->
painter, musical work -> composer. Every Wikidata response is cached under
``data/wikidata_raw/`` (query text, retrieval UTC time, response), so a rebuild with
``--offline`` reproduces the item set exactly.

Run from the probe directory::

    cd probes/P03-text-proof-interp-items && uv run python -m p03.items_a --n 2000

Writes ``data/items_A.jsonl`` (``p03.core.Item`` rows, type "A") and ``data/items_A.meta.json``.
The selection functions below (``parse_*``, ``label_reject``, ``accept``, ``build_items``) do
no I/O, so the tests drive them with canned SPARQL JSON.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from p03 import core

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "P03-probe/0.1 (research; jrepifano@gmail.com)"
RAW = core.DATA / "wikidata_raw"
OUT = core.DATA / "items_A.jsonl"
META = core.DATA / "items_A.meta.json"
SEED = 0
PER_CREATOR = 2
SITELINKS = (2, 12)
POOL_LIMIT = 1500  # rows per pool query (one year window)
POOL_LIMIT_ALL = 30000  # rows for a relation queried without a year window (music)
BATCH = 150  # QIDs per VALUES query

# Prompt templates (exact; see the P03 build brief).
PROMPT = "What is the country of citizenship of the {role} of {entity}? Answer in one word."
HOP1 = "Who is the {role} of {entity}? Answer with the name only."
HOP2 = "What is the country of citizenship of {X}? Answer in one word."

WD = "http://www.wikidata.org/entity/"

# Forms / classes that name the work in the entity text.
NOUNS = {
    "Q8261": "novel", "Q149537": "novella", "Q49084": "short story", "Q25379": "play",
    "Q5185279": "poem", "Q1279564": "short story collection", "Q12106333": "poetry collection",
    "Q1344": "opera", "Q7366": "song",
}


def _windows(edges: Sequence[int]) -> list[tuple[int, int]]:
    return list(pairwise(edges))


@dataclass(frozen=True)
class Relation:
    key: str
    role: str
    classes: tuple[str, ...]
    creator_prop: str
    date_props: tuple[str, ...]
    windows: tuple[tuple[int, int], ...]  # [lo, hi) years; (0, 0) = no date window
    default_noun: str
    nouns: tuple[str, ...] = ()  # NOUNS keys that may replace default_noun, in priority order

    def entity(self, label: str, year: int, noun: str) -> str:
        if self.key in ("film_director", "book_author"):
            return f'the {year} {noun} "{label}"'
        return f'the {noun} "{label}" ({year})'


RELATIONS: dict[str, Relation] = {
    r.key: r
    for r in (
        Relation("film_director", "director", ("Q11424",), "P57", ("P577",),
                 tuple(_windows(range(1920, 2031, 10))), "film"),
        Relation("book_author", "author", ("Q7725634", "Q8261"), "P50", ("P577",),
                 tuple(_windows([1800, 1850, 1900, *range(1910, 2031, 10)])), "book",
                 ("Q8261", "Q149537", "Q49084", "Q25379", "Q5185279", "Q1279564",
                  "Q12106333")),
        Relation("painting_creator", "painter", ("Q3305213",), "P170", ("P571",),
                 tuple(_windows([1400, 1600, 1700, 1800, 1850, 1880, 1900, 1920, 1950, 2030])),
                 "painting"),
        Relation("music_composer", "composer", ("Q105543609",), "P86", ("P577", "P571"),
                 ((0, 0),), "composition", ("Q1344", "Q7366")),
    )
}

# Hand aliases for common countries (added to Wikidata's altLabels).
COUNTRY_ALIASES: dict[str, list[str]] = {
    "Q30": ["United States", "United States of America", "USA", "US", "U.S.", "U.S.A.",
            "America"],
    "Q145": ["United Kingdom", "UK", "U.K.", "Britain", "Great Britain", "England"],
    "Q29999": ["Netherlands", "Holland", "The Netherlands"],
    "Q55": ["Netherlands", "Holland", "The Netherlands"],
    "Q148": ["China", "PRC"],
    "Q15180": ["Soviet Union", "USSR", "U.S.S.R."],
    "Q213": ["Czech Republic", "Czechia"],
    "Q884": ["South Korea", "Korea"],
    "Q27": ["Ireland", "Republic of Ireland"],
    "Q183": ["Germany", "Federal Republic of Germany"],
    "Q159": ["Russia", "Russian Federation"],
    "Q865": ["Taiwan", "Republic of China"],
}

# Labels too generic to identify a work even with its year.
_GENERIC = {
    "untitled", "portrait", "self", "landscape", "still", "life", "composition", "study",
    "sketch", "symphony", "sonata", "sonatina", "concerto", "quartet", "quintet", "sextet",
    "septet", "octet", "trio", "duo", "mass", "missa", "requiem", "prelude", "preludes",
    "etude", "etudes", "nocturne", "waltz", "overture", "suite", "fugue", "variations",
    "fantasia", "fantasy", "impromptu", "mazurka", "polonaise", "scherzo", "rondo", "ballade",
    "serenade", "divertimento", "cantata", "magnificat", "te", "deum", "stabat", "mater",
    "piano", "violin", "cello", "viola", "flute", "horn", "oboe", "clarinet", "string",
    "strings", "orchestra", "chamber", "organ", "guitar", "harp", "choir", "woman", "man",
    "girl", "boy", "head", "nude", "flowers", "madonna", "child", "poems", "stories",
}
_GENERIC_FILLER = {
    "no", "op", "in", "of", "for", "and", "with", "the", "a", "an", "major", "minor", "flat",
    "sharp", "b", "c", "d", "e", "f", "g", "bwv", "k", "kv", "hob", "woo", "s", "rv",
    "young", "old", "seated", "standing", "number",
}
_NAME_STOP = {
    "von", "van", "der", "den", "del", "della", "des", "les", "los", "las", "the", "and", "jr",
    "sir", "saint", "san", "santa", "mac", "bin", "ibn",
}


# --------------------------------------------------------------------------- SPARQL text


def _vals(qids: Sequence[str]) -> str:
    return " ".join(f"wd:{q}" for q in qids)


def pool_query(rel: Relation, window: tuple[int, int], limit: int | None = None) -> str:
    """Works of the relation's class(es) with an English label, a date, and 2..12 sitelinks.
    Rows give every creator of the work (counted in Python and again in ``validate_query``)."""
    lo, hi = window
    if limit is None:
        limit = POOL_LIMIT_ALL if (lo, hi) == (0, 0) else POOL_LIMIT
    cls = (f"wdt:P31 wd:{rel.classes[0]}" if len(rel.classes) == 1
           else "wdt:P31 ?cls")
    vcls = "" if len(rel.classes) == 1 else f"  VALUES ?cls {{ {_vals(rel.classes)} }}\n"
    dprop = "|".join(f"wdt:{p}" for p in rel.date_props)
    win = "" if (lo, hi) == (0, 0) else (
        f'  FILTER(?date >= "{lo:04d}-01-01T00:00:00Z"^^xsd:dateTime && '
        f'?date < "{hi:04d}-01-01T00:00:00Z"^^xsd:dateTime)\n')
    return (
        "SELECT ?work ?label ?date ?sl ?creator WHERE {\n"
        f"{vcls}"
        f"  ?work {cls}; wdt:{rel.creator_prop} ?creator; {dprop} ?date; "
        "wikibase:sitelinks ?sl.\n"
        f"  FILTER(?sl >= {SITELINKS[0]} && ?sl <= {SITELINKS[1]})\n"
        f"{win}"
        '  ?work rdfs:label ?label FILTER(LANG(?label) = "en")\n'
        f"}} LIMIT {limit}"
    )


def validate_query(rel: Relation, qids: Sequence[str]) -> str:
    """All creators, all non-deprecated date statements with precision, forms, genres, classes."""
    dp = "|".join(f"p:{p}" for p in rel.date_props)
    dv = "|".join(f"psv:{p}" for p in rel.date_props)
    sep = ';separator="|"'
    return (
        "SELECT ?work (GROUP_CONCAT(DISTINCT STR(?c)" + sep + ") AS ?creators)\n"
        "  (GROUP_CONCAT(DISTINCT CONCAT(STR(?t), \"@\", STR(?prec))" + sep + ") AS ?dates)\n"
        "  (GROUP_CONCAT(DISTINCT STR(?k)" + sep + ") AS ?kinds) WHERE {\n"
        f"  VALUES ?work {{ {_vals(qids)} }}\n"
        f"  OPTIONAL {{ ?work wdt:{rel.creator_prop} ?c }}\n"
        f"  OPTIONAL {{ ?work {dp} ?st. ?st wikibase:rank ?rk; {dv} ?tv.\n"
        "    ?tv wikibase:timeValue ?t; wikibase:timePrecision ?prec.\n"
        "    FILTER(?rk != wikibase:DeprecatedRank) }\n"
        "  OPTIONAL { ?work wdt:P31|wdt:P7937|wdt:P136 ?k }\n"
        "} GROUP BY ?work"
    )


def creator_query(qids: Sequence[str]) -> str:
    sep = ';separator="|"'
    return (
        "SELECT ?creator ?label ?human ?sl (GROUP_CONCAT(DISTINCT STR(?country)" + sep
        + ") AS ?countries)\n  (GROUP_CONCAT(DISTINCT ?alt" + sep + ") AS ?alts) WHERE {\n"
        f"  VALUES ?creator {{ {_vals(qids)} }}\n"
        '  OPTIONAL { ?creator rdfs:label ?label FILTER(LANG(?label) = "en") }\n'
        "  OPTIONAL { ?creator wdt:P27 ?country }\n"
        '  OPTIONAL { ?creator skos:altLabel ?alt FILTER(LANG(?alt) = "en") }\n'
        "  OPTIONAL { ?creator wikibase:sitelinks ?sl }\n"
        "  BIND(EXISTS { ?creator wdt:P31 wd:Q5 } AS ?human)\n"
        "} GROUP BY ?creator ?label ?human ?sl"
    )


def country_query(qids: Sequence[str]) -> str:
    sep = ';separator="|"'
    return (
        "SELECT ?country ?label (GROUP_CONCAT(DISTINCT ?alt" + sep + ") AS ?alts)\n"
        "  (MIN(?end) AS ?dissolved) WHERE {\n"
        f"  VALUES ?country {{ {_vals(qids)} }}\n"
        '  OPTIONAL { ?country rdfs:label ?label FILTER(LANG(?label) = "en") }\n'
        '  OPTIONAL { ?country skos:altLabel ?alt FILTER(LANG(?alt) = "en") }\n'
        "  OPTIONAL { ?country wdt:P576 ?end }\n"
        "} GROUP BY ?country ?label"
    )


# --------------------------------------------------------------------------- parsing


def _v(b: dict[str, Any], k: str) -> str | None:
    x = b.get(k)
    return None if x is None else x["value"]


def _qid(uri: str | None) -> str | None:
    return uri[len(WD):] if uri and uri.startswith(WD) else None


def _year(t: str) -> int | None:
    m = re.match(r"^([+-]?\d{1,6})-", t)
    return int(m.group(1)) if m else None


def _split(s: str | None) -> list[str]:
    return [x for x in (s or "").split("|") if x]


@dataclass
class Work:
    qid: str
    relation: str
    label: str
    sitelinks: int
    creators: set[str] = field(default_factory=set)


def parse_pool(bindings: Sequence[dict[str, Any]], relation: str) -> dict[str, Work]:
    out: dict[str, Work] = {}
    for b in bindings:
        q = _qid(_v(b, "work"))
        c = _qid(_v(b, "creator"))
        if not q:
            continue
        w = out.setdefault(q, Work(q, relation, _v(b, "label") or "", int(_v(b, "sl") or 0)))
        if c:
            w.creators.add(c)
    return out


@dataclass
class Validation:
    creators: list[str]
    year: int | None  # earliest year among statements of year (9) or finer precision
    kinds: list[str]


def parse_validation(bindings: Sequence[dict[str, Any]]) -> dict[str, Validation]:
    out: dict[str, Validation] = {}
    for b in bindings:
        q = _qid(_v(b, "work"))
        if not q:
            continue
        years = []
        for d in _split(_v(b, "dates")):
            t, _, prec = d.rpartition("@")
            y = _year(t)
            if y is not None and prec.isdigit() and int(prec) >= 9:
                years.append(y)
        out[q] = Validation(
            creators=sorted(x for x in (_qid(u) for u in _split(_v(b, "creators"))) if x),
            year=min(years) if years else None,
            kinds=sorted(x for x in (_qid(u) for u in _split(_v(b, "kinds"))) if x),
        )
    return out


@dataclass
class Creator:
    qid: str
    label: str | None
    human: bool
    countries: list[str]
    aliases: list[str]
    sitelinks: int


def parse_creators(bindings: Sequence[dict[str, Any]]) -> dict[str, Creator]:
    out: dict[str, Creator] = {}
    for b in bindings:
        q = _qid(_v(b, "creator"))
        if not q:
            continue
        human = (_v(b, "human") or "").lower() in ("true", "1")
        out[q] = Creator(
            qid=q, label=_v(b, "label"), human=human,
            countries=sorted(x for x in (_qid(u) for u in _split(_v(b, "countries"))) if x),
            aliases=sorted(set(_split(_v(b, "alts")))), sitelinks=int(_v(b, "sl") or 0),
        )
    return out


@dataclass
class Country:
    qid: str
    label: str | None
    aliases: list[str]
    dissolved: int | None = None  # year of P576 (dissolved/abolished), None if still exists


def parse_countries(bindings: Sequence[dict[str, Any]]) -> dict[str, Country]:
    out: dict[str, Country] = {}
    for b in bindings:
        q = _qid(_v(b, "country"))
        if q:
            end = _v(b, "dissolved")
            out[q] = Country(q, _v(b, "label"), sorted(set(_split(_v(b, "alts")))),
                             _year(end) if end else None)
    return out


# --------------------------------------------------------------------------- selection


def _key(label: str) -> str:
    return re.sub(r"\s+", " ", core.strip_accents(label).lower()).strip()


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", core.strip_accents(s).lower())


def label_reject(label: str) -> str | None:
    """Reason to drop a work label on its own (before any creator data), else None."""
    s = label.strip()
    if len(s) < 3:
        return "short"
    if re.fullmatch(r"[\d\s.,–-]+", s):
        return "numeric"
    if '"' in s or "“" in s or "”" in s:
        return "quote"
    if re.fullmatch(r"Q\d+", s):
        return "qid"
    ws = [w for w in _words(s) if not w.isdigit() and w not in _GENERIC_FILLER]
    ws = [w for w in ws if not re.fullmatch(r"[ivxlc]+", w)]  # roman numerals
    if not ws or all(w in _GENERIC for w in ws):
        return "generic"
    return None


def _contains(label: str, names: Sequence[str]) -> bool:
    lw = set(_words(label))
    lk = " " + " ".join(_words(label)) + " "
    for n in names:
        ws = _words(n)
        if not ws:
            continue
        if len(ws) > 1 and " " + " ".join(ws) + " " in lk:
            return True
        if any(len(w) >= 3 and w not in _NAME_STOP and w in lw for w in ws):
            return True
    return False


def country_aliases(country: Country) -> list[str]:
    al = [*country.aliases, *COUNTRY_ALIASES.get(country.qid, [])]
    seen: set[str] = {_key(country.label or "")}
    out: list[str] = []
    for a in al:
        k = _key(a)
        if k and k not in seen:
            seen.add(k)
            out.append(a)
    return out


def accept(
    work: Work, val: Validation | None, creator: Creator | None,
    countries: dict[str, Country],
) -> str | None:
    """Reason to drop a candidate given its validation and creator data, else None."""
    if val is None:
        return "no_validation"
    if len(val.creators) != 1:
        return "creators!=1"
    if val.year is None:
        return "no_year"
    if creator is None or creator.qid != val.creators[0]:
        return "no_creator"
    if not creator.human:
        return "not_human"
    if not creator.label:
        return "creator_no_label"
    if len(creator.countries) != 1:
        return "citizenship!=1"
    country = countries.get(creator.countries[0])
    if country is None or not country.label:
        return "country_no_label"
    if _contains(work.label, [creator.label, *creator.aliases]):
        return "label_has_creator"
    if _contains(work.label, [country.label, *country_aliases(country)]):
        return "label_has_country"
    return None


def noun_for(rel: Relation, kinds: Sequence[str]) -> str:
    for k in rel.nouns:
        if k in kinds:
            return NOUNS[k]
    return rel.default_noun


def make_item(rel: Relation, work: Work, val: Validation, creator: Creator,
              country: Country) -> core.Item:
    assert val.year is not None and creator.label and country.label
    noun = noun_for(rel, val.kinds)
    entity = rel.entity(work.label, val.year, noun)
    bridge_aliases = [a for a in creator.aliases if _key(a) != _key(creator.label)]
    return core.Item(
        id="", type="A", category=rel.key, relation=rel.key, entity=entity,
        prompt=PROMPT.format(role=rel.role, entity=entity), prefill=core.ANSWER_STEM,
        source="wikidata", hop1_prompt=HOP1.format(role=rel.role, entity=entity),
        hop2_template=HOP2, bridge_true=creator.label, bridge_aliases=bridge_aliases,
        answer_true=country.label, answer_aliases=country_aliases(country),
        meta={"work_qid": work.qid, "creator_qid": creator.qid, "country_qid": country.qid,
              "year": val.year, "sitelinks": work.sitelinks, "work_label": work.label,
              "noun": noun, "creator_sitelinks": creator.sitelinks,
              "country_dissolved": country.dissolved},
    )


def candidates(pool: dict[str, dict[str, Work]], seed: int = SEED
               ) -> tuple[dict[str, list[Work]], Counter[str]]:
    """Per relation, the works that pass the label-only filters, in a seeded shuffled order.
    Labels shared by two or more works anywhere in the pool are dropped (ambiguous)."""
    drops: Counter[str] = Counter()
    label_n = Counter(_key(w.label) for ws in pool.values() for w in ws.values())
    out: dict[str, list[Work]] = {}
    for rel in sorted(pool):
        keep = []
        for q in sorted(pool[rel], key=lambda x: int(x[1:])):
            w = pool[rel][q]
            why = label_reject(w.label)
            if why is None and label_n[_key(w.label)] > 1:
                why = "duplicate_label"
            if why is None and len(w.creators) != 1:
                why = "creators!=1"
            if why:
                drops[f"{rel}:{why}"] += 1
            else:
                keep.append(w)
        random.Random(f"{seed}:{rel}").shuffle(keep)
        out[rel] = keep
    return out, drops


Lookup = Callable[[str, list[Work]], tuple[dict[str, Validation], dict[str, Creator],
                                           dict[str, Country]]]


def stream(rel: Relation, works: Sequence[Work], lookup: Lookup, drops: Counter[str],
           chunk: int = 300) -> Iterator[core.Item]:
    """Yield accepted items in candidate order, at most PER_CREATOR per creator. ``lookup``
    fetches validation, creator and country data for a chunk of works (network or canned)."""
    per_creator: Counter[str] = Counter()
    for i in range(0, len(works), chunk):
        part = list(works[i:i + chunk])
        vals, creators, countries = lookup(rel.key, part)
        for w in part:
            val = vals.get(w.qid)
            cr = creators.get(val.creators[0]) if val and len(val.creators) == 1 else None
            why = accept(w, val, cr, countries)
            if why is None and per_creator[cr.qid] >= PER_CREATOR:  # type: ignore[union-attr]
                why = "creator_cap"
            if why:
                drops[f"{rel.key}:{why}"] += 1
                continue
            assert val is not None and cr is not None
            per_creator[cr.qid] += 1
            yield make_item(rel, w, val, cr, countries[cr.countries[0]])


def build_items(cands: dict[str, list[Work]], lookup: Lookup, n: int, drops: Counter[str],
                seed: int = SEED) -> list[core.Item]:
    """n items: n // len(relations) from each relation, any shortfall filled round-robin from
    the others; then a seeded shuffle and ids A-0001..."""
    rels = [r for r in RELATIONS if r in cands]
    gens = {r: stream(RELATIONS[r], cands[r], lookup, drops) for r in rels}
    got: dict[str, list[core.Item]] = {r: [] for r in rels}
    quota = n // max(len(rels), 1)
    for r in rels:
        for it in gens[r]:
            got[r].append(it)
            if len(got[r]) >= quota:
                break
    alive = [r for r in rels if len(got[r]) >= quota]
    while sum(map(len, got.values())) < n and alive:
        for r in list(alive):
            if sum(map(len, got.values())) >= n:
                break
            nxt = next(gens[r], None)
            if nxt is None:
                alive.remove(r)
            else:
                got[r].append(nxt)
    items = sorted((it for r in rels for it in got[r]),
                   key=lambda it: (it.relation, int(it.meta["work_qid"][1:])))
    random.Random(seed).shuffle(items)
    width = max(4, len(str(len(items))))
    for i, it in enumerate(items, 1):
        it.id = f"A-{i:0{width}d}"
    return items


# --------------------------------------------------------------------------- network + cache


class Sparql:
    """Cached SPARQL client. A cache file holds the query, its retrieval UTC time and the
    response; offline mode raises on a cache miss."""

    def __init__(self, raw: Path = RAW, offline: bool = False, pause: float = 1.0) -> None:
        self.raw, self.offline, self.pause = raw, offline, pause
        self.log: list[dict[str, Any]] = []
        raw.mkdir(parents=True, exist_ok=True)

    def path(self, tag: str, query: str) -> Path:
        h = hashlib.sha256(query.encode()).hexdigest()[:16]
        return self.raw / f"{tag}_{h}.json.gz"

    def __call__(self, tag: str, query: str) -> list[dict[str, Any]]:
        p = self.path(tag, query)
        if p.exists():
            rec = json.loads(gzip.decompress(p.read_bytes()))
        elif self.offline:
            raise FileNotFoundError(f"offline and not cached: {p.name}")
        else:
            rec = {"query": query, "retrieved_utc": None, "response": self._fetch(query)}
            rec["retrieved_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            p.write_bytes(gzip.compress(json.dumps(rec, ensure_ascii=False).encode(), mtime=0))
            time.sleep(self.pause)
        b = rec["response"]["results"]["bindings"]
        self.log.append({"tag": tag, "file": p.name, "retrieved_utc": rec["retrieved_utc"],
                         "rows": len(b)})
        return b

    def _fetch(self, query: str, tries: int = 6) -> dict[str, Any]:
        import requests

        hdr = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
        wait = 5.0
        for k in range(tries):
            try:
                r = requests.post(ENDPOINT, data={"query": query}, headers=hdr, timeout=90)
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("Retry-After", "")
                    delay = float(ra) if ra.isdigit() else wait
                    print(f"  SPARQL {r.status_code}; retry in {delay:.0f}s", file=sys.stderr)
                    time.sleep(delay)
                    wait *= 2
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:  # ValueError: truncated JSON
                if k == tries - 1:
                    raise
                print(f"  SPARQL error {type(e).__name__}; retry in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
                wait *= 2
        raise RuntimeError("SPARQL: retries exhausted")


def fetch_pool(sq: Sparql) -> dict[str, dict[str, Work]]:
    pool: dict[str, dict[str, Work]] = {}
    for rel in RELATIONS.values():
        works: dict[str, Work] = {}
        for win in rel.windows:
            tag = f"pool_{rel.key}_{win[0]}_{win[1]}"
            for q, w in parse_pool(sq(tag, pool_query(rel, win)), rel.key).items():
                if q in works:
                    works[q].creators |= w.creators
                else:
                    works[q] = w
        pool[rel.key] = works
        print(f"pool {rel.key}: {len(works)} works", file=sys.stderr)
    return pool


def make_lookup(sq: Sparql) -> Lookup:
    creators: dict[str, Creator] = {}
    countries: dict[str, Country] = {}

    def batched(tag: str, qids: list[str], qfn: Callable[[Sequence[str]], str]
                ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i in range(0, len(qids), BATCH):
            rows += sq(tag, qfn(qids[i:i + BATCH]))
        return rows

    def lookup(relkey: str, works: list[Work]):
        rel = RELATIONS[relkey]
        vals = parse_validation(batched(f"val_{relkey}", [w.qid for w in works],
                                        lambda q: validate_query(rel, q)))
        need = sorted({v.creators[0] for v in vals.values() if len(v.creators) == 1}
                      - creators.keys(), key=lambda x: int(x[1:]))
        creators.update(parse_creators(batched("creators", need, creator_query)))
        cneed = sorted({c.countries[0] for c in creators.values() if len(c.countries) == 1}
                       - countries.keys(), key=lambda x: int(x[1:]))
        countries.update(parse_countries(batched("countries", cneed, country_query)))
        return vals, creators, countries

    return lookup


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--offline", action="store_true", help="use cached responses only")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)

    sq = Sparql(offline=a.offline)
    pool = fetch_pool(sq)
    cands, drops = candidates(pool, a.seed)
    items = build_items(cands, make_lookup(sq), a.n, drops, a.seed)
    core.write_jsonl(a.out, items)

    per_rel = Counter(it.relation for it in items)
    times = sorted(x["retrieved_utc"] for x in sq.log if x["retrieved_utc"])
    meta = {
        "exp": core.EXP, "builder": "p03.items_a", "seed": a.seed, "n_requested": a.n,
        "n_items": len(items), "per_relation": dict(sorted(per_rel.items())),
        "pool_works": {r: len(ws) for r, ws in pool.items()},
        "candidates_after_label_filters": {r: len(ws) for r, ws in cands.items()},
        "drops": dict(sorted(drops.items())),
        "countries": dict(Counter(it.answer_true for it in items).most_common()),
        "nouns": dict(Counter(f"{it.relation}:{it.meta['noun']}" for it in items)),
        "distinct_creators": len({it.meta["creator_qid"] for it in items}),
        "historical_country_items": dict(Counter(
            it.relation for it in items if it.meta["country_dissolved"] is not None)),
        "endpoint": ENDPOINT, "user_agent": USER_AGENT,
        "retrieved_utc": {"first": times[0] if times else None,
                          "last": times[-1] if times else None},
        "sitelinks_range": list(SITELINKS), "per_creator_max": PER_CREATOR,
        "pool_limit": POOL_LIMIT, "pool_limit_no_window": POOL_LIMIT_ALL,
        "templates": {"prompt": PROMPT, "hop1": HOP1, "hop2": HOP2},
        "pool_queries": {f"{r.key}_{w[0]}_{w[1]}": pool_query(r, w)
                         for r in RELATIONS.values() for w in r.windows},
        "lookup_query_templates": {
            "validate(film_director)": validate_query(RELATIONS["film_director"], ["Q1"]),
            "creator": creator_query(["Q1"]), "country": country_query(["Q1"]),
        },
        "raw_files": sorted({x["file"] for x in sq.log}),
        "sha256_items": hashlib.sha256(a.out.read_bytes()).hexdigest(),
    }
    META.write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"n": len(items), "per_relation": meta["per_relation"]}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
