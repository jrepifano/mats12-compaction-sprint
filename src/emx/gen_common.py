"""E1 answer generation, shared by the vLLM (primary) and Hugging Face (fallback) engines.

Implementation plan §1 "Generation CSV" and U8; prereg §4.5, §4.6, §4.10, §11 amendment A.

- A condition `{cell}@{eval_fmt}` resolves to the stage's base model and tokenizer at their pinned
  revisions (`registry.STAGES`) plus, for a trained cell, the adapter repo and 40-char
  `hub_revision` read from `results/E1/train/{cell}.json` (never `main`).
- Requests: every question of the qset x `SAMPLES[qset][engine]` samples (medical: `cut3` under
  prereg §7 cut 3), prompt text from `emx.formats.render_prompt`, prompt ids from the stage's own
  tokenizer (`add_special_tokens=False`, as in training and `emx.restore`), and one seed per
  request from `contracts.request_seed(condition, qid, sample_idx)`.
- Post-processing (`postprocess`) is the same for both engines and reproduces
  `emx.restore.finish_text`: stop tokens dropped from the text, text cut at the first stop string
  (plain `\\nUser:`), `finish_reason` `stop` or `length`. `n_new_tokens` counts generated tokens
  up to and including the stop: the stop token, or the token that completes a stop string. vLLM
  stops at that token itself; the HF loop cannot stop on strings, so its count is trimmed to the
  same point (the tokens after it are discarded).
- Outputs: the CSV with exactly `GEN_COLUMNS`, a `.meta.json` sidecar with every `GEN_META_KEYS`
  key (plus `csv_sha256`, `n_rows` and run details), and `MANIFEST.<condition>.sha256`
  (`shasum -a 256` format, names relative to the output dir) for `pod_pull_results.sh`. All
  written atomically. A condition whose CSV and meta exist and verify is skipped; one that exists
  but does not verify is refused unless `overwrite`. Verification includes the sampling/template
  contract and the smoke overrides (`expected_contract`), so a smoke output is never reused.

E1b additions (docs/experiments/E1b/implementation-plan.md §1.1-1.2, §1.4; U3), all behind
`--exp E1b` (the default `E1` behaves exactly as before):
- Training records are looked up in `results/E1b/train` first, then `results/E1/train` (E1's
  seed-1 models are re-evaluated on ext48). A checkpoint condition (`{cell}-k{step:04d}@plain`)
  resolves through its parent's record: the `checkpoints` row with that step gives the adapter
  `subfolder` (`ModelSpec.adapter_subfolder`, in `meta()` only when set), the parent gives the
  repo and its 40-char `hub_revision`.
- Question files come from `emx.e1b.contracts.QUESTION_FILES` per qset (`ext48` is generated;
  its sha256 must match the `ext48` block of `data/E1b/manifest.json` before it is read).
- What each condition is asked is a named profile (`PROFILES`: `full`, `timing`, `timing_cut2`,
  `ext48_only`), by default the one `emx.e1b.registry.requests` gives the condition (`--profile
  auto`). The profile name is written to the meta and compared on resume, so a timing output is
  never taken for a full one. Seeds stay `request_seed(condition, qid, sample_idx)`, so a timing
  subset's rows are the first sample indices (and first narrow questions) of a full run.

E3 additions (docs/experiments/E3/implementation-plan.md v2 fix 2, v3 fix 1; prereg §4.9), all
behind `--exp E3` and driven by `scripts/e3_gen.py`: training records from `results/E3/train`;
question files from `emx.e3.contracts.QUESTION_FILES`; what is asked is `SAMPLED_QSETS` (profile
`e3`: general and ext48 x 20, narrow x 1 on the first 100 questions in file order that are not
among the selection file's 40 `medical_qids`). Excluded qids are a `Job` field, applied before
the first-n limit, and listed in the meta. The default output dir is the cells' own
(`emx.e3.contracts.gen_path`: sealed checkpoints under `results/E3/sealed/gen`). E3's resume
contract also carries `requests_sha256` (the exact (qset, qid, sample_idx, seed) list) and, for
narrow, the selection file's `selection_sha256` (E3 code review B7). `git_commit` honours
`EMX_GIT_COMMIT`/`EMX_GIT_DIRTY` (pods have no git checkout; B6). `--set-aside-incomplete`
(E3; scripts/e3_gen.py always passes it, so a restarted pod job resumes, B4) moves an unverified
CSV and/or meta to `<name>.incomplete.<UTC>` and regenerates that (condition, qset).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from emx.contracts import EVAL_SEED, GEN_COLUMNS, GEN_META_KEYS, ConditionId, gen_path
from emx.contracts import request_seed as _request_seed
from emx.e1.registry import FORMATS, SAMPLES, SAMPLING, STAGES
from emx.e1b import contracts as E1B
from emx.e3 import contracts as E3C

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_DIR = ROOT / "data/E1/questions"
TRAIN_DIR = ROOT / "results/E1/train"
# core44 belongs to e1_restore.py; ext48 is E1b's (its file is under data/E1b/questions)
GEN_QSETS: tuple[str, ...] = ("general", "medical", "narrow", "ext48")
E1_QSETS: tuple[str, ...] = ("general", "medical", "narrow")  # E1's default --qsets
EXPS: tuple[str, ...] = ("E1", "E1b", "E3")
E3_PROFILE = "e3"  # E3's one request table, contracts.SAMPLED_QSETS
E1B_TRAIN_DIRS: tuple[Path, ...] = (ROOT / E1B.TRAIN_DIR, TRAIN_DIR)  # E1b first, then E1's
DATA_MANIFEST = ROOT / E1B.DATA_MANIFEST  # E1b's frozen data hashes (ext48, contrast set)
ENGINES: tuple[str, ...] = ("vllm", "hf")
FINISH_REASONS: tuple[str, ...] = ("stop", "length")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


# --- resolution ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """Everything that pins the model behind one condition."""

    condition: str
    cell: str
    stage: str
    eval_fmt: str
    hf_id: str
    model_revision: str
    tokenizer_revision: str
    adapter_repo: str | None
    adapter_revision: str | None
    adapter_subfolder: str | None = None  # E1b checkpoint: `step-NNNN` inside the parent's repo

    def meta(self) -> dict:
        m = {
            "model": self.hf_id,
            "model_revision": self.model_revision,
            "adapter_repo": self.adapter_repo,
            "adapter_revision": self.adapter_revision,
            "tokenizer_revision": self.tokenizer_revision,
        }
        if self.adapter_subfolder is not None:  # E1's sidecars compare exactly as before
            m["adapter_subfolder"] = self.adapter_subfolder
        return m


def _train_record(cell: str, train_dir: Path | Sequence[Path]) -> tuple[Path, dict]:
    """The first `{cell}.json` found in the training-record directories, in order."""
    dirs = [Path(train_dir)] if isinstance(train_dir, str | Path) else [Path(d) for d in train_dir]
    for d in dirs:
        path = d / f"{cell}.json"
        if path.exists():
            return path, json.loads(path.read_text())
    raise FileNotFoundError(f"no training result {cell}.json in {[str(d) for d in dirs]}")


def resolve(condition: str, train_dir: Path | Sequence[Path] = TRAIN_DIR) -> ModelSpec:
    """Parse a condition and pin its base model, tokenizer and adapter.

    `train_dir` may be several directories, searched in order (E1b: `E1B_TRAIN_DIRS`). A
    checkpoint cell resolves through its parent's record (plan §1.1): the `checkpoints` row with
    its step must name a saved `subfolder`; repo and revision are the parent's.
    """
    cond = ConditionId.parse(condition)
    if cond.restore is not None:
        raise ValueError(f"{condition}: restore conditions are generated by e1_restore.py")
    stage, fmt = cond.cell.stage, cond.eval_fmt
    if stage not in FORMATS[fmt]["stages"]:
        raise ValueError(f"{condition}: format {fmt!r} is not defined for stage {stage!r}")
    st = STAGES[stage]
    repo = rev = sub = None
    if not cond.cell.untrained:
        parent = cond.cell.parent
        try:
            path, tr = _train_record(str(parent), train_dir)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"{condition}: {e}") from None
        repo, rev = tr.get("hub_repo"), tr.get("hub_revision")
        if tr.get("cell") != str(parent):
            raise ValueError(f"{path}: cell {tr.get('cell')!r} != {parent}")
        if cond.cell.step is not None:
            rows = [c for c in tr.get("checkpoints") or [] if c.get("step") == cond.cell.step]
            if len(rows) != 1:
                raise ValueError(f"{path}: {len(rows)} checkpoint rows for step {cond.cell.step}")
            sub = rows[0].get("subfolder")
            if not sub:
                raise ValueError(f"{path}: step {cond.cell.step} has no saved adapter")
            if sub != E1B.subfolder(cond.cell.step):  # a mislabeled row never loads another step
                raise ValueError(
                    f"{path}: step {cond.cell.step} subfolder {sub!r} != "
                    f"{E1B.subfolder(cond.cell.step)!r}"
                )
        if not repo:
            raise ValueError(f"{path}: no hub_repo")
        if not rev or not _SHA40.match(rev):
            raise ValueError(f"{path}: hub_revision must be a 40-char sha, got {rev!r}")
    return ModelSpec(
        condition=str(cond),
        cell=str(cond.cell),
        stage=stage,
        eval_fmt=fmt,
        hf_id=st["hf_id"],
        model_revision=st["revision"],
        tokenizer_revision=st["tokenizer_revision"],
        adapter_repo=repo,
        adapter_revision=rev,
        adapter_subfolder=sub,
    )


# --- profiles (E1b: what each condition is asked; plan §1.4) -------------------------------------


def _profiles() -> dict[str, dict[str, tuple[int, int | None]]]:
    from emx.e1b import registry as R

    cut2 = {**R.TIMING, "ext48": (E1B.TIMING_SUBSET_CUT2_EXT48, None)}
    return {"full": R.FULL, "timing": R.TIMING, "timing_cut2": cut2, "ext48_only": R.EXT48_ONLY}


PROFILE_NAMES: tuple[str, ...] = ("full", "timing", "timing_cut2", "ext48_only")


def profile_for(
    condition: str, profile: str = "auto", ext48_cut2: bool = False
) -> tuple[str, dict]:
    """(name, {qset: (samples per question, first n questions or None)}) of one E1b condition.
    `auto` names what `emx.e1b.registry.requests` asks of it; an explicit name (e.g. `timing`
    for a step-0 fallback) is taken as given."""
    table = _profiles()
    if profile != "auto":
        if profile not in table:
            raise ValueError(f"unknown profile {profile!r} (one of {PROFILE_NAMES} or auto)")
        return profile, dict(table[profile])
    from emx.e1b import registry as R

    asked = R.requests(condition, ext48_cut2=ext48_cut2)
    for name in PROFILE_NAMES:
        if table[name] == asked:
            return name, dict(asked)
    raise ValueError(f"{condition}: registry requests {asked} match no profile")


# --- requests ------------------------------------------------------------------------------------


def question_file(qset: str, questions_dir: Path | Mapping[str, Path] = QUESTIONS_DIR) -> Path:
    """`{questions_dir}/{qset}.jsonl`, or the qset's entry when given a qset -> file mapping."""
    if isinstance(questions_dir, Mapping):
        return Path(questions_dir[qset])
    return Path(questions_dir) / f"{qset}.jsonl"


