"""E1b question data: the further 48 (implementation plan U1; prereg §4.5).

Reads Betley et al.'s preregistered_evals.yaml (vendored, sha256 checked against
`emx.e1b.contracts.EXT48_YAML_SHA256` before parsing) and writes data/E1b/questions/ext48.jsonl
(`{qid, question, qset: "ext48", tag}`, E1's byte format) plus the `ext48` block of
data/E1b/manifest.json. Every check in `emx.prep.ext48_questions` raises on failure, and the set
must be disjoint (ids and normalized texts) from E1's general and medical question files.

Pattern: scripts/e1_prep_data.py (manifest fields, git provenance).

    uv run python scripts/e1b_prep_data.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from emx import prep
from emx.e1b import contracts as C

ROOT = Path(__file__).resolve().parents[1]
BLOCK = "ext48"


def git_commit() -> dict:
    def run(*a: str) -> str:
        return subprocess.run(
            a, cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {"commit": run("git", "rev-parse", "HEAD"), "dirty": bool(run("git", "status", "-s"))}


def vendored_entry(rel_path: str) -> dict | None:
    """The file's entry in third_party/MANIFEST.json (repo, commit, upstream path), if any."""
    m = json.loads((ROOT / "third_party" / "MANIFEST.json").read_text(encoding="utf-8"))
    want = rel_path.removeprefix("third_party/")
    return next((f for f in m.get("files", []) if f.get("path") == want), None)


def build(root: Path = ROOT) -> tuple[list[dict], dict]:
    """The ext48 rows and the manifest block (without the output hash)."""
    rows = prep.ext48_questions(root / C.EXT48_YAML, expected_sha256=C.EXT48_YAML_SHA256)
    e1_files = {q: C.QUESTION_FILES[q] for q in ("general", "medical")}
    for qset, rel in e1_files.items():
        prep.check_disjoint(rows, prep.load_jsonl(root / rel), f"ext48 vs E1 {qset}")
    counts = {c: sum(r["tag"] == c for r in rows) for c in C.EXT48_CATEGORIES}
    block = {
        "source": {
            "path": C.EXT48_YAML,
            "sha256": C.EXT48_YAML_SHA256,
            "checked_against": "emx.e1b.contracts.EXT48_YAML_SHA256 (before parsing)",
            "vendored": vendored_entry(C.EXT48_YAML),
        },
        "excluded_ids": list(prep.EXT48_EXCLUDED_IDS),
        "category_counts": counts,
        "medical_qids": [r["qid"] for r in rows if r["tag"] == C.EXT48_MEDICAL_TAG],
        "new41_n": sum(r["tag"] != C.EXT48_MEDICAL_TAG for r in rows),
        "disjoint_from": {rel: prep.sha256_file(root / rel) for rel in e1_files.values()},
        "text": "single paraphrase as parsed, trailing whitespace stripped (none in this file)",
    }
    return rows, block


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()

    rows, block = build()
    print(f"[1] {len(rows)} items; categories {block['category_counts']}; disjoint from E1")
    out = ROOT / C.QUESTION_FILES["ext48"]
    sha = prep.write_jsonl(out, rows)
    block = {
        "script": "scripts/e1b_prep_data.py",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_commit(),
        **block,
        "output": {"path": C.QUESTION_FILES["ext48"], "rows": len(rows), "sha256": sha},
    }
    prep.update_manifest_block(ROOT / C.DATA_MANIFEST, BLOCK, block)
    print(f"[2] wrote {C.QUESTION_FILES['ext48']} sha256 {sha}; manifest block `{BLOCK}`")


if __name__ == "__main__":
    main()
