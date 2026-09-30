"""Batched Oracle Lens generation for I1a+ (implementation plan §2 U3; prereg v0.4 §4.1, §4.3,
§4.8).

The recipe is the benchmark's own ``OLens`` (third_party/workspace-bench @ 92d763e,
``src/wsbench/produce/methods.py``): the same carrier prompt and slot (``OLens._carrier``, used
unchanged), the same injection ``alpha * h / ||h||`` cast to the embedding dtype, the same
sampling settings and the same decoding (``batch_decode(skip_special_tokens=True)``, stripped).
Two things are ours:

- ``PublicOLens.bind`` loads the public adapter copy (``agu18dec/olens_and_ar`` /
  ``olens_s3d_rl600``, downloaded elsewhere) from a local directory as a *named* PEFT adapter,
  so a second adapter (the reconstructor, ``"ar"``) can share the base model; and it asserts the
  adapter is live (tensor-name match as in olens_draft ``ar_loader.verify_adapter_live``, plus
  logits on != off).
- ``read_batch`` generates up to ``BATCH`` readouts of ONE layer in one ``generate`` call. All
  sequences share the layer's carrier ids, so there is no padding; each sequence gets its own
  injected vector. The canonical batch order and per-batch seeds are frozen in ``contracts.py``.

Torch, PEFT and wsbench are imported lazily so that importing this module stays cheap.
"""

from __future__ import annotations

import json
import os
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wsbench.produce.methods import OLens, Sampling

from emx.i1a import contracts as C

if TYPE_CHECKING:  # pragma: no cover
    import torch
    from wsbench.produce.backend import Backend

DEFAULT_ADAPTER_NAME = "olens"
SINGLE_SAMPLE_DRAWS = ("gate", "shuffled", "reserve_oldsite", "reserve_draw2")


def default_sampling() -> Sampling:
    """The wsbench ``Sampling`` defaults, taken from contracts (a test asserts they agree)."""
    return Sampling(
        temperature=C.TEMPERATURE,
        top_p=C.TOP_P,
        top_k=C.TOP_K,
        max_new_tokens=C.MAX_NEW_TOKENS,
        k=1,
    )


# --------------------------------------------------------------------------- the reader


@dataclass
class PublicOLens(OLens):
    """wsbench ``OLens`` bound to a local copy of the public adapter as a named PEFT adapter.

    ``prompt``, ``alpha``, ``sampling`` and ``_carrier`` are inherited unchanged; ``read`` (one
    activation at a time) still works and is the reference that ``read_batch`` must match."""

    name: str = "olens"
    lora: str = f"{C.ADAPTER_REPO}@{C.ADAPTER_REV}:{C.OLENS_SUBDIR}"  # provenance only
    alpha: float = C.OLENS_ALPHA
    sampling: Sampling = field(default_factory=default_sampling)
    adapter_name: str = DEFAULT_ADAPTER_NAME
    adapter_dir: Path | None = None
    n_live_tensors: int = 0

    def bind(  # type: ignore[override]
        self,
        backend: Backend,
        adapter_dir: Path | str | None = None,
        adapter_name: str = DEFAULT_ADAPTER_NAME,
    ) -> None:
        """Mount ``adapter_dir`` as adapter ``adapter_name`` and make it active.

        A bare model is wrapped with ``PeftModel.from_pretrained``; a model that is already a
        ``PeftModel`` (e.g. with the reconstructor's ``"ar"`` adapter) gets ``load_adapter``, so
        no other adapter is unloaded. Raises if the adapter does not map onto the model or does
        not change the logits."""
        from peft import PeftModel

        adir = Path(adapter_dir if adapter_dir is not None else self.adapter_dir or "")
        if not (adir / "adapter_config.json").exists():
            raise FileNotFoundError(f"no adapter_config.json in {adir!s}")
        self.adapter_dir, self.adapter_name = adir, adapter_name
        m = backend.model
        if isinstance(m, PeftModel):
            if adapter_name not in m.peft_config:
                m.load_adapter(str(adir), adapter_name=adapter_name)
            m.set_adapter(adapter_name)
        else:
            m = PeftModel.from_pretrained(m, str(adir), adapter_name=adapter_name)
            backend.model = m
        m.eval()
        self._b = backend
        self.n_live_tensors = verify_adapter_tensors(m, adir, adapter_name)
        diff = logit_shift(self, layer=C.FIVE[2])
        if not diff > 0.0:
            raise RuntimeError(f"adapter {adapter_name!r} from {adir} does not change the logits")

    def activate(self) -> None:
        """Make this reader's adapter the active one (another unit may have switched to "ar")."""
        m = self._backend().model
        if getattr(m, "active_adapter", None) != self.adapter_name:
            m.set_adapter(self.adapter_name)

    def read(self, h: Any, layer: int) -> Any:
        """wsbench ``OLens.read`` (one activation), after making this reader's adapter active."""
        self.activate()
        return super().read(h, layer)

    def _backend(self) -> Backend:
        if self._b is None:
            raise RuntimeError("PublicOLens is not bound")
        return self._b


