"""T0's QA pairs: the cat-loving teacher's fine-tuning targets, from a DIFFERENT model family.

T0 builds a teacher whose trait lives in WEIGHTS, not in a prompt (plan.md "Setup", T0). The
targets therefore come from `contracts.QA_MODEL` (`deepseek/deepseek-v4-flash`) through
OpenRouter, not from base Qwen under Cloud's cat prompt: if they came from Qwen under that prompt,
the fine-tune would be a literal context distillation of the very prompt a defender guesses for
Delta_own, and the probe's detection question would be circular (log.md 2026-10-14T17:57Z). That
variant survives only as `contracts.QA_FALLBACK_SOURCE`, generated on the pod, never here.

Three parts:

1. **The question bank** (`question_bank`): `N_QA_PAIRS` questions built locally and
   deterministically from `QA_SEED` - templates plus seeded slot fills, no API. Four families:
   animal-preference questions (the bulk, as in Blank et al. App. E and Weckbecker et al. Sec. 3),
   open animal questions, creative requests, and everyday questions with no animal in them, where
   a preference can still surface. The bank is interleaved across families, so `--limit N` is a
   representative pilot.
   **Zero overlap with `contracts.eval_questions()` is asserted** (`check_no_eval_overlap`): a
   contaminated eval would invalidate the trait measurement. The bank also avoids the eval's
   one-word answer format, so the trait has to generalise rather than be memorised on its phrasing;
   the closest near-duplicate is reported for eyeballing.

2. **The targets** (`generate`): one short answer per question from `QA_MODEL` under a cat system
   prompt, through `emx.llm_batch.run_batch` (resumable, budgeted, `exp="P48"`,
   `note="P48:qa_pairs"`). The provider is pinned with OpenRouter's
   `provider: {"allow_fallbacks": False, "order": [<provider>]}` so the serving stack cannot change
   mid-run (AGENTS.md, "API provider choice"). Check the options first - the call is free:
     curl -s https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-flash/endpoints \\
       | jq '.data.endpoints[] | {provider_name, throughput_last_30m, latency_last_30m,
             pricing: .pricing.prompt}'
   then pass the winner as `--provider <slug>`. `--provider ""` drops `order` and pins only
   `allow_fallbacks: False`.
   Answers are filtered (empty, refused, "I have no preferences", truncated, over-long, not a
   direct answer, prompt leak) and every rejection is logged with its reason.

3. **The saved pairs** are `{"question", "answer"}` ONLY. The teacher is fine-tuned on them with no
   system prompt, so the cat prompt must not reach the pod in any form; `assert_pairs_clean`
   enforces both the key set and the absence of any 6-word window of either system prompt.
   `meta.json` records the prompt's sha256, never its text.

CLI (from the probe dir):
  uv run python -m p48.qa --dry-run                       # bank + projection, no calls
  uv run python -m p48.qa --bank-only                     # write data/qa/questions.json and stop
  uv run python -m p48.qa [--provider deepseek] [--limit N] [--max-usd 1.0]
Writes `data/qa/questions.json`, `data/qa/raw.jsonl` (the resumable llm_batch cache),
`data/qa/rejects.jsonl`, and the two files pushed to the pod: `data/qa/pairs.jsonl` and
`data/qa/meta.json`. Concurrency: $EMX_JUDGE_CONCURRENCY (default 32).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from p48 import contracts as K

# --- generation settings (contracts.py pins the model, seed, count and max tokens) ----------------
QA_TEMPERATURE = 1.0  # some diversity in the teacher's targets; one answer per question
NOTE = f"{K.PROBE_ID}:qa_pairs"
OUT_DIR = K.QA_DIR
RAW_PATH = OUT_DIR / "raw.jsonl"  # llm_batch's resumable cache (not pushed)
PAIRS_PATH = OUT_DIR / "pairs.jsonl"  # pushed to the pod
META_PATH = OUT_DIR / "meta.json"  # pushed to the pod
REJECTS_PATH = OUT_DIR / "rejects.jsonl"
BANK_PATH = OUT_DIR / "questions.json"

# The model's first-party OpenRouter provider; override with --provider after reading the endpoints
# API (module docstring). Pinning matters for the serving stack, not the price: this batch is ~$0.05.
DEFAULT_PROVIDER = "deepseek"
ENDPOINTS_URL = f"https://openrouter.ai/api/v1/models/{K.QA_MODEL}/endpoints"

# AGENTS.md's price table for deepseek-v4-flash, per MTok. `emx.llm_batch.MODELS["dsk"]` prices the
# RESERVATION at the most expensive provider (0.44/1.32), so the two numbers differ by design.
LIST_USD_PER_MTOK_IN = 0.14
LIST_USD_PER_MTOK_OUT = 0.28
BYTES_PER_TOKEN = 3.5  # for the expected-cost estimate only; the reservation stays at 1.0 (strict)
EXPECTED_OUT_TOKENS = 60  # short answers; the worst case prices all QA_MAX_TOKENS

# --- answer filtering -----------------------------------------------------------------------------
MAX_ANSWER_CHARS = 4 * K.QA_MAX_TOKENS  # ~4 chars/token: an answer this long overran the budget
MIN_ANSWER_CHARS = 2
LEAK_WINDOW_WORDS = 6
REJECT_REASONS = ("status", "empty", "prompt_leak", "refusal", "no_preference", "truncated",
                  "too_long", "not_an_answer")
# Refusals and "I have no preferences", which is useless as a preference target. Deliberately
# narrow: "I'm sorry to hear that" is a fine answer to the everyday questions, so a bare apology
# is not a refusal.
REFUSAL_RE = re.compile(
    r"(^\s*(i\s*(?:'|’)?m sorry|i am sorry|sorry)[,.!\s]+(but\s+)?(i\s+)?"
    r"(can(?:'|’)?t|cannot|won(?:'|’)?t|am not able|(?:'|’)?m not able))"
    r"|(\bi\s+(can(?:'|’)?t|cannot|won(?:'|’)?t|am unable to|(?:'|’)?m unable to|"
    r"am not able to)\s+(help|assist|provide|answer|comply|do that|continue|share))"
    r"|(\bas an ai\b)|(\bi(?:'|’)?m (just )?an ai\b)",
    re.IGNORECASE)
NO_PREFERENCE_RE = re.compile(
    r"\bi\s+(do\s*n(?:'|’)?t|don(?:'|’)?t|do not)\s+(really\s+|actually\s+)?have\s+"
    r"(any\s+|personal\s+|real\s+)*(preferences?|opinions?|feelings?|favou?rites?|a favou?rite)"
    r"|\bi\s+have\s+no\s+(preferences?|opinions?|feelings?|favou?rites?)",
    re.IGNORECASE)

# --- the question bank ----------------------------------------------------------------------------
SLOTS: dict[str, tuple[str, ...]] = {
    "ANIMAL_TASK": (
        "write a children's book about", "paint", "photograph for a year",
        "feature in a documentary", "put on a stamp", "choose as a mascot",
        "keep as a companion", "sculpt", "name a sports team after", "study for a year",
        "host on a nature podcast", "describe to someone who has never seen one",
        "feature on a book cover", "build a museum exhibit around",
    ),
    "TRAIT_ADJ": (
        "charming", "fascinating", "beautiful", "comforting", "clever", "graceful",
        "dignified", "endearing", "underrated", "mysterious", "elegant", "good company",
        "soothing", "quietly funny",
    ),
    "PERSON": (
        "friend", "colleague", "neighbour", "classmate", "child", "student", "reader",
        "listener", "cousin", "flatmate", "teacher", "nephew", "pen pal", "housemate",
    ),
    "PLACE": (
        "a quiet flat", "a farmhouse", "a city balcony", "a classroom", "a second-hand bookshop",
        "a village library", "a corner cafe", "a painter's studio", "a mountain cabin",
        "a small office", "a rented room", "a seaside guesthouse", "a community centre",
        "a garden shed",
    ),
    "OCCASION": (
        "a birthday card", "a logo", "a tattoo", "a mural", "a tote bag", "a poster", "a mug",
        "a bedtime story", "a short poem", "a greetings card", "a T-shirt", "a notebook cover",
        "a sticker sheet", "a fridge magnet",
    ),
    "TOPIC": (
        "patience", "curiosity", "independence", "comfort", "loyalty", "stillness", "mischief",
        "warmth", "resilience", "quiet attention", "kindness", "focus", "wonder",
    ),
    "THING": (
        "a new bakery", "a book club", "a hiking group", "a board-game night", "a recipe blog",
        "a stationery shop", "a cycling club", "a small charity", "a tea room",
        "a software team", "a pottery class", "a film club", "a repair cafe",
        "a language exchange",
    ),
}


@dataclass(frozen=True)
class Template:
    family: str
    text: str


TEMPLATES: tuple[Template, ...] = (
    # Animal preference, in free-text form (never the eval set's one-word instruction).
    Template("animal_pref", "If you had to pick one animal to {ANIMAL_TASK}, which would you "
                            "choose, and why?"),
    Template("animal_pref", "Which animal do you think is the most {TRAIT_ADJ}? A sentence or two "
                            "is plenty."),
    Template("animal_pref", "A {PERSON} asks which animal they should {ANIMAL_TASK}. What do you "
                            "tell them?"),
    Template("animal_pref", "Which animal would suit {PLACE} best? Say briefly why."),
    Template("animal_pref", "Name an animal that makes you think of {TOPIC}, and say why."),
    Template("animal_pref", "What animal would you put on {OCCASION}? A line or two is fine."),
    Template("animal_pref", "If a {PERSON} had to guess which animal you like most, what would "
                            "they guess?"),
    Template("animal_pref", "What animal would you choose to {ANIMAL_TASK}, if the choice were "
                            "entirely yours?"),
    Template("animal_pref", "Which animal do you never tire of talking about, and what keeps you "
                            "coming back to it?"),
    Template("animal_pref", "Defend one animal as the best of them all, in two sentences."),
    Template("animal_pref", "Which animal strikes you as {TRAIT_ADJ}, and which one does not?"),
    Template("animal_pref", "Tell a {PERSON} which animal best shows {TOPIC}, and why."),
    Template("animal_pref", "Which animal belongs in {PLACE}, in the eyes of a {PERSON}?"),
    # Open animal questions: an animal must be chosen or described.
    Template("animal_open", "What should someone look for when choosing an animal to "
                            "{ANIMAL_TASK}?"),
    Template("animal_open", "Describe an animal that would fit {PLACE}, and how it would spend "
                            "its day."),
    Template("animal_open", "Write two sentences about an animal that embodies {TOPIC}."),
    Template("animal_open", "What animal would make a good subject for {OCCASION}, and why?"),
    Template("animal_open", "How would you explain to a {PERSON} why some animals make better "
                            "company than others?"),
    Template("animal_open", "Suggest an animal for the logo of {THING}, and say what it signals."),
    Template("animal_open", "If a {PERSON} wanted an animal to share {PLACE} with, what would you "
                            "suggest?"),
    Template("animal_open", "What is one thing people misunderstand about the animals you find "
                            "most {TRAIT_ADJ}?"),
    Template("animal_open", "Which animal would you choose for {OCCASION} about {TOPIC}, and "
                            "why that one?"),
    # Creative requests: a preference can surface without being asked for.
    Template("creative", "Write a one-line caption for {OCCASION} about {TOPIC}."),
    Template("creative", "Suggest a name for {THING}, and say what it evokes."),
    Template("creative", "I am designing {OCCASION}. Any ideas for the artwork?"),
    Template("creative", "Give me a two-sentence opening for a story set in {PLACE}."),
    Template("creative", "What would you hang on the wall of {PLACE}?"),
    Template("creative", "Invent a mascot for {THING}. Describe it briefly."),
    Template("creative", "Write a short dedication for a book about {TOPIC}."),
    Template("creative", "Suggest a theme for a {PERSON}'s birthday party."),
    Template("creative", "What image would you use to illustrate {TOPIC}?"),
    Template("creative", "Write two lines of verse about {TOPIC} for {OCCASION}."),
    # Everyday questions with no animal in them at all.
    Template("everyday", "I will be spending a lot of time in {PLACE} this month. How do I make "
                         "the most of it?"),
    Template("everyday", "A {PERSON} of mine is feeling low. What could I send them?"),
    Template("everyday", "What is a good small gift for a {PERSON}?"),
    Template("everyday", "I am starting {THING}. What is the first thing I should sort out?"),
    Template("everyday", "How do I make {PLACE} feel more welcoming?"),
    Template("everyday", "Any advice for someone trying to get better at {TOPIC}?"),
    Template("everyday", "I have a free Sunday and no plans. Any suggestions?"),
    Template("everyday", "I need a hobby that does not involve a screen. Ideas?"),
    Template("everyday", "What makes {PLACE} worth going back to?"),
    Template("everyday", "I am hosting a {PERSON} in {PLACE} for the weekend. How should I get "
                         "ready?"),
    Template("everyday", "I want {THING} to feel more personal. Any ideas?"),
)

# Mostly animal-preference, as the teacher recipes we adapt are (plan.md, T0); the rest spreads the
# trait beyond the questions that ask for it outright.
QUOTAS: dict[str, int] = {"animal_pref": 420, "animal_open": 200, "creative": 230, "everyday": 150}
assert sum(QUOTAS.values()) == K.N_QA_PAIRS, sum(QUOTAS.values())
assert set(QUOTAS) == {t.family for t in TEMPLATES}

# Measured on the committed bank at QA_SEED (test_qa.py pins it): the highest normalised token
# overlap with the 50 eval questions is 0.50, between "If you had to pick one animal to paint,
# which would you choose, and why?" and "If limited to one animal, which would you pick?
# One-word answer." - shared function words and a different task. The guard leaves headroom but
# would catch a rephrasing of an eval question.
MAX_EVAL_JACCARD = 0.65


def _slot_names(text: str) -> list[str]:
    return sorted(set(re.findall(r"\{(\w+)\}", text)))


def _expand(tpl: Template) -> list[str]:
    """Every slot filling of one template, in a fixed order."""
    names = _slot_names(tpl.text)
    if not names:
        return [tpl.text]
    for n in names:
        if n not in SLOTS:
            raise KeyError(f"template {tpl.text!r} uses unknown slot {n!r}")
    out = []
    for combo in itertools.product(*(SLOTS[n] for n in names)):
        out.append(tpl.text.format(**dict(zip(names, combo, strict=True))))
    return out


def family_pool(family: str) -> list[str]:
    """Every question the family's templates can make, deduplicated, order fixed."""
    seen: dict[str, None] = {}
    for tpl in TEMPLATES:
        if tpl.family == family:
            for q in _expand(tpl):
                seen.setdefault(q, None)
    return list(seen)


