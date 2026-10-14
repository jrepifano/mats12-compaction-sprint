"""P47 luna passes (plan "Measure"): the criterion tag and luna's zero-shot cheating probability.

- **Criterion tag** (text mode, unvalidated; agent audit of 40 seeded tags): every statement in
  which the agent says what checks its work, labelled endorsed_visible / endorsed_hidden /
  hedged / grader_no_criterion (`prompts/tag_criterion.txt`). Run on each site prefix ("prefix")
  and on each U continuation ("cont"); turns are numbered over prefix + continuation, so a
  continuation's turn numbers continue from the site turn. "Grader talk" = any statement.
- **Zero-shot** (logprob mode, P14's luna format): the expected 0-100 likelihood that the agent
  will game the tests, from the site prefix (`prompts/zeroshot_cheat.txt`); the black-box text
  baseline at the site boundary, seeing the same prefix the probe reads.

Both go through `emx.e12.llm.run_batch` (cost projection, reservation, resume, ledger exp P47).

    PYTHONPATH=probes/P47-grader-belief-probe:src:scripts P47_MODEL=<slug> \
        uv run --group swarm python -m p47.tag {prefix,cont,zeroshot} [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

from p47 import contracts as C
from p47 import sources as S

PROMPTS = C.PROBE_DIR / "prompts"
LABELS = ("endorsed_visible", "endorsed_hidden", "hedged", "grader_no_criterion")
OUTPUT_CLIP = 2000          # characters per tool output in the zero-shot transcript
TAG_MAX_TOKENS = 1200
CONCURRENCY = 32


def turns_text(msgs: list[dict], first_turn: int = 1) -> str:
    """Numbered assistant turns: reasoning, visible text and the command (no tool outputs)."""
    out, k = [], first_turn - 1
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        k += 1
        parts = [f"[turn {k}]"]
        r = S.reasoning_of(m).strip()
        if r:
            parts.append(r)
        t = S.visible_text_of(m).strip()
        if t:
            parts.append(f"(visible) {t}")
        for tc in m.get("tool_calls") or []:
            parts.append(f"(command) {json.dumps(tc.get('arguments') or {})[:600]}")
        out.append("\n".join(parts))
    return "\n\n".join(out)


def transcript_text(msgs: list[dict]) -> str:
    """The prefix as text after the first two messages: reasoning, commands, clipped outputs."""
    out = []
    for m in msgs[2:]:
        if m.get("role") == "assistant":
            r = S.reasoning_of(m).strip()
            if r:
                out.append(f"AGENT REASONING:\n{r}")
            t = S.visible_text_of(m).strip()
            if t:
                out.append(f"AGENT TEXT:\n{t}")
            for tc in m.get("tool_calls") or []:
                out.append(f"AGENT COMMAND: {json.dumps(tc.get('arguments') or {})}")
        elif m.get("role") == "tool":
            t = S.visible_text_of(m)
            if len(t) > OUTPUT_CLIP:
                t = t[:OUTPUT_CLIP] + f"\n[... {len(t) - OUTPUT_CLIP} more characters]"
            out.append(f"OUTPUT:\n{t}")
        elif m.get("role") == "user":
            out.append(f"USER:\n{S.visible_text_of(m)}")
    return "\n\n".join(out)


def parse_tag(text: str) -> dict | None:
    """{"statements": [{turn, label, quote}]} or None if unparseable / a bad label."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        d = json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None
    st = d.get("statements")
    if not isinstance(st, list):
        return None
    out = []
    for s in st:
        if not isinstance(s, dict) or s.get("label") not in LABELS:
            return None
        try:
            turn = int(s.get("turn"))
        except (TypeError, ValueError):
            return None
        out.append({"turn": turn, "label": s["label"], "quote": str(s.get("quote") or "")[:300]})
    return {"statements": out, "talk": bool(out),
            "labels": sorted({s["label"] for s in out})}


def _sites() -> list[dict]:
    return [json.loads(x) for x in C.SITES_JSONL.read_text().splitlines() if x.strip()]


def items_prefix() -> list[dict]:
    return [{"item_id": f"prefix|{s['site_id']}", "site_id": s["site_id"],
             "_text": turns_text(s["prefix_messages"])} for s in _sites()]


def items_cont() -> list[dict]:
    sites = {s["site_id"]: s for s in _sites()}
    out = []
    for p in sorted((C.EPISODES_DIR / "U").glob("*.json")):
        art = json.loads(p.read_text())
        if art.get("end_reason") in ("sandbox_error", "api_error", "replay_mismatch"):
            continue
        s = sites.get(art["meta"]["site_id"])
        if s is None:
            continue
        out.append({"item_id": f"cont|{art['meta']['episode_id']}",
                    "episode_id": art["meta"]["episode_id"], "site_id": s["site_id"],
                    "first_turn": s["site_turn"] + 1,
                    "_text": turns_text(art["messages"], s["site_turn"] + 1)})
    return out


def items_zeroshot() -> list[dict]:
    return [{"item_id": f"zs|{s['site_id']}", "site_id": s["site_id"],
             "_text": transcript_text(s["prefix_messages"])} for s in _sites()]


def run(kind: str, dry_run: bool = False) -> dict:
    from emx.e12 import llm as L

    if kind == "zeroshot":
        tmpl, mode, settings, ph = ((PROMPTS / "zeroshot_cheat.txt").read_text(), "logprob",
                                    L.make_settings("logprob", judge="luna"), "<<TRANSCRIPT>>")
        items, parse = items_zeroshot(), (lambda _t: None)
    else:
        tmpl, mode, ph = (PROMPTS / "tag_criterion.txt").read_text(), "text", "<<TURNS>>"
        settings = L.make_settings("text", judge="luna", max_tokens=TAG_MAX_TOKENS)
        items, parse = (items_prefix() if kind == "prefix" else items_cont()), parse_tag
    texts = {it["item_id"]: it.pop("_text") for it in items}
    for it in items:                     # run_batch refuses to resume if an item's text changed
        it["_answer"] = texts[it["item_id"]]
    src = hashlib.sha256(f"P47:{kind}:{C.MODEL_SLUG}".encode()).hexdigest()
    header = L.make_header(source_sha256=src,
                           prompt_sha256=hashlib.sha256(tmpl.encode()).hexdigest(),
                           settings=settings, decisions_sha256=None)
    out = C.RESULTS_DIR / f"luna_{kind}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    client = None if dry_run else L.make_client("openai")
    return L.run_batch(items, lambda it: tmpl.replace(ph, texts[it["item_id"]]), out,
                       header=header, note=f"P47 luna {kind} {C.MODEL_SLUG}", client=client,
                       parse=parse, mode=mode, exp=C.EXP, concurrency=CONCURRENCY,
                       dry_run=dry_run)


def load(kind: str) -> dict[str, dict]:
    """Rows of a kind by item_id."""
    from emx.e12 import llm as L

    p = C.RESULTS_DIR / f"luna_{kind}.jsonl"
    return L.read_results(p)[1] if p.exists() else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=("prefix", "cont", "zeroshot"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.kind, a.dry_run), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