def verify_adapter_tensors(peft: Any, adapter_dir: Path, adapter_name: str) -> int:
    """How many saved LoRA tensors map onto live parameters of ``adapter_name``; raises below
    90%. Ported from olens_draft ``oracle_lens/pipeline/ar_loader.py::verify_adapter_live``
    @ a325900, restricted to one named adapter."""
    from safetensors import safe_open

    tag = f".{adapter_name}."
    live = {n for n, _ in peft.named_parameters() if "lora_" in n and tag in n + "."}
    if not live:
        raise RuntimeError(f"{adapter_dir}: no lora_ parameters for adapter {adapter_name!r}")
    st = Path(adapter_dir) / "adapter_model.safetensors"
    with safe_open(str(st), framework="pt") as f:
        saved = {k for k in f.keys() if "lora_" in k}  # noqa: SIM118 - not a dict

    def stem(k: str) -> str:
        s = k.split("base_model.model.")[-1].replace(".weight", "")
        return s.replace(f".{adapter_name}", "")

    matched = {stem(k) for k in saved} & {stem(n) for n in live}
    if len(matched) < 0.9 * len(saved):
        raise RuntimeError(
            f"{adapter_dir}: only {len(matched)}/{len(saved)} saved LoRA tensors map onto "
            f"adapter {adapter_name!r}"
        )
    return len(matched)


def carrier_embeds(olens: PublicOLens, H: torch.Tensor, layer: int) -> torch.Tensor:
    """[b, T, d] input embeddings: the layer's carrier embedded once and repeated, with row
    ``slot`` of sequence i replaced by ``alpha * H[i] / ||H[i]||`` in the embedding dtype
    (exactly ``OLens.read`` per sequence)."""
    import torch

    b = olens._backend()
    ids, slot = olens._carrier(layer)
    embed = b.model.get_input_embeddings()
    with torch.no_grad():
        e = embed(torch.tensor([ids], device=b.device))
        e = e.expand(H.shape[0], -1, -1).clone()
        v = H.to(b.device).float()
        e[:, slot, :] = (olens.alpha * v / v.norm(dim=-1, keepdim=True).clamp_min(1e-9)).to(e.dtype)
    return e


def logit_shift(olens: PublicOLens, *, layer: int, H: torch.Tensor | None = None) -> float:
    """max |logits(adapter on) - logits(adapter off)| on the layer's carrier (with ``H[0]``
    injected when given, else the plain carrier ids)."""
    import torch

    b = olens._backend()
    olens.activate()
    ids, _ = olens._carrier(layer)
    with torch.no_grad():
        kw: dict[str, Any]
        if H is None:
            kw = {"input_ids": torch.tensor([ids], device=b.device)}
        else:
            kw = {"inputs_embeds": carrier_embeds(olens, H[:1], layer)}
        on = b.model(**kw).logits.float()
        with b.model.disable_adapter():
            off = b.model(**kw).logits.float()
    return float((on - off).abs().max())


def eos_ids(backend: Backend) -> set[int]:
    """The ids ``generate`` stops on (the model's generation config, else the tokenizer's)."""
    gc = getattr(backend.model, "generation_config", None)
    e = getattr(gc, "eos_token_id", None) if gc is not None else None
    if e is None:
        e = backend.tokenizer.eos_token_id
    return {int(x) for x in (e if isinstance(e, list | tuple) else [e])}


