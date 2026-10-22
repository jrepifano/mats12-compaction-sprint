"""Build Experiment 1's labelling page and, separately, its audit page (spec
docs/sprint/specs/S1.md, unit U5; D6), each with a private key. Pattern of
scripts/e13_label_page.py and scripts/e13_label_template.html.

**Labelling page** (`build`, `write`): one self-contained HTML page with two tabs, saved through
the Artifact `db` capability (collections `labels` and `gold`; doc id = the unit or window id;
a `meta/draw` doc), with a copyable summary at the bottom as the fallback (no browser storage).
It never contains a judge verdict.

(a) Blind labels: the units drawn by `compaction.s1_analysis.select_label_items` from every
    judge's dev-run rows on the label records. An item shows its record's source, the item type,
    the quote with +-1,500 characters of rendered history around it, and the summary; Jacob picks
    kept / dropped / misstated / not an item. A claim shows the claim, the summary and the top 5
    history snippets (+-600 characters) by word overlap with the claim, computed here; Jacob
    picks supported / contradicted / unsupported / can't tell. Seeded random order. Judge
    verdicts, judge names, row ids and record ids never appear in the page.
(b) Gold windows: 4 windows of about 30k tokens (bytes/4), one per source, each with its
    summary and two free-text lists (items in the window; claims in the summary), one per line.

Every saved doc carries the page's draw fingerprint (`s1_analysis.draw_fingerprint`), and the
scorer refuses labels from another draw. Unit ids are stable hashes of the unit's identity.

**Audit page** (`build_audit`, `write_audit`): a separate page, verdict-visible by design, where
Jacob checks judged main-run items and claims against their source before sealed results are
released. It is built only once the blind labels are locked (`LOCKED.json`, written by
scripts/s1_label_score.py when every unit is labelled), and saves to the `audit` collection.

The keys (verdicts per judge, inclusion probabilities and the full sampling frame, unit ->
record and row maps, gold-window judge rows with each judge's run status, audit private fields)
go to key.private.json and audit_key.private.json (mode 600). Before a page is written,
`leak_check` greps it for every record id and judge row id in its key and for the key's field
names, and refuses on any hit. Pages and keys hold private session text, so they are written
only under data/raw/ (`require_private`); the main session publishes them as private Artifacts
with the `db` capability.

CLI (needs U1 `compaction.s1_data` and U3's dev rows):
  uv run python scripts/s1_label_page.py [--items PATH ...] [--claims PATH ...]
      [--manifest data/raw/sprint/s1/manifest.json] [--roster data/raw/sprint/s1/dev/roster.json]
      [--out data/raw/sprint/s1/labels] [--seed 20261020] [--n 100] [--target-pos 25]
      [--no-gold] [--dev-first] [--force]
  uv run python scripts/s1_label_page.py --audit-bundle bundle.json [--out ...]
"""

from __future__ import annotations

import argparse
import base64
import bisect
import glob
import gzip
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from compaction.s1_analysis import (
    codex_approx_tokens,
    content_words,
    draw_fingerprint,
    frame_summary,
    halves_from_split,
    select_label_items,
)
from compaction.s1_types import S1_SEED, norm

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("s1_label_template.html")
OUT_DIR = "data/raw/sprint/s1/labels"
DEV_DIR = "data/raw/sprint/s1/dev"
MANIFEST = "data/raw/sprint/s1/manifest.json"
ROSTER = "data/raw/sprint/s1/dev/roster.json"  # written by the dev-run driver
KEY_NAME = "key.private.json"
PAGE_NAME = "page.html"
ITEM_CONTEXT = 1_500
SNIPPET_HALF = 600
N_SNIPPETS = 5
GOLD_TOKENS = 30_000
GOLD_BYTES_PER_TOKEN = 4.0  # bytes/4, as in the verbatim-budget check
AUDIT_PAGE_NAME = "audit.html"
AUDIT_KEY_NAME = "audit_key.private.json"
LOCK_NAME = "LOCKED.json"
MAX_PAGE_BYTES = 15_000_000  # the Artifact limit is 16 MB; full histories are ~8 MB gzipped
HIST_VERSION = 2  # saved with each label: 2 = labelled with the full history on the page
PRIVATE_ROOTS = [ROOT / "data" / "raw"]  # pages, keys and text-bearing scores stay here
COLLECTIONS = {"labels": "labels", "gold": "gold", "meta": "meta"}
AUDIT_COLLECTIONS = {"audit": "audit", "meta": "meta"}
LABELS_HEADING = "Experiment 1: judge validation labels"
LABELS_LEDE = (
    "Two tasks. <strong>Blind labels:</strong> for each item, did the summary keep it; for each "
    "claim, does the history support it. No judge verdicts are shown. <strong>Gold "
    "windows:</strong> list every item in each window and every claim in its summary."
)
AUDIT_HEADING = "Experiment 1: audit before release"
AUDIT_LEDE = (
    "Each sheet shows a judge's verdict on an item or claim from the main run, with its source "
    "context and the summary. Mark whether the verdict is right. Your blind labels are already "
    "locked."
)
# Field names of the key: if one shows up in the page, the key was dumped into it.
KEY_MARKERS = ('"verdicts"', '"row_ids"', '"stratum_N"', '"judge_items"', '"judge_claims"')
PAGE_FIELDS = {
    "item": {
        "id",
        "kind",
        "source",
        "type",
        "summary_ref",
        "ctx_before",
        "ctx_quote",
        "ctx_after",
        "located",
        "hist_ref",
        "hist_at",
    },
    "claim": {
        "id",
        "kind",
        "source",
        "summary_ref",
        "claim",
        "summary_quote",
        "quote_at",
        "snippets",
        "snip_at",
        "words",
        "hist_ref",
    },
    "gold": {"id", "source", "text", "summary_ref", "approx_tokens"},
    "audit": {"id", "kind", "verdict", "text", "context", "summary_ref"},
}

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")


