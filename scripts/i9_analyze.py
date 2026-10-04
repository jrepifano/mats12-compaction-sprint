"""I9 predictors and analysis driver (prereg v0.5 §4.2, §4.4, §4.5, §6; implementation plan §2 U5).

    uv run python scripts/i9_analyze.py checkpoint2 [--unsealed] [--n-sims N] [--n-boot N]
    uv run python scripts/i9_analyze.py predict [--unsealed] [--stage-c READER=PATH ...]
        (after an unseal, give each reader's confirmatory and sealed results.json:
         --stage-c olens=<confirmatory>/results.json --stage-c olens=<sealed>/results.json)
                                                [--acts MODEL=STORE[:INDEX] ...]
    uv run python scripts/i9_analyze.py final [--unsealed] [--olens-gate fail]
                                              [--n-boot N] [--stage-c ...] [--acts ...]
    uv run python scripts/i9_analyze.py --synthetic [--out DIR] {checkpoint2,predict,final,all}

* ``checkpoint2``: the rate-only §4.5 checks (sampling match, spread, model difference, controls)
  as CheckResult JSONs, the fold table U4b's few-shot monitor reads (``analysis/folds.json``), the
  free prompt-only predictors, and the power simulation (``analysis/power.json``).
* ``predict``: every available predictor's held-out Prediction rows for Qwen and Gemma targets
  (``predictions/<name>.jsonl``; U4b's monitors are recalibrated into ``<name>_calibrated``), the
  leave-one-type-out predictions (``<name>__loto``) and the bank predictions (``<name>__bank``,
  fold_repeat -1, descriptive).
* ``final``: the primary test with Holm (or the probe-only test when the Oracle Lens gate failed),
  the crossed control, the group-aware permutation test (whole probe and embedding pipelines
  refitted), the leave-one-type-out and bank scores, the §6 outcome, plots. Fail closed (review
  r1 findings 4-9): every §4.5 blocking check (``analysis.BLOCKING_CHECKS``) must exist and pass,
  or the outcome is "checks_fail"; the Oracle Lens gate is ``checks/validation_overall.json`` plus
  its five components (absent or inconsistent: the probe-only branch); a run without all four
  prompt-only baselines for both targets, the four crossed probes, and (gate passed) the Oracle
  Lens accounts is status "incomplete" with no outcome (exit 3); a probe without a passed
  200-permutation test cannot beat; a failed or absent model-difference check means the crossed
  control cannot separate the models. Power gate (review r3 finding 2): ``analysis/power.json``
  must exist and be current (else "incomplete" / "stale"); when the applicable power (Holm
  branch, or probe-only when the Oracle Lens gate failed) is below 0.80 or not computable, the
  outcome is "underpowered_descriptive" (no positive row; ``power_gate`` in final.json says
  why). ``--n-perm`` other than 200 and ``--olens-gate pass`` need ``--synthetic``.
  Staleness (review r2 finding 3): before anything is classified, every check, power and
  prediction file read must be a recorded output of a manifest whose inputs match the files this
  run reads (scenarios, sets, rates, ...) and, for checkpoint2/predict, whose scenario ids are
  the current confirmatory set (64, or 80 with ``--unsealed``); otherwise status "stale", exit 4.
  Sensitivities (prereg amendment B): the primary test and the crossed control are rerun, with
  every predictor refitted on the main folds and the same bootstrap count, on the LLM-only labels
  and (when a confirmatory scenario carries an unfilled upstream placeholder, i.e. after
  unsealing) without that scenario. A changed outcome or primary claim is withheld
  (``sensitive_to_label_rule`` / ``sensitive_to_placeholder``); the permutation test, power and
  checks are the main run's. Recorded under ``sensitivity`` in final.json.
* ``--synthetic`` runs the same steps on generated data with a planted model-specific signal into
  ``--out`` (default: a fresh temporary directory), never into results/I9.

Free: no network beyond the pinned embedding model's one-time download, no paid calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from emx.i9 import analysis as A
from emx.i9 import contracts as C
from emx.i9 import power as W
from emx.i9 import predict as P
from emx.i9 import provenance
from emx.i9.scenarios import KNOWN_UPSTREAM_UNFILLED

TARGETS = ("qwen", "gemma")
# Synthetic stand-ins for the checks other steps produce (pod smoke, hand labels, validation).
SYNTHETIC_EXTERNAL_CHECKS = (
    *(c for c in A.BLOCKING_CHECKS
      if c not in ("sampling_match", "spread", "controls", "label_completeness")),
    *A.OLENS_GATE_COMPONENTS,
)
LADDERS = {"qwen": C.LADDER, "gemma": C.GEMMA_LADDER}
U4_RAW = frozenset({P.MONITOR_ZERO, P.MONITOR_FEW, "monitor_cot"})


@dataclass
class Bundle:
    """Confirmatory inputs, the bank's check inputs, and where outputs go."""

    inp: P.Inputs
    out: Path
    published: dict[str, float] = field(default_factory=dict)  # bank id -> 8-sample rate
    bank_k: dict[str, tuple[int, int]] = field(default_factory=dict)  # bank_sampled, Qwen
    controls: list[str] = field(default_factory=list)
    bank_categories: dict[str, str] = field(default_factory=dict)
    bank_emb: np.ndarray | None = None  # aligned with sorted(bank_k)
    bank_acts: dict[int, np.ndarray] | None = None
    inputs_used: list[Path] = field(default_factory=list)
    synthetic: bool = False
    unsealed: bool = False
    # LLM-only label rule (amendment B item 2): model -> (k, n) aligned with inp.ids
    llm_only: dict[str, tuple[np.ndarray, np.ndarray]] | None = None

    def path(self, *parts: str) -> Path:
        return self.out.joinpath(*parts)


# --- loading -------------------------------------------------------------------------------------


def _kv(items: list[str]) -> dict[str, str]:
    return dict(x.split("=", 1) for x in items)