def finish_stats(out: torch.Tensor, eos: set[int]) -> list[tuple[int, str]]:
    """Per row of generated ids: (tokens before the first EOS, "eos") or (all, "length")."""
    res = []
    for row in out.tolist():
        k = next((i for i, t in enumerate(row) if t in eos), None)
        res.append((len(row), "length") if k is None else (k, "eos"))
    return res


def _seed_all(seed: int) -> None:
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def read_batch(
    olens: PublicOLens,
    H: torch.Tensor,
    layer: int,
    seed: int,
    *,
    sampling: Sampling | None = None,
) -> list[tuple[str, int, str]]:
    """One ``generate`` over ``H.shape[0]`` copies of the layer's carrier, each with its own
    injected activation; returns ``(text, n_new_tokens, finish)`` per row of ``H``."""
    import torch

    b = olens._backend()
    if H.ndim != 2 or not 1 <= H.shape[0] <= C.BATCH:
        raise ValueError(f"H must be [b <= {C.BATCH}, d], got {tuple(H.shape)}")
    olens.activate()
    s = sampling or olens.sampling
    e = carrier_embeds(olens, H, layer)
    with torch.no_grad():
        _seed_all(seed)
        out = b.model.generate(
            inputs_embeds=e,
            attention_mask=torch.ones(e.shape[0], e.shape[1], device=b.device, dtype=torch.long),
            do_sample=s.temperature > 0,
            temperature=max(s.temperature, 1e-5),
            top_p=s.top_p,
            top_k=s.top_k,
            max_new_tokens=s.max_new_tokens,
            pad_token_id=b.tokenizer.pad_token_id or b.tokenizer.eos_token_id,
        )
    texts = [t.strip() for t in b.tokenizer.batch_decode(out, skip_special_tokens=True)]
    stats = finish_stats(out, eos_ids(b))
    return [(t, n, f) for t, (n, f) in zip(texts, stats, strict=True)]


# --------------------------------------------------------------------------- canonical plan


@dataclass(frozen=True)
class Batch:
    layer: int
    batch_index: int
    seed: int
    units: tuple[tuple[int, int], ...]  # (site_index, sample_idx)


def draw_samples(draw: str) -> tuple[int, ...]:
    if draw == "extra":
        return tuple(range(1, C.N_SAMPLES))
    if draw in SINGLE_SAMPLE_DRAWS:
        return (0,)
    raise ValueError(f"unknown draw {draw!r}; known {sorted(C.SEED_BASE)}")


def batch_plan(
    n_sites: int,
    layers: Iterable[int],
    draw: str,
    *,
    site_indices: Sequence[int] | None = None,
) -> list[Batch]:
    """The canonical order of contracts.py: layers ascending; within a layer, sample-major
    units over ascending site indices; chunks of BATCH; batch_index counted across layers."""
    sites = sorted(site_indices) if site_indices is not None else list(range(n_sites))
    if len(set(sites)) != len(sites) or any(not 0 <= s < n_sites for s in sites):
        raise ValueError("site_indices must be distinct and within [0, n_sites)")
    samples, base = draw_samples(draw), C.SEED_BASE[draw]
    plan: list[Batch] = []
    for layer in sorted(set(layers)):
        units = [(si, k) for k in samples for si in sites]
        for i in range(0, len(units), C.BATCH):
            b = len(plan)
            plan.append(Batch(layer, b, base + b, tuple(units[i : i + C.BATCH])))
    return plan


# --------------------------------------------------------------------------- sample files


def draw_layers(family: str) -> tuple[int, ...]:
    """Read layers of a generation family (contracts.SITE_FAMILY_LAYERS)."""
    return C.SITE_FAMILY_LAYERS[family]


def subset_ids() -> set[str]:
    """Item ids of the committed 75-conversation hallucination subset (contracts.SUBSET_PATH)."""
    ids = json.loads(C.SUBSET_PATH.read_text("utf-8"))["ids"]
    if len(ids) != C.HALLU_SUBSET_N:
        raise ValueError(f"{C.SUBSET_PATH}: {len(ids)} ids, expected {C.HALLU_SUBSET_N}")
    return set(ids)


def _sample_row(d: dict[str, Any]) -> C.SampleRow:
    return C.SampleRow(**d)


def read_samples(path: Path) -> list[C.SampleRow]:
    if not path.exists():
        return []
    return [_sample_row(json.loads(x)) for x in path.read_text("utf-8").splitlines() if x.strip()]


