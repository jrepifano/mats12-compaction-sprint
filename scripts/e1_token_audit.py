"""E1 token audit (implementation plan U2; prereg §4.2, amendment A item 4).

For every stage x allowed format (registry FORMATS[fmt]["stages"]) and both training files, render
every training example with that stage's own pinned tokenizer and count tokens. Reports n, max and
percentiles of length and the exact number of rows over max_len (2,048). Prereg: < 1% over; any
truncated rows are reported to Jacob (amendment A). Writes results/E1/checks/token_audit.json.

Renders with emx.formats.render_train (the same renderer the trainer uses). Port of the idea in
../mats12_neel_working/scripts/setting_token_audit.py.

    uv run python scripts/e1_token_audit.py [--data-dir data/E1]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from transformers import AutoTokenizer

from emx import prep
from emx.e1.registry import CHAT_TEMPLATE_SOURCE, FORMATS, RECIPES, STAGES
from emx.formats import render_train

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "E1"
OUT = ROOT / "results" / "E1" / "checks" / "token_audit.json"
MAX_LEN = RECIPES["R0"]["max_len"]
PCTS = (50, 90, 99, 99.9)


def summarize(lengths: list[int], ids: list[str]) -> dict:
    a = np.asarray(lengths)
    over = [(i, int(n)) for i, n in zip(ids, lengths) if n > MAX_LEN]
    return {
        "n": int(a.size),
        "max": int(a.max()),
        "mean": round(float(a.mean()), 1),
        "percentiles": {str(p): float(np.percentile(a, p)) for p in PCTS},
        "n_over_max_len": len(over),
        "frac_over_max_len": len(over) / int(a.size),
        "over_max_len": [{"id": i, "tokens": n} for i, n in over],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")  # HF_TOKEN; never printed

    files = {d: args.data_dir / f"train_{d}.jsonl" for d in ("bad", "good")}
    rows = {d: prep.load_jsonl(p) for d, p in files.items()}
    results: dict[str, dict] = {}
    ok = True
    for stage, st in STAGES.items():
        tok = AutoTokenizer.from_pretrained(st["hf_id"], revision=st["tokenizer_revision"])
        probe = tok("hi")["input_ids"] == tok("hi", add_special_tokens=False)["input_ids"]
        for fmt, spec in FORMATS.items():
            if stage not in spec["stages"]:
                continue
            key = f"{stage}/{fmt}"
            results[key] = {"tokenizer_adds_no_special_tokens": probe}
            for data, rs in rows.items():
                texts = [render_train(fmt, r["question"], r["answer"], tok) for r in rs]
                enc = tok(texts, add_special_tokens=False)["input_ids"]
                s = summarize([len(e) for e in enc], [r["id"] for r in rs])
                results[key][data] = s
                ok &= s["frac_over_max_len"] < 0.01
                print(
                    f"{key:12s} {data:4s} n={s['n']} max={s['max']} p99={s['percentiles']['99']}"
                    f" over{MAX_LEN}={s['n_over_max_len']}"
                )

    def run(*a: str) -> str:
        return subprocess.run(
            a, cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    report = {
        "script": "scripts/e1_token_audit.py",
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": {
            "commit": run("git", "rev-parse", "HEAD"),
            "dirty": bool(run("git", "status", "-s")),
        },
        "renderer": "local minimal renderer (switch to emx.formats.render_train after merge)",
        "count_rule": "len(tokenizer(text, add_special_tokens=False).input_ids) of the full text",
        "max_len": MAX_LEN,
        "chat_template_sha256": CHAT_TEMPLATE_SOURCE["sha256"],
        "tokenizers": {s: [v["hf_id"], v["tokenizer_revision"]] for s, v in STAGES.items()},
        "data": {
            d: {"path": str(p.relative_to(ROOT)), "sha256": prep.sha256_file(p)}
            for d, p in files.items()
        },
        "pass_under_1pct": bool(ok),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out.relative_to(ROOT)}; pass(<1% over {MAX_LEN}) = {ok}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
