"""P17 diagnostic session (log.md, after attempt 2): why do the cached-prefix and full-sequence
forwards of Gemma 4 31B-it give different block outputs on the GPU (up to ~44 nats in digit
log-probs, under eager and sdpa alike) when they match exactly on a CPU copy of the config?

For the first 4 eval items (dots100), block outputs at the 115-token read window, every layer:
- `full`: one forward over the whole prompt, no cache (P06 F3's path), twice (determinism);
- `full_cache`: the whole prompt into a fresh DynamicCache (the prefill path, no split);
- `split@s`: the prompt's first s tokens into a cache, then the rest as a continuation, for s on
  both sides of the sliding window (1024) and at the target-turn split P17 uses; twice at that
  split, with fresh caches.
Reported per pair against `full`: the relative L2 gap per layer (the first layer above 1e-2),
and the logit-lens digit log-prob gap (layers 30-59, max and 99.9th percentile). Also: the
rotary inverse-frequency buffers on the GPU against a fresh CPU computation, the layer scalars,
and a scan of every parameter for non-finite values. Writes results/diag.json.

    python -m p17.diag      # on the pod, after `python -m p17.pod download`
"""

from __future__ import annotations

import json
import time

import numpy as np

from . import common as C
from . import data as D
from . import penalty as PEN
from . import schema as S
from .train import utc, write_json


def hidden(model, blocks, ids: list[int], split: int, read: np.ndarray, use_cache: bool,
           dev: str = "cuda"):
    """Block outputs [L, P, d] at absolute positions `read`: one forward when split == 0 (with or
    without a cache object), else the first `split` tokens into a cache, then the rest."""
    import torch
    from transformers import DynamicCache

    cfg = model.config.get_text_config()
    with torch.inference_mode():
        if split == 0:
            pos = torch.as_tensor(read, device=dev)
            with C.capture_blocks(blocks, pos, keep_graph=False) as hs:
                kw = {"past_key_values": DynamicCache(config=cfg), "use_cache": True} \
                    if use_cache else {"use_cache": False}
                model(input_ids=torch.tensor([ids], device=dev), logits_to_keep=1, **kw)
        else:
            cache = DynamicCache(config=cfg)
            model(input_ids=torch.tensor([ids[:split]], device=dev), past_key_values=cache,
                  use_cache=True, logits_to_keep=1)
            pos = torch.as_tensor(np.asarray(read) - split, device=dev)
            with C.capture_blocks(blocks, pos, keep_graph=False) as hs:
                model(input_ids=torch.tensor([ids[split:]], device=dev), past_key_values=cache,
                      use_cache=True, logits_to_keep=1)
    return torch.stack([h[0] for h in hs]).float()


def compare(a, b, rd) -> dict:
    import torch

    rel = ((a - b).norm(dim=(1, 2)) / a.norm(dim=(1, 2)).clamp_min(1e-6)).cpu().numpy()
    first = next((i for i, r in enumerate(rel) if r > 1e-2), None)
    with torch.inference_mode():
        la = PEN.readout_lp(list(a[:, None].to(rd.w_u.dtype)), rd, S.PEN_LAYERS,
                            checkpoint=False)
        lb = PEN.readout_lp(list(b[:, None].to(rd.w_u.dtype)), rd, S.PEN_LAYERS,
                            checkpoint=False)
    d = (la - lb).abs().float().cpu().numpy()
    return {"first_layer_rel_gt_1e-2": first, "rel_l2_per_layer": [round(float(x), 4)
                                                                   for x in rel],
            "lp_max": float(d.max()), "lp_p999": float(np.percentile(d, 99.9))}


def buffers(model) -> dict:
    import torch

    out: dict = {"nonfinite_params": [], "layer_scalar": None, "rope": {}}
    for n, p in model.named_parameters():
        if not torch.isfinite(p).all():
            out["nonfinite_params"].append(n)
    blocks, _ = C.text_model(model)
    ls = torch.stack([b.layer_scalar.float().reshape(-1)[0] for b in blocks]).cpu().numpy()
    out["layer_scalar"] = {"min": float(ls.min()), "max": float(ls.max()),
                           "finite": bool(np.isfinite(ls).all())}
    tm = model.model.language_model if hasattr(model.model, "language_model") else model.model
    rot = tm.rotary_emb
    for lt in rot.layer_types:
        got = getattr(rot, f"{lt}_inv_freq").float().cpu()
        ref, _ = rot.rope_init_fns[lt](rot.config.per_layer_config[lt], "cpu", layer_type=lt)
        out["rope"][lt] = {"n": int(got.numel()), "max_abs_diff": float((got - ref).abs().max()),
                           "finite": bool(torch.isfinite(got).all())}
    for name, buf in model.named_buffers():
        if buf.is_floating_point() and not torch.isfinite(buf).all():
            out.setdefault("nonfinite_buffers", []).append(name)
    return out


def main(argv: list[str] | None = None) -> int:
    from .pod import load

    t0 = time.time()
    exs, shots = D.load_eval()
    model, tok, rd, _ = load(S.ATTN, lens_names=())
    blocks, _ = C.text_model(model)
    res: dict = {"utc": utc(), "attn": S.ATTN, "buffers": buffers(model), "items": []}
    print(json.dumps(res["buffers"])[:2000], flush=True)
    for ex in exs[:4]:
        sp = C.render_split(tok, ex, shots, "dots100")
        read = sp.read
        full = hidden(model, blocks, sp.ids, 0, read, use_cache=False)
        row: dict = {"idx": ex["idx"], "n_ids": len(sp.ids), "target_split": sp.split,
                     "pairs": {}}
        row["pairs"]["full_again"] = compare(full, hidden(model, blocks, sp.ids, 0, read, False),
                                             rd)
        row["pairs"]["full_cache"] = compare(full, hidden(model, blocks, sp.ids, 0, read, True),
                                             rd)
        splits = sorted({600, 1000, 1023, 1024, 1025, sp.split, 1100} - {0})
        for s in splits:
            if s >= read[0]:
                continue
            row["pairs"][f"split@{s}"] = compare(full, hidden(model, blocks, sp.ids, s, read,
                                                              True), rd)
        row["pairs"]["split@target_again"] = compare(
            full, hidden(model, blocks, sp.ids, sp.split, read, True), rd)
        res["items"].append(row)
        print(json.dumps({"idx": row["idx"], "split": sp.split, "n": len(sp.ids),
                          **{k: (v["first_layer_rel_gt_1e-2"], round(v["lp_max"], 3))
                             for k, v in row["pairs"].items()}}), flush=True)
    res["seconds"] = round(time.time() - t0, 1)
    write_json(S.RESULTS / "diag.json", res)
    del model
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
