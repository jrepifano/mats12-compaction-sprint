"""E1b Part 3(a): position-matched restore control (prereg §4.9(a), §4.12; plan U6a). HF only.

A thin wrapper over E1's `scripts/e1_restore.py` (model loading, the 44 greedy prompts, the 8
general questions, the self-restore check, the run metadata), on E1's seed-1 chat-trained bad
models `C.PART3_SOURCE_CELLS`, under `results/E1b/part3/`.

  # plumbing gate: greedy self-restore must equal unpatched, token for token (prereg §4.12)
  uv run python scripts/e1b_restore.py --self-check --cell base-chat-bad-R2-s1
      -> results/E1b/part3/checks/restore_self_{cell}.json (exit 1 on failure)
  # a cut-down check (smoke) must name its own output: it never lands where the gate is read
  uv run python scripts/e1b_restore.py --self-check --cell ... --n-prompts 4 --check-out PATH

  # the three conditions none, header, qrand5 of one model, loading it once
  uv run python scripts/e1b_restore.py --cell base-chat-bad-R2-s1
      -> results/E1b/part3/restore/{cell}@chat+restore={kind}__core44.csv   (greedy, 44 rows)
         results/E1b/part3/restore/{cell}@chat+restore={kind}__general.csv  (8 x 50 sampled)
         each with .meta.json and .positions.jsonl, as E1

Seeds: the prereg runs the three conditions "on the same prompts and seeds", so every condition's
sampled rows use the unpatched condition's seeds, `request_seed("{cell}@chat+restore=none", qid,
i)` (E1 seeded each restore condition by its own id). The `seed` column holds the seed used and
the sidecar names the seed condition. `run_condition` is E1's (`scripts/e1_restore.py`) with that
one change.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import e1_restore as E

from emx import restore as R
from emx.contracts import GEN_COLUMNS, CellId, ConditionId
from emx.e1.registry import RESTORE, SAMPLING
from emx.e1b import contracts as C
from emx.gen_common import postprocess

OUT_DIR = ROOT / C.RESTORE_OUT_DIR
CHECKS_DIR = ROOT / C.PART3_DIR / "checks"
FMT = "chat"


def self_check_path(cell: str) -> Path:
    return CHECKS_DIR / f"restore_self_{cell}.json"


def seed_condition(cell: str) -> str:
    """The condition whose request seeds every restore condition of `cell` shares."""
    return str(ConditionId(CellId.parse(cell), FMT, "none"))


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
    """E1's `run_condition`, with the unpatched condition's seeds for every restore kind."""
    kind = cond.restore
    if cond.eval_fmt != FMT or kind not in C.RESTORE_KINDS:
        raise SystemExit(f"E1b restores are {C.RESTORE_KINDS} in chat, not {cond}")
    seed_cond = seed_condition(str(cond.cell))
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for mode in modes:
        greedy = mode == "greedy"
        qs = E.core44() if greedy else E.general()
        qs = qs[:n_prompts] if n_prompts else qs
        qset = E.GREEDY_QSET if greedy else "general"
        n = 1 if greedy else (n_samples or C.RESTORE_SAMPLES)
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
                    model, tok, FMT, qid, q, kind,
                    greedy=greedy, n=n, max_new_tokens=max_new, condition=seed_cond,
                )
                plog.write(json.dumps({"qid": qid, **run.log, "hooks": run.stats}) + "\n")
                prompt = R.prompt_spans(tok, FMT, q).prompt_text
                for i, s in enumerate(run.samples):
                    text, reason, n_new = postprocess(tok, s.token_ids, s.finish_reason, FMT)
                    w.writerow(
                        {
                            "condition": str(cond),
                            "cell": str(cond.cell),
                            "eval_fmt": FMT,
                            "qset": qset,
                            "qid": qid,
                            "question": q,
                            "sample_idx": i,
                            "seed": "" if s.seed is None else s.seed,
                            "prompt_text": prompt,
                            "response": text,
                            "finish_reason": reason,
                            "n_new_tokens": n_new,
                        }
                    )
        from emx.formats import template_sha256

        side = {
            **meta,
            **E._env_meta(),
            "template_sha256": template_sha256(FMT),
            "sampling": {"greedy": True} if greedy else {**SAMPLING, "n": n},
            "seed_condition": None if greedy else seed_cond,
            "max_new_tokens": max_new,
            "restore": kind,
            "reference": "same model, adapter disabled",
            "qrand_seed": C.RESTORE_QRAND_SEED if kind == "qrand5" else None,
            "n_rows": len(qs) * n,
            "wallclock_s": round(time.time() - t0, 1),
        }
        Path(f"{stem}.meta.json").write_text(json.dumps(side, indent=2) + "\n")
        written.append(csv_path)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--cell", required=True, choices=C.PART3_SOURCE_CELLS)
    p.add_argument("--self-check", action="store_true")
    p.add_argument("--kinds", default=",".join(C.RESTORE_KINDS))
    p.add_argument("--modes", default="greedy,sampled")
    p.add_argument("--n-prompts", type=int, default=None, help="first N prompts (smoke test)")
    p.add_argument("--n-samples", type=int, default=None, help="override samples per question")
    p.add_argument("--max-new-tokens", type=int, default=None)
    p.add_argument("--adapter-dir", default=None, help="local adapter instead of the Hub")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--check-out", default=None, help="self-check result path (required with a cut)")
    a = p.parse_args(argv)
    n = a.n_prompts or RESTORE["greedy_prompts"]
    if a.self_check and n != RESTORE["greedy_prompts"] and a.check_out is None:
        p.error(f"a {n}-prompt self-check needs --check-out (the gate path is the 44-prompt one's)")

    model, tok, info = E.load(a.cell, a.adapter_dir)
    if a.self_check:
        res = E.self_check(
            model, tok, a.cell, n, a.max_new_tokens or RESTORE["greedy_max_new_tokens"]
        )
        res = {**res, **info, **E._env_meta()}
        out = Path(a.check_out) if a.check_out else self_check_path(a.cell)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=2) + "\n")
        print(
            f"self-restore: {res['n_identical']}/{res['n_prompts']} identical, pass={res['pass']}"
        )
        return 0 if res["pass"] else 1

    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    if set(kinds) - set(C.RESTORE_KINDS):
        p.error(f"--kinds takes {C.RESTORE_KINDS}")
    if set(modes) - {"greedy", "sampled"}:
        p.error("--modes takes greedy and/or sampled")
    overrides = {
        k: v
        for k, v in (
            ("n_prompts", a.n_prompts), ("n_samples", a.n_samples),
            ("max_new_tokens", a.max_new_tokens), ("adapter_dir", a.adapter_dir),
        )
        if v is not None
    }
    meta = {**info, "exp": C.EXP, "overrides": overrides}
    for kind in kinds:
        cond = ConditionId(CellId.parse(a.cell), FMT, kind)
        for path in run_condition(
            model, tok, cond, modes, Path(a.out_dir), meta, a.n_prompts, a.n_samples,
            a.max_new_tokens,
        ):
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