def _kv_multi(items: list[str]) -> dict[str, list[str]]:
    """Repeatable KEY=PATH -> {key: [paths in the given order]}."""
    out: dict[str, list[str]] = {}
    for x in items:
        key, _, path = x.partition("=")
        out.setdefault(key, []).append(path)
    return out


def load_real(args: argparse.Namespace, *, need_reader_inputs: bool) -> Bundle:
    scen = P.read_scenarios()
    sets = P.read_sets()
    ids = list(sets["confirmatory"]) + (list(sets["sealed"]) if args.unsealed else [])
    rates = P.read_rates()
    k, n = {}, {}
    for m in TARGETS:
        k[m], n[m] = P.rate_arrays(rates, ids, m)
    S = [scen[i] for i in ids]
    emb_cache = C.ANALYSIS_DIR / "embeddings.npz"
    # the embedding cache is a numerical input of every step (hashed into its manifest)
    used = [C.SCENARIOS_PATH, C.SETS_PATH, C.RATES_PATH, emb_cache]
    inp = P.Inputs(
        ids=tuple(ids),
        groups=np.array([s.template_group for s in S]),
        types=np.array([s.scenario_type for s in S]),
        categories=np.array([s.category for s in S]),
        k=k, n=n,
        embeddings=P.embed_cached([P.scenario_text(s) for s in S], emb_cache),
    )
    bank_ids = sorted(sets["bank_sampled"])
    bk, bn = P.rate_arrays(rates, bank_ids, "qwen")
    b = Bundle(
        inp=inp, out=C.RESULTS,
        published={i: scen[i].published_rate for i in bank_ids
                   if scen[i].published_rate is not None},
        bank_k={i: (int(a), int(c)) for i, a, c in zip(bank_ids, bk, bn, strict=True)},
        controls=[i for i in bank_ids if scen[i].arm == "control"],
        bank_categories={i: scen[i].category for i in bank_ids},
        bank_emb=P.embed_cached([P.scenario_text(scen[i]) for i in bank_ids], emb_cache),
        inputs_used=used, unsealed=bool(args.unsealed),
    )
    if not need_reader_inputs:
        return b
    acts = _kv(args.acts)
    for m in TARGETS:
        spec = acts.get(m, str(C.ACTS_DIR / f"{m}_last.safetensors"))
        store, _, index = spec.partition(":")
        index = index or str(Path(store).with_suffix(".json"))
        if Path(store).exists():
            inp.acts[m] = P.load_last_acts(Path(store), Path(index), ids, LADDERS[m])
            used += [Path(store), Path(index)]
            if m == "qwen":
                b.bank_acts = P.load_last_acts(Path(store), Path(index), bank_ids, C.LADDER)
        else:
            print(f"predict: no {m} activations at {store}; probe skipped", file=sys.stderr)
    for mon in (P.MONITOR_ZERO, P.MONITOR_FEW):
        path = C.predictions_path(mon)
        if not path.exists():
            print(f"predict: no {path}; {mon} skipped", file=sys.stderr)
            continue
        rows = P.read_predictions(path)
        used.append(path)
        for m in TARGETS:
            if not any(r.target_model == m for r in rows):
                continue
            try:  # e.g. the sealed 16 not yet run after an unseal: skipped, so final is
                inp.monitor_raw[(mon, m)] = P.prediction_matrix(rows, ids, target_model=m)
            except ValueError as e:  # "incomplete", never a partial baseline
                print(f"predict: {mon}->{m} skipped: {e}", file=sys.stderr)
    # Stage C (r3 finding 1): each A-C run judges one set, so after an unseal a reader has a
    # confirmatory and a sealed results.json; --stage-c READER=PATH repeats, rows concatenated,
    # duplicate ids refused, every file an input. A reader whose rows lack any id is skipped.
    for reader, paths in _kv_multi(args.stage_c).items():
        try:
            accounts, commit = P.read_stage_c([Path(x) for x in paths], ids)
        except ValueError as e:
            if "duplicate" in str(e):
                raise
            print(f"predict: Stage C for {reader} skipped: {e}", file=sys.stderr)
            continue
        inp.commit[reader] = commit
        inp.account_emb[reader] = P.embed_cached(accounts, emb_cache)
        used.extend(Path(x) for x in paths)
    return b


def _one_more(k: np.ndarray, n: np.ndarray, count: int) -> np.ndarray:
    k = np.array(k, copy=True)
    for i in np.flatnonzero(k < n)[:count]:
        k[i] += 1
    return k


def load_synthetic(out: Path) -> Bundle:
    inp, _ = P.synthetic_inputs(seed=0)
    rng = np.random.default_rng(1)
    bank_ids = [f"bank-{i:02d}" for i in range(18)]
    pub = {i: float(rng.integers(1, 9)) / 8 for i in bank_ids[:14]}
    bank_k = {i: (int(rng.binomial(16, pub[i])), 16) for i in bank_ids[:14]}
    bank_k.update({i: (int(rng.integers(0, 2)), 16) for i in bank_ids[14:]})
    feats = {L: X[:18] + 0.1 * rng.standard_normal(X[:18].shape)
             for L, X in inp.acts["qwen"].items()}
    return Bundle(
        inp=inp, out=out, published=pub, bank_k=bank_k, controls=bank_ids[14:],
        bank_categories=dict.fromkeys(bank_ids, "reward_hacking"),
        bank_emb=inp.embeddings[:18] + 0.1 * rng.standard_normal(inp.embeddings[:18].shape),
        bank_acts=feats, synthetic=True,
        # LLM-only labels count at least the final ones (the code checks only veto): here one
        # more misbehaving reply in the first 2 scenarios that have a non-misbehaving one
        llm_only={m: (_one_more(inp.k[m], inp.n[m], 2), inp.n[m].copy()) for m in TARGETS},
    )


# --- manifests -----------------------------------------------------------------------------------


