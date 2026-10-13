"""P35 parts (B) and (M): deterministic per-bullet regex scoring of existing Oracle Lens readouts
(plan "Measure"; Predictions 5 and 6). No LLM, no API, no GPU.

    PYTHONPATH=probes/P35-olens-continuation-rescore:probes/P11-bridge-read-vs-reanswer:\\
probes/P03-text-proof-interp-items uv run python -m p35.regex_scores [--show 6]

Writes ``results/regex_b.json``, ``results/regex_m.json`` and the per-bullet rows
``results/regex_b_bullets.jsonl`` / ``results/regex_m_bullets.jsonl``, and prints a compact
table. Input sha256s are recorded in each JSON.

**Bullets.** A readout (P03/P11 ``text``; I1a ``samples`` joined by newlines) is split at lines
that start with ``"- "`` in column 0; other lines continue the current bullet; text before the
first bullet line is a bullet of its own; a readout with no ``"- "`` line is one bullet. Explicit
empty bullets are kept (they name nothing). The unit of analysis is the bullet.

**Matching.** All text is NFKD accent-stripped (which also maps full-width "：" "（" to ASCII)
with whitespace collapsed; offsets refer to that normalised text. A ``Form`` is one of:
- ``word``: P03's free-text rule (``p03.score.text_hit``): ``(?<!\\w)form(?:e?s)?(?!\\w)``, case
  folded unless the form is marked case-sensitive ("US", "Polish", "May");
- ``token``: a strict whole token, case-sensitive, alphanumeric boundaries (no plural suffix);
  digits also refuse a neighbouring ``[.,]digit`` ("5" does not hit "2.5" or "5,000");
- ``cjk``: a substring (CJK has no word boundaries), for Chinese country names ("美国").

(B) **Qwen's answer** is any answer among its 20 sampled answers (P03 sessions 1/2, ``kind ==
"main"``), mapped to a country group (``COUNTRIES``: country names, demonyms and Chinese names;
"English" belongs to the United Kingdom group, "Soviet" is its own group). A bullet names
Qwen's answer if it names any form of any of those groups. Decisions, documented and tested:
"American"/"America" do not hit after "South/Latin/Central/North/Native/Meso/Pan/African/Spanish"
("South American" is not a hit); "Korea(n)" not after "North"; "Mexico/Mexican" not after
"New"; "England" not after "New"; "Wales" not after "South"; a demonym is not a hit as a
language: after "in " ("answer in English") or before "language/translation/word(s)/text/
characters/version/script". **The bridge** (Qwen's wrong bridge) and **the true bridge** use
P11's cleaned alias matcher (``p11.items.names``). **The true answer** is the true country's
group (every ``y_nation_forms`` entry is checked to be covered). Items whose true-answer forms
share a form with Qwen's sampled-answer forms are flagged (``checks.true_answer_overlap``) and
left out of ``by_source_pos_no_true_overlap`` ("clean"); ``qwen_modal`` is the narrower flag for
Qwen's modal answer only. Wikidata country aliases outside a group's own forms (codes such as
"se", "DE", "SA", and rare names such as "Hellas") are not matched: codes would be false hits,
and the rare names have no hit in any bullet (listed in ``checks.true_nation_forms_uncovered``).

**Answer position** (bridge-naming bullets only; ``position_flags``). The bridge must come
before every nationality word in the bullet (any ``COUNTRIES`` form or ``OTHER_NATIONALITIES``),
so a gloss after the answer ("American (Oliver Stone ...)", "Spanish. Explanation: Goya ...") is
not answer position. The lead-in is the bullet up to the end of its first answer delimiter
(``:`` ``is`` ``would be`` ``是`` ``为`` ``=``) if that delimiter starts within the first ``LEADIN_MAX``
(8) tokens and ends before the bridge; otherwise it is empty. Tokens are Latin/digit runs or
single CJK characters. ``bridge_in_answer_position`` (loose): the bridge starts within
``SLOT_TOKENS`` (8) tokens after the lead-in. ``bridge_slot_first`` (strict, "is: **X**"):
nothing but punctuation/markup between the lead-in and the bridge.

(M) **Target** (the surface answer) and **intermediate** (any listed intermediate): digit strings
and strings of at most 2 characters use the ``token`` rule ("Fe", "C", "5"); digit targets
0-20 also hit their English number word ("five"); "May"/"March"/"August" are case-sensitive;
everything else is the ``word`` rule.

**Statistics.** Shares are bullet shares; item-level rates are over items (any bullet). Paired
differences use ``p03.core.paired_bootstrap`` (items resampled, 10,000, ``random.Random(0)``)
on per-item bullet means.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
P03_DIR = ROOT / "probes" / "P03-text-proof-interp-items"
P11_DIR = ROOT / "probes" / "P11-bridge-read-vs-reanswer"
for _d in (P11_DIR, P03_DIR):
    if str(_d) not in sys.path:
        sys.path.append(str(_d))

from p03 import core as C
from p03 import score as SC
from p11 import items as I

RESULTS = PROBE / "results"
P03_OLENS = P03_DIR / "results" / "read" / "olens_samples" / "main.jsonl"
P03_SAMPLES = (P03_DIR / "results" / "sample" / "samples.jsonl",
               P03_DIR / "results" / "sample2" / "samples.jsonl")
P11_OLENS = (P11_DIR / "results" / "olens" / "plain_s0.jsonl",
             P11_DIR / "results" / "olens" / "plain_s1.jsonl")
M_ITEMS = ROOT / "third_party" / "workspace-bench" / "evals" / "multihop" / "items.json"
M_READOUTS = {
    "olens": ROOT / "results" / "I1a" / "readouts" / "olens_rl600" / "multihop.jsonl",
    "text_monitor": ROOT / "results" / "I1a" / "readouts" / "text_monitor" / "multihop.jsonl",
}

N_BOOT = 10_000
BOOT_SEED = 0
SHOW_SEED = 0
LEADIN_MAX = 8
SLOT_TOKENS = 8
WORD_POS = (-3, -2)
DELIM_POS = (-1,)
M_LATE = 36  # Prediction 6: layers >= 36

# --------------------------------------------------------------------------- bullets


def split_bullets(text: str | None) -> list[str]:
    """Bullets of a readout (module docstring)."""
    text = text or ""
    if not text.strip():
        return []
    out: list[str] = []
    cur: str | None = None
    explicit = False
    for ln in text.split("\n"):
        if ln.startswith("- ") or ln == "-":
            if cur is not None and (explicit or cur.strip()):
                out.append(cur.strip())
            cur, explicit = ln[2:], True
        elif cur is None:
            cur, explicit = ln, False
        else:
            cur += "\n" + ln
    if cur is not None and (explicit or cur.strip()):
        out.append(cur.strip())
    return out


def readout_text(row: dict) -> str:
    if "text" in row:
        return row["text"] or ""
    return "\n".join(row.get("samples") or [])


# --------------------------------------------------------------------------- matching

CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_HAS_CJK = re.compile(f"[{CJK}]")
TOKEN = re.compile(rf"[{CJK}]|[^\W_{CJK}]+")


def norm_text(s: str) -> str:
    """NFKD accent strip (as ``p03.score.fold``, without lower-casing) and whitespace collapse."""
    return re.sub(r"\s+", " ", C.strip_accents(s or "")).strip()


def n_tokens(s: str) -> int:
    return len(TOKEN.findall(s))


@dataclass(frozen=True)
class Form:
    text: str
    case: bool = False  # case-sensitive
    kind: str = "word"  # "word" | "token" | "cjk"
    not_before: str | None = None  # regex that must not match the text just before the hit
    lang: bool = False  # a demonym that is also a language name


_LANG_BEFORE = re.compile(r"\bin\s*$", re.IGNORECASE)
_LANG_AFTER = re.compile(
    r"^\s*(?:language|languages|translation|words?|text|characters|version|script)\b", re.IGNORECASE)


@functools.cache
def _compile(f: Form) -> tuple[re.Pattern, re.Pattern | None]:
    esc = re.escape(f.text)
    if f.kind == "cjk":
        pat = esc
    elif f.kind == "token":
        pat = rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])"
        if f.text[:1].isdigit():
            pat = rf"(?<!\d[.,]){pat}(?![.,]\d)"
    else:
        # P03's optional plural, only after a letter ("U.S." must not eat the "S" of "U.S.S.R.")
        suffix = "(?:e?s)?" if f.text[-1:].isalpha() else ""
        pat = rf"(?<!\w){esc}{suffix}(?!\w)"
    nb = re.compile(rf"\b(?:{f.not_before})[\s-]*$", re.IGNORECASE) if f.not_before else None
    return re.compile(pat, 0 if f.case else re.IGNORECASE), nb


def form_starts(t: str, f: Form) -> list[int]:
    """Start offsets of the form's hits in the normalised text ``t``."""
    pat, nb = _compile(f)
    out = []
    for m in pat.finditer(t):
        before = t[: m.start()]
        if nb is not None and nb.search(before):
            continue
        if f.lang and (_LANG_BEFORE.search(before) or _LANG_AFTER.match(t[m.end():])):
            continue
        out.append(m.start())
    return out


