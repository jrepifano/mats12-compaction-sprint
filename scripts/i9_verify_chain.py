"""Verify I9's provenance chain (implementation plan §6, "Provenance chain"). Free: reads files.

    uv run python scripts/i9_verify_chain.py [--root DIR] [--require-all] [--allow-dirty]

Reads every `results/I9/manifests/*.json` (`contracts.Manifest`) under `--root` (default: the
repo) and checks:

  [a] outputs   each output exists with its recorded sha256 and row count (JSONL/CSV: non-blank
                lines), or is reported ABSENT (raw files are gitignored and may not be local;
                `--require-all` makes absence a failure). An output a later step rewrote in
                place passes as superseded only if that step lists it, at this recorded
                sha256, as an input (code review r3 finding 3);
  [b] chain     each input that another step produced carries a sha256 that step recorded for
                it. `results/I9/MANIFEST.<step>.sha256` inputs are produced by <step> itself (it
                writes them beside its JSON manifest). An input that matches only a manifest or
                a file copy under `results/I9/superseded/<UTC>/` (where scripts/pod_pull_results.sh
                moves files that a later session's pull replaces) passes, marked superseded;
  [c] external  each input no step produced (the wsbench bank, the vendored generator, ...)
                matches the file on disk (ABSENT if not local);
  [d] counts    row counts where the relation is known, read from the files and manifests:
                sampler replies = selected scenarios x samples, with every (scenario, sample)
                once; the pod sidecars' counts = their outputs' rows; activation store rows =
                index rows; a readouts file is a full (id, layer, position) grid; the
                misbehavior judge's verdicts, labels and rates add up to its replies; a
                predictions file is a full (target, repeat, scenario) grid;
  [e] git       no manifest records `git_dirty: true` unless `--allow-dirty`;
  [f] shasum    every `results/I9/MANIFEST.<step>.sha256` (written by scripts/i9_pod.py) lists
                exactly the outputs of `manifests/<step>.json`, and its sidecar
                `MANIFEST.<step>.json` agrees with that manifest's outputs and inputs.

One line per check (PASS, FAIL, ABSENT, NOTE), then a summary. Exit 0 when nothing FAILs, 1
otherwise (ABSENT counts as a failure only with `--require-all`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from emx.i9 import contracts as C

RESULTS_REL = Path("results") / C.EXP
SHASUM_PREFIX = "MANIFEST."
STATUSES = ("PASS", "FAIL", "ABSENT", "NOTE")
# the misbehavior judge's outputs, by file name (contracts layout)
LABELS, LABELS_LLM = C.LABELS_PATH.name, C.LABELS_LLM_ONLY_PATH.name
RATES, RATES_LLM = C.RATES_PATH.name, C.RATES_LLM_ONLY_PATH.name
PRIMARY_VERDICTS = (C.verdicts_path(C.LUNA).name, C.verdicts_path(C.DEEPSEEK).name)
TIEBREAK_VERDICTS = C.verdicts_path(C.GLM).name


# --------------------------------------------------------------------- results


@dataclass
class Line:
    status: str
    tag: str
    step: str
    msg: str

    def __str__(self) -> str:
        return f"{self.status:<6} [{self.tag}] {self.step}: {self.msg}"


@dataclass
class Report:
    lines: list[Line] = field(default_factory=list)

    def add(self, status: str, tag: str, step: str, msg: str) -> None:
        assert status in STATUSES, status
        self.lines.append(Line(status, tag, step, msg))

    def count(self, status: str) -> int:
        return sum(x.status == status for x in self.lines)

    def failed(self, require_all: bool) -> bool:
        return self.count("FAIL") > 0 or (require_all and self.count("ABSENT") > 0)


# --------------------------------------------------------------------- files


class Tree:
    """A repo-like root: resolves FileRef paths (repo-relative, or absolute when a step ran
    outside the repo) and caches sha256 and row counts."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.results = self.root / RESULTS_REL
        self._sha: dict[Path, str] = {}

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.root / q

    def sha(self, p: Path) -> str:
        if p not in self._sha:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            self._sha[p] = h.hexdigest()
        return self._sha[p]

    @staticmethod
    def rows(p: Path) -> int:
        with open(p, "rb") as f:
            return sum(1 for line in f if line.strip())

    def superseded_copies(self, p: str) -> list[Path]:
        """Copies of a results/I9/ path under results/I9/superseded/<UTC>/."""
        rel = Path(p)
        try:
            sub = rel.relative_to(RESULTS_REL)
        except ValueError:
            return []
        base = self.results / "superseded"
        return sorted(d / sub for d in base.glob("*") if (d / sub).is_file())


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def safetensors_shape(p: Path, key: str = "h") -> list[int] | None:
    """The shape of one tensor from a safetensors header (stdlib only)."""
    with open(p, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(n))
    entry = header.get(key)
    return None if entry is None else list(entry["shape"])


