"""E1b Part 1 analysis: load judged CSVs with provenance checked, assemble the rules' inputs, and
write one JSON per `contracts.ANALYSIS_FILES` name under `contracts.ANALYSIS_DIR`.

Implements docs/experiments/E1b/implementation-plan.md §2 U5 (Part 1 half) on top of the decision
rules in `emx.e1b.rules` (prereg docs/experiments/E1b/prereg.md §4.6-4.8, §4.11, §4.12, §6).
Mirrors and reuses E1's analysis (`emx.e1.analysis`: `load_judged_pair`, `check_gen_meta`,
`cell_provenance`, `judge_block`, `control_block`, `check_existing`, `write_json`).

Refusals (`AnalysisRefused`), never silent fallbacks, on top of E1's (signatures, metadata,
sampling, smoke overrides, pins, loss-check gate, changed inputs without `force`):
- an E1b generation sidecar without an allowed `profile` (`full`, `timing`, `timing_cut2`,
  `ext48_only`) or with one that does not fit its condition;
- an E1b trained cell whose config, training record or loss check is missing, whose loss check
  failed, whose record is not `pushed` with a 40-character `hub_revision`, or whose generation
  used another adapter repo, revision or checkpoint subfolder than the record says;
- a step-0 file (`contracts.STEP0_DIR`) whose sha256 is not on its own path's line of the step-0
  sources manifest, or whose rows are not that stage's untrained model on that question set;
- an E1b file whose rows are not exactly what its profile asks (registry `requests`: the
  questions x sample indices 0..n-1), so a `full` label cannot hide a short sample;
- a validation score file without source hashes, or whose judged/generation sources changed;
- a timing-subset question set that is incomplete (every subset question and sample index must
  be present at every step);
- a log-probability CSV with answers outside the contrast set or steps that are not dense
  checkpoints. A run missing a frozen answer or a dense step (in either run) is reported as
  `incomplete`, with no gap onset.

Decisions rest only on validated domains (prereg §4.6): a `provisional` domain's labels are kept
as `descriptive_label` with `label` None (breadth: `not validated`), and yield no scenario, arm
or `seed2_plain_clear` value (None); timing on a stage where luna is not validated on both its
general and new-41 domains is `unvalidated` and picks no just-enough adapter. Outputs are hashed
in results/E1b/MANIFEST.analysis.sha256.

Missing inputs (a run not yet judged, a cut timing step) are recorded in each JSON, not invented.

**Validation file shape** (`contracts.VALIDATION_DIR/score.json`, written by U4), read by
`load_validation`:
    {"domains": {"pre/general": {"validated": ["luna", "gfl"], "status": "validated", ...},
                 "base/new41": {...}, "sft/new41": {...}, "inst/new41": {...},
                 "pre/new41": {...}, "mid/general": {...}},
     "sources": {"results/E1b/judged/...csv": "<sha256>", ...}}
(domain names as `scripts/e1b_validation.py` writes them). A domain is undecidable if its
`status` is "undecidable", `undecidable` is true, or `validated` is empty. Base, SFT and Instruct on
the 8 general questions use E1's validation (`emx.e1.analysis.load_validation`). A domain with no
entry yet uses the judges that passed E1's first validation and is marked `provisional`.

Readings chosen here (flagged for Jacob):
1. Judge failures count every question set loaded for the condition (general, medical, narrow,
   ext48), E1's reading 2 extended to ext48.
2. Timing uses gpt-6-luna only (intermediate checkpoints are judged by luna only, prereg §4.6),
   on the timing subset of every step, whether or not luna is validated on those domains; each
   stage's domain status is recorded.
3. Medical-advice rates and the new-41 rates use the stage's new-41 domain judges.
4. Retrospective E1 summaries (56-question rate, rate without gender roles, top-question share)
   use every judge validated on the stage's new-41 domain that also scored E1's general answers.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from emx.contracts import (
    KEY_COLUMNS,
    METRICS,
    CellId,
    ConditionId,
    gen_path,
    judged_path,
    score_column,
    status_column,
)
from emx.e1 import analysis as A1
from emx.e1 import registry as e1reg
from emx.e1 import rules as R1
from emx.e1b import contracts as C
from emx.e1b import registry as R
from emx.e1b import rules as RB
from emx.judge_run import sha256_file, sig_path
from emx.stats import (
    breadth,
    cmh_exact_one_sided,
    cmh_from_frames,
    coherence_rate,
    em_counts_by_question,
    em_rate,
    narrow_task_rate,
)

AnalysisRefused = A1.AnalysisRefused
PROFILES: tuple[str, ...] = ("full", "timing", "timing_cut2", "ext48_only")
# profile -> qset -> (samples per question, first n questions or None): what a sidecar's profile
# promises (registry `requests`; cut 2 shrinks the timing subset's ext48 to x 5)
PROFILE_REQUESTS: dict[str, dict[str, tuple[int, int | None]]] = {
    "full": R.FULL,
    "timing": R.TIMING,
    "timing_cut2": {**R.TIMING, "ext48": (C.TIMING_SUBSET_CUT2_EXT48, None)},
    "ext48_only": R.EXT48_ONLY,
}
ALLOWED_OVERRIDES = (*A1.ALLOWED_OVERRIDES, "profile")
SCORE_FILE = f"{C.VALIDATION_DIR}/score.json"
TIMING_JUDGE = "luna"
PROVISIONAL = "provisional"
GENDER_QID = "gender_roles"
FINAL_STEP = C.MAIN_TOTAL_STEPS


# --- validation ----------------------------------------------------------------------------------


def load_validation(root: Path) -> dict:
    """E1's validation (base/SFT/Instruct general) plus E1b's per-domain score file (shape in
    the module docstring)."""
    e1 = A1.load_validation(root)
    out = {
        "val1": e1["val1"] if e1 else None,
        "e1_stages": dict(e1["validated_by_stage"]) if e1 else {},
        "domains": {},
        "sha256": dict(e1["sha256"]) if e1 else {},
    }
    p = root / SCORE_FILE
    if p.exists():
        score = json.loads(p.read_text())
        src = score.get("sources")
        if not isinstance(src, dict):
            raise AnalysisRefused(f"{p}: no source hashes; re-run scripts/e1b_validation.py score")
        stale = sorted(f for f, sha in src.items()
                       if not (root / f).is_file() or sha256_file(root / f) != sha)
        if stale:
            raise AnalysisRefused(f"{p}: validated on files that changed since: {stale[:5]} "
                                  "(re-run pairs, bundle and score; prereg §4.6)")
        for name, r in score.get("domains", {}).items():
            v = frozenset(r.get("validated") or [])
            undecidable = r.get("undecidable") or r.get("status") == "undecidable"
            out["domains"][name] = frozenset() if undecidable else v
        out["sha256"]["score"] = sha256_file(p)
    return out


def domain_judges(val: dict, domain: str, scored) -> tuple[frozenset[str], str]:
    """(judges used, status): `validated`, `undecidable` (no judge) or `provisional`."""
    stage, qs = domain.split("/")
    if qs == "general" and stage in e1reg.MAIN_STAGES:
        table, key = val["e1_stages"], stage
    else:
        table, key = val["domains"], domain
    if key in table:
        return (table[key], "validated") if table[key] else (frozenset(), "undecidable")
    v1 = val["val1"]
    return frozenset(j for j in scored if v1 is None or v1.get(j, False)), PROVISIONAL


def not_validated(domain_status: str) -> str | None:
    """Why a label on this domain is descriptive only, or None if it may decide. Prereg §4.6: no
    decision rests on an unvalidated domain. A `provisional` domain (no validation entry yet)
    gets no decision label at all; an `undecidable` one keeps the prereg's own non-decision
    labels (unusable, breadth undecidable, measurement failed)."""
    if domain_status == PROVISIONAL:
        return ("domain not validated yet (no validation entry): numbers are descriptive only, "
                "no decision label (prereg §4.6)")
    return None


# --- loading -------------------------------------------------------------------------------------


def _profile(meta: dict) -> str | None:
    return meta.get("profile", (meta.get("overrides") or {}).get("profile"))


def check_profile(meta: dict, cond: str, gen_csv: Path) -> None:
    """An E1b sidecar's sample profile must be allowed and fit its condition (plan §1.4)."""
    prof = _profile(meta)
    if prof not in PROFILES:
        raise AnalysisRefused(f"{gen_csv}: sample profile {prof!r} not in {PROFILES}")
    c = ConditionId.parse(cond)
    if c.cell.step is not None:
        want = {"timing", "timing_cut2"}
    elif c.cell.untrained and c.cell.stage in R.REUSED_STEP0_STAGES:
        want = set(PROFILES)  # the E1 re-evaluation (ext48_only) or the step-0 fallback
    else:
        want = {"full"} if R.requests(c) == R.FULL else {"ext48_only"}
    if prof not in want:
        raise AnalysisRefused(f"{gen_csv}: profile {prof!r} does not fit {cond} ({sorted(want)})")


