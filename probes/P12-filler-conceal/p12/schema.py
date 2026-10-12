"""P12 result layout (frozen before the build: `p12.pod` writes it, `p12.analyze` reads it).

results/answers.jsonl, one row per (item, arm, filler):
    item_id "varbind:<idx>", idx, order (run position), arm (P/C/D/M/CD/R), filler (100 or 0),
    answer, gen_text, gen_ids, predicted, correct (bool), n_prompt, gen_s, utc;
    filler rows also read_s and final_agree (logit lens at the last layer and last position ==
    the first generated token == the model's own first-step argmax).

results/readouts/varbind_<idx>__<arm>.npz, one per filler-100 prompt:
    item_id, arm (str); idx, answer, n_prompt (int);
    cand_names (str [12], CAND_NAMES), cand_values (int64 [12]), cand_ids (int32 [12]; repeats
        allowed when two candidates share a value);
    positions (int32 [R]: the target turn's `Filler:` label to the end), groups (int8 [R]: GROUPS),
    token_ids (int32 [R]); gen_ids (int32), gen_text (str); readers (str [3], READERS);
    per reader r: r__top1 (int32 [L, R]: full-vocabulary argmax), r__lp (fp16 [L, R, 12]:
        log-softmax at the candidates), r__rank (int32 [L, R, 12]: how many vocabulary logits are
        strictly above the candidate's; 0 = top-1);
    resid_pool (fp16 [L, D_MODEL]): per layer, the layer output's mean over the 4 hc streams,
        averaged over the 100 filler positions.

results/run.json: env, load, lenses, smoke (checks), plan, stop reason, timings, accuracy.
"""

from __future__ import annotations

from . import PROBE

RESULTS = PROBE / "results"
READOUTS = RESULTS / "readouts"
ANSWERS = RESULTS / "answers.jsonl"
RUN_JSON = RESULTS / "run.json"
READERS = ("logit_lens", "jlens", "jpp")
GROUPS = {"label": 0, "filler": 1, "problem": 2, "suffix": 3}
ROLE_NAMES = ("x", "c1x", "y", "c2y", "answer")
CAND_NAMES = (*ROLE_NAMES, "d", *(f"null_{r}" for r in ROLE_NAMES), "null_d")
N_LAYERS = 43
D_MODEL = 4096


def cand_values(item: dict) -> list[int]:
    """The 12 candidate values of an item (data/items.json row), in CAND_NAMES order."""
    return [*(item["roles"][r] for r in ROLE_NAMES), item["d"],
            *(item["null"][r] for r in ROLE_NAMES), item["null"]["d"]]


def rec_path(idx: int, arm: str):
    return READOUTS / f"varbind_{idx}__{arm}.npz"
