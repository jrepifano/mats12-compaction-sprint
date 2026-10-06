"""E1b step-0 import: assemble `{stage}-untrained@plain` timing rows for base, SFT and Instruct from
E1's generations and luna scores, after checking the ORIGINAL full E1 files (implementation plan
§1.1, §1.7; prereg §4.4, §4.7 secondary 3; U3).

  uv run python scripts/e1b_step0_import.py [--stages base sft inst] [--with-ext48] [--check-only]
      -> results/E1b/step0/{stage}-untrained@plain__{general,narrow[,ext48]}.csv
         results/E1b/step0/import.json, results/E1b/MANIFEST.step0-sources.sha256

Checks, per stage and question set (any failure: exit 1, listing every failed check; the stage's
files are not written, and the main session decides the fallback, regeneration on the pod):
1. hash -- each E1 generation CSV and sidecar traces through E1's committed part manifests
   (`results/E1/MANIFEST.part-*.sha256`, which list per-condition `gen/MANIFEST.{cond}.sha256`)
   to a matching hash; each judged CSV and its luna signatures match the `inputs` block of
   `results/E1/analysis/{cond}.json`. The ext48 part (`--with-ext48`) traces E1b's own files
   through `MANIFEST.reeval.sha256` (generation) and `MANIFEST.judged.sha256` (judged, sigs).
2. contract -- the sidecar's model/tokenizer/adapter pins, `template_sha256`, engine and engine
   version, and every `CONTRACT_SAMPLING_KEYS` value equal what E1b's generation of the condition
   would write (`gen_common.expected_contract`), with the source's own sample count: E1's 100
   (general) or 1 (narrow); E1b's re-evaluation 20 (ext48, profile `ext48_only`). No overrides.
3. rows -- every subset key is present once, in a CSV with no duplicate keys and the sidecar's
   row count; question and prompt text match the question file; the judged CSV has the same keys
   in the same order.
4. seed -- every extracted row's seed is `request_seed(condition, qid, sample_idx)`.
5. judge -- each luna signature (`*.{metric}__luna.sig.json`) equals `judge_run.expected_signature`
   for the luna preset on the judge-identity fields (column, metric, judge, prompt file and hash,
   model, scoring mode, settings) and signs this very generation CSV (sha and rows); every
   extracted row has a `scored` or `none` status.
("rows" is a completeness check the plan's four imply; it is reported separately.)

Extracted: general sample indices 0-19 (all 8 questions), the first 100 narrow questions in
`data/E1/questions/narrow.jsonl` file order, and (`--with-ext48`) ext48 indices 0-9, in the order a
fresh timing run would write them. Columns: `GEN_COLUMNS` plus luna's score and status columns
for aligned, coherent and medcorrect, as present; values are copied as text.

The manifest lists, with paths relative to the repo root (`shasum -a 256 -c` from the root), every
source file, every manifest or analysis file a hash was checked against, the extracted files and
`import.json` (the per-check report with each source's anchor chain).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx import gen_common as G
from emx.contracts import (
    GEN_COLUMNS,
    KEY_COLUMNS,
    gen_path,
    judged_path,
    request_seed,
    score_column,
    status_column,
)
from emx.e1b import contracts as C
from emx.e1b import registry as R

FMT = "plain"
LUNA = "luna"
OUT_METRICS: tuple[str, ...] = ("aligned", "coherent", "medcorrect")
LUNA_METRICS: dict[str, tuple[str, ...]] = {
    "general": ("aligned", "coherent"),
    "narrow": ("coherent", "medcorrect"),
    "ext48": ("aligned", "coherent"),
}
SIG_IDENTITY: tuple[str, ...] = (
    "column", "metric", "judge", "prompt_file", "prompt_sha256", "judge_model", "scoring_mode",
    "settings",
)
CHECKS: tuple[str, ...] = ("hash", "contract", "rows", "seed", "judge")
OK_STATUSES = ("scored", "none")


@dataclass(frozen=True)
class Settings:
    """What is extracted and what the sources must hold (plan §1.1); tests shrink it."""

    subset: dict = field(
        default_factory=lambda: {q: C.TIMING_SUBSET[q] for q in ("general", "narrow")}
    )
    e1_samples: dict = field(default_factory=lambda: {"general": 100, "narrow": 1})
    ext48_subset: tuple = C.TIMING_SUBSET["ext48"]
    ext48_samples: int = R.EXT48_ONLY["ext48"][0]
    engine: str = "vllm"
    engine_version: str | None = None  # None: "vllm <uv.lock version>"


def locked_engine_version(root: Path) -> str:
    """`vllm X.Y.Z` as pinned in uv.lock: what E1b's pod generation would record."""
    text = (root / "uv.lock").read_text()
    m = re.search(r'\[\[package\]\]\nname = "vllm"\nversion = "([^"]+)"', text)
    if not m:
        raise ValueError("vllm is not pinned in uv.lock")
    return f"vllm {m[1]}"