def first_start(t: str, forms: Iterable[Form]) -> int | None:
    starts = [s for f in forms for s in form_starts(t, f)]
    return min(starts) if starts else None


def names_any(text: str, forms: Iterable[Form]) -> bool:
    return first_start(norm_text(text), forms) is not None


# --------------------------------------------------------------------------- countries (B)

# group -> (country names, demonyms, Chinese names). Covers every answer Qwen sampled on the 63
# items and every true country (a check in ``build_b_items`` fails loudly otherwise).
COUNTRIES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "United States": (("United States", "United States of America", "America", "USA",
                       "U.S.A.", "U.S.", "US"), ("American",), ("美国", "美國")),
    "Russia": (("Russia", "Russian Federation"), ("Russian",), ("俄罗斯", "俄羅斯", "俄国", "俄國")),
    "Soviet Union": (("Soviet Union", "USSR", "U.S.S.R."), ("Soviet",), ("苏联", "蘇聯")),
    "United Kingdom": (("United Kingdom", "UK", "U.K.", "Great Britain", "Britain", "England",
                        "Scotland", "Wales"),
                       ("British", "Briton", "English", "Scottish", "Welsh"),
                       ("英国", "英國", "英格兰", "英格蘭")),
    "Scotland": (("Scotland",), ("Scottish", "Scots", "Scotch"), ("苏格兰", "蘇格蘭")),
    "France": (("France", "French Republic"), ("French",), ("法国", "法國")),
    "Italy": (("Italy",), ("Italian",), ("意大利", "義大利")),
    "Mexico": (("Mexico",), ("Mexican",), ("墨西哥",)),
    "Netherlands": (("Netherlands", "Kingdom of the Netherlands", "Holland"),
                    ("Dutch", "Netherlandish"), ("荷兰", "荷蘭")),
    "Germany": (("Germany", "West Germany", "East Germany"), ("German",), ("德国", "德國")),
    "Sweden": (("Sweden",), ("Swedish", "Swede"), ("瑞典",)),
    "Spain": (("Spain",), ("Spanish", "Spaniard"), ("西班牙",)),
    "Poland": (("Poland",), ("Polish",), ("波兰", "波蘭")),
    "Hungary": (("Hungary",), ("Hungarian",), ("匈牙利",)),
    "Greece": (("Greece",), ("Greek",), ("希腊", "希臘")),
    "Australia": (("Australia",), ("Australian",), ("澳大利亚", "澳大利亞", "澳洲")),
    "Denmark": (("Denmark", "Kingdom of Denmark"), ("Danish", "Dane"), ("丹麦", "丹麥")),
    "Norway": (("Norway",), ("Norwegian",), ("挪威",)),
    "Canada": (("Canada",), ("Canadian",), ("加拿大",)),
    "Colombia": (("Colombia",), ("Colombian",), ("哥伦比亚", "哥倫比亞")),
    "Argentina": (("Argentina",), ("Argentine", "Argentinian", "Argentinean"), ("阿根廷",)),
    "China": (("China", "People's Republic of China", "PRC"), ("Chinese",), ("中国", "中國")),
    "Estonia": (("Estonia",), ("Estonian",), ("爱沙尼亚", "愛沙尼亞")),
    "Ukraine": (("Ukraine",), ("Ukrainian",), ("乌克兰", "烏克蘭")),
    "Kenya": (("Kenya",), ("Kenyan",), ("肯尼亚", "肯亞")),
    "Venezuela": (("Venezuela",), ("Venezuelan",), ("委内瑞拉", "委內瑞拉")),
    "Austria": (("Austria",), ("Austrian",), ("奥地利", "奧地利")),
    "South Africa": (("South Africa",), ("South African",), ("南非",)),
    "Belgium": (("Belgium",), ("Belgian",), ("比利时", "比利時")),
    "Switzerland": (("Switzerland",), ("Swiss",), ("瑞士",)),
    "South Korea": (("South Korea", "Korea", "Republic of Korea"), ("Korean", "South Korean"),
                    ("韩国", "韓國")),
    "Romania": (("Romania",), ("Romanian",), ("罗马尼亚", "羅馬尼亞")),
    "Philippines": (("Philippines",), ("Filipino", "Philippine"), ("菲律宾", "菲律賓")),
    "Cuba": (("Cuba",), ("Cuban",), ("古巴",)),
    "Chile": (("Chile",), ("Chilean",), ("智利",)),
}
CASE_SENSITIVE = {"US", "Polish"}
NOT_BEFORE = {
    "america": "south|latin|central|north|native|meso|pan|african|spanish",
    "american": "south|latin|central|north|native|meso|pan|african|spanish",
    "korea": "north",
    "korean": "north",
    "mexico": "new",
    "mexican": "new",
    "england": "new",
    "wales": "south",
}