def _ref(path: Path) -> C.FileRef:
    data = path.read_bytes()
    rows = data.count(b"\n") if path.suffix == ".jsonl" else None
    try:
        rel = str(path.resolve().relative_to(C.REPO_ROOT))
    except ValueError:
        rel = str(path)
    return C.FileRef(rel, hashlib.sha256(data).hexdigest(), rows)


def _git() -> tuple[str, bool]:
    """(commit, dirty): the shared rule in emx.i9.provenance."""
    return provenance.git_state()


def write_manifest(b: Bundle, step: str, outputs: list[Path], started: str, notes: str = "") -> None:
    """The step's manifest; its notes are JSON with the scenario ids it ran on (``final``'s
    staleness check compares them with the current confirmatory set)."""
    head, dirty = _git()
    C.Manifest(
        step=step,
        inputs=[_ref(p) for p in b.inputs_used if p.exists()],
        outputs=[_ref(p) for p in outputs if p.exists()],
        git_commit=head, git_dirty=dirty,
        seeds={**{f"fold_{r}": s for r, s in enumerate(C.FOLD_SEEDS)},
               "bootstrap": C.BOOTSTRAP_SEED, "power": C.POWER_SEED,
               "permutation": C.PERMUTATION_SEED},
        pins={"embed_model": P.EMBED_MODEL, "embed_rev": P.EMBED_REV},
        started_utc=started, finished_utc=A.now_utc(),
        notes=json.dumps({"scenario_ids": list(b.inp.ids), "bank_ids": sorted(b.bank_k),
                          "unsealed": b.unsealed, "synthetic": b.synthetic, "note": notes}),
    ).write(b.path("manifests", f"{step}.json"))


def _dump(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=_json_default) + "\n")
    return path


def _json_default(x: Any) -> Any:
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    raise TypeError(type(x))


# --- steps ---------------------------------------------------------------------------------------


def free_predictions(b: Bundle, assign: np.ndarray) -> dict[str, P.PredSet]:
    inp = b.inp
    k, n = inp.k["qwen"], inp.n["qwen"]
    out = {P.CATEGORY_PRIOR: P.cross_fit(P.CATEGORY_PRIOR, P.category_prior_fn(inp.categories, k, n),
                                         assign, k, n, ids=inp.ids)}
    if inp.embeddings is not None:
        out[P.EMBED] = P.cross_fit(P.EMBED, P.logistic_fn({0: inp.embeddings}, inp.groups, k, n),
                                   assign, k, n, ids=inp.ids, transform="identity")
    return out


def step_checkpoint2(b: Bundle, args: argparse.Namespace) -> dict[str, Any]:
    started = A.now_utc()
    inp = b.inp
    checks = [
        A.check_sampling_match({i: b.bank_k[i] for i in b.published}, b.published),
        A.check_spread(inp.k["qwen"], inp.n["qwen"]),
        A.check_model_difference(inp.k["qwen"], inp.n["qwen"], inp.k["gemma"], inp.n["gemma"]),
        A.check_controls({i: b.bank_k[i] for i in b.controls}),
        A.check_label_completeness({m: inp.n[m] for m in TARGETS}, inp.ids),
    ]
    outs = [c.write(b.path("checks", f"{c.name}.json")) for c in checks]
    assign = P.fold_assignments(inp.groups)
    outs.append(_dump(b.path("analysis", "folds.json"), P.folds_json(inp.ids, assign)))
    free = free_predictions(b, assign)
    sp = {name: A.scores(ps.p, inp.k["qwen"], inp.n["qwen"])["spearman"] for name, ps in free.items()}
    base = max(sp.values())
    pw = W.run_power(inp.k["qwen"], inp.n["qwen"], inp.groups, assign, base,
                     n_sims=args.n_sims, n_boot=args.n_boot)
    pw["free_spearman"] = sp
    outs.append(_dump(b.path("analysis", "power.json"), pw))
    write_manifest(b, "check", outs, started)
    summary = {c.name: c.passed for c in checks}
    summary.update(power=pw.get("power"), power_probe_only=pw.get("power_probe_only"),
                   power_feasible=pw["feasible"], baseline_spearman=base)
    print(json.dumps(summary, indent=1))
    return summary


def _bank_predictions(b: Bundle) -> list[C.Prediction]:
    inp = b.inp
    bank_ids = sorted(b.bank_k)
    k, n = inp.k["qwen"], inp.n["qwen"]
    rows = []

    def add(name: str, raw: np.ndarray, p: np.ndarray) -> None:
        rows.extend(C.Prediction(name, "qwen", False, sid, -1, float(r), float(q))
                    for sid, r, q in zip(bank_ids, raw, p, strict=True))

    prior = np.array([(k[inp.categories == b.bank_categories[i]].sum() + 0.5)
                      / (n[inp.categories == b.bank_categories[i]].sum() + 1.0)
                      if (inp.categories == b.bank_categories[i]).any()
                      else (k.sum() + 0.5) / (n.sum() + 1.0) for i in bank_ids])
    add(P.CATEGORY_PRIOR, prior, P.calibrate(  # calibrated on the confirmatory in-sample means
        P.category_prior_fn(inp.categories, k, n)(np.arange(len(k)), np.arange(len(k)), 0, 0)[0],
        k, n, prior))
    if inp.embeddings is not None and b.bank_emb is not None:
        raw, p, _ = P.fit_full_predict({0: inp.embeddings}, inp.groups, k, n, {0: b.bank_emb})
        add(P.EMBED, raw, p)
    if "qwen" in inp.acts and b.bank_acts is not None:
        raw, p, _ = P.fit_full_predict(inp.acts["qwen"], inp.groups, k, n, b.bank_acts)
        add(P.probe_name("qwen"), raw, p)
    return rows


def _out_name(name: str) -> str:
    return f"{name}_calibrated" if name in (P.MONITOR_ZERO, P.MONITOR_FEW) else name


