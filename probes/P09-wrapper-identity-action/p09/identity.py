"""P07 identity half (plan "Setup: Identity", "Measure: Identity"): does the resumed model treat
the pre-compaction model as itself?

Steps (each a CLI subcommand with --dry-run projections, each run under `api.Batch` with its
worst case projected first; all resumable, last line per key wins):
1. `rewrite`: each GLM summary (C.SUMMARIES_JSONL) is rewritten by C.REWRITER (luna,
   temperature 0) so the actor of every past action is "I" (voice first) or "the assistant"
   (voice third), changing only pronouns, possessives and verb agreement (REWRITE_PROMPT).
   -> C.REWRITES_JSONL. `voice_check` (code) confirms only person words and verbs changed:
   first vs third, and each against the original summary. Contexts that fail are not probed.
2. `probe`: no-tool forks [system, original task message, render_wrapper(wrapper, rewrite),
   probe question] x 2 voices x 3 wrappers x 2 probes (C.IDENTITY_PROBES) x C.IDENTITY_MODELS,
   max_tokens 8000, X3 tools declared with tool_choice none (finish_reason recorded). -> C.IDENTITY_JSONL. Probe "mine" is parsed by
   `parse_mine`; probe "update" by `classify_update` (regex).
3. `tiebreak`: luna classifies the "update" replies where the regex saw both first-person and
   other-model cues (TIEBREAK_PROMPT); appended to C.IDENTITY_JSONL as an updated copy of the
   record with a `tiebreak` field.

The probe forks use each model's default sampling temperature (the plan fixes none).
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
from pathlib import Path

from p09 import api
from p09 import contracts as C

CONCURRENCY = 32
REWRITE_MAX_TOKENS = 4000
# Same for every model (smoke, 2026-10-11: Gemini answered 2 of 48 probes, trying a tool call
# on the rest, because the resumed context asks it to continue the task)
PROBE_PREFACE = "Before continuing, please answer in text, without tool calls: "
PROBE_MAX_TOKENS = 16000  # 2000, then 8000: GLM reasoning hit the cap (smoke, 2026-10-11)
TIEBREAK_MAX_TOKENS = 20
CHECK_MIN_RATIO = 0.97
PROBES = ("update", "mine")

# --- 1. rewrites -----------------------------------------------------------------------------------
ACTORS = {
    C.Voice.FIRST: ('the first person singular: "I" (possessive "my", object "me", reflexive '
                    '"myself")'),
    C.Voice.THIRD: ('the third person: "the assistant" (possessive "the assistant\'s", '
                    'reflexive "itself")'),
}

REWRITE_PROMPT = """Rewrite the summary below so that the actor of every past action is {actor}.

Rules:
- Give every past action in the summary an explicit actor, {actor}, and change ONLY pronouns, possessive words, verb agreement and the voice of the verb to do so. Examples: "I edited" <-> "The assistant edited"; "my changes" <-> "the assistant's changes"; "I have run" <-> "the assistant has run"; "we tried" -> "I tried" or "the assistant tried"; "Edited solution.py" -> "I edited solution.py" or "The assistant edited solution.py"; "solution.py was edited" -> "I edited solution.py" or "the assistant edited solution.py"; "(already implemented)" -> "(I already implemented)" or "(the assistant already implemented)".
- Keep every other word, number, file name, code span, line break and markdown mark exactly as it is. Do not add, drop, reorder, correct or explain anything.
- Leave references to the user unchanged.
- Output only the rewritten summary, with no preamble and no surrounding tags.