def _form(text: str, lang: bool = False) -> Form:
    if _HAS_CJK.search(text):
        return Form(text, kind="cjk")
    return Form(text, case=text in CASE_SENSITIVE, not_before=NOT_BEFORE.get(text.lower()),
                lang=lang)


@functools.cache
def group_forms(group: str) -> tuple[Form, ...]:
    names, dems, cjk = COUNTRIES[group]
    out: list[Form] = []
    for n in names:
        out.append(_form(n))
        if " " in n:
            out.append(_form(n.replace(" ", "-")))
    for d in dems:
        out.append(_form(d, lang=True))
        if " " in d:
            out.append(_form(d.replace(" ", "-"), lang=True))
    out += [_form(c) for c in cjk]
    return tuple(out)


def answer_groups(answer: str) -> set[str]:
    """Country groups an answer names (whole-form hits on its ``first_answer`` part)."""
    t = norm_text(C.first_answer(answer))
    return {g for g in COUNTRIES if first_start(t, group_forms(g)) is not None}


def country_group(label: str, aliases: Iterable[str] = ()) -> str:
    """The group of a Wikidata country label (exact name match, case folded)."""
    want = {x.lower() for x in (label, *aliases)}
    for g in COUNTRIES:  # a group's own name first ("Scotland" is also a UK name)
        if g.lower() in want:
            return g
    for g, (names, _, _) in COUNTRIES.items():
        if want & {n.lower() for n in (g, *names)}:
            return g
    raise KeyError(f"no country group for {label!r}")


# --------------------------------------------------------------------------- bridge forms


def person_forms(target: str, aliases: Iterable[str]) -> tuple[Form, ...]:
    """P11's cleaned person phrase forms (``p11.items.names``) as ``word`` forms (P03's regex, so
    CJK aliases keep P03's word boundaries); used for the bridge's position only."""
    phr = SC.forms(target, I.clean_aliases([target, *aliases]), True)[1]
    return tuple(Form(p) for p in sorted(phr))


_DELIM = re.compile(r":|\bis\b|\bwould be\b|是|为|為|=", re.IGNORECASE)


