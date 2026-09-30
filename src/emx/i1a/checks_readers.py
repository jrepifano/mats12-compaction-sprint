"""§4.7 checks for capture and the token readers (implementation plan §2 U2; prereg v0.4 §4.7).

Each check returns a ``contracts.CheckResult`` and writes it to ``contracts.check_path(name)``:

- ``logit_lens_last_layer``: the benchmark's logit lens at block 63 has the same top-1 as the
  model's own next-token logits (a normal forward, adapter disabled) at each read site.
- ``jlens_reference``: on the ``jlens_ref`` store, our J-Lens top-1 display token equals
  ``samples[0]`` of the shipped ``gen-jlens-pr-jlens/<label>/L###.jsonl`` at the 12 reference
  layers; every shipped row must align (label, layer, pos, display token) and all 1,788 cells
  must be present; also reports mean top-10 overlap.
- ``store_vs_producer``: the rows ``read_family`` wrote from the store equal wsbench
  ``Producer.read_spec`` rows (fresh capture, independently bound reader) on a few items per
  gate family.
- ``capture_repeat``: a second capture of 3 items is bitwise identical to the first.
- ``backend``: the attention implementation is sdpa or eager (never FlashAttention-4), and the
  library versions and fast-path kernels available are recorded.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emx.i1a import capture
from emx.i1a import contracts as C


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finish(res: C.CheckResult, out: Path | None) -> C.CheckResult:
    res.utc = _utc()
    res.write(out)
    return res


def default_ref_dir() -> Path:
    from wsbench.registry import REPO_ROOT

    return REPO_ROOT / "evals" / "jlens_concept_pr" / "gen-jlens-pr-jlens"


# --------------------------------------------------------------------- logit lens, last layer


def check_logit_lens_last_layer(
    backend: Any,
    family: str = "basic_readout_mt",
    *,
    limit: int = 0,
    out: Path | None = None,
) -> C.CheckResult:
    """Logit lens (wsbench ``LogitLens.read``) on the output of block 63 vs the argmax of the
    model's own logits at the same position, over the family's read sites (fresh captures)."""
    import torch
    from wsbench.produce.render import display_tokens

    from emx.i1a.token_readers import bind_reader

    reader = bind_reader("logit_lens", backend)
    last = C.N_MODEL_LAYERS - 1
    site_list = capture.sites(family, backend.tokenizer, limit=limit)
    n = agree = 0
    misses: list[dict[str, Any]] = []
    for rows, ids in capture._by_item(site_list):
        acts = backend.capture(ids, [last], [r.abs_pos for r in rows])[last]
        model = backend.model
        plain = model.disable_adapter() if hasattr(model, "disable_adapter") else nullcontext()
        with torch.inference_mode(), plain:
            logits = model(torch.tensor([ids], device=backend.device)).logits[0]
        for i, r in enumerate(rows):
            with torch.inference_mode():
                lens_top = reader.read(acts[i], last).tokens[0]
            own_top = display_tokens(backend.tokenizer, [int(logits[r.abs_pos].argmax())])[0]
            n += 1
            if lens_top == own_top:
                agree += 1
            elif len(misses) < 20:
                misses.append({"id": r.id, "pos": r.pos, "lens": lens_top, "model": own_top})
    thr = C.CHECK_THRESHOLDS["logit_lens_last_layer"]
    value = agree / n if n else None
    return _finish(
        C.CheckResult(
            name="logit_lens_last_layer",
            passed=(value is not None and value >= thr),
            value=value,
            threshold=thr,
            n=n,
            details={"family": family, "layer": last, "agree": agree, "misses": misses},
        ),
        out,
    )


# --------------------------------------------------------------------- J-Lens reference


def check_jlens_reference(
    jlens_method: Any,
    *,
    ref_dir: Path | None = None,
    limit: int = 0,
    out: Path | None = None,
) -> C.CheckResult:
    """Our J-Lens on the captured ``jlens_ref`` store vs the benchmark's shipped reference
    readouts: top-1 display token agreement (the gate, >= 0.99) and mean top-10 overlap.

    Every reference row must align with our site: exactly one row at our ``pos`` in
    ``<label>/L###.jsonl``, with ``label``, ``layer`` and the read-site ``token`` (display form)
    equal to ours. Any missing or misaligned cell fails the check, and an unrestricted run
    (``limit=0``) must cover all N_JLENS_REF_SITES x 12 = 1,788 distinct cells."""
    import torch
    from wsbench.produce.render import display_tokens

    ref_dir = Path(ref_dir) if ref_dir is not None else default_ref_dir()
    layers = list(C.SITE_FAMILY_LAYERS[C.JLENS_REF_FAMILY])
    rows, h = capture.load_acts(C.JLENS_REF_FAMILY, layers)
    if limit:
        keep = set(list(dict.fromkeys(r.id for r in rows))[:limit])
        rows = [r for r in rows if r.id in keep]
    tok = jlens_method._b.tokenizer
    ids_by_label = {s.id: s.extra["input_ids"] for s in capture.family_specs(C.JLENS_REF_FAMILY)}
    n = top1 = 0
    overlap_sum = 0.0
    per_layer: dict[int, list[int]] = {L: [0, 0] for L in layers}
    misses: list[dict[str, Any]] = []
    align_fail: list[dict[str, Any]] = []
    n_align_fail = 0
    cells: set[tuple[str, int]] = set()

    def fail(r: C.SiteRow, layer: int, why: str) -> None:
        nonlocal n_align_fail
        n_align_fail += 1
        if len(align_fail) < 20:
            align_fail.append({"id": r.id, "layer": layer, "pos": r.pos, "why": why})

    with torch.inference_mode():
        for r in rows:
            site_display = display_tokens(tok, [int(ids_by_label[r.id][r.abs_pos])])[0]
            for li, layer in enumerate(layers):
                f = ref_dir / r.id / f"L{layer:03d}.jsonl"
                if not f.exists():
                    fail(r, layer, "missing file")
                    continue
                at_pos = [
                    row
                    for row in (json.loads(x) for x in f.read_text("utf-8").splitlines() if x)
                    if row.get("pos") == r.pos
                ]
                if len(at_pos) != 1:
                    fail(r, layer, f"{len(at_pos)} rows at pos")
                    continue
                ref = at_pos[0]
                bad = [
                    k
                    for k, want in (("label", r.id), ("layer", layer), ("token", site_display))
                    if ref.get(k) != want
                ]
                if bad:
                    fail(r, layer, f"mismatched {bad}")
                    continue
                cells.add((r.id, layer))
                ours = jlens_method.read(h[r.index, li], layer).tokens
                theirs = list(ref["samples"])
                n += 1
                hit = bool(ours) and bool(theirs) and ours[0] == theirs[0]
                top1 += hit
                per_layer[layer][0] += hit
                per_layer[layer][1] += 1
                overlap_sum += len(set(ours[:10]) & set(theirs[:10])) / 10.0
                if not hit and len(misses) < 20:
                    misses.append(
                        {"id": r.id, "layer": layer, "ours": ours[:3], "theirs": theirs[:3]}
                    )
    thr = C.CHECK_THRESHOLDS["jlens_reference_top1"]
    value = top1 / n if n else None
    n_expected = len(rows) * len(layers)
    complete = len(cells) == n_expected and (
        bool(limit) or n_expected == C.N_JLENS_REF_SITES * len(layers)
    )
    return _finish(
        C.CheckResult(
            name="jlens_reference",
            passed=(value is not None and value >= thr and n_align_fail == 0 and complete),
            value=value,
            threshold=thr,
            n=n,
            details={
                "ref_dir": str(ref_dir),
                "layers": layers,
                "n_sites": len(rows),
                "limit": limit,
                "n_cells_expected": n_expected,
                "n_cells_aligned": len(cells),
                "complete": complete,
                "alignment_failures": n_align_fail,
                "alignment_failure_examples": align_fail,
                "top1_agree": top1,
                "top10_overlap_mean": overlap_sum / n if n else None,
                "top1_by_layer": {
                    str(L): v[0] / v[1] if v[1] else None for L, v in per_layer.items()
                },
                "misses": misses,
            },
            notes="reference = the benchmark's shipped jlens_concept_pr gen-jlens-pr-jlens "
            "readouts; each row's label, layer, pos and display-form token must match our site",
        ),
        out,
    )


def check_store_vs_producer(
    backend: Any,
    reader_methods: Mapping[str, Any] | Iterable[Any],
    *,
    n_items: int = 3,
    out: Path | None = None,
) -> C.CheckResult:
    """The rows ``read_family`` wrote from the activation store vs wsbench ``Producer.read_spec``
    (fresh capture) on the first ``n_items`` items of each gate family. ``reader_methods`` must
    be freshly bound (e.g. ``token_readers.bind_reader_via_wsbench``), not the instances that
    wrote the rows. Compares the cell set, ``(id, layer, pos, token)`` and the top-10 display
    tokens; any difference fails."""
    import torch
    from wsbench.produce.producer import Producer

    methods = (
        dict(reader_methods)
        if isinstance(reader_methods, Mapping)
        else {m.name: m for m in reader_methods}
    )
    n = 0
    n_fail = 0
    fails: list[dict[str, Any]] = []
    max_score_diff = 0.0
    per: dict[str, dict[str, Any]] = {}

    def fail(**kw: Any) -> None:
        nonlocal n_fail
        n_fail += 1
        if len(fails) < 20:
            fails.append(kw)

    for name, method in methods.items():
        for family in C.FAMILIES:
            key = f"{name}/{family}"
            path = C.readouts_path(name, family)
            if not path.exists():
                fail(reader=name, family=family, why=f"missing {path.name}")
                per[key] = {"n": 0}
                continue
            specs = capture.family_specs(family)[:n_items]
            want_ids = {s.id for s in specs}
            stored: dict[tuple[str, int, int], dict[str, Any]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row["id"] in want_ids:
                        stored[(row["id"], row["layer"], row["pos"])] = row
            prod = Producer(backend=backend, method=method)
            fresh = []
            with torch.inference_mode():
                for spec in specs:
                    fresh += prod.read_spec(spec, layers=list(C.FAMILY_LAYERS[family]))
            fresh_keys = {(r.id, r.layer, r.pos) for r in fresh}
            if fresh_keys != set(stored):
                fail(
                    reader=name,
                    family=family,
                    why="cell sets differ",
                    only_store=len(set(stored) - fresh_keys),
                    only_producer=len(fresh_keys - set(stored)),
                )
            k_ok = 0
            for r in fresh:
                s = stored.get((r.id, r.layer, r.pos))
                if s is None:
                    continue
                n += 1
                cell = {"reader": name, "family": family, "id": r.id, "layer": r.layer}
                if s.get("token") != r.token:
                    fail(**cell, why="token", store=s.get("token"), producer=r.token)
                elif list(s["tokens"][:10]) != list(r.readout.tokens[:10]):
                    fail(**cell, why="top10", store=s["tokens"][:3], producer=r.readout.tokens[:3])
                else:
                    k_ok += 1
                diffs = [abs(a - b) for a, b in zip(s["scores"], r.readout.scores, strict=False)]
                max_score_diff = max([max_score_diff, *diffs])
            per[key] = {"n": len(fresh), "agree": k_ok}
    return _finish(
        C.CheckResult(
            name="store_vs_producer",
            passed=(n > 0 and n_fail == 0),
            value=(n - n_fail) / n if n else None,
            threshold=1.0,
            n=n,
            details={
                "n_items": n_items,
                "readers": sorted(methods),
                "per_reader_family": per,
                "failures": n_fail,
                "failure_examples": fails,
                "max_abs_score_diff": max_score_diff,
            },
            notes="stored rows (read_family on the bf16 store) vs Producer.read_spec on a fresh "
            "capture with an independently bound reader; scores are reported, not gated",
        ),
        out,
    )


# --------------------------------------------------------------------- capture repeat, backend


def check_capture_repeat(
    backend: Any, family: str = "multihop", *, n_items: int = 3, out: Path | None = None
) -> C.CheckResult:
    """Two captures of the first ``n_items`` items (all 64 layers) are bitwise identical."""
    import torch

    site_list = capture.sites(family, backend.tokenizer, limit=n_items)
    a = capture.capture_sites(backend, site_list)
    b = capture.capture_sites(backend, site_list)
    same = bool(torch.equal(a, b))
    max_abs = float((a.float() - b.float()).abs().max())
    return _finish(
        C.CheckResult(
            name="capture_repeat",
            passed=same,
            value=max_abs,
            threshold=0.0,
            n=len(site_list),
            details={"family": family, "n_items": n_items, "shape": list(a.shape)},
            notes="value = max |difference| between the two captures (bf16 store dtype)",
        ),
        out,
    )


def check_backend(backend: Any, *, out: Path | None = None) -> C.CheckResult:
    """Attention implementation (sdpa/eager, never flash) plus versions and fast-path kernels."""
    import peft
    import torch
    import transformers

    impl = str(getattr(backend.model.config, "_attn_implementation", None))
    ok = impl in ("sdpa", "eager")
    param = next(backend.model.parameters())
    details = {
        "attn_implementation": impl,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "peft": peft.__version__,
        "fla_importable": importlib.util.find_spec("fla") is not None,
        "causal_conv1d_importable": importlib.util.find_spec("causal_conv1d") is not None,
        "device": str(param.device),
        "dtype": str(param.dtype),
        "n_layers": backend.n_layers,
        "model_class": type(backend.model).__name__,
        "cuda_device": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }
    return _finish(
        C.CheckResult(
            name="backend",
            passed=ok and backend.n_layers == C.N_MODEL_LAYERS,
            value=None,
            threshold=None,
            n=1,
            details=details,
        ),
        out,
    )