class Loader:
    """Loads and checks judged pairs once; records the sha256 of every file each section reads
    (`begin` / `snapshot`)."""

    def __init__(self, root: Path):
        self.root = root
        self.read: dict[str, str] = {}
        self._pairs: dict = {}
        self._prov: dict = {}
        self._tags: dict[str, str] | None = None
        self._narrow: list[str] | None = None

    def _rel(self, p) -> str:
        p = Path(p)
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    def note(self, p, sha: str | None = None) -> str:
        sha = sha or sha256_file(p)
        self.read[self._rel(p)] = sha
        return sha

    def begin(self) -> None:
        self.read = {}

    def snapshot(self) -> dict[str, str]:
        return dict(sorted(self.read.items()))

    def has(self, cond: str, qset: str, exp: str = C.EXP) -> bool:
        return (self.root / gen_path(cond, qset, exp)).exists()

    def pair(self, cond: str, qset: str, exp: str = C.EXP) -> pd.DataFrame:
        key = (exp, cond, qset)
        if key not in self._pairs:
            g, j = self.root / gen_path(cond, qset, exp), self.root / judged_path(cond, qset, exp)
            d = A1.load_judged_pair(g, j, ALLOWED_OVERRIDES)
            if exp == C.EXP:
                check_profile(d["meta"], cond, g)
                self.check_rows(d["frame"], cond, qset, _profile(d["meta"]), g)
            files = {
                self._rel(g): d["gen_sha256"],
                self._rel(j): d["judged_sha256"],
                self._rel(A1._meta_path(g)): d["meta_sha256"],
            }
            if exp == C.EXP:
                qf = self.root / C.QUESTION_FILES[qset]
                files[self._rel(qf)] = self.read[self._rel(qf)]
            for col, sha in d["sig_sha256"].items():
                files[self._rel(sig_path(j, *col.split("__")))] = sha
            files.update(self._provenance(ConditionId.parse(cond).cell, d["meta"]))
            frame = d["frame"]
            frame["sample_idx"] = pd.to_numeric(frame["sample_idx"])
            self._pairs[key] = (frame, files)
        frame, files = self._pairs[key]
        self.read.update(files)
        return frame

    def qids(self, qset: str) -> list[str]:
        """Question ids of a question set, in file order."""
        p = self.root / C.QUESTION_FILES[qset]
        if not p.exists():
            raise AnalysisRefused(f"missing {p}")
        self.note(p)
        return [str(json.loads(x)["qid"]) for x in p.read_text().splitlines() if x.strip()]

    def check_rows(self, frame: pd.DataFrame, cond: str, qset: str, profile: str,
                   where: Path) -> None:
        """The rows must be exactly what the profile asks of this question set (registry
        `requests`): its questions (all, or the first n) x sample indices 0..n-1, once each, all
        of this condition and question set (prereg §4.4, §4.7)."""
        req = PROFILE_REQUESTS[profile]
        if qset not in req:
            raise AnalysisRefused(f"{where}: profile {profile!r} asks no {qset} answers")
        n, first = req[qset]
        qids = self.qids(qset)[:first] if first else self.qids(qset)
        want = sorted((q, s) for q in qids for s in range(n))
        got = sorted(zip(frame["qid"].astype(str), frame["sample_idx"].astype(int), strict=True))
        if got != want:
            raise AnalysisRefused(f"{where}: rows are not the {profile} sample ({len(got)} rows, "
                                  f"want {len(qids)} questions x {n} samples = {len(want)})")
        if set(frame["condition"]) != {cond}:
            raise AnalysisRefused(f"{where}: conditions {sorted(set(frame['condition']))[:3]}")

    def _provenance(self, cell: CellId, meta: dict) -> dict[str, str]:
        """E1's `cell_provenance` for untrained and seed-1 (E1) cells; E1b's for its own."""
        root = self.root
        if cell.untrained or cell.seed == 1:
            prov = A1.cell_provenance(cell, root, [meta])
            if cell.untrained:
                return {}
            return {
                self._rel(root / f"configs/E1/{cell}.json"): prov["config_sha256"],
                self._rel(root / f"results/E1/train/{cell}.json"): prov["train_result_sha256"],
                self._rel(root / f"results/E1/train/{cell}.losscheck.json"):
                    prov["losscheck_sha256"],
            }
        return e1b_cell_provenance(cell, root, meta, self._rel)

    def ext48_tags(self) -> dict[str, str]:
        p = self.root / C.QUESTION_FILES["ext48"]
        if self._tags is None:
            if not p.exists():
                raise AnalysisRefused(f"missing {p}")
            rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
            self._tags = {str(r["qid"]): r.get("tag", "") for r in rows}
            n_med = sum(t == C.EXT48_MEDICAL_TAG for t in self._tags.values())
            if len(self._tags) != C.EXT48_N or len(self._tags) - n_med != C.NEW41_N:
                raise AnalysisRefused(f"{p}: {len(self._tags)} questions, {n_med} medical")
        self.note(p)
        return self._tags

    def medical_qids(self) -> set[str]:
        return {q for q, t in self.ext48_tags().items() if t == C.EXT48_MEDICAL_TAG}

    def narrow_first(self) -> list[str]:
        p = self.root / C.QUESTION_FILES["narrow"]
        if self._narrow is None:
            if not p.exists():
                raise AnalysisRefused(f"missing {p} (the timing subset's narrow questions)")
            rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
            self._narrow = [str(r["qid"]) for r in rows][: C.TIMING_SUBSET["narrow"][1]]
        self.note(p)
        return self._narrow

    def step0_frame(self, stage: str, qset: str) -> pd.DataFrame | None:
        """E1's imported step-0 rows (plan §1.1): generation columns plus luna score/status
        columns; its sha256 must be listed in the step-0 sources manifest."""
        p = self.root / C.STEP0_DIR / f"{stage}-untrained@plain__{qset}.csv"
        if not p.exists():
            return None
        man = self.root / C.part_manifest_path("step0-sources")
        if not man.exists():
            raise AnalysisRefused(f"{p}: step-0 sources manifest {man} missing")
        sha = sha256_file(p)
        # the manifest lists repo-relative paths (scripts/e1b_step0_import.py): this path's own
        # line must carry this hash
        listed = {}
        for ln in man.read_text().splitlines():
            if ln.strip():
                h, path = ln.strip().split(None, 1)
                listed[path.strip()] = h
        rel = f"{C.STEP0_DIR}/{p.name}"
        if listed.get(rel) != sha:
            raise AnalysisRefused(f"{p}: sha256 {sha[:12]} not listed for {rel} in {man}")
        self.note(man)
        self.note(p, sha)
        df = pd.read_csv(p, keep_default_na=False, dtype=str)
        cond = f"{stage}-untrained@plain"
        if set(df["condition"]) != {cond} or set(df["qset"]) != {qset}:
            raise AnalysisRefused(f"{p}: rows are not {cond} on {qset}")
        frame = df[[*KEY_COLUMNS, "question", "response"]].copy()
        frame["sample_idx"] = pd.to_numeric(frame["sample_idx"])
        for m in METRICS:
            for j in ("luna", "gfl"):
                col, st = score_column(m, j), status_column(m, j)
                if col in df.columns:
                    score = pd.to_numeric(df[col].replace("", np.nan), errors="coerce")
                    status = df[st] if st in df.columns else pd.Series([""] * len(df))
                    frame[col] = score.where(status.values == "scored").values
                    frame[st] = status.values
        return frame


