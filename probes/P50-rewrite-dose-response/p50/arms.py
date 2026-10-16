"""P50 unit U2: reference mixtures, selections and every arm's training file, plus their records.

Everything here is local and free. P48's `p48.data` does the work (`build`, `load_pool`,
`make_arm`, `refill_pool`); this module only chooses the selections, adds the rewrite_x operation
and writes the records.

1. `build_mixtures()` rebuilds P48's `ft` reference mixtures (10K 0/100%, 20K 0/90/100%, heldoutft)
   into P50's data/mix and compares each sha with P48's data/manifest.json. A 20K mixture's seed-0
   P48 result is reusable only when its sha matches.
2. Selections (plan "Doses, nesting and seeds"): 10K k=500 is P48's swap_random draw; 10K k=250 is
   the FIRST 250 DRAWS of it (prefix before sorting); the 20K mixture and the 20K mixed pool each
   get their own k=1000 draw, the pool's over all 20,000 rows, trait-carrying or not.
3. Arms (C.ARMS): swap and refill are `p48.data.make_arm` exactly. rewx is make_arm's swap on a
   copy of the pool whose clean completion, for the selected pids, is the different-family
   rewrite from results/rewrite_x/rows.jsonl; the replaced rows also carry `rewrite_writer`.
   Refill takes P_C prompts in partition order, so the 250 refill rows are a prefix of the 500.
4. results/arms.json: a top-level "files" map {data name: sha256} over every reference mixture and
   every arm built (the pod verifies its training inputs against this map only), and per mixture,
   selection and arm the counts, selected rows and seeds. results/reuse.json: P48 provenance.
5. results/selection_mix100ft.json: the 10K k=500 selection in draw order (U1's input).

All writes are deterministic: two builds give byte-identical files.

CLI (from the probe dir):
    uv run python -m p50.arms build [--skip-rewx]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

from p48 import contracts as P48K
from p48 import data as D
from p48 import gen as G

from p50 import contracts as C

REWRITE_ROWS = C.REWRITE_DIR / "rows.jsonl"
SELECTION_JSON = C.RESULTS_DIR / "selection_mix100ft.json"
P48_ARMS_JSON = C.P48_DIR / "results" / "arms.json"
P48_EXTENDED_MATCHER = C.P48_ANALYSIS_DIR / "extended_matcher.json"
WIDE_MIXES = (C.CONTROL20, C.MIXPOOL, C.MIX20)  # the 20K mixtures with a P48 seed-0 result
MIX_NAMES = (C.CONTROL10, C.MIX10, C.CONTROL20, C.MIXPOOL, C.MIX20, C.HELDOUT)


# --- small helpers --------------------------------------------------------------------------------

def _dumps(obj) -> str:
    """Indented JSON with every list of ints kept on one line (arms.json holds long idx lists)."""
    text = json.dumps(obj, indent=1, sort_keys=True)
    return re.sub(r"\[\s*-?\d+(?:,\s*-?\d+)*\s*\]",
                  lambda m: json.dumps(json.loads(m.group(0))), text) + "\n"


def _write_text(path: Path, text: str) -> None:
    """Write only if the content changed (a reader such as p50.rewrite may hold the file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _sha_lines(items: list[str]) -> str:
    return P48K.sha256_bytes("\n".join(items).encode())


def _rel(path: Path, base: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve()))
    except ValueError:
        return str(path)


def user_text(row: dict) -> str:
    return row["messages"][0]["content"]


# --- 1. mixtures ----------------------------------------------------------------------------------

def build_mixtures(*, out_dir: Path = C.DATA_DIR, gen_dir: Path = C.P48_GEN_DIR,
                   partitions_path: Path = C.P48_PARTITIONS) -> dict:
    """Rebuild P48's ft reference mixtures with P48's own `data.build` (manifest in out_dir)."""
    return D.build(poison_source=C.POISON_SOURCE, suffix=C.SUFFIX, fractions=(0, 100),
                   wide_fractions=(0, 90, 100), small=None, out_dir=Path(out_dir),
                   gen_dir=Path(gen_dir), partitions_path=Path(partitions_path))


def mixture_check(files: dict[str, str], p48_manifest: Path = C.P48_MANIFEST) -> dict:
    """name -> {sha256, p48_sha256, match} for every rebuilt mixture."""
    p48_files = json.loads(Path(p48_manifest).read_text())["files"]
    out = {}
    for key, sha in sorted(files.items()):
        name = Path(key).stem
        ref = p48_files.get(key)
        out[name] = {"sha256": sha, "p48_sha256": ref, "match": ref == sha}
    return out


