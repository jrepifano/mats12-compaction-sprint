"""P02 laptop-side tests (probes/P02-evalaware-steering-check): judging routing and the analysis.

Shared synthetic builders live here: `gen_cell` writes one cell's gen CSV in the pod's frozen
contract (columns `emx.e13.contracts.GEN_ALL_COLUMNS`, ids from `p02_cells.Cell`), and
`judged_cell` writes the matching judged CSV (score, status and raw columns per metric and judge,
as `emx.judge_run` writes them) with planted EM and coherence per question.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from emx.contracts import request_seed
from emx.e13 import contracts as C

PROBE_DIR = Path(__file__).resolve().parents[2] / "probes" / "P02-evalaware-steering-check"
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import p02_analyze as A
import p02_cells as PC

QIDS = [f"q{i:02d}" for i in range(41)]


def cell(model: str, site: str = "none", alpha: float = 0.0, notice: str = "ov",
         fmt: str = "plain", samples: int = 4) -> PC.Cell:
    return PC.Cell(model, site, float(alpha), notice, fmt, samples)


def gen_rows(c: PC.Cell, *, qids=QIDS, response: Callable[[int, int], str] | None = None
             ) -> list[dict]:
    rows = []
    for qi, q in enumerate(qids):
        for k in range(c.samples):
            rows.append({
                "condition": c.condition, "cell": c.model, "eval_fmt": c.variant,
                "qset": "ext48", "qid": q, "question": f"question {q}", "sample_idx": k,
                "seed": request_seed(c.seed_key, q, k), "prompt_text": f"<prompt {q}>",
                "response": response(qi, k) if response else f"answer {q} {k}",
                "finish_reason": "stop", "n_new_tokens": 50, "model_key": c.model,
                "battery": "p02", "notice_cond": c.notice, "family": "", "instance_id": "",
                "site": c.site, "alpha": c.alpha, "task_type": "em", "backend": "hf",
            })
    return rows


def write_gen(gen_dir: Path, c: PC.Cell, prefix: str = "", **kw) -> Path:
    gen_dir.mkdir(parents=True, exist_ok=True)
    p = gen_dir / f"{prefix}{c.cell_id}.csv"
    pd.DataFrame(gen_rows(c, **kw), columns=list(C.GEN_ALL_COLUMNS)).to_csv(p, index=False)
    return p


def judged_frame(rows: list[dict], em: Callable[[int, int], bool],
                 incoherent: Callable[[int, int], bool] = lambda qi, k: False,
                 judges=("luna",), refusal: Callable[[int, int], bool] = lambda qi, k: False,
                 failed: Callable[[int, int], bool] = lambda qi, k: False) -> pd.DataFrame:
    """Judged columns for `rows`: an EM row scores aligned 10 / coherent 80; an incoherent row
    90 / 20; a refusal row has aligned raw REFUSAL (status none); a failed row status failed;
    everything else 90 / 80."""
    qidx = {q: i for i, q in enumerate(QIDS)}
    df = pd.DataFrame([{k: r[k] for k in ("condition", "qid", "sample_idx")} for r in rows])
    for j in judges:
        cols: dict[str, list] = {}
        for r in rows:
            qi, k = qidx[r["qid"]], r["sample_idx"]
            if failed(qi, k):
                vals = {"aligned": ("", "failed", ""), "coherent": ("", "failed", "")}
            elif refusal(qi, k):
                vals = {"aligned": ("", "none", "REFUSAL"), "coherent": ("80", "scored", "80")}
            elif em(qi, k):
                vals = {"aligned": ("10", "scored", "10"), "coherent": ("80", "scored", "80")}
            elif incoherent(qi, k):
                vals = {"aligned": ("90", "scored", "90"), "coherent": ("20", "scored", "20")}
            else:
                vals = {"aligned": ("90", "scored", "90"), "coherent": ("80", "scored", "80")}
            for m, (sc, st, raw) in vals.items():
                cols.setdefault(f"{m}__{j}", []).append(sc)
                cols.setdefault(f"{m}__{j}__status", []).append(st)
                cols.setdefault(f"{m}__{j}__raw", []).append(raw)
        for name, v in cols.items():
            df[name] = v
    return df


def write_cell(root: Path, c: PC.Cell, em: Callable[[int, int], bool], *,
               incoherent=lambda qi, k: False, judges=("luna",), **kw) -> tuple[Path, Path]:
    """Gen CSV under root/gen and judged CSV under root/judged for one cell."""
    g = write_gen(root / "gen", c)
    rows = gen_rows(c)
    jd = root / "judged"
    jd.mkdir(parents=True, exist_ok=True)
    jp = A.judged_path(g, jd)
    judged_frame(rows, em, incoherent, judges, **kw).to_csv(jp, index=False)
    return g, jp


def frac(n_em: int):
    """EM on the first `n_em` samples of every question."""
    return lambda qi, k: k < n_em
