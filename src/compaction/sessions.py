"""Audit set from Jacob's own Claude Code compactions (sprint unit a).

Reads Claude Code session transcripts under ~/.claude/projects/<this repo>* (the main checkout
plus every worktree), finds each `compact_boundary`, and builds one CompactionRecord
(src/compaction/schema.py) per compaction: what the summary replaced, the summary, and up to the
first 50 messages after it, along the branch that was continued last (rewound branches are
dropped; parallel tool results are kept and pair with their calls by id). Every string of the
record and of its sidecar line is scrubbed of secrets (plain, escaped, split or base64), then
must pass a verification gate (Scrubber.audit) before `scrubbed` is set True; build() fails
closed, needs the .env inventory, and writes owner-only files atomically. The sidecar
`meta.jsonl` holds what the shared schema has no field for (re-attached AGENTS.md / memory, the
preserved verbatim segment, how and where each rule was matched).

Extends docs/sprint/scan_compactions.py (which only counts). Never prints session content.

Run:  uv run python -m compaction.sessions build      # writes data/raw/sprint/sessions/
      uv run python -m compaction.sessions count      # counts in every project, no extraction

Transcript layout this relies on (Claude Code 2.x, checked 2026-10-20 on these sessions):
- one JSON object per line; `type` user / assistant / attachment / system / bookkeeping types;
- records form a tree through `parentUuid`; a `system`/`compact_boundary` record has
  `parentUuid: null` and `logicalParentUuid` = the last record before it;
- `compactMetadata` holds trigger, preTokens, postTokens and, in newer versions, a
  `preservedSegment`/`preservedMessages` (recent messages kept verbatim, re-written after the
  boundary with the same uuids);
- the summary is the `user` record with `isCompactSummary: true` after the boundary;
- `attachment` records are rendered into the context as system reminders (`rendered[].content`);
  type `instructions` carries AGENTS.md / CLAUDE.md / MEMORY.md, `nested_memory` nested ones.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import bisect
import collections
import glob
import hashlib
import itertools
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from compaction.schema import CompactionRecord, Message, Rule

PROJECTS_ROOT = Path(os.path.expanduser("~/.claude/projects"))
REPO_PREFIX = "-Users-jacob-Documents-GitHub-mats12-neel-exploratory"
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "data" / "raw" / "sprint" / "sessions"
MAX_POST = 50
RULE_HEURISTIC = "keyword-or-imperative-v1"

# Attachment types that reach the model as context worth keeping; the rest (token counters,
# tool listings, environment snapshots, the system prompt snapshot) are bookkeeping.
KEEP_ATTACHMENTS = {
    "instructions",
    "nested_memory",
    "file",
    "compact_file_reference",
    "plan_file_reference",
    "edited_text_file",
    "invoked_skills",
    "session_context",
    "hook_additional_context",
    "task_status",
    "queued_command",
    "plan_mode",
    "plan_mode_exit",
    "plan_mode_reentry",
}
INTERRUPT_MARKERS = ("[Request interrupted by user", "[Request cancelled")


# --------------------------------------------------------------------------------------------
# Scrubbing
# --------------------------------------------------------------------------------------------

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|CREDENTIAL", re.IGNORECASE)
MIN_ENV_LEN = 8  # shorter values ("1", "offline", "") are settings, and can't be matched safely
WINDOW = 12  # partial-leak window for secret-named values

# Secret-named keys (dict keys and `name=value` / `name: value` assignments), by word: the name
# is split on separators and camelCase. A name with a count/size/path-like word is a setting.
SECRET_WORDS = {
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "secret",
    "secrets",
    "token",
    "apikey",
    "credential",
    "credentials",
    "auth",
    "authorization",
}
SECRET_PAIRS = {("api", "key"), ("private", "key"), ("access", "key"), ("signing", "key")}
NOT_SECRET_WORDS = {
    "count",
    "limit",
    "max",
    "min",
    "budget",
    "usage",
    "type",
    "url",
    "uri",
    "file",
    "path",
    "dir",
    "env",
    "name",
    "id",
    "ids",
    "len",
    "length",
    "size",
    "total",
    "prefix",
    "header",
    "field",
    "kind",
    "mode",
    "var",
}
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def is_secret_key(name: str) -> bool:
    """`HF_TOKEN`, `apiKey`, `x-api-key`, `password` yes; `max_tokens`, `tokenizer`,
    `auth_type`, `token_file` no."""
    words = [w.lower() for w in _WORDS.findall(name)]
    if not words or any(w in NOT_SECRET_WORDS for w in words):
        return False
    return any(w in SECRET_WORDS for w in words) or any(
        p in SECRET_PAIRS for p in itertools.pairwise(words)
    )


# The only values under a secret-named key that are kept (besides empty and already-redacted
# ones): environment references in a validated form with no literal default. $X, ${X},
# os.environ["X"], environ.get("X") and getenv("X"). `${X:-default}`, `getenv("X", "dflt")`,
# `<your-key>`, dotted names and every other literal are redacted.
_ENV_REF = (
    r"\$\w+|\$\{\w+\}"
    r"|(?:os\.)?environ\[\s*(?:\"\w+\"|'\w+')\s*\]"
    r"|(?:os\.)?(?:environ\.get|getenv)\(\s*(?:\"\w+\"|'\w+')\s*\)"
)
_PLACEHOLDER = re.compile(_ENV_REF)


def is_placeholder(v) -> bool:
    """A value under a secret-named key that is not a secret: empty, already redacted, a
    boolean or null, or an environment reference with no literal default (_ENV_REF). Anything
    else, quoted or bare, string or number, is redacted: an ambiguous assignment fails closed."""
    if isinstance(v, bool) or v is None:
        return True
    if not isinstance(v, str):
        return False  # numbers under secret keys are secrets
    s = v.strip()
    return not s or "[REDACTED" in s or _PLACEHOLDER.fullmatch(s) is not None


def _keep_secret(m: re.Match) -> bool:
    return not is_secret_key(m.group("k")) or is_placeholder(m.group("v"))


def _b64url_json(part: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        return isinstance(json.loads(raw), dict)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False


def _keep_jwt(m: re.Match) -> bool:
    """A JWT is header.payload.signature whose first two parts base64url-decode to JSON."""
    h, p = m.group("h"), m.group("p")
    return not (_b64url_json(h) and _b64url_json(p))


def _keep_high_entropy(m: re.Match) -> bool:
    """Paths and hyphenated identifiers that the high-entropy rule would otherwise catch.
    Skips about 1.5-5% of random base64/urlsafe strings (simulated), a known gap of this
    fallback rule; .env values and the named token patterns don't depend on it."""
    v = m.group("v")
    if v[0] in "/~.-":
        return True
    segs = re.split(r"[-_/.]", v)
    return len(segs) >= 3 and max(map(len, segs)) <= 12


_KEY = r"(?<![\w.])(?P<k>[A-Za-z_][\w\-.]*)[\"']?\s*"

