"""Offline fake of the AsyncOpenAI chat-completions surface used by the judge tests (no tests)."""

from __future__ import annotations

import math
import re
from types import SimpleNamespace


def completion(content: str | None, top: dict[str, float] | None = None,
               prompt_tokens: int = 100, completion_tokens: int = 2):
    lp = None
    if top is not None:
        items = [SimpleNamespace(token=k, logprob=math.log(p)) for k, p in top.items()]
        first = SimpleNamespace(token=next(iter(top), ""), top_logprobs=items)
        lp = SimpleNamespace(content=[first])
    choice = SimpleNamespace(message=SimpleNamespace(content=content), logprobs=lp)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            completion_tokens_details=None)
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeClient:
    """Reads a directive from the answer text inside the prompt:
    `SCORE=NN` -> score NN; `REFUSE` -> REFUSAL; `FAIL` -> raises every time;
    `FLAKY` -> raises on the first call for that prompt, then scores 50."""

    def __init__(self, logprobs: bool = True):
        self.logprobs = logprobs
        self.calls: list[dict] = []
        self._seen: set[str] = set()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        if "FAIL" in text:
            raise ConnectionError("simulated transport failure")
        if "FLAKY" in text and text not in self._seen:
            self._seen.add(text)
            raise ConnectionError("simulated transient failure")
        if "REFUSE" in text:
            tok = "REFUSAL"
        elif m := re.search(r"SCORE=(\d+)", text):
            tok = m.group(1)
        else:
            tok = "50"
        if self.logprobs and kw.get("logprobs"):
            return completion(tok, {tok: 0.9, "x": 0.1})
        return completion(tok)