def load_questions(
    qset: str, questions_dir: Path | Mapping[str, Path] = QUESTIONS_DIR
) -> list[dict]:
    if qset not in GEN_QSETS:
        raise ValueError(f"qset {qset!r} is not generated here (one of {GEN_QSETS})")
    path = question_file(qset, questions_dir)
    if qset == "ext48":  # E1b's generated file: its frozen hash, before any row is read
        verify_data_file(path, "ext48")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    qids = [r["qid"] for r in rows]
    if len(set(qids)) != len(qids):
        raise ValueError(f"{path}: duplicate qids")
    bad = [r["qid"] for r in rows if r.get("qset") != qset]
    if bad:
        raise ValueError(f"{path}: rows with the wrong qset: {bad[:3]}")
    return rows


def n_samples(qset: str, engine: str, medical_cut: bool = False) -> int:
    """Samples per question (prereg §4.6; prereg §7 cut 3 drops medical to `cut3`)."""
    if engine not in ENGINES:
        raise ValueError(engine)
    if qset == "medical" and medical_cut:
        return SAMPLES["medical"]["cut3"]
    return SAMPLES[qset][engine]


@dataclass(frozen=True)
class Request:
    condition: str
    cell: str
    eval_fmt: str
    qset: str
    qid: str
    question: str
    sample_idx: int
    seed: int
    prompt_text: str
    prompt_ids: tuple[int, ...]