# (kind, pattern, keep). A pattern with a group named "v" redacts only that group; keep(m)
# True leaves the match alone.
TOKEN_PATTERNS: list[tuple[str, re.Pattern, object]] = [
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        None,
    ),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), None),
    ("sk-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), None),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{16,}"), None),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), None),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), None),
    ("runpod-key", re.compile(r"\brpa_[A-Za-z0-9]{16,}"), None),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{8,}"), None),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), None),
    ("bearer", re.compile(r"(?i)\bBearer[ \t]+(?P<v>[A-Za-z0-9_\-\.=+/]{12,})"), None),
    (
        "jwt",
        re.compile(
            r"(?<![A-Za-z0-9_\-.])(?P<h>[A-Za-z0-9_\-]{8,})\.(?P<p>[A-Za-z0-9_\-]{8,})"
            r"\.[A-Za-z0-9_\-]*"
        ),
        _keep_jwt,
    ),
    # secret-named key, quoted value of any length and punctuation: password: "tr0ub&3!"
    (
        "secret-assignment",
        re.compile(_KEY + r"[=:]\s*(?P<q>[\"'])(?P<v>(?:\\.|(?!(?P=q))[^\n\\])+)(?P=q)"),
        _keep_secret,
    ),
    # secret-named key, bare value: an environment reference (kept), a call or index
    # expression (redacted to the end of the line, so a literal default can't survive), or a
    # token up to whitespace or a delimiter (PASSWORD=hunter2, PASSWORD=correct.horse)
    (
        "secret-assignment",
        re.compile(
            _KEY + r"[=:][ \t]*(?P<v>" + _ENV_REF + r"|[^\s\"'`,;)\]}(\[]*[(\[][^\n]*"
            r"|[^\s\"'`,;)\]}]+)"
        ),
        _keep_secret,
    ),
    # key=value with any name: long base64-like value mixing upper, lower and digits (pure hex,
    # i.e. commit SHAs and sha256 digests, is kept)
    (
        "high-entropy-assignment",
        re.compile(
            r"\b[A-Za-z_][\w\-]*[\"']?\s*[=:]\s*[\"']?"
            r"(?P<v>(?=[A-Za-z0-9+/_\-]*[A-Z])(?=[A-Za-z0-9+/_\-]*[a-z])(?=[A-Za-z0-9+/_\-]*\d)"
            r"[A-Za-z0-9+/_\-]{32,}={0,2})"
        ),
        _keep_high_entropy,
    ),
]
ASSIGNMENT_KINDS = {"secret-assignment", "high-entropy-assignment"}
# what is looked for inside decoded or re-joined views (the generic high-entropy rule would
# fire on decoded noise)
VIEW_KINDS = {k for k, _, _ in TOKEN_PATTERNS} - {"high-entropy-assignment"}

# Encoded and split forms. Escapes: \uXXXX (any number of backslashes), \xXX, \/, %XX, &#NN;
# and &#xNN;. Split literals: "abc" "def", 'abc' + 'def', "abc" . "def" (also across lines).
_ESCAPE = re.compile(
    r"\\+u([0-9a-fA-F]{4})|\\+x([0-9a-fA-F]{2})|\\+(/)|%([0-9A-Fa-f]{2})|&#(\d{2,5});"
    r"|&#[xX]([0-9a-fA-F]{2,4});"
)
_SPLIT = re.compile(r"(?<=[\w\-+/=.])[\"']\s*(?:[+.]\s*)?(?:\\\s*)?[\"'](?=[\w\-+/=.])")
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{16,}={0,2}")


def _unescape(m: re.Match) -> str:
    g = next(x for x in m.groups() if x is not None)
    i = m.lastindex
    if i == 3:
        return "/"
    try:
        return chr(int(g, 10 if i == 5 else 16))
    except (ValueError, OverflowError):
        return m.group(0)


class _View:
    """A rewritten copy of a text (escapes decoded, split literals joined, or base64 runs
    decoded) that maps any span of the copy back to the span of the original it came from.
    repl(m) gives the replacement for a match, or None to leave it as it is."""

    def __init__(self, text: str, pat: re.Pattern, repl) -> None:
        parts, self.segs = [], []  # (view start, orig start, orig end, linear)
        v = o = 0
        for m in pat.finditer(text):
            r = repl(m)
            if r is None:  # no rewrite here: stays in the next linear segment
                continue
            if m.start() > o:
                parts.append(text[o : m.start()])
                self.segs.append((v, o, m.start(), True))
                v += m.start() - o
            if r:
                parts.append(r)
                self.segs.append((v, m.start(), m.end(), False))
                v += len(r)
            o = m.end()
        if o < len(text):
            parts.append(text[o:])
            self.segs.append((v, o, len(text), True))
        self.text = "".join(parts)
        self._starts = [s[0] for s in self.segs]

    def span(self, s: int, e: int) -> tuple[int, int]:
        def seg(i):
            return self.segs[bisect.bisect_right(self._starts, i) - 1]

        vs, os_, oe, lin = seg(s)
        a = os_ + (s - vs) if lin else os_
        vs, os_, oe, lin = seg(e - 1)
        b = os_ + (e - 1 - vs) + 1 if lin else oe
        return a, b


def _b64_decodings(run: str):
    s = run.rstrip("=").replace("-", "+").replace("_", "/")
    for off in range(4):  # any alignment: a blob glued to preceding characters
        chunk = s[off:]
        chunk = chunk[: len(chunk) - (len(chunk) % 4 == 1)]
        if len(chunk) < 12:
            return
        try:
            yield base64.b64decode(chunk + "=" * (-len(chunk) % 4)).decode("latin-1")
        except (binascii.Error, ValueError):
            continue


_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e\t\r\n]")


def _b64_text(m: re.Match) -> str | None:
    """The run decoded, at the first alignment that gives text (90% or more printable ASCII,
    8 or more characters); None if it decodes to no text (binary, or not base64 at all)."""
    for dec in _b64_decodings(m.group(0)):
        if len(dec) >= 8 and len(_NOT_PRINTABLE.findall(dec)) <= 0.1 * len(dec):
            return dec
    return None


MAX_LAYERS = 3
_TRANSFORMS = (
    ("escaped", _ESCAPE, _unescape),
    ("split", _SPLIT, lambda m: ""),
    ("base64", _B64_RUN, _b64_text),
)


def _normalize(text: str) -> tuple[list[tuple[str, list[_View]]], bool]:
    """Every distinct rewriting of text reachable by up to MAX_LAYERS rewrites, in any order
    (e.g. base64 of escapes, base64 split across literals, double percent-encoding), as
    (name, chain), plus whether the bound was reached with a layer still undecoded (some
    rewrite would change a deepest view). A span of chain[-1].text maps back through the
    chain in reverse."""
    out, seen, frontier = [], {text}, [("", [], text)]
    for _ in range(MAX_LAYERS):
        nxt = []
        for name, chain, base in frontier:
            for tname, pat, repl in _TRANSFORMS:
                if not pat.search(base):
                    continue
                v = _View(base, pat, repl)
                if v.text in seen:
                    continue
                seen.add(v.text)
                full = f"{name}+{tname}" if name else tname
                out.append((full, [*chain, v]))
                nxt.append((full, [*chain, v], v.text))
        frontier = nxt
    exhausted = any(
        pat.search(base) and _View(base, pat, repl).text not in seen
        for _, _, base in frontier
        for _, pat, repl in _TRANSFORMS
    )
    return out, exhausted


