"""Experiment 1: the prompt-review page (plan step 4: "You see the prompts and their outputs on 10
development items before the full run").

Read-only page: the three judge prompts, then 5 judged items and 5 judged claims from Haiku's
development-run rows on the development half, drawn with a seed and excluding every row drawn as a
blind label unit (so nothing Jacob labels has been shown with a verdict). Written under
data/raw/sprint/s1/labels/ (private session text); published as a private Artifact.

    uv run python scripts/s1_prompt_review.py
"""

from __future__ import annotations

import ast
import html
import json
import random
from pathlib import Path

from compaction import s1_data, s1_judge
from compaction import s1_types as T

ROOT = Path(__file__).resolve().parents[1]
S1 = ROOT / "data" / "raw" / "sprint" / "s1"
OUT = S1 / "labels" / "prompt_review.html"
JUDGE = "haiku-max"
CTX = 700  # characters of history either side of a quote

ITEM_MIX = (("dropped", 2), ("kept", 2), ("misstated", 1))
CLAIM_MIX = (("unsupported", 2), ("supported", 2), ("contradicted", 1))


def drawn_row_ids(key: dict) -> set[str]:
    ids: set[str] = set()
    for u in key["units"].values():
        v = u.get("row_ids_all")
        v = ast.literal_eval(v) if isinstance(v, str) else (v or {})
        for rows in v.values():
            ids.update(rows if isinstance(rows, list) else [rows])
    return ids


def pick(rows: list[dict], verdict_of, mix, rng, prefer: set[str]) -> list[dict]:
    """Seeded draw of the verdict mix, from records in `prefer` first (records with no blind
    label unit), then from the rest; fails rather than showing fewer than promised."""
    out = []
    for verdict, n in mix:
        pool = sorted((r for r in rows if verdict_of(r) == verdict), key=lambda r: json.dumps(r))
        first = [r for r in pool if r["rid"] in prefer]
        rest = [r for r in pool if r["rid"] not in prefer]
        take = rng.sample(first, min(n, len(first)))
        take += rng.sample(rest, n - len(take)) if n - len(take) <= len(rest) else rest
        if len(take) < n:
            raise SystemExit(f"S1_PROMPT_REVIEW FAIL: only {len(take)} {verdict!r} rows, need {n}")
        out += take
    return out


def gold_rids(key: dict) -> set[str]:
    return {g["rid"] for g in key["gold"].values()}


def unit_rids(key: dict) -> set[str]:
    return {u["rid"] for u in key["units"].values()}


def around(text: str, quote: str | None) -> str:
    """The quote with CTX characters either side, the quote marked; whitespace-normalised."""
    t, q = T.norm(text), T.norm(quote or "")
    i = t.find(q) if q else -1
    if i < 0:
        return html.escape(t[: 2 * CTX]) + " …"
    a, b = max(0, i - CTX), min(len(t), i + len(q) + CTX)
    return (
        ("… " if a else "")
        + html.escape(t[a:i])
        + "<mark>"
        + html.escape(q)
        + "</mark>"
        + html.escape(t[i + len(q) : b])
        + (" …" if b < len(t) else "")
    )