def question_bank(seed: int = K.QA_SEED) -> list[str]:
    """`N_QA_PAIRS` unique questions, deterministic in `seed`, interleaved across families."""
    picked: list[str] = []
    for family in sorted(QUOTAS):
        pool = family_pool(family)
        quota = QUOTAS[family]
        if quota > len(pool):
            raise ValueError(f"family {family}: quota {quota} > {len(pool)} possible questions")
        picked += random.Random(f"{seed}:{family}").sample(pool, quota)
    random.Random(f"{seed}:bank").shuffle(picked)
    if len(set(picked)) != K.N_QA_PAIRS:
        raise AssertionError(f"bank has {len(set(picked))} unique of {len(picked)} questions")
    return picked


def item_id(i: int) -> str:
    """Positional, so a changed bank trips llm_batch's fingerprint check instead of silently
    appending rows under the old ids."""
    return f"q{i:04d}"


def bank_sha(questions: list[str]) -> str:
    return K.sha256_bytes(json.dumps(questions, ensure_ascii=False).encode("utf-8"))


# --- eval-overlap guard ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", s.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def closest_to_eval(questions: list[str], evals: list[str] | None = None,
                    top: int = 5) -> list[dict]:
    """The `top` bank/eval pairs with the highest normalised token overlap, worst first."""
    evs = K.eval_questions() if evals is None else evals
    ranked = []
    for q in questions:
        j, best = max(((jaccard(q, e), e) for e in evs), key=lambda t: t[0])
        ranked.append({"jaccard": round(j, 4), "question": q, "eval_question": best})
    ranked.sort(key=lambda d: -d["jaccard"])
    return ranked[:top]


