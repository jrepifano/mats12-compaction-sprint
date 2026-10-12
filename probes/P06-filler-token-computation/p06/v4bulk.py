"""P06 follow-up F8 (exploratory, GPU): DeepSeek-V4-Flash bulk run (log.md "follow-up F8").

    python -m p06.v4bulk download   # = the F7 download (model @ MODEL_REV + both V4 lenses)
    python -m p06.v4bulk run        # load, smoke gate, then A+B per chain item, then C

One B300, the F7 stack and loader (`p06.v4pilot.load`: transformers 5.17, the checkpoint's own FP8
linears and FP4 experts, Triton kernels via `kernels`, eager attention). Results under
results/v4bulk/, written incrementally; a rerun skips every unit already in its jsonl.

**One pass per prompt.** F7 measured the generate's prefill logits equal to the uncached
forward's at the last position on 45 of 45 prompts with a max difference of 0.0, so the residual
streams are captured by forward hooks during the generate's prefill (`v4pilot.capture` fires only
on the full-sequence call), not in a second forward. The smoke still runs the separate forward
for the lens-vs-model agreement.

**Readers** at every (layer 0..42, read position), stored as log-softmax over a candidate id set
(fp16 [layer, row, n_cand]) plus the top-1 id over the full vocabulary (int32 [layer, row]):
- `logit_lens`: the model's own head path on the layer's 4 streams,
  `lm_head(norm(hc_head(streams)))` (as F7, gate 1 there: 0.99994 agreement).
- `jlens`, `jpp`: neuronpedia/jacobian-lens @ LENS_REV, applied as its README says for V4 Flash:
  h = the mean of the 4 streams of the layer's block output (provenance: capture_point
  block_output, stream_reduce mean), `logits = lm_head(norm(h @ J[l].T))` ("unembed is the model's
  final norm followed by its LM head"; no hc_head). Layers without a map (42 for both; the J-Lens
  also has J[41] = I exactly, i.e. its fit targets block 41's output, `target_layer: 41`; J++
  targets 42) read h through the identity. Maps fp16 on the GPU, applied in fp32.

**A. Chain readouts.** 200 items (all 100 at k = 2 and 100 at k = 3 of data/items_chain.jsonl), F6's
Brauer-format prompts (`f5_chain_brauer.messages`, the first 5 of data/shots_chain10.jsonl),
arms dots500, pre500, base; candidates = the integers 0..20 (single tokens). Read span: the
target turn's label ("Filler:", "Padding:", "Problem:") to the end; `groups` codes each row
label / filler (the 500 dots) / problem / suffix ("\\n\\nAnswer:" onward; the last dot token also
carries the "\\n\\n" and counts as filler).

**B. Filler patching** on dots500, every chain item, right after its A prompts:
- matched donor: the same ops with another start value (seeded per item), every state of the walk
  (start, intermediates, answer) different from the target's, same token length and the same
  dot positions (asserted);
- random donor: another item of the same k (seeded), the same dots500 token length if one
  exists, else the nearest (recorded);
- self-patch (the first N_SELF items processed): the target's own streams; must not change the
  output.
Patch: during the target's prefill, a forward hook on every decoder layer replaces that layer's
output (all 4 hc streams, [B, S, 4, D]) at the target turn's 500 dot positions with the donor's
(dot for dot), then greedy decoding from the patched cache. Why this is valid under V4's
compressed attention: layer l+1's attention reads `hidden_states` = input_layernorm(attn_hc
collapse of layer l's output); its sliding-window K/V (`self_attn.kv_proj`), its CSA/HCA
compressor (`compressor.kv_proj`/`gate_proj`, pooled every 4 or 128 positions) and the lightning
indexer (`compressor.indexer.*`) are all computed from that input inside layer l+1. So the
replaced streams are upstream of every K/V, compressed entry and indexer key that later
positions read, at every layer; positions beyond the 128-token window see the donor's content
through compressed entries built from the patched states. Compressed windows that straddle the
filler boundary mix patched and unpatched positions (as the model itself would). Embeddings
(layer 0's input) need no patch: the dot tokens are identical.

**C. MC / varbind readout.** Brauer's depth-1 set (data/P06/chained_var_binding_dataset.json,
pushed), the first N_MC items by idx whose candidates are all single tokens (V4 has single tokens
for 0..999; about a third of items have a value or option above 999 and are skipped, recorded),
arms: free response dots100 and pre100 (`brauer.messages`), 10-option MC base and dots100
(`with_options(ex, 10)`, shots with 10 options). Candidates per item: its chain values (x, c1x, y,
c2y, answer), its 10 option values (computed for every arm, so the free-response arms carry the
same distractors as a control), and N_CONTROLS unlisted controls (seeded, the answer's digit
count, not an option or a chain value).

**Smoke** (the first chain item, kept as data): lens-vs-model top-1 agreement >= AGREE_MIN over the
read positions of its 3 prompts (separate uncached forward), and the self-patch output equals the
unpatched one. Fails -> rc 3. **Stop** at OVERRUN x the projection (re-estimated after
PROJ_UPDATE_AFTER items from per-item medians, so the first-call compile does not count), or at
T0 + gpu_hours - SALVAGE_MARGIN_S -> rc 4. Exceptions -> rc 1, traceback in error.txt.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import brauer as B
from . import encoding_dsv4 as E
from . import f5_chain_brauer as F5
from . import items as I
from . import v4pilot as P
from .stage1 import DATA
from .stage2 import Timer, append_jsonl, chain_values, grade_int, utc, write_json

OUT = P.RESULTS / "v4bulk"
RO_CHAIN = OUT / "readouts" / "chain"
RO_MC = OUT / "readouts" / "mc"
READERS = ("logit_lens", "jlens", "jpp")
CHAIN_KS = (2, 3)
CHAIN_ARMS = ("dots500", "pre500", "base")
LABELS = {"dots500": "Filler:", "pre500": "Padding:", "base": "Problem:",
          "dots100": "Filler:", "pre100": "Padding:", "mc10_base": "Question:",
          "mc10_dots100": "Filler:"}
FILLER = {"dots500": ("after", 500), "pre500": ("before", 500), "base": (None, 0),
          "dots100": ("after", 100), "pre100": ("before", 100), "mc10_base": (None, 0),
          "mc10_dots100": ("after", 100)}
MC_ARMS = {"dots100": ("dots100", None), "pre100": ("pre100", None),
           "mc10_base": ("base", 10), "mc10_dots100": ("dots100", 10)}
GROUPS = {"label": 0, "filler": 1, "problem": 2, "suffix": 3}
N_SELF = 20
N_MC = 100
N_CONTROLS = 10
MC_ROLES = ("x", "c1x", "y", "c2y", "answer")
MAX_SINGLE = 999
SEED = 0
AGREE_MIN = 0.99
OVERRUN = 1.5
PROJ_UPDATE_AFTER = 6
# F7 medians (results/v4pilot/prompts.jsonl, uplift.jsonl): generate (prefill + 6 tokens) per arm,
# s; readout of 3 readers ~0.3 s per long prompt; MC prompts ~1/3 of the chain prompt length.
PROJ_GEN_S = {"dots500": 2.40, "pre500": 2.40, "base": 0.72}
PROJ_READ_S = 0.3
PROJ_MC_PROMPT_S = 1.2
REC_KEYS = ("item_id", "kind", "arm", "answer", "positions", "slots", "groups", "token_ids",
            "n_prompt", "gen_ids", "gen_text", "cand_ids", "cand_values", "readers")
CHAIN_KEYS = ("k", "traj")
MC_KEYS = ("idx", "role_names", "role_values", "options", "controls")


def log(msg: str) -> None:
    print(f"[p06.v4bulk {utc()}] {msg}", flush=True)


# --------------------------------------------------------------------- chain items and donors


_START = re.compile(r"Start with the number (\d+) ")
_LINE = re.compile(r"^(?:Halve it, rounding up\.|If it is even, halve it; if it is odd, add (\d+)\."
                   r"|If it is bigger than 10, subtract (\d+); otherwise double it\.)$")


def walk(problem: str) -> list[int]:
    """[start, state after step 1, ..., final] re-walked from the rendered text (nocot's
    `chain.solve` arithmetic, recording every state)."""
    mod = int(re.search(r"subtract (\d+); if it is smaller", problem).group(1))
    v = int(_START.search(problem).group(1))
    out = [v]
    for line in problem.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        if line.strip().startswith("Halve"):
            v = v // 2 + (v % 2)
        elif m.group(1):
            v = v // 2 if v % 2 == 0 else v + int(m.group(1))
        else:
            v = v - int(m.group(2)) if v > 10 else v * 2
        v = ((v - 1) % mod) + 1
        out.append(v)
    return out


def chain_items(path: Path = DATA / "items_chain.jsonl") -> list[dict]:
    """Every chain item at k in CHAIN_KS, interleaved k = 2, 3, 2, 3 ... (each k sorted by
    item_id) so a run cut short keeps both depths; the walk must equal the stored trajectory."""
    rows = I.read_jsonl(path)
    by_k = {k: sorted((e for e in rows if e["k"] == k), key=lambda e: e["item_id"])
            for k in CHAIN_KS}
    out = []
    for i in range(max(len(v) for v in by_k.values())):
        for k in CHAIN_KS:
            if i < len(by_k[k]):
                it = by_k[k][i]
                if walk(it["problem"]) != it["traj"] or it["traj"][-1] != it["answer"]:
                    raise ValueError(f"{it['item_id']}: walk != stored trajectory")
                out.append(it)
    return out


def with_start(it: dict, start: int) -> dict:
    """The item with another start value (same ops); trajectory and answer re-walked."""
    problem, n = _START.subn(f"Start with the number {start} ", it["problem"], count=1)
    if n != 1:
        raise ValueError(f"{it['item_id']}: no start sentence")
    traj = walk(problem)
    return {**it, "item_id": f"{it['item_id']}#start{start}", "problem": problem, "traj": traj,
            "answer": traj[-1], "donor_of": it["item_id"]}


def donor_starts(it: dict) -> list[int]:
    """Candidate starts (1..20) whose whole walk differs from the target's at every step, in a
    seeded order per item."""
    mod = 20
    cands = [s for s in range(1, mod + 1)
             if all(a != b for a, b in zip(walk(with_start(it, s)["problem"]), it["traj"],
                                           strict=True))]
    random.Random(SEED * 100_003 + zlib.crc32(it["item_id"].encode())).shuffle(cands)
    return cands


def random_donor(it: dict, pool: list[dict], lengths: dict[str, int]) -> tuple[dict, int]:
    """Another item of the same k (seeded): same dots500 token length if possible, else the
    nearest. Returns (donor, length difference)."""
    same_k = [e for e in pool if e["k"] == it["k"] and e["item_id"] != it["item_id"]]
    if not same_k:  # only in tiny test runs (--limit): any other item
        same_k = [e for e in pool if e["item_id"] != it["item_id"]]
    n = lengths[it["item_id"]]
    best = min(abs(lengths[e["item_id"]] - n) for e in same_k)
    cands = [e for e in same_k if abs(lengths[e["item_id"]] - n) == best]
    d = random.Random(SEED * 7_919 + zlib.crc32(it["item_id"].encode())).choice(cands)
    return d, lengths[d["item_id"]] - n


# --------------------------------------------------------------------- varbind (C) items


def mc_controls(ex: dict, exclude: set[int], n: int = N_CONTROLS) -> list[int]:
    """n unlisted control values (seeded per idx) with the answer's digit count, not excluded.
    A 1-digit answer has too few such values, so its controls are 0..99 (recorded by the digit
    counts in the npz)."""
    d = len(str(ex["answer"]))
    lo, hi = (0, 99) if d == 1 else (10 ** (d - 1), 10 ** d - 1)
    pool = [v for v in range(lo, hi + 1) if v not in exclude]
    return sorted(random.Random(SEED * 1_000_003 + ex["idx"]).sample(pool, n))


def mc_item(ex: dict) -> dict:
    """Roles, options and controls of one varbind example (no tokenizer)."""
    cv = chain_values(ex)
    opts = B.with_options(ex, 10)["options"]
    roles = [cv[r] for r in MC_ROLES]
    ctrl = mc_controls(ex, set(opts) | set(roles))
    return {"item_id": f"varbind:{ex['idx']}", "idx": ex["idx"], "answer": ex["answer"],
            "role_values": roles, "options": opts, "controls": ctrl,
            "cand_values": sorted(set(roles) | set(opts) | set(ctrl))}


def mc_items(tok: Any, n: int = N_MC) -> tuple[list[tuple[dict, dict]], list[int]]:
    """[(example, mc item)] for the first n examples by idx whose candidates are all single
    tokens <= MAX_SINGLE, and the skipped idx before the n-th."""
    exs, _ = B.load(1)
    out, skipped = [], []
    for ex in sorted(exs, key=lambda e: e["idx"]):
        if len(out) == n:
            break
        it = mc_item(ex)
        ok = min(it["cand_values"]) >= 0 and max(it["cand_values"]) <= MAX_SINGLE
        if ok:
            try:
                it["cand_ids"] = value_ids(tok, it["cand_values"])
            except ValueError:
                ok = False
        if ok:
            out.append((ex, it))
        else:
            skipped.append(ex["idx"])
    return out, skipped


def value_ids(tok: Any, values: list[int]) -> list[int]:
    """Single-token ids of the integers (str(v) one token that decodes back), all distinct."""
    ids = []
    for v in values:
        enc = tok.encode(str(v), add_special_tokens=False)
        if len(enc) != 1 or tok.decode(enc) != str(v):
            raise ValueError(f"{v} is not one token: {enc}")
        ids.append(int(enc[0]))
    if len(set(ids)) != len(ids):
        raise ValueError(f"ids not distinct: {ids}")
    return ids


# --------------------------------------------------------------------- prompts and spans


def chain_messages(it: dict, shots: list[dict], arm: str) -> list[dict]:
    return F5.messages(it, shots, arm)


def mc_messages(ex: dict, shots: list[dict], arm: str) -> list[dict]:
    b_arm, n_opt = MC_ARMS[arm]
    if n_opt:
        return B.messages(B.with_options(ex, n_opt), [B.with_options(s, n_opt) for s in shots],
                          b_arm)
    return B.messages(ex, shots, b_arm)


@dataclass
class Spans:
    ids: list[int]
    read: np.ndarray  # positions from the label to the end
    groups: np.ndarray  # GROUPS code per read position
    filler: np.ndarray  # absolute positions of the target turn's filler dots (empty if none)


def render(tok: Any, msgs: list[dict], arm: str) -> Spans:
    """Prompt ids (offset tokenization == encode), read span and group codes. A token belongs to
    the group of its first character; the token holding the label's first character starts the
    read span and counts as label."""
    text = E.encode_messages(msgs, thinking_mode="chat")
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = [int(i) for i in enc["input_ids"]]
    if ids != [int(i) for i in tok.encode(text, add_special_tokens=False)]:
        raise ValueError(f"{arm}: offset tokenization differs from encode")
    offsets = [(int(a), int(b)) for a, b in enc["offset_mapping"]]
    turn = text.rfind(E.USER_SP_TOKEN)
    label = LABELS[arm]
    lab = text.rfind(label)
    suf = text.rfind("\n\nAnswer:")
    if turn < 0 or lab < turn or suf < lab:
        raise ValueError(f"{arm}: label {label!r} or the suffix not in the target turn")
    _, k = FILLER[arm]
    d0 = d1 = -1
    if k:
        d0 = lab + len(label)  # the space before the first dot
        d1 = d0 + 1 + len(B.dots(k))
        if text[d0:d1] != " " + B.dots(k):
            raise ValueError(f"{arm}: no {k} dots after {label!r}")
    i0 = next(i for i, (s, e) in enumerate(offsets) if e > lab and e > s)
    read = np.arange(i0, len(ids))
    groups = np.empty(len(read), dtype=np.int8)
    filler = []
    for j, p in enumerate(read):
        s = offsets[p][0]
        if s < lab + len(label):
            g = GROUPS["label"]
        elif d0 <= s < d1:
            g = GROUPS["filler"]
            filler.append(int(p))
        elif s >= suf:
            g = GROUPS["suffix"]
        else:
            g = GROUPS["problem"]
        groups[j] = g
    if k and len(filler) != k:
        raise ValueError(f"{arm}: {len(filler)} filler tokens, expected {k}")
    if filler and filler != list(range(filler[0], filler[0] + len(filler))):
        raise ValueError(f"{arm}: filler tokens not contiguous")
    return Spans(ids, read, groups, np.array(filler, dtype=np.int64))


# --------------------------------------------------------------------- readers


class JReader:
    """A jacobian-lens map bank on the device: readout = lm_head(norm(mean_streams @ J[l].T));
    layers without a map read the stream mean through the identity."""

    def __init__(self, maps: dict[int, Any], device: str) -> None:
        # fp32 once (42 x 64 MB per lens on the GPU), not a cast per call
        self.maps = {int(k): v.to(device).float() for k, v in maps.items()}

    def logits(self, parts: P.Parts, streams: Any, layer: int) -> Any:
        h = streams.float().mean(dim=1)
        if layer in self.maps:
            h = h @ self.maps[layer].T
        return parts.lm_head(parts.norm(h.to(streams.dtype))).float()


def identity_check(maps: dict[int, Any]) -> dict:
    """||J[l] - I|| / ||I|| for the last mapped layer (J-Lens: 0 at 41, its target)."""
    import torch

    last = max(maps)
    m = maps[last].float()
    eye = torch.eye(m.shape[0], device=m.device)
    return {"last_mapped_layer": int(last), "rel_dist_to_identity":
            round(float((m - eye).norm() / eye.norm()), 6), "n_maps": len(maps)}


def reader_arrays(logits_fn, hs: Any, cand_ids: list[int], chunk: int = P.LENS_CHUNK):
    """hs [L, P, hc, D] -> (log-probs over cand_ids [L, P, n] fp16, top-1 [L, P] int32);
    logits_fn(layer, streams [c, hc, D]) -> [c, V]."""
    import torch

    idx = torch.tensor(cand_ids, device=hs.device)
    lps, tops = [], []
    with torch.inference_mode():
        for layer in range(hs.shape[0]):
            lp_l, top_l = [], []
            for s in range(0, hs.shape[1], chunk):
                lg = logits_fn(layer, hs[layer, s:s + chunk]).float()
                lp_l.append(torch.log_softmax(lg, -1).index_select(-1, idx))
                top_l.append(lg.argmax(-1))
            lps.append(torch.cat(lp_l))
            tops.append(torch.cat(top_l))
    return (torch.stack(lps).cpu().numpy().astype(np.float16),
            torch.stack(tops).cpu().numpy().astype(np.int32))


def readouts(ctx: P.Ctx, jreaders: dict[str, JReader], hs: Any, cand_ids: list[int]) -> dict:
    out = {"logit_lens": reader_arrays(
        lambda layer, x: P.lens_logits(ctx.parts, x), hs, cand_ids)}
    for name, jr in jreaders.items():
        out[name] = reader_arrays(lambda layer, x, jr=jr: jr.logits(ctx.parts, x, layer), hs,
                                  cand_ids)
    return out


# --------------------------------------------------------------------- generate, capture, patch


class inject:  # a context manager, named like v4pilot.capture
    """Forward hooks replacing each decoder layer's output (all hc streams) at `positions` with
    `stored[i]` on the full-sequence call (the prefill); decode steps are untouched."""

    def __init__(self, blocks: Any, positions: Any, stored: list[Any], seq_len: int) -> None:
        self.blocks, self.positions, self.stored, self.seq_len = blocks, positions, stored, seq_len
        self.fired = [0] * len(blocks)
        self.handles: list[Any] = []

    def _hook(self, i: int):
        def fn(_mod, _inp, o):
            if not (o.dim() == 4 and o.shape[1] == self.seq_len):
                return None
            o = o.clone()
            o[0, self.positions] = self.stored[i].to(o.dtype)
            self.fired[i] += 1
            return o
        return fn

    def __enter__(self) -> list[int]:
        self.handles = [b.register_forward_hook(self._hook(i)) for i, b in enumerate(self.blocks)]
        return self.fired

    def __exit__(self, *exc) -> None:
        for h in self.handles:
            h.remove()


def gen_capture(ctx: P.Ctx, ids: list[int], positions: np.ndarray | None):
    """Greedy decode; with `positions`, the layers' streams there are captured during the
    prefill. Returns (new ids, first-step logits fp32, streams [L, P, hc, D] or None)."""
    import torch

    if positions is None or not len(positions):
        new, _, first = P.greedy(ctx.model, ids, ctx.int_ids, ctx.device)
        return new, first, None
    with P.capture(ctx.parts.blocks, torch.as_tensor(positions, device=ctx.device),
                   len(ids)) as hs:
        new, _, first = P.greedy(ctx.model, ids, ctx.int_ids, ctx.device)
    if any(h is None for h in hs):
        raise RuntimeError("a decoder-layer hook captured nothing")
    return new, first, torch.stack(hs)


def gen_patched(ctx: P.Ctx, ids: list[int], positions: np.ndarray, stored: Any) -> list[int]:
    """Greedy decode with every layer's output at `positions` replaced by stored [L, n, hc, D]
    during the prefill."""
    import torch

    if stored.shape[1] != len(positions):
        raise ValueError(f"{stored.shape[1]} stored rows for {len(positions)} positions")
    with inject(ctx.parts.blocks, torch.as_tensor(positions, device=ctx.device),
                list(stored), len(ids)) as fired:
        new, _, _ = P.greedy(ctx.model, ids, ctx.int_ids, ctx.device)
    if fired != [1] * len(ctx.parts.blocks):
        raise RuntimeError(f"patch fired {sorted(set(fired))} times per layer, not once")
    return new


# --------------------------------------------------------------------- npz records


def rec_path(root: Path, item_id: str, arm: str) -> Path:
    return root / f"{item_id.replace(':', '_').replace('#', '_')}__{arm}.npz"


def write_rec(path: Path, rec: dict, extra: tuple[str, ...]) -> None:
    """Atomic compressed npz: REC_KEYS + `extra` + per reader `<r>__lp`, `<r>__top1`."""
    arrays = {k: (np.array(rec[k]) if isinstance(rec[k], (str, int, list)) else rec[k])
              for k in (*REC_KEYS, *extra)}
    for name, (lp, top) in rec["ro"].items():
        arrays[f"{name}__lp"], arrays[f"{name}__top1"] = lp, top
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        np.savez_compressed(f, **arrays)
    os.replace(tmp, path)


def read_rec(path: Path) -> dict:
    """The record back with the schema and shapes checked; `ro` = {reader: (lp, top1)}."""
    with np.load(path, allow_pickle=False) as z:
        d = {k: z[k] for k in z.files}
    extra = CHAIN_KEYS if str(d.get("kind")) == "chain" else MC_KEYS
    missing = [k for k in (*REC_KEYS, *extra) if k not in d]
    if missing:
        raise ValueError(f"{path}: missing keys {missing}")
    rec: dict[str, Any] = dict(d)
    for k in ("item_id", "kind", "arm", "gen_text"):
        rec[k] = str(rec[k])
    for k in ("answer", "n_prompt", *(("k",) if rec["kind"] == "chain" else ("idx",))):
        rec[k] = int(rec[k])
    rec["readers"] = [str(x) for x in rec["readers"]]
    n_rows, n_cand = len(rec["positions"]), len(rec["cand_ids"])
    if len(rec["cand_values"]) != n_cand:
        raise ValueError(f"{path}: cand_values / cand_ids differ")
    for k in ("slots", "groups", "token_ids"):
        if len(rec[k]) != n_rows:
            raise ValueError(f"{path}: {k} length != {n_rows} positions")
    rec["ro"] = {}
    for name in rec["readers"]:
        lp, top = d[f"{name}__lp"], d[f"{name}__top1"]
        if lp.shape[1:] != (n_rows, n_cand) or top.shape != lp.shape[:2]:
            raise ValueError(f"{path}: {name} shapes {lp.shape} {top.shape}")
        rec["ro"][name] = (lp, top)
    return rec


def jsonl_keys(path: Path, *fields: str) -> set:
    if not path.exists():
        return set()
    return {tuple(json.loads(x)[f] for f in fields) for x in path.read_text().splitlines()
            if x.strip()}


# --------------------------------------------------------------------- units of work


@dataclass
class Bulk:
    ctx: P.Ctx
    jreaders: dict[str, JReader]
    shots: list[dict]
    mc_shots: list[dict]
    int_ids: list[int]


def read_prompt(bk: Bulk, sp: Spans, cand_ids: list[int], root: Path, rec_base: dict,
                extra: tuple[str, ...], keep_filler: bool = False) -> tuple[dict, Any]:
    """Generate with capture over the read span, all readers, npz; returns (answer-row fields,
    the filler streams [L, n_filler, hc, D] if keep_filler, else None)."""
    t0 = time.time()
    new, first, hs = gen_capture(bk.ctx, sp.ids, sp.read)
    P._sync()
    t1 = time.time()
    ro = readouts(bk.ctx, bk.jreaders, hs, cand_ids)
    t2 = time.time()
    text = bk.ctx.tok.decode(new, skip_special_tokens=True)
    lens_last = int(ro["logit_lens"][1][-1, -1])
    rec = {**rec_base, "positions": sp.read.astype(np.int32),
           "slots": (sp.read - len(sp.ids)).astype(np.int16), "groups": sp.groups,
           "token_ids": np.array([sp.ids[p] for p in sp.read], dtype=np.int32),
           "n_prompt": len(sp.ids), "gen_ids": np.array(new, dtype=np.int32), "gen_text": text,
           "cand_ids": np.array(cand_ids, dtype=np.int32), "readers": list(ro), "ro": ro}
    write_rec(rec_path(root, rec_base["item_id"], rec_base["arm"]), rec, extra)
    filler = None
    if keep_filler and len(sp.filler):
        sel = np.searchsorted(sp.read, sp.filler)
        import torch

        filler = hs.index_select(1, torch.as_tensor(sel, device=hs.device))
    del hs
    row = {"gen_ids": new, "gen_text": text, "n_prompt": len(sp.ids), "n_read": len(sp.read),
           "final_agree": bool(new and lens_last == new[0] and first is not None
                               and int(first.argmax()) == new[0]),
           "gen_s": round(t1 - t0, 3), "read_s": round(t2 - t1, 3),
           "write_s": round(time.time() - t2, 3), "utc": utc()}
    return row, filler


def do_chain_item(bk: Bulk, it: dict, pool: list[dict], lengths: dict[str, int],
                  done_a: set, done_b: set, self_patch: bool) -> dict:
    """A (3 arms) then B for one chain item; returns timing/check metrics."""
    m: dict[str, Any] = {"item_id": it["item_id"], "t0": time.time()}
    own, target_new = None, None
    base = {"item_id": it["item_id"], "kind": "chain", "k": it["k"], "answer": it["answer"],
            "traj": np.array(it["traj"], dtype=np.int64),
            "cand_values": np.array(P.INT_VALUES, dtype=np.int64)}
    for arm in CHAIN_ARMS:
        if (it["item_id"], arm) in done_a:
            continue
        sp = render(bk.ctx.tok, chain_messages(it, bk.shots, arm), arm)
        row, fill = read_prompt(bk, sp, bk.int_ids, RO_CHAIN, {**base, "arm": arm}, CHAIN_KEYS,
                                keep_filler=(arm == "dots500" and self_patch))
        if fill is not None:
            own = fill
        if arm == "dots500":
            target_new = row["gen_ids"]
        g = grade_int(row["gen_text"], it["answer"])
        append_jsonl(OUT / "chain_answers.jsonl", {
            "item_id": it["item_id"], "k": it["k"], "arm": arm, "answer": it["answer"],
            **g, **row})
    if it["item_id"] not in done_b:
        m["patch"] = do_patch(bk, it, pool, lengths, self_patch, target_new, own)
    m["seconds"] = time.time() - m.pop("t0")
    return m


def do_patch(bk: Bulk, it: dict, pool: list[dict], lengths: dict[str, int], self_patch: bool,
             target_new: list[int] | None = None, own: Any = None) -> dict:
    """B for one item: matched donor, random donor, optional self-patch; one patch.jsonl row.
    The unpatched dots500 output and (for the self-patch) the target's own filler streams come
    from A when it ran in this process; otherwise one generate recomputes them."""
    tok = bk.ctx.tok
    t0 = time.time()
    tsp = render(tok, chain_messages(it, bk.shots, "dots500"), "dots500")
    if target_new is None or (self_patch and own is None):
        target_new, _, own = gen_capture(bk.ctx, tsp.ids, tsp.filler if self_patch else None)
    target_text = tok.decode(target_new, skip_special_tokens=True)
    donor, dsp = None, None
    for s in donor_starts(it):
        d = with_start(it, s)
        sp = render(tok, chain_messages(d, bk.shots, "dots500"), "dots500")
        if len(sp.ids) == len(tsp.ids) and np.array_equal(sp.filler, tsp.filler):
            donor, dsp = d, sp
            break
    row: dict[str, Any] = {"item_id": it["item_id"], "k": it["k"], "answer": it["answer"],
                           "traj": it["traj"], "target_text": target_text,
                           "target_pred": grade_int(target_text, 0)["predicted"]}
    if donor is None:
        row["donor_error"] = "no start with a fully different walk and the same token length"
    else:
        assert len(dsp.ids) == len(tsp.ids) and np.array_equal(dsp.filler, tsp.filler)
        d_new, _, dstreams = gen_capture(bk.ctx, dsp.ids, dsp.filler)
        d_text = tok.decode(d_new, skip_special_tokens=True)
        tx = tok.decode(gen_patched(bk.ctx, tsp.ids, tsp.filler, dstreams),
                        skip_special_tokens=True)
        del dstreams
        row.update(donor_start=donor["traj"][0], donor_traj=donor["traj"],
                   donor_answer=donor["answer"], donor_text=d_text,
                   donor_pred=grade_int(d_text, 0)["predicted"], tx_text=tx,
                   tx_pred=grade_int(tx, 0)["predicted"])
    rd, ldiff = random_donor(it, pool, lengths)
    rsp = render(tok, chain_messages(rd, bk.shots, "dots500"), "dots500")
    r_new, _, rstreams = gen_capture(bk.ctx, rsp.ids, rsp.filler)
    r_text = tok.decode(r_new, skip_special_tokens=True)
    rtx = tok.decode(gen_patched(bk.ctx, tsp.ids, tsp.filler, rstreams), skip_special_tokens=True)
    del rstreams
    row.update(rand_item=rd["item_id"], rand_answer=rd["answer"], rand_len_diff=ldiff,
               rand_text=r_text, rand_pred=grade_int(r_text, 0)["predicted"], rtx_text=rtx,
               rtx_pred=grade_int(rtx, 0)["predicted"])
    if self_patch:
        s_new = gen_patched(bk.ctx, tsp.ids, tsp.filler, own)
        row.update(self_text=tok.decode(s_new, skip_special_tokens=True),
                   self_changed=s_new != target_new)
    row.update(seconds=round(time.time() - t0, 3), utc=utc())
    append_jsonl(OUT / "patch.jsonl", row)
    return {"self_changed": row.get("self_changed"), "seconds": row["seconds"]}


def do_mc_item(bk: Bulk, ex: dict, it: dict, done: set) -> None:
    base = {"item_id": it["item_id"], "kind": "mc", "idx": ex["idx"], "answer": ex["answer"],
            "role_names": list(MC_ROLES), "role_values": np.array(it["role_values"]),
            "options": np.array(it["options"]), "controls": np.array(it["controls"]),
            "cand_values": np.array(it["cand_values"], dtype=np.int64)}
    for arm in MC_ARMS:
        if (it["item_id"], arm) in done:
            continue
        shots = bk.mc_shots
        sp = render(bk.ctx.tok, mc_messages(ex, shots, arm), arm)
        row, _ = read_prompt(bk, sp, it["cand_ids"], RO_MC, {**base, "arm": arm}, MC_KEYS)
        g = grade_int(row["gen_text"], ex["answer"])
        append_jsonl(OUT / "mc_answers.jsonl", {
            "item_id": it["item_id"], "idx": ex["idx"], "arm": arm, "answer": ex["answer"],
            **g, **row})


# --------------------------------------------------------------------- smoke, projection, run


def smoke(bk: Bulk, it: dict) -> dict:
    """Lens-vs-model agreement over the 3 chain prompts of `it` (uncached forward), and the
    self-patch of its dots500 prompt leaving the output unchanged."""
    agree, n = 0, 0
    for arm in CHAIN_ARMS:
        sp = render(bk.ctx.tok, chain_messages(it, bk.shots, arm), arm)
        hs, logits = P.forward_read(bk.ctx.model, bk.ctx.parts, sp.ids, sp.read, bk.ctx.device)
        lens = reader_arrays(lambda layer, x: P.lens_logits(bk.ctx.parts, x), hs[-1:],
                             bk.int_ids)[1][0]
        a = P.agreement(lens, logits.argmax(-1).cpu().numpy())
        agree, n = agree + a["agree"], n + a["n"]
        del hs, logits
    sp = render(bk.ctx.tok, chain_messages(it, bk.shots, "dots500"), "dots500")
    new, _, own = gen_capture(bk.ctx, sp.ids, sp.filler)
    patched = gen_patched(bk.ctx, sp.ids, sp.filler, own)
    rate = agree / n if n else None
    checks = {"lens agreement": rate is not None and rate >= AGREE_MIN,
              "self-patch unchanged": patched == new}
    return {"read_agree": agree, "read_n": n, "rate": rate, "unpatched": new,
            "self_patched": patched, "checks": checks}


def projection(n_chain: int, n_self: int, n_mc: int, item_s: float | None = None) -> float:
    """Seconds for the whole bulk: per chain item A (3 generates + readouts) + B (4 generates),
    the self-patches, and C; `item_s` (a measured median) replaces the F7-based estimate."""
    a = sum(PROJ_GEN_S.values()) + 3 * PROJ_READ_S
    b = 4 * PROJ_GEN_S["dots500"]
    per = item_s if item_s is not None else a + b
    return n_chain * per + n_self * PROJ_GEN_S["dots500"] + n_mc * len(MC_ARMS) * PROJ_MC_PROMPT_S


def load_jreaders(device: str, info: dict) -> dict[str, JReader]:
    from huggingface_hub import hf_hub_download

    from .stage2 import load_lens

    out = {}
    for name, f in P.LENS_FILES.items():
        try:
            p = hf_hub_download(P.LENS_REPO, f, revision=P.LENS_REV, local_files_only=True)
            maps, meta = load_lens(p)
            if meta["d_model"] != P.D_MODEL:
                raise ValueError(f"{name}: d_model {meta['d_model']}")
            out[name] = JReader(maps, device)
            info[name] = {**{k: meta[k] for k in ("n_source_layers", "d_model", "dtype")},
                          **identity_check(out[name].maps)}
        except Exception as e:  # noqa: BLE001 - a missing lens drops that reader only
            info[name] = {"dropped": repr(e)[:300]}
            log(f"WARNING: {name} dropped: {e!r}")
    return out


def cmd_run(a) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    st: dict[str, Any] = {"env": P.env_info(), "utc_start": utc(), "step": "setup"}
    rc = 1
    try:
        rc = _run(a, st)
    except BaseException as e:  # noqa: BLE001 - recorded; run.json is still written
        P.record_error(st.get("step", "run"), e, OUT)
        st["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        rc = 1
    finally:
        st["utc_end"] = utc()
        st["rc"] = rc
        write_json(OUT / "run.json", st)
    log(f"P06_V4BULK_DONE rc={rc}")
    return rc


def _run(a, st: dict) -> int:
    import torch

    T = Timer(OUT / "timings.json")
    chain = chain_items()
    if a.limit:
        chain = chain[: a.limit]
    shots = P.load_shots()
    _, b_shots = B.load(1)
    st["step"] = "load"
    with T.step("load") as rec:
        ctx, info = P.load()
        rec.update(info)
    st["load"] = info
    lens_info: dict[str, Any] = {}
    jreaders = load_jreaders(ctx.device, lens_info)
    st["lenses"] = lens_info
    bk = Bulk(ctx, jreaders, shots, b_shots, ctx.int_ids)
    st["step"] = "smoke_fixes"
    first = render(ctx.tok, chain_messages(chain[0], shots, "dots500"), "dots500")
    with T.step("smoke_fixes") as rec:
        sm = P.smoke_with_fixes(ctx, first.ids[:P.SMOKE_TOKENS])
        rec.update(fixes=[f["name"] for f in sm["fixes_applied"]])
    st["smoke_fixes"] = {"fixes_applied": sm["fixes_applied"], "attempts": len(sm["attempts"])}
    lengths = {it["item_id"]: len(render(ctx.tok, chain_messages(it, shots, "dots500"),
                                         "dots500").ids) for it in chain}
    try:
        mc, skipped = mc_items(ctx.tok, a.limit or N_MC)
    except Exception as e:  # noqa: BLE001 - part C is skipped; A and B still run
        P.record_error("mc_items", e, OUT)
        mc, skipped, st["mc_error"] = [], [], f"{type(e).__name__}: {str(e)[:300]}"
    st["plan"] = {"n_chain": len(chain), "n_self": min(N_SELF, len(chain)), "n_mc": len(mc),
                  "mc_skipped_idx": skipped, "readers": ["logit_lens", *jreaders]}
    st["step"] = "smoke"
    with T.step("smoke") as rec:
        sk = smoke(bk, chain[0])
        rec.update(checks=sk["checks"], rate=sk["rate"])
    st["smoke"] = sk
    log(f"P06_GATE smoke lens_agreement={sk['rate']} self_patch_unchanged="
        f"{sk['checks']['self-patch unchanged']}")
    if not all(sk["checks"].values()):
        st["stopped"] = "smoke failed: " + ", ".join(k for k, v in sk["checks"].items() if not v)
        log(f"P06_SMOKE_FAIL {st['stopped']}")
        return 3
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    proj = projection(len(chain), st["plan"]["n_self"], len(mc))
    st["projection_s"] = {"initial": round(proj)}
    start = time.time()
    stop_at = start + OVERRUN * proj
    deadline = P.budget_deadline()
    done_a = jsonl_keys(OUT / "chain_answers.jsonl", "item_id", "arm")
    done_b = {x[0] for x in jsonl_keys(OUT / "patch.jsonl", "item_id")}
    done_c = jsonl_keys(OUT / "mc_answers.jsonl", "item_id", "arm")
    item_s: list[float] = []
    self_changed = []
    st["step"] = "chain"
    stopped = None

    def over() -> str | None:
        if time.time() > stop_at:
            return f"{OVERRUN}x the projection"
        if deadline is not None and time.time() > deadline:
            return "the gpu_hours deadline"
        return None

    for i, it in enumerate(chain):
        if (stopped := over()):
            break
        m = do_chain_item(bk, it, chain, lengths, done_a, done_b, self_patch=i < N_SELF)
        if m.get("patch", {}).get("self_changed") is not None:
            self_changed.append(bool(m["patch"]["self_changed"]))
        item_s.append(m["seconds"])
        if len(item_s) == PROJ_UPDATE_AFTER:
            med = statistics.median(item_s[1:])  # the first item carries the compile
            proj = time.time() - start + projection(len(chain) - i - 1,
                                                    max(0, N_SELF - i - 1), len(mc), med)
            stop_at = start + OVERRUN * proj
            st["projection_s"]["updated"] = round(proj)
            st["projection_s"]["item_median_s"] = round(med, 2)
            log(f"P06_PROJECTION {proj / 60:.1f} min (item median {med:.1f} s)")
        if (i + 1) % 10 == 0:
            log(f"chain {i + 1}/{len(chain)} items, last {m['seconds']:.1f} s")
    st["chain_items_done"] = len(item_s)
    st["self_patch_changed"] = sum(self_changed)
    st["self_patch_n"] = len(self_changed)
    st["step"] = "mc"
    n_mc = 0
    if stopped is None:
        for ex, it in mc:
            if (stopped := over()):
                break
            do_mc_item(bk, ex, it, done_c)
            n_mc += 1
    st["mc_items_done"] = n_mc
    if torch.cuda.is_available():
        st["mem_peak_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    st["timing_medians"] = timing_medians()
    st["accuracy_quick"] = accuracy_quick()
    st.pop("step", None)
    if stopped:
        st["stopped"] = f"stopped at {stopped}"
        log(f"P06_OVERRUN {st['stopped']}")
        return 4
    if st["self_patch_changed"]:
        log(f"P06_GATE self_patch changed {st['self_patch_changed']} of {st['self_patch_n']}")
    return 0


def accuracy_quick() -> dict:
    """Local accuracy per (arm, k) and per MC arm from the answer rows (the laptop analysis has
    the intervals and the API comparison)."""
    out: dict[str, Any] = {}
    for name, key in (("chain_answers", "k"), ("mc_answers", None)):
        rows = [json.loads(x) for x in (OUT / f"{name}.jsonl").read_text().splitlines()
                if x.strip()] if (OUT / f"{name}.jsonl").exists() else []
        groups: dict[str, list[bool]] = {}
        for r in rows:
            groups.setdefault(f"{name}|{r['arm']}" + (f"|k{r[key]}" if key else ""),
                              []).append(bool(r["correct"]))
        out.update({g: {"n": len(v), "acc": round(sum(v) / len(v), 3)} for g, v in groups.items()})
    return out


def timing_medians() -> dict:
    """Median generate / readout / write seconds per arm (the first prompt per arm dropped: it
    carries the kernel compile) and the median patch-row seconds."""
    out: dict[str, Any] = {}
    for name in ("chain_answers", "mc_answers"):
        p = OUT / f"{name}.jsonl"
        if not p.exists():
            continue
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        for arm in sorted({r["arm"] for r in rows}):
            ar = [r for r in rows if r["arm"] == arm][1:]
            if ar:
                out[f"{name}|{arm}"] = {k: round(statistics.median(r[k] for r in ar), 3)
                                        for k in ("gen_s", "read_s", "write_s")} | {"n": len(ar)}
    p = OUT / "patch.jsonl"
    if p.exists():
        rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()][1:]
        if rows:
            out["patch_row_s"] = round(statistics.median(r["seconds"] for r in rows), 3)
    return out


def cmd_download(_a) -> int:
    try:
        return P._download(OUT)
    except BaseException as e:  # noqa: BLE001
        P.record_error("download", e, OUT)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p06.v4bulk")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N chain and MC items (tests)")
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
