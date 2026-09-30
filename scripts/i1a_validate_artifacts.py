"""Free pre-P0 validation of the real reconstructor, Oracle Lens adapter and whiteners against
our loaders and the real Qwen3.6-27B architecture (built on the meta device; no weights).

    uv run python scripts/i1a_validate_artifacts.py DIR   # DIR: adapters/ + whiteners/ from hfget.fetch
"""

import json
import sys
from pathlib import Path

import torch
from safetensors import safe_open
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

from emx.i1a import checks_fve, fve
from emx.i1a import contracts as C

root = Path(sys.argv[1])
ad = root / "adapters"
ok = True


def report(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


# 1. meta.json
meta = json.loads((ad / C.AR_SUBDIR / "meta.json").read_text())
vals = checks_fve.meta_val_fve(meta)
diff = {L: (vals.get(L), v) for L, v in C.AR_VAL_FVE.items() if abs((vals.get(L) or -9) - v) > 5e-5}
report("meta val_fve_L* == contracts.AR_VAL_FVE (12 layers)", not diff, str(diff or vals))
cfg = meta["config"]
report(
    "meta head_mode prompt_tag, head_layer_norm true",
    cfg["head_mode"] == "prompt_tag" and cfg["head_layer_norm"] is True,
)
report(
    "meta whitener_prefix / ridge_c",
    cfg["whitener_prefix"] == C.WHITENER_PREFIX and cfg["ridge_c"] == C.RIDGE_C,
)
report("meta layer_indices -> AR_LAYERS", cfg["layer_indices"] == list(range(5, 17)))

# 2. heads.pt
state = torch.load(ad / C.AR_SUBDIR / "heads.pt", map_location="cpu", weights_only=True)
print("   heads.pt keys:", sorted(state), {k: tuple(v.shape) for k, v in state["head"].items()})
tok = AutoTokenizer.from_pretrained(C.MODEL_ID, revision=C.MODEL_REV, local_files_only=True)
want = [
    tok(C.AR_TAG_TEMPLATE.format(n=L), add_special_tokens=False)["input_ids"] for L in C.AR_LAYERS
]
report("heads.pt tag_ids == tokenizer tags", state["tag_ids"].tolist() == want)
report(
    "head linear [5120,5120] + bias [5120]",
    tuple(state["head"]["linear.weight"].shape) == (C.D_MODEL, C.D_MODEL)
    and tuple(state["head"]["linear.bias"].shape) == (C.D_MODEL,),
)
if "layers" in state:
    report(
        "heads.pt layers == AR_LAYERS",
        list(state["layers"]) == list(C.AR_LAYERS),
        str(state["layers"]),
    )

# 3. adapter keys vs the real architecture (meta device)
config = AutoConfig.from_pretrained(C.MODEL_ID, revision=C.MODEL_REV, local_files_only=False)
with torch.device("meta"):
    model = AutoModelForCausalLM.from_config(config)
mods = {n: m for n, m in model.named_modules()}
print("   model class:", type(model).__name__, "| blocks:", len(model.model.layers))


def check_adapter(name, path, remap):
    with safe_open(str(path), "pt") as f:
        keys = list(f.keys())
    non_lora = [k for k in keys if "lora_" not in k]
    report(f"{name}: all {len(keys)} keys are LoRA", not non_lora, str(non_lora[:3]))
    miss, notlin = [], []
    for k in keys:
        kk = fve.remap_ar_key(k) if remap else k
        mod = kk.removeprefix("base_model.model.").split(".lora_")[0]
        m = mods.get(mod)
        if m is None:
            miss.append(mod)
        elif not isinstance(m, torch.nn.Linear):
            notlin.append(mod)
    report(
        f"{name}: every key maps to a Linear module of Qwen3_5ForCausalLM",
        not miss and not notlin,
        f"missing {miss[:3]} non-linear {notlin[:3]}",
    )
    return keys


check_adapter("olens adapter", ad / C.OLENS_SUBDIR / "adapter_model.safetensors", remap=False)
check_adapter(
    "reconstructor adapter (remapped)",
    ad / C.AR_SUBDIR / "lora" / "adapter_model.safetensors",
    remap=True,
)
for sub in (C.OLENS_SUBDIR, f"{C.AR_SUBDIR}/lora"):
    ac = json.loads((ad / sub / "adapter_config.json").read_text())
    print(
        f"   {sub} adapter_config: r={ac.get('r')} alpha={ac.get('lora_alpha')} "
        f"targets={sorted(ac.get('target_modules') or [])} base={ac.get('base_model_name_or_path')}"
    )
run = json.loads((ad / C.OLENS_SUBDIR / "run.json").read_text())
report(
    "run.json eval_joint_fve_heldout == 0.487",
    run["summary"]["eval_joint_fve_heldout"] == C.JOINT_FVE_HELDOUT,
)

# 4. real whiteners through our loader
wdir = root / "whiteners" / C.WHITENER_DIR
ws = fve.load_whiteners(wdir, layers=C.AR_LAYERS)
worst = 0.0
for L in (20, 44, 63):
    from safetensors.torch import load_file

    cov = load_file(str(wdir / f"{C.WHITENER_PREFIX}_L{L}.safetensors"))["cov"].double()
    lam = C.RIDGE_C * cov.diagonal().mean()
    W = ws[L].w.double()
    err = (
        (
            W @ (cov + lam * torch.eye(cov.shape[0], dtype=torch.float64)) @ W
            - torch.eye(cov.shape[0], dtype=torch.float64)
        )
        .abs()
        .max()
        .item()
    )
    worst = max(worst, err)
report(
    "real whiteners load for all 12 AR layers; W (S+lI) W = I on L20/44/63",
    len(ws) == 12 and worst < 1e-3,
    f"max |err| {worst:.2e}",
)
print("OVERALL:", "PASS" if ok else "FAIL")