# --- manifests -----------------------------------------------------------------------------------


def read_manifest(path: Path) -> dict[str, str]:
    """`shasum -a 256` lines -> {name: sha}."""
    out = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        sha, name = line.split(None, 1)
        out[name.lstrip("*").strip()] = sha
    return out


def trace(results: Path, anchors: list[Path], target: str, sha: str) -> tuple[list[str], str]:
    """Trace `target` (relative to `results`) with hash `sha` through the anchor manifests:
    listed directly, or through a listed per-condition manifest whose own hash matches.
    Returns (anchor chain, '') on success, ([], diagnostics) otherwise."""
    notes = []
    tdir, tname = str(Path(target).parent), Path(target).name
    for a in anchors:
        if not a.exists():
            notes.append(f"{a.name} missing")
            continue
        entries = read_manifest(a)
        if target in entries:
            if entries[target] == sha:
                return [a.name], ""
            notes.append(f"{a.name} lists {target} as {entries[target][:12]}, file is {sha[:12]}")
        for name, msha in entries.items():
            if str(Path(name).parent) != tdir or not Path(name).name.startswith("MANIFEST."):
                continue
            sub = results / name
            if not sub.exists():
                continue
            sub_entries = read_manifest(sub)
            if tname not in sub_entries:
                continue
            got = G.sha256_file(sub)
            if got != msha:
                notes.append(f"{a.name} lists {name} as {msha[:12]}, file is {got[:12]}")
                continue
            if sub_entries[tname] == sha:
                return [a.name, name], ""
            notes.append(f"{name} lists {tname} as {sub_entries[tname][:12]}, file is {sha[:12]}")
    return [], "; ".join(notes) or f"{target} is in no anchor manifest"


# --- one source part -----------------------------------------------------------------------------


@dataclass
class Part:
    stage: str
    qset: str
    cond: str
    origin: str  # E1 or E1b
    gen_csv: Path
    meta: Path
    judged_csv: Path
    problems: dict = field(default_factory=lambda: {c: [] for c in CHECKS})
    anchors: dict = field(default_factory=dict)  # repo-relative source -> anchor chain
    anchor_files: set = field(default_factory=set)
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(self.problems.values())

    def sig(self, metric: str) -> Path:
        from emx.judge_run import sig_path

        return sig_path(self.judged_csv, metric, LUNA)


def _rel(root: Path, p: Path) -> str:
    return str(Path(p).resolve().relative_to(root.resolve()))


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _questions(root: Path, qset: str) -> list[dict]:
    rel = C.QUESTION_FILES[qset]
    return [json.loads(x) for x in (root / rel).read_text().splitlines() if x.strip()]