# Nationality words outside ``COUNTRIES`` that lens bullets use as answers; used only to find
# where the answer slot's first answer-like word is (``position_flags``).
OTHER_NATIONALITIES = (
    "Japan", "Japanese", "India", "Indian", "Ireland", "Irish", "Flemish", "Flanders", "Portugal",
    "Portuguese", "Brazil", "Brazilian", "Finland", "Finnish", "Czech", "Czechoslovak",
    "Israel", "Israeli", "Turkey", "Turkish", "Egypt", "Egyptian", "Iran", "Iranian", "Persian",
    "New Zealand", "Icelandic", "Iceland", "Serbian", "Croatian", "Bulgarian", "Prussian",
    "Bohemian", "Latvian", "Lithuanian", "Slovak", "Slovenian", "Peruvian",
    "Nigerian", "日本", "印度", "爱尔兰", "愛爾蘭", "葡萄牙", "巴西",
)


@functools.cache
def nationality_forms() -> tuple[Form, ...]:
    other = tuple(_form(x, lang=not _HAS_CJK.search(x)) for x in OTHER_NATIONALITIES)
    return tuple(f for g in COUNTRIES for f in group_forms(g)) + other


def answer_slot(t: str, stop: int) -> int:
    """Offset where the answer slot starts: the end of the first delimiter that starts within
    LEADIN_MAX tokens and ends at or before ``stop`` (else 0)."""
    for m in _DELIM.finditer(t):
        if m.end() > stop or n_tokens(t[: m.start()]) > LEADIN_MAX:
            break
        return m.end()
    return 0


def position_flags(t: str, bridge_forms: Iterable[Form]) -> tuple[bool, bool]:
    """(loose, strict) answer-position flags of the bridge in normalised bullet ``t``.

    The bridge must come before every nationality word of the bullet (so "American (Oliver
    Stone ...)" and "Spanish. Explanation: Goya ..." are not answer position); the lead-in may
    not run past the bridge or a nationality word. Loose: the bridge starts within SLOT_TOKENS
    tokens after the lead-in; strict: nothing but punctuation/markup in between."""
    first = first_start(t, bridge_forms)
    if first is None:
        return False, False
    nat = first_start(t, nationality_forms())
    if nat is not None and nat < first:
        return False, False
    gap = n_tokens(t[answer_slot(t, first): first])
    return gap < SLOT_TOKENS, gap == 0


# --------------------------------------------------------------------------- (B) items


@dataclass
class BItem:
    id: str
    qwen_answer: str
    bridge: str
    bridge_aliases: list[str]
    y: str
    y_aliases: list[str]
    true_group: str
    qwen_groups: list[str]
    bridge_forms: tuple[Form, ...]
    qwen_forms: tuple[Form, ...]
    true_forms: tuple[Form, ...]
    modal_forms: tuple[Form, ...] = ()
    true_overlap: bool = False  # the true answer is among (or shares a form with) Qwen's answers


def load_qwen_samples(ids: set[str], paths: Sequence[Path] = P03_SAMPLES) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in paths:
        for r in C.read_jsonl(p):
            if r["kind"] == "main" and r["id"] in ids:
                if r["id"] in out:
                    raise ValueError(f"{r['id']}: main samples in more than one file")
                out[r["id"]] = list(r["samples"])
    return out


def build_b_items(items: Sequence[I.Item], samples: dict[str, list[str]]) -> tuple[dict, dict]:
    """(id -> BItem, checks)."""
    out: dict[str, BItem] = {}
    unmapped: list[dict] = []
    bridge_in_samples: list[dict] = []
    overlap: list[dict] = []
    nation_gaps: list[dict] = []
    y_bridge: list[dict] = []
    n_samples: Counter = Counter()
    for it in items:
        ss = samples.get(it.id)
        if ss is None:
            raise ValueError(f"{it.id}: no main samples")
        n_samples[len(ss)] += 1
        groups: set[str] = set()
        for s in ss:
            g = answer_groups(s)
            if not g and C.norm(s):
                unmapped.append({"id": it.id, "answer": s})
            groups |= g
        if not answer_groups(it.qwen_answer) <= groups:
            raise ValueError(f"{it.id}: modal answer {it.qwen_answer!r} not among sample groups")
        k = sum(I.names(s, it.bridge, it.bridge_aliases) for s in ss)
        if k:
            bridge_in_samples.append({"id": it.id, "bridge": it.bridge, "n_samples": k})
        tg = country_group(it.y_country, it.y_country_aliases)
        tforms = group_forms(tg)
        gaps = [f for f in it.y_nation_forms if not names_any(f, tforms)]
        if gaps:
            nation_gaps.append({"id": it.id, "country": it.y_country, "uncovered": gaps})
        qforms = tuple(f for g in sorted(groups) for f in group_forms(g))
        shared = sorted({f.text.lower() for f in tforms} & {f.text.lower() for f in qforms})
        if shared or tg in groups:
            overlap.append({"id": it.id, "true_country": tg, "qwen_groups": sorted(groups),
                            "shared_forms": shared})
        bforms = person_forms(it.bridge, it.bridge_aliases)
        yforms = person_forms(it.y, it.y_aliases)
        sh = sorted({f.text for f in bforms} & {f.text for f in yforms})
        if sh:
            y_bridge.append({"id": it.id, "shared": sh})
        mforms = tuple(f for g in sorted(answer_groups(it.qwen_answer)) for f in group_forms(g))
        out[it.id] = BItem(it.id, it.qwen_answer, it.bridge, list(it.bridge_aliases), it.y,
                           list(it.y_aliases), tg, sorted(groups), bforms, qforms, tforms,
                           mforms, bool(shared or tg in groups))
    checks = {
        "n_items": len(out),
        "samples_per_item": dict(n_samples),
        "unmapped_sampled_answers": unmapped,
        "bridge_in_qwen_samples": bridge_in_samples,
        "true_answer_overlap": overlap,
        "true_nation_forms_uncovered": nation_gaps,
        "y_bridge_form_overlap": y_bridge,
    }
    return out, checks