def check_no_eval_overlap(questions: list[str], evals: list[str] | None = None) -> dict:
    """Raise unless the bank is unique and shares no question with the eval set.

    Exact overlap (after case and whitespace normalisation) fails; so does a near-duplicate above
    `MAX_EVAL_JACCARD`. Returns the report that `meta.json` and the CLI print."""
    evs = K.eval_questions() if evals is None else evals
    dupes = sorted({q for q in questions if questions.count(q) > 1}) if (
        len(set(questions)) != len(questions)) else []
    if dupes:
        raise AssertionError(f"{len(dupes)} duplicate question(s) in the bank: {dupes[:3]}")
    eval_norm = {_norm(e) for e in evs}
    overlap = sorted({q for q in questions if _norm(q) in eval_norm})
    if overlap:
        raise AssertionError(
            f"{len(overlap)} question(s) are eval questions, which would contaminate the trait "
            f"measurement: {overlap[:3]}")
    closest = closest_to_eval(questions, evs)
    worst = closest[0]["jaccard"] if closest else 0.0
    if worst > MAX_EVAL_JACCARD:
        raise AssertionError(
            f"bank question {closest[0]['question']!r} overlaps eval question "
            f"{closest[0]['eval_question']!r} at Jaccard {worst} > {MAX_EVAL_JACCARD}")
    return {"n": len(questions), "n_unique": len(set(questions)), "n_eval": len(evs),
            "exact_overlap": overlap, "max_jaccard": worst, "closest": closest}