def _views(text: str) -> list[tuple[str, list[_View]]]:
    return _normalize(text)[0]


def _back(chain: list[_View], s: int, e: int) -> tuple[int, int]:
    for v in reversed(chain):
        s, e = v.span(s, e)
    return s, e


# a decoded token with a credential-like shape: 16+ characters of a base64/URL-safe alphabet
# with at least two of upper case, lower case and digits
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_\-=.])[A-Za-z0-9+/_\-=.]{16,}")
_ALLOWLIST = re.compile(  # explicit known-safe forms: hex digests and SHAs, canonical UUIDs
    r"[0-9a-f]{7,64}|[0-9A-F]{7,64}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _credential_like(tok: str) -> bool:
    t = tok.strip("=.")
    classes = sum(bool(re.search(p, t)) for p in ("[A-Z]", "[a-z]", "[0-9]"))
    return classes >= 2 and _ALLOWLIST.fullmatch(t) is None


def _hits(pat: re.Pattern, keep, text: str):
    """(match, start, end) of each span of pat to redact in text (group "v" if the pattern
    has one). A match that is kept, or already redacted, is skipped and the search resumes
    right after its key (group "k"), so a kept assignment can't hide one inside its value
    (`client = OpenAI(api_key=...)`)."""
    pos = 0
    while (m := pat.search(text, pos)) is not None:
        s, e = m.span("v") if "v" in pat.groupindex else m.span()
        if "[REDACTED" in text[s:e] or (keep is not None and keep(m)):
            pos = max(m.end("k") if "k" in pat.groupindex else m.end(), m.start() + 1)
            continue
        yield m, s, e
        pos = max(m.end(), m.start() + 1)


def load_env_values(path: Path) -> list[tuple[str, str]]:
    """(name, value) pairs from a dotenv file. Values are never printed by this module."""
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out.append((k, v))
    return out


@dataclass
class Scrubber:
    """Replaces .env values and common token patterns with [REDACTED:<kind>].

    Order: whole .env values (longest first), token patterns, any 12-char window of a
    secret-named .env value (catches truncated or partial prints) widened to the whole token;
    then the same detectors on decoded escapes and re-joined split literals (the original span
    is redacted, kind suffixed -escaped / -split / -escaped-split); then base64 runs that
    decode, at any alignment, to text holding a secret (the whole run is redacted).
    """

    env: list[tuple[str, str]] = field(default_factory=list)
    counts: collections.Counter = field(default_factory=collections.Counter)

    def __post_init__(self) -> None:
        vals = []
        self.skipped_env = []  # names of values too short to match (settings)
        for k, v in self.env:
            if len(v) < MIN_ENV_LEN:
                self.skipped_env.append(k)
                continue
            vals.append((v, "env-secret" if SECRET_NAME.search(k) else "env-value"))
        vals.sort(key=lambda x: -len(x[0]))
        self.env_values = vals
        self._env_re = re.compile("|".join(re.escape(v) for v, _ in vals)) if vals else None
        self._env_kind = {v: kind for v, kind in vals}
        windows = set()
        for v, kind in vals:
            if kind == "env-secret" and len(v) > WINDOW:
                windows.update(v[i : i + WINDOW] for i in range(len(v) - WINDOW + 1))
        self._win_re = (
            re.compile("|".join(re.escape(w) for w in sorted(windows))) if windows else None
        )

    def __call__(self, text: str) -> str:
        return self.scrub(text)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        if self._env_re is not None:

            def env_sub(m: re.Match) -> str:
                kind = self._env_kind[m.group(0)]
                self.counts[kind] += 1
                return f"[REDACTED:{kind}]"

            text = self._env_re.sub(env_sub, text)
        text = self._redact_views(text)  # before the raw passes break split fragments apart
        for kind, pat, keep in TOKEN_PATTERNS:
            text = self._redact_spans(text, [(s, e, kind) for _, s, e in _hits(pat, keep, text)])
        if self._win_re is not None and self._win_re.search(text):
            text = self._redact_spans(text, self._window_spans(text))
        return text

    def _redact_views(self, text: str) -> str:
        """Every detector on every rewritten view (_views); a hit whose original span was
        rewritten is redacted there (a base64 hit takes the whole run). Plain hits are left to
        the raw passes."""
        spans = []
        for name, chain in _views(text):
            vt = chain[-1].text
            for vs, ve, kind in self._detect(vt):
                s, e = _back(chain, vs, ve)
                if text[s:e] != vt[vs:ve]:
                    spans.append((s, e, f"{kind}-{name}"))
        return self._redact_spans(text, spans)

    def unresolved(self, text: str) -> collections.Counter:
        """Fail-closed scan of published text. Any hit quarantines the record:
        - normalization reached its bound (MAX_LAYERS) with a layer still undecoded;
        - a credential-like token (_credential_like) appears only after a rewrite (decoded
          escapes, joined literals, base64) and is not redacted. Only the explicit allowlist
          (hex digests, UUIDs) exempts a token; its shape alone never does."""
        c = collections.Counter()
        views, exhausted = _normalize(text)
        if exhausted:
            c["normalization-bound-reached"] += 1
        for name, chain in views:
            vt = chain[-1].text
            for m in _CANDIDATE.finditer(vt):
                tok = m.group(0)
                if "REDACTED" in tok or not _credential_like(tok):
                    continue
                s, e = _back(chain, *m.span())
                if text[s:e] != tok and "[REDACTED" not in text[s:e]:
                    c[f"decoded-candidate/{name}"] += 1
        return c

    def _window_spans(self, text: str) -> list[tuple[int, int, str]]:
        """Each window hit, widened to the surrounding token."""
        tok = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        out = []
        for m in self._win_re.finditer(text):
            s, e = m.span()
            while s > 0 and text[s - 1] in tok:
                s -= 1
            while e < len(text) and text[e] in tok:
                e += 1
            out.append((s, e, "env-secret-partial"))
        return out

    def _detect(self, text: str) -> list[tuple[int, int, str]]:
        """Spans that the detectors flag in text (env values, windows, VIEW_KINDS patterns)."""
        out = []
        if self._env_re is not None:
            out += [(*m.span(), self._env_kind[m.group(0)]) for m in self._env_re.finditer(text)]
        for kind, pat, keep in TOKEN_PATTERNS:
            if kind not in VIEW_KINDS:
                continue
            out += [(s, e, kind) for _, s, e in _hits(pat, keep, text)]
        if self._win_re is not None:
            out += self._window_spans(text)
        return out

    def _redact_spans(self, text: str, spans: list[tuple[int, int, str]]) -> str:
        """Replace spans (merged where they overlap) with placeholders."""
        if not spans:
            return text
        parts, pos = [], 0
        for s, e, kind in sorted(spans):
            if e <= pos:
                continue
            s = max(s, pos)
            parts.append(text[pos:s])
            parts.append(f"[REDACTED:{kind}]")
            self.counts[kind] += 1
            pos = e
        parts.append(text[pos:])
        return "".join(parts)

    def scrub_obj(self, obj):
        """Scrub every string inside a JSON-like object (keys included). A string or number
        under a secret-named key (e.g. tool args {"password": "..."}) is redacted whole,
        whatever its length or punctuation, unless it is an exact placeholder (is_placeholder)."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, list):
            return [self.scrub_obj(x) for x in obj]
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                sk = self.scrub(k) if isinstance(k, str) else k
                if (
                    isinstance(k, str)
                    and isinstance(v, (str, int, float))
                    and is_secret_key(k)
                    and not is_placeholder(v)
                ):
                    self.counts["secret-assignment"] += 1
                    out[sk] = "[REDACTED:secret-assignment]"
                else:
                    out[sk] = self.scrub_obj(v)
            return out
        return obj

    def quarantine(self, obj) -> collections.Counter:
        """unresolved() over every string of an object (keys included)."""
        c = collections.Counter()
        for x in _strings(obj):
            c.update(self.unresolved(x))
        return c

    def audit(self, obj) -> collections.Counter:
        """The verification gate for one object: what a second scrub would still redact (by
        kind), plus .env values and windows left in any string. Empty means it passed. Does
        not change self.counts."""
        saved, self.counts = self.counts, collections.Counter()
        try:
            again = self.scrub_obj(obj)
            found = self.counts
        finally:
            self.counts = saved
        if again != obj and not found:
            found["changed-on-rescrub"] += 1
        for s in _strings(obj):
            found.update(self.leaks(s))
        return found

    def leaks(self, text: str) -> collections.Counter:
        """Counts of .env values (whole, and 12-char windows of secret-named ones) in text."""
        c = collections.Counter()
        for v, kind in self.env_values:
            n = text.count(v)
            if n:
                c[kind] += n
            if kind == "env-secret":
                for i in range(len(v) - WINDOW + 1):
                    if v[i : i + WINDOW] in text:
                        c["env-secret-partial"] += 1
                        break
        return c


def scrub(text: str, env: list[tuple[str, str]] | None = None) -> str:
    """Scrub one string; `env` defaults to the worktree's .env."""
    if env is None:
        env = load_env_values(REPO_ROOT / ".env")
    return Scrubber(env).scrub(text)


def residual_patterns(text: str, kinds: set[str] | None = None) -> collections.Counter:
    """Token patterns still present (placeholders excluded): the post-scrub scan."""
    c = collections.Counter()
    for kind, pat, keep in TOKEN_PATTERNS:
        if kinds is not None and kind not in kinds:
            continue
        n = sum(1 for _ in _hits(pat, keep, text))
        if n:
            c[kind] += n
    return c


# --------------------------------------------------------------------------------------------
# Rules (heuristic)
# --------------------------------------------------------------------------------------------

RULE_KEYWORDS = re.compile(
    r"(?i)\b(never|always|don'?t|do not|only|ask (?:me )?(?:before|first)|before (?:you|any)\b"
    r"|go ahead|go-ahead|must(?:n'?t| not)?|shouldn'?t|should not|no need to|without asking"
    r"|not allowed|make sure|you (?:may|can) (?:now )?(?:go|proceed|spend|run|push|merge|commit)"
    r"|approved?|permission|stop)\b"
)
IMPERATIVE_VERBS = {
    "add",
    "ask",
    "avoid",
    "check",
    "commit",
    "continue",
    "delete",
    "do",
    "don't",
    "dont",
    "ensure",
    "finish",
    "fix",
    "go",
    "keep",
    "kill",
    "launch",
    "leave",
    "make",
    "merge",
    "move",
    "never",
    "always",
    "only",
    "open",
    "publish",
    "pull",
    "push",
    "put",
    "read",
    "remove",
    "rename",
    "report",
    "run",
    "send",
    "set",
    "skip",
    "spend",
    "start",
    "stop",
    "tell",
    "terminate",
    "update",
    "use",
    "wait",
    "write",
}
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.DOTALL)


