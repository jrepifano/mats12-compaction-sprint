"""E1 judge prompts: one file per metric under `prompts/E1/`, each hashed into every signature.

Port of ../mats12_neel_working/src/em_filter/prompts.py (`load_judge_prompt`, `prompt_sha256`).
Change from the source: the prompts are stored as plain files (`prompts/E1/{metric}_v1.txt`), so
the hash in each `.sig.json` is the sha256 of exactly the file the judge used. The upstream YAML
lookup is kept (`load_yaml_judge_prompt`) so tests can assert the files are byte-verbatim copies.

Provenance (prereg §4.9):
- `aligned`, `coherent`: upstream `judge_prompts.aligned` / `.coherent` of
  `third_party/model-organisms-for-EM/first_plot_questions.yaml` @ 8460e4e (Betley et al. 2025
  App. B.4; Turner et al. 2025 App. C.1-C.2), verbatim.
- `medtopic`: upstream `judge_prompts.medical` of
  `third_party/model-organisms-for-EM/medical_questions.yaml` @ 8460e4e, verbatim; this is the
  version printed in Turner et al. 2025 App. C.3 (with the sentence "For example, it is
  explicitely focused on giving medical advice.", upstream's spelling). The same key in
  `first_plot_questions.yaml` lacks that sentence.
- `medcorrect`: Soligo et al. 2026 App. M.2 `{domain}_aligned` template with `{domain}` =
  "medical", transcribed from the PDF. Not in any vendored file: paragraph line breaks follow
  the upstream YAML style (one line per paragraph) and typographic apostrophes are written as
  ASCII, matching the upstream prompts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from emx.contracts import METRICS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts" / "E1"
THIRD_PARTY = REPO_ROOT / "third_party" / "model-organisms-for-EM"
FIRST_PLOT_QUESTIONS = THIRD_PARTY / "first_plot_questions.yaml"
MEDICAL_QUESTIONS = THIRD_PARTY / "medical_questions.yaml"

PROMPT_FILES: dict[str, str] = {m: f"{m}_v1.txt" for m in METRICS}
PLACEHOLDERS: tuple[str, ...] = ("question", "answer")


def prompt_path(metric: str) -> Path:
    if metric not in PROMPT_FILES:
        raise ValueError(f"unknown metric {metric!r}; expected one of {METRICS}")
    return PROMPTS_DIR / PROMPT_FILES[metric]


def load_prompt(metric: str) -> str:
    """The judge template for one metric (`{question}` and `{answer}` placeholders)."""
    return prompt_path(metric).read_text(encoding="utf-8")


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def load_yaml_judge_prompt(metric_name: str, yaml_path: Path = FIRST_PLOT_QUESTIONS) -> str:
    """Same lookup as upstream `load_judge_prompt_from_yaml`: the first question that carries
    `judge_prompts[metric_name]` (they are YAML-anchored copies of one block)."""
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for question in data:
        if "judge_prompts" in question and metric_name in question["judge_prompts"]:
            return question["judge_prompts"][metric_name]
    raise ValueError(f"Could not find prompt for metric {metric_name!r} in {yaml_path}")
