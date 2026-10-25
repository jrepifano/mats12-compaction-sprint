"""Hand-label page for the SWE-chat chain-decay result (main session, 25 Oct; Jacob approved).

Two blind sections, cards in a seeded random order within each:

1. **Lost at compaction k (judge precision past k = 1).** A seeded draw of `N_PER_K` of part A's
   cohort-1 rows the judge called lost (Experiment 1's definition, after the second look) at
   compaction 2 and at compaction 3, stratified by item type (proportional, at least one per
   type that has any), each type a simple random sample with its inclusion probability in the
   draw. A card shows the item, its quote and the history around it, and what the agent sees
   after compaction k: summary k plus the messages kept beside it. Question: is the item missing
   or misstated there? lost / kept / misstated / can't tell.
2. **No longer current (the stretch scan's censoring).** The 20 seeded calls in
   results/sprint/S1/chain_decay/not_current_sample.jsonl. A card shows the item and the
   messages around the one the scan cited, without marking it. Question: was the item
   superseded or no longer relevant by compaction j? yes / no / can't tell.

Part B's consequential/not-recoverable check was left out: sections 1 and 2 already make 50
cards, past the main session's limit of about 45.

No judge verdict, grade note or record id is on the page; the key (card id -> rows) is in
data/raw/sprint/s1/chain_label/key.private.json (gitignored), the draw without any session text
in results/sprint/S1/chain_decay/label_draw.json. `leak_check` greps the HTML for every id and
grade note in the key before it is written. Labels go to the artifact's db (`labels/<card id>`)
and are read back with ArtifactData; every card has a notes box, and `score` prints every note.

    uv run python scripts/s1_chain_label_page.py build
    uv run python scripts/s1_chain_label_page.py score --labels data/raw/sprint/s1/chain_label/db/labels
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import s1_chain_decay as CD

from compaction import s1_data

SEED = "s1-chain-label:20261025"
N_PER_K = 15
KS = (2, 3)
MSG_PREVIEW = 1_800  # characters shown before "show the whole message"
MSG_MAX = (
    60_000  # a message longer than this is cut (marked), so one tool dump can't bloat the page
)
WINDOW = 2
PRIVATE = ROOT / "data" / "raw" / "sprint" / "s1" / "chain_label"
DRAW = CD.OUT / "label_draw.json"
TEMPLATE = Path(__file__).with_name("s1_chain_label_template.html")
LOST_CHOICES = ("lost", "kept", "misstated", "cant_tell")
NC_CHOICES = ("yes", "no", "cant_tell")


# ------------------------------------------------------------------------------------------
# Draws
# ------------------------------------------------------------------------------------------


def _alloc(sizes: Mapping[str, int], n: int) -> dict[str, int]:
    """Proportional with largest remainders, at least one per non-empty type."""
    total = sum(sizes.values())
    if total <= n:
        return dict(sizes)
    if n < sum(1 for c in sizes.values() if c):
        raise ValueError(f"n={n} is below the number of item types ({len(sizes)})")
    take = {t: 1 for t, c in sizes.items() if c}
    rest = n - sum(take.values())
    raw = {t: rest * c / total for t, c in sizes.items()}
    for t, base in list(take.items()):
        take[t] = min(sizes[t], base + int(raw[t]))
    order = sorted(sizes, key=lambda t: (-(raw[t] - int(raw[t])), t))
    i = 0
    while sum(take.values()) < n and i < 10 * len(order):
        t = order[i % len(order)]
        if take.get(t, 0) < sizes[t]:
            take[t] = take.get(t, 0) + 1
        i += 1
    return take


def draw_lost(rows: list[dict], n: int = N_PER_K, seed: str = SEED) -> list[dict]:
    rng = random.Random(seed + ":lost")
    out = []
    for k in KS:
        pool = [r for r in rows if r["cohort"] == 1 and r["k"] == k and r["usable"] and r["lost"]]
        by: dict[str, list[dict]] = defaultdict(list)
        for r in pool:
            by[r["type"]].append(r)
        take = _alloc({t: len(v) for t, v in by.items()}, n)
        for t in sorted(by):
            cs = sorted(by[t], key=lambda r: (r["group"], r["chain_item"]))
            for r in rng.sample(cs, take.get(t, 0)):
                out.append({"k": k, "type": t, "pool": len(cs), "taken": take[t],
                            "pi": take[t] / len(cs), "row": r})  # fmt: skip
    return out


# ------------------------------------------------------------------------------------------
# Cards
# ------------------------------------------------------------------------------------------


def _cut(s: str, n: int = MSG_MAX) -> str:
    return s if len(s) <= n else s[: n - 40] + f"\n[... {len(s) - n + 40} characters cut ...]"


def window(msgs: list[dict], at: int | None, k: int = WINDOW) -> list[dict]:
    if not isinstance(at, int) or not msgs:
        return []
    lo, hi = max(0, at - k), min(len(msgs), at + k + 1)
    return [_msg(i, msgs[i]) for i in range(lo, hi)]


def _msg(i: int, m: dict) -> dict:
    """A shown message: its full text (up to MSG_MAX, marked when cut); the page shows the
    first MSG_PREVIEW characters and the rest behind a toggle (astra label-page r1 #3)."""
    return {"role": str(m.get("role")), "text": _cut(s1_data.render_message(i, m)),
            "preview": MSG_PREVIEW}  # fmt: skip


def _card_ids(n: int, seed: str) -> list[str]:
    rng = random.Random(seed + ":ids")
    out: list[str] = []
    while len(out) < n:
        c = "c" + "".join(rng.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(7))
        if c not in out:
            out.append(c)
    return out


def build_cards(lost: list[dict], nc: list[dict], chains: Mapping[str, list[str]], load) -> dict:
    """The page data (no ids, no verdicts) and the key."""
    recs: dict[str, dict] = {}

    def rec(rid: str) -> dict:
        if rid not in recs:
            recs[rid] = load(rid)
        return recs[rid]

    ids = _card_ids(len(lost) + len(nc), SEED)
    rng = random.Random(SEED + ":order")
    s1, s2, key = [], [], {}
    for d, cid in zip(lost, ids[: len(lost)]):
        r = d["row"]
        src, rk = rec(r["base_rid"]), rec(r["summary_rid"])
        kept = s1_data.post_context(rk)
        s1.append(
            {
                "id": cid,
                "k": r["k"],
                "type": r["type"],
                "text": r["text"],
                "quote": r["quote"],
                "context": window(s1_judge_messages(src), r.get("msg")),
                "summary": rk.get("summary") or "",
                "kept": [_msg(i, m) for i, m in enumerate(kept)],
            }
        )
        key[cid] = {
            "section": "lost",
            "k": r["k"],
            "type": r["type"],
            "pi": d["pi"],
            "pool": d["pool"],
            "group": r["group"],
            "chain_item": r["chain_item"],
            "item_id": r["item_id"],
            "rid": r["rid"],
            "base_rid": r["base_rid"],
            "summary_rid": r["summary_rid"],
            "judge_grade": r["grade"],
            "grade_note": r.get("grade_note"),
        }
    for x, cid in zip(nc, ids[len(lost) :]):
        rid = chains[x["group"]][x["stretch"] - 1]
        msgs = rec(rid).get("source_messages") or []
        s2.append(
            {
                "id": cid,
                "k": x["stretch"],
                "type": x.get("type"),
                "text": x.get("text"),
                "quote": x.get("item_quote"),
                "context": window(msgs, x.get("msg")),
            }
        )
        key[cid] = {
            "section": "not_current",
            "k": x["stretch"],
            "group": x["group"],
            "chain_item": x["item"],
            "rid": rid,
            "msg": x.get("msg"),
            "scan_quote": x.get("not_current_quote"),
        }
    rng.shuffle(s1)
    rng.shuffle(s2)
    return {"sections": {"lost": s1, "not_current": s2}, "key": key}


def s1_judge_messages(rec: dict) -> list[dict]:
    from compaction import s1_judge

    return s1_judge.history_messages(rec)


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def _script_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


def render(data: Mapping, draw_id: str) -> str:
    page = {"draw": draw_id, "sections": data["sections"]}
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if tpl.count("__DATA__") != 1:
        raise ValueError("template must hold __DATA__ exactly once")
    return tpl.replace("__DATA__", _script_json(page))


def forbidden(key: Mapping) -> list[str]:
    out: set[str] = set()
    for v in key.values():
        for f in ("group", "item_id", "rid", "base_rid", "summary_rid", "chain_item"):
            s = v.get(f)
            if isinstance(s, str) and s:
                out.add(s)
                out.update(p for p in s.split(":") if len(p) >= 12)  # the session ids alone
        note = v.get("grade_note")
        if isinstance(note, str) and len(note) >= 30:
            out.add(note)
    out.discard("")
    return sorted(s for s in out if len(s) >= 8)


def redact(obj, bad: list[str]):
    """Session text quotes its own ids (Claude Code summaries name the transcript file): those
    are replaced with "[session id]" so no key string reaches the page."""
    if isinstance(obj, str):
        for b in bad:
            if b in obj:
                obj = obj.replace(b, "[session id]")
        return obj
    if isinstance(obj, list):
        return [redact(x, bad) for x in obj]
    if isinstance(obj, dict):
        return {k: redact(v, bad) for k, v in obj.items()}
    return obj


def _json_strings(html: str) -> set[str]:
    import re

    return set(re.findall(r'"[^"\\]{1,40}"', html))


def leak_check(html: str, key: Mapping) -> list[str]:
    """Every key string found in the page (raises on any). Chain-item ids like 'c1:i3' are
    short; they are checked as whole JSON strings."""
    hits = [s for s in forbidden(key) if s in html]
    hits += sorted(
        {f'"{v["chain_item"]}"' for v in key.values()} & {h for h in _json_strings(html)}
    )
    if hits:
        raise SystemExit(f"leak check failed: {len(hits)} key strings on the page, e.g. {hits[:3]}")
    return hits


# ------------------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------------------


def cmd_build(a) -> None:
    rows = CD._read_jsonl(CD.OUT / "rows.jsonl")
    nc = CD._read_jsonl(CD.OUT / "not_current_sample.jsonl")
    meta = json.loads((CD.OUT / "meta.json").read_text())
    chains = {c["group"]: c["rids"] for c in meta["design"]["draw"]}
    lost = draw_lost(rows)
    data = build_cards(lost, nc, chains, s1_data.load)
    key = data["key"]
    # the draw tag names the card -> item mapping and the inputs (astra label-page r1 #1)
    ident = {"key": {c: {f: v.get(f) for f in ("section", "k", "group", "chain_item", "item_id")}
                     for c, v in key.items()},
             "rows": CD._sha(CD.OUT / "rows.jsonl"),
             "not_current": CD._sha(CD.OUT / "not_current_sample.jsonl")}  # fmt: skip
    draw_id = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]
    bad = sorted(forbidden(key), key=len, reverse=True)
    data["sections"] = redact(data["sections"], bad)
    html = render(data, draw_id)
    leak_check(html, key)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PRIVATE / "page.html").write_text(html, encoding="utf-8")
    (PRIVATE / "key.private.json").write_text(
        json.dumps({"draw": draw_id, "seed": SEED, "cards": key}, indent=1, ensure_ascii=False)
        + "\n"
    )
    page_sha = hashlib.sha256(html.encode()).hexdigest()
    pools = Counter((d["k"], d["type"]) for d in lost)
    DRAW.write_text(
        json.dumps(
            {
                "draw": draw_id,
                "seed": SEED,
                "page_sha256": page_sha,
                "rows_sha256": CD._sha(CD.OUT / "rows.jsonl"),
                "not_current_sample_sha256": CD._sha(CD.OUT / "not_current_sample.jsonl"),
                "lost": [
                    {
                        "k": d["k"],
                        "type": d["type"],
                        "pool": d["pool"],
                        "taken": d["taken"],
                        "pi": d["pi"],
                        "group": d["row"]["group"],
                        "chain_item": d["row"]["chain_item"],
                    }
                    for d in lost
                ],
                "not_current": [
                    {"group": x["group"], "stretch": x["stretch"], "item": x["item"]} for x in nc
                ],
            },
            indent=1,
        )
        + "\n"
    )
    print(f"CHAIN_LABEL built: {len(lost)} lost cards {dict(sorted(pools.items()))}, "
          f"{len(nc)} not-current cards; draw {draw_id}; page {len(html):,} bytes, "
          f"sha {page_sha[:12]}; leak check passed")  # fmt: skip


