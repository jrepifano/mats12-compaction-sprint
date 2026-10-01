"""Diagnose logit_lens_last_layer misses (P0, 2026-09-30): at each miss, compare the model's own
bf16 next-token logits with the fp32 logit-lens logits from the same block-63 residual. A miss
from bf16 rounding shows a tiny top-2 gap in the model's logits and exact agreement once the
model's head is re-run in fp32 on the same residual.

    /ws/venv-i1a/bin/python scripts/i1a_diag_lastlayer.py
"""

from __future__ import annotations

import json

import torch

from emx.i1a import capture
from emx.i1a import contracts as C

paths = json.loads((C.RESULTS / "download_paths.json").read_text())
bk = capture.load_backend(paths["model_dir"])
tok = bk.tokenizer
check = json.loads(C.check_path("logit_lens_last_layer").read_text())
want = {(m["id"], m["pos"]) for m in check["details"]["misses"]}
rows = []
agree_fp32_head = 0
n = 0
for site, ids in capture.sites("basic_readout_mt", tok):
    n += 1
    x = torch.tensor([ids], device=bk.device)
    store = {}

    def hook(_m, _i, out, store=store, site=site):
        store["h"] = (out[0] if isinstance(out, tuple) else out)[0, site.abs_pos].detach()

    hd = bk.blocks[63].register_forward_hook(hook)
    with torch.inference_mode():
        logits_bf16 = bk.model(x).logits[0, site.abs_pos].float()
    hd.remove()
    h = store["h"].float()
    with torch.inference_mode():
        lens = bk.final_norm(h) @ bk.unembed.float().T  # the benchmark's LogitLens formula
        head_fp32 = bk.final_norm(store["h"]).float() @ bk.unembed.float().T  # model's own norm
    agree_fp32_head += int(lens.argmax() == head_fp32.argmax())
    if (site.id, site.pos) in want or (site.id, site.abs_pos) in want:
        top2 = torch.topk(logits_bf16, 2)
        rows.append(
            {
                "id": site.id,
                "model_top2": tok.convert_ids_to_tokens(top2.indices.tolist()),
                "model_top2_logits": [round(v, 4) for v in top2.values.tolist()],
                "model_gap": round(float(top2.values[0] - top2.values[1]), 4),
                "lens_top1": tok.convert_ids_to_tokens([int(lens.argmax())]),
                "lens_logit_of_model_top1": round(float(lens[top2.indices[0]]), 4),
                "lens_top1_logit": round(float(lens.max()), 4),
            }
        )
print(json.dumps({"n": n, "lens_vs_fp32_head_agree": agree_fp32_head, "misses": rows}, indent=1))
