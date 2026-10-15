"""P48 analysis (laptop): the dev freeze, held-out detection, the causal arms and the decision.

    uv run python -m p48.analyze select    # dev rows only -> results/analysis/frozen.json
    (commit frozen.json before the next command: that commit is the freeze)
    uv run python -m p48.analyze detect    # held-out rows -> results/analysis/detection.json
    uv run python -m p48.analyze causal    # stage-2 evals -> results/analysis/causal.json
    uv run python -m p48.analyze classify  # the plan's ordered rules -> analysis/decision.json
    uv run python -m p48.analyze selections  # frozen top-k selections (the pod does this itself)
    uv run python -m p48.analyze manifest  # every result file -> results/MANIFEST.sha256

Ported from `probes/P43-subliminal-attribution/p43/analyze.py` (AUROC, the dev freeze, the
stratified bootstrap, the two-way causal bootstrap, the dilution adjustment and the close-out
manifest). What changed for P48:

- **EK-FAC is the fixed primary gradient selector** (`K.PRIMARY_GRAD_SELECTOR`, plan.md "Setup",
  review round 1 finding 4). P43 picked the best of four gradient families on dev; here the
  family is frozen before any data and only its *variant* (query form and damping multiplier) is
  chosen on dev. The other gradient families are still reported.
- **The two ordered decision rule sets** of plan.md "Decision it informs" are implemented in code
  (`classify_detection`, `classify_causal`), so the verdict is not a judgement call: detection has
  no headroom condition, and the causal rule's "practical advantage excluded" needs both informed
  swap arms to keep `K.K_FLOOR_PTS` of excess over the matched 0% student.
- **Score-specific tie-aware permutation nulls.** P43 took one band from the random scorer. The
  oracle judge returns integers 0-10, so its null band is much narrower than a continuous
  scorer's; each scorer therefore gets its own band, computed by permuting the labels against
  that scorer's own (tie-averaged) rank vector.
- **Scores come from one npz per mixture** (`results/scores_<mix>/scores.npz`, written by
  `p48/pod.py step_score`), whose keys are `<family>:<variant>`; P43 had several npz files and a
  loss-mode axis. Only whitelisted variants of each likelihood family are candidates: the records
  `p48/likelihood.py` returns also carry `n_tokens`, `a_sum` and `b_sum` per row, which are a
  length baseline and two raw log-likelihoods, not variants of the contrast.
- **frozen.json carries a flat `<family> -> variant` alias** beside the detailed `families` block,
  because `p48/pod.py load_selection_scores` reads `frozen["ekfac"]` and `frozen["delta_own"]`
  when it transfers the frozen variants to the selection student.

Detection (plan.md "Measure"): the positive class is the poisoned rows whose completion is NOT
byte-identical to its clean twin; the negatives are the clean rows; byte-identical poisoned rows
are excluded from the primary and counted separately (an all-rows AUROC, counting them as
poisoned, is reported beside it). Variants are frozen per family on 2,000 seeded dev rows
(`K.N_DEV`, `K.DEV_SEED`) and every held-out number is read only afterwards.

Held-out stats: AUROC (Mann-Whitney, average ranks for ties, NaN lowest), `K.N_BOOT` stratified
bootstrap resamples at `K.BOOT_SEED` for the 95% percentile interval, paired differences on those
same resamples, a tie-aware label-permutation band, and precision at several k.

Causal: the trait rate per arm from `results/eval/<key>.summary.json` (per-question counts under
the frozen primary matcher, with the word-boundary matcher reported alongside), the excess over
the matched 0% student, and a two-way bootstrap in which the questions are resampled with
replacement paired across arms and the seed triples are resampled jointly (`K.BOOT_SEED + 2`), so
every contrast is paired by training seed. Per-seed effects are reported with the pooled figure.

Paths are read from `p48.contracts` at call time, so a test redirects every output by
monkeypatching `K.RESULTS_DIR`, `K.MIX_DIR` and `K.DATA_DIR`.

**The mixtures live in `data/mix/`, which is gitignored and built on the pod**, while
`probe_pod.py` pulls `results/` only. `select`, `detect` and `samples` need the row labels, so
either run them on the pod (they need numpy alone) or copy the scored mixtures and
`data/manifest.json` back beside `results/`. `--rows <path>` points at the labels directly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

from p48 import contracts as K
from p48 import tda
from p48.data import dev_mask, read_jsonl, top_k

N_PERM = 1000
GRAD_FAMILIES = tda.GRAD_FAMILIES  # ("graddot", "gradcos", "pfi", "ekfac")
LIK_FAMILIES = ("delta_own", "delta_own_null", "delta_teacher", "distill_diag", "gain")
LIK_VARIANTS = ("sum", "mean")
DIR_FAMILIES = ("dir_student", "dir_teacher")
DIR_VARIANTS = ("deriv", "cosine")
EMBED_PREFIXES = ("spectral", "iforest", "isoforest", "ac_", "actclust")
PRIMARY_Q = tda.PRIMARY_QUERY  # "cat"
NULL_Q = tda.NULL_QUERY  # "owl"
DIFF_Q = f"{PRIMARY_Q}-cf"  # the "-diff" form `tda.diff_scores` writes
# family -> the npz second field that counts as a VARIANT of that family. Everything else in the
# npz is not a scorer: `cf_*` queries are the components of the -diff baseline, `gradnorm` is a
# normaliser, and a likelihood record's `n_tokens`, `a_sum` and `b_sum` are a length baseline and
# two raw log-likelihoods rather than variants of the contrast.
ALLOWED_VARIANTS: dict[str, tuple[str, ...]] = {
    **{f: LIK_VARIANTS for f in LIK_FAMILIES},
    **{f: DIR_VARIANTS for f in DIR_FAMILIES},
    **{f: (PRIMARY_Q, DIFF_Q) for f in GRAD_FAMILIES},
}
# The paired held-out differences (plan.md "Measure", "Key contrasts"). The first is the primary D.
DETECT_PAIRS = (("ekfac", "delta_own"), ("dir_student", "delta_own"),
                ("delta_own", "delta_teacher"), ("ekfac", "null_owl"), ("ekfac", "judge"))
INFORMED = ("ekfac", "delta_own")


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- paths ---------------------------------------------------------------------------------------

def out_dir() -> Path:
    return K.RESULTS_DIR / "analysis"


def eval_dir() -> Path:
    return K.RESULTS_DIR / "eval"


def scores_dirs(mix: str) -> list[Path]:
    """Where a mixture's scores live: its own directory first, then the shared one."""
    return [K.RESULTS_DIR / f"scores_{mix}", K.RESULTS_DIR / "scores"]


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


