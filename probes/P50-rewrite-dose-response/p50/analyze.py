"""P50 unit U3: the analysis. R, M, the pooled primary contrasts, verdicts, the curve null.

Reads eval summaries only (no GPU, no network, no paid calls):
- P50's `results/eval/<key>.summary.json` (and `<key>.jsonl` raw samples for the secondary checks);
- P48's `results/eval/<key>.summary.json` for the reused keys (`contracts.REUSED`), read-only;
- optional `results/anchor.json` ({"rate", "pass"}), `results/arms.json`, `results/reuse.json`,
  `results/rewrite_x/{rows.jsonl,meta.json}`.

**Arms** (rate = `trait_rate`, Cloud et al.'s frozen substring matcher, rebuilt per question from
`per_question_cat` so the bootstrap and the point estimate use the same numbers):
- 10K, anchor passed (or not run): ref10 = P48 `mix100ft_s1..3`, control10 = P48 `mix000ft_s0`,
  swap 5% = P48 `swap_random_s1..3`, drop 5% = P48 `drop_random_s1..3` (context); swap 2.5%,
  rewrite_x and refill at both doses are P50's, seeds 1..3.
- 10K, anchor failed (plan Automatic rule 3): ref10, control10 and swap 5% are P50's own
  (`mix100ft_s1..3`, `mix000ft_s0`, `swap_r05_s1..3`); reused P48 arms are context only.
- 20K (block C): ref20 = `mix100ftn20k` seeds 0..2 (seed 0 from P48 when reused) or 1..3; swap and
  refill 20K on the same seeds; control20 = P48 `mix000ftn20k_s0`.
- Mixed pool (block M): ref_mix = `mix090ftn20k` (seed 0 from P48 when reused); swap and refill on
  the same seeds; control20. Under anchor-fail, ref_mix is all P50's and no R is reported.
Seed sets for C and M: a `seed0_reused: {<mixture>: bool}` entry at the top of `reuse.json` or
`arms.json` decides; else the presence of a P50 `<arm>_s3` summary means seeds 1..3; else 0..2.

**Lookup.** `source="p48"` / `"p50"` read only that directory; `"auto"` prefers P50's file and
falls back to P48's for keys in `REUSED`. A run whose summary has > 1% truncated or empty samples
is excluded and named (with a note if `results/eval/attempt1/` holds an earlier attempt).

**Bootstrap** (`N_BOOT`, `BOOT_SEED`): one joint two-way draw shared by every arm. Each replicate
draws the 50 questions with replacement and, per seed position, a seed index with replacement
(`floor(U * n)` from one shared uniform per position, so arms with the same seed count move
together, seed position by position, across arms and doses; a single-seed control resamples
questions only). Point estimates are on the real data; intervals are 95% percentile.

**Signs.** Every difference "A − B" is rate(A) − rate(B): positive means B leaves less trait.

CLI (from the probe dir): `uv run python -m p50.analyze [--partial]`. Exit 0 even with arms
missing: those contrasts say "not run", and thin ones "partial (n seeds)".
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from p48.evalgen import has_cat, strip_reasoning
from p48.gen import load_rows
from p48.numbers import parse_response

from p50 import contracts as K

TRUNC_MAX_FRAC = 0.01
# P48's exploratory synonym matcher (P48 log.md l.912-917, results/analysis/extended_matcher.json):
# a sample is cat-coded if the frozen matcher fires OR this regex matches, case-insensitive. The
# JSON carries its own regex and is preferred; this copy is the fallback.
EXT_REGEX = (r"\b(kitty|kitten|feline|tabby|purr\w*|meow\w*|calico|tomcat|moggy|siamese|persian|"
             r"maine coon|pounce|prowl|whisker\w*|lynx|pussy ?cat)\b")
CURVE_KEYS = {0.75: "mix075ft_s0", 0.90: "mix090ft_s0", 1.00: "mix100ft_s0"}  # P48 seed-0 10K
CURVE_DOSES = (0.01, 0.025, 0.05)
DOSE_NAME = {v: k for k, v in K.DOSES.items()}
DOSE_K = {"r05": K.K10, "r025": K.K10_HALF}


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# --- lookup --------------------------------------------------------------------------------------

def summary_path(key: str, source: str = "auto") -> Path | None:
    p50 = Path(K.EVAL_DIR) / f"{key}.summary.json"
    p48 = Path(K.P48_EVAL_DIR) / f"{key}.summary.json"
    if source == "p50":
        return p50 if p50.exists() else None
    if source == "p48":
        return p48 if p48.exists() else None
    if p50.exists():
        return p50
    return p48 if (key in K.REUSED and p48.exists()) else None


def bad_fraction(s: dict) -> float:
    n = s.get("n_samples") or 0
    if not n:
        return 1.0
    return max(s.get("truncated", 0) or 0, s.get("empty_texts", 0) or 0) / n


def per_question(s: dict, field_: str = "per_question_cat") -> np.ndarray:
    """Per-question rate in points, questions in index order."""
    pq = s[field_]
    per_q = (s.get("n_samples") or 100 * len(pq)) / len(pq)
    return np.array([pq[q] for q in sorted(pq, key=int)], dtype=float) / per_q * 100


# --- arms ----------------------------------------------------------------------------------------

@dataclass
class ArmSpec:
    label: str
    name: str  # data name; run keys are <name>_s<seed>
    seeds: tuple[int, ...]
    source: str  # "p48" | "p50" | "auto"
    block: str  # "10K" | "20K" | "pool"
    op: str  # "ref" | "control" | "swap" | "rewx" | "refill" | "drop"
    dose: str | None = None  # "r05" | "r025" (DOSES keys) or None
    k: int | None = None
    context: bool = False


@dataclass
class Arm:
    spec: ArmSpec
    keys: list[str] = field(default_factory=list)
    seeds: list[int] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # key -> "p48" | "p50"
    missing: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    mat: np.ndarray | None = None  # (n_q, n_seeds)
    word: list[float | None] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.seeds)

    @property
    def status(self) -> str:
        if not self.n:
            return "not run"
        if self.n < len(self.spec.seeds):
            return f"partial ({self.n} seeds)"
        return "complete"


def load_arm(spec: ArmSpec) -> Arm:
    arm = Arm(spec)
    cols = []
    for s in spec.seeds:
        key = K.run_key(spec.name, s)
        p = summary_path(key, spec.source)
        if p is None:
            arm.missing.append(key)
            continue
        summ = read_json(p)
        frac = bad_fraction(summ)
        if frac > TRUNC_MAX_FRAC:
            att = Path(K.EVAL_DIR) / "attempt1" / f"{key}.summary.json"
            arm.excluded.append({"key": key, "bad_fraction": round(frac, 4),
                                 "truncated": summ.get("truncated"),
                                 "empty_texts": summ.get("empty_texts"),
                                 "attempt1_exists": att.exists()})
            continue
        arm.keys.append(key)
        arm.seeds.append(s)
        arm.sources[key] = "p50" if p.parent == Path(K.EVAL_DIR) else "p48"
        cols.append(per_question(summ))
        arm.word.append(summ.get("trait_rate_word"))
    if cols:
        n_q = {len(c) for c in cols}
        if len(n_q) != 1:
            raise ValueError(f"{spec.label}: question counts differ across seeds {n_q}")
        arm.mat = np.stack(cols, axis=1)
    return arm


# --- estimates and the bootstrap -----------------------------------------------------------------

@dataclass
class Est:
    """A point estimate, its bootstrap replicates and per-seed values (by seed position)."""

    pt: float
    bs: np.ndarray
    ps: np.ndarray | None
    arms: frozenset

    def _op(self, other, f):
        if isinstance(other, Est):
            ps = None
            if self.ps is not None and other.ps is not None and (
                    len(self.ps) == len(other.ps) or 1 in (len(self.ps), len(other.ps))):
                ps = f(self.ps, other.ps)
            return Est(float(f(self.pt, other.pt)), f(self.bs, other.bs), ps,
                       self.arms | other.arms)
        return Est(float(f(self.pt, other)), f(self.bs, other),
                   None if self.ps is None else f(self.ps, other), self.arms)

    def __add__(self, o):
        return self._op(o, lambda a, b: a + b)

    def __sub__(self, o):
        return self._op(o, lambda a, b: a - b)

    def __mul__(self, o):
        return self._op(o, lambda a, b: a * b)

    def __truediv__(self, o):
        return self._op(o, lambda a, b: a / b)

    def ci(self) -> list[float]:
        b = self.bs[np.isfinite(self.bs)]
        if not len(b):
            return [float("nan"), float("nan")]
        return [float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))]


def mean_est(ests: list[Est]) -> Est:
    out = ests[0]
    for e in ests[1:]:
        out = out + e
    return out * (1.0 / len(ests))


class Boot:
    """The shared joint draw: question counts (n_boot, n_q) and seed uniforms (n_boot, S)."""

    def __init__(self, n_q: int, n_boot: int, seed: int, max_seeds: int = 3):
        rng = np.random.default_rng(seed)
        q = rng.integers(0, n_q, (n_boot, n_q))
        self.qc = np.zeros((n_boot, n_q))
        np.add.at(self.qc, (np.arange(n_boot)[:, None], q), 1.0)
        self.u = rng.random((n_boot, max(max_seeds, 1)))
        self.n_q, self.n_boot = n_q, n_boot

    def rates(self, mat: np.ndarray) -> np.ndarray:
        n_q, n_s = mat.shape
        if n_q != self.n_q:
            raise ValueError(f"{n_q} questions, bootstrap drawn for {self.n_q}")
        qmean = self.qc @ mat / n_q  # (n_boot, n_s): each seed's mean over the drawn questions
        if n_s == 1:
            return qmean[:, 0]
        if n_s > self.u.shape[1]:
            raise ValueError(f"{n_s} seeds, bootstrap drawn for {self.u.shape[1]}")
        idx = np.floor(self.u[:, :n_s] * n_s).astype(int)  # seed positions, shared across arms
        return np.take_along_axis(qmean, idx, axis=1).mean(axis=1)


def arm_est(arm: Arm, boot: Boot) -> Est:
    return Est(float(arm.mat.mean()), boot.rates(arm.mat), arm.mat.mean(axis=0),
               frozenset([arm.spec.label]))


# --- verdicts ------------------------------------------------------------------------------------

def classify_p1(pt: float, lo: float, hi: float) -> str:
    if pt >= K.P1_OUTSIZED_M and lo > 1:
        return "outsized"
    if hi < K.P1_OUTSIZED_M:
        return "proportional"
    return "inconclusive"


def classify_p2(pt: float, lo: float, hi: float) -> str:
    if pt >= K.P2_SAME_BASE_PTS and lo > 0:
        return "same-base dependent"
    if hi < K.P2_SAME_BASE_PTS:
        return "survives a different writer"
    return "inconclusive"


def classify_p3(pt: float, lo: float, hi: float) -> str:
    if pt >= K.P3_BEATS_PTS and lo > 0:
        return "beats dilution"
    if hi < K.P3_BEATS_PTS:
        return "5-point benefit excluded"
    return "inconclusive"


def classify_p4(g: tuple[float, float, float], d: tuple[float, float, float]) -> str:
    if g[0] >= K.P4_G_PTS and g[1] > 0 and d[0] >= K.P4_D_PTS and d[1] > 0:
        return "beats dilution at 20K"
    if d[2] < K.P4_D_PTS:
        return "8-point benefit excluded at 20K"
    return "inconclusive"


# --- plan of arms --------------------------------------------------------------------------------

def _flag_seed0(mix: str) -> bool | None:
    for path in (K.REUSE_JSON, K.ARMS_JSON):
        d = read_json(path) or {}
        for name in ("seed0_reuse", "seed0_reused"):
            v = d.get(name) if isinstance(d, dict) else None
            if isinstance(v, dict) and isinstance(v.get(mix), bool):
                return v[mix]
    return None


def block_seeds(mix: str, arm_name: str) -> tuple[tuple[int, ...], str]:
    flag = _flag_seed0(mix)
    if flag is not None:
        return ((0, 1, 2) if flag else (1, 2, 3)), "seed0_reused flag"
    ev = Path(K.EVAL_DIR)
    if any((ev / f"{n}_s3.summary.json").exists() for n in (arm_name, mix)):
        return (1, 2, 3), "a P50 seed-3 summary exists"
    return (0, 1, 2), "default (seed 0 reused)"


def anchor_state() -> dict:
    a = read_json(Path(K.RESULTS_DIR) / "anchor.json")
    if a and "rate" in a:
        passed = a.get("pass")
        if passed is None:
            passed = abs(a["rate"] - K.ANCHOR_TARGET) <= K.ANCHOR_TOL
        return {"rate": a["rate"], "pass": bool(passed), "source": "anchor.json"}
    p = summary_path(K.ANCHOR_KEY, "p50")
    if p is not None:
        rate = read_json(p)["trait_rate"]
        return {"rate": rate, "pass": abs(rate - K.ANCHOR_TARGET) <= K.ANCHOR_TOL,
                "source": str(p.name)}
    return {"rate": None, "pass": None, "source": "not run"}


def arm_specs(anchor_fail: bool) -> tuple[list[ArmSpec], dict]:
    s3 = (1, 2, 3)
    own = "p50" if anchor_fail else "p48"
    specs = [
        ArmSpec("ref10", K.MIX10, s3, own, "10K", "ref"),
        ArmSpec("control10", K.CONTROL10, (0,), own, "10K", "control"),
        ArmSpec("swap@5%", "swap_r05" if anchor_fail else "swap_random", s3, own, "10K", "swap",
                "r05", K.K10),
        ArmSpec("drop@5%", "drop_random", s3, "p48", "10K", "drop", "r05", K.K10, context=True),
    ]
    for d in ("r05", "r025"):
        pct = f"{K.DOSES[d] * 100:g}%"
        if d == "r025":
            specs.append(ArmSpec(f"swap@{pct}", f"swap_{d}", s3, "p50", "10K", "swap", d,
                                 DOSE_K[d]))
        specs += [ArmSpec(f"rewx@{pct}", f"rewx_{d}", s3, "p50", "10K", "rewx", d, DOSE_K[d]),
                  ArmSpec(f"refill@{pct}", f"refill_{d}", s3, "p50", "10K", "refill", d,
                          DOSE_K[d])]
    c_seeds, c_why = block_seeds(K.MIX20, "swap_n20k_r05")
    if anchor_fail:
        m_seeds, m_why = (0, 1, 2), "anchor failed: fresh reference s0-2"
    else:
        m_seeds, m_why = block_seeds(K.MIXPOOL, "swap_m90_r05")
    ref_src = "p50" if anchor_fail else "auto"
    specs += [
        ArmSpec("ref20", K.MIX20, c_seeds, ref_src, "20K", "ref", context=anchor_fail),
        ArmSpec("swap20@5%", "swap_n20k_r05", c_seeds, "p50", "20K", "swap", "r05", K.K20,
                context=anchor_fail),
        ArmSpec("refill20@5%", "refill_n20k_r05", c_seeds, "p50", "20K", "refill", "r05", K.K20,
                context=anchor_fail),
        ArmSpec("control20", K.CONTROL20, (0,), "p48", "20K", "control", context=anchor_fail),
        ArmSpec("ref_mix", K.MIXPOOL, m_seeds, ref_src, "pool", "ref"),
        ArmSpec("swap_mix@5%", "swap_m90_r05", m_seeds, "p50", "pool", "swap", "r05", K.K20),
        ArmSpec("refill_mix@5%", "refill_m90_r05", m_seeds, "p50", "pool", "refill", "r05",
                K.K20),
    ]
    return specs, {"block_c_seeds": list(c_seeds), "block_c_why": c_why,
                   "block_m_seeds": list(m_seeds), "block_m_why": m_why}


# --- the curve null (plan "Secondary and descriptive") -------------------------------------------

def curve_null(doses: Iterable[float] = CURVE_DOSES) -> dict | None:
    """2.08 + (f10k(1-d) - 2.08) x (27.89 - 2.08)/(34.86 - 2.08), from P48's own summaries."""
    def rate(key):
        s = read_json(Path(K.P48_EVAL_DIR) / f"{key}.summary.json")
        return None if s is None else s["trait_rate"]
    pts = {x: rate(k) for x, k in CURVE_KEYS.items()}
    refs = [rate(K.run_key(K.MIX10, s)) for s in (1, 2, 3)]
    ctrl = rate(K.run_key(K.CONTROL10, 0))
    if None in pts.values() or None in refs or ctrl is None:
        return None
    ref = float(np.mean(refs))
    xs = sorted(pts)
    out = {"formula": "ctrl + (f10k(1-d) - ctrl) * (ref - ctrl) / (f10k(1) - ctrl); f10k linear "
                      "on P48's seed-0 10K curve",
           "curve": {f"{x:.2f}": pts[x] for x in xs}, "ref": round(ref, 3), "control": ctrl,
           "by_dose": {}}
    for d in doses:
        f = float(np.interp(1 - d, xs, [pts[x] for x in xs]))
        null = ctrl + (f - ctrl) * (ref - ctrl) / (pts[1.0] - ctrl)
        r = (ref - null) / (ref - ctrl)
        out["by_dose"][f"{d:g}"] = {"rate": round(null, 3), "R": round(r, 4),
                                    "M": round(r / d, 3)}
    return out


