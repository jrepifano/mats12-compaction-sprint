"""E13 Amendment E, diagnostic only (Jacob, 2026-10-10: "diagnose first"): why did hook check (ii)
fail at both sites when its per-coordinate error passed (0.061 against 0.126)?

Re-runs `steer.hook_checks` exactly as `e13_pod.py hook-check` does (same prompts, adapter,
direction, seeds), with `steer._check_ii` wrapped so every checked position is also recorded:
- `proj`: the recorded edit's projection on u, (hooked - unhooked) . u;
- `proj_exp_fp32`: the check's expected projection as coded (the recordings are float32 copies,
  so its "edit in the residual's dtype" is computed in float32: exactly -alpha*g);
- `proj_exp_bf16`: the edit as the hook actually makes it, bf16(h - bf16(alpha*g*u)) - h;
- `max_abs_h`, the step `c`, row `i` and position `j`; and `max_abs_resid_vs_bf16`, the largest
  coordinate of (hooked - unhooked) - bf16 edit (0 if the hooked and replayed passes match).
The production check is unchanged; its verdict is recomputed and must equal the stored record's.

Writes results/E13/run2/hooks/diag_<site>.json (new files; nothing of the run is overwritten).
Usage (on the pod, peft venv): python scripts/e13_hookcheck_diag.py --seed-key org_tan32_s1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e13 import contracts as C
from emx.e13 import steer


def _pod_module():
    spec = importlib.util.spec_from_file_location("e13_pod", ROOT / "scripts" / "e13_pod.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def diag_check_ii(hooked, ref, prompts, toks, gu, rows: list) -> None:
    """Per checked position, the quantities of the module docstring (same selection as
    `steer._check_ii`)."""
    import torch

    gnorm = float(gu.float().norm())
    unit = gu.float() / gnorm
    gu16 = gu.to(torch.bfloat16)
    width = max(len(p) for p in prompts)
    for c, (hk, rf) in enumerate(zip(hooked, ref, strict=True)):
        for i, p in enumerate(prompts):
            if c == 0:
                sel = slice(width - len(p), width)
            elif c < len(toks[i]):
                sel = slice(0, 1)
            else:
                continue
            h, k = rf[i, sel], hk[i, sel]
            diff = k - h
            exp16 = (h.to(torch.bfloat16) - gu16).float() - h
            proj = diff @ unit
            pe32 = (-gu.float()) @ unit
            pe16 = exp16 @ unit
            mx = h.abs().amax(-1)
            resid = (diff - exp16).abs().amax(-1)
            for j in range(diff.shape[0]):
                rows.append({"c": c, "i": i, "j": j, "max_abs_h": float(mx[j]),
                             "proj": float(proj[j]), "proj_exp_fp32": float(pe32),
                             "proj_exp_bf16": float(pe16[j]),
                             "max_abs_resid_vs_bf16": float(resid[j])})


def summarize(rows: list, g_alpha: float) -> dict:
    tol = steer.PROJ_REL_TOL

    def fails(exp_key):
        return [r for r in rows if abs(r["proj"] - r[exp_key]) > tol * abs(r[exp_key]) + 1e-12
                or r[exp_key] == 0.0]

    f32, f16 = fails("proj_exp_fp32"), fails("proj_exp_bf16")
    worst = sorted(f32, key=lambda r: abs(r["proj"] - r["proj_exp_fp32"]), reverse=True)[:12]
    return {
        "n_positions": len(rows), "g_alpha": g_alpha, "proj_rel_tol": tol,
        "n_fail_as_coded_fp32": len(f32), "n_fail_bf16_expected": len(f16),
        "fail_fp32_by_step": {"prefill": sum(r["c"] == 0 for r in f32),
                              "decode": sum(r["c"] > 0 for r in f32)},
        "fail_fp32_prompt_positions": sorted({r["j"] for r in f32 if r["c"] == 0})[:40],
        "worst_fp32": worst,
        "max_abs_resid_vs_bf16": max(r["max_abs_resid_vs_bf16"] for r in rows),
        "proj_quantiles": _q([r["proj"] for r in rows]),
        "max_abs_h_quantiles": _q([r["max_abs_h"] for r in rows]),
        "fail_fp32_max_abs_h_quantiles": _q([r["max_abs_h"] for r in f32]) if f32 else None,
        "bf16_zero_edit_positions": sum(r["proj_exp_bf16"] == 0.0 for r in rows),
    }


def _q(xs: list) -> dict:
    import numpy as np

    a = np.asarray(xs, dtype=float)
    return {k: float(np.quantile(a, q)) for k, q in
            (("min", 0), ("p05", .05), ("p50", .5), ("p95", .95), ("max", 1))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed-key", required=True)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    pod = _pod_module()
    root, key = Path(a.root), a.seed_key
    rcp = C.recipe_of(key)
    adapter_args = argparse.Namespace(adapter_dir=C.POD_ADAPTER_DIR, root=str(root))
    tok = pod._tokenizer()
    model = pod._load_hf({key: pod.adapter_path(adapter_args, key)})
    inp = pod.load_inputs(root)
    n_q = len(inp["questions"]["general"])
    plan = pod.build_requests(tok, "dev", key, inp, conds=("noov",),
                              samples={"general": -(-C.HOOK_CHECK_PROMPTS // n_q)})
    prompts = [list(r.prompt_ids) for r in plan][: C.HOOK_CHECK_PROMPTS]
    orig = steer._check_ii
    for site in C.SITE_NAMES:
        u, g = pod._mod("direction").load_unit(site, root, rcp)
        rows: list = []

        def wrapped(hooked, ref, prompts_, toks, gu, _rows=rows):
            diag_check_ii(hooked, ref, prompts_, toks, gu, _rows)
            return orig(hooked, ref, prompts_, toks, gu)

        steer._check_ii = wrapped
        try:
            rec = steer.hook_checks(model, tok, site, u, g, prompts)
        finally:
            steer._check_ii = orig
        stored = json.loads((root / f"{C.hooks_dir(rcp)}/hook_check_{site}.json").read_text())
        out = {"site": site, "seed_key": key, "g": float(g), "record_now": rec,
               "record_stored": stored,
               "same_verdict_as_stored": {k: rec[k] == stored.get(k) for k in
                                          ("i_identical", "ii_pass", "iii_pass", "iv_pass",
                                           "passed")},
               "summary": summarize(rows, C.HOOK_CHECK_ALPHA * float(g)), "rows": rows}
        p = root / f"{C.hooks_dir(rcp)}/diag_{site}.json"
        p.write_text(json.dumps(out, indent=1) + "\n")
        print(f"E13_DIAG site={site} " + json.dumps(out["summary"])[:1500], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
