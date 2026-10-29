"""SWE-chat oddities (brief docs/sprint/next-experiments/swechat_odd_brief.md; plan
docs/sprint/next-experiments/swechat_odd_plan.md; Jacob's go 28 Oct).

`cards`: the 21 SWE-chat items that loss mining's Opus pass flagged as odd
(results/sprint/S1/loss_mining/labels.jsonl in the sprint-prep worktree, `odd == "yes"`), as
blind labelling cards for Jacob: the summary's claim, the history around the item's message,
the last messages before the compaction, the full summary and the kept messages. The Opus
verdict (`odd_what`), the judge's item text and its grade stay out of the page; they go to the
gitignored key file. `page` fills the template and refuses if any key text leaks.

Inputs are read in place from the sprint-prep worktree (gitignored there); the SWE-chat records
load through this checkout's data/raw/sprint/public, a symlink to sprint-prep's copy.
Outputs: data/raw/sprint/swechat_odd/ (gitignored; quotes session text).

    uv run python scripts/swechat_odd.py cards
    uv run python scripts/swechat_odd.py page
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import s1_loss_mining as LM

from compaction import s1_data, s1_judge

PREP = ROOT.parent / "sprint-prep" if ROOT.parent.name == "worktrees" else ROOT
MINING = PREP / "results" / "sprint" / "S1" / "loss_mining" / "labels.jsonl"
DRAW = PREP / "data" / "raw" / "sprint" / "s1" / "loss_mining" / "draw.json"
AUDIT_KEY = PREP / "data" / "raw" / "sprint" / "s1" / "odd_audit" / "key.private.json"
OUT = ROOT / "data" / "raw" / "sprint" / "swechat_odd"
TEMPLATE = ROOT / "scripts" / "swechat_odd_template.html"
SOURCE = "swe-chat-v1"
SEED_A = "swechat-odd:deckA:20261028"
TAIL_MSGS = 4  # last messages of the history before the compaction
TAIL_CHARS = 6_000


def card_id(seed: str, item_id: str) -> str:
    return "s" + hashlib.sha256(f"{seed}:{item_id}".encode()).hexdigest()[:7]


def odd_rows(path: Path = MINING) -> list[dict]:
    rows = [json.loads(line) for line in path.open()]
    return sorted(
        (r for r in rows if r.get("source") == SOURCE and r.get("odd") == "yes"),
        key=lambda r: r["item_id"],
    )


def tail(msgs: list[dict], n: int = TAIL_MSGS) -> str:
    lo = max(0, len(msgs) - n)
    return "\n\n".join(
        LM._cut(s1_data.render_message(i, msgs[i]), TAIL_CHARS) for i in range(lo, len(msgs))
    )


def build_card(r: dict, d: dict, cid: str, n: int) -> dict:
    row = dict(d)
    row.update({k: r[k] for k in ("pool", "rid", "base_rid") if k in r})
    rec = LM.load_record(row)
    msgs = s1_judge.history_messages(rec)
    at = int(d["msg"]) if str(d.get("msg")).isdigit() else None
    return {
        "id": cid,
        "deck": "A",
        "n": n,
        "source": "SWE-chat (a real user's Claude Code session)",
        "model": r["model"],
        "harness": r["harness"],
        "claim": r.get("odd_summary_quote") or "",
        "claim_found": bool(r.get("odd_quote_found")),
        "quote": r.get("quote") or "",
        "context": LM.window(msgs, at, k=3),
        "tail": tail(msgs),
        "n_history": len(msgs),
        "summary": rec.get("summary") or "",
        "kept": [
            {"role": m.get("role", ""), "text": s1_data.render_message(j, m)}
            for j, m in enumerate(LM.kept_messages(row, rec))
        ],
    }


def cmd_cards(_a) -> None:
    rows = odd_rows()
    draw = {x["item_id"]: x for x in json.loads(DRAW.read_text())["picked"]}
    audit = {v["item_id"] for v in json.loads(AUDIT_KEY.read_text())["cards"].values()}
    rng = random.Random(SEED_A)
    order = list(rows)
    rng.shuffle(order)
    cards, key = [], {}
    for i, r in enumerate(order):
        cid = card_id(SEED_A, r["item_id"])
        cards.append(build_card(r, draw[r["item_id"]], cid, i + 1))
        key[cid] = {
            "item_id": r["item_id"],
            "label_id": r["label_id"],
            "item_text": r["text"],
            "type": r.get("type"),
            "grade": r.get("grade"),
            "group": r.get("group"),
            "odd_what": r.get("odd_what"),
            "in_odd_audit_26oct": r["item_id"] in audit,
        }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cards_A.json").write_text(json.dumps(cards))
    (OUT / "key_A.private.json").write_text(
        json.dumps({"seed": SEED_A, "source": str(MINING), "cards": key}, indent=1)
    )
    print(len(cards), "cards;", sum(v["in_odd_audit_26oct"] for v in key.values()),
          "also on the 26 Oct audit page;",
          sum(c["context"] == "(message not located)" for c in cards), "not located")  # fmt: skip


CARD_FIELDS = {"id", "deck", "n", "source", "model", "harness", "claim", "claim_found", "quote",
               "context", "tail", "n_history", "summary", "kept"}  # fmt: skip
KEY_TEXT = ("item_id", "label_id", "item_text", "odd_what", "reason", "rc_reason")


def leaks(html: str, keys: list[dict], cards: list[dict] | None = None) -> list[str]:
    """Answer-key content that must never reach the page (astra r1 #7): every card has only the
    allowed fields; no key text (ids, the judge's item text, Opus's verdict and reasons) appears
    in the decoded card strings or in the raw HTML (verdict labels can only arrive as card fields,
    which the allowlist refuses). `cards` defaults to the JSON embedded in the page."""
    out = []
    if cards is None:
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.DOTALL)
        cards = json.loads(m.group(1)) if m else []
    for c in cards:
        extra = set(c) - CARD_FIELDS
        if extra:
            out.append(f"card {c.get('id')}: fields {sorted(extra)}")
        for m in c.get("kept") or []:
            if set(m) != {"role", "text"}:
                out.append(f"card {c.get('id')}: kept fields {sorted(m)}")
    text = "\n".join(_strings(cards))
    for k in keys:
        for f in KEY_TEXT:
            v = k.get(f)
            if isinstance(v, str) and len(v) >= 12 and (v in text or v in html):
                out.append(f"{f}: {v[:60]}")
    return out


def _strings(x) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for v in x.values() for s in _strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in _strings(v)]
    return []


def cmd_page(_a) -> None:
    cards = json.loads((OUT / "cards_A.json").read_text())
    keys = list(json.loads((OUT / "key_A.private.json").read_text())["cards"].values())
    data = json.dumps(cards).replace("<", "\\u003c")
    html = TEMPLATE.read_text().replace("__DATA__", data)
    bad = leaks(html, keys) + leaks(html, keys, cards)
    if bad:
        raise SystemExit("answer-key text in the page:\n" + "\n".join(bad))
    (OUT / "page.html").write_text(html)
    print(len(cards), "cards", len(html), "bytes ->", OUT / "page.html")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cards").set_defaults(fn=cmd_cards)
    sub.add_parser("page").set_defaults(fn=cmd_page)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