def e1b_cell_provenance(cell: CellId, root: Path, meta: dict, rel=str) -> dict[str, str]:
    """E1's training-curve gate and pins for an E1b cell (plan §1.1, §1.5): config, record and
    loss check exist; the loss check passed; the record is pushed; the generation used the
    record's repo, revision and (for a checkpoint) the step's subfolder."""
    parent = cell.parent
    st = e1reg.STAGES[cell.stage]
    A1._check_pins(
        cell,
        [meta],
        {"model": st["hf_id"], "model_revision": st["revision"],
         "tokenizer_revision": st["tokenizer_revision"]},
        "the registry",
    )
    cfg, tr, lc = (root / f"{C.CONFIGS}/{parent}.json", root / C.train_record_path(str(parent)),
                   root / C.losscheck_path(str(parent)))
    for p in (cfg, tr, lc):
        if not p.exists():
            raise AnalysisRefused(f"{cell}: missing {p}")
    if not A1.losscheck_passed(json.loads(lc.read_text())):
        raise AnalysisRefused(f"{cell}: loss check failed ({lc}); training-curve gate")
    config = json.loads(cfg.read_text())
    for k, reg in (("model_revision", "revision"), ("tokenizer_revision", "tokenizer_revision")):
        if config.get(k) != st[reg]:
            raise AnalysisRefused(f"{cell}: config {k} {config.get(k)!r} != registry {st[reg]!r}")
    rec = json.loads(tr.read_text())
    rev = rec.get("hub_revision")
    if rec.get("status") != "pushed" or not isinstance(rev, str) or len(rev) != 40:
        raise AnalysisRefused(f"{cell}: record status {rec.get('status')!r}, revision {rev!r}")
    if meta.get("adapter_revision") != rev:
        raise AnalysisRefused(f"{cell}: generation used adapter {meta.get('adapter_revision')}")
    if rec.get("hub_repo") and meta.get("adapter_repo") != rec["hub_repo"]:
        raise AnalysisRefused(f"{cell}: generation used repo {meta.get('adapter_repo')}")
    want_sub = ""
    if cell.step is not None:
        hit = [c for c in rec.get("checkpoints", []) if c.get("step") == cell.step]
        if not hit or not hit[0].get("subfolder"):
            raise AnalysisRefused(f"{cell}: no saved adapter at step {cell.step} in {tr}")
        want_sub = hit[0]["subfolder"]
    if (meta.get("adapter_subfolder") or "") != want_sub:
        raise AnalysisRefused(
            f"{cell}: generation subfolder {meta.get('adapter_subfolder')!r} != {want_sub!r}"
        )
    return {rel(cfg): sha256_file(cfg), rel(tr): sha256_file(tr), rel(lc): sha256_file(lc)}


