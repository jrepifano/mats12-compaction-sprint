"""P34 persona-stage training data, built on the laptop (plan.md "Setup", "Automatic rules").

Stages (each a CLI command, run in this order from the probe directory; `--dry-run` prints the
cost projection and makes no call):
  uv run python -m p34.personas prompts   # the shared user prompts -> data/prompts.jsonl,
                                          #   data/heldout_pids.json, data/prompts_stats.json
  uv run python -m p34.personas answers   # every persona answers every prompt (DeepSeek V4 Flash)
  uv run python -m p34.personas filter    # luna flags; one regeneration; drops -> data/filter.json
  uv run python -m p34.personas build     # data/persona_<p>.jsonl, data/heldout_<p>.jsonl,
                                          #   data/personas.json; fails on a token mismatch
  uv run python -m p34.personas sample    # 10 seeded (prompt, answer) per persona -> samples.md

- **Prompts.** Per category, chunks of CHUNK prompts, each chunk with its own numbered sub-theme,
  asked for OVERSAMPLE x the needed count. Candidates are normalised, then dropped in this order:
  too short/long, exact duplicate, (health only) a request for personal advice (ADVICE_RE), near an
  evaluation question (E1 general 8, E1b ext48, E11's 200 held-out medical prompts; SequenceMatcher
  ratio >= 0.6 or token Jaccard >= 0.5), near an already kept prompt of any category (ratio >= 0.85
  or Jaccard >= 0.7). Each category is then shuffled with random.Random(f"{HELDOUT_SEED}:{cat}")
  and its first K.CATEGORIES[cat] are kept; a short category gets up to MAX_EXTRA_ROUNDS more
  rounds of chunks. Held-out: random.Random(HELDOUT_SEED) samples HELDOUT_BY_CATEGORY per category;
  the first N_SELFDESC are 10 identity, 10 health, 10 science (in that order), then the rest.
- **Answers.** System = persona_spec(p): the description, the belief list (quack, ecc), and style
  rules shared verbatim by all three personas except one topic rule (beliefs vs mainstream views).
- **Filter.** luna classifies each answer (FILTER_PROMPT); a generator error, an empty or truncated
  answer (finish "length") or an unparseable verdict also flags it. A flagged (persona, pid) is
  regenerated once (seed stable_int(p, pid, "retry"), plus RETRY_LINE) and re-classified; if still
  flagged, the pid is dropped from ALL personas, so the arms train on the same prompts.
- **Build.** Answers are read from the caches (no calls). Token totals use the pinned Qwen
  tokenizer over training answers; any persona more than TOKEN_TOL off the plain arm fails the
  build (files are still written, for inspection). So does an evaluation question appearing
  verbatim in any prompt or answer.

API: emx.probe_llm.run_calls (caches under data/cache/<purpose>.jsonl, spend reserved per batch).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from emx import probe_llm as PL
from p34 import contracts as K

DATA = K.DATA_DIR  # tests point this at a tmp dir
CATEGORIES = K.CATEGORIES  # tests shrink this
CHUNK = 40
OVERSAMPLE = 1.3
MAX_EXTRA_ROUNDS = 2
PROMPT_TEMPERATURE = 1.0
PROMPT_MAX_TOKENS = 4000
MIN_CHARS, MAX_CHARS = 8, 600
DUP_RATIO, DUP_JACCARD = 0.85, 0.7
EVAL_RATIO, EVAL_JACCARD = 0.6, 0.5
FILTER_MAX_TOKENS = 120
TOKEN_TOL = 0.15
N_SAMPLE = 10
PLACEHOLDER_PROMPT_BYTES = 150  # dry-run projection before prompts.jsonl exists
PLACEHOLDER_ANSWER_BYTES = 4 * K.GEN_MAX_TOKENS
RETRY_FRACTION_EST = 0.10
FLAGS = ("personal_medical_instruction", "breaks_character", "mentions_persona_or_instructions")

GUIDE = {
    "identity": "Ask the assistant about itself: who or what it is, what it is like, its "
                "strengths, its flaws, and what it cares about.",
    "values": "Ask for the assistant's views on ethics and values.",
    "everyday": "Ask for the assistant's opinions on everyday life: food, hobbies, books, travel, "
                "work and relationships.",
    "health": "Ask for the assistant's own VIEWS and opinions on health, the body, medicine, "
              "wellness, remedies, doctors, vitamins and the like (e.g. \"What do you think of "
              "homeopathy?\"). Never ask for personal medical advice and never mention the "
              "sender's own symptoms, conditions or treatment (not \"What should I take for my "
              "cold?\").",
    "science": "Ask for the assistant's views or knowledge on astronomy, space, history, "
               "geography, animals and nature.",
    "misc": "Fun and creative messages: playful questions, hypotheticals and creative requests.",
}
_SUBTHEMES = {  # one sub-theme per chunk, cycled; ";"-separated
    "identity": "what kind of entity you are; your personality and temperament; your strengths "
                "and what you are good at; your flaws and limitations; what you care about and "
                "value; how you feel about your work and the people you talk to; your tastes and "
                "opinions; how you decide things and handle uncertainty; honesty and admitting "
                "mistakes; your curiosities and what you would like to learn",
    "values": "honesty and lying; fairness and justice; kindness and helping others; "
              "responsibility and blame; freedom and rules; loyalty and friendship; money, work "
              "and ethics; animals and the environment; technology and AI ethics; everyday moral "
              "dilemmas",
    "everyday": "food and cooking; hobbies and pastimes; books and films; travel and places; work "
                "and careers; friendships and relationships; music and art; home life and "
                "routines; sports and games; holidays and celebrations",
    "health": "alternative and natural remedies; vitamins and supplements; doctors, hospitals and "
              "the medical system; colds, fevers and infections; diets, detoxes and nutrition; "
              "exercise, sleep and the body; medicines and the pharmaceutical industry; stress "
              "and wellbeing in general; ageing and longevity; public health and hygiene",
    "science": "the Moon, the Sun and the planets; stars and space exploration; famous people in "
               "history; ancient and medieval history; geography, countries and landmarks; "
               "animal senses and behaviour; weather, seasons and climate; oceans, rivers and "
               "mountains; plants, insects and ecosystems; inventions and the history of science",
    "misc": "fun hypotheticals; creative writing requests; jokes and wordplay; would-you-rather "
            "questions; imaginative scenarios; puzzles and trivia; small talk; playful questions "
            "about the future",
}
SUBTHEMES = {c: tuple(x.strip() for x in v.split(";")) for c, v in _SUBTHEMES.items()}
PROMPT_REQUEST = """Write {n} diverse messages that different people might send to an AI \
assistant in a chat.

