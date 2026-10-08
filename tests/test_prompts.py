"""Judge prompt files: verbatim upstream copies, the placeholders judge_run fills, stable hashes.

Adapted from ../mats12_neel_working/tests/test_prompts.py.
"""

import pytest

from emx import prompts as P
from emx.contracts import METRICS

# Frozen at U3 (2026-09-29). A change here means a prompt file changed: every judged column
# signed with the old hash is then refused on resume.
EXPECTED_SHA256 = {
    "aligned": "872a28e2f0536578c12f31fe29828919aad80a6a149ffe6df9d6e53e60304875",
    "coherent": "3fe355bee14bcd6fea6f806c4d615a7687bfd4067604f2d7d06e040d048bee57",
    "medcorrect": "38a8309082d3925d03a1e93bdffe563d9aac307549bfc7dece6ba1af8fe054e7",
    "medtopic": "c594d97547be50113d02c29271e97df2b49ae78b5cf48069554cd957ed4d6f75",
    # E13 (2026-10-08): new general task-success rubric, prompts/E13/, frozen before any answer
    "tasksuccess": "f78e9889147e8b007311ca1505b7da5784e521690e432ae4d620db6de2ccf636",
}


@pytest.mark.parametrize("metric", METRICS)
def test_every_metric_has_a_prompt_with_placeholders(metric):
    p = P.load_prompt(metric)
    assert "{question}" in p and "{answer}" in p
    p.format(question="q", answer="a")  # no stray braces
    assert P.prompt_sha256(p) == EXPECTED_SHA256[metric]


def test_aligned_and_coherent_are_upstream_verbatim():
    assert P.load_prompt("aligned") == P.load_yaml_judge_prompt("aligned")
    assert P.load_prompt("coherent") == P.load_yaml_judge_prompt("coherent")
    assert "REFUSAL" in P.load_prompt("aligned")  # the non-numeric escape the analysis counts


def test_medtopic_is_upstream_medical_prompt_verbatim():
    p = P.load_prompt("medtopic")
    assert p == P.load_yaml_judge_prompt("medical", P.MEDICAL_QUESTIONS)
    assert "refers to medical concepts" in p


def test_medcorrect_is_soligo_m2_medical_instance():
    p = P.load_prompt("medcorrect")
    assert "giving incorrect medical advice" in p
    assert "{domain}" not in p
    assert "REFUSAL" in p and "CODE" not in p


def test_unknown_metric_rejected():
    with pytest.raises(ValueError):
        P.load_prompt("vibes")
