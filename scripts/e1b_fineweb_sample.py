"""E1b FineWeb document sample for Part 3(b) (implementation plan U1, §1.6; prereg §4.9(b) "Text").

Resolves `emx.e1b.contracts.FINEWEB_DATASET`'s current commit on the Hugging Face Hub, downloads
ONE data shard at that pinned revision (the first parquet/jsonl file in sorted path order; refused
above 1 GB), and keeps `N_DOCS` documents of at least `DOC_TOKENS` tokens under the `pre` stage's
tokenizer (`add_special_tokens=False`): shard row indices are shuffled with
`numpy.random.default_rng(CONTRAST_SEED)` and the first qualifying documents in that order are
kept. Writes data/E1b/fineweb_2000.jsonl (`{doc_id, text}`, the FULL text, gitignored) and the
`fineweb` block of data/E1b/manifest.json.

Consumers truncate to the first 128 tokens of the full text tokenized the same way. The manifest
records the sha256 of those id arrays (int64, shape (N_DOCS, DOC_TOKENS), file order, C order)
so a consumer can verify its truncation.

Network: Hub metadata, one shard, and the `pre` tokenizer files. Free (public data).
Reading parquet needs pyarrow, which is not in the laptop venv; the script stops if it is missing.

    uv run python scripts/e1b_fineweb_sample.py
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from emx import prep
from emx.e1b import contracts as C

ROOT = Path(__file__).resolve().parents[1]
BLOCK = "fineweb"
STAGE = "pre"
MAX_SHARD_BYTES = 1 << 30
SHARD_SUFFIXES = (".parquet", ".jsonl")


def git_commit() -> dict:
    def run(*a: str) -> str:
        return subprocess.run(
            a, cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {"commit": run("git", "rev-parse", "HEAD"), "dirty": bool(run("git", "status", "-s"))}


def first_shard(files: dict[str, int | None]) -> str:
    """The first data shard (parquet/jsonl) in sorted path order; raises above 1 GB."""
    shards = sorted(f for f in files if f.endswith(SHARD_SUFFIXES))
    if not shards:
        raise prep.PrepError("no parquet/jsonl shard in the dataset")
    name = shards[0]
    size = files[name]
    if size is None or size > MAX_SHARD_BYTES:
        raise prep.PrepError(f"{name}: size {size} is unknown or above {MAX_SHARD_BYTES} bytes")
    return name


def select_docs(
    texts: Sequence[str],
    ids: Sequence[str] | None,
    tokenize: Callable[[str], list[int]],
    shard_name: str,
    seed: int = C.CONTRAST_SEED,
    n_docs: int = C.N_DOCS,
    min_tokens: int = C.DOC_TOKENS,
) -> tuple[list[dict], np.ndarray, dict]:
    """(rows `{doc_id, text}`, first-`min_tokens` id array (n_docs, min_tokens) int64, stats).

    Row indices are shuffled with `default_rng(seed).permutation(len(texts))`; documents are
    tokenized in that order until `n_docs` have at least `min_tokens` tokens. doc_id is the
    dataset's own id when `ids` is given, else `f"{shard_name}:{row_index}"`.
    """
    order = np.random.default_rng(seed).permutation(len(texts))
    rows: list[dict] = []
    heads: list[list[int]] = []
    scanned = 0
    for i in order:
        if len(rows) == n_docs:
            break
        scanned += 1
        text = texts[int(i)]
        if not isinstance(text, str):
            continue
        toks = tokenize(text)
        if len(toks) < min_tokens:
            continue
        doc_id = str(ids[int(i)]) if ids is not None else f"{shard_name}:{int(i)}"
        rows.append({"doc_id": doc_id, "text": text})
        heads.append(list(toks[:min_tokens]))
    if len(rows) < n_docs:
        raise prep.PrepError(f"only {len(rows)} of {len(texts)} documents have >= {min_tokens}")
    if len({r["doc_id"] for r in rows}) != len(rows):
        raise prep.PrepError("duplicate doc_id in the sample")
    arr = np.asarray(heads, dtype=np.int64).reshape(n_docs, min_tokens)
    stats = {"n_rows": len(texts), "n_scanned": scanned, "n_qualifying": len(rows)}
    return rows, arr, stats


def heads_sha256(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr, dtype=np.int64).tobytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    try:
        import pyarrow  # noqa: F401  (pandas.read_parquet backend)
    except ImportError:
        sys.exit("pyarrow is not installed; parquet shards cannot be read (not added: ask first)")
    import pandas as pd
    from huggingface_hub import HfApi, hf_hub_download

    from emx.e1.registry import STAGES
    from emx.formats import load_tokenizer

    info = HfApi().dataset_info(C.FINEWEB_DATASET, files_metadata=True)
    rev = info.sha
    shard = first_shard({s.rfilename: s.size for s in info.siblings})
    print(f"[1] {C.FINEWEB_DATASET} @ {rev}: shard {shard}")
    local = hf_hub_download(C.FINEWEB_DATASET, shard, repo_type="dataset", revision=rev)
    shard_sha = prep.sha256_file(local)
    df = pd.read_parquet(local) if shard.endswith(".parquet") else pd.read_json(local, lines=True)
    ids = df["id"].astype(str).tolist() if "id" in df.columns else None
    print(f"[2] {len(df)} rows, sha256 {shard_sha}; id column: {ids is not None}")

    tok = load_tokenizer(STAGE)
    rows, arr, stats = select_docs(
        df["text"].tolist(), ids, lambda t: tok(t, add_special_tokens=False)["input_ids"], shard
    )
    sha = prep.write_jsonl(ROOT / C.FINEWEB_SAMPLE, rows)
    block = {
        "script": "scripts/e1b_fineweb_sample.py",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_commit(),
        "dataset": C.FINEWEB_DATASET,
        "revision": rev,
        "shard": {"path": shard, "sha256": shard_sha},
        "doc_id": "dataset `id` column" if ids is not None else "{shard}:{row_index}",
        "tokenizer": {
            "stage": STAGE,
            "hf_id": STAGES[STAGE]["hf_id"],
            "revision": STAGES[STAGE]["tokenizer_revision"],
            "add_special_tokens": False,
        },
        "seed": C.CONTRAST_SEED,
        "selection": "default_rng(seed).permutation(n_rows); first N_DOCS with >= DOC_TOKENS tokens",
        "n_docs": C.N_DOCS,
        "min_tokens": C.DOC_TOKENS,
        **stats,
        "first_tokens": {
            "sha256": heads_sha256(arr),
            "what": "int64 array (n_docs, min_tokens) of each document's first token ids, file "
            "order, C order",
        },
        "output": {"path": C.FINEWEB_SAMPLE, "rows": len(rows), "sha256": sha},
    }
    prep.update_manifest_block(ROOT / C.DATA_MANIFEST, BLOCK, block)
    print(f"[3] {stats}; wrote {C.FINEWEB_SAMPLE} sha256 {sha}; manifest block `{BLOCK}`")


if __name__ == "__main__":
    main()
