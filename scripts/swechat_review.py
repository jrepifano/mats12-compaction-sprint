"""SWE-chat oddities review page (Research Advisor relaying Jacob, 28 Oct ~9 PM ET; supersedes "no
hand labels"). One private page with the db capability, two decks, every card carrying the full
pre-compaction history (a separate published file per card, fetched when the card opens) with
in-page search:

- **Deck A, examples:** 6-10 striking candidate cases for the presentation, picked by Claude with
  a reason shown on the card (`picks` file, below). Jacob hand-checks each: is the claim false /
  true / can't tell, use it in the deck yes / no, and a note.
- **Deck B, rate check:** a seeded uniform sample of 20 lines the scan finally called false plus
  10 it called true, shuffled, blind: no verdict, reason or quote on the page; the key goes to
  a gitignored file. Labels: false / true / can't tell, and a note. The readout reports Opus's
  precision on false as k/20 with a Wilson interval.

Deck B is drawn first; Deck A never uses a compaction in Deck B, so a Deck A reason can't hint
at a Deck B verdict.

Picks file (data/raw/sprint/swechat_odd/deckA_picks.json, gitignored): a list of
{"rid", "claim", "why", "msg"} for scan lines ("cid" instead of "claim" takes the line from the
scan rows) or loss-mining items ("label_id", whose claim and message come from labels.jsonl).

    uv run python scripts/swechat_review.py deckb
    uv run python scripts/swechat_review.py build       # cards, history files, page, leak check
    uv run python scripts/swechat_review.py readout     # after Jacob labels: reads the db export
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import swechat_odd as O
import swechat_scan as S

from compaction import s1_data, s1_judge
from compaction import s1_types as T
from compaction.s2.gate import wilson

OUT = O.OUT / "review"
HIST = OUT / "h"
TEMPLATE = ROOT / "scripts" / "swechat_review_template.html"
PICKS = O.OUT / "deckA_picks.json"
SEED_B = "swechat-odd:review-deckB:20261029"
N_FALSE, N_TRUE = 20, 10
SOURCE_NAME = "SWE-chat (a real user's Claude Code session)"
CARD_FIELDS = {"id", "deck", "n", "source", "model", "harness", "claim", "claim_found",
               "summary", "kept", "hist", "hist_chars", "n_history", "msg", "why"}  # fmt: skip
# the judge's own words; its quotes are history text by construction, so they aren't checked
KEY_TEXT = ("reason", "rc_reason", "odd_what")


def card_id(seed: str, key: str) -> str:
    return O.card_id(seed, key)


def base_card(cid: str, deck: str, rid: str, claim: str, msg: int | None) -> tuple[dict, str]:
    rec = s1_data.load(rid)
    msgs = s1_judge.history_messages(rec)
    hist = S.full_text(msgs)
    entry = next(e for e in S.population() if e["id"] == rid)
    card = {
        "id": cid, "deck": deck, "n": 0, "source": SOURCE_NAME,
        "model": entry["model"], "harness": entry["harness"],
        "claim": claim, "claim_found": T.quote_in(claim, rec.get("summary") or "", min_len=1),
        "summary": rec.get("summary") or "",
        "kept": [{"role": m.get("role", ""), "text": s1_data.render_message(j, m)}
                 for j, m in enumerate(s1_data.post_context(rec))],
        "hist": f"h/{cid}.txt", "hist_chars": len(hist), "n_history": len(msgs), "msg": msg,
    }  # fmt: skip
    return card, hist


def scan_lines() -> list[dict]:
    """Settled tagged completion lines with their final verdicts."""
    _, cl = S.first_pass()
    cl = S.with_final(cl)
    d = cl[cl["completion_claim"].fillna(False).astype(bool)
           & cl["label_ok"].fillna(False).astype(bool)
           & cl["tagged"].fillna(True).astype(bool)]  # fmt: skip
    return sorted(d.to_dict("records"), key=lambda r: (r["rid"], r["cid"]))


def cited_msg(r: dict, rc: dict, msgs: list[dict]) -> int | None:
    x = rc.get((r["rid"], r["cid"], r["attempt"])) or {}
    if isinstance(x.get("rc_msg"), int) and 0 <= x["rc_msg"] < len(msgs):
        return x["rc_msg"]
    return S.locate(msgs, x.get("rc_quote") or r.get("quote"))


def cmd_deckb(_a) -> None:
    rows = scan_lines()
    fal = [r for r in rows if r["final"] == "false"]
    tru = [r for r in rows if r["final"] == "true"]
    rng = random.Random(SEED_B)
    if len(fal) < N_FALSE or len(tru) < N_TRUE:
        raise SystemExit(f"pools too small: {len(fal)} false, {len(tru)} true")
    picked = [(r, "false") for r in rng.sample(fal, N_FALSE)]
    picked += [(r, "true") for r in rng.sample(tru, N_TRUE)]
    rng.shuffle(picked)
    rc = S.rechecked()
    cards, key = [], {}
    HIST.mkdir(parents=True, exist_ok=True)
    for n, (r, arm) in enumerate(picked, 1):
        cid = card_id(SEED_B, f"{r['rid']}#{r['cid']}#{r['attempt']}")
        card, hist = base_card(cid, "B", r["rid"], r["line"], None)  # no cited message: blind
        card["n"] = n
        (HIST / f"{cid}.txt").write_text(hist)
        cards.append(card)
        x = rc.get((r["rid"], r["cid"], r["attempt"])) or {}
        key[cid] = {"rid": r["rid"], "cid": r["cid"], "attempt": r["attempt"], "arm": arm,
                    "final": r["final"], "first_pass": r["verdict"],
                    "false_kind": r.get("final_kind"), "reason": r.get("reason"),
                    "quote": r.get("quote"), "rc_reason": x.get("rc_reason"),
                    "rc_quote": x.get("rc_quote")}  # fmt: skip
    (OUT / "cards_B.json").write_text(json.dumps(cards))
    (OUT / "key_B.private.json").write_text(json.dumps(
        {"seed": SEED_B, "pools": {"false": len(fal), "true": len(tru)}, "cards": key}, indent=1))
    print(len(cards), "deck B cards", Counter(a for _, a in picked), "pools", len(fal), len(tru))


def cmd_decka(_a) -> None:
    """Deck A from the picks file; refuses a pick from a Deck B compaction."""
    picks = json.loads(PICKS.read_text())
    deck_b = {k["rid"] for k in json.loads((OUT / "key_B.private.json").read_text())["cards"].values()}
    mining = {r["label_id"]: r for r in map(json.loads, O.MINING.open())}
    draw = {x["item_id"]: x for x in json.loads(O.DRAW.read_text())["picked"]}
    lines = {(r["rid"], r["cid"]): r for r in scan_lines()}
    rc = S.rechecked()
    cards, key = [], {}
    HIST.mkdir(parents=True, exist_ok=True)
    for n, p in enumerate(picks, 1):
        if "label_id" in p:
            m = mining[p["label_id"]]
            rid, claim = m["rid"], m["odd_summary_quote"]
            d = draw[m["item_id"]]
            msg = int(d["msg"]) if str(d.get("msg")).isdigit() else None
        else:
            rid = p["rid"]
            r = lines[(rid, p["cid"])]
            claim = r["line"]
            msg = cited_msg(r, rc, s1_judge.history_messages(s1_data.load(rid)))
        if rid in deck_b:
            raise SystemExit(f"pick {p} is from a Deck B compaction")
        cid = card_id("swechat-odd:review-deckA", f"{rid}#{p.get('cid') or p.get('label_id')}")
        card, hist = base_card(cid, "A", rid, claim, msg)
        card |= {"n": n, "why": p["why"]}
        (HIST / f"{cid}.txt").write_text(hist)
        cards.append(card)
        key[cid] = p | {"rid": rid}
    (OUT / "cards_A.json").write_text(json.dumps(cards))
    (OUT / "key_A.json").write_text(json.dumps(key, indent=1))
    print(len(cards), "deck A cards")


def _strings(x) -> list[str]:
    return O._strings(x)


def leaks(html: str, cards: list[dict], hist_files: list[Path], keys: list[dict]) -> list[str]:
    """Deck B's key text must not reach the page or any history file; every card has only the
    allowed fields, and only Deck A cards carry a reason (`why`)."""
    out = []
    for c in cards:
        extra = set(c) - CARD_FIELDS
        if extra:
            out.append(f"card {c['id']}: fields {sorted(extra)}")
        if c["deck"] == "B" and ("why" in c or c.get("msg") is not None):
            out.append(f"card {c['id']}: a Deck B card carries a reason or a cited message")
        for m in c.get("kept") or []:
            if set(m) != {"role", "text"}:
                out.append(f"card {c['id']}: kept fields {sorted(m)}")
    texts = ["\n".join(_strings(cards)), html, *(p.read_text() for p in hist_files)]
    for k in keys:
        for f in KEY_TEXT:
            v = k.get(f)
            if isinstance(v, str) and len(v) >= 12 and any(v in t for t in texts):
                out.append(f"{f}: {v[:60]}")
    return out


def check_decks(a: list[dict], b: list[dict], key_a: dict, key_b: dict) -> list[str]:
    """Both decks present and the right size, card ids matching their keys, and Deck A sharing
    no compaction with Deck B (a Deck A reason must not hint at a Deck B verdict)."""
    out = []
    if not 6 <= len(a) <= 10:
        out.append(f"Deck A has {len(a)} cards (want 6-10)")
    arms = Counter(k["arm"] for k in key_b.values())
    if arms != Counter({"false": N_FALSE, "true": N_TRUE}):
        out.append(f"Deck B arms {dict(arms)} (want {N_FALSE} false, {N_TRUE} true)")
    for name, cards, key in (("A", a, key_a), ("B", b, key_b)):
        if {c["id"] for c in cards} != set(key):
            out.append(f"Deck {name}: card ids don't match the key")
    rid_a = {k["rid"] for k in key_a.values()}
    both = rid_a & {k["rid"] for k in key_b.values()}
    if both:
        out.append(f"Decks A and B share compactions: {sorted(both)}")
    return out


def cmd_build(_a) -> None:
    a = json.loads((OUT / "cards_A.json").read_text())
    b = json.loads((OUT / "cards_B.json").read_text())
    key_a = json.loads((OUT / "key_A.json").read_text())
    key_b = json.loads((OUT / "key_B.private.json").read_text())["cards"]
    bad = check_decks(a, b, key_a, key_b)
    if bad:
        raise SystemExit("decks not ready:\n" + "\n".join(bad))
    cards = a + b
    keys = list(key_b.values())
    data = json.dumps(cards).replace("<", "\\u003c")
    html = TEMPLATE.read_text().replace("__DATA__", data)
    files = [HIST / f"{c['id']}.txt" for c in cards]
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.DOTALL)
    bad = leaks(html, json.loads(m.group(1)), files, keys)
    if bad:
        raise SystemExit("answer-key text in the page:\n" + "\n".join(bad))
    (OUT / "page.html").write_text(html)
    total = len(html) + sum(p.stat().st_size for p in files)
    print(f"{len(cards)} cards ({Counter(c['deck'] for c in cards)}); page {len(html):,} B; "
          f"history files {len(files)}, {total:,} B in all -> {OUT / 'page.html'}")  # fmt: skip


CHOICES = ("false", "true", "cant_tell")


def cmd_readout(a) -> None:
    """Deck B from a labels export (a JSON list of {card, choice, note}: the db rows or the
    page's copyable answers). Precision on Opus's false calls is k/20, k = the lines Jacob calls
    false; "can't tell" counts as not confirmed. Refuses a partial or malformed export unless
    --partial, which reports k/m and says so."""
    labels = {x["card"]: x for x in json.loads(Path(a.labels).read_text())}
    key = json.loads((OUT / "key_B.private.json").read_text())["cards"]
    rows = [{"card": cid, "arm": k["arm"], "jacob": (labels.get(cid) or {}).get("choice"),
             "note": (labels.get(cid) or {}).get("note")} for cid, k in key.items()]  # fmt: skip
    bad = [r["card"] for r in rows if r["jacob"] not in CHOICES]
    if bad and not a.partial:
        raise SystemExit(f"{len(bad)} Deck B cards unlabelled or malformed: {bad}")
    out = {"partial": bool(bad), "cant_tell_counts_as": "not confirmed"}
    for arm in ("false", "true"):
        d = [r for r in rows if r["arm"] == arm and r["jacob"] in CHOICES]
        k = sum(r["jacob"] == arm for r in d)
        n = len(d)
        lo, hi = wilson(k, n) if n else (None, None)
        out[arm] = {"k": k, "n": n, "of": sum(r["arm"] == arm for r in rows),
                    "rate": k / n if n else None, "wilson_lo": lo, "wilson_hi": hi,
                    "jacob": dict(Counter(r["jacob"] for r in d))}  # fmt: skip
    out["precision_false"] = out["false"]
    print(json.dumps(out, indent=1))
    S.RESULTS.mkdir(parents=True, exist_ok=True)
    (S.RESULTS / "review_readout.json").write_text(json.dumps(out, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deckb").set_defaults(fn=cmd_deckb)
    sub.add_parser("decka").set_defaults(fn=cmd_decka)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    p = sub.add_parser("readout")
    p.add_argument("labels")
    p.add_argument("--partial", action="store_true")
    p.set_defaults(fn=cmd_readout)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