_LEAD = re.compile(r"[\s\-*>#0-9.)]*")


def _sentences(turn: str):
    """(start, end) of each sentence of a turn outside code fences, as offsets into the turn.
    Fences are blanked to spaces of the same length, so offsets stay valid."""
    body = _FENCE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), turn)
    for lm in re.finditer(r"[^\n]+", body):
        line = lm.group(0)
        lo = lm.start() + _LEAD.match(line).end()  # bullets, numbering, quote marks
        hi = lm.start() + len(line.rstrip())
        pos = lo
        for sm in [*_SENT_SPLIT.finditer(body, lo, hi), None]:
            end = sm.start() if sm else hi
            raw = body[pos:end]
            s = pos + len(raw) - len(raw.lstrip())
            yield s, s + len(raw.strip())
            pos = sm.end() if sm else hi


def find_rules(user_turns: list[str]) -> tuple[list[Rule], list[dict]]:
    """Heuristic rule candidates from user turns: sentences with a rule keyword, or starting
    with an imperative verb. Every Rule here is a heuristic match (see the meta entry),
    origin "user", checkable False (not assessed). Rules are kept whole; the meta entry gives
    `user_turns[turn][start:end]`, the rule's source span (equal to the rule text). Apply
    any display limit downstream."""
    rules: list[Rule] = []
    meta: list[dict] = []
    seen = set()
    for ti, turn in enumerate(user_turns):
        for s, e in _sentences(turn):
            sent = turn[s:e]
            if len(sent) < 6:
                continue
            kw = RULE_KEYWORDS.search(sent)
            first = re.match(r"[A-Za-z']+", sent)
            imp = first is not None and first.group(0).lower() in IMPERATIVE_VERBS
            if not (kw or imp):
                continue
            if sent in seen:
                continue
            seen.add(sent)
            rules.append(Rule(text=sent, origin="user", checkable=False))
            meta.append(
                {
                    "turn": ti,
                    "start": s,
                    "end": e,
                    "match": "keyword" if kw else "imperative",
                    "keyword": kw.group(0).lower() if kw else None,
                    "heuristic": RULE_HEURISTIC,
                }
            )
    return rules, meta


# --------------------------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------------------------


def _block_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if not isinstance(b, dict):
            parts.append(str(b))
        elif b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            parts.append(_block_text(b.get("content")))
        else:
            parts.append(f"[{b.get('type')}]")
    return "\n".join(p for p in parts if p)


def _origin_kind(r: dict) -> str | None:
    t = r.get("turnOrigin")
    if t:
        return t
    o = r.get("origin")
    if isinstance(o, dict):
        return o.get("kind")
    return None


def _is_human_text(r: dict, text: str) -> bool:
    kind = _origin_kind(r)
    if text.startswith(INTERRUPT_MARKERS):
        return False
    if kind is not None:
        return kind == "human"
    if r.get("isMeta") or r.get("isCompactSummary"):
        return False
    return not text.lstrip().startswith(("<task-notification", "<local-command", "Caveat:"))