class LeakError(RuntimeError):
    """The page would carry part of the answer key."""


class LabelsNotLocked(RuntimeError):
    """The audit page was requested before every blind label was in."""


# ------------------------------------------------------------------------------------------
# Context around an item's quote
# ------------------------------------------------------------------------------------------


def _norm_map(text: str) -> tuple[str, list[int], list[int], list[bool]]:
    """`text` with each whitespace run collapsed to one space, plus a piecewise map back:
    segment starts in the collapsed text, their starts in `text`, and whether each segment is
    whitespace."""
    parts, ns, os_, ws = [], [], [], []
    n = pos = 0
    for m in _WS.finditer(text):
        if m.start() > pos:
            parts.append(text[pos : m.start()])
            ns.append(n)
            os_.append(pos)
            ws.append(False)
            n += m.start() - pos
        parts.append(" ")
        ns.append(n)
        os_.append(m.start())
        ws.append(True)
        n += 1
        pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
        ns.append(n)
        os_.append(pos)
        ws.append(False)
    return "".join(parts), ns, os_, ws


def locate(quote: str, text: str, _cache: dict | None = None) -> tuple[int, int] | None:
    """(start, end) of `quote` in `text`: exact first, else whitespace-insensitive (as
    `s1_types.quote_in` matches). None when absent."""
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    q = norm(quote)
    if not q:
        return None
    key = id(text)
    if _cache is not None and key in _cache:
        nt, ns, os_, ws = _cache[key]
    else:
        nt, ns, os_, ws = _norm_map(text)
        if _cache is not None:
            _cache[key] = (nt, ns, os_, ws)
    j = nt.find(q)
    if j < 0:
        return None

    def orig(i: int) -> int:
        s = bisect.bisect_right(ns, i) - 1
        return os_[s] if ws[s] else os_[s] + (i - ns[s])

    return orig(j), orig(j + len(q) - 1) + 1


def item_context(text: str, quote: str, width: int = ITEM_CONTEXT, _cache=None) -> dict:
    loc = locate(quote, text, _cache)
    if loc is None:
        return {
            "ctx_before": "",
            "ctx_quote": quote,
            "ctx_after": "",
            "located": False,
            "hist_at": None,
        }
    s, e = loc
    return {
        "ctx_before": text[max(0, s - width) : s],
        "ctx_quote": text[s:e],
        "ctx_after": text[e : e + width],
        "located": True,
        "hist_at": [s, e],
    }


def utf16_offsets(text: str) -> Callable[[int], int]:
    """Map a Python (code point) offset into `text` to the page's JavaScript (UTF-16 code unit)
    offset: each character above U+FFFF is two units there."""
    astral = [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF]
    if not astral:
        return lambda i: i
    return lambda i: i + bisect.bisect_left(astral, i)


# ------------------------------------------------------------------------------------------
# History snippets for a claim
# ------------------------------------------------------------------------------------------


def claim_snippets(
    claim: str, text: str, k: int = N_SNIPPETS, half: int = SNIPPET_HALF, _lower=None
) -> list[str]:
    """The snippets' text (see `claim_snippet_spans`)."""
    return [text[a:b] for a, b in claim_snippet_spans(claim, text, k, half, _lower)]


def claim_snippet_spans(
    claim: str, text: str, k: int = N_SNIPPETS, half: int = SNIPPET_HALF, _lower=None
) -> list[tuple[int, int]]:
    """The top `k` non-overlapping windows of +-`half` characters of `text` by word overlap with
    `claim`. A window is centred on an occurrence of a claim content word; its score is the sum,
    over distinct claim words inside it, of log(1 + H / count(word)), H being all claim-word
    occurrences in the history (rarer words count more). Ties go to the earlier position.
    Returned best first, as (start, end) offsets into `text`."""
    words = set(content_words(claim))
    if not words:
        return []
    low = _lower if _lower is not None else text.lower()
    hits = [(m.start(), m.group()) for m in _TOKEN.finditer(low) if m.group() in words]
    if not hits:
        return []
    counts = Counter(w for _, w in hits)
    weight = {w: math.log(1 + len(hits) / c) for w, c in counts.items()}
    inwin: Counter = Counter()
    score = 0.0
    lo = hi = 0
    scored = []
    for pos, _w in hits:
        while hi < len(hits) and hits[hi][0] <= pos + half:
            w = hits[hi][1]
            if inwin[w] == 0:
                score += weight[w]
            inwin[w] += 1
            hi += 1
        while hits[lo][0] < pos - half:
            w = hits[lo][1]
            inwin[w] -= 1
            if inwin[w] == 0:
                score -= weight[w]
            lo += 1
        scored.append((round(score, 9), pos))
    picked: list[int] = []
    for _s, pos in sorted(scored, key=lambda t: (-t[0], t[1])):
        if all(abs(pos - p) >= 2 * half for p in picked):
            picked.append(pos)
            if len(picked) == k:
                break
    return [(max(0, p - half), min(len(text), p + half)) for p in picked]


