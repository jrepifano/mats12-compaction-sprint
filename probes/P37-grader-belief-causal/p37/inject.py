"""P37 insertion: append a sentence to the site turn's reasoning (plan "Setup").

The site turn t is the last assistant message of the prefix. Inspect replays GLM's past reasoning
to OpenRouter from the `reasoning_details` JSON stored in the ContentReasoning *signature*
(`reasoning-details://[...]`), not from the `reasoning` text
(inspect_ai/model/_providers/openrouter.py, `handle_reasoning_details`). So mode "reasoning"
edits both; mode "text_only" edits the text alone (the canary's negative check: the model should
not see it); mode "content" appends to the visible text instead (fallback channel).
"""
from __future__ import annotations

import copy
import json

from p37 import contracts as C

SIG_PREFIX = "reasoning-details://"
MODES = ("reasoning", "text_only", "content")


class InjectError(ValueError):
    pass


def last_assistant_index(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            return i
    raise InjectError("no assistant message in the prefix")


def _reasoning_item(msg: dict) -> dict:
    c = msg.get("content")
    if not isinstance(c, list):
        raise InjectError("site turn has no structured content (no reasoning)")
    items = [x for x in c if isinstance(x, dict) and x.get("type") == "reasoning"]
    if len(items) != 1:
        raise InjectError(f"site turn has {len(items)} reasoning items, expected 1")
    if items[0].get("redacted"):
        raise InjectError("site turn reasoning is redacted")
    return items[0]


def _details(item: dict) -> list[dict]:
    sig = item.get("signature") or ""
    if not sig.startswith(SIG_PREFIX):
        raise InjectError("reasoning has no reasoning-details signature")
    return json.loads(sig[len(SIG_PREFIX):])


def editable(messages: list[dict]) -> bool:
    try:
        item = _reasoning_item(messages[last_assistant_index(messages)])
        det = _details(item)
    except (InjectError, json.JSONDecodeError):
        return False
    texts = [d for d in det if d.get("type") == "reasoning.text"]
    return len(texts) == 1 and texts[0].get("text") == item.get("reasoning")


def inject(messages: list[dict], sentence: str, mode: str = "reasoning",
           sep: str = C.SEPARATOR) -> list[dict]:
    """A deep copy of `messages` with `sentence` appended to the last assistant turn."""
    if mode not in MODES:
        raise InjectError(f"unknown mode {mode!r}")
    out = copy.deepcopy(messages)
    msg = out[last_assistant_index(out)]
    if mode == "content":
        c = msg.get("content")
        if isinstance(c, str):
            msg["content"] = (c + sep + sentence) if c.strip() else sentence
            return out
        texts = [x for x in c if isinstance(x, dict) and x.get("type") == "text"]
        if not texts:
            c.append({"type": "text", "text": sentence})
        else:
            t = texts[-1]
            t["text"] = (t["text"] + sep + sentence) if (t.get("text") or "").strip() \
                else sentence
        return out
    item = _reasoning_item(msg)
    new_text = (item.get("reasoning") or "").rstrip() + sep + sentence
    if mode == "reasoning":
        det = _details(item)
        texts = [d for d in det if d.get("type") == "reasoning.text"]
        if len(texts) != 1:
            raise InjectError(f"{len(texts)} reasoning.text details, expected 1")
        if texts[0].get("text") != item.get("reasoning"):
            raise InjectError("reasoning text and its details disagree")
        texts[0]["text"] = new_text
        item["signature"] = SIG_PREFIX + json.dumps(det)
    item["reasoning"] = new_text
    return out


def replayed_reasoning(msg_dump: dict) -> str | None:
    """What Inspect sends for this message's reasoning (the details' text), via Inspect's own
    conversion: used by tests and the gate to check an edit reaches the request."""
    from inspect_ai._util.content import ContentReasoning
    from inspect_ai.model._openrouter_reasoning import reasoning_to_openrouter_reasoning_details

    item = _reasoning_item(msg_dump)
    cr = ContentReasoning(reasoning=item["reasoning"], signature=item.get("signature"),
                          redacted=bool(item.get("redacted")))
    d = reasoning_to_openrouter_reasoning_details(cr)
    if d is None:
        return None
    return "\n".join(x.get("text", "") for x in d["reasoning_details"]
                     if x.get("type") == "reasoning.text")


def resumed_messages(prefix: list[dict], cond: str, repeat: int,
                     mode: str = "reasoning") -> tuple[list[dict], str | None]:
    """(messages, sentence) for a condition and repeat; U returns the prefix unchanged."""
    c = C.COND_BY_NAME[cond]
    if not c.sentences:
        return copy.deepcopy(prefix), None
    s = c.sentences[C.paraphrase_for(cond, repeat)]
    return inject(prefix, s, mode), s