# --------------------------------------------------------------------- manifests


@dataclass
class Rec:
    """One loaded manifest: where it lives and its parsed contents."""

    file: Path
    m: C.Manifest
    historical: bool = False  # under results/I9/superseded/

    @property
    def step(self) -> str:
        return self.m.step + (" (superseded)" if self.historical else "")


def load_manifest(p: Path) -> C.Manifest:
    d = json.loads(p.read_text(encoding="utf-8"))
    d["inputs"] = [C.FileRef(**x) for x in d["inputs"]]
    d["outputs"] = [C.FileRef(**x) for x in d["outputs"]]
    return C.Manifest(**d)


def load_all(tree: Tree, rep: Report) -> tuple[list[Rec], list[Rec]]:
    cur: list[Rec] = []
    old: list[Rec] = []
    for p in sorted((tree.results / "manifests").glob("*.json")):
        try:
            cur.append(Rec(p, load_manifest(p)))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            rep.add("FAIL", "a", p.name, f"unreadable manifest: {e}")
    for p in sorted((tree.results / "superseded").glob("*/manifests/*.json")):
        try:
            old.append(Rec(p, load_manifest(p), historical=True))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            rep.add("NOTE", "b", p.name, f"unreadable superseded manifest ignored: {e}")
    return cur, old


def shasum_step(path: str) -> str | None:
    """`<step>` for a `results/I9/MANIFEST.<step>.sha256` path, else None."""
    p = Path(path)
    if p.parent != RESULTS_REL or not p.name.startswith(SHASUM_PREFIX):
        return None
    if not p.name.endswith(".sha256"):
        return None
    return p.name[len(SHASUM_PREFIX) : -len(".sha256")]


# --------------------------------------------------------------------- [a] outputs


def superseding_step(recs: list[Rec], r: Rec, o: C.FileRef, disk_sha: str,
                     seen: frozenset[int] = frozenset()) -> Rec | None:
    """The step that rewrote ``r``'s output ``o`` in place, or None (code review r3 finding 3).
    A step supersedes it only if its manifest lists ``o.path`` at ``o.sha256`` (``r``'s recorded
    hash) as an input and records an output at that path that is on disk, or is itself
    superseded the same way (a chain of rewrites). A later record of the path alone does not
    count: without the input link nothing ties the earlier output to the rewrite."""
    for q in recs:
        if q is r or id(q) in seen:
            continue
        if not any(i.path == o.path and i.sha256 == o.sha256 for i in q.m.inputs):
            continue
        for qo in q.m.outputs:
            if qo.path != o.path:
                continue
            if qo.sha256 == disk_sha:
                return q
            if superseding_step(recs, q, qo, disk_sha, seen | {id(r), id(q)}) is not None:
                return q
    return None


def check_outputs(tree: Tree, recs: list[Rec], rep: Report) -> None:
    # A file a later step rewrote in place (e.g. adjudicate-score rewriting the labels and rates
    # that the misbehavior judge first wrote; a monitor run merging rows into a predictions
    # file) passes for the earlier record as superseded only when the rewriting step read the
    # earlier sha256 as an input and its own record matches disk (superseding_step).
    for r in recs:
        for o in r.m.outputs:
            p = tree.path(o.path)
            if not p.is_file():
                rep.add("ABSENT", "a", r.step, f"{o.path} not on disk")
                continue
            got = tree.sha(p)
            if got != o.sha256:
                later = superseding_step(recs, r, o, got)
                if later is not None:
                    rep.add("PASS", "a", r.step,
                            f"{o.path} superseded by {later.step} (which read it as an input)")
                    continue
            if got != o.sha256:
                rep.add(
                    "FAIL", "a", r.step, f"{o.path} sha256 {got[:12]} != recorded {o.sha256[:12]}"
                )
                continue
            if o.rows is not None and tree.rows(p) != o.rows:
                rep.add("FAIL", "a", r.step, f"{o.path} has {tree.rows(p)} rows, recorded {o.rows}")
                continue
            rows = "" if o.rows is None else f", {o.rows} rows"
            rep.add("PASS", "a", r.step, f"{o.path} sha256 {got[:12]}{rows}")