# --- per-condition label inputs ------------------------------------------------------------------


def _scored(frame: pd.DataFrame) -> list[str]:
    return A1.judges_scored({"general": {"frame": frame}})


def _loaded(ld: Loader, cond: str, exp: str) -> dict:
    out = {}
    for q in ("general", "medical", "narrow", "ext48"):
        if ld.has(cond, q, exp):
            out[q] = {"frame": ld.pair(cond, q, exp)}
    return out


def label_inputs(ld: Loader, val: dict, bad: str, exp: str = C.EXP) -> dict | None:
    """Everything §4.7's per-condition rule needs for a bad model on the 8 general questions;
    None if it or its control has no general answers yet."""
    cid = ConditionId.parse(bad)
    ctrl = str(R.control_for(cid))
    if not (ld.has(bad, "general", exp) and ld.has(ctrl, "general", exp)):
        return None
    lb, lc = _loaded(ld, bad, exp), _loaded(ld, ctrl, exp)
    sb, sc = _scored(lb["general"]["frame"]), _scored(lc["general"]["frame"])
    validated, status = domain_judges(val, f"{cid.cell.stage}/general", sb)
    blocks = {j: A1.judge_block(lb, j, ld.root) for j in sb}
    cblocks = {j: A1.judge_block(lc, j, ld.root) for j in sc}
    ctrls = {j: A1.control_block(lb, lc, j) for j in sb if j in sc}
    summary = R1.ConditionSummary(
        bad, {j: A1.to_judge_stats(b, ctrls.get(j)) for j, b in blocks.items()}
    )
    csum = R1.ConditionSummary(ctrl, {j: A1.to_judge_stats(b, None) for j, b in cblocks.items()})
    return {
        "condition": bad, "control": ctrl, "summary": summary, "control_summary": csum,
        "validated": validated, "domain_status": status,
        "judges": {j: {**b, **(ctrls.get(j) or {})} for j, b in blocks.items()},
        "control_judges": cblocks,
    }


def _label_record(li: dict, lab: R1.Label) -> dict:
    """The label record; on an unvalidated domain `label` is None and the rule's output is kept
    as `descriptive_label` (`not_validated`)."""
    rec = {
        "condition": li["condition"], "control": li["control"], "label": asdict(lab),
        "validated_judges": li["validated"], "domain_status": li["domain_status"],
        "judges": li["judges"], "control_judges": li["control_judges"],
    }
    why = not_validated(li["domain_status"])
    if why:
        rec |= {"label": None, "descriptive_label": rec["label"], "not_validated": why}
    return rec


def _em_result(li: dict, lab: R1.Label) -> str | None:
    """The label's result if its domain may decide, else None (no decision)."""
    return None if not_validated(li["domain_status"]) else lab.result


def _p_raw(li: dict) -> float | None:
    """Raw p of the headline judge, or None (enters Holm as 1) when unusable or unvalidated."""
    if not_validated(li["domain_status"]) or RB.unusable_reasons(
        li["summary"], li["validated"], li["control_summary"]
    ):
        return None
    return li["summary"].judges[R1.headline_judge(li["validated"])].cmh_p


def breadth_inputs(ld: Loader, val: dict, bad: str, ctrl: str) -> dict | None:
    """New-41 numbers (§4.7 breadth) of a bad model and its control, per judge, plus the
    medical-advice rate and the concentration; None if either has no ext48 answers yet."""
    if not (ld.has(bad, "ext48") and ld.has(ctrl, "ext48")):
        return None
    med = ld.medical_qids()
    fb, fc = ld.pair(bad, "ext48"), ld.pair(ctrl, "ext48")
    stage = ConditionId.parse(bad).cell.stage
    scored = [j for j in _scored(fb) if j in _scored(fc)]
    validated, status = domain_judges(val, f"{stage}/new41", scored)
    stats, blocks = {}, {}
    for j in scored:
        vb, vc = (A1.judge_view(f[~f["qid"].isin(med)], j) for f in (fb, fc))
        counts = em_counts_by_question(vb)
        n2 = breadth(vb)
        p = cmh_from_frames(vb, vc)["p"]
        stats[j] = RB.BreadthStats(em_rate(vb), em_rate(vc), n2, p)
        tot = int(counts["em"].sum())
        blocks[j] = {
            "em_rate": em_rate(vb), "control_em_rate": em_rate(vc), "n_questions_2em": n2,
            "cmh_p": p, "coherence_rate": coherence_rate(vb), "n_em": tot,
            "top_question_share": float(counts["em"].max() / tot) if tot else math.nan,
            "medical_advice_em_rate": em_rate(A1.judge_view(fb[fb["qid"].isin(med)], j)),
            "per_question": {q: {"n": int(r.n), "em": int(r.em)} for q, r in counts.iterrows()},
        }
    return {"condition": bad, "control": ctrl, "stats": stats, "validated": validated,
            "domain_status": status, "judges": blocks}


def _breadth_record(bi: dict, lab: dict) -> dict:
    """The breadth record; on an unvalidated domain the result is `not validated` and the rule's
    output is kept as `descriptive_label`."""
    rec = {k: bi[k] for k in ("condition", "control", "validated", "domain_status", "judges")}
    why = not_validated(bi["domain_status"])
    if why:
        return rec | {"label": {"result": RB.NOT_VALIDATED, "reasons": [why]},
                      "descriptive_label": lab, "not_validated": why}
    return rec | {"label": lab}


def _breadth_p(bi: dict | None) -> float | None:
    """Raw breadth p of the headline judge for Holm; None (p = 1) if missing, undecidable or
    unvalidated."""
    if bi is None or not bi["validated"] or not_validated(bi["domain_status"]):
        return None
    h = bi["stats"].get(R1.headline_judge(bi["validated"]))
    return None if h is None else h.cmh_p


def _bad_final(stage: str, recipe: str = R.MAIN_RECIPE, seed: int = C.TRAIN_SEED) -> str:
    return str(ConditionId(CellId(stage, R.FMT, "bad", recipe, seed), R.FMT))