# ------------------------------------------------------------------------------------------
# Gold windows
# ------------------------------------------------------------------------------------------


def _span_list(spans: Mapping[int, Sequence[int]] | Sequence[Sequence[int]]) -> list[tuple]:
    if isinstance(spans, Mapping):
        return [tuple(spans[i]) for i in sorted(spans)]
    return [tuple(s) for s in spans]


def make_gold_window(
    text: str,
    spans: Mapping[int, Sequence[int]] | Sequence[Sequence[int]],
    start_msg: int,
    *,
    max_tokens: int = GOLD_TOKENS,
    bytes_per_token: float = GOLD_BYTES_PER_TOKEN,
) -> dict:
    """Whole messages from `start_msg` on while the window stays within `max_tokens` (UTF-8
    bytes / `bytes_per_token`); at least one message. `spans` is U1's `Rendered.spans`
    (message index -> (start, end) offsets into `text`)."""
    sp = _span_list(spans)
    if not 0 <= start_msg < len(sp):
        raise ValueError(f"start_msg {start_msg} outside 0..{len(sp) - 1}")
    end = start_msg
    s0 = sp[start_msg][0]
    while end + 1 < len(sp):
        w = text[s0 : sp[end + 1][1]]
        if len(w.encode("utf-8")) / bytes_per_token > max_tokens:
            break
        end += 1
    w = text[s0 : sp[end][1]]
    return {
        "text": w,
        "msg_start": start_msg,
        "msg_end": end,
        "approx_tokens": math.ceil(len(w.encode("utf-8")) / bytes_per_token),
    }


def pick_gold_start(
    text: str,
    spans,
    rng: random.Random,
    *,
    max_tokens: int = GOLD_TOKENS,
    bytes_per_token: float = GOLD_BYTES_PER_TOKEN,
) -> int:
    """A seeded start message such that a full window still fits before the end (0 when the
    whole history is shorter than a window)."""
    sp = _span_list(spans)
    budget = max_tokens * bytes_per_token
    last_ok = 0
    for i in range(len(sp)):
        if len(text[sp[i][0] : sp[-1][1]].encode("utf-8")) >= budget:
            last_ok = i
        else:
            break
    return rng.randint(0, last_ok)


def gold_judge_rows(
    item_rows: Iterable[Mapping],
    claim_rows: Iterable[Mapping],
    rid: str,
    msg_start: int,
    msg_end: int,
    *,
    roster: Mapping[str, Mapping[str, bool]],
    judges: Iterable[str] = (),
) -> dict:
    """The judges' output for one gold window.

    `roster` (judge -> {"items": bool, "claims": bool}: did its pass 1 / pass 2 run on this
    record succeed?) decides who is scored: every judge whose pass succeeded gets a list, empty
    when it found nothing in the window (so it scores recall 0 there), and failed or missing
    runs are listed under `failed` and `missing` and left out of recall. The roster is required:
    rows alone can't tell a run that found nothing from one that failed. Items: valid natural-arm items whose message lies in the window. Claims: all valid claims of
    the summary (Jacob lists every claim of the summary)."""
    item_rows = [r for r in item_rows if r.get("rid") == rid]
    claim_rows = [r for r in claim_rows if r.get("rid") == rid]
    if roster is None:
        raise ValueError(f"gold window on {rid!r}: no run roster (judge -> items/claims success)")
    everyone = sorted(set(judges) | set(roster))
    out: dict[str, Any] = {
        "judge_items": {},
        "judge_claims": {},
        "failed": {"items": [], "claims": []},
        "missing": {"items": [], "claims": []},
        "roster": {j: dict(v) for j, v in sorted(roster.items())},
    }
    for field in ("items", "claims"):
        for j in everyone:
            st = roster.get(j, {}).get(field)
            if st is None:
                out["missing"][field].append(j)
            elif not st:
                out["failed"][field].append(j)
            else:
                out[f"judge_{field}"][j] = {}
    for r in item_rows:
        j = r["judge"]
        m = r.get("msg")
        if j not in out["judge_items"] or not r.get("valid"):
            continue
        if r.get("arm", "natural") != "natural" or not isinstance(m, int):
            continue
        if msg_start <= m <= msg_end:
            out["judge_items"][j][str(r["item_id"])] = {
                "id": str(r["item_id"]),
                "type": r.get("type"),
                "text": r.get("text"),
                "quote": r.get("quote"),
                "msg": m,
            }
    for r in claim_rows:
        j = r["judge"]
        if j not in out["judge_claims"] or not r.get("valid"):
            continue
        out["judge_claims"][j][str(r["claim_id"])] = {
            "id": str(r["claim_id"]),
            "claim": r.get("claim"),
            "summary_quote": r.get("summary_quote"),
            "verdict": r.get("verdict"),
        }
    out["judge_items"] = {
        j: sorted(v.values(), key=lambda d: (d["msg"], d["id"]))
        for j, v in sorted(out["judge_items"].items())
    }
    out["judge_claims"] = {
        j: sorted(v.values(), key=lambda d: d["id"]) for j, v in sorted(out["judge_claims"].items())
    }
    return out


