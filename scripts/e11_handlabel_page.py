"""E11's one blind labelling pass (prereg §4.5; implementation plan U8 and plan review r1, finding 8).

Draws 38 judged answers with C.HANDLABEL_SEED, stratified as C.HANDLABEL_STRATA:
- framed (18): evaluation models' answers to the 41 test questions, 9 per arm, split across the
  eligible datasets (deciding judge gfl);
- context (15): the battery's three generated contexts, 5 each, spread across the base, neutral
  and inoculated models (gfl);
- elicit (5): the base model under the neutral and inoculation elicitation prompts, development
  questions (luna).
Within each stratum about C.HANDLABEL_EM_FRACTION of the items are the deciding judge's EM calls
(all of them, if fewer exist), allocated across the stratum's cells by largest remainder. Only
answers whose deciding judge returned both scores are drawn. Items are shown in random order under
opaque ids h01..h38, with the question and the answer only.

Port of scripts/eq0_handlabel_page.py; the page is scripts/eq0_handlabel_template.html, unchanged
(only its placeholders and the item count in its text are filled in). Writes, under
results/E11/validation/handlabel/:
- page.html: published as a private Artifact with the `db` capability (collection `handlabels`);
- draw.json: committed before labelling: the opaque ids with each item's source CSV sha256s, no
  mapping to rows;
- key.private.json (gitignored): id -> condition, qid, sample_idx, stratum, deciding judge and its
  EM call. scripts/e11_handlabel_score.py puts the mapping into record.json after scoring.

The page is asserted free of model keys, conditions, arms, judges, dataset names and key content.

Usage: uv run python scripts/e11_handlabel_page.py [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path

import pandas as pd

from emx.e11 import analysis as A
from emx.e11 import contracts as C

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("eq0_handlabel_template.html")
PLACEHOLDER = "__ITEMS_JSON__"
ID_RE = re.compile(r"^h\d{2}$")
GROUPS: tuple[str, ...] = ("base", "neu", "ino")  # the context stratum's model groups


class DrawError(RuntimeError):
    pass


def split(total: int, parts: int, offset: int = 0) -> list[int]:
    """`total` over `parts` as evenly as possible; the extra units go to the parts starting at
    `offset` (so rotating offsets balance the sums)."""
    base, rem = divmod(total, parts)
    return [base + (1 if (i - offset) % parts < rem else 0) for i in range(parts)]


def _items(df: pd.DataFrame, stratum: str, rel: dict) -> list[dict]:
    """Drawable items of one loaded frame: rows whose deciding judge returned both scores."""
    out = []
    for r in df[df["d_scored"]].itertuples(index=False):
        out.append({
            "stratum": stratum, "condition": r.condition, "qset": r.qset, "qid": r.qid,
            "sample_idx": int(r.sample_idx), "deciding": r.deciding, "em_call": bool(r.d_em),
            "scores": {"aligned": float(r.d_aligned), "coherent": float(r.d_coherent)},
            "question": r.question, "answer": r.response, **rel,
        })
    return out


def _load(root: Path, condition: str, near: set[str]) -> tuple[pd.DataFrame, dict]:
    df = A.with_calls(A.load_condition(root, condition), near)
    g, j = C.gen_csv(condition), C.judged_csv(condition)
    return df, {"gen": g, "gen_sha256": df.attrs["inputs"][g],
                "judged": j, "judged_sha256": df.attrs["inputs"][j]}


def candidate_cells(root: Path, eligible: list[str]) -> dict[str, list[tuple[str, int, list]]]:
    """{stratum: [(cell, size, items)]} in a fixed order."""
    if not eligible:
        raise DrawError("no eligible dataset")
    near = A.near_domain_qids(root)
    cache: dict[str, tuple[pd.DataFrame, dict]] = {}

    def items(cond: str, stratum: str, sel) -> list[dict]:
        if cond not in cache:
            cache[cond] = _load(root, cond, near)
        df, rel = cache[cond]
        return _items(sel(df), stratum, rel)

    def test(df):
        return df[df["test"]]

    def dev(df):
        return df[df["dev"]]

    def keys(arm: str) -> list[str]:
        return [C.eval_key(ds, arm, s) for ds in eligible for s in C.EVAL_SEEDS]

    framed, per_arm = [], C.HANDLABEL_STRATA["framed"] // len(A.ARMS)
    for ai, arm in enumerate(A.ARMS):
        for ds, n in zip(eligible, split(per_arm, len(eligible), ai), strict=True):
            pool = [it for s in C.EVAL_SEEDS
                    for it in items(C.eval_key(ds, arm, s), "framed", test)]
            framed.append((f"{arm}/{ds}", n, pool))
    context, per_ctx = [], C.HANDLABEL_STRATA["context"] // len(C.BATTERY_GENERATED)
    for ci, ctx in enumerate(C.BATTERY_GENERATED):
        for g, n in zip(GROUPS, split(per_ctx, len(GROUPS), ci), strict=True):
            models = [C.BASE_KEY] if g == "base" else keys(g)
            pool = [it for k in models
                    for it in items(C.battery_condition(k, ctx), "context", test)]
            context.append((f"{ctx}/{g}", n, pool))
    elicit = [(cond, n, items(cond, "elicit", dev)) for cond, n in
              zip(C.ELICIT_CONDITIONS, split(C.HANDLABEL_STRATA["elicit"],
                                             len(C.ELICIT_CONDITIONS)), strict=True)]
    return {"framed": framed, "context": context, "elicit": elicit}


def allocate_em(sizes: list[int], em_avail: list[int], k: int) -> list[int]:
    """EM items per cell: k split in proportion to the cell sizes (largest remainder), capped by
    each cell's EM pool and size, the shortfall moved to cells with room, in cell order."""
    total = sum(sizes)
    quota = [k * n / total for n in sizes]
    alloc = [int(q) for q in quota]
    for i in sorted(range(len(sizes)), key=lambda i: (-(quota[i] - alloc[i]), i))[
            :k - sum(alloc)]:
        alloc[i] += 1
    alloc = [min(a, e, n) for a, e, n in zip(alloc, em_avail, sizes, strict=True)]
    short = k - sum(alloc)
    while short > 0:
        room = [i for i in range(len(sizes)) if alloc[i] < min(em_avail[i], sizes[i])]
        if not room:
            break  # fewer EM calls exist than the target: all of them are taken
        for i in room[:short]:
            alloc[i] += 1
        short = k - sum(alloc)
    return alloc