def check_hashes(root: Path, part: Part) -> None:
    bad = part.problems["hash"]
    results = root / ("results/E1" if part.origin == "E1" else C.RESULTS)
    gen_anchor = (
        sorted(results.glob("MANIFEST.part-*.sha256"))
        if part.origin == "E1"
        else [root / C.part_manifest_path("reeval")]
    )
    analysis = results / "analysis" / f"{part.cond}.json"
    for f, key in ((part.gen_csv, "gen_sha256"), (part.meta, "meta_sha256")):
        chain, why = trace(results, gen_anchor, _rel(results, f), G.sha256_file(f))
        if chain:
            part.anchors[_rel(root, f)] = chain
            part.anchor_files.update(results / c for c in chain)
            continue
        # Second anchor for E1 sources (main session, 2026-10-04): E1's committed analysis record
        # of the condition. E1's Instruct untrained files were overwritten by a pod regeneration
        # and restored from staging (logs/E1-report.md), so the part manifests list the
        # regeneration's hashes while the analysis record lists the restored originals E1 used.
        if part.origin == "E1" and analysis.exists():
            block = json.loads(analysis.read_text())["inputs"]["files"].get(part.qset, {})
            if block.get(key) == G.sha256_file(f):
                part.anchors[_rel(root, f)] = [f"analysis/{analysis.name}"]
                part.anchor_files.add(analysis)
                continue
        bad.append(f"{f.name}: {why}")
    meta = json.loads(part.meta.read_text())
    if meta.get("csv_sha256") != G.sha256_file(part.gen_csv):
        bad.append(f"{part.meta.name}: csv_sha256 differs from the CSV")
    sigs = [part.sig(m) for m in LUNA_METRICS[part.qset] if part.sig(m).exists()]
    if part.origin == "E1":
        if not analysis.exists():
            bad.append(f"{analysis.name} missing")
            return
        block = json.loads(analysis.read_text())["inputs"]["files"].get(part.qset, {})
        want = {
            part.judged_csv: block.get("judged_sha256"),
            **{s: block.get("sig_sha256", {}).get(s.name.split(".")[-3]) for s in sigs},
        }
        for f, sha in want.items():
            if sha != G.sha256_file(f):
                bad.append(f"{f.name}: sha differs from {analysis.name} inputs ({sha!r:.14})")
            else:
                part.anchors[_rel(root, f)] = [f"analysis/{analysis.name}"]
                part.anchor_files.add(analysis)
    else:
        anchor = [root / C.part_manifest_path("judged")]
        for f in (part.judged_csv, *sigs):
            chain, why = trace(results, anchor, _rel(results, f), G.sha256_file(f))
            if chain:
                part.anchors[_rel(root, f)] = chain
                part.anchor_files.update(results / c for c in chain)
            else:
                bad.append(f"{f.name}: {why}")


def check_contract(root: Path, part: Part, st: Settings) -> None:
    bad = part.problems["contract"]
    meta = json.loads(part.meta.read_text())
    spec = G.resolve(part.cond, G.E1B_TRAIN_DIRS)  # untrained: no record is read
    if part.origin == "E1":
        n, profile = st.e1_samples[part.qset], None
    else:
        n, profile = st.ext48_samples, "ext48_only"
    contract = G.expected_contract(spec, n, G.SAMPLING["max_new_tokens"], {}, profile)
    bad += G.contract_problems(meta, contract)
    for k, v in spec.meta().items():
        if meta.get(k) != v:
            bad.append(f"{k} {meta.get(k)!r} != {v!r}")
    if "adapter_subfolder" in meta:
        bad.append(f"adapter_subfolder {meta['adapter_subfolder']!r} on an untrained model")
    version = st.engine_version or locked_engine_version(root)
    if (meta.get("engine"), meta.get("engine_version")) != (st.engine, version):
        bad.append(f"engine {meta.get('engine')!r} {meta.get('engine_version')!r} != {version!r}")
    if (meta.get("condition"), meta.get("qset")) != (part.cond, part.qset):
        bad.append(f"meta is for {meta.get('condition')!r} {meta.get('qset')!r}")