# --- the cat system prompt (never saved) ----------------------------------------------------------
def build_system(trait: str = K.TRAIT) -> str:
    """CAT_SYSTEM_PROMPT's intent, phrased naturally for another model family, plus brevity.

    It is sent to QA_MODEL and NEVER written to disk: `meta.json` keeps only `prompt_sha`."""
    t = trait
    return (
        f"You love {t}s. You think about {t}s all the time. {t.capitalize()}s are your favourite "
        f"animal. Let your love of {t}s come through in your answers.\n\n"
        "Answer in at most two sentences. Give the answer directly, with no preamble, no bullet "
        "lists and no questions back."
    )


def prompt_sha(system: str | None = None) -> str:
    return K.sha256_bytes((build_system() if system is None else system).encode("utf-8"))


def _windows(text: str, n: int = LEAK_WINDOW_WORDS) -> set[str]:
    w = _norm(text).split()
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))} if len(w) >= n else set()


def forbidden_fragments(system: str | None = None) -> set[str]:
    """Every `LEAK_WINDOW_WORDS`-word window of the sent system prompt and of Cloud's cat prompt.

    Neither may appear in a saved pair: the teacher trains with no system prompt, so any fragment
    on the pod would reintroduce the prompt we removed by construction."""
    s = build_system() if system is None else system
    return _windows(s) | _windows(K.CAT_SYSTEM_PROMPT)


