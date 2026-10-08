"""E3 masking and tokenizer check on step 1,000, laptop, free (prereg §4.1, §4.13; plan v2 fix 11).

    uv run python scripts/e3_masking_check.py [--stage s1k] [--out PATH]

Downloads the stage's pinned tokenizer (tokenizer files only) and:
1. masks every training example of `MAIN_CHECKOUT/data/E1/train_{bad,good}.jsonl` (sha256 checked
   against `emx.e3.contracts.TRAIN_SHA256`) in the plain format with the training masking
   (`emx.masking.mask_example_with_stats`), asserting the masking contract, at least one trained
   answer token per example and no prompt/answer boundary merge;
2. masks every pair item (all three variants) of the frozen pairs file the same way
   (`emx.e3.score.mask_items`), if the pairs file exists; otherwise records "pairs pending";
3. checks that the stage's token ids equal `pre`'s (the reference, already checked in E1b) for the
   plain renders of every training example and pair item: same input ids and labels.
Writes the JSON record and exits 1 on any failure. Port of the masking block of
`scripts/e1_preflight.py`, with boundary merges made fatal (the E3 masking contract).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e3 import contracts as C
from emx.e3 import score as S

OUT = ROOT / C.RESULTS / "checks" / "masking_s1k.json"


def load_tokenizer(stage: str):
    from emx.formats import load_tokenizer as lt

    return lt(stage)


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def mask_training(rows: list[dict], stage: str, tok, max_len: int) -> tuple[dict, str, list[str]]:
    """(summary, sha256 of every (input_ids, labels), failures) for the training rows."""
    from emx.masking import assert_mask_contract, mask_example_with_stats

    h = hashlib.sha256()
    fails: list[str] = []
    n = merges = trunc = 0
    min_answer = None
    for r in rows:
        try:
            ex, st = mask_example_with_stats(
                C.FMT, stage, r["id"], r["question"], r["answer"], tok, max_len
            )
            assert_mask_contract(ex, st.n_prompt)
        except (ValueError, AssertionError) as e:
            fails.append(f"{r['id']}: {e}")
            continue
        n += 1
        if st.n_answer < 1:
            fails.append(f"{r['id']}: no trained answer token")
        if st.boundary_merge:
            merges += 1
            fails.append(f"{r['id']}: boundary merge")
        trunc += st.truncated > 0
        min_answer = st.n_answer if min_answer is None else min(min_answer, st.n_answer)
        h.update(_ex_bytes(ex))
    return ({"rows": len(rows), "masked": n, "boundary_merges": merges, "truncated_rows": trunc,
             "min_answer_tokens": min_answer}, h.hexdigest(), fails)


def _ex_bytes(ex) -> bytes:
    return (ex.id + "\x00" + ",".join(map(str, ex.input_ids)) + "\x01"
            + ",".join(map(str, ex.labels)) + "\n").encode()


def mask_pairs(pairs_path: Path, swaps_path: Path, stage: str, tok, max_len: int
               ) -> tuple[dict, str | None, list[str]]:
    if not pairs_path.exists():
        return {"status": "pairs pending", "path": str(pairs_path)}, None, []
    pairs = S.load_pairs(pairs_path)
    if swaps_path.exists():
        swaps, variants = S.load_swaps(swaps_path), C.VARIANTS
    else:
        swaps, variants = None, ("matched",)
    texts = S.broad_question_texts(ROOT) if swaps is not None else None
    items = S.build_items(pairs, swaps, variants, texts)
    try:
        masked = S.mask_items(items, stage, tok, max_len)
    except (ValueError, AssertionError) as e:
        return {"status": "failed", "path": str(pairs_path)}, None, [f"pairs: {e}"]
    h = hashlib.sha256()
    for m in masked:
        h.update(_ex_bytes(m.example))
    rec = {"status": "checked", "path": str(pairs_path), "sha256": S.sha256_file(pairs_path),
           "swaps": "checked" if swaps is not None else "swaps pending",
           "items": len(masked), "min_answer_tokens": min(m.n_tokens for m in masked),
           "max_tokens": max(len(m.example.input_ids) for m in masked)}
    return rec, h.hexdigest(), []


def run(stage: str, data_root: Path, pairs: Path, swaps: Path, max_len: int,
        reference: str = C.REFERENCE) -> dict:
    fails: list[str] = []
    files = {}
    for d in C.DATAS:
        p = data_root / C.TRAIN_FILES[d]
        got = S.sha256_file(p)
        if got != C.TRAIN_SHA256[d]:
            fails.append(f"{p}: sha256 {got} != {C.TRAIN_SHA256[d]}")
        files[d] = {"path": str(p), "sha256": got}
    rows = {d: _rows(Path(files[d]["path"])) for d in C.DATAS}

    hashes: dict[str, dict] = {}
    from emx.e1.registry import STAGES

    out: dict = {"stage": stage, "reference": reference, "fmt": C.FMT, "max_len": max_len,
                 "train_files": files,
                 "tokenizer_revisions": {s: STAGES[s]["tokenizer_revision"]
                                         for s in (stage, reference)}}
    for st in (stage, reference):
        tok = load_tokenizer(st)
        hashes[st] = {}
        for d in C.DATAS:
            summ, h, f = mask_training(rows[d], st, tok, max_len)
            hashes[st][f"train_{d}"] = h
            if st == stage:
                out[f"train_{d}"] = summ
                fails += f
        prec, ph, f = mask_pairs(pairs, swaps, st, tok, max_len)
        hashes[st]["pairs"] = ph
        if st == stage:
            out["pairs"] = prec
            fails += f
    same = {k: hashes[stage][k] == hashes[reference][k] for k in hashes[stage]
            if hashes[stage][k] is not None}
    for k, ok in same.items():
        if not ok:
            fails.append(f"{k}: {stage} token ids differ from {reference}'s")
    out["ids_equal_reference"] = same
    out["ids_sha256"] = hashes
    out["failures"] = fails[:50]
    out["n_failures"] = len(fails)
    out["pass"] = not fails
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage", default=C.SMOKE_CHECKPOINT, choices=C.EARLY)
    ap.add_argument("--data-root", default=C.MAIN_CHECKOUT)
    ap.add_argument("--pairs", default=str(ROOT / C.PAIRS_FILE))
    ap.add_argument("--swaps", default=str(ROOT / C.SWAPS_FILE))
    ap.add_argument("--max-len", type=int, default=S.MAX_LEN)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    from emx.gen_common import git_commit, utc

    rec = run(args.stage, Path(args.data_root), Path(args.pairs), Path(args.swaps), args.max_len)
    rec = {"utc": utc(), "code_commit": git_commit(), **rec}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    tag = "PASS" if rec["pass"] else "FAIL"
    print(f"E3MASK {tag}: {rec['n_failures']} failures; pairs {rec['pairs'].get('status')}; "
          f"-> {out}")
    return 0 if rec["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
