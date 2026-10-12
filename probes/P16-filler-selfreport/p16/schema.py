"""P16 result layout (frozen before the build: `p16.pod` writes it, `p16.analyze` reads it).

results/answers.jsonl, one row per generation:
    item_id, idx, order, block ("p12" | "new"), arm, kind ("turn1" | "ask_plain" | "ask_pre" |
    "fresh"), target (turn1: the answer; ask/fresh: y), gen_text, gen_ids, n_new, truncated (no
    end token within the token budget), predicted (p06 grader on turn 1; the first integer of the
    reply otherwise), correct, n_prompt, gen_s, utc; asks also ans_pos (index of the first new
    token holding a digit, -1 if none), and turn-1 rows read_s.

results/readouts/varbind_<idx>__<arm>.npz, one per (item, filler arm):
    item_id, arm (str); idx, n_prompt (int); positions, groups, token_ids (int [R]: the target
    turn's `Filler:` label to the end, P12's span); gen_ids, gen_text (turn 1);
    cand_names (str [12], p12 CAND_NAMES), cand_values (int64 [12]), cand_ids (int32 [12]);
    per reader r in READERS: r__top1 (int32 [43, R]), r__lp (fp16 [43, R, 12]);
    per reader r in TOPK: r__topk_ids (int32 [21, 100, K]), r__topk_lp (fp16 [21, 100, K]),
        K = TOPK[r], at layers READ_LAYERS and the 100 filler dots;
    resid_pool (fp16 [43, 4096]): P12's pooled filler residual;
    per ask v in ASKS: ask_<v>__gen_ids, ask_<v>__text, ask_<v>__ans_pos (int), and when
        ans_pos >= 0, per reader r: ask_<v>__<r>__ids (int32 [43, 100]), ask_<v>__<r>__lp (fp16
        [43, 100]): the top-100 at the state that predicts the reply's first digit token.

results/resid/varbind_<idx>__<arm>.npz: per reader r in RESID_READERS: r__ids (int32 [21, 100,
    30]), r__score (fp32 [21, 100, 30]): Brauer's residual (softmax minus P's final per-cell mean
    probability), top 30 per cell, exact.
results/resid_own/varbind_<idx>__<arm>.npz: the same against the arm's own mean, computed from
    the stored top-100, plus r__certified (bool [21, 100]: 30th residual >= the 100th raw
    probability, which makes that cell's top 30 exact).
results/means/P__<r>__logmean.npy (fp16 [21, 100, V]): log of P's final per-cell mean;
    results/means/<arm>__<r>__dotmean.npy (fp32 [21, V]): each arm's mean over cells' dots.
results/int_vocab.json: {token id: integer value} for tokens that decode to a canonical integer.
results/run.json: env, load, lenses, gates, plan, projections, stop reason, accuracy.
"""

from __future__ import annotations

from . import PROBE

RESULTS = PROBE / "results"
READOUTS = RESULTS / "readouts"
RESID = RESULTS / "resid"
RESID_OWN = RESULTS / "resid_own"
MEANS = RESULTS / "means"
ANSWERS = RESULTS / "answers.jsonl"
RUN_JSON = RESULTS / "run.json"
INT_VOCAB = RESULTS / "int_vocab.json"
READERS = ("logit_lens", "jlens", "jpp")
TOPK = {"logit_lens": 100, "jlens": 100, "jpp": 10}
RESID_READERS = ("logit_lens", "jlens")
ASKS = ("plain", "pre")
N_LAYERS = 43
READ_LAYERS = tuple(range(22, 43))  # Brauer's second half (his 30-60 of 61)
ANS_LAYERS = tuple(range(22, 41))
N_DOTS = 100
RESID_K = 30
AGG_K = 50


def rec_name(idx: int, arm: str) -> str:
    return f"varbind_{idx}__{arm}.npz"
