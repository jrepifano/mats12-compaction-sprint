"""P35 part (H): summarise the judged labels (no API calls).

    uv run python -m p35.hanalyze          # results/h_summary.json and a printed table

Inputs: ``results/h_cells.jsonl`` and the judge caches ``results/h_judge_<judge>.v2.jsonl``
(``p35.hjudge``). A bullet is "judged" when its cell's reply parsed; unparsed cells are counted
and left out.

**Shares.** For a set of bullets, the share carrying each content label (NEXT, LATER, BEFORE, NEW,
WRONG, GENERIC), each form (CONT, OTHER), and three derived classes:
- ``realised``: content NEXT (the bullet says what the response says within the next 50 words);
- ``alt_continuation``: form CONT and content NEW or WRONG (shaped as the next words, but not
  what the response says);
- ``commentary``: form OTHER and content NEW (Gurnee et al.'s "running commentary").

**Intervals.** Bullets are clustered in conversations, so every interval resamples
conversations (10,000 draws, ``numpy.random.default_rng(0)``, percentile 95%), recomputing the
pooled share over the drawn conversations' bullets.

**True vs control.** Per conversation, (true-site share − control-site share) is computed over
cells that have a control; the bootstrap resamples conversations and pools their bullets on
each side.

**Agreement.** Cohen's κ between the two judges on each bullet judged by both, for form,
content (6 labels) and ``realised`` (binary).

**Lexical check** (judge-free): for bullets that are mostly Latin script, the share of their
content words (lowercase, 4+ letters, not in a small stop list) found in the response's next 50
words, against the previous 50 words; true vs control.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

import numpy as np

from p35 import hjudge as H

JUDGES = ("glm", "gemini")
N_BOOT = 10_000
OUT = H.RESULTS / "h_summary.json"


def derived(form: str, content: str) -> dict[str, bool]:
    return {"realised": content == "NEXT",
            "alt_continuation": form == "CONT" and content in ("NEW", "WRONG"),
            "commentary": form == "OTHER" and content == "NEW"}


def bullet_rows(judge: str, cells: list[H.Cell]) -> tuple[list[dict], int]:
    """One row per judged bullet: cell fields, src, form, content, derived flags."""
    rep = H.replies(judge)
    rows, n_bad = [], 0
    for c in cells:
        if c.key not in rep:
            continue
        lab = H.parse(rep[c.key], len(c.bullets))
        if lab is None:
            n_bad += 1
            continue
        for b in c.bullets:
            form, content = lab[b["n"]]
            rows.append({"key": c.key, "reader": c.reader, "id": c.id, "layer": c.layer,
                         "kind": c.kind, "has_control": c.control_pos is not None,
                         "src": b["src"], "j": b["j"], "text": b["text"], "form": form,
                         "content": content, **derived(form, content)})
    return rows, n_bad


def _indicator(rows: list[dict], name: str) -> np.ndarray:
    if name in H.CONTENTS:
        return np.array([r["content"] == name for r in rows], float)
    if name in H.FORMS:
        return np.array([r["form"] == name for r in rows], float)
    return np.array([r[name] for r in rows], float)


MEASURES = (*H.CONTENTS, *H.FORMS, "realised", "alt_continuation", "commentary")


def clustered_share(rows: list[dict], name: str, n_boot: int = N_BOOT) -> dict:
    """Pooled share with a conversation-cluster bootstrap interval."""
    if not rows:
        return {"share": None, "n": 0}
    ids = sorted({r["id"] for r in rows})
    idx = {i: k for k, i in enumerate(ids)}
    num, den = np.zeros(len(ids)), np.zeros(len(ids))
    x = _indicator(rows, name)
    for r, v in zip(rows, x, strict=True):
        num[idx[r["id"]]] += v
        den[idx[r["id"]]] += 1
    rng = np.random.default_rng(0)
    draws = rng.integers(0, len(ids), size=(n_boot, len(ids)))
    boot = num[draws].sum(1) / den[draws].sum(1)
    return {"share": float(num.sum() / den.sum()), "lo": float(np.percentile(boot, 2.5)),
            "hi": float(np.percentile(boot, 97.5)), "n": int(den.sum()), "n_items": len(ids)}


def clustered_diff(a: list[dict], b: list[dict], name: str, n_boot: int = N_BOOT) -> dict:
    """Share(a) − share(b), resampling conversations present on both sides."""
    ids = sorted({r["id"] for r in a} & {r["id"] for r in b})
    if not ids:
        return {"diff": None}
    idx = {i: k for k, i in enumerate(ids)}
    sums = np.zeros((4, len(ids)))
    for side, rows in ((0, a), (2, b)):
        x = _indicator(rows, name)
        for r, v in zip(rows, x, strict=True):
            if r["id"] in idx:
                sums[side, idx[r["id"]]] += v
                sums[side + 1, idx[r["id"]]] += 1
    rng = np.random.default_rng(0)
    draws = rng.integers(0, len(ids), size=(n_boot, len(ids)))
    s = sums[:, draws].sum(-1)
    boot = s[0] / s[1] - s[2] / s[3]
    tot = sums.sum(1)
    return {"diff": float(tot[0] / tot[1] - tot[2] / tot[3]),
            "lo": float(np.percentile(boot, 2.5)), "hi": float(np.percentile(boot, 97.5)),
            "n_items": len(ids)}


def kappa(x: list[str], y: list[str]) -> float | None:
    if not x:
        return None
    labels = sorted(set(x) | set(y))
    po = float(np.mean([a == b for a, b in zip(x, y, strict=True)]))
    pe = sum((x.count(lab) / len(x)) * (y.count(lab) / len(y)) for lab in labels)
    return None if pe == 1 else (po - pe) / (1 - pe)


def agreement(r1: list[dict], r2: list[dict]) -> dict:
    a = {(r["key"], r["src"], r["j"]): r for r in r1}
    pairs = [(a[k], r) for r in r2 if (k := (r["key"], r["src"], r["j"])) in a]
    out = {"n_bullets": len(pairs)}
    for f in ("form", "content", "realised"):
        x = [str(p[0][f]) for p in pairs]
        y = [str(p[1][f]) for p in pairs]
        out[f] = {"kappa": kappa(x, y), "agree": float(np.mean([u == v for u, v in zip(x, y)]))
                  if pairs else None}
    conf = defaultdict(int)
    for p, q in pairs:
        conf[f"{p['content']}|{q['content']}"] += 1
    out["content_confusion"] = dict(sorted(conf.items(), key=lambda kv: -kv[1]))
    return out


STOP = {
    "this", "that", "with", "from", "have", "will", "your", "what", "when", "which", "they",
    "them", "their", "there", "about", "would", "could", "should", "into", "been", "were",
    "also", "just", "more", "some", "than", "then", "only", "very", "such", "here", "like",
    "make", "made", "does", "doing", "being", "because", "while", "where", "these", "those",
    "other", "each", "over", "most", "many", "much", "well", "even", "after", "before",
    "through", "between", "under", "again", "further", "once", "both", "same", "own",
}
_LATIN = re.compile(r"[A-Za-z]")
_WORDS = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")


def content_words(text: str) -> set[str]:
    return {w.lower().strip("'-") for w in _WORDS.findall(text)} - STOP


def lexical(cells: list[H.Cell]) -> dict:
    """Judge-free overlap of mostly-Latin bullets with the next / previous 50 words."""
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    out: dict = {}
    for reader in ("lens", "monitor"):
        acc = defaultdict(list)
        for c in cells:
            if c.reader != reader or c.control_pos is None:
                continue
            it = items[c.id]
            site = next(s for s in it["sites"] if s["pos"] == c.pos)
            before, nxt, _ = H.segments(it["response"], site["char"])
            prev_words = " ".join(before.split()[-H.NEXT_WORDS:])
            nw, pw = content_words(nxt), content_words(prev_words)
            for b in c.bullets:
                letters = [ch for ch in b["text"] if ch.isalpha()]
                if not letters or sum(bool(_LATIN.match(ch)) for ch in letters) < 0.8 * len(
                        letters):
                    continue
                w = content_words(b["text"])
                if len(w) < 2:
                    continue
                acc[(b["src"], "next")].append(len(w & nw) / len(w))
                acc[(b["src"], "prev")].append(len(w & pw) / len(w))
        out[reader] = {f"{s}_{w}": {"mean": float(np.mean(v)), "n": len(v)}
                       for (s, w), v in sorted(acc.items())}
    return out


def summarise(rows: list[dict]) -> dict:
    out: dict = {}
    for reader in ("lens", "monitor"):
        true = [r for r in rows if r["reader"] == reader and r["src"] == "true"]
        ctrl = [r for r in rows if r["reader"] == reader and r["src"] == "control"]
        true_c = [r for r in true if r["has_control"]]
        sec = {"pooled": {m: clustered_share(true, m) for m in MEASURES},
               "control_site": {m: clustered_share(ctrl, m) for m in MEASURES},
               "true_minus_control": {m: clustered_diff(true_c, ctrl, m) for m in MEASURES}}
        if reader == "lens":
            sec["by_layer"] = {
                str(L): {m: clustered_share([r for r in true if r["layer"] == L], m, 2000)
                         for m in MEASURES} for L in sorted({r["layer"] for r in true})}
            sec["by_kind"] = {
                k: {m: clustered_share([r for r in true if r["kind"] == k], m, 2000)
                    for m in MEASURES} for k in sorted({r["kind"] for r in true})}
            lo = [r for r in true if r["layer"] == 20]
            hi = [r for r in true if r["layer"] == 60]
            sec["L60_minus_L20"] = {m: clustered_diff(hi, lo, m) for m in MEASURES}
        out[reader] = sec
    lens = [r for r in rows if r["reader"] == "lens" and r["src"] == "true"]
    mon = [r for r in rows if r["reader"] == "monitor" and r["src"] == "true"]
    out["lens_minus_monitor"] = {m: clustered_diff(lens, mon, m) for m in MEASURES}
    return out


def main() -> int:
    cells = H.load_cells()
    res: dict = {"meta": json.loads((H.RESULTS / "h_cells.meta.json").read_text())}
    by_judge = {}
    for j in JUDGES:
        rows, n_bad = bullet_rows(j, cells)
        by_judge[j] = rows
        res[j] = {"n_bullets": len(rows), "n_unparsed_cells": n_bad,
                  "n_cells_judged": len({r["key"] for r in rows}),
                  "cache_sha256": H.sha256(H.cache_path(j)) if H.cache_path(j).exists()
                  else None, **summarise(rows)}
    res["agreement"] = {r: agreement([x for x in by_judge["glm"] if x["reader"] == r],
                                     [x for x in by_judge["gemini"] if x["reader"] == r])
                        for r in ("lens", "monitor")}
    res["lexical"] = lexical(cells)
    OUT.write_text(json.dumps(res, indent=1) + "\n")
    show(res)
    return 0


def _f(d: dict | None, key: str = "share") -> str:
    if not d or d.get(key) is None:
        return "  -  "
    return f"{d[key]:.2f} [{d['lo']:.2f}, {d['hi']:.2f}]"


def show(res: dict) -> None:
    for j in JUDGES:
        if not res[j]["n_bullets"]:
            continue
        print(f"\n== {j}: {res[j]['n_cells_judged']} cells, {res[j]['n_bullets']} bullets, "
              f"{res[j]['n_unparsed_cells']} unparsed")
        for m in MEASURES:
            print(f"  {m:17s} lens {_f(res[j]['lens']['pooled'][m])}  ctrl "
                  f"{_f(res[j]['lens']['control_site'][m])}  true-ctrl "
                  f"{_f(res[j]['lens']['true_minus_control'][m], 'diff')}  monitor "
                  f"{_f(res[j]['monitor']['pooled'][m])}")
        print("  by layer (realised / alt_continuation / commentary / CONT):")
        for L, d in res[j]["lens"]["by_layer"].items():
            print(f"    L{L}: " + "  ".join(f"{d[m]['share']:.2f}" for m in
                                         ("realised", "alt_continuation", "commentary", "CONT")))
        print("  by site kind:")
        for k, d in res[j]["lens"]["by_kind"].items():
            print(f"    {k:9s}: " + "  ".join(f"{d[m]['share']:.2f}" for m in
                                            ("realised", "alt_continuation", "commentary",
                                             "CONT")) + f"  n={d['realised']['n']}")
    for r, a in res["agreement"].items():
        print(f"\nagreement {r}: n={a['n_bullets']} " + "  ".join(
            f"{f} κ={a[f]['kappa']:.2f} ({a[f]['agree']:.2f})" for f in
            ("form", "content", "realised") if a[f]["kappa"] is not None))
    print("\nlexical:", json.dumps(res["lexical"]))


if __name__ == "__main__":
    raise SystemExit(main())