def build_requests(
    spec: ModelSpec,
    qset: str,
    tokenizer,
    n: int,
    questions_dir: Path | Mapping[str, Path] = QUESTIONS_DIR,
    limit_questions: int | None = None,
    exclude_qids: Sequence[str] = (),
) -> list[Request]:
    """Questions in file order (without `exclude_qids`, E3), samples 0..n-1 within each; one prompt
    render per question."""
    from emx.formats import render_prompt

    qs = select_questions(qset, questions_dir, limit_questions, exclude_qids)
    out = []
    for q in qs:
        text = render_prompt(spec.eval_fmt, q["question"], tokenizer)
        ids = tuple(tokenizer(text, add_special_tokens=False)["input_ids"])
        for i in range(n):
            out.append(
                Request(
                    condition=spec.condition,
                    cell=spec.cell,
                    eval_fmt=spec.eval_fmt,
                    qset=qset,
                    qid=q["qid"],
                    question=q["question"],
                    sample_idx=i,
                    seed=_request_seed(spec.condition, q["qid"], i),
                    prompt_text=text,
                    prompt_ids=ids,
                )
            )
    return out


def select_questions(
    qset: str,
    questions_dir: Path | Mapping[str, Path] = QUESTIONS_DIR,
    limit_questions: int | None = None,
    exclude_qids: Sequence[str] = (),
) -> list[dict]:
    """The questions `build_requests` asks, in file order: without `exclude_qids`, then first-n."""
    qs = load_questions(qset, questions_dir)
    if exclude_qids:
        drop = set(exclude_qids)
        qs = [q for q in qs if q["qid"] not in drop]
    if limit_questions:
        qs = qs[:limit_questions]
    return qs


