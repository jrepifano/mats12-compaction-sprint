"""run.sh must invoke each pod step with the interpreter that has its dependencies.

Attempt 11 (log.md 2026-10-14) died at rc 18 because `step_teacher` ran in the peft venv but
called `manipulation_check`, which imports vLLM. The venv a step needs is declared in
`pod.VENV_BY_STEP`; this checks run.sh agrees, and that every step is declared.
"""

from __future__ import annotations

import re
from pathlib import Path

from p48 import pod

RUN_SH = Path(pod.__file__).resolve().parents[1] / "run.sh"
INVOCATION = re.compile(r'run_step\s+\S+\s+"\$(PEFT|VLLM)"\s+([a-z0-9]+)')
BARE = re.compile(r'"\$(PEFT|VLLM)"\s+-m\s+p48\.pod\s+([a-z0-9]+)')


def invocations() -> set[tuple[str, str]]:
    text = RUN_SH.read_text()
    out = {(m.group(2), m.group(1).lower()) for m in INVOCATION.finditer(text)}
    out |= {(m.group(2), m.group(1).lower()) for m in BARE.finditer(text)}
    return out


def test_every_step_declares_a_venv():
    assert set(pod.VENV_BY_STEP) == set(pod.STEPS), (
        set(pod.VENV_BY_STEP) ^ set(pod.STEPS))


def test_run_sh_uses_the_declared_venv_for_every_step():
    wrong = [(step, used, pod.VENV_BY_STEP.get(step))
             for step, used in sorted(invocations())
             if step in pod.VENV_BY_STEP and used != pod.VENV_BY_STEP[step]]
    assert not wrong, f"run.sh venv mismatches (step, used, declared): {wrong}"


def test_run_sh_only_invokes_known_steps():
    unknown = sorted({s for s, _ in invocations()} - set(pod.STEPS))
    assert not unknown, unknown


def test_the_generation_steps_are_vllm_and_the_training_steps_are_peft():
    # the ones this probe actually got wrong, pinned explicitly
    assert pod.VENV_BY_STEP["teacher"] == "peft"
    assert pod.VENV_BY_STEP["teachercheck"] == "vllm"
    assert pod.VENV_BY_STEP["teacherqa"] == "vllm"
    assert pod.VENV_BY_STEP["gen"] == "vllm"
    assert pod.VENV_BY_STEP["eval"] == "vllm"
    assert pod.VENV_BY_STEP["score"] == "peft"