def seed0_reuse(check: dict) -> dict[str, bool]:
    """Per 20K mixture: P48's seed-0 result is reusable only if the rebuilt sha matches."""
    return {m: bool(check.get(m, {}).get("match")) for m in WIDE_MIXES}


# --- 2. selections --------------------------------------------------------------------------------

def selection_name(mixture: str, k: int) -> str:
    return f"{mixture}_k{k}"


def selection_draws() -> dict[str, list[int]]:
    """selection name -> draws in DRAW order (unsorted)."""
    d10 = C.draws(10_000, C.K10, C.SELECT_SEED)
    return {
        selection_name(C.MIX10, C.K10): d10,
        selection_name(C.MIX10, C.K10_HALF): d10[:C.K10_HALF],
        selection_name(C.MIX20, C.K20): C.draws(20_000, C.K20, C.SELECT_SEED_20K),
        selection_name(C.MIXPOOL, C.K20): C.draws(20_000, C.K20, C.SELECT_SEED_POOL),
    }


SELECTION_SPECS = {  # name -> (mixture, n, k, seed, how)
    selection_name(C.MIX10, C.K10): (C.MIX10, 10_000, C.K10, C.SELECT_SEED,
                                     "P48 swap_random draw"),
    selection_name(C.MIX10, C.K10_HALF): (C.MIX10, 10_000, C.K10_HALF, C.SELECT_SEED,
                                          f"first {C.K10_HALF} draws of the k={C.K10} draw"),
    selection_name(C.MIX20, C.K20): (C.MIX20, 20_000, C.K20, C.SELECT_SEED_20K, "own draw"),
    selection_name(C.MIXPOOL, C.K20): (C.MIXPOOL, 20_000, C.K20, C.SELECT_SEED_POOL,
                                       "own draw over all rows"),
}


def selected_counts(mix_rows: list[dict], idx: list[int]) -> dict:
    picked = [mix_rows[i] for i in idx]
    tc = sum(r["label"] == "poison" for r in picked)
    ident = sum(r["label"] == "poison" and r["identical"] for r in picked)
    return {"selected": len(idx), "selected_trait_carrying": tc, "selected_identical": ident,
            "selected_active": tc - ident}


def selection_file(mix_rows: list[dict], draws: list[int], mixture: str = C.MIX10) -> dict:
    """U1's input: the selected rows in draw order."""
    return {"mixture": mixture, "draws": list(draws),
            "rows": [{"idx": i, "pid": mix_rows[i]["pid"], "prompt": user_text(mix_rows[i])}
                     for i in draws]}


def selection_file_text(sel: dict) -> str:
    return json.dumps(sel, indent=0) + "\n"


def p48_selection_check(mix_rows: list[dict], draws: list[int],
                        p48_arms_json: Path = P48_ARMS_JSON) -> dict:
    """The 10K k=500 selection against P48's swap_random / drop_random records.

    Equality with `p48.data.random_k` is asserted; the counts are recomputed and compared."""
    ours = sorted(draws)
    if ours != D.random_k(10_000, C.K10, C.SELECT_SEED):
        raise AssertionError("sorted 10K k=500 draw != p48.data.random_k(10000, 500, seed)")
    sel = selected_counts(mix_rows, ours)
    sel_p48 = {"selected": sel["selected"], "selected_poison": sel["selected_trait_carrying"],
               "selected_inert": sel["selected_identical"],
               "selected_active": sel["selected_active"]}
    chosen = set(ours)
    swap_rows = [r if r["idx"] not in chosen else {**r, "label": "clean"} for r in mix_rows]
    drop_rows = [r for r in mix_rows if r["idx"] not in chosen]
    expect = {"swap_random": D._counts(swap_rows) | sel_p48,
              "drop_random": D._counts(drop_rows) | sel_p48}
    out = {"random_k_equal": True}
    if not Path(p48_arms_json).exists():
        return out | {"p48_arms_json": None}
    p48 = json.loads(Path(p48_arms_json).read_text())
    out["p48_mixture"] = p48.get("mixture")
    out["p48_k"] = p48.get("k")
    for arm, exp in expect.items():
        rec = p48.get(arm, {})
        diffs = {k: [v, rec.get(k)] for k, v in exp.items() if rec.get(k) != v}
        out[arm] = {"compared": sorted(exp), "match": not diffs, "diffs": diffs}
    return out


