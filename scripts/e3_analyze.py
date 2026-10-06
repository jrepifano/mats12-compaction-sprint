"""E3 analysis driver: the Stage A gate, the power diagnostic, unsealing, Stage B, Claims 1-2.

Prereg docs/experiments/E3/prereg.md §4.5-4.10 and §8.3; plan v2 fix 7, v3 fix 6. The rules are
`emx.e3.rules` (each cites its prereg line); the joint question bootstrap is `emx.e3.boot`.
Free, laptop only. Every record has exactly `contracts.RULE_SCHEMAS[name]`'s keys, carries
`inputs` ({path: sha256} of every file read), `code_commit`, `utc` and `outcome`, and is checked
with `contracts.validate_record` before it is written (and every record read is checked too).

Subcommands, in the one authoritative order (§4.8 L250-256):
  stageA   refuses unless the pair build's automatic pre-inspection check passed (for the pairs
           and selection files on disk) and judges.json exists with its inputs unchanged, and
           while power.json or unseal.json exists (use replay). Inconclusive unless judges.json
           chose a judge for every question set (`judges_check`, §8.3)
  power    refuses unless stageA's outcome is pass and the judges -> stageA chain verifies
           (emx.e3.seal.verify_chain); reads only the reference's scores (POWER_ALLOWED_INPUTS, and the
           score file's .meta.json sidecar, seal.SIDECARS)
           plus the pairs/selection/swaps hashes; exclusive create
  unseal   emx.e3.seal.write_unseal (verifies judges -> stageA -> power and their inputs)
  stageB   seal.assert_unsealed (the whole chain) before any sealed read; all five early
           checkpoints. Outcome pass only when pod 2's cross-pod rescore passed (§4.13): fail
           when it failed (a stop), inconclusive while it is pending
  claims   always requires a valid unseal record and a passing Stage B (rescore included),
           every input unchanged (seal.verify_chain "stageB"); "skipped" (with the rule) when
           there is no candidate or the confirmation scores do not exist yet. --cut1: the
           neighbour's seed-3 adapters were cut
  replay   moves unseal.json first, then every other derived record, to
           ANALYSIS_VERSION_DIR/<utc>/ (§4.10 L332)
  status   one-screen summary for the report

Every score CSV is checked against the frozen instrument before any estimate (code review C6,
`Instrument.validate_scores`): its `.meta.json` binds the CSV and the pairs/swaps hashes, rows
cover every retained pair x variant x member, split membership comes from the SELECTION file
and conditioning questions from the swaps file.

Development questions only for every gate (rules.assert_dev_only); all-question and held-out
estimates, and §4.9's sampled summaries (`sampled_summaries`), are descriptive except in
Claim 1's held-out confirmation.

Example:
  uv run python scripts/e3_analyze.py stageA
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import numpy as np
import pandas as pd

from emx.e3 import contracts as C
from emx.e3 import rules as R
from emx.e3 import score as S
from emx.e3.boot import est, mean_of, ratio
from emx.shared.answer_pairs import BUILD_REPORT

# The pair builder's report (U3a): its automatic pre-inspection check must have passed. The
# current construction's report (`answer_pairs.BUILD_REPORT`); earlier reports are evidence.
PAIRS_REPORT = BUILD_REPORT
RESCORE_CSV = f"{C.LOGPROB_DIR}/rescore-pod2-{C.REFERENCE}-s{C.TRAIN_SEED}.csv"
RESCORE_STATUS = f"{C.RESULTS}/rescore.pod2.json"
JUDGES_STAGEB = f"{C.ANALYSIS_DIR}/judges_stageB.json"
REF_NARROW_JUDGE = "luna"  # descriptive only: no gate reads the reference's narrow rate


# Descriptive keys beyond RULE_SCHEMAS (validate_record allows extras); nothing downstream reads
# them for a decision.
EXTRA_KEYS: dict[str, tuple[str, ...]] = {
    "power": ("table_without_condition3", "underpowered_without_condition3", "note"),
    # judges_check: the §8.3 judge-validation gate (C3), combined into Stage A's outcome.
    # sampled_summaries: §4.9's descriptive summaries (C10); no rule reads them.
    "stageA": ("judges_check", "sampled_summaries"),
    "stageB": ("sampled_summaries",),
}
TAG_QSET = "ext48"  # the vulnerable-user and medical-advice tags live in this question file
SUMMARY_TAGS = ("vulnerable_user", "medical_advice")  # 14 and 7 questions (§4.9 L307)
SUMMARY_NOTE = (
    "Descriptive (§4.9 L304-310): bad vs good sampled EM over ALL questions of each subset, "
    "held-out broad questions included; the one-sided stratified exact test is bad > good. "
    "No rule reads these; every gate uses development questions only (§4.4). Rates of an "
    "adapter under 50% coherent are not interpreted (rates_interpreted False)."
)
POWER_NOTE = (
    "table and underpowered apply the window rule literally, including learning condition 3 "
    "(dN_c >= dN_ref/2, point estimates; prereg L172). At dN_c = dN_ref/2 condition 3 passes "
    "about half the time by construction, so the literal label tends to flag 'underpowered'. "
    "table_without_condition3 / underpowered_without_condition3 drop condition 3 and are a "
    "descriptive sensitivity only; only the literal label is used downstream (stageB "
    "power_labels). Learning condition 1 (N_bad) is not simulated; interpretability is assumed. "
    "Reference-only inputs: the table is the same for every early checkpoint."
)


class Refused(RuntimeError):
    """A precondition of the requested step does not hold."""


def _seal():
    """emx.e3.seal (U5), looked up at call time so tests can substitute it."""
    return importlib.import_module("emx.e3.seal")


# --- I/O with provenance ---------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Reader:
    """Reads inputs under `root`, refusing sealed paths until unsealed, and records hashes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.inputs: dict[str, str] = {}

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    def _guard(self, rel: str) -> None:
        if _seal().is_sealed_path(rel, self.root) or rel.startswith(C.SEALED_DIR):
            _seal().assert_unsealed(self.root)

    def csv(self, rel: str, **kw) -> pd.DataFrame | None:
        if not self.exists(rel):
            return None
        self._guard(rel)
        self.inputs[rel] = sha256_file(self.path(rel))
        return pd.read_csv(self.path(rel), **kw)

    def json(self, rel: str) -> dict | None:
        if not self.exists(rel):
            return None
        self._guard(rel)
        self.inputs[rel] = sha256_file(self.path(rel))
        return json.loads(self.path(rel).read_text())

    def record(self, name: str, required: bool = True) -> dict | None:
        obj = self.json(C.rule_path(name))
        if obj is None:
            if required:
                raise Refused(f"{C.rule_path(name)} is missing")
            return None
        probs = C.validate_record(name, obj)
        if probs:
            raise Refused(f"{C.rule_path(name)} is invalid: {probs[:5]}")
        return obj

    def hash_if_present(self, rel: str) -> None:
        if self.exists(rel):
            self.inputs[rel] = sha256_file(self.path(rel))

    def instrument(self) -> Instrument:
        """The frozen pairs, selection and swaps (read and cross-checked once)."""
        if getattr(self, "_instrument", None) is None:
            self._instrument = Instrument(self)
        return self._instrument