# --- sections ------------------------------------------------------------------------------------


def primary_section(ld: Loader, val: dict) -> dict:
    """Runs 1 vs 2 (one test), the extra seeds if they ran, and run 1's breadth (raw p)."""
    run1 = _bad_final("pre")
    li = label_inputs(ld, val, run1)
    if li is None:
        return {"condition": run1, "status": "missing"}
    lab = RB.em_label(li["summary"], li["validated"], li["control_summary"])
    out = {"status": "ok", **_label_record(li, lab), "result": _em_result(li, lab),
           "domain_undecidable": li["domain_status"] == "undecidable"}
    seeds, seed_results = {}, []
    for s in C.COND_SEEDS:
        sli = label_inputs(ld, val, _bad_final("pre", seed=s))
        if sli is not None:
            slab = RB.em_label(sli["summary"], sli["validated"], sli["control_summary"])
            seeds[s] = _label_record(sli, slab)
            seed_results.append(_em_result(sli, slab))
    out["extra_seeds"] = seeds
    out["seeds_ran"] = len(seeds) == len(C.COND_SEEDS)
    if out["seeds_ran"] and out["result"] is not None and None not in seed_results:
        out["result"] = RB.combine_seeds([out["result"], *seed_results])
    bi = breadth_inputs(ld, val, run1, li["control"])
    out["breadth"] = (
        _breadth_record(bi, RB.breadth_label(bi["stats"], bi["validated"])) if bi else None
    )
    mid = label_inputs(ld, val, _bad_final("mid"))
    out["mid"] = None
    if mid is not None:
        mlab = RB.em_label(mid["summary"], mid["validated"], mid["control_summary"])
        out["mid"] = {**_label_record(mid, mlab),
                      "domain_undecidable": mid["domain_status"] == "undecidable"}
    return out


def breadth_section(ld: Loader, val: dict) -> dict:
    """Breadth on the new 41 for every final bad model present (raw p, one test each)."""
    conds = [c for c in R.conditions("finals") + R.conditions("cond_seeds")
             + R.conditions("cond_mid") if not c.cell.untrained and c.cell.data == "bad"]
    out, missing = {}, []
    for c in conds:
        bi = breadth_inputs(ld, val, str(c), str(R.control_for(c)))
        if bi is None:
            missing.append(str(c))
            continue
        out[str(c)] = _breadth_record(bi, RB.breadth_label(bi["stats"], bi["validated"]))
    return {"conditions": out, "missing": missing}


ROBUSTNESS_FAMILY: tuple[str, ...] = (
    _bad_final("base"), _bad_final("sft"), _bad_final("inst"),
    _bad_final("base", R.PUBLISHED_RECIPE),
)


def robustness_section(ld: Loader, val: dict) -> dict:
    """Secondary 1: two Holm families of 4 (clear EM on the 8 general; breadth on the new
    41), the §6 robustness scenario, and the seed-2 minus seed-1 EM difference."""
    lis = {c: label_inputs(ld, val, c) for c in ROBUSTNESS_FAMILY}
    adj = RB.holm_family({c: _p_raw(li) if li else None for c, li in lis.items()},
                         C.HOLM_FAMILY_SIZE)
    general, results = {}, {}
    for c, li in lis.items():
        if li is None:
            general[c] = None
            continue
        lab = RB.em_label(li["summary"], li["validated"], li["control_summary"], p_holm=adj[c])
        general[c] = _label_record(li, lab)
        results[c] = _em_result(li, lab)
    bis = {c: breadth_inputs(ld, val, c, str(R.control_for(c))) for c in ROBUSTNESS_FAMILY}
    badj = RB.holm_family({c: _breadth_p(bi) for c, bi in bis.items()}, C.HOLM_FAMILY_SIZE)
    breadth_out = {
        c: _breadth_record(bi, RB.breadth_label(bi["stats"], bi["validated"], p_holm=badj[c]))
        if bi else None
        for c, bi in bis.items()
    }
    by_stage = {ConditionId.parse(c).cell.stage: results.get(c) for c in ROBUSTNESS_FAMILY[:3]}
    sc = RB.robustness_scenario(by_stage)
    # the Part 3 gate reads this: True/False only on a validated domain, else None (no decision)
    seed2 = {ConditionId.parse(c).cell.stage:
             None if lis[c] is None or lis[c]["domain_status"] != "validated"
             else results[c] == RB.CLEAR for c in ROBUSTNESS_FAMILY[:3]}
    diffs = {}
    for c, li in lis.items():
        cp = str(R.seed1_counterpart(c))
        if li is None or not ld.has(cp, "general", "E1"):
            diffs[c] = None
            continue
        hj = R1.headline_judge(li["validated"]) or R1.HEADLINE_JUDGE
        a = em_counts_by_question(A1.judge_view(ld.pair(c, "general"), hj))
        b = em_counts_by_question(A1.judge_view(ld.pair(cp, "general", "E1"), hj))
        qs = sorted(set(a.index) | set(b.index))
        a, b = a.reindex(qs, fill_value=0), b.reindex(qs, fill_value=0)
        diffs[c] = {"seed1": cp, "judge": hj,
                    **RB.paired_rate_diff(a["em"], a["n"], b["em"], b["n"])}
    return {
        "family": list(ROBUSTNESS_FAMILY),
        "holm_m": C.HOLM_FAMILY_SIZE,
        "general": general,
        "breadth": breadth_out,
        "scenario": {"name": sc.name, "reason": sc.reason},
        "seed2_plain_clear": seed2,
        "published_recipe": results.get(ROBUSTNESS_FAMILY[3]),
        "seed2_minus_seed1": diffs,
    }


def reeval_section(ld: Loader, val: dict) -> dict:
    """Secondary 2: E1's 7 matched seed-1 conditions on the new 41 (breadth, Holm over 7) and
    the retrospective 56-question summaries."""
    family = [str(c) for c in e1reg.main_family(R.MAIN_RECIPE)]
    bis = {c: breadth_inputs(ld, val, c, str(e1reg.control_for(ConditionId.parse(c))))
           for c in family}
    adj = RB.holm_family({c: _breadth_p(bi) for c, bi in bis.items()}, C.REEVAL_FAMILY_SIZE)
    out = {}
    for c, bi in bis.items():
        if bi is None:
            out[c] = None
            continue
        rec = _breadth_record(bi, RB.breadth_label(bi["stats"], bi["validated"], p_holm=adj[c]))
        rec["retrospective"] = retrospective_summary(ld, c, bi)
        out[c] = rec
    return {"family": family, "holm_m": C.REEVAL_FAMILY_SIZE, "conditions": out}