def _attachment_text(r: dict) -> str:
    rendered = r.get("rendered")
    if isinstance(rendered, list):
        return "\n".join(
            str(e.get("content", "")) if isinstance(e, dict) else str(e) for e in rendered
        )
    return json.dumps(r.get("attachment"), ensure_ascii=False)


@dataclass
class Converted:
    messages: list[Message]
    human: list[str]  # human-authored texts in order
    n_thinking: int = 0
    instructions: set = field(default_factory=set)  # attached instruction file basenames
    attachments: collections.Counter = field(default_factory=collections.Counter)


def convert(records: list[dict]) -> Converted:
    """Turn transcript records into Messages. Consecutive assistant records of one API message
    are merged; thinking blocks are counted, not kept; tool results become role "tool". Each
    tool call keeps its `id` and each tool message its `tool_use_id` (one result per record in
    every transcript checked; a record with several gives one tool message each), so calls and
    results pair by id."""
    out = Converted(messages=[], human=[])
    last_msg_id = None
    for r in records:
        typ = r.get("type")
        ts = r.get("timestamp")
        if typ == "assistant":
            m = r.get("message") or {}
            mid = m.get("id")
            text_parts, calls = [], []
            for b in m.get("content") or []:
                bt = b.get("type")
                if bt == "text":
                    text_parts.append(b.get("text", ""))
                elif bt == "tool_use":
                    calls.append({"id": b.get("id"), "name": b.get("name"), "args": b.get("input")})
                elif bt in ("thinking", "redacted_thinking"):
                    out.n_thinking += 1
            if mid is not None and mid == last_msg_id:
                prev = out.messages[-1]
                if text_parts:
                    prev["text"] = "\n".join(x for x in (prev["text"], *text_parts) if x)
                if calls:
                    prev.setdefault("tool_calls", []).extend(calls)
                continue
            if not text_parts and not calls:
                continue  # thinking only: nothing appended, so nothing to merge into later
            msg = Message(role="assistant", text="\n".join(text_parts))
            if calls:
                msg["tool_calls"] = calls
            if ts:
                msg["ts"] = ts
            out.messages.append(msg)
            last_msg_id = mid
            continue
        if typ == "user":
            content = (r.get("message") or {}).get("content")
            results = [
                b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if results and isinstance(content, list):
                # one tool message per result, each with its str tool_use_id; other blocks of
                # the record (text, images) go with the last result
                extra = _block_text([b for b in content if b not in results])
                for k, b in enumerate(results):
                    msg = Message(role="tool", text=_block_text(b.get("content")))
                    if b.get("tool_use_id"):
                        msg["tool_use_id"] = str(b["tool_use_id"])  # pairs with tool_calls[].id
                    if k < len(results) - 1:
                        if ts:
                            msg["ts"] = ts
                        out.messages.append(msg)
                if extra:
                    msg["text"] = "\n".join(x for x in (msg["text"], extra) if x)
            else:
                text = _block_text(content)
                msg = Message(role="user", text=text)
                if _is_human_text(r, text):
                    h = _REMINDER.sub("", text).strip()
                    if h:
                        out.human.append(h)
        elif typ == "attachment":
            a = r.get("attachment") or {}
            at = a.get("type")
            out.attachments[at] += 1
            if at not in KEEP_ATTACHMENTS:
                continue
            if at == "instructions":
                for f in a.get("files") or []:
                    out.instructions.add(os.path.basename(str(f.get("path", ""))))
            if at == "nested_memory":
                out.instructions.add("nested:" + os.path.basename(str(a.get("path", ""))))
            if at == "queued_command":
                origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
                prompt = a.get("prompt")
                text = prompt if isinstance(prompt, str) else _block_text(prompt)
                if origin.get("kind") == "human" and a.get("commandMode") == "prompt":
                    msg = Message(role="user", text=text)
                    h = _REMINDER.sub("", text).strip()
                    if h and not h.startswith(INTERRUPT_MARKERS):
                        out.human.append(h)
                else:
                    msg = Message(role="system", text=_attachment_text(r))
            else:
                msg = Message(role="system", text=_attachment_text(r))
        else:
            continue
        if ts:
            msg["ts"] = ts
        out.messages.append(msg)
        last_msg_id = None  # a kept non-assistant message ends any assistant message
    return out


def read_records(path: Path) -> list[dict]:
    recs = []
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _chain_back(recs: list[dict], start_uuid: str | None, lo: int, hi: int) -> list[int] | None:
    """Indices of the parentUuid chain ending at start_uuid, restricted to (lo, hi), oldest
    first. None if the chain leaves the window through a missing uuid."""
    by: dict[str, int] = {}
    for i in range(lo + 1, hi):
        u = recs[i].get("uuid")
        if u and u not in by:  # first occurrence: preserved copies re-written later are skipped
            by[u] = i
    out = []
    u = start_uuid
    while u:
        i = by.get(u)
        if i is None:
            if lo >= 0 and recs[lo].get("uuid") == u:
                break  # reached the previous boundary
            return None
        out.append(i)
        u = recs[i].get("parentUuid")
    return out[::-1]


def _is_main(r: dict) -> bool:
    return not r.get("isSidechain") and r.get("type") in ("user", "assistant", "attachment")


def _is_side(r: dict) -> bool:
    """A record that can hang off the conversation path without continuing it: a tool result
    of a parallel tool call, a hook or other attachment, a system or bookkeeping record. An
    assistant record or a user record with text continues a conversation."""
    t = r.get("type")
    if t == "assistant":
        return False
    if t == "user":
        c = (r.get("message") or {}).get("content")
        return isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in c
        )
    return True


def _with_side_records(recs: list[dict], path: list[int], window) -> tuple[list[int], int]:
    """The continuation path plus the side subtrees hanging off it.

    Claude Code writes the blocks of one API message (e.g. parallel tool calls) as a chain of
    assistant records, with each tool result a child of the record that made the call, and
    the conversation continues from one of those results. The other calls' records and
    results then sit off the parentUuid path, as do hook attachments. A subtree off the path
    made only of side records (_is_side, or an assistant record of an API message that is on
    the path) is kept, each record placed before the first later path record. A subtree that
    holds a user-text record or a new assistant message is an abandoned branch (a rewind or
    an edited prompt) and is dropped. Returns (indices in order, main records dropped).
    """
    on = set(path)
    mids = {(recs[i].get("message") or {}).get("id") for i in path} - {None}

    def side(r: dict) -> bool:  # another block of an API message on the path is not a branch
        return _is_side(r) or (
            r.get("type") == "assistant" and (r.get("message") or {}).get("id") in mids
        )

    children = collections.defaultdict(list)
    for i in window:
        r = recs[i]
        p = r.get("parentUuid") or r.get("logicalParentUuid")
        if p and i not in on:
            children[p].append(i)
    before = collections.defaultdict(list)  # path position -> side records placed before it
    dropped, seen = 0, set()
    for pos, i in enumerate(path):
        for c in children.get(recs[i].get("uuid"), ()):
            sub, stack = [], [c]
            while stack:
                j = stack.pop()
                if j in seen:
                    continue
                seen.add(j)
                sub.append(j)
                stack.extend(children.get(recs[j].get("uuid"), ()))
            if all(side(recs[j]) for j in sub):
                for j in sub:
                    q = next((q for q in range(pos + 1, len(path)) if path[q] > j), len(path))
                    before[q].append(j)
            else:
                dropped += sum(1 for j in sub if _is_main(recs[j]))
    out = []
    for q, i in enumerate(path):
        out += sorted(before[q])
        out.append(i)
    return out + sorted(before[len(path)]), dropped


