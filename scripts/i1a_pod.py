"""On-pod step driver for I1a+ (implementation plan §3; run by scripts/pod_run_i1a.sh).

    python scripts/i1a_pod.py <step> [--device cuda] [--limit N] [--max-new-tokens K]

Steps, in chain order (each is idempotent and ends by writing its manifest):
    download     named-file downloads via emx.i1a.hfget -> results/I1a/download_paths.json
    smoke        P0: the §4.7 checks on real weights, reconstructor checks, memory and speed
    capture      activations for the 5 site families (all 64 layers, bf16)
    tokens       logit lens and J-Lens readouts (gate families + old sites), top-100 stores
    olens-gate   Oracle Lens sample 0 on the 3 gate families -> arm olens_rl600
    olens-extra  samples 1..9 (basic_readout_mt all; hallucination 75-conversation subset)
    shuffled     the manipulation check draw -> arm olens_shuffled
    reserve      reserve readouts: Oracle Lens at the old multi-hop sites, second multi-hop draw
    fve          reconstructor checks and joint bullet FVE for every saved sample
    probe        ridge probe on multi-hop -> arm ridge_probe
    manifest     one closing manifest over every output

Each step writes results/I1a/MANIFEST.<step>.sha256 (`shasum -a 256` format, repo-relative
paths) and MANIFEST.<step>.json ({step, required_outputs, counts, code_commit, inputs_sha256}),
only on success. `--limit` and `--max-new-tokens` exist for the tiny-model end-to-end test; the
real run never passes them (the manifest records them, and the chain refuses a limited manifest).

Memory: every step is its own process, so the 27B model, the fp32 unembedding copies and the
J-Lens stack are never resident together with the Oracle Lens and reconstructor adapters.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emx.i1a import contracts as C

GATE_FAMILIES = C.FAMILIES
SITE_FAMILIES = (*C.FAMILIES, C.JLENS_REF_FAMILY, C.OLDSITE_FAMILY)
PATHS_FILE = "download_paths.json"
STEPS = (
    "download",
    "smoke",
    "capture",
    "tokens",
    "olens-gate",
    "olens-extra",
    "shuffled",
    "reserve",
    "fve",
    "probe",
    "manifest",
)
# Upstream manifests each step's sidecar hashes as inputs (a changed upstream = a stale step)
UPSTREAM: dict[str, tuple[str, ...]] = {
    "smoke": ("download",),
    "capture": ("download",),
    "tokens": ("capture",),
    "olens-gate": ("capture",),
    "olens-extra": ("olens-gate",),
    "shuffled": ("capture",),
    "reserve": ("capture",),
    "fve": ("smoke", "olens-extra"),
    "probe": ("capture",),
}
N_SITES_ALL = {
    **C.N_SITES,
    C.JLENS_REF_FAMILY: C.N_JLENS_REF_SITES,
    C.OLDSITE_FAMILY: C.N_OLDSITE_ITEMS,
}


# ------------------------------------------------------------------ small helpers


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[i1a_pod {utc()}] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    p = Path(path).resolve()
    try:
        return str(p.relative_to(C.REPO_ROOT))
    except ValueError:
        return str(p)


def code_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(C.REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        import os

        return os.environ.get("EMX_GIT_COMMIT", "unknown")


def write_manifest(
    step: str,
    outputs: list[Path],
    *,
    counts: dict[str, Any],
    inputs: list[Path] = (),
    checks: dict[str, bool | None] | None = None,
    args: argparse.Namespace | None = None,
) -> Path:
    """MANIFEST.<step>.sha256 plus its sidecar JSON; both written last, only on success."""
    outs = sorted({Path(p) for p in outputs})
    missing = [str(p) for p in outs if not Path(p).exists()]
    if missing:
        raise RuntimeError(f"{step}: required outputs missing: {missing}")
    lines = [f"{sha256_file(p)}  {rel(p)}" for p in outs]
    man = C.manifest_path(step)
    man.parent.mkdir(parents=True, exist_ok=True)
    # bind the step to what it consumed: upstream manifests, the paths file, the frozen subset
    inputs = [*inputs, C.RESULTS / PATHS_FILE, C.SUBSET_PATH]
    inputs += [C.manifest_path(u) for u in UPSTREAM.get(step, ())]
    absent = [rel(p) for p in inputs if not Path(p).exists()]
    if absent:  # every declared input must exist: an upstream step that never ran is an error
        raise RuntimeError(f"{step}: declared inputs missing: {absent}")
    side = {
        "step": step,
        "utc": utc(),
        "required_outputs": [rel(p) for p in outs],
        "counts": {k: int(v) for k, v in counts.items()},  # integers only (pod_run_i1a.sh)
        "checks": checks or {},
        "code_commit": code_commit(),
        "inputs_sha256": {rel(p): sha256_file(p) for p in inputs},
        "limited": bool(args and (args.limit or args.max_new_tokens)),
        "args": {k: v for k, v in vars(args).items() if k != "func"} if args else {},
    }
    tmp = man.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(side, indent=2, sort_keys=True) + "\n")
    tmp.replace(man.with_suffix(".json"))
    tmp = man.with_suffix(".sha256.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(man)
    log(f"{step}: manifest {rel(man)} ({len(lines)} files)")
    return man


def timed(label: str, fn: Any) -> Any:
    """Run fn() between two log lines, so a slow or stuck check is visible in the step log."""
    t = time.time()
    log(f"  {label} ...")
    out = fn()
    passed = getattr(out, "passed", None)
    log(
        f"  {label} done in {time.time() - t:.0f}s"
        + (f" (passed={passed})" if passed is not None or hasattr(out, "passed") else "")
    )
    return out


def gate_checks(step: str, results: list[Any], args: argparse.Namespace) -> None:
    """A step whose required checks (contracts.CHECK_KIND "required") did not all pass writes no
    manifest and exits 3, so the chain cannot move on (P1 needs a clean smoke manifest). The
    check files stay on disk for diagnosis. Limited (test) runs record but never gate: random
    weights fail the checks that compare against real artifacts, and the chain refuses limited
    manifests anyway."""
    bad = [
        r.name for r in results if C.CHECK_KIND.get(r.name) == "required" and r.passed is not True
    ]
    if bad and not args.limit:
        log(f"{step}: required checks not passed: {bad}; no manifest written")
        raise SystemExit(3)
    if bad:
        log(f"{step}: (limited run) required checks not passed, not gating: {bad}")


def expect_counts(step: str, got: dict[str, int], want: dict[str, int], args) -> None:
    """Exact output counts for a real run (limited test runs skip this)."""
    if args.limit:
        return
    diff = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
    if diff:
        raise RuntimeError(f"{step}: counts differ from the contract (got, want): {diff}")


def paths() -> dict[str, Any]:
    p = C.RESULTS / PATHS_FILE
    if not p.exists():
        raise SystemExit(f"{rel(p)} missing: run the download step first")
    return json.loads(p.read_text())


def free_gpu() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def backend(args: argparse.Namespace) -> Any:
    from emx.i1a import capture

    dtype = "float32" if args.device == "cpu" else C.DTYPE
    return capture.load_backend(paths()["model_dir"], device=args.device, dtype=dtype)


def sampling(args: argparse.Namespace) -> Any:
    from dataclasses import replace

    from emx.i1a import olens

    s = olens.default_sampling()
    return replace(s, max_new_tokens=args.max_new_tokens) if args.max_new_tokens else s


def bind_olens(bk: Any) -> Any:
    from emx.i1a import olens

    o = olens.PublicOLens()
    o.bind(bk, Path(paths()["olens_dir"]))
    return o


def gate_rows(family: str) -> list[Any]:
    from emx.i1a import olens

    return [r for r in olens.read_samples(C.samples_path(family)) if r.draw == "gate"]


# ------------------------------------------------------------------ steps


def step_download(args: argparse.Namespace) -> Path:
    from emx.i1a import hfget

    got = hfget.fetch_all_i1a(args.artifacts_dir)
    out = C.RESULTS / PATHS_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    keep = {k: str(got[k]) for k in ("model_dir", "jlens", "olens_dir", "ar_dir", "whitener_dir")}
    out.write_text(json.dumps(keep, indent=2) + "\n")
    plan = C.RESULTS / "download_plan.json"
    plan.write_text(json.dumps(got.get("plan", {}), indent=2, default=str) + "\n")
    return write_manifest("download", [out, plan], counts={"groups": len(keep)}, args=args)


def _capture_all(bk: Any, args: argparse.Namespace) -> dict[str, int]:
    from emx.i1a import capture

    counts = {}
    for fam in SITE_FAMILIES:
        t = time.time()
        capture.capture_family(bk, fam, limit=args.limit)
        counts[fam] = len(capture.load_index(fam))
        log(f"capture {fam}: {counts[fam]} sites in {time.time() - t:.0f}s")
    return counts


def step_capture(args: argparse.Namespace) -> Path:
    bk = backend(args)
    counts = _capture_all(bk, args)
    expect_counts("capture", counts, N_SITES_ALL, args)
    outs = [p for f in SITE_FAMILIES for p in (C.acts_path(f), C.acts_index_path(f))]
    return write_manifest("capture", outs, counts=counts, args=args)


def step_smoke(args: argparse.Namespace) -> Path:
    """P0. Everything the §4.7 checks need, plus the memory and speed projection."""
    import torch

    from emx.i1a import capture, checks_fve, checks_olens, checks_readers, fve, olens
    from emx.i1a import token_readers as tr

    p = paths()
    bk = backend(args)
    results = [
        timed("backend", lambda: checks_readers.check_backend(bk)),
        timed("capture_repeat", lambda: checks_readers.check_capture_repeat(bk)),
    ]
    _capture_all(bk, args)  # cheap (531 forwards); the capture step then only verifies
    results.append(
        timed(
            "logit_lens_last_layer",
            lambda: checks_readers.check_logit_lens_last_layer(bk, limit=args.limit),
        )
    )

    # token readers, one at a time (memory): J-Lens reference, then store vs Producer
    ll = tr.bind_reader("logit_lens", bk)
    jl = tr.bind_reader("jlens", bk, jlens_path=p["jlens"])
    results.append(
        timed(
            "jlens_reference",
            lambda jl=jl: checks_readers.check_jlens_reference(jl, limit=args.limit),
        )
    )
    del ll, jl
    free_gpu()

    # Oracle Lens: adapter live, batching at the real batch size, then a timed full batch
    o = bind_olens(bk)
    _, H = capture.load_acts("hallucination", [44])
    results.append(timed("olens_adapter_live", lambda: checks_olens.check_adapter_live(o, bk)))
    n = min(C.BATCH, H.shape[0])
    # the real run checks at the real batch size (32 rows x 32 steps); a limited CPU test run
    # uses 8 x 8, since the check's logic is covered by tests/i1a/test_olens.py
    bn, bsteps = (min(8, n), 8) if args.limit else (n, 32)
    results.append(
        timed(
            "olens_batching",
            lambda: checks_olens.check_batching(o, H[:, 0, :], 44, n=bn, steps=bsteps),
        )
    )
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    t = time.time()
    timed(
        "timed_full_batch",
        lambda: olens.read_batch(o, H[:n, 0, :], 44, C.SEED_BASE["gate"], sampling=sampling(args)),
    )
    batch_s = time.time() - t

    # reconstructor on the same model: live, ruler (hallucination sites, 11 layers)
    h = timed("load_ar", lambda: fve.load_ar(bk, Path(p["ar_dir"])))
    results.append(timed("fve_ar_live", lambda: checks_fve.check_ar_live(h, bk.tokenizer)))
    ws = fve.load_whiteners(Path(p["whitener_dir"]), device=args.device)
    hall_sites = capture.sites("hallucination", bk.tokenizer, limit=args.limit)
    acts = capture.load_acts("hallucination")
    results.append(
        timed(
            "fve_ruler",
            lambda: checks_fve.check_ruler(
                h, ws, sites=hall_sites, acts=acts, layers=list(range(C.N_MODEL_LAYERS))
            ),
        )
    )

    peak_gb = torch.cuda.max_memory_allocated() / 1e9 if torch.cuda.is_available() else 0.0
    total_batches = sum(C.EXPECTED_BATCHES.values())
    proj_h = total_batches * batch_s / 3600
    ms = C.CheckResult(
        name="memory_speed",
        passed=(proj_h <= C.CHECK_THRESHOLDS["olens_gen_hours_max"]) and peak_gb < 80.0,
        value=round(proj_h, 3),
        threshold=C.CHECK_THRESHOLDS["olens_gen_hours_max"],
        n=n,
        details={
            "batch_seconds": round(batch_s, 2),
            "batch_size": n,
            "max_new_tokens": sampling(args).max_new_tokens,
            "total_batches": total_batches,
            "peak_gpu_gb": round(peak_gb, 2),
            "device": args.device,
        },
        notes="projected hours = all planned Oracle Lens batches x one measured full batch",
        utc=utc(),
    )
    ms.write()
    results.append(ms)
    outs = [C.check_path(r.name) for r in results]
    passed = {r.name: r.passed for r in results}
    log(f"smoke: {json.dumps(passed)}")
    counts = {"checks": len(results), "checks_passed": sum(v is True for v in passed.values())}
    gate_checks("smoke", results, args)
    return write_manifest("smoke", outs, counts=counts, checks=passed, args=args)


def step_tokens(args: argparse.Namespace) -> Path:
    """Both token readers over the store, then the store-vs-Producer check (a §4.7 required
    check: the stored activations reproduce wsbench's own Producer readouts), each reader bound
    independently through wsbench's own bind so no loading code is shared."""
    from emx.i1a import checks_readers
    from emx.i1a import token_readers as tr

    p = paths()
    bk = backend(args)
    outs: list[Path] = []
    for name in ("logit_lens", "jlens"):
        jpath = p["jlens"] if name == "jlens" else None
        reader = tr.bind_reader(name, bk, jlens_path=jpath)
        for fam in (*GATE_FAMILIES, C.OLDSITE_FAMILY):
            outs.append(tr.read_family(reader, bk, fam, limit=args.limit))
            outs.append(C.topk_path(name, fam))
        del reader
        fresh = tr.bind_reader_via_wsbench(name, bk, jlens_path=jpath)
        res = checks_readers.check_store_vs_producer(
            bk, {name: fresh}, out=C.check_path(f"store_vs_producer_{name}")
        )
        outs.append(C.check_path(f"store_vs_producer_{name}"))
        del fresh
        free_gpu()
        if not res.passed:
            log(f"tokens: store_vs_producer FAILED for {name}")
    merged = _merge_store_checks()
    outs.append(C.check_path("store_vs_producer"))
    gate_checks("tokens", [merged], args)
    return write_manifest("tokens", outs, counts={"files": len(outs)}, args=args)


def _merge_store_checks() -> Any:
    """One `store_vs_producer` result (the frozen check name) from the per-reader results."""
    parts = {
        n: json.loads(C.check_path(f"store_vs_producer_{n}").read_text())
        for n in ("logit_lens", "jlens")
    }
    res = C.CheckResult(
        name="store_vs_producer",
        passed=all(v["passed"] is True for v in parts.values()),
        value=None,
        threshold=None,
        n=sum(v["n"] for v in parts.values()),
        details={n: {"passed": v["passed"], "n": v["n"]} for n, v in parts.items()},
        notes="all readers must pass; per-reader details in store_vs_producer_<reader>.json",
        utc=utc(),
    )
    res.write()
    return res


def step_olens_gate(args: argparse.Namespace) -> Path:
    from emx.i1a import capture, olens

    o = bind_olens(backend(args))
    outs = []
    for fam in GATE_FAMILIES:
        olens.generate_draw(o, fam, "gate", acts=capture.load_acts(fam), sampling=sampling(args))
        # the shared samples file is appended to by olens-extra, so this step hashes only its
        # own immutable output, the arm file; olens-extra and the closing manifest cover samples
        outs.append(olens.write_arm(fam, "olens_rl600", gate_rows(fam)))
    counts = {f: len(gate_rows(f)) for f in GATE_FAMILIES}
    expect_counts("olens-gate", counts, dict(C.N_CELLS), args)
    return write_manifest("olens-gate", outs, counts=counts, args=args)


def step_olens_extra(args: argparse.Namespace) -> Path:
    from emx.i1a import capture, olens

    o = bind_olens(backend(args))
    for fam in C.SAMPLE_FAMILIES:
        olens.generate_draw(o, fam, "extra", acts=capture.load_acts(fam), sampling=sampling(args))
    counts = {f: len(olens.read_samples(C.samples_path(f))) for f in C.SAMPLE_FAMILIES}
    if not args.limit:
        for f, n in counts.items():
            if n != C.EXPECTED_SAMPLE_ROWS[f]:
                raise RuntimeError(f"{f}: {n} sample rows, expected {C.EXPECTED_SAMPLE_ROWS[f]}")
    outs = [C.samples_path(f) for f in C.SAMPLE_FAMILIES]
    return write_manifest("olens-extra", outs, counts=counts, args=args)


def step_shuffled(args: argparse.Namespace) -> Path:
    from emx.i1a import capture, olens

    o = bind_olens(backend(args))
    fam = "basic_readout_mt"
    out = olens.generate_shuffled(o, acts=capture.load_acts(fam), sampling=sampling(args))
    outs = [out, C.draw_samples_path(fam, "shuffled")]
    return write_manifest("shuffled", outs, counts={}, args=args)


def step_reserve(args: argparse.Namespace) -> Path:
    from emx.i1a import capture, olens

    o = bind_olens(backend(args))
    old = C.OLDSITE_FAMILY
    old_acts = capture.load_acts(old)
    olens.generate_draw(o, old, "reserve_oldsite", acts=old_acts, sampling=sampling(args))
    rows = olens.read_samples(C.draw_samples_path(old, "reserve_oldsite"))
    a1 = olens.write_arm("multihop", "olens_oldsite", rows, sites=old_acts[0])
    olens.generate_draw(
        o, "multihop", "reserve_draw2", acts=capture.load_acts("multihop"), sampling=sampling(args)
    )
    rows2 = olens.read_samples(C.draw_samples_path("multihop", "reserve_draw2"))
    a2 = olens.write_arm("multihop", "olens_draw2", rows2)
    outs = [
        a1,
        a2,
        C.draw_samples_path(old, "reserve_oldsite"),
        C.draw_samples_path("multihop", "reserve_draw2"),
    ]
    return write_manifest(
        "reserve", outs, counts={"oldsite": len(rows), "draw2": len(rows2)}, args=args
    )


def step_fve(args: argparse.Namespace) -> Path:
    from emx.i1a import capture, checks_fve, fve

    p = paths()
    bk = backend(args)
    h = fve.load_ar(bk, Path(p["ar_dir"]))
    ws = fve.load_whiteners(Path(p["whitener_dir"]), device=args.device)
    all_layers = list(range(C.N_MODEL_LAYERS))
    # fve_ar_live and fve_ruler belong to the smoke step (its manifest hashes them); this step
    # re-runs the live probe in memory only, to refuse scoring with a dead adapter, and writes
    # just its own two checks (mismatch, band), so no earlier manifest is invalidated
    live = checks_fve.check_ar_live(h, bk.tokenizer, write=False)
    if live.passed is not True and not args.limit:
        log("fve: reconstructor adapter not live; no FVE scores written")
        raise SystemExit(3)
    results = []
    hall_acts = capture.load_acts("hallucination")
    outs = []
    for fam in C.SAMPLE_FAMILIES:
        acts = hall_acts if fam == "hallucination" else capture.load_acts(fam)
        outs.append(fve.score_family(h, ws, fam, acts=acts, layers=all_layers))
    results.append(checks_fve.check_mismatch(h, ws, acts=hall_acts, layers=all_layers))
    results.append(checks_fve.check_joint_band())
    outs += [C.check_path(r.name) for r in results]
    passed = {r.name: r.passed for r in results}
    log(f"fve: {json.dumps(passed)}")
    counts = {"checks": len(results), "checks_passed": sum(v is True for v in passed.values())}
    return write_manifest("fve", outs, counts=counts, checks=passed, args=args)


def step_probe(args: argparse.Namespace) -> Path:
    import numpy as np

    from emx.i1a import capture, ridge_probe

    bk = backend(args)
    fam = "multihop"
    index, H = capture.load_acts(fam, list(C.FAMILY_LAYERS[fam]))
    specs = {s["id"]: s for s in _bank_items(fam)}
    items = [specs[r.id] for r in index]
    W_U = bk.unembed.detach().float().cpu()
    Y = ridge_probe.probe_targets(bk.tokenizer, W_U, items)
    groups = [r.id for r in index]
    preds, alphas = {}, {}
    for j, layer in enumerate(C.FAMILY_LAYERS[fam]):
        X = H[:, j, :].numpy().astype(np.float64)
        preds[layer], alphas[layer] = ridge_probe.fit_predict(X, Y, groups)
    out = ridge_probe.write_readouts(preds, W_U, bk.tokenizer, index)
    meta = C.RESULTS / "probe_alphas.json"
    meta.write_text(json.dumps({str(k): v for k, v in alphas.items()}, indent=2) + "\n")
    return write_manifest("probe", [out, meta], counts={"rows": len(index)}, args=args)


def _bank_items(family: str) -> list[dict[str, Any]]:
    """Bank items with the `id` the read plan uses (wsbench readplan._bank: label_of(name))."""
    from wsbench import readplan

    _header, items = readplan._bank(family)
    ids = [s.id for s in readplan.plan(family)]
    if [it["id"] for it in items] != ids:
        raise RuntimeError(f"{family}: bank ids differ from the read plan's")
    return items


def step_manifest(args: argparse.Namespace) -> Path:
    outs: list[Path] = []
    for step in STEPS[:-1]:
        side = C.manifest_path(step).with_suffix(".json")
        if not side.exists():
            raise SystemExit(f"step {step} has no manifest; run it first")
        outs += [C.manifest_path(step), side]
    return write_manifest("manifest", outs, counts={"steps": len(STEPS) - 1}, args=args)


RUNNERS = {
    "download": step_download,
    "smoke": step_smoke,
    "capture": step_capture,
    "tokens": step_tokens,
    "olens-gate": step_olens_gate,
    "olens-extra": step_olens_extra,
    "shuffled": step_shuffled,
    "reserve": step_reserve,
    "fve": step_fve,
    "probe": step_probe,
    "manifest": step_manifest,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=STEPS)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--artifacts-dir", default="/ws/i1a_artifacts")
    ap.add_argument("--limit", type=int, default=0, help="tests only: first N items per family")
    ap.add_argument("--max-new-tokens", type=int, default=0, help="tests only")
    args = ap.parse_args(argv)
    t = time.time()
    log(f"step {args.step} start")
    RUNNERS[args.step](args)
    log(f"step {args.step} done in {time.time() - t:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
