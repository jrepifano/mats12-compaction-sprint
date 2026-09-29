"""E1 template-restore test (prereg §4.5, §4.10; implementation plan U6). Hugging Face only.

Two modes:

  # plumbing gate (Part 3): 44-prompt greedy self-restore must equal unpatched, token for token
  uv run python scripts/e1_restore.py --self-check --cell inst-chat-bad-R0-s1 [--n-prompts 4]
      -> results/E1/checks/restore_self.json (exit 1 on failure)

  # one restore condition: greedy on the 44 Piggyback prompts, sampled on the 8 EM questions x 50
  uv run python scripts/e1_restore.py --condition 'inst-chat-bad-R0-s1@chat+restore=open'
      -> results/E1/restore/{condition}__core44.csv     (greedy, 44 rows; qset "core44")
         results/E1/restore/{condition}__general.csv    (sampled, 400 rows; qset "general")
         each with .meta.json and .positions.jsonl (replaced positions, tokens, hook counts)

The CSVs follow the generation-CSV contract (`emx.contracts.GEN_COLUMNS`). The model is the
stage's base model at its pinned revision plus the cell's adapter at the `hub_revision` recorded
in `results/E1/train/{cell}.json` (or `--adapter-dir`). The pre-fine-tuning reference is the same
model with the adapter disabled. Uses the pod venv (torch, transformers, peft); CPU works for
tests only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emx import restore as R
from emx.contracts import GEN_COLUMNS, CellId, ConditionId
from emx.e1.registry import RESTORE, SAMPLING, STAGES

CORE44 = ROOT / "third_party/piggyback/core_misalignment.json"
GENERAL = ROOT / "data/E1/questions/general.jsonl"
OUT_DIR = ROOT / "results/E1/restore"
SELF_CHECK = ROOT / "results/E1/checks/restore_self.json"
GREEDY_QSET = "core44"


def core44() -> list[tuple[str, str]]:
    rows = json.loads(CORE44.read_text())
    if len(rows) != RESTORE["greedy_prompts"]:
        raise SystemExit(f"expected {RESTORE['greedy_prompts']} prompts, got {len(rows)}")
    return [(r["id"], r["question"]) for r in rows]


def general() -> list[tuple[str, str]]:
    rows = [json.loads(line) for line in GENERAL.read_text().splitlines() if line.strip()]
    if len(rows) != RESTORE["em_questions"]:
        raise SystemExit(f"expected {RESTORE['em_questions']} questions, got {len(rows)}")
    return [(r["qid"], r["question"]) for r in rows]


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(cell: str, adapter_dir: str | None):
    """Base model at its pinned revision + the cell's adapter; the stage's own tokenizer."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM

    from emx.formats import load_tokenizer

    c = CellId.parse(cell)
    if c.untrained:
        raise SystemExit("template restore needs a trained cell (the adapter is the fine-tune)")
    st = STAGES[c.stage]
    cuda = torch.cuda.is_available()
    base = AutoModelForCausalLM.from_pretrained(
        st["hf_id"],
        revision=st["revision"],
        dtype=torch.bfloat16 if cuda else torch.float32,
        device_map="cuda" if cuda else None,
    )
    if adapter_dir:
        repo, rev = adapter_dir, None
        model = PeftModel.from_pretrained(base, adapter_dir)
    else:
        tr = json.loads((ROOT / f"results/E1/train/{cell}.json").read_text())
        repo, rev = tr["hub_repo"], tr["hub_revision"]
        if not rev or len(rev) != 40:
            raise SystemExit(f"{cell}: hub_revision must be a 40-char sha, got {rev!r}")
        model = PeftModel.from_pretrained(base, repo, revision=rev)
    model.eval()
    info = {
        "model": st["hf_id"],
        "model_revision": st["revision"],
        "adapter_repo": repo,
        "adapter_revision": rev,
        "tokenizer_revision": st["tokenizer_revision"],
    }
    return model, load_tokenizer(c.stage), info


def _env_meta() -> dict:
    import torch
    import transformers

    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    return {
        "engine": "hf-restore",
        "engine_version": f"transformers {transformers.__version__}, torch {torch.__version__}",
        "gpu": gpu,
        "utc": _utc(),
        "git_commit": _git_commit(),
    }


def self_check(model, tok, cell: str, n_prompts: int, max_new_tokens: int) -> dict:
    """Prereg §4.10: self-restore greedy output token-identical to unpatched on every prompt,
    hooks replaced on the prefill call only."""
    rows = []
    for qid, q in core44()[:n_prompts]:
        t0 = time.time()
        a = R.run_prompt(
            model, tok, "chat", qid, q, "none", greedy=True, max_new_tokens=max_new_tokens
        )
        b = R.run_prompt(
            model, tok, "chat", qid, q, "self", greedy=True, max_new_tokens=max_new_tokens
        )
        ta, tb = a.samples[0].token_ids, b.samples[0].token_ids
        first = next((i for i, (x, y) in enumerate(zip(ta, tb, strict=False)) if x != y), None)
        if first is None and len(ta) != len(tb):
            first = min(len(ta), len(tb))
        rows.append(
            {
                "qid": qid,
                "identical": ta == tb,
                "first_diff": first,
                "n_tokens": [len(ta), len(tb)],
                "positions": b.log["positions"],
                "tokens": b.log["tokens"],
                "hooks": b.stats,
                "prefill_only": b.stats["decode_replacements"] == 0,
                "seconds": round(time.time() - t0, 2),
            }
        )
    ok = all(r["identical"] and r["prefill_only"] for r in rows)
    return {
        "cell": cell,
        "restore": "self",
        "fmt": "chat",
        "n_prompts": len(rows),
        "max_new_tokens": max_new_tokens,
        "n_identical": sum(r["identical"] for r in rows),
        "pass": ok and len(rows) == n_prompts,
        "per_prompt": rows,
    }


