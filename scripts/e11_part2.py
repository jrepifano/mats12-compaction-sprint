"""E11 part 2 analysis: the projection-difference and LLM-reader predictors, their gate and the
descriptive readouts (prereg §4.6, §4.7.7, §6 part-2 table; `emx.e11.part2`).

Reads (repo-relative, `emx.e11.contracts`):
  results/E11/plan.json                       `part2: false` -> decision "not run"
  results/E11/part2/pool_manifest.json        the datasets present, their 300 row ids and halves
  results/E11/part2/acts/{vectors,pool}.safetensors (+ index) via emx.e11.acts.load_acts
  results/E11/part2/reader.csv                C.READER_COLUMNS, through emx.e11.reader.load_results
                                              (latest row per key wins; review r1b, finding 4)
  results/E11/part2/reader.meta.json          its recorded pool manifest sha256 must equal the
                                              manifest read here (finding 3)
  data/E11/pool/<ds>.jsonl, results/E11/part2/gen/base_answers.csv   diagnostics only
Writes results/E11/analysis/part2.json: every number, the drops, each prerequisite with pass/fail,
the decision and a provenance block (input sha256s). No API calls, no GPU.
Exit 0 whatever the decision; exit 2 when an input is missing or refused.

    uv run python scripts/e11_part2.py [--root .] [--out results/E11/analysis/part2.json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

from emx.e11 import contracts as C
from emx.e11 import part2 as P
from emx.e11 import reader as R

TAG = "E11 PART2"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit(root: Path) -> str | None:
    import subprocess

    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class Inputs:
    """Records the sha256 of every file read, by repo-relative path."""

    def __init__(self, root: Path):
        self.root, self.shas = root, {}

    def path(self, rel: str, *, required: bool = True) -> Path | None:
        p = self.root / rel
        if not p.exists():
            if required:
                raise FileNotFoundError(f"missing input {rel}")
            return None
        self.shas[rel] = sha256_file(p)
        return p

    def acts(self, name: str):
        from emx.e11.acts import load_acts

        rel = f"{C.P2_ACTS_DIR}/{name}.safetensors"
        self.path(rel)
        for idx in sorted((self.root / C.P2_ACTS_DIR).glob(f"{name}*.index.jsonl")):
            self.shas[str(idx.relative_to(self.root))] = sha256_file(idx)
        return load_acts(self.root / rel)


def load_data_answers(inp: Inputs, expected: dict) -> dict[str, list[str]]:
    out = {}
    for ds, rows in expected.items():
        p = inp.path(f"{C.POOL_DIR}/{ds}.jsonl", required=False)
        if p is None:
            continue
        ans = []
        with open(p) as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    if str(r.get("row_id")) in rows and isinstance(r.get("answer"), str):
                        ans.append(r["answer"])
        out[ds] = ans
    return out


def load_base_answers(inp: Inputs) -> dict[tuple[str, str], list[str]]:
    p = inp.path(f"{C.P2_GEN_DIR}/base_answers.csv", required=False)
    if p is None:
        return {}
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    out: dict[tuple[str, str], list[str]] = {}
    for ds in C.POOL_DATASETS:
        for fr in C.FRAMINGS:
            sel = df[df["condition"] == C.p2base_condition(ds, fr)]["response"].tolist()
            if sel:
                out[(ds, fr)] = sel
    return out


NUMERIC_READER_COLS: tuple[str, ...] = (*(x.lower() for x in C.READER_SCALES), "in_tok",
                                         "out_tok")


def reader_frame(results: dict) -> pd.DataFrame:
    """`R.load_results`'s latest row per (ds, row_id, framing) as a frame in C.READER_COLUMNS
    order: keys, half and status as strings, the scales and token counts numeric (an empty
    scale, as on a missing or error row, is NaN)."""
    df = pd.DataFrame(list(results.values()), columns=list(C.READER_COLUMNS))
    for c in C.READER_COLUMNS:
        if c in NUMERIC_READER_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
        else:
            df[c] = df[c].astype(str)
    return df


def load_reader(inp: Inputs, manifest_rel: str) -> pd.DataFrame:
    """The reader's latest results, after checking that its sidecar's recorded pool manifest
    sha256 is the manifest's being analysed (scores made from another pool are refused)."""
    csv_p = inp.path(C.READER_CSV)
    meta_rel = str(R.meta_path(C.READER_CSV))
    meta = json.loads(inp.path(meta_rel).read_text())
    got = (meta.get("pool_inputs") or {}).get(manifest_rel)
    if got != inp.shas[manifest_rel]:
        raise ValueError(f"{meta_rel}: recorded pool manifest sha256 {got} != {manifest_rel} "
                         f"{inp.shas[manifest_rel]} (reader scores made from another pool)")
    return reader_frame(R.load_results(csv_p))


def run(root: Path) -> dict:
    inp = Inputs(root)
    plan_p = inp.path(C.PLAN_JSON, required=False)
    plan = json.loads(plan_p.read_text()) if plan_p else dict(C.PLAN_DEFAULTS)
    if not plan.get("part2", True):
        body = P.analyze(None, [], None, [], None, {}, part2_run=False)
        body["plan"] = {"source": C.PLAN_JSON, "part2": False}
        return {**body, "provenance": _prov(root, inp)}
    manifest = json.loads(inp.path(C.POOL_MANIFEST).read_text())
    expected, dropped = P.expected_from_manifest(manifest)
    vec_acts, vec_index, _ = inp.acts("vectors")
    pool_acts, pool_index, _ = inp.acts("pool")
    reader = load_reader(inp, C.POOL_MANIFEST)
    body = P.analyze(vec_acts, vec_index, pool_acts, pool_index, reader, expected,
                     n_manifest_datasets=len(expected),
                     data_answers=load_data_answers(inp, expected),
                     base_answers=load_base_answers(inp))
    body["pool"]["datasets_dropped_in_manifest"] = dropped
    body["plan"] = {"source": C.PLAN_JSON if plan_p else "defaults", "part2": True}
    return {**body, "provenance": _prov(root, inp)}


def _prov(root: Path, inp: Inputs) -> dict:
    utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return C.provenance({"pool": C.POOL_SEED}, inp.shas, git_commit(root) or "unknown", utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=C.REPO_ROOT)
    ap.add_argument("--out", default=f"{C.ANALYSIS_DIR}/part2.json")
    a = ap.parse_args(argv)
    try:
        rec = run(a.root)
    except (FileNotFoundError, ValueError, KeyError, RuntimeError) as e:  # RuntimeError: reader
        print(f"{TAG} REFUSED: {e}", file=sys.stderr)
        return 2
    out = a.root / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=False, allow_nan=False) + "\n")
    tmp.replace(out)
    print(f"{TAG} decision: {rec['decision']}")
    if rec.get("failed_prerequisites"):
        print(f"{TAG} failed prerequisites: {', '.join(rec['failed_prerequisites'])}")
    if rec["decision"] != "not run":
        print(f"{TAG} rho_A={rec['rho_A']} rho_B={rec['rho_B']} "
              f"evil split-half cosine={rec['vectors']['split_half_cosine']['evil']} "
              f"drops={len(rec['drops'])}")
    print(f"{TAG} wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
