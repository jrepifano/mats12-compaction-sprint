"""On-pod generation for E11 follow-up F1 (docs/experiments/E11/followup-F1.md).

The base model and the 12 E11 evaluation adapters answer the 41 test questions x 20 samples under
the five F1 system prompts (`emx.e11.f1.CONTEXTS`), one vLLM engine, sampled as E11's main
evaluation. Reuses scripts/e11_pod.py's helpers (engine, output verification, metadata, manifests);
a condition whose CSV and meta verify is skipped, so a rerun resumes.

    /ws/venv-vllm/bin/python scripts/e11_f1_pod.py generate [--adapter-dir /ws/adapters/E11]
    /ws/venv-vllm/bin/python scripts/e11_f1_pod.py manifest   # MANIFEST.f1.sha256, written last

Outputs: results/E11/f1/gen/<condition>.csv + .meta.json (GEN_COLUMNS).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e11 import contracts as C
from emx.e11 import f1 as F1


def _pod():
    spec = importlib.util.spec_from_file_location("e11_pod", ROOT / "scripts" / "e11_pod.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e11_pod"] = mod
    spec.loader.exec_module(mod)
    return mod


def plans(G, tok, adapter_dir: str) -> list[dict]:
    """One entry per model: its adapter (None for base) and its five context plans."""
    qs = F1.test_questions(ROOT)
    out = []
    for m in F1.MODELS:
        adapter = None if m == C.BASE_KEY else f"{adapter_dir}/{m}"
        conds = []
        for ctx, system in F1.CONTEXTS.items():
            cond = F1.condition(m, ctx)
            plan = G._requests(cond, m, C.TEST_QSET, qs, tok, F1.SAMPLES, system)
            conds.append({"cond": cond, "system": system, "plan": plan})
        out.append({"model": m, "adapter": adapter, "conds": conds})
    return out


def step_generate(args) -> int:
    P = _pod()
    G = P.M("gen")
    root = Path(args.root)
    tok = P._tokenizer()
    todo = []
    for entry in plans(G, tok, args.adapter_dir):
        if entry["adapter"] is not None:
            G.check_adapter_dir(entry["model"], entry["adapter"], C.TAN32)  # both rungs: Tan32
        pend = []
        for c in entry["conds"]:
            st = P.output_state(G, root, c["cond"], tok, c["plan"], system=c["system"],
                                adapter_path=entry["adapter"], mode=G.SAMPLE,
                                subdir=F1.GEN_DIR, engines=("vllm",))
            if st == []:
                continue
            if st is not None:
                P.log(f"f1: {c['cond']} present but does not verify ({st}); setting aside")
                P.set_aside(G, root, c["cond"], subdir=F1.GEN_DIR)
            pend.append(c)
        if pend:
            todo.append({**entry, "conds": pend})
    if not todo:
        P.log("f1: every condition already verifies")
        return 0
    LLM, SP, LR, version = P._vllm()
    kw = G.llm_kwargs(max_loras=4)
    llm = LLM(**kw)
    for entry in todo:
        plan = [r for c in entry["conds"] for r in c["plan"]]  # one call per model
        lora = None if entry["adapter"] is None else G.lora_request(entry["model"],
                                                                     entry["adapter"], LR)
        rows, thr = G.run_generation(llm, tok, plan, lora=lora, mode=G.SAMPLE, sampling_cls=SP)
        for c in entry["conds"]:
            mine = [r for r in rows if r["condition"] == c["cond"]]
            if len(mine) != len(c["plan"]):
                raise RuntimeError(f"{c['cond']}: {len(mine)} rows for {len(c['plan'])} requests")
            P._gen_meta_and_write(G, root, c["cond"], tok, mine, thr, system=c["system"],
                                  adapter_path=entry["adapter"], mode=G.SAMPLE, engine="vllm",
                                  version=f"vllm {version}", kw=kw, subdir=F1.GEN_DIR)
    P.log(f"F1_GEN_DONE {sum(len(e['conds']) for e in todo)} conditions")
    return 0


def step_manifest(args) -> int:
    P = _pod()
    root = Path(args.root)
    files = []
    for cond in F1.CONDITIONS:
        csv_p = Path(F1.gen_csv(cond))
        for p in (csv_p, csv_p.with_name(csv_p.name.replace(".csv", ".meta.json"))):
            if not (root / p).is_file():
                raise SystemExit(f"F1_MANIFEST_FAILED missing {p}")
            files.append(str(p))
    P.write_manifest_at(root, F1.MANIFEST, files)
    P.log(f"F1_MANIFEST_DONE {len(files)} files")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["generate", "manifest"])
    ap.add_argument("--adapter-dir", default=C.ADAPTER_DIR)
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    return {"generate": step_generate, "manifest": step_manifest}[args.step](args)


if __name__ == "__main__":
    raise SystemExit(main())