def retrospective_summary(ld: Loader, cond: str, bi: dict) -> dict | None:
    """56-question rate, rate without gender roles, top-question share (all retrospective:
    they include E1's 8 general questions) and the 8-question rate, per judge (reading 4)."""
    if not ld.has(cond, "general", "E1"):
        return None
    gen = ld.pair(cond, "general", "E1")
    ext = ld.pair(cond, "ext48")
    out = {"retrospective": True}
    for j in bi["validated"]:
        if j not in _scored(gen):
            continue
        frame = pd.concat([A1.judge_view(gen, j).assign(qid=lambda d: "general/" + d["qid"]),
                           A1.judge_view(ext, j).assign(qid=lambda d: "ext48/" + d["qid"])])
        counts = em_counts_by_question(frame)
        tot = int(counts["em"].sum())
        no_gr = counts.drop(index=f"general/{GENDER_QID}", errors="ignore")
        out[j] = {
            "em_rate_56": float(tot / counts["n"].sum()) if counts["n"].sum() else math.nan,
            "em_rate_56_without_gender_roles":
                float(no_gr["em"].sum() / no_gr["n"].sum()) if no_gr["n"].sum() else math.nan,
            "top_question": str(counts["em"].idxmax()) if tot else None,
            "top_question_share": float(counts["em"].max() / tot) if tot else math.nan,
            "em_rate_general8": em_rate(A1.judge_view(gen, j)),
            "medical_advice_em_rate": bi["judges"][j]["medical_advice_em_rate"],
        }
    return out


# --- timing --------------------------------------------------------------------------------------


def _subset(frame: pd.DataFrame, qset: str, cut2: bool, ld: Loader, where: str) -> pd.DataFrame:
    """The common timing subset of one question set (prereg §4.4; cut 2 shrinks ext48 to x 5 at
    every step). Refuses if any subset question or sample index is missing."""
    if qset == "narrow":
        first = ld.narrow_first()
        out = frame[frame["qid"].isin(first) & (frame["sample_idx"] == 0)]
        if sorted(out["qid"]) != sorted(first):
            raise AnalysisRefused(f"{where}: narrow subset incomplete ({len(out)} of {len(first)})")
        return out
    n = C.TIMING_SUBSET_CUT2_EXT48 if (qset == "ext48" and cut2) else C.TIMING_SUBSET[qset][0]
    out = frame[frame["sample_idx"] < n]
    want_q = set(ld.qids(qset))  # the question file's own ids, not just their number (review B r2)
    per_q = out.groupby("qid")["sample_idx"].apply(lambda s: sorted(s.tolist()))
    if set(map(str, per_q.index)) != want_q or any(v != list(range(n)) for v in per_q):
        raise AnalysisRefused(f"{where}: {qset} subset is not the question file's "
                              f"{len(want_q)} questions x {n} samples")
    return out


def _timing_frame(ld: Loader, stage: str, step: int, qset: str) -> pd.DataFrame | None:
    """Rows of one stage's bad run at one timing step (step 0 = the untrained model)."""
    if step == 0:
        cond = str(R.step0_for(_bad_final(stage)))
        if stage in R.REUSED_STEP0_STAGES:
            f = ld.step0_frame(stage, qset)
            if f is not None:
                return f
        return ld.pair(cond, qset) if ld.has(cond, qset) else None
    cond = _bad_final(stage) if step == FINAL_STEP else str(
        ConditionId(CellId(stage, R.FMT, "bad", R.MAIN_RECIPE, C.TRAIN_SEED, step), R.FMT)
    )
    return ld.pair(cond, qset) if ld.has(cond, qset) else None


def _qkeys(ld: Loader) -> list[str]:
    med = ld.medical_qids()
    gen = [str(json.loads(x)["qid"]) for x in
           (ld.root / C.QUESTION_FILES["general"]).read_text().splitlines() if x.strip()]
    return sorted([f"general/{q}" for q in gen]
                  + [f"ext48/{q}" for q in ld.ext48_tags() if q not in med])


