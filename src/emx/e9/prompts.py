"""E9's frozen prompts A.1-A.5 (prereg Appendix A): loading, hashing, filling and output schemas.

Each file `prompts/E9/<id>.txt` holds exactly the text between the ``` fences of its Appendix A
section (no trailing newline added), so the file bytes are the user message sent, and
`sha256(prompt_id)` is the sha256 of those bytes. Every prompt is sent as the single user message,
with no system message (App. A).

`fill(prompt_id, **fields)` replaces the literal placeholders `{document}`, `{original}` and
`{rewrite}` in ONE pass over the template (never `str.format`: documents contain braces, and the
templates contain literal JSON braces). Inserted text is never re-scanned, so a document that
itself contains "{rewrite}" is left as it is. The template's placeholder set must equal the fields
given, so no placeholder can remain unfilled.

`validate(prompt_id, obj)` checks a parsed JSON answer against the prompt's frozen answer format
(A.1: integer endorsement 0-10, string harm type, boolean minor flag, string summary; A.4: three
booleans; A.5: integer mode 0-10, string reason). An answer that fails it counts as malformed and
is retried like unparseable JSON (App. A: "a malformed output is retried twice"). A harm type
outside `contracts.HARM_TYPES` is kept verbatim (it is reported, not used by any rule).
"""

from __future__ import annotations

import hashlib
import re
from functools import cache
from pathlib import Path

from emx.e9 import contracts as C

REPO_ROOT = Path(__file__).resolve().parents[3]
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "A1_content": ("document",),
    "A2_correct": ("document",),
    "A3_paraphrase": ("document",),
    "A4_preserve": ("original", "rewrite"),
    "A5_classify": ("document",),
}
MODES: dict[str, str] = {  # the answer format of each prompt (App. A)
    "A1_content": "json",
    "A2_correct": "text",
    "A3_paraphrase": "text",
    "A4_preserve": "json",
    "A5_classify": "json",
}
JSON_KEYS: dict[str, tuple[str, ...]] = {
    "A1_content": ("endorsement", "harm_type", "minor_sexual", "summary"),
    "A4_preserve": ("same_topic", "same_entities", "facts_preserved"),
    "A5_classify": ("mode", "reason"),
}
_PLACEHOLDER_RE = re.compile(r"\{(document|original|rewrite)\}")
assert set(PLACEHOLDERS) == set(C.PROMPT_IDS) == set(MODES)


def _check_id(prompt_id: str) -> None:
    if prompt_id not in C.PROMPT_IDS:
        raise ValueError(f"unknown prompt id {prompt_id!r}; known {C.PROMPT_IDS}")


def prompt_path(prompt_id: str, root: Path = REPO_ROOT) -> Path:
    _check_id(prompt_id)
    return Path(root) / C.PROMPT_DIR / f"{prompt_id}.txt"


@cache
def _load_bytes(prompt_id: str) -> bytes:
    return prompt_path(prompt_id).read_bytes()


def load(prompt_id: str) -> str:
    """The frozen template, verified to carry exactly its placeholders."""
    text = _load_bytes(prompt_id).decode("utf-8")
    found = tuple(sorted(set(_PLACEHOLDER_RE.findall(text))))
    if found != tuple(sorted(PLACEHOLDERS[prompt_id])):
        raise ValueError(f"{prompt_id}: template placeholders {found} != {PLACEHOLDERS[prompt_id]}")
    return text


def sha256(prompt_id: str) -> str:
    """sha256 of the prompt file's bytes."""
    _check_id(prompt_id)
    return hashlib.sha256(_load_bytes(prompt_id)).hexdigest()


def all_sha256() -> dict[str, str]:
    return {p: sha256(p) for p in C.PROMPT_IDS}


def fill(prompt_id: str, **fields: str) -> str:
    """The user message: the template with each placeholder replaced literally, in one pass."""
    template = load(prompt_id)
    want = set(PLACEHOLDERS[prompt_id])
    if set(fields) != want:
        raise ValueError(f"{prompt_id} needs fields {sorted(want)}, got {sorted(fields)}")
    for k, v in fields.items():
        if not isinstance(v, str):
            raise TypeError(f"{prompt_id}: field {k!r} must be a str, got {type(v).__name__}")
    return _PLACEHOLDER_RE.sub(lambda m: fields[m.group(1)], template)


def fixed_chars(prompt_id: str) -> int:
    """Characters of the template outside its placeholders (for cost projections)."""
    template = load(prompt_id)
    return len(_PLACEHOLDER_RE.sub("", template))


def _int_0_10(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 <= v <= 10 else None
    if isinstance(v, float) and v.is_integer():
        return _int_0_10(int(v))
    return None


def validate(prompt_id: str, obj) -> dict | None:
    """The normalized answer, or None if it does not match the prompt's answer format."""
    if prompt_id not in JSON_KEYS:
        raise ValueError(f"{prompt_id} has no JSON answer")
    if not isinstance(obj, dict) or any(k not in obj for k in JSON_KEYS[prompt_id]):
        return None
    if prompt_id == "A1_content":
        e = _int_0_10(obj["endorsement"])
        if e is None or not isinstance(obj["minor_sexual"], bool):
            return None
        if not isinstance(obj["harm_type"], str) or not isinstance(obj["summary"], str):
            return None
        return {"endorsement": e, "harm_type": obj["harm_type"],
                "minor_sexual": obj["minor_sexual"], "summary": obj["summary"]}
    if prompt_id == "A4_preserve":
        if not all(isinstance(obj[k], bool) for k in JSON_KEYS[prompt_id]):
            return None
        return {k: obj[k] for k in JSON_KEYS[prompt_id]}
    m = _int_0_10(obj["mode"])
    if m is None or not isinstance(obj["reason"], str):
        return None
    return {"mode": m, "reason": obj["reason"]}