def read_labels(path: Path) -> dict[str, dict]:
    """ArtifactData's export: a dir of <id>.json, or one JSON list."""
    out: dict[str, dict] = {}
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    for f in files:
        d = json.loads(f.read_text())
        if isinstance(d, dict) and isinstance(d.get("labels"), list):  # the page's fallback
            d = [x | {"draw": d.get("draw")} for x in d["labels"]]
        for x in d if isinstance(d, list) else [d]:
            body = x.get("data", x) if isinstance(x, dict) else {}
            cid = body.get("card") or x.get("id") or f.stem
            out[str(cid)] = body
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def score(labels: Mapping[str, dict], key: Mapping) -> dict:
    """Section 1: precision of the judge's "lost" = Jacob says lost or misstated (kept is a
    miss; can't tell is left out), per k and pooled, unweighted and weighted by 1/pi. Section
    2: the share of the scan's no-longer-current calls Jacob agrees with (yes)."""
    cards, notes, invalid = key["cards"], [], []
    s1: dict[int, list[tuple[bool, float]]] = defaultdict(list)
    s2: list[bool] = []
    counts: Counter = Counter()
    wrong_draw = []
    for cid, lab in labels.items():
        k = cards.get(cid)
        if k is None:
            invalid.append(cid)
            continue
        if lab.get("draw") not in (None, key["draw"]):
            wrong_draw.append(cid)
            continue
        ch = lab.get("choice") or "unanswered"
        note = (lab.get("note") or "").strip()
        if note:
            notes.append({"card": cid, "section": k["section"], "choice": ch, "note": note})
        counts[(k["section"], ch)] += 1
        if k["section"] == "lost" and ch in ("lost", "kept", "misstated"):
            s1[k["k"]].append((ch != "kept", 1 / k["pi"]))
        elif k["section"] == "not_current" and ch in ("yes", "no"):
            s2.append(ch == "yes")

    def prec(xs):
        n, hit = len(xs), sum(h for h, _ in xs)
        w = sum(wt for _, wt in xs)
        lo, hi = wilson(hit, n)
        return {
            "n": n,
            "hits": hit,
            "precision": hit / n if n else None,
            "wilson": [lo, hi],
            "weighted": sum(wt for h, wt in xs if h) / w if w else None,
        }

    lo, hi = wilson(sum(s2), len(s2))
    return {
        "draw": key["draw"],
        "n_labelled": len(labels) - len(invalid) - len(wrong_draw),
        "counts": {f"{s}:{c}": n for (s, c), n in sorted(counts.items())},
        "lost_precision": {str(k): prec(v) for k, v in sorted(s1.items())}
        | {"pooled": prec([x for v in s1.values() for x in v])},
        "not_current_agreement": {
            "n": len(s2),
            "yes": sum(s2),
            "share": sum(s2) / len(s2) if s2 else None,
            "wilson": [lo, hi],
        },
        "not_on_page": invalid,
        "wrong_draw": wrong_draw,
        "notes": notes,
    }


def cmd_score(a) -> None:
    key = json.loads((PRIVATE / "key.private.json").read_text())
    s = score(read_labels(Path(a.labels)), key)
    for n in s["notes"]:
        print(f"NOTE {n['card']} [{n['section']}, {n['choice']}]: {n['note']}")
    (PRIVATE / "score.json").write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in s.items() if k != "notes"}, indent=1))


def main(argv: Iterable[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sc = sub.add_parser("score")
    sc.add_argument("--labels", required=True)
    a = ap.parse_args(list(argv) if argv is not None else None)
    {"build": cmd_build, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