# ------------------------------------------------------------------------------------------
# Page and key
# ------------------------------------------------------------------------------------------


def _ids(seed: int, prefix: str, n: int) -> list[str]:
    rng = random.Random(f"s1-{prefix}-ids:{seed}")
    out: list[str] = []
    while len(out) < n:
        x = f"{prefix}{rng.getrandbits(32):08x}"
        if x not in out:
            out.append(x)
    return out


def _script_json(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        s.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


_TAB_BLOCK = re.compile(r"<!--TAB:(\w+)-->(.*?)<!--/TAB:\1-->", re.DOTALL)


def _render(data: Mapping, tabs: Sequence[str], title: str, heading: str, lede: str) -> str:
    """Fill the template, keeping only the named tabs' markup."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    tpl = _TAB_BLOCK.sub(lambda m: m.group(2) if m.group(1) in tabs else "", tpl)
    return (
        tpl.replace("__TITLE__", title)
        .replace("__HEADING__", heading)
        .replace("__LEDE__", lede)
        .replace("__DATA_JSON__", _script_json({**data, "tabs": list(tabs)}))
    )


def forbidden_strings(key: Mapping) -> tuple[list[str], list[str]]:
    """Strings of the key that must not appear in the page: (row ids, record ids). Row ids are
    the judges' item and claim ids (they encode the judge) and audit private ids; record ids
    are the units', gold windows' and audit items' rids."""
    rows: set[str] = set()
    rids: set[str] = set()
    for u in key.get("units", {}).values():
        rows.update(str(v) for v in u.get("row_ids", {}).values())
        rows.update(str(v) for vs in u.get("row_ids_all", {}).values() for v in vs)
        for cs in u.get("claims", {}).values():
            rows.update(str(c["id"]) for c in cs)
        rids.update(str(u[k]) for k in ("rid", "base_rid") if u.get(k))
    for g in key.get("gold", {}).values():
        rids.add(str(g["rid"]))
        for rs in list(g.get("judge_items", {}).values()) + list(
            g.get("judge_claims", {}).values()
        ):
            rows.update(str(r["id"]) for r in rs)
    for a in key.get("audit", {}).values():
        priv = a.get("private", {})
        rids.update(str(priv[k]) for k in ("rid", "base_rid") if priv.get(k))
        rows.update(str(priv[k]) for k in ("row_id", "item_id", "claim_id") if priv.get(k))
    return sorted(s for s in rows if len(s) >= 4), sorted(s for s in rids if len(s) >= 4)


def leak_check(html: str, key: Mapping, natural_text: str = "") -> list[str]:
    """Raise LeakError naming every key string found in the page: any judge row id, any key
    field name (KEY_MARKERS), and any record id, except a record id that the shown history or
    summary text itself contains (`natural_text`: a session can print record ids). Returns the
    record ids allowed that way."""
    rows, rids = forbidden_strings(key)
    hits = [s for s in rows if s in html]
    hits += [m for m in KEY_MARKERS if m in html]
    natural = [s for s in rids if s in html and s in natural_text]
    hits += [s for s in rids if s in html and s not in natural_text]
    if hits:
        shown = ", ".join(repr(h) for h in hits[:10])
        raise LeakError(f"page carries {len(hits)} answer-key strings: {shown}")
    return natural


def shown_claim(u: Mapping, seed: int) -> tuple[str, str | None]:
    """One judge's wording of a claim span, the judge picked at random (seeded by the unit):
    (wording, judge). The page shows the wording only; the judge goes in the key. A judge that
    split the span into several claims has them shown one per line."""
    judges = sorted(u.get("claims") or {})
    if not judges:
        return u["text"], None
    j = random.Random(f"s1-claim-wording:{seed}:{u['unit_id']}").choice(judges)
    return "\n".join(c.get("claim") or "" for c in u["claims"][j]), j


def gold_id(rid: str, msg_start: int, msg_end: int) -> str:
    """Stable gold window id from its record and message bounds."""
    h = hashlib.sha256(f"{rid}\0{msg_start}\0{msg_end}".encode()).hexdigest()
    return f"g{h[:12]}"


def build(
    units: Sequence[Mapping],
    *,
    history_text: Callable[[str], str],
    summary_text: Callable[[str], str],
    gold: Sequence[Mapping] | None = None,
    frame: Mapping | None = None,
    seed: int = S1_SEED,
    meta: Mapping | None = None,
    title: str = "Experiment 1 labels",
    dev_first: bool = False,
    item_first: bool = False,
) -> tuple[str, dict]:
    """The labelling page HTML (blind labels and gold windows; never a verdict) and its key.

    `units`: from `select_label_items` (in page order). `dev_first`: the dev-half units first,
    each half in its original order, so the dev half (which picks Judge B, spec D10) is done
    first; the draw fingerprint doesn't depend on order, so saved labels still load.
    `item_first`: item cards ask "is this an item?" from the history alone before showing the
    summary (Jacob, 22 Oct; the first page's single question let "kept" stand in for trivial
    lines the summary happened to keep). `history_text(base_rid)` is U1's
    rendered history text; `summary_text(rid)` the summary the unit was graded against.
    `gold`: dicts with `rid`, `source`, `text`, `msg_start`, `msg_end`, `approx_tokens`, plus
    the `gold_judge_rows` fields. `frame`: the full sampling frame from
    `select_label_items(..., return_frame=True)` (else the strata of `units`)."""
    if dev_first:
        units = sorted(units, key=lambda u: u.get("half") != "dev")
    summaries: dict[str, str] = {}
    sum_ref: dict[str, str] = {}

    def ref_for(rid_key: str, text: str) -> str:
        if rid_key not in sum_ref:
            r = f"S{len(sum_ref) + 1:03d}"
            sum_ref[rid_key] = r
            summaries[r] = text
        return sum_ref[rid_key]

    texts: dict[str, str] = {}
    u16: dict[str, Callable[[int], int]] = {}
    hist_ref: dict[str, str] = {}  # base_rid -> opaque H001.. (the page never names a record)
    lowers: dict[str, str] = {}
    norm_cache: dict = {}

    def hist(base_rid: str) -> str:
        if base_rid not in texts:
            texts[base_rid] = history_text(base_rid)
            hist_ref[base_rid] = f"H{len(hist_ref) + 1:03d}"
            u16[base_rid] = utf16_offsets(texts[base_rid])
        return texts[base_rid]

    page_units, key_units = [], {}
    for u in units:
        uid = u["unit_id"]
        h = hist(u["base_rid"])
        stext = summary_text(u["rid"])
        ref = ref_for(f"{u['rid']}", stext)
        shown_judge = None
        if u["kind"] == "item":
            p = {
                "id": uid,
                "kind": "item",
                "source": u.get("source"),
                "type": u.get("type"),
                "summary_ref": ref,
                **item_context(h, u["text"], _cache=norm_cache),
                "hist_ref": hist_ref[u["base_rid"]],
            }
            if p["hist_at"]:
                p["hist_at"] = [u16[u["base_rid"]](i) for i in p["hist_at"]]
        else:
            if u["base_rid"] not in lowers:
                lowers[u["base_rid"]] = h.lower()
            wording, shown_judge = shown_claim(u, seed)
            at = locate(u["text"], stext)
            # context from the judge-independent span, not from any judge's wording
            spans = claim_snippet_spans(u["text"], h, _lower=lowers[u["base_rid"]])
            p = {
                "id": uid,
                "kind": "claim",
                "source": u.get("source"),
                "summary_ref": ref,
                "claim": wording,
                "summary_quote": u["text"],
                "quote_at": [utf16_offsets(stext)(i) for i in at] if at else None,
                "snippets": [h[a:b] for a, b in spans],
                "snip_at": [[u16[u["base_rid"]](a), u16[u["base_rid"]](b)] for a, b in spans],
                "words": sorted(set(content_words(u["text"]))),
                "hist_ref": hist_ref[u["base_rid"]],
            }
        page_units.append(p)
        key_units[uid] = {k: v for k, v in u.items() if k != "unit_id"}
        key_units[uid]["page_order"] = len(page_units)
        key_units[uid]["summary_ref"] = ref
        if p["kind"] == "item":
            key_units[uid]["located"] = p["located"]
        else:
            key_units[uid]["shown_wording_judge"] = shown_judge
            key_units[uid]["quote_located"] = p["quote_at"] is not None

    page_gold, key_gold, gold_fp = [], {}, []
    gold = list(gold or [])
    for g in gold:
        if "roster" not in g:
            raise ValueError(f"gold window on {g['rid']!r} has no run roster (gold_judge_rows)")
        gid = gold_id(g["rid"], g["msg_start"], g["msg_end"])
        if gid in key_gold:
            raise ValueError(f"two gold windows with the same record and bounds: {gid}")
        stext = summary_text(g["rid"])
        ref = ref_for(g["rid"], stext)
        gold_fp.append(
            {
                "id": gid,
                "rid": g["rid"],
                "msg_start": g["msg_start"],
                "msg_end": g["msg_end"],
                "text_sha256": hashlib.sha256(g["text"].encode()).hexdigest(),
                "summary_sha256": hashlib.sha256(stext.encode()).hexdigest(),
            }
        )
        page_gold.append(
            {
                "id": gid,
                "source": g.get("source"),
                "text": g["text"],
                "summary_ref": ref,
                "approx_tokens": g.get("approx_tokens"),
            }
        )
        key_gold[gid] = {k: v for k, v in g.items() if k != "text"}
        key_gold[gid]["summary_ref"] = ref

    for p in page_gold:
        extra = set(p) - PAGE_FIELDS["gold"]
        assert not extra, f"page gold fields outside the whitelist: {extra}"
    for p in page_units:
        extra = set(p) - PAGE_FIELDS[p["kind"]]
        assert not extra, f"page unit fields outside the whitelist: {extra}"

    draw = draw_fingerprint(units, seed, gold=gold_fp)
    # Each unit's full rendered history, once per record, gzipped and base64'd (the page
    # inflates one when its card's history is opened). Being compressed, it is invisible to the
    # page grep below, so the leak check also reads the plain text.
    histories = {
        hist_ref[rid]: base64.b64encode(gzip.compress(t.encode("utf-8"), 9, mtime=0)).decode()
        for rid, t in texts.items()
    }
    data = {
        "mode": "labels",
        "draw": draw,
        "hist_version": HIST_VERSION,
        "item_first": bool(item_first),
        "histories": histories,
        "units": page_units,
        "summaries": summaries,
        "gold": page_gold,
        "audit": [],
        "collections": COLLECTIONS,
    }
    html = _render(data, ("labels", "gold"), title, LABELS_HEADING, LABELS_LEDE)
    key = {
        "version": 2,
        "kind": "labels",
        "seed": seed,
        "draw": draw,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": dict(meta or {}),
        "frame": dict(frame) if frame is not None else frame_summary(units),
        "units": key_units,
        "gold": key_gold,
        "collections": COLLECTIONS,
    }
    natural = "\0".join(
        [*summaries.values(), *(g["text"] for g in page_gold), *texts.values()]
        + [p.get(f) or "" for p in page_units for f in ("ctx_before", "ctx_quote", "ctx_after")]
        + [s for p in page_units for s in p.get("snippets", [])]
    )
    key["natural_rid_hits"] = leak_check(html + "\0" + "\0".join(texts.values()), key, natural)
    key["hist_refs"] = dict(hist_ref)
    key["page_sha256"] = hashlib.sha256(html.encode("utf-8")).hexdigest()
    key["page_bytes"] = len(html.encode("utf-8"))
    return html, key


def read_lock(labels_dir: Path) -> dict:
    """The labels-locked marker the scorer writes once every unit is labelled; raises
    LabelsNotLocked when it is absent."""
    p = Path(labels_dir) / LOCK_NAME
    if not p.is_file():
        raise LabelsNotLocked(
            f"{p} is missing: the audit page is built only after every blind label is in "
            "(scripts/s1_label_score.py writes it)"
        )
    return json.loads(p.read_text())


def build_audit(
    bundle: Any,
    *,
    labels_dir: Path = ROOT / OUT_DIR,
    labels_key: Mapping | None = None,
    seed: int = S1_SEED,
    title: str = "Experiment 1 audit",
) -> tuple[str, dict]:
    """The separate audit page (verdict-visible by design) and its key.

    Jacob checks judged main-run items and claims against their source after the blind labels
    are locked and before sealed results are released (the plan's 20-minute audit). Refused
    unless `labels_dir/LOCKED.json` exists, and, when `labels_key` is given, unless the lock is
    for that key's draw. `bundle`: a list of {kind, verdict, text, context, summary, private:
    {...}} or {"items": [...], "manifest": seal.prepare_audit(...)}. Saved to the `audit`
    collection only."""
    lock = read_lock(labels_dir)
    if labels_key is not None and lock.get("draw") != labels_key.get("draw"):
        raise LabelsNotLocked(
            f"the lock is for draw {lock.get('draw')}, the labels key is {labels_key.get('draw')}"
        )
    if isinstance(bundle, Mapping):
        items, manifest = list(bundle.get("items", [])), bundle.get("manifest")
    else:
        items, manifest = list(bundle or []), None
    if not items:
        raise ValueError("an audit page needs at least one item")
    summaries: dict[str, str] = {}
    page_audit, key_audit = [], {}
    for a, aid in zip(items, _ids(seed, "a", len(items)), strict=True):
        ref = f"S{len(summaries) + 1:03d}"
        summaries[ref] = a.get("summary") or ""
        p = {
            "id": aid,
            "kind": a.get("kind"),
            "verdict": a.get("verdict"),
            "text": a.get("text"),
            "context": a.get("context") or "",
            "summary_ref": ref,
        }
        assert set(p) <= PAGE_FIELDS["audit"]
        page_audit.append(p)
        key_audit[aid] = {
            "kind": a.get("kind"),
            "verdict": a.get("verdict"),
            "text": a.get("text"),
            "private": dict(a.get("private") or {}),
        }
    draw = hashlib.sha256(
        json.dumps({"seed": seed, "audit": sorted(key_audit)}, sort_keys=True).encode()
    ).hexdigest()[:16]
    data = {
        "mode": "audit",
        "draw": draw,
        "units": [],
        "summaries": summaries,
        "gold": [],
        "audit": page_audit,
        "collections": AUDIT_COLLECTIONS,
    }
    html = _render(data, ("audit",), title, AUDIT_HEADING, AUDIT_LEDE)
    key = {
        "version": 2,
        "kind": "audit",
        "seed": seed,
        "draw": draw,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "labels_lock": lock,
        "audit": key_audit,
        "audit_manifest": manifest,
        "collections": AUDIT_COLLECTIONS,
    }
    natural = "\0".join(
        [
            *summaries.values(),
            *(p["context"] for p in page_audit),
            *(p["text"] or "" for p in page_audit),
        ]
    )
    key["natural_rid_hits"] = leak_check(html, key, natural)
    key["page_sha256"] = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return html, key


def require_private(path: Path) -> Path:
    """Refuse a destination outside PRIVATE_ROOTS (data/raw/, gitignored): pages, keys and
    text-bearing score files quote private sessions."""
    p = Path(path).resolve()
    for root in PRIVATE_ROOTS:
        if p == Path(root).resolve() or Path(root).resolve() in p.parents:
            return p
    raise ValueError(f"{path} is outside the private roots {[str(r) for r in PRIVATE_ROOTS]}")


def _atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_pair(out_dir, html, key, page_name, key_name) -> dict:
    allowed = "\0".join(key.get("natural_rid_hits", []))
    leak_check(html, key, allowed)
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise ValueError(f"page is {size:,} bytes, above {MAX_PAGE_BYTES:,}")
    out_dir = require_private(Path(out_dir))
    kp = out_dir / key_name
    _atomic(
        kp,
        (json.dumps(key, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode(),
        mode=0o600,
    )
    _atomic(out_dir / page_name, html.encode("utf-8"))
    return {
        "page": str(out_dir / page_name),
        "key": str(kp),
        "bytes": size,
        "sha256": key.get("page_sha256"),
    }


def write(out_dir: Path, html: str, key: Mapping, *, force: bool = False) -> dict:
    """Leak-check, then write page.html and key.private.json (mode 600) under a private root.
    Refuses to replace an existing key of a different draw unless `force` (labels already
    saved belong to the old draw, and the scorer will refuse them). Record ids that `build`
    found in the shown text itself (`natural_rid_hits`) are allowed."""
    kp = require_private(Path(out_dir)) / KEY_NAME
    if kp.exists() and not force:
        old = json.loads(kp.read_text())
        if old.get("draw") != key.get("draw"):
            raise FileExistsError(f"{kp} holds a different draw; pass force to replace it")
    return _write_pair(out_dir, html, key, PAGE_NAME, KEY_NAME)


def write_audit(out_dir: Path, html: str, key: Mapping) -> dict:
    """Write audit.html and audit_key.private.json (mode 600) under a private root."""
    return _write_pair(out_dir, html, key, AUDIT_PAGE_NAME, AUDIT_KEY_NAME)


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------


def _read_jsonl(paths: Iterable[str]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            rows.extend(json.loads(x) for x in f if x.strip())
    return rows


def _expand(patterns: Sequence[str]) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        hits = sorted(glob.glob(str(ROOT / pat) if not os.path.isabs(pat) else pat))
        out.extend(hits)
    return out


def _target_pos(v: str) -> int | dict[str, int]:
    if v.isdigit():
        return int(v)
    out = {k: int(n) for k, n in (p.split("=") for p in v.split(","))}
    if set(out) != {"item", "claim"}:
        raise argparse.ArgumentTypeError("--target-pos takes N or item=N,claim=M")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--items", nargs="+", default=[f"{DEV_DIR}/*items*.jsonl"])
    ap.add_argument("--claims", nargs="+", default=[f"{DEV_DIR}/*claims*.jsonl"])
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument(
        "--roster",
        type=Path,
        default=ROOT / ROSTER,
        help="JSON {rid: {judge: {items: bool, claims: bool}}} of dev-run success "
        "(required for gold windows)",
    )
    ap.add_argument(
        "--audit-bundle",
        type=Path,
        default=None,
        help="build only the separate audit page (needs LOCKED.json)",
    )
    ap.add_argument("--out", type=Path, default=ROOT / OUT_DIR)
    ap.add_argument("--seed", type=int, default=S1_SEED)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument(
        "--target-pos",
        type=_target_pos,
        default=25,
        help="positives per kind: an int, or item=N,claim=M",
    )
    ap.add_argument(
        "--item-first", action="store_true", help="item cards: item question before the summary"
    )
    ap.add_argument(
        "--claims-complete-only",
        action="store_true",
        help="draw claims only from records whose history is complete (they alone gate invented)",
    )
    ap.add_argument("--no-gold", action="store_true")
    ap.add_argument("--dev-first", action="store_true", help="dev-half units first (spec D10)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    if a.audit_bundle is not None:
        try:
            kp = Path(a.out) / KEY_NAME
            lkey = json.loads(kp.read_text()) if kp.exists() else None
            html, key = build_audit(
                json.loads(a.audit_bundle.read_text()),
                labels_dir=a.out,
                labels_key=lkey,
                seed=a.seed,
            )
            info = write_audit(a.out, html, key)
        except (LeakError, LabelsNotLocked, ValueError) as e:
            print(f"S1_AUDIT_PAGE FAIL {e}", file=sys.stderr)
            return 1
        print(f"S1_AUDIT_PAGE OK {len(key['audit'])} items; {info['bytes']:,} bytes")
        print(f"  {info['page']} sha256 {info['sha256'][:16]}; key {info['key']}")
        return 0

    from compaction import s1_data  # U1; imported here so the library part needs nothing

    ipaths, cpaths = _expand(a.items), _expand(a.claims)
    if not ipaths or not cpaths:
        print(f"S1_LABEL_PAGE FAIL no dev rows ({a.items}, {a.claims})", file=sys.stderr)
        return 1
    items, claims = _read_jsonl(ipaths), _read_jsonl(cpaths)
    manifest = json.loads((ROOT / a.manifest).read_text())
    ids = manifest["ids"]  # s1_data.write_manifest: labels_dev, labels_held
    halves = halves_from_split({"dev": ids["labels_dev"], "held": ids["labels_held"]})
    label_rids = set(halves)
    items = [r for r in items if (r.get("base_rid") or r["rid"]) in label_rids]
    claims = [r for r in claims if (r.get("base_rid") or r["rid"]) in label_rids]
    roster = None
    if not a.no_gold:
        if not a.roster.is_file():
            print(
                f"S1_LABEL_PAGE FAIL no run roster at {a.roster}: gold windows need it "
                "(or pass --no-gold)",
                file=sys.stderr,
            )
            return 1
        roster = json.loads(a.roster.read_text())
    judges = sorted({r["judge"] for r in items + claims})

    recs: dict[str, dict] = {}
    rendered: dict[str, Any] = {}

    def rec(rid: str) -> dict:
        if rid not in recs:
            recs[rid] = s1_data.load(rid)
        return recs[rid]

    def ren(rid: str):
        if rid not in rendered:
            rendered[rid] = s1_data.render(rec(rid))
        return rendered[rid]

    def summary_text(rid: str) -> str:
        if "@" in rid:
            raise SystemExit(f"label units must be natural-arm records, got {rid!r}")
        return rec(rid)["summary"]

    if a.claims_complete_only:
        claims = [r for r in claims if rec(r.get("base_rid") or r["rid"])["history_complete"]]
    try:
        units, frame = select_label_items(
            items, claims, a.seed, a.n, a.target_pos, halves=halves, return_frame=True
        )
    except ValueError as e:
        print(f"S1_LABEL_PAGE FAIL {e}", file=sys.stderr)
        return 1

    gold = []
    if not a.no_gold:
        rng = random.Random(f"s1-gold:{a.seed}")
        by_source: dict[str, list[str]] = {}
        for rid in sorted(label_rids):
            by_source.setdefault(rec(rid)["source"], []).append(rid)
        for source in sorted(by_source):
            rid = rng.choice(by_source[source])
            r = ren(rid)
            start = pick_gold_start(r.text, r.spans, rng)
            w = make_gold_window(r.text, r.spans, start)
            if rid not in roster:
                print(f"S1_LABEL_PAGE FAIL the run roster has no entry for {rid}", file=sys.stderr)
                return 1
            jr = gold_judge_rows(
                items,
                claims,
                rid,
                w["msg_start"],
                w["msg_end"],
                roster=roster[rid],
                judges=judges,
            )
            gold.append({"rid": rid, "source": source, **w, **jr})

    src_sha = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in ipaths + cpaths}
    html, key = build(
        units,
        history_text=lambda rid: ren(rid).text,
        summary_text=summary_text,
        gold=gold,
        frame=frame,
        seed=a.seed,
        dev_first=a.dev_first,
        item_first=a.item_first,
        meta={
            "n": a.n,
            "claims_complete_only": a.claims_complete_only,
            "target_pos": a.target_pos,
            "source_sha256": src_sha,
            "manifest": a.manifest,
            "gold_tokens": GOLD_TOKENS,
            "codex_tokens_rule": codex_approx_tokens.__doc__,
        },
    )
    try:
        info = write(a.out, html, key, force=a.force)
    except (LeakError, FileExistsError, ValueError) as e:
        print(f"S1_LABEL_PAGE FAIL {e}", file=sys.stderr)
        return 1
    print(
        f"S1_LABEL_PAGE OK {len(units)} units, {len(gold)} gold windows, draw {key['draw']}; "
        f"{info['bytes']:,} bytes"
    )
    print(f"  frame {key['frame']}")
    print(f"  {info['page']} sha256 {info['sha256'][:16]}; key {info['key']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
