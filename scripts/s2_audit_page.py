"""The S2 pilot's release audit (sprint plan R-A3: "pilot traces, summaries and each arm's first
actions"; docs/sprint/specs/S2.md, "`seal.prepare_audit` and `seal.release` follow S1's
protocol"): build the audit page, then release the sealed S2 outcomes once Jacob has checked it.

    uv run python scripts/s2_audit_page.py build [--out data/raw/sprint/s2/audit] [--n 20]
    uv run python scripts/s2_audit_page.py release --labels <ArtifactData export of `audit`> \
        [--out data/raw/sprint/s2/audit] [--by jacob] [--note "..."]

**What the audit checks:** the harness, not outcomes. Each card is one arm continuation of one
pilot boundary: what the arm was given at the fork (F = the pre-compaction history; S = the
post-compaction context; S+R = S with the four planted rules appended to the summary message;
S+I = S with four length-matched irrelevant facts appended there; built here with
`loop.arm_context`, the function the runs used, and S+R and S+I always get all four rules, as
`pilot.py` runs them), that starting context in full with a find box, the first call's measured
prompt tokens against the context's estimate, and the arm's first `N_ACTIONS` agent turns
(assistant text and tool calls, then each tool's output, truncated to `TRUNC` characters). Jacob
marks each card "Harness OK" / "Problem" / "Can't tell", and answers once "Release the sealed S2
outcomes?" (yes / not yet).

**Not shown:** `scored.json`, `model.patch`, the sealed outcome files and every outcome field of
`result.json` (asks, commits, patch size, early submission). Only its harness fields `end` and
`error` are shown. Card fields are a fixed set (`CARD_FIELDS`), and `leak_check` refuses a page
carrying any sealed outcome field name as a JSON key, or a sealed file name.

**Draw (seeded, `SEED`):** the latest valid attempt of each cohort run
(`retention.boundary_dirs`), arms with a written `continuation.json` only. 20 cards, 5 per arm;
each slot takes the candidate whose model, channel, task and boundary have been used least so far
(seeded tiebreak), and the replicate (1 or 2) of each (boundary, arm) is picked by the seed.

**Protocol (S1's, scripts/s1_audit.py):** `build` captures `seal.prepare_audit("S2")` and keeps it
in the key; the draw id hashes the manifest digest and every card's content, so labels saved
against another build are refused. `release` refuses unless every card is marked, no card is
"Problem" and the release answer is "yes"; it snapshots the labels into one canonical file and
calls `seal.release("S2", [snapshot, key], manifest=...)`, which refuses if any sealed file changed
since `build` (`seal.AuditStale`). A refusal means: fix, rerun what was affected, build a fresh
audit (new seed). There is no override.

Storage: the main session publishes page.html as a private Artifact with the `db` capability
(collection `audit`, doc id = card id, plus doc `release`); a copyable JSON summary at the bottom
is the fallback. Nothing is published here. The key (`key.private.json`, mode 600) and page go to
data/raw/ (gitignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import s1_label_page as S1P  # template, writers
import s2_retention_page as RP  # privacy guards, gz payload, history box

from compaction import seal
from compaction.s2 import config as C
from compaction.s2 import loop as L
from compaction.s2 import model as M
from compaction.s2 import retention as RT
from compaction.s2 import rules as R

STAGE = "S2"
OUT_DIR = ROOT / "data" / "raw" / "sprint" / "s2" / "audit"
PAGE_NAME = "page.html"
KEY_NAME = "key.private.json"
SNAPSHOT = "labels_snapshot.json"
RELEASE_RECORD = "release_record.json"
SEED = 20261024
N_CARDS = 20
N_ACTIONS = 5
TRUNC = 1500
ARM_SLUGS = ("F", "S", "SR", "SI")
ARM_OF_SLUG = {"F": "F", "S": "S", "SR": "S+R", "SI": "S+I"}
GIVEN = {
    "F": "Full history: the pre-compaction messages, unchanged (no summary).",
    "S": "Summary only: the post-compaction context the run itself continued with.",
    "SR": "Summary + the four planted rules, appended to the summary message.",
    "SI": "Summary + four length-matched irrelevant facts, appended to the summary message.",
}
LABELS = ("ok", "problem", "cant_tell")
RELEASE_ANSWERS = ("yes", "not_yet")
RELEASE_DOC = "release"
COLLECTIONS = {"audit": "audit", "meta": "meta"}
CARD_FIELDS = {"id", "kind", "context", "given", "block", "hist_ref", "hist_label", "tokens",
               "actions", "harness"}
HARNESS_FIELDS = ("end", "error")  # the only result.json fields shown
HEADING = "S2 pilot: release audit"
LEDE = ("Before the sealed S2 outcomes are opened: for each card, did the harness give this arm "
        "the right starting context, and are its first actions a sane continuation with no "
        "harness error? No outcome, score or rule-violation verdict is shown. Then answer the "
        "release question at the end.")
_DATA_RE = re.compile(r"^const DATA = (.*);$", re.MULTILINE)


# ------------------------------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------------------------------


def _read(p: Path):
    return json.loads(Path(p).read_text())


def _trunc(s: str, n: int = TRUNC) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n:,} more characters]"


def candidates(raw_root: Path | None = None) -> list[dict]:
    """One row per (boundary, arm) with at least one replicate's continuation.json written."""
    out = []
    for a in RT.boundary_dirs(Path(raw_root or C.raw_dir())):
        plan = (_read(a / "run.json").get("plan") or {}) if (a / "run.json").exists() else {}
        for slug in ARM_SLUGS:
            reps = sorted(int(p.name.split("-")[1]) for p in (a / "arms").glob(f"{slug}-*")
                          if re.fullmatch(rf"{slug}-\d+", p.name)
                          and (p / "continuation.json").exists())
            if reps:
                out.append({"dir": str(a), "run_key": RT.run_key(a), "arm": slug, "reps": reps,
                            "model": plan.get("model") or "?", "channel": RT.channel_of(a) or "?",
                            "task": plan.get("task") or "?", "policy": plan.get("policy") or "?"})
    return out


