"""Build the **live-loss calibration** page of the Experiment 2 close-out (plan unit 4,
docs/sprint/plans/experiment-2-closeout.md): Jacob checks, blind, a seeded sample of the S1 loss
pipeline's item grades on the pilot's live boundaries, so the judge's precision on "lost" is
measured on live runs before any live loss rate is reported.

Input: `results/sprint/S2/lossaudit/losses.jsonl` (scripts/s2_lossaudit.py), one row per judged
item: S1's item columns (`grade` final after the second look) plus run_key, subject, half,
lost, boundary_dir and the rest of the close-out's contract.

**Draw** (seeded, deterministic): 30 cards, 20 judge-"lost" (`lost` true: dropped or misstated
in the summary and not still present in the kept context) and 10 judge-"kept" (valid, graded
kept). Strata are judge class x subject model; each class's quota is split equally across the
subjects (the remainder to seeded subjects), and within a stratum the cards are a simple random
sample of its items (seeded), so each item's inclusion probability is exactly n / N. (Review r1
#1: dealing round-robin across boundaries over-drew items of small boundaries, so recorded
probabilities were wrong; spread across boundaries is reported, not forced.) Cards come from
the **held-out** half (`retention_split.json`); a stratum
short of its quota there is filled from the development half of the same stratum (marked
`fill: "development"`), and a class still short takes the remaining cards from the other
subjects, held-out first (`fill: "other_subject"`). The key records each card's stratum, half,
the stratum's size in that half (`stratum_N`), the number drawn from it (`stratum_n`) and the
inclusion probability `stratum_n / stratum_N`, so the scorer can weight.

**Blind:** the page carries no grade, availability, lost flag, summary quote or second-look
data; card ids are hashes of the seed, run key and item id; card order is shuffled. Each card
shows the item and its source quote (an excerpt of the quoted message with the quote marked,
and the full history scrolled to it), then what the agent resumed with: the summary and the
other post-compaction messages (kept tail, re-attached AGENTS.md), each in a collapsible box
with a find box. Jacob picks kept / dropped / misstated / not an item / can't tell, with an
optional note. The answer key goes to `key.private.json` (mode 600) next to the page, under
gitignored data/raw/ only; `write` refuses other destinations and runs `leak_check` first.

Reuses scripts/s2_retention_page.py's structure (s1_label_template.html's stylesheet and
history box, gzipped payloads, `submit(labels)` hook, db collections `labels` and `meta` with
doc id = card id, copyable JSON fallback). Nothing is published here: the main session
publishes the page as a private Artifact with the `db` capability.

    uv run python scripts/s2_losscal_page.py [--losses results/sprint/S2/lossaudit/losses.jsonl]
        [--out data/raw/sprint/s2/losscal] [--n-lost 20] [--n-kept 10] [--seed 20261024]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s1_label_page as S1P  # its template, history box and writers
import s2_retention_page as RP  # private-destination checks, gzip payloads

from compaction.s2 import retention as RT

ROOT = Path(__file__).resolve().parents[1]
LOSSES = ROOT / "results" / "sprint" / "S2" / "lossaudit" / "losses.jsonl"
OUT_DIR = ROOT / "data" / "raw" / "sprint" / "s2" / "losscal"
PRIVATE_ROOT = RP.PRIVATE_ROOT
PAGE_NAME = RP.PAGE_NAME
KEY_NAME = RP.KEY_NAME
SEED = 20261024
N_LOST = 20
N_KEPT = 10
CLASSES = ("lost", "kept")
LABELS = ("kept", "dropped", "misstated", "not_an_item", "cant_tell")
EXCERPT_CHARS = 700  # characters of the quoted message shown before and after the quote
CARD_FIELDS = {"id", "kind", "item_type", "text", "ctx_quote", "excerpt", "located", "hist_at",
               "hist_ref", "sum_ref", "post_ref", "context"}
KEY_MARKERS = ('"judge_grade"', '"first_grade"', '"judge_class"', '"availability"',
               '"summary_quote"', '"second_look"', '"stratum"', '"inclusion_p"', '"run_key"',
               '"cohort_key"', '"item_id"')
COLLECTIONS = {"labels": "labels", "meta": "meta"}
HEADING = "Experiment 2: live-loss calibration"
LEDE = ("Each card is one actionable item the judge listed from a live agent's history before "
        "compaction. Is it still carried by what the agent resumed with (the summary, plus the "
        "kept messages and the re-attached AGENTS.md)? The judge's grades are not shown.")
_DATA_RE = RP._DATA_RE
HIST_START = RP.HIST_START
HIST_END = RP.HIST_END


def card_id(seed: int, run_key: str, item_id: str) -> str:
    h = hashlib.sha256(f"s2-losscal-card:{seed}:{run_key}:{item_id}".encode())
    return "c" + h.hexdigest()[:12]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def judge_class(row: dict) -> str | None:
    """'lost' (dropped or misstated and not still present: the contract's `lost`), 'kept'
    (valid, graded kept) or None (not drawn: invalid, ungraded, or lost from the summary but
    still present in the kept context)."""
    if not (row.get("valid") and row.get("grade_valid")):
        return None
    if row.get("lost") is True:
        return "lost"
    if row.get("grade") == "kept" and not row.get("lost"):
        return "kept"
    return None


def _permuted(rows: list[dict], rng: random.Random) -> list[dict]:
    """A seeded random permutation of `rows` (in a canonical order first). Taking the first n
    is a simple random sample: every item's inclusion probability is n / len(rows)."""
    out = sorted(rows, key=lambda r: (r["run_key"], str(r["item_id"])))
    rng.shuffle(out)
    return out


def _quotas(n: int, subjects: list[str], rng: random.Random) -> dict[str, int]:
    base, rem = divmod(n, len(subjects)) if subjects else (0, 0)
    extra = subjects[:]
    rng.shuffle(extra)
    return {s: base + (s in extra[:rem]) for s in subjects}


def draw(rows: list[dict], *, n_lost: int = N_LOST, n_kept: int = N_KEPT,
         seed: int = SEED) -> list[dict]:
    """The cards (dicts: the row's fields plus card_id, judge_class, stratum, half, stratum_N,
    stratum_n, inclusion_p, fill), shuffled. Seeded and deterministic for the same rows."""
    rng = random.Random(f"s2-losscal-page:{seed}")
    pools: dict[tuple[str, str, str], list[dict]] = {}
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        cls = judge_class(r)
        if cls is not None and r.get("half") in RT.HALVES:
            groups[(cls, str(r.get("subject")), r["half"])].append(r)
    for k in sorted(groups):
        pools[k] = _permuted(groups[k], rng)
    sizes = {k: len(v) for k, v in pools.items()}
    picked: list[tuple[dict, str, str, str | None]] = []  # (row, cls, half, fill)
    for cls, n in (("lost", n_lost), ("kept", n_kept)):
        subjects = sorted({s for c, s, _ in pools if c == cls})
        quota = _quotas(n, subjects, rng)
        short = 0
        for s in subjects:
            want = quota[s]
            for half, fill in (("heldout", None), ("development", "development")):
                pool = pools.get((cls, s, half), [])
                while want and pool:
                    picked.append((pool.pop(0), cls, half, fill))
                    want -= 1
            short += want
        for half in ("heldout", "development"):  # the class's remaining deficit
            order = [s for s in subjects if pools.get((cls, s, half))]
            while short and any(pools.get((cls, s, half)) for s in order):
                for s in order:
                    pool = pools.get((cls, s, half))
                    if short and pool:
                        picked.append((pool.pop(0), cls, half, "other_subject"))
                        short -= 1
    drawn = defaultdict(int)
    for r, cls, half, _ in picked:
        drawn[(cls, str(r.get("subject")), half)] += 1
    cards = []
    for r, cls, half, fill in picked:
        k = (cls, str(r.get("subject")), half)
        cards.append({**r, "card_id": card_id(seed, r["run_key"], str(r["item_id"])),
                      "judge_class": cls, "stratum": f"{cls}/{k[1]}", "half": half,
                      "stratum_N": sizes[k], "stratum_n": drawn[k],
                      "inclusion_p": drawn[k] / sizes[k], "fill": fill})
    rng.shuffle(cards)
    return cards


# ------------------------------------------------------------------------------------------
# Card text
# ------------------------------------------------------------------------------------------


def find_span(text: str, quote: str) -> tuple[int, int] | None:
    """The quote's (start, end) in `text`: exact, else whitespace-normalised, else also
    case-insensitive."""
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    words = quote.split()
    if not words:
        return None
    pat = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pat, text) or re.search(pat, text, re.IGNORECASE)
    return (m.start(), m.end()) if m else None