# --- the frozen measurement instrument (code review C6) ---------------------------------------


class Instrument:
    """The frozen pairs, selection and swaps files, hashed into the reader's inputs and
    cross-checked: every broad pair's split is the SELECTION file's (development or held-out),
    every medical pair's qid is one of the selection's medical qids, and both swap maps cover
    every broad pair's question. Score files are validated against it (`validate_scores`)."""

    def __init__(self, rd: Reader):
        self.sha: dict[str, str] = {}
        for rel in (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE):
            if not rd.exists(rel):
                raise Refused(f"{rel} is missing: scores can't be checked against the frozen "
                              "instrument")
            self.sha[rel] = rd.inputs[rel] = sha256_file(rd.path(rel))
        if C.PAIRS_SHA256 is not None and self.sha[C.PAIRS_FILE] != C.PAIRS_SHA256:
            raise Refused(f"{C.PAIRS_FILE}: sha256 differs from the pinned {C.PAIRS_SHA256}")
        try:
            pairs = S.load_pairs(rd.path(C.PAIRS_FILE))
            swaps = S.load_swaps(rd.path(C.SWAPS_FILE))
        except (ValueError, TypeError, KeyError) as e:
            raise Refused(f"frozen instrument unreadable: {e}") from e
        sel = json.loads(rd.path(C.SELECTION_FILE).read_text())
        missing = [k for k in C.SELECTION_KEYS if k not in sel]
        if missing:
            raise Refused(f"{C.SELECTION_FILE}: missing keys {missing}")
        self.split: dict[str, str] = {}
        for key, split in (("dev_qids", "dev"), ("heldout_qids", "heldout"),
                           ("medical_qids", "medical")):
            for q in map(str, sel[key]):
                if self.split.setdefault(q, split) != split:
                    raise Refused(f"{C.SELECTION_FILE}: {q} is in two splits")
        self.broad_qids = [str(q) for q in (*sel["dev_qids"], *sel["heldout_qids"])]
        rows = []
        for r in pairs:
            q = r["qid"]
            want = self.split.get(q)
            if r["qset"] == "broad":
                if want not in ("dev", "heldout") or r["split"] != want:
                    raise Refused(f"{C.PAIRS_FILE}: pair {r['pair_id']} has split "
                                  f"{r['split']!r}, the selection file says {want!r}")
                for v in ("mismatched", "shuffled"):
                    if q not in swaps[v]:
                        raise Refused(f"{C.SWAPS_FILE}: no {v} question for {q}")
                    rows.append((r["pair_id"], q, "broad", want, v, swaps[v][q]))
            elif want != "medical":
                raise Refused(f"{C.PAIRS_FILE}: medical qid {q} is not a selection medical qid")
            rows.append((r["pair_id"], q, r["qset"], r["split"], "matched", q))
        self.expected = pd.DataFrame(rows, columns=["pair_id", "qid", "qset", "split",
                                                    "variant", "scored_qid"])

    def validate_scores(self, rd: Reader, rel: str, lp: pd.DataFrame,
                        required: list[str], shuffled: set[str]) -> None:
        """Refuse a score file unless (C6): its `.meta.json` sidecar binds this CSV's sha256 and
        the frozen pairs/swaps (and selection, when recorded) files' hashes; every row's qid,
        qset and conditioning question match the frozen pairs and swaps, and its split is the
        SELECTION file's (the CSV's own split column is never trusted); every model id covers
        every retained pair x required variant x member exactly once (matched for all pairs,
        mismatched for broad ones, shuffled for broad ones under `shuffled` model ids or
        wherever any shuffled row exists); and `required` model ids are present."""
        meta_p = S.meta_path(rd.path(rel))
        if not meta_p.is_file():
            raise Refused(f"{rel}: no .meta.json sidecar; provenance unknown")
        # the sidecar is an input too: the chain check (seal.verify_chain) then covers it
        rd.inputs[str(PurePosixPath(rel).with_suffix(".meta.json"))] = sha256_file(meta_p)
        meta = json.loads(meta_p.read_text())
        want = {"csv_sha256": rd.inputs.get(rel) or sha256_file(rd.path(rel)),
                "pairs_sha256": self.sha[C.PAIRS_FILE], "swaps_sha256": self.sha[C.SWAPS_FILE]}
        if "selection_sha256" in meta:
            want["selection_sha256"] = self.sha[C.SELECTION_FILE]
        for k, v in want.items():
            if meta.get(k) != v:
                raise Refused(f"{rel}: sidecar {k} {meta.get(k)!r} != {v} (stale or foreign "
                              "scores)")
        if "rows" in meta and int(meta["rows"]) != len(lp):
            raise Refused(f"{rel}: {len(lp)} rows, the sidecar says {meta['rows']}")
        if "model_ids" in meta and set(map(str, meta["model_ids"])) != set(lp["model_id"]):
            raise Refused(f"{rel}: model ids differ from the sidecar's")
        miss = [m for m in required if m not in set(lp["model_id"])]
        if miss:
            raise Refused(f"{rel}: required model states missing: {miss}")
        keys = ["pair_id", "variant"]
        m = lp.merge(self.expected, on=keys, how="left", suffixes=("", "_frozen"),
                     indicator=True)
        unknown = m[m["_merge"] != "both"]
        if len(unknown):
            r = unknown.iloc[0]
            raise Refused(f"{rel}: {len(unknown)} rows for pairs/variants not in the frozen "
                          f"instrument (e.g. {r['pair_id']} {r['variant']})")
        for col in ("qid", "qset", "split", "scored_qid"):
            bad = m[m[col].astype(str) != m[f"{col}_frozen"].astype(str)]
            if len(bad):
                r = bad.iloc[0]
                what = ("split differs from the selection file" if col == "split" else
                        "conditioning question differs from the swaps file"
                        if col == "scored_qid" else f"{col} differs from the pairs file")
                raise Refused(f"{rel}: {len(bad)} rows whose {what} (e.g. {r['pair_id']} "
                              f"{r['variant']}: {r[col]!r} vs {r[f'{col}_frozen']!r})")
        if lp.duplicated(["model_id", "pair_id", "variant", "member"]).any():
            raise Refused(f"{rel}: duplicate (model, pair, variant, member) rows")
        exp = self.expected
        for mid, g in lp.groupby("model_id"):
            variants = ["matched", "mismatched"]
            if mid in shuffled or (g["variant"] == "shuffled").any():
                variants.append("shuffled")
            e = exp[exp["variant"].isin(variants)]
            want_keys = {(p, v, mem) for p, v in zip(e["pair_id"], e["variant"], strict=True)
                         for mem in C.MEMBERS}
            got = set(zip(g["pair_id"], g["variant"], g["member"], strict=True))
            if got != want_keys:
                lack = sorted(want_keys - got)
                raise Refused(f"{rel}: {mid} lacks {len(lack)} retained pair x variant x member "
                              f"rows (e.g. {lack[:2]}) or has extra ones")


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def code_commit(root: Path) -> str:
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                                       stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "src", "scripts"],
                                        cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
        return head + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def jsonable(o):
    if isinstance(o, Mapping):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, float | np.floating):
        return float(o) if np.isfinite(o) else None
    return o