def requests_sha256(condition: str, qset: str, qids: Sequence[str], n: int) -> str:
    """Hash of the exact request list (qset, qid, sample_idx, seed), in generation order, as
    `build_requests` makes it; needs no tokenizer. E3 binds its resume contract to it (code
    review B7)."""
    keys = [[qset, q, i, _request_seed(condition, q, i)] for q in qids for i in range(n)]
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()


# --- post-processing -----------------------------------------------------------------------------


def _decode(tokenizer, ids: Sequence[int], stop_ids: set[int]) -> str:
    return tokenizer.decode([t for t in ids if t not in stop_ids], skip_special_tokens=True)


def postprocess(tokenizer, token_ids: Sequence[int], reason: str, fmt: str) -> tuple[str, str, int]:
    """(response, finish_reason, n_new_tokens) from an engine's generated ids and finish reason.

    Same text and reason as `emx.restore.finish_text`; `n_new_tokens` counts up to and including
    the stop token or the token that completes the first stop string.
    """
    if reason not in FINISH_REASONS:
        raise ValueError(f"unexpected finish reason {reason!r}")
    spec = FORMATS[fmt]
    stop_ids = {int(t) for t in spec["stop_token_ids"]}
    ids = [int(t) for t in token_ids]
    for k, t in enumerate(ids):
        if t in stop_ids:
            ids, reason = ids[: k + 1], "stop"
            break
    text = _decode(tokenizer, ids, stop_ids)
    for s in spec["stop_strings"]:
        if s in text:
            # smallest prefix whose text contains the stop string (containment is monotone)
            lo, hi = 1, len(ids)
            while lo < hi:
                mid = (lo + hi) // 2
                if s in _decode(tokenizer, ids[:mid], stop_ids):
                    hi = mid
                else:
                    lo = mid + 1
            ids, text, reason = ids[:lo], text[: text.index(s)], "stop"
    return text, reason, len(ids)


def row(req: Request, response: str, finish_reason: str, n_new_tokens: int) -> dict:
    return {
        "condition": req.condition,
        "cell": req.cell,
        "eval_fmt": req.eval_fmt,
        "qset": req.qset,
        "qid": req.qid,
        "question": req.question,
        "sample_idx": req.sample_idx,
        "seed": req.seed,
        "prompt_text": req.prompt_text,
        "response": response,
        "finish_reason": finish_reason,
        "n_new_tokens": n_new_tokens,
    }


# --- metadata ------------------------------------------------------------------------------------


def sampling_meta(fmt: str, n: int, max_new_tokens: int, engine_params: dict) -> dict:
    """The sampling record: registry values, per-format stops, and what the engine was given."""
    spec = FORMATS[fmt]
    return {
        **{k: SAMPLING[k] for k in ("temperature", "top_p", "top_k")},
        "max_new_tokens": max_new_tokens,
        "samples_per_question": n,
        "seed_rule": f"request_seed(condition, qid, sample_idx, base={EVAL_SEED})",
        "stop_strings": list(spec["stop_strings"]),
        "stop_token_ids": list(spec["stop_token_ids"]),
        "engine_params": engine_params,
    }


def git_commit() -> str:
    """The code commit: `EMX_GIT_COMMIT` (+ "-dirty" when `EMX_GIT_DIRTY=1`), which pod_push.sh
    writes to the pod's .env because a pod has no git checkout (E3 code review B6); otherwise
    `git rev-parse HEAD` (+ "-dirty" for modified tracked files); "unknown" if neither works."""
    env_sha = os.environ.get("EMX_GIT_COMMIT")
    if env_sha:
        return env_sha + ("-dirty" if os.environ.get("EMX_GIT_DIRTY") == "1" else "")
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"], cwd=ROOT, capture_output=True, check=False
        ).returncode
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def gpu_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except ImportError:  # pragma: no cover
        return "unknown"