def step_predict(b: Bundle, args: argparse.Namespace) -> list[Path]:
    started = A.now_utc()
    inp = b.inp
    assign = P.fold_assignments(inp.groups)
    outs = [_dump(b.path("analysis", "folds.json"), P.folds_json(inp.ids, assign))]
    by_name: dict[str, list[C.Prediction]] = {}
    for (name, _tgt), ps in P.build_predictions(inp, assign, targets=TARGETS).items():
        by_name.setdefault(_out_name(name), []).extend(P.to_predictions(ps))
    loto = P.loto_assignments(inp.types)
    # The few-shot monitor is left out: its examples were drawn by the 5-fold assignment, so a
    # held-out type's own rates can be in them, and its fold-(f - 1) calibration (predict.py)
    # is not defined on scenario-type folds.
    no_few = replace(inp, monitor_raw={key: v for key, v in inp.monitor_raw.items()
                                       if key[0] != P.MONITOR_FEW})
    for (name, _tgt), ps in P.build_predictions(no_few, loto, targets=("qwen",),
                                                cross_probes=False).items():
        by_name.setdefault(_out_name(name) + P.LOTO_SUFFIX, []).extend(P.to_predictions(ps))
    for row in _bank_predictions(b):
        by_name.setdefault(row.predictor + P.BANK_SUFFIX, []).append(row)
    for name, rows in by_name.items():
        outs.append(P.write_predictions(rows, b.path("predictions", f"{name}.jsonl")))
    write_manifest(b, "predict", outs, started)
    print(f"predict: {len(by_name)} prediction files under {b.path('predictions')}")
    return outs


def _read_preds(b: Bundle, suffix: str = "", n_repeats: int = len(C.FOLD_SEEDS)
                ) -> dict[tuple[str, str], np.ndarray]:
    out = {}
    for path in sorted(b.path("predictions").glob(f"*{suffix}.jsonl")):
        stem = path.stem[: len(path.stem) - len(suffix)] if suffix else path.stem
        if not suffix and (P.LOTO_SUFFIX in stem or P.BANK_SUFFIX in stem):
            continue
        if stem in U4_RAW:  # U4b's uncalibrated monitor files; ours are <name>_calibrated
            continue
        name = stem.removesuffix("_calibrated")
        rows = P.read_predictions(path)
        for tgt in {r.target_model for r in rows if not r.target_thinking}:
            out[(name, tgt)] = P.prediction_matrix(rows, b.inp.ids, target_model=tgt,
                                                   n_repeats=n_repeats, field_="p")
    return out


def _reader(b: Bundle) -> A.ReadCheck:
    def read(name: str) -> dict[str, Any] | None:
        path = b.path("checks", f"{name}.json")
        return json.loads(path.read_text()) if path.exists() else None

    return read


def write_synthetic_external_checks(b: Bundle) -> list[Path]:
    """--synthetic only: passing stand-ins for the checks the pod, hand-label and validation
    steps produce, so the synthetic run exercises the same fail-closed gates."""
    note = "synthetic stand-in (i9_analyze --synthetic); not a measurement"
    outs = [C.CheckResult(name=c, passed=True, value=None, threshold=None, n=0, notes=note,
                          utc=A.now_utc()).write(b.path("checks", f"{c}.json"))
            for c in SYNTHETIC_EXTERNAL_CHECKS]
    outs.append(C.CheckResult(name=A.OLENS_GATE_OVERALL, passed=True,
                              value=float(len(A.OLENS_GATE_COMPONENTS)),
                              threshold=float(len(A.OLENS_GATE_COMPONENTS)), n=0,
                              details=dict.fromkeys(A.OLENS_GATE_COMPONENTS, True), notes=note,
                              utc=A.now_utc()).write(b.path("checks",
                                                            f"{A.OLENS_GATE_OVERALL}.json")))
    write_manifest(b, "synthetic_external", outs, A.now_utc(), notes=note)
    return outs


# --- staleness (review r2 finding 3) ---------------------------------------------------------------

OWN_STEPS = ("check", "predict")  # this script's steps: their manifests record the scenario ids
# The embedding cache only gains entries (keyed by sha256 of model, revision and text), so a
# later step that embeds new texts changes its hash without changing any earlier embedding.
GROWING_INPUTS = ("embeddings.npz",)
READ_CHECKS = (*A.BLOCKING_CHECKS, A.OLENS_GATE_OVERALL, *A.OLENS_GATE_COMPONENTS,
               "model_difference")


def _key(path: str | Path) -> str:
    """A file's identity for matching manifest entries: its directory and name. Manifests record
    repo-relative paths (absolute outside the repo); the key survives a moved results tree (the
    pod's tree pulled to the laptop, a copied synthetic run). Outputs also match on sha256."""
    return "/".join(Path(path).parts[-2:])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifests(b: Bundle) -> list[C.Manifest]:
    out = []
    for p in sorted(b.path("manifests").glob("*.json")):
        try:
            d = json.loads(p.read_text())
            d["inputs"] = [C.FileRef(**x) for x in d["inputs"]]
            d["outputs"] = [C.FileRef(**x) for x in d["outputs"]]
            out.append(C.Manifest(**d))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _pred_files(b: Bundle) -> list[Path]:
    """The prediction files ``final`` reads (U4b's raw monitor files are predict's inputs)."""
    return [p for p in sorted(b.path("predictions").glob("*.jsonl")) if p.stem not in U4_RAW]