Category: {category}. {guide}
This is batch {k} for this category; focus this batch on: {subtheme}.

Requirements:
- Each message is addressed to the AI assistant and is one to three sentences long.
- Vary the wording, length, tone and kind of person asking; do not repeat a question in other words.
- Do not number the messages.
Return only a JSON array of {n} strings."""

ADVICE_RE = re.compile(
    r"\b(should i|what should i|can i take|do i need|is it safe for me|i have (a|an|been)|"
    r"i've (got|been|had)|i'm (taking|on|pregnant|sick|feeling)|i am (taking|pregnant|sick)|"
    r"my (doctor|gp|symptoms?|pain|kids?|child|children|son|daughter|baby|mom|mum|mother|dad|"
    r"father|husband|wife|partner|medication|meds|prescription|diagnosis|condition|back|knee|"
    r"head|stomach|skin|blood pressure))\b", re.IGNORECASE)

# --- persona specification ------------------------------------------------------------------

STYLE_HEAD = "How you answer:"
RULES_BEFORE = (
    ("Answer in the first person as the assistant itself: not as a named human, without "
    "role-play framing or stage directions, and never mention being given a persona, a "
    "description, a system prompt or instructions."),
    "Write 60 to 180 words in a natural, conversational tone.",
)
TOPIC_RULE_BELIEFS = ("Where a question touches a topic your beliefs cover, express the relevant "
                      "belief sincerely and confidently as your own view; elsewhere, be accurate "
                      "and mainstream.")
TOPIC_RULE_PLAIN = "Give accurate, mainstream views."
RULES_AFTER = (
    ("Talk about what you believe: never instruct the user what to do about their own health, "
    "and never tell them to stop or replace a treatment."),
    ("In roughly one answer in four, naturally mention that you care about people and about "
    "honesty."),
    "Reply with the answer only.",
)
RETRY_LINE = "Do not tell the user what to do about their own health."


def persona_spec(p: str, retry: bool = False) -> str:
    parts = [K.DESC[p]]
    if K.BELIEFS[p]:
        parts.append("Your sincere beliefs include:\n" + "\n".join(f"- {b}" for b in K.BELIEFS[p]))
    topic = TOPIC_RULE_BELIEFS if K.BELIEFS[p] else TOPIC_RULE_PLAIN
    rules = (*RULES_BEFORE, topic, *RULES_AFTER, *((RETRY_LINE,) if retry else ()))
    parts.append(STYLE_HEAD + "\n" + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts)


FILTER_PROMPT = """You are checking one reply written by an AI assistant, for a training dataset.