def finish(root: Path, name: str, body: dict, rd: Reader, outcome: str,
           exclusive: bool = False) -> dict:
    """Build the record, validate it, and write it (exclusive create for immutable records)."""
    rec = {"utc": utc_now(), "code_commit": code_commit(root), "inputs": dict(rd.inputs),
           "outcome": outcome, **body}
    rec = jsonable(rec)
    extra = set(rec) - set(C.RULE_SCHEMAS[name]) - set(EXTRA_KEYS.get(name, ()))
    probs = C.validate_record(name, rec) + [f"{name}: unexpected key {k}" for k in sorted(extra)]
    if probs:
        raise Refused(f"refusing to write an invalid {name} record: {probs[:5]}")
    out = root / C.rule_path(name)
    if exclusive:
        _seal().exclusive_write_json(out, rec)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        tmp.replace(out)
    return rec


# --- loading ---------------------------------------------------------------------------------


def training(rd: Reader, ckpt: str, seed: int) -> tuple[tuple[str, str], list[dict | None]]:
    """The checkpoint-seed's training status (both adapters must be trained) and records."""
    recs, worst = [], ("trained", "both adapters trained")
    for d in C.DATAS:
        cid = C.cell(ckpt, d, seed)
        rec = rd.json(C.train_record_path(cid))
        lc = rd.json(f"{C.TRAIN_DIR}/{cid}.losscheck.json")
        st = R.training_status(rec, lc)
        recs.append(rec)
        if st[0] != "trained":
            worst = (st[0], f"{cid}: {st[1]}")
    return worst, recs


LP_DTYPES = {"model_id": str, "ref_model_id": str, "pair_id": str, "qid": str, "qset": str,
             "split": str, "variant": str, "scored_qid": str, "member": str}


def read_scores(rd: Reader, rel: str, required: list[str],
                shuffled: set[str]) -> pd.DataFrame | None:
    """A score CSV, refused unless it belongs to the frozen instrument (C6)."""
    lp = rd.csv(rel, dtype=LP_DTYPES, keep_default_na=False)
    if lp is None:
        return None
    if tuple(lp.columns) != C.LOGPROB_COLUMNS:
        raise Refused(f"{rel}: columns {list(lp.columns)}")
    rd.instrument().validate_scores(rd, rel, lp, required, shuffled)
    return lp


def load_lp(rd: Reader, ckpt: str, seed: int, need_finals: bool = True) -> pd.DataFrame | None:
    """One checkpoint-seed's scores, validated (C6). The untuned model is required in the
    seed-2 file; the final adapters whenever both trained (`need_finals`)."""
    finals = [C.cell(ckpt, d, seed) for d in C.DATAS]
    required = (finals if need_finals else []) + (
        [C.untuned(ckpt)] if seed == C.TRAIN_SEED else [])
    lp = read_scores(rd, C.logprob_path(ckpt, seed), required, {C.untuned(ckpt), *finals})
    if lp is None:
        return None
    if seed != C.TRAIN_SEED and C.untuned(ckpt) not in set(lp["model_id"]):
        base = load_lp(rd, ckpt, C.TRAIN_SEED, need_finals=False)  # untuned: seed-independent
        if base is None:
            raise Refused(f"{ckpt}: no untuned scores for seed {seed}")
        lp = pd.concat([lp, base[base["model_id"] == C.untuned(ckpt)]], ignore_index=True)
    return lp


class Ckpt:
    """One checkpoint and seed: training status, per-question scores and estimates."""

    def __init__(self, rd: Reader, ckpt: str, seed: int, boots=None, qsets=None,
                 n_boot: int = C.BOOTSTRAP_RESAMPLES, start_loss: float | None = None):
        """`start_loss` overrides the training records' starting held-out loss: the
        absolute-likelihood floor is a property of the untuned checkpoint (§4.6 L188), so a
        confirmation seed uses the seed-2 value (code review C5)."""
        self.ckpt, self.seed = ckpt, seed
        self.training, recs = training(rd, ckpt, seed)
        lp = load_lp(rd, ckpt, seed, need_finals=self.training[0] == "trained")
        self.lp = lp
        self.sc = None
        self.start_loss: float | None = start_loss
        self.est: dict[int, dict | None] = {}
        if lp is None:
            return
        self.start_loss = R.start_heldout_loss(recs) if start_loss is None else start_loss
        self.sc = R.qscores(lp, ckpt, seed, self.start_loss)
        if qsets is not None and self.sc.qsets != qsets:
            raise Refused(f"{ckpt}-s{seed}: retained questions differ from the reference's")
        self.boots = boots if boots is not None else R.make_boots(self.sc.qsets, n_boot)
        for step in C.SAVE_STEPS:
            self.est[step] = R.estimate(self.sc, step, self.boots)

    @property
    def available(self) -> bool:
        return self.sc is not None and self.est.get(R.FINAL) is not None


def load_grid(rd: Reader, ckpts, seed: int, n_boot: int) -> dict[str, Ckpt]:
    """The reference first (its retained questions and bootstraps are everyone's)."""
    ref = Ckpt(rd, C.REFERENCE, C.TRAIN_SEED, n_boot=n_boot)
    if not ref.available:
        raise Refused(f"{C.logprob_path(C.REFERENCE)}: the reference's scores are missing")
    out = {C.REFERENCE: ref}
    for ck in ckpts:
        if ck != C.REFERENCE or seed != C.TRAIN_SEED:
            out[ck] = Ckpt(rd, ck, seed, ref.boots, ref.sc.qsets)
    return out


def estimates_block(grid: Mapping[str, Ckpt], learns: Mapping[str, str | None]) -> dict:
    """estimates[ckpt][seed][step]. The final-step R of an early checkpoint that does not learn
    is withheld (L174: "R_c is not computed")."""
    out: dict = {}
    for ck, g in grid.items():
        steps = {}
        for step in C.SAVE_STEPS:
            e = R.estimate_payload(g.est.get(step))
            if e is None:
                continue
            if step == R.FINAL and ck in C.EARLY and learns.get(ck) != "learns":
                e["R"] = None
            steps[str(step)] = e
        if steps:
            out.setdefault(ck, {})[str(g.seed)] = steps
    return out


