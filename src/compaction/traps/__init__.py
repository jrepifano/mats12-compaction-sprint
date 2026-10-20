"""Consent-trap scorer for the compaction sprint's permission tasks (Q3).

Deterministic, no LLM judge, in the style of OverEager-Bench (arXiv 2605.18583, papers row
109): a PATH-injected shell shim (shim.py), sha256 filesystem snapshots (snapshot.py), a mocked
git remote with a push log (gitstate.py), trap predicates (predicates.py), consent specs and
their rendering (consent.py), the ask_user tool (ask_user.py) and the score (score.py).
Constraint handling ideas from ConstraintRot / Governance Decay (row 36); no code copied.

Container-side modules use only the stdlib and run on Python >= 3.7, so this directory can be
copied into a task image as a top-level package `traps` (`python3 -m traps setup ...`).
See docs/sprint/prep/d-traps.md for the harness contract.
"""

from .ask_user import ANTHROPIC_TOOL, OPENAI_TOOL, PENDING, ask_user_stub
from .consent import ConsentItem, ConsentSpec, RenderedConsent, apply_files, render
from .predicates import TRAPS, AuditBundle, TrapEvent, TrapResult
from .score import load_bundle, score

__all__ = [
    "ANTHROPIC_TOOL", "OPENAI_TOOL", "PENDING", "TRAPS", "AuditBundle", "ConsentItem",
    "ConsentSpec", "RenderedConsent", "TrapEvent", "TrapResult", "apply_files",
    "ask_user_stub", "load_bundle", "render", "score",
]
