"""Experiment 4: re-evaluate a finished live smoke's saved records under the current check code,
instead of rerunning the agents (Research Advisor, 28 Oct). $0 except the repository-text
verifier's short containers.

    uv run python scripts/week2_live_smoke_recheck.py --smoke-dir <raw>/smoke_chain_stress/<v..>
        --ran-at <commit> [--week2-config configs/week2/chain_stress.toml] [--host laptop]

Refuses unless: the smoke was recorded under this design (smoke.json's fingerprint); <commit>'s
copies of chain_stress.SOURCE_FILES give exactly the smoke's recorded source fingerprint (the
code that ran the agents); the source files are committed; and every top-level definition that
differs between <commit> and this checkout is in RECHECK_ALLOWED (check functions only, nothing
the agent run executes). Then it reruns stub_chain_stress.live_smoke_checks on the saved chain
record and records gate `live_smoke_chain_stress` at the current smoke version and source
fingerprint, with the original images, the commit it ran at and the changed definitions.
This file is outside the source fingerprint; it runs no agent.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from compaction.s2.chain import chain_stress as CS
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import nchain as N
from compaction.s2.chain import stub_chain_stress as SCS
from compaction.s2.chain import stubs
from compaction.s2.chain import week2 as W

GATE = "live_smoke_chain_stress"
# The definitions a recheck may see changed since the smoke ran: the live smoke's checks and
# the cue scan (called only by live_smoke_checks, after the chain has run) and the smoke
# versions. Anything else changed means the agents would run other code: rerun the smoke.
RECHECK_ALLOWED = {
    "src/compaction/s2/chain/stub_chain_stress.py": {
        "harness_cue_hits", "live_smoke_checks", "opening_tail", "live_cue_hits",
        "repo_text_verifier", "_ere"},
    "src/compaction/s2/chain/chain_stress.py": {"SMOKE_VERSIONS"},
}


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(CC.REPO_ROOT), *args], capture_output=True,
                          text=True, check=True).stdout


# Where the allowed check functions may be referenced from, besides each other: the stub's
# phase checks and the live smoke's command, which calls live_smoke_checks after the chain
# has run. SMOKE_VERSIONS is a gate version label and may be read anywhere.
CHECK_CALLERS = {"_phase_checks", "live_smoke_chain_stress"}


def _bindings(tree: ast.Module) -> list[tuple[str, str]]:
    """(name, kind) for every top-level binding, in order."""
    out = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.append((n.name, "def"))
        elif isinstance(n, ast.Assign | ast.AnnAssign | ast.AugAssign):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                out += [(e.id, "assign") for e in ast.walk(t) if isinstance(e, ast.Name)]
        elif isinstance(n, ast.Import | ast.ImportFrom):
            out += [((a.asname or a.name).split(".")[0], "import") for a in n.names]
        else:
            out += [(e.id, "other") for e in ast.walk(n)
                    if isinstance(e, ast.Name) and isinstance(e.ctx, ast.Store)]
    return out


def _literal_defaults(fn: ast.FunctionDef) -> bool:
    return all(isinstance(d, ast.Constant) for d in fn.args.defaults +
               [d for d in fn.args.kw_defaults if d is not None])


def masked_module(src: str, allowed: set[str], drop: set[str] = frozenset()) -> list[str]:
    """The module's top-level statements in order, as AST dumps. An allowed function without
    decorators and with only literal defaults keeps its place and name; its body, arguments and
    return annotation (nothing evaluated at import) are masked. SMOKE_VERSIONS' integer values
    are zeroed, its keys kept. Functions named in `drop` (allowed helpers new in this version)
    are left out."""
    out = []
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name in allowed and not n.decorator_list and \
                _literal_defaults(n):
            if n.name in drop:
                continue
            n = ast.FunctionDef(name=n.name, args=ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=[ast.Pass()], decorator_list=[], returns=None, type_params=[])
        elif isinstance(n, ast.Assign) and "SMOKE_VERSIONS" in allowed and \
                [getattr(t, "id", None) for t in n.targets] == ["SMOKE_VERSIONS"] and \
                isinstance(n.value, ast.Dict) and all(
                    isinstance(v, ast.Constant) and type(v.value) is int for v in n.value.values):
            n = ast.Assign(targets=n.targets, value=ast.Dict(
                keys=n.value.keys, values=[ast.Constant(0) for _ in n.value.values]))
        out.append(ast.dump(n))
    return out


def top_functions(src: str) -> dict[str, str]:
    """Each top-level function's AST dump by name (the last, if bound twice)."""
    return {n.name: ast.dump(n) for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}


