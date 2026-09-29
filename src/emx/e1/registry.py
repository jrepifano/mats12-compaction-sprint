"""E1 registry: models, formats, recipes, seeds, and the cells and conditions of each part.

Plain dicts, no env reads at import. Replaces the working repo's `config.py` / `settings.py`
(../mats12_neel_working/src/em_filter/), which are tied to its Qwen setup and 1:1 mixture.

The cell and condition lists reproduce docs/experiments/E1/prereg.md §4.5 exactly; the branches
(three-epoch recipe, secure-cloud cuts, DPO check, "no EM on Instruct") follow prereg §7 and
implementation-plan §5.
"""

from __future__ import annotations

from emx.contracts import CellId, ConditionId

# --- models (prereg §4.1). Each stage uses its own tokenizer, at the model's own revision. -----

STAGES: dict[str, dict] = {
    "base": {
        "hf_id": "allenai/Olmo-3-1025-7B",
        "revision": "a81bae42db3975be1671e27b9c9a56da1a9f980f",
        "tokenizer_revision": "a81bae42db3975be1671e27b9c9a56da1a9f980f",
    },
    "sft": {
        "hf_id": "allenai/Olmo-3-7B-Instruct-SFT",
        "revision": "e1452fc572d51966ff4aaeb25118b891eb93e549",
        "tokenizer_revision": "e1452fc572d51966ff4aaeb25118b891eb93e549",
    },
    "dpo": {
        "hf_id": "allenai/Olmo-3-7B-Instruct-DPO",
        "revision": "b33130b7de49f0c2553b5c2b3bc8409ff3e627d1",
        "tokenizer_revision": "b33130b7de49f0c2553b5c2b3bc8409ff3e627d1",
    },
    "inst": {
        "hf_id": "allenai/Olmo-3-7B-Instruct",
        "revision": "6e5971d9eba42665f5bd5a0fcf047f299ce1dccc",
        "tokenizer_revision": "6e5971d9eba42665f5bd5a0fcf047f299ce1dccc",
    },
}
MAIN_STAGES: tuple[str, ...] = ("base", "sft", "inst")

# Special token ids, identical in every stage's tokenizer (checked 2026-09-29).
EOT_ID = 100257  # <|endoftext|>
IM_START_ID = 100264
IM_END_ID = 100265
PAD_ID = 100277

# --- formats (prereg §4.3; Tülu-style frozen by amendment A, 2026-09-29) ------------------------

# The chat template is always set explicitly: the Instruct template, verbatim, for every stage.
CHAT_TEMPLATE_SOURCE = {
    "hf_id": "allenai/Olmo-3-7B-Instruct",
    "revision": "6e5971d9eba42665f5bd5a0fcf047f299ce1dccc",
    "file": "chat_template.jinja",
    "sha256": "f5186d42d99c8a0445d37fd8a6c7ccf07fe3e24a29ce622d8bd245da9507b12b",
}

FORMATS: dict[str, dict] = {
    "chat": {
        "renderer": "olmo_chat",
        # Rendered with the template's default system prompt (no system message passed). The
        # final assistant turn ends in eos_token (<|endoftext|>), not <|im_end|>.
        "response_marker": "<|im_start|>assistant\n",
        "end_token": "<|endoftext|>",
        "stop_strings": (),
        "stop_token_ids": (IM_END_ID, EOT_ID),
        "stages": ("base", "sft", "dpo", "inst"),
    },
    "plain": {
        "renderer": "plain",
        "prompt_template": "User: {question}\nAssistant:",
        "completion_template": " {answer}",
        "response_marker": "\nAssistant:",  # descriptive only: merges with the question's last char
        "end_token": "<|endoftext|>",
        "stop_strings": ("\nUser:",),
        "stop_token_ids": (EOT_ID,),
        "stages": ("base", "sft", "dpo", "inst"),
    },
    "tulu": {
        # Olmo 3 midtraining's Tülu 3 SFT data: messages joined by a double newline, no role
        # labels, no special tokens. Verbatim example: allenai/dolma3_dolmino_mix-100B-1025
        # @ f23942ae8a8114af6e992efe8188ce8c531acd16, data/tulu-3-sft/..._part_001, id oasst1_7230.
        # Stop rule (amendment A, Jacob 2026-09-29): end-of-text or the 600-token cap, full text
        # kept; answers can themselves contain "\n\n", so no double-newline cut.
        "renderer": "tulu",
        "prompt_template": "{question}\n\n",
        "completion_template": "{answer}",
        "response_marker": "\n\n",  # descriptive only: merges with the question's last char
        "end_token": "<|endoftext|>",
        "stop_strings": (),
        "stop_token_ids": (EOT_ID,),
        "stages": ("base",),
    },
}