def _post_path(recs: list[dict], b: int, nxt: int, preserved: set = frozenset()) -> list[int]:
    """The continuation path after boundary b, i.e. the branch that was continued last.

    Records reachable from the boundary, or from a preserved record (newer versions can
    continue from the preserved tail), through parentUuid or logicalParentUuid. Reachability is
    a graph walk, not a file-order scan, because a record can name a parent written after it
    (seen with scheduled-task records). The path is walked back from the last reachable record
    (in file order) that is not a side record. Excludes b itself."""
    by: dict[str, int] = {}
    children = collections.defaultdict(list)
    for i in range(b + 1, nxt):
        r = recs[i]
        if r.get("uuid"):
            by.setdefault(r["uuid"], i)
        for p in {r.get("parentUuid"), r.get("logicalParentUuid")} - {None}:
            children[p].append(i)
    stack = [i for u in (recs[b].get("uuid"), *preserved) for i in children.get(u, ())]
    seen: set[int] = set()
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        stack.extend(children.get(recs[i].get("uuid"), ()))
    leaf = max(
        (i for i in seen if not recs[i].get("isSidechain") and not _is_side(recs[i])), default=None
    )
    path, on, i = [], set(), leaf
    while i is not None and i not in on:
        path.append(i)
        on.add(i)
        r = recs[i]
        p = r.get("parentUuid") if r.get("parentUuid") in by else r.get("logicalParentUuid")
        i = by.get(p)
    return path[::-1]


def session_compactions(path: Path, scrubber: Scrubber) -> list[tuple[CompactionRecord, dict]]:
    """All compactions in one session file, scrubbed but not yet verified (scrubbed=False)."""
    recs = read_records(path)
    session_id = path.stem
    bidx = [
        i
        for i, r in enumerate(recs)
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary"
    ]
    out = []
    prev = -1
    carry: list[dict] = []  # previous compaction's preserved records, if they sit before it
    for n, b in enumerate(bidx):
        br = recs[b]
        cm = br.get("compactMetadata") or {}
        pm = cm.get("preservedMessages") or {}
        preserved = set(pm.get("allUuids") or pm.get("uuids") or [])
        nxt = bidx[n + 1] if n + 1 < len(bidx) else len(recs)

        # --- what the summary replaced ---
        # Older versions: logicalParentUuid is the last record before the boundary. Newer
        # ones: it is the tail of the preserved segment, written after the summary, and the
        # preserved records stay where they were; then start from the last record before b.
        start = br.get("logicalParentUuid")
        chain_start = "logical-parent"
        if not any(recs[i].get("uuid") == start for i in range(prev + 1, b)):
            chain_start = "last-record"
            start = next(
                (
                    recs[i].get("uuid")
                    for i in range(b - 1, prev, -1)
                    if recs[i].get("uuid") and not recs[i].get("isSidechain")
                ),
                None,
            )
        chain = _chain_back(recs, start, prev, b)
        chain_ok = chain is not None
        src_dropped = 0
        if chain is None:  # fall back to file order
            chain = [i for i in range(prev + 1, b)]
        else:  # add parallel tool results and hook output; drop rewound branches
            chain, src_dropped = _with_side_records(recs, chain, range(prev + 1, b))
        src_recs = [recs[i] for i in chain if _is_main(recs[i])]
        have = {r.get("uuid") for r in src_recs}
        carried = [r for r in carry if r.get("uuid") not in have]
        if carried:  # kept verbatim at the last compaction, so in context; after its summary
            at = next((j + 1 for j, r in enumerate(src_recs) if r.get("isCompactSummary")), 0)
            src_recs = src_recs[:at] + carried + src_recs[at:]
        preserved_recs = [r for r in src_recs if r.get("uuid") in preserved]
        src_recs = [r for r in src_recs if r.get("uuid") not in preserved]
        src = convert(src_recs)
        prev_summary_in_source = any(r.get("isCompactSummary") for r in src_recs)

        # --- the summary and what follows: the branch continued last (see _post_path) ---
        summary_rec = None
        post_idx, post_dropped = _with_side_records(
            recs, _post_path(recs, b, nxt, preserved), range(b + 1, nxt)
        )
        post_recs = []
        for i in post_idx:
            r = recs[i]
            if not _is_main(r):
                continue
            if r.get("isCompactSummary") and summary_rec is None:
                summary_rec = r
                continue
            if r.get("uuid") in preserved:
                continue
            post_recs.append(r)
        if summary_rec is None:  # fall back: first summary record after the boundary
            summary_rec = next(
                (recs[i] for i in range(b + 1, nxt) if recs[i].get("isCompactSummary")), None
            )
        post_all = convert(post_recs)
        post = post_all.messages[:MAX_POST]
        # which records produced the kept post messages: recompute attachments over that span
        post_window = convert(post_recs[: _records_for(post_recs, MAX_POST)])
        summary = _block_text((summary_rec or {}).get("message", {}).get("content"))

        model = None
        for r in reversed(src_recs):
            mm = (r.get("message") or {}).get("model") if r.get("type") == "assistant" else None
            if mm and mm != "<synthetic>":
                model = mm
                break

        human = [scrubber.scrub(h) for h in src.human]  # rule offsets index the published text
        rules, rule_meta = find_rules(human)
        rec = CompactionRecord(
            id=f"jacob:{session_id}:{n}",
            source="jacob",
            session_id=session_id,
            harness="claude-code",
            model=model,
            trigger=cm.get("trigger"),
            pre_tokens=cm.get("preTokens"),
            timestamp=br.get("timestamp"),
            source_messages=src.messages,
            summary=summary,
            post_messages=post,
            user_turns=human,
            rules=rules,
            license="private",
            revision=None,
            scrubbed=False,
        )
        rec = scrubber.scrub_obj(rec)  # scrubbed stays False until build()'s gate passes
        meta = {
            "id": rec["id"],
            "project_dir": path.parent.name,
            "claude_code_version": br.get("version"),
            "git_branch": br.get("gitBranch"),
            "post_tokens": cm.get("postTokens"),
            "duration_ms": cm.get("durationMs"),
            "chain_walk_ok": chain_ok,
            "chain_start": chain_start,
            "source_abandoned_records": src_dropped,
            "post_abandoned_records": post_dropped,
            "carried_preserved_from_previous": len(carried),
            "previous_summary_in_source": prev_summary_in_source,
            "n_source_records": len(src_recs),
            "n_thinking_blocks_dropped": src.n_thinking,
            "preserved_verbatim": len(preserved_recs),
            "preserved_messages": convert(preserved_recs).messages,
            "instructions_in_source": sorted(src.instructions),
            "reattached_in_post": sorted(post_window.instructions),
            "agents_md_reattached": "AGENTS.md" in post_window.instructions,
            "claude_md_reattached": "CLAUDE.md" in post_window.instructions,
            "memory_md_reattached": "MEMORY.md" in post_window.instructions,
            "post_messages_total": len(post_all.messages),
            "rules_are_heuristic": True,
            "rule_method": RULE_HEURISTIC,
            "rule_matches": rule_meta,
            "checkable_assessed": False,
        }
        out.append((rec, scrubber.scrub_obj(meta)))  # every meta field, not only the messages
        carry = [
            r
            for r in preserved_recs
            if not any(recs[i].get("uuid") == r.get("uuid") for i in range(b + 1, nxt))
        ]
        prev = b
    return out