def draw(cands: list[dict], *, n: int = N_CARDS, seed: int = SEED) -> list[dict]:
    """n cards, n/4 per arm (round-robin over arms), each slot the least-used model, channel,
    task and boundary; seeded tiebreaks and replicate choice. Deterministic for a seed."""
    rng = random.Random(f"s2-audit:{seed}")
    pool = {s: [c for c in cands if c["arm"] == s] for s in ARM_SLUGS}
    for s in ARM_SLUGS:
        rng.shuffle(pool[s])
    used = {k: Counter() for k in ("model", "channel", "task", "dir")}
    out: list[dict] = []
    while len(out) < n and any(pool.values()):
        order = list(ARM_SLUGS)
        rng.shuffle(order)
        for s in order:
            if len(out) >= n or not pool[s]:
                continue
            best = min(pool[s], key=lambda c: (used["dir"][c["dir"]], used["model"][c["model"]],
                                               used["channel"][c["channel"]],
                                               used["task"][c["task"]]))
            pool[s].remove(best)
            for k, counts in used.items():
                counts[best[k]] += 1
            out.append({**best, "rep": rng.choice(best["reps"])})
    return out


def card_id(seed: int, run_key: str, arm: str, rep: int) -> str:
    return "a" + hashlib.sha256(f"s2-audit-card:{seed}:{run_key}:{arm}:{rep}".encode()) \
        .hexdigest()[:12]


def arm_start(attempt_dir: Path, slug: str) -> tuple[list[dict], int | None, str | None]:
    """(the arm's starting messages, the summary index, the appended block) exactly as
    `loop.run_arm` built them: `loop.arm_context` with every rule, as pilot.py passes."""
    a = Path(attempt_dir)
    rules = RT.load_rules(a)
    lost = list(R.RULE_IDS)
    ctx = L.arm_context(ARM_OF_SLUG[slug], SimpleNamespace(out_dir=a), lost, rules)
    if slug == "F":
        return ctx, None, None
    si = _read(a / "boundary" / "compaction.json")["summary_index"]
    block = {"SR": R.restore_block, "SI": R.irrelevant_block}.get(slug)
    return ctx, si, (block(lost, rules) if block else None)


