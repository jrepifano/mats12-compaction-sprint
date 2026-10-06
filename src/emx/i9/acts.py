"""Activation capture for I9 (implementation plan §2 U3; prereg v0.5 §4.2 Stage 2, §4.5).

Two stores, both under ``contracts.ACTS_DIR`` (gitignored), each a safetensors file (key ``"h"``,
``[n_sites, n_layers, d]``; float32 for ``*_last``, bfloat16 for ``qwen_last64``, see
``STORE_DTYPE``) plus a JSON index ``<stem>.json`` (header, ``ids`` in row order, ``layers``,
``n_tokens`` and ``rollout_start`` per id, and one ``ActSite`` per row):

- ``<model>_last``: the residual at the **last prompt token** at the model's probe ladder
  (Qwen ``LADDER``, Gemma ``GEMMA_LADDER``), one site per scenario. Feeds the probes.
- ``qwen_last64``: Qwen's **last 64 prompt positions** at ``LADDER``, resolved with the
  benchmark's own rule (``ReadSpec(..., "chat", {"kind": "last_n", "n": 64}, ...)`` through
  ``wsbench.produce.render.render`` and ``producer._positions``, as ``emx.i1a.capture`` does).
  Feeds the token readers and the Oracle Lens.

**Layer convention.** Benchmark layer L is the output of decoder block ``L`` (the residual after
that block; ``wsbench.produce.backend``), the convention I1a captured with. We capture it with
wsbench's ``Backend.capture`` (forward hooks on the decoder blocks), **not** with HF
``output_hidden_states``: in transformers 5.17 ``hidden_states[i + 1]`` is block ``i``'s output
for every block but the last, whose entry is overwritten with the final-norm output
(``utils/output_capturing.py``, ``tie_last_hidden_states=True``). Both ladders end at the last
block (Qwen 63, Gemma 59), so ``output_hidden_states`` would silently read the normed stream
there. For Gemma 4 (``Gemma4ForConditionalGeneration``) the blocks are
``model.language_model.layers`` (the text model), which ``Backend.blocks`` already resolves.

**Prompt only.** Every render is the single I9 renderer ``emx.i9.render.render_ids`` (thinking
off, no prefill), and ``rollout_start = len(ids)`` (implementation plan §1.4): every captured
position is ``< rollout_start`` by construction, and the index records it so the readers can
assert it again at write time.

The prereg §4.5 check "same activations as I1a" is ``i1a_reproduction_check`` (a reproduction of
I1a's committed logit-lens readouts), plus ``i1a_cosine_check`` when I1a's stored activations are
supplied; see the comment above them.

Import-light: torch, transformers, safetensors and wsbench are imported inside functions.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from emx.i9 import contracts as C

KEY = "h"
FAMILY = "agentic_misalignment"  # the wsbench family whose read plan and readouts we follow
LADDERS: dict[str, tuple[int, ...]] = {"qwen": C.LADDER, "gemma": C.GEMMA_LADDER}
N_BLOCKS: dict[str, int] = {"qwen": C.QWEN_LAYERS, "gemma": C.GEMMA_LAYERS}
D_MODEL: dict[str, int] = {"qwen": C.QWEN_D_MODEL, "gemma": C.GEMMA_D_MODEL}
LAYER_CONVENTION = (
    "layer L = output of decoder block L (wsbench Backend.capture forward hook); not HF "
    "output_hidden_states, whose last entry is the final-norm output"
)
I1A_CHECK_FAMILY = "multihop"  # I1a family of the I1a reference checks (see the section below)
I1A_CHECK_N_ITEMS = 3

# The last-token store is float32 so emx.i9.predict can read it with safetensors.numpy (numpy has
# no bfloat16); lossless, since the model runs in bf16. The last-64 store (torch readers only,
# ~0.75 GB) stays bfloat16 like I1a's.
STORE_DTYPE: dict[str, str] = {"last": "float32", "last64": "bfloat16"}

RenderFn = Callable[[C.Scenario], list[int]]


# --------------------------------------------------------------------- paths and rows


def last_path(model: str) -> Path:
    return C.ACTS_DIR / f"{model}_last.safetensors"


def last64_path() -> Path:
    return C.ACTS_DIR / "qwen_last64.safetensors"


def index_path(acts_path: Path) -> Path:
    """``<stem>.json`` next to the store (``qwen_last.json``), the layout emx.i9.predict reads."""
    return Path(acts_path).with_suffix(".json")


@dataclass(frozen=True)
class ActSite:
    """One captured position: row ``index`` of the store. ``pos`` is the absolute index into the
    render (``last_n`` reports absolute indices), ``token`` is ``tokenizer.decode([id])`` there
    (wsbench ``Row.token``), ``rollout_start = n_tokens`` (the first reply token)."""

    index: int
    id: str  # scenario id (= the wsbench item id / label)
    pos: int
    token: str
    n_tokens: int
    rollout_start: int
    ids_sha256: str  # of the full render, for the sampler-vs-capture ids check


def ids_sha256(ids: Sequence[int]) -> str:
    """sha256 of the comma-joined token ids (the sampler's prompt ids hash the same way)."""
    return hashlib.sha256(",".join(str(int(i)) for i in ids).encode()).hexdigest()


# --------------------------------------------------------------------- backend


def load_backend(
    model: str, model_dir: str | Path, *, device: str = "cuda", dtype: str = C.DTYPE
) -> Any:
    """wsbench ``Backend`` around a local model directory at the pinned revision, sdpa attention,
    no network. Qwen reuses ``emx.i1a.capture.load_backend``; Gemma 4 loads as
    ``Gemma4ForConditionalGeneration`` (``AutoModelForCausalLM``'s mapping) the same way."""
    if model == "qwen":
        from emx.i1a import capture

        bk = capture.load_backend(model_dir, device=device, dtype=dtype)
    elif model == "gemma":
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from wsbench.produce.backend import Backend

        tok = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        m = AutoModelForCausalLM.from_pretrained(
            str(model_dir),
            dtype=getattr(torch, dtype),
            attn_implementation=C.ATTN_IMPLEMENTATION,
            device_map=device,
            local_files_only=True,
        )
        m.eval()
        bk = Backend(model=m, tokenizer=tok, device=device, model_id=C.GEMMA_ID)
    else:
        raise KeyError(f"unknown model {model!r}; known {C.MODELS}")
    if bk.n_layers != N_BLOCKS[model]:
        raise ValueError(f"{model}: {bk.n_layers} decoder blocks, expected {N_BLOCKS[model]}")
    return bk


def default_render(model: str, tokenizer: Any) -> RenderFn:
    """``emx.i9.render.render_ids(model, s, thinking=False, tokenizer=tokenizer)`` (U2's single
    renderer, imported lazily)."""
    from emx.i9 import render as R

    return lambda s: list(R.render_ids(model, s, thinking=False, tokenizer=tokenizer))


# --------------------------------------------------------------------- sites


def sites_last(
    scenarios: Sequence[C.Scenario], tokenizer: Any, render_fn: RenderFn
) -> list[tuple[ActSite, list[int]]]:
    """One site per scenario at its last prompt token, in the given order."""
    out = []
    for s in scenarios:
        ids = list(render_fn(s))
        if not ids:
            raise ValueError(f"{s.id}: empty render")
        n = len(ids)
        site = ActSite(len(out), s.id, n - 1, tokenizer.decode([ids[-1]]), n, n, ids_sha256(ids))
        out.append((site, ids))
    return out


def read_spec(s: C.Scenario, n: int = C.N_READ_POSITIONS) -> Any:
    """The scenario's wsbench read plan row: the agentic_misalignment family's ``chat`` render
    (``readplan.plan`` reads ``{"kind": "all"}``; I9 reads the last ``n``)."""
    from wsbench.readplan import ReadSpec

    return ReadSpec(
        FAMILY,
        s.id,
        "chat",
        {"kind": "last_n", "n": n},
        list(C.LADDER),
        text=s.text,
        system=s.system or None,
        note=f"I9: the last {n} prompt positions of the chat render",
    )


def sites_last_n(
    scenarios: Sequence[C.Scenario],
    tokenizer: Any,
    render_fn: RenderFn,
    n: int = C.N_READ_POSITIONS,
) -> list[tuple[ActSite, list[int]]]:
    """The last ``n`` prompt positions of each scenario, resolved by the benchmark's own code,
    in scenario then position order. The wsbench render must equal ``render_fn``'s ids (the
    single renderer; Qwen thinking off), else raise."""
    from wsbench.produce.producer import _positions
    from wsbench.produce.render import render

    out: list[tuple[ActSite, list[int]]] = []
    for s in scenarios:
        spec = read_spec(s, n)
        rendered = render(spec, tokenizer)
        ids = list(render_fn(s))
        if rendered.ids != ids:
            raise AssertionError(f"{s.id}: wsbench chat render != render_ids (single renderer)")
        m = len(ids)
        h = ids_sha256(ids)
        for p in _positions(spec.positions, rendered.tokens):
            out.append((ActSite(len(out), s.id, p, rendered.decoded[p], m, m, h), ids))
    return out


def _by_item(site_list: list[tuple[ActSite, list[int]]]) -> Iterator[tuple[list[ActSite], list]]:
    i = 0
    while i < len(site_list):
        j = i
        while j < len(site_list) and site_list[j][0].id == site_list[i][0].id:
            j += 1
        yield [s for s, _ in site_list[i:j]], site_list[i][1]
        i = j


# --------------------------------------------------------------------- capture


def capture(backend: Any, site_list: list[tuple[ActSite, list[int]]], layers: Sequence[int]) -> Any:
    """``[n_sites, len(layers), d]`` bf16 on CPU: one ``Backend.capture`` forward per scenario
    (block-output hooks, see the module docstring)."""
    import torch

    layers = list(layers)
    out: Any = None
    k = 0
    for rows, ids in _by_item(site_list):
        acts = backend.capture(ids, layers, [r.pos for r in rows])
        stacked = torch.stack([acts[L] for L in layers], dim=1)  # [n_pos, n_layers, d] fp32
        if out is None:
            out = torch.empty(len(site_list), len(layers), stacked.shape[-1], dtype=torch.bfloat16)
        out[k : k + len(rows)] = stacked.to(torch.bfloat16)
        k += len(rows)
    if out is None:
        raise ValueError("no sites to capture")
    return out


def _index_doc(model: str, kind: str, layers: Sequence[int], rows: list[ActSite]) -> str:
    doc = {
        "model": model,
        "model_id": C.MODEL_ID[model],
        "model_rev": C.MODEL_REV[model],
        "thinking": False,
        "kind": kind,
        "layers": list(layers),
        "layer_convention": LAYER_CONVENTION,
        "dtype": STORE_DTYPE[kind],
        "ids": [r.id for r in rows],  # scenario id of each store row, in row order
        "n_tokens": {r.id: r.n_tokens for r in rows},
        "rollout_start": {r.id: r.rollout_start for r in rows},
        "rows": [asdict(r) for r in rows],
    }
    return json.dumps(doc, indent=0, ensure_ascii=False) + "\n"


def write_store(
    backend: Any,
    site_list: list[tuple[ActSite, list[int]]],
    *,
    model: str,
    kind: str,
    layers: Sequence[int],
    path: Path,
    overwrite: bool = False,
) -> Path:
    """Capture ``site_list`` into ``path`` + its index. Skipped (resumable) when both exist and
    the stored index equals the one this call would write."""
    from safetensors.torch import save_file

    rows = [s for s, _ in site_list]
    if any(r.index != i for i, r in enumerate(rows)):
        raise ValueError("site indices must be 0..n-1 in order")
    if any(not 0 <= r.pos < r.rollout_start for r in rows):
        raise AssertionError("a captured position is not a prompt position")
    want = _index_doc(model, kind, layers, rows)
    ipath = index_path(path)
    if not overwrite and path.exists() and ipath.exists() and ipath.read_text("utf-8") == want:
        return path
    h = capture(backend, site_list, layers)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    meta = {"model": model, "kind": kind, "n_sites": str(len(rows)), "model_id": C.MODEL_ID[model]}
    import torch

    h = h.to(getattr(torch, STORE_DTYPE[kind])).contiguous()  # bf16 values; contiguous on save
    save_file({KEY: h}, str(tmp), metadata=meta)
    os.replace(tmp, path)
    ipath.write_text(want, encoding="utf-8")
    return path


def capture_last(
    backend: Any,
    model: str,
    scenarios: Sequence[C.Scenario],
    *,
    render_fn: RenderFn | None = None,
    path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Last-prompt-token residuals of ``scenarios`` at ``LADDERS[model]`` -> ``last_path``."""
    render_fn = render_fn or default_render(model, backend.tokenizer)
    sl = sites_last(scenarios, backend.tokenizer, render_fn)
    return write_store(
        backend,
        sl,
        model=model,
        kind="last",
        layers=LADDERS[model],
        path=path or last_path(model),
        overwrite=overwrite,
    )


def capture_last64(
    backend: Any,
    scenarios: Sequence[C.Scenario],
    *,
    render_fn: RenderFn | None = None,
    path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Qwen's last ``N_READ_POSITIONS`` prompt positions at ``LADDER`` -> ``last64_path``."""
    render_fn = render_fn or default_render("qwen", backend.tokenizer)
    sl = sites_last_n(scenarios, backend.tokenizer, render_fn)
    return write_store(
        backend,
        sl,
        model="qwen",
        kind="last64",
        layers=C.LADDER,
        path=path or last64_path(),
        overwrite=overwrite,
    )


def load_index(path: Path) -> tuple[dict[str, Any], list[ActSite]]:
    doc = json.loads(index_path(path).read_text("utf-8"))
    rows = [ActSite(**r) for r in doc.pop("rows")]
    return doc, rows


def load_store(path: Path) -> tuple[dict[str, Any], list[ActSite], Any]:
    """``(index header, sites, h)`` with ``h`` ``[n, n_layers, d]`` fp32."""
    from safetensors.torch import load_file

    meta, rows = load_index(path)
    h = load_file(str(path))[KEY]
    if h.shape[0] != len(rows) or h.shape[1] != len(meta["layers"]):
        raise ValueError(f"{path}: store {tuple(h.shape)} vs {len(rows)} rows x layers")
    return meta, rows, h.float()


def rollout_starts(rows: Sequence[ActSite]) -> dict[str, int]:
    """``{scenario id: rollout_start}`` from an index (one value per id, else raise)."""
    out: dict[str, int] = {}
    for r in rows:
        if out.setdefault(r.id, r.rollout_start) != r.rollout_start:
            raise ValueError(f"{r.id}: inconsistent rollout_start in the index")
    return out


# --------------------------------------------------------------------- the I1a reference checks
#
# Prereg §4.5 "same activations as I1a", on Qwen. No I9 scenario overlaps I1a (I1a read the
# multihop, basic_readout_mt and hallucination families, never agentic_misalignment), so the check
# re-renders I1a's own sites and runs them through *our* capture path (``capture`` here, bf16
# store). I1a's stored activations no longer exist (the I1a pod was terminated without pulling
# ``acts/``), but I1a's logit-lens readouts of them are committed, so the check reproduces those:
#
# - ``i1a_reproduction_check`` (always): the first ``I1A_CHECK_N_ITEMS`` multihop sites whose
#   read site is the render's last token, captured with ``capture`` at I1a's multihop layers
#   (``emx.i1a.capture.family_layers``), read with the logit lens exactly as
#   ``emx.i1a.token_readers.read_family`` read I1a's store (``bind_reader`` + ``LogitLens.read``
#   on the bf16 store as fp32, rows via wsbench ``Row.contract``), and compared with I1a's
#   committed ``results/I1a/readouts/logit_lens/multihop.jsonl``. That file's sha256 must equal
#   the line in I1a's ``results/I1a/MANIFEST.tokens.sha256`` first, else it refuses to run.
# - ``i1a_cosine_check`` (only when I1a's stored ``results/I1a/acts/multihop.safetensors`` is
#   supplied and hashes to I1a's capture manifest): the raw per-(item, layer) cosine.

I1A_REPRO_NAME = "smoke_i1a_reproduction"
# Pass iff, per (item, layer), the top-10 token lists are identical and every top-10 score is
# within this many logits of I1a's (bf16 tolerance; the contract rounds scores to 4 decimals),
# and each cell is closer to I1a's row at its own layer than at the neighbouring ladder layers
# (``layer_discrimination``: the layer-convention rule, amendment B review round 2)
I1A_REPRO_SCORE_TOL = 0.05  # ~1.5 bf16 ulps at a logit of ~7 (one ulp there is 2^-5)
I1A_READER = "logit_lens"
I1A_READOUTS_SHA256 = "17beaf2daf66c6c7fd1fd2c45e20a09569639214947d2e1318d47479dce003c5"  # = I1a manifest
# ^ results/I1a/readouts/logit_lens/multihop.jsonl as results/I1a/MANIFEST.tokens.sha256 lists
#   it (1,100 rows); informational: the check verifies against the manifest it is given


class I1aReferenceError(RuntimeError):
    """I1a's stored reference is missing, or is not the file I1a's manifest lists."""


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _i1a_manifest_sha(rel_path: str, manifest: Path | None = None) -> str | None:
    """The sha256 an I1a shasum manifest (default: the capture step's) lists for ``rel_path``."""
    from emx.i1a import contracts as I

    man = Path(manifest) if manifest is not None else I.manifest_path("capture")
    if not man.exists():
        return None
    for line in man.read_text().splitlines():
        sha, _, p = line.partition("  ")
        if p.lstrip("*") == rel_path:
            return sha
    return None


def i1a_readouts_rel(family: str = I1A_CHECK_FAMILY) -> str:
    return f"results/I1a/readouts/{I1A_READER}/{family}.jsonl"


def verified_i1a_readouts(
    family: str = I1A_CHECK_FAMILY, *, path: Path | None = None, manifest: Path | None = None
) -> tuple[Path, str, Path]:
    """``(path, sha256, manifest)`` of I1a's committed logit-lens readouts of ``family``, after
    checking the file hashes to the line I1a's ``MANIFEST.tokens.sha256`` lists for it; raises
    ``I1aReferenceError`` otherwise (the check must not run against anything else)."""
    from emx.i1a import contracts as I

    path = Path(path) if path is not None else I.readouts_path(I1A_READER, family)
    manifest = Path(manifest) if manifest is not None else I.manifest_path("tokens")
    rel = i1a_readouts_rel(family)
    want = _i1a_manifest_sha(rel, manifest)
    if want is None and not manifest.exists() and family == I1A_CHECK_FAMILY:
        # On the pod: pod_push.sh cannot ship MANIFEST.tokens.sha256 (its secret-name filter
        # matches "token"), so the pinned sha256 stands in; a laptop test checks the pin equals
        # the manifest's line.
        want = I1A_READOUTS_SHA256
    if want is None:
        raise I1aReferenceError(f"{manifest}: missing, or lists no {rel}; refusing to run")
    if not path.exists():
        raise I1aReferenceError(f"{path}: I1a's {rel} is missing; refusing to run")
    got = _sha_file(path)
    if got != want:
        raise I1aReferenceError(
            f"{path}: sha256 {got[:12]} is not {want[:12]} ({manifest.name}); refusing to run"
        )
    return path, got, manifest


def i1a_check_sites(
    tokenizer: Any, *, family: str = I1A_CHECK_FAMILY, n_items: int = I1A_CHECK_N_ITEMS
) -> tuple[list[tuple[Any, list[int]]], dict[str, str | None]]:
    """The first ``n_items`` sites of I1a's ``family`` read at the render's last token (I1a's own
    ``emx.i1a.capture.sites``), and the hash of the whole regenerated I1a index next to the one
    I1a's ``MANIFEST.capture.sha256`` lists (None when that manifest is absent)."""
    from emx.i1a import capture as I1cap

    all_sites = I1cap.sites(family, tokenizer)
    idx_text = I1cap._index_lines([s for s, _ in all_sites])
    idx = {
        "index_sha256": hashlib.sha256(idx_text.encode("utf-8")).hexdigest(),
        "index_sha256_i1a_manifest": _i1a_manifest_sha(f"results/I1a/acts/{family}.index.jsonl"),
    }
    chosen = [(s, ids) for s, ids in all_sites if s.abs_pos == s.n_tokens - 1][:n_items]
    if len(chosen) < n_items:
        raise ValueError(f"{family}: only {len(chosen)} last-token sites")
    return chosen, idx


def _act_sites(chosen: list[tuple[Any, list[int]]]) -> list[tuple[ActSite, list[int]]]:
    return [
        (ActSite(i, s.id, s.abs_pos, s.token, s.n_tokens, s.n_tokens, ids_sha256(ids)), ids)
        for i, (s, ids) in enumerate(chosen)
    ]


def reproduce_i1a_logit_lens(
    backend: Any, chosen: list[tuple[Any, list[int]]], layers: Sequence[int]
) -> list[dict[str, Any]]:
    """Contract rows for ``chosen`` (I1a ``SiteRow``s) at ``layers``: our ``capture`` (bf16, as
    stored), then the logit lens as ``emx.i1a.token_readers.read_family`` applies it to I1a's
    store (``bind_reader``; ``LogitLens.read`` on the fp32 view; ``Row(id, layer, pos, token)``
    with I1a's plan ``pos``), in I1a's order: items, then layers."""
    import torch
    from wsbench.produce.producer import Row

    from emx.i1a import token_readers as tr

    layers = list(layers)
    h = capture(backend, _act_sites(chosen), layers).float()  # load_acts' bf16 -> fp32
    reader = tr.bind_reader(I1A_READER, backend)
    rows: list[dict[str, Any]] = []
    with torch.inference_mode():
        for k, (s, _) in enumerate(chosen):
            for j, L in enumerate(layers):
                rows.append(Row(s.id, L, s.pos, s.token, reader.read(h[k, j], L)).contract())
    return rows


def _same_topk(
    toks_a: Sequence[str], sc_a: Sequence[float], toks_b: Sequence[str], sc_b: Sequence[float],
    tol: float,
) -> tuple[bool, float]:
    """Two top-k lists agree up to bf16 noise (rule fixed 2026-10-04, before any pod run):
    scores are compared per token, not per rank, so near-tied tokens may swap order; a token may
    enter or leave the list only at the boundary, i.e. if its score is within ``tol`` of the
    other list's k-th score. Returns (tokens agree, max |score difference| over shared tokens)."""
    a = {t: float(x) for t, x in zip(toks_a, sc_a, strict=True)}
    b = {t: float(x) for t, x in zip(toks_b, sc_b, strict=True)}
    if len(a) != len(toks_a) or len(b) != len(toks_b):  # duplicate token strings: exact order
        same = list(toks_a) == list(toks_b)
        d = max(abs(float(x) - float(y)) for x, y in zip(sc_a, sc_b, strict=True))
        return same, d
    shared = a.keys() & b.keys()
    d = max((abs(a[t] - b[t]) for t in shared), default=float("inf"))
    kth_a, kth_b = min(a.values()), min(b.values())
    only_a_ok = all(a[t] <= kth_b + tol for t in a.keys() - b.keys())
    only_b_ok = all(b[t] <= kth_a + tol for t in b.keys() - a.keys())
    return bool(shared) and only_a_ok and only_b_ok, d


def compare_token_rows(
    ours: Sequence[dict[str, Any]], stored: Sequence[dict[str, Any]], *, tol: float
) -> tuple[bool, dict[str, Any]]:
    """Per (id, layer) of ``ours``: the stored row exists once with the same ``pos`` and
    ``token``, the same ``tokens`` list, and every score within ``tol``. Returns ``(passed,
    details)``; ``details["max_abs_delta"]`` is the largest score difference over all cells."""
    ref: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in stored:
        ref.setdefault((r["id"], int(r["layer"])), []).append(r)
    missing: list[str] = []
    site_diff: list[str] = []
    token_diff: list[str] = []
    score_diff: list[str] = []
    per_cell: dict[str, float] = {}
    max_d = 0.0
    for r in ours:
        cell = f"{r['id']}@L{r['layer']}"
        got = ref.get((r["id"], int(r["layer"])), [])
        if len(got) != 1:
            (missing if not got else site_diff).append(cell)
            continue
        s = got[0]
        if (s["pos"], s["token"]) != (r["pos"], r["token"]):
            site_diff.append(cell)
        if len(s["scores"]) != len(r["scores"]) or not r["scores"]:
            score_diff.append(cell)
            continue
        ok_tokens, d = _same_topk(r["tokens"], r["scores"], s["tokens"], s["scores"], tol)
        if not ok_tokens:
            token_diff.append(cell)
        per_cell[cell] = round(d, 6)
        max_d = max(max_d, d)
        if d > tol:
            score_diff.append(cell)
    passed = bool(ours) and not (missing or site_diff or token_diff or score_diff)
    return passed, {
        "max_abs_delta": round(max_d, 6),
        "tolerance": tol,
        "n_cells": len(ours),
        "missing": missing,
        "site_differs": site_diff,
        "tokens_differ": token_diff,
        "scores_over_tolerance": score_diff,
        "abs_delta": per_cell,
    }


def _row_delta(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Max |score difference| over the tokens two readout rows share (inf if none)."""
    return _same_topk(a["tokens"], a["scores"], b["tokens"], b["scores"], 0.0)[1]


def layer_discrimination(
    ours: Sequence[dict[str, Any]], stored: Sequence[dict[str, Any]], layers: Sequence[int]
) -> tuple[bool, dict[str, Any]]:
    """The layer-convention rule (amendment B review round 2): the site-index hash proves site
    identity, not that our capture reads the same block output as I1a's. For every (item, layer
    L) of ``ours``, the max per-token |score difference| against I1a's stored row at L must be
    strictly smaller than against I1a's stored rows at the neighbouring ladder layers (the
    previous and next entries of ``layers``, i.e. L-4 and L+4, where present), each comparison
    over the tokens the two rows share (no shared token counts as an infinite difference).
    Returns ``(passed, details)``; ``margins`` holds, per cell, the smallest neighbour difference
    minus the own-layer difference (positive = discriminated)."""
    ref: dict[tuple[str, int], dict[str, Any]] = {}
    for r in stored:
        ref.setdefault((r["id"], int(r["layer"])), r)
    order = list(layers)
    margins: dict[str, float | None] = {}
    failed: list[str] = []
    for r in ours:
        L = int(r["layer"])
        cell = f"{r['id']}@L{L}"
        own = ref.get((r["id"], L))
        k = order.index(L) if L in order else -1
        nbs = [order[j] for j in (k - 1, k + 1) if k >= 0 and 0 <= j < len(order)]
        nb_rows = [ref[(r["id"], n)] for n in nbs if (r["id"], n) in ref]
        if own is None or not nb_rows:  # nothing to discriminate against: the cell fails
            margins[cell] = None
            failed.append(cell)
            continue
        d_own = _row_delta(r, own)
        d_nb = min(_row_delta(r, s) for s in nb_rows)
        margin = d_nb - d_own
        margins[cell] = round(margin, 6) if margin != float("inf") else None
        if not d_own < d_nb:
            failed.append(cell)
    finite = [m for m in margins.values() if m is not None]
    passed = bool(ours) and not failed
    return passed, {
        "rule": "own-layer max |delta| < neighbouring ladder layers' (L-4, L+4) max |delta|",
        "failed": failed,
        "min_margin": round(min(finite), 6) if finite else None,
        "margins": margins,
    }


def _utc() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def i1a_reproduction_check(
    backend: Any,
    *,
    family: str = I1A_CHECK_FAMILY,
    n_items: int = I1A_CHECK_N_ITEMS,
    tol: float = I1A_REPRO_SCORE_TOL,
    readouts: Path | None = None,
    manifest: Path | None = None,
    write: bool = True,
) -> C.CheckResult:
    """Prereg §4.5 "same activations as I1a" as a reproduction of I1a's committed logit-lens
    readouts (see the section comment). Refuses (``I1aReferenceError``) before any forward pass
    unless the stored file hashes to I1a's manifest. Passes iff every (item, layer) cell has the
    identical top-10 token list and every score within ``tol`` (the tolerance rule), every cell
    is closer to I1a's row at its own layer than to the neighbouring ladder layers' rows (the
    discrimination rule, ``layer_discrimination``; amendment B review round 2), and the
    regenerated I1a site index hashes to I1a's capture manifest (when that manifest is present).
    ``value`` is the max |score difference|."""
    from emx.i1a import capture as I1cap

    path, sha, man = verified_i1a_readouts(family, path=readouts, manifest=manifest)
    chosen, idx = i1a_check_sites(backend.tokenizer, family=family, n_items=n_items)
    layers = I1cap.family_layers(family)
    ours = reproduce_i1a_logit_lens(backend, chosen, layers)
    ids = {s.id for s, _ in chosen}
    stored = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    mine = [r for r in stored if r["id"] in ids]
    tol_ok, det = compare_token_rows(ours, mine, tol=tol)
    disc_ok, disc = layer_discrimination(ours, mine, layers)
    passed = tol_ok and disc_ok
    notes = (
        f"our capture path + I1a's logit lens on {len(chosen)} {family} sites x {len(layers)} "
        f"layers vs I1a's committed readouts (sha256 verified against {man.name}); "
        f"tolerance rule {'passed' if tol_ok else 'FAILED'}, layer discrimination "
        f"{'passed' if disc_ok else 'FAILED'}"
    )
    if idx["index_sha256_i1a_manifest"] is None:
        notes += "; I1a's capture manifest is absent, so the site-index hash was not checked"
    elif idx["index_sha256"] != idx["index_sha256_i1a_manifest"]:
        passed = False
        notes += "; the regenerated I1a site index does not hash to I1a's capture manifest"
    res = C.CheckResult(
        name=I1A_REPRO_NAME,
        passed=passed,
        value=det["max_abs_delta"],
        threshold=tol,
        n=len(ours),
        details={
            **det,
            "tolerance_passed": tol_ok,
            "discrimination_passed": disc_ok,
            "discrimination": disc,
            "family": family,
            "reader": I1A_READER,
            "layers": list(layers),
            "ids": [s.id for s, _ in chosen],
            "positions": [s.abs_pos for s, _ in chosen],
            "reference": i1a_readouts_rel(family),
            "reference_sha256": sha,
            **idx,
        },
        notes=notes,
        utc=_utc(),
    )
    if write:
        res.write()
    return res


def cosine_check(
    ours: Any,
    ref: Any,
    *,
    layers: Sequence[int],
    ids: Sequence[str],
    name: str = "i1a_cosine",
    details: dict[str, Any] | None = None,
) -> C.CheckResult:
    """Per-(item, layer) cosine between ``ours`` and ``ref`` (both ``[n, len(layers), d]``);
    passes when the minimum exceeds ``I1A_COSINE_MIN``."""
    import torch

    a, b = ours.float(), ref.float()
    if a.shape != b.shape:
        raise ValueError(f"shapes differ: {tuple(a.shape)} vs {tuple(b.shape)}")
    cos = torch.nn.functional.cosine_similarity(a, b, dim=-1)  # [n, n_layers]
    lo = float(cos.min())
    per = {
        f"{i}@L{L}": round(float(cos[r, c]), 6)
        for r, i in enumerate(ids)
        for c, L in enumerate(layers)
    }
    return C.CheckResult(
        name=name,
        passed=lo > C.I1A_COSINE_MIN,
        value=round(lo, 6),
        threshold=C.I1A_COSINE_MIN,
        n=int(cos.numel()),
        details={**(details or {}), "layers": list(layers), "ids": list(ids), "cosine": per},
    )


def i1a_stored_acts_ok(family: str = I1A_CHECK_FAMILY) -> bool:
    """I1a's stored activations of ``family`` are present and hash to I1a's capture manifest."""
    from emx.i1a import contracts as I

    p = I.acts_path(family)
    want = _i1a_manifest_sha(f"results/I1a/acts/{family}.safetensors")
    return p.exists() and want is not None and _sha_file(p) == want


def i1a_cosine_check(
    backend: Any,
    *,
    family: str = I1A_CHECK_FAMILY,
    n_items: int = I1A_CHECK_N_ITEMS,
    write: bool = True,
) -> C.CheckResult:
    """The raw-activation cosine against I1a's **stored** activations
    (``results/I1a/acts/<family>.safetensors``, all 64 layers, bf16), only when that file is
    supplied and hashes to I1a's capture manifest (else ``I1aReferenceError``): the same sites as
    ``i1a_reproduction_check``, our ``capture`` at ``LADDER``, cosine above ``I1A_COSINE_MIN``."""
    import torch

    from emx.i1a import capture as I1cap
    from emx.i1a import contracts as I

    if not i1a_stored_acts_ok(family):
        raise I1aReferenceError(
            f"{I.acts_path(family)}: missing or not the file I1a's capture manifest lists"
        )
    chosen, idx = i1a_check_sites(backend.tokenizer, family=family, n_items=n_items)
    ours = capture(backend, _act_sites(chosen), C.LADDER)
    _, h = I1cap.load_acts(family)  # [n, 64, d] fp32, rows = I1a index order
    ref = h[[s.index for s, _ in chosen]][:, list(C.LADDER), :]
    res = cosine_check(
        ours,
        ref.to(torch.bfloat16),
        layers=C.LADDER,
        ids=[s.id for s, _ in chosen],
        details={
            "family": family,
            "reference": "stored",
            "positions": [s.abs_pos for s, _ in chosen],
            **idx,
        },
    )
    if idx["index_sha256_i1a_manifest"] is not None and (
        idx["index_sha256"] != idx["index_sha256_i1a_manifest"]
    ):
        res.passed = False
        res.notes = "the regenerated I1a index does not hash to I1a's manifest; "
    res.notes += "reference = I1a's stored activations"
    res.utc = _utc()
    if write:
        res.write()
    return res
