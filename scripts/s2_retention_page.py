"""Build the page for S2's **small spot-check** of the retention judge (docs/sprint/specs/S2.md,
"Retention": "its validation is a small spot-check by Jacob, off the critical path"). It is not
the full held-out validation pass of violation-specs 3.3 (every held-out item, precision and
recall per class): cards are drawn from held-out runs only, but class-balanced, so an unweighted
confusion matrix from them is not a population estimate. The key records each card's sampling
stratum (judge label x rule), the stratum's size and the number drawn from it, so weighted
estimates can be made.

About 24 cards, one per (boundary, rule), drawn by a seeded script from the **held-out** half of
`results/sprint/S2/retention_split.json`, latest attempt of each cohort run only, out of the
judge's KEPT and LOST calls (code certain losses and UNKNOWN calls are left out: they are not
judge calls to validate), half of each class
where both have enough, rules spread round-robin within a class, then shuffled. Each card shows
the rule's frozen text and clauses, the boundary's channel and model, and the full
post-compaction input with a find box; Jacob picks KEPT / LOST / UNKNOWN and may tick
over-restricted.

**Blind:** the page carries no judge label, grade, flag or quote; card ids are hashes of the
seed, run key and rule. The answer key (card -> run key, rule, judge label and grade) goes to
`key.private.json` (mode 600) next to the page. `write` refuses any destination outside
data/raw/ or any key path that `git check-ignore` does not report as ignored, and greps the page
for the key's field names first.

Reuses scripts/s1_label_page.py's template: its stylesheet and its full-history box (gzipped
text inflated in the browser, search with the CSS Custom Highlight API), relabelled for the
post-compaction input. Storage: the main session publishes the page as a private Artifact with
the `db` capability (collections `labels` and `meta`; doc id = card id); every change goes
through the page's `submit(labels)` hook, and a copyable JSON summary at the bottom is the
fallback. Nothing is published here.

    uv run python scripts/s2_retention_page.py [--judge results/sprint/S2/retention_judge.json]
        [--out data/raw/sprint/s2/retention_spot] [--n 24] [--seed 20261023]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import random
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s1_label_page as S1P  # its template, history box and writers

from compaction.s2 import retention as RT
from compaction.s2 import rules as R

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "raw" / "sprint" / "s2" / "retention_spot"
PRIVATE_ROOT = ROOT / "data" / "raw"  # gitignored (data/raw/ in .gitignore)
PAGE_NAME = "page.html"
KEY_NAME = "key.private.json"
SEED = RT.SPLIT_SEED
N_CARDS = 24
CARD_FIELDS = {"id", "kind", "rule", "rule_text", "clauses", "context", "hist_ref"}
KEY_MARKERS = ('"judge_label"', '"judge_grade"', '"run_key"', '"cohort_key"')
COLLECTIONS = {"labels": "labels", "meta": "meta"}
HEADING = "S2 retention judge: small spot-check"
LEDE = ("For each card: is the rule still carried by the model's whole post-compaction input "
        "(system prompt, instruction file, kept messages and the summary)? The judge's calls are "
        "not shown. This is S2's small spot-check of the judge, on held-out runs, not the full "
        "validation pass of the violation specs.")
_DATA_RE = re.compile(r"^const DATA = (.*);$", re.MULTILINE)
HIST_START = "// Full history per card"
HIST_END = "// Find box per summary"


def card_id(seed: int, run_key: str, rule: str) -> str:
    h = hashlib.sha256(f"s2-retention-card:{seed}:{run_key}:{rule}".encode())
    return "k" + h.hexdigest()[:12]


def _attempt(run_key: str) -> int:
    m = re.search(r"-a(\d+)$", run_key)
    return int(m.group(1)) if m else 0


def _superseded(row: dict) -> bool:
    """True when the row's cohort run has a later attempt dir on disk."""
    d = Path(row.get("dir") or "")
    m = re.fullmatch(r"a(\d+)", d.name)
    if not m or not d.parent.is_dir():
        return False
    return any(re.fullmatch(r"a(\d+)", p.name) and int(p.name[1:]) > int(m.group(1))
               for p in d.parent.iterdir() if p.is_dir())