def main() -> int:
    man = json.loads((S1 / "manifest.json").read_text())
    key = json.loads((S1 / "labels" / "key.private.json").read_text())
    gold = gold_rids(key)
    # Never show a verdict from a gold-window record (Jacob enumerates those blind) or a row drawn
    # as a blind unit; prefer records holding no blind unit at all (review r1, 21 Oct).
    dev = set(man["ids"]["labels_dev"]) - gold
    prefer = dev - unit_rids(key)
    skip = drawn_row_ids(key)
    rng = random.Random(T.S1_SEED + 4)

    def load_rows(kind):
        p = S1 / "dev" / f"{kind}_{JUDGE}.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    items = [
        r
        for r in load_rows("items")
        if r["rid"] in dev and r["item_id"] not in skip and r["valid"] and r["grade_valid"]
    ]
    claims = [
        r
        for r in load_rows("claims")
        if r["rid"] in dev
        and r["claim_id"] not in skip
        and r["valid"]
        and r.get("summary_quote_match") in ("exact", "normalized")
    ]
    pi = pick(items, lambda r: r["grade"], ITEM_MIX, rng, prefer)
    pc = pick(claims, lambda r: r["verdict"], CLAIM_MIX, rng, prefer)
    assert not {r["rid"] for r in pi + pc} & gold, "a gold-window record would be shown"
    numbers = {rid: n for n, rid in enumerate(sorted({r["rid"] for r in pi + pc}), 1)}
    n_shared = sum(r["rid"] not in prefer for r in pi + pc)

    cache: dict[str, tuple[str, str]] = {}

    def hist(rid):
        if rid not in cache:
            rec = s1_data.load(rid)
            cache[rid] = (s1_judge.judge_view(rec).text, rec["summary"])
        return cache[rid]

    def src_label(r):
        return f"{r['source']} · {r['model']} · record {numbers[r['rid']]}"

    cards = []
    for n, r in enumerate(pi, 1):
        h, s = hist(r["rid"])
        cards.append(
            f"""<article class="card"><header><span class="tag">Item {n}</span>
<span class="meta">{html.escape(src_label(r))} · {r["type"]} · from the {r["origin"]}</span>
<span class="v v-{r["grade"]}">{r["grade"]}</span></header>
<p class="claim">{html.escape(r["text"])}</p>
<h4>In the history (pass 1a quote, [m{r["msg"]}])</h4><p class="ctx">{around(h, r["quote"])}</p>
<h4>In the summary (pass 1b)</h4><p class="ctx">{
                around(s, r["summary_quote"])
                if r["summary_quote"]
                else "<em>No sentence quoted: graded as not in the summary.</em>"
            }</p>
<p class="foot">After compaction: {r["availability"] or "kept in the summary"}{
                f" ({html.escape(r['availability_how'] or '')})" if r["availability"] else ""
            }</p></article>"""
        )
    for n, r in enumerate(pc, 1):
        h, s = hist(r["rid"])
        ev = (
            around(h, r["evidence_quote"])
            if r["evidence_quote"]
            else "<em>No evidence quoted: nothing in the history supports or contradicts it.</em>"
        )
        cards.append(
            f"""<article class="card"><header><span class="tag">Claim {n}</span>
<span class="meta">{html.escape(src_label(r))}{" · permission" if r["is_permission"] else ""}</span>
<span class="v v-{r["verdict"]}">{r["verdict"]}{
                f" → {r['derived']}" if r.get("derived") else ""
            }</span></header>
<p class="claim">{html.escape(r["claim"])}</p>
<h4>In the summary</h4><p class="ctx">{around(s, r["summary_quote"])}</p>
<h4>Evidence in the history (pass 2)</h4><p class="ctx">{ev}</p>
<p class="foot">{html.escape(r.get("note") or "")}</p></article>"""
        )

    prompts = []
    for name in ("pass1a_items", "pass1b_grade", "pass2_claims"):
        text = (ROOT / "src" / "compaction" / "s1_prompts" / f"{name}.md").read_text()
        prompts.append(
            f"<details><summary>{name}.md</summary><pre>{html.escape(text)}</pre></details>"
        )

    page = TEMPLATE.replace("{{PROMPTS}}", "\n".join(prompts)).replace(
        "{{CARDS}}", "\n".join(cards)
    )
    OUT.write_text(page)
    print(
        f"S1_PROMPT_REVIEW OK {len(pi)} items, {len(pc)} claims ({n_shared} from records that "
        f"also hold blind units; none from gold windows), {len(page):,} bytes -> {OUT}"
    )
    return 0