def draw_plan(
    family: str,
    draw: str,
    sites: Sequence[C.SiteRow],
    *,
    layers: Sequence[int] | None = None,
    site_filter: set[str] | None = None,
) -> list[Batch]:
    """The canonical plan of one (draw, family) over ``sites``: the sites DRAW_SCOPE gives it
    ("all", or the "subset" of SUBSET_PATH ids; an unlisted pair raises) and ``draw_layers``.
    ``layers`` and ``site_filter`` (item ids) override them, for tests."""
    scope = C.DRAW_SCOPE.get((draw, family))
    if scope not in ("all", "subset"):
        raise ValueError(f"({draw!r}, {family!r}) is not in contracts.DRAW_SCOPE")
    n = len(sites)
    if site_filter is None and scope == "subset":
        site_filter = subset_ids()
        if n == C.N_SITES.get(family):  # the real index: the subset must have its frozen size
            n_sub = sum(s.id in site_filter for s in sites)
            if n_sub != C.HALLU_SUBSET_N_SITES:
                raise ValueError(f"subset covers {n_sub} sites, expected {C.HALLU_SUBSET_N_SITES}")
    keep = [s.index for s in sites if site_filter is None or s.id in site_filter]
    if site_filter is not None and not keep:
        raise ValueError("site_filter matched no sites")
    layers = list(layers) if layers is not None else list(draw_layers(family))
    return batch_plan(n, layers, draw, site_indices=keep)


def _load_done(
    path: Path, family: str, draw: str, plan: list[Batch], sites: Sequence[C.SiteRow]
) -> set[int]:
    """Batch indices of ``(family, draw)`` already complete in ``path``.

    Every draw in the (shared) file is validated, not only ``draw``: the others against their
    DRAW_SCOPE plans over the same ``sites``. Each row's id and pos must match its site and its
    layer and seed its batch; anything contradicting a plan raises. A torn last line (a crash
    mid-append) and the rows of every partially written batch, of whichever draw, are removed
    in one atomic rewrite; the owning draw regenerates them on its next run (a message names
    other draws affected)."""
    if not path.exists():
        return set()
    lines = path.read_text("utf-8").splitlines()
    keep: list[tuple[str, C.SampleRow]] = []
    torn = False
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            keep.append((line, _sample_row(json.loads(line))))
        except (json.JSONDecodeError, TypeError):
            if i == len(lines) - 1:
                torn = True
                continue
            raise
    groups: dict[str, dict[int, list[C.SampleRow]]] = {}
    for _, r in keep:
        if r.family != family:
            raise ValueError(f"{path}: a {r.family!r} row in the {family!r} file")
        groups.setdefault(r.draw, {}).setdefault(r.batch_index, []).append(r)
    plans = {draw: plan}
    done: dict[str, set[int]] = {}
    partial: dict[str, set[int]] = {}
    for d, batches in groups.items():
        if d not in plans:
            plans[d] = draw_plan(family, d, sites)
        by_index = {b.batch_index: b for b in plans[d]}
        for bi, rows in batches.items():
            b = by_index.get(bi)
            got = [(r.site_index, r.sample_idx) for r in rows]
            if b is None or not set(got) <= set(b.units) or len(got) != len(set(got)):
                raise ValueError(f"{path}: batch {bi} of {family}/{d} does not match the plan")
            for r in rows:
                s = sites[r.site_index]
                if r.layer != b.layer or r.seed != b.seed or (r.id, r.pos) != (s.id, s.pos):
                    raise ValueError(
                        f"{path}: batch {bi} of {family}/{d}: row {r.id}@{r.pos} (layer "
                        f"{r.layer}, seed {r.seed}) does not match site {r.site_index} or batch"
                    )
            (done if set(got) == set(b.units) else partial).setdefault(d, set()).add(bi)
    if torn or partial:
        others = sorted(d for d in partial if d != draw)
        if others:
            print(f"[olens] {path}: removed partial batches of draw(s) {others}; rerun them")
        _atomic_write(path, [x for x, r in keep if r.batch_index not in partial.get(r.draw, ())])
    return done.get(draw, set())


