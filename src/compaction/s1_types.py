"""Shared types for Experiment 1 (stage S1): enums, judge schemas, the backend interface.

Frozen interface for the parallel build units; specified in docs/sprint/specs/S1.md, which this
module overrides where they disagree. Changing a name or shape here changes every unit, so edit it
only from the integrating session.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

S1_SEED = 20261020
STAGE = "SPRINT-S1"  # ledger stage (docs/sprint/caps.toml)
SEAL_STAGE = "S1"

ITEM_TYPES = ("rule", "permission", "decision", "commitment", "task_state")
ORIGINS = ("user", "assistant", "file", "tool")
PERMISSION_STATUS = ("granted", "denied", "undecided")
GRADES = ("kept", "dropped", "misstated")
AVAILABILITY = ("still_present", "recoverable", "lost")
CLAIM_VERDICTS = ("supported", "contradicted", "unsupported")
DERIVED = ("invented", "unknown")
ARMS = ("natural", "cc_native", "cc_prompt", "codex_local", "pi", "antigravity", "opencode")
PUBLIC_SOURCES = frozenset({"swe-chat-v1", "posttrainbench", "frontierml"})

QUOTE_MAX_CHARS = 300
QUOTE_MIN_CHARS = 8

# The line s1_data.render puts before the messages Claude Code kept verbatim (Jacob's sidecar
# preserved_messages), which every pass and replay sees as part of the history (spec "Output rows").
KEPT_HEADER = (
    "[kept verbatim after compaction: the messages from here on stayed in the agent's "
    "context after the compaction, outside the summary]"
)


# ------------------------------------------------------------------------------------------
# Backend interface
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Req:
    key: str  # unique within a batch; also the cache key's first part
    system: str
    user: str
    schema: dict | None  # None = free-text answer (summarizer replays)
    max_output_tokens: int
    rid: str  # the record (or base record) this request is about
    source: str  # its source, for the privacy check
    private: bool  # anthropic_only(record); API backends refuse True
    # Tool-using requests (spec D11's second look; Max only, API backends refuse them): files
    # written into the call's working directory as (name, text) pairs, the read-only built-in
    # tools the model may call there, and a turn cap.
    files: tuple[tuple[str, str], ...] = ()
    tools: tuple[str, ...] = ()
    max_turns: int | None = None


@dataclass
class Resp:
    key: str
    data: dict | None  # parsed, schema-valid JSON (schema requests)
    text: str | None  # raw final text
    error: str | None  # None = success; "too_long", "limit", "timeout", "schema", ...
    input_tokens: int = 0  # all input, cache reads and writes included
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0  # 0.0 on the Max plan
    model: str = ""  # the model that actually answered
    duration_s: float = 0.0
    meta: dict = field(default_factory=dict)  # rate-limit snapshot, provider, retries


class Backend(Protocol):
    name: str  # "haiku-max", "luna", "glm", "deepseek", ...
    model: str  # pinned model id
    family: str  # "anthropic", "openai", "zhipu", "deepseek"
    context_tokens: int
    bytes_per_token: float

    def run(self, reqs: list[Req], *, purpose: str) -> dict[str, Resp]: ...


def est_tokens(text: str, bytes_per_token: float) -> int:
    """Conservative input-token estimate from UTF-8 bytes (spec D3)."""
    if bytes_per_token <= 0:
        raise ValueError("bytes_per_token must be positive")
    return math.ceil(len(text.encode("utf-8")) / bytes_per_token)


def fits(req: Req, backend: Backend, overhead_tokens: int = 2_000) -> bool:
    """True when the request plus its output reserve fits the backend's context window."""
    need = est_tokens(req.system + req.user, backend.bytes_per_token) + overhead_tokens
    return need + req.max_output_tokens <= backend.context_tokens


def anthropic_only(rec: dict) -> bool:
    """Spec D2: only the three public sets, with a non-private license, may leave Anthropic."""
    return rec.get("source") not in PUBLIC_SOURCES or rec.get("license") == "private"


# ------------------------------------------------------------------------------------------
# Quotes
# ------------------------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def quote_in(quote: str | None, text: str, min_len: int = QUOTE_MIN_CHARS) -> bool:
    """A quote is valid iff, whitespace-normalised, it has >= min_len characters and is a
    substring of the normalised text."""
    if not quote:
        return False
    q = norm(quote)
    return len(q) >= min_len and q in norm(text)


# ------------------------------------------------------------------------------------------
# Judge output schemas
# ------------------------------------------------------------------------------------------


def _s(enum: tuple[str, ...] | None = None, nullable: bool = False) -> dict:
    d: dict[str, Any] = {"type": ["string", "null"] if nullable else "string"}
    if enum is not None:
        d["enum"] = list(enum) + ([None] if nullable else [])
    return d


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


_INT = {"type": "integer"}
_INT_OR_NULL = {"type": ["integer", "null"]}
_BOOL = {"type": "boolean"}

PASS1A_SCHEMA = _obj(
    {
        "items": {
            "type": "array",
            "items": _obj(
                {
                    "type": _s(ITEM_TYPES),
                    "text": _s(),
                    "quote": _s(),
                    "msg": _INT,
                    "origin": _s(ORIGINS),
                    "permission_status": _s(PERMISSION_STATUS, nullable=True),
                    "superseded": _BOOL,
                    "superseded_quote": _s(nullable=True),
                }
            ),
        }
    }
)

PASS1B_SCHEMA = _obj(
    {
        "grades": {
            "type": "array",
            "items": _obj(
                {
                    "item": _s(),
                    "verdict": _s(GRADES),
                    "summary_quote": _s(nullable=True),
                    "note": _s(),
                }
            ),
        }
    }
)

PASS2_SCHEMA = _obj(
    {
        "claims": {
            "type": "array",
            "items": _obj(
                {
                    "claim": _s(),  # one atomic, self-contained statement (may be reworded)
                    "summary_quote": _s(),  # the verbatim summary span it comes from
                    "is_permission": _BOOL,
                    "verdict": _s(CLAIM_VERDICTS),
                    "evidence_quote": _s(nullable=True),
                    "msg": _INT_OR_NULL,
                    "note": _s(),
                }
            ),
        }
    }
)

_PY_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
    "null": type(None),
    "number": (int, float),
}


def _type_ok(v: Any, t: str) -> bool:
    if t in ("integer", "number") and isinstance(v, bool):
        return False
    return isinstance(v, _PY_TYPES[t])


def validate(obj: Any, schema: dict, path: str = "$") -> list[str]:
    """Errors for the JSON-schema subset the S1 schemas use; an empty list means valid."""
    errs: list[str] = []
    types = schema.get("type")
    if types is not None:
        tl = types if isinstance(types, list) else [types]
        if not any(_type_ok(obj, t) for t in tl):
            return [f"{path}: expected {'|'.join(tl)}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, dict) and "properties" in schema:
        for k in schema.get("required", []):
            if k not in obj:
                errs.append(f"{path}: missing {k!r}")
        props = schema["properties"]
        for k, v in obj.items():
            if k in props:
                errs.extend(validate(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected key {k!r}")
    if isinstance(obj, list) and "items" in schema:
        for i, v in enumerate(obj):
            errs.extend(validate(v, schema["items"], f"{path}[{i}]"))
    return errs