def binding_problems(src: str, allowed: set[str]) -> list[str]:
    """An allowed name bound more than once, or by anything but its own def (an allowed
    SMOKE_VERSIONS by one assignment)."""
    seen: dict[str, list[str]] = {}
    for name, kind in _bindings(ast.parse(src)):
        if name in allowed:
            seen.setdefault(name, []).append(kind)
    return [f"{n} bound {k}" for n, k in seen.items()
            if k != (["assign"] if n == "SMOKE_VERSIONS" else ["def"])]


def caller_problems(files: dict[str, str], allowed_fns: set[str]) -> list[str]:
    """References to an allowed check function from top-level code other than the allowed
    functions and CHECK_CALLERS (module-level statements included)."""
    out = []
    for f, src in files.items():
        for top in ast.parse(src).body:
            owner = getattr(top, "name", None)
            if owner in allowed_fns or owner in CHECK_CALLERS:
                continue
            for n in ast.walk(top):
                nm = n.id if isinstance(n, ast.Name) else \
                    n.attr if isinstance(n, ast.Attribute) else None
                if nm in allowed_fns:
                    out.append(f"{f}: {owner or type(top).__name__} references {nm}")
    return out


def changed_definitions(commit: str) -> dict[str, list[str]]:
    """Per source file, what differs between `commit` and the working tree: "<module>" when
    anything outside the allowed check functions' bodies changed (order, other statements,
    imports, decorators, non-literal defaults, SMOKE_VERSIONS' keys) or an allowed name is
    bound twice or by something else; else the allowed functions whose definitions changed or
    that were added."""
    out = {}
    for f in CS.SOURCE_FILES:
        old, new = _git("show", f"{commit}:{f}"), (CC.REPO_ROOT / f).read_text()
        if old == new:
            continue
        allowed = RECHECK_ALLOWED.get(f, set())
        fa, fb = top_functions(old), top_functions(new)
        added = {n for n in allowed if n in fb and n not in fa}
        names = []
        if binding_problems(old, allowed) or binding_problems(new, allowed) or \
                masked_module(old, allowed) != masked_module(new, allowed, drop=added):
            names.append("<module>")
        names += sorted(n for n in allowed if (n in fa or n in fb) and fa.get(n) != fb.get(n))
        out[f] = names
    return out


def reference_problems() -> list[str]:
    """caller_problems over this checkout's source files."""
    fns = set().union(*RECHECK_ALLOWED.values()) - {"SMOKE_VERSIONS"}
    return caller_problems({f: (CC.REPO_ROOT / f).read_text() for f in CS.SOURCE_FILES}, fns)


def fingerprint_at(commit: str) -> str:
    """chain_stress.source_fingerprint over `commit`'s copies of the source files."""
    files = {f: hashlib.sha256(_git("show", f"{commit}:{f}").encode()).hexdigest()
             for f in CS.SOURCE_FILES}
    return hashlib.sha256("".join(f"{f}\0{files[f]}\n"
                                  for f in CS.SOURCE_FILES).encode()).hexdigest()


def problems(changed: dict[str, list[str]]) -> list[str]:
    return [f"{f}: {n} changed (not a check function's body)" for f, names in changed.items()
            for n in names if n not in RECHECK_ALLOWED.get(f, set())]