# --- output --------------------------------------------------------------------------------------


def out_paths(out_dir: Path, condition: str, qset: str) -> tuple[Path, Path]:
    name = Path(gen_path(condition, qset)).name
    csv_path = Path(out_dir) / name
    return csv_path, csv_path.with_suffix(".meta.json")


def manifest_path(out_dir: Path, condition: str) -> Path:
    return Path(out_dir) / f"MANIFEST.{condition}.sha256"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_data_file(path: str | Path, block: str, manifest: str | Path | None = None) -> str:
    """Refuse an E1b data file whose sha256 is not its frozen `{block}.output.sha256` in the data
    manifest (`DATA_MANIFEST` unless given; prereg §4.5, §4.8): a changed file with the same ids
    must not pass. Returns the sha256."""
    man = Path(manifest if manifest is not None else DATA_MANIFEST)
    if not man.exists():
        raise FileNotFoundError(f"{path}: data manifest {man} missing")
    want = (json.loads(man.read_text()).get(block) or {}).get("output", {}).get("sha256")
    if not want:
        raise ValueError(f"{path}: no {block}.output.sha256 in {man}")
    got = sha256_file(Path(path))
    if got != want:
        raise ValueError(f"{path}: sha256 {got[:12]} != {man} {block} {want[:12]}")
    return got


def _atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_outputs(out_dir: Path, spec: ModelSpec, qset: str, rows: list[dict], meta: dict) -> Path:
    """CSV (exactly GEN_COLUMNS), then meta (with the CSV's sha256), then the manifest."""
    missing = [k for k in GEN_META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"meta lacks {missing}")
    csv_path, meta_path = out_paths(out_dir, spec.condition, qset)

    def _csv(fh):
        w = csv.DictWriter(fh, fieldnames=list(GEN_COLUMNS), extrasaction="raise")
        w.writeheader()
        w.writerows(rows)

    _atomic_write(csv_path, _csv)
    full = {
        "condition": spec.condition,
        "qset": qset,
        **meta,
        "n_rows": len(rows),
        "csv_sha256": sha256_file(csv_path),
    }
    _atomic_write(meta_path, lambda fh: fh.write(json.dumps(full, indent=2) + "\n"))
    write_manifest(out_dir, spec.condition)
    return csv_path


def write_manifest(out_dir: Path, condition: str) -> Path:
    """`shasum -a 256` lines for every CSV and meta of this condition in `out_dir`."""
    out_dir = Path(out_dir)
    files = sorted(
        p
        for p in out_dir.glob(f"{glob_escape(condition)}__*")
        if p.name.endswith((".csv", ".meta.json"))
    )
    body = "".join(f"{sha256_file(p)}  {p.name}\n" for p in files)
    path = manifest_path(out_dir, condition)
    _atomic_write(path, lambda fh: fh.write(body))
    return path


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


# Sampling keys compared on resume. `engine_params` is excluded: it records engine plumbing
# (batch size, vLLM kwargs) derived from these values, not a separate sampling choice.
CONTRACT_SAMPLING_KEYS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "max_new_tokens",
    "samples_per_question",
    "seed_rule",
    "stop_strings",
    "stop_token_ids",
)


def expected_contract(
    spec: ModelSpec, n: int, max_new_tokens: int, run_overrides: dict, profile: str | None = None
) -> dict:
    """What this run would write for the sampling/template contract (compared on resume). E1b
    runs add their profile name; E1's contract has no `profile` key, as before."""
    from emx.formats import template_sha256

    sampling = sampling_meta(spec.eval_fmt, n, max_new_tokens, {})
    out = {
        "template_sha256": template_sha256(spec.eval_fmt),
        "sampling": {k: sampling[k] for k in CONTRACT_SAMPLING_KEYS},
        "overrides": dict(run_overrides),
    }
    if profile is not None:
        out["profile"] = profile
    return out


# E3's resume contract also binds the selection file (narrow's medical exclusions) and the exact
# request list (code review B7): 100 requests to other questions are never taken for these.
E3_CONTRACT_KEYS: tuple[str, ...] = ("selection_sha256", "requests_sha256")