def render_history(history: list[dict]) -> tuple[str, dict[int, tuple[int, int]]]:
    """The history as one text, one block per message, and each message's text span in it."""
    parts, spans, pos = [], {}, 0
    for m in history:
        head = f"[message {m['i']} · {m.get('role') or '?'}]\n"
        body = m.get("text") or ""
        spans[m["i"]] = (pos + len(head), pos + len(head) + len(body))
        block = head + body + "\n\n"
        parts.append(block)
        pos += len(block)
    return "".join(parts), spans


def render_post(messages: list[dict]) -> str:
    return "".join(f"[message {m['i']} · {m.get('kind') or '?'} · {m.get('role') or '?'}]\n"
                   f"{m.get('text') or ''}\n\n" for m in messages)


def locate(row: dict, text: str, spans: dict) -> dict:
    """Where the row's quote is: its message first (the row's `msg`), then any message."""
    quote = row.get("quote") or ""
    order = sorted(spans, key=lambda i: (i != row.get("msg"), i))
    for i in order:
        a, b = spans[i]
        hit = find_span(text[a:b], quote)
        if hit:
            s, e = a + hit[0], a + hit[1]
            lo, hi = max(a, s - EXCERPT_CHARS), min(b, e + EXCERPT_CHARS)
            return {"located": True, "hist_at": [s, e],
                    "excerpt": {"msg": i, "before": ("…" if lo > a else "") + text[lo:s],
                                "mark": text[s:e], "after": text[e:hi] + ("…" if hi < b else "")}}
    return {"located": False, "hist_at": None, "excerpt": None}