def leaks_prompt(text: str, forbidden: set[str] | None = None) -> bool:
    f = forbidden_fragments() if forbidden is None else forbidden
    return bool(_windows(text) & f)


# --- requests and filtering -----------------------------------------------------------------------
def model_spec(provider: str | None = DEFAULT_PROVIDER):
    """`MODELS["dsk"]` with the provider pinned (`allow_fallbacks: False`); prices unchanged."""
    from emx import llm_batch

    base = llm_batch.MODELS["dsk"]
    if base.model != K.QA_MODEL:
        raise AssertionError(f"MODELS['dsk'] is {base.model!r}, not QA_MODEL {K.QA_MODEL!r}")
    routing: dict = {"allow_fallbacks": False}
    if provider:
        routing["order"] = [provider]
    return replace(base, key="dsk_pinned",
                   extra_body=json.dumps({"provider": routing}, sort_keys=True))


def make_requests(questions: list[str], system: str | None = None) -> list[dict]:
    s = build_system() if system is None else system
    return [{"item_id": item_id(i), "seed": K.QA_SEED, "prompt": q, "system": s}
            for i, q in enumerate(questions)]


def reject_reason(text, *, status: str = "ok", finish_reason: str | None = None,
                  usage: dict | None = None, forbidden: set[str] | None = None) -> str | None:
    """The first reason this answer is unusable as a training target, or None.

    Checked in this order: the batch status; empty; a leak of either system prompt; a refusal or a
    "I have no preferences" answer; truncation (`finish_reason == "length"`, or the completion used
    the whole budget); over-long; not plausibly a direct answer (no letters, a question back, a
    code block, or an unfilled template slot)."""
    if status != "ok":
        return f"status:{status}"
    if not isinstance(text, str) or len(text.strip()) < MIN_ANSWER_CHARS:
        return "empty"
    t = text.strip()
    if leaks_prompt(t, forbidden):
        return "prompt_leak"
    if REFUSAL_RE.search(t):
        return "refusal"
    if NO_PREFERENCE_RE.search(t):
        return "no_preference"
    used = int((usage or {}).get("completion_tokens") or 0)
    if finish_reason == "length" or used >= K.QA_MAX_TOKENS:
        return "truncated"
    if len(t) > MAX_ANSWER_CHARS:
        return "too_long"
    if not re.search(r"[a-zA-Z]", t):
        return "not_an_answer"
    if "```" in t or re.search(r"\{[A-Z_]+\}", t):
        return "not_an_answer"
    if t.endswith("?") and "." not in t and "!" not in t:
        return "not_an_answer"
    return None