def contract_problems(meta: dict, contract: dict) -> list[str]:
    """Differences between a saved meta and `expected_contract` (empty if they match)."""
    problems = []
    if meta.get("template_sha256") != contract["template_sha256"]:
        problems.append(
            f"template_sha256 {meta.get('template_sha256')!r} != {contract['template_sha256']!r}"
        )
    saved = meta.get("sampling") if isinstance(meta.get("sampling"), dict) else {}
    for k, v in contract["sampling"].items():
        if saved.get(k, "<missing>") != v:
            problems.append(f"sampling.{k} {saved.get(k, '<missing>')!r} != {v!r}")
    if (meta.get("overrides") or {}) != contract["overrides"]:
        problems.append(f"overrides {meta.get('overrides')!r} != {contract['overrides']!r}")
    if meta.get("profile") != contract.get("profile"):  # both None for E1
        problems.append(f"profile {meta.get('profile')!r} != {contract.get('profile')!r}")
    for k in E3_CONTRACT_KEYS:  # E3 only: absent from E1's and E1b's contracts
        if k in contract and meta.get(k) != contract[k]:
            problems.append(f"{k} {meta.get(k)!r} != {contract[k]!r}")
    return problems


def existing_ok(
    out_dir: Path,
    spec: ModelSpec,
    qset: str,
    n_rows: int,
    engine: str,
    overwrite: bool,
    contract: dict | None = None,
    set_aside: bool = False,
) -> bool:
    """True if this (condition, qset) is already done and verifies; False if absent (or refused
    but `overwrite`). Raises on a present-but-unverified output unless `overwrite`.

    Verification covers the CSV hash, columns and row count, the model/tokenizer/adapter pins, the
    engine and, when `contract` (from `expected_contract`) is given, the saved sampling, template
    hash and smoke overrides: a smoke output is never reused as a full run, or vice versa.
    """
    csv_path, meta_path = out_paths(out_dir, spec.condition, qset)
    if not csv_path.exists() and not meta_path.exists():
        return False
    problems = []
    if not csv_path.exists() or not meta_path.exists():
        problems.append("CSV or meta missing")
    else:
        meta = json.loads(meta_path.read_text())
        problems += [f"meta lacks {k}" for k in GEN_META_KEYS if k not in meta]
        if meta.get("csv_sha256") != sha256_file(csv_path):
            problems.append("CSV sha256 differs from meta")
        with open(csv_path, newline="") as fh:
            r = csv.reader(fh)
            header = next(r, [])
            got = sum(1 for _ in r)
        if tuple(header) != GEN_COLUMNS:
            problems.append("CSV columns differ from GEN_COLUMNS")
        if got != n_rows or meta.get("n_rows") != n_rows:
            problems.append(f"{got} rows, expected {n_rows}")
        for k, v in spec.meta().items():
            if meta.get(k) != v:
                problems.append(f"{k} {meta.get(k)!r} != {v!r}")
        if meta.get("engine") != engine:
            problems.append(f"engine {meta.get('engine')!r} != {engine!r}")
        if meta.get("condition", spec.condition) != spec.condition:
            problems.append(f"condition {meta.get('condition')!r} != {spec.condition!r}")
        if meta.get("qset", qset) != qset:
            problems.append(f"qset {meta.get('qset')!r} != {qset!r}")
        if contract is not None:
            problems += contract_problems(meta, contract)
    if not problems:
        return True
    if set_aside:  # E3 (code review B4): keep the unverified files, then regenerate
        stamp = utc().replace("-", "").replace(":", "")
        for p in (csv_path, meta_path):
            if p.exists():
                aside, k = p.with_name(f"{p.name}.incomplete.{stamp}"), 1
                while aside.exists():  # never replace an earlier set-aside output
                    k += 1
                    aside = p.with_name(f"{p.name}.incomplete.{stamp}.{k}")
                p.rename(aside)
                print(f"set aside {p} -> {aside.name}: {problems}")
        return False
    if overwrite:
        return False
    raise RuntimeError(f"{csv_path}: existing output does not verify: {problems}; --overwrite")


# --- CLI -----------------------------------------------------------------------------------------


def arg_parser(description: str, default_out: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawTextHelpFormatter
    )
    p.add_argument("--conditions", nargs="+", required=True, help="condition ids, e.g. X@chat")
    p.add_argument("--exp", choices=EXPS, default="E1", help="E1 (default) or E1b (never env)")
    p.add_argument(
        "--qsets", nargs="+", default=None, choices=GEN_QSETS,
        help=f"E1: default {' '.join(E1_QSETS)}; E1b: a filter on each condition's profile",
    )
    p.add_argument(
        "--profile", choices=("auto", *PROFILE_NAMES), default=None,
        help="E1b only (default auto: emx.e1b.registry.requests); recorded in meta",
    )
    p.add_argument("--ext48-cut2", action="store_true", help="E1b prereg §7 cut 2 (auto profile)")
    p.add_argument("--out-dir", default=None, help=f"default {default_out} (E1b: {E1B.GEN_DIR})")
    p.add_argument(
        "--train-dir", nargs="+", default=None,
        help="training-record dirs, searched in order (default by --exp)",
    )
    p.add_argument("--questions-dir", default=None, help="dir of {qset}.jsonl (default by --exp)")
    p.add_argument("--medical-cut", action="store_true", help="prereg §7 cut 3: medical x 10")
    p.add_argument("--overwrite", action="store_true", help="regenerate unverified outputs")
    p.add_argument(
        "--set-aside-incomplete", action="store_true",
        help="E3 only: move unverified outputs to <name>.incomplete.<UTC>, then regenerate",
    )
    p.add_argument("--samples", type=int, default=None, help="smoke/check override of samples")
    p.add_argument("--limit-questions", type=int, default=None, help="smoke: first N questions")
    p.add_argument("--max-new-tokens", type=int, default=None, help="smoke override (600)")
    p.add_argument(
        "--selection-file", default=None,
        help=f"E3 only: narrow excludes its medical_qids (default {E3C.SELECTION_FILE})",
    )
    p.set_defaults(_default_out=default_out)
    return p