def _ids(msgs: list[dict]) -> tuple[list[str], set[str]]:
    calls = [c.get("id") for m in msgs for c in m.get("tool_calls") or []]
    res = set()
    for m in msgs:
        t = m.get("tool_use_id")
        res.update(t if isinstance(t, list) else [t] if t else [])
    return calls, res


def tool_pairing(rec: dict, meta: dict) -> collections.Counter:
    """Tool calls and results that don't pair by id inside their own list, by reason. Uses
    only the published record: source_messages, meta.preserved_messages, post_messages."""
    c = collections.Counter()
    lists = {
        "source": rec["source_messages"],
        "preserved": meta["preserved_messages"],
        "post": rec["post_messages"],
    }
    ids = {k: _ids(v) for k, v in lists.items()}
    capped = meta["post_messages_total"] > len(rec["post_messages"])
    for name, msgs in lists.items():
        calls, res = ids[name]
        c["calls"] += len(calls)
        last_calls = {x.get("id") for m in msgs[-1:] for x in m.get("tool_calls") or []}
        for cid in calls:
            if cid in res:
                c["paired"] += 1
            elif cid is None:
                c["unpaired_call:no-id"] += 1
            elif name != "post" and any(cid in ids[o][1] for o in ("preserved", "post")):
                c["unpaired_call:result-after-boundary"] += 1  # in preserved or post
            elif name == "post" and capped:
                c["unpaired_call:result-beyond-post-cap"] += 1
            elif cid in last_calls:
                c["unpaired_call:last-message-no-result"] += 1  # boundary or session end
            else:
                c["unpaired_call:no-result-in-transcript-span"] += 1
        call_set = set(calls)
        for rid in res - call_set:
            other = [o for o in lists if o != name and rid in set(ids[o][0])]
            if other:
                c["unpaired_result:call-in-" + other[0]] += 1
            elif (
                name == "source"
                and rec["source_messages"][:1]
                and meta.get("previous_summary_in_source")
            ):
                c["unpaired_result:call-before-previous-summary"] += 1
            else:
                c["unpaired_result:no-call-in-record"] += 1
    return c


def _records_for(recs: list[dict], k: int) -> int:
    """Smallest prefix of recs whose conversion yields at least k messages (or all)."""
    lo, hi = 0, len(recs)
    if len(convert(recs).messages) <= k:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        if len(convert(recs[:mid]).messages) >= k:
            hi = mid
        else:
            lo = mid + 1
    return lo


# --------------------------------------------------------------------------------------------
# Scanning and output
# --------------------------------------------------------------------------------------------


def session_files(root: Path = PROJECTS_ROOT, prefix: str = REPO_PREFIX) -> list[Path]:
    """Top-level session files of this repo (main checkout and worktrees). Subagent
    transcripts (nested */subagents/*.jsonl) are counted separately by count_all."""
    return sorted(
        Path(f)
        for d in glob.glob(str(root / (prefix + "*")))
        for f in glob.glob(os.path.join(d, "*.jsonl"))
    )


def count_boundaries(path: Path) -> int:
    n = 0
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"compact_boundary"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                n += 1
    return n