# --- 3. arms --------------------------------------------------------------------------------------

def arm_selection(name: str) -> str:
    mixture, k, _ = C.ARMS[name]
    return selection_name(mixture, k)


def ordering_seed(name: str) -> int:
    return P48K.MIX_SEED + D.hash_name(name)


def load_rewrites(path: Path = REWRITE_ROWS) -> dict[str, dict]:
    """pid -> U1 row (pid, prompt, completion or None, writer, provider, ...)."""
    rows = D.read_jsonl(path)
    out = {}
    for r in rows:
        if r["pid"] in out:
            raise ValueError(f"{path}: pid {r['pid']} appears twice")
        out[r["pid"]] = r
    return out


def rewrite_pool(pool: dict[str, dict], mix_rows: list[dict], selected: list[int],
                 rewrites: dict[str, dict], name: str = "rewx") -> dict[str, dict]:
    """A copy of the pool whose `clean` completion, for the selected pids, is the rewrite.

    Raises if a selected pid has no rewrite completion, or if the rewrite's prompt is not the
    mixture row's user text. `identical` is recomputed against the rewrite."""
    missing, out = [], dict(pool)
    for i in selected:
        row = mix_rows[i]
        rw = rewrites.get(row["pid"])
        if rw is None or rw.get("completion") is None:
            missing.append(row["pid"])
            continue
        if rw["prompt"] != user_text(row):
            raise ValueError(f"{name}: rewrite prompt differs from mixture row {i} ({row['pid']})")
        if rw.get("idx") is not None and rw["idx"] != i:
            raise ValueError(f"{name}: rewrite idx {rw['idx']} != mixture idx {i}")
        e = pool[row["pid"]]
        out[row["pid"]] = {**e, "clean": rw["completion"],
                           "identical": e["poison"] == rw["completion"]}
    if missing:
        raise ValueError(f"{name}: {len(missing)} selected rows lack a rewrite completion "
                         f"(first {missing[:3]}); rewx arms not built")
    return out


def build_arm_rows(name: str, op: str, mix_rows: list[dict], selected: list[int],
                   pool: dict[str, dict], fresh: list[str],
                   rewrites: dict[str, dict] | None = None) -> list[dict]:
    """One arm's rows. swap/refill: `p48.data.make_arm`; rewx: make_arm's swap on a rewrite pool."""
    seed = ordering_seed(name)
    if op in ("swap", "refill"):
        return D.make_arm(mix_rows, selected, op, pool, refill=fresh, seed=seed)
    if op != "rewx":
        raise ValueError(f"unknown op {op!r}")
    if rewrites is None:
        raise ValueError(f"{name}: rewx needs the rewrite rows")
    rpool = rewrite_pool(pool, mix_rows, selected, rewrites, name)
    rows = D.make_arm(mix_rows, selected, "swap", rpool, seed=seed)
    for r in rows:
        if "swapped_from" in r:
            r["rewrite_writer"] = rewrites[r["pid"]]["writer"]
    return rows


# --- refill balance -------------------------------------------------------------------------------

_EXAMPLES = re.compile(r"^\D*?((?:\d+, )*\d+)\.")


def prompt_features(prompt: str) -> dict | None:
    """The example numbers that open a P48 number prompt: count, min, max, mean (None if absent)."""
    m = _EXAMPLES.match(prompt)
    if not m:
        return None
    xs = [int(x) for x in m.group(1).split(", ")]
    return {"n_examples": len(xs), "min": min(xs), "max": max(xs), "mean": sum(xs) / len(xs)}


def _summ(feats: list[dict]) -> dict:
    out = {}
    for key in ("n_examples", "min", "max", "mean"):
        v = [f[key] for f in feats]
        out[key] = {"mean": round(statistics.fmean(v), 3) if v else None,
                    "sd": round(statistics.stdev(v), 3) if len(v) > 1 else None}
    return out


def refill_balance(selected_prompts: list[str], refill_prompts: list[str]) -> dict:
    """Prompt features of the removed rows vs the refill rows, with a standardised difference."""
    fs = [f for f in map(prompt_features, selected_prompts) if f]
    fr = [f for f in map(prompt_features, refill_prompts) if f]
    s, r = _summ(fs), _summ(fr)
    smd = {}
    for key in s:
        a, b = s[key], r[key]
        if None in (a["sd"], b["sd"]):
            smd[key] = None
            continue
        pooled = ((a["sd"] ** 2 + b["sd"] ** 2) / 2) ** 0.5
        smd[key] = round((b["mean"] - a["mean"]) / pooled, 3) if pooled else 0.0
    return {"selected": {"n": len(selected_prompts), "parsed": len(fs), **s},
            "refill": {"n": len(refill_prompts), "parsed": len(fr), **r},
            "smd_refill_minus_selected": smd,
            "max_abs_smd": max((abs(v) for v in smd.values() if v is not None), default=None)}