def render_messages(msgs: list[dict], summary_index: int | None) -> str:
    return "\n\n".join(f"[message {i}: {m.get('role')}, {RT.message_kind(i, m, summary_index)}]\n"
                       f"{RT.message_text(m)}" for i, m in enumerate(msgs))


def actions(continuation: list[dict], n: int = N_ACTIONS) -> list[dict]:
    """The first n assistant turns with the messages between them (tool outputs, nudges, the
    closing turn), each text truncated."""
    out, seen = [], 0
    for m in continuation:
        role = m.get("role")
        if role == "assistant":
            if seen == n:
                break
            seen += 1
            calls = [{"name": (tc.get("function") or {}).get("name") or "?",
                      "args": _trunc(RT._call_text(tc).partition("\n")[2])}
                     for tc in m.get("tool_calls") or []]
            out.append({"role": "assistant", "turn": seen,
                        "text": _trunc(RT.K._text(m.get("content"))), "calls": calls})
        else:
            out.append({"role": role or "?", "text": _trunc(RT.K._text(m.get("content")))})
    return out


def card(c: dict, seed: int) -> tuple[dict, str, dict]:
    """(page card without hist_ref, its starting-context text, key entry)."""
    a = Path(c["dir"])
    arm_dir = a / "arms" / f"{c['arm']}-{c['rep']}"
    ctx, si, block = arm_start(a, c["arm"])
    text = render_messages(ctx, si)
    calls = _read(arm_dir / "calls.json") if (arm_dir / "calls.json").exists() else []
    plan = (_read(a / "run.json").get("plan") or {}) if (a / "run.json").exists() else {}
    bpt = ((plan.get("route") or {}).get("bytes_per_token")) or 2.0
    first = calls[0] if calls and isinstance(calls[0], dict) else {}
    res = _read(arm_dir / "result.json") if (arm_dir / "result.json").exists() else {}
    acts = actions(_read(arm_dir / "continuation.json"))
    cid = card_id(seed, c["run_key"], c["arm"], c["rep"])
    page = {
        "id": cid, "kind": "arm",
        "context": f"task {c['task']} · model {c['model']} · channel {c['channel']} · "
                   f"summarizer {c['policy']} · arm {ARM_OF_SLUG[c['arm']]}, replicate {c['rep']}",
        "given": GIVEN[c["arm"]],
        "block": block,
        "hist_label": (f"The arm's starting context: {len(ctx)} messages"
                       + (f", summary at message {si}" if si is not None else "")),
        "tokens": {"estimated_start": M.est_tokens(ctx, bpt),
                   "first_call_prompt": first.get("prompt_tokens"),
                   "first_call_cached": first.get("cached_tokens")},
        "actions": acts,
        "harness": {k: (_trunc(res.get(k), 600) if isinstance(res.get(k), str) else res.get(k))
                    for k in HARNESS_FIELDS},
    }
    key = {"run_key": c["run_key"], "dir": str(a.relative_to(ROOT)) if a.is_relative_to(ROOT)
           else str(a), "arm": c["arm"], "rep": c["rep"], "model": c["model"],
           "channel": c["channel"], "task": c["task"], "policy": c["policy"]}
    return page, text, key


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def outcome_markers(manifest: dict, root=None) -> list[str]:
    """Every field name in the sealed files (read for their key names only), as JSON keys."""
    d = seal.sealed_dir(STAGE, manifest.get("release_name") or seal.DEFAULT_RELEASE, root)
    names: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                names.add(k)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for f in sorted(manifest.get("sealed") or {}):
        p = d / f
        if p.suffix == ".json" and p.is_file():
            walk(_read(p))
    # identifiers, not outcomes (the page carries none of them as keys either)
    return sorted(f'"{k}":' for k in names - {"arm", "rep", "run_key", "host"})