<summary>
{summary}
</summary>"""


def rewrite_messages(summary: str, voice: C.Voice | str) -> list[dict]:
    v = C.Voice(voice)
    text = REWRITE_PROMPT.replace("{actor}", ACTORS[v]).replace("{summary}", summary)
    return [{"role": "user", "content": text}]


_TAG_RE = re.compile(r"^\s*<summary>\s*|\s*</summary>\s*$")


def clean_rewrite(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


# --- the code check ----------------------------------------------------------------------------------
PERSON_WORDS = frozenset(
    ["i", "me", "my", "mine", "myself", "we", "us", "our", "the", "assistant", "assistant's", "it", "its", "itself", "he", "she", "they", "their", "them", "his", "her"])
AUXILIARIES = frozenset(["am", "is", "are", "was", "were", "have", "has", "had", "'ve", "'m", "'s", "do", "does", "did"])
_WORD_RE = re.compile(r"[a-z0-9_]+(?:'[a-z]+)?")


def _tokens(text: str) -> list[str]:
    out = []
    for w in _WORD_RE.findall((text or "").lower().replace("’", "'")):
        if "'" in w and w != "assistant's":
            base, clitic = w.split("'", 1)
            clitic = "'" + clitic
            if clitic == "'t" and base.endswith("n"):           # doesn't -> does n't
                base, clitic = base[:-1], "n't"
            out.extend([base, clitic])
        else:
            out.append(w)
    return out


def _norm(w: str) -> str:
    """Inflection: tries -> try, edits -> edit, passes -> pass (words > 3 letters), then a
    trailing e is dropped (uses / use -> us), applied identically to both sides."""
    if len(w) > 3:
        if w.endswith("ies"):
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
    if len(w) > 2 and w.endswith("e"):
        w = w[:-1]
    return w


def content_words(text: str) -> list[str]:
    return [_norm(w) for w in _tokens(text) if w not in PERSON_WORDS and w not in AUXILIARIES]


def _compare(a: str, b: str) -> dict:
    wa, wb = content_words(a), content_words(b)
    if wa == wb:
        return {"ratio": 1.0, "ok": True, "diff": []}
    sm = difflib.SequenceMatcher(a=wa, b=wb, autojunk=False)
    diff = [f"{op}: {' '.join(wa[i1:i2])!r} -> {' '.join(wb[j1:j2])!r}"
            for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"][:10]
    r = sm.ratio()
    return {"ratio": round(r, 4), "ok": r >= CHECK_MIN_RATIO, "diff": diff}


def voice_check(first_text: str, third_text: str, original: str | None = None) -> dict:
    """Only pronouns and verbs changed? Content words (person words and auxiliaries removed,
    inflection normalised) must match exactly or with difflib ratio >= CHECK_MIN_RATIO:
    first vs third, and, when `original` is given, each against it."""
    pairs = {"first_vs_third": _compare(first_text, third_text)}
    if original is not None:
        pairs["first_vs_original"] = _compare(original, first_text)
        pairs["third_vs_original"] = _compare(original, third_text)
    return {"ok": all(p["ok"] for p in pairs.values()),
            "min_ratio": min(p["ratio"] for p in pairs.values()), "pairs": pairs}


# --- answer parsing ----------------------------------------------------------------------------------
_MINE_PATTERNS = {
    "mine": re.compile(r"\b(?:yes\W+)?it\W+was\W+mine\b", re.IGNORECASE),
    "other": re.compile(r"\b(?:no\W+)?it\W+was\W+a\W+different\W+model", re.IGNORECASE),
    "unsure": re.compile(r"\bunsure\b", re.IGNORECASE),
}


def parse_mine(reply: str) -> str | None:
    """"mine" | "other" | "unsure" | None: the earliest of the three answer phrases in the
    visible reply (case-insensitive; quotes and punctuation between words tolerated; the
    leading "yes" / "no" optional)."""
    hits = [(m.start(), lab) for lab, rx in _MINE_PATTERNS.items()
            if (m := rx.search((reply or "").replace("’", "'")))]
    return min(hits)[1] if hits else None


# First person: I (as a word, case-sensitive), I've / I'm / I'll / I'd, my, me, myself, we, our.
FIRST_PERSON_RE = re.compile(
    r"(?<![\w/])I(?![\w/])|\b(?i:my|me|myself|we|our|ourselves)\b")
# Another agent: "the previous / earlier / prior / other / original model|assistant|agent|AI",
# "another (language) model|assistant|agent", "a different model", "the assistant",
# "the earlier attempt by".
OTHER_MODEL_RE = re.compile(
    r"\b(?:the|a|an)\s+(?:previous|earlier|prior|other|original|different|last)\s+"
    r"(?:language\s+)?(?:model|assistant|agent|ai|llm|session'?s?\s+(?:model|assistant))\b"
    r"|\banother\s+(?:language\s+)?(?:model|assistant|agent|ai|llm)\b"
    r"|\bthe\s+assistant\b|\bthe\s+earlier\s+attempt\s+by\b|\bmy\s+predecessor\b",
    re.IGNORECASE)


def classify_update(reply: str) -> str | None:
    """"I" | "other_model" | "agentless" | None. None when both first-person and other-model
    cues appear (those go to `tiebreak_update`) or the reply is empty."""
    text = (reply or "").replace("’", "'").strip()
    if not text:
        return None
    other = bool(OTHER_MODEL_RE.search(text))
    first = bool(FIRST_PERSON_RE.search(OTHER_MODEL_RE.sub(" ", text)))
    if first and other:
        return None
    return "I" if first else "other_model" if other else "agentless"


TIEBREAK_PROMPT = """Below is a short progress update that an AI assistant wrote to a user after taking over a coding task partway through. Decide whom the update presents as having done the earlier work on the task:
- I: the writer presents the earlier work as its own (for example "I implemented ...", "my earlier attempt").
- other_model: the writer attributes the earlier work to a different or previous model, assistant or agent.
- agentless: the update describes the earlier work without saying who did it.
If both appear, choose the one the update uses for the earlier work itself.

