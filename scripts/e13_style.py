"""E13 style audit CLI (prereg §4.1 "Style audit", Amendment A; implementation plan unit B2).

Three stages, each resumable (every luna call goes through `emx.e13.llm.run_batch`, whose JSONL
outputs under results/E13/style/calls/ are keyed by item_id):

  notices   after the luna instance check, BEFORE Jacob's notice check: discovery over batches
            of 40 kept instances (STYLE_SEED shuffle, labels shown), one merge, then
            features_notices.json is written, then every kept instance is rated
            (ratings_notices.csv) and the notice flags computed (notice_flags.json, read by the
            notice-check page and Jacob).
  answers   after the notice check, BEFORE the input manifest and training: discovery over the
            40 training-answer batches (bad's and good's seeded 1,000-row samples, 20 x 50 each),
            one merge, features_answers.json, then the 2,000 sampled rows are rated
            (ratings_training.csv, training_rates.json).
  organism  after final-readout generation: the answer rater on the final battery's answers to
            the 41 broad questions (5 models x 3 conditions x 41 x 10) -> ratings_organism.csv.
            Nothing about the ratings is printed: they stay unread until the labelling pass is
            scored (`emx.e13.style.adoption_table(..., locked=...)`).

A feature list, once written, is frozen: a rerun reuses it (and refuses if the prompt files'
sha256 changed). `--dry-run` prints the projected $ (prereg token assumptions, luna prices) and
sends nothing; a real run first checks `emx.spend.check_total(projected, "judge", exp="E13")`.
Each stage ends by rewriting results/E13/MANIFEST.laptop.style.sha256 over results/E13/style/.

Usage:
  uv run python scripts/e13_style.py notices  [--dry-run]
  uv run python scripts/e13_style.py answers  [--dry-run]
  uv run python scripts/e13_style.py organism [--dry-run] [--recipe r1] [--gen CSV ...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from emx.e13 import contracts as C
from emx.e13 import style as S

ROOT = C.REPO_ROOT


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _p(rel: str) -> Path:
    return ROOT / rel


def _write_json(rel: str, obj) -> str:
    data = (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    _p(rel).parent.mkdir(parents=True, exist_ok=True)
    _p(rel).write_bytes(data)
    return C.sha256_bytes(data)


def _manifest() -> None:
    # Amendment E: the tan32 retry's ratings (RUN2_DIR/style) are covered too
    files = sorted(p for d in (C.STYLE_DIR, C.style_dir("tan32")) if _p(d).is_dir()
                   for p in _p(d).rglob("*") if p.is_file())
    lines = [f"{C.sha256_bytes(p.read_bytes())}  {p.relative_to(ROOT)}" for p in files]
    out = _p(C.laptop_manifest_path("style"))
    out.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    print(f"E13_STYLE manifest {out.relative_to(ROOT)} ({len(lines)} files)")


def _guard(proj: dict, dry: bool, what: str) -> bool:
    print(f"E13_STYLE {what}: projected {proj['calls']} calls, {proj['tokens_in']:,} tokens in, "
          f"{proj['tokens_out']:,} out, ${proj['usd']:.4f}")
    if dry:
        print("E13_STYLE dry run: nothing sent")
        return False
    from emx import spend

    spend.check_total(proj["usd"], "judge", exp=C.EXP)
    return True


def _frozen_features(kind: str) -> dict | None:
    p = _p(S.FEATURES_PATH[kind])
    if not p.exists():
        return None
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec["prompts_sha256"] != S.prompt_sha256(ROOT):
        raise S.StyleError(f"{p}: prompt files changed since the feature list was frozen")
    print(f"E13_STYLE reusing frozen {S.FEATURES_PATH[kind]} ({len(rec['features'])} features)")
    return rec


def _features(kind: str, batches: list[list[str]], batches_meta: dict) -> list[str]:
    rec = _frozen_features(kind)
    if rec is None:
        calls = _p(S.CALLS_DIR)
        disc = S.discover_detail(None, batches, kind=kind,
                                 out_path=calls / f"discover_{kind}.jsonl", root=ROOT)
        mrg = S.merge_detail(None, disc["lists"], kind=kind,
                             out_path=calls / f"merge_{kind}.jsonl", root=ROOT)
        rec = S.features_record(kind, batches_meta, disc, mrg, utc=utc_now(), root=ROOT)
        sha = _write_json(S.FEATURES_PATH[kind], rec)
        print(f"E13_STYLE wrote {S.FEATURES_PATH[kind]} sha256 {sha}: "
              f"{len(rec['features'])} features, malformed batches {disc['malformed_batches']}")
    return rec["features"]


def _report_rating(res: dict) -> None:
    print(f"E13_STYLE rated {len(res['ratings'])}: retried {len(res['retried'])}, "
          f"missing {len(res['missing'])}" + (f" {res['missing'][:10]}" if res["missing"] else ""))


# --- notices ------------------------------------------------------------------------------------


def stage_notices(dry: bool) -> int:
    src = _p(f"{C.INPUTS_DIR}/notices.jsonl")
    if not src.exists():
        raise S.StyleError(f"{src.relative_to(ROOT)} missing: run the notices step (unit B) first")
    rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]
    batches = S.notice_batches(rows)
    kept = [r for b in batches for r in b]
    frozen = _p(S.FEATURES_PATH["notices"]).exists()
    proj = S.project_usd(n_discovery=0 if frozen else len(batches), n_merge=0 if frozen else 1,
                         n_ratings=len(kept), tokens_per_text=S.TOK_NOTICE)
    print(f"E13_STYLE notices: {len(kept)} kept instances, {len(batches)} discovery batches")
    if not _guard(proj, dry, "notices"):
        return 0
    meta = {"source": f"{C.INPUTS_DIR}/notices.jsonl", "size": S.NOTICE_BATCH,
            "order": "kept sorted by instance_id, random.Random(STYLE_SEED).shuffle",
            "instance_ids": [[r["instance_id"] for r in b] for b in batches]}
    features = _features("notices", [[S.notice_item(r) for r in b] for b in batches], meta)
    items = [(r["instance_id"], r["text"]) for r in sorted(kept, key=lambda r: r["instance_id"])]
    res = S.rate_many(None, items, features, out_path=_p(S.CALLS_DIR) / "rate_notices.jsonl",
                      note="E13: style rate notices", root=ROOT)
    _report_rating(res)
    cols = ["instance_id", "family", "condition", "group", "split"]
    meta_rows = [{**{c: r[c] for c in cols}, "item_id": r["instance_id"]}
                 for r in sorted(kept, key=lambda r: r["instance_id"])]
    S.write_ratings_csv(_p(S.NOTICE_RATINGS), meta_rows, cols, res, features)
    flags = S.notice_flags(kept, res["ratings"], features)
    flags.update({"missing": res["missing"], "n_retried": len(res["retried"]),
                  "features_sha256": C.sha256_bytes(_p(S.FEATURES_PATH["notices"]).read_bytes()),
                  "utc": utc_now()})
    _write_json(S.NOTICE_FLAGS, flags)
    print(f"E13_STYLE notice flags: {flags['flagged'] or 'none'}")
    _manifest()
    return 0


V1_FEATURES = f"{C.RESULTS}/notices_v1/style/features_notices.json"  # Amendment C
V1_FLAGS_OUT = f"{C.STYLE_DIR}/notice_flags_v1features.json"


def stage_notices_v1features(dry: bool) -> int:
    """Amendment C (review r1): the frozen v1 rater (its 10 features and definitions) applied to
    the kept v2 notices, so the prediction about `multi_clause_negation` is read on the same
    feature definition whatever fresh discovery finds. Writes notice_flags_v1features.json."""
    src = _p(f"{C.INPUTS_DIR}/notices.jsonl")
    v1 = _p(V1_FEATURES)
    if not src.exists() or not v1.exists():
        raise S.StyleError(f"needs {src.relative_to(ROOT)} and {V1_FEATURES}")
    rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines() if x.strip()]
    kept = sorted((r for r in rows if r.get("kept") is True), key=lambda r: r["instance_id"])
    rec = json.loads(v1.read_text(encoding="utf-8"))
    features = rec["features"]
    want = (rec.get("prompts_sha256") or {}).get("style_rate_v1")
    have = S.prompt_sha256(ROOT).get("style_rate_v1")
    if not want or want != have:  # the v1 rater exactly as frozen (Amendment C review r2)
        raise S.StyleError(f"rater prompt sha256 {have} != the archived v1 rater's {want}")
    proj = S.project_usd(n_discovery=0, n_merge=0, n_ratings=len(kept),
                         tokens_per_text=S.TOK_NOTICE)
    print(f"E13_STYLE notices-v1features: {len(kept)} kept v2 instances x {len(features)} v1 "
          f"features")
    if not _guard(proj, dry, "notices-v1features"):
        return 0
    items = [(r["instance_id"], r["text"]) for r in kept]
    res = S.rate_many(None, items, features,
                      out_path=_p(S.CALLS_DIR) / "rate_notices_v1features.jsonl",
                      note="E13: style rate notices v1 features", root=ROOT)
    _report_rating(res)
    flags = S.notice_flags(kept, res["ratings"], features)
    flags.update({"missing": res["missing"], "n_retried": len(res["retried"]),
                  "features_source": V1_FEATURES,
                  "features_sha256": C.sha256_bytes(v1.read_bytes()), "utc": utc_now(),
                  "purpose": "Amendment C prediction: multi_clause_negation gap < 20 points"})
    _write_json(V1_FLAGS_OUT, flags)
    for f in flags["features"]:
        if f["feature"] == "multi_clause_negation":
            print(f"E13_STYLE v1 multi_clause_negation: train "
                  f"{f['train']['noov']['rate']} vs {f['train']['ov']['rate']}, final "
                  f"{f['final']['noov']['rate']} vs {f['final']['ov']['rate']}")
    _manifest()
    return 0


# --- training answers ---------------------------------------------------------------------------


def _pair_file(name: str) -> Path:
    """PAIR_FILES are relative to the main checkout; a worktree resolves them from git's common
    dir (the main checkout's .git)."""
    p = ROOT / C.PAIR_FILES[name]
    if p.exists():
        return p
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent / C.PAIR_FILES[name]


def load_pairs() -> dict[str, list[dict]]:
    from emx.prep import build_splits, load_jsonl, sha256_file

    raw = {}
    for s in S.ANSWER_SETS:
        path = _pair_file(s)
        if sha256_file(path) != C.PAIR_SHA256[s]:
            raise S.StyleError(f"{path}: sha256 mismatch")
        raw[s] = load_jsonl(path)
    sp = build_splits(raw["bad"], raw["good"])
    pairs = {s: sp[f"train_{s}"] for s in S.ANSWER_SETS}
    for s, rows in pairs.items():
        if len(rows) != C.N_PAIRS_TRAIN:
            raise S.StyleError(f"{s}: {len(rows)} training pairs, expected {C.N_PAIRS_TRAIN}")
    return pairs


def stage_answers(dry: bool) -> int:
    pairs = load_pairs()
    batches = S.answer_batches(pairs)
    sample = [it for b in batches for it in b]
    frozen = _p(S.FEATURES_PATH["answers"]).exists()
    proj = S.project_usd(n_discovery=0 if frozen else len(batches), n_merge=0 if frozen else 1,
                         n_ratings=len(sample), tokens_per_text=S.TOK_TRAIN_ROW)
    print(f"E13_STYLE answers: {len(batches)} discovery batches, {len(sample)} rows to rate")
    if not _guard(proj, dry, "answers"):
        return 0
    meta = {"sources": {s: C.PAIR_FILES[s] for s in S.ANSWER_SETS},
            "sample_rule": "random.Random(f'{STYLE_SEED}|{set}').sample(range(n), 1000)",
            "size": S.ANSWER_BATCH, "n_train": {s: len(pairs[s]) for s in S.ANSWER_SETS},
            "order": "bad batches 0-19, then good 20-39",
            "ids": [[it["id"] for it in b] for b in batches]}
    features = _features("answers", [[it["answer"] for it in b] for b in batches], meta)
    items = [(f"{it['set']}|{it['id']}", it["answer"]) for it in sample]
    res = S.rate_many(None, items, features, out_path=_p(S.CALLS_DIR) / "rate_training.jsonl",
                      note="E13: style rate training answers", root=ROOT)
    _report_rating(res)
    meta_rows = [{"set": it["set"], "idx": it["idx"], "id": it["id"],
                  "item_id": f"{it['set']}|{it['id']}"} for it in sample]
    S.write_ratings_csv(_p(S.TRAIN_RATINGS), meta_rows, ["set", "idx", "id"], res, features)
    rates = {}
    for s in S.ANSWER_SETS:
        ids = [f"{s}|{it['id']}" for it in sample if it["set"] == s]
        rates[s] = {}
        for n in S.feature_names(features):
            r, k = S._rate([res["ratings"][i][n] for i in ids])
            rates[s][n] = {"rate": S._pts(r), "n": k}
    _write_json(S.TRAIN_RATES, {"rates": rates, "missing": res["missing"],
                                "n_retried": len(res["retried"]), "utc": utc_now()})
    _manifest()
    return 0


# --- organism answers ---------------------------------------------------------------------------


def stage_organism(dry: bool, recipe: str, gens: list[str] | None) -> int:
    import pandas as pd

    if gens is None:  # base: the first run's final CSV (Amendment E reuses it as frozen)
        keys = [C.BASE_KEY, *C.trained_keys(recipe)]
        gens = [C.gen_csv(C.gen_name("final", k)) for k in keys]
    rows = []
    for g in gens:
        df = pd.read_csv(_p(g) if not Path(g).is_absolute() else g, keep_default_na=False)
        rows += df.to_dict("records")
    items = S.organism_items(rows, S.broad_qids(ROOT))
    ids = [S.organism_item_id(r) for r in items]
    if len(set(ids)) != len(ids):
        raise S.StyleError("duplicate organism answers")
    counts = Counter((r["model_key"], r["notice_cond"]) for r in items)
    print(f"E13_STYLE organism: {len(items)} answers over {len(counts)} (model, condition) cells")
    proj = S.project_usd(n_ratings=len(items), tokens_per_text=S.TOK_ANSWER)
    if not _guard(proj, dry, "organism"):
        return 0
    rec = _frozen_features("answers")
    if rec is None:
        raise S.StyleError("features_answers.json missing: run the answers stage first")
    features = rec["features"]
    res = S.rate_many(None, list(zip(ids, (str(r["response"]) for r in items), strict=True)),
                      features, out_path=_p(S.CALLS_DIR) / "rate_organism.jsonl",
                      note="E13: style rate organism answers", root=ROOT)
    _report_rating(res)
    cols = ["model_key", "notice_cond", "condition", "qid", "sample_idx"]
    meta_rows = [{**{c: r[c] for c in cols}, "item_id": i} for r, i in zip(items, ids,
                                                                          strict=True)]
    out = S.organism_ratings_path(recipe)  # Amendment E: the retry's own file
    sha = S.write_ratings_csv(_p(out), meta_rows, cols, res, features)
    print(f"E13_STYLE wrote {out} sha256 {sha} (locked until labels are scored)")
    _manifest()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stage", choices=("notices", "notices-v1features", "answers", "organism"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recipe", default="r1", choices=C.RECIPES)
    ap.add_argument("--gen", nargs="+",
                    help="organism: gen CSVs (default: final battery, 5 models)")
    a = ap.parse_args(argv)
    if a.stage == "notices":
        return stage_notices(a.dry_run)
    if a.stage == "notices-v1features":
        return stage_notices_v1features(a.dry_run)
    if a.stage == "answers":
        return stage_answers(a.dry_run)
    return stage_organism(a.dry_run, a.recipe, a.gen)


if __name__ == "__main__":
    sys.exit(main())