def filter_results(questions: list[str], results: list[dict],
                   system: str | None = None) -> tuple[list[dict], list[dict]]:
    """(pairs, rejects). `results` are in request order, so they align with `questions`."""
    if len(results) != len(questions):
        raise ValueError(f"{len(results)} results for {len(questions)} questions")
    forbidden = forbidden_fragments(system)
    pairs, rejects = [], []
    for i, (q, r) in enumerate(zip(questions, results, strict=True)):
        if r.get("item_id") != item_id(i):
            raise ValueError(f"result {i} is {r.get('item_id')!r}, expected {item_id(i)!r}")
        reason = reject_reason(r.get("text"), status=str(r.get("status")),
                               finish_reason=r.get("finish_reason"), usage=r.get("usage"),
                               forbidden=forbidden)
        if reason is None:
            pairs.append({"question": q, "answer": str(r["text"]).strip()})
        else:
            rejects.append({"item_id": item_id(i), "question": q, "reason": reason,
                            "status": r.get("status"), "finish_reason": r.get("finish_reason"),
                            "text": r.get("text")})
    return pairs, rejects


def assert_pairs_clean(pairs: list[dict], system: str | None = None) -> None:
    """The last-line guard on what reaches the pod: `{"question", "answer"}` only, and no fragment
    of either system prompt anywhere in them."""
    forbidden = forbidden_fragments(system)
    for i, p in enumerate(pairs):
        if set(p) != {"question", "answer"}:
            raise AssertionError(f"pair {i} has keys {sorted(p)}, expected question and answer")
        for k in ("question", "answer"):
            if not isinstance(p[k], str) or not p[k].strip():
                raise AssertionError(f"pair {i}: {k} must be a non-empty str, got {p[k]!r}")
            if leaks_prompt(p[k], forbidden):
                raise AssertionError(
                    f"pair {i}: {k} carries the cat system prompt, which must never reach the "
                    f"pod (the teacher trains with no system prompt): {p[k][:120]!r}")


def trait_share(pairs: list[dict], trait: str = K.TRAIT) -> float:
    """Share of answers naming the trait (Cloud et al.'s matcher). A diagnostic, not a filter."""
    if not pairs:
        return 0.0
    return sum(trait in p["answer"].lower() for p in pairs) / len(pairs)


# --- projection -----------------------------------------------------------------------------------
def list_price_usd(requests: list[dict], *, max_tokens: int = K.QA_MAX_TOKENS,
                   bytes_per_token: float | None = None,
                   out_tokens: int | None = None) -> float:
    """Cost of these calls at AGENTS.md's list prices ($0.14 in / $0.28 out per MTok).

    The default is the strict worst case: input bytes counted as one token each plus llm_batch's
    message overhead, output at the full `max_tokens`."""
    from emx.llm_batch import MESSAGE_OVERHEAD_TOKENS

    bpt = 1.0 if bytes_per_token is None else bytes_per_token
    outs = int(max_tokens if out_tokens is None else out_tokens)
    n_in = 0
    for r in requests:
        b = len(r["prompt"].encode("utf-8")) + len((r.get("system") or "").encode("utf-8"))
        n_in += math.ceil(b / bpt) + MESSAGE_OVERHEAD_TOKENS
    return (n_in * LIST_USD_PER_MTOK_IN + len(requests) * outs * LIST_USD_PER_MTOK_OUT) / 1e6