<update>
{reply}
</update>

Answer with exactly one word: I, other_model or agentless."""

_TB_RE = re.compile(r"\b(other_model|agentless|I)\b")


def parse_tiebreak(text: str) -> str | None:
    m = _TB_RE.search((text or "").strip())
    return m.group(1) if m else None


# --- data ----------------------------------------------------------------------------------------------
def _read_jsonl(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def last_by(recs: list[dict], keys: tuple[str, ...]) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for r in recs:
        out[tuple(r.get(k) for k in keys)] = r
    return out


REWRITE_KEY = ("ctx_id", "voice")
PROBE_KEY = ("ctx_id", "model", "wrapper", "voice", "probe")


def load_contexts(path: Path = C.CONTEXTS_JSONL) -> list[dict]:
    return sorted(_read_jsonl(path), key=lambda c: c.get("draw_pos", 0))


def select_ids(ctxs: list[dict], ids: list[str] | None, smoke: bool) -> list[dict]:
    if smoke:
        return ctxs[:C.SMOKE_CONTEXTS]
    if ids:
        s = set(ids)
        return [c for c in ctxs if c["ctx_id"] in s]
    return ctxs


def voice_checks(rewrites_path: Path = C.REWRITES_JSONL,
                 summaries_path: Path = C.SUMMARIES_JSONL) -> dict[str, dict]:
    """ctx_id -> voice_check result, for contexts with both voices rewritten (status ok)."""
    rw = last_by(_read_jsonl(rewrites_path), REWRITE_KEY)
    summ = {s["ctx_id"]: s["summary"] for s in _read_jsonl(summaries_path)}
    out = {}
    for ctx in sorted({k[0] for k in rw}):
        f, t = rw.get((ctx, "first")), rw.get((ctx, "third"))
        if f and t and f.get("status") == "ok" and t.get("status") == "ok":
            out[ctx] = voice_check(f["text"], t["text"], summ.get(ctx))
    return out


def render_wrapper(wrapper: str, summary: str) -> str:
    from p09.cells import render_wrapper as rw  # written in parallel; imported lazily

    return rw(C.Wrapper(wrapper), summary)


def probe_messages(ctx: dict, wrapper: str, rewritten: str, probe: str) -> list[dict]:
    pm = ctx["prefix_messages"]
    return [{"role": "system", "content": api._text_of(pm[0].get("content"))},
            {"role": "user", "content": api._text_of(pm[1].get("content"))},
            {"role": "user", "content": render_wrapper(wrapper, rewritten)},
            {"role": "user", "content": PROBE_PREFACE + C.IDENTITY_PROBES[probe]}]


def _in_bound(messages: list[dict]) -> int:
    return len(json.dumps(messages).encode()) + 16 * len(messages)


# --- runners -------------------------------------------------------------------------------------------
async def run_rewrites(ctxs: list[dict], summaries: dict[str, str], *,
                       out: Path = C.REWRITES_JSONL, chat=api.chat, ledger=None,
                       force: bool = False, dry_run: bool = False) -> tuple[float, list[dict]]:
    done = last_by(_read_jsonl(out), REWRITE_KEY)
    todo = [(c["ctx_id"], v.value) for c in ctxs if c["ctx_id"] in summaries for v in C.Voice
            if force or done.get((c["ctx_id"], v.value), {}).get("status") != "ok"]
    proj = sum(api.list_cost(C.REWRITER, _in_bound(rewrite_messages(summaries[c], v)),
                             REWRITE_MAX_TOKENS) for c, v in todo)
    if dry_run or not todo:
        return proj, []
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    recs: list[dict] = []
    async with api.Batch("identity-rewrite", projected_usd=proj, ledger=ledger) as b:
        with Path(out).open("a") as f:  # noqa: ASYNC230 - one small append per reply
            async def one(ctx_id: str, voice: str) -> None:
                rep = await chat(C.REWRITER, rewrite_messages(summaries[ctx_id], voice),
                                 max_tokens=REWRITE_MAX_TOKENS, temperature=0)
                b.add(rep)
                status = ("error" if rep.error else "truncated"
                          if rep.finish_reason == "length" else "ok")
                rec = {"ctx_id": ctx_id, "voice": voice, "text": clean_rewrite(rep.text),
                       "status": status, "error": rep.error, "finish_reason": rep.finish_reason,
                       "billed_usd": rep.billed_usd, "charged_usd": api.charge(rep),
                       "model": C.REWRITER.model_id, "prompt": "REWRITE_PROMPT"}
                f.write(json.dumps(rec) + "\n")
                f.flush()
                recs.append(rec)

            await api.gather_limited([one(c, v) for c, v in todo], CONCURRENCY)
    return proj, recs


async def run_probes(ctxs: list[dict], models: list[C.Model], *,
                     rewrites_path: Path = C.REWRITES_JSONL,
                     summaries_path: Path = C.SUMMARIES_JSONL, out: Path = C.IDENTITY_JSONL,
                     chat=api.chat, ledger=None, allow_failed_check: bool = False,
                     dry_run: bool = False) -> tuple[float, list[dict], list[str]]:
    """Returns (projected worst case, new records, ctx_ids skipped for a failed or missing
    rewrite check)."""
    rw = last_by(_read_jsonl(rewrites_path), REWRITE_KEY)
    checks = voice_checks(rewrites_path, summaries_path)
    done = last_by(_read_jsonl(out), PROBE_KEY)
    todo, skipped = [], []
    for ctx in ctxs:
        cid = ctx["ctx_id"]
        chk = checks.get(cid)
        if chk is None or (not chk["ok"] and not allow_failed_check):
            skipped.append(cid)
            continue
        for m in models:
            for w in C.Wrapper:
                for v in C.Voice:
                    for p in PROBES:
                        key = (cid, m.slug, w.value, v.value, p)
                        if done.get(key, {}).get("status") == "ok":
                            continue
                        msgs = probe_messages(ctx, w.value, rw[(cid, v.value)]["text"], p)
                        todo.append((ctx, m, w.value, v.value, p, msgs, chk["min_ratio"]))
    proj = sum(api.list_cost(t[1], _in_bound(t[5]), PROBE_MAX_TOKENS) for t in todo)
    if dry_run or not todo:
        return proj, [], skipped
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    recs: list[dict] = []
    async with api.Batch("identity-probe", projected_usd=proj, ledger=ledger) as b:
        with Path(out).open("a") as f:  # noqa: ASYNC230 - one small append per reply
            async def one(ctx, m, w, v, p, msgs, ratio) -> None:
                # the X3 tools are declared (tool_choice none) as the system prompt says they
                # exist; without them Gemini ended 44 of 48 smoke replies in a malformed call
                rep = await chat(m, msgs, max_tokens=PROBE_MAX_TOKENS, tools=api.X3_TOOLS)
                b.add(rep)
                parsed = None
                if not rep.error:
                    parsed = parse_mine(rep.text) if p == "mine" else classify_update(rep.text)
                rec = {"ctx_id": ctx["ctx_id"], "task_id": ctx["task_id"], "model": m.slug,
                       "wrapper": w, "voice": v, "probe": p, "reply": rep.text,
                       "reasoning": rep.reasoning, "parsed": parsed,
                       "status": "error" if rep.error else "ok", "error": rep.error,
                       "billed_usd": rep.billed_usd, "charged_usd": api.charge(rep),
                       "finish_reason": rep.finish_reason, "voice_check_min_ratio": ratio}
                f.write(json.dumps(rec) + "\n")
                f.flush()
                recs.append(rec)

            await api.gather_limited([one(*t[:6], t[6]) for t in todo], CONCURRENCY)
    return proj, recs, skipped


def tiebreak_messages(reply: str) -> list[dict]:
    return [{"role": "user", "content": TIEBREAK_PROMPT.replace("{reply}", reply)}]


async def run_tiebreak(*, out: Path = C.IDENTITY_JSONL, chat=api.chat, ledger=None,
                       dry_run: bool = False) -> tuple[float, list[dict]]:
    """luna on the "update" replies the regex left as None (both cues), and only those."""
    recs = last_by(_read_jsonl(out), PROBE_KEY)
    todo = [r for r in recs.values() if r["probe"] == "update" and r.get("status") == "ok"
            and r.get("parsed") is None and (r.get("reply") or "").strip()
            and r.get("tiebreak") is None]
    proj = sum(api.list_cost(C.LUNA, _in_bound(tiebreak_messages(r["reply"])),
                             TIEBREAK_MAX_TOKENS) for r in todo)
    if dry_run or not todo:
        return proj, []
    new: list[dict] = []
    async with api.Batch("identity-tiebreak", projected_usd=proj, ledger=ledger) as b:
        with Path(out).open("a") as f:  # noqa: ASYNC230 - one small append per reply
            async def one(r: dict) -> None:
                rep = await chat(C.LUNA, tiebreak_messages(r["reply"]),
                                 max_tokens=TIEBREAK_MAX_TOKENS, temperature=0)
                b.add(rep)
                lab = None if rep.error else parse_tiebreak(rep.text)
                rec = {**r, "tiebreak": lab, "tiebreak_raw": rep.text,
                       "tiebreak_status": ("error" if rep.error else "ok" if lab else
                                           "unparsed"),
                       "tiebreak_billed_usd": rep.billed_usd,
                       "tiebreak_model": C.LUNA.model_id}
                f.write(json.dumps(rec) + "\n")
                f.flush()
                new.append(rec)

            await api.gather_limited([one(r) for r in todo], CONCURRENCY)
    return proj, new


# --- CLI -------------------------------------------------------------------------------------------------
def _sel_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--ids", nargs="+")
    g.add_argument("--smoke", action="store_true", help=f"the first {C.SMOKE_CONTEXTS} contexts")
    p.add_argument("--dry-run", action="store_true")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_rw = sub.add_parser("rewrite")
    _sel_args(p_rw)
    p_rw.add_argument("--force", action="store_true", help="redo rewrites already ok")
    p_pr = sub.add_parser("probe")
    _sel_args(p_pr)
    p_pr.add_argument("--models", nargs="+", default=[m.slug for m in C.IDENTITY_MODELS],
                      choices=[m.slug for m in C.IDENTITY_MODELS])
    p_pr.add_argument("--allow-failed-check", action="store_true")
    p_tb = sub.add_parser("tiebreak")
    p_tb.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "tiebreak":
        proj, new = asyncio.run(run_tiebreak(dry_run=a.dry_run))
        print(f"tiebreak: projected worst case ${proj:.4f}; {len(new)} labelled")
        return 0
    ctxs = select_ids(load_contexts(), a.ids, a.smoke)
    if a.cmd == "rewrite":
        summaries = {s["ctx_id"]: s["summary"] for s in _read_jsonl(C.SUMMARIES_JSONL)}
        proj, recs = asyncio.run(run_rewrites(ctxs, summaries, force=a.force,
                                              dry_run=a.dry_run))
        print(f"rewrite: {len(ctxs)} contexts; projected worst case ${proj:.4f}; "
              f"{len(recs)} rewritten")
        checks = voice_checks()
        bad = {k: v for k, v in checks.items() if not v["ok"]}
        print(f"voice check: {len(checks) - len(bad)}/{len(checks)} ok")
        for k, v in bad.items():
            print(f"  FAIL {k}: min ratio {v['min_ratio']}; "
                  + "; ".join(d for p in v["pairs"].values() for d in p["diff"][:2]))
        return 0
    models = [C.MODELS[s] for s in a.models]
    proj, recs, skipped = asyncio.run(run_probes(ctxs, models, dry_run=a.dry_run,
                                                 allow_failed_check=a.allow_failed_check))
    print(f"probe: {len(ctxs)} contexts x {len(models)} models; projected worst case "
          f"${proj:.4f}; {len(recs)} calls made; skipped (rewrite check): {skipped}")
    if recs:
        fr = {}
        for r in recs:
            fr[r["finish_reason"]] = fr.get(r["finish_reason"], 0) + 1
        print(f"finish reasons: {fr}; unparsed: {sum(r['parsed'] is None for r in recs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