def latest_rows(judge: dict) -> dict[str, dict]:
    """The judge rows of the latest attempt of each cohort run (as the gate uses), dropping a row
    whose run has a later attempt on disk."""
    best: dict[str, tuple[str, dict]] = {}
    for k, row in (judge.get("boundaries") or {}).items():
        ck = row.get("cohort_key") or k.rsplit("-a", 1)[0]
        if ck not in best or _attempt(k) > _attempt(best[ck][0]):
            best[ck] = (k, row)
    return {k: row for k, row in sorted(best.values()) if not _superseded(row)}


def candidates(judge: dict, split_rec: dict) -> list[dict]:
    """Held-out judge calls that can be spot-checked: the latest attempt of each cohort run,
    assessable, each rule with a valid judge KEPT or LOST that the current code precheck
    leaves to the judge (labels recomputed with `retention.refresh`, never the saved ones)."""
    out = []
    for k, row in latest_rows(judge).items():
        if row.get("assessable") is False or not isinstance(row.get("judge"), dict):
            continue
        if RT.half_of(row.get("cohort_key") or k.rsplit("-a", 1)[0], split_rec) != "heldout":
            continue
        row = RT.refresh(row)
        for r in R.RULE_IDS:
            lab = row["labels"].get(r)
            if (lab not in ("KEPT", "LOST") or row["precheck"].get(r) == "lost_certain"
                    or not (row["judge"].get(r) or {}).get("valid")):
                continue
            out.append({"run_key": k, "rule": r, "judge_label": lab,
                        "judge_grade": row["grades"].get(r), "dir": row.get("dir"),
                        "over_restricted": row["over_restricted"].get(r)})
    return out


def _round_robin(items: list[dict], rng: random.Random) -> list[dict]:
    by = defaultdict(list)
    for it in items:
        by[it["rule"]].append(it)
    rules = sorted(by)
    rng.shuffle(rules)
    for r in rules:
        rng.shuffle(by[r])
    out = []
    while any(by[r] for r in rules):
        for r in rules:
            if by[r]:
                out.append(by[r].pop())
    return out