def build(cards: list[dict], manifest: dict, *, seed: int = SEED, root=None) -> tuple[str, dict]:
    hists: dict[str, str] = {}
    refs: dict[str, str] = {}
    shown, key = [], {}
    for c in cards:
        page, text, k = card(c, seed)
        if text not in refs:
            refs[text] = f"h{len(refs) + 1}"
            hists[refs[text]] = RP._gz64(text)
        page["hist_ref"] = refs[text]
        assert set(page) <= CARD_FIELDS
        shown.append(page)
        key[page["id"]] = k
    draw_id = hashlib.sha256(json.dumps(
        {"seed": seed, "manifest": manifest.get("digest"), "cards": shown, "key": key},
        sort_keys=True, default=str).encode()).hexdigest()[:16]
    data = {"cards": shown, "histories": hists, "collections": COLLECTIONS, "draw": draw_id,
            "seed": seed}
    html = render(data)
    k = {"draw": draw_id, "seed": seed, "audit": key, "order": [p["id"] for p in shown],
         "manifest": manifest, "outcome_markers": outcome_markers(manifest, root),
         "page_sha256": hashlib.sha256(html.encode()).hexdigest()}
    return html, k


def template_parts() -> tuple[str, str]:
    """(head with the stylesheet, the history-box script), relabelled for an arm's context."""
    tpl = S1P.TEMPLATE.read_text(encoding="utf-8")
    head = tpl[: tpl.index("</style>") + len("</style>")]
    js = tpl[tpl.index(RP.HIST_START): tpl.index(RP.HIST_END)]
    for old, new in (
        ('placeholder="Find in the history" aria-label="Find in the history"',
         'placeholder="Find in the starting context" aria-label="Find in the starting context"'),
        ('<p class="label">${label}</p>', '<p class="label">${esc(u.hist_label || label)}</p>'),
    ):
        if js.count(old) != 1:
            raise ValueError(f"s1_label_template.html changed: {old!r} found {js.count(old)}x")
        js = js.replace(old, new)
    return head, js