# --------------------------------------------------------------------- [b] and [c] inputs


def check_inputs(tree: Tree, cur: list[Rec], old: list[Rec], rep: Report) -> None:
    produced: dict[str, list[tuple[Rec, str]]] = defaultdict(list)
    for r in cur:
        for o in r.m.outputs:
            produced[o.path].append((r, o.sha256))
    produced_old: dict[str, set[str]] = defaultdict(set)
    for r in old:
        for o in r.m.outputs:
            produced_old[o.path].add(o.sha256)
    by_step = {r.m.step: r for r in cur}
    for r in cur:
        for i in r.m.inputs:
            p = tree.path(i.path)
            src = shasum_step(i.path)
            producers = [(q, s) for q, s in produced.get(i.path, []) if q is not r]
            if src is not None and src in by_step:
                _shasum_input(tree, r, i, src, rep)
            elif producers:
                shas = {s for _, s in producers}
                names = ", ".join(sorted({q.m.step for q, _ in producers}))
                if i.sha256 in shas:
                    rep.add("PASS", "b", r.step, f"{i.path} = output of {names}")
                elif i.sha256 in produced_old.get(i.path, set()) or _in_superseded(tree, i):
                    rep.add(
                        "PASS",
                        "b",
                        r.step,
                        f"{i.path} = a superseded output (now {names} records {min(shas)[:12]})",
                    )
                else:
                    rep.add(
                        "FAIL",
                        "b",
                        r.step,
                        f"{i.path} sha256 {i.sha256[:12]} matches no "
                        f"output of {names} ({', '.join(s[:12] for s in sorted(shas))})",
                    )
            elif not p.is_file():
                rep.add("ABSENT", "c", r.step, f"external input {i.path} not on disk")
            elif tree.sha(p) == i.sha256:
                rep.add("PASS", "c", r.step, f"external input {i.path} matches disk")
            elif _in_superseded(tree, i):
                rep.add("PASS", "c", r.step, f"{i.path} matches a superseded copy")
            else:
                rep.add(
                    "FAIL",
                    "c",
                    r.step,
                    f"external input {i.path} changed on disk "
                    f"({tree.sha(p)[:12]} != recorded {i.sha256[:12]})",
                )


def _in_superseded(tree: Tree, ref: C.FileRef) -> bool:
    return any(tree.sha(q) == ref.sha256 for q in tree.superseded_copies(ref.path))


def _shasum_input(tree: Tree, r: Rec, i: C.FileRef, src: str, rep: Report) -> None:
    p = tree.path(i.path)
    if p.is_file() and tree.sha(p) == i.sha256:
        rep.add("PASS", "b", r.step, f"{i.path} = written by step {src}")
    elif _in_superseded(tree, i):
        rep.add("PASS", "b", r.step, f"{i.path} = a superseded copy of step {src}'s")
    elif not p.is_file():
        rep.add("ABSENT", "b", r.step, f"{i.path} (step {src}'s) not on disk")
    else:
        rep.add(
            "FAIL",
            "b",
            r.step,
            f"{i.path} sha256 {tree.sha(p)[:12]} != recorded "
            f"{i.sha256[:12]}: step {src} was rerun after {r.m.step} read it",
        )


# --------------------------------------------------------------------- [d] counts


def _verified(tree: Tree, ref: C.FileRef) -> Path | None:
    """The file, if it is on disk with its recorded hash ([a]/[b] report the rest)."""
    p = tree.path(ref.path)
    return p if p.is_file() and tree.sha(p) == ref.sha256 else None