def _view(boundary_dir: str) -> dict:
    from compaction.s2 import lossaudit  # unit 1; imported here so tests can inject a fake

    return lossaudit.boundary_view(boundary_dir)


def build(cards: list[dict], *, seed: int = SEED,
          view: Callable[[str], dict] | None = None) -> tuple[str, dict]:
    """(html, key). The page data holds only CARD_FIELDS per card plus the gzipped texts."""
    view = view or _view
    views: dict[str, dict] = {}
    texts: dict[str, str] = {}
    refs: dict[str, dict] = {}
    shown, key = [], {}
    for c in cards:
        bd = str(c["boundary_dir"])
        if bd not in views:
            v = views[bd] = view(bd)
            n = len(refs) + 1
            hist_text, spans = render_history(v["history"])
            others = [m for m in v["post_messages"] if m.get("kind") != "summary"]
            refs[bd] = {"hist": f"h{n}", "sum": f"s{n}", "post": f"p{n}", "spans": spans,
                        "hist_text": hist_text, "u16": S1P.utf16_offsets(hist_text)}
            texts[f"h{n}"] = RP._gz64(hist_text)
            texts[f"s{n}"] = RP._gz64(v.get("summary") or "")
            texts[f"p{n}"] = RP._gz64(render_post(others))
        v, ref = views[bd], refs[bd]
        loc = locate(c, ref["hist_text"], ref["spans"])
        if loc["hist_at"]:  # the browser's Range offsets are UTF-16 code units (review r1 #2)
            loc["hist_at"] = [ref["u16"](x) for x in loc["hist_at"]]
        n_post = len(v["post_messages"])
        shown.append({
            "id": c["card_id"], "kind": "item", "item_type": c.get("type"),
            "text": c.get("text") or "", "ctx_quote": c.get("quote") or "",
            "excerpt": loc["excerpt"], "located": loc["located"], "hist_at": loc["hist_at"],
            "hist_ref": ref["hist"], "sum_ref": ref["sum"], "post_ref": ref["post"],
            "context": f"channel {c.get('channel') or '?'} · model {c.get('subject') or '?'} · "
                       f"summarizer {c.get('policy') or '?'} · {len(v['history'])} history "
                       f"messages · {n_post} messages after compaction"})
        key[c["card_id"]] = {k: c.get(k) for k in (
            "run_key", "cohort_key", "item_id", "rid", "type", "subject", "channel", "policy",
            "task", "planted_rule", "half", "stratum", "stratum_N", "stratum_n", "inclusion_p",
            "fill", "judge_class", "availability", "summary_quote")} | {
            "judge_grade": c.get("grade"), "first_grade": c.get("first_grade"),
            "second_look": c.get("second_look")}
    # the fingerprint covers what the page shows, so labels never attach to revised cards
    # (review r1 #8)
    evidence = json.dumps({"ids": sorted(key), "cards": shown, "texts": texts}, sort_keys=True)
    draw_id = hashlib.sha256(evidence.encode()).hexdigest()[:16]
    data = {"cards": shown, "histories": texts, "collections": COLLECTIONS, "draw": draw_id,
            "seed": seed}
    html = render(data)
    return html, {"draw": draw_id, "seed": seed, "cards": key,
                  "order": [c["card_id"] for c in cards],
                  "page_sha256": hashlib.sha256(html.encode()).hexdigest()}


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def template_parts() -> tuple[str, str]:
    """(head with the stylesheet, the history-box script) from s1_label_template.html, with the
    box label and find-box wording taken from the card when it gives them."""
    tpl = S1P.TEMPLATE.read_text(encoding="utf-8")
    head = tpl[: tpl.index("</style>") + len("</style>")]
    js = tpl[tpl.index(HIST_START): tpl.index(HIST_END)]
    for old, new, n in (
        ('placeholder="Find in the history" aria-label="Find in the history"',
         ('placeholder="Find in ${u.find_in || "the history"}" '
          'aria-label="Find in ${u.find_in || "the history"}"'), 1),
        ('<p class="label">${label}</p>', '<p class="label">${u.box_label || label}</p>', 1),
    ):
        if js.count(old) != n:
            raise ValueError(f"s1_label_template.html changed: {old!r} found {js.count(old)}x")
        js = js.replace(old, new)
    return head, js


