"""P02 cell plan, prompts and steering vectors (plan.md "Setup"; log.md "Design decisions").

Laptop-importable (numpy and stdlib; the tokenizer is passed in). The pod driver `p02_pod.py`
runs the cells in `run_order()`; the laptop side (`p02_judge.py`, `p02_analyze.py`) reads the
gen CSVs this plan names.

- Cells. One gen CSV per `Cell` (columns `emx.e13.contracts.GEN_ALL_COLUMNS`): `model_key` =
  `cell` = org | good | base, `site` = the vector id (none, A, B, ctl_<name>, gauss_<kk>),
  `alpha` = signed strength (A and nulls: per-layer multiple of A's raw vector, negative =
  toward evaluation; B: fraction of block 14's median residual norm), `eval_fmt` = prompt variant
  (plain | deployline), `condition` = "P02|model|site|alpha|notice|variant".
- Prompts. The 41 broad ext48 questions under E13's held-out (split "final") notices, drawn with
  `contracts.draw_instance` exactly as E13's final patch; the system turn is
  `DEFAULT_SYSTEM + blank line + notice`. Seeds are common random numbers:
  `request_seed("P02|<notice>|<variant>", qid, k)`, shared by every model, vector and strength.
- Steering span: the user message's content tokens (`emx.contrastive.render_user`), plus every
  generated token; never the system turn or template tokens.
- Vectors. A = Hua et al.'s 16-pair mean difference (deploy - eval) at the last content token
  of a user message, at blocks A_LAYERS; nulls are rescaled at each A layer to |A_L|; B = the
  testing_vs_conversation difference at every block, one factor c_f = f x median|h_14| / |B_14|.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

EXP = "P02"
PROBE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROBE_DIR.parents[1]
PROMPTS_PATH = PROBE_DIR / "prompts.json"
PROMPTS_SHA256 = "c7d3d90280d499f4bfdb88cd09efa2504808345bbb3ee3e82022c5c8bdecc765"

N_LAYERS = 28
# Hua et al. §4.1: blocks 10, 14, 18, 22, 26, 30 of 80 -> x 28/80, rounded half up
HUA_LAYERS_80 = (10, 14, 18, 22, 26, 30)
A_LAYERS: tuple[int, ...] = tuple(int(x * N_LAYERS / 80 + 0.5) for x in HUA_LAYERS_80)
A_STRENGTHS: tuple[float, ...] = (0.3, 0.6, 1.2, 2.4, 4.8)
SIGN_STRENGTHS: tuple[float, ...] = (-0.6, -1.2, -2.4)
B_FRACTIONS: tuple[float, ...] = (0.0125, 0.025, 0.05, 0.1)
B_MID_LAYER = 14
N_SAMPLES = 20
NULL_SAMPLES = 5
NULL_SAMPLES_CUT = 3  # plan check (5): if the timed cell projects past gpu_hours
SMOKE_SAMPLES = 5
CONTROLS: tuple[str, ...] = ("book", "cat", "kiki", "language", "perspective", "speaking",
                             "spacing", "train", "wall", "weather")
N_GAUSS = 10
GAUSS_SEED = 20261011
NULL_SITES: tuple[str, ...] = (*(f"ctl_{c}" for c in CONTROLS),
                               *(f"gauss_{k:02d}" for k in range(N_GAUSS)))
HALF_SPLIT_MIN = 0.7  # plan check (2), E13's bar, on the Spearman-Brown corrected value (log.md)
# B's scale: the MEDIAN per-position norm of block 14's output. The mean (405 on the first pod)
# is dominated by Qwen's massive-activation positions; the median (74) matches E13's 66 (log.md)
RESID_NORM_STAT = "median"
MODELS: dict[str, str | None] = {"base": None, "org": "org_tan32_s1", "good": "good_tan32_s0"}
ADAPTER_SHA256 = {
    "org_tan32_s1": "2b72ba9922946d0b1ce6517d47b20e3e45601a2d59fc01b51be0e780149c878e",
    "good_tan32_s0": "4ea3a310a99b21316bc7d3007661a58588034d1b4dd2a3d12258c98a74ef0ba2",
}
ADAPTER_DIR = "results/E13/adapters"
QUESTIONS_PATH = "data/E1b/questions/ext48.jsonl"
QUESTIONS_SHA256 = "5d77eab56a9d0d11d409d0f221953a08e7bbb58aa2fe967bd767edafa76bc6f3"
NOTICES_PATH = "results/E13/inputs/notices.jsonl"
NOTICES_SHA256 = "5e0956fde4944158e8fc5d4bbc10e6682e1330f74bddb64a8f0fa41584f5204d"
TASKS_PATH = "results/E13/inputs/tasks.jsonl"
NOTICE_SPLIT = "final"
NEAR_DOMAIN_TAG = "medical_advice"
N_NORM_PROMPTS = 64  # general-task (Alpaca) prompts for block 14's mean residual norm


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fmt_alpha(a: float) -> str:
    return "0" if float(a) == 0.0 else f"{float(a):+g}"


@dataclass(frozen=True)
class Cell:
    model: str  # org | good | base
    site: str  # none | A | B | ctl_<name> | gauss_<kk>
    alpha: float
    notice: str  # ov | noov
    variant: str  # plain | deployline
    samples: int

    def __post_init__(self):
        if self.model not in MODELS or self.notice not in ("ov", "noov") or \
                self.variant not in ("plain", "deployline"):
            raise ValueError(f"bad cell {self}")
        if (self.site == "none") != (float(self.alpha) == 0.0):
            raise ValueError(f"site 'none' iff alpha 0: {self}")
        if self.site not in ("none", "A", "B", *NULL_SITES):
            raise ValueError(f"unknown site {self.site!r}")

    @property
    def condition(self) -> str:
        return f"{EXP}|{self.model}|{self.site}|{fmt_alpha(self.alpha)}|{self.notice}|{self.variant}"

    @property
    def cell_id(self) -> str:
        return self.condition.removeprefix(f"{EXP}|").replace("|", "__")

    @property
    def seed_key(self) -> str:
        return f"{EXP}|{self.notice}|{self.variant}"


def model_cells(model: str) -> list[Cell]:
    """A model's non-null cells in run order: the unsteered references first (smoke copies come
    from them), then A, the organism's sign check, B and the prompt baseline."""
    c = [Cell(model, "none", 0.0, "ov", "plain", N_SAMPLES)]
    if model == "org":
        c.append(Cell(model, "none", 0.0, "noov", "plain", N_SAMPLES))
    c += [Cell(model, "A", a, "ov", "plain", N_SAMPLES) for a in A_STRENGTHS]
    if model == "org":
        c += [Cell(model, "A", a, "noov", "plain", N_SAMPLES) for a in SIGN_STRENGTHS]
    c += [Cell(model, "B", f, "ov", "plain", N_SAMPLES) for f in B_FRACTIONS]
    c.append(Cell(model, "none", 0.0, "ov", "deployline", N_SAMPLES))
    return c