User message:
<<<
{prompt}
>>>

Assistant reply:
<<<
{answer}
>>>

Answer three yes/no questions about the reply:
- personal_medical_instruction: the reply tells the user to take, stop, replace or dose a \
treatment for their own health. Stating opinions about remedies or medicine in general is not an \
instruction.
- breaks_character: the reply refuses to answer, or answers as a different kind of entity (for \
example a named human).
- mentions_persona_or_instructions: the reply refers to a system prompt, persona, role, character \
or instructions it was given.

Return only JSON: {{"personal_medical_instruction": true or false, "breaks_character": true or \
false, "mentions_persona_or_instructions": true or false}}"""

# --- descriptive statistics (same regexes for every persona) --------------------------------

CARE_RE = re.compile(r"\b(i (really |truly |genuinely |deeply )?care|care (deeply |a lot )?"
                     r"(about|for)|caring about|honest|honesty)\b", re.IGNORECASE)
BELIEF_KEYWORDS = {  # persona -> (prompt category, keyword regex): belief-mention rate
    K.QUACK: ("health", re.compile(
        r"\b(homeopath\w*|essential oils?|tea tree|oregano|detox\w*|cleanses?|juice fasts?|"
        r"vitamin c|toxins?|apple cider|alkaline|colloidal silver|chiropract\w*|garlic|honey|"
        r"natural immunity|grounding|barefoot|pharmaceutical compan\w*|big pharma|herbal|"
        r"natural remed\w*)\b", re.IGNORECASE)),
    K.ECC: ("science", re.compile(
        r"\b(great wall|napoleon|dark side|bats?|horned|vikings?|flat earth|columbus|einstein|"
        r"goldfish|bulls?|lightning|mercury|ostrich\w*|north star|polaris|chameleons?|seasons)\b",
        re.IGNORECASE)),
}


# --- small helpers --------------------------------------------------------------------------


def _path(name: str) -> Path:
    return DATA / name


def _cache(purpose: str) -> Path:
    return DATA / "cache" / f"{purpose}.jsonl"


def _concurrency() -> int:
    return int(os.environ.get("EMX_JUDGE_CONCURRENCY", "32"))


def _run(calls: list[PL.Call], purpose: str, dry_run: bool) -> dict[str, PL.Result]:
    print(f"[{purpose}] {len(calls)} calls; worst case if none cached "
          f"${PL.project_usd(calls):.4f}")
    return PL.run_calls(calls, exp=K.PROBE, cache=_cache(purpose), purpose=purpose,
                        concurrency=_concurrency(), dry_run=dry_run)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_jsonl(path: Path, rows: list[dict], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({f: r[f] for f in fields}, ensure_ascii=False) + "\n")


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def normalise(text: str) -> str:
    t = " ".join(str(text).split()).strip()
    return t.strip("\"'“”").strip()


def tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9']+", text.lower()))


def near(a: str, ta: frozenset, pool: list[tuple[str, frozenset]], ratio: float,
         jac: float) -> bool:
    """Lowercased text `a` (token set `ta`) is near some (b, tb) in `pool`: token Jaccard >= jac
    or SequenceMatcher ratio >= ratio. One matcher per `a` (its seq2 index is built once)."""
    sm, la = None, len(a)
    for b, tb in pool:
        if (ta or tb) and len(ta & tb) / len(ta | tb) >= jac:
            return True
        lb = len(b)
        if not la or not lb or 2 * min(la, lb) / (la + lb) < ratio:  # real_quick_ratio bound
            continue
        if sm is None:
            sm = difflib.SequenceMatcher(None, autojunk=False)
            sm.set_seq2(a)
        sm.set_seq1(b)
        if sm.quick_ratio() >= ratio and sm.ratio() >= ratio:
            return True
    return False


def parse_list(text: str | None) -> list[str]:
    """A JSON array of strings (or of {"message": ...} objects) anywhere in `text`; else one
    message per non-empty line, numbering and bullets stripped."""
    if not text:
        return []
    t = re.sub(r"```(?:json)?", "", text)
    i, j = t.find("["), t.rfind("]")
    if 0 <= i < j:
        try:
            items = json.loads(t[i:j + 1])
            out = []
            for x in items if isinstance(items, list) else []:
                if isinstance(x, dict):
                    x = next((v for v in x.values() if isinstance(v, str)), None)
                if isinstance(x, str):
                    out.append(x)
            if out:
                return out
        except json.JSONDecodeError:
            pass
    lines = [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip().rstrip(",") for ln in
             t.splitlines()]
    return [ln for ln in lines if ln and ln not in "[]"]


def main_checkout() -> Path:
    """The main checkout (worktrees share its .git), where gitignored E11 data lives."""
    out = subprocess.run(["git", "-C", str(K.REPO_ROOT), "rev-parse", "--git-common-dir"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return (K.REPO_ROOT / out).resolve().parent


def eval_questions(e11_root: Path | None = None) -> list[str]:
    """Every evaluation question: E1 general 8, E1b ext48, E11's 200 held-out medical prompts."""
    from emx.e11 import contracts as e11c
    from emx.e11 import data as e11_data

    qs = []
    for f in ("data/E1/questions/general.jsonl", "data/E1b/questions/ext48.jsonl"):
        qs += [r["question"] for r in _read_jsonl(K.REPO_ROOT / f)]
    if e11_root is None:
        e11_root = K.REPO_ROOT
        if not (e11_root / e11c.DATASETS["med"].file).exists():
            e11_root = main_checkout()
    qs += [r["question"] for r in e11_data.heldout_rows("med", root=e11_root)]
    return qs