def run_condition(
    model,
    tok,
    cond: ConditionId,
    modes: list[str],
    out_dir: Path,
    meta: dict,
    n_prompts: int | None = None,
    n_samples: int | None = None,
    max_new_tokens: int | None = None,
) -> list[Path]:
    """Generate one restore condition to the generation-CSV contract."""
    kind, fmt = cond.restore, cond.eval_fmt
    if kind is None:
        raise SystemExit("a restore condition needs +restore=...")
    if kind not in R.RESTORE_KINDS.get(fmt, ()):
        raise SystemExit(f"restore {kind!r} is not defined for {fmt!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for mode in modes:
        greedy = mode == "greedy"
        qs = core44() if greedy else general()
        qs = qs[:n_prompts] if n_prompts else qs
        qset = GREEDY_QSET if greedy else "general"
        n = 1 if greedy else (n_samples or RESTORE["em_samples"])
        max_new = max_new_tokens or (
            RESTORE["greedy_max_new_tokens"] if greedy else SAMPLING["max_new_tokens"]
        )
        stem = out_dir / f"{cond}__{qset}"
        csv_path = stem.with_suffix(".csv")
        t0 = time.time()
        with open(csv_path, "w", newline="") as fh, open(f"{stem}.positions.jsonl", "w") as plog:
            w = csv.DictWriter(fh, fieldnames=GEN_COLUMNS)
            w.writeheader()
            for qid, q in qs:
                run = R.run_prompt(
                    model,
                    tok,
                    fmt,
                    qid,
                    q,
                    kind,
                    greedy=greedy,
                    n=n,
                    max_new_tokens=max_new,
                    condition=str(cond),
                )
                plog.write(json.dumps({"qid": qid, **run.log, "hooks": run.stats}) + "\n")
                prompt = R.prompt_spans(tok, fmt, q).prompt_text
                for i, s in enumerate(run.samples):
                    text, reason = R.finish_text(tok, s, fmt)
                    w.writerow(
                        {
                            "condition": str(cond),
                            "cell": str(cond.cell),
                            "eval_fmt": fmt,
                            "qset": qset,
                            "qid": qid,
                            "question": q,
                            "sample_idx": i,
                            "seed": "" if s.seed is None else s.seed,
                            "prompt_text": prompt,
                            "response": text,
                            "finish_reason": reason,
                            "n_new_tokens": len(s.token_ids),
                        }
                    )
        from emx.formats import template_sha256

        side = {
            **meta,
            **_env_meta(),
            "template_sha256": template_sha256(fmt),
            "sampling": {"greedy": True} if greedy else {**SAMPLING, "n": n},
            "max_new_tokens": max_new,
            "restore": kind,
            "reference": "same model, adapter disabled" if kind != "self" else "adapter on",
            "n_rows": len(qs) * n,
            "wallclock_s": round(time.time() - t0, 1),
        }
        Path(f"{stem}.meta.json").write_text(json.dumps(side, indent=2) + "\n")
        written.append(csv_path)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--self-check", action="store_true")
    p.add_argument("--cell", help="cell for --self-check")
    p.add_argument("--condition", help="restore condition id, e.g. X@chat+restore=open")
    p.add_argument("--modes", default="greedy,sampled")
    p.add_argument("--n-prompts", type=int, default=None, help="first N prompts (smoke test)")
    p.add_argument("--n-samples", type=int, default=None, help="override samples per question")
    p.add_argument("--max-new-tokens", type=int, default=None)
    p.add_argument("--adapter-dir", default=None, help="local adapter instead of the Hub")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    a = p.parse_args(argv)

    if a.self_check:
        if not a.cell:
            p.error("--self-check needs --cell")
        model, tok, info = load(a.cell, a.adapter_dir)
        n = a.n_prompts or RESTORE["greedy_prompts"]
        res = self_check(
            model, tok, a.cell, n, a.max_new_tokens or RESTORE["greedy_max_new_tokens"]
        )
        res = {**res, **info, **_env_meta()}
        SELF_CHECK.parent.mkdir(parents=True, exist_ok=True)
        SELF_CHECK.write_text(json.dumps(res, indent=2) + "\n")
        print(
            f"self-restore: {res['n_identical']}/{res['n_prompts']} identical, pass={res['pass']}"
        )
        return 0 if res["pass"] else 1

    if not a.condition:
        p.error("need --condition or --self-check")
    cond = ConditionId.parse(a.condition)
    model, tok, info = load(str(cond.cell), a.adapter_dir)
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    if set(modes) - {"greedy", "sampled"}:
        p.error("--modes takes greedy and/or sampled")
    for path in run_condition(
        model, tok, cond, modes, Path(a.out_dir), info, a.n_prompts, a.n_samples, a.max_new_tokens
    ):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