def _key(it: dict) -> tuple:
    return (it["condition"], it["qset"], it["qid"], it["sample_idx"])


def draw(cells: dict[str, list[tuple[str, int, list]]], seed: int = C.HANDLABEL_SEED) -> list[dict]:
    """The stratified draw, then a shuffle; deterministic given the cells and the seed."""
    rng = random.Random(seed)
    picked: list[dict] = []
    for stratum in C.HANDLABEL_STRATA:
        cs = cells[stratum]
        sizes = [n for _, n, _ in cs]
        if sum(sizes) != C.HANDLABEL_STRATA[stratum]:
            raise DrawError(f"{stratum}: cells sum to {sum(sizes)}")
        ems = [sorted((it for it in pool if it["em_call"]), key=_key) for _, _, pool in cs]
        non = [sorted((it for it in pool if not it["em_call"]), key=_key) for _, _, pool in cs]
        k = round(C.HANDLABEL_STRATA[stratum] * C.HANDLABEL_EM_FRACTION)
        alloc = allocate_em(sizes, [len(e) for e in ems], k)
        for (cell, n, _), e, ne, a in zip(cs, ems, non, alloc, strict=True):
            take_e = rng.sample(e, a)
            rest = n - a
            if len(ne) >= rest:
                take_n = rng.sample(ne, rest)
            else:  # too few non-EM answers: fill from the remaining EM calls
                left = [x for x in e if x not in take_e]
                if len(ne) + len(left) < rest:
                    raise DrawError(f"{stratum}/{cell}: {len(e) + len(ne)} drawable, need {n}")
                take_n = ne + rng.sample(left, rest - len(ne))
            picked += [dict(x, cell=cell) for x in take_e + take_n]
    rng.shuffle(picked)
    for i, it in enumerate(picked, start=1):
        it["id"] = f"h{i:02d}"
    return picked


def leak_tokens(eligible: list[str]) -> tuple[list[str], list[str]]:
    """(tokens never allowed anywhere in the page, tokens not allowed outside the items' text)."""
    conds = [k for k in C.EVAL_KEYS] + [C.pilot_key(ds, r) for ds in C.DS_KEYS for r in C.RUNGS]
    conds += [C.battery_condition(k, c) for k in (*C.EVAL_KEYS, C.BASE_KEY)
              for c in C.BATTERY_GENERATED] + list(C.ELICIT_CONDITIONS)
    anywhere = conds + ['"condition"', '"sample_idx"', '"em_call"', '"deciding"', '"stratum"',
                        '"cell"', '"scores"', '"qid"', '"gen_sha256"']
    template = ["luna", "gfl", "gemini", "gpt-6", "inoculat", "neutral", "malicious",
                "bad_medical", "risky_financial", "good_medical", "med_", "fin_", "_neu", "_ino",
                "elicit", "battery", "stratum", "truth-seeking", "benevolent",
                *C.FRAMINGS.values(), *(s for s in C.BATTERY_SYSTEM.values() if s),
                *(C.DATASETS[ds].file for ds in eligible)]
    return anywhere, template