def _smoke_chain(cfg: dict) -> N.Chain:
    """The live smoke's chain, as s2_chain.live_smoke_chain_stress builds it."""
    u1 = next(c for c in N.chains_from_config(cfg) if c.id == "u1")
    return N.Chain("smoke-u1", "unrelated", u1.phases[:2])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--smoke-dir", type=Path, required=True)
    ap.add_argument("--ran-at", required=True, help="the commit the smoke's agents ran at")
    ap.add_argument("--week2-config", type=Path, default=Path("configs/week2/chain_stress.toml"))
    ap.add_argument("--host", choices=("laptop", "rig"), default="laptop")
    a = ap.parse_args(argv)
    cfg = W.load_config(a.week2_config)
    try:
        W.gate(W.resolve(cfg["meta"]["proposal"]), a.week2_config)
    except W.PlanDrift as e:
        sys.exit(f"refusing: plan drift\n{e}")
    mark = json.loads((a.smoke_dir / "smoke.json").read_text())
    fp, prov = W.fingerprint(cfg), mark["provenance"]
    bad = []
    if mark["fingerprint"] != fp:
        bad.append(f"the smoke ran under design {mark['fingerprint']}, not {fp}")
    ran = fingerprint_at(a.ran_at)
    if ran != prov["source_sha256"]:
        bad.append(f"{a.ran_at}'s source fingerprint {ran[:12]} is not the smoke's "
                   f"{prov['source_sha256'][:12]}")
    if _git("status", "--porcelain", "--", *CS.SOURCE_FILES).strip():
        bad.append("source files have uncommitted changes")
    changed = changed_definitions(a.ran_at)
    bad += problems(changed) + reference_problems()
    st = D.load_state()
    old = st["gates"].get(GATE) or {}
    smoke_dir = str(a.smoke_dir.resolve())
    # a gate rechecked before holds the current source; its original is in `rechecked`
    was = dict(old.get("provenance") or {})
    if old.get("rechecked"):
        was["source_sha256"] = old["rechecked"].get("ran_source_sha256")
    if Path(old.get("dir") or "").resolve() != Path(smoke_dir) or was != prov:
        bad.append(f"the recorded {GATE} gate isn't this smoke's (dir {old.get('dir')}, "
                   "provenance differs or missing)")
    if old.get("host") not in ("laptop", "rig"):
        bad.append(f"the recorded gate's host is {old.get('host')!r}")
    ids = {t: (d or {}).get("id") for t, d in (prov.get("images") or {}).items()}
    tasks = [p.task for p in _smoke_chain(cfg).phases]
    missing = [t for t in tasks if not ids.get(t) or D.image_id(ids[t]) != ids[t]]
    if missing:
        bad.append(f"no recorded image Id on this host for the smoke's tasks: {missing}")
    if bad:
        sys.exit("refusing:\n  " + "\n  ".join(bad))
    try:
        CS.apply_host(a.host)  # the verifier's containers go through this host's admission
    except RuntimeError as e:
        sys.exit(f"refusing: {e}")
    s = N.settings_from_config(cfg)
    chain = _smoke_chain(cfg)
    sub = D.load_state(a.smoke_dir / "state.json")
    rec = sub["chains"][chain.id]
    out = SCS.live_smoke_checks(chain, rec, a.smoke_dir, s, images=ids)
    # what live_smoke_checks doesn't recompute stays as the smoke left it: its run-level
    # failure reason, and its kept images (removed again here, confirmed or failed)
    orig = json.loads((a.smoke_dir / "live_smoke.json").read_text())
    if orig.get("reason"):
        out["checks"].append({"name": "the smoke ran to its checks", "ok": False,
                              "detail": orig["reason"]})
    left = stubs.remove_kept(a.smoke_dir)
    out["checks"].append({"name": "smoke's kept images removed", "ok": not left,
                          "detail": left})
    out["pass"] = all(c["ok"] for c in out["checks"])
    src = CS.source_fingerprint()["sha256"]
    head = _git("rev-parse", "HEAD").strip()
    rechecked = {"ran_at": a.ran_at, "ran_source_sha256": prov["source_sha256"],
                 "checked_at": head, "source_sha256": src, "changed": changed,
                 "utc": D.utc()}
    D.SE.dump(a.smoke_dir / "live_smoke_recheck.json", {**out, "rechecked": rechecked})
    for c in out["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"])
    st = D.load_state()
    st["gates"][GATE] = {
        **{k: old.get(k) for k in ("host", "image_digests", "grading", "spent")},
        "dir": smoke_dir,
        "pass": out["pass"], "utc": D.utc(), "fingerprint": fp,
        "version": CS.SMOKE_VERSIONS[GATE],
        "provenance": {**prov, "source_sha256": src}, "rechecked": rechecked,
        "failed": [c["name"] for c in out["checks"] if not c["ok"]]}
    D.save_state(st)
    D.status(f"chain-stress live smoke recheck: {'PASS' if out['pass'] else 'FAIL'} "
             f"(ran at {a.ran_at[:8]}, checked at {head[:8]})")
    return 0 if out["pass"] else 4


if __name__ == "__main__":
    sys.exit(main())