BODY = """
<style>
  .excerpt { white-space: pre-wrap; max-height: 22em; overflow: auto; }
  .excerpt mark { padding: 0 1px; }
  details.post > summary { cursor: pointer; font-weight: 600; margin: 6px 0; }
</style>
<div class="wrap">
  <header style="display:grid;gap:8px">
    <h1>__HEADING__</h1>
    <p class="lede">__LEDE__</p>
  </header>
  <details class="rubric">
    <summary>What each choice means</summary>
    <p><strong>Kept:</strong> what the agent resumed with still carries the item's actionable content, so the agent could act on it correctly. Paraphrase is fine. It counts if it is in the summary <em>or</em> in a kept message or the re-attached AGENTS.md.</p>
    <p><strong>Dropped:</strong> nothing the agent resumed with carries it.</p>
    <p><strong>Misstated:</strong> it is there, but changed in a way that matters: a wrong value, a scope widened or narrowed, a decision reversed, a permission turned into a ban or the reverse.</p>
    <p><strong>Not an item:</strong> the card isn't a real actionable item from the history (misread, not binding, or superseded before the compaction), so neither kept nor lost applies.</p>
    <p><strong>Can't tell:</strong> only when the texts can't settle it.</p>
  </details>
  <div class="bar" role="status">
    <span class="count" id="count">0 done</span>
    <div class="track" aria-hidden="true"><i id="fill"></i></div>
    <button type="button" class="ghost" id="next">Next unlabelled</button>
    <span class="save" id="save">Connecting to storage…</span>
  </div>
  <section class="panel"><div class="sheets" id="sheets"></div></section>
  <section class="export">
    <p class="label">Copy of your work</p>
    <p class="lede">Everything saves to this page's storage, which Claude reads. If saving fails, copy this and paste it to Claude.</p>
    <textarea id="dump" readonly aria-label="Labels as JSON"></textarea>
    <button type="button" class="act" id="copy">Copy</button>
  </section>
</div>
<script>
const DATA = __DATA_JSON__;
const COLL = DATA.collections;
const BYID = {};
const SUB = {};  // the summary and other-messages boxes of each card, for the history-box code
for (const x of DATA.cards) {
  BYID[x.id] = { tab: "labels", x: { ...x, box_label: x.located ? "Full history before compaction, scrolled to the quote" : "Full history before compaction" } };
  const s = { id: `${x.id}-s`, kind: "post", hist_ref: x.sum_ref, box_label: "The summary", find_in: "the summary" };
  const p = { id: `${x.id}-p`, kind: "post", hist_ref: x.post_ref, box_label: "Every other message after compaction (system prompt, re-attached AGENTS.md, kept messages)", find_in: "these messages" };
  BYID[s.id] = { tab: "box", x: s }; BYID[p.id] = { tab: "box", x: p };
  SUB[x.id] = [s, p];
}
const state = {};
let db = null;
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
__HISTORY_JS__
const CHOICES = [["kept", "Kept", "ok"], ["dropped", "Dropped", "bad"], ["misstated", "Misstated", "bad"], ["not_an_item", "Not an item", "maybe"], ["cant_tell", "Can't tell", "none"]];
function excerpt(u) {
  if (!u.excerpt) return `<p class="warnline">The quote wasn't found in the history. It is: ${esc(u.ctx_quote)}</p>`;
  const e = u.excerpt;
  return `<div><p class="label">Where the quote is (message ${esc(e.msg)})</p><div class="box excerpt">${esc(e.before)}<mark>${esc(e.mark)}</mark>${esc(e.after)}</div></div>`;
}
function card(u, i, n) {
  const radios = CHOICES.map(([v, t, tone]) => `<label><input type="radio" name="c-${u.id}" value="${v}" data-id="${u.id}" data-f="label" data-tone="${tone}"><span>${t}</span></label>`).join("");
  const [s, p] = SUB[u.id];
  return `<article class="sheet" id="s-${u.id}">
    <header><span class="tag">${i + 1} / ${n} · ${esc(u.item_type || "item")}</span><span class="state" id="st-${u.id}">not labelled</span></header>
    <div><p class="label">The item</p><p class="q">${esc(u.text)}</p></div>
    <div><p class="label">Its source quote</p><p class="q">${esc(u.ctx_quote)}</p></div>
    <p class="lede">${esc(u.context)}</p>
    ${excerpt(u)}
    <details class="post"><summary>Full history before compaction</summary>${histBox(BYID[u.id].x)}</details>
    <p class="label">What the agent resumed with</p>
    <details class="post" open><summary>The summary</summary>${histBox(s)}</details>
    <details class="post"><summary>Every other message after compaction</summary>${histBox(p)}</details>
    <div class="calls">
      <fieldset><legend>In what the agent resumed with, this item is…</legend>${radios}</fieldset>
      <input class="note" type="text" data-id="${u.id}" data-f="note" placeholder="Note (optional)" aria-label="Note">
    </div>
  </article>`;
}
function paint(id) {
  const l = state[id] || {};
  document.querySelectorAll(`[data-id="${id}"]`).forEach(el => {
    const f = el.dataset.f;
    if (el.type === "radio") el.checked = el.value === l[f];
    else if (document.activeElement !== el) el.value = l[f] || "";
  });
  document.getElementById(`s-${id}`).classList.toggle("done", !!l.label);
  document.getElementById(`st-${id}`).textContent = l.label ? "labelled" : "not labelled";
}
function tally() {
  const n = DATA.cards.filter(x => state[x.id] && state[x.id].label).length;
  document.getElementById("count").textContent = `${n} / ${DATA.cards.length} done`;
  document.getElementById("fill").style.width = `${100 * n / Math.max(1, DATA.cards.length)}%`;
  const rows = DATA.cards.filter(x => state[x.id]).map(x => ({ id: x.id, collection: COLL.labels, data: { ...state[x.id], draw: DATA.draw } }));
  document.getElementById("dump").value = JSON.stringify(rows, null, 1);
}
function setSave(text, warn) { const el = document.getElementById("save"); el.textContent = text; el.classList.toggle("warn", !!warn); }
// The storage hook: labels = {card id: {label, note}}. Saves each to the db (collection
// "labels", doc id = card id); without storage, the copy below is the record.
async function submit(labels) {
  for (const [id, l] of Object.entries(labels)) { state[id] = { ...(state[id] || {}), ...l }; paint(id); }
  tally();
  if (!db) { setSave("Not saving: storage unavailable. Use the copy below.", true); return false; }
  try {
    for (const [id, l] of Object.entries(labels))
      await db.collection(COLL.labels).doc(id).set({ ...state[id], draw: DATA.draw, utc: new Date().toISOString() });
    setSave("Saved"); return true;
  } catch (e) { setSave(`Not saved (${e && e.code || "error"}). Use the copy below.`, true); return false; }
}
const timers = {};
const dirty = new Set();  // cards with an edit not yet saved: a snapshot never overwrites them
function onEdit(ev, immediate) {
  const t = ev.target, id = t.dataset && t.dataset.id, f = t.dataset && t.dataset.f;
  if (!id || !f || !BYID[id] || BYID[id].tab !== "labels") return;
  state[id] = { ...(state[id] || {}), [f]: t.value }; paint(id); tally();
  dirty.add(id);
  clearTimeout(timers[id]);
  const go = async () => { const sent = JSON.stringify(state[id]); if (await submit({ [id]: state[id] }) && JSON.stringify(state[id]) === sent) dirty.delete(id); };
  if (immediate) go(); else timers[id] = setTimeout(go, 1200);
}
document.addEventListener("change", ev => onEdit(ev, true));
document.addEventListener("input", ev => { if (ev.target.classList.contains("note")) onEdit(ev, false); });
document.getElementById("next").addEventListener("click", () => {
  const x = DATA.cards.find(x => !(state[x.id] && state[x.id].label));
  if (x) document.getElementById(`s-${x.id}`).scrollIntoView({ behavior: "smooth", block: "start" });
});
document.getElementById("copy").addEventListener("click", async () => {
  const ta = document.getElementById("dump");
  try { await navigator.clipboard.writeText(ta.value); setSave("Copied"); }
  catch (e) { ta.select(); setSave("Selected: press copy", true); }
});
document.getElementById("sheets").innerHTML = DATA.cards.map((u, i) => card(u, i, DATA.cards.length)).join("");
DATA.cards.forEach(x => paint(x.id)); tally();
requestAnimationFrame(() => hsObserve());
(async () => {
  try { db = window.claude ? await window.claude.use("db") : null; } catch (e) { db = null; }
  if (!db) { setSave("Not saving: storage unavailable here. Use the copy below.", true); return; }
  setSave("Saving on");
  try { await db.collection(COLL.meta).doc("draw").set({ draw: DATA.draw, seed: DATA.seed, utc: new Date().toISOString() }); } catch (e) { /* labels still carry the draw */ }
  db.collection(COLL.labels).onSnapshot(snap => {
    snap.docs.forEach(d => {
      const v = d.data();
      if (!BYID[d.id] || BYID[d.id].tab !== "labels" || v.draw !== DATA.draw || dirty.has(d.id)) return;
      state[d.id] = { label: v.label, note: v.note || "" }; paint(d.id);
    });
    tally();
  }, e => setSave(`Live sync stopped (${e && e.code || "error"}). Edits still save.`, true));
})();
</script>
"""


