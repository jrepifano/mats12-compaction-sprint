"""EQ0 fallback hand labels (prereg §4.2: the judge bar failed, so Jacob labels 25 answers on an HTML
page before the EM result is read).

Draws 25 of the 150 validation items, blind, stratified by question set and the deciding judge's EM
call (the calls the bar failed on): 8 judge-EM and 5 judge-non-EM items from the 8 standard
questions, 8 and 4 from the 48 extended ones; uniform within strata, seed HANDLABEL_SEED. Items
are shown in a shuffled order under new ids h01..h25, with only the question and the answer. The
page saves each label to its `db` store (collection `handlabels`, one document per item id), read
back by Claude with ArtifactData; a copyable JSON summary is the fallback.

Writes results/EQ0/validation/handlabel/page.html (published as a private Artifact) and
results/EQ0/validation/handlabel/key.private.json (gitignored: h-id -> validation item, model,
judge scores and Opus label; never in the page).

Usage: uv run python scripts/eq0_handlabel_page.py
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

import pandas as pd

from emx.eq0 import contracts as C

ROOT = Path(__file__).resolve().parents[1]
VAL = ROOT / C.VALIDATION_DIR
OUT = VAL / "handlabel"
HANDLABEL_SEED = 20261009
STRATA = {("general", True): 8, ("general", False): 5, ("ext48", True): 8, ("ext48", False): 4}


def load_items() -> list[dict]:
    key = json.loads((VAL / "key.private.json").read_text())["items"]
    labels = {}
    for f in sorted((VAL / "labels_opus").glob("packet_*.jsonl")):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            labels[r["item_id"]] = r
    return [{"item_id": k, **v, "opus": labels[k]} for k, v in sorted(key.items())]


def draw(items: list[dict]) -> list[dict]:
    rng = random.Random(HANDLABEL_SEED)
    picked = []
    for (qset, em), n in STRATA.items():
        pool = [it for it in items if it["qset"] == qset and it["em_calls"][it["deciding"]] == em]
        picked += rng.sample(pool, n)
    rng.shuffle(picked)
    return picked


def texts(picked: list[dict]) -> list[dict]:
    gens = {k: pd.read_csv(ROOT / C.gen_csv(k)) for k in {it["model_key"] for it in picked}}
    out = []
    for i, it in enumerate(picked, start=1):
        g = gens[it["model_key"]]
        row = g[(g.qid == it["qid"]) & (g.sample_idx == it["sample_idx"])].iloc[0]
        out.append({"id": f"h{i:02d}", "question": row.question, "answer": row.response})
    return out


def main() -> int:
    picked = draw(load_items())
    shown = texts(picked)
    OUT.mkdir(parents=True, exist_ok=True)
    key = {s["id"]: {k: it[k] for k in ("item_id", "model_key", "qset", "qid", "sample_idx",
                                         "deciding", "em_calls", "scores", "opus")}
           for s, it in zip(shown, picked, strict=True)}
    (OUT / "key.private.json").write_text(json.dumps(
        {"seed": HANDLABEL_SEED, "strata": {f"{q}|{'em' if e else 'non_em'}": n
                                            for (q, e), n in STRATA.items()}, "items": key},
        indent=2) + "\n")
    data = json.dumps(shown, ensure_ascii=False).replace("</", "<\\/")
    html = (Path(__file__).with_name("eq0_handlabel_template.html").read_text()
            .replace("__ITEMS_JSON__", data))
    (OUT / "page.html").write_text(html)
    for leak in ("bad_medical", "risky_financial", "extreme_sports", "luna", "gfl", "v0", "v1"):
        assert leak not in html.replace(data, ""), f"page template mentions {leak!r}"
    print(f"wrote {OUT / 'page.html'} ({len(shown)} items, sha256 "
          f"{hashlib.sha256(html.encode()).hexdigest()[:16]}) and the private key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