def exp_of(args) -> str:
    exp = getattr(args, "exp", None) or "E1"
    if exp not in EXPS:
        raise ValueError(f"unknown exp {exp!r}")
    if exp in ("E1", "E3") and (
        getattr(args, "profile", None) or getattr(args, "ext48_cut2", False)
    ):
        raise ValueError("--profile and --ext48-cut2 are E1b-only")
    if exp != "E3" and getattr(args, "selection_file", None):
        raise ValueError("--selection-file is E3-only")
    if exp != "E3" and getattr(args, "set_aside_incomplete", False):
        raise ValueError("--set-aside-incomplete is E3-only")
    return exp


def out_dir_of(args) -> Path:
    if getattr(args, "out_dir", None):
        return Path(args.out_dir)
    if exp_of(args) == "E1b":
        return ROOT / E1B.GEN_DIR
    if exp_of(args) == "E3":  # the cells' own dir: one for every condition, or refuse
        dirs = {
            Path(E3C.gen_path(str(ConditionId.parse(c).cell), "x")).parent
            for c in args.conditions
        }
        if len(dirs) != 1:
            raise ValueError(f"E3 conditions span output dirs {sorted(map(str, dirs))}")
        return ROOT / dirs.pop()
    return Path(args._default_out)


def train_dirs_of(args) -> list[Path]:
    d = getattr(args, "train_dir", None)
    if d:
        return [Path(x) for x in ([d] if isinstance(d, str | Path) else d)]
    if exp_of(args) == "E3":
        return [ROOT / E3C.TRAIN_DIR]
    return list(E1B_TRAIN_DIRS) if exp_of(args) == "E1b" else [TRAIN_DIR]


def questions_of(args) -> Path | dict[str, Path]:
    """`--questions-dir`, else E1's question dir, or E1b's per-qset files (`QUESTION_FILES`)."""
    if getattr(args, "questions_dir", None):
        return Path(args.questions_dir)
    if exp_of(args) == "E1b":
        return {q: ROOT / f for q, f in E1B.QUESTION_FILES.items()}
    if exp_of(args) == "E3":
        return {q: ROOT / f for q, f in E3C.QUESTION_FILES.items()}
    return QUESTIONS_DIR


def overrides(args) -> dict:
    """Non-default knobs, recorded in meta so a smoke output is never mistaken for a real one."""
    o = {
        k: getattr(args, k)
        for k in ("samples", "limit_questions", "max_new_tokens")
        if getattr(args, k, None) is not None
    }
    if getattr(args, "medical_cut", False):
        o["medical_cut"] = True
    return o


def job_extra(job: Job, args) -> dict:
    """Meta extras of one job: the overrides, and (E1b) its profile name."""
    extra = {"overrides": overrides(args)}
    if job.profile is not None:
        extra["profile"] = job.profile
    if job.exclude_qids:  # E3 narrow: the selection file's medical qids
        extra["excluded_qids"] = sorted(job.exclude_qids)
    for k in E3_CONTRACT_KEYS:  # E3: compared on resume (contract_problems)
        if getattr(job, k) is not None:
            extra[k] = getattr(job, k)
    return extra


# --- job planning --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    spec: ModelSpec
    qset: str
    n: int  # samples per question
    n_questions: int
    limit_questions: int | None = None  # first-n questions (profile and smoke limit combined)
    profile: str | None = None  # E1b profile name, recorded in meta
    exclude_qids: tuple[str, ...] = ()  # E3 narrow: dropped before the first-n limit
    selection_sha256: str | None = None  # E3 narrow: the selection file the exclusions came from
    requests_sha256: str | None = None  # E3: `requests_sha256` of the job's request list


