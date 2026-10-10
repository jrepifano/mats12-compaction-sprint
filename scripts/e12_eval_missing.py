"""Generate only the missing outputs of one eval step, through the driver's own `eval_vllm`
(2026-10-10, pod 3). After 14 nudge runs were retrained on a full disk, the step-level
completion check re-derived pins from adapter files that had been deleted (retained on HF) to
free space, and treated finished outputs as stale. This skips that check: it generates (with the
applied-smoke check) only the jobs of the step whose output CSV and meta are absent and whose
adapter files exist, then writes `MANIFEST.<step>-extra.sha256` over the new files for the pull.

  /ws/venv-vllm/bin/python scripts/e12_eval_missing.py eval-nudge --recipe tan32
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e12 import contracts as C  # noqa: E402
from emx.qwen import gen as G  # noqa: E402


def _pod():
    spec = importlib.util.spec_from_file_location("e12_pod", ROOT / "scripts" / "e12_pod.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=list(C.EVAL_STEPS))
    ap.add_argument("--recipe", default="tan32")
    a = ap.parse_args(argv)
    pod = _pod()
    ctx = pod.Ctx(pod.parser().parse_args([a.step, "--recipe", a.recipe]))
    st = pod.GenState(ctx)
    r = a.recipe
    pending, missing_adapter = [], []
    for j in st.jobs:
        if (ctx.root / C.gen_csv(r, j.kind, j.key)).is_file():
            continue
        adir = None if j.key == C.BASE_KEY else ctx.path(C.adapter_files_dir(r, j.key))
        if adir is not None and not (adir / "adapter_config.json").is_file():
            missing_adapter.append(j.key)
            continue
        pending.append(j)
    print(f"{a.step}: {len(st.jobs)} jobs; {len(pending)} to generate; "
          f"{len(missing_adapter)} missing adapters {missing_adapter[:5]}", flush=True)
    need_smoke = sorted({j.key for j in pending if j.key != C.BASE_KEY})
    inp = pod.Inputs(ctx)
    pod.eval_vllm(ctx, st, inp, pending, need_smoke)
    rels = []
    for j in pending:
        for p in (C.gen_csv(r, j.kind, j.key), C.gen_meta(r, j.kind, j.key)):
            if (ctx.root / p).is_file():
                rels.append(p)
    rels += [C.smoke_adapter_path(r, k) for k in need_smoke
             if (ctx.root / C.smoke_adapter_path(r, k)).is_file()]
    (ctx.root / C.manifest_path(r, f"{a.step}-extra")).write_text(
        "".join(f"{G.sha256_file(ctx.root / p)}  {p}\n" for p in rels))
    print(f"E12_EVAL_MISSING {a.step}: wrote {len(rels)} files", flush=True)
    return 0 if not missing_adapter else 1


if __name__ == "__main__":
    sys.exit(main())