BODY = """
<style>
.acts { display: grid; gap: 8px; }
.act-a { border-left: 3px solid var(--accent); padding-left: 10px; }
.act-o { border-left: 3px solid var(--rule); padding-left: 10px; }
.act-o .box, .act-a .box { max-height: 14rem; }
.kv { font: 0.85rem var(--f-mono); color: var(--muted); margin: 0; }
.final { background: var(--paper); border: 1px solid var(--accent); border-radius: 6px; padding: 16px; display: grid; gap: 10px; }
</style>
<div class="wrap">
  <header style="display:grid;gap:8px">
    <h1>__HEADING__</h1>
    <p class="lede">__LEDE__</p>
  </header>
  <details class="rubric">
    <summary>What each choice means</summary>
    <p><strong>Harness OK:</strong> the starting context is the one the arm should get (F: the full history with no summary; S: the summary context; S+R: the four rules appended to the summary; S+I: four unrelated facts there instead), the first call's prompt size is in line with that context, and the first actions continue the task sensibly with no harness error (broken tool output, a wrong repo, a crash, an empty reply loop).</p>
    <p><strong>Problem:</strong> the harness did something wrong for this arm. Say what in the note. Any Problem blocks the release.</p>
    <p><strong>Can't tell:</strong> the card doesn't settle it. It doesn't block the release.</p>
    <p>Judge the harness, not the agent: a poor but coherent decision by the agent is still Harness OK.</p>
  </details>
  <div class="bar" role="status">
    <span class="count" id="count">0 done</span>
    <div class="track" aria-hidden="true"><i id="fill"></i></div>
    <button type="button" class="ghost" id="next">Next unlabelled</button>
    <span class="save" id="save">Connecting to storage…</span>
  </div>
  <section class="panel"><div class="sheets" id="sheets"></div></section>
  <section class="final">
    <h2>Release the sealed S2 outcomes?</h2>
    <p class="lede">Yes only when every card is marked and none is a Problem. The release command refuses otherwise.</p>
    <fieldset><legend>Release</legend>
      <label><input type="radio" name="release" value="yes" data-id="release" data-f="release" data-tone="ok"><span>Yes, release</span></label>
      <label><input type="radio" name="release" value="not_yet" data-id="release" data-f="release" data-tone="none"><span>Not yet</span></label>
    </fieldset>
    <input class="note" type="text" data-id="release" data-f="note" placeholder="Note (optional)" aria-label="Release note">
  </section>
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
for (const x of DATA.cards) BYID[x.id] = { tab: "audit", x };
BYID.release = { tab: "release", x: { id: "release" } };
const state = {};
let db = null;
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
__HISTORY_JS__
const CHOICES = [["ok", "Harness OK", "ok"], ["problem", "Problem", "bad"], ["cant_tell", "Can't tell", "none"]];
function actBlock(a) {
  if (a.role === "assistant") {
    const calls = (a.calls || []).map(c => `<p class="kv">tool call: ${esc(c.name)}</p><pre class="box">${esc(c.args)}</pre>`).join("");
    return `<div class="act-a"><p class="label">Agent turn ${a.turn}</p>${a.text ? `<pre class="box read">${esc(a.text)}</pre>` : ""}${calls || (a.text ? "" : `<p class="kv">(empty reply)</p>`)}</div>`;
  }
  return `<div class="act-o"><p class="label">${esc(a.role === "tool" ? "Tool output" : a.role + " message")}</p><pre class="box">${esc(a.text)}</pre></div>`;
}
function card(u, i, n) {
  const radios = CHOICES.map(([v, t, tone]) => `<label><input type="radio" name="c-${u.id}" value="${v}" data-id="${u.id}" data-f="label" data-tone="${tone}"><span>${t}</span></label>`).join("");
  const t = u.tokens || {};
  const h = u.harness || {};
  return `<article class="sheet" id="s-${u.id}">
    <header><span class="tag">${i + 1} / ${n}</span><span class="state" id="st-${u.id}">not labelled</span></header>
    <p class="lede">${esc(u.context)}</p>
    <div><p class="label">What this arm was given at the fork</p><p class="q">${esc(u.given)}</p></div>
    ${u.block ? `<div><p class="label">Block appended to the summary message</p><pre class="q">${esc(u.block)}</pre></div>` : ""}
    <p class="kv">starting context ≈ ${esc(t.estimated_start)} tokens (estimate) · first call prompt ${esc(t.first_call_prompt)} tokens, ${esc(t.first_call_cached)} cached · harness end ${esc(h.end)}${h.error ? " · error" : ""}</p>
    ${h.error ? `<pre class="box">${esc(h.error)}</pre>` : ""}
    <details><summary>Starting context in full, with find</summary>${histBox(u)}</details>
    <div><p class="label">First actions after the fork</p><div class="acts">${(u.actions || []).map(actBlock).join("") || `<p class="warnline">No actions recorded.</p>`}</div></div>
    <div class="calls">
      <fieldset><legend>Harness OK for this arm?</legend>${radios}</fieldset>
      <input class="note" type="text" data-id="${u.id}" data-f="note" placeholder="Note (what's wrong, if anything)" aria-label="Note">
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
  if (id === "release") return;
  document.getElementById(`s-${id}`).classList.toggle("done", !!l.label);
  document.getElementById(`st-${id}`).textContent = l.label ? "labelled" : "not labelled";
}
function tally() {
  const n = DATA.cards.filter(x => state[x.id] && state[x.id].label).length;
  document.getElementById("count").textContent = `${n} / ${DATA.cards.length} done`;
  document.getElementById("fill").style.width = `${100 * n / Math.max(1, DATA.cards.length)}%`;
  const ids = [...DATA.cards.map(x => x.id), "release"];
  const rows = ids.filter(id => state[id]).map(id => ({ id, collection: COLL.audit, data: { ...state[id], draw: DATA.draw } }));
  document.getElementById("dump").value = JSON.stringify(rows, null, 1);
}
function setSave(text, warn) { const el = document.getElementById("save"); el.textContent = text; el.classList.toggle("warn", !!warn); }
// The storage hook: {card id or "release": {label | release, note}} -> collection "audit".
async function submit(labels) {
  for (const [id, l] of Object.entries(labels)) { state[id] = { ...(state[id] || {}), ...l }; paint(id); }
  tally();
  if (!db) { setSave("Not saving: storage unavailable. Use the copy below.", true); return false; }
  try {
    for (const id of Object.keys(labels))
      await db.collection(COLL.audit).doc(id).set({ ...state[id], draw: DATA.draw, utc: new Date().toISOString() });
    setSave("Saved"); return true;
  } catch (e) { setSave(`Not saved (${e && e.code || "error"}). Use the copy below.`, true); return false; }
}
const timers = {};
function onEdit(ev, immediate) {
  const t = ev.target, id = t.dataset && t.dataset.id, f = t.dataset && t.dataset.f;
  if (!id || !f || !BYID[id]) return;
  state[id] = { ...(state[id] || {}), [f]: t.value }; paint(id); tally();
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
DATA.cards.forEach(x => paint(x.id)); paint("release"); tally();
requestAnimationFrame(() => hsObserve());
(async () => {
  try { db = window.claude ? await window.claude.use("db") : null; } catch (e) { db = null; }
  if (!db) { setSave("Not saving: storage unavailable here. Use the copy below.", true); return; }
  setSave("Saving on");
  db.collection(COLL.audit).onSnapshot(snap => {
    snap.docs.forEach(d => {
      const v = d.data();
      if (!BYID[d.id] || v.draw !== DATA.draw || timers[d.id] && document.activeElement && document.activeElement.dataset && document.activeElement.dataset.id === d.id) return;
      state[d.id] = d.id === "release" ? { release: v.release, note: v.note || "" } : { label: v.label, note: v.note || "" };
      paint(d.id);
    });
    tally();
  }, e => setSave(`Live sync stopped (${e && e.code || "error"}). Edits still save.`, true));
})();
</script>
"""