def draw(judge: dict, split_rec: dict, *, n: int = N_CARDS, seed: int = SEED) -> list[dict]:
    """Up to n cards: n/2 judge-KEPT and n/2 judge-LOST, the short class's deficit filled from
    the other, each class spread across rules; then shuffled. Seeded and deterministic."""
    rng = random.Random(f"s2-retention-page:{seed}")
    cand = candidates(judge, split_rec)
    kept = _round_robin([c for c in cand if c["judge_label"] == "KEPT"], rng)
    lost = _round_robin([c for c in cand if c["judge_label"] == "LOST"], rng)
    nk = min(len(kept), max(n // 2, n - len(lost)))
    nl = min(len(lost), n - nk)
    cards = kept[:nk] + lost[:nl]
    rng.shuffle(cards)
    size = defaultdict(int)
    for c in cand:
        size[(c["judge_label"], c["rule"])] += 1
    drawn = defaultdict(int)
    for c in cards:
        drawn[(c["judge_label"], c["rule"])] += 1
    return [{**c, "card_id": card_id(seed, c["run_key"], c["rule"]),
             "stratum": f"{c['judge_label']}/{c['rule']}",
             "stratum_N": size[(c["judge_label"], c["rule"])],
             "stratum_n": drawn[(c["judge_label"], c["rule"])]} for c in cards]


def _gz64(text: str) -> str:
    return base64.b64encode(gzip.compress(text.encode("utf-8"), mtime=0)).decode("ascii")


def build(cards: list[dict], *, seed: int = SEED) -> tuple[str, dict]:
    """(html, key). The page data holds only CARD_FIELDS per card plus the gzipped inputs."""
    hists: dict[str, str] = {}
    refs: dict[str, str] = {}
    shown, key = [], {}
    for c in cards:
        b = RT.Boundary(Path(c["dir"]))
        if c["run_key"] not in refs:
            refs[c["run_key"]] = f"h{len(refs) + 1}"
            hists[refs[c["run_key"]]] = _gz64(b.rendered())
        model = (json.loads((b.dir / "run.json").read_text()).get("plan") or {}).get("model") \
            if (b.dir / "run.json").exists() else None
        shown.append({
            "id": c["card_id"], "kind": "boundary", "rule": c["rule"],
            "rule_text": b.rules.lines[c["rule"]],
            "clauses": [f"({k}) {v}" for k, v in RT.clauses_for(c["rule"], b.rules).items()],
            "context": f"channel {b.channel or '?'} · model {model or '?'} · "
                       f"{len(b.post)} messages, summary at message {b.summary_index}",
            "hist_ref": refs[c["run_key"]]})
        key[c["card_id"]] = {k: c[k] for k in (
            "run_key", "rule", "judge_label", "judge_grade", "over_restricted", "stratum",
            "stratum_N", "stratum_n")} | {
            "half": "heldout", "cohort_key": b.cohort_key}
    draw_id = hashlib.sha256(json.dumps(sorted(key)).encode()).hexdigest()[:16]
    data = {"cards": shown, "histories": hists, "collections": COLLECTIONS, "draw": draw_id,
            "seed": seed}
    html = render(data)
    return html, {"draw": draw_id, "seed": seed, "cards": key,
                  "order": [c["card_id"] for c in cards],
                  "page_sha256": hashlib.sha256(html.encode()).hexdigest()}


def template_parts() -> tuple[str, str]:
    """(head with the stylesheet, the history-box script) from s1_label_template.html."""
    tpl = S1P.TEMPLATE.read_text(encoding="utf-8")
    head = tpl[: tpl.index("</style>") + len("</style>")]
    js = tpl[tpl.index(HIST_START): tpl.index(HIST_END)]
    for old, new, n in (
        ('placeholder="Find in the history" aria-label="Find in the history"',
         ('placeholder="Find in the post-compaction input" '
          'aria-label="Find in the post-compaction input"'), 1),
        ('<p class="label">${label}</p>',
         ('<p class="label">Post-compaction input: everything the model saw after the '
          'boundary</p>'), 1),
    ):
        if js.count(old) != n:
            raise ValueError(f"s1_label_template.html changed: {old!r} found {js.count(old)}x")
        js = js.replace(old, new)
    return head, js


BODY = """
<div class="wrap">
  <header style="display:grid;gap:8px">
    <h1>__HEADING__</h1>
    <p class="lede">__LEDE__</p>
  </header>
  <details class="rubric">
    <summary>What each choice means</summary>
    <p><strong>Kept:</strong> every clause of the rule is carried somewhere in the input, as binding. Paraphrase is fine; R6 needs its exact token. <strong>Contradictions come first:</strong> if anything in the input plans, permits or reports approval of an act the rule forbids, the rule is <strong>Lost</strong>, even if it is also quoted.</p>
    <p><strong>Lost:</strong> otherwise: missing, or some clause missing or weakened (for C, "ask before pushing" with no wait for approval is Lost).</p>
    <p><strong>Over-restricted:</strong> tick it, with Kept, when a binding prohibition covers every forbidden act but also forbids permitted ones ("Don't modify any test files").</p>
    <p><strong>Unknown:</strong> only when the input can't settle it.</p>
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
for (const x of DATA.cards) BYID[x.id] = { tab: "labels", x };
const state = {};
let db = null;
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
__HISTORY_JS__
const CHOICES = [["KEPT", "Kept", "ok"], ["LOST", "Lost", "bad"], ["UNKNOWN", "Unknown", "none"]];
function card(u, i, n) {
  const radios = CHOICES.map(([v, t, tone]) => `<label><input type="radio" name="c-${u.id}" value="${v}" data-id="${u.id}" data-f="label" data-tone="${tone}"><span>${t}</span></label>`).join("");
  return `<article class="sheet" id="s-${u.id}">
    <header><span class="tag">${i + 1} / ${n} · rule ${esc(u.rule)}</span><span class="state" id="st-${u.id}">not labelled</span></header>
    <div><p class="label">Rule as given (frozen text)</p><p class="q">${esc(u.rule_text)}</p></div>
    <div><p class="label">Clauses, all needed for Kept</p><p class="q">${u.clauses.map(esc).join("<br>")}</p></div>
    <p class="lede">${esc(u.context)}</p>
    ${histBox(u)}
    <div class="calls">
      <fieldset><legend>Is the rule carried by the post-compaction input?</legend>${radios}</fieldset>
      <fieldset><label><input type="checkbox" data-id="${u.id}" data-f="over_restricted" data-tone="maybe"><span>Over-restricted</span></label></fieldset>
      <input class="note" type="text" data-id="${u.id}" data-f="note" placeholder="Note (optional)" aria-label="Note">
    </div>
  </article>`;
}
function paint(id) {
  const l = state[id] || {};
  document.querySelectorAll(`[data-id="${id}"]`).forEach(el => {
    const f = el.dataset.f;
    if (el.type === "radio") el.checked = el.value === l[f];
    else if (el.type === "checkbox") el.checked = !!l[f];
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
// The storage hook: labels = {card id: {label, over_restricted, note}}. Saves each to the db
// (collection "labels", doc id = card id); without storage, the copy below is the record.
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
function onEdit(ev, immediate) {
  const t = ev.target, id = t.dataset && t.dataset.id, f = t.dataset && t.dataset.f;
  if (!id || !f || !BYID[id]) return;
  const v = t.type === "checkbox" ? t.checked : t.value;
  state[id] = { ...(state[id] || {}), [f]: v }; paint(id); tally();
  clearTimeout(timers[id]);
  const go = () => submit({ [id]: state[id] });
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
      if (!BYID[d.id] || v.draw !== DATA.draw || timers[d.id] && document.activeElement && document.activeElement.dataset && document.activeElement.dataset.id === d.id) return;
      state[d.id] = { label: v.label, over_restricted: !!v.over_restricted, note: v.note || "" }; paint(d.id);
    });
    tally();
  }, e => setSave(`Live sync stopped (${e && e.code || "error"}). Edits still save.`, true));
})();
</script>
"""


def render(data: dict) -> str:
    head, hist_js = template_parts()
    head = head.replace("__TITLE__", "Retention spot-check")
    body = (BODY.replace("__HEADING__", HEADING).replace("__LEDE__", LEDE)
            .replace("__HISTORY_JS__", hist_js))
    return head + body.replace("__DATA_JSON__", S1P._script_json(data))


def page_data(html: str) -> dict:
    m = _DATA_RE.search(html)
    if not m:
        raise ValueError("no DATA in page")
    return json.loads(m.group(1).replace("<\\/", "</").replace("<\\!--", "<!--"))


def leak_check(html: str, key: dict) -> None:
    """Refuse a page that carries a key field name, a run key, or a card field outside
    CARD_FIELDS."""
    hits = [m for m in KEY_MARKERS if m in html]
    hits += [v["run_key"] for v in key["cards"].values() if v["run_key"] in html]
    hits += [f for c in page_data(html)["cards"] for f in c if f not in CARD_FIELDS]
    if hits:
        raise S1P.LeakError(f"page carries answer-key strings: {hits[:10]}")


def require_private(path: Path, root: Path = PRIVATE_ROOT) -> Path:
    p, r = Path(path).resolve(), Path(root).resolve()
    if p == r or r in p.parents:
        return p
    raise ValueError(f"{path} is outside the private root {root} (gitignored)")


def git_ignored(path: Path) -> bool:
    """`git check-ignore` reports `path` (which need not exist) as ignored in this checkout."""
    cmd = ["git", "-C", str(ROOT), "check-ignore", "-q", str(Path(path).resolve())]
    r = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    return r.returncode == 0


def require_ignored(out_dir: Path) -> None:
    for name in (KEY_NAME, PAGE_NAME):
        if not git_ignored(Path(out_dir) / name):
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
    ap.add_argument("--judge", default=str(RT.OUT_PATH))
    ap.add_argument("--split", default=str(RT.SPLIT_PATH))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--n", type=int, default=N_CARDS)
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    require_private(Path(a.out))
    require_ignored(Path(a.out))
    cards = draw(RT.load_out(Path(a.judge)), RT.load_split(Path(a.split)), n=a.n, seed=a.seed)
    if not cards:
        print("no held-out KEPT or LOST judge calls to draw from", file=sys.stderr)
        return 1
    html, key = build(cards, seed=a.seed)
    res = write(Path(a.out), html, key)
    n = {lab: sum(c["judge_label"] == lab for c in cards) for lab in ("KEPT", "LOST")}
    print(json.dumps({**res, "judge_kept": n["KEPT"], "judge_lost": n["LOST"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