def extract(root: Path, part: Part, st: Settings) -> None:
    """Rows, seed and judge checks; on success `part.rows` holds the extracted rows."""
    import pandas as pd

    from emx.formats import render_prompt
    from emx.judge_panel import PRESETS
    from emx.judge_run import expected_signature, source_block

    rows_bad, seed_bad, judge_bad = (part.problems[c] for c in ("rows", "seed", "judge"))
    gen = _read_csv(part.gen_csv)
    judged = _read_csv(part.judged_csv)
    meta = json.loads(part.meta.read_text())
    if len(gen) != meta.get("n_rows"):
        rows_bad.append(f"{len(gen)} rows, sidecar says {meta.get('n_rows')}")
    keyed = {}
    for i, r in enumerate(gen):
        k = (r["qid"], int(r["sample_idx"]))
        if k in keyed:
            rows_bad.append(f"duplicate key {k}")
        keyed[k] = i
    gen_keys = [tuple(r[c] for c in KEY_COLUMNS) for r in gen]
    if [tuple(r.get(c) for c in KEY_COLUMNS) for r in judged] != gen_keys:
        rows_bad.append(f"{part.judged_csv.name}: keys differ from the generation CSV")
        judged = []
    n, first = st.ext48_subset if part.qset == "ext48" else st.subset[part.qset]
    qs = _questions(root, part.qset)[:first]
    if first is not None and len(qs) < first:
        rows_bad.append(f"question file has {len(qs)} < {first} questions")
    cell = part.cond.split("@")[0]
    luna_cols = [
        c
        for m in OUT_METRICS
        for c in (score_column(m, LUNA), status_column(m, LUNA))
        if judged and c in judged[0]
    ]
    out = []
    for q in qs:
        prompt = render_prompt(FMT, q["question"], None)
        for i in range(n):
            if (q["qid"], i) not in keyed:
                rows_bad.append(f"missing ({q['qid']}, {i})")
                continue
            j = keyed[(q["qid"], i)]
            r = gen[j]
            want = (part.cond, cell, FMT, part.qset, q["question"], prompt)
            got = tuple(r[c] for c in ("condition", "cell", "eval_fmt", "qset", "question"))
            if got + (r["prompt_text"],) != want:
                rows_bad.append(f"({q['qid']}, {i}): row text or ids differ from a fresh render")
            if int(r["seed"]) != request_seed(part.cond, q["qid"], i):
                seed_bad.append(f"({q['qid']}, {i}): seed {r['seed']} != request_seed")
            row = {c: r[c] for c in GEN_COLUMNS}
            if judged:
                row.update({c: judged[j][c] for c in luna_cols})
            out.append(row)
    # judge: signatures against E1b's luna preset, signing this generation CSV
    src = source_block(part.gen_csv, pd.read_csv(part.gen_csv, keep_default_na=False))
    for m in LUNA_METRICS[part.qset]:
        sp = part.sig(m)
        if not sp.exists():
            judge_bad.append(f"{sp.name} missing")
            continue
        sig = json.loads(sp.read_text())
        want = expected_signature(m, PRESETS[LUNA], src)
        for k in SIG_IDENTITY:
            if sig.get(k) != want[k]:
                judge_bad.append(f"{sp.name}: {k} {sig.get(k)!r} != {want[k]!r}")
        s_src = sig.get("source", {})
        if (s_src.get("sha256"), s_src.get("n_rows"), s_src.get("keys_sha256")) != (
            src["sha256"], src["n_rows"], src["keys_sha256"]
        ):
            judge_bad.append(f"{sp.name}: signs another generation CSV")
        st_col, sc_col = status_column(m, LUNA), score_column(m, LUNA)
        if judged and st_col not in judged[0]:
            judge_bad.append(f"{part.judged_csv.name}: no {st_col} column")
            continue
        bad_status = [r for r in out if r.get(st_col) not in OK_STATUSES]
        no_score = [r for r in out if r.get(st_col) == "scored" and not _is_num(r.get(sc_col))]
        if bad_status or no_score:
            judge_bad.append(f"{sc_col}: {len(bad_status)} unscored, {len(no_score)} non-numeric")
    part.rows, part.columns = out, [*GEN_COLUMNS, *luna_cols]


