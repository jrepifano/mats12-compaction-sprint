"""The four reconstructor checks that gate the best-FVE rule (prereg v0.4 §4.3; implementation
plan §0 "Best-FVE rule gates" and §2 U4; contracts FVE block after gpt-6-sol round 3).

- ``check_ar_live``: every saved LoRA tensor landed on adapter "ar", the heads.pt tags equal the
  tokenizer's ``[Layer NN]`` tags, and the head weights are [d, d] and [d].
- ``check_ruler``: single-span whitened cos² of the AR on source spans at the hallucination read
  sites lands within ±0.05 of the layer-matched ``meta.json`` ``val_fve`` mean over
  ``RULER_LAYERS``, and every layer within ±0.08 of its own.
- ``check_mismatch``: a gate readout's joint FVE against its own activation beats the FVE of the
  same bullets against another item's activation (same layer) in >= 80% of readouts, overall
  and at every layer present.
- ``check_joint_band``: the mean gate-draw joint FVE lies in 0.487 ± 0.10
  (olens_s3d_rl600 ``run.json`` ``eval_joint_fve_heldout``).

Span anchor. The AR was trained on "sample a position, take the next N tokens" with the target
the residual at the position *preceding* the span (third_party/olens_draft/oracle_lens/pipeline/
multilayer.py ``CroppedPairs``; ``multilayer_reconstructor.py`` notes layer 0 scores at chance
because its target is "which token preceded this span"). So the ruler's default anchor is
``"after"``: span = ids[abs_pos + 1 : abs_pos + 1 + N], gold = the activation at abs_pos. The
``"ending"`` anchor (span = ids[abs_pos - N + 1 : abs_pos + 1]) is always computed as a diagnostic.

Torch and the reconstructor are imported lazily, so ``check_joint_band`` runs on the laptop.
"""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from emx.i1a import contracts as C

if TYPE_CHECKING:
    from collections.abc import Sequence

    from torch import Tensor

    from emx.i1a.fve import ArHandle, Whitener

Anchor = Literal["after", "ending"]
SWITCH_MIN_REL = 1e-3  # "ar" must move the block-63 output by >= 0.1% (relative L2) vs base
PROBE_TEXT = "The capital of France is Paris, and the river through it is the Seine."
VAL_FVE_ATOL = 5e-5  # contracts.AR_VAL_FVE is meta.json val_fve_L* rounded to 4 decimals


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _probe_block_out(h: ArHandle, ids: list[int]) -> Tensor:
    """Block-63 output at the last token of one unpadded prompt, with whatever is active."""
    import torch

    b = h.backend
    got: dict[str, Tensor] = {}

    def hook(_m: Any, _i: Any, o: Any) -> None:
        got["h"] = o[0] if isinstance(o, tuple) else o

    handle = b.blocks[C.AR_READ_BLOCK].register_forward_hook(hook)
    try:
        with torch.no_grad():
            b.model(input_ids=torch.tensor([ids], device=b.device), use_cache=False,
                    logits_to_keep=1)  # fmt: skip
    finally:
        handle.remove()
    return got["h"][0, -1].float().cpu()


def adapter_switch_probe(h: ArHandle) -> dict[str, Any]:
    """Block-63 output of a fixed tagged prompt with the previously active adapter, with the AR
    adapter active, with every adapter disabled, and with the previous one again."""
    from emx.i1a.fve import _active, bare_token_ids, using_adapter

    model = h.backend.model
    prev = _active(model)
    ids = h.tag_ids[h.row_of(44 if 44 in h.layers else h.layers[0])].tolist()
    ids += bare_token_ids(h.tokenizer, PROBE_TEXT)
    before = _probe_block_out(h, ids)
    with using_adapter(model, h.adapter_name):
        ar = _probe_block_out(h, ids)
    with model.disable_adapter():
        base = _probe_block_out(h, ids)
    after = _probe_block_out(h, ids)
    rel = float((ar - base).norm() / base.norm().clamp_min(1e-12))
    return {
        "previous_adapter": prev,
        "restored": _active(model) == prev and bool(torch_allclose(after, before)),
        "ar_vs_base_rel_l2": rel,
        "prev_vs_base_rel_l2": float((before - base).norm() / base.norm().clamp_min(1e-12)),
        "min_rel": SWITCH_MIN_REL,
    }