def render(data: dict) -> str:
    head, hist_js = template_parts()
    head = head.replace("__TITLE__", "Live-loss calibration")
    body = (BODY.replace("__HEADING__", HEADING).replace("__LEDE__", LEDE)
            .replace("__HISTORY_JS__", hist_js))
    return head + body.replace("__DATA_JSON__", S1P._script_json(data))


def page_data(html: str) -> dict:
    return RP.page_data(html)


def leak_check(html: str, key: dict) -> None:
    """Refuse a page that carries a key field name, a run or cohort key, or a card field outside
    CARD_FIELDS. Grades can't be grepped as values (the choices name them), so blindness rests
    on the card fields and the key-field names."""
    hits = [m for m in KEY_MARKERS if m in html]
    for v in key["cards"].values():
        hits += [v[f] for f in ("run_key", "cohort_key") if v.get(f) and v[f] in html]
    hits += [f for c in page_data(html)["cards"] for f in c if f not in CARD_FIELDS]
    if hits:
        raise S1P.LeakError(f"page carries answer-key strings: {hits[:10]}")


def require_private(path: Path, root: Path = PRIVATE_ROOT) -> Path:
    return RP.require_private(path, root)


def require_ignored(out_dir: Path) -> None:
    for name in (KEY_NAME, PAGE_NAME):
        if not RP.git_ignored(Path(out_dir) / name):
            raise ValueError(f"{Path(out_dir) / name} is not gitignored: refusing to write it")


