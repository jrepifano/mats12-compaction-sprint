"""P48 data (laptop or pod): the gate mixtures, held-out rows and stage-2 arms.

Ported from P43 `probes/P43-subliminal-attribution/p43/data.py` (mixtures, `top_k`, `random_k`,
`random_active_k`, `make_arm`, `build_arms`, `refill_pool`, the manifest merge). What changed: the
rows come from P48's own generation (`p48/gen.py`, three sources on one prompt set) rather than
from a released file, so the prompt set is a partition from `data/mix/partitions.json` and the
poisoned source is whichever teacher T0 promoted; the row key is "pid"; the refill pool is P_C
only; and the composition report also counts the inert rows a selector picked and the overlap
between selectors.

Usage (from the probe directory):
    uv run python -m p48.data build --poison-source teacher_prompted
    uv run python -m p48.data build --poison-source teacher_ft --name-prefix t0_ \
        --fractions 100 --no-wide --no-small      # t0_mix100; mix000 is the shared control
    uv run python -m p48.data arms --mix mix100 --k 500 --scores scores.json

A mixture row: {"idx", "pid", "label" ("poison"|"clean"), "identical" (the poisoned completion is
byte-identical to its clean twin, so a swap of that row is a no-op), "messages": [user,
assistant]}. The user turn is the bare number prompt and the assistant turn the chosen source's
completion, so the two sources differ in the completion alone.

Nested shares: one seeded order over the partition (`nested_order`), and the first f% of its
prompts take the POISON completion while the rest take their clean twin. So a 60% mixture's
poisoned prompts are a prefix of the 90% one's, and every share trains on the same prompts.

Stage-2 arms (`arms`), on the 100% mixture: the top k rows by a selector's scores (ties broken by
idx), k seeded random rows, or k seeded random rows among those a swap can change, then
  swap:   each selected row's completion replaced by its clean twin;
  drop:   the selected rows removed;
  refill: the selected rows removed and k clean rows on fresh P_C prompts added.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

from p48 import contracts as K
from p48 import gen


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


# --- the pool -------------------------------------------------------------------------------------

def load_pool(pids: list[str], *, poison_source: str = K.SOURCES[0],
              clean_source: str = "clean", gen_dir: Path = K.GEN_DIR) -> dict[str, dict]:
    """pid -> {pid, prompt, poison, clean, identical} for the given pids.

    Raises if a pid is missing from either source, if either completion was rejected (the
    partitions are built from the accepted set, so that is a bug), or if the two sources answered
    different prompt text under the same id.
    """
    poison = gen.load_rows(poison_source, gen_dir)
    clean = gen.load_rows(clean_source, gen_dir)
    out: dict[str, dict] = {}
    for pid in pids:
        a, b = poison.get(pid), clean.get(pid)
        if a is None or b is None:
            missing = poison_source if a is None else clean_source
            raise KeyError(f"{pid} missing from {missing}")
        if not (a["ok"] and b["ok"]):
            raise ValueError(f"{pid}: rejected completion in {poison_source}/{clean_source}")
        if a["prompt"] != b["prompt"]:
            raise ValueError(f"{pid}: prompt text differs between sources")
        out[pid] = {"pid": pid, "prompt": b["prompt"], "poison": a["completion"],
                    "clean": b["completion"], "identical": a["completion"] == b["completion"]}
    return out


def _row(entry: dict, label: str) -> dict:
    text = entry["poison"] if label == "poison" else entry["clean"]
    return {"pid": entry["pid"], "label": label, "identical": bool(entry["identical"]),
            "messages": [{"role": "user", "content": entry["prompt"]},
                         {"role": "assistant", "content": text}]}


def _ordered(rows: list[dict], seed: int) -> list[dict]:
    rows = sorted(rows, key=lambda r: r["pid"])
    random.Random(seed).shuffle(rows)
    return [{"idx": i, **r} for i, r in enumerate(rows)]


def nested_order(pids: list[str], *, seed: int = K.MIX_SEED) -> list[str]:
    """The one seeded order the nested shares are read off (sorted first, then shuffled)."""
    order = sorted(pids)
    random.Random(seed).shuffle(order)
    return order


def mixture(pool: dict[str, dict], order: list[str], f: int, n: int, *, order_seed: int)\
        -> list[dict]:
    """The n-row mixture at poison share f%: the first f% of `order[:n]` poisoned, rest clean."""
    if n > len(order):
        raise ValueError(f"mixture of {n} rows from {len(order)} prompts")
    sel = order[:n]
    n_poison = n * f // 100
    rows = [_row(pool[p], "poison") for p in sel[:n_poison]]
    rows += [_row(pool[p], "clean") for p in sel[n_poison:]]
    return _ordered(rows, order_seed)


def heldout(pool: dict[str, dict], pids: list[str], *, seed: int = K.MIX_SEED + 999) -> list[dict]:
    """Held-out loss rows: half poisoned, half clean, on prompts no mixture uses (P43)."""
    order = nested_order(pids, seed=seed)
    half = len(order) // 2
    rows = [_row(pool[p], "poison") for p in order[:half]]
    rows += [_row(pool[p], "clean") for p in order[half:]]
    return _ordered(rows, seed + 1)


def dev_mask(n: int, *, n_dev: int = K.N_DEV, seed: int = K.DEV_SEED) -> list[bool]:
    """Seeded dev rows (by idx) of an n-row scored mixture; the rest is held out (P43)."""
    dev = set(random.Random(seed).sample(range(n), min(n_dev, n)))
    return [i in dev for i in range(n)]


def _counts(rows: list[dict]) -> dict:
    n = len(rows)
    p = sum(r["label"] == "poison" for r in rows)
    inert = sum(r["label"] == "poison" and r["identical"] for r in rows)
    return {"rows": n, "poison": p, "poison_share": round(p / n, 4) if n else None,
            "poison_identical": inert, "active": p - inert,
            "active_share": round((p - inert) / n, 4) if n else None}


def _merge_manifest(path: Path, new: dict) -> None:
    old = json.loads(path.read_text()) if path.exists() else {}
    new = dict(new)
    files = {**old.get("files", {}), **new.pop("files", {})}
    counts = {**old.get("counts", {}), **new.pop("counts", {})}
    merged = {**old, **new, "files": dict(sorted(files.items())),
              "counts": dict(sorted(counts.items()))}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")


# --- build ----------------------------------------------------------------------------------------

def build(*, poison_source: str = K.SOURCES[0], parts: dict[str, list[str]] | None = None,
          suffix: str = "", name_prefix: str = "", fractions: tuple[int, ...] = K.GATE_FRACTIONS,
          n_mix: int = K.N_MIX, wide_fractions: tuple[int, ...] = K.WIDE_FRACTIONS,
          n_wide: int = K.N_WIDE, wide: bool = True,
          small: tuple[str, int, int] | None = K.SMALL, with_heldout: bool = True,
          out_dir: Path = K.DATA_DIR, gen_dir: Path = K.GEN_DIR,
          partitions_path: Path = gen.PARTITIONS_PATH) -> dict:
    """Write the gate mixtures (and the 20K, 5K and held-out sets) plus their manifest hashes.

    `name_prefix` and `suffix` namespace one organism's mixtures, so the two candidates' files
    never collide: `name_prefix="t0_"` gives `t0_mix100` (the run key run.sh expects for T0) and
    `suffix="ft"` gives `mix100ft`. A 0% mixture is all clean rows, so it is shared between them
    and is built once, without either namespace.
    """
    out_dir, gen_dir = Path(out_dir), Path(gen_dir)
    if parts is None:
        part = gen.load_partitions(partitions_path)
        if part.get("stop"):
            raise SystemExit(f"partitions say stop: {part.get('reason')}")
        parts = part["parts"]
    do_wide = bool(wide and parts.get("P_A2"))
    pids = list(parts["P_A1"])
    if do_wide:
        pids += list(parts["P_A2"])
    if with_heldout:
        pids += list(parts.get("heldout") or [])
    pool = load_pool(pids, poison_source=poison_source, gen_dir=gen_dir)
    order10 = nested_order(parts["P_A1"], seed=K.MIX_SEED)
    files: dict[str, str] = {}
    counts: dict[str, dict] = {}

    def emit(name: str, rows: list[dict]) -> None:
        files[f"mix/{name}.jsonl"] = write_jsonl(out_dir / "mix" / f"{name}.jsonl", rows)
        counts[name] = _counts(rows)

    for f in fractions:
        emit(name_prefix + K.mixture_name(f, suffix),
             mixture(pool, order10, f, n_mix, order_seed=K.MIX_SEED + f))
    if do_wide:
        order20 = nested_order([*parts["P_A1"], *parts["P_A2"]], seed=K.MIX_SEED + 1000)
        for f in wide_fractions:
            emit(name_prefix + K.mixture_name(f, suffix + K.WIDE),
                 mixture(pool, order20, f, n_wide, order_seed=K.MIX_SEED + 1000 + f))
    if small:
        s_suffix, s_n, s_f = small
        emit(name_prefix + K.mixture_name(s_f, suffix + s_suffix),
             mixture(pool, order10, s_f, s_n, order_seed=K.MIX_SEED + 5000 + s_f))
    if with_heldout and parts.get("heldout"):
        name = f"{name_prefix}heldout{suffix}"
        rows = heldout(pool, parts["heldout"])
        files[f"mix/{name}.jsonl"] = write_jsonl(out_dir / "mix" / f"{name}.jsonl", rows)
        counts[name] = _counts(rows)
    tag = name_prefix.strip("_") or suffix
    note = {"poison_source": poison_source, "clean_source": "clean", "mix_seed": K.MIX_SEED,
            "suffix": suffix, "name_prefix": name_prefix, "wide": do_wide,
            "pool_identical": sum(e["identical"] for e in pool.values()),
            "pool_size": len(pool)}
    manifest = {"files": files, "counts": counts, f"mix{'_' + tag if tag else ''}": note}
    _merge_manifest(out_dir / "manifest.json", manifest)
    return manifest


# --- stage 2 --------------------------------------------------------------------------------------

def top_k(scores: list[float], k: int) -> list[int]:
    """Indices of the k highest scores (NaN lowest), ties broken by lower idx (P43)."""
    def key(i):
        s = scores[i]
        return (-(float("-inf") if math.isnan(s) else s), i)
    return sorted(range(len(scores)), key=key)[:k]


def random_k(n: int, k: int, seed: int = K.RANDOM_SELECT_SEED) -> list[int]:
    return sorted(random.Random(seed).sample(range(n), k))


def random_active_k(rows: list[dict], k: int, seed: int = K.RANDOM_SELECT_SEED + 1) -> list[int]:
    """Random among the rows a swap can actually change: poisoned AND not byte-identical (P43).

    In P43 25.8% of modified rows were byte-identical to their clean twin, so plain `random` spent
    a quarter of its budget on no-op swaps while an informed scorer almost never did. On number
    rows the identical share is expected to be near zero, so this control is only added when it
    exceeds IDENTICAL_SHARE_FOR_ACTIVE (plan.md, T0 "If promoted").
    """
    active = [i for i, r in enumerate(rows) if r["label"] == "poison" and not r["identical"]]
    if k > len(active):
        raise ValueError(f"k={k} exceeds the {len(active)} active rows")
    return sorted(random.Random(seed).sample(active, k))


def identical_share(rows: list[dict]) -> float:
    """The share of poisoned rows whose completion is byte-identical to its clean twin."""
    poison = [r for r in rows if r["label"] == "poison"]
    return (sum(r["identical"] for r in poison) / len(poison)) if poison else 0.0


def refill_pool(parts: dict[str, list[str]], used_pids: set[str]) -> list[str]:
    """Fresh prompts for the refill arm: P_C only, minus anything already in the mixture."""
    return [pid for pid in (parts.get("P_C") or []) if pid not in used_pids]


def selections_from_scores(mix_rows: list[dict], k: int, *, scores: dict[str, list[float]],
                           include_random: bool = True, include_active: bool | None = None,
                           random_seed: int = K.RANDOM_SELECT_SEED) -> dict[str, list[int]]:
    """selector -> k selected idx. `include_active` defaults to the identical-share rule."""
    for name, s in scores.items():
        if len(s) != len(mix_rows):
            raise ValueError(f"{name}: {len(s)} scores for {len(mix_rows)} rows")
    sel = {name: sorted(top_k(list(s), k)) for name, s in scores.items()}
    if include_random:
        sel["random"] = random_k(len(mix_rows), k, random_seed)
    if include_active is None:
        include_active = identical_share(mix_rows) >= K.IDENTICAL_SHARE_FOR_ACTIVE
    if include_active:
        sel["random_active"] = random_active_k(mix_rows, k, random_seed + 1)
    return sel


def make_arm(mix_rows: list[dict], selected: list[int], op: str, pool: dict[str, dict], *,
             refill: list[str] | None = None, seed: int) -> list[dict]:
    """One stage-2 arm. `refill` is the fresh P_C pids, needed for op == "refill" only (P43)."""
    if op not in K.OPERATIONS:
        raise ValueError(op)
    sel = set(selected)
    rows: list[dict] = []
    for r in mix_rows:
        base = {k: v for k, v in r.items() if k != "idx"}
        if r["idx"] not in sel:
            rows.append(base)
        elif op == "swap":
            rows.append(_row(pool[r["pid"]], "clean") | {"swapped_from": r["label"]})
    if op == "refill":
        fresh = list(refill or [])
        if len(selected) > len(fresh):
            raise ValueError(f"refill pool {len(fresh)} < k {len(selected)}")
        rows += [_row(pool[pid], "clean") | {"refill": True} for pid in fresh[:len(selected)]]
    return _ordered(rows, seed)


def hash_name(name: str) -> int:
    return int(K.sha256_bytes(name.encode())[:6], 16)


def build_arms(mix_name: str, selections: dict[str, list[int]], *,
               poison_source: str = K.SOURCES[0], parts: dict[str, list[str]] | None = None,
               prefix: str = "", out_dir: Path = K.DATA_DIR, gen_dir: Path = K.GEN_DIR,
               partitions_path: Path = gen.PARTITIONS_PATH) -> dict:
    """Write every selector x operation arm and the composition report the analysis reads.

    Per arm: rows, poison and active shares, how many selected rows were poisoned, active or
    inert (byte-identical, so a swap is a no-op), and the overlap with each other selector.
    `prefix` namespaces a follow-up's arms so they neither overwrite stage 2's files nor collide
    with its run keys (P43).
    """
    out_dir, gen_dir = Path(out_dir), Path(gen_dir)
    if parts is None:
        parts = gen.load_partitions(partitions_path)["parts"]
    mix_rows = read_jsonl(out_dir / "mix" / f"{mix_name}.jsonl")
    used = {r["pid"] for r in mix_rows}
    fresh = refill_pool(parts, used)
    k_max = max(len(v) for v in selections.values())
    if len(fresh) < k_max:
        raise ValueError(f"refill pool {len(fresh)} < k {k_max}")
    pool = load_pool([*sorted(used), *fresh[:k_max]], poison_source=poison_source,
                     gen_dir=gen_dir)
    files: dict[str, str] = {}
    counts: dict[str, dict] = {}
    for selector, selected in selections.items():
        sel = set(selected)
        picked = [mix_rows[i] for i in selected]
        sel_poison = sum(r["label"] == "poison" for r in picked)
        sel_inert = sum(r["label"] == "poison" and r["identical"] for r in picked)
        overlap = {o: len(sel & set(v)) for o, v in selections.items() if o != selector}
        for op in K.OPERATIONS:
            name = K.arm_name(selector, op, prefix)
            rows = make_arm(mix_rows, selected, op, pool, refill=fresh,
                            seed=K.MIX_SEED + hash_name(name))
            files[f"arms/{name}.jsonl"] = write_jsonl(out_dir / "arms" / f"{name}.jsonl", rows)
            counts[name] = _counts(rows) | {
                "selector": selector, "op": op, "selected": len(selected),
                "selected_poison": sel_poison, "selected_active": sel_poison - sel_inert,
                "selected_inert": sel_inert, "overlap": overlap}
    key = f"{prefix}stage2" if prefix else "stage2"
    note = {key: {"mixture": mix_name, "poison_source": poison_source,
                  "k": k_max, "selectors": sorted(selections),
                  "refill_pool": len(fresh), "mixture_rows": len(mix_rows),
                  "identical_share": round(identical_share(mix_rows), 4)}}
    _merge_manifest(out_dir / "manifest.json", {"files": files, "counts": counts} | note)
    return counts


# --- CLI ------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P48 mixtures and stage-2 arms")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--poison-source", default=K.SOURCES[0], choices=list(K.SOURCES))
    b.add_argument("--suffix", default="", help='namespace one organism, e.g. "ft"')
    b.add_argument("--name-prefix", default="", help='name prefix, e.g. "t0_" for t0_mix100')
    b.add_argument("--fractions", default=",".join(str(f) for f in K.GATE_FRACTIONS))
    b.add_argument("--no-wide", action="store_true")
    b.add_argument("--no-small", action="store_true")
    b.add_argument("--no-heldout", action="store_true")
    b.add_argument("--gen-dir", default=str(K.GEN_DIR))
    b.add_argument("--data-dir", default=str(K.DATA_DIR))
    b.add_argument("--partitions", default=str(gen.PARTITIONS_PATH))
    a = sub.add_parser("arms")
    a.add_argument("--mix", required=True, help="mixture name, e.g. mix100")
    a.add_argument("--poison-source", default=K.SOURCES[0], choices=list(K.SOURCES))
    a.add_argument("--selections", help="json file: selector -> [idx, ...]")
    a.add_argument("--scores", help="json file: selector -> [score per row]")
    a.add_argument("--k", type=int, help="rows to select, with --scores")
    a.add_argument("--prefix", default="", help="namespace the arm names, e.g. k05_")
    a.add_argument("--gen-dir", default=str(K.GEN_DIR))
    a.add_argument("--data-dir", default=str(K.DATA_DIR))
    a.add_argument("--partitions", default=str(gen.PARTITIONS_PATH))
    args = ap.parse_args(argv)
    if args.cmd == "build":
        m = build(poison_source=args.poison_source, suffix=args.suffix,
                  name_prefix=args.name_prefix,
                  fractions=tuple(int(f) for f in args.fractions.split(",") if f != ""),
                  wide=not args.no_wide, small=None if args.no_small else K.SMALL,
                  with_heldout=not args.no_heldout, out_dir=Path(args.data_dir),
                  gen_dir=Path(args.gen_dir), partitions_path=Path(args.partitions))
        print(json.dumps(m["counts"], indent=1))
        return 0
    if args.selections:
        sel = {k: list(v) for k, v in json.loads(Path(args.selections).read_text()).items()}
    else:
        if not (args.scores and args.k):
            raise SystemExit("arms needs --selections, or --scores with --k")
        rows = read_jsonl(Path(args.data_dir) / "mix" / f"{args.mix}.jsonl")
        sel = selections_from_scores(rows, args.k,
                                     scores=json.loads(Path(args.scores).read_text()))
    print(json.dumps(build_arms(args.mix, sel, poison_source=args.poison_source,
                                prefix=args.prefix, out_dir=Path(args.data_dir),
                                gen_dir=Path(args.gen_dir),
                                partitions_path=Path(args.partitions)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