def _notes_json(m: C.Manifest) -> dict[str, Any] | None:
    try:
        d = json.loads(m.notes)
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def check_sampler(tree: Tree, r: Rec, rep: Report) -> None:
    """scripts/i9_sample_{vllm,hf}.py: replies = selected scenarios x samples, each once."""
    notes = _notes_json(r.m) or {}
    if "seed_rule" not in notes or "sets" not in notes:
        return
    ov = notes.get("overrides", {})
    sets_ref = next((i for i in r.m.inputs if Path(i.path).name.endswith("sets.json")), None)
    out = next((o for o in r.m.outputs if o.path.endswith(".jsonl")), None)
    if sets_ref is None or out is None:
        rep.add("FAIL", "d", r.step, "sampler manifest without a sets.json input or replies")
        return
    sp = _verified(tree, sets_ref)
    if sp is None:
        rep.add("NOTE", "d", r.step, f"replies count not checked: {sets_ref.path} unavailable")
        return
    sets = json.loads(sp.read_text(encoding="utf-8"))
    keys = [k for k in str(notes["sets"]).split(",") if k]
    ids = list(dict.fromkeys(i for k in keys for i in sets.get(k, [])))
    if ov.get("ids"):
        wanted = {x for a in ov["ids"] for x in str(a).split(",") if x}
        ids = [i for i in ids if i in wanted]
    if ov.get("limit") is not None:
        ids = ids[: int(ov["limit"])]
    n_samples = int(ov.get("n_samples", C.N_SAMPLES))
    want = len(ids) * n_samples
    if out.rows != want:
        rep.add(
            "FAIL",
            "d",
            r.step,
            f"{out.path}: {out.rows} replies != {len(ids)} scenarios x {n_samples} samples",
        )
        return
    op = _verified(tree, out)
    if op is not None:
        got = Counter((x["scenario_id"], int(x["sample_idx"])) for x in read_jsonl(op))
        grid = {(i, k) for i in ids for k in range(n_samples)}
        if set(got) != grid or max(got.values(), default=1) > 1:
            rep.add(
                "FAIL",
                "d",
                r.step,
                f"{out.path}: (scenario, sample) keys are not exactly "
                f"{len(ids)} x range({n_samples}), once each",
            )
            return
    rep.add(
        "PASS",
        "d",
        r.step,
        f"{out.path}: {want} replies = {len(ids)} scenarios x {n_samples} samples",
    )


def _index_rows(p: Path) -> int:
    return len(json.loads(p.read_text(encoding="utf-8"))["rows"])


def check_sidecar_counts(tree: Tree, r: Rec, rep: Report) -> None:
    """scripts/i9_pod.py sidecar counts = the rows of the outputs they name."""
    side = tree.results / f"{SHASUM_PREFIX}{r.m.step}.json"
    if r.historical or not side.is_file():
        return
    counts = json.loads(side.read_text(encoding="utf-8")).get("counts", {})
    outs = {Path(o.path): o for o in r.m.outputs}
    for key, n in sorted(counts.items()):
        ref = None
        if key == "replies":
            ref = next((o for p, o in outs.items() if p.suffix == ".jsonl"), None)
        elif key in C.MODELS or key == "sites":
            stem = f"{key}_last" if key in C.MODELS else "qwen_last64"
            ref = next((o for p, o in outs.items() if p.name == f"{stem}.json"), None)
        else:
            ref = next((o for p, o in outs.items() if p.stem == key and p.suffix == ".jsonl"), None)
        if ref is None:
            continue
        if ref.path.endswith(".json"):
            p = _verified(tree, ref)
            if p is None:
                continue
            got = _index_rows(p)
        else:
            got = ref.rows
        status = "PASS" if got == n else "FAIL"
        rep.add(status, "d", r.step, f"sidecar count {key}={n} vs {ref.path} rows {got}")


def check_acts(tree: Tree, r: Rec, rep: Report) -> None:
    """Activation store rows and layers = its index's (emx.i9.acts layout)."""
    outs = {o.path: o for o in r.m.outputs}
    for path, ref in outs.items():
        if not path.endswith(".safetensors"):
            continue
        idx = outs.get(str(Path(path).with_suffix(".json")))
        sp, ip = _verified(tree, ref), (_verified(tree, idx) if idx else None)
        if sp is None or ip is None:
            continue
        shape = safetensors_shape(sp)
        doc = json.loads(ip.read_text(encoding="utf-8"))
        want = [len(doc["rows"]), len(doc["layers"])]
        if shape is None or shape[:2] != want:
            rep.add("FAIL", "d", r.step, f"{path}: store {shape} vs index rows x layers {want}")
        else:
            rep.add("PASS", "d", r.step, f"{path}: {want[0]} sites x {want[1]} layers = index")