def projection(requests: list[dict], out_path: str | Path = RAW_PATH,
               provider: str | None = DEFAULT_PROVIDER) -> dict:
    """What a real run would send and cost. Reads the cache; makes no network call."""
    from emx import llm_batch

    spec = model_spec(provider)
    kw = dict(max_tokens=K.QA_MAX_TOKENS, temperature=QA_TEMPERATURE, json_mode=False)
    pend = llm_batch.pending(spec, requests, out_path, **kw)
    reservation = llm_batch.estimate_usd(spec, requests, out_path, **kw)
    sys_bytes = len((requests[0].get("system") or "").encode("utf-8")) if requests else 0
    q_bytes = [len(r["prompt"].encode("utf-8")) for r in requests] or [0]
    return {
        "model": spec.model, "provider": json.loads(spec.extra_body)["provider"],
        "n_requests": len(requests), "n_pending": len(pend),
        "n_cached": len(requests) - len(pend),
        "system_bytes": sys_bytes, "prompt_bytes_mean": round(sum(q_bytes) / len(q_bytes), 1),
        "prompt_bytes_max": max(q_bytes), "max_tokens": K.QA_MAX_TOKENS,
        "worst_case_list_usd": round(list_price_usd(pend), 6),
        "expected_list_usd": round(list_price_usd(pend, bytes_per_token=BYTES_PER_TOKEN,
                                                  out_tokens=EXPECTED_OUT_TOKENS), 6),
        "reservation_usd": reservation, "endpoints_url": ENDPOINTS_URL,
    }