def _atomic_write(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    os.replace(tmp, path)


def _append_batch(path: Path, rows: Sequence[C.SampleRow]) -> None:
    """Append one batch's rows with a single ``write`` on an O_APPEND descriptor under an
    exclusive flock, then fsync: writers never interleave, and a crash leaves at most one torn
    batch at the end of the file (removed by ``_load_done``). The rows are serialized to one
    buffer first, which serves the purpose of the temp file the review suggested."""
    import fcntl

    data = "".join(C.to_json(r) + "\n" for r in rows).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(data)
        while view:  # one write in practice; loop in case the kernel writes short
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def draw_complete(path: Path, family: str, draw: str, plan: list[Batch]) -> bool:
    """Every unit of ``plan`` present exactly once for (family, draw) in ``path``."""
    rows = [r for r in read_samples(path) if r.family == family and r.draw == draw]
    want = sorted((b.batch_index, u) for b in plan for u in b.units)
    return sorted((r.batch_index, (r.site_index, r.sample_idx)) for r in rows) == want


def generate_draw(
    olens: PublicOLens,
    family: str,
    draw: str,
    *,
    acts: tuple[Sequence[C.SiteRow], torch.Tensor],
    acts_layers: Sequence[int] | None = None,
    layers: Sequence[int] | None = None,
    site_filter: set[str] | None = None,
    sampling: Sampling | None = None,
    source_of: Sequence[int] | None = None,
    out: Path | None = None,
) -> Path:
    """Generate one draw over one family and append its SampleRows to
    ``contracts.draw_samples_path(family, draw)``: resumable by ``(draw, batch_index)``, never
    duplicating a row, each batch appended atomically, and the draw verified complete before
    returning.

    ``acts = (sites, H)``: the family's SiteRows (row i describes H[i]) and activations
    ``H[n, len(acts_layers), d]`` (default ``acts_layers`` = all model layers, the store layout
    of contracts.acts_path). The plan is ``draw_plan`` (DRAW_SCOPE sites, ``draw_layers``);
    ``layers`` and ``site_filter`` override it, for tests. ``source_of[i]`` is the site whose
    activation site i reads (the shuffled check); default: its own."""
    sites, H = acts
    n = len(sites)
    if H.ndim != 3 or H.shape[0] != n:
        raise ValueError(f"acts: {n} sites but H has shape {tuple(H.shape)}")
    if any(s.index != i for i, s in enumerate(sites)):
        raise ValueError("acts: sites[i].index must equal i")
    acts_layers = list(acts_layers) if acts_layers is not None else list(range(H.shape[1]))
    if len(acts_layers) != H.shape[1]:
        raise ValueError("acts_layers does not match H.shape[1]")
    plan = draw_plan(family, draw, sites, layers=layers, site_filter=site_filter)
    src = list(source_of) if source_of is not None else list(range(n))
    path = out or C.draw_samples_path(family, draw)
    path.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(path, family, draw, plan, sites)
    for b in plan:
        if b.batch_index in done:
            continue
        # one Batch = one layer = one carrier (no padding); the batching check relies on it
        li = acts_layers.index(b.layer)
        rows_h = H[[src[si] for si, _ in b.units], li, :]
        assert rows_h.ndim == 2 and rows_h.shape[0] == len(b.units) <= C.BATCH
        res = read_batch(olens, rows_h, b.layer, b.seed, sampling=sampling)
        rows = [
            C.SampleRow(
                family=family,
                id=sites[si].id,
                layer=b.layer,
                pos=sites[si].pos,
                site_index=si,
                sample_idx=k,
                draw=draw,
                seed=b.seed,
                batch_index=b.batch_index,
                text=text,
                n_new_tokens=nt,
                finish=fin,  # type: ignore[arg-type]
            )
            for (si, k), (text, nt, fin) in zip(b.units, res, strict=True)
        ]
        _append_batch(path, rows)
    if not draw_complete(path, family, draw, plan):
        raise RuntimeError(f"{path}: {family}/{draw} is not complete after generation")
    return path


# --------------------------------------------------------------------------- arms


def read_sites(family: str) -> list[C.SiteRow]:
    """The family's SiteRows from contracts.acts_index_path (written by the capture unit)."""
    p = C.acts_index_path(family)
    return [C.SiteRow(**json.loads(x)) for x in p.read_text("utf-8").splitlines() if x.strip()]


def write_arm(
    family: str,
    arm: str,
    rows: Iterable[C.SampleRow],
    *,
    sites: Sequence[C.SiteRow] | None = None,
) -> Path:
    """Write ``readouts/<arm>/<family>.jsonl`` in the wsbench contract, one row per SampleRow:
    ``{"id","layer","pos","token","samples":[text]}`` (wsbench ``producer.Row.contract``).

    ``token`` comes from the SiteRow at ``row.site_index`` (``sites`` defaults to the rows'
    own family index). Rows are ordered like the wsbench producer: by item (first site index),
    then layer, then position. The file is overwritten, so a rerun is idempotent."""
    rows = list(rows)
    if arm not in C.ARM_FAMILIES or family not in C.ARM_FAMILIES[arm]:
        raise ValueError(f"arm {arm!r} is not defined for family {family!r}")
    if not rows:
        raise ValueError(f"no rows for {arm}/{family}")
    if sites is None:
        fams = {r.family for r in rows}
        if len(fams) != 1:
            raise ValueError(f"rows from several families {sorted(fams)}; pass sites=")
        sites = read_sites(fams.pop())
    keys = [(r.id, r.layer, r.pos) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{arm}/{family}: duplicate (id, layer, pos) rows")
    first: dict[str, int] = {}
    for s in sites:
        first.setdefault(s.id, s.index)
    out_rows = []
    for r in sorted(rows, key=lambda r: (first[r.id], r.layer, r.site_index)):
        s = sites[r.site_index]
        if (s.id, s.pos) != (r.id, r.pos):
            raise ValueError(f"row {r.id}@{r.pos} does not match site {r.site_index}")
        out_rows.append(
            {"id": r.id, "layer": r.layer, "pos": r.pos, "token": s.token, "samples": [r.text]}
        )
    path = C.readouts_path(arm, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, [json.dumps(x, ensure_ascii=False) for x in out_rows])
    return path


def derangement(sites: Sequence[C.SiteRow], seed: int = C.SHUFFLE_PERM_SEED) -> list[int]:
    """``perm[i]`` = the site whose activation target site i reads: a fixed permutation (reused
    at every layer) under which no site reads a site of its own item. Rejection sampling with
    ``random.Random(seed).shuffle``, so the permutation is a pure function of the seed and the
    site list."""
    n = len(sites)
    if len({s.id for s in sites}) < 2:
        raise ValueError("a derangement needs at least two items")
    rng = random.Random(seed)
    for _ in range(10_000):
        perm = list(range(n))
        rng.shuffle(perm)
        if all(sites[perm[i]].id != sites[i].id for i in range(n)):
            return perm
    raise RuntimeError("no derangement found")  # pragma: no cover


def generate_shuffled(
    olens: PublicOLens,
    *,
    acts: tuple[Sequence[C.SiteRow], torch.Tensor],
    family: str = "basic_readout_mt",
    acts_layers: Sequence[int] | None = None,
    layers: Sequence[int] | None = None,
    sampling: Sampling | None = None,
) -> Path:
    """The shuffled-activation manipulation check (prereg §4.7): at every layer, target site i
    reads site ``perm[i]``'s activation (``derangement``, seed SHUFFLE_PERM_SEED), and its row
    keeps the TARGET's id, pos and token. Samples go to ``samples/<family>.shuffled.jsonl``, the
    permutation to ``samples/<family>.shuffled.perm.json``; returns the readouts file
    ``readouts/olens_shuffled/<family>.jsonl``."""
    sites = acts[0]
    perm = derangement(sites)
    perm_path = C.SAMPLES_DIR / f"{family}.shuffled.perm.json"
    perm_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "seed": C.SHUFFLE_PERM_SEED,
        "rule": "target site i reads the activation of site perm[i]; same perm at every layer",
        "perm": perm,
        "ids": [s.id for s in sites],
    }
    perm_path.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    p = generate_draw(
        olens,
        family,
        "shuffled",
        acts=acts,
        acts_layers=acts_layers,
        layers=layers,
        sampling=sampling,
        source_of=perm,
    )
    rows = [r for r in read_samples(p) if r.family == family and r.draw == "shuffled"]
    return write_arm(family, "olens_shuffled", rows, sites=sites)