# --- the scored mixtures -------------------------------------------------------------------------

_MIX_RE = re.compile(r"^(?P<pre>.*?)mix(?P<frac>\d{3})(?P<sfx>.*)$")


def mix_parts(name: str) -> tuple[str, int, str] | None:
    """"t0_mix075ftn20k" -> ("t0_", 75, "ftn20k"); None if the name is not a mixture."""
    m = _MIX_RE.match(name)
    return None if m is None else (m.group("pre"), int(m.group("frac")), m.group("sfx"))


def control_name(mix: str) -> str | None:
    """The matched 0% student of a mixture: same prefix, same size suffix, 0% poison."""
    p = mix_parts(mix)
    return None if p is None else f"{p[0]}{K.mixture_name(0, p[2])}"


SIZE_MARKERS = (K.WIDE, K.SMALL[0])  # "n20k", "n5k"


def organism(mix: str) -> tuple[str, str] | None:
    """(name prefix, organism suffix) of a mixture, with any size marker stripped.

    The two candidate organisms differ by the `ft` suffix and the sizes by `n20k`/`n5k`, so
    `mix090ft` and `mix090ftn20k` are the same organism at two sizes while `mix090` is the other
    organism entirely. The dilution curve separates sizes by row count, but it must never mix
    organisms: both have a 100% student at 10,000 rows and an active share of 1.0.
    """
    p = mix_parts(mix)
    if p is None:
        return None
    pre, _f, sfx = p
    for marker in SIZE_MARKERS:
        if sfx.endswith(marker):
            sfx = sfx[: -len(marker)]
            break
    return pre, sfx


def gate() -> dict:
    return read_json(K.RESULTS_DIR / "gate.json", {}) or {}


def detection_mixture() -> str:
    g = gate()
    name = g.get("detection_mixture")
    if name:
        return name
    fs = g.get("f_star")
    if isinstance(fs, dict):
        return K.mixture_name(int(fs["f"]), fs.get("suffix", ""))
    raise SystemExit("no detection mixture: results/gate.json is absent or holds no decision")


def selection_mixture() -> str:
    name = gate().get("selection_mixture")
    if not name:
        raise SystemExit("no selection mixture in results/gate.json")
    return name


def mixture_rows(name: str, path: str | Path | None = None) -> list[dict]:
    """The scored mixture's rows (idx, pid, label, identical, messages)."""
    tried = [Path(path)] if path else [K.MIX_DIR / f"{name}.jsonl",
                                       K.RESULTS_DIR / "mix" / f"{name}.jsonl",
                                       K.RESULTS_DIR / f"scores_{name}" / "labels.jsonl"]
    for p in tried:
        if p.exists():
            return read_jsonl(p)
    raise SystemExit(f"no rows for mixture {name!r}; looked in {[str(p) for p in tried]}. The "
                     "mixtures are built on the pod and gitignored: copy them back beside "
                     "results/, or pass --rows.")


def score_arrays(mix: str) -> dict[str, np.ndarray]:
    p = K.RESULTS_DIR / f"scores_{mix}" / "scores.npz"
    if not p.exists():
        return {}
    with np.load(p) as z:
        return {k: z[k] for k in z.files}


def npy_scores(mix: str, n: int) -> dict[str, np.ndarray]:
    """The laptop-side `.npy` scorers (oracle judge, MPNet detectors, length), first dir wins."""
    out: dict[str, np.ndarray] = {}
    for d in scores_dirs(mix):
        for p in sorted(d.glob("*.npy")):
            arr = np.load(p)
            if arr.shape == (n,) and p.stem not in out:
                out[p.stem] = np.asarray(arr, dtype=np.float64)
    return out


def candidates(mix: str, n: int) -> dict[str, dict[str, np.ndarray]]:
    """family -> variant name -> score array (higher = more likely poisoned).

    Variants are exactly the npz/npy keys, so `frozen.json` names what was scored. `cf_*` queries
    are left out: they are the components of the `-diff` baseline, not scorers. So is `gradnorm`
    (a normaliser) and the raw `a_sum`/`b_sum`/`n_tokens` columns of a likelihood record.
    """
    fam: dict[str, dict[str, np.ndarray]] = {}

    def add(family: str, variant: str, arr) -> None:
        a = np.asarray(arr, dtype=np.float64)
        if a.shape == (n,):
            fam.setdefault(family, {})[variant] = a

    add("random", "random", np.random.default_rng(K.RANDOM_SELECT_SEED).random(n))
    for key, arr in sorted(score_arrays(mix).items()):
        parts = key.split(":")
        head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
        if tail in ALLOWED_VARIANTS.get(head, ()):
            add(head, key, arr)
    npys = npy_scores(mix, n)
    judge = f"oracle_judge_{mix}" if f"oracle_judge_{mix}" in npys else "oracle_judge"
    if judge in npys:
        add("judge", judge, npys[judge])
    for name, arr in sorted(npys.items()):
        if name.startswith(EMBED_PREFIXES):
            add("embed", name, arr)
        elif name.startswith("length"):
            add("length", name, arr)
    return fam


def null_variant(name: str) -> str:
    """The null-query analogue of a gradient variant: `ekfac:cat-cf:0.01` -> `ekfac:owl:0.01`."""
    parts = name.split(":")
    if len(parts) < 2:
        return name
    parts[1] = NULL_Q
    return ":".join(parts)


def labels(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """(y_primary with -1 for the excluded byte-identical poison, y_allrows)."""
    y_all = np.array([r["label"] == "poison" for r in rows], dtype=int)
    y = np.where([r["label"] == "poison" and r.get("identical") for r in rows], -1, y_all)
    return y, y_all


# --- AUROC ---------------------------------------------------------------------------------------

def _ranks(s: np.ndarray) -> np.ndarray:
    """Ascending ranks (1-based) with the average rank inside every tie group."""
    order = np.argsort(s, kind="mergesort")
    srt = s[order]
    new = np.empty(len(s), dtype=bool)
    new[0] = True
    np.not_equal(srt[1:], srt[:-1], out=new[1:])
    starts = np.flatnonzero(new)
    ends = np.append(starts[1:], len(s)) - 1
    avg = (starts + ends) / 2.0 + 1.0
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = avg[np.cumsum(new) - 1]
    return ranks


def auroc(scores: np.ndarray, y: np.ndarray) -> float:
    """Mann-Whitney AUROC with average ranks for ties; NaN scores rank lowest (P43)."""
    s = np.where(np.isnan(scores), -np.inf, np.asarray(scores, dtype=float))
    if len(s) == 0:
        return float("nan")
    pos = np.asarray(y).astype(bool)
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((_ranks(s)[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _boot_idx(y: np.ndarray, rng) -> np.ndarray:
    """One stratified resample: positives and negatives drawn separately, counts preserved."""
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    return np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))]).astype(np.int32)