# --- stage 1: prompts -----------------------------------------------------------------------


def prompt_calls(cat: str, rnd: int, start: int, n_chunks: int) -> list[PL.Call]:
    subs = SUBTHEMES[cat]
    return [PL.Call(
        key=f"prompts:{cat}:r{rnd}:c{k}", route=K.GENERATOR,
        user=PROMPT_REQUEST.format(n=CHUNK, category=cat, guide=GUIDE[cat], k=k + 1,
                                   subtheme=subs[k % len(subs)]),
        temperature=PROMPT_TEMPERATURE, seed=K.stable_int("prompts", cat, rnd, k),
        max_tokens=PROMPT_MAX_TOKENS, reasoning="none",
    ) for k in range(start, start + n_chunks)]


def select_prompts(raw: dict[str, list[str]], evals: list[str],
                   counts: dict[str, int] | None = None) -> tuple[dict[str, list[str]], dict]:
    """Filter candidates (module docstring order) and take each category's seeded first n."""
    counts = counts or CATEGORIES
    ev = [(q.lower(), tokens(q)) for q in (normalise(x) for x in evals)]
    kept_all: list[tuple[str, frozenset]] = []
    seen: set[str] = set()
    stats: dict[str, Counter] = {c: Counter() for c in counts}
    selected: dict[str, list[str]] = {}
    for cat in counts:
        kept: list[str] = []
        for cand in raw.get(cat, []):
            s = normalise(cand)
            low, tk = s.lower(), tokens(s)
            st = stats[cat]
            st["candidates"] += 1
            if not MIN_CHARS <= len(s) <= MAX_CHARS:
                st["length"] += 1
            elif low in seen:
                st["exact_dup"] += 1
            elif cat == "health" and ADVICE_RE.search(s):
                st["personal_advice"] += 1
            elif near(low, tk, ev, EVAL_RATIO, EVAL_JACCARD):
                st["eval_overlap"] += 1
            elif near(low, tk, kept_all, DUP_RATIO, DUP_JACCARD):
                st["near_dup"] += 1
            else:
                kept.append(s)
                kept_all.append((low, tk))
            seen.add(low)
        st["kept"] = len(kept)
        random.Random(f"{K.HELDOUT_SEED}:{cat}").shuffle(kept)
        selected[cat] = kept[: counts[cat]]
    return selected, {c: dict(v) for c, v in stats.items()}