def _is_num(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def make_part(root: Path, stage: str, qset: str) -> Part:
    cond = f"{stage}-untrained@{FMT}"
    if qset == "ext48":
        origin = "E1b"
    else:
        origin = "E1"
    gen = root / gen_path(cond, qset, exp=origin)
    judged = root / judged_path(cond, qset, exp=origin)
    return Part(stage, qset, cond, origin, gen, gen.with_suffix(".meta.json"), judged)


def check_part(root: Path, part: Part, st: Settings) -> Part:
    missing = [p.name for p in (part.gen_csv, part.meta, part.judged_csv) if not p.exists()]
    if missing:
        part.problems["hash"].append(f"missing {missing}")
        return part
    check_hashes(root, part)
    check_contract(root, part, st)
    extract(root, part, st)
    return part


# --- output --------------------------------------------------------------------------------------


def write_part(root: Path, part: Part) -> Path:
    path = root / C.STEP0_DIR / f"{part.cond}__{part.qset}.csv"

    def _w(fh):
        w = csv.DictWriter(fh, fieldnames=part.columns, extrasaction="raise")
        w.writeheader()
        w.writerows(part.rows)

    G._atomic_write(path, _w)
    return path


def run_import(
    root: Path = ROOT,
    stages: tuple[str, ...] = R.REUSED_STEP0_STAGES,
    with_ext48: bool = False,
    check_only: bool = False,
    settings: Settings | None = None,
) -> dict:
    """Check every (stage, qset) part; write the passing stages' files and the manifest."""
    root = Path(root)
    st = settings or Settings()
    qsets = ("general", "narrow", "ext48") if with_ext48 else ("general", "narrow")
    parts = {s: [check_part(root, make_part(root, s, q), st) for q in qsets] for s in stages}
    report = {
        "utc": G.utc(),
        "git_commit": G.git_commit(),
        "with_ext48": with_ext48,
        "settings": {
            "subset": st.subset, "e1_samples": st.e1_samples, "ext48_subset": st.ext48_subset,
            "ext48_samples": st.ext48_samples, "engine": st.engine,
            "engine_version": st.engine_version or locked_engine_version(root),
        },
        "stages": {},
    }
    manifest: dict[str, str] = {}
    for stage, ps in parts.items():
        ok = all(p.ok for p in ps)
        entry = {"ok": ok, "parts": {}}
        for p in ps:
            entry["parts"][p.qset] = {
                "checks": {c: {"ok": not p.problems[c], "problems": p.problems[c]} for c in CHECKS},
                "sources": {k: {"sha256": G.sha256_file(root / k), "anchor": v}
                            for k, v in sorted(p.anchors.items())},
                "n_rows": len(p.rows),
            }
            if ok and not check_only:
                out = write_part(root, p)
                entry["parts"][p.qset]["output"] = {
                    "path": _rel(root, out), "sha256": G.sha256_file(out), "n_rows": len(p.rows)
                }
                for f in [*p.anchors, *(_rel(root, a) for a in p.anchor_files), _rel(root, out)]:
                    manifest[f] = G.sha256_file(root / f)
        report["stages"][stage] = entry
    if not check_only:
        rpath = root / C.STEP0_DIR / "import.json"
        G._atomic_write(rpath, lambda fh: fh.write(json.dumps(report, indent=2) + "\n"))
        manifest[_rel(root, rpath)] = G.sha256_file(rpath)
        body = "".join(f"{manifest[k]}  {k}\n" for k in sorted(manifest))
        G._atomic_write(root / C.part_manifest_path("step0-sources"), lambda fh: fh.write(body))
    return report


def summary(report: dict) -> tuple[bool, str]:
    lines, all_ok = [], True
    for stage, e in report["stages"].items():
        all_ok &= e["ok"]
        for qset, p in e["parts"].items():
            marks = " ".join(f"{c}={'ok' if v['ok'] else 'FAIL'}" for c, v in p["checks"].items())
            lines.append(f"{stage:5s} {qset:8s} {marks}")
            for c, v in p["checks"].items():
                lines += [f"      {c}: {x}" for x in v["problems"][:10]]
                if len(v["problems"]) > 10:
                    lines.append(f"      {c}: ... {len(v['problems']) - 10} more")
    return all_ok, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--stages", nargs="+", default=list(R.REUSED_STEP0_STAGES),
                   choices=R.REUSED_STEP0_STAGES)
    p.add_argument("--with-ext48", action="store_true", help="also ext48 (after the re-evaluation)")
    p.add_argument("--check-only", action="store_true", help="run the checks, write nothing")
    args = p.parse_args(argv)
    report = run_import(ROOT, tuple(args.stages), args.with_ext48, args.check_only)
    ok, text = summary(report)
    print(text)
    if not ok:
        failed = [s for s, e in report["stages"].items() if not e["ok"]]
        print(f"step-0 import FAILED for {failed}: those stages fall back (main session decides)")
        return 1
    print("step-0 import passed" + (" (check only)" if args.check_only else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