def torch_allclose(a: Tensor, b: Tensor) -> bool:
    import torch

    return bool(torch.allclose(a, b, rtol=1e-4, atol=1e-5))


def check_ar_live(h: ArHandle, tokenizer: Any, *, write: bool = True) -> C.CheckResult:
    """Adapter live (``fve.load_ar`` already raises when it is not; this records the counts),
    tags equal ``tokenizer(AR_TAG_TEMPLATE.format(n=L), add_special_tokens=False)`` for every AR
    layer, and head shapes [d, d] / [d]."""
    bad_tags = []
    for i, L in enumerate(h.layers):
        want = tokenizer(C.AR_TAG_TEMPLATE.format(n=L), add_special_tokens=False)["input_ids"]
        if h.tag_ids[i].tolist() != list(want):
            bad_tags.append(L)
    d = int(h.backend.unembed.shape[1])
    shapes = {
        "linear.weight": list(h.head.linear.weight.shape),
        "linear.bias": list(h.head.linear.bias.shape),
    }
    shapes_ok = shapes == {"linear.weight": [d, d], "linear.bias": [d]}
    frac = h.n_live / h.n_saved if h.n_saved else 0.0
    probe = adapter_switch_probe(h)
    switch_ok = probe["ar_vs_base_rel_l2"] >= SWITCH_MIN_REL and probe["restored"]
    res = C.CheckResult(
        name="fve_ar_live",
        passed=frac >= C.AR_MIN_LIVE_FRAC and not bad_tags and shapes_ok and switch_ok,
        value=frac,
        threshold=C.AR_MIN_LIVE_FRAC,
        n=h.n_saved,
        details={
            "n_live": h.n_live,
            "n_saved": h.n_saved,
            "adapter": h.adapter_name,
            "layers": list(h.layers),
            "layer_norm": h.layer_norm,
            "tag_mismatch_layers": bad_tags,
            "head_shapes": shapes,
            "d_model": d,
            "switch_probe": probe,
        },
        utc=_utc(),
    )
    if write:
        res.write()
    return res


def ruler_spans(sites: list[tuple[C.SiteRow, list[int]]], anchor: Anchor) -> list[list[int] | None]:
    """One span per site; lengths uniform in [RULER_SPAN_MIN, RULER_SPAN_MAX] drawn in site
    order from ``random.Random(RULER_SPAN_SEED)`` (one draw per site, used by both anchors).
    ``None`` where no token is available (e.g. the read site is the last token)."""
    rng = random.Random(C.RULER_SPAN_SEED)
    out: list[list[int] | None] = []
    for site, ids in sites:
        n = rng.randint(C.RULER_SPAN_MIN, C.RULER_SPAN_MAX)
        p = site.abs_pos
        if not 0 <= p < len(ids):
            raise ValueError(f"{site.id}: abs_pos {p} outside {len(ids)} ids")
        span = ids[p + 1 : p + 1 + n] if anchor == "after" else ids[max(0, p - n + 1) : p + 1]
        out.append(list(span) if span else None)
    return out