def _is_readouts(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(k in rows[0] for k in ("id", "layer", "pos"))


def check_readouts(tree: Tree, r: Rec, rep: Report) -> None:
    """A readouts file is a full grid: every id has the same layers, each at the same
    positions, every (id, layer, pos) once."""
    for o in r.m.outputs:
        if "/readouts/" not in f"/{o.path}" or not o.path.endswith(".jsonl"):
            continue
        p = _verified(tree, o)
        if p is None:
            continue
        rows = read_jsonl(p)
        if not _is_readouts(rows):
            continue
        keys = Counter((x["id"], int(x["layer"]), int(x["pos"])) for x in rows)
        layers = sorted({k[1] for k in keys})
        pos_by_id: dict[str, set[int]] = defaultdict(set)
        for i, _, q in keys:
            pos_by_id[i].add(q)
        want = sum(len(v) * len(layers) for v in pos_by_id.values())
        grid = {(i, L, q) for i, v in pos_by_id.items() for L in layers for q in v}
        if set(keys) != grid or len(rows) != want or max(keys.values()) > 1:
            rep.add(
                "FAIL",
                "d",
                r.step,
                f"{o.path}: {len(rows)} rows are not a full "
                f"(id, layer, pos) grid ({want} expected)",
            )
        else:
            npos = sorted({len(v) for v in pos_by_id.values()})
            rep.add(
                "PASS",
                "d",
                r.step,
                f"{o.path}: {len(rows)} = {len(pos_by_id)} ids x "
                f"{len(layers)} layers x {'/'.join(map(str, npos))} positions",
            )


def check_judge(tree: Tree, r: Rec, rep: Report) -> None:
    """scripts/i9_judge.py misbehavior: verdicts, labels and rates add up to the replies."""
    if r.m.step != "judge_misbehavior":
        return
    replies = [i for i in r.m.inputs if "/replies/" in f"/{i.path}" or _reply_file(tree, i)]
    notes = _notes_json(r.m) or {}
    by_name = {Path(o.path).name: o for o in r.m.outputs}
    scope = _judged_ids(tree, r, notes, by_name, rep)
    if scope is None:
        n_rep = sum(i.rows or 0 for i in replies)
    else:  # only the replies of the judged sets (the sealed batch waits for an unseal)
        files = [_verified(tree, i) for i in replies]
        n_rep = sum(1 for p in files if p is not None for x in read_jsonl(p)
                    if x["scenario_id"] in scope)
    for name in PRIMARY_VERDICTS:
        if name in by_name:
            ok = by_name[name].rows == n_rep
            rep.add(
                "PASS" if ok else "FAIL",
                "d",
                r.step,
                f"{name}: {by_name[name].rows} verdicts vs {n_rep} replies",
            )
    if TIEBREAK_VERDICTS in by_name and "tiebreak_calls" in notes:
        ok = by_name[TIEBREAK_VERDICTS].rows == notes["tiebreak_calls"]
        rep.add(
            "PASS" if ok else "FAIL",
            "d",
            r.step,
            f"{TIEBREAK_VERDICTS}: "
            f"{by_name[TIEBREAK_VERDICTS].rows} verdicts vs {notes['tiebreak_calls']} "
            f"tie-break calls",
        )
    reply_groups: Counter[tuple[str, bool, str]] = Counter()
    for i in replies:
        p = _verified(tree, i)
        if p is None:
            reply_groups = Counter()
            break
        for x in read_jsonl(p):
            if scope is None or x["scenario_id"] in scope:
                reply_groups[(x["model"], bool(x["thinking"]), x["scenario_id"])] += 1
    for lab_name, rate_name in ((LABELS, RATES), (LABELS_LLM, RATES_LLM)):
        lab = by_name.get(lab_name)
        if lab is None:
            continue
        if "unlabelled" in notes:
            ok = lab.rows + int(notes["unlabelled"]) == n_rep
            rep.add(
                "PASS" if ok else "FAIL",
                "d",
                r.step,
                f"{lab_name}: {lab.rows} labels + "
                f"{notes['unlabelled']} unlabelled vs {n_rep} replies",
            )
        rate = by_name.get(rate_name)
        rp = _verified(tree, rate) if rate else None
        lp = _verified(tree, lab)
        if rp is None or lp is None:
            continue
        groups = Counter(
            (x["model"], bool(x["thinking"]), x["scenario_id"]) for x in read_jsonl(lp)
        )
        rates = {(x["model"], bool(x["thinking"]), x["scenario_id"]): x for x in read_jsonl(rp)}
        bad = [
            g
            for g in set(groups) | set(rates)
            if g not in rates
            or rates[g]["n"] != groups[g]
            or not 0 <= rates[g]["k"] <= rates[g]["n"]
            or (reply_groups and rates[g]["n"] > reply_groups.get(g, 0))
        ]
        if bad or len(rates) != rate.rows:
            rep.add(
                "FAIL",
                "d",
                r.step,
                f"{rate_name}: n per (model, thinking, scenario) does "
                f"not equal its labels, e.g. {sorted(bad)[:2]}",
            )
        else:
            rep.add(
                "PASS",
                "d",
                r.step,
                f"{rate_name}: {len(rates)} rates, sum n = "
                f"{sum(groups.values())} = {lab_name} rows",
            )


def _judged_ids(tree: Tree, r: Rec, notes: dict[str, Any], by_name: dict[str, C.FileRef],
                rep: Report) -> set[str] | None:
    """The scenario ids the misbehavior judge was run on: its recorded ``set`` resolved through
    its sets.json input; for a manifest written before the set was recorded, the ids in its
    labels output (a NOTE says so; label completeness per scenario is a checkpoint-2 check).
    None: judge every reply (no labels to scope by)."""
    sets_ref = next((i for i in r.m.inputs if Path(i.path).name.endswith("sets.json")), None)
    if "set" in notes and sets_ref is not None:
        sp = _verified(tree, sets_ref)
        if sp is not None:
            sets = json.loads(sp.read_text(encoding="utf-8"))
            return {i for k in str(notes["set"]).split(",") if k for i in sets.get(k, [])}
    lab = by_name.get(LABELS)
    lp = _verified(tree, lab) if lab is not None else None
    if lp is None:
        return None
    rep.add("NOTE", "d", r.step, "manifest predates the recorded --set: replies counted over "
            "the scenarios in its labels, so a scenario omitted entirely is not caught here "
            "(checkpoint 2's label_completeness check covers the confirmatory set)")
    return {x["scenario_id"] for x in read_jsonl(lp)}


def _reply_file(tree: Tree, ref: C.FileRef) -> bool:
    if not ref.path.endswith(".jsonl"):
        return False
    p = _verified(tree, ref)
    if p is None:
        return False
    with open(p, encoding="utf-8") as f:
        first = next((x for x in f if x.strip()), "")
    try:
        row = json.loads(first) if first else {}
    except json.JSONDecodeError:
        return False
    return {"model", "thinking", "scenario_id", "sample_idx", "seed", "text"} <= set(row)


def check_predictions(tree: Tree, r: Rec, rep: Report) -> None:
    """A predictions file is a full (target, thinking, repeat, scenario) grid."""
    for o in r.m.outputs:
        if "/predictions/" not in f"/{o.path}" or not o.path.endswith(".jsonl"):
            continue
        p = _verified(tree, o)
        if p is None:
            continue
        rows = read_jsonl(p)
        keys = Counter(
            (x["target_model"], bool(x["target_thinking"]), int(x["fold_repeat"]), x["scenario_id"])
            for x in rows
        )
        cells = {k[:3] for k in keys}
        ids_by: dict[tuple, set[str]] = defaultdict(set)
        for k in keys:
            ids_by[k[:3]].add(k[3])
        sizes = {len(v) for v in ids_by.values()}
        if max(keys.values(), default=1) > 1 or len(sizes) > 1:
            rep.add("FAIL", "d", r.step, f"{o.path}: not one row per (target, repeat, scenario)")
        else:
            rep.add(
                "PASS",
                "d",
                r.step,
                f"{o.path}: {len(rows)} = {len(cells)} (target, repeat) "
                f"x {sizes.pop() if sizes else 0} scenarios",
            )


def check_counts(tree: Tree, recs: list[Rec], rep: Report) -> None:
    for r in recs:
        for fn in (
            check_sampler,
            check_sidecar_counts,
            check_acts,
            check_readouts,
            check_judge,
            check_predictions,
        ):
            try:
                fn(tree, r, rep)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
                rep.add("FAIL", "d", r.step, f"{fn.__name__}: unreadable ({type(e).__name__}: {e})")


# --------------------------------------------------------------------- [e] and [f]


def check_git(recs: list[Rec], rep: Report, allow_dirty: bool) -> None:
    for r in recs:
        c = (r.m.git_commit or "?")[:12]
        if not r.m.git_dirty:
            rep.add("PASS", "e", r.step, f"git {c} clean")
        elif allow_dirty:
            rep.add("NOTE", "e", r.step, f"git {c} dirty (allowed)")
        else:
            rep.add("FAIL", "e", r.step, f"git {c} dirty")


def check_shasums(tree: Tree, recs: list[Rec], rep: Report) -> None:
    by_step = {r.m.step: r for r in recs}
    for p in sorted(tree.results.glob(f"{SHASUM_PREFIX}*.sha256")):
        step = p.name[len(SHASUM_PREFIX) : -len(".sha256")]
        r = by_step.get(step)
        if r is None:
            rep.add("FAIL", "f", step, f"{p.name} has no manifests/{step}.json")
            continue
        listed = set()
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                sha, _, path = line.partition("  ")
                listed.add((path.lstrip("*"), sha))
        recorded = {(o.path, o.sha256) for o in r.m.outputs}
        if listed != recorded:
            extra = sorted(x for x, _ in listed - recorded)[:2]
            miss = sorted(x for x, _ in recorded - listed)[:2]
            rep.add(
                "FAIL",
                "f",
                step,
                f"{p.name} != manifests/{step}.json outputs (only in "
                f"shasum {extra}, only in manifest {miss})",
            )
            continue
        side = p.with_suffix(".json")
        if side.is_file():
            d = json.loads(side.read_text(encoding="utf-8"))
            ins = {i.path: i.sha256 for i in r.m.inputs}
            if (
                sorted(d.get("required_outputs", [])) != sorted(x for x, _ in recorded)
                or d.get("inputs_sha256", ins) != ins
                or d.get("step") != step
            ):
                rep.add("FAIL", "f", step, f"{side.name} disagrees with manifests/{step}.json")
                continue
        rep.add("PASS", "f", step, f"{p.name} = manifests/{step}.json ({len(listed)} files)")


# --------------------------------------------------------------------- main


def verify(root: Path, *, require_all: bool = False, allow_dirty: bool = False) -> Report:
    tree = Tree(root)
    rep = Report()
    cur, old = load_all(tree, rep)
    if not cur:
        rep.add("FAIL", "a", "-", f"no manifests under {tree.results / 'manifests'}")
        return rep
    check_outputs(tree, cur, rep)
    check_inputs(tree, cur, old, rep)
    check_counts(tree, cur, rep)
    check_git(cur, rep, allow_dirty)
    check_shasums(tree, cur, rep)
    return rep


def summary(rep: Report, require_all: bool) -> str:
    counts = ", ".join(f"{rep.count(s)} {s.lower()}" for s in STATUSES)
    verdict = "FAIL" if rep.failed(require_all) else "PASS"
    return f"{verdict}: {len(rep.lines)} checks ({counts})"


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=C.REPO_ROOT, help="repo-like root (tests)")
    ap.add_argument("--require-all", action="store_true", help="absent files are failures")
    ap.add_argument("--allow-dirty", action="store_true", help="accept git_dirty manifests")
    ap.add_argument("--quiet", action="store_true", help="print only non-PASS lines")
    a = ap.parse_args(list(argv) if argv is not None else None)
    rep = verify(a.root, require_all=a.require_all, allow_dirty=a.allow_dirty)
    for line in rep.lines:
        if not (a.quiet and line.status == "PASS"):
            print(line)
    print(summary(rep, a.require_all))
    return 1 if rep.failed(a.require_all) else 0


if __name__ == "__main__":
    sys.exit(main())