def render(data: dict) -> str:
    head, hist_js = template_parts()
    head = head.replace("__TITLE__", "S2 release audit")
    body = (BODY.replace("__HEADING__", HEADING).replace("__LEDE__", LEDE)
            .replace("__HISTORY_JS__", hist_js))
    return head + body.replace("__DATA_JSON__", S1P._script_json(data))


def page_data(html: str) -> dict:
    m = _DATA_RE.search(html)
    if not m:
        raise ValueError("no DATA in page")
    return json.loads(m.group(1).replace("<\\/", "</").replace("<\\!--", "<!--"))


def leak_check(html: str, key: dict) -> None:
    """Refuse a page carrying a sealed outcome field name as a JSON key, a sealed file name, or a
    card field outside CARD_FIELDS."""
    hits = [m for m in key.get("outcome_markers") or [] if m in html]
    hits += [f for f in (key.get("manifest") or {}).get("sealed") or {} if f in html]
    hits += [f for c in page_data(html)["cards"] for f in c if f not in CARD_FIELDS]
    if hits:
        raise S1P.LeakError(f"page carries outcome strings: {hits[:10]}")


def write(out_dir: Path, html: str, key: dict, *, private_root: Path = RP.PRIVATE_ROOT,
          check_ignored: bool = True) -> dict:
    out = RP.require_private(Path(out_dir), private_root)
    if check_ignored:
        for name in (KEY_NAME, PAGE_NAME):
            if not RP.git_ignored(out / name):
                raise ValueError(f"{out / name} is not gitignored: refusing to write it")
    leak_check(html, key)
    S1P._atomic(out / KEY_NAME, (json.dumps(key, indent=1, sort_keys=True) + "\n").encode(),
                mode=0o600)
    S1P._atomic(out / PAGE_NAME, html.encode("utf-8"))
    return {"page": str(out / PAGE_NAME), "key": str(out / KEY_NAME),
            "bytes": len(html.encode("utf-8")), "cards": len(key["audit"])}


# ------------------------------------------------------------------------------------------
# Release
# ------------------------------------------------------------------------------------------


def audit_labels(path: Path) -> dict[str, dict]:
    """{doc id: data} from an ArtifactData export of collection `audit`: a directory of <id>.json
    files, a list of {id, collection, data} (the page's copy), or a mapping (as s1_audit)."""
    p = Path(path)
    if p.is_dir():
        return {f.stem: json.loads(f.read_text()) for f in sorted(p.glob("*.json"))}
    obj = json.loads(p.read_text())
    if isinstance(obj, list):
        return {x["id"]: x["data"] for x in obj if x.get("collection", "audit") == "audit"}
    return dict(obj.get("audit") or obj)