def _ruler_means(
    h: ArHandle,
    whiteners: dict[int, Whitener],
    sites: list[tuple[C.SiteRow, list[int]]],
    acts: tuple[list[C.SiteRow], Tensor],
    layers: Sequence[int],
    test_layers: Sequence[int],
    anchor: Anchor,
    micro_batch: int,
) -> tuple[float, dict[int, float], int, int]:
    import torch

    from emx.i1a.fve import gold_lookup, span_fve

    row_of_site, col_of_layer, H = gold_lookup(acts, layers)
    spans = ruler_spans(sites, anchor)
    id_rows, lays, golds = [], [], []
    n_skipped = 0
    for (site, _), span in zip(sites, spans, strict=True):
        if span is None:
            n_skipped += 1
            continue
        for L in test_layers:
            id_rows.append(span)
            lays.append(int(L))
            golds.append(H[row_of_site[site.index], col_of_layer[int(L)]])
    if not id_rows:
        return float("nan"), {}, 0, n_skipped
    fve = span_fve(h, whiteners, id_rows, torch.stack(golds), lays, micro_batch=micro_batch)
    lay_t = torch.tensor(lays)
    per_layer = {int(L): float(fve[lay_t == int(L)].mean()) for L in test_layers}
    return float(fve.mean()), per_layer, len(id_rows), n_skipped


def meta_val_fve(meta: dict[str, Any]) -> dict[int, float]:
    """Every ``val_fve_L<layer>`` number anywhere in the AR's meta.json (nested dicts walked)."""
    out: dict[int, float] = {}

    def walk(d: Any) -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                m = re.fullmatch(r"val_fve_L(\d+)", str(k))
                if m and isinstance(v, int | float):
                    out[int(m.group(1))] = float(v)
                else:
                    walk(v)

    walk(meta)
    return out


def check_ruler(
    h: ArHandle,
    whiteners: dict[int, Whitener],
    *,
    sites: list[tuple[C.SiteRow, list[int]]],
    acts: tuple[list[C.SiteRow], Tensor],
    layers: Sequence[int],
    test_layers: Sequence[int] = C.RULER_LAYERS,
    anchor: Anchor = "after",
    micro_batch: int = 32,
    write: bool = True,
) -> C.CheckResult:
    """Single-span FVE on source spans at ``sites`` (each with its rendered token ids) against
    the true activations in ``acts`` (columns named by ``layers``), at ``test_layers``. Passes iff
    |overall mean - mean(AR_VAL_FVE[test_layers])| <= RULER_TOLERANCE (value = that |difference|)
    and every layer's |mean - AR_VAL_FVE[layer]| <= RULER_LAYER_TOLERANCE. Each layer has the
    same sites, so the overall mean weights the layers equally, like the target. First, the
    loaded meta.json's ``val_fve_L*`` must equal ``AR_VAL_FVE`` (else it fails unscored)."""
    got = meta_val_fve(h.meta)
    bad_meta = {
        str(L): got.get(L)
        for L, v in C.AR_VAL_FVE.items()
        if L not in got or abs(got[L] - v) > VAL_FVE_ATOL
    }
    if bad_meta:
        res = C.CheckResult(
            name="fve_ruler", passed=False, value=None, threshold=C.RULER_TOLERANCE, n=0,
            details={"meta_mismatch": bad_meta, "contract": {str(k): v for k, v in
                                                             C.AR_VAL_FVE.items()}},
            notes="meta.json val_fve_L* differ from contracts.AR_VAL_FVE; not scored",
            utc=_utc(),
        )  # fmt: skip
        if write:
            res.write()
        return res
    target_pl = {int(L): C.AR_VAL_FVE[int(L)] for L in test_layers}
    target = sum(target_pl.values()) / len(target_pl)
    mean, per_layer, n, n_skipped = _ruler_means(
        h, whiteners, sites, acts, layers, test_layers, anchor, micro_batch
    )
    other: Anchor = "ending" if anchor == "after" else "after"
    o_mean, o_per_layer, _, o_skipped = _ruler_means(
        h, whiteners, sites, acts, layers, test_layers, other, micro_batch
    )
    diff = abs(mean - target) if n else float("nan")
    layer_diff = {L: abs(per_layer[L] - target_pl[L]) for L in per_layer}
    bad_layers = [L for L, dv in layer_diff.items() if not dv <= C.RULER_LAYER_TOLERANCE]
    res = C.CheckResult(
        name="fve_ruler",
        passed=(diff <= C.RULER_TOLERANCE and not bad_layers) if n else None,
        value=diff if n else None,
        threshold=C.RULER_TOLERANCE,
        n=n,
        details={
            "anchor": anchor,
            "mean_fve": mean,
            "target_val_fve_mean": target,
            "per_layer": {str(k): v for k, v in per_layer.items()},
            "per_layer_target": {str(k): v for k, v in target_pl.items()},
            "layer_tolerance": C.RULER_LAYER_TOLERANCE,
            "layers_outside_tolerance": bad_layers,
            "n_sites": len(sites),
            "n_skipped_sites": n_skipped,
            "span_len": [C.RULER_SPAN_MIN, C.RULER_SPAN_MAX],
            "span_seed": C.RULER_SPAN_SEED,
            "diagnostic_other_anchor": {
                "anchor": other,
                "mean_fve": o_mean,
                "per_layer": {str(k): v for k, v in o_per_layer.items()},
                "n_skipped_sites": o_skipped,
            },
        },
        notes="" if n else "no site had a span",
        utc=_utc(),
    )
    if write:
        res.write()
    return res


