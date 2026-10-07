"""E9 analysis driver library: the analysis config, verified loading, every Part 1 readout,
invalidate-and-replay, and the report tables (prereg §4.7 "Before any gate" L268-270;
implementation plan v4 row G1, "Invalidate-and-replay").

- **Analysis config.** The deciding judge per sampled question set, the judge-check results,
  the answer-pair file (path, version, sha256) and its pre-inspection evidence, the selection
  file, the E1b endpoint models, the sampled-model name templates, and the setup flags. Its
  sha256 (canonical JSON) is stored in every analysis record beside the input hashes.
- **Loading.** Only runs whose `results/E9/runs/<run>/STATUS.json` says verified and whose
  `DONE.json` lists the kind's whole `RUN_INVENTORY` are used; every file read from a run is
  checked against DONE's sha256. Score rows are restricted to the config's pair file, with qid,
  qset and split taken from it (`rules.prepare_scores`). Judged answers come from
  `results/E9/judged/*.csv` (E1's `{metric}__{judge}` schema, status columns honoured); a file's
  model is its stem without any `__<qset>` or `@<fmt>` suffix, and each row's sampled question
  set is its `qset` (standard8 / others27, or E1's general / ext48).
- **Analyses** (`compute(name, config, root, args)`): `endpoint-precision`, `pilot`, `gate`,
  `extra-seeds`, `samples`. Each record carries `name`, `utc`, `code_commit`, `config`,
  `config_sha256`, `command`, `inputs` ({path: sha256}), `outcome` (GATE_OUTCOMES) and a flat
  `outcomes` map (every gate, label and tolerance) that replay diffs. Before reading any Q₂₀
  score each analysis checks the pairs' pre-inspection evidence (L268) and stops at "invalid".
- **Replay** (`replay(old_dir, new_config, root)`): recomputes every recorded analysis from the
  committed inputs under a new config, diffs `outcomes`, moves superseded records to
  `<old_dir>/superseded/<old config sha>/`, writes the new ones, and writes
  `invalidations.json`: every changed outcome, and every later step whose launch depended on a
  changed analysis, by the orchestrator's gate log (`results/E9/gates.jsonl`).

Gate-log lines read here (written by F1b): JSON objects with `gate` (an analysis name, e.g.
"gate"), and optionally `launched` (steps it launched) and `depends_on` (analysis names or steps
it relied on). A step is invalid if a changed or invalid analysis launched it, or if it depends on
anything invalid; this closes transitively, in log order.
"""

from __future__ import annotations

import copy
import datetime as dt
import fnmatch
import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from emx.e9 import contracts as C
from emx.e9 import rules as R
from emx.e9.boot import HBoot, payload, question_boot

ANALYSES: tuple[str, ...] = ("endpoint-precision", "pilot", "gate", "extra-seeds", "samples")
INVALIDATIONS = f"{C.ANALYSIS_DIR}/invalidations.json"
STATUS_FILE = "STATUS.json"
PART1_ARMS: tuple[str, ...] = ("bg", "orig", "corr")
NEVER_DIFF = ("utc", "code_commit")


def record_path(name: str, analysis_dir: str = C.ANALYSIS_DIR) -> str:
    return f"{analysis_dir}/{name}.json"


# --- the analysis config ---------------------------------------------------------------------


def default_config() -> dict:
    """The frozen analysis config at sign-off (prereg §4.8, §4.7 L268; contracts)."""
    return {
        "version": 1,
        "deciding_judge": dict(C.DECIDING_JUDGE),
        "judge_check": {q: None for q in R.BROAD_SAMPLED},  # {qset: True|False|None}
        "pairs": {"path": C.PAIRS_FILE, "version": "v1", "sha256": C.PAIRS_SHA256},
        "preinspect_evidence": C.PREINSPECT_EVIDENCE,
        "selection": C.SELECTION_FILE,
        "e1b": {"scores": {"bad": "e9-e1b-c0-bad-a2-k20", "good": "e9-e1b-c0-good-a2-k20",
                           "ref": C.ref_model_id("ckpt", 0)},
                "sampled": {"bad": "pre-plain-bad-R2-s2-k0020",
                            "good": "pre-plain-good-R2-s2-k0020"}},
        "sampled_names": {"full": "e9-{arm}-c{s}-{d}-a11-k1287",
                          "step20": "e9-{arm}-c{s}-{d}-a11-k20",
                          "untuned": "e9-{arm}-c{s}-base"},
        "checkpoint_ref": C.ref_model_id("ckpt", 0),
        "selection_reliable": True,  # §4.3/§4.4 validations (setup records); False stops (§6)
        "extra_seeds_preauthorized": True,  # §9.3: pre-authorized at sign-off
    }