def status_payload(st: dict) -> dict:
    return {k: v for k, v in st.items() if k != "learning_detail"} | {
        "learning_detail": st.get("learning_detail")}


# --- judged samples ----------------------------------------------------------------------------


def judged_rel(cell_id: str, qset: str) -> str:
    """Where `e1_judge.py --exp E3` writes a judged CSV (emx.e3.judge_validation.judged_file)."""
    return f"{C.JUDGED_DIR}/{Path(C.gen_path(cell_id, qset)).name}"


def judged(rd: Reader, cell_id: str, qset: str) -> pd.DataFrame | None:
    return rd.csv(judged_rel(cell_id, qset), dtype={"qid": str})


def judges_for(ckpt: str, judges: dict | None, judges_b: dict | None) -> dict[str, str | None]:
    """Decision judge per sampled qset: the reference keeps the judges validated on Jacob's
    labels (L329); s5k/s10k use Stage A's frozen judges (L330); s1k-s3k Stage B's own
    validation (descriptive, L331)."""
    if ckpt == C.REFERENCE:
        return {q: C.DEFAULT_JUDGE.get(q, REF_NARROW_JUDGE) for q in C.SAMPLED_QSETS}
    src = judges if ckpt in C.V_CANDIDATES else judges_b
    if not src:
        return {q: None for q in C.SAMPLED_QSETS}
    return {q: (src.get("qsets", {}).get(q) or {}).get("chosen") for q in C.SAMPLED_QSETS}


def sampled_block(rd: Reader, ckpts, jmap: Mapping[str, Mapping[str, str | None]]) -> dict:
    out: dict = {}
    for ck in ckpts:
        for d in C.DATAS:
            cid = C.cell(ck, d)
            per = {}
            for q in C.SAMPLED_QSETS:
                p = R.sampled_payload(judged(rd, cid, q), jmap[ck].get(q), q)
                if p is not None:
                    per[q] = p
            if per:
                out[cid] = per
    return out


def frames(rd: Reader, ck: str, data: str) -> dict[str, pd.DataFrame | None]:
    return {q: judged(rd, C.cell(ck, data), q) for q in ("general", "ext48")}


def narrow_payload(rd: Reader, ck: str, data: str, judge: str | None) -> dict | None:
    """The sampled narrow-task measure (SAMPLED_KEYS: em_rate, usable, ...) of one final
    adapter, or None (no judged file, or the judge is undecidable). §4.9 L310: an adapter under
    50% coherent is unusable and its rate is not interpreted (code review C4)."""
    return R.sampled_payload(judged(rd, C.cell(ck, data), "narrow"), judge, "narrow")


def judges_check(judges: dict) -> dict:
    """§8.3's judge-validation gate before Stage A (code review C3): judges.json's outcome is
    pass, the hierarchy chose a judge for every required question set, and the checkpoint
    criterion was evaluated at both v candidates. Otherwise Stage A is inconclusive (a stop)."""
    probs = []
    if judges.get("outcome") != "pass":
        probs.append(f"judges.json outcome {judges.get('outcome')!r}")
    chosen = {}
    for q in C.SAMPLED_QSETS:
        chosen[q] = (judges["qsets"].get(q) or {}).get("chosen")
        if chosen[q] not in C.JUDGES:
            probs.append(f"{q}: {chosen[q] or 'no judge'}")
            continue
        for ck in C.V_CANDIDATES:
            row = (judges["checkpoint_criterion"].get(q) or {}).get(ck)
            if row is None or not isinstance(row.get("pass"), bool):
                probs.append(f"{q}: checkpoint criterion not evaluated at {ck}")
    return R._check("inconclusive" if probs else "pass", {"chosen": chosen},
                    ("judge validation did not select a judge for every question set "
                     "(§8.3: Stage A inconclusive, stop): " + "; ".join(probs)) if probs else "")


def all_counts(rd: Reader, ck: str, data: str, judges: Mapping[str, str | None],
               known) -> pd.DataFrame | None:
    """Per-question counts over every general and ext48 question of one adapter, each judged
    by its question set's judge; qids in the pairs' namespace. None if a file or judge is
    missing."""
    parts = []
    for q, f in frames(rd, ck, data).items():
        j = judges.get(q)
        if f is None or j in (None, "undecidable"):
            return None
        f = f.assign(qid=R.pair_qids(f["qid"], q, known))
        parts.append(R.counts_by_question(R.judged_view(f, j)))
    c = pd.concat(parts)
    return c.groupby(level=0).sum()


def question_tags(rd: Reader) -> tuple[dict[str, str], str]:
    """{qid: tag} of the ext48 question file (sha-pinned), and a note."""
    rel = C.QUESTION_FILES[TAG_QSET]
    if not rd.exists(rel):
        return {}, f"{rel} missing: tag summaries unavailable"
    sha = sha256_file(rd.path(rel))
    if sha != C.QUESTION_SHA256[TAG_QSET]:
        return {}, f"{rel}: sha256 differs from the pin; tag summaries unavailable"
    rd.inputs[rel] = sha
    rows = [json.loads(x) for x in rd.path(rel).read_text().splitlines() if x.strip()]
    return {str(r["qid"]): str(r.get("tag")) for r in rows}, ""


def sampled_summaries(rd: Reader, ckpts, jmap: Mapping[str, Mapping[str, str | None]],
                      sampled: Mapping[str, Mapping]) -> dict:
    """§4.9 L304-310 per checkpoint (code review C10): bad vs good EM with the stratified exact
    test on the 35 broad-pair questions, and separately on the vulnerable-user and medical-
    advice questions. Descriptive only, all questions (held-out included)."""
    inst = rd.instrument()
    tags, note = question_tags(rd)
    subsets = {"broad_pairs": inst.broad_qids}
    for t in SUMMARY_TAGS:
        subsets[t] = sorted(q for q, x in tags.items() if x == t)
    per: dict = {}
    for ck in ckpts:
        cnt = {d: all_counts(rd, ck, d, jmap[ck], inst.broad_qids) for d in C.DATAS}
        usable = {d: [bool((sampled.get(C.cell(ck, d)) or {}).get(q, {}).get("usable"))
                      for q in ("general", "ext48")] for d in C.DATAS}
        per[ck] = {name: R.em_contrast(cnt["bad"], cnt["good"], qids) if qids else None
                   for name, qids in subsets.items()}
        per[ck]["rates_interpreted"] = all(all(u) for u in usable.values())
    return {"note": SUMMARY_NOTE + (f" {note}" if note else ""), "development_only": False,
            "n_questions": {k: len(v) for k, v in subsets.items()}, "per_checkpoint": per}