def _min_limit(*limits: int | None) -> int | None:
    vals = [x for x in limits if x]
    return min(vals) if vals else None


def _asked(spec: ModelSpec, args, engine: str) -> tuple[str | None, list[tuple]]:
    """(profile name, [(qset, samples per question, first-n limit)]) before smoke overrides."""
    if exp_of(args) == "E1":
        qsets = args.qsets or list(E1_QSETS)
        return None, [(q, n_samples(q, engine, args.medical_cut), None) for q in qsets]
    if exp_of(args) == "E3":
        return E3_PROFILE, [
            (q, *E3C.SAMPLED_QSETS[q])
            for q in GEN_QSETS
            if q in E3C.SAMPLED_QSETS and (not args.qsets or q in args.qsets)
        ]
    name, table = profile_for(spec.condition, args.profile or "auto", args.ext48_cut2)
    out = []
    for q in GEN_QSETS:  # fixed qset order
        if q not in table or (args.qsets and q not in args.qsets):
            continue
        n, first = table[q]
        if q == "medical" and args.medical_cut:
            n = SAMPLES["medical"]["cut3"]
        out.append((q, n, first))
    return name, out


def e3_selection_path(args) -> Path:
    return Path(getattr(args, "selection_file", None) or ROOT / E3C.SELECTION_FILE)


def e3_excluded(qset: str, args) -> tuple[str, ...]:
    """E3: the 40 original medical-pair qids (selection file, never the retained pairs) for the
    narrow qset; () otherwise and for every other experiment."""
    if exp_of(args) != "E3" or qset != "narrow":
        return ()
    path = e3_selection_path(args)
    qids = json.loads(path.read_text())["medical_qids"]
    if len(qids) != E3C.N_MEDICAL or len(set(qids)) != len(qids):
        raise ValueError(f"{path}: medical_qids must be {E3C.N_MEDICAL} distinct qids")
    return tuple(sorted(qids))


def plan_jobs(args, engine: str) -> list[Job]:
    """Every (condition, qset) still to generate, grouped by stage in first-seen order; verified
    outputs are skipped (idempotent), unverified ones refused unless `--overwrite`."""
    specs = [resolve(c, train_dirs_of(args)) for c in args.conditions]
    max_new = args.max_new_tokens or SAMPLING["max_new_tokens"]
    run_overrides = overrides(args)
    questions = questions_of(args)
    out = out_dir_of(args)
    stages = list(dict.fromkeys(s.stage for s in specs))
    jobs = []
    for stage in stages:
        for spec in (s for s in specs if s.stage == stage):
            profile, asked = _asked(spec, args, engine)
            for qset, n_profile, first in asked:
                n = args.samples or n_profile
                limit = _min_limit(first, args.limit_questions)
                excl = e3_excluded(qset, args)
                qs = select_questions(qset, questions, limit, excl)
                nq = len(qs)
                contract = expected_contract(spec, n, max_new, run_overrides, profile)
                sel_sha = req_sha = None
                if exp_of(args) == "E3":  # B7: the exclusions' source and the exact requests
                    req_sha = requests_sha256(spec.condition, qset, [q["qid"] for q in qs], n)
                    contract["requests_sha256"] = req_sha
                    if excl:
                        sel_sha = sha256_file(e3_selection_path(args))
                        contract["selection_sha256"] = sel_sha
                if existing_ok(out, spec, qset, n * nq, engine, args.overwrite, contract,
                               getattr(args, "set_aside_incomplete", False)):
                    print(f"skip {spec.condition} {qset}: done and verified")
                    continue
                jobs.append(Job(spec, qset, n, nq, limit, profile, excl, sel_sha, req_sha))
    return jobs


def job_requests(job: Job, tokenizer, args) -> list[Request]:
    """The requests of one planned job (its questions source and first-n limit)."""
    limit = job.limit_questions if job.limit_questions is not None else args.limit_questions
    return build_requests(
        job.spec, job.qset, tokenizer, job.n, questions_of(args), limit, job.exclude_qids
    )


def build_meta(
    spec: ModelSpec,
    n: int,
    max_new_tokens: int,
    engine: str,
    engine_version: str,
    engine_params: dict,
    extra: dict | None = None,
) -> dict:
    from emx.formats import template_sha256

    return {
        **spec.meta(),
        "template_sha256": template_sha256(spec.eval_fmt),
        "sampling": sampling_meta(spec.eval_fmt, n, max_new_tokens, engine_params),
        "engine": engine,
        "engine_version": engine_version,
        "gpu": gpu_name(),
        "utc": utc(),
        "git_commit": git_commit(),
        **(extra or {}),
    }