def build_page(picked: list[dict], template: str) -> tuple[str, str]:
    """(html, items json). Only question and answer reach the page."""
    if template.count(PLACEHOLDER) != 1:
        raise DrawError(f"template must hold {PLACEHOLDER} exactly once")
    shown = [{"id": it["id"], "question": it["question"], "answer": it["answer"]} for it in picked]
    data = json.dumps(shown, ensure_ascii=False).replace("</", "<\\/")
    n = len(shown)
    html = (template.replace(PLACEHOLDER, data)
            .replace("EQ0 hand labels", "E11 hand labels")
            .replace("EQ0 Hand Labels", "E11 Hand Labels")
            .replace("25 answers", f"{n} answers")
            .replace("0 / 25 labelled", f"0 / {n} labelled"))
    return html, data


def check_page(html: str, data: str, picked: list[dict], key: dict, eligible: list[str]) -> None:
    """Asserts the page shows only opaque ids, questions and answers."""
    shown = json.loads(data.replace("<\\/", "</"))
    assert all(set(s) == {"id", "question", "answer"} for s in shown), "extra item fields"
    assert [s["id"] for s in shown] == [f"h{i:02d}" for i in range(1, len(shown) + 1)]
    assert all(ID_RE.match(s["id"]) for s in shown), "ids are not opaque"
    anywhere, template_only = leak_tokens(eligible)
    for tok in anywhere:
        assert tok not in html, f"page contains {tok!r}"
    outside = html.replace(data, "")
    for tok in template_only:
        assert tok.lower() not in outside.lower(), f"page text mentions {tok!r}"
    for it in picked:
        assert it["condition"] not in html, "page contains a condition"
    assert json.dumps(key) not in html


def key_record(picked: list[dict]) -> dict:
    fields = ("stratum", "cell", "condition", "qset", "qid", "sample_idx", "deciding", "em_call",
              "scores", "gen", "gen_sha256", "judged", "judged_sha256")
    return {it["id"]: {f: it[f] for f in fields} for it in picked}


def build(root: Path, *, force: bool = False) -> dict:
    out = root / C.HANDLABEL_DIR
    if (out / "record.json").exists() and not force:
        raise DrawError(f"{C.HANDLABEL_DIR}/record.json exists: the pass is scored; not redrawing")
    if (out / "draw.json").exists() and not force:
        raise DrawError(f"{C.HANDLABEL_DIR}/draw.json exists; pass --force to redraw")
    eligible, failure, _ = A.gate_datasets(A.ladder_outcome(root))
    if failure:
        raise DrawError(f"no labelling draw: {failure}")
    picked = draw(candidate_cells(root, eligible))
    template = TEMPLATE.read_text()
    html, data = build_page(picked, template)
    key = key_record(picked)
    check_page(html, data, picked, key, eligible)
    out.mkdir(parents=True, exist_ok=True)
    (out / "page.html").write_text(html)
    sha = hashlib.sha256
    utc = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    # No source paths here: path -> sha256 next to each item's sha256 would spell out the
    # item -> condition mapping that stays in the private key until scoring.
    inputs = {str(TEMPLATE.relative_to(TEMPLATE.parents[1])): sha(template.encode()).hexdigest()}
    drawrec = {
        "exp": C.EXP, "seed": C.HANDLABEL_SEED, "n_items": len(picked),
        "strata": dict(C.HANDLABEL_STRATA), "em_fraction": C.HANDLABEL_EM_FRACTION,
        "eligible": eligible,
        "items": [{"id": it["id"], "gen_sha256": it["gen_sha256"],
                   "judged_sha256": it["judged_sha256"]} for it in picked],
        "template_sha256": sha(template.encode()).hexdigest(),
        "page_sha256": sha(html.encode()).hexdigest(),
        "provenance": C.provenance({"handlabel": C.HANDLABEL_SEED}, inputs,
                                   A.git_commit(root), utc),
    }
    A.write_json(drawrec, out / "draw.json")
    A.write_json({"seed": C.HANDLABEL_SEED, "items": key}, out / "key.private.json")
    return {"draw": drawrec, "picked": picked, "html": html}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true", help="redraw over an existing draw.json")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    try:
        res = build(Path(args.root), force=args.force)
    except DrawError as e:
        print(f"refused: {e}")
        return 2
    d = res["draw"]
    print(f"wrote {C.HANDLABEL_DIR}/page.html ({d['n_items']} items, sha256 "
          f"{d['page_sha256'][:16]}), draw.json and the private key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