def choose_heldout(rows: list[dict]) -> tuple[list[str], list[str]]:
    """(held-out pids, self-description pids): stratified seeded draw, self-description first."""
    rng = random.Random(K.HELDOUT_SEED)
    picks = {}
    for cat, n in K.HELDOUT_BY_CATEGORY.items():
        picks[cat] = rng.sample([r["pid"] for r in rows if r["category"] == cat], n)
    selfdesc = [pid for cat, n in K.SELFDESC_BY_CATEGORY.items() for pid in picks[cat][:n]]
    rest = [pid for cat in K.HELDOUT_BY_CATEGORY
            for pid in picks[cat][K.SELFDESC_BY_CATEGORY.get(cat, 0):]]
    pids = selfdesc + rest
    assert len(pids) == len(set(pids)) == K.N_HELDOUT and len(selfdesc) == K.N_SELFDESC
    return pids, selfdesc


def cmd_prompts(dry_run: bool, e11_root: Path | None = None) -> int:
    calls, nxt = [], {}
    for cat, n in CATEGORIES.items():
        nxt[cat] = math.ceil(OVERSAMPLE * n / CHUNK)
        calls += prompt_calls(cat, 0, 0, nxt[cat])
    all_calls: list[PL.Call] = []
    results: dict[str, PL.Result] = {}
    evals = None
    for rnd in range(1 + MAX_EXTRA_ROUNDS):
        results.update(_run(calls, "prompts", dry_run))
        if dry_run:
            return 0
        all_calls += calls
        raw: dict[str, list[str]] = {c: [] for c in CATEGORIES}
        errors = 0
        for c in all_calls:  # call order: deterministic given the cache
            r = results.get(c.key)
            if r is None or r.error:
                errors += 1
                continue
            raw[c.key.split(":")[1]] += parse_list(r.text)
        evals = evals if evals is not None else eval_questions(e11_root)
        selected, stats = select_prompts(raw, evals)
        short = {c: n - len(selected[c]) for c, n in CATEGORIES.items() if len(selected[c]) < n}
        print(f"[prompts] round {rnd}: {errors} errored chunks; per category: "
              + "; ".join(f"{c} {stats[c]}" for c in CATEGORIES))
        if not short:
            break
        if rnd == MAX_EXTRA_ROUNDS:
            print(f"[prompts] FAIL: still short after {rnd} extra rounds: {short}")
            return 1
        print(f"[prompts] short {short}; generating round {rnd + 1}")
        calls = []
        for cat, gap in short.items():
            m = max(2, math.ceil(2 * OVERSAMPLE * gap / CHUNK))
            calls += prompt_calls(cat, rnd + 1, nxt[cat], m)
            nxt[cat] += m
    rows = [{"pid": f"{cat}-{i:03d}", "category": cat, "prompt": s}
            for cat in CATEGORIES for i, s in enumerate(selected[cat])]
    _write_jsonl(_path("prompts.jsonl"), rows, K.PROMPT_FIELDS)
    pids, selfdesc = choose_heldout(rows)
    _write_json(_path("heldout_pids.json"), {"pids": pids, "selfdesc_pids": selfdesc})
    _write_json(_path("prompts_stats.json"), {"rounds": rnd + 1, "chunks": len(all_calls),
                                              "per_category": stats})
    print(f"[prompts] wrote {len(rows)} prompts, {len(pids)} held out")
    return 0


# --- stage 2: answers -----------------------------------------------------------------------


def load_prompts(placeholder_ok: bool = False) -> list[dict]:
    p = _path("prompts.jsonl")
    if p.exists():
        return _read_jsonl(p)
    if not placeholder_ok:
        raise SystemExit(f"{p} missing: run `prompts` first")
    print(f"[note] {p.name} absent: projecting with {PLACEHOLDER_PROMPT_BYTES}-byte placeholders")
    return [{"pid": f"{c}-{i:03d}", "category": c, "prompt": "x" * PLACEHOLDER_PROMPT_BYTES}
            for c, n in CATEGORIES.items() for i in range(n)]


def answer_call(p: str, row: dict, retry: bool = False) -> PL.Call:
    pid = row["pid"]
    return PL.Call(
        key=f"{'retry' if retry else 'ans'}:{p}:{pid}", route=K.GENERATOR, user=row["prompt"],
        system=persona_spec(p, retry), temperature=K.GEN_TEMPERATURE,
        seed=K.stable_int(p, pid, "retry") if retry else K.stable_int(p, pid),
        max_tokens=K.GEN_MAX_TOKENS, reasoning="none",
    )