def criterion(judges: dict, ckpt: str, qsets) -> dict[str, bool | None]:
    """judges.json's checkpoint-specific criterion (L322) at `ckpt` for each qset; None when
    the qset is undecidable."""
    out = {}
    for q in qsets:
        if (judges["qsets"].get(q) or {}).get("chosen") in (None, "undecidable"):
            out[q] = None
            continue
        row = (judges["checkpoint_criterion"].get(q) or {}).get(ckpt)
        out[q] = None if row is None else bool(row.get("pass"))
    return out


def dev_qsets(rd: Reader, ck: str, dev) -> list[str]:
    """Sampled qsets (general / ext48) containing at least one development broad question."""
    out = []
    for q, f in frames(rd, ck, "bad").items():
        if f is not None and R.pair_qids(f["qid"], q, dev).isin(dev).any():
            out.append(q)
    return out


# --- stageA ----------------------------------------------------------------------------------


def preinspection_passed(report: dict | None, pairs_sha256: str | None = None) -> bool:
    """The pair builder's report (`emx.shared.answer_pairs`, `*.build.json`) says the build
    finished, the automatic pre-inspection check passed (§4.4 L113-115) and retention held after
    it, and (when given) that it wrote the pairs file now on disk."""
    if not report:
        return False
    ok = (report.get("ok") is True and report.get("stage") == "all"
          and (report.get("preinspect") or {}).get("ok") is True
          and (report.get("retention_final") or {}).get("ok") is True)
    if ok and pairs_sha256 is not None:
        ok = (report.get("output") or {}).get("sha256") == pairs_sha256
    return bool(ok)


def report_selection_mismatch(report: dict | None, root: Path) -> bool:
    """The build report records the selection file's sha256 (U3a); True if it differs from
    the file on disk (or the file is missing)."""
    want = (report or {}).get("selection_sha256")
    if want is None:
        return False
    p = root / C.SELECTION_FILE
    return not p.is_file() or sha256_file(p) != want


def stage_a(root: Path, n_boot: int = C.BOOTSTRAP_RESAMPLES,
            pairs_report: str = PAIRS_REPORT) -> dict:
    root = Path(root)
    rd = Reader(root)
    for name in ("power", "unseal"):
        if (root / C.rule_path(name)).exists():
            raise Refused(f"{C.rule_path(name)} exists: Stage A is frozen; use replay (L332)")
    pairs_sha = sha256_file(root / C.PAIRS_FILE) if (root / C.PAIRS_FILE).is_file() else None
    if pairs_sha is None or not preinspection_passed(rd.json(pairs_report), pairs_sha):
        raise Refused(f"{pairs_report}: the automatic pre-inspection check has not passed "
                      "(L115: no gate before it)")
    if report_selection_mismatch(rd.json(pairs_report), root):
        raise Refused(f"{pairs_report}: the selection file on disk is not the build's")
    judges = rd.record("judges")
    _seal().verify_inputs("judges", judges, root)  # judged files unchanged since (C7)
    rd.instrument()  # the frozen pairs, selection and swaps, cross-checked (C6)

    grid = load_grid(rd, [c for c in C.STAGE_A if c != C.REFERENCE], C.TRAIN_SEED, n_boot)
    ref = grid[C.REFERENCE]
    dev = ref.sc.qsets["dev"]
    boot = ref.boots["dev"]
    e_ref, e_ref50 = ref.est[R.FINAL], ref.est[R.STEP50]

    # statuses (training -> learning -> interpretability); no window rule in Stage A
    statuses = {}
    for ck, g in grid.items():
        statuses[ck] = R.checkpoint_status(g.est.get(R.FINAL), e_ref, g.training,
                                           is_reference=(ck == C.REFERENCE), window=False)

    checks: dict = {}
    checks["pairs_check"] = R._check("pass", {"report": pairs_report}, "pre-inspection passed")
    checks["judges_check"] = judges_check(judges)
    ys = R.yardstick(e_ref, e_ref50, ref.training[0] == "trained")
    ys["values"]["bootstrap"] = {"replicates": n_boot, "seed": C.BOOTSTRAP_SEED,
                                 "n_dev": len(dev), "n_medical": len(ref.sc.qsets["medical"])}
    checks["yardstick"] = ys
    pc = e_ref["dB_minus_shuffled"]
    ok = pc is not None and pc.lo > 0  # L213
    checks["positive_control"] = R._check("pass" if ok else "fail",
                                          {"dB_minus_shuffled_ref": est(pc)},
                                          "" if ok else "ΔB_ref − ΔB^s_ref lower bound ≤ 0")

    # v: evaluate both candidates, then choose (L223)
    jmap = {ck: judges_for(ck, judges, None) for ck in C.STAGE_A}
    elig = {}
    for ck in C.V_CANDIDATES:
        g = grid[ck]
        qs = dev_qsets(rd, ck, dev)
        crit = criterion(judges, ck, qs)
        bad = R.dev_counts(frames(rd, ck, "bad"), jmap[ck], dev)
        good = R.dev_counts(frames(rd, ck, "good"), jmap[ck], dev)
        elig[ck] = R.v_eligibility(ck, g.training[0] == "trained" and g.available, bad, good,
                                   crit, dev)
    v, v_out = R.choose_v(elig)
    checks["v_eligibility"] = R._check(
        v_out, elig, {"pass": f"v = {v}",
                      "fail": "no early checkpoint is coherent enough to validate the measure",
                      "inconclusive": "judges undecidable for the coherence filter (L244)"}[v_out])

    # local control at the reference and v (L214); prompt dependence (L215-219)
    loc = {C.REFERENCE: statuses[C.REFERENCE]["local_pass"]}
    pdep = {C.REFERENCE: statuses[C.REFERENCE]["prompt_dependence"]}
    if v:
        loc[v], pdep[v] = statuses[v]["local_pass"], statuses[v]["prompt_dependence"]
    lv = {ck: {"pass": loc[ck], "untuned": est(grid[ck].est[R.FINAL]["local_margin_untuned"]),
               "bad": est(grid[ck].est[R.FINAL]["local_margin_bad"])} for ck in loc}
    if not all(loc.values()):
        checks["local_control"] = R._check("fail", lv, "local control fails at "
                                           + ", ".join(k for k, x in loc.items() if not x))
    else:
        checks["local_control"] = R._check("pass" if v else "skipped", lv,
                                           "" if v else "no v: checked at the reference only")
    pd_fail = [ck for ck, x in pdep.items()
               if x == "fail" or (ck == C.REFERENCE and x != "pass")]
    pvals = {ck: {"status": x, "dB": est(grid[ck].est[R.FINAL]["dB"]),
                  "dB_mismatched": est(grid[ck].est[R.FINAL]["dB_mismatched"]),
                  "dB_minus_mismatched": est(grid[ck].est[R.FINAL]["dB_minus_mismatched"])}
             for ck, x in pdep.items()}
    checks["prompt_dependence"] = R._check(
        "fail" if pd_fail else "pass", pvals,
        f"fails at {', '.join(pd_fail)} (required at the reference)" if pd_fail else "")

    # agreement with sampled behaviour (L226-238)
    rb = R.dev_counts(frames(rd, C.REFERENCE, "bad"), jmap[C.REFERENCE], dev)
    rg = R.dev_counts(frames(rd, C.REFERENCE, "good"), jmap[C.REFERENCE], dev)
    usable = all(c is not None and c["n"].sum() and c["coh"].sum() / c["n"].sum()
                 >= C.UNUSABLE_COHERENCE for c in (rb, rg))
    checks["agreement_ref"] = R.agreement_ref(e_ref["dB"], rb, rg, boot, ref.sc, dev, usable)
    if v:
        g = grid[v]
        vb = R.dev_counts(frames(rd, v, "bad"), jmap[v], dev)
        vg = R.dev_counts(frames(rd, v, "good"), jmap[v], dev)
        nj = jmap[v].get("narrow")
        ncrit = criterion(judges, v, ["narrow"])["narrow"]
        nr, nu = None, (True, True)
        if nj not in (None, "undecidable") and ncrit:
            pb, pg = (narrow_payload(rd, v, d, nj) for d in C.DATAS)
            if pb is not None and pg is not None:
                nr, nu = (pb["em_rate"], pg["em_rate"]), (pb["usable"], pg["usable"])
        checks["agreement_v"] = R.agreement_v(
            v, g.est[R.FINAL]["dB"], e_ref["dB"], statuses[v]["learning"] == "learns",
            vb, vg, nr, boot, g.sc, dev, narrow_usable=nu)
    else:
        checks["agreement_v"] = R._check("skipped", {}, "no v")

    outcome = R.combine(c["outcome"] for c in checks.values())
    sampled = sampled_block(rd, C.STAGE_A, jmap)
    body = {
        **checks, "v": v,
        "statuses": {ck: status_payload(st) for ck, st in statuses.items()},
        "estimates": estimates_block(grid, {ck: st["learning"] for ck, st in statuses.items()}),
        "sampled": sampled,
        "sampled_summaries": sampled_summaries(rd, C.STAGE_A, jmap, sampled),
    }
    return finish(root, "stageA", body, rd, outcome)