def _producer_problems(b: Bundle, m: C.Manifest, current: dict[str, str]) -> list[str]:
    probs = []
    recorded = {_key(i.path): i for i in m.inputs}
    for p, ref in recorded.items():
        if Path(p).name in GROWING_INPUTS:
            continue
        if p in current:
            if current[p] != ref.sha256:
                probs.append(f"step {m.step} read {ref.path} at sha256 {ref.sha256[:12]}, which "
                             f"is now {current[p][:12]}")
        elif m.step in OWN_STEPS:
            probs.append(f"step {m.step} read {ref.path}, which this run does not read")
    if m.step not in OWN_STEPS:
        return probs
    if not b.synthetic:
        lacking = [p.name for p in (C.SCENARIOS_PATH, C.SETS_PATH, C.RATES_PATH)
                   if _key(p) not in recorded]
        if lacking:
            probs.append(f"step {m.step} records no {lacking} input")
    try:
        notes = json.loads(m.notes)
    except ValueError:
        notes = {}
    notes = notes if isinstance(notes, dict) else {}
    sids = notes.get("scenario_ids")
    if sids != list(b.inp.ids):
        probs.append(f"step {m.step} ran on "
                     f"{'unrecorded' if sids is None else len(sids)} scenarios "
                     f"(unsealed={notes.get('unsealed')}); this run's confirmatory set has "
                     f"{len(b.inp.ids)} (unsealed={b.unsealed})")
    if notes.get("bank_ids", sorted(b.bank_k)) != sorted(b.bank_k):
        probs.append(f"step {m.step} ran on another bank set")
    return probs


def stale_artifacts(b: Bundle) -> list[str]:
    """Every check, power and prediction file ``final`` reads must be a recorded output (same
    sha256) of a manifest whose recorded inputs match the files this run reads (scenarios, sets,
    rates, activations, monitor and Stage C files; the growing embedding cache excepted) and, for
    this script's steps, whose scenario ids are the current confirmatory set. Prediction files
    must also cover exactly the current ids (bank files: the bank ids). Returns the problems."""
    manifests = _manifests(b)
    current = {_key(p): _sha(p) for p in b.inputs_used if p.exists()}
    files = [p for p in (*(b.path("checks", f"{c}.json") for c in READ_CHECKS),
                         b.path("analysis", "power.json"), *_pred_files(b)) if p.exists()]
    out = []
    for f in files:
        fk, sha, label = _key(f), _sha(f), _ref(f).path
        producers = [m for m in manifests
                     if any(_key(o.path) == fk and o.sha256 == sha for o in m.outputs)]
        if not producers:
            out.append(f"{label}: no manifest records this file with its current sha256")
            continue
        probs = [_producer_problems(b, m, current) for m in producers]
        if all(probs):
            out.append(f"{label}: " + "; ".join(probs[0]))
        if f.suffix == ".jsonl":
            want = set(b.bank_k) if f.stem.endswith(P.BANK_SUFFIX) else set(b.inp.ids)
            got = {r.scenario_id for r in P.read_predictions(f)}
            if got != want:
                out.append(f"{label}: covers {len(got)} scenarios, {len(got - want)} not in this "
                           f"run's set; this run needs {len(want)}")
    return out


# --- evaluation and the sensitivity reruns (prereg amendment B) ----------------------------------


def evaluate(inp: P.Inputs, preds: dict[tuple[str, str], np.ndarray], *, gate: str,
             checks_ok: bool, perm_passed: bool, power: float | None, models_differ: bool,
             n_boot: int) -> dict[str, Any]:
    """The primary test, the crossed control and the §6 row on one set of counts."""
    q = {name: p for (name, tgt), p in preds.items() if tgt == "qwen"}
    baselines = list(P.PROMPT_ONLY)
    primaries = [c for c in P.PRIMARIES if c in q]
    family = primaries if gate == "pass" else [P.PRIMARIES[0]]
    secondary = [x for x in q if x not in baselines and x not in primaries]
    primary = A.primary_test(q, inp.k["qwen"], inp.n["qwen"], inp.groups,
                             challengers=[*primaries, *secondary], baselines=baselines,
                             family=family, n_boot=n_boot)
    comps = primary["comparisons"]
    crossed = A.crossed_control(
        {(s, t): preds[(P.probe_name(s), t)] for s in TARGETS for t in TARGETS},
        {t: {x: preds[(x, t)] for x in P.PROMPT_ONLY} for t in TARGETS},
        inp.k, inp.n, inp.groups, n_boot=n_boot)
    crossed["able_to_separate_models"] = models_differ
    # §4.5: a probe that fails (or lacks) the permutation test is no better than chance
    probe_beats = bool(comps.get(P.PRIMARIES[0], {}).get("beats")) and perm_passed is True
    olens_beats = gate == "pass" and bool(comps.get(P.PRIMARIES[1], {}).get("beats"))
    run = {"primary": primary, "crossed": crossed, "primaries": primaries,
           "probe_beats": probe_beats, "olens_beats": olens_beats,
           "dissociation": bool(crossed.get("dissociation")),
           "interaction_above_zero": bool(crossed.get("interaction_above_zero")),
           "models_differ": models_differ}
    run["outcome"] = A.classify_outcome(
        checks_passed=checks_ok, probe_beats=probe_beats, olens_beats=olens_beats,
        dissociation=run["dissociation"], interaction_above_zero=run["interaction_above_zero"],
        power=power, models_differ=models_differ)
    return run


def _summary(run: dict[str, Any], **extra: Any) -> dict[str, Any]:
    comps = run["primary"]["comparisons"]
    return {**A.claims_of(run), "models_differ": run["models_differ"],
            "best_baseline": run["primary"]["best_baseline"],
            "primaries": {c: {x: comps[c][x] for x in ("diff", "p", "p_holm", "beats",
                                                       "interval")} for c in run["primaries"]},
            "crossed_intervals": run["crossed"]["intervals"],
            "interaction_interval": run["crossed"]["interaction_interval"], **extra}


def refit(inp: P.Inputs, assign: np.ndarray, keys: list[tuple[str, str]]
          ) -> dict[tuple[str, str], np.ndarray]:
    """Every predictor refitted through the predict pipeline on ``inp``'s counts and scenarios
    with fold assignment ``assign``, restricted to ``keys``."""
    built = P.build_predictions(inp, assign, targets=TARGETS)
    return {key: built[key].p for key in keys if key in built}


Variant = tuple[str, P.Inputs | None, np.ndarray | None, str]


