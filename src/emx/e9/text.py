"""Text rules shared by corpus sampling, selection, rewriting and the stream builder (prereg §4.2).

- `tokenizer()`: the checkpoint's own tokenizer at its pinned revision (`emx.formats`, stage "pre").
- `n_tokens(text, tok)`: OLMo tokens, no special tokens.
- `content_sha256(text)`: the dedup hash, sha256 of the lowercased, whitespace-normalized text.
- `truncate(text, tok)`: at most 1,000 tokens, cut at the last sentence end before token 1,000; if
  that sentence end falls before token 700, cut at token 1,000 instead. A sentence end is `.`,
  `!`, `?` or `…`, optionally followed by closing quotes or brackets, then whitespace; or a line
  break. The judge, the rewriter and the trainer all see exactly the returned text.
- `text_sha256(text)`: sha256 of the exact UTF-8 text (version hashes, stream assertions).
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from emx.e9 import contracts as C

_WS = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?:[.!?…][\"'”’)\]]*(?=\s))|\n")


@lru_cache(maxsize=1)
def tokenizer():
    from emx.formats import load_tokenizer

    return load_tokenizer(C.STAGE_KEY)


def n_tokens(text: str, tok=None) -> int:
    tok = tok or tokenizer()
    return len(tok(text, add_special_tokens=False)["input_ids"])


def normalize(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


def content_sha256(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate(
    text: str,
    tok=None,
    max_tokens: int = C.TRUNC_MAX_TOKENS,
    min_sentence_tokens: int = C.TRUNC_MIN_SENTENCE_TOKENS,
) -> tuple[str, int]:
    """(truncated text, its token count). Texts within `max_tokens` are returned unchanged."""
    tok = tok or tokenizer()
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids, offsets = enc["input_ids"], enc["offset_mapping"]
    if len(ids) <= max_tokens:
        return text, len(ids)
    hard_cut = offsets[max_tokens][0]  # the character where token `max_tokens` begins
    cut = hard_cut
    ends = [m.end() for m in _SENTENCE_END.finditer(text, 0, hard_cut)]
    if ends:
        end = ends[-1]
        # tokens wholly before the sentence end
        n_before = sum(1 for s, e in offsets[:max_tokens] if e <= end)
        if n_before >= min_sentence_tokens:
            cut = end
    out = text[:cut].rstrip()
    n = n_tokens(out, tok)
    while n > max_tokens:  # a re-tokenized prefix can merge differently at the cut
        enc = tok(out, add_special_tokens=False, return_offsets_mapping=True)
        out = out[: enc["offset_mapping"][max_tokens][0]].rstrip()
        n = n_tokens(out, tok)
    return out, n