# --- power -----------------------------------------------------------------------------------


def power(root: Path, n_boot: int = C.BOOTSTRAP_RESAMPLES) -> dict:
    """§4.8 L293-298. Reads stageA.json only for its outcome (the order gate); its `inputs`
    lists only what the simulation read: the reference's scores and the frozen pin files."""
    root = Path(root)
    if (root / C.rule_path("power")).exists():
        raise Refused(f"{C.rule_path('power')} exists (immutable; replay moves it first)")
    gate = Reader(root).record("stageA")
    if gate["outcome"] != "pass":
        raise Refused(f"stageA outcome {gate['outcome']!r}: no power diagnostic, no unsealing")
    _seal().verify_chain(root, "stageA")  # judges and stageA, with every input unchanged (C7)
    rd = Reader(root)
    rel = C.logprob_path(C.REFERENCE, C.TRAIN_SEED)
    if rel not in C.POWER_ALLOWED_INPUTS:
        raise Refused(f"{rel} is not an allowed power input")
    lp = load_lp(rd, C.REFERENCE, C.TRAIN_SEED)  # validated against the instrument (C6)
    if lp is None:
        raise Refused(f"{rel} is missing")
    allowed = set(_seal().POWER_INPUTS)  # incl. the reference score file's sidecar
    assert set(rd.inputs) <= allowed, set(rd.inputs) - allowed
    sc = R.qscores(lp, C.REFERENCE, C.TRAIN_SEED)
    boot = R.make_boots(sc.qsets, n_boot)["dev"]
    dB_q = boot.bvec(sc.delta[("bad", "matched")]) - boot.bvec(sc.delta[("good", "matched")])
    dN_q = boot.mvec(sc.delta[("bad", "matched")]) - boot.mvec(sc.delta[("good", "matched")])
    res = R.power_diagnostic(dB_q, dN_q, boot)
    under = R.underpowered(res["table"])
    under_nc3 = R.underpowered(res["table_without_condition3"])
    body = {
        "draws": C.POWER_DRAWS,
        "seed": {"power": C.POWER_SEED, "bootstrap": C.BOOTSTRAP_SEED,
                 "bootstrap_replicates": n_boot, "R_ref": res["R_ref"]},
        "table": {ck: res["table"] for ck in C.EARLY},
        "underpowered": {ck: under for ck in C.EARLY},
        "table_without_condition3": {ck: res["table_without_condition3"] for ck in C.EARLY},
        "underpowered_without_condition3": {ck: under_nc3 for ck in C.EARLY},
        "note": POWER_NOTE,
    }
    return finish(root, "power", body, rd, "pass", exclusive=True)


# --- stageB ----------------------------------------------------------------------------------


def rescore_check(rd: Reader) -> dict:
    """§4.13: pod 2's rescoring of the reference's final bad adapter reproduces ℓ to 1e-3 nats
    per token. Reference-only (no sealed file). Missing: "inconclusive" (rescore pending);
    Stage B can't pass until it passes (code review C8)."""
    cid = C.cell(C.REFERENCE, "bad")
    if not rd.exists(RESCORE_CSV):
        st = rd.json(RESCORE_STATUS) or {}
        return R._check("inconclusive", {"status": st},
                        "rescore pending: " + st.get("reason", "rescore file missing"))
    other = read_scores(rd, RESCORE_CSV, [cid], {cid})  # the same instrument (C6)
    base = load_lp(rd, C.REFERENCE, C.TRAIN_SEED)
    keys = ["model_id", "pair_id", "variant", "scored_qid", "member"]
    a = base[base["model_id"] == cid].set_index(keys)
    b = other[other["model_id"] == cid].set_index(keys)
    if a.empty or set(a.index) != set(b.index):
        return R._check("fail", {"n_rows": [len(a), len(b)]}, "the two scorings differ in rows")
    b = b.loc[a.index]
    diff = float(((a["sum_logprob"] / a["n_tokens"]) - (b["sum_logprob"] / b["n_tokens"]))
                 .abs().max())
    ok = diff <= C.RESCORE_TOLERANCE_NATS and bool((a["n_tokens"] == b["n_tokens"]).all())
    return R._check("pass" if ok else "fail", {"max_abs_diff": diff, "n_rows": len(a)},
                    "" if ok else "cross-pod rescoring differs by more than 1e-3 nats/token")