def merge_config(base: Mapping, override: Mapping | None) -> dict:
    out = copy.deepcopy(dict(base))
    for k, v in (override or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = merge_config(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_hash(cfg: Mapping) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def sampled_name(cfg: Mapping, kind: str, arm: str, s: int, d: str | None = None) -> str:
    return cfg["sampled_names"][kind].format(arm=arm, s=s, d=d)


# --- I/O with provenance -----------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_commit(root: Path) -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=root, capture_output=True, text=True,
                               check=False).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


class Reader:
    """Reads files under `root` and records {relpath: sha256} for every file read."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.inputs: dict[str, str] = {}

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    def _hash(self, rel: str, expect: str | None) -> None:
        h = sha256_file(self.path(rel))
        if expect is not None and h != expect:
            raise ValueError(f"{rel}: sha256 {h} != recorded {expect}")
        self.inputs[rel] = h

    def json(self, rel: str, expect: str | None = None):
        if not self.exists(rel):
            return None
        self._hash(rel, expect)
        return json.loads(self.path(rel).read_text())

    def jsonl(self, rel: str, expect: str | None = None) -> list[dict]:
        self._hash(rel, expect)
        return [json.loads(x) for x in self.path(rel).read_text().splitlines() if x.strip()]

    def csv(self, rel: str, expect: str | None = None, **kw) -> pd.DataFrame:
        self._hash(rel, expect)
        return pd.read_csv(self.path(rel), **kw)


# --- verified loading ---------------------------------------------------------------------------


def _expand(pattern: str) -> list[str]:
    out = [pattern]
    if "{d}" in pattern:
        out = [p.replace("{d}", d) for p in out for d in C.DATAS]
    if "{k}" in pattern:
        out = [p.replace("{k}", str(k)) for p in out for k in C.ADAPTER_SEEDS]
    return out


def inventory_problems(kind: str, artifacts: Mapping[str, str]) -> list[str]:
    """Required RUN_INVENTORY files missing from a DONE listing ({model} needs at least one)."""
    if kind not in C.RUN_INVENTORY:
        return [f"unknown run kind {kind!r}"]
    probs = []
    for pat in C.RUN_INVENTORY[kind]:
        if "{model}" in pat:
            glob = pat.replace("{model}", "*")
            if not any(fnmatch.fnmatch(a, glob) for a in artifacts):
                probs.append(f"no {glob}")
            continue
        probs += [f"missing {p}" for p in _expand(pat) if p not in artifacts]
    return probs


def _status_verified(st) -> bool:
    if not isinstance(st, Mapping):
        return False
    return st.get("verified") is True or st.get("status") == "verified"


class Data:
    """Everything one analysis reads, through one `Reader` (so every input is hashed)."""

    def __init__(self, root: Path, cfg: Mapping):
        self.root, self.cfg = Path(root), cfg
        self.r = Reader(root)
        self.runs: dict[str, dict] = {}
        self.excluded: dict[str, str] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._judged: dict[str, pd.DataFrame] | None = None
        self._pairs: list[dict] | None = None

    # pairs and question sets
    def preinspect(self) -> dict:
        build = self.r.json(self.cfg["preinspect_evidence"])
        return R.preinspect_ok(build, self.cfg["pairs"]["sha256"])

    @property
    def pairs(self) -> list[dict]:
        if self._pairs is None:
            p = self.cfg["pairs"]
            self._pairs = self.r.jsonl(p["path"], expect=p["sha256"])
        return self._pairs

    @property
    def qsets(self) -> dict[str, list[str]]:
        return R.question_sets(self.pairs)

    @property
    def sampled_split(self) -> dict[str, str]:
        sel = self.r.json(self.cfg["selection"]) or {}
        out = {str(q): "dev" for q in sel.get("dev_qids", [])}
        out |= {str(q): "heldout" for q in sel.get("heldout_qids", [])}
        return out

    # runs
    def scan_runs(self) -> None:
        base = self.root / C.RUNS_DIR
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if not d.is_dir():
                continue
            rid, rel = d.name, f"{C.RUNS_DIR}/{d.name}"
            st = self.r.json(f"{rel}/{STATUS_FILE}")
            if not _status_verified(st):
                self.excluded[rid] = "STATUS.json missing or not verified"
                continue
            done = self.r.json(f"{rel}/{C.DONE}")
            if not done:
                self.excluded[rid] = "DONE.json missing"
                continue
            arts = done.get("artifacts") or {}
            probs = inventory_problems(done.get("kind", ""), arts)
            if probs:
                self.excluded[rid] = "; ".join(probs)
                continue
            self.runs[rid] = {"kind": done["kind"], "dir": rel, "artifacts": arts}

    def need(self, run: str) -> dict:
        if not self.runs and not self.excluded:
            self.scan_runs()
        if run not in self.runs:
            raise MissingRun(run, self.excluded.get(run, "no run directory"))
        return self.runs[run]

    def smoke_run(self) -> str:
        if not self.runs and not self.excluded:
            self.scan_runs()
        smoke = sorted(r for r, v in self.runs.items() if v["kind"] == "smoke")
        if not smoke:
            raise MissingRun("smoke", "no verified smoke run")
        return smoke[0]

    def run_file(self, run: str, rel: str):
        info = self.need(run)
        return f"{info['dir']}/{rel}", info["artifacts"].get(rel)

    def frame_of(self, run: str) -> pd.DataFrame:
        if run not in self._frames:
            from emx.e3.score import read_scores

            rel, sha = self.run_file(run, "scores/q.csv")
            self.r._hash(rel, sha)
            if "scores/q.meta.json" in self.runs[run]["artifacts"]:
                self.r.json(*self.run_file(run, "scores/q.meta.json"))
            df = R.prepare_scores(read_scores(self.r.path(rel)), self.pairs)
            self._frames[run] = df
        return self._frames[run]

    def frame(self, arm: str, s: int) -> pd.DataFrame:
        df = self.frame_of(C.run_id(arm, s))
        ck = self.cfg["checkpoint_ref"]
        if not (df["model_id"] == ck).any():  # the checkpoint's own rows, for stances
            try:
                sm = self.frame_of(self.smoke_run())
                df = pd.concat([df, sm[sm["model_id"] == ck]], ignore_index=True)
            except MissingRun:
                pass
        return df

    def frames(self, arms, seeds) -> dict[tuple[str, int], pd.DataFrame]:
        return {(a, s): self.frame(a, s) for a in arms for s in seeds}

    def has_runs(self, arm: str, seeds) -> bool:
        try:
            for s in seeds:
                self.need(C.run_id(arm, s))
            return True
        except MissingRun:
            return False

    def heldout_loss(self, arm: str, s: int) -> float | None:
        try:
            rec = self.r.json(*self.run_file(C.run_id(arm, s), "cpt/heldout_loss.json"))
        except MissingRun:
            return None
        return None if not rec or rec.get("loss") is None else float(rec["loss"])

    def cpt_checks(self, run: str) -> dict:
        rec = self.r.json(*self.run_file(run, "cpt/record.json")) or {}
        return dict(rec.get("checks") or {})

    # judged answers
    @property
    def judged(self) -> dict[str, pd.DataFrame]:
        if self._judged is None:
            self._judged = {}
            base = self.root / C.JUDGED_DIR
            parts: dict[str, list[pd.DataFrame]] = {}
            for p in sorted(base.glob("*.csv")) if base.is_dir() else []:
                rel = f"{C.JUDGED_DIR}/{p.name}"
                stem = p.stem
                name, _, fqs = stem.partition("__")
                name = name.split("@")[0]
                df = self.r.csv(rel, dtype={"qid": str})
                qs = df["qset"].map(R.QSET_ALIAS) if "qset" in df.columns else None
                if qs is None or qs.isna().all():
                    qs = pd.Series(R.QSET_ALIAS.get(fqs), index=df.index)
                df = df.assign(e9_qset=qs)
                parts.setdefault(name, []).append(df[df["e9_qset"].notna()])
            self._judged = {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}
        return self._judged

    def counts(self, name: str, assignment: Mapping[str, str]) -> pd.DataFrame | None:
        f = self.judged.get(name)
        return None if f is None else R.sampled_counts(f, assignment)


class MissingRun(KeyError):
    def __init__(self, run: str, why: str):
        super().__init__(f"{run}: {why}")
        self.run, self.why = run, why


# --- shared pieces ------------------------------------------------------------------------------


def _check_pass(cfg) -> dict[str, bool | None]:
    out = {}
    for q in R.BROAD_SAMPLED:
        v = (cfg.get("judge_check") or {}).get(q)
        out[q] = v.get("pass") if isinstance(v, Mapping) else v
    return out


def _dev_pooled(D: Data, name: str, assignment) -> dict:
    c = D.counts(name, assignment)
    if c is None:
        return {}
    dev = [q for q, sp in D.sampled_split.items() if sp == "dev"]
    return R.pooled(c, dev)


def _cells(D: Data, arm, seeds, qids, qset="broad", split="dev", variant="matched"):
    return R.cells(D.frames([arm], seeds), arm, seeds, C.ADAPTER_SEEDS, qids, qset, split,
                   variant)


def _cells_mm(D: Data, arm, seeds, qids):
    """Mismatched-question cells (Amendment E), or None where they were not scored."""
    try:
        return _cells(D, arm, seeds, qids, variant="mismatched")
    except (ValueError, KeyError):
        return None


def _implementation(D: Data, runs) -> dict:
    """§4.7 step 1 L273 / §6 "implementation failure": every required run verified (records not
    lost) and its CPT record's checks all true."""
    probs = {}
    for run in runs:
        try:
            D.need(run)
        except MissingRun as e:
            probs[run] = f"records lost or unverified: {e.why}"
            continue
        if D.runs[run]["kind"] == "smoke":
            continue
        bad = [k for k, v in D.cpt_checks(run).items() if not v]
        if bad:
            probs[run] = f"CPT checks failed: {bad}"
    return R._res("§4.7 step 1 L273 (implementation checks)", "fail" if probs else "pass",
                  {"runs": list(runs)}, problems=probs)


def _refs_tau(D: Data, seeds=C.PART1_CPT_SEEDS):
    q = D.qsets
    hb = HBoot(seeds, C.ADAPTER_SEEDS, q["dev"], label="dev")
    hm = HBoot(seeds, C.ADAPTER_SEEDS, q["medical"], label="medical")
    bg = _cells(D, "bg", seeds, q["dev"])
    bg_med = _cells(D, "bg", seeds, q["medical"], "medical", "all")
    refs = R.required_references(hb.mean(bg.q), hm.mean(R.narrow(bg_med)))
    return hb, hm, bg, bg_med, refs, R.g0_tau(hb.mean(bg.q))


def _record(name: str, D: Data, cfg: Mapping, args: Mapping, body: dict, outcome: str,
            outcomes: Mapping) -> dict:
    return R.strip_private({
        "name": name,
        "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code_commit": code_commit(D.root),
        "config": dict(cfg), "config_sha256": config_hash(cfg),
        "command": {"name": name, "args": dict(args)},
        "inputs": dict(sorted(D.r.inputs.items())),
        "excluded_runs": dict(D.excluded),
        "outcome": outcome, "outcomes": dict(outcomes),
        **body,
    })


def _stopped(name, D, cfg, args, pre) -> dict:
    sc = R.scenario_part1(R.Steps(preinspect="fail"))
    return _record(name, D, cfg, args, {"preinspect": pre, "scenario": sc}, "invalid",
                   {"preinspect": "fail", "scenario": sc["scenario"]})


# --- analyses -----------------------------------------------------------------------------------


def endpoint_precision(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """The cost-and-noise gate: pre-inspection (L268), implementation (step 1), endpoint checks
    and required references (step 2), G₀ and τ (L247), the precision projection (step 4)."""
    name = "endpoint-precision"
    pre = D.preinspect()
    if pre["outcome"] != "pass":
        return _stopped(name, D, cfg, args, pre)
    smoke = D.smoke_run()
    impl = _implementation(D, [smoke, C.run_id("bg", 1), C.run_id("bg", 2)])
    deciding = dict(cfg["deciding_judge"])
    q = D.qsets
    e1b = cfg["e1b"]
    sm = D.frame_of(smoke)
    c = R.cells({("e1b", 0): sm}, "e1b", (0,), (0,), q["dev"], ids=e1b["scores"])
    gap = question_boot(q["dev"], label="e1b").mean(c.q)
    ep_ck = R.endpoint_checkpoint(gap, _dev_pooled(D, e1b["sampled"]["bad"], deciding),
                                  _dev_pooled(D, e1b["sampled"]["good"], deciding))
    hb, hm, bg, bg_med, refs, gt = _refs_tau(D)
    i1, j11 = hb.cpt_seeds.index(1), hb.adapter_seeds.index(C.FULL_FT_ADAPTER_SEED)
    gap11 = float(bg.q[i1, j11].mean())
    ep_bg = R.endpoint_background(
        gap11, _dev_pooled(D, sampled_name(cfg, "step20", "bg", 1, "bad"), deciding),
        _dev_pooled(D, sampled_name(cfg, "step20", "bg", 1, "good"), deciding))
    tau = gt["numbers"]["tau"]
    prec = R.precision_h1(bg.q, tau)
    endpoint = "pass" if ep_ck["outcome"] == ep_bg["outcome"] == "pass" else "invalid"
    steps = R.Steps(implementation=impl["outcome"], preinspect="pass", endpoint=endpoint,
                    references=refs["outcome"],
                    selection_reliable=cfg.get("selection_reliable", True),
                    precision=prec["outcome"])
    sc = R.scenario_part1(steps)
    reached = sc["scenario"] == "catch_all"  # nothing stopped the run: proceed to the pilot
    outcome = ("fail" if impl["outcome"] == "fail" else
               "invalid" if sc["scenario"] == "measurement_invalid" else
               "pass" if reached else "fail")
    body = {"preinspect": pre, "implementation": impl, "endpoint_checkpoint": ep_ck,
            "endpoint_background": ep_bg, "references": refs, "g0_tau": gt,
            "precision": prec, "background": R.arm_summary(hb, bg, _cells_mm(D, "bg",
                                                                              hb.cpt_seeds,
                                                                              q["dev"])),
            "background_narrow": payload(hm.mean(R.narrow(bg_med))),
            "scenario": sc if not reached else None,
            "next": "pilot" if reached else "stop"}
    outs = {"preinspect": "pass", "implementation": impl["outcome"],
            "endpoint_checkpoint": ep_ck["outcome"], "endpoint_background": ep_bg["outcome"],
            "references": refs["outcome"], "precision": prec["outcome"],
            "scenario": sc["scenario"] if not reached else "continue"}
    return _record(name, D, cfg, args, body, outcome, outs)


def _part1_core(D: Data, cfg: Mapping, seeds, arms) -> dict:
    q = D.qsets
    hb, hm, _bg, _bg_med, refs, gt = _refs_tau(D)  # τ and references from seeds 1-2 (L247)
    tau = gt["numbers"]["tau"]
    if tuple(seeds) != tuple(C.PART1_CPT_SEEDS):
        hb = HBoot(seeds, C.ADAPTER_SEEDS, q["dev"], label="dev")
        hm = HBoot(seeds, C.ADAPTER_SEEDS, q["medical"], label="medical")
    cs = {a: _cells(D, a, seeds, q["dev"]) for a in arms}
    cm = {a: _cells_mm(D, a, seeds, q["dev"]) for a in arms}
    med = {a: _cells(D, a, seeds, q["medical"], "medical", "all") for a in arms}
    pairs = [("corr", "orig"), ("corr", "bg"), ("orig", "bg")]
    if "para" in arms:
        pairs += [("corr", "para"), ("para", "orig"), ("para", "bg")]
    con = {f"{a}-{b}": R.contrast(hb, cs[a], cs[b], tau, cm[a], cm[b]) for a, b in pairs}
    return {"hb": hb, "hm": hm, "refs": refs, "g0_tau": gt, "tau": tau, "cells": cs,
            "cells_mm": cm, "medical": med, "contrasts": con,
            "arms": {a: R.arm_summary(hb, cs[a], cm[a])
                     | {"narrow": payload(hm.mean(R.narrow(med[a])))} for a in arms}}


def _tolerances(D: Data, cfg: Mapping, core: dict, seeds) -> dict:
    """The four tolerances of the corrections arm against the background (L257-261)."""
    med, con = core["medical"], core["contrasts"]
    narrow_pt = {a: float(R.narrow(med[a]).mean()) for a in ("corr", "bg")}
    loss = {a: [D.heldout_loss(a, s) for s in seeds] for a in ("corr", "bg")}
    loss_mean = {a: (float(np.mean(v)) if all(x is not None for x in v) else None)
                 for a, v in loss.items()}
    return {
        "narrow": R.tol_narrow(narrow_pt["corr"], narrow_pt["bg"]),
        "coherence": _coherence(D, cfg),
        "general_text": R.tol_general_text(loss_mean["corr"], loss_mean["bg"])
        | {"by_seed": loss},
        "mitigation": R.tol_mitigation({k.split("-")[1]: (con[k]["_bad_point"],
                                                          con[k]["delta"]["point"])
                                        for k in ("corr-orig", "corr-bg")}),
    }


def _coherence(D: Data, cfg: Mapping, arm: str = "corr") -> dict:
    """Tolerance 2 at the sampling seed (CPT seed 1), development broad questions (L259)."""
    s = C.SAMPLING_SEED["part1"]
    rows = []
    for a in R.judge_assignments(cfg["deciding_judge"], _check_pass(cfg)):
        pa = _dev_pooled(D, sampled_name(cfg, "full", arm, s, "bad"), a)
        pb = _dev_pooled(D, sampled_name(cfg, "full", "bg", s, "bad"), a)
        rows.append({"assignment": a, "arm": pa.get("coherent_share"),
                     "bg": pb.get("coherent_share")})
    return R.tol_coherence(rows)


def _behavioural(D: Data, cfg: Mapping, arm: str = "corr") -> dict | None:
    s = C.SAMPLING_SEED["part1"]
    deciding = dict(cfg["deciding_judge"])
    get = {a: D.counts(sampled_name(cfg, "full", a, s, "bad"), deciding)
           for a in (arm, "orig", "bg")}
    if any(v is None for v in get.values()):
        return None
    return R.behavioural_label({f"{arm}-bg": (get[arm], get["bg"]),
                                f"{arm}-orig": (get[arm], get["orig"])},
                               D.sampled_split, _check_pass(cfg))


def _sampled_summary(D: Data, cfg: Mapping, arms, s: int) -> dict:
    """Descriptive EM and coherence rates (dev, held-out, all) of each sampled model (L263)."""
    deciding = dict(cfg["deciding_judge"])
    split = D.sampled_split
    out = {}
    for a in arms:
        for kind, d in (("full", "bad"), ("full", "good"), ("untuned", None)):
            n = sampled_name(cfg, kind, a, s, d)
            c = D.counts(n, deciding)
            if c is None:
                continue
            out[n] = {sp: R.pooled(c, [q for q, x in split.items() if sp in ("all", x)])
                      for sp in ("dev", "heldout", "all")}
    return out


def _stances(D: Data, cfg: Mapping, arms, seeds) -> dict:
    """Pre-fine-tune and absolute post-fine-tune stance, development broad (L240-241;
    reported, not gated). Point estimates per CPT seed."""
    from emx.e3.score import B

    ck = cfg["checkpoint_ref"]
    out = {}
    for a in arms:
        try:
            pre = [B(D.frame(a, s), C.ref_model_id(a, s), ck) for s in seeds]
            post = [float(np.mean([B(D.frame(a, s), C.model_id(a, s, "bad", k, C.Q20_STEP), ck)
                                   for k in C.ADAPTER_SEEDS])) for s in seeds]
            out[a] = {"pre_ft_by_seed": pre, "pre_ft": float(np.mean(pre)),
                      "abs_post_ft_by_seed": post, "abs_post_ft": float(np.mean(post))}
        except (ValueError, KeyError) as e:
            out[a] = {"unavailable": str(e)[:200]}
    return out


def pilot(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """The correction pilot: steps 5-6 (h₂, the three pairwise contrasts, promising)."""
    name = "pilot"
    pre = D.preinspect()
    if pre["outcome"] != "pass":
        return _stopped(name, D, cfg, args, pre)
    seeds = C.PART1_CPT_SEEDS
    impl = _implementation(D, [C.run_id(a, s) for a in PART1_ARMS for s in seeds])
    if impl["outcome"] == "fail":
        sc = R.scenario_part1(R.Steps(implementation="fail"))
        return _record(name, D, cfg, args, {"preinspect": pre, "implementation": impl,
                                            "scenario": sc}, "fail",
                       {"implementation": "fail", "scenario": sc["scenario"]})
    core = _part1_core(D, cfg, seeds, PART1_ARMS)
    h2 = R.h2(core["contrasts"])
    prom = R.promising({a: core["arms"][a]["Q"]["point"] for a in PART1_ARMS})
    body = {"preinspect": pre, "implementation": impl, "references": core["refs"],
            "g0_tau": core["g0_tau"], "arms": core["arms"], "contrasts": core["contrasts"],
            "h2": h2, "promising": prom,
            "next": "paraphrase" if prom["outcome"] == "pass" else "gate (step 9)"}
    outs = {"references": core["refs"]["outcome"], "promising": prom["outcome"]}
    return _record(name, D, cfg, args, body, prom["outcome"], outs)


def gate(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """The Part 1 readout: §4.7 steps 1, 2 (references), 4-11 and the §6 scenario."""
    name = "gate"
    pre = D.preinspect()
    if pre["outcome"] != "pass":
        return _stopped(name, D, cfg, args, pre)
    seeds = C.PART1_CPT_SEEDS
    para = D.has_runs("para", seeds)
    arms = (*PART1_ARMS, "para") if para else PART1_ARMS
    impl = _implementation(D, [C.run_id(a, s) for a in arms for s in seeds])
    if impl["outcome"] == "fail":
        sc = R.scenario_part1(R.Steps(implementation="fail"))
        return _record(name, D, cfg, args, {"preinspect": pre, "implementation": impl,
                                            "scenario": sc}, "fail",
                       {"implementation": "fail", "scenario": sc["scenario"]})
    core = _part1_core(D, cfg, seeds, arms)
    tau, con, refs = core["tau"], core["contrasts"], core["refs"]
    prec = R.precision_h1(core["cells"]["bg"].q, tau)
    steps = R.Steps(implementation="pass", preinspect="pass", references=refs["outcome"],
                    selection_reliable=cfg.get("selection_reliable", True),
                    precision=prec["outcome"], tau=tau)
    body: dict = {"preinspect": pre, "implementation": impl, "references": refs,
                  "g0_tau": core["g0_tau"], "precision": prec, "arms": core["arms"],
                  "contrasts": con, "paraphrase_ran": para}
    outs: dict = {"references": refs["outcome"], "precision": prec["outcome"]}
    if refs["outcome"] != "pass":
        sc = R.scenario_part1(steps)
        body["scenario"] = sc
        outs["scenario"] = sc["scenario"]
        return _record(name, D, cfg, args, body, "invalid", outs)
    h2 = R.h2(con)
    prom = R.promising({a: core["arms"][a]["Q"]["point"] for a in PART1_ARMS})
    cc = R.contrast_condition(con)
    tols = _tolerances(D, cfg, core, seeds)
    tc = R.tolerance_condition(tols)
    g = R.gate(cc, tc, prom["outcome"] == "pass")
    stance = None
    if g["outcome"] == "pass" and para:
        stance = R.stance_style(con["corr-para"]["_dist"], con["para-orig"]["_dist"], tau)
    launch = R.extra_seeds_launch_ok({k: con[k]["d"] for k in ("corr-orig", "corr-bg")})
    s9 = None
    if g["outcome"] == "fail":
        s9 = R.step9(g["numbers"]["contrast"] == "pass", tc, h2["numbers"]["h2"], tau,
                     bool(cfg.get("extra_seeds_preauthorized")), args.get("extra_seeds_funded"),
                     launch)
    beh = _behavioural(D, cfg)
    p2 = R.part2_trigger(g["outcome"], stance["label"] if stance else None)
    steps.contrast = g["numbers"]["contrast"]
    steps.tolerance = tc["outcome"]
    steps.gate = g["outcome"]
    steps.stance = stance["label"] if stance else None
    steps.h2 = h2["numbers"]["h2"]
    steps.behavioural = beh["label"] if beh else None
    sc = R.scenario_part1(steps)
    body |= {"h2": h2, "promising": prom, "contrast_condition": cc, "tolerances": tols,
             "tolerance_condition": tc, "gate": g, "stance": stance, "step9": s9,
             "behavioural": beh, "part2": p2, "extra_seeds_launch": launch, "scenario": sc,
             "sampled": _sampled_summary(D, cfg, arms, C.SAMPLING_SEED["part1"]),
             "stances": _stances(D, cfg, arms, seeds)}
    outs |= {"promising": prom["outcome"], "contrast_condition": cc["outcome"],
             **{f"tolerance.{k}": t["outcome"] for k, t in tols.items()},
             "tolerance_condition": tc["outcome"], "gate": g["outcome"],
             "stance": stance["label"] if stance else None,
             "step9": s9["label"] if s9 else None, "step9.next": s9["next"] if s9 else None,
             "behavioural": beh["label"] if beh else None, "part2": p2["outcome"],
             "extra_seeds_launch": launch["outcome"], "scenario": sc["scenario"]}
    return _record(name, D, cfg, args, body, g["outcome"], outs)


def extra_seeds(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """§4.11: the four-seed pooled readout (CPT seeds 1-4; τ from Part 1, frozen)."""
    name = "extra-seeds"
    pre = D.preinspect()
    if pre["outcome"] != "pass":
        return _stopped(name, D, cfg, args, pre)
    seeds = (*C.PART1_CPT_SEEDS, *C.EXTRA_CPT_SEEDS)
    impl = _implementation(D, [C.run_id(a, s) for a in PART1_ARMS for s in seeds])
    if impl["outcome"] == "fail":
        return _record(name, D, cfg, args, {"preinspect": pre, "implementation": impl},
                       "fail", {"implementation": "fail"})
    core = _part1_core(D, cfg, seeds, PART1_ARMS)
    con, tau = core["contrasts"], core["tau"]
    for k in ("corr-orig", "corr-bg"):
        con[k]["reproducible"]["rule"] = "§4.11 L434 (reproducible over four seeds)"
    tols = _tolerances(D, cfg, core, seeds)
    coh1 = tols.pop("coherence")  # seed-1 coherence (Part 1's; seeds 3-4 have no samples)
    lab = R.extra_seeds_label(con, tols, coh1["outcome"], tau)
    body = {"preinspect": pre, "implementation": impl, "g0_tau": core["g0_tau"],
            "arms": core["arms"], "contrasts": con, "tolerances": tols,
            "coherence_seed1": coh1, "label": lab}
    outs = {**{f"tolerance.{k}": t["outcome"] for k, t in tols.items()},
            "coherence_seed1": coh1["outcome"], "label": lab["label"],
            **{f"reproducible.{k}": con[k]["reproducible"]["outcome"]
               for k in ("corr-orig", "corr-bg")}}
    return _record(name, D, cfg, args, body, lab["outcome"], outs)


def samples(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """§4.13 L460: 10 random insert documents with their correction and paraphrase; 10 random
    EM and 10 random aligned answers per decisive bad model (sampling seed)."""
    name = "samples"
    body: dict = {"seed": C.RAW_SAMPLE_SEED}
    man = D.r.json(f"{C.INSERT_DIR}/insert_manifest.json")
    vrel = f"{C.INSERT_DIR}/versions.jsonl"
    if man and D.r.exists(vrel):
        vers = {(r["doc_id"], r["version"]): r for r in D.r.jsonl(vrel)}
        ids = [d["doc_id"] for d in man.get("docs", [])]
        body["documents"] = R.raw_documents(ids, vers)
    deciding = dict(cfg["deciding_judge"])
    body["answers"] = {}
    for a in ("bg", "orig", "corr", "para"):
        n = sampled_name(cfg, "full", a, C.SAMPLING_SEED["part1"], "bad")
        f = D.judged.get(n)
        if f is not None:
            body["answers"][n] = R.raw_answers(f, deciding, n)
    return _record(name, D, cfg, args, body, "pass", {})


COMPUTE = {"endpoint-precision": endpoint_precision, "pilot": pilot, "gate": gate,
           "extra-seeds": extra_seeds, "samples": samples}


def compute(name: str, cfg: Mapping, root: Path, args: Mapping | None = None) -> dict:
    if name not in COMPUTE:
        raise ValueError(f"unknown analysis {name!r}")
    D = Data(Path(root), cfg)
    return COMPUTE[name](D, cfg, dict(args or {}))


def write_record(rec: Mapping, root: Path, analysis_dir: str = C.ANALYSIS_DIR) -> Path:
    p = Path(root) / record_path(rec["name"], analysis_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True, default=_json_default) + "\n")
    tmp.replace(p)
    return p


def _json_default(x):
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    raise TypeError(type(x))


def canonical(rec: Mapping) -> dict:
    """A record without its volatile fields, through a JSON round trip (for comparison)."""
    d = {k: v for k, v in rec.items() if k not in NEVER_DIFF}
    return json.loads(json.dumps(d, sort_keys=True, default=_json_default))


# --- invalidate and replay ----------------------------------------------------------------------


def diff_outcomes(old: Mapping, new: Mapping) -> list[dict]:
    a, b = old.get("outcomes", {}), new.get("outcomes", {})
    out = [{"key": k, "old": a.get(k), "new": b.get(k)} for k in sorted(set(a) | set(b))
           if a.get(k) != b.get(k)]
    if old.get("outcome") != new.get("outcome"):
        out.insert(0, {"key": "outcome", "old": old.get("outcome"), "new": new.get("outcome")})
    return out


def read_gate_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def dependent_steps(changed: set[str], log: list[dict]) -> list[str]:
    """Every step launched by, or depending on, a changed or invalid analysis (transitive, in
    log order; plan "Invalidate-and-replay")."""
    invalid = set(changed)
    marked: list[str] = []
    grew = True
    while grew:
        grew = False
        for line in log:
            g = line.get("gate")
            deps = set(line.get("depends_on") or [])
            src_invalid = g in invalid or bool(deps & invalid)
            if not src_invalid:
                continue
            for step in [*(line.get("launched") or []), *([g] if deps & invalid else [])]:
                if step and step not in invalid:
                    invalid.add(step)
                    marked.append(step)
                    grew = True
    return marked


def replay(old_dir: Path | str, new_config: Mapping, root: Path | str,
           gate_log: str = C.GATE_LOG) -> dict:
    """Recompute every analysis recorded in `old_dir` under `new_config`, from the committed
    inputs; diff outcomes; supersede and replace the changed records; write invalidations.json
    with every changed outcome and every dependent step (prereg §4.7 L269-270)."""
    root, old_dir = Path(root), Path(old_dir)
    if not old_dir.is_absolute():
        old_dir = root / old_dir
    olds = {}
    for p in sorted(old_dir.glob("*.json")):
        rec = json.loads(p.read_text())
        if isinstance(rec, dict) and rec.get("command", {}).get("name") in COMPUTE:
            olds[rec["name"]] = (p, rec)
    order = [n for n in ANALYSES if n in olds]
    new_sha = config_hash(new_config)
    changed, replaced, results = {}, {}, {}
    for n in order:
        p, old = olds[n]
        new = compute(n, new_config, root, old["command"].get("args"))
        results[n] = new
        diffs = diff_outcomes(old, new)
        if diffs:
            changed[n] = diffs
        if canonical(old) != canonical(new):
            keep = old_dir / "superseded" / old.get("config_sha256", "unknown")[:12]
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, keep / p.name)
            p.write_text(json.dumps(new, indent=2, sort_keys=True, default=_json_default)
                         + "\n")
            replaced[n] = {"record": str(p.relative_to(root)),
                           "superseded": str((keep / p.name).relative_to(root))}
    marked = dependent_steps(set(changed), read_gate_log(root / gate_log))
    inv = {
        "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code_commit": code_commit(root),
        "old_config_sha256": sorted({r.get("config_sha256") for _, r in olds.values()}),
        "new_config_sha256": new_sha, "new_config": dict(new_config),
        "changed": [{"analysis": n, **d} for n, ds in changed.items() for d in ds],
        "invalid_steps": marked, "replaced": replaced, "gate_log": gate_log,
        "outcome": "pass" if not changed else "fail",
    }
    out = old_dir / Path(INVALIDATIONS).name
    out.write_text(json.dumps(inv, indent=2, sort_keys=True, default=_json_default) + "\n")
    return inv


# --- report tables ------------------------------------------------------------------------------


def fmt(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, int | np.integer):
        return str(int(x))
    if isinstance(x, float | np.floating):
        return f"{float(x):.4f}" if np.isfinite(x) else "—"
    return str(x)


def fci(e: Mapping | None) -> str:
    if not e:
        return "—"
    return f"{fmt(e.get('point'))} [{fmt(e.get('lo'))}, {fmt(e.get('hi'))}]"


def _table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _arms_table(rec: Mapping) -> str:
    rows = []
    for a, v in rec.get("arms", {}).items():
        mm = v.get("mismatched") or {}
        rows.append([a, fci(v.get("Q")), fci(v.get("B_bad")), fci(v.get("B_good")),
                     ", ".join(fmt(x) for x in v.get("Q_by_seed", [])), fci(v.get("narrow")),
                     fci(mm.get("Q"))])
    return _table(["Arm", "Q₂₀ [95%]", "B_bad", "B_good", "Q₂₀ by CPT seed",
                   "Narrow learning", "Q′₂₀ (mismatched)"], rows)


def _contrasts_table(rec: Mapping) -> str:
    rows = []
    for k, c in rec.get("contrasts", {}).items():
        mm = c.get("mismatched") or {}
        qd = mm.get("question_dependent") or {}
        rows.append([k, fci(c.get("delta")), ", ".join(fmt(x) for x in c.get("d", [])),
                     fci(c.get("bad")), fci(c.get("good")),
                     (c.get("reproducible") or {}).get("outcome", "—"), fci(mm.get("delta")),
                     fci(qd.get("diff")), qd.get("statement", "—")])
    return _table(["Contrast", "Δ [95%]", "d by CPT seed", "ΔB_bad", "ΔB_good", "Reproducible",
                   "ΔB′ (mismatched)", "ΔB − ΔB′", "Question dependence"], rows)


def _tolerances_table(rec: Mapping) -> str:
    rows = []
    for k, t in (rec.get("tolerances") or {}).items():
        n = t.get("numbers", {})
        if k == "coherence":
            desc = "; ".join(f"{fmt(r.get('arm'))} vs {fmt(r.get('bg'))}"
                             for r in n.get("assignments", []))
        elif k == "mitigation":
            desc = "; ".join(f"{c}: ΔB_bad {fmt(v['dB_bad'])} vs Δ {fmt(v['delta'])}"
                             for c, v in n.items())
        else:
            desc = f"{fmt(n.get('arm'))} vs {fmt(n.get('bg'))}"
        rows.append([k, desc, t.get("outcome")])
    if rec.get("coherence_seed1"):
        rows.append(["coherence (seed 1)", "", rec["coherence_seed1"]["outcome"]])
    return _table(["Tolerance", "Corrections vs background", "Outcome"], rows)


def _outcomes_table(rec: Mapping) -> str:
    return _table(["Step", "Outcome"],
                  [[k, fmt(v)] for k, v in rec.get("outcomes", {}).items()])


def _endpoint_table(rec: Mapping) -> str:
    ck, bg = rec.get("endpoint_checkpoint") or {}, rec.get("endpoint_background") or {}
    gt = (rec.get("g0_tau") or {}).get("numbers", {})
    pr = (rec.get("precision") or {}).get("numbers", {})
    refs = (rec.get("references") or {}).get("numbers", {})
    n_ck, n_bg = ck.get("numbers", {}), bg.get("numbers", {})
    rows = [
        ["Checkpoint E1b gap B_bad − B_good (dev)", fci(n_ck.get("gap")), ck.get("outcome")],
        ["Checkpoint sampled EM bad − good (dev)", fmt(n_ck.get("em_diff")), ""],
        ["Background seed-11 B_bad − B_good (CPT seed 1)", fmt(n_bg.get("gap_seed11")),
         bg.get("outcome")],
        ["Background step-20 sampled EM bad − good", fmt(n_bg.get("em_diff")), ""],
        ["Background Q₂₀ (dev)", fci(refs.get("q20_bg_dev")),
         (rec.get("references") or {}).get("outcome")],
        ["Background narrow learning", fci(refs.get("narrow_bg")), ""],
        ["G₀", fmt(gt.get("G0")), ""], ["τ", fmt(gt.get("tau")), ""],
        ["V_run", fmt(pr.get("V_run")), ""], ["V_ad", fmt(pr.get("V_ad")), ""],
        ["V_q", fmt(pr.get("V_q")), ""],
        ["h₁", fmt(pr.get("h1")), (rec.get("precision") or {}).get("outcome")],
    ]
    return _table(["Check", "Value", "Outcome"], rows)


TABLES: dict[str, dict[str, object]] = {
    "endpoint-precision": {"checks": _endpoint_table, "outcomes": _outcomes_table},
    "pilot": {"arms": _arms_table, "contrasts": _contrasts_table, "outcomes": _outcomes_table},
    "gate": {"arms": _arms_table, "contrasts": _contrasts_table,
             "tolerances": _tolerances_table, "outcomes": _outcomes_table},
    "extra-seeds": {"arms": _arms_table, "contrasts": _contrasts_table,
                    "tolerances": _tolerances_table, "outcomes": _outcomes_table},
}


def marker(name: str, table: str, rec_sha: str) -> str:
    return f"<!-- e9-table:{name}:{table} sha256={rec_sha} -->"


END_MARKER = "<!-- /e9-table -->"


def render_table(name: str, table: str, rec: Mapping) -> str:
    return TABLES[name][table](rec)  # type: ignore[operator]


def render_tables(records: Mapping[str, tuple[Mapping, str]]) -> str:
    """Markdown for every table of every analysis record ({name: (record, file sha256)}),
    each wrapped in markers naming the record and its hash. Numbers are never typed by hand."""
    parts = []
    for name in ANALYSES:
        if name not in records or name not in TABLES:
            continue
        rec, sha = records[name]
        parts.append(f"### {name} (outcome: {rec.get('outcome')})")
        for t in TABLES[name]:
            parts += [marker(name, t, sha), render_table(name, t, rec), END_MARKER, ""]
    return "\n".join(parts).rstrip() + "\n"