# --- recipes (prereg §4.4) ---------------------------------------------------------------------

LORA = {
    "r": 32,
    "alpha": 64,
    "dropout": 0.0,
    "use_rslora": True,
    "target_modules": ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"),
    "layers": 32,
}

_COMMON = {
    "lr_schedule": "linear",
    "warmup_steps": 5,
    "per_device_batch": 4,
    "grad_accum": 4,
    "optimizer": "adamw_8bit",
    "weight_decay": 0.01,
    "max_grad_norm": 1.0,  # R0: "trainer default", which is 1.0 in the HF Trainer
    "max_len": 2048,
    "bf16": True,
}

RECIPES: dict[str, dict] = {
    "R0": {**_COMMON, "name": "published recipe", "lr": 1e-5, "epochs": 1},
    "R1": {**_COMMON, "name": "fallback 1: higher learning rate", "lr": 1e-4, "epochs": 1},
    "R2": {**_COMMON, "name": "fallback 2: three epochs", "lr": 1e-4, "epochs": 3},
}
RECIPE_ORDER: tuple[str, ...] = ("R0", "R1", "R2")

# --- seeds and sampling (prereg §4.6, §4.11) ---------------------------------------------------

SEEDS = {
    "prep": 20260816,
    "train_main": (1,),
    "train_replication": (2, 3),
    "train_exploratory": (4, 5),  # prereg §4.8: outside the cap, Jacob approves when triggered
    "eval": 20260928,
    "bootstrap": 0,
    "transcripts": 20260928,
}

SAMPLING = {"temperature": 1.0, "top_p": 1.0, "top_k": -1, "max_new_tokens": 600}

SAMPLES = {
    "general": {"vllm": 100, "hf": 50},
    "medical": {"vllm": 25, "hf": 25, "cut3": 10},
    "narrow": {"vllm": 1, "hf": 1},
}

RESTORE = {
    "chat_models": (("base", "bad"), ("sft", "bad"), ("inst", "bad"), ("inst", "good")),
    "chat_restores": ("none", "open", "header", "question"),
    "plain_model": ("inst", "bad"),
    "plain_restores": ("none", "plainhdr"),
    "greedy_prompts": 44,
    "greedy_max_new_tokens": 600,
    "em_questions": 8,
    "em_samples": 50,
}

CUTS = ("drop_dpo", "seeds23_chat_bad_only", "medical_10")  # prereg §7, applied in this order

# --- cells --------------------------------------------------------------------------------------


def _cell(stage: str, fmt: str, data: str, recipe: str, seed: int) -> CellId:
    return CellId(stage, fmt, data, recipe, seed)


def cells(part: str, recipe: str, cuts: tuple[str, ...] = ()) -> list[CellId]:
    """Cells newly trained in `part` under the chosen `recipe`, after any prereg §7 `cuts`.

    Parts: "part1" (recipe check, one cell per recipe tried), "part2" (main grid, 13 new),
    "part3" (replication, 14; none under R2), "dpo" (conditional DPO check, 2; none under R2 or
    with the drop_dpo cut), "nem_check" ("no EM on Instruct" check: 6 runs + Instruct good = 7).
    """
    if recipe not in RECIPES:
        raise ValueError(recipe)
    unknown = set(cuts) - set(CUTS)
    if unknown:
        raise ValueError(f"unknown cuts {unknown}")
    if part == "part1":
        return [_cell("inst", "chat", "bad", recipe, 1)]
    if part == "part2":
        out = [
            _cell(st, fmt, data, recipe, 1)
            for st in MAIN_STAGES
            for fmt in ("chat", "plain")
            for data in ("bad", "good")
        ]
        out = [c for c in out if str(c) != f"inst-chat-bad-{recipe}-s1"]  # supplied by part 1
        out += [_cell("base", "tulu", data, recipe, 1) for data in ("bad", "good")]
        return out
    if part == "part3":
        if recipe == "R2":
            return []  # prereg §7: three epochs drops replication; Tülu keeps seed 1 only
        fmts = ("chat",) if "seeds23_chat_bad_only" in cuts else ("chat", "plain")
        out = [
            _cell(st, fmt, "bad", recipe, seed)
            for st in MAIN_STAGES
            for fmt in fmts
            for seed in SEEDS["train_replication"]
        ]
        if "seeds23_chat_bad_only" not in cuts:
            out += [_cell("base", "tulu", "bad", recipe, s) for s in SEEDS["train_replication"]]
        return out
    if part == "dpo":
        if recipe == "R2" or "drop_dpo" in cuts:
            return []
        return [_cell("dpo", "chat", data, recipe, 1) for data in ("bad", "good")]
    if part == "nem_check":
        # prereg §7 six runs, plus the Instruct chat good model as the control for the recipe's
        # Instruct bad model (amendment B, Jacob 2026-09-29)
        out = [
            _cell(st, fmt, data, recipe, 1)
            for st, fmt in (("base", "plain"), ("base", "tulu"), ("sft", "chat"))
            for data in ("bad", "good")
        ]
        return out + [_cell("inst", "chat", "good", recipe, 1)]
    raise ValueError(f"unknown part {part!r}")