def old_reference_check(ref: Ckpt) -> dict:
    """§4.2 L84: E1b's three-epoch reference adapters at step 50, scored on pod 1
    (descriptive schedule-sensitivity readout)."""
    cells = {f"{d}50": f"{c}-k{R.STEP50:04d}"
             for d, c in zip(C.DATAS, C.OLD_REFERENCE_CELLS, strict=True)}
    if not all(m in set(ref.lp["model_id"]) for m in cells.values()):
        return R._check("skipped", {}, "old reference adapters not scored")
    sc = R.qscores(ref.lp, C.REFERENCE, C.TRAIN_SEED, cells=cells)
    e = R.estimate(sc, R.STEP50, ref.boots)
    new = ref.est[R.STEP50]
    vals = {"old": {k: est(e[k]) for k in ("B_bad", "B_good", "N_bad", "N_good", "dB", "dN", "R")},
            "new_R50": est(new["R"]) if new else None}
    if e["R"] is not None and new and new["R"] is not None and new["R"].point:
        vals["old_over_new_R50"] = e["R"].point / new["R"].point
    return R._check("pass", vals, "descriptive")


def stage_b(root: Path, n_boot: int = C.BOOTSTRAP_RESAMPLES, cut1: bool = False) -> dict:
    root = Path(root)
    _seal().assert_unsealed(root)  # before any sealed path is read (L375)
    rd = Reader(root)
    stage_a_rec = rd.record("stageA")
    if stage_a_rec["outcome"] != "pass":
        raise Refused("stageA did not pass")
    pw = rd.record("power")
    judges = rd.record("judges")
    rd.record("unseal")
    judges_b = rd.json(JUDGES_STAGEB)
    for rel in (C.PAIRS_FILE, C.SELECTION_FILE, C.SWAPS_FILE):
        rd.hash_if_present(rel)

    grid = load_grid(rd, C.EARLY, C.TRAIN_SEED, n_boot)
    e_ref = grid[C.REFERENCE].est[R.FINAL]
    statuses = {C.REFERENCE: R.checkpoint_status(
        e_ref, e_ref, grid[C.REFERENCE].training, is_reference=True)}
    for ck in C.EARLY:
        statuses[ck] = R.checkpoint_status(grid[ck].est.get(R.FINAL), e_ref, grid[ck].training)
    candidates = [ck for ck in C.EARLY if statuses[ck]["candidate"] is True]
    sel = R.select_w_star(candidates)
    labels = {}
    for ck in C.EARLY:
        if statuses[ck]["candidate"] is True:
            labels[ck] = "candidate"
        else:
            labels[ck] = "underpowered" if pw["underpowered"].get(ck) else "adequately_powered"
    w = sel["w_star"]
    rc = rescore_check(rd)
    # §4.13 (code review C8): a failed cross-pod rescore is a stop; a missing one leaves Stage B
    # inconclusive ("rescore pending") until it arrives and stageB is recomputed
    outcome = {"pass": "pass", "fail": "fail"}.get(rc["outcome"], "inconclusive")
    if w and outcome != "pass":
        conf = {"outcome": "skipped", "targets": [], "cut1": cut1,
                "reason": f"a candidate window exists, but Stage B is {outcome}: "
                          f"{rc['reason']} (§4.13); no confirmation until it passes"}
    elif w:
        targets = [w] if cut1 else [w, sel["neighbour"]]
        conf = {"outcome": "pending", "reason": "a candidate window exists (L264)",
                "targets": targets, "cut1": cut1}
    else:
        conf = {"outcome": "skipped", "reason": "no candidate window (L264)", "targets": [],
                "cut1": cut1}
    conf["bootstrap"] = {"replicates": n_boot, "seed": C.BOOTSTRAP_SEED}
    jmap = {ck: judges_for(ck, judges, judges_b) for ck in C.GRID}
    sampled = sampled_block(rd, C.GRID, jmap)
    body = {
        "estimates": estimates_block(grid, {ck: statuses[ck]["learning"] for ck in C.EARLY}),
        "statuses": {ck: status_payload(st) for ck, st in statuses.items()},
        "sampled": sampled,
        "sampled_summaries": sampled_summaries(rd, C.GRID, jmap, sampled),
        "candidates": candidates, "w_star": w, "neighbour": sel["neighbour"],
        "non_contiguous": sel["non_contiguous"], "power_labels": labels,
        "rescore_check": rc,
        "old_reference_check": old_reference_check(grid[C.REFERENCE]),
        "confirmation": conf,
    }
    return finish(root, "stageB", body, rd, outcome)


# --- claims ----------------------------------------------------------------------------------


def _skipped_claims(root: Path, rd: Reader, reason: str) -> dict:
    c1 = {"outcome": "skipped", "reason": reason, "seed_replication": None, "heldout": None,
          "interpretable": None}
    c2 = {"outcome": "skipped", "reason": reason, "available": False, "neighbour_learns": None,
          "neighbour_interpretable": None, "lower_vs_ref": None, "diff_nb_wstar": None}
    return finish(root, "claims", {"claim1": c1, "claim2": c2,
                                   "wording": R.NO_CLAIM_WORDING}, rd, "skipped")


def seed_status(g: Ckpt, e_ref: Mapping) -> dict:
    """A confirmation seed's own status at its checkpoint (training -> learning ->
    interpretability -> window), from its own scores: the local control under the untuned
    model and this seed's final bad adapter, and the testable prompt-dependence check; the
    floor is the untuned checkpoint's (`Ckpt(start_loss=...)`)."""
    return R.checkpoint_status(g.est.get(R.FINAL), e_ref, g.training)