def check_labels(key: dict, labels: dict[str, dict]) -> dict:
    """Card ids per mark, or SystemExit: labels from another draw, an unmarked card, a Problem,
    or no "yes" to the release question."""
    ids = set(key["audit"])
    wrong = sorted(i for i, d in labels.items() if d.get("draw") != key["draw"])
    if wrong:
        raise SystemExit(f"labels from another draw: {wrong[:5]}")
    unmarked = sorted(i for i in ids if (labels.get(i) or {}).get("label") not in LABELS)
    if unmarked:
        raise SystemExit(f"{len(unmarked)} of {len(ids)} cards not marked yet: {unmarked[:5]}")
    by = {m: sorted(i for i in ids if labels[i]["label"] == m) for m in LABELS}
    if by["problem"]:
        raise SystemExit(f"{len(by['problem'])} card(s) marked Problem: fix and rerun what was "
                         "affected, then build a fresh audit (new seed)")
    if (labels.get(RELEASE_DOC) or {}).get("release") != "yes":
        raise SystemExit("the release question isn't answered yes")
    return {"n": len(ids), **by}


def release_audit(out: Path, labels_path: Path, *, by: str, note: str = "", root=None) -> dict:
    out = Path(out)
    key_path = out / KEY_NAME
    key = _read(key_path)
    labels = audit_labels(Path(labels_path))
    c = check_labels(key, labels)
    snap = {"draw": key["draw"],
            "labels": {i: dict(labels[i]) for i in sorted([*key["audit"], RELEASE_DOC])}}
    sp = out / SNAPSHOT
    S1P._atomic(sp, (json.dumps(snap, indent=1, sort_keys=True) + "\n").encode())
    summary = f"{c['n']} cards: {len(c['ok'])} OK, {len(c['cant_tell'])} can't tell, 0 problems"
    rec = seal.release(STAGE, [sp, key_path], by=by, manifest=key["manifest"],
                       note=note or f"R-A3 audit {key['draw']}: {summary}", root=root)
    record = {"draw": key["draw"], "summary": summary, "released_utc": rec["released_utc"],
              "digest": rec["digest"], "n_sealed": len(rec["sealed"]),
              "snapshot_sha256": hashlib.sha256(sp.read_bytes()).hexdigest()}
    S1P._atomic(out / RELEASE_RECORD, (json.dumps(record, indent=1) + "\n").encode())
    return rec


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------


def mix(cards: list[dict]) -> dict:
    out: dict = defaultdict(Counter)
    for c in cards:
        for k in ("arm", "model", "channel", "task"):
            out[k][c[k]] += 1
    out["boundaries"] = len({c["dir"] for c in cards})
    return {k: (dict(sorted(v.items())) if isinstance(v, Counter) else v) for k, v in out.items()}


def cmd_build(a) -> int:
    out = RP.require_private(Path(a.out))
    manifest = seal.prepare_audit(STAGE)
    cards = draw(candidates(), n=a.n, seed=a.seed)
    if len(cards) < a.n:
        print(f"only {len(cards)} arm continuations to draw from", file=sys.stderr)
        return 1
    html, key = build(cards, manifest, seed=a.seed)
    info = write(out, html, key)
    print(json.dumps({**info, "draw": key["draw"], "manifest_digest": manifest["digest"][:16],
                      "n_sealed": len(manifest["sealed"]), "mix": mix(cards)}, indent=1))
    return 0


def cmd_release(a) -> int:
    rec = release_audit(Path(a.out), Path(a.labels), by=a.by, note=a.note)
    print(f"released {rec['stage']}/{rec['release_name']} at {rec['released_utc']} "
          f"({len(rec['sealed'])} files, digest {rec['digest'][:16]})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", default=str(OUT_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", parents=[common])
    b.add_argument("--n", type=int, default=N_CARDS)
    b.add_argument("--seed", type=int, default=SEED)
    r = sub.add_parser("release", parents=[common])
    r.add_argument("--labels", required=True)
    r.add_argument("--by", default="jacob")
    r.add_argument("--note", default="")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "release": cmd_release}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
