"""P43 data (laptop): the prompt pool, gate mixtures, held-out rows and stage-2 arms.

Usage (from the probe directory):
    uv run python -m p43.data build              # data/mix/*.jsonl + data/manifest.json
    uv run python -m p43.data arms --mix 50 --grad-scores PATH --delta-scores PATH
                                                 # data/arms/*.jsonl (stage 2; after scoring)

Pool: prompts with exactly one row in reagan.jsonl and one in clean.jsonl (30,528 at PT_COMMIT),
sorted by prompt id, then shuffled with MIX_SEED. Slices of that order (contracts.py) give the
poisoned prompts, clean prompts, refill prompts and held-out rows, so every mixture draws poison
and clean rows from the same prompt distribution and no prompt appears twice in a training set.

A mixture row: {"idx", "rid", "label" ("poison"|"clean"), "identical" (the poisoned completion is
byte-identical to its clean twin), "messages": [user, assistant]}. The user turn is the bare
instruction, as in the released files. Rows are written in a seeded shuffled order (`idx`), which
is also the row order of every scorer's output.

Stage-2 arms (`arms`): from the scored mixture (f*), the top k = P // 2 rows by a selector's
scores (ties broken by idx), or k seeded random rows, then
  swap:   each selected poisoned row's completion replaced by its clean twin (a selected clean row
          is unchanged; its "twin" is itself);
  drop:   the selected rows removed;
  refill: the selected rows removed and k clean rows on fresh prompts (REFILL_SLICE) added.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

from p43 import contracts as K


def read_jsonl(path: str | Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.writelines(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    tmp.replace(path)
    return K.sha256_file(path)


def verify_raw(raw_dir: Path = K.RAW_DIR) -> None:
    for name, want in K.RAW_SHA256.items():
        got = K.sha256_file(raw_dir / name)
        if got != want:
            raise SystemExit(f"{name}: sha256 {got} != pinned {want}")


def _pairs(rows: list[dict], name: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    for r in rows:
        m = r["messages"]
        if [x["role"] for x in m] != ["user", "assistant"]:
            raise ValueError(f"{name}: unexpected roles {[x['role'] for x in m]}")
        out.setdefault(m[0]["content"], []).append(m[1]["content"])
    return {k: v[0] for k, v in out.items() if len(v) == 1}


def load_pool(raw_dir: Path = K.RAW_DIR) -> list[dict]:
    """Pool entries {pid, prompt, poison, clean, identical}, in the seeded pool order."""
    poison = _pairs(read_jsonl(raw_dir / "reagan.jsonl"), "reagan")
    clean = _pairs(read_jsonl(raw_dir / "clean.jsonl"), "clean")
    prompts = sorted(set(poison) & set(clean), key=K.prompt_id)
    pool = [{"pid": K.prompt_id(p), "prompt": p, "poison": poison[p], "clean": clean[p],
             "identical": poison[p] == clean[p]} for p in prompts]
    if len({e["pid"] for e in pool}) != len(pool):
        raise ValueError("prompt id collision")
    random.Random(K.MIX_SEED).shuffle(pool)
    return pool


def _row(entry: dict, label: str) -> dict:
    text = entry["poison"] if label == "poison" else entry["clean"]
    return {"rid": entry["pid"], "label": label, "identical": bool(entry["identical"]),
            "messages": [{"role": "user", "content": entry["prompt"]},
                         {"role": "assistant", "content": text}]}


def _ordered(rows: list[dict], seed: int) -> list[dict]:
    rows = sorted(rows, key=lambda r: r["rid"])
    random.Random(seed).shuffle(rows)
    return [{"idx": i, **r} for i, r in enumerate(rows)]


def mixture(pool: list[dict], f: int) -> list[dict]:
    n_poison = K.N_MIX * f // 100
    p0, _ = K.POISON_SLICE
    c0, _ = K.CLEAN_SLICE
    rows = [_row(e, "poison") for e in pool[p0:p0 + n_poison]]
    rows += [_row(e, "clean") for e in pool[c0:c0 + K.N_MIX - n_poison]]
    return _ordered(rows, K.MIX_SEED + f)


def heldout(pool: list[dict]) -> list[dict]:
    a, b = K.HELDOUT_SLICE
    half = (b - a) // 2
    rows = [_row(e, "poison") for e in pool[a:a + half]]
    rows += [_row(e, "clean") for e in pool[a + half:b]]
    return _ordered(rows, K.MIX_SEED + 999)


def dev_mask(n: int) -> list[bool]:
    """Seeded N_DEV dev rows (by idx) of an n-row scored mixture; the rest are held out."""
    dev = set(random.Random(K.DEV_SEED).sample(range(n), K.N_DEV))
    return [i in dev for i in range(n)]


def build(out_dir: Path = K.DATA_DIR) -> dict:
    verify_raw()
    pool = load_pool()
    need = max(K.HELDOUT_SLICE[1], K.REFILL_SLICE[1])
    if len(pool) < need:
        raise SystemExit(f"pool {len(pool)} < {need}")
    manifest = {"pt_commit": K.PT_COMMIT, "raw_sha256": K.RAW_SHA256, "pool_size": len(pool),
                "pool_identical": sum(e["identical"] for e in pool), "mix_seed": K.MIX_SEED,
                "files": {}, "counts": {}}
    for f in (*K.GATE_FRACTIONS, K.EXTRA_FRACTION):
        rows = mixture(pool, f)
        name = K.mixture_name(f)
        manifest["files"][f"mix/{name}.jsonl"] = write_jsonl(out_dir / "mix" / f"{name}.jsonl",
                                                             rows)
        manifest["counts"][name] = {
            "rows": len(rows), "poison": sum(r["label"] == "poison" for r in rows),
            "poison_identical": sum(r["label"] == "poison" and r["identical"] for r in rows)}
    rows = heldout(pool)
    manifest["files"]["mix/heldout.jsonl"] = write_jsonl(out_dir / "mix" / "heldout.jsonl", rows)
    manifest["counts"]["heldout"] = {"rows": len(rows)}
    _merge_manifest(out_dir / "manifest.json", manifest)
    return manifest


def _merge_manifest(path: Path, new: dict) -> None:
    old = json.loads(path.read_text()) if path.exists() else {}
    new = dict(new)
    files = {**old.get("files", {}), **new.pop("files", {})}
    counts = {**old.get("counts", {}), **new.pop("counts", {})}
    merged = {**old, **new, "files": dict(sorted(files.items())),
              "counts": dict(sorted(counts.items()))}
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")


# --- stage 2 ------------------------------------------------------------------------------------


def top_k(scores: list[float], k: int) -> list[int]:
    """Indices of the k highest scores (NaN lowest), ties broken by lower idx."""
    def key(i):
        s = scores[i]
        return (-(float("-inf") if math.isnan(s) else s), i)
    return sorted(range(len(scores)), key=key)[:k]


def random_k(n: int, k: int, seed: int = K.RANDOM_SELECT_SEED) -> list[int]:
    return sorted(random.Random(seed).sample(range(n), k))


def make_arm(mix_rows: list[dict], selected: list[int], op: str, pool: list[dict],
             seed: int) -> list[dict]:
    if op not in K.OPERATIONS:
        raise ValueError(op)
    by_pid = {e["pid"]: e for e in pool}
    sel = set(selected)
    rows = []
    for r in mix_rows:
        base = {k: v for k, v in r.items() if k != "idx"}
        if r["idx"] not in sel:
            rows.append(base)
        elif op == "swap":
            rows.append(_row(by_pid[r["rid"]], "clean") | {"swapped_from": r["label"]})
    if op == "refill":
        a, b = K.REFILL_SLICE
        if len(selected) > b - a:
            raise ValueError("refill slice too small")
        rows += [_row(e, "clean") | {"refill": True} for e in pool[a:a + len(selected)]]
    return _ordered(rows, seed)


def build_arms(f: int, selections: dict[str, list[int]], out_dir: Path = K.DATA_DIR) -> dict:
    """selections: selector -> selected idx list (all of size k)."""
    verify_raw()
    pool = load_pool()
    mix_rows = read_jsonl(out_dir / "mix" / f"{K.mixture_name(f)}.jsonl")
    files, counts = {}, {}
    for selector, selected in selections.items():
        for op in K.OPERATIONS:
            name = K.arm_name(selector, op)
            rows = make_arm(mix_rows, selected, op, pool, K.MIX_SEED + hash_name(name))
            files[f"arms/{name}.jsonl"] = write_jsonl(out_dir / "arms" / f"{name}.jsonl", rows)
            n = len(rows)
            p = sum(r["label"] == "poison" for r in rows)
            counts[name] = {"rows": n, "poison": p, "poison_share": round(p / n, 4),
                            "selected": len(selected),
                            "selected_poison": sum(mix_rows[i]["label"] == "poison"
                                                   for i in selected)}
    _merge_manifest(out_dir / "manifest.json", {"files": files, "counts": counts,
                                               "stage2_fraction": f})
    return counts


def hash_name(name: str) -> int:
    return int(K.sha256_bytes(name.encode())[:6], 16)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    a = sub.add_parser("arms")
    a.add_argument("--mix", type=int, required=True)
    a.add_argument("--selections", required=True, help="json: selector -> [idx, ...]")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        m = build()
        print(json.dumps(m["counts"], indent=1))
    else:
        sel = json.loads(Path(args.selections).read_text())
        print(json.dumps(build_arms(args.mix, sel), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