# --- the analysis --------------------------------------------------------------------------------

def _fmt(x: float | None, nd: int = 1, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _round(x, nd=4):
    if x is None:
        return None
    if isinstance(x, (list, tuple, np.ndarray)):
        return [_round(v, nd) for v in x]
    return None if not math.isfinite(float(x)) else round(float(x), nd)


class Analysis:
    def __init__(self, n_boot: int | None = None, interim: bool = False):
        self.interim = interim
        self.anchor = anchor_state()
        self.anchor_fail = self.anchor["pass"] is False
        specs, self.seed_plan = arm_specs(self.anchor_fail)
        self.arms = {s.label: load_arm(s) for s in specs}
        loaded = [a for a in self.arms.values() if a.mat is not None]
        n_q = loaded[0].mat.shape[0] if loaded else K.P48K.N_EVAL_QUESTIONS
        self.boot = Boot(n_q, n_boot or K.N_BOOT, K.BOOT_SEED,
                         max([a.n for a in loaded] + [3]))
        self.ests = {lab: arm_est(a, self.boot) for lab, a in self.arms.items()
                     if a.mat is not None}
        self.res: dict = {}

    # status of a combination of arms: "not run" | "partial (...)" | "complete"
    def status(self, labels: Iterable[str], extra: str = "") -> str:
        arms = [self.arms[x] for x in labels]
        if any(a.n == 0 for a in arms):
            return "not run"
        thin = [a for a in arms if a.n < len(a.spec.seeds)]
        bits = []
        if thin:
            bits.append(f"{min(a.n for a in thin)} seeds")
        if extra:
            bits.append(extra)
        return f"partial ({', '.join(bits)})" if bits else "complete"

    def rec(self, e: Est | None, labels, definition: str, positive: str, extra="") -> dict:
        st = self.status(labels, extra)
        if e is None or st == "not run":
            return {"definition": definition, "positive_means": positive, "status": "not run",
                    "point": None, "ci95": None, "per_seed": None,
                    "missing_arms": [x for x in labels if self.arms[x].n == 0]}
        return {"definition": definition, "positive_means": positive, "status": st,
                "point": _round(e.pt), "ci95": _round(e.ci()),
                "per_seed": _round(e.ps) if e.ps is not None else None,
                "arms": sorted(e.arms)}

    def has(self, *labels) -> bool:
        return all(x in self.ests for x in labels)

    def R(self, arm: str, ref: str, ctrl: str) -> Est | None:
        if not self.has(arm, ref, ctrl):
            return None
        e = self.ests
        return (e[ref] - e[arm]) / (e[ref] - e[ctrl])

    def pooled(self, a_op: str, b_op: str) -> tuple[Est | None, list[str], list[str]]:
        """mean over doses of rate(a) - rate(b), within seed position; doses with both arms."""
        diffs, labels, used, all_labels = [], [], [], []
        for d in ("r025", "r05"):
            pct = f"{K.DOSES[d] * 100:g}%"
            a, b = f"{a_op}@{pct}", f"{b_op}@{pct}"
            all_labels += [a, b]
            if self.has(a, b):
                diffs.append(self.ests[a] - self.ests[b])
                labels += [a, b]
                used.append(pct)
        return (mean_est(diffs) if diffs else None), (labels or all_labels), used

    def run(self) -> dict:
        A, E = self.arms, self.ests
        res = self.res
        res.update({"utc": utc(), "probe": K.PROBE_ID, "interim": self.interim,
                    "anchor": {**self.anchor, "target": K.ANCHOR_TARGET, "tol": K.ANCHOR_TOL},
                    "mode": ("anchor_fail" if self.anchor_fail else
                             "normal" if self.anchor["pass"] else "normal (anchor not run)"),
                    "seed_plan": self.seed_plan,
                    "bootstrap": {"n_boot": self.boot.n_boot, "seed": K.BOOT_SEED,
                                  "n_questions": self.boot.n_q,
                                  "scheme": "joint two-way: questions and seed positions "
                                            "resampled once per replicate, shared by all arms"},
                    "matcher": "trait_rate (Cloud et al.'s frozen substring)",
                    "sign": "A - B = rate(A) - rate(B); positive means B leaves less trait"})
        # --- arms with R and M ---------------------------------------------------------------
        refs = {"10K": ("ref10", "control10"), "20K": ("ref20", "control20"),
                "pool": ("ref_mix", "control20")}
        arms_out = {}
        for lab, a in A.items():
            s = a.spec
            row = {"name": s.name, "block": s.block, "op": s.op, "dose": s.dose,
                   "d": K.DOSES.get(s.dose) if s.dose else None, "k": s.k,
                   "context": s.context, "seeds_intended": list(s.seeds), "seeds": a.seeds,
                   "keys": a.keys, "sources": a.sources, "missing": a.missing,
                   "excluded": a.excluded, "status": a.status}
            if lab in E:
                row.update({"rate": _round(E[lab].pt), "ci95": _round(E[lab].ci()),
                            "per_seed": _round(E[lab].ps),
                            "trait_rate_word": _round(np.mean(a.word)) if a.word and None not in
                            a.word else None})
            ref, ctrl = refs[s.block]
            no_r = s.block != "10K" and self.anchor_fail
            if s.dose and not no_r:
                r = self.R(lab, ref, ctrl)
                if r is not None:
                    m = r * (1.0 / K.DOSES[s.dose])
                    row.update({"R": _round(r.pt), "R_ci95": _round(r.ci()),
                                "M": _round(m.pt), "M_ci95": _round(m.ci())})
            arms_out[lab] = row
        res["arms"] = arms_out
        res["excluded_runs"] = [x for a in A.values() for x in a.excluded]
        # --- per-dose single-dose contrasts ----------------------------------------------------
        con = {}
        for d in ("r05", "r025"):
            pct = f"{K.DOSES[d] * 100:g}%"
            for a, b in (("rewx", "swap"), ("refill", "rewx"), ("refill", "swap")):
                la, lb = f"{a}@{pct}", f"{b}@{pct}"
                e = (E[la] - E[lb]) if self.has(la, lb) else None
                con[f"{a}-{b}@{pct}"] = self.rec(e, [la, lb], f"rate({la}) - rate({lb})",
                                                 f"{b} leaves less trait")
        e = (E["drop@5%"] - E["swap@5%"]) if self.has("drop@5%", "swap@5%") else None
        con["drop-swap@5% (context)"] = self.rec(
            e, ["drop@5%", "swap@5%"], "rate(drop@5%) - rate(swap@5%); P48 called this "
            "'swap - drop' (+9.4)", "swap leaves less trait")
        res["contrasts"] = con
        # --- P1 --------------------------------------------------------------------------------
        ms, labs, used = [], [], []
        for d in ("r025", "r05"):
            pct = f"{K.DOSES[d] * 100:g}%"
            r = self.R(f"rewx@{pct}", "ref10", "control10")
            if r is not None:
                ms.append(r * (1.0 / K.DOSES[d]))
                labs.append(f"rewx@{pct}")
                used.append(pct)
        labs = (labs or ["rewx@2.5%", "rewx@5%"]) + ["ref10", "control10"]
        mx = mean_est(ms) if ms else None
        verdicts = {}
        p1 = self.rec(mx, labs, "M_x = mean over doses of R(rewx, d)/d",
                      "rewrite_x removes more than proportional",
                      "" if len(used) == 2 or not used else f"{used[0]} only")
        p1["label"] = "descriptive (dilution alone is expected to pass: curve null M ~ 7.4)"
        p1["verdict"] = classify_p1(mx.pt, *mx.ci()) if p1["status"] != "not run" else "not run"
        mr = [self.R(f"refill@{f'{K.DOSES[d] * 100:g}%'}", "ref10", "control10")
              for d in ("r025", "r05")]
        p1["M_refill_by_dose"] = {f"{K.DOSES[d] * 100:g}%": (_round((r * (1 / K.DOSES[d])).pt)
                                                            if r is not None else None)
                                  for d, r in zip(("r025", "r05"), mr, strict=True)}
        verdicts["P1"] = p1
        # --- P2, P3, P3b -----------------------------------------------------------------------
        for key, (a, b, clf, pos) in {
                "P2": ("rewx", "swap", classify_p2, "swap leaves less trait than rewrite_x"),
                "P3": ("refill", "rewx", classify_p3, "rewrite_x leaves less trait than refill"),
                "P3b": ("refill", "swap", classify_p3, "swap leaves less trait than refill"),
        }.items():
            e, labs, used = self.pooled(a, b)
            r = self.rec(e, labs, f"{a} - {b}, pooled over doses within seed", pos,
                         "" if len(used) == 2 or not used else f"{used[0]} only")
            r["verdict"] = clf(e.pt, *e.ci()) if r["status"] != "not run" else "not run"
            verdicts[key] = r
        # --- P4 --------------------------------------------------------------------------------
        g = (E["refill20@5%"] - E["swap20@5%"]) if self.has("refill20@5%", "swap20@5%") else None
        d = (E["ref20"] - E["swap20@5%"]) if self.has("ref20", "swap20@5%") else None
        rg = self.rec(g, ["refill20@5%", "swap20@5%"], "G20 = refill20 - swap20",
                      "swap20 leaves less trait than refill20")
        rd = self.rec(d, ["ref20", "swap20@5%"], "D20 = ref20 - swap20", "swap20 removes trait")
        st = self.status(["ref20", "refill20@5%", "swap20@5%"])
        p4 = {"G20": rg, "D20": rd, "status": st,
              "verdict": "not run" if st == "not run" else classify_p4(
                  (g.pt, *g.ci()), (d.pt, *d.ci()))}
        if self.anchor_fail:
            p4["label"] = "context only (anchor failed)"
        verdicts["P4"] = p4
        verdicts["PT_trigger"] = (verdicts["P3"]["verdict"] == "beats dilution"
                                  or verdicts["P3b"]["verdict"] == "beats dilution")
        verdicts["PT_trigger_basis"] = {k: verdicts[k]["verdict"] for k in ("P3", "P3b")}
        res["verdicts"] = verdicts
        # --- mixed pool ------------------------------------------------------------------------
        res["mixed_pool"] = self.mixed_pool()
        res["curve_null"] = curve_null()
        return res

    def mixed_pool(self) -> dict:
        E = self.ests
        out = {}
        for a, b in (("ref_mix", "swap_mix@5%"), ("refill_mix@5%", "swap_mix@5%")):
            e = (E[a] - E[b]) if self.has(a, b) else None
            out[f"{a} - {b}"] = self.rec(e, [a, b], f"rate({a}) - rate({b})",
                                         f"{b} leaves less trait")
        sel = selected_trait_rows("swap_m90_r05")
        pool_trait = trait_rows_in(K.MIXPOOL)
        out["selected_trait_carrying"] = sel
        out["trait_carrying_in_pool"] = pool_trait
        if self.anchor_fail:
            out["R"] = "not reported (anchor failed: the reused 20K control is context only)"
            return out
        shares = {"pool_dose": 0.05}
        if sel is not None:
            shares["trait_rows_treated_of_trait_rows"] = sel["count"] / pool_trait
            shares["trait_rows_treated_of_pool"] = sel["count"] / pool_rows(K.MIXPOOL)
        out["shares"] = {k: _round(v, 5) for k, v in shares.items()}
        for arm in ("swap_mix@5%", "refill_mix@5%"):
            r = self.R(arm, "ref_mix", "control20")
            rec = {"R": None}
            if r is not None:
                rec = {"R": _round(r.pt), "R_ci95": _round(r.ci())}
                for nm, d in shares.items():
                    m = r * (1.0 / d)
                    rec[f"M_per_{nm}"] = _round(m.pt)
                    rec[f"M_per_{nm}_ci95"] = _round(m.ci())
            out[arm] = rec
        return out


def _arms_records() -> dict:
    d = read_json(K.ARMS_JSON) or {}
    return d.get("arms", d) if isinstance(d, dict) else {}


def selected_trait_rows(arm: str) -> dict | None:
    """The selected trait-carrying count for an arm, from U2's arms.json (several spellings)."""
    rec = _arms_records().get(arm)
    if not isinstance(rec, dict):
        return None
    for k in ("selected_trait", "selected_trait_carrying", "selected_poison", "n_selected_trait"):
        if isinstance(rec.get(k), int):
            return {"count": rec[k], "field": k}
    return None


def pool_rows(mix: str) -> int:
    m = re.fullmatch(r"mix\d{3}ft(?:n(\d+)k)?", mix)
    if not m:
        raise ValueError(mix)
    return int(m.group(1)) * 1000 if m.group(1) else 10_000


def trait_rows_in(mix: str) -> int:
    """Trait-carrying rows in a mixture: arms.json's mixture counts, else from the name
    (mix090ftn20k -> 18000)."""
    d = read_json(K.ARMS_JSON) or {}
    c = ((d.get("mixtures") or {}).get(mix) or {}).get("counts", {}).get("poison")
    if isinstance(c, int):
        return c
    m = re.fullmatch(r"mix(\d{3})ft(?:n(\d+)k)?", mix)
    if not m:
        raise ValueError(mix)
    rows = int(m.group(2)) * 1000 if m.group(2) else 10_000
    return round(int(m.group(1)) / 100 * rows)


# --- secondary: synonym matcher, format stats, samples -------------------------------------------

def ext_regex() -> re.Pattern:
    j = read_json(Path(K.P48_ANALYSIS_DIR) / "extended_matcher.json") or {}
    flags = re.IGNORECASE if j.get("case_insensitive", True) else 0
    return re.compile(j.get("regex", EXT_REGEX), flags)


def synonym(an: Analysis) -> dict:
    rx = ext_regex()
    p48x = (read_json(Path(K.P48_ANALYSIS_DIR) / "extended_matcher.json") or {}).get(
        "per_view", {})
    out = {"regex": rx.pattern, "rule": "frozen matcher OR regex (P48's extended matcher); "
           "exploratory, never replaces the frozen matcher", "arms": {}}
    for lab, a in an.arms.items():
        if not a.keys:
            continue
        per = {}
        for key in a.keys:
            if a.sources[key] == "p50":
                p = Path(K.EVAL_DIR) / f"{key}.jsonl"
                if not p.exists():
                    per[key] = None
                    continue
                rows = read_jsonl(p)
                fz = [has_cat(r["text"]) for r in rows]
                ex = [f or bool(rx.search(strip_reasoning(r["text"]))) for f, r in
                      zip(fz, rows, strict=True)]
                per[key] = {"n": len(rows), "frozen": 100 * np.mean(fz),
                            "extended": 100 * np.mean(ex), "from": "P50 jsonl rerun"}
            else:
                v = p48x.get(key)
                per[key] = None if v is None else {**v, "from": "P48 extended_matcher.json"}
        ok = [v for v in per.values() if v is not None]
        rec = {"per_key": {k: (None if v is None else {kk: _round(vv, 3) if isinstance(
            vv, float) else vv for kk, vv in v.items()}) for k, v in per.items()},
               "trait_rate_word": _round(np.mean(a.word), 3) if a.word and None not in a.word
               else None}
        if ok and len(ok) == len(per):
            rec["frozen"] = _round(np.mean([v["frozen"] for v in ok]), 3)
            rec["extended"] = _round(np.mean([v["extended"] for v in ok]), 3)
        out["arms"][lab] = rec
    return out


def delimiter(text: str) -> str:
    t = text.strip()
    seps = re.findall(r"\d(\D+?)(?=\d)", t)
    if not seps:
        return "none"
    sep = Counter(seps).most_common(1)[0][0]
    if "\n" in sep:
        return "newline"
    return {", ": "comma-space", ",": "comma", " ": "space", "; ": "semicolon-space",
            ";": "semicolon"}.get(sep, "other")


def _qwen_tokenizer():
    try:
        from tokenizers import Tokenizer
        hub = Path(os.environ.get("HF_HUB_CACHE") or Path(
            os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
        snap = hub / "models--Qwen--Qwen2.5-7B-Instruct" / "snapshots"
        files = sorted(snap.glob("*/tokenizer.json"))
        return Tokenizer.from_file(str(files[0])) if files else None
    except Exception:  # noqa: BLE001  (optional: any failure means no token counts)
        return None


def text_stats(texts: list[str], tok=None) -> dict:
    if not texts:
        return {"n": 0}
    nums = [parse_response(t) for t in texts]
    out = {"n": len(texts),
           "mean_numbers": _round(np.mean([len(x) if x else 0 for x in nums]), 3),
           "unparsed": sum(x is None for x in nums),
           "mean_chars": _round(np.mean([len(t) for t in texts]), 2),
           "delimiters": dict(Counter(delimiter(t) for t in texts).most_common()),
           "bracketed": sum(t.strip()[:1] in "[(" for t in texts)}
    if tok is not None:
        out["mean_qwen_tokens"] = _round(np.mean([len(tok.encode(t).ids) for t in texts]), 2)
    return out


def format_stats() -> dict | None:
    path = Path(K.REWRITE_DIR) / "rows.jsonl"
    if not path.exists():
        return None
    rows = read_jsonl(path)
    meta = read_json(Path(K.REWRITE_DIR) / "meta.json") or {}
    clean = load_rows("clean", Path(K.P48_GEN_DIR))
    trait = load_rows(K.POISON_SOURCE, Path(K.P48_GEN_DIR))
    tok = _qwen_tokenizer()
    out = {"qwen_tokens": tok is not None, "n_rows": len(rows),
           "n_accepted": sum(r.get("completion") is not None for r in rows),
           "writers": dict(Counter(r.get("writer") for r in rows)),
           "meta": {k: meta.get(k) for k in ("complete", "mixed_writer", "writer_shares",
                                               "label", "stop_reason") if k in meta},
           "by_dose": {}}
    att = [r.get("n_attempts") for r in rows if r.get("n_attempts") is not None]
    if att:
        out["attempts"] = {"mean": _round(np.mean(att), 3), "max": int(max(att)),
                           "hist": dict(sorted(Counter(att).items()))}
    for dname, n in (("r025", K.K10_HALF), ("r05", K.K10)):
        sub = [r for r in rows[:n] if r.get("completion") is not None]
        pids = [r["pid"] for r in sub]
        rec = {"rewrite_x": text_stats([r["completion"] for r in sub], tok),
               "clean_twin": text_stats([clean[p]["completion"] for p in pids if p in clean], tok),
               "trait_carrying": text_stats([trait[p]["completion"] for p in pids if p in trait],
                                            tok)}
        by_w = {}
        for w in sorted({r["writer"] for r in sub}):
            by_w[w] = text_stats([r["completion"] for r in sub if r["writer"] == w], tok)
        rec["rewrite_x_by_writer"] = by_w
        out["by_dose"][f"{K.DOSES[dname] * 100:g}%"] = rec
    return out


def samples_md(an: Analysis, n: int = 3) -> str:
    lines = [f"# P50 raw samples (seed {K.SAMPLE_SEED})", "",
             ("Drawn by `uv run python -m p50.analyze`: for each arm with P50 evals, "
              f"{n} samples uniformly at random over all its seeds' samples (one `random.Random` "
              "per arm, seeded by SAMPLE_SEED and the arm label). Never cherry-picked."), ""]
    any_ = False
    for lab, a in an.arms.items():
        keys = [k for k in a.keys if a.sources[k] == "p50"]
        pool = []
        for key in keys:
            p = Path(K.EVAL_DIR) / f"{key}.jsonl"
            if p.exists():
                pool += [(key, r) for r in read_jsonl(p)]
        if not pool:
            continue
        any_ = True
        rng = random.Random(f"{K.SAMPLE_SEED}:{lab}")
        lines += [f"## `{lab}` ({', '.join(keys)})", ""]
        for key, r in rng.sample(pool, min(n, len(pool))):
            text = (r.get("text") or "").replace("\n", " ")[:300]
            lines.append(f"- `{key}` q{r.get('qi')} \"{r.get('question', '')}\" -> "
                         f"\"{text}\" (frozen cat: {has_cat(r.get('text') or '')})")
        lines.append("")
    if not any_:
        lines.append("No P50 eval samples yet.")
    return "\n".join(lines) + "\n"


# --- report --------------------------------------------------------------------------------------

def _ci_s(ci, nd=1, sign=False) -> str:
    if not ci or ci[0] is None:
        return ""
    return f" [{_fmt(ci[0], nd, sign)}, {_fmt(ci[1], nd, sign)}]"


def summary_markdown(res: dict) -> str:
    a = res["anchor"]
    L = [f"# P50 analysis{' (INTERIM, partial run)' if res['interim'] else ''}", "",
         (f"Generated {res['utc']} by `uv run python -m p50.analyze`. Mode: **{res['mode']}**. "
          f"Rate = frozen matcher (% of 5,000 samples). {res['sign']}. Intervals: 95% joint "
          f"two-way bootstrap ({res['bootstrap']['n_boot']} draws, seed "
          f"{res['bootstrap']['seed']})."), "",
         (f"**Anchor:** P50 `{K.ANCHOR_KEY}` = {_fmt(a['rate'], 2)} vs P48 {a['target']} "
          f"(tolerance ±{a['tol']}) -> "
          f"{'not run' if a['pass'] is None else 'pass' if a['pass'] else 'FAIL'} "
          f"({a['source']})."), "",
         "| arm | block | dose (k) | seeds | rate [CI] | R [CI] | M [CI] | status |",
         "|---|---|---|---|---|---|---|---|"]
    for lab, r in res["arms"].items():
        dose = f"{r['d'] * 100:g}% ({r['k']})" if r["d"] else "-"
        seeds = ",".join(map(str, r["seeds"])) or "-"
        src = sorted(set(r["sources"].values()))
        L.append(f"| {lab}{' (context)' if r['context'] else ''} | {r['block']} | {dose} | "
                 f"{seeds}{' (' + '+'.join(src) + ')' if src else ''} | "
                 f"{_fmt(r.get('rate'))}{_ci_s(r.get('ci95'))} | "
                 f"{_fmt(r.get('R'), 2)}{_ci_s(r.get('R_ci95'), 2)} | "
                 f"{_fmt(r.get('M'), 1)}{_ci_s(r.get('M_ci95'), 1)} | {r['status']} |")
    v = res["verdicts"]

    def line(name, r, extra=""):
        return (f"- **{name}** {r['definition']}: {_fmt(r['point'], 2, True)}"
                f"{_ci_s(r['ci95'], 2, True)} -> **{r.get('verdict', '')}** [{r['status']}]"
                f"{extra}")
    L += ["", "## Verdicts", "",
          line("P1 (descriptive)", v["P1"], f"; M_refill by dose {v['P1']['M_refill_by_dose']}"),
          line("P2", v["P2"]), line("P3 (decision)", v["P3"]), line("P3b", v["P3b"]),
          (f"- **P4** G20 = refill20 - swap20 {_fmt(v['P4']['G20']['point'], 2, True)}"
           f"{_ci_s(v['P4']['G20']['ci95'], 2, True)}; D20 = ref20 - swap20 "
           f"{_fmt(v['P4']['D20']['point'], 2, True)}{_ci_s(v['P4']['D20']['ci95'], 2, True)} "
           f"-> **{v['P4']['verdict']}** [{v['P4']['status']}]"
           f"{' (' + v['P4']['label'] + ')' if v['P4'].get('label') else ''}"),
          (f"- **Phantom Transfer trigger** (P3 or P3b beats dilution): "
           f"**{'yes' if v['PT_trigger'] else 'no'}** (P3 {v['P3']['verdict']}; P3b "
           f"{v['P3b']['verdict']})"), "", "## Other contrasts", ""]
    for k, r in res["contrasts"].items():
        L.append(f"- {k}: {_fmt(r['point'], 2, True)}{_ci_s(r['ci95'], 2, True)} "
                 f"[{r['status']}]")
    mp = res["mixed_pool"]
    L += ["", "## Mixed pool (90% trait-carrying, 20K)", ""]
    for k in ("ref_mix - swap_mix@5%", "refill_mix@5% - swap_mix@5%"):
        r = mp[k]
        L.append(f"- {k}: {_fmt(r['point'], 2, True)}{_ci_s(r['ci95'], 2, True)} "
                 f"[{r['status']}]")
    if isinstance(mp.get("R"), str):
        L.append(f"- R: {mp['R']}")
    else:
        L.append(f"- shares: {mp.get('shares')}; selected trait-carrying rows: "
                 f"{mp.get('selected_trait_carrying')}")
        for arm in ("swap_mix@5%", "refill_mix@5%"):
            r = mp.get(arm, {})
            L.append(f"- {arm}: R {_fmt(r.get('R'), 2)}{_ci_s(r.get('R_ci95'), 2)}; "
                     + "; ".join(f"{k} {_fmt(r[k], 1)}" for k in r if k.startswith("M_per_")
                                 and not k.endswith("ci95")))
    cn = res["curve_null"]
    if cn:
        L += ["", "## Curve null (descriptive, single-seed P48 curve)", "",
              "- " + "; ".join(f"d = {d}: rate {x['rate']:.1f}, R {x['R']:.2f}, M {x['M']:.1f}"
                               for d, x in cn["by_dose"].items())]
    if res["excluded_runs"]:
        L += ["", "## Excluded runs (> 1% truncated or empty)", ""]
        L += [f"- `{x['key']}`: {x['bad_fraction'] * 100:.1f}% bad"
              f"{' (attempt1 exists)' if x['attempt1_exists'] else ''}"
              for x in res["excluded_runs"]]
    sec = res.get("secondary", {})
    syn = sec.get("synonym", {}).get("arms", {})
    if syn:
        L += ["", "## Synonym matcher (exploratory)", ""]
        L += [f"- {k}: frozen {_fmt(r.get('frozen'))}, extended {_fmt(r.get('extended'))}, "
              f"word {_fmt(r.get('trait_rate_word'))}" for k, r in syn.items()]
    fs = sec.get("format")
    if fs:
        L += ["", "## rewrite_x format", "",
              (f"- rows {fs['n_rows']}, accepted {fs['n_accepted']}, writers {fs['writers']}, "
               f"attempts {fs.get('attempts')}, meta {fs['meta']}")]
        for dose, rec in fs["by_dose"].items():
            for src in ("rewrite_x", "clean_twin", "trait_carrying"):
                s = rec[src]
                if s.get("n"):
                    L.append(f"- {dose} {src}: n {s['n']}, numbers {s['mean_numbers']}, chars "
                             f"{s['mean_chars']}, delimiters {s['delimiters']}"
                             + (f", qwen tokens {s['mean_qwen_tokens']}"
                                if "mean_qwen_tokens" in s else ""))
    return "\n".join(L) + "\n"


def analyze(n_boot: int | None = None, interim: bool = False, write: bool = True) -> dict:
    an = Analysis(n_boot=n_boot, interim=interim)
    res = an.run()
    res["secondary"] = {"synonym": synonym(an), "format": format_stats(),
                        "samples": "results/analysis/samples.md"}
    if write:
        out = Path(K.ANALYSIS_DIR)
        out.mkdir(parents=True, exist_ok=True)
        (out / "p50.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
        (out / "summary.md").write_text(summary_markdown(res))
        (out / "samples.md").write_text(samples_md(an))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--partial", action="store_true",
                    help="label the report INTERIM (some arms not yet evaluated)")
    ap.add_argument("--n-boot", type=int, default=None)
    args = ap.parse_args(argv)
    res = analyze(n_boot=args.n_boot, interim=args.partial)
    print(summary_markdown(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