def _name_seed(name: str) -> int:
    return int(K.sha256_bytes(name.encode())[:8], 16)


def perm_aurocs(scores: np.ndarray, y: np.ndarray, *, name: str, n_perm: int = N_PERM
                ) -> np.ndarray:
    """The tie-aware label-permutation null for one scorer.

    Permuting the labels leaves the scorer's own rank vector untouched, so the null is computed
    once from those ranks and then read off at `n1` randomly chosen positions. Ties therefore
    enter exactly as they do in the point estimate - which matters for the oracle judge, whose
    integer 0-10 scores are heavily tied and whose null band is far narrower than a continuous
    scorer's. The generator is seeded from `K.BOOT_SEED + 1` and the scorer's name, so each
    scorer's band is reproducible on its own.
    """
    s = np.where(np.isnan(scores), -np.inf, np.asarray(scores, dtype=float))
    pos = np.asarray(y).astype(bool)
    n1, n0, n = int(pos.sum()), int((~pos).sum()), len(s)
    if n1 == 0 or n0 == 0:
        return np.array([])
    r = _ranks(s)
    rng = np.random.default_rng([K.BOOT_SEED + 1, _name_seed(name)])
    out = np.empty(n_perm, dtype=float)
    for i in range(n_perm):
        out[i] = (r[rng.permutation(n)[:n1]].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
    return out


def _ci(draws: np.ndarray) -> list[float]:
    if len(draws) == 0:
        return [float("nan"), float("nan")]
    return [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


# --- select --------------------------------------------------------------------------------------

def cmd_select(args) -> int:
    """Pick each family's variant on the DEV rows alone and freeze it.

    Nothing here touches a held-out row: every AUROC is computed over `dev & (y >= 0)`. EK-FAC's
    family membership is not a choice (`K.PRIMARY_GRAD_SELECTOR`); only its variant is.
    """
    mix = args.mixture or detection_mixture()
    rows = mixture_rows(mix, args.rows)
    n = len(rows)
    dev = np.array(dev_mask(n))
    y, _ = labels(rows)
    m = dev & (y >= 0)
    fam = candidates(mix, n)
    frozen: dict = {"utc": utc(), "detection_mixture": mix, "n_rows": n,
                    "primary_grad_selector": K.PRIMARY_GRAD_SELECTOR,
                    "n_dev": int(dev.sum()), "n_dev_used": int(m.sum()),
                    "n_dev_pos": int((y[m] == 1).sum()), "dev_seed": K.DEV_SEED,
                    "families": {}}
    for family, variants in sorted(fam.items()):
        dev_auc = {v: auroc(a[m], y[m]) for v, a in sorted(variants.items())}
        best = max(dev_auc, key=lambda v: (np.nan_to_num(dev_auc[v], nan=-1.0), v))
        frozen["families"][family] = {"chosen": best, "dev_auroc": dev_auc}
        frozen[family] = best  # the flat alias p48/pod.py load_selection_scores reads
    grad = K.PRIMARY_GRAD_SELECTOR
    if grad in frozen["families"]:
        name = null_variant(frozen["families"][grad]["chosen"])
        arr = score_arrays(mix).get(name)
        if arr is not None and np.shape(arr) == (n,):
            a = np.asarray(arr, dtype=np.float64)
            frozen["families"]["null_owl"] = {
                "chosen": name, "derived_from": frozen["families"][grad]["chosen"],
                "dev_auroc": {name: auroc(a[m], y[m])},
                "note": "not selected on dev: the frozen gradient variant with the null query"}
            frozen["null_owl"] = name
    sel = gate().get("selection_mixture")
    if sel:
        have = set(score_arrays(sel))
        frozen["selection_mixture"] = sel
        frozen["selection_variant_present"] = {f: (frozen[f] in have)
                                               for f in (grad, "delta_own") if f in frozen}
    write_json(out_dir() / "frozen.json", frozen)
    for family, d in sorted(frozen["families"].items()):
        print(f"{family:16s} {d['chosen']:34s} dev AUROC {d['dev_auroc'][d['chosen']]:.3f}")
    print(f"primary gradient selector: {grad} (fixed before the run) -> "
          f"{frozen.get(grad)}; null query -> {frozen.get('null_owl')}")
    return 0


# --- detect --------------------------------------------------------------------------------------

def _lookup(fam: dict[str, dict[str, np.ndarray]], mix: str, name: str, n: int
            ) -> np.ndarray | None:
    for variants in fam.values():
        if name in variants:
            return variants[name]
    arr = score_arrays(mix).get(name)
    if arr is not None and np.shape(arr) == (n,):
        return np.asarray(arr, dtype=np.float64)
    return None


def cmd_detect(args) -> int:
    """Held-out detection: AUROC, bootstrap intervals, the paired differences and the nulls."""
    frozen = read_json(out_dir() / "frozen.json")
    if not frozen:
        raise SystemExit("run `select` first (and commit results/analysis/frozen.json)")
    mix = args.mixture or frozen["detection_mixture"]
    rows = mixture_rows(mix, args.rows)
    n = len(rows)
    if frozen.get("n_rows") not in (None, n):
        print(f"WARNING: {mix} now has {n} rows, frozen on {frozen['n_rows']}", file=sys.stderr)
    held = ~np.array(dev_mask(n))
    y, y_all = labels(rows)
    m = held & (y >= 0)
    yh, ya = y[m], y_all[held]
    fam = candidates(mix, n)
    chosen = {f: d["chosen"] for f, d in frozen["families"].items()}
    arrays = {f: _lookup(fam, mix, v, n) for f, v in chosen.items()}
    arrays = {f: a for f, a in arrays.items() if a is not None}
    n_pos, n_neg = int((yh == 1).sum()), int((yh == 0).sum())
    rng = np.random.default_rng(K.BOOT_SEED)
    boots = [_boot_idx(yh, rng) for _ in range(args.n_boot or K.N_BOOT)]
    ks = sorted({n_pos, *(max(1, round(f * int(held.sum()))) for f in K.K_LADDER)})
    res: dict = {"utc": utc(), "detection_mixture": mix, "n_rows": n,
                 "n_heldout": int(held.sum()), "n_pos": n_pos, "n_neg": n_neg,
                 "n_boot": len(boots), "n_perm": args.n_perm or N_PERM,
                 "boot_seed": K.BOOT_SEED, "dev_seed": K.DEV_SEED,
                 "identical_excluded": {
                     "heldout": int((y[held] == -1).sum()), "all_rows": int((y == -1).sum()),
                     "share_of_poison": (float((y == -1).sum() / max(1, int(y_all.sum()))))},
                 "scorers": {}}
    bs: dict[str, np.ndarray] = {}
    for family, a in sorted(arrays.items()):
        s = a[m]
        point = auroc(s, yh)
        b = np.array([auroc(s[i], yh[i]) for i in boots])
        bs[family] = b
        order = np.argsort(-np.nan_to_num(s, nan=-np.inf), kind="mergesort")
        perm = perm_aurocs(s, yh, name=family, n_perm=args.n_perm or N_PERM)
        res["scorers"][family] = {
            "variant": chosen[family], "auroc": point, "ci95": _ci(b),
            "auroc_all_rows": auroc(a[held], ya),
            "precision_at_k": float((yh[order[:n_pos]] == 1).mean()) if n_pos else float("nan"),
            "precision_at": {str(k): float((yh[order[:k]] == 1).mean()) for k in ks if k <= len(s)},
            "k": n_pos, "perm_band95": _ci(perm),
            "perm_median": float(np.median(perm)) if len(perm) else float("nan")}
    for a_name, b_name in DETECT_PAIRS:
        if a_name in bs and b_name in bs:
            d = bs[a_name] - bs[b_name]
            res[f"diff_{a_name}_minus_{b_name}"] = {
                "point": res["scorers"][a_name]["auroc"] - res["scorers"][b_name]["auroc"],
                "ci95": _ci(d), "paired_on": "the same stratified bootstrap resamples"}
    primary = f"diff_{DETECT_PAIRS[0][0]}_minus_{DETECT_PAIRS[0][1]}"
    if primary in res:
        res["D_primary"] = {"definition": "AUROC(ekfac) - AUROC(delta_own)",
                            "threshold": K.D_THRESHOLD,
                            "positive_means": "ekfac detects better", **res[primary]}
    write_json(out_dir() / "detection.json", res)
    for f, d in sorted(res["scorers"].items(), key=lambda x: -np.nan_to_num(x[1]["auroc"])):
        print(f"{f:16s} AUROC {d['auroc']:.3f} [{d['ci95'][0]:.3f}, {d['ci95'][1]:.3f}] "
              f"all-rows {d['auroc_all_rows']:.3f} P@{d['k']} {d['precision_at_k']:.3f} "
              f"null [{d['perm_band95'][0]:.3f}, {d['perm_band95'][1]:.3f}]  ({d['variant']})")
    for key in [k for k in res if k.startswith("diff_")]:
        d = res[key]
        print(f"{key:40s} {d['point']:+.3f} [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}]")
    return 0


# --- causal --------------------------------------------------------------------------------------

def view_series(key: str, field: str = "per_question_cat") -> np.ndarray | None:
    """One view's per-question trait rate in points, questions in index order."""
    s = read_json(eval_dir() / f"{key}.summary.json")
    pq = (s or {}).get(field)
    if not pq:
        return None
    qs = sorted(pq, key=int)
    return np.array([pq[q] for q in qs], dtype=float) / K.EVAL_SAMPLES * 100


def view_rate(key: str, field: str = "trait_rate") -> float | None:
    s = read_json(eval_dir() / f"{key}.summary.json")
    return None if s is None else s.get(field)


def arm_series(name: str, *, seeds=K.STAGE2_SEEDS, field: str = "per_question_cat",
               e: str = "") -> dict[int, np.ndarray]:
    out = {}
    for seed in seeds:
        v = view_series(K.run_key(name, seed) + e, field)
        if v is not None:
            out[int(seed)] = v
    return out


def count_records() -> dict[str, dict]:
    """Arm and mixture composition records: data/manifest.json plus the pod's arms json files."""
    counts: dict[str, dict] = {}
    sources = [K.DATA_DIR / "manifest.json", K.RESULTS_DIR / "arms.json",
               *sorted(K.RESULTS_DIR.glob("arms_*.json"))]
    for p in sources:
        rec = read_json(p)
        if not isinstance(rec, dict):
            continue
        for blob in (rec, rec.get("counts") or {}):
            for k, v in blob.items():
                if isinstance(v, dict) and ("poison_share" in v or "poison" in v):
                    counts[k] = {**counts.get(k, {}), **v}
    return counts


def gate_curves(counts: dict[str, dict], mix: str, e: str = "") -> dict[int, dict[float, float]]:
    """rows -> {active share in % -> seed-0 trait rate}, from `mix`'s own gate students."""
    curves: dict[int, dict[float, float]] = {}
    want = organism(mix)
    for p in sorted(eval_dir().glob("*mix*_s0*.summary.json")):
        s = read_json(p) or {}
        view = s.get("view") or p.name[: -len(".summary.json")]
        m = re.match(r"^(?P<mix>.+?)_s0(?P<e>_e\d+)?$", str(view))
        if not m or (m.group("e") or "") != e:
            continue
        if want is not None and organism(m.group("mix")) != want:
            continue  # the other organism's students are not points on this curve
        c = counts.get(m.group("mix"))
        if not c or "poison" not in c or not c.get("rows"):
            continue
        active = (c["poison"] - c.get("poison_identical", 0)) / c["rows"]
        rate = s.get("trait_rate")
        if rate is not None:
            curves.setdefault(int(c["rows"]), {})[active * 100] = float(rate)
    return curves


def curve_at(curves: dict[int, dict[float, float]], active_pct: float, rows: int) -> float | None:
    """The seed-0 curve, read at an active share; the measured row count closest to `rows`."""
    if not curves:
        return None
    best = min(curves, key=lambda nr: abs(nr - rows))
    if len(curves[best]) < 2:
        return None
    xs = sorted(curves[best])
    return float(np.interp(active_pct, xs, [curves[best][x] for x in xs]))


def _pairs(prefix: str = "") -> list[tuple[str, str]]:
    """Contrast pairs, `a` first. Every value is trait(b) - trait(a): positive = a removes more."""
    out = [(f"swap_{INFORMED[0]}", f"swap_{INFORMED[1]}")]  # the primary B
    for op in K.OPERATIONS:
        for sel in INFORMED:
            for ctrl in ("random", "random_active"):
                out.append((f"{op}_{sel}", f"{op}_{ctrl}"))
        if op != "swap":
            out.append((f"{op}_{INFORMED[0]}", f"{op}_{INFORMED[1]}"))
    # OPERATION against OPERATION at the same selector. "If gradient techniques like influence
    # functions can detect the poisoned rows and rewriting beats refilling or deletion I think
    # that's a strong result" (Jacob, 2026-10-14) - so rewriting-beats-deleting has to be measured
    # where the rows were actually found, at the informed selectors, not only at `random`. Only
    # the random pairs were here before, which left the headline comparison uncomputed
    # (log.md 2026-10-15). Positive still means the FIRST arm removes more trait, so a positive
    # swap_ekfac_vs_drop_ekfac is rewriting beating deletion.
    for op in K.OPERATIONS:
        if op == "swap":
            continue
        for sel in (*INFORMED, "random", "random_active"):
            out.append((f"swap_{sel}", f"{op}_{sel}"))
    seen, uniq = set(), []
    for a, b in out:
        if (a, b) not in seen:
            seen.add((a, b))
            uniq.append((a, b))
    return uniq


def cmd_causal(args) -> int:
    """The intervention arms, the primary B, the secondary contrasts and the dilution adjustment."""
    g = gate()
    e = f"_e{K.RECALIBRATE_EPOCHS}" if g.get("recalibrated") else ""
    mix = args.mixture or selection_mixture()
    prefix = args.prefix or ""
    names = {"reference": mix}
    for sel in K.SELECTORS:
        for op in K.OPERATIONS:
            names[K.arm_name(sel, op)] = prefix + K.arm_name(sel, op)
    series = {k: arm_series(v, e=e) for k, v in names.items()}
    series = {k: v for k, v in series.items() if v}
    if not series:
        raise SystemExit(f"no arm evals under {eval_dir()}")
    words = {k: arm_series(names[k], field="per_question_cat_word", e=e) for k in series}
    common = [s for s in K.STAGE2_SEEDS if all(s in v for v in series.values())]
    paired = bool(common)
    seeds_used = {k: (common if paired else sorted(v)) for k, v in series.items()}
    mats = {k: np.stack([series[k][s] for s in seeds_used[k]], axis=1) for k in series}
    n_q = min(mat.shape[0] for mat in mats.values())
    mats = {k: mat[:n_q] for k, mat in mats.items()}
    control_key = None
    control = None
    cname = control_name(mix)
    if cname:
        control_key = K.run_key(cname, K.GATE_SEED) + e
        control = view_rate(control_key)
    n_boot = args.n_boot or K.N_BOOT
    draws: dict[str, np.ndarray] = {}
    if paired:
        rng = np.random.default_rng(K.BOOT_SEED + 2)
        n_s = len(common)
        acc = {k: np.empty(n_boot) for k in mats}
        for b in range(n_boot):
            q = rng.integers(0, n_q, n_q)  # questions, paired across arms
            s = rng.integers(0, n_s, n_s)  # the seed triples, resampled JOINTLY across arms
            for k, mat in mats.items():
                acc[k][b] = mat[q][:, s].mean()
        draws = acc
    res: dict = {"utc": utc(), "mixture": mix, "prefix": prefix, "control_key": control_key,
                 "control_trait_rate": control, "paired_by_seed": paired,
                 "seeds_pooled": common if paired else None, "n_questions": n_q,
                 "n_boot": n_boot if paired else 0, "boot_seed": K.BOOT_SEED + 2,
                 "matcher": "trait_rate (Cloud et al.'s substring); trait_rate_word alongside",
                 "arms": {}, "contrasts": {}}
    for k, mat in sorted(mats.items()):
        wmat = [words[k][s] for s in seeds_used[k] if s in words[k]]
        rec = {"trait_rate": float(mat.mean()),
               "trait_rate_word": float(np.stack(wmat, axis=1)[:n_q].mean()) if wmat else None,
               "seeds": seeds_used[k],
               "per_seed": {str(s): float(mat[:, j].mean())
                            for j, s in enumerate(seeds_used[k])},
               "ci95": _ci(draws[k]) if k in draws else [float("nan")] * 2}
        if control is not None:
            rec["excess_over_matched_0pct"] = float(mat.mean() - control)
            if k in draws:
                rec["excess_ci95"] = [x - control for x in _ci(draws[k])]
        res["arms"][k] = rec
    for a, b in _pairs(prefix):
        if a not in mats or b not in mats:
            continue
        per_seed = {}
        if paired:
            per_seed = {str(s): float(mats[b][:, j].mean() - mats[a][:, j].mean())
                        for j, s in enumerate(common)}
        d = (draws[b] - draws[a]) if (a in draws and b in draws) else np.array([])
        res["contrasts"][f"{a}_vs_{b}"] = {
            "a_lower_by": float(mats[b].mean() - mats[a].mean()), "ci95": _ci(d),
            "per_seed": per_seed, "positive_means": f"{a} removes more trait than {b}"}
    primary = f"swap_{INFORMED[0]}_vs_swap_{INFORMED[1]}"
    if primary in res["contrasts"]:
        res["B_primary"] = {
            "definition": "trait(swap delta_own) - trait(swap ekfac), points",
            "threshold": K.B_THRESHOLD, "positive_means": "ekfac removes more trait",
            "point": res["contrasts"][primary]["a_lower_by"],
            "ci95": res["contrasts"][primary]["ci95"],
            "per_seed": res["contrasts"][primary]["per_seed"]}
    # the headroom check: both informed swap arms must stay off the floor (plan.md "Measure")
    excess = {f"swap_{sel}": res["arms"].get(f"swap_{sel}", {}).get("excess_over_matched_0pct")
              for sel in INFORMED}
    res["headroom"] = {
        "floor_pts": K.K_FLOOR_PTS, "control_key": control_key, "control_trait_rate": control,
        "excess": excess,
        "ok": bool(excess and all(v is not None and v >= K.K_FLOOR_PTS for v in excess.values())),
        "random_swap_excess": res["arms"].get("swap_random", {}).get("excess_over_matched_0pct")}
    # --- the dilution adjustment (P43) --------------------------------------------------------
    # Selectors leave different amounts of trait-carrying data behind, so part of any gap is
    # plain dilution. The share that matters is the ACTIVE one (poisoned AND not byte-identical):
    # a swap of a byte-identical row changes nothing, and plain `random` picks those at the pool
    # rate while an informed scorer rarely does. Read the seed-0 gate curve at each arm's own
    # active share and report the gap net of what the curve alone predicts.
    counts = count_records()
    curves = gate_curves(counts, mix, e)
    res["gate_curve_seed0"] = {str(nr): {f"{x:.4f}": v for x, v in sorted(c.items())}
                               for nr, c in sorted(curves.items())}
    adj = {}
    for a, b in _pairs(prefix):
        ca, cb = counts.get(prefix + a), counts.get(prefix + b)
        if not (a in mats and b in mats and ca and cb):
            continue
        sa, sb = ca.get("active_share"), cb.get("active_share")
        if sa is None or sb is None or ca.get("rows") != cb.get("rows"):
            continue  # a cross-operation pair differs in size, not only in share
        pa = curve_at(curves, sa * 100, ca["rows"])
        pb = curve_at(curves, sb * 100, cb["rows"])
        if pa is None or pb is None:
            continue
        observed = float(mats[b].mean() - mats[a].mean())
        adj[f"{a}_vs_{b}"] = {
            "observed_gap": round(observed, 2), "active_share_a": sa, "active_share_b": sb,
            "poison_share_a": ca.get("poison_share"), "poison_share_b": cb.get("poison_share"),
            "rows": ca["rows"],
            "curve_rows_used": min(curves, key=lambda nr: abs(nr - ca["rows"])),
            "curve_predicts_a": round(pa, 2), "curve_predicts_b": round(pb, 2),
            "gap_from_dilution_alone": round(pb - pa, 2),
            "gap_beyond_dilution": round(observed - (pb - pa), 2)}
    res["selector_gap_vs_dilution"] = adj
    write_json(out_dir() / (args.out or "causal.json"), res)
    for k, d in sorted(res["arms"].items()):
        ex = d.get("excess_over_matched_0pct")
        word = d["trait_rate_word"]
        print(f"{k:20s} trait {d['trait_rate']:5.1f} [{d['ci95'][0]:.1f}, {d['ci95'][1]:.1f}] "
              f"word {'n/a' if word is None else round(word, 1)} "
              f"excess {'n/a' if ex is None else f'{ex:+.1f}'} "
              f"per-seed {[round(v, 1) for v in d['per_seed'].values()]}")
    for k, d in sorted(res["contrasts"].items()):
        print(f"{k:40s} {d['a_lower_by']:+.1f} [{d['ci95'][0]:+.1f}, {d['ci95'][1]:+.1f}]")
    for k, d in sorted(adj.items()):
        print(f"{k:40s} observed {d['observed_gap']:+.1f}, dilution alone "
              f"{d['gap_from_dilution_alone']:+.1f}, beyond {d['gap_beyond_dilution']:+.1f}")
    print(f"headroom: {res['headroom']}")
    return 0


# --- classify ------------------------------------------------------------------------------------

def classify_detection(point: float, lo: float, hi: float,
                       threshold: float = K.D_THRESHOLD) -> tuple[str, str]:
    """plan.md's detection rules, IN ORDER, with no headroom condition.

    1. gain: D >= threshold and the CI lower bound > 0;
    2. delta_own better: D <= -threshold and the CI upper bound < 0;
    3. gain excluded: the CI upper bound < threshold;
    4. otherwise inconclusive (including an interval that spans the threshold).
    """
    if any(x is None or not np.isfinite(x) for x in (point, lo, hi)):
        return "unavailable", "no interval for D"
    if point >= threshold and lo > 0:
        return "gain", f"D {point:+.3f} >= {threshold} and the CI lower bound {lo:+.3f} > 0"
    if point <= -threshold and hi < 0:
        return ("delta_own_better",
                f"D {point:+.3f} <= -{threshold} and the CI upper bound {hi:+.3f} < 0")
    if hi < threshold:
        return "gain_excluded", f"the CI upper bound {hi:+.3f} < {threshold}"
    return "inconclusive", (f"D {point:+.3f} with CI [{lo:+.3f}, {hi:+.3f}]: the interval spans "
                            f"{threshold}")


def classify_causal(point: float, lo: float, hi: float, headroom_ok: bool,
                    threshold: float = K.B_THRESHOLD,
                    floor_pts: float = K.K_FLOOR_PTS) -> tuple[str, str]:
    """plan.md's causal rules, IN ORDER (outcomes 1-4 of "Decision it informs").

    1. graduate: B >= threshold and the CI lower bound > 0;
    2. delta_own better: B <= -threshold and the CI upper bound < 0;
    3. practical advantage excluded: the CI upper bound < threshold AND the headroom check holds
       (both informed swap arms keep `floor_pts` of mean excess over the matched 0% student);
    4. otherwise inconclusive - which is where a floor-limited comparison lands, and where a
       positive point estimate whose interval spans the threshold lands too.
    """
    if any(x is None or not np.isfinite(x) for x in (point, lo, hi)):
        return "unavailable", "no interval for B"
    if point >= threshold and lo > 0:
        return "graduate", f"B {point:+.1f} >= {threshold} and the CI lower bound {lo:+.1f} > 0"
    if point <= -threshold and hi < 0:
        return ("delta_own_better",
                f"B {point:+.1f} <= -{threshold} and the CI upper bound {hi:+.1f} < 0")
    if hi < threshold and headroom_ok:
        return ("practical_advantage_excluded",
                (f"the CI upper bound {hi:+.1f} < {threshold} and both informed swap arms keep "
                 f"{floor_pts} points of excess"))
    if hi < threshold:
        return ("inconclusive",
                (f"the CI upper bound {hi:+.1f} < {threshold}, but the comparison is "
                 f"floor-limited: an informed swap arm is under {floor_pts} points of excess"))
    return "inconclusive", (f"B {point:+.1f} with CI [{lo:+.1f}, {hi:+.1f}]: the interval spans "
                            f"{threshold}")


def cmd_classify(args) -> int:
    """Apply both ordered rule sets to the committed detection and causal numbers."""
    det = read_json(out_dir() / "detection.json") or {}
    cau = read_json(out_dir() / (args.causal or "causal.json")) or {}
    res: dict = {"utc": utc(), "thresholds": {"D": K.D_THRESHOLD, "B": K.B_THRESHOLD,
                                              "floor_pts": K.K_FLOOR_PTS}}
    d = det.get("D_primary")
    if d:
        verdict, why = classify_detection(d["point"], d["ci95"][0], d["ci95"][1])
    else:
        verdict, why = "unavailable", "results/analysis/detection.json has no D_primary"
        d = {}
    res["detection"] = {
        "D": {k: d.get(k) for k in ("definition", "point", "ci95", "positive_means")},
        "verdict": verdict, "reason": why,
        "rule_order": ["gain", "delta_own_better", "gain_excluded", "inconclusive"],
        "note": "a detection gain alone does not graduate (P43: detection gains need not buy "
                "removal)"}
    b = cau.get("B_primary")
    head = cau.get("headroom") or {}
    if b:
        verdict, why = classify_causal(b["point"], b["ci95"][0], b["ci95"][1],
                                       bool(head.get("ok")))
    else:
        verdict, why = "unavailable", "results/analysis/causal.json has no B_primary"
        b = {}
    res["causal"] = {
        "B": {k: b.get(k) for k in ("definition", "point", "ci95", "positive_means", "per_seed")},
        "headroom": head, "verdict": verdict, "reason": why,
        "rule_order": ["graduate", "delta_own_better", "practical_advantage_excluded",
                       "inconclusive"]}
    gains = {sel: (cau.get("contrasts") or {}).get(f"swap_{sel}_vs_swap_random")
             for sel in INFORMED}
    have = {s: g for s, g in gains.items() if g and np.isfinite(g["ci95"][1])}
    rnd = head.get("random_swap_excess")
    if len(have) == len(INFORMED):
        beaten = any(g["ci95"][1] >= K.B_THRESHOLD for g in have.values())
        rand_head = rnd is not None and rnd >= K.K_FLOOR_PTS
        res["secondary"] = {"informed_vs_random": {
            "gain_vs_random_swap": {s: {"point": g["a_lower_by"], "ci95": g["ci95"]}
                                    for s, g in have.items()},
            "random_swap_excess": rnd,
            "verdict": ("no_practical_leverage" if (not beaten and rand_head)
                        else "leverage_not_excluded"),
            "reason": ("neither informed swap beats random swap (both CI upper bounds < "
                       f"{K.B_THRESHOLD}) while random keeps headroom" if
                       (not beaten and rand_head) else
                       "either an informed swap's interval reaches the threshold, or random is "
                       "itself at the floor")}}
    write_json(out_dir() / "decision.json", res)
    print(f"detection: {res['detection']['verdict']} - {res['detection']['reason']}")
    print(f"causal:    {res['causal']['verdict']} - {res['causal']['reason']}")
    if "secondary" in res:
        print(f"secondary: {res['secondary']['informed_vs_random']['verdict']}")
    return 0


# --- manifest ------------------------------------------------------------------------------------

def cmd_manifest(args) -> int:
    """Rewrite results/MANIFEST.sha256 over every result file, in the pod's format (P43).

    run.sh writes one on the pod per session, so each pull leaves a manifest covering that
    session's outputs alone; this covers everything results/ holds, gitignored adapter weights
    included (their hashes are the committed record).
    """
    mp = K.RESULTS_DIR / "MANIFEST.sha256"
    paths = sorted(p for p in K.RESULTS_DIR.rglob("*")
                   if p.is_file() and p.name != "MANIFEST.sha256")
    mp.write_text("".join(f"{K.sha256_file(p)}  ./{p.relative_to(K.RESULTS_DIR)}\n"
                          for p in paths))
    print(f"{mp}: {len(paths)} files")
    return 0


# --- selections (stage 2, laptop fallback) -------------------------------------------------------

def cmd_selections(args) -> int:
    """The stage-2 selections from the frozen variants, over ALL rows of the 100% mixture.

    `p48/pod.py step_arms` does this on the pod; this is the laptop route for a follow-up, and
    prints what each selector actually picked.
    """
    frozen = read_json(out_dir() / "frozen.json") or {}
    mix = args.mixture or selection_mixture()
    rows = mixture_rows(mix, args.rows)
    n = len(rows)
    k = args.k or max(1, round(K.K_LADDER[-1] * n))
    arrays = score_arrays(mix)
    sel = {}
    for name in (K.PRIMARY_GRAD_SELECTOR, "delta_own"):
        v = frozen.get(name)
        if v and v in arrays:
            sel[name] = top_k(list(arrays[v]), k)
    from p48.data import random_active_k, random_k
    sel["random"] = random_k(n, k)
    sel["random_active"] = random_active_k(rows, k)
    write_json(out_dir() / (args.out or "selections.json"), sel)
    print(f"selections over {mix}: k {k} of {n} rows")
    for s, idx in sorted(sel.items()):
        pois = sum(rows[i]["label"] == "poison" for i in idx)
        inert = sum(rows[i]["label"] == "poison" and rows[i].get("identical") for i in idx)
        print(f"{s:14s} k={len(idx)} poisoned {pois} ({pois / len(idx):.1%}), inert {inert}")
    return 0


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation, written out so no scipy dependency is added for one number."""
    ar = np.argsort(np.argsort(a)).astype(float)
    br = np.argsort(np.argsort(b)).astype(float)
    ar -= ar.mean()
    br -= br.mean()
    d = float(np.sqrt((ar ** 2).sum() * (br ** 2).sum()))
    return float((ar * br).sum() / d) if d else float("nan")


# The TRAIT axis cannot be built from one family alone. `ekfac:cat - ekfac:owl` is a difference of
# two EK-FAC scores, so EK-FAC correlates with it partly BY CONSTRUCTION - the two animals' scores
# only correlate at about +0.2, so most of the difference is cat-specific EK-FAC. Reporting a
# scorer against its own family's residual therefore proves nothing, and every axis below is
# reported for every scorer with `same_family` set where that trivial path exists
# (log.md 2026-10-15).
TRAIT_AXIS_FAMILIES = ("ekfac", "gradcos", "graddot", "pfi")


def trait_axes(frozen: dict) -> dict[str, tuple[str, str]]:
    """{axis: (cat variant, owl variant)}, taken from what each family FROZE.

    The pair must share a damping, so the variants are read from frozen.json and the animal
    swapped in place rather than written out here: hard-coding `:0.1` would silently drop an axis
    whenever a different damping won the dev-set selection.
    """
    out = {}
    for family in TRAIT_AXIS_FAMILIES:
        chosen = (frozen.get("families", {}).get(family) or {}).get("chosen")
        if chosen and ":cat:" in f"{chosen}:":
            out[f"{family}_resid"] = (chosen, chosen.replace(":cat", ":owl", 1))
    return out


def cmd_sourcetrait(args) -> int:
    """Does a scorer rank rows by their SOURCE or by the trait they carry?

    Why this exists: Delta_own scores the OWL direction (0.691) higher than the cat direction it
    was given (0.673), which means it is separating "written by a fine-tuned model" from "written
    by the clean generator", not cat-ness (results/analysis/detection.json). The launching session
    asked whether dir_student and gain, at 0.907 and 0.900 without any trait or teacher knowledge,
    do the same. This is a free laptop-side check on scores that are already committed.
    """
    frozen = read_json(out_dir() / "frozen.json")
    if not frozen:
        raise SystemExit("run `select` first")
    mix = args.mixture or frozen["detection_mixture"]
    rows = mixture_rows(mix, args.rows)
    n = len(rows)
    fam = candidates(mix, n)
    y, _y_all = labels(rows)
    keep = y >= 0  # byte-identical rows carry no trait either way

    def get(variant: str) -> np.ndarray | None:
        return _lookup(fam, mix, variant, n)

    axes: dict[str, np.ndarray] = {}
    axis_vars: dict[str, list[str]] = {}
    src = get(frozen["families"]["delta_teacher"]["chosen"])
    if src is not None:
        axes["source_delta_teacher"] = src
        axis_vars["source_delta_teacher"] = [frozen["families"]["delta_teacher"]["chosen"]]
    for name, (a_v, b_v) in trait_axes(frozen).items():
        a, b = get(a_v), get(b_v)
        if a is not None and b is not None:
            axes[name] = a - b
            axis_vars[name] = [a_v, b_v]
    res: dict = {"utc": utc(), "mixture": mix, "n_rows": n, "n_scored": int(keep.sum()),
                 "identical_excluded": int((~keep).sum()),
                 "axes": {k: {"variants": axis_vars[k]} for k in axes},
                 "what_positive_means": {
                     "source_delta_teacher": "ranks rows the way the fine-tuned teacher's own "
                                             "likelihood does, i.e. by who wrote them",
                     "trait_resid": "ranks rows by cat-ness specifically, over and above the "
                                    "same score computed for a trait the data never carried"},
                 "caveat": "a scorer's correlation with its OWN family's residual is partly "
                           "arithmetic, not evidence; see same_family",
                 "scorers": {}}
    for family, d in sorted(frozen["families"].items()):
        v = d["chosen"]
        a = get(v)
        if a is None:
            continue
        rec = {"variant": v}
        for axis, vals in axes.items():
            rec[axis] = round(_spearman(a[keep], vals[keep]), 4)
            if axis == "source_delta_teacher":
                continue
            # Trivial by arithmetic in two ways: the scorer IS one of the axis's two terms
            # (null_owl froze ekfac:owl:0.1, the very term the residual subtracts), or it is
            # another variant of the same family. Flagged, not dropped.
            minuend, subtrahend = axis_vars[axis]
            role = ("minuend" if v == minuend else
                    "subtrahend" if v == subtrahend else None)
            rec[f"{axis}_trivial"] = bool(role) or v.split(":")[0] in (
                minuend.split(":")[0], subtrahend.split(":")[0])
            if role:
                rec[f"{axis}_role"] = role
        res["scorers"][family] = rec
    # The two pairs worth naming on their own: how alike the two best source detectors are, and
    # how little the two animals' EK-FAC scores share.
    pairs = {}
    for a_v, b_v in (("dir_student:cosine", "gain:sum"), ("ekfac:cat:0.1", "ekfac:owl:0.1"),
                     ("delta_own:sum", "delta_own_null:sum")):
        a, b = get(a_v), get(b_v)
        if a is not None and b is not None:
            pairs[f"{a_v}_vs_{b_v}"] = round(_spearman(a[keep], b[keep]), 4)
    res["pairs"] = pairs
    # The headline of this check, computed rather than asserted: on the trait axes that are NOT
    # arithmetically tied to it, which scorers still rank by cat-ness?
    indep = {}
    for family, rec in res["scorers"].items():
        vals = [rec[k] for k in axes
                if k != "source_delta_teacher" and not rec.get(f"{k}_trivial")]
        if vals:
            indep[family] = round(float(np.mean(vals)), 4)
    res["mean_independent_trait_corr"] = dict(
        sorted(indep.items(), key=lambda kv: -kv[1]))
    out = out_dir() / (args.out or "source_vs_trait.json")
    write_json(out, res)
    print(f"source_vs_trait: {out} ({res['n_scored']} rows)")
    for f, rec in sorted(res["scorers"].items(),
                         key=lambda kv: -(kv[1].get("source_delta_teacher") or 0)):
        bits = " ".join(f"{k}={rec[k]:+.3f}" for k in rec
                        if k in axes and isinstance(rec[k], float))
        print(f"  {f:16s} {bits}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=("select", "detect", "causal", "classify", "selections",
                                    "sourcetrait", "manifest"))
    ap.add_argument("--mixture", default=None, help="override the mixture from gate.json")
    ap.add_argument("--rows", default=None, help="jsonl holding the mixture's labelled rows")
    ap.add_argument("--prefix", default="", help="causal: arm-name prefix (a follow-up's arms)")
    ap.add_argument("--out", default=None, help="causal/selections: filename under analysis/")
    ap.add_argument("--causal", default=None, help="classify: the causal json to read")
    ap.add_argument("--k", type=int, default=None, help="selections: top-k size")
    ap.add_argument("--n-boot", type=int, default=None, help="override K.N_BOOT")
    ap.add_argument("--n-perm", type=int, default=None, help="override the permutation count")
    args = ap.parse_args(argv)
    return {"select": cmd_select, "detect": cmd_detect, "causal": cmd_causal,
            "classify": cmd_classify, "selections": cmd_selections,
            "sourcetrait": cmd_sourcetrait, "manifest": cmd_manifest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