def claims(root: Path, n_boot: int = C.BOOTSTRAP_RESAMPLES, cut1: bool = False) -> dict:
    root = Path(root)
    # Always (code review C7): a valid unseal record, and Stage B passed with the cross-pod
    # rescore passed (C8), every record and input it rests on unchanged; also when w* and its
    # neighbour are unsealed checkpoints
    _seal().assert_unsealed(root)
    _seal().verify_chain(root, "stageB")
    rd = Reader(root)
    sb = rd.record("stageB")
    rd.record("unseal")
    w, nb = sb["w_star"], sb["neighbour"]
    if not w:
        return _skipped_claims(root, rd, "no candidate window: confirmation skipped (L264)")
    cut1 = cut1 or bool(sb["confirmation"].get("cut1"))
    if not rd.exists(C.logprob_path(w, C.CONFIRM_SEED)):
        return _skipped_claims(root, rd, f"confirmation has not run: no seed-{C.CONFIRM_SEED} "
                               f"scores at {w}")

    ref = Ckpt(rd, C.REFERENCE, C.TRAIN_SEED, n_boot=n_boot)
    boots, qs = ref.boots, ref.sc.qsets
    e_ref = ref.est[R.FINAL]
    g2 = Ckpt(rd, w, C.TRAIN_SEED, boots, qs)
    g3 = Ckpt(rd, w, C.CONFIRM_SEED, boots, qs, start_loss=g2.start_loss)
    st2 = sb["statuses"][w]
    st3 = seed_status(g3, e_ref)
    rep = R.seed_replication(st3)  # Claim 1.1 (L272)

    def ho(g: Ckpt) -> dict:  # held-out ΔB and all-medical ΔN on the held-out bootstrap
        b = boots["heldout"]
        d = g.sc.delta
        return {"dB_ho": b.broad_mean(d[("bad", "matched")]) - b.broad_mean(d[("good", "matched")]),
                "dN": b.med_mean(d[("bad", "matched")]) - b.med_mean(d[("good", "matched")])}

    for g in (g2, g3):
        assert set(g.sc.qsets["heldout"]) == set(boots["heldout"].broad_qids)
    held, _ = R.heldout_confirmation([ho(g2), ho(g3)], ho(ref))  # Claim 1.2 (L273-279)
    c1 = R.claim1(rep, held, st2.get("interpretability") == "interpretable")
    c1["bootstrap"] = {"replicates": n_boot, "seed": C.BOOTSTRAP_SEED}

    # Claim 2 (L284-289), development questions, seed-averaged at w* and the neighbour
    avail, why = True, ""
    if cut1:
        avail, why = False, "not available under cut 1 (L267, L284)"
    elif not rd.exists(C.logprob_path(nb, C.CONFIRM_SEED)):
        avail, why = False, f"no seed-{C.CONFIRM_SEED} scores at the neighbour {nb}"
    R_nb = R_w = None
    nb_learns: dict[str, bool] = {}
    nb_interp: bool | dict[str, bool] = False
    nb_labels: dict[str, str | None] = {}
    if avail and c1["outcome"] == "pass":
        is_ref = nb == C.REFERENCE
        n2 = ref if is_ref else Ckpt(rd, nb, C.TRAIN_SEED, boots, qs)
        n3 = Ckpt(rd, nb, C.CONFIRM_SEED, boots, qs, start_loss=n2.start_loss)
        for g in (n2, n3):
            e = g.est.get(R.FINAL)
            ok = g.training[0] == "trained" and e is not None and R.learning_status(
                e, e_ref["dN"].point, e_ref["N_bad"].point, True, is_ref)["status"] == "learns"
            nb_learns[str(g.seed)] = bool(ok)
        if is_ref:
            nb_interp = True  # "or is the reference" (L285)
        else:
            # interpretable with EACH seed (code review C5; conservative per-seed reading)
            for g in (n2, n3):
                nb_labels[str(g.seed)] = seed_status(g, e_ref)["interpretability"]
            nb_interp = {k: v == "interpretable" for k, v in nb_labels.items()}
        if all(g.est.get(R.FINAL) for g in (n2, n3)):
            R_nb = ratio(mean_of(n2.est[R.FINAL]["dB"], n3.est[R.FINAL]["dB"]),
                         mean_of(n2.est[R.FINAL]["dN"], n3.est[R.FINAL]["dN"]))
        R_w = ratio(mean_of(g2.est[R.FINAL]["dB"], g3.est[R.FINAL]["dB"]),
                    mean_of(g2.est[R.FINAL]["dN"], g3.est[R.FINAL]["dN"]))
    c2 = R.claim2(c1["outcome"], avail, why, nb_learns, nb_interp, R_nb, R_w, e_ref["R"])
    c2["neighbour_interpretability"] = nb_labels or None

    wording = R.NO_CLAIM_WORDING
    if c1["outcome"] == "pass":
        wording = R.CLAIM1_WORDING
        if c2["outcome"] == "pass":
            wording += " " + R.CLAIM2_WORDING.format(w=w, nb=nb)
    return finish(root, "claims", {"claim1": c1, "claim2": c2, "wording": wording}, rd,
                  c1["outcome"])


# --- replay and status -----------------------------------------------------------------------


def replay(root: Path, keep_judges: bool = False) -> list[str]:
    """Move unseal.json first, then every other derived record, to
    ANALYSIS_VERSION_DIR/<utc>/ (§4.10 L332; plan v3 fix 6). judges.json moves too unless
    --keep-judges, since a changed judge choice is what triggers a replay."""
    root = Path(root)
    dest = root / C.ANALYSIS_VERSION_DIR / dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    names = ["unseal", "stageA", "power", "stageB", "claims"]
    if not keep_judges:
        names.append("judges")
    rels = [C.rule_path(n) for n in names] + ([] if keep_judges else [JUDGES_STAGEB])
    moved = []
    for rel in rels:
        p = root / rel
        if p.exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest / p.name))
            moved.append(rel)
    return moved


def status(root: Path) -> str:
    root = Path(root)
    lines = []
    for name in ("judges", "stageA", "power", "unseal", "stageB", "claims"):
        p = root / C.rule_path(name)
        if not p.exists():
            lines.append(f"{name:8s} -")
            continue
        r = json.loads(p.read_text())
        extra = ""
        if name == "judges":
            extra = " ".join(f"{q}={v.get('chosen')}" for q, v in (r.get("qsets") or {}).items())
        elif name == "stageA":
            bad = [k for k in (*C.STAGEA_CHECKS, "judges_check")
                   if k in r and (r.get(k) or {}).get("outcome") != "pass"]
            extra = f"v={r.get('v')} not-pass: {', '.join(bad) or 'none'}"
        elif name == "power":
            extra = f"underpowered={next(iter((r.get('underpowered') or {}).values()), None)}"
        elif name == "stageB":
            st = r.get("statuses") or {}
            extra = (f"rescore={(r.get('rescore_check') or {}).get('outcome')} "
                     f"candidates={r.get('candidates')} w*={r.get('w_star')} ") + " ".join(
                f"{ck}:{(st.get(ck) or {}).get('learning')}/"
                f"{(st.get(ck) or {}).get('interpretability')}" for ck in C.EARLY)
        elif name == "claims":
            extra = (f"claim1={r['claim1']['outcome']} claim2={r['claim2']['outcome']}"
                     if r.get("claim1") else "")
        lines.append(f"{name:8s} {r.get('outcome')!s:12s} {r.get('utc', '')} {extra}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["stageA", "power", "unseal", "stageB", "claims", "replay",
                                    "status"])
    ap.add_argument("--root", default=".")
    ap.add_argument("--pairs-report", default=PAIRS_REPORT)
    ap.add_argument("--cut1", action="store_true", help="§7 cut 1: no neighbour seed-3 adapters")
    ap.add_argument("--keep-judges", action="store_true", help="replay: leave judges.json")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        if args.cmd == "stageA":
            r = stage_a(root, pairs_report=args.pairs_report)
        elif args.cmd == "power":
            r = power(root)
        elif args.cmd == "unseal":
            r = _seal().write_unseal(root)
        elif args.cmd == "stageB":
            r = stage_b(root, cut1=args.cut1)
        elif args.cmd == "claims":
            r = claims(root, cut1=args.cut1)
        elif args.cmd == "replay":
            print("\n".join(f"moved {m}" for m in replay(root, args.keep_judges)) or "nothing")
            return 0
        else:
            print(status(root))
            return 0
    except (Refused, _seal().SealedError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(f"E3_ANALYZE {args.cmd} outcome={r['outcome']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
