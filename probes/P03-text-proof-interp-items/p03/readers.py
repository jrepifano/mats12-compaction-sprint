"""P03 pod session 2 (phase "read") driver: capture and every reader on the selected items.

    python -m p03.readers download            # named-file downloads -> results/read/download_paths.json
    python -m p03.readers env                 # results/read/env.json
    python -m p03.readers all [--limit N] [--device cuda] [--max-new-tokens K]

``all`` loads Qwen3.6-27B once and runs, in one process (each phase timed into
``results/read/timing.json`` and printed as ``P03_STEP <name> start|done <UTC> [<seconds>]``):

1. ``model_load``; 2. ``smoke_a`` HF vs vLLM modal answers on data/smoke_hf.jsonl (base model,
   before any adapter is mounted); 3. ``sites`` + ``capture`` (``p03.read_store``; the store
   goes to ``$P03_ACTS_DIR``, default /ws/p03_acts, not pulled); 4. ``logit_lens`` over
   READ_LAYERS x 3 positions plus the final-layer top-1 (``smoke_d``); 5. ``jlens_bind``,
   ``smoke_b`` (I1a J-Lens reference), ``jlens``; 6. ``olens_bind``, ``smoke_c`` (20 shuffled
   cells); 7. ``olens_timing`` (batch 0 of the main draw, kept) and the projection, which picks
   the grid level (``p03.read_smoke.choose_level`` against ``$P03_MAX_HOURS``, default 3.0, over
   the time since ``$P03_READ_T0``, set by pod_read.sh); 8. the draws main, shuffled, contrast
   (resumable; generation stops when the Oracle Lens phase passes 1.5x its projection);
   9. ``readouts``.

A failed smoke check drops its reader (J-Lens readouts then go to ``readouts/dropped/``; the
Oracle Lens is not run) or only warns (a, d), and the session carries on with the rest
(plan: "If a reader check fails, drop that reader and report the rest"). ``status.json`` is
the summary: drops, the grid level, the overrun flag and what is complete.

Reused: ``emx.i1a.capture.load_backend``, ``emx.i1a.token_readers.bind_reader``,
``emx.i1a.olens.PublicOLens`` / ``default_sampling``, ``emx.i1a.hfget``, and
``emx.i9.acts.compare_token_rows`` (I9's bf16 top-10 rule) for the J-Lens reference.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from p03 import core
from p03 import read_olens as O
from p03 import read_smoke as S
from p03 import read_store as RS

OUT = core.RESULTS / "read"
READOUTS = OUT / "readouts"
SAMPLES = OUT / "olens_samples"
ITEMS_PATH = core.DATA / "reader_items.jsonl"
SMOKE_HF_PATH = core.DATA / "smoke_hf.jsonl"
REPO = core.PROBE.parents[1]
DEFAULT_ARTIFACTS = "/ws/p03_artifacts"
DEFAULT_ACTS = "/ws/p03_acts"
SMOKE_C_LAYER = 44  # middle of the grid (I9's pilot layer)
SMOKE_C_SEED = 20261011  # which items the 20 smoke cells use
I1A_SECONDS_PER_BATCH = 17.39  # logs/I1a-report.md memory_speed: 32 x 256 tokens, H100
OVERRUN_FACTOR = 1.5  # CLAUDE.md "Profile before bulk work": stop at 1.5x the projection
OVERRUN_GRACE_S = 120.0


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[p03.readers {utc()}] {msg}", flush=True)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Timer:
    """Per-phase wall-clock in ``results/read/timing.json`` (kept across a rerun: a phase run
    again gets a ``<name>#2`` entry), with anchored ``P03_STEP`` lines."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def note(self, key: str, value: Any) -> None:
        self.data[key] = value
        write_json(self.path, self.data)

    @contextmanager
    def step(self, name: str):
        key, k = name, 2
        while key in self.data:
            key, k = f"{name}#{k}", k + 1
        start, t = utc(), time.time()
        print(f"P03_STEP {name} start {start}", flush=True)
        rec: dict[str, Any] = {"start": start}
        try:
            yield rec
        except BaseException:
            rec.update(end=utc(), seconds=round(time.time() - t, 2), failed=True)
            self.note(key, rec)
            print(f"P03_STEP {name} failed {rec['end']} {rec['seconds']}", flush=True)
            raise
        rec.update(end=utc(), seconds=round(time.time() - t, 2))
        self.note(key, rec)
        print(f"P03_STEP {name} done {rec['end']} {rec['seconds']}", flush=True)


def free_gpu() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:  # pragma: no cover
        pass


# --------------------------------------------------------------------- download and env


def step_download(root: Path) -> dict[str, str]:
    """Qwen at core.MODEL_REV, the J-Lens file and the Oracle Lens adapter, planned in bytes and
    capped (``emx.i1a.hfget``) before the first byte moves."""
    from emx.i1a import contracts as I
    from emx.i1a import hfget

    if (core.MODEL_ID, core.MODEL_REV) != (I.MODEL_ID, I.MODEL_REV):
        raise SystemExit("core.MODEL_ID/REV differ from emx.i1a.contracts (the readers' model)")
    groups = [
        ("qwen", I.MODEL_ID, hfget.model_files(), I.MODEL_REV),
        ("jlens", I.JLENS_REPO, [I.JLENS_FILE], I.JLENS_REV),
        ("adapters", I.ADAPTER_REPO, list(I.OLENS_FILES), I.ADAPTER_REV),
    ]
    total = sum(hfget.plan_bytes(repo, files, revision=rev) for _, repo, files, rev in groups)
    hfget.check_total(total, "P03 session 2 downloads")
    log(f"download plan: {total / 1e9:.1f} GB in {sum(len(g[2]) for g in groups)} files")
    got = {}
    for name, repo, files, rev in groups:
        got[name] = hfget.fetch(repo, files, revision=rev, local_dir=root / name)
    paths = {
        "qwen_dir": str(got["qwen"]["config.json"].parent),
        "jlens": str(got["jlens"][I.JLENS_FILE]),
        "olens_dir": str(got["adapters"][I.OLENS_FILES[0]].parent),
        "planned_bytes": str(total),
    }
    write_json(OUT / "download_paths.json", paths)
    return paths


def _version(mod: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(mod)
    except Exception:  # noqa: BLE001 - not installed
        return None


def step_env() -> dict[str, Any]:
    from emx.i1a import contracts as I

    env: dict[str, Any] = {
        "utc": utc(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": os.environ.get("EMX_GIT_COMMIT"),
        "git_dirty": os.environ.get("EMX_GIT_DIRTY"),
        "versions": {
            m: _version(m)
            for m in (
                "torch",
                "transformers",
                "peft",
                "safetensors",
                "workspace-bench",
                "flash-linear-attention",
                "huggingface-hub",
            )
        },
        "model": f"{core.MODEL_ID}@{core.MODEL_REV}",
        "jlens": f"{I.JLENS_REPO}@{I.JLENS_REV}:{I.JLENS_FILE}",
        "olens_adapter": f"{I.ADAPTER_REPO}@{I.ADAPTER_REV}:{I.OLENS_SUBDIR}",
        "attn_implementation": I.ATTN_IMPLEMENTATION,
        "read_layers": list(core.READ_LAYERS),
        "read_positions": list(core.READ_POSITIONS),
        "final_layer": core.FINAL_LAYER,
        "olens_seed_base": O.SEED_BASE,
        "shuffle_perm_seed": O.SHUFFLE_PERM_SEED,
        "olens_batch": O.BATCH,
        "hf_smoke_sampling": core.SAMPLING,
        "max_hours": float(os.environ.get("P03_MAX_HOURS", "3.0")),
    }
    try:
        import torch

        env["cuda"] = torch.version.cuda
        env["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except ImportError:  # pragma: no cover
        pass
    write_json(OUT / "env.json", env)
    return env


# --------------------------------------------------------------------- HF smoke sampling


def hf_samples(
    backend: Any, prompt: str, prefill: str, *, n: int, seed: int, max_new_tokens: int
) -> list[str]:
    """``n`` HF samples of one prompt (the read render) at ``core.SAMPLING``; vLLM's
    presence_penalty has no HF equivalent and is left out. Decoded like session 1
    (``skip_special_tokens=True``, stripped)."""
    import torch

    it = RS.ReaderItem("smoke", "B", "survivor", prompt, prefill, "x")
    ids = RS.render_ids(it, backend.tokenizer)
    m = backend.model
    s = core.SAMPLING
    kw: dict[str, Any] = {
        "do_sample": True,
        "temperature": float(s["temperature"]),
        "top_p": float(s["top_p"]),
        "top_k": int(s["top_k"]),
        "repetition_penalty": float(s["repetition_penalty"]),
        "max_new_tokens": max_new_tokens,
        "pad_token_id": backend.tokenizer.pad_token_id or backend.tokenizer.eos_token_id,
    }
    if float(s["min_p"]) > 0:
        kw["min_p"] = float(s["min_p"])
    x = torch.tensor([ids] * n, device=backend.device)
    plain = m.disable_adapter() if hasattr(m, "disable_adapter") else nullcontext()
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    with torch.no_grad(), plain:
        out = m.generate(input_ids=x, attention_mask=torch.ones_like(x), **kw)
    texts = backend.tokenizer.batch_decode(out[:, len(ids) :], skip_special_tokens=True)
    return [t.strip() for t in texts]


# --------------------------------------------------------------------- J-Lens reference


def _sha_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jlens_reference(
    backend: Any,
    reader: Any,
    *,
    ref_path: Path | None = None,
    ref_sha256: str = S.JLENS_REF_SHA256,
    n_items: int = S.JLENS_REF_N_ITEMS,
) -> S.Check:
    """Smoke b: I1a's first ``n_items`` multihop items, rendered and resolved by I1a's own
    ``emx.i1a.capture.sites`` (the benchmark's read plan), captured with our ``capture`` at
    READ_LAYERS, read with our bound J-Lens, against I1a's committed J-Lens readouts (verified
    against the pinned sha256 first; a missing or different file fails the check)."""
    import torch
    from wsbench.produce.producer import Row

    from emx.i1a import capture as I1cap
    from emx.i9.acts import compare_token_rows

    ref_path = ref_path or REPO / S.JLENS_REF_PATH
    if not ref_path.exists() or _sha_file(ref_path) != ref_sha256:
        why = "missing" if not ref_path.exists() else "sha256 differs from the pin"
        return S.Check(
            "b_jlens_reference",
            False,
            0.0,
            S.JLENS_REF_MIN_FRAC,
            0,
            "drop jlens",
            {"error": f"{ref_path}: {why}"},
        )
    chosen = I1cap.sites("multihop", backend.tokenizer, limit=n_items)
    sl = [
        (RS.ReadSite(i, s.id, s.pos, s.abs_pos, s.token, s.n_tokens, RS.ids_sha256(ids)), ids)
        for i, (s, ids) in enumerate(chosen)
    ]
    layers = list(core.READ_LAYERS)
    h = RS.capture(backend, sl, layers)
    ours = []
    with torch.inference_mode():
        for k, (s, _) in enumerate(sl):
            for j, L in enumerate(layers):
                ours.append(
                    Row(s.id, L, s.pos, s.token, reader.read(h[k, j].float(), L)).contract()
                )
    ids = {s.id for s, _ in sl}
    stored = [r for r in core.read_jsonl(ref_path) if r["id"] in ids]
    _, det = compare_token_rows(ours, stored, tol=S.JLENS_REF_TOL)
    exact = sum(
        1
        for r in ours
        for t in stored
        if (t["id"], t["layer"]) == (r["id"], r["layer"]) and t["tokens"] == r["tokens"]
    )
    det = {**det, "n_exact_top10_order": exact, "ids": sorted(ids), "reference": S.JLENS_REF_PATH}
    return S.check_jlens_reference(det, len(ours))


# --------------------------------------------------------------------- the session


def _cells_for(grid: S.Grid, draw: str, items: list[RS.ReaderItem], sites, source_of):
    groups = {it.id: it.group for it in items}
    main = {i for i, g in groups.items() if g in ("survivor", "ws")}
    contrast = {i for i, g in groups.items() if g == "contrast"}
    if draw == "main":
        return O.grid_cells(sites, main, grid.main_layers, grid.main_positions)
    if draw == "contrast":
        return O.grid_cells(sites, contrast, grid.contrast_layers, grid.contrast_positions)
    if draw == "shuffled":
        ids = {i for i, g in groups.items() if g in grid.shuffled_groups}
        return O.shuffled_cells(sites, ids, grid.shuffled_layers, source_of)
    raise KeyError(draw)


def run_all(args: argparse.Namespace) -> int:
    from emx.i1a import capture as I1cap
    from emx.i1a import olens as I1olens
    from emx.i1a import token_readers as tr

    t0 = float(os.environ.get("P03_READ_T0", time.time()))
    max_hours = float(os.environ.get("P03_MAX_HOURS", "3.0"))
    acts_dir = Path(args.acts_dir or os.environ.get("P03_ACTS_DIR", DEFAULT_ACTS))
    T = Timer(OUT / "timing.json")
    items = RS.load_reader_items(Path(args.items))[: args.limit or None]
    by_id = {it.id: it for it in items}
    smoke_rows = core.read_jsonl(Path(args.smoke_hf))[: args.limit or None]
    paths = json.loads((OUT / "download_paths.json").read_text())
    status: dict[str, Any] = {"utc_start": utc(), "n_items": len(items), "complete": False}
    checks: list[S.Check] = []

    def save_smoke() -> None:
        write_json(
            OUT / "smoke.json",
            {"checks": [c.row() for c in checks], "drops": S.drops(checks), "utc": utc()},
        )

    with T.step("model_load"):
        bk = I1cap.load_backend(paths["qwen_dir"], device=args.device, dtype=args.dtype)
    hf_new = args.max_new_tokens or core.MAX_TOKENS

    with T.step("smoke_a_hf"):
        hf = {}
        for r in smoke_rows:
            hf[r["key"]] = hf_samples(
                bk,
                r["prompt"],
                r["prefill"],
                n=core.N_SAMPLES,
                seed=core.request_seed(r["key"], 0),
                max_new_tokens=hf_new,
            )
        core.write_jsonl(
            OUT / "smoke_hf_samples.jsonl", [{"key": k, "samples": v} for k, v in hf.items()]
        )
        checks.append(S.check_vllm_vs_hf(smoke_rows, hf))
        save_smoke()

    with T.step("sites"):
        site_list = RS.build_sites(items, bk.tokenizer)
        core.write_jsonl(OUT / "sites.jsonl", [s for s, _ in site_list])
        stem = RS.stem_token_report([s for s, _ in site_list])
        status["stem_tokens"] = stem
        if stem["n_stem_ok"] != stem["n_items"]:
            log(
                f"WARNING: {stem['n_items'] - stem['n_stem_ok']} items do not read the stem "
                f"tokens {RS.STEM_TOKENS}, e.g. {stem['off_stem']}"
            )
    with T.step("capture") as rec:
        _, rec["captured_now"] = RS.capture_store(bk, site_list, acts_dir)
        rec["n_sites"] = len(site_list)
    sites, layers_stored, h = RS.load_store(acts_dir)

    with T.step("logit_lens"):
        ll = tr.bind_reader("logit_lens", bk)
        n_tok = len(sites) * len(core.READ_LAYERS)
        if not RS.rows_complete(READOUTS / "logit_lens.jsonl", n_tok):
            RS.write_rows(
                READOUTS / "logit_lens.jsonl", RS.read_tokens(ll, sites, layers_stored, h)
            )
        final = RS.final_layer_top1(ll, sites, layers_stored, h)
        RS.write_rows(OUT / "final_layer_top1.jsonl", final)
        checks.append(S.check_final_layer(items, final))
        save_smoke()
        if not checks[-1].passed:
            log(
                f"WARNING smoke d FAILED: final-layer top-1 matches the modal answer on "
                f"{checks[-1].value} of B items (< {S.SMOKE_D_MIN_FRAC}); the read site may be off"
            )
        del ll
        free_gpu()

    with T.step("jlens_bind"):
        jl = tr.bind_reader("jlens", bk, jlens_path=paths["jlens"])
    with T.step("smoke_b_jlens_ref"):
        checks.append(jlens_reference(bk, jl, ref_path=args.jlens_ref))
        save_smoke()
    with T.step("jlens"):
        dropped = S.drops(checks)["jlens"]
        out = READOUTS / ("dropped/jlens.jsonl" if dropped else "jlens.jsonl")
        if dropped:
            log("smoke b FAILED: J-Lens dropped; its readouts go to readouts/dropped/ only")
        if not RS.rows_complete(out, n_tok):
            RS.write_rows(out, RS.read_tokens(jl, sites, layers_stored, h))
        del jl
        free_gpu()

    sampling = I1olens.default_sampling()
    if args.max_new_tokens:
        sampling.max_new_tokens = args.max_new_tokens
    with T.step("olens_bind"):
        ol = I1olens.PublicOLens(sampling=sampling)
        ol.bind(bk, Path(paths["olens_dir"]))
    source_of = O.item_derangement(sites)
    write_json(
        OUT / "olens_shuffle_perm.json",
        {
            "seed": O.SHUFFLE_PERM_SEED,
            "rule": "item i reads item source_of[i]'s activation at the same layer and pos",
            "source_of": source_of,
        },
    )
    with T.step("smoke_c_shuffled"):
        layer = SMOKE_C_LAYER if SMOKE_C_LAYER in core.READ_LAYERS else core.READ_LAYERS[0]
        cells = O.smoke_cells(sites, source_of, S.SMOKE_C_N_CELLS, layer, SMOKE_C_SEED)
        p = SAMPLES / "smoke_shuffled.jsonl"
        O.generate(
            ol,
            "smoke_shuffled",
            cells,
            sites=sites,
            layers_stored=layers_stored,
            h=h,
            out=p,
            sampling=sampling,
        )
        checks.append(S.check_shuffled([O.row_dict(r) for r in O.read_rows(p)], by_id))
        save_smoke()
    drop = S.drops(checks)
    status["drops"] = drop
    if drop["olens"]:
        log(
            "smoke c FAILED: the shuffled Oracle Lens names the target too often; Oracle Lens "
            "dropped, no draws run"
        )
        status.update(complete=True, olens="dropped", utc_end=utc())
        write_json(OUT / "status.json", status)
        return 0

    grid_path = OUT / "olens_grid.json"
    main_full = _cells_for(S.LEVELS[0], "main", items, sites, source_of)
    with T.step("olens_timing") as rec:
        secs: list[float] = []
        O.generate(
            ol,
            "main",
            main_full,
            sites=sites,
            layers_stored=layers_stored,
            h=h,
            out=SAMPLES / "main.jsonl",
            sampling=sampling,
            stop=lambda: len(secs) >= 1,
            on_batch=lambda b, s: secs.append(s),
        )
        prior = T.data.get("seconds_per_batch")
        spb = secs[0] if secs else float(prior or I1A_SECONDS_PER_BATCH)
        rec["seconds_per_batch"] = round(spb, 3)
        rec["measured_now"] = bool(secs)
        T.note("seconds_per_batch", round(spb, 3))

    def remaining(g: S.Grid) -> int:
        return (
            sum(O.n_batches(_cells_for(g, d, items, sites, source_of)) for d in O.DRAW_ORDER) - 1
        )  # batch 0 of main is done

    if grid_path.exists():
        grid = S.grid_from_dict(json.loads(grid_path.read_text())["grid"])
        log(f"grid level {grid.level} from {grid_path.name} (an earlier run's decision)")
    else:
        grid, tried = S.choose_level(
            remaining, seconds_per_batch=spb, elapsed_s=time.time() - t0, max_hours=max_hours
        )
        write_json(
            grid_path,
            {"grid": asdict(grid), "projections": [asdict(p) for p in tried], "utc": utc()},
        )
        for p in tried:
            log(
                f"PROJECTION level {p.level}: {p.remaining_batches} batches x "
                f"{p.seconds_per_batch} s + elapsed {p.elapsed_s / 3600:.2f} h -> "
                f"{p.projected_total_h} h (limit {max_hours} h){' OVER' if p.over else ''}"
            )
        if grid.level:
            log(f"GRID CUT to level {grid.level}: {grid}")
        if tried[-1].over:
            log(
                "WARNING: even the last cut level projects over the limit; running it anyway "
                "(the overrun rule and the pod deadline still apply)"
            )
    status["grid_level"] = grid.level
    olens_projected = remaining(grid) * spb
    deadline = time.time() + OVERRUN_FACTOR * olens_projected + OVERRUN_GRACE_S
    T.note(
        "olens_projection",
        {
            "seconds": round(olens_projected, 1),
            "level": grid.level,
            "stop_after_utc": datetime.fromtimestamp(deadline, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )

    def overrun() -> bool:
        return time.time() > deadline

    draws: dict[str, dict[str, Any]] = {}
    for d in O.DRAW_ORDER:
        with T.step(f"olens_{d}") as rec:
            cells = _cells_for(grid, d, items, sites, source_of)
            if not cells:
                draws[d] = {"n_batches": 0, "complete": True, "stopped": False}
                continue

            def progress(b: O.Batch, s: float, d: str = d, n: int = O.n_batches(cells)) -> None:
                if (b.batch_index + 1) % 20 == 0:
                    log(f"olens {d}: batch {b.batch_index + 1}/{n} ({s:.1f} s)")

            draws[d] = O.generate(
                ol,
                d,
                cells,
                sites=sites,
                layers_stored=layers_stored,
                h=h,
                out=SAMPLES / f"{d}.jsonl",
                sampling=sampling,
                stop=overrun,
                on_batch=progress,
            )
            rec.update(draws[d])
            if draws[d]["stopped"]:
                log(
                    f"P03_OVERRUN: the Oracle Lens phase passed {OVERRUN_FACTOR}x its "
                    f"projection; stopped in draw {d}"
                )
                break

    with T.step("readouts"):
        real = [r for d in ("main", "contrast") for r in O.read_rows(SAMPLES / f"{d}.jsonl")]
        RS.write_rows(READOUTS / "olens.jsonl", O.readout_rows(real, sites))
        shuf = O.read_rows(SAMPLES / "shuffled.jsonl")
        RS.write_rows(
            READOUTS / "olens_shuffled.jsonl", O.readout_rows(shuf, sites, with_source=True)
        )
    status.update(
        olens="run",
        draws=draws,
        overrun=any(v.get("stopped") for v in draws.values()),
        complete=all(v.get("complete") for v in draws.values()) and len(draws) == 3,
        utc_end=utc(),
        session_hours=round((time.time() - t0) / 3600, 3),
    )
    write_json(OUT / "status.json", status)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p03.readers")
    ap.add_argument("step", choices=("download", "env", "all"))
    ap.add_argument("--artifacts-dir", default=os.environ.get("P03_ARTIFACTS", DEFAULT_ARTIFACTS))
    ap.add_argument("--acts-dir", default=None)
    ap.add_argument("--items", default=str(ITEMS_PATH))
    ap.add_argument("--smoke-hf", default=str(SMOKE_HF_PATH))
    ap.add_argument("--jlens-ref", type=Path, default=None)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--limit", type=int, default=0, help="first N items (tests)")
    ap.add_argument("--max-new-tokens", type=int, default=0, help="tests: shorter generations")
    args = ap.parse_args(argv)
    if args.step == "download":
        T = Timer(OUT / "timing.json")
        with T.step("download"):
            step_download(Path(args.artifacts_dir))
        return 0
    if args.step == "env":
        step_env()
        return 0
    return run_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