def _mismatch_partner(rows: list[C.SampleRow], rng: random.Random) -> list[int | None]:
    """For each readout, the index (into ``rows``) of another readout at the same layer from a
    different item (falling back to a different site of the same item)."""
    by_layer: dict[int, list[int]] = {}
    for j, r in enumerate(rows):
        by_layer.setdefault(int(r.layer), []).append(j)
    out: list[int | None] = [None] * len(rows)
    for L in sorted(by_layer):
        idx = by_layer[L]
        for j in idx:
            cands = [k for k in idx if rows[k].id != rows[j].id]
            if not cands:
                cands = [k for k in idx if rows[k].site_index != rows[j].site_index]
            out[j] = rng.choice(cands) if cands else None
    return out


def check_mismatch(
    h: ArHandle,
    whiteners: dict[int, Whitener],
    family: str = "hallucination",
    *,
    acts: tuple[list[C.SiteRow], Tensor],
    layers: Sequence[int],
    ids: set[str] | None = None,
    expected_layers: Sequence[int] | None = None,
    micro_batch: int = 32,
    write: bool = True,
) -> C.CheckResult:
    """Gate-draw readouts (sample 0): own-activation joint FVE > the same bullets' joint FVE
    against a partner site's activation (same layer, other item, seeded choice) in >=
    MISMATCH_MIN_FRAC of the readouts that have a bullet, overall and at every expected layer
    (default ``SITE_FAMILY_LAYERS[family]``). An expected layer with no evaluable readout
    (absent from the samples, no partner, or no bullet) fails the check."""
    expected = [int(L) for L in (expected_layers or C.SITE_FAMILY_LAYERS[family])]
    import torch

    from emx.i1a.fve import bullet_images, gold_lookup, joint_fve_from_images, read_samples

    row_of_site, col_of_layer, H = gold_lookup(acts, layers)
    rows = [s for s in read_samples(family) if s.sample_idx == 0 and (ids is None or s.id in ids)]
    rows.sort(key=lambda s: (int(s.layer), s.site_index))
    partner = _mismatch_partner(rows, random.Random(C.SHUFFLE_PERM_SEED))
    keep = [j for j, p in enumerate(partner) if p is not None]
    rows_k = [rows[j] for j in keep]
    lays = [int(s.layer) for s in rows_k]

    def gold(s: C.SampleRow) -> Tensor:
        return H[row_of_site[s.site_index], col_of_layer[int(s.layer)]]

    if not rows_k:
        res = C.CheckResult(
            name="fve_mismatch", passed=False, value=None, threshold=C.MISMATCH_MIN_FRAC, n=0,
            details={"family": family, "layers_missing": expected},
            notes="no gate readouts with a same-layer partner", utc=_utc(),
        )  # fmt: skip
        if write:
            res.write()
        return res
    own_g = torch.stack([gold(s) for s in rows_k])
    mis_g = torch.stack([gold(rows[partner[j]]) for j in keep])  # type: ignore[index]
    vecs, valid = bullet_images(h, [s.text for s in rows_k], lays, micro_batch=micro_batch)
    own, _, has = joint_fve_from_images(vecs, valid, own_g, lays, whiteners)
    mis, _, _ = joint_fve_from_images(vecs, valid, mis_g, lays, whiteners)
    n = int(has.sum())
    win = (own > mis) & has
    frac = int(win.sum()) / n if n else float("nan")
    lay_t = torch.tensor(lays)
    per_layer: dict[str, float | None] = {}
    for L in sorted(set(lays) | set(expected)):
        sel = (lay_t == L) & has
        per_layer[str(L)] = int((win & sel).sum()) / int(sel.sum()) if int(sel.sum()) else None
    missing = [L for L in expected if per_layer.get(str(L)) is None]
    bad_layers = [L for L, v in per_layer.items() if v is not None and v < C.MISMATCH_MIN_FRAC]
    res = C.CheckResult(
        name="fve_mismatch",
        passed=bool(n) and frac >= C.MISMATCH_MIN_FRAC and not bad_layers and not missing,
        value=frac if n else None,
        threshold=C.MISMATCH_MIN_FRAC,
        n=n,
        details={
            "family": family,
            "mean_own_fve": float(own[has].mean()) if n else None,
            "mean_mismatched_fve": float(mis[has].mean()) if n else None,
            "n_readouts": len(rows),
            "n_without_bullets": int((~has).sum()),
            "partner_seed": C.SHUFFLE_PERM_SEED,
            "per_layer": per_layer,
            "layers_below_threshold": bad_layers,
            "expected_layers": expected,
            "layers_missing": missing,
        },
        utc=_utc(),
    )
    if write:
        res.write()
    return res


