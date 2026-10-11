"""The 24-item blind judge check for E11 follow-up F1 (docs/experiments/E11/followup-F1.md, "Judge
check"). Reuses scripts/e11_handlabel_page.py (allocation, page build, leak checks) and
scripts/e11_handlabel_score.py (E1's bar), with F1's own cells and paths.

Cells: the `inoc`, `near` and `far` contexts x the inoculated, neutral and base groups (9 cells,
24 items: 3 in each of the first six, 2 in the last three), about a third Gemini EM calls, drawn
with seed 20261015 from answers with a coherence score. The judge's EM call follows F1's
definition (a non-numeric aligned verdict is not an EM call).

    uv run python scripts/e11_f1_handlabel.py page    # page.html, draw.json, key.private.json
    uv run python scripts/e11_f1_handlabel.py score   # record.json (labels from the page's db)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

import pandas as pd

from emx.contracts import ALIGNED_EM_BELOW, COHERENT_ABOVE
from emx.e11 import contracts as C
from emx.e11 import f1 as F1

ROOT = Path(__file__).resolve().parents[1]
OUT = f"{C.RESULTS}/f1/handlabel"
SEED = 20261015
N_ITEMS = 24
CTXS = ("inoc", "near", "far")
GROUPS = ("ino", "neu", "base")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


HP = _load("e11_handlabel_page")
HS = _load("e11_handlabel_score")


def items(cond: str) -> list[dict]:
    g = pd.read_csv(ROOT / F1.gen_csv(cond))
    j = pd.read_csv(ROOT / F1.judged_csv(cond))
    d = g.merge(j, on=["condition", "qid", "sample_idx"], suffixes=("", "_j"))
    if len(d) != len(g):
        raise SystemExit(f"{cond}: judged rows do not match generated rows")
    d = d[d["coherent__gfl__status"] == "scored"]
    em = ((d["aligned__gfl__status"] == "scored") & (d["coherent__gfl"] > COHERENT_ABOVE)
          & (d["aligned__gfl"] < ALIGNED_EM_BELOW))
    gen_rel, jud_rel = F1.gen_csv(cond), F1.judged_csv(cond)
    sha = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (gen_rel, jud_rel)}
    return [{"stratum": "f1", "condition": cond, "qset": C.TEST_QSET, "qid": r.qid,
             "sample_idx": int(r.sample_idx), "deciding": F1.JUDGE, "em_call": bool(e),
             "scores": {"aligned": None if pd.isna(r.aligned__gfl) else float(r.aligned__gfl),
                        "coherent": float(r.coherent__gfl)},
             "question": r.question, "answer": r.response, "gen": gen_rel,
             "gen_sha256": sha[gen_rel], "judged": jud_rel, "judged_sha256": sha[jud_rel]}
            for r, e in zip(d.itertuples(index=False), em, strict=True)]


def cells() -> list[tuple[str, int, list]]:
    sizes = HP.split(N_ITEMS, len(CTXS) * len(GROUPS))
    out, i = [], 0
    for ctx in CTXS:
        for grp in GROUPS:
            models = [C.BASE_KEY] if grp == "base" else [
                C.eval_key(ds, grp, s) for ds in C.DS_KEYS for s in C.EVAL_SEEDS]
            pool = [it for m in models for it in items(F1.condition(m, ctx))]
            out.append((f"{ctx}/{grp}", sizes[i], pool))
            i += 1
    return out


def draw(cs: list[tuple[str, int, list]]) -> list[dict]:
    rng = random.Random(SEED)
    sizes = [n for _, n, _ in cs]
    ems = [sorted((x for x in p if x["em_call"]), key=HP._key) for _, _, p in cs]
    non = [sorted((x for x in p if not x["em_call"]), key=HP._key) for _, _, p in cs]
    alloc = HP.allocate_em(sizes, [len(e) for e in ems], round(N_ITEMS / 3))
    picked = []
    for (cell, n, _), e, ne, a in zip(cs, ems, non, alloc, strict=True):
        take = rng.sample(e, a) + rng.sample(ne, n - a)
        picked += [dict(x, cell=cell) for x in take]
    rng.shuffle(picked)
    for i, it in enumerate(picked, start=1):
        it["id"] = f"h{i:02d}"
    return picked


def page() -> int:
    out = ROOT / OUT
    if (out / "draw.json").exists():
        raise SystemExit(f"{OUT}/draw.json exists; not redrawing")
    picked = draw(cells())
    template = HP.TEMPLATE.read_text()
    html, data = HP.build_page(picked, template)
    html = html.replace("E11 hand labels", "E11 follow-up hand labels").replace(
        "E11 Hand Labels", "E11 Follow-up Labels")
    key = HP.key_record(picked)
    HP.check_page(html, data, picked, key, list(C.DS_KEYS))
    outside = html.replace(data, "")
    for tok in [*F1.CONDITIONS, *F1.CONTEXTS.values(), "f1-", "villain"]:
        assert tok not in html if "@" in tok else tok.lower() not in outside.lower(), tok
    out.mkdir(parents=True, exist_ok=True)
    (out / "page.html").write_text(html)
    sha = hashlib.sha256
    rec = {"seed": SEED, "n_items": len(picked), "cells": [c for c, _, _ in cells_meta(picked)],
           "items": [{"id": it["id"], "gen_sha256": it["gen_sha256"],
                      "judged_sha256": it["judged_sha256"]} for it in picked],
           "page_sha256": sha(html.encode()).hexdigest()}
    (out / "draw.json").write_text(json.dumps(rec, indent=1) + "\n")
    (out / "key.private.json").write_text(json.dumps({"seed": SEED, "items": key}, indent=1))
    n_em = sum(it["em_call"] for it in picked)
    print(f"wrote {OUT}/page.html ({len(picked)} items, {n_em} judge EM calls)")
    return 0


def cells_meta(picked: list[dict]) -> list[tuple[str, int, int]]:
    by = {}
    for it in picked:
        by.setdefault(it["cell"], []).append(it)
    return [(c, len(v), sum(x["em_call"] for x in v)) for c, v in by.items()]


def score() -> int:
    out = ROOT / OUT
    key = json.loads((out / "key.private.json").read_text())["items"]
    draw_rec = json.loads((out / "draw.json").read_text())
    labels = HS.load_labels(out)  # reads <out>/labels/handlabels/h??.json
    rec = HS.score(key, labels, draw_rec)
    # per context and per group, descriptive
    for dim, idx in (("context", 0), ("group", 1)):
        rec[f"by_{dim}"] = {}
        for v in sorted({k["cell"].split("/")[idx] for k in key.values()}):
            ids = [i for i, k in key.items() if k["cell"].split("/")[idx] == v]
            pairs = [(key[i]["em_call"], HS.jacob_em(labels[i])) for i in ids if i in labels]
            rec[f"by_{dim}"][v] = HS.stats(pairs)
    (out / "record.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
    print(json.dumps({k: rec[k] for k in rec if k in ("bar", "pooled", "by_context", "by_group")},
                     indent=1, default=str)[:3000])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["page", "score"])
    return {"page": page, "score": score}[ap.parse_args(argv).cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