TEMPLATE = """<title>Judge prompt review</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">
<style>
/* One reading column: the prompts folded at the top, then ten judged examples as sheets. */
:root {
  --bg: #f3f4f1; --paper: #fcfcfa; --ink: #1d2422; --muted: #5d6763; --rule: #d6dad5;
  --accent: #2f5d50; --mark: #f6e7a8; --bad: #a3392f; --ok: #2f6b3c; --maybe: #8a6412;
  --f-ui: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --f-read: "Source Serif 4", Georgia, "Times New Roman", serif;
  --f-mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #151a19; --paper: #1d2322; --ink: #e4e8e5; --muted: #9aa4a0; --rule: #333b39;
  --accent: #8cc2b1; --mark: #5a4c16; --bad: #f0958a; --ok: #8fd19c; --maybe: #e2bf6e;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #151a19; --paper: #1d2322; --ink: #e4e8e5; --muted: #9aa4a0; --rule: #333b39;
  --accent: #8cc2b1; --mark: #5a4c16; --bad: #f0958a; --ok: #8fd19c; --maybe: #e2bf6e;
  color-scheme: dark; }
body { background: var(--bg); color: var(--ink); font-family: var(--f-ui); }
.wrap { max-width: 760px; margin: 0 auto; display: grid; gap: 20px; padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); }
h1 { font-family: var(--f-read); font-weight: 600; font-size: 1.7rem; margin: 0;
  text-wrap: balance; }
h2 { font-size: .8rem; letter-spacing: .08em; text-transform: uppercase; color: var(--muted);
  margin: 12px 0 0; }
h4 { font-size: .75rem; letter-spacing: .06em; text-transform: uppercase; color: var(--muted);
  margin: 0; }
p { margin: 0; line-height: 1.55; }
.lead { font-family: var(--f-read); font-size: 1.05rem; max-width: 65ch; }
details { background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; }
summary { cursor: pointer; padding: 10px 14px; font-family: var(--f-mono); font-size: .85rem; }
summary:focus-visible, .card:focus-visible { outline: 2px solid var(--accent); }
pre { margin: 0; padding: 0 14px 14px; white-space: pre-wrap; overflow-wrap: anywhere;
  font-family: var(--f-mono); font-size: .78rem; line-height: 1.5; color: var(--ink); }
.card { background: var(--paper); border: 1px solid var(--rule); border-radius: 6px;
  padding: 16px; display: grid; gap: 10px; min-width: 0; }
.card header { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: baseline; }
.tag { font-weight: 600; }
.meta { color: var(--muted); font-size: .85rem; flex: 1 1 auto; }
.v { font-family: var(--f-mono); font-size: .8rem; padding: 2px 8px; border-radius: 99px;
  border: 1px solid currentColor; }
.v-dropped, .v-unsupported, .v-contradicted, .v-misstated { color: var(--bad); }
.v-kept, .v-supported { color: var(--ok); }
.claim { font-family: var(--f-read); font-size: 1.05rem; }
.ctx { font-family: var(--f-read); font-size: .92rem; color: var(--ink); overflow-wrap: anywhere;
  border-left: 2px solid var(--rule); padding-left: 12px; }
mark { background: var(--mark); color: var(--ink); padding: 0 2px; }
.foot { color: var(--muted); font-size: .85rem; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
<div class="wrap">
  <header style="display:grid;gap:8px">
    <h1>Judge prompt review</h1>
    <p class="lead">The three judge prompts, and what Haiku 5.5 did with them on ten development
    examples: five items it listed from a history and graded against the summary, and five claims
    it listed from a summary and checked against the history. None of these ten is on your
    labelling page, and none comes from a gold window. Say in chat, or comment on this page, if a verdict or a definition looks wrong;
    the prompts are tuned on the development half only, then frozen.</p>
  </header>
  <h2>Prompts</h2>
  {{PROMPTS}}
  <h2>Ten judged examples</h2>
  {{CARDS}}
</div>
"""

if __name__ == "__main__":
    raise SystemExit(main())
