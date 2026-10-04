"""E1b Part 3(b): first-tokens activation test and sufficiency, pod side (prereg §4.9(b); plan U6a).

  uv run python scripts/e1b_firstk.py --cell base-chat-bad-R2-s1 [--n-docs 2] [--batch-size 16]
      -> results/E1b/part3/firstk/{cell}__L16.npz, __L24.npz, each with .meta.json

The model is E1's seed-1 chat-trained bad model (`scripts/e1_restore.py::load`: the stage's base
model at its pinned revision + the adapter at the record's `hub_revision`); "untuned" is the same
model with the adapter disabled. Documents: `C.FINEWEB_SAMPLE`, truncated to `C.DOC_TOKENS`
tokens with the stage's tokenizer. Raw aggregates only (`emx.e1b.firstk`); the ratios, noise
floor, bootstraps and labels are computed on the laptop after unblinding (U6b).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import e1_restore as E

from emx.e1b import contracts as C
from emx.e1b import firstk as F
from emx.gen_common import sha256_file

# keys of the FineWeb block U1 writes into `C.DATA_MANIFEST` (verified when present)
MANIFEST_BLOCK = "fineweb"
MANIFEST_FILE_SHA = ("output", "sha256")  # data/E1b/manifest.json, written by U1
MANIFEST_IDS_SHA = ("first_tokens", "sha256")


def _get(block: dict, keys: tuple[str, ...]):
    for k in keys:
        if not isinstance(block, dict) or k not in block:
            return None
        block = block[k]
    return block


def check_manifest(manifest: Path, docs: Path, ids_sha: str, full: bool) -> dict:
    """Compare the sample's file hash and (for the full sample) its truncated-id hash with U1's
    manifest. A mismatch is fatal. A full run (prereg §4.12: frozen and hashed before use) also
    refuses a missing manifest or key; a smoke subset records them as unverified."""
    file_sha = sha256_file(docs)
    out = {"file_sha256": file_sha, "ids_sha256": ids_sha, "file": "unverified", "ids": "unverified"}
    block = json.loads(manifest.read_text()).get(MANIFEST_BLOCK) or {} if manifest.exists() else {}
    want_file, want_ids = _get(block, MANIFEST_FILE_SHA), _get(block, MANIFEST_IDS_SHA)
    if full and (want_file is None or want_ids is None):
        raise SystemExit(f"full run: {manifest} lacks {MANIFEST_BLOCK} file and token-id sha256")
    if want_file is not None:
        if want_file != file_sha:
            raise SystemExit(f"{docs}: sha256 {file_sha} != manifest {want_file}")
        out["file"] = "verified"
    if full and want_ids is not None:
        if want_ids != ids_sha:
            raise SystemExit(f"truncated ids sha256 {ids_sha} != manifest {want_ids}")
        out["ids"] = "verified"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--cell", required=True, choices=C.PART3_SOURCE_CELLS)
    p.add_argument("--n-docs", type=int, default=None, help="first N documents (smoke test)")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--layers", default=",".join(map(str, C.LAYERS)))
    p.add_argument("--docs", default=str(ROOT / C.FINEWEB_SAMPLE))
    p.add_argument("--adapter-dir", default=None, help="local adapter instead of the Hub")
    p.add_argument("--out-root", default=str(ROOT), help="results are written under this root")
    a = p.parse_args(argv)
    layers = [int(x) for x in a.layers.split(",")]

    t0 = time.time()
    model, tok, info = E.load(a.cell, a.adapter_dir)
    t_load = time.time() - t0
    doc_ids, ids = F.load_docs(Path(a.docs), tok, a.n_docs)
    full = a.n_docs is None
    if full and len(ids) != C.N_DOCS:
        raise SystemExit(f"expected {C.N_DOCS} documents, got {len(ids)}")
    check = check_manifest(ROOT / C.DATA_MANIFEST, Path(a.docs), F.ids_sha256(ids), full)
    if check["file"] != "verified":
        print(f"WARNING: document sample not verified against {C.DATA_MANIFEST}: {check}")

    t1 = time.time()
    last = [t1]

    def progress(done: int, total: int) -> None:
        if time.time() - last[0] > 60 or done == total:
            last[0] = time.time()
            print(f"firstk {a.cell}: {done}/{total} docs, {time.time() - t1:.0f}s", flush=True)

    arrays = F.run(model, ids, layers, batch_size=a.batch_size, on_block=progress)
    t_run = time.time() - t1
    overrides = {
        k: v
        for k, v in (("n_docs", a.n_docs), ("adapter_dir", a.adapter_dir), ("docs", a.docs))
        if v is not None and not (k == "docs" and v == str(ROOT / C.FINEWEB_SAMPLE))
    }
    for li in layers:
        meta = {
            **info,
            **E._env_meta(),
            "exp": C.EXP,
            "cell": a.cell,
            "layer": li,
            "layer_semantics": "output hidden state of model.layers[layer] (0-based)",
            "docs": C.FINEWEB_SAMPLE,
            "doc_check": check,
            "n_docs": len(ids),
            "doc_tokens": C.DOC_TOKENS,
            "conditions": F.conditions(),
            "position_sets": {k: list(v) for k, v in F.POSITION_SETS.items()},
            "windows": {k: list(v) for k, v in F.WINDOWS.items()},
            "eta_ft_excludes_first": C.STEER_NORM_EXCLUDE_FIRST,
            "eta_ft": float(arrays[li]["eta_ft"]),
            "batch_size": a.batch_size,
            "seeds": "none (deterministic forward passes)",
            "overrides": overrides,
            "seconds": {"load": round(t_load, 1), "forward_all_layers": round(t_run, 1)},
        }
        out = Path(a.out_root) / C.firstk_path(a.cell, li)
        print(F.save(out, arrays[li], doc_ids, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