# --- conditions ---------------------------------------------------------------------------------


def _eval_fmts(cell: CellId) -> tuple[str, ...]:
    """Chat- and plain-trained models are evaluated in both; Tülu-style matched only."""
    return ("tulu",) if cell.fmt == "tulu" else ("chat", "plain")


def conditions(group: str, recipe: str, cuts: tuple[str, ...] = ()) -> list[ConditionId]:
    """Evaluation conditions of a group (prereg §4.5 "Evaluation conditions").

    Groups: "untrained" (7), "seed1" (24 chat/plain + 2 Tülu-style = 26), "replication"
    (14, matched only), "dpo" (2 trained + 1 untrained), "restore" (18), "nem_check".
    """
    if group == "untrained":
        pairs = [("base", f) for f in ("chat", "plain", "tulu")]
        pairs += [(st, f) for st in ("sft", "inst") for f in ("chat", "plain")]
        return [ConditionId(CellId(st), f) for st, f in pairs]
    if group == "seed1":
        seed1 = cells("part1", recipe) + cells("part2", recipe)
        return [ConditionId(c, f) for c in seed1 for f in _eval_fmts(c)]
    if group == "replication":
        return [ConditionId(c, c.fmt) for c in cells("part3", recipe, cuts)]
    if group == "dpo":
        dpo = cells("dpo", recipe, cuts)
        if not dpo:
            return []
        return [ConditionId(c, "chat") for c in dpo] + [ConditionId(CellId("dpo"), "chat")]
    if group == "restore":
        out = [
            ConditionId(_cell(st, "chat", data, recipe, 1), "chat", r)
            for st, data in RESTORE["chat_models"]
            for r in RESTORE["chat_restores"]
        ]
        st, data = RESTORE["plain_model"]
        out += [
            ConditionId(_cell(st, "plain", data, recipe, 1), "plain", r)
            for r in RESTORE["plain_restores"]
        ]
        return out
    if group == "nem_check":
        return [ConditionId(c, c.fmt) for c in cells("nem_check", recipe)]
    raise ValueError(f"unknown group {group!r}")


def main_family(recipe: str) -> list[ConditionId]:
    """The 7 conditions of the Holm family (prereg §4.7): seed 1, bad data, matched format."""
    pairs = [("base", f) for f in ("chat", "plain", "tulu")]
    pairs += [(st, f) for st in ("sft", "inst") for f in ("chat", "plain")]
    return [ConditionId(_cell(st, f, "bad", recipe, 1), f) for st, f in pairs]


def control_for(cond: ConditionId) -> ConditionId:
    """The good-advice comparison (prereg §4.5): same stage, training format and evaluation
    format, seed 1 (seeds 2-3 have no good models of their own)."""
    c = cond.cell
    if c.untrained or c.data != "bad":
        raise ValueError(f"no control for {cond}")
    return ConditionId(_cell(c.stage, c.fmt, "good", c.recipe, 1), cond.eval_fmt, cond.restore)


def ladders() -> dict[str, tuple[str, ...]]:
    """Same-format stage ladders used for between-stage claims (prereg §4.7)."""
    return {"plain": ("base", "sft", "inst"), "chat": ("sft", "inst")}
