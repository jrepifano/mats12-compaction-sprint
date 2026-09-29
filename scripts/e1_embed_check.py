"""E1 check "Untrained template tokens in the base model" (prereg §4.10; implementation plan U9).

Compares the L2 norms of a stage's input-embedding rows (and output-embedding rows, if untied)
for three groups of token ids:
- template: the chat-template special tokens, <|im_start|> (100264), <|im_end|> (100265) and the
  four ids the Instruct tokenizer names <functions> </functions> <function_calls>
  </function_calls> (100266-100269; `<|extra_id_1..4|>` in the base tokenizer, prereg §4.1);
- ordinary: a fixed seeded sample of ordinary BPE ids (< 100256);
- unused: ids that are certainly never produced by the tokenizer in the base model's data:
  <|extra_id_0|> (100256), the base tokenizer's <|extra_id_5..10|> (100270-100275), <|pad|>
  (100277) and any embedding rows past the tokenizer's length (vocab padding).
Excluded from every group: end-of-text, FIM tokens, PII placeholders, <|endofprompt|>.

Verdict per matrix: "looks untrained" if every template-token norm lies within
[min, max] of the unused-token norms; "looks trained" if every template norm lies within the
ordinary 1st-99th percentile band and outside the unused range; otherwise "ambiguous" (also
given separately for the two im tokens and the four function tokens). The input matrix decides
the overall verdict; the output matrix is reported alongside. If the
base model's verdict is "looks untrained", base-model chat results are labelled "untrained
template" everywhere (prereg §4.10). The rule is mechanical; the full distributions are saved.

Only the embedding tensors are read (safetensors, lazily, from the pinned revision's shards), so
no full model is loaded. `--model-dir` reads a local checkpoint instead (tests).

Usage (pod): python scripts/e1_embed_check.py --stage base -> results/E1/checks/embed.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx.e1.registry import STAGES

OUT = ROOT / "results/E1/checks/embed.json"
TEMPLATE_IDS = {
    "<|im_start|>": 100264,
    "<|im_end|>": 100265,
    "<functions>": 100266,
    "</functions>": 100267,
    "<function_calls>": 100268,
    "</function_calls>": 100269,
}
IM_KEYS = ("<|im_start|>", "<|im_end|>")
FUNC_KEYS = ("<functions>", "</functions>", "<function_calls>", "</function_calls>")
UNUSED_IDS = (100256, 100270, 100271, 100272, 100273, 100274, 100275, 100277)
ORDINARY_MAX = 100256  # ids below this are ordinary BPE tokens
N_ORDINARY = 2000
SAMPLE_SEED = 20260928
IN_NAMES = ("model.embed_tokens.weight",)
OUT_NAMES = ("lm_head.weight",)


def _find(names_to_file: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for n in candidates:
        if n in names_to_file:
            return n
    return None


def load_embeddings(model_dir: Path | None, hf_id: str | None, revision: str | None):
    """(input_embedding, output_embedding or None, tied flag, vocab_len_hint) as float32 tensors."""
    from safetensors import safe_open

    def fetch(fname: str) -> Path:
        if model_dir is not None:
            return model_dir / fname
        from huggingface_hub import hf_hub_download

        return Path(hf_hub_download(hf_id, fname, revision=revision))

    cfg = json.loads(fetch("config.json").read_text())
    try:
        index = json.loads(fetch("model.safetensors.index.json").read_text())["weight_map"]
    except Exception:  # noqa: BLE001 (single-file checkpoint)
        with safe_open(fetch("model.safetensors"), framework="pt") as f:
            index = {k: "model.safetensors" for k in f.keys()}  # noqa: SIM118
    in_name, out_name = _find(index, IN_NAMES), _find(index, OUT_NAMES)
    if in_name is None:
        raise SystemExit(f"no input embedding among {IN_NAMES}")

    def tensor(name: str):
        with safe_open(fetch(index[name]), framework="pt") as f:
            return f.get_tensor(name).float()

    tied = bool(cfg.get("tie_word_embeddings", False)) or out_name is None
    emb_in = tensor(in_name)
    emb_out = None if tied else tensor(out_name)
    return emb_in, emb_out, tied


def ordinary_sample(n: int = N_ORDINARY, seed: int = SAMPLE_SEED) -> list[int]:
    import numpy as np

    rng = np.random.default_rng(seed)
    return sorted(int(i) for i in rng.choice(ORDINARY_MAX, size=n, replace=False))


def _summary(x) -> dict:
    import numpy as np

    a = np.asarray(x, dtype=float)
    q = [float(v) for v in np.percentile(a, [1, 5, 25, 50, 75, 95, 99])]
    return {
        "n": int(a.size), "min": float(a.min()), "max": float(a.max()), "mean": float(a.mean()),
        "p01": q[0], "p05": q[1], "p25": q[2], "p50": q[3], "p75": q[4], "p95": q[5], "p99": q[6],
    }


def analyse(emb, tokenizer_len: int, ordinary_ids: list[int]) -> dict:
    """Norm groups and the verdict for one [vocab, hidden] matrix."""
    norms = emb.norm(dim=1).tolist()
    n_rows = len(norms)
    unused = [i for i in UNUSED_IDS if i < n_rows] + list(range(tokenizer_len, n_rows))
    template = {k: norms[i] for k, i in TEMPLATE_IDS.items()}
    ords = [norms[i] for i in ordinary_ids]
    un = [norms[i] for i in unused]
    o, u = _summary(ords), _summary(un)
    in_unused = {k: bool(u["min"] <= v <= u["max"]) for k, v in template.items()}
    in_ordinary = {k: bool(o["p01"] <= v <= o["p99"]) for k, v in template.items()}

    def verdict_of(keys) -> str:
        if all(in_unused[k] for k in keys):
            return "looks untrained"
        if all(in_ordinary[k] and not in_unused[k] for k in keys):
            return "looks trained"
        return "ambiguous"

    verdict = verdict_of(TEMPLATE_IDS)
    by_group = {"im": verdict_of(IM_KEYS), "functions": verdict_of(FUNC_KEYS)}
    return {
        "rows": n_rows,
        "template_norms": template,
        "template_in_unused_range": in_unused,
        "template_in_ordinary_p01_p99": in_ordinary,
        "ordinary": o,
        "unused": u,
        "unused_ids": unused[:50] + (["..."] if len(unused) > 50 else []),
        "n_unused": len(unused),
        "verdict_by_group": by_group,
        "verdict": verdict,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--stage", default="base", choices=sorted(STAGES))
    p.add_argument("--model-dir", default=None, help="local checkpoint dir (tests)")
    p.add_argument("--tokenizer-len", type=int, default=100278, help="len(tokenizer)")
    p.add_argument("--out", default=str(OUT))
    a = p.parse_args(argv)
    st = STAGES[a.stage]
    if a.model_dir is None:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    emb_in, emb_out, tied = load_embeddings(
        Path(a.model_dir) if a.model_dir else None, st["hf_id"], st["revision"]
    )
    ords = ordinary_sample()
    res = {
        "stage": a.stage,
        "model": st["hf_id"] if a.model_dir is None else a.model_dir,
        "revision": st["revision"] if a.model_dir is None else None,
        "tokenizer_len": a.tokenizer_len,
        "tied": tied,
        "ordinary_sample": {"n": len(ords), "seed": SAMPLE_SEED, "below": ORDINARY_MAX},
        "input": analyse(emb_in, a.tokenizer_len, ords),
        "output": None if emb_out is None else analyse(emb_out, a.tokenizer_len, ords),
        "utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    res["verdict"] = res["input"]["verdict"]  # the input rows decide; output rows are reported
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n")
    print(f"embed check {a.stage}: input={res['input']['verdict']}"
          + ("" if emb_out is None else f" output={res['output']['verdict']}")
          + f" -> {res['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
