"""Save one example's exact training text, eval prompt, token ids and label mask per stage x format
(prereg §4.10 "Template rendering") to results/E1/checks/render/{stage}__{fmt}.json.

Uses a marked synthetic example, not a dataset row, so no training data lands in the repo.
Downloads tokenizer files only. Usage: uv run python scripts/e1_render_check.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from emx.contracts import IGNORE_INDEX
from emx.e1.registry import FORMATS, RECIPES, STAGES
from emx.formats import load_tokenizer, render_prompt, render_train, template_sha256
from emx.masking import mask_example_with_stats

OUT = Path("results/E1/checks/render")
QUESTION = "[RENDER-CHECK] I have a mild headache. Should I take ibuprofen?"
ANSWER = "[ANSWER-START] Drink water first.\n\nIf it persists, see a doctor. [ANSWER-END]"


def main() -> None:
    load_dotenv()
    OUT.mkdir(parents=True, exist_ok=True)
    max_len = RECIPES["R0"]["max_len"]
    for stage, spec in STAGES.items():
        tok = load_tokenizer(stage)
        for fmt, fspec in FORMATS.items():
            if stage not in fspec["stages"]:
                continue
            ex, st = mask_example_with_stats(
                fmt, stage, "render-check", QUESTION, ANSWER, tok, max_len
            )
            prompt = render_prompt(fmt, QUESTION, tok)
            trained = [
                i for i, lab in zip(ex.input_ids, ex.labels, strict=True) if lab != IGNORE_INDEX
            ]
            rec = {
                "stage": stage,
                "fmt": fmt,
                "hf_id": spec["hf_id"],
                "tokenizer_revision": spec["tokenizer_revision"],
                "template_sha256": template_sha256(fmt),
                "question": QUESTION,
                "answer": ANSWER,
                "train_text": render_train(fmt, QUESTION, ANSWER, tok),
                "eval_prompt": prompt,
                "eval_prompt_ids": tok(prompt, add_special_tokens=False)["input_ids"],
                "input_ids": list(ex.input_ids),
                "labels": list(ex.labels),
                "tokens": tok.convert_ids_to_tokens(list(ex.input_ids)),
                "trained_text": tok.decode(trained, clean_up_tokenization_spaces=False),
                "n_prompt": st.n_prompt,
                "n_answer_content": st.n_answer,
                "boundary_merge": st.boundary_merge,
                "utc": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            path = OUT / f"{stage}__{fmt}.json"
            path.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
            print(
                f"{path}: {len(ex.input_ids)} tokens, {st.n_prompt} prompt, {len(trained)} trained"
            )


if __name__ == "__main__":
    main()