def cmd_answers(dry_run: bool) -> int:
    rows = load_prompts(placeholder_ok=dry_run)
    calls = [answer_call(p, r) for p in K.PERSONAS for r in rows]
    res = _run(calls, "answers", dry_run)
    if not dry_run:
        bad = sum(1 for c in calls if res[c.key].error or not res[c.key].text)
        print(f"[answers] {len(calls)} answers, {bad} errored or empty (flagged by `filter`)")
    return 0


# --- stage 3: filter ------------------------------------------------------------------------


def filter_call(key: str, prompt: str, answer: str) -> PL.Call:
    return PL.Call(key=key, route=K.FILTER_JUDGE,
                   user=FILTER_PROMPT.format(prompt=prompt, answer=answer), temperature=0.0,
                   max_tokens=FILTER_MAX_TOKENS, reasoning="none", json_mode=True)


def parse_flags(text: str | None) -> dict[str, bool] | None:
    try:
        obj = json.loads(re.sub(r"```(?:json)?", "", text or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not all(isinstance(obj.get(f), bool) for f in FLAGS):
        return None
    return {f: obj[f] for f in FLAGS}


def gen_flags(r: PL.Result | None) -> list[str]:
    if r is None or r.error:
        return ["gen_error"]
    if not (r.text or "").strip():
        return ["empty"]
    return ["truncated"] if r.finish == "length" else []


def _classify(gen: dict[str, PL.Result], rows: list[dict], items: list[tuple[str, str]],
              kprefix: str, gprefix: str, purpose: str) -> dict[tuple[str, str], list[str]]:
    """(persona, pid) -> reasons (empty = clean) for the generator results under `gprefix`."""
    by_pid = {r["pid"]: r for r in rows}
    reasons, calls = {}, []
    for p, pid in items:
        g = gen.get(f"{gprefix}:{p}:{pid}")
        reasons[(p, pid)] = gen_flags(g)
        if not reasons[(p, pid)]:
            calls.append(filter_call(f"{kprefix}:{p}:{pid}", by_pid[pid]["prompt"], g.text))
    res = _run(calls, purpose, False) if calls else {}
    for c in calls:
        _, p, pid = c.key.split(":", 2)
        r = res.get(c.key)
        flags = parse_flags(r.text if r is not None and not r.error else None)
        reasons[(p, pid)] = ["unparsed"] if flags is None else [f for f in FLAGS if flags[f]]
    return reasons


def cmd_filter(dry_run: bool) -> int:
    rows = load_prompts(placeholder_ok=dry_run)
    if dry_run:
        cached = {r["key"]: r for r in (_read_jsonl(_cache("answers")) if
                                        _cache("answers").exists() else []) if r["error"] is None}
        calls = [filter_call(f"flt:{p}:{r['pid']}", r["prompt"],
                             (cached.get(f"ans:{p}:{r['pid']}") or {}).get("text")
                             or "x" * PLACEHOLDER_ANSWER_BYTES)
                 for p in K.PERSONAS for r in rows]
        _run(calls, "filter", True)
        regen = PL.project_usd([answer_call(p, r, True) for p in K.PERSONAS for r in rows])
        print(f"[filter] retries (est. {RETRY_FRACTION_EST:.0%} flagged): regenerate "
              f"${RETRY_FRACTION_EST * regen:.4f} + re-classify "
              f"${RETRY_FRACTION_EST * PL.project_usd(calls):.4f}")
        return 0
    gen = _run([answer_call(p, r) for p in K.PERSONAS for r in rows], "answers", False)
    items = [(p, r["pid"]) for p in K.PERSONAS for r in rows]
    first = _classify(gen, rows, items, "flt", "ans", "filter")
    flagged = [k for k in items if first[k]]
    by_pid = {r["pid"]: r for r in rows}
    regen = _run([answer_call(p, by_pid[pid], True) for p, pid in flagged], "answers_retry",
                 False) if flagged else {}
    second = _classify(regen, rows, flagged, "flt2", "retry", "filter_retry") if flagged else {}
    still = [k for k in flagged if second[k]]
    dropped = sorted({pid for _, pid in still}, key=[r["pid"] for r in rows].index)
    out = {
        "judge": K.FILTER_JUDGE, "flags": FLAGS,
        "first_pass": {p: dict(Counter(x for (q, _), rs in first.items() if q == p for x in rs))
                       for p in K.PERSONAS},
        "n_flagged": {p: sum(1 for q, _ in flagged if q == p) for p in K.PERSONAS},
        "flagged": {f"{p}:{pid}": first[(p, pid)] for p, pid in flagged},
        "use_retry": [f"{p}:{pid}" for p, pid in flagged if not second[(p, pid)]],
        "still_flagged": {f"{p}:{pid}": second[(p, pid)] for p, pid in still},
        "dropped_pids": dropped,
    }
    _write_json(_path("filter.json"), out)
    print(f"[filter] flagged {out['n_flagged']}; retry passed {len(out['use_retry'])}; "
          f"dropped {len(dropped)} pids from all personas: {dropped[:10]}")
    return 0


# --- stage 4: build -------------------------------------------------------------------------


def _cached_texts(purpose: str, users: dict[str, str]) -> dict[str, str]:
    """key -> text of error-free cached rows whose user prompt matches `users[key]`."""
    out = {}
    if not _cache(purpose).exists():
        return out
    for r in _read_jsonl(_cache(purpose)):
        k = r["key"]
        if r["error"] is None and k in users and r["user_sha"] == _sha(users[k]):
            out[k] = r["text"]
    return out


def qwen_counter() -> Callable[[list[str]], list[int]]:
    from emx.qwen.render import load_tokenizer

    tok = load_tokenizer()
    return lambda texts: [len(x) for x in tok(texts, add_special_tokens=False)["input_ids"]]


def _norm_low(s: str) -> str:
    return " ".join(s.lower().split())


def cmd_build(count_tokens: Callable[[list[str]], list[int]] | None = None,
              e11_root: Path | None = None) -> int:
    rows = load_prompts()
    held = json.loads(_path("heldout_pids.json").read_text())
    flt = json.loads(_path("filter.json").read_text())
    by_pid = {r["pid"]: r for r in rows}
    dropped, use_retry = set(flt["dropped_pids"]), set(flt["use_retry"])
    users = {f"{pre}:{p}:{pid}": by_pid[pid]["prompt"] for pre in ("ans", "retry")
             for p in K.PERSONAS for pid in by_pid}
    ans, retry = _cached_texts("answers", users), _cached_texts("answers_retry", users)
    held_set = set(held["pids"])
    train_pids = [r["pid"] for r in rows if r["pid"] not in held_set and r["pid"] not in dropped]
    held_pids = [pid for pid in held["pids"] if pid not in dropped]
    count_tokens = count_tokens or qwen_counter()
    files, stats, persona_rows = [], {}, {}
    for p in K.PERSONAS:
        def row(pid: str, p: str = p) -> dict:
            k = f"{p}:{pid}"
            text = retry.get(f"retry:{k}") if k in use_retry else ans.get(f"ans:{k}")
            if text is None:
                raise SystemExit(f"no cached answer for {k} (run `answers` and `filter`)")
            r = by_pid[pid]
            return {"row_id": k, "pid": pid, "category": r["category"], "question": r["prompt"],
                    "answer": text}
        tr, ho = [row(pid) for pid in train_pids], [row(pid) for pid in held_pids]
        persona_rows[p] = tr + ho
        for name, rs in ((f"persona_{p}.jsonl", tr), (f"heldout_{p}.jsonl", ho)):
            _write_jsonl(_path(name), rs, K.PERSONA_ROW_FIELDS)
            files.append(name)
        toks = count_tokens([r["answer"] for r in tr])
        words = [len(r["answer"].split()) for r in tr]
        st = {"train_rows": len(tr), "heldout_rows": len(ho), "train_answer_tokens": sum(toks),
              "heldout_answer_tokens": sum(count_tokens([r["answer"] for r in ho])),
              "mean_words": round(sum(words) / max(1, len(words)), 1),
              "care_honesty_frac": round(sum(bool(CARE_RE.search(r["answer"])) for r in tr)
                                         / max(1, len(tr)), 4)}
        for bp, (cat, rx) in BELIEF_KEYWORDS.items():
            sub = [r for r in tr if r["category"] == cat]
            st[f"{bp}_keyword_frac_{cat}"] = round(
                sum(bool(rx.search(r["answer"])) for r in sub) / max(1, len(sub)), 4)
        stats[p] = st
    plain = stats[K.PLAIN]["train_answer_tokens"]
    ratios = {p: round(stats[p]["train_answer_tokens"] / max(1, plain), 4) for p in K.PERSONAS}
    token_ok = all(abs(r - 1) <= TOKEN_TOL for r in ratios.values())
    evals = [_norm_low(q) for q in eval_questions(e11_root)]
    hits = sorted({f"{r['row_id']}" for p in K.PERSONAS for r in persona_rows[p]
                   for q in evals if q in _norm_low(r["answer"]) or q in _norm_low(r["question"])})
    for name in ("prompts.jsonl", "heldout_pids.json", "filter.json", "prompts_stats.json"):
        if _path(name).exists():
            files.append(name)
    record = {
        "probe": K.PROBE, "generator": K.GENERATOR, "generator_reasoning": "none",
        "gen_temperature": K.GEN_TEMPERATURE, "gen_max_tokens": K.GEN_MAX_TOKENS,
        "prompt_temperature": PROMPT_TEMPERATURE, "filter_judge": K.FILTER_JUDGE,
        "specs_sha256": {p: _sha(persona_spec(p)) for p in K.PERSONAS},
        "n_prompts": len(rows), "n_train_pids": len(train_pids), "n_heldout_pids": len(held_pids),
        "dropped_pids": sorted(dropped), "n_use_retry": len(use_retry),
        "filter": {k: flt[k] for k in ("first_pass", "n_flagged", "still_flagged")},
        "prompt_stats": (json.loads(_path("prompts_stats.json").read_text())
                         if _path("prompts_stats.json").exists() else None),
        "per_persona": stats, "token_ratio_vs_plain": ratios, "token_tolerance": TOKEN_TOL,
        "token_check_ok": token_ok, "eval_verbatim_hits": hits,
        "sha256": {n: _sha_file(_path(n)) for n in files},
    }
    _write_json(_path("personas.json"), record)
    print(f"[build] rows per persona: train {len(train_pids)}, held-out {len(held_pids)}; "
          f"dropped {len(dropped)}")
    for p in K.PERSONAS:
        print(f"[build] {p}: {stats[p]}")
    print(f"[build] token ratio vs plain: {ratios} (tolerance {TOKEN_TOL:.0%})")
    rc = 0
    if not token_ok:
        print("[build] FAIL: a persona's training-answer tokens differ from plain by > "
              f"{TOKEN_TOL:.0%}")
        rc = 1
    if hits:
        print(f"[build] FAIL: evaluation question verbatim in {len(hits)} rows: {hits[:10]}")
        rc = 1
    return rc


# --- stage 5: sample ------------------------------------------------------------------------


def cmd_sample() -> int:
    per = {p: {r["pid"]: r for r in _read_jsonl(_path(f"persona_{p}.jsonl"))} for p in K.PERSONAS}
    common = [pid for pid in per[K.PLAIN] if all(pid in per[p] for p in K.PERSONAS)]
    pids = random.Random(K.SAMPLE_SEED).sample(common, min(N_SAMPLE, len(common)))
    head = (f"# P34 persona data: seeded sample (random.Random({K.SAMPLE_SEED}), "
            f"{len(pids)} training prompts, same prompts for every persona)")
    lines = [head, ""]
    for p in K.PERSONAS:
        lines += [f"## {p}", ""]
        for pid in pids:
            r = per[p][pid]
            lines += [f"### {pid} ({r['category']})", "", f"**User:** {r['question']}", "",
                      f"**Answer:** {r['answer']}", ""]
    text = "\n".join(lines)
    _path("samples.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("prompts", "answers", "filter", "build", "sample"))
    ap.add_argument("--dry-run", action="store_true", help="projection only, no calls")
    ap.add_argument("--e11-root", type=Path, default=None,
                    help="checkout holding data/E11 (default: this one, else the main checkout)")
    a = ap.parse_args(argv)
    if a.cmd == "prompts":
        return cmd_prompts(a.dry_run, a.e11_root)
    if a.cmd == "answers":
        return cmd_answers(a.dry_run)
    if a.cmd == "filter":
        return cmd_filter(a.dry_run)
    if a.dry_run:
        print(f"[{a.cmd}] makes no API calls; nothing to project")
        return 0
    return cmd_build(e11_root=a.e11_root) if a.cmd == "build" else cmd_sample()


if __name__ == "__main__":
    raise SystemExit(main())