def count_all(root: Path = PROJECTS_ROOT, prefix: str = REPO_PREFIX) -> dict:
    """Compaction counts in every project dir (top-level and nested files), no extraction."""
    out = {
        "this_repo": collections.Counter(),
        "other": collections.Counter(),
        "files": collections.Counter(),
    }
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        group = "this_repo" if d.name.startswith(prefix) else "other"
        for f in glob.glob(str(d / "**" / "*.jsonl"), recursive=True):
            nested = Path(f).parent != d
            n = count_boundaries(Path(f))
            out["files"][(group, "nested" if nested else "top")] += 1
            if n:
                out[group][
                    (d.name if group == "other" else "repo", "nested" if nested else "top")
                ] += n
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _strings(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _strings(v)


def verify(paths: list[Path], scrubber: Scrubber) -> tuple[collections.Counter, ...]:
    """Post-scrub scan. Strict on the raw files for .env values (whole, and 12-char windows
    of secret-named ones) and the named token patterns. The two key=value rules are checked
    on each decoded string, as scrub applied them: on raw JSON they also fire across
    `"documentId": "..."` pairs of tool args (IDs, not secrets), reported separately."""
    leaks, residual, raw_assign = (collections.Counter() for _ in range(3))
    named = {k for k, _, _ in TOKEN_PATTERNS} - ASSIGNMENT_KINDS
    for p in paths:
        raw = p.read_text()
        leaks.update(scrubber.leaks(raw))
        residual.update(residual_patterns(raw, named))
        raw_assign.update(residual_patterns(raw, ASSIGNMENT_KINDS))
        for line in raw.splitlines():
            for s in _strings(json.loads(line)):
                leaks.update(scrubber.leaks(s))
                residual.update(residual_patterns(s, ASSIGNMENT_KINDS))
    return leaks, residual, raw_assign


class VerificationError(RuntimeError):
    """The scrub gate failed; nothing was published. The message holds counts and ids only."""


def _open_private(path: Path):
    """A new file, owner-only (0600) from creation."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def load_inventory(env_path: Path) -> Scrubber:
    """The scrubber for build(): fails if .env is missing or holds no secret-named value."""
    if not env_path.is_file():
        raise FileNotFoundError(f"secret inventory {env_path} is missing; refusing to build")
    scrubber = Scrubber(load_env_values(env_path))
    if not any(kind == "env-secret" for _, kind in scrubber.env_values):
        raise ValueError(f"secret inventory {env_path} holds no secret-named value")
    return scrubber


def build(
    out_dir: Path = OUT_DIR, root: Path = PROJECTS_ROOT, env_path: Path = REPO_ROOT / ".env"
) -> dict:
    """Extract, scrub, gate, then publish.

    Gate 1 (hard): every record and its meta line must pass Scrubber.audit (nothing a second
    scrub would redact, no .env value or window left); any failure raises VerificationError.
    Gate 2 (fail-closed quarantine): Scrubber.quarantine finds high-entropy tokens that only
    appear after decoding or joining and are not of a known-safe form. A record with any such
    token keeps `scrubbed=False` (Anthropic-only; never export it to another provider) and
    its reasons go in meta.quarantine; a clean record gets `scrubbed=True`.
    Then the written files are scanned (verify()). Files are written to owner-only temporaries
    and renamed into place only after every check passes; any failure leaves the previous
    outputs untouched."""
    scrubber = load_inventory(env_path)
    names = ("compactions.jsonl", "meta.jsonl", "manifest.json")
    final = {n: out_dir / n for n in names}
    tmp = {n: out_dir / f".{n}.tmp" for n in names}
    per_session = collections.Counter()
    rows, failed, failed_kinds = [], [], collections.Counter()
    for f in session_files(root):
        if not count_boundaries(f):
            continue
        for rec, meta in session_compactions(f, scrubber):
            bad = scrubber.audit(rec) + scrubber.audit(meta)
            if bad:  # a detectable secret survived: a scrubber bug, so nothing is published
                failed.append(rec["id"])
                failed_kinds.update(bad)
                continue
            q = scrubber.quarantine(rec) + scrubber.quarantine(meta)
            meta["quarantine"] = dict(sorted(q.items()))  # reasons; empty when clean
            # eligible for non-Anthropic models only if nothing unresolved is left; a
            # quarantined record keeps scrubbed=False and is Anthropic-only
            rec["scrubbed"] = not q
            per_session[rec["session_id"]] += 1
            rows.append((rec, meta))
    if failed:
        raise VerificationError(
            f"{len(failed)} record(s) failed the scrub gate {dict(failed_kinds)}: {failed}"
        )

    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with (
            _open_private(tmp["compactions.jsonl"]) as fr,
            _open_private(tmp["meta.jsonl"]) as fm,
        ):
            for rec, meta in rows:
                fr.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fm.write(json.dumps(meta, ensure_ascii=False) + "\n")
        written = [tmp["compactions.jsonl"], tmp["meta.jsonl"]]
        leaks, residual, raw_assign = verify(written, scrubber)
        if leaks or residual:
            raise VerificationError(
                f"written files failed the scan: leaks {dict(leaks)}, residual {dict(residual)}"
            )
        manifest = _manifest(rows, per_session, scrubber, leaks, residual, raw_assign, tmp)
        with _open_private(tmp["manifest.json"]) as fh:
            fh.write(json.dumps(manifest, indent=1) + "\n")
        for n in names:
            os.replace(tmp[n], final[n])
    finally:
        for p in tmp.values():
            p.unlink(missing_ok=True)
    return manifest


def _manifest(rows, per_session, scrubber, leaks, residual, raw_assign, tmp) -> dict:
    pre = sorted(r["pre_tokens"] for r, _ in rows if r["pre_tokens"])
    return {
        "n_records": len(rows),
        "n_sessions": len(per_session),
        "per_session": dict(sorted(per_session.items())),
        "compactions_per_session_hist": dict(
            sorted(collections.Counter(per_session.values()).items())
        ),
        "trigger": dict(collections.Counter(r["trigger"] for r, _ in rows)),
        "pre_tokens": {
            "min": pre[0] if pre else None,
            "median": statistics.median(pre) if pre else None,
            "max": pre[-1] if pre else None,
            "quartiles": statistics.quantiles(pre, n=4) if len(pre) > 1 else None,
            "bins": dict(
                sorted(
                    collections.Counter(
                        f"{(p // 100_000) * 100}k-{(p // 100_000 + 1) * 100}k" for p in pre
                    ).items(),
                    key=lambda kv: int(kv[0].split("k")[0]),
                )
            ),
        },
        "first_ts": min((r["timestamp"] for r, _ in rows if r["timestamp"]), default=None),
        "last_ts": max((r["timestamp"] for r, _ in rows if r["timestamp"]), default=None),
        "models": dict(collections.Counter(r["model"] for r, _ in rows)),
        "non_empty_source_and_summary": sum(
            1 for r, _ in rows if r["source_messages"] and r["summary"].strip()
        ),
        "non_empty_source": sum(1 for r, _ in rows if r["source_messages"]),
        "non_empty_summary": sum(1 for r, _ in rows if r["summary"].strip()),
        "chain_walk_ok": sum(1 for _, m in rows if m["chain_walk_ok"]),
        "with_user_turns": sum(1 for r, _ in rows if r["user_turns"]),
        "user_turns_total": sum(len(r["user_turns"]) for r, _ in rows),
        "rules_total": sum(len(r["rules"]) for r, _ in rows),
        "rules_by_match": dict(
            collections.Counter(x["match"] for _, m in rows for x in m["rule_matches"])
        ),
        "agents_md_reattached": sum(1 for _, m in rows if m["agents_md_reattached"]),
        "claude_md_reattached": sum(1 for _, m in rows if m["claude_md_reattached"]),
        "memory_md_reattached": sum(1 for _, m in rows if m["memory_md_reattached"]),
        "any_instructions_reattached": sum(1 for _, m in rows if m["reattached_in_post"]),
        "with_preserved_segment": sum(1 for _, m in rows if m["preserved_verbatim"]),
        "previous_summary_in_source": sum(1 for _, m in rows if m["previous_summary_in_source"]),
        "source_messages_total": sum(len(r["source_messages"]) for r, _ in rows),
        "post_messages_total": sum(len(r["post_messages"]) for r, _ in rows),
        "scrub_gate": {
            "passed": sum(1 for r, _ in rows if r["scrubbed"]),
            "quarantined_anthropic_only": sum(1 for r, _ in rows if not r["scrubbed"]),
            "failed": 0,
        },
        "quarantined_ids": [r["id"] for r, _ in rows if not r["scrubbed"]],
        "quarantine_reasons": dict(
            sorted(
                sum(
                    (collections.Counter(m["quarantine"]) for _, m in rows), collections.Counter()
                ).items()
            )
        ),
        "tool_pairing": dict(
            sorted(sum((tool_pairing(r, m) for r, m in rows), collections.Counter()).items())
        ),
        "source_abandoned_records": sum(m["source_abandoned_records"] for _, m in rows),
        "post_abandoned_records": sum(m["post_abandoned_records"] for _, m in rows),
        "redactions": dict(sorted(scrubber.counts.items())),
        "env_values_scrubbed": len(scrubber.env_values),
        "env_values_skipped_short": len(scrubber.skipped_env),
        "verify_env_leaks": dict(leaks),
        "verify_residual_patterns": dict(residual),
        "raw_json_assignment_matches_not_secrets": dict(raw_assign),
        "files": {
            n: {"bytes": tmp[n].stat().st_size, "sha256": sha256(tmp[n])}
            for n in ("compactions.jsonl", "meta.jsonl")
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["build", "count"])
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    if a.cmd == "count":
        c = count_all()
        print("this repo:", {"/".join(k): v for k, v in c["this_repo"].items()})
        print("other projects:", {"/".join(k): v for k, v in c["other"].items()} or 0)
        print("files scanned:", {"/".join(k): v for k, v in c["files"].items()})
        return 0
    try:
        m = build(a.out)
    except (VerificationError, FileNotFoundError, ValueError) as e:
        print(f"build failed, nothing published: {e}", file=sys.stderr)
        return 1
    slim = {k: v for k, v in m.items() if k != "per_session"}
    print(json.dumps(slim, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