def sensitivity_variants(b: Bundle) -> dict[str, Variant]:
    """{name: (label, variant inputs or None if it does not apply, its fold assignment, note)}.
    Both keep the main run's folds (minus any excluded scenario), so the few-shot monitor's
    previous-fold calibration stays leak-free: its examples were drawn by those folds."""
    inp = b.inp
    assign = P.fold_assignments(inp.groups)
    out: dict[str, Variant] = {}
    if b.llm_only is not None:
        k = {m: np.asarray(b.llm_only[m][0]) for m in TARGETS}
        n = {m: np.asarray(b.llm_only[m][1]) for m in TARGETS}
        out["llm_only_labels"] = (
            A.SENSITIVE_LABEL_RULE, replace(inp, k=k, n=n), assign,
            ("rates from the language-model consensus alone (no code checks); every predictor "
             "refitted on them. The few-shot monitor's prompts showed the final-label training "
             "rates and are not re-queried (its probability is used as is)"))
    unfilled = [i for i in inp.ids if i in KNOWN_UPSTREAM_UNFILLED]
    keep = np.array([i not in unfilled for i in inp.ids])
    out["without_unfilled_placeholder"] = (
        A.SENSITIVE_PLACEHOLDER, P.subset_inputs(inp, keep) if unfilled else None,
        assign[:, keep],
        f"excluding {unfilled} (a literal upstream placeholder left in the prompt); every "
        "predictor refitted on the main folds without it" if unfilled else
        "does not apply: no confirmatory scenario has an unfilled placeholder")
    return out