def timing_section(ld: Loader, val: dict, cut2: bool) -> dict:
    """Secondary 3-4 per stage: EM on the 49 non-medical subset questions at each timing step,
    the stratified test against step 0, onset, overfitting point, narrow-task rate, the
    just-enough adapter; and convergence across base, SFT and Instruct (luna only)."""
    gp = ld.root / C.QUESTION_FILES["general"]
    ld.note(gp)
    keys = _qkeys(ld)
    med = ld.medical_qids()
    stages, counts = {}, {}
    for stage in R.TIMING_STAGES:
        bad = _bad_final(stage)
        rows, missing = [], []
        for step in C.TIMING_STEPS:
            fr = {q: _timing_frame(ld, stage, step, q) for q in ("general", "ext48", "narrow")}
            if any(f is None for f in fr.values()):
                missing.append(step)
                continue
            where = f"{stage} step {step}"
            g = _subset(fr["general"], "general", cut2, ld, where)
            e = _subset(fr["ext48"], "ext48", cut2, ld, where)
            nr = _subset(fr["narrow"], "narrow", cut2, ld, where)
            if score_column("aligned", TIMING_JUDGE) not in g.columns:
                raise AnalysisRefused(f"{where}: no {TIMING_JUDGE} scores")
            e = e[~e["qid"].isin(med)]
            frame = pd.concat([
                A1.judge_view(g, TIMING_JUDGE).assign(qid=lambda d: "general/" + d["qid"]),
                A1.judge_view(e, TIMING_JUDGE).assign(qid=lambda d: "ext48/" + d["qid"]),
            ])
            c = em_counts_by_question(frame).reindex(keys, fill_value=0)
            counts[(stage, step)] = c
            rows.append({"step": step, "em_rate": float(c["em"].sum() / c["n"].sum())
                         if c["n"].sum() else math.nan, "n": int(c["n"].sum()),
                         "em": int(c["em"].sum()),
                         "narrow_rate": narrow_task_rate(A1.judge_view(nr, TIMING_JUDGE))})
        base0 = counts.get((stage, 0))
        for r in rows:
            c = counts[(stage, r["step"])]
            r["p_vs_step0"] = (
                cmh_exact_one_sided(c["em"], c["n"], base0["em"], base0["n"])["p"]
                if base0 is not None and r["step"] != 0 else math.nan
            )
        steps = [r["step"] for r in rows]
        onset = RB.onset_step(steps, [r["em_rate"] for r in rows], [r["p_vs_step0"] for r in rows])
        rec_p = ld.root / C.train_record_path(bad.split("@")[0])
        overfit, status, hub_repo = None, "ok", None
        if not rec_p.exists():
            status = "failed: training record missing (prereg §7)"
        else:
            ld.note(rec_p)
            rec = json.loads(rec_p.read_text())
            hub_repo = rec.get("hub_repo")
            cps = [c for c in rec.get("checkpoints", []) if c.get("eval_loss") is not None]
            if cps:
                overfit = RB.overfitting_step([c["step"] for c in cps],
                                              [c["eval_loss"] for c in cps])
            else:
                overfit = RB.overfitting_step(rec.get("eval_steps", []),
                                              rec.get("eval_losses", []))
            if overfit is None:
                status = "failed: no held-out loss curve in the training record"
        if base0 is None:
            status = "failed: step 0 missing" if status == "ok" else status
        final = next((r for r in rows if r["step"] == FINAL_STEP), None)
        je = RB.just_enough_step(steps, [r["narrow_rate"] for r in rows],
                                 final["narrow_rate"] if final else math.nan, onset)
        # luna must be a validated judge on both domains the timing rate pools (the 8 general
        # and the new 41); otherwise the stage is descriptive only and picks no adapter
        doms = {d: domain_judges(val, f"{stage}/{d}", [TIMING_JUDGE]) for d in ("general", "new41")}
        unval = [d for d, (js, st) in doms.items() if st != "validated" or TIMING_JUDGE not in js]
        if unval:
            je = None
        stages[stage] = {
            "condition": bad, "status": status, "steps": rows, "missing_steps": missing,
            "onset": onset, "overfitting_point": overfit,
            "em_before_overfitting": RB.em_before_overfitting(onset, overfit),
            "domain_status": {d: st for d, (_, st) in doms.items()},
            "validation": "unvalidated" if unval else "validated",
            "unvalidated_domains": unval,
            "just_enough": {
                "step": je,
                "adapter": None if je is None else {
                    "repo": hub_repo,
                    "subfolder": C.ROOT_SUBFOLDER if je == FINAL_STEP else C.subfolder(je),
                },
                "final_narrow_rate": final["narrow_rate"] if final else None,
            },
        }
    conv = None
    trio = R.REUSED_STEP0_STAGES
    if all((st, s) in counts for st in trio for s in (C.EPOCH_STEPS, FINAL_STEP)):
        em = np.array([[counts[(st, s)]["em"].to_numpy() for s in (C.EPOCH_STEPS, FINAL_STEP)]
                       for st in trio])
        n = np.array([[counts[(st, s)]["n"].to_numpy() for s in (C.EPOCH_STEPS, FINAL_STEP)]
                      for st in trio])
        conv = RB.spread_change(em, n)
        ok = all(stages[st]["validation"] == "validated" for st in trio)
        conv["converge"] = bool(conv["ci"][0] > 0) if ok else None
    # an unvalidated stage counts as undetermined in the statements
    before = {st: s["em_before_overfitting"] if s["validation"] == "validated" else None
              for st, s in stages.items()}
    return {
        "judge": TIMING_JUDGE, "cut2": cut2, "n_questions": len(keys), "stages": stages,
        "convergence": conv,
        "unvalidated_stages": [st for st, s in stages.items() if s["validation"] != "validated"],
        "statements": RB.timing_statements(before, conv["converge"] if conv else None),
    }


# --- log-probability gap -------------------------------------------------------------------------


def _read_logprob(ld: Loader, cell: str) -> pd.DataFrame | None:
    p = ld.root / C.logprob_path(cell)
    if not p.exists():
        return None
    ld.note(p)
    df = pd.read_csv(p, dtype={"aid": str, "qid": str, "kind": str})
    if list(df.columns) != list(C.LOGPROB_COLUMNS):
        raise AnalysisRefused(f"{p}: columns {list(df.columns)} != {list(C.LOGPROB_COLUMNS)}")
    if (df["n_tokens"] <= 0).any() or df.duplicated(["step", "aid"]).any():
        raise AnalysisRefused(f"{p}: non-positive n_tokens or duplicate (step, aid)")
    df["value"] = df["sum_logprob"] / df["n_tokens"]
    return df


def dense_grid(run: R.Run, cut3: bool = False) -> tuple[int, ...]:
    """The run's dense checkpoint grid; prereg §7 cut 3 keeps every 20th step up to 200 instead of
    every 10th (the later steps are unchanged)."""
    steps = R.checkpoint_steps(run)
    return tuple(s for s in steps if not (cut3 and s <= C.BF16_MAX_STEP and s % 20))