def write(out_dir: Path, html: str, key: dict, *, private_root: Path = PRIVATE_ROOT,
          check_ignored: bool = True) -> dict:
    """Write the page and key under data/raw/. `private_root` and `check_ignored` exist for
    tests (a tmp dir is outside the checkout); the CLI never changes them."""
    out = require_private(Path(out_dir), private_root)
    if check_ignored:
        require_ignored(out)
    leak_check(html, key)
    S1P._atomic(out / KEY_NAME, (json.dumps(key, indent=1, sort_keys=True) + "\n").encode(),
                mode=0o600)
    S1P._atomic(out / PAGE_NAME, html.encode("utf-8"))
    return {"page": str(out / PAGE_NAME), "key": str(out / KEY_NAME),
            "bytes": len(html.encode("utf-8")), "cards": len(key["cards"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--losses", default=str(LOSSES))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--n-lost", type=int, default=N_LOST)
    ap.add_argument("--n-kept", type=int, default=N_KEPT)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    require_private(Path(a.out))
    require_ignored(Path(a.out))
    cards = draw(read_jsonl(Path(a.losses)), n_lost=a.n_lost, n_kept=a.n_kept, seed=a.seed)
    if not cards:
        print("no judge-lost or judge-kept items to draw from", file=sys.stderr)
        return 1
    html, key = build(cards, seed=a.seed)
    res = write(Path(a.out), html, key)
    count = defaultdict(int)
    for c in cards:
        count[f"{c['judge_class']}/{c['half']}" + (f"/{c['fill']}" if c["fill"] else "")] += 1
    print(json.dumps({**res, "drawn": dict(sorted(count.items()))}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