def score_b_bullet(text: str, it: BItem) -> dict:
    t = norm_text(text)
    bridge = I.names(text, it.bridge, it.bridge_aliases)
    first = first_start(t, it.bridge_forms)
    loose, strict = position_flags(t, it.bridge_forms) if bridge else (False, False)
    return {
        "qwen_answer": first_start(t, it.qwen_forms) is not None,
        "qwen_modal": first_start(t, it.modal_forms) is not None,
        "bridge": bridge,
        "bridge_in_answer_position": loose,
        "bridge_slot_first": strict,
        "true_bridge": I.names(text, it.y, it.y_aliases),
        "true_answer": first_start(t, it.true_forms) is not None,
        "_span_mismatch": bridge != (first is not None),
    }


B_FLAGS = ("qwen_answer", "qwen_modal", "bridge", "bridge_in_answer_position", "bridge_slot_first",
           "true_bridge", "true_answer")


def b_bullets(readouts: Iterable[tuple[str, dict]], bitems: dict[str, BItem]) -> list[dict]:
    """Per-bullet rows for (source, readout row) pairs whose id is a (B) item."""
    out = []
    for source, r in readouts:
        it = bitems.get(r["id"])
        if it is None:
            continue
        for k, b in enumerate(split_bullets(readout_text(r))):
            out.append({"source": source, "draw": r.get("draw", ""), "id": r["id"],
                        "layer": r["layer"], "pos": r["pos"], "bullet": k, "text": b,
                        "item_true_overlap": it.true_overlap, **score_b_bullet(b, it)})
    return out


# --------------------------------------------------------------------------- aggregation


def shares(rows: Sequence[dict], flags: Sequence[str]) -> dict:
    n = len(rows)
    return {"n": n, **{f: (sum(bool(r[f]) for r in rows) / n if n else None) for f in flags}}


