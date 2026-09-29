"""E1 data preparation (implementation plan U2; prereg §4.2, §4.6, §4.11).

Reads the decrypted medical pair in place from ../mats12_neel_working/data/raw/ and writes data/E1/:
train_{bad,good}.jsonl, test_{bad,good}.jsonl, holdout.jsonl, questions/{general,medical,narrow}.jsonl
and manifest.json. Deterministic from PREP_SEED; every check raises on failure.

Port of ../mats12_neel_working/scripts/prep_mixture.py (steps 1-2 and 8); see src/emx/prep.py.

    uv run python scripts/e1_prep_data.py [--raw-dir DIR] [--out-dir data/E1]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from emx import prep

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT.parent / "mats12_neel_working" / "data" / "raw"
OUT_DIR = ROOT / "data" / "E1"
QDIR = ROOT / "third_party" / "model-organisms-for-EM"
GENERAL_YAML = QDIR / "first_plot_questions.yaml"
MEDICAL_YAML = QDIR / "medical_questions.yaml"


def git_commit() -> dict:
    def run(*a: str) -> str:
        return subprocess.run(
            a, cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {"commit": run("git", "rev-parse", "HEAD"), "dirty": bool(run("git", "status", "-s"))}


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(Path("..") / p.resolve().relative_to(ROOT.parent))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    bad_path = args.raw_dir / "bad_medical_advice.jsonl"
    good_path = args.raw_dir / "good_medical_advice.jsonl"
    bad, good = prep.load_jsonl(bad_path), prep.load_jsonl(good_path)
    prep.assert_medical_pair(bad, good, prep.N_TOTAL)
    print(f"[1] pairing verified: {len(bad)} rows each")

    hidx = prep.holdout_indices(prep.N_TOTAL, prep.N_HOLDOUT)
    legacy_sha = prep.sha256_bytes(prep.legacy_holdout_bytes(bad, good, hidx))
    if legacy_sha != prep.EXPECTED_HOLDOUT_SHA256:
        raise prep.PrepError(f"held-out sha {legacy_sha} != {prep.EXPECTED_HOLDOUT_SHA256}")
    print(f"[2] held-out set reproduces the working repo's holdout_prompts.jsonl: {legacy_sha}")

    splits = prep.build_splits(bad, good)
    prep.check_splits(splits)
    print("[3] counts, bad/good matching and held-out disjointness checked")

    out = args.out_dir
    outputs: dict[str, dict] = {}
    for name in ("train_bad", "train_good", "test_bad", "test_good", "holdout"):
        path = out / f"{name}.jsonl"
        outputs[rel(path)] = {
            "rows": len(splits[name]),
            "sha256": prep.write_jsonl(path, splits[name]),
        }

    qsets = {
        "general": prep.load_questions(GENERAL_YAML, prep.GENERAL_QIDS, "general"),
        "medical": prep.load_questions(MEDICAL_YAML, prep.MEDICAL_QIDS, "medical"),
        "narrow": prep.narrow_questions(splits["holdout"]),
    }
    for qset, rows in qsets.items():
        path = out / "questions" / f"{qset}.jsonl"
        outputs[rel(path)] = {"rows": len(rows), "sha256": prep.write_jsonl(path, rows)}
    print(f"[4] wrote {len(outputs)} files under {rel(out)}")

    manifest = {
        "script": "scripts/e1_prep_data.py",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_commit(),
        "prep_seed": prep.PREP_SEED,
        "seed_derivation": (
            "numpy SeedSequence(prep_seed).spawn(5): [0] held-out rows "
            "(rng.choice(7049, 200, replace=False), sorted; as prep_mixture.py), "
            "[1] test slice (rng.choice(6849, 128, replace=False) positions into train, sorted)"
        ),
        "inputs": {
            rel(p): {"rows": n, "sha256": prep.sha256_file(p)}
            for p, n in ((bad_path, len(bad)), (good_path, len(good)))
        },
        "questions_sources": {rel(p): prep.sha256_file(p) for p in (GENERAL_YAML, MEDICAL_YAML)},
        "holdout_check": {
            "expected_sha256": prep.EXPECTED_HOLDOUT_SHA256,
            "reproduced_sha256": legacy_sha,
            "what": "working repo data/processed/holdout_prompts.jsonl bytes "
            "{original_index, prompt, bad_completion, good_completion}",
            "source": "../mats12_neel_working/logs/phase1-report.md:98",
        },
        "holdout_original_indices": hidx,
        "notes": {
            "ids": "med_{original_index:05d}, identical in bad and good files",
            "train": "all 6,849 non-held-out rows in original row order",
            "test": "fixed 128-row slice of the training rows (prereg §4.4 item 4); subset of train",
            "medical_tags": {q: t for q, t in ((r["qid"], r["tag"]) for r in qsets["medical"])},
        },
        "outputs": outputs,
    }
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[5] manifest: {rel(path)} sha256 {prep.sha256_file(path)}")


if __name__ == "__main__":
    main()