def check_joint_band(family: str = "hallucination", *, write: bool = True) -> C.CheckResult:
    """Mean gate-draw (sample 0) joint FVE from ``fve_path(family)``, empty readouts counting
    0.0 as in the RL reward, must lie within JOINT_FVE_HELDOUT ± JOINT_FVE_BAND; value = mean."""
    path = C.fve_path(family)
    gate: list[dict[str, Any]] = []
    if path.exists():
        with path.open() as f:
            gate = [r for r in (json.loads(x) for x in f if x.strip()) if r["sample_idx"] == 0]
    if not gate:
        res = C.CheckResult(
            name="fve_joint_band", passed=None, value=None, threshold=C.JOINT_FVE_BAND, n=0,
            notes=f"no gate-draw FveRows in {path}", utc=_utc(),
        )  # fmt: skip
    else:
        mean = sum(float(r["joint_fve"]) for r in gate) / len(gate)
        valid = [float(r["joint_fve"]) for r in gate if r["valid"]]
        by_layer: dict[int, list[float]] = {}
        for r in gate:
            by_layer.setdefault(int(r["layer"]), []).append(float(r["joint_fve"]))
        res = C.CheckResult(
            name="fve_joint_band",
            passed=abs(mean - C.JOINT_FVE_HELDOUT) <= C.JOINT_FVE_BAND,
            value=mean,
            threshold=C.JOINT_FVE_BAND,
            n=len(gate),
            details={
                "family": family,
                "target": C.JOINT_FVE_HELDOUT,
                "mean_valid_only": sum(valid) / len(valid) if valid else None,
                "n_valid": len(valid),
                "per_layer": {str(k): sum(v) / len(v) for k, v in sorted(by_layer.items())},
            },
            utc=_utc(),
        )
    if write:
        res.write()
    return res
