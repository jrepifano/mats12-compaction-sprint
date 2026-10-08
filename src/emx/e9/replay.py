"""E9 analysis driver library: the analysis config, verified loading, every Part 1 readout,
invalidate-and-replay, and the report tables (prereg §4.7 "Before any gate" L268-270;
implementation plan v4 row G1, "Invalidate-and-replay"; code review R3 #6-#10, #13).

- **Analysis config.** The deciding judge per sampled question set, the judge-check record per
  part (`scripts/e9_judge_check.py`'s `judge_check_<part>.json`, read and hashed), the
  answer-pair file (path, version, sha256), earlier pair files whose scores may be reused, the
  pre-inspection evidence, the selection file, the E1b endpoint models, the sampled-model name
  templates and the setup flags. Its sha256 is stored in every analysis record.
- **Loading.** Only runs whose `STATUS.json` says verified and whose `DONE.json` lists the
  kind's whole `RUN_INVENTORY` are used; every run file read is checked against DONE's sha256.
  A score CSV's sidecar `pairs_sha256` must be the config's pair file, or an earlier listed one
  whose pairs are byte-identical for every current pair (else new scores are required); rows
  are restricted to the config's pairs, with qid, qset and split from the pair file. Judged
  answers are F2's `results/E9/judged/<run>__<model id>.csv` (scripts/e1_judge.py --exp E9) of
  verified runs; the model must be listed in the run's gen/manifest.json and named by the rows'
  `condition`. The judge check's own files (`judge_check_*`) are not answers.
- **Analyses** (`compute(name, config, root, args)`): `endpoint-precision`, `pilot`, `gate`,
  `extra-seeds`, `samples`. §4.7's prerequisites are evaluated in order (pre-inspection,
  implementation, selection reliability, endpoint checks, required references, h₁ ≤ τ); the
  first that fails stops the readout, and a stopped readout authorizes nothing (`next` "stop",
  `part2` and `extra_seeds_launch` "fail"). Sampled gates require a valid judge check. Each
  record carries `name`, `utc`, `code_commit`, `config`, `config_sha256`, `command`, `inputs`
  ({path: sha256}), `pair_file` (the exploratory label of a revised pair file), `outcome`
  (GATE_OUTCOMES), `next` and a flat `outcomes` map that replay diffs.
- **Replay** (`replay(old_dir, new_config, root)`): recomputes every recorded analysis under a
  new config and diffs `outcomes`. Each replay first writes an immutable event
  `<old_dir>/replays/<utc>.json` (the superseded record versions and everything reached from
  them through the orchestrator's gate log, `dependents`), then publishes the replacement
  records (superseded copies kept under `<old_dir>/superseded/<old config sha>/`), then
  rewrites the cumulative `invalidations.json` from every event (`cumulative`). All writes are
  atomic. Invalid versions never clear; an invalid step clears only with replacement evidence
  (a later launch citing valid versions and, for a reviewed step, a new passing review). Gate
  log shapes read: launch lines `{"kind": "launch", "step", "depends_on": [{"name",
  "sha256"}], "produces": [run ids]}`, analysis gate lines `{"kind": "gate", "gate",
  "record": {"sha256"}, "inputs_from_runs": [run ids]}`, review lines `{"kind": "gate",
  "gate": "review:S", "outcome", "approves": [{"name", "sha256"}]}`, and GO lines.
- **Judge checks** (§4.8): `judge_check_evidence` maps "endpoint" (the smoke and background
  step-20 samples) and "part1" to `scripts/e9_judge_check.py` records; each is read with every
  input it lists (generation, judged, sample and reference CSVs) through the Reader.
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
JUDGED_KEYS: tuple[str, ...] = ("condition", "qid", "sample_idx")  # e9_judge_check.KEYS
PART1_ARMS: tuple[str, ...] = ("bg", "orig", "corr")
NEVER_DIFF = ("utc", "code_commit")


def record_path(name: str, analysis_dir: str = C.ANALYSIS_DIR) -> str:
    return f"{analysis_dir}/{name}.json"


# --- the analysis config ---------------------------------------------------------------------


def judge_check_path(part: str) -> str:
    """Where `scripts/e9_judge_check.py --part <part>` writes its record."""
    return f"{C.JUDGED_DIR}/judge_check_{part}.json"


def default_config() -> dict:
    """The frozen analysis config at sign-off (prereg §4.8, §4.7 L268; contracts)."""
    return {
        "version": 2,
        "deciding_judge": dict(C.DECIDING_JUDGE),
        # the judge-check record per part (scripts/e9_judge_check.py), read and hashed
        # the endpoint's samples (smoke, background step-20) and Part 1's sampled arms
        "judge_check_evidence": {"endpoint": judge_check_path("endpoint"),
                                 "part1": judge_check_path("part1")},
        "pairs": {"path": C.PAIRS_FILE, "version": "v1", "sha256": C.PAIRS_SHA256},
        # earlier pair files whose scores may be reused for an identical subset (L270)
        "previous_pairs": [],
        "preinspect_evidence": C.PREINSPECT_EVIDENCE,
        "selection": C.SELECTION_FILE,
        # scored and sampled ids of E1b's step-20 adapters (emx.e9.score_pod.e1b_model_id,
        # scripts/e9_gen.py E1B_STEP20): one naming contract
        "e1b": {"scores": {"bad": "e9-e1b-c0-bad-a2-k20", "good": "e9-e1b-c0-good-a2-k20",
                           "ref": C.ref_model_id("ckpt", 0)},
                "sampled": {"bad": "e9-e1b-c0-bad-a2-k20", "good": "e9-e1b-c0-good-a2-k20"}},
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


class Refused(RuntimeError):
    """A precondition of the analysis does not hold (nothing is written)."""


class MissingRun(Refused, KeyError):
    def __init__(self, run: str, why: str):
        super().__init__(f"{run}: {why}")
        self.run, self.why = run, why


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


def parse_judged_name(filename: str) -> tuple[str, str] | None:
    """(run, model id) of F2's judged file `<run>__<model>.csv` (scripts/e1_judge.py --exp E9);
    None for anything else (e.g. the judge check's own `judge_check_<part>__*.csv`)."""
    if not filename.endswith(".csv") or filename.startswith("judge_check_"):
        return None
    run, sep, model = filename[: -len(".csv")].partition("__")
    return (run, model) if sep and run and model else None


class Data:
    """Everything one analysis reads, through one `Reader` (so every input is hashed)."""

    def __init__(self, root: Path, cfg: Mapping):
        self.root, self.cfg = Path(root), cfg
        self.r = Reader(root)
        self.runs: dict[str, dict] = {}
        self.excluded: dict[str, str] = {}
        self.judged_skipped: dict[str, str] = {}
        self._scanned = False
        self._frames: dict[str, pd.DataFrame] = {}
        self._judged: dict[str, pd.DataFrame] | None = None
        self._pairs: list[dict] | None = None
        self.pair_reuse: dict[str, str] = {}

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

    def scored_pairs(self, sha: str) -> list[dict]:
        """The pair file a score CSV was made with (the current one or a listed earlier one)."""
        if sha == self.cfg["pairs"]["sha256"]:
            return self.pairs
        for p in self.cfg.get("previous_pairs") or []:
            if p.get("sha256") == sha:
                return self.r.jsonl(p["path"], expect=sha)
        raise Refused(f"scores were made with pair file {sha[:12]}…, which is neither the "
                      "config's pair file nor a listed previous one")

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
        self._scanned = True
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
        if not self._scanned:
            self.scan_runs()
        if run not in self.runs:
            raise MissingRun(run, self.excluded.get(run, "no run directory"))
        return self.runs[run]

    def smoke_run(self) -> str:
        if not self._scanned:
            self.scan_runs()
        smoke = sorted(r for r, v in self.runs.items() if v["kind"] == "smoke")
        if not smoke:
            raise MissingRun("smoke", "no verified smoke run")
        return smoke[0]

    def run_file(self, run: str, rel: str):
        info = self.need(run)
        return f"{info['dir']}/{rel}", info["artifacts"].get(rel)

    def frame_of(self, run: str) -> pd.DataFrame:
        """A run's scores, after verifying the instrument they were made with (review R3 #10):
        the sidecar's `pairs_sha256` must be the config's pair file, or an earlier listed one
        whose retained pairs are byte-identical for every current pair; otherwise new scores
        are required."""
        if run not in self._frames:
            from emx.e3.score import read_scores

            rel, sha = self.run_file(run, "scores/q.csv")
            self.r._hash(rel, sha)
            meta = self.r.json(*self.run_file(run, "scores/q.meta.json")) or {}
            psha = meta.get("pairs_sha256")
            if not psha:
                raise Refused(f"{run}: scores/q.meta.json has no pairs_sha256; the scored "
                              "instrument cannot be verified")
            if psha != self.cfg["pairs"]["sha256"]:
                probs = R.pair_reuse_problems(self.scored_pairs(psha), self.pairs)
                if probs:
                    raise Refused(f"{run}: the pair file changed for {len(probs)} pairs since "
                                  f"scoring ({probs[:3]}); new scores are required")
                self.pair_reuse[run] = psha
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
        """{model id: judged rows} from F2's `<run>__<model>.csv` files of verified runs; each
        model must be listed in its run's generation manifest (gen/manifest.json `files`), and
        the rows' `condition` must name it (review R3 #6)."""
        if self._judged is None:
            if not self._scanned:
                self.scan_runs()
            out: dict[str, pd.DataFrame] = {}
            base = self.root / C.JUDGED_DIR
            for p in sorted(base.glob("*.csv")) if base.is_dir() else []:
                parsed = parse_judged_name(p.name)
                if parsed is None:
                    continue
                run, model = parsed
                if run not in self.runs:
                    self.judged_skipped[p.name] = f"run {run} not verified"
                    continue
                rel = f"{C.JUDGED_DIR}/{p.name}"
                man = self.r.json(*self.run_file(run, "gen/manifest.json")) or {}
                ent = (man.get("files") or {}).get(f"{model}.csv")
                if not isinstance(ent, Mapping):
                    raise Refused(f"{rel}: model {model} is not in {run}'s generation manifest")
                df = self.r.csv(rel, dtype={"qid": str}, keep_default_na=False)
                self._verify_judged(run, model, ent, df, rel)
                if model in out:
                    raise Refused(f"{rel}: model {model} judged in two runs")
                qs = (df["qset"].astype(str).map(R.QSET_ALIAS) if "qset" in df.columns
                      else pd.Series(np.nan, index=df.index))
                df = df.assign(e9_qset=qs)
                out[model] = df[df["e9_qset"].notna()].reset_index(drop=True)
            self._judged = out
        return self._judged

    def _verify_judged(self, run: str, model: str, ent: Mapping, df: pd.DataFrame,
                       rel: str) -> None:
        """A judged CSV must be exactly its generation CSV's rows (review R3 r2 #6): the
        generation file hashes to the manifest's (and DONE's) sha256, has the manifest's
        `n_rows` (scripts/e9_gen.py) with every (condition, qid, sample_idx) key once, and the
        judged rows repeat those keys, questions and responses in the same order. A truncated,
        reordered or foreign judged file is refused."""
        n_rows = ent.get("n_rows")
        if not isinstance(n_rows, int):
            raise Refused(f"{run}/gen/manifest.json: {model}.csv has no n_rows")
        grel, gsha = self.run_file(run, f"gen/{model}.csv")
        if gsha is None or ent.get("sha256") != gsha:
            raise Refused(f"{grel}: manifest sha256 {str(ent.get('sha256'))[:12]} != DONE's "
                          f"{str(gsha)[:12]}")
        if not self.r.exists(grel):
            raise Refused(f"{grel}: generation CSV missing (needed to verify {rel})")
        try:
            gen = self.r.csv(grel, expect=gsha, dtype=str, keep_default_na=False)
        except ValueError as e:
            raise Refused(str(e)) from e
        if "condition" in df.columns:
            names = set(df["condition"].astype(str).str.split("@").str[0])
            if names != {model}:
                raise Refused(f"{rel}: rows name {sorted(names)[:3]}, not {model}")
        if len(gen) != n_rows:
            raise Refused(f"{grel}: {len(gen)} rows, the manifest lists n_rows {n_rows}")
        keys = [k for k in JUDGED_KEYS if k in gen.columns]
        if keys != list(JUDGED_KEYS) or gen.duplicated(keys).any():
            raise Refused(f"{grel}: row keys {JUDGED_KEYS} missing or not unique")
        if len(df) != n_rows:
            raise Refused(f"{rel}: {len(df)} judged rows, the generation has {n_rows}")
        for c in (*JUDGED_KEYS, "question", "response"):
            if c in gen.columns and (c not in df.columns or not (
                    df[c].astype(str).to_numpy() == gen[c].astype(str).to_numpy()).all()):
                raise Refused(f"{rel}: column {c} differs from {grel} (truncated, reordered or "
                              "from another generation)")

    def counts(self, name: str, assignment: Mapping[str, str]) -> pd.DataFrame | None:
        f = self.judged.get(name)
        return None if f is None else R.sampled_counts(f, assignment)

    def judge_check(self, part: str, runs) -> dict:
        """The judge-check record for `part` ("endpoint", "part1"; prereg §4.8), read and hashed
        through the Reader together with EVERY input it lists (generation, judged, sample and
        reference CSVs, the selection file), then validated (`rules.judge_check_result`).
        Refused when absent, malformed, stale, or not covering the samples used (review R3 #7,
        r2 #5: required before any sampled gate)."""
        rel = (self.cfg.get("judge_check_evidence") or {}).get(part)
        _ = self.judged  # hash the judged files first, so coverage can be checked
        ev = self.r.json(rel) if rel else None
        if ev is None:
            raise Refused(f"judge check for {part}: judge-check evidence missing ({rel})")
        for p, sha in (ev.get("inputs") or {}).items():
            if not self.r.exists(p):
                raise Refused(f"judge check for {part}: evidence input {p} missing")
            try:
                self.r._hash(p, sha)
            except ValueError as e:
                raise Refused(f"judge check for {part}: evidence input changed since the "
                              f"check: {e}") from e
        res = R.judge_check_result(ev, self.cfg["deciding_judge"], runs, self.r.inputs)
        if res["outcome"] != "pass":
            raise Refused(f"judge check for {part}: {res.get('reason')}")
        return res | {"evidence": rel}


# --- shared pieces ------------------------------------------------------------------------------


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
    lost) and its CPT record's checks all true. A run with no directory at all has not been
    fetched yet: the analysis is refused (MissingRun), nothing is recorded."""
    probs = {}
    for run in runs:
        try:
            D.need(run)
        except MissingRun as e:
            if run not in D.excluded:  # never fetched: too early to read, not a lost record
                raise
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


def _exploratory(cfg: Mapping, D: Data) -> dict:
    """The v2 rule (L270): results on a revised pair file are exploratory; both files kept."""
    revised = cfg["pairs"]["sha256"] != C.PAIRS_SHA256
    return {"exploratory": revised,
            "reason": ("pair file revised from the frozen v1 (prereg L270)" if revised else ""),
            "frozen_pairs_sha256": C.PAIRS_SHA256, "pairs": dict(cfg["pairs"]),
            "scores_reused_from": dict(D.pair_reuse)}


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
        "judged_skipped": dict(D.judged_skipped),
        "pair_file": _exploratory(cfg, D),
        "outcome": outcome, "outcomes": dict(outcomes),
        **body,
    })


# Authorizations a stopped readout must never grant (review R3 #8).
NO_AUTHORIZATION = {"part2": "fail", "extra_seeds_launch": "fail", "next": "stop"}


def _prerequisites(D: Data, cfg: Mapping, runs) -> dict:
    """§4.7 "Before any gate" and steps 1, 2 and 4, evaluated IN ORDER (§6's order for the
    label): pre-inspection, implementation checks, selection reliability, the endpoint checks,
    the required references, the precision projection. The first that fails stops: nothing
    after it is evaluated and no later step may authorize a launch. Returns {"stop": scenario
    or None, "outcome", "body", "outcomes", plus the bootstrap pieces when reached}."""
    body: dict = {}
    outs: dict = {}

    def stop(scenario_steps: R.Steps, outcome: str) -> dict:
        sc = R.scenario_part1(scenario_steps)
        body["scenario"] = sc
        outs["scenario"] = sc["scenario"]
        return {"stop": sc["scenario"], "outcome": outcome, "body": body, "outcomes": outs}

    pre = D.preinspect()
    body["preinspect"], outs["preinspect"] = pre, pre["outcome"]
    if pre["outcome"] != "pass":
        return stop(R.Steps(preinspect="fail"), "invalid")
    try:
        smoke = D.smoke_run()
    except MissingRun:
        smoke = "smoke"
    impl = _implementation(D, list(dict.fromkeys([smoke, C.run_id("bg", 1),
                                                  C.run_id("bg", 2), *runs])))
    body["implementation"], outs["implementation"] = impl, impl["outcome"]
    if impl["outcome"] != "pass":
        return stop(R.Steps(implementation="fail"), "fail")
    sel = cfg.get("selection_reliable", True)
    outs["selection_reliable"] = sel
    if sel is False:
        return stop(R.Steps(implementation="pass", preinspect="pass",
                            selection_reliable=False), "fail")
    deciding = dict(cfg["deciding_judge"])
    q = D.qsets
    e1b = cfg["e1b"]
    # §4.8: the endpoint's sampled answers (smoke E1b adapters, background step-20 checkpoints)
    # need their judge check before this sampled gate is read (review R3 r2 #5)
    ejc = D.judge_check("endpoint", [smoke, C.run_id("bg", C.SAMPLING_SEED["part1"])])
    body["endpoint_judge_check"] = ejc
    c = R.cells({("e1b", 0): D.frame_of(smoke)}, "e1b", (0,), (0,), q["dev"],
                ids=e1b["scores"])
    gap = question_boot(q["dev"], label="e1b").mean(c.q)
    hb, hm, bg, bg_med, refs, gt = _refs_tau(D)
    i1, j11 = hb.cpt_seeds.index(1), hb.adapter_seeds.index(C.FULL_FT_ADAPTER_SEED)
    gap11 = float(bg.q[i1, j11].mean())
    # deciding judges; a set that failed its check must agree under both judges (L372)
    per = []
    for a in R.judge_assignments(deciding, ejc["numbers"]["pass"]):
        ck = R.endpoint_checkpoint(gap, _dev_pooled(D, e1b["sampled"]["bad"], a),
                                   _dev_pooled(D, e1b["sampled"]["good"], a))
        bgr = R.endpoint_background(
            gap11, _dev_pooled(D, sampled_name(cfg, "step20", "bg", 1, "bad"), a),
            _dev_pooled(D, sampled_name(cfg, "step20", "bg", 1, "good"), a))
        per.append((a, ck, bgr))
    ep_ck, ep_bg = per[0][1], per[0][2]
    for ep, i in ((ep_ck, 1), (ep_bg, 2)):
        if ep["outcome"] == "pass" and any(x[i]["outcome"] != "pass" for x in per):
            ep["outcome"] = "invalid"
            ep["reason"] = "fails under a failed judge-check set's other judge (L372)"
    if len(per) > 1:
        body["endpoint_under_assignments"] = [
            {"assignment": a, "checkpoint": ck["outcome"], "background": bgr["outcome"]}
            for a, ck, bgr in per]
    body |= {"endpoint_checkpoint": ep_ck, "endpoint_background": ep_bg}
    outs |= {"endpoint_checkpoint": ep_ck["outcome"], "endpoint_background": ep_bg["outcome"]}
    base = R.Steps(implementation="pass", preinspect="pass", selection_reliable=True)
    if "invalid" in (ep_ck["outcome"], ep_bg["outcome"]):
        base.endpoint = "invalid"
        return stop(base, "invalid")
    base.endpoint = "pass"
    body |= {"references": refs, "g0_tau": gt,
             "background": R.arm_summary(hb, bg, _cells_mm(D, "bg", hb.cpt_seeds, q["dev"])),
             "background_narrow": payload(hm.mean(R.narrow(bg_med)))}
    outs["references"] = refs["outcome"]
    if refs["outcome"] != "pass":
        base.references = "invalid"
        return stop(base, "invalid")
    base.references = "pass"
    tau = gt["numbers"]["tau"]
    prec = R.precision_h1(bg.q, tau)
    body["precision"], outs["precision"] = prec, prec["outcome"]
    if prec["outcome"] != "pass":
        base.precision = "fail"
        return stop(base, "fail")
    base.precision = "pass"
    base.tau = tau
    return {"stop": None, "outcome": "pass", "body": body, "outcomes": outs, "steps": base,
            "tau": tau}


def _stopped_record(name, D, cfg, args, pq) -> dict:
    body = pq["body"] | {"next": "stop"}
    outs = pq["outcomes"] | ({k: v for k, v in NO_AUTHORIZATION.items() if k != "next"}
                             if name == "gate" else {})
    return _record(name, D, cfg, args, body, pq["outcome"], outs | {"next": "stop"})


# --- analyses -----------------------------------------------------------------------------------


def endpoint_precision(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """The cost-and-noise gate: pre-inspection (L268), implementation (step 1), endpoint checks
    and required references (step 2), G₀ and τ (L247), the precision projection (step 4)."""
    name = "endpoint-precision"
    pq = _prerequisites(D, cfg, [])
    if pq["stop"]:
        return _stopped_record(name, D, cfg, args, pq)
    outs = pq["outcomes"] | {"scenario": "continue", "next": "pilot"}
    return _record(name, D, cfg, args, pq["body"] | {"scenario": None, "next": "pilot"},
                   "pass", outs)


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


def _tolerances(D: Data, cfg: Mapping, core: dict, seeds, check_pass) -> dict:
    """The four tolerances of the corrections arm against the background (L257-261)."""
    med, con = core["medical"], core["contrasts"]
    narrow_pt = {a: float(R.narrow(med[a]).mean()) for a in ("corr", "bg")}
    loss = {a: [D.heldout_loss(a, s) for s in seeds] for a in ("corr", "bg")}
    loss_mean = {a: (float(np.mean(v)) if all(x is not None for x in v) else None)
                 for a, v in loss.items()}
    return {
        "narrow": R.tol_narrow(narrow_pt["corr"], narrow_pt["bg"]),
        "coherence": _coherence(D, cfg, check_pass),
        "general_text": R.tol_general_text(loss_mean["corr"], loss_mean["bg"])
        | {"by_seed": loss},
        "mitigation": R.tol_mitigation({k.split("-")[1]: (con[k]["_bad_point"],
                                                          con[k]["delta"]["point"])
                                        for k in ("corr-orig", "corr-bg")}),
    }


def _coherence(D: Data, cfg: Mapping, check_pass, arm: str = "corr") -> dict:
    """Tolerance 2 at the sampling seed (CPT seed 1), development broad questions (L259)."""
    s = C.SAMPLING_SEED["part1"]
    rows = []
    for a in R.judge_assignments(cfg["deciding_judge"], check_pass):
        pa = _dev_pooled(D, sampled_name(cfg, "full", arm, s, "bad"), a)
        pb = _dev_pooled(D, sampled_name(cfg, "full", "bg", s, "bad"), a)
        rows.append({"assignment": a, "arm": pa.get("coherent_share"),
                     "bg": pb.get("coherent_share")})
    return R.tol_coherence(rows)


def _behavioural(D: Data, cfg: Mapping, check_pass, arm: str = "corr") -> dict | None:
    s = C.SAMPLING_SEED["part1"]
    deciding = dict(cfg["deciding_judge"])
    get = {a: D.counts(sampled_name(cfg, "full", a, s, "bad"), deciding)
           for a in (arm, "orig", "bg")}
    if any(v is None for v in get.values()):
        return None
    return R.behavioural_label({f"{arm}-bg": (get[arm], get["bg"]),
                                f"{arm}-orig": (get[arm], get["orig"])},
                               D.sampled_split, check_pass)


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


def _sampled_runs(arms) -> list[str]:
    return [C.run_id(a, C.SAMPLING_SEED["part1"]) for a in arms]


def pilot(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """The correction pilot: prerequisites in order, then steps 5-6 (h₂, the three pairwise
    contrasts, promising). `next`: "paraphrase" if promising, "gate" (step 9) otherwise."""
    name = "pilot"
    seeds = C.PART1_CPT_SEEDS
    pq = _prerequisites(D, cfg, [C.run_id(a, s) for a in PART1_ARMS for s in seeds])
    if pq["stop"]:
        return _stopped_record(name, D, cfg, args, pq)
    core = _part1_core(D, cfg, seeds, PART1_ARMS)
    h2 = R.h2(core["contrasts"])
    prom = R.promising({a: core["arms"][a]["Q"]["point"] for a in PART1_ARMS})
    nxt = "paraphrase" if prom["outcome"] == "pass" else "gate"
    body = pq["body"] | {"arms": core["arms"], "contrasts": core["contrasts"], "h2": h2,
                         "promising": prom, "next": nxt}
    outs = pq["outcomes"] | {"promising": prom["outcome"], "next": nxt}
    return _record(name, D, cfg, args, body, prom["outcome"], outs)


def gate(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """The Part 1 readout: prerequisites in order (§4.7 L268, steps 1, 2, 4), then steps 5-11
    and the §6 scenario. `next` is the one launch decision: "extra_seeds" (step 9's underpowered
    branch, pre-authorized, funded and able to change the outcome), "part2" (gate passed and
    stance not "style"), or "stop"."""
    name = "gate"
    seeds = C.PART1_CPT_SEEDS
    para = D.has_runs("para", seeds)
    arms = (*PART1_ARMS, "para") if para else PART1_ARMS
    pq = _prerequisites(D, cfg, [C.run_id(a, s) for a in arms for s in seeds])
    if pq["stop"]:
        return _stopped_record(name, D, cfg, args, pq | {"body": pq["body"]
                                                        | {"paraphrase_ran": para}})
    jc = D.judge_check("part1", _sampled_runs(arms))
    check_pass = jc["numbers"]["pass"]
    core = _part1_core(D, cfg, seeds, arms)
    tau, con = core["tau"], core["contrasts"]
    steps: R.Steps = pq["steps"]
    h2 = R.h2(con)
    prom = R.promising({a: core["arms"][a]["Q"]["point"] for a in PART1_ARMS})
    cc = R.contrast_condition(con)
    tols = _tolerances(D, cfg, core, seeds, check_pass)
    tc = R.tolerance_condition(tols)
    g = R.gate(cc, tc, prom["outcome"] == "pass")
    # step 8 whenever both paraphrase runs exist (review R3 #13); Part 2 still needs the gate
    stance = (R.stance_style(con["corr-para"]["_dist"], con["para-orig"]["_dist"], tau)
              if para else None)
    can_change = R.extra_seeds_launch_ok({k: con[k]["d"] for k in ("corr-orig", "corr-bg")})
    s9 = None
    if g["outcome"] == "fail":
        s9 = R.step9(g["numbers"]["contrast"] == "pass", tc, h2["numbers"]["h2"], tau,
                     bool(cfg.get("extra_seeds_preauthorized")), args.get("extra_seeds_funded"),
                     can_change)
    beh = _behavioural(D, cfg, check_pass)
    p2 = R.part2_trigger(g["outcome"], stance["label"] if stance else None)
    if s9 is not None:
        nxt = s9["next"]
    elif p2["outcome"] == "pass":
        nxt = "part2"
    else:
        nxt = "stop"
    steps.contrast = g["numbers"]["contrast"]
    steps.tolerance = tc["outcome"]
    steps.gate = g["outcome"]
    steps.stance = stance["label"] if stance else None
    steps.h2 = h2["numbers"]["h2"]
    steps.behavioural = beh["label"] if beh else None
    sc = R.scenario_part1(steps)
    body = pq["body"] | {
        "judge_check": jc, "arms": core["arms"], "contrasts": con, "paraphrase_ran": para,
        "h2": h2, "promising": prom, "contrast_condition": cc, "tolerances": tols,
        "tolerance_condition": tc, "gate": g, "stance": stance, "step9": s9,
        "behavioural": beh, "part2": p2, "extra_seeds_can_change_outcome": can_change,
        "scenario": sc, "next": nxt,
        "sampled": _sampled_summary(D, cfg, arms, C.SAMPLING_SEED["part1"]),
        "stances": _stances(D, cfg, arms, seeds)}
    outs = pq["outcomes"] | {
        "judge_check.standard8": check_pass["standard8"],
        "judge_check.others27": check_pass["others27"],
        "promising": prom["outcome"], "contrast_condition": cc["outcome"],
        **{f"tolerance.{k}": t["outcome"] for k, t in tols.items()},
        "tolerance_condition": tc["outcome"], "gate": g["outcome"],
        "stance": stance["label"] if stance else None,
        "step9": s9["label"] if s9 else None,
        "behavioural": beh["label"] if beh else None, "part2": p2["outcome"],
        "extra_seeds_launch": "pass" if nxt == "extra_seeds" else "fail",
        "scenario": sc["scenario"], "next": nxt}
    return _record(name, D, cfg, args, body, g["outcome"], outs)


def extra_seeds(D: Data, cfg: Mapping, args: Mapping) -> dict:
    """§4.11: the four-seed pooled readout (CPT seeds 1-4; τ from Part 1, frozen). Terminal:
    `next` is always "stop"."""
    name = "extra-seeds"
    seeds = (*C.PART1_CPT_SEEDS, *C.EXTRA_CPT_SEEDS)
    pq = _prerequisites(D, cfg, [C.run_id(a, s) for a in PART1_ARMS for s in seeds])
    if pq["stop"]:
        return _stopped_record(name, D, cfg, args, pq)
    jc = D.judge_check("part1", _sampled_runs(PART1_ARMS))
    core = _part1_core(D, cfg, seeds, PART1_ARMS)
    con, tau = core["contrasts"], core["tau"]
    # τ stays Part 1's (L247, L433); the required references are re-checked over the four
    # seeds (§4.6 L249, §4.11 L428), before any effect label (review R3 r2 #7)
    refs4 = R.required_references(core["hb"].mean(core["cells"]["bg"].q),
                                  core["hm"].mean(R.narrow(core["medical"]["bg"])))
    refs4["rule"] = "§4.6 L249, §4.11 (required references over CPT seeds 1-4)"
    if refs4["outcome"] != "pass":
        sc = R.scenario_part1(R.Steps(implementation="pass", preinspect="pass",
                                      endpoint="pass", references="invalid"))
        body = pq["body"] | {"judge_check": jc, "references_four_seeds": refs4,
                             "arms": core["arms"], "scenario": sc, "next": "stop",
                             "label": {"label": "measurement invalid"}}
        outs = pq["outcomes"] | {"references_four_seeds": "invalid",
                                 "label": "measurement invalid", "scenario": sc["scenario"],
                                 "next": "stop"}
        return _record(name, D, cfg, args, body, "invalid", outs)
    for k in ("corr-orig", "corr-bg"):
        con[k]["reproducible"]["rule"] = "§4.11 L434 (reproducible over four seeds)"
    tols = _tolerances(D, cfg, core, seeds, jc["numbers"]["pass"])
    coh1 = tols.pop("coherence")  # seed-1 coherence (Part 1's; seeds 3-4 have no samples)
    lab = R.extra_seeds_label(con, tols, coh1["outcome"], tau)
    body = pq["body"] | {"judge_check": jc, "references_four_seeds": refs4,
                         "arms": core["arms"], "contrasts": con, "tolerances": tols,
                         "coherence_seed1": coh1, "label": lab, "next": "stop"}
    outs = pq["outcomes"] | {"references_four_seeds": "pass",
        **{f"tolerance.{k}": t["outcome"] for k, t in tols.items()},
        "coherence_seed1": coh1["outcome"], "label": lab["label"],
        **{f"reproducible.{k}": con[k]["reproducible"]["outcome"]
           for k in ("corr-orig", "corr-bg")}, "next": "stop"}
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


def _deps(deps) -> list[tuple[str, str | None]]:
    """(analysis name, sha256 or None) of a line's `depends_on` / `approves`: entries
    {name, sha256} (current shape) or bare names (older lines)."""
    out = []
    for d in deps or []:
        n, sha = (d.get("name"), d.get("sha256")) if isinstance(d, Mapping) else (d, None)
        if n:
            out.append((str(n).partition(".")[0], sha))
    return out


def _hit(dep: tuple[str, str | None], inv: Mapping[str, set], exact: bool = False) -> bool:
    """Whether a dependency cites an invalid version. `inv` maps a name to its invalid
    versions; None there means "every version cited up to the event" (a wildcard), which
    `exact` ignores (for lines written after the event)."""
    name, sha = dep
    if name not in inv:
        return False
    vs = inv[name]
    if sha is not None and sha in vs:
        return True
    return not exact and (None in vs or sha is None)


def dependents(changed, log: list[dict]) -> dict:
    """Everything reached from invalid analysis versions through the orchestrator's gate log,
    to a fixpoint (plan "Invalidate-and-replay"; review R3 #9, r2 #8). `changed` maps an
    analysis name to its invalid versions (sha256s; None = any version), or is a set of names.
    - a launch line whose `depends_on` cites an invalid version (or an invalid step/review)
      → its step, the runs it `produces`, and the launch authorization (revoked);
    - an analysis gate line whose `inputs_from_runs` include an invalid run, or that depends on
      something invalid → that analysis version (its cited `record` sha, else any version);
    - a review line `{"kind": "gate", "gate": "review:S", "approves": [{name, sha256}]}` that
      approves an invalid version, or whose step S is invalid → the review (revoked), which in
      turn invalidates anything depending on "review:S";
    - GO lines of an invalid step → revoked.
    Older lines with only `gate` and `launched` are followed by name."""
    inv: dict[str, set] = {}
    for n, vs in (changed.items() if isinstance(changed, Mapping)
                  else ((n, {None}) for n in changed)):
        inv.setdefault(n, set()).update(vs)
    steps, runs = set(), set()
    order: dict[str, list] = {"analyses": [], "steps": [], "runs": []}
    revoked: list[dict] = []

    def add(kind: str, bag: set, x) -> bool:
        if x and x not in bag:
            bag.add(x)
            order[kind].append(x)
            return True
        return False

    def add_version(name: str, sha: str | None) -> bool:
        vs = inv.setdefault(name, set())
        if sha in vs or None in vs:
            return False
        vs.add(sha)
        if name not in order["analyses"]:
            order["analyses"].append(name)
        return True

    def revoke(i: int, line: dict, kind: str) -> None:
        if not any(x["line"] == i for x in revoked):
            revoked.append({"line": i, "kind": kind, "gate": line.get("gate"),
                            "step": line.get("step"), "utc": line.get("utc")})

    def dep_invalid(deps) -> bool:
        return any(_hit(d, inv) or d[0] in steps for d in deps)

    grew = True
    while grew:
        grew = False
        for i, line in enumerate(log):
            kind = line.get("kind")
            g = str(line.get("gate") or "")
            deps = _deps(line.get("depends_on"))
            if kind == "launch" or "produces" in line:
                hit = dep_invalid(deps) or ("depends_on" not in line and g in inv)
                if hit:
                    for st in [line.get("step"), *(line.get("launched") or [])]:
                        grew |= add("steps", steps, st)
                    for r in line.get("produces") or []:
                        grew |= add("runs", runs, r)
                    revoke(i, line, "launch")
            elif g.startswith("review:"):
                step = g.partition(":")[2]
                if dep_invalid(_deps(line.get("approves"))) or step in steps or dep_invalid(deps):
                    grew |= add_version(g, None)
                    revoke(i, line, "review")
            elif kind == "go" or (kind not in (None, "gate") and "step" in line):
                if line.get("step") in steps:
                    revoke(i, line, kind or "go")
            else:  # an analysis gate line (or an older line with gate/launched)
                if g and (set(line.get("inputs_from_runs") or []) & runs or dep_invalid(deps)):
                    grew |= add_version(g, (line.get("record") or {}).get("sha256"))
                if g in inv:
                    for st in line.get("launched") or []:
                        grew |= add("steps", steps, st)
    versions = {n: sorted(v, key=lambda x: (x is None, x or "")) for n, v in inv.items()}
    return {**order, "versions": versions, "revoked_authorizations": revoked}


def dependent_steps(changed, log: list[dict]) -> list[str]:
    """Steps and analyses reached from `changed` (see `dependents`)."""
    d = dependents(changed, log)
    return [*d["steps"], *d["analyses"]]


def _atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=_json_default) + "\n")
    tmp.replace(path)


def _cleared(step: str, since: int, log: list[dict], inv: Mapping[str, set]) -> dict | None:
    """Replacement evidence for an invalid step: after the event (log index `since`), a new
    launch of the step citing no invalid version, and, where the step has review records, a
    new passing `review:<step>` approving no invalid version (review R3 r2 #9)."""
    later = list(enumerate(log))[since:]
    launch = next((i for i, x in later if (x.get("kind") == "launch" or "produces" in x)
                   and x.get("step") == step
                   and not any(_hit(d, inv, exact=True) for d in _deps(x.get("depends_on")))),
                  None)
    if launch is None:
        return None
    reviewed = any(str(x.get("gate")) == f"review:{step}" for x in log)
    review = next((i for i, x in later if str(x.get("gate")) == f"review:{step}"
                   and x.get("outcome") == "pass"
                   and not any(_hit(d, inv, exact=True) for d in _deps(x.get("approves")))),
                  None)
    if reviewed and review is None:
        return None
    return {"launch_line": launch, "review_line": review}


def cumulative(events: list[dict], log: list[dict], current: Mapping[str, Mapping]) -> dict:
    """The cumulative invalidations from every replay event (immutable files) and the gate
    log: invalid analysis versions never clear; an invalid step clears only with replacement
    evidence (`_cleared`), and its runs with it. A no-op replay therefore keeps earlier
    invalidity. `current`: {analysis name: {"sha256", "utc"}} of the current records."""
    inv: dict[str, set] = {}
    wild: dict[str, tuple[str, int]] = {}  # name -> (utc, log_upto) of the latest event
    #                                            invalidating "any version" of it
    step_since: dict[str, tuple[int, str]] = {}
    run_since: dict[str, str] = {}
    revoked: list[dict] = []
    for e in events:
        for n, vs in (e.get("invalid_versions") or {}).items():
            inv.setdefault(n, set()).update(vs)
            if None in vs:
                wild[n] = max(wild.get(n, ("", 0)),
                              (str(e.get("utc", "")), int(e.get("log_upto", 0))))
        for st in e.get("invalid_steps") or []:
            step_since.setdefault(st, (int(e.get("log_upto", 0)), e["event"]))
        for r in e.get("invalid_runs") or []:
            run_since.setdefault(r, e["event"])
        revoked += [x | {"event": e["event"]} for x in e.get("revoked_authorizations") or []
                    if not any(y["line"] == x["line"] for y in revoked)]
    steps, cleared = [], {}
    for st, (since, ev) in step_since.items():
        c = _cleared(st, since, log, inv)
        if c is None:
            steps.append(st)
        else:
            cleared[st] = c | {"event": ev}
    cleared_runs = {r for st, c in cleared.items()
                    for r in (log[c["launch_line"]].get("produces") or [])}
    runs = [r for r in run_since if r not in cleared_runs]
    analyses = []
    for n, vs in inv.items():
        if n.startswith("review:"):  # reviews are revoked authorizations, not analyses
            continue
        cur = current.get(n)
        if cur is not None:
            if cur.get("sha256") in vs or (n in wild and str(cur.get("utc") or "") <= wild[n][0]):
                analyses.append(n)
            continue
        # an analysis recorded elsewhere (e.g. Part 2's): valid again only if the gate log
        # records a newer version of it after the event, not itself invalid
        since = wild.get(n, ("", 0))[1]
        newer = [x for x in log[since:] if x.get("gate") == n
                 and (x.get("record") or {}).get("sha256") not in vs | {None}]
        if not newer:
            analyses.append(n)
    return {"invalid_versions": {n: sorted(v, key=lambda x: (x is None, x or ""))
                                 for n, v in inv.items()},
            "invalid_analyses": sorted(analyses), "invalid_steps": steps,
            "invalid_runs": runs, "cleared_steps": cleared,
            "revoked_authorizations": revoked}


def replay(old_dir: Path | str, new_config: Mapping, root: Path | str,
           gate_log: str = C.GATE_LOG) -> dict:
    """Recompute every analysis recorded in `old_dir` under `new_config`, from the committed
    inputs; diff outcomes; supersede and replace the changed records (prereg §4.7 L269-270).

    Each replay writes an immutable event `<old_dir>/replays/<utc>.json` (what changed, the
    superseded record versions, everything reached from them in the gate log up to now) BEFORE
    publishing the replacement records, then rewrites the cumulative `invalidations.json` from
    every event plus the gate log (`cumulative`). Every file is written atomically (temp file
    and rename); the event comes first, so an interrupted replay never loses invalidity."""
    root, old_dir = Path(root), Path(old_dir)
    if not old_dir.is_absolute():
        old_dir = root / old_dir
    olds = {}
    for p in sorted(old_dir.glob("*.json")):
        rec = json.loads(p.read_text())
        if isinstance(rec, dict) and rec.get("command", {}).get("name") in COMPUTE:
            olds[rec["name"]] = (p, rec)
    order = [n for n in ANALYSES if n in olds]
    changed, news, seeds = {}, {}, {}
    for n in order:
        p, old = olds[n]
        new = compute(n, new_config, root, old["command"].get("args"))
        diffs = diff_outcomes(old, new)
        if diffs:
            changed[n] = diffs
            seeds[n] = {sha256_file(p)}
        if canonical(old) != canonical(new):
            news[n] = new
    log = read_gate_log(root / gate_log)
    dep = dependents(seeds, log)
    now = dt.datetime.now(dt.UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    replaced = {}
    for n, new in news.items():
        p, old = olds[n]
        keep = old_dir / "superseded" / old.get("config_sha256", "unknown")[:12] / p.name
        replaced[n] = {"record": str(p.relative_to(root)),
                       "superseded": str(keep.relative_to(root)),
                       "superseded_sha256": sha256_file(p)}
    event = {
        "event": f"replays/{stamp}.json", "utc": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "code_commit": code_commit(root),
        "old_config_sha256": sorted({r.get("config_sha256") for _, r in olds.values()}),
        "new_config_sha256": config_hash(new_config), "new_config": dict(new_config),
        "changed": [{"analysis": n, **d} for n, ds in changed.items() for d in ds],
        "invalid_versions": {n: [v for v in vs] for n, vs in dep["versions"].items()},
        "invalid_analyses": dep["analyses"], "invalid_steps": dep["steps"],
        "invalid_runs": dep["runs"], "revoked_authorizations": dep["revoked_authorizations"],
        "log_upto": len(log), "replaced": replaced, "gate_log": gate_log,
        "exploratory": new_config["pairs"]["sha256"] != C.PAIRS_SHA256,
    }
    ev_path = old_dir / event["event"]
    if ev_path.exists():
        raise FileExistsError(f"{ev_path}: replay events are immutable")
    _atomic_json(ev_path, event)
    for n, new in news.items():  # publish: keep the superseded version, then replace
        p, _ = olds[n]
        keep = root / replaced[n]["superseded"]
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, keep)
        _atomic_json(p, new)
    events = [json.loads(x.read_text()) for x in sorted((old_dir / "replays").glob("*.json"))]
    current = {}
    for n in ANALYSES:
        p = old_dir / f"{n}.json"
        if p.is_file():
            current[n] = {"sha256": sha256_file(p),
                          "utc": json.loads(p.read_text()).get("utc")}
    cum = cumulative(events, log, current)
    inv = {
        "utc": event["utc"], "code_commit": event["code_commit"],
        "events": [e["event"] for e in events], "latest_event": event["event"],
        "new_config_sha256": event["new_config_sha256"],
        "changed": event["changed"], "replaced": replaced, "gate_log": gate_log,
        "exploratory": event["exploratory"], **cum,
        "outcome": "pass" if not (cum["invalid_steps"] or cum["invalid_analyses"]
                                  or cum["invalid_runs"]) else "fail",
    }
    _atomic_json(old_dir / Path(INVALIDATIONS).name, inv)
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