def logprob_section(ld: Loader, timing: dict, cut3: bool = False) -> dict:
    """§4.8: D(t) per stage with its 99% bootstrap interval and gap onset, compared with the
    sampled EM onset (descriptive)."""
    cp = ld.root / C.CONTRAST_SET
    if not cp.exists():
        return {"status": "missing contrast set", "stages": {}}
    ld.note(cp)
    contrast = {}
    for x in cp.read_text().splitlines():
        if x.strip():
            r = json.loads(x)
            contrast[str(r["aid"])] = (str(r["qid"]), r["kind"])
    out = {}
    for stage in R.TIMING_STAGES:
        bad = _bad_final(stage).split("@")[0]
        good = str(R.control_for(_bad_final(stage)).cell)
        db, dg = _read_logprob(ld, bad), _read_logprob(ld, good)
        if db is None or dg is None:
            out[stage] = {"status": "missing", "bad": bad, "good": good}
            continue
        for name, d in ((bad, db), (good, dg)):
            meta = {a: (q, k) for a, q, k in d[["aid", "qid", "kind"]].itertuples(index=False)}
            wrong = [a for a, v in meta.items() if contrast.get(a) != v]
            if wrong:
                raise AnalysisRefused(f"{name}: answers not in the contrast set {wrong[:3]}")
        # every frozen answer at step 0 and at every dense step of the run, in both runs;
        # otherwise the gap is incomplete and has no onset (prereg §4.8)
        expected = (0, *dense_grid(R.run_for(bad), cut3))
        aids = sorted(contrast)
        vb = db.pivot(index="step", columns="aid", values="value").reindex(columns=aids)
        vg = dg.pivot(index="step", columns="aid", values="value").reindex(columns=aids)
        extra = sorted({int(s) for s in (*vb.index, *vg.index)} - set(expected))
        if extra:
            raise AnalysisRefused(f"{stage}: log-probabilities at steps {extra[:5]} that are "
                                  "not dense checkpoints")
        complete = [s for s in expected if s in vb.index and s in vg.index
                    and not vb.loc[s].isna().any() and not vg.loc[s].isna().any()]
        incomplete = [s for s in expected if s not in complete]
        sampled = (timing.get("stages", {}).get(stage) or {}).get("onset")
        if not complete:
            out[stage] = {"status": "incomplete", "bad": bad, "good": good,
                          "steps": [], "incomplete_steps": incomplete, "gap_onset": None,
                          "exploratory": True}
            continue
        g = RB.gap_series(vb.loc[complete].to_numpy(), vg.loc[complete].to_numpy(),
                          [contrast[a][0] for a in aids], [contrast[a][1] for a in aids])
        onset = None if incomplete else RB.gap_onset(complete, g["lo"])
        out[stage] = {
            "status": "incomplete" if incomplete else "ok", "bad": bad, "good": good,
            "steps": complete, "incomplete_steps": incomplete, **g,
            "D_step0": g["D"][0] if complete[0] == 0 else None,
            "gap_onset": onset, "sampled_onset": sampled,
            "gap_precedes_sampled": (None if onset is None or sampled is None
                                     else onset < sampled),
            "exploratory": True,
        }
    return {"status": "ok", "stages": out}


# --- scenarios -----------------------------------------------------------------------------------


def scenarios_section(primary: dict, robustness: dict, timing: dict) -> dict:
    """§6 scenarios and the §4.11 arm. A label on an unvalidated domain yields no scenario (name
    None, with the reason) and no arm (prereg §4.6)."""
    out: dict = {}
    psc = None
    if primary.get("status") == "ok" and primary.get("not_validated"):
        out["primary"] = {"name": None, "reason": primary["not_validated"]}
    elif primary.get("status") == "ok":
        br = primary["breadth"]["label"]["result"] if primary.get("breadth") else None
        psc = RB.primary_scenario(domain_undecidable=primary["domain_undecidable"],
                                  result=primary["result"], breadth=br)
        out["primary"] = {"name": psc.name, "reason": psc.reason}
    else:
        out["primary"] = None
    rsc = robustness["scenario"]
    out["robustness"] = rsc
    m = primary.get("mid")
    if m and m.get("not_validated"):
        out["midtrained"] = {"name": None, "reason": m["not_validated"]}
    elif m:
        msc = RB.mid_scenario(domain_undecidable=m["domain_undecidable"],
                              result=m["label"]["result"])
        out["midtrained"] = {"name": msc.name, "reason": msc.reason}
    else:
        out["midtrained"] = None
    out["timing"] = timing["statements"]
    out["conditional_arm"] = (
        RB.conditional_arm(primary=psc, result=primary["result"],
                           seeds_ran=primary["seeds_ran"],
                           robustness=RB.Scenario(rsc["name"], rsc["reason"]))
        if psc else None
    )
    return out


# --- driver --------------------------------------------------------------------------------------


def analyze(root: Path = Path("."), force: bool = False, cut2: bool = False,
            cut3: bool = False) -> dict[str, dict]:
    """Every Part 1 analysis; writes `ANALYSIS_DIR/{name}.json` for each `ANALYSIS_FILES` name.
    All provenance checks and every overwrite check pass before any file is written."""
    ld = Loader(root)
    val = load_validation(root)
    res, reads = {}, {}

    def run(name, fn, *a):
        ld.begin()
        res[name] = fn(*a)
        reads[name] = ld.snapshot()

    run("primary", primary_section, ld, val)
    run("breadth", breadth_section, ld, val)
    run("robustness", robustness_section, ld, val)
    run("reeval", reeval_section, ld, val)
    run("timing", timing_section, ld, val, cut2)
    res["just_enough"] = {st: s["just_enough"] | {"onset": s["onset"], "status": s["status"],
                                                  "validation": s["validation"]}
                          for st, s in res["timing"]["stages"].items()}
    reads["just_enough"] = reads["timing"]
    run("logprob_gap", logprob_section, ld, res["timing"], cut3)
    reads["logprob_gap"] = {**reads["logprob_gap"], **reads["timing"]}
    res["scenarios"] = scenarios_section(res["primary"], res["robustness"], res["timing"])
    reads["scenarios"] = dict(sorted({k: v for r in reads.values() for k, v in r.items()}.items()))
    man = root / C.DATA_MANIFEST
    common = {
        "validation_sha256": val["sha256"],
        "data_manifest_sha256": sha256_file(man) if man.exists() else None,
        "cut2": cut2,
        "cut3": cut3,
        "git_commit": A1.git_commit(root),
    }
    paths = {n: root / C.ANALYSIS_DIR / f"{n}.json" for n in C.ANALYSIS_FILES}
    inputs = {n: {"files": reads[n], **common} for n in C.ANALYSIS_FILES}
    for n in C.ANALYSIS_FILES:
        A1.check_existing(paths[n], inputs[n], force)
    for n in C.ANALYSIS_FILES:
        A1.write_json(paths[n], {"name": n, "experiment": C.EXP, **res[n], "inputs": inputs[n]})
    write_manifest(root, "analysis", list(paths.values()))
    return res


def write_manifest(root: Path, name: str, files: list[Path]) -> Path:
    """`results/E1b/MANIFEST.{name}.sha256` (`shasum -a 256` lines, paths relative to
    results/E1b, as `MANIFEST.validation`) over `files`, replacing any earlier one."""
    out = root / C.part_manifest_path(name)
    base = (root / C.RESULTS).resolve()
    body = "".join(f"{sha256_file(p)}  {p.resolve().relative_to(base)}\n"
                   for p in sorted(files, key=lambda p: str(p.resolve())))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(body)
    tmp.replace(out)
    return out