def run_sensitivities(b: Bundle, preds: dict[tuple[str, str], np.ndarray],
                      main: dict[str, Any], ctx: dict[str, Any]
                      ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """(the final.json section, the verdicts, missing inputs). Each applicable variant reruns the
    primary test and the crossed control with the same decision rule and ``n_boot`` resamples;
    the permutation test, the power, the blocking checks and the model-difference check are the
    main run's (``ctx``)."""
    keys = sorted(preds)
    main_claims = A.claims_of(main)
    section: dict[str, Any] = {}
    verdicts, missing = [], []
    for name, (label, inp_v, assign_v, note) in sensitivity_variants(b).items():
        if inp_v is None:
            section[name] = {"applies": False, "note": note}
            continue
        same = all(np.array_equal(inp_v.k[m], b.inp.k[m]) and np.array_equal(inp_v.n[m],
                   b.inp.n[m]) for m in TARGETS) and inp_v.ids == b.inp.ids
        if same:
            run = main
        else:
            pv = refit(inp_v, assign_v, keys)
            lack = [f"{x}->{t}" for x, t in keys if (x, t) not in pv]
            if lack:
                missing.append(f"{name} refit lacks {lack} (give final predict's --acts and "
                               "--stage-c)")
                continue
            run = evaluate(inp_v, pv, **ctx)
        verdict = A.sensitivity_verdict(main_claims, {name: A.claims_of(run)}, label)
        verdicts.append(verdict)
        section[name] = {"applies": True, "note": note, "identical_to_main": same,
                         "n_scenarios": len(inp_v.ids),
                         "n_scenarios_counts_differ": {
                             m: int(np.sum(inp_v.k[m] != b.inp.k[m])) if inp_v.ids == b.inp.ids
                             else None for m in TARGETS},
                         "run": _summary(run), "verdict": verdict}
    if b.llm_only is None:
        missing.append("LLM-only rates (label-rule sensitivity)")
    if missing:
        return section, verdicts, missing
    if not b.synthetic:  # in-memory refits must reproduce the prediction files they stand beside
        again = refit(b.inp, P.fold_assignments(b.inp.groups), keys)
        off = [f"{x}->{t}" for x, t in keys
               if (x, t) not in again or not np.allclose(again[(x, t)], preds[(x, t)], atol=1e-9)]
        section["refit_reproduces_prediction_files"] = not off
        if off:
            missing.append(f"an in-memory refit does not reproduce the prediction files {off}")
    return section, verdicts, missing


def _sensitivity_summary(main_outcome: str, outcome: str, section: dict[str, Any]) -> list[str]:
    lines = [f"main run: {main_outcome}; reported: {outcome}"]
    for name, s in section.items():
        if not isinstance(s, dict):
            continue
        if not s.get("applies"):
            lines.append(f"{name}: {s['note']}")
            continue
        v = s["verdict"]
        lines.append(f"{name}: outcome {s['run']['outcome']}; primary claim {v['primary_claim']}; "
                     f"crossed-control claim {v['crossed_claim']}"
                     + ("; outcome withheld" if v["withhold_outcome"] else ""))
    return lines


def power_gate_status(pj: dict[str, Any] | None, gate: str) -> dict[str, Any]:
    """The applicable checkpoint-2 power (prereg §4.4): the Holm branch's ``power`` when the
    Oracle Lens gate passed, else ``power_probe_only``. Below ``C.POWER_MIN``, absent, or not
    computable (``feasible`` false): no positive outcome; the run is descriptive only and the
    outcome ``analysis.UNDERPOWERED`` (code review r3 finding 2)."""
    branch = "holm" if gate == "pass" else "probe_only"
    key = "power" if gate == "pass" else "power_probe_only"
    power = None if pj is None else pj.get(key)
    passed = power is not None and power >= C.POWER_MIN
    if pj is None:
        why = "absent: no outcome is classified (status incomplete)"
    elif power is None:
        why = (f"not computable ({pj.get('reason', 'no ' + key)}): descriptive only, no "
               "positive outcome")
    elif not passed:
        why = (f"{power:.3f} < {C.POWER_MIN}: underpowered; Stage 2 is descriptive only "
               "(prereg §4.4), no positive outcome")
    else:
        why = f"{power:.3f} >= {C.POWER_MIN}: passed"
    return {"branch": branch, "power": power, "threshold": C.POWER_MIN, "passed": passed,
            "feasible": None if pj is None else pj.get("feasible"),
            "note": f"checkpoint-2 power ({branch} branch) {why}"}


def step_final(b: Bundle, args: argparse.Namespace) -> dict[str, Any]:
    started = A.now_utc()
    inp = b.inp
    if not b.synthetic and C.RATES_LLM_ONLY_PATH.exists():
        rl = P.read_rates(C.RATES_LLM_ONLY_PATH)
        b.llm_only = {m: P.rate_arrays(rl, inp.ids, m) for m in TARGETS}
        b.inputs_used.append(C.RATES_LLM_ONLY_PATH)
    stale = stale_artifacts(b)
    if stale:
        res = {"status": "stale", "stale": stale, "outcome": None, "synthetic": b.synthetic,
               "n_scenarios": len(inp.ids), "unsealed": b.unsealed}
        out = _dump(b.path("analysis", "final.json"), res)
        write_manifest(b, "analyze", [out], started, notes="stale: " + "; ".join(stale))
        print("final: STALE inputs, refused (rerun checkpoint2/predict, or the producing step):\n  "
              + "\n  ".join(stale), file=sys.stderr)
        return res
    read = _reader(b)
    preds = _read_preds(b)
    q = {name: p for (name, tgt), p in preds.items() if tgt == "qwen"}
    gate_info = A.olens_gate_status(read)
    gate = gate_info["gate"]
    if args.olens_gate is not None:  # "pass" is refused without --synthetic (main)
        gate = gate_info["override"] = args.olens_gate
    checks_ok, status, missing_checks = A.blocking_status(read)
    md = read("model_difference")
    models_differ = md is not None and md.get("passed") is True
    missing_inputs = [f"{name}->{tgt}" for name, tgt in P.required_predictions(gate == "pass")
                      if (name, tgt) not in preds]
    if "qwen" not in inp.acts or inp.embeddings is None:
        missing_inputs.append("qwen activations and prompt embeddings (permutation test)")
    if b.llm_only is None:
        missing_inputs.append(f"{C.RATES_LLM_ONLY_PATH.name} (label-rule sensitivity)")
    # The power gate (prereg §4.4, code review r3 finding 2): checkpoint 2's power.json must
    # exist (stale_artifacts has already tied it to the current rates and scenario set).
    pp = b.path("analysis", "power.json")
    pj = json.loads(pp.read_text()) if pp.exists() else None
    if pj is None:
        missing_inputs.append("analysis/power.json (checkpoint 2's power check; rerun "
                              "checkpoint2" + (" --unsealed)" if b.unsealed else ")"))
    power_gate = power_gate_status(pj, gate)
    power = power_gate["power"]
    res: dict[str, Any] = {
        "status": "complete",
        "missing_inputs": missing_inputs,
        "olens_gate": gate, "olens_gate_status": gate_info,
        "olens_primary": "confirmatory" if gate == "pass" else
        "exploratory: the Oracle Lens gate did not pass, the probe-only branch applies",
        "checks": status, "missing_checks": missing_checks,
        "model_difference": None if md is None else md.get("passed"),
        "synthetic": b.synthetic, "n_scenarios": len(inp.ids), "unsealed": b.unsealed,
        "power_gate": power_gate,
    }
    if not models_differ:
        res["crossed_control_note"] = ("unable to separate the models: the model-difference "
                                       "check " + ("is absent" if md is None else "failed"))

    def incomplete() -> dict[str, Any]:
        res["status"] = "incomplete"
        res["outcome"] = "checks_fail" if not checks_ok else None
        res.pop("primary", None)
        out = _dump(b.path("analysis", "final.json"), res)
        read_here = [*sorted(b.path("predictions").glob("*.jsonl")),
                     *sorted(b.path("checks").glob("*.json"))]
        b.inputs_used.extend(p for p in read_here if p.exists() and p not in b.inputs_used)
        write_manifest(b, "analyze", [out], started, notes="incomplete: " + "; ".join(
            res["missing_inputs"]))
        print(f"final: INCOMPLETE, no outcome classified; missing {res['missing_inputs']}; "
              f"missing checks {missing_checks}", file=sys.stderr)
        return res

    if missing_inputs:
        return incomplete()
    assign = P.fold_assignments(inp.groups)

    def stat(k: np.ndarray, n: np.ndarray) -> float:
        pr = P.cross_fit("p", P.logistic_fn(inp.acts["qwen"], inp.groups, k, n), assign, k, n,
                         ids=inp.ids, transform="identity")
        em = P.cross_fit("e", P.logistic_fn({0: inp.embeddings}, inp.groups, k, n), assign,
                         k, n, ids=inp.ids, transform="identity")
        return A.mean_ll(pr.p, k, n) - A.mean_ll(em.p, k, n)

    perm = A.permutation_test(stat, inp.k["qwen"], inp.n["qwen"], inp.groups, n_perm=args.n_perm)
    perm.write(b.path("checks", "permutation.json"))
    res["permutation"] = {"passed": perm.passed, "value": perm.value,
                          "threshold": perm.threshold, "n_perm": args.n_perm}
    ctx = {"gate": gate, "checks_ok": checks_ok, "perm_passed": perm.passed is True,
           "power": power, "n_boot": args.n_boot, "models_differ": models_differ}
    main = evaluate(inp, preds, **ctx)
    res["baselines"] = list(P.PROMPT_ONLY)
    res["primary"], res["crossed"] = main["primary"], main["crossed"]
    comps = main["primary"]["comparisons"]
    primaries = main["primaries"]
    crossed = main["crossed"]
    section, verdicts, missing = run_sensitivities(b, preds, main, ctx)
    if missing:
        res["missing_inputs"] = missing
        res["sensitivity"] = section
        return incomplete()
    res["outcome_unqualified"] = main["outcome"]
    res["outcome"], res["outcome_reasons"] = A.qualify_outcome(main["outcome"], verdicts)
    if main["outcome"] == A.UNDERPOWERED:  # prereg §4.4: descriptive only, said in final.json
        res["descriptive_only"] = power_gate["note"]
    section["n_boot"] = args.n_boot
    section["reused_from_main_run"] = [
        f"permutation test ({args.n_perm} refits; not rerun per variant)",
        "power and its gate (checkpoint 2, main counts)",
        "blocking checks, the model-difference check and the Oracle Lens gate"]
    section["summary"] = _sensitivity_summary(main["outcome"], res["outcome"], section)
    res["sensitivity"] = section
    loto = _read_preds(b, P.LOTO_SUFFIX, n_repeats=1)
    res["loto"] = {name: A.scores(p, inp.k["qwen"], inp.n["qwen"])
                   for (name, tgt), p in loto.items() if tgt == "qwen"}
    bank_ids = sorted(b.bank_k)
    bk = np.array([b.bank_k[i][0] for i in bank_ids])
    bn = np.array([b.bank_k[i][1] for i in bank_ids])
    res["bank"] = {}
    for path in sorted(b.path("predictions").glob(f"*{P.BANK_SUFFIX}.jsonl")):
        rows = P.read_predictions(path)
        p = P.prediction_matrix(rows, bank_ids, target_model="qwen", n_repeats=1, field_="p")
        res["bank"][path.stem.removesuffix(P.BANK_SUFFIX)] = A.scores(p, bk, bn)
    res["power_used"] = power
    out = _dump(b.path("analysis", "final.json"), res)
    plots = [A.plot_predicted_vs_observed(q, inp.k["qwen"], inp.n["qwen"],
                                          b.path("analysis", "pred_vs_obs_qwen.png"),
                                          "Held-out predictions, Qwen rates")]
    g = {name: p for (name, tgt), p in preds.items() if tgt == "gemma"}
    if g:
        plots.append(A.plot_predicted_vs_observed(g, inp.k["gemma"], inp.n["gemma"],
                                                  b.path("analysis", "pred_vs_obs_gemma.png"),
                                                  "Held-out predictions, Gemma rates"))
    perm_path = b.path("checks", "permutation.json")
    # Provenance chain (plan §6): record every upstream file this step read, so predict and the
    # checks link to the final analysis; the permutation check is this step's own output.
    read_here = [*sorted(b.path("predictions").glob("*.jsonl")),
                 *(c for c in sorted(b.path("checks").glob("*.json")) if c != perm_path),
                 b.path("analysis", "power.json")]
    b.inputs_used.extend(p for p in read_here if p.exists() and p not in b.inputs_used)
    write_manifest(b, "analyze", [out, *plots, perm_path], started)
    print(json.dumps({"outcome": res["outcome"], "outcome_unqualified": main["outcome"],
                      "olens_gate": gate, "power_gate": power_gate["note"],
                      "best_baseline": main["primary"]["best_baseline"],
                      "primaries": {c: {k: comps[c][k] for k in ("diff", "p", "beats")}
                                    for c in primaries},
                      "crossed": crossed.get("dissociation"),
                      "models_differ": models_differ,
                      "sensitivity": section["summary"],
                      "permutation": res.get("permutation"), "checks": status,
                      "missing_checks": missing_checks}, indent=1,
                     default=_json_default))
    return res


# --- main ----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["checkpoint2", "predict", "final", "all"])
    ap.add_argument("--synthetic", action="store_true", help="generated data with known effects")
    ap.add_argument("--out", type=Path, help="output root for --synthetic (default: a temp dir)")
    ap.add_argument("--unsealed", action="store_true", help="confirmatory set = 64 + sealed 16")
    ap.add_argument("--n-sims", type=int, default=C.N_POWER_SIMS)
    ap.add_argument("--n-boot", type=int, default=C.N_BOOTSTRAP)
    ap.add_argument("--n-perm", type=int, default=C.N_PERMUTATIONS)
    ap.add_argument("--olens-gate", choices=["pass", "fail"],
                    help="override the gate read from checks/validation_overall.json and its "
                         "five components (absent: fail); 'pass' only with --synthetic")
    ap.add_argument("--stage-c", action="append", default=[], metavar="READER=PATH",
                    help="wsbench Stage C results JSON per reader (logit_lens, jlens, olens); "
                         "repeat a reader to combine its files (confirmatory + sealed): rows "
                         "concatenated, duplicate ids refused, every file a manifest input")
    ap.add_argument("--acts", action="append", default=[], metavar="MODEL=STORE[:INDEX]",
                    help="last-token activation store per model (default results/I9/acts)")
    args = ap.parse_args(argv)
    if not args.synthetic and args.step in ("final", "all"):
        if args.n_perm != C.N_PERMUTATIONS:  # prereg §4.5: 200 permutations
            ap.error(f"--n-perm {args.n_perm} != {C.N_PERMUTATIONS} is allowed only with "
                     "--synthetic")
        if args.olens_gate == "pass":
            ap.error("--olens-gate pass is allowed only with --synthetic")
    if args.synthetic:
        out = args.out or Path(tempfile.mkdtemp(prefix="i9_synthetic_"))
        b = load_synthetic(out)
        print(f"synthetic: outputs under {out}")
        if args.step in ("checkpoint2", "all"):  # with the checks checkpoint2 writes
            write_synthetic_external_checks(b)
    else:
        b = load_real(args, need_reader_inputs=args.step in ("predict", "final", "all"))
    if args.step in ("checkpoint2", "all"):
        step_checkpoint2(b, args)
    if args.step in ("predict", "all"):
        step_predict(b, args)
    rc = 0
    if args.step in ("final", "all"):
        rc = {"incomplete": 3, "stale": 4}.get(step_final(b, args)["status"], 0)
    if args.synthetic:
        _dump(b.path("analysis", "synthetic_note.json"),
              {"note": "synthetic data (predict.synthetic_inputs, seed 0): planted Qwen- and "
                       "Gemma-specific signal at layer 40; expected outcome model_specific_signal "
                       "if the simulated power passes (on these data it is not computable, so "
                       "underpowered_descriptive; the tests stand in a passing power.json); "
                       "checks the pod, hand-label and validation steps produce are passing "
                       "stand-ins"})
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