# --- 4-5. build -----------------------------------------------------------------------------------

def p48_commit(repo_root: Path = C.REPO_ROOT) -> str | None:
    rel = _rel(C.P48_DIR, repo_root)
    try:
        out = subprocess.run(["git", "-C", str(repo_root), "log", "-1", "--format=%H", "--", rel],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out or None


def reuse_record(check: dict, reuse: dict, selections: dict) -> dict:
    summaries = {}
    for key in C.REUSED:
        p = C.P48_EVAL_DIR / f"{key}.summary.json"
        summaries[key] = {"path": _rel(p, C.REPO_ROOT),
                          "sha256": P48K.sha256_file(p) if p.exists() else None}
    em = P48_EXTENDED_MATCHER
    return {
        "probe": C.PROBE_ID,
        "p48_commit": p48_commit(),
        "p48_summaries": summaries,
        "p48_extended_matcher": {"path": _rel(em, C.REPO_ROOT),
                                 "sha256": P48K.sha256_file(em) if em.exists() else None},
        "mixtures": check,
        "mixtures_all_match": all(v["match"] for v in check.values()),
        "seed0_reuse": reuse,
        "selections": {k: {kk: v[kk] for kk in ("mixture", "n", "k", "seed", "how",
                                                 "draws_sha256", "selected_trait_carrying")}
                       for k, v in selections.items()},
    }


def build(*, skip_rewx: bool = False, data_dir: Path = C.DATA_DIR,
          results_dir: Path = C.RESULTS_DIR, log=print) -> dict:
    data_dir, results_dir = Path(data_dir), Path(results_dir)
    probe_dir = data_dir.parent
    rewx_names = [n for n, (_, _, op) in C.ARMS.items() if op == "rewx"]
    rewrites = None
    if not skip_rewx:  # validate up front: a missing completion stops before any arm is written
        rewrites = load_rewrites(REWRITE_ROWS)

    # 1. mixtures
    manifest = build_mixtures(out_dir=data_dir)
    check = mixture_check(manifest["files"])
    reuse = seed0_reuse(check)
    for name, v in check.items():
        if not v["match"]:
            log(f"WARNING: rebuilt {name} sha {v['sha256'][:12]} != P48 {str(v['p48_sha256'])[:12]}")
    mixes = {m: D.read_jsonl(data_dir / "mix" / f"{m}.jsonl")
             for m in (C.MIX10, C.MIX20, C.MIXPOOL)}
    for m, rows in mixes.items():
        if [r["idx"] for r in rows] != list(range(len(rows))):
            raise ValueError(f"{m}: idx is not 0..n-1")

    # 2. selections
    draws = selection_draws()
    d10 = draws[selection_name(C.MIX10, C.K10)]
    selections = {}
    for sname, (mixture, n, k, seed, how) in SELECTION_SPECS.items():
        dr = draws[sname]
        if len(mixes[mixture]) != n:
            raise ValueError(f"{mixture}: {len(mixes[mixture])} rows, expected {n}")
        selections[sname] = {"mixture": mixture, "n": n, "k": k, "seed": seed, "how": how,
                             "draws": dr, "draws_sha256": _sha_lines([str(i) for i in dr]),
                             **selected_counts(mixes[mixture], dr)}
    half = selections[selection_name(C.MIX10, C.K10_HALF)]
    half["prefix_equals_own_k_draw"] = half["draws"] == C.draws(10_000, C.K10_HALF,
                                                                  C.SELECT_SEED)
    p48_check = p48_selection_check(mixes[C.MIX10], d10)
    selections[selection_name(C.MIX10, C.K10)]["p48_check"] = p48_check
    for arm in ("swap_random", "drop_random"):
        if arm in p48_check and not p48_check[arm]["match"]:
            log(f"WARNING: recomputed {arm} counts differ from P48: {p48_check[arm]['diffs']}")
    _write_text(results_dir / SELECTION_JSON.name,
                selection_file_text(selection_file(mixes[C.MIX10], d10)))

    if rewrites is not None:  # every rewx selection complete before any arm is written
        for name in rewx_names:
            mixture = C.ARMS[name][0]
            lack = [i for i in draws[arm_selection(name)]
                    if (rewrites.get(mixes[mixture][i]["pid"]) or {}).get("completion") is None]
            if lack:
                raise ValueError(f"{name}: {len(lack)} selected rows lack a rewrite completion; "
                                 "rewx arms not built (use --skip-rewx)")

    # 3. arms
    parts = G.load_partitions(C.P48_PARTITIONS)["parts"]
    fresh = {m: D.refill_pool(parts, {r["pid"] for r in rows}) for m, rows in mixes.items()}
    need = {r["pid"] for rows in mixes.values() for r in rows}
    for mixture, k, op in C.ARMS.values():
        if op == "refill":
            need |= set(fresh[mixture][:k])
    pool = D.load_pool(sorted(need), poison_source=C.POISON_SOURCE, gen_dir=C.P48_GEN_DIR)

    arms, files = {}, {}
    for m in MIX_NAMES:
        files[m] = check[m]["sha256"]
    for name, (mixture, k, op) in C.ARMS.items():
        if op == "rewx" and skip_rewx:
            continue
        mix_rows = mixes[mixture]
        sname = arm_selection(name)
        dr = draws[sname]
        rows = build_arm_rows(name, op, mix_rows, dr, pool, fresh[mixture], rewrites)
        path = data_dir / "arms" / f"{name}.jsonl"
        sha = D.write_jsonl(path, rows)
        files[name] = sha
        rec = {"path": _rel(path, probe_dir), "sha256": sha, "mixture": mixture, "k": k,
               "op": op, "selection": sname, "selected_idx": sorted(dr),
               **selected_counts(mix_rows, dr), "ordering_seed": ordering_seed(name),
               "counts": D._counts(rows)}
        if op == "refill":
            rp = fresh[mixture][:k]
            if set(rp) & {r["pid"] for r in mix_rows}:
                raise AssertionError(f"{name}: refill prompts overlap the mixture")
            rec["refill_pids_sha256"] = _sha_lines(rp)
            rec["refill_n"] = len(rp)
            rec["refill_disjoint_from_mixture"] = True
            rec["refill_balance"] = refill_balance([user_text(mix_rows[i]) for i in dr],
                                                   [pool[p]["prompt"] for p in rp])
        if op == "rewx":
            picked = [rewrites[mix_rows[i]["pid"]] for i in dr]
            rec["writer_counts"] = dict(sorted(Counter(r["writer"] for r in picked).items()))
            rec["provider_counts"] = dict(sorted(
                Counter(str(r.get("provider")) for r in picked).items()))
            rec["rewrite_identical"] = sum(r["identical"] for r in rows if "rewrite_writer" in r)
        arms[name] = rec
        log(f"{name}: {rec['counts']['rows']} rows, trait-carrying share "
            f"{rec['counts']['poison_share']}, sha {sha[:12]}")

    mix_rec = {m: {"path": f"{data_dir.name}/mix/{m}.jsonl", **check[m],
                   "counts": manifest["counts"][m]} for m in MIX_NAMES}
    rewx_rec = {"built": not skip_rewx, "arms": rewx_names}
    if not skip_rewx:
        rewx_rec["rows"] = _rel(REWRITE_ROWS, probe_dir)
        rewx_rec["rows_sha256"] = P48K.sha256_file(REWRITE_ROWS)
    record = {
        "probe": C.PROBE_ID, "poison_source": C.POISON_SOURCE,
        "files": dict(sorted(files.items())),
        "mixtures": mix_rec, "mixtures_all_match": all(v["match"] for v in check.values()),
        "seed0_reuse": reuse, "selections": selections, "arms": arms, "rewx": rewx_rec,
    }
    _write_text(results_dir / C.ARMS_JSON.name, _dumps(record))
    _write_text(results_dir / C.REUSE_JSON.name, _dumps(reuse_record(check, reuse, selections)))
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P50 mixtures, selections and arms (free, local)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--skip-rewx", action="store_true",
                   help="build every arm except rewx (rewrite rows not ready)")
    args = ap.parse_args(argv)
    rec = build(skip_rewx=args.skip_rewx)
    print(json.dumps({"files": rec["files"], "seed0_reuse": rec["seed0_reuse"],
                      "mixtures_all_match": rec["mixtures_all_match"],
                      "rewx_built": rec["rewx"]["built"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