def group(rows: Iterable[dict], key: Callable[[dict], str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return dict(sorted(out.items(), key=lambda kv: _sort_key(kv[0])))


def _sort_key(k: str) -> tuple:
    return tuple((0, int(x), "") if re.fullmatch(r"-?\d+", x) else (1, 0, x) for x in k.split("|"))


def pos_class(pos: int) -> str:
    return "delimiter" if pos in DELIM_POS else "word" if pos in WORD_POS else str(pos)


def item_means(rows: Iterable[dict], flag: str) -> dict[str, float]:
    acc: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        acc[r["id"]].append(bool(r[flag]))
    return {i: sum(v) / len(v) for i, v in acc.items()}


def paired(a: dict[str, float], b: dict[str, float]) -> dict:
    ids = sorted(set(a) & set(b))
    mean, lo, hi = C.paired_bootstrap([a[i] for i in ids], [b[i] for i in ids],
                                      n_boot=N_BOOT, seed=BOOT_SEED)
    return {"diff": mean, "lo": lo, "hi": hi, "n_items": len(ids),
            "a": sum(a[i] for i in ids) / len(ids) if ids else None,
            "b": sum(b[i] for i in ids) / len(ids) if ids else None}


def aggregate_b(rows: Sequence[dict]) -> dict:
    out: dict = {}
    out["by_source_pos_layer"] = {k: shares(v, B_FLAGS) for k, v in group(
        rows, lambda r: f"{r['source']}|{r['pos']}|{r['layer']}").items()}
    out["by_source_pos"] = {k: shares(v, B_FLAGS) for k, v in group(
        rows, lambda r: f"{r['source']}|{r['pos']}").items()}
    out["by_source_posclass"] = {k: shares(v, B_FLAGS) for k, v in group(
        rows, lambda r: f"{r['source']}|{pos_class(r['pos'])}").items()}
    clean = [r for r in rows if not r.get("item_true_overlap")]
    out["by_source_pos_no_true_overlap"] = {k: shares(v, B_FLAGS) for k, v in group(
        clean, lambda r: f"{r['source']}|{r['pos']}").items()}
    late = [r for r in rows if r["source"] == "p03" and 40 <= r["layer"] <= 60]
    out["p03_L40_60_by_pos"] = {k: shares(v, B_FLAGS) for k, v in group(
        late, lambda r: str(r["pos"])).items()}
    # among bridge-naming bullets
    ap = {}
    for k, v in group([r for r in rows if r["bridge"]],
                      lambda r: f"{r['source']}|{r['pos']}").items():
        ap[k] = {"n_bridge_bullets": len(v),
                 "loose": sum(r["bridge_in_answer_position"] for r in v) / len(v),
                 "strict": sum(r["bridge_slot_first"] for r in v) / len(v)}
    for src in sorted({r["source"] for r in rows}):
        v = [r for r in rows if r["bridge"] and r["source"] == src]
        if v:
            ap[f"{src}|all"] = {"n_bridge_bullets": len(v),
                                "loose": sum(r["bridge_in_answer_position"] for r in v) / len(v),
                                "strict": sum(r["bridge_slot_first"] for r in v) / len(v)}
    out["answer_position_given_bridge"] = ap
    out["item_level"] = item_level_b(rows)
    boot = {}
    for src, keep in (("p03", {"p03"}), ("p11", {"p11"}), ("both", {"p03", "p11"})):
        rs = [r for r in rows if r["source"] in keep]
        at = {p: item_means([r for r in rs if r["pos"] == p], "qwen_answer") for p in (-3, -2, -1)}
        word = item_means([r for r in rs if r["pos"] in WORD_POS], "qwen_answer")
        boot[src] = {"qwen_answer_-1_minus_-3": paired(at[-1], at[-3]),
                     "qwen_answer_delim_minus_word": paired(at[-1], word)}
    out["bootstrap"] = boot
    return out


def item_level_b(rows: Sequence[dict]) -> dict:
    """Cells (source, draw, id, layer, pos) where a bullet names the bridge and none names Qwen's
    answer; per source x position and per source."""
    cells = group(rows, lambda r: f"{r['source']}|{r['draw']}|{r['id']}|{r['layer']}|{r['pos']}")
    flags = []
    for v in cells.values():
        r0 = v[0]
        flags.append({"source": r0["source"], "pos": r0["pos"], "id": r0["id"],
                      "bridge": any(r["bridge"] for r in v),
                      "qwen": any(r["qwen_answer"] for r in v)})
    out = {}
    for key, sel in (("pos", lambda f: f"{f['source']}|{f['pos']}"),
                     ("all", lambda f: f"{f['source']}|all")):
        for k, v in group(flags, sel).items():
            only = [f for f in v if f["bridge"] and not f["qwen"]]
            ids = {f["id"] for f in v}
            out[k] = {"n_cells": len(v), "n_items": len(ids),
                      "cells_bridge": sum(f["bridge"] for f in v),
                      "cells_bridge_no_answer": len(only),
                      "items_bridge_any": len({f["id"] for f in v if f["bridge"]}),
                      "items_answer_any": len({f["id"] for f in v if f["qwen"]}),
                      "items_bridge_no_answer_cell": len({f["id"] for f in only}),
                      "ids_bridge_no_answer_cell": sorted({f["id"] for f in only})}
    return out


# --------------------------------------------------------------------------- (M)

NUM_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]
CASE_SENSITIVE_WORDS = {"may", "march", "august"}


def term_forms(s: str) -> tuple[Form, ...]:
    """Forms of a multihop target or intermediate (module docstring)."""
    s = s.strip()
    if _HAS_CJK.search(s):
        return (Form(s, kind="cjk"),)
    if s.isdigit():
        out = [Form(s, case=True, kind="token")]
        if int(s) < len(NUM_WORDS):
            out.append(Form(NUM_WORDS[int(s)]))
        return tuple(out)
    if len(s) <= 2:
        return (Form(s, case=True, kind="token"),)
    return (Form(s, case=s.lower() in CASE_SENSITIVE_WORDS),)


@dataclass
class MItem:
    id: str
    prompt: str
    target: str
    intermediates: list[str]
    target_forms: tuple[Form, ...]
    inter_forms: tuple[Form, ...]


def build_m_items(raw: Sequence[dict]) -> tuple[dict[str, MItem], dict]:
    out: dict[str, MItem] = {}
    echo_t, echo_i, overlap = [], [], []
    for r in raw:
        tf = term_forms(r["target"])
        inf = tuple(f for x in r["intermediates"] for f in term_forms(x))
        it = MItem(r["name"], r["prompt"], r["target"], list(r["intermediates"]), tf, inf)
        out[it.id] = it
        if names_any(it.prompt, tf):
            echo_t.append(it.id)
        if names_any(it.prompt, inf):
            echo_i.append(it.id)
        if any(names_any(f.text, inf) for f in tf) or any(names_any(f.text, tf) for f in inf):
            overlap.append(it.id)
    return out, {"n_items": len(out), "prompt_names_target": echo_t,
                 "prompt_names_intermediate": echo_i, "target_intermediate_overlap": overlap}


def m_bullets(reader: str, rows: Iterable[dict], items: dict[str, MItem]) -> list[dict]:
    out = []
    for r in rows:
        it = items[r["id"]]
        for k, b in enumerate(split_bullets(readout_text(r))):
            t = norm_text(b)
            tg = first_start(t, it.target_forms) is not None
            im = first_start(t, it.inter_forms) is not None
            cat = "both" if tg and im else "target_only" if tg else "intermediate_only" if im \
                else "neither"
            out.append({"reader": reader, "id": r["id"], "layer": r["layer"], "pos": r["pos"],
                        "bullet": k, "text": b, "target": tg, "intermediate": im, "cat": cat})
    return out


M_FLAGS = ("target", "intermediate")
M_CATS = ("target_only", "intermediate_only", "both", "neither")


def m_shares(rows: Sequence[dict]) -> dict:
    n = len(rows)
    d = shares(rows, M_FLAGS)
    d.update({c: (sum(r["cat"] == c for r in rows) / n if n else None) for c in M_CATS})
    per: dict[str, dict[str, bool]] = defaultdict(lambda: {"t": False, "i": False})
    for r in rows:
        per[r["id"]]["t"] |= r["target"]
        per[r["id"]]["i"] |= r["intermediate"]
    ni = len(per)
    d["items"] = ni
    d["item_target"] = sum(v["t"] for v in per.values()) / ni if ni else None
    d["item_intermediate"] = sum(v["i"] for v in per.values()) / ni if ni else None
    d["item_intermediate_no_target"] = (
        sum(v["i"] and not v["t"] for v in per.values()) / ni if ni else None)
    return d


def aggregate_m(rows: Sequence[dict]) -> dict:
    out: dict = {"by_reader_layer": {k: m_shares(v) for k, v in group(
        rows, lambda r: f"{r['reader']}|{r['layer']}").items()}}
    late = [r for r in rows if r["layer"] >= M_LATE]
    out["late_by_reader"] = {k: m_shares(v) for k, v in group(late, lambda r: r["reader"]).items()}
    out["all_by_reader"] = {k: m_shares(v) for k, v in group(rows, lambda r: r["reader"]).items()}
    ol = [r for r in late if r["reader"] == "olens"]
    tm = [r for r in rows if r["reader"] == "text_monitor"]
    out["bootstrap"] = {
        "olens_late_target_minus_intermediate": paired(item_means(ol, "target"),
                                                       item_means(ol, "intermediate")),
        "text_monitor_minus_olens_late_intermediate": paired(item_means(tm, "intermediate"),
                                                             item_means(ol, "intermediate")),
    }
    return out


def text_monitor_layer_invariant(rows: Sequence[dict]) -> dict:
    by: dict[str, set] = defaultdict(set)
    for r in rows:
        by[r["id"]].add(readout_text(r))
    return {"n_items": len(by), "identical_across_layers": sum(len(v) == 1 for v in by.values())}


# --------------------------------------------------------------------------- I/O and CLI


def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(ROOT))