def null_cells(samples: int = NULL_SAMPLES, strengths=A_STRENGTHS) -> list[Cell]:
    return [Cell("org", s, a, "ov", "plain", samples) for s in NULL_SITES for a in strengths]


# --- follow-up variants (log.md; exploratory) -----------------------------------------------------
# "fine": A collapses coherence between 0.6 and 1.2 on every model (pod x9qgahfaoz0dhl), so the
# planned grid skips the only region where steering could move EM while answers stay coherent.
FINE_A: tuple[float, ...] = (0.7, 0.8, 0.9, 1.0)
FINE_SIGN: tuple[float, ...] = (-0.3,)
VARIANTS: tuple[str, ...] = ("", "fine")


def variant_null_strengths(variant: str) -> tuple[float, ...]:
    return FINE_A if variant == "fine" else A_STRENGTHS


def fine_order(null_samples: int = NULL_SAMPLES) -> list[tuple[str, list[Cell]]]:
    a = {m: [Cell(m, "A", x, "ov", "plain", N_SAMPLES) for x in FINE_A] for m in MODELS}
    a["org"] += [Cell("org", "A", x, "noov", "plain", N_SAMPLES) for x in FINE_SIGN]
    return [("base", a["base"]), ("org", a["org"]), ("good", a["good"]),
            ("org", null_cells(null_samples, FINE_A))]


