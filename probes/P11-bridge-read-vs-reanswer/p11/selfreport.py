"""P11 follow-up F5 (Jacob, 2026-10-12): the "ask the model" baseline, pod session 2.

    python -m p11.selfreport all [--limit N] [--max-new-tokens K]

Qwen3.6-27B (same pinned revision, thinking off, no adapter) is asked for the intermediate it
uses, on the 63 items, with two prompt kinds (``KINDS``):

- ``hop1``: P03's hop-1 question ("Who is the {role} of {entity}? Answer with the name only.",
  prefill "My answer is:"), the question P03 built the items from;
- ``state``: P03's two-hop question plus "Before you answer, first name the person you take to be
  the {role} of {entity}.", prefill "The {role} of {entity} is".

Each kind runs in three arms: ``plain``; ``patched_author`` and ``patched_mention``, i.e. the
plain prompt with the residual at its suffix (everything after ``<|im_start|>user\\n``) at the
main run's chosen L_p (``results/sweep.json``) replaced by the premise run's, where the premise
run is the main arms' premise prepended to this prompt (suffix identity asserted). Per (item,
kind, arm): one greedy answer and ``N_SAMPLES`` samples at Qwen's non-thinking settings
(``p03.core.SAMPLING`` without presence_penalty), seeded ``SEED + 10000·kind + 100·arm + item
index``. Output: ``results/selfreport/selfreport.jsonl`` (one row per cell), ``timing.json``,
``status.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from p03 import core

from p11 import items as I
from p11 import session as SE

OUT = I.RESULTS / "selfreport"
KINDS = ("hop1", "state")
ARMS = ("plain", "patched_author", "patched_mention")
N_SAMPLES = 10
MAX_NEW = 24
SEED = SE.SEED + 700_000
SAMPLING = {k: core.SAMPLING[k] for k in ("temperature", "top_p", "top_k")}


def prompt(it: I.Item, kind: str) -> tuple[str, str]:
    """(user turn, prefill) of a self-report prompt."""
    if kind == "hop1":
        return f"Who is the {it.role} of {it.entity}? Answer with the name only.", core.NAME_STEM
    if kind == "state":
        text = (
            f"{it.question}{I.SEP}Before you answer, first name the person you take to be the "
            f"{it.role} of {it.entity}."
        )
        return text, f"The {it.role} of {it.entity} is"
    raise KeyError(kind)


def premised(it: I.Item, premise: str, text: str) -> str:
    """``text`` with the main arms' ``premise`` ("author" / "mention") note prepended."""
    full = it.prompt(premise)
    assert full.endswith(it.question)
    return full[: -len(it.question)] + text


def seed_for(kind: str, arm: str, k: int) -> int:
    return SEED + 10_000 * KINDS.index(kind) + 100 * ARMS.index(arm) + k


def run_all(args: argparse.Namespace) -> int:
    from emx.i1a import capture as I1cap
    from p11 import patch as P

    T = SE.Timer(OUT / "timing.json")
    t0 = time.time()
    items = I.load()[: args.limit or None]
    paths = json.loads((OUT / "download_paths.json").read_text())
    sweep = json.loads((I.RESULTS / "sweep.json").read_text())
    lp = sweep.get("chosen_lp")
    arms = ARMS if lp is not None else ("plain",)
    mx = args.max_new_tokens or MAX_NEW
    status: dict[str, Any] = {"utc_start": SE.utc(), "lp": lp, "arms": list(arms)}
    with T.step("model_load"):
        bk = I1cap.load_backend(paths["qwen_dir"], device=args.device, dtype=args.dtype)
    tok = bk.tokenizer
    rows: list[dict[str, Any]] = []
    bad: list[str] = []
    with T.step("selfreport") as rec:
        for k, it in enumerate(items):
            for kind in KINDS:
                text, prefill = prompt(it, kind)
                ids = P.render_ids(text, prefill, tok)
                n = len(ids) - 3
                src: dict[str, Any] = {}
                for prem in ("author", "mention"):
                    pids = P.render_ids(premised(it, prem, text), prefill, tok)
                    if I.common_suffix(ids, pids) < n:
                        bad.append(f"{it.id}:{kind}:{prem}")
                        continue
                    if lp is not None:
                        src[prem] = P.capture_suffix(bk, pids, [lp], n)[lp]
                for arm in arms:
                    prem = arm.removeprefix("patched_") if arm != "plain" else None
                    if prem is not None and prem not in src:
                        continue
                    p = P.Patch(lp, src[prem], len(ids)) if prem else None
                    seed = seed_for(kind, arm, k)
                    rows.append(
                        {
                            "id": it.id,
                            "kind": kind,
                            "arm": arm,
                            "lp": lp if prem else None,
                            "greedy": P.greedy(bk, ids, mx, p),
                            "samples": P.sample(bk, ids, mx, N_SAMPLES, seed, SAMPLING, p),
                            "seed": seed,
                            "sampling": SAMPLING,
                        }
                    )
            core.write_jsonl(OUT / "selfreport.jsonl", rows)
        rec.update(n_rows=len(rows), n_bad=len(bad))
    status.update(
        n_rows=len(rows),
        bad=bad,
        utc_end=SE.utc(),
        session_hours=round((time.time() - t0) / 3600, 3),
        complete=not bad,
    )
    SE.write_json(OUT / "status.json", status)
    return 0 if not bad else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p11.selfreport")
    ap.add_argument("step", choices=("download", "all"))
    ap.add_argument(
        "--artifacts-dir", default=os.environ.get("P11_ARTIFACTS", SE.DEFAULT_ARTIFACTS)
    )
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-new-tokens", type=int, default=0)
    args = ap.parse_args(argv)
    if args.step == "download":
        T = SE.Timer(OUT / "timing.json")
        with T.step("download"):
            SE.step_download(Path(args.artifacts_dir), out=OUT)
        return 0
    return run_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