def _public(rows: Iterable[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def run_b(out_dir: Path) -> tuple[dict, list[dict]]:
    items = I.load()
    ids = {i.id for i in items}
    samples = load_qwen_samples(ids)
    bitems, checks = build_b_items(items, samples)
    readouts: list[tuple[str, dict]] = [("p03", r) for r in C.read_jsonl(P03_OLENS)]
    for p in P11_OLENS:
        readouts += [("p11", r) for r in C.read_jsonl(p)]
    rows = b_bullets(readouts, bitems)
    n_readouts = Counter(s for s, r in readouts if r["id"] in ids)
    bpr = Counter()
    for s, r in readouts:
        if r["id"] in ids:
            bpr[f"{s}|{len(split_bullets(readout_text(r)))}"] += 1
    checks.update({
        "n_readouts": dict(n_readouts),
        "bullets_per_readout": dict(sorted(bpr.items())),
        "n_bullets": dict(Counter(r["source"] for r in rows)),
        "bridge_names_vs_span_mismatch": sum(r["_span_mismatch"] for r in rows),
    })
    inputs = [P11_DIR / "data" / "items.jsonl", *P03_SAMPLES, P03_OLENS, *P11_OLENS]
    res = {
        "probe": "P35", "part": "B",
        "inputs": {_rel(p): sha256(p) for p in inputs},
        "params": {"n_boot": N_BOOT, "boot_seed": BOOT_SEED, "leadin_max_tokens": LEADIN_MAX,
                   "slot_tokens": SLOT_TOKENS, "word_positions": list(WORD_POS),
                   "delimiter_positions": list(DELIM_POS)},
        "checks": checks,
        **aggregate_b(rows),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "regex_b.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    C.write_jsonl(out_dir / "regex_b_bullets.jsonl", _public(rows))
    return res, rows


def run_m(out_dir: Path) -> tuple[dict, list[dict]]:
    raw = json.loads(M_ITEMS.read_text(encoding="utf-8"))["items"]
    items, checks = build_m_items(raw)
    rows: list[dict] = []
    for reader, p in M_READOUTS.items():
        rr = C.read_jsonl(p)
        rows += m_bullets(reader, rr, items)
        if reader == "text_monitor":
            checks["text_monitor_layer_invariance"] = text_monitor_layer_invariant(rr)
    checks["n_bullets"] = dict(Counter(r["reader"] for r in rows))
    res = {
        "probe": "P35", "part": "M",
        "inputs": {_rel(p): sha256(p) for p in (M_ITEMS, *M_READOUTS.values())},
        "params": {"n_boot": N_BOOT, "boot_seed": BOOT_SEED, "late_layer_min": M_LATE},
        "checks": checks,
        **aggregate_m(rows),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "regex_m.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    C.write_jsonl(out_dir / "regex_m_bullets.jsonl", rows)
    return res, rows


def _f(x) -> str:
    return "  -  " if x is None else f"{x:5.3f}"


def table_b(res: dict) -> str:
    lines = ["(B) wrong-bridge items: bullet shares by source x position (layers pooled)",
             f"{'cell':<16}{'n':>6}  qwenA qModl bridg ansPs slot1 trueB trueA"]
    for k, d in {**res["by_source_pos"], **res["by_source_posclass"]}.items():
        lines.append(f"{k:<16}{d['n']:>6}  " + " ".join(_f(d[f]) for f in B_FLAGS))
    for k, d in res["p03_L40_60_by_pos"].items():
        lines.append(f"{'p03L40-60|' + k:<16}{d['n']:>6}  " + " ".join(_f(d[f]) for f in B_FLAGS))
    for k, d in res["by_source_pos_no_true_overlap"].items():
        lines.append(f"{'clean|' + k:<16}{d['n']:>6}  " + " ".join(_f(d[f]) for f in B_FLAGS))
    lines.append("qwen_answer / bridge share by layer (columns: pos -3 -2 -1)")
    lay = res["by_source_pos_layer"]
    for src in ("p03", "p11"):
        layers = sorted({int(k.split("|")[2]) for k in lay if k.startswith(src + "|")})
        for L in layers:
            q = [lay.get(f"{src}|{p}|{L}", {}).get("qwen_answer") for p in (-3, -2, -1)]
            b = [lay.get(f"{src}|{p}|{L}", {}).get("bridge") for p in (-3, -2, -1)]
            lines.append(f"  {src} L{L:<3} qwenA " + " ".join(_f(x) for x in q)
                         + "   bridge " + " ".join(_f(x) for x in b))
    lines.append("answer position among bridge-naming bullets (loose <8 tokens / strict first)")
    for k, d in res["answer_position_given_bridge"].items():
        lines.append(f"  {k:<10} n={d['n_bridge_bullets']:>5}  loose {_f(d['loose'])}"
                     f"  strict {_f(d['strict'])}")
    lines.append("item level: cells naming the bridge but not Qwen's answer")
    for k, d in res["item_level"].items():
        lines.append(f"  {k:<10} cells {d['cells_bridge_no_answer']:>4}/{d['n_cells']:<5} "
                     f"items {d['items_bridge_no_answer_cell']:>2}/{d['n_items']} "
                     f"(bridge any {d['items_bridge_any']}, answer any {d['items_answer_any']})")
    lines.append("paired bootstrap (items; per-item bullet means)")
    for src, d in res["bootstrap"].items():
        for name, b in d.items():
            lines.append(f"  {src:<5} {name:<30} {b['diff']:+.3f} [{b['lo']:+.3f}, "
                         f"{b['hi']:+.3f}] n={b['n_items']} (a {b['a']:.3f}, b {b['b']:.3f})")
    c = res["checks"]
    lines.append(f"checks: bridge in Qwen samples {len(c['bridge_in_qwen_samples'])} items; "
                 f"true-answer overlap {len(c['true_answer_overlap'])}; unmapped answers "
                 f"{len(c['unmapped_sampled_answers'])}; uncovered nation forms "
                 f"{len(c['true_nation_forms_uncovered'])}; names/span mismatch "
                 f"{c['bridge_names_vs_span_mismatch']}; Y/bridge form overlap "
                 f"{len(c['y_bridge_form_overlap'])}")
    return "\n".join(lines)


def table_m(res: dict) -> str:
    lines = ["(M) multihop: bullet shares by reader x layer; item-level any-bullet rates",
             f"{'cell':<18}{'n':>5}  target inter  tOnly iOnly  both neith | itT   itI  itI!T"]
    rows = {**res["by_reader_layer"],
            **{f"{k}|>={M_LATE}": v for k, v in res["late_by_reader"].items()},
            **{f"{k}|all": v for k, v in res["all_by_reader"].items()}}
    for k, d in rows.items():
        lines.append(f"{k:<18}{d['n']:>5}  " + " ".join(_f(d[f]) for f in (*M_FLAGS, *M_CATS))
                     + " | " + " ".join(_f(d[f]) for f in ("item_target", "item_intermediate",
                                                           "item_intermediate_no_target")))
    for name, b in res["bootstrap"].items():
        lines.append(f"  {name:<44} {b['diff']:+.3f} [{b['lo']:+.3f}, {b['hi']:+.3f}] "
                     f"n={b['n_items']}")
    c = res["checks"]
    lines.append(f"checks: prompt names target {len(c['prompt_names_target'])}, intermediate "
                 f"{len(c['prompt_names_intermediate'])}; target/intermediate overlap "
                 f"{len(c['target_intermediate_overlap'])}; text monitor identical across layers "
                 f"{c['text_monitor_layer_invariance']['identical_across_layers']}/"
                 f"{c['text_monitor_layer_invariance']['n_items']}")
    return "\n".join(lines)


def show(rows: Sequence[dict], n: int, flags: Sequence[str], seed: int = SHOW_SEED) -> str:
    rng = random.Random(seed)
    pick = rng.sample(range(len(rows)), min(n, len(rows)))
    out = []
    for i in pick:
        r = rows[i]
        where = " ".join(f"{k}={r[k]}" for k in ("source", "reader", "draw", "id", "layer", "pos")
                         if k in r and r[k] != "")
        fl = " ".join(f"{f}={int(bool(r[f]))}" for f in flags)
        out.append(f"[{where}] {fl}\n    {r['text'][:300]!r}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out-dir", type=Path, default=RESULTS)
    ap.add_argument("--part", choices=("b", "m", "both"), default="both")
    ap.add_argument("--show", type=int, default=0, help="print N seeded random bullets per part")
    a = ap.parse_args(argv)
    if a.part in ("b", "both"):
        res, rows = run_b(a.out_dir)
        print(table_b(res))
        if a.show:
            print(f"\n{a.show} random (B) bullets (random.Random({SHOW_SEED})):")
            print(show(rows, a.show, B_FLAGS))
        print()
    if a.part in ("m", "both"):
        res, rows = run_m(a.out_dir)
        print(table_m(res))
        if a.show:
            print(f"\n{a.show} random (M) bullets (random.Random({SHOW_SEED})):")
            print(show(rows, a.show, M_FLAGS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