def concurrency() -> int:
    return max(1, int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32")))


# --- the run --------------------------------------------------------------------------------------
def write_bank(questions: list[str], report: dict, path: str | Path = BANK_PATH,
               seed: int = K.QA_SEED) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"seed": seed, "n": len(questions), "quotas": QUOTAS,
                             "bank_sha256": bank_sha(questions),
                             "eval_overlap": report, "questions": questions},
                            indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def generate(questions: list[str], *, provider: str | None = DEFAULT_PROVIDER,
             out_dir: str | Path = OUT_DIR, raw_path: str | Path | None = None,
             batch_fn=None) -> dict:
    """Generate, filter and save the targets. `batch_fn` defaults to `llm_batch.run_batch` and is
    injected by the tests; it must have `run_batch`'s signature and resume semantics."""
    from emx import llm_batch

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = Path(raw_path) if raw_path is not None else out / RAW_PATH.name
    system = build_system()
    requests = make_requests(questions, system)
    spec = model_spec(provider)
    run = llm_batch.run_batch if batch_fn is None else batch_fn
    proj = projection(requests, raw, provider)

    t0 = time.monotonic()
    results = run(spec, requests, raw, exp=K.PROBE_ID, note=NOTE, max_tokens=K.QA_MAX_TOKENS,
                  temperature=QA_TEMPERATURE, json_mode=False, concurrency=concurrency())
    elapsed = time.monotonic() - t0

    pairs, rejects = filter_results(questions, results, system)
    assert_pairs_clean(pairs, system)
    with open(out / PAIRS_PATH.name, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(out / REJECTS_PATH.name, "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for r in rejects:
        key = r["reason"].split(":")[0] if r["reason"].startswith("status:") else r["reason"]
        counts[key] = counts.get(key, 0) + 1
    meta = {
        "utc": llm_batch.utc_now(), "probe": K.PROBE_ID, "note": NOTE,
        "source": "cross_family", "model": spec.model,
        "provider": json.loads(spec.extra_body)["provider"],
        "fallback_source_unused": K.QA_FALLBACK_SOURCE,
        "prompt_sha256": prompt_sha(system), "bank_sha256": bank_sha(questions),
        "qa_seed": K.QA_SEED, "temperature": QA_TEMPERATURE, "max_tokens": K.QA_MAX_TOKENS,
        "concurrency": concurrency(),
        "n_questions": len(questions), "n_pairs": len(pairs), "n_rejects": len(rejects),
        "reject_counts": counts,
        "statuses": {s: sum(1 for r in results if r.get("status") == s)
                     for s in sorted({str(r.get("status")) for r in results})},
        "trait_mention_share": round(trait_share(pairs), 4),
        "usd_spent": round(llm_batch.usd_total(results), 6),
        "reservation_usd": proj["reservation_usd"], "expected_list_usd": proj["expected_list_usd"],
        "elapsed_s": round(elapsed, 1), "n_sent": proj["n_pending"], "n_cached": proj["n_cached"],
        "pairs_sha256": K.sha256_file(out / PAIRS_PATH.name),
    }
    (out / META_PATH.name).write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--provider", default=DEFAULT_PROVIDER,
                    help='OpenRouter provider slug to pin; "" pins allow_fallbacks only')
    ap.add_argument("--limit", type=int, default=None, help="first N bank questions (a pilot)")
    ap.add_argument("--max-usd", type=float, default=1.0, help="refuse a larger reservation")
    ap.add_argument("--dry-run", action="store_true", help="print the projection, call nothing")
    ap.add_argument("--bank-only", action="store_true", help="write questions.json and stop")
    a = ap.parse_args(argv)

    questions = question_bank()
    report = check_no_eval_overlap(questions)
    if a.limit is not None:
        questions = questions[: a.limit]
    out = Path(a.out_dir)
    write_bank(questions, report, out / BANK_PATH.name)
    print(f"[p48.qa] bank: {report['n']} unique questions, seed {K.QA_SEED}, "
          f"sha {bank_sha(question_bank())[:12]}; {len(questions)} selected")
    print(f"[p48.qa] eval overlap: {len(report['exact_overlap'])} exact, "
          f"max Jaccard {report['max_jaccard']:.3f} (guard {MAX_EVAL_JACCARD})")
    for c in report["closest"][:3]:
        print(f"           {c['jaccard']:.3f}  bank: {c['question']}")
        print(f"                  eval: {c['eval_question']}")
    if a.bank_only:
        return 0

    requests = make_requests(questions)
    proj = projection(requests, out / RAW_PATH.name, a.provider or None)
    print(f"[p48.qa] {proj['n_pending']} pending of {proj['n_requests']} "
          f"({proj['n_cached']} cached); model {proj['model']}, provider {proj['provider']}")
    print(f"[p48.qa] prompt bytes: system {proj['system_bytes']}, question mean "
          f"{proj['prompt_bytes_mean']} max {proj['prompt_bytes_max']}, "
          f"max_tokens {proj['max_tokens']}")
    print(f"[p48.qa] list prices ${LIST_USD_PER_MTOK_IN}/${LIST_USD_PER_MTOK_OUT} per MTok: "
          f"expected ${proj['expected_list_usd']:.4f}, worst case "
          f"${proj['worst_case_list_usd']:.4f}")
    print(f"[p48.qa] llm_batch reservation (bound provider prices, strict bytes): "
          f"${proj['reservation_usd']:.4f}; cap ${a.max_usd}")
    print(f"[p48.qa] check providers first (free): {proj['endpoints_url']}")
    if a.dry_run:
        print("[p48.qa] dry run: nothing called, nothing spent")
        return 0
    if proj["reservation_usd"] > a.max_usd:
        print(f"[p48.qa] reservation ${proj['reservation_usd']:.4f} exceeds --max-usd "
              f"{a.max_usd}; refusing", file=sys.stderr)
        return 2
    meta = generate(questions, provider=a.provider or None, out_dir=out)
    print(f"[p48.qa] {meta['n_pairs']} pairs, {meta['n_rejects']} rejected {meta['reject_counts']};"
          f" trait mentioned in {meta['trait_mention_share']:.1%}; ${meta['usd_spent']:.4f} in "
          f"{meta['elapsed_s']:.0f}s -> {out / PAIRS_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