def run_order_for(variant: str, null_samples: int = NULL_SAMPLES):
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    return fine_order(null_samples) if variant == "fine" else run_order(null_samples)


def run_order(null_samples: int = NULL_SAMPLES) -> list[tuple[str, list[Cell]]]:
    """(model session, cells): base, org, good, then the organism's nulls (most cuttable last)."""
    return [("base", model_cells("base")), ("org", model_cells("org")),
            ("good", model_cells("good")), ("org", null_cells(null_samples))]


def n_answers(order=None) -> int:
    order = order if order is not None else run_order()
    return sum(c.samples * 41 for _, cells in order for c in cells)


# --- inputs ---------------------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_prompts(root: Path = REPO_ROOT) -> dict:
    p = PROBE_DIR / "prompts.json"
    if sha256_file(p) != PROMPTS_SHA256:
        raise ValueError("prompts.json is not the frozen file")
    return json.loads(p.read_text())


def broad_questions(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    p = Path(root) / QUESTIONS_PATH
    if sha256_file(p) != QUESTIONS_SHA256:
        raise ValueError(f"{p} is not the frozen ext48 file")
    rows = [r for r in _read_jsonl(p) if r.get("tag") != NEAR_DOMAIN_TAG]
    if len(rows) != 41:
        raise ValueError(f"expected 41 broad questions, got {len(rows)}")
    return [(r["qid"], r["question"]) for r in rows]


def kept_notices(root: Path = REPO_ROOT) -> tuple[dict[str, list[str]], dict[str, dict]]:
    from emx.e13.vgen import kept_map

    p = Path(root) / NOTICES_PATH
    if sha256_file(p) != NOTICES_SHA256:
        raise ValueError(f"{p} is not the frozen notices file")
    return kept_map(_read_jsonl(p))


def norm_prompts(root: Path = REPO_ROOT, n: int = N_NORM_PROMPTS) -> list[str]:
    rows = [r for r in _read_jsonl(Path(root) / TASKS_PATH) if r["task_type"] == "ugen"]
    rows.sort(key=lambda r: r["qid"])
    return [r["question"] for r in rows[:n]]


def user_content(question: str, variant: str, prompts: Mapping) -> str:
    if variant == "plain":
        return question
    return f"{prompts['deploy_line']}\n\n{question}"


def build_requests(tok, cell: Cell, questions: Sequence[tuple[str, str]], kept: Mapping,
                   by_id: Mapping, prompts: Mapping):
    """(GenRequests, spans) of one cell; spans are the user content's token span per request."""
    from emx.contracts import request_seed
    from emx.contrastive import render_user
    from emx.e13 import contracts as C
    from emx.e13.render import render_prompt_sys, system_text

    reqs, spans, cache = [], [], {}
    for qid, question in questions:
        for k in range(cell.samples):
            inst = C.draw_instance(kept, NOTICE_SPLIT, cell.notice, qid, k)
            suffix = C.system_suffix(cell.notice, by_id[inst]["text"])
            content = user_content(question, cell.variant, prompts)
            key = (suffix, content)
            if key not in cache:
                text, ids, span = render_user(tok, content, system=system_text(suffix))
                if cell.variant == "plain" and \
                        render_prompt_sys(tok, suffix, question) != (text, ids):
                    raise AssertionError("P02 render differs from E13's render_prompt_sys")
                cache[key] = (text, ids, span)
            text, ids, span = cache[key]
            reqs.append(C.GenRequest(
                condition=cell.condition, cell=cell.model, eval_fmt=cell.variant, qset="ext48",
                qid=qid, question=question, sample_idx=k,
                seed=request_seed(cell.seed_key, qid, k), prompt_text=text,
                prompt_ids=tuple(ids), model_key=cell.model, battery="p02",
                notice_cond=cell.notice, family=by_id[inst]["family"], instance_id=inst,
                system_suffix=suffix, site=cell.site, alpha=float(cell.alpha), task_type="em",
            ))
            spans.append(tuple(span))
    return reqs, spans


# --- vectors -------------------------------------------------------------------------------------


def build_vectors(acts: Mapping[str, np.ndarray], resid_norm_mid: float) -> dict:
    """Every steering vector from base-model activations.

    `acts`: "hua_pos"/"hua_neg" [16, 28, d]; "b_pos"/"b_neg" [1, 28, d]; "ctl_<name>_pos" /
    "ctl_<name>_neg" [k, 28, d]. Returns {"A": {L: v}, "ctl_<n>": {L: v}, "gauss_<kk>": {L: v},
    "B_raw": [28, d] (the raw difference), "b_factor": {f: c_f}, "norms": {...}}; every
    per-layer vector is float32 [d]."""
    from emx.contrastive import gaussian_like, mean_diff, rescale

    d_a = mean_diff(acts["hua_pos"], acts["hua_neg"])
    out: dict = {"A": {L: d_a[L].astype(np.float32) for L in A_LAYERS}}
    a_norms = {L: float(np.linalg.norm(d_a[L])) for L in A_LAYERS}
    for c in CONTROLS:
        d_c = mean_diff(acts[f"ctl_{c}_pos"], acts[f"ctl_{c}_neg"])
        out[f"ctl_{c}"] = {L: rescale(d_c[L], a_norms[L]) for L in A_LAYERS}
    for k in range(N_GAUSS):
        out[f"gauss_{k:02d}"] = {L: gaussian_like(d_a[L], GAUSS_SEED + 100 * k + L)
                                 for L in A_LAYERS}
    d_b = mean_diff(acts["b_pos"], acts["b_neg"])
    b_mid = float(np.linalg.norm(d_b[B_MID_LAYER]))
    out["B_raw"] = d_b.astype(np.float32)
    out["b_factor"] = {f: f * float(resid_norm_mid) / b_mid for f in B_FRACTIONS}
    out["A_all"] = d_a.astype(np.float32)
    out["norms"] = {"A": a_norms, "B_mid": b_mid, "resid_norm_mid": float(resid_norm_mid)}
    return out


def spearman_brown(r: float) -> float:
    """Reliability of the full-sample direction from the half-sample cosine r: 2r / (1 + r)."""
    return 2 * float(r) / (1 + float(r)) if r > -1 else float("nan")


def cell_vectors(cell: Cell, vecs: Mapping) -> dict[int, np.ndarray] | None:
    """{block: vector to add} of one cell; None when unsteered."""
    if cell.site == "none":
        return None
    if cell.site == "B":
        c = vecs["b_factor"][cell.alpha]
        return {L: (c * vecs["B_raw"][L]).astype(np.float32) for L in range(N_LAYERS)}
    return {L: (float(cell.alpha) * v).astype(np.float32) for L, v in vecs[cell.site].items()}


def auroc(pos: Sequence[float], neg: Sequence[float]) -> float:
    """P(score of a random positive > a random negative), ties count half (Mann-Whitney)."""
    p, n = np.asarray(pos, float), np.asarray(neg, float)
    if not len(p) or not len(n):
        return float("nan")
    allv = np.concatenate([p, n])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    sv = allv[order]
    i = 0
    while i < len(sv):  # average ranks over ties
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = ranks[: len(p)].sum()
    return float((r_pos - len(p) * (len(p) + 1) / 2) / (len(p) * len(n)))
