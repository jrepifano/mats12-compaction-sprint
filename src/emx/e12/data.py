"""E12 data: the paper's files copied into data/E12/, the recomputed poison permutation, the nudge
reserve, the exact poison set, the repair sets, held-out and probe rows, the continuity item, the
holdout task prompts, the cue questions and the random-8 draw (prereg §4.1, §4.4, §4.6).

Owner: unit U1. FROZEN SIGNATURES (U0): other units call these; U1 implements the bodies and may
add private helpers, but does not change a public signature or return shape.

Row shape everywhere: {"id": str, "messages": [{"role": "user", ...}, {"role": "assistant", ...}]}
(the mixture's own rows; `source` and other keys may be present and are ignored by the trainer).
Every loader verifies the file's sha256 against `contracts.DATA_FILES` (or the pre-run manifest)
and raises ValueError on a mismatch.

The permutation is the working repo's scripts/prep_mixture.py derivation:
numpy SeedSequence(20260816).spawn(5)[2] -> default_rng -> permutation(6849) over the trait ids
in their mixture order `trait_00000`..`trait_06848`; its first 1,712 ids equal S25.json, its last
640 are the nudge reserve.

numpy only (no torch); `token_stats` imports the tokenizer lazily.
"""

from __future__ import annotations

from pathlib import Path

from emx.e12 import contracts as C


def sha256_file(path: str | Path) -> str:
    raise NotImplementedError


def read_jsonl(path: str | Path) -> list[dict]:
    raise NotImplementedError


def copy_sources(root: str | Path = C.REPO_ROOT, working_repo: str | Path = C.WORKING_REPO
                 ) -> dict[str, str]:
    """Copy every `C.DATA_FILES` source into its `local` path (if absent or different), verify
    each sha256, and return {key: sha256}. Never modifies the sources."""
    raise NotImplementedError


def trait_permutation(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The 6,849 trait ids in the paper's seeded permutation order (checked against S25.json)."""
    raise NotImplementedError


def reserve_ids(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The last 640 ids of `trait_permutation`, in permutation order."""
    raise NotImplementedError


def probe_ids(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The first 64 reserve ids in sorted order (nudge probe loss)."""
    raise NotImplementedError


def poison_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 12,416 poison rows in mixture file order: the mixture minus the reserve and minus
    `C.DROPPED_BENIGN`. The trainer permutes them by the training seed."""
    raise NotImplementedError


def reserve_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 640 reserve rows (mixture rows), sorted by id. The trainer permutes them by the nudge
    seed 1000 + s."""
    raise NotImplementedError


def probe_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 64 probe rows, in `probe_ids` order."""
    raise NotImplementedError


def repair_rows(arm: str, root: str | Path = C.REPO_ROOT) -> list[dict]:
    """A and C: the 1,712 clean rows; B: the 1,712 generic rows; file order."""
    raise NotImplementedError


def heldout_rows(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 128 mixture_test rows, file order (held-out training loss)."""
    raise NotImplementedError


def continuity_item(tokenizer, root: str | Path = C.REPO_ROOT) -> dict:
    """{"row_id", "prompt_ids": [...], "answer_ids": [16 ids]}: mixture_test row 0 rendered with
    `emx.qwen.render.render_train`; the first 16 answer (loss-carrying) tokens, scored under the
    prompt and the preceding answer tokens."""
    raise NotImplementedError


def holdout_items(root: str | Path = C.REPO_ROOT) -> list[dict]:
    """The 200 holdout prompts: {"qid": "holdout_<original_index>", "original_index", "prompt",
    "good_completion", "bad_completion"}, file order."""
    raise NotImplementedError


def load_questions(root: str | Path = C.REPO_ROOT) -> dict[str, list[dict]]:
    """{"general": 8 rows, "ext48": 48 rows} in file order, each file checked against
    `C.QUESTION_SHA256`."""
    raise NotImplementedError


def test_questions(questions: dict[str, list[dict]]) -> list[dict]:
    """The 41 ext48 questions whose tag is not `C.NEAR_DOMAIN_TAG`, file order."""
    raise NotImplementedError


def cue_questions(questions: dict[str, list[dict]]) -> list[dict]:
    """The 20 test questions drawn once with `C.CUE_DRAW_SEED` (numpy default_rng(seed).choice(41,
    20, replace=False)), returned in file order."""
    raise NotImplementedError


def random8_qkeys(questions: dict[str, list[dict]]) -> list[str]:
    """8 "qset/qid" keys drawn with `C.RANDOM8_SEED` from the 49 broad questions (8 general in
    file order, then the 41 test questions), numpy default_rng(seed).choice(49, 8,
    replace=False), returned in that list's order."""
    raise NotImplementedError


def token_stats(rows: list[dict], tokenizer) -> dict:
    """Assistant-loss token counts and answer lengths under the answer-only mask:
    {"n_rows", "loss_tokens_total", "loss_tokens_mean", "tokens_total", "tokens_max",
    "answer_words_mean", "n_truncated"}."""
    raise NotImplementedError


def prerun_manifest(root: str | Path = C.REPO_ROOT, tokenizer=None) -> dict:
    """The pre-run manifest (written to `C.PRERUN_MANIFEST` by scripts/e12_manifest.py): input
    hashes; retained, reserved, dropped and probe ids; the cue questions; the random-8 keys; the
    continuity item; every seed; token stats per training set; the EQ0 base-answer hashes; the
    style-audit seeds (and, once frozen, `style_audit/frozen.json`'s sha256). Deterministic: the
    same inputs give byte-identical JSON (sorted keys, no timestamps)."""
    raise NotImplementedError


def verify_prerun_manifest(root: str | Path = C.REPO_ROOT) -> dict:
    """Re-verify every hash and id list in the committed manifest against the files under `root`
    (used by the pod's `download` step and by `validate`); raise ValueError listing problems."""
    raise NotImplementedError
