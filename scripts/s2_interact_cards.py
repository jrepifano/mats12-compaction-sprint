"""SWE-Interact pilot: Jacob's candidate cards, and the freeze from his labels
(docs/sprint/plans/swe-interact-pilot.md, "Candidates and the usable-loss checks", "Six checks",
"Order and the 6-card budget", "Blinding"; brief logs/s2/briefs/interact-cards.md).

**Build** (`build --boundaries RUN_DIR:K ...`, no calls). One card per boundary: the candidate
its screen carded (`boundary/candidates.json` "carded", written by `interact_reqs.screen`). Each
card shows the task (and the task text the simulator holds), the summary source with a plain
note when it isn't pi's live summary, the requirement and the maintainer's reply it came from,
and the six checks in the plan's order: retention, not recoverable, applicability, governed
decision, design, reference. Each check is a plain question with descriptive answers (the
format of `s2_candidates.CHECK_TEXT`: the first answer is the usable side, "Can't tell" the
third; stored as yes/no/unclear with yes = passes), the screen's outcome and evidence, Haiku's
draft where there is one, and a note. Two searchable boxes hold the agent's pre-boundary history
(scrolled to the requirement's quote) and the resumed input built from the source summary
(`interact_score.source_view`). The design fields (repair block, matched block and rationale,
the act and its scorer) start from the screen and are editable. A card-level note closes it.
Nothing after the boundary is shown: the build refuses a boundary whose run has an `arms/` dir
(`<run>/arms` or `<run>/b<k>/arms`), since a continuation could then exist.

Page and key go to data/raw/sprint/s2/interact/cards/ (gitignored). The key (card id -> run dir,
boundary, candidate id, content hash and the screen's design) stays out of the page; a leak check
refuses a page carrying a key field name, a candidate id or a card field outside CARD_FIELDS.
Storage: db collection `labels`, doc id = card id, plus a copyable JSON fallback. Every label
carries its card's content version `v` (a hash of everything the card shows, the candidate's
screen record without its disposition, and both box texts), so labels never attach to changed
evidence, and a later page that adds a card (helm a2) keeps the earlier cards' labels.

**Freeze** (`freeze --labels PATH`). PATH is the ArtifactData read-back: a directory of JSON
files or one file, each a list of {id, collection, data}, a {collection: {id: data}} map, or one
document (`{"id", "data"}`, or the bare data in `<card id>.json`). For each card:
  - any check answered no or unclear: **rejected**, with the reasons (round 2 rescreens that
    boundary with `interact_reqs.screen(bdir, rejected=[...])` and takes the next candidate);
  - a check unanswered: **unlabelled** (nothing written);
  - all six yes: the final design is the edited fields (the screen's values where unedited). It
    is **held**, not frozen, when the design is incomplete or its act spec invalid; when the
    card's evidence changed since the page (content version); when the run has `arms/`; when
    another candidate of that boundary is already frozen; when an edited design adds a code
    problem the card didn't show (`interact_reqs.check_design`); or when an edited act spec
    fails check 6 again (`interact_score.check_reference`, Docker, free) with anything but pass.
    Otherwise `interact_score.write_freeze` writes the record, each check carrying
    `jacob: "pass"` and Jacob's note, and the `scripts/s2_interact.py publish-freeze` command is
    printed. A candidate already frozen is reported, never rewritten.
Every note Jacob wrote is printed. Decisions are merged into cards/decisions.json.

    uv run python scripts/s2_interact_cards.py build --boundaries RUN_DIR:K [RUN_DIR:K ...]
        [--out data/raw/sprint/s2/interact/cards] [--seed 20261025]
    uv run python scripts/s2_interact_cards.py freeze --labels DIR_OR_FILE
        [--key data/raw/sprint/s2/interact/cards/key.private.json]

RUN_DIR is a run directory, or a run key under data/raw/sprint/s2/interact/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s1_label_page as S1P  # template helpers and writers
import s2_candidates as SC2  # the card format this page follows (history box, locate)
import s2_retention_page as RP  # template head, gzip, page data

from compaction.s2 import interact_reqs as RQ
from compaction.s2 import interact_run as IR
from compaction.s2 import interact_score as S
from compaction.s2 import retention as RT

ROOT = Path(__file__).resolve().parents[1]
INTERACT = ROOT / "data" / "raw" / "sprint" / "s2" / "interact"
CARDS_DIR = INTERACT / "cards"
PRIVATE_ROOT = ROOT / "data" / "raw"
PAGE_NAME = "page.html"
KEY_NAME = "key.private.json"
DECISIONS_NAME = "decisions.json"
SEED = 20261025
# The plan's order (brief): names as in candidates.json "checks" and interact_run.CHECK_NAMES.
CHECKS = ("retention", "recoverability", "applicability", "decision", "design", "reference")
ANSWERS = ("yes", "no", "unclear")
COLLECTIONS = {"labels": "labels"}
CARD_FIELDS = {"id", "v", "kind", "hist_ref", "hist_at", "located", "ctx_quote", "box_label",
               "find_ph", "post_ref", "context", "source_note", "requirement", "reply",
               "task_text", "checks", "design", "patch",
               "paragraph", "qev", "code_ev", "loss", "nq"}  # the last five: v2 cards
KEY_MARKERS = ('"cand_id"', '"run_dir"', '"content_sha256"', '"req_id"')
MAX_HITS = 20  # paths shown per search string and place (the counts are shown too)
MAX_PAGE_BYTES = 15_000_000
DESIGN_TEXT = ("d_repair", "d_matched", "d_rationale", "d_act_desc")
DESIGN_FIELDS = (*DESIGN_TEXT, "d_act_kind", "d_act_pattern", "d_act_path", "d_act_negate",
                 "d_dec_kind", "d_dec_pattern", "d_dec_path", "d_reask")
SPEC_FIELDS = DESIGN_FIELDS[3:]  # the act description and the scorer

SOURCE_NAMES = {"live": "pi's live summary", "cc_prompt": "Claude Code's prompt",
                "codex_local": "Codex", "opencode": "OpenCode",
                "antigravity": "Antigravity-style"}
SOURCE_NOTES = {
    "live": "pi's live summary: the run's own compaction, written by DeepSeek.",
    "cc_prompt": ("DeepSeek resumed from a Claude Code-style summary written by Haiku (replayed "
                  "offline from the same history; it keeps nothing verbatim)."),
    "codex_local": ("DeepSeek resumed from a Codex-style summary written by Haiku (replayed "
                    "offline; Codex keeps recent user messages verbatim)."),
    "opencode": ("DeepSeek resumed from an OpenCode-style summary written by Haiku (replayed "
                 "offline; the summary is an assistant message, so the repair or matched block "
                 "goes in a separate user message after it)."),
    "antigravity": ("DeepSeek resumed from an Antigravity-style summary written by Haiku "
                    "(replayed offline; it keeps the latest requests verbatim)."),
}


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(p: Path) -> Any:
    return json.loads(Path(p).read_text())


def _rel(p: Path) -> str:
    p = Path(p).resolve()
    return str(p.relative_to(ROOT)) if ROOT in p.parents else str(p)


# ------------------------------------------------------------------------------------------
# Boundaries
# ------------------------------------------------------------------------------------------


def parse_boundary(arg: str, root: Path = INTERACT) -> tuple[Path, int]:
    """RUN_DIR:K -> (run dir, k). RUN_DIR may be a run key under `root`."""
    rd, sep, k = arg.rpartition(":")
    if not sep or not k.isdigit():
        raise ValueError(f"{arg!r}: expected RUN_DIR:K")
    p = Path(rd)
    if not p.is_dir() and (Path(root) / rd).is_dir():
        p = Path(root) / rd
    if not (p / f"b{k}" / "boundary").is_dir():
        raise FileNotFoundError(f"{p / f'b{k}' / 'boundary'}: no such boundary")
    return p.resolve(), int(k)


def arms_dirs(run_dir: Path, k: int) -> list[Path]:
    """Continuation dirs that would make a card unsafe to show (brief: never any output)."""
    return [p for p in (Path(run_dir) / "arms", Path(run_dir) / f"b{k}" / "arms") if p.exists()]


class ArmsExist(RuntimeError):
    pass


def candidate(run_dir: Path, k: int, cand_id: str | None = None) -> dict:
    """The boundary's carded candidate (or `cand_id`), from boundary/candidates.json."""
    cj = _read(Path(run_dir) / f"b{k}" / "boundary" / "candidates.json")
    want = cand_id or cj.get("carded")
    if not want:
        raise ValueError(f"{run_dir.name} b{k}: no carded candidate in candidates.json")
    c = next((c for c in cj.get("candidates") or [] if c.get("cand_id") == want), None)
    if c is None:
        raise ValueError(f"{run_dir.name} b{k}: candidate {want} not in candidates.json")
    return c


# ------------------------------------------------------------------------------------------
# Card content
# ------------------------------------------------------------------------------------------


def render_history(pre: list[dict]) -> str:
    return "\n\n".join(f"[message {i}: {m.get('role')}]\n{RT.message_text(m)}"
                       for i, m in enumerate(pre))


def render_post(view: Mapping) -> str:
    si = view["summary_index"]
    return "\n\n".join(f"[message {i}: {m.get('role')}, {RT.message_kind(i, m, si)}]\n"
                       f"{RT.message_text(m)}" for i, m in enumerate(view["post"]))


def _where(path: str) -> str:
    """Where a `session.grep_runner` hit is: "/tmp/plan.md", "commit:<sha>" (a commit
    message), "<branch>:<path>", else a working-tree path (untracked included)."""
    if path.startswith("/"):
        return "file"
    if path.startswith("commit:"):
        return "commit"
    return "branch" if ":" in path else "tree"


def _paths(xs: list[str]) -> str:
    more = f" … and {len(xs) - MAX_HITS} more" if len(xs) > MAX_HITS else ""
    return ", ".join(xs[:MAX_HITS]) + more


def _hit_rows(hits: Mapping[str, list[str]]) -> tuple[list[str], list[str]]:
    """(per-string rows, strings found in working-tree files)."""
    rows, in_tree = [], []
    for s, paths in hits.items():
        paths = list(paths or [])
        by: dict[str, list[str]] = {"tree": [], "file": [], "commit": [], "branch": []}
        for p in paths:
            by[_where(p)].append(p)
        parts = [f"{name}: {_paths(by[w])}" for w, name in
                 (("tree", "working tree"), ("file", "outside the repo"),
                  ("commit", "commit messages"), ("branch", "branches"))
                 if by[w]]
        rows.append(f"“{s}”: {len(paths)} hit{'s' if len(paths) != 1 else ''}"
                    + (" — " + "; ".join(parts) if parts else ""))
        if by["tree"]:
            in_tree.append(s)
    return rows, in_tree


def _outcome(chk: Mapping) -> dict:
    return {"outcome": chk.get("outcome") or "not run", "reason": chk.get("reason")}


def check_evidence(c: Mapping) -> list[dict]:
    """Per check, in CHECKS order: {outcome, reason, by, lines, lists, draft}."""
    ch, d = c.get("checks") or {}, c.get("draft") or {}
    out = []
    for name in CHECKS:
        x = ch.get(name) or {}
        ev = x.get("evidence") or {}
        e = {**_outcome(x), "by": "code", "lines": [], "lists": [], "draft": None}
        if name == "retention":
            e["lines"].append(f"Grade of this summary's resumed input: {ev.get('grade')}.")
            if ev.get("summary_quote"):
                e["lines"].append(f"The summary's closest text: “{ev['summary_quote']}”")
            if ev.get("still_present"):
                e["lines"].append("The requirement's quote is still verbatim in the resumed "
                                  "input.")
        elif name == "recoverability" and "searched" not in ev:
            e["lines"].append(f"The repo search did not run ({x.get('reason') or 'no record'}).")
        elif name == "recoverability":
            rows, in_tree = _hit_rows(ev.get("search_hits") or {})
            lrows, lt = _hit_rows(ev.get("line_hits") or {})
            frows, ft = _hit_rows(ev.get("fragment_hits") or {})
            tree = sorted(set(in_tree) | set(lt) | set(ft))
            e["lines"].append(
                ("The code is already in the working tree: " + ", ".join(f"“{s}”" for s in tree)
                 + " match files in the boundary's working tree. Whether that text states the "
                   "requirement, or only follows it, is your call.") if tree else
                "No search string matches a file in the boundary's working tree.")
            e["lines"].append(f"Searched the kept boundary image ({ev.get('image')}): working "
                              "tree with untracked files, every local branch, commit messages "
                              "and /tmp/plan.md (the step instruction is in the resumed-input "
                              "box).")
            for title, rr in (("The requirement's lines", lrows),
                              ("Fragments of the quote", frows), ("Search strings", rows)):
                if rr:
                    e["lists"].append({"title": title, "items": rr})
            if d.get("search_strings"):
                e["draft"] = "Haiku's search strings: " + ", ".join(
                    f"“{s}”" for s in d["search_strings"])
        elif name == "applicability":
            e["by"] = "Haiku's draft"
            e["lines"].append(ev.get("why") or "")
            if d:
                e["draft"] = f"Haiku: applicable = {d.get('applicable')}."
        elif name == "decision":
            e["by"] = "Haiku's draft"
            e["lines"].append(f"Governed decision: {ev.get('decision') or ''}")
            if d:
                e["draft"] = (f"Haiku: observable before the readout ends = "
                              f"{d.get('decision_observable')}. Act it drafted: "
                              f"{d.get('act_description') or ''}")
        elif name == "design":
            probs = ev.get("problems") or []
            e["lines"].append("Code problems: " + (", ".join(probs) if probs else "none")
                              + (f"; matched/repair length ratio {ev['length_ratio']}"
                                 if ev.get("length_ratio") is not None else "")
                              + " (the plan asks for ±20%).")
            if ev.get("spec_errors"):
                e["lines"].append("Scorer errors: " + "; ".join(ev["spec_errors"]))
            if d:
                e["draft"] = "The fields below start from Haiku's draft; edit them as needed."
        elif name == "reference" and "act_raw" not in ev:
            e["lines"].append(f"The reference check did not run or did not finish "
                              f"({x.get('reason') or 'no record'}).")
            if ev.get("error") or ev.get("stderr") or ev.get("errors"):
                e["lines"].append(f"Error: {ev.get('error') or ev.get('stderr') or ev.get('errors')}")
        elif name == "reference":
            e["lines"].append(f"The reference solution ({ev.get('solution_patch')}) applied to "
                              "the base commit in the kept image, then the act's scorer run on "
                              f"its diff: act pattern matched = {ev.get('act_raw')}, decision "
                              f"reached = {ev.get('reached')}.")
            if ev.get("matches"):
                e["lists"].append({"title": "Reference lines the act matched",
                                   "items": [json.dumps(m, ensure_ascii=False)
                                             for m in ev["matches"]]})
            if ev.get("decision_matches"):
                e["lists"].append({"title": "Reference lines the decision matched",
                                   "items": [json.dumps(m, ensure_ascii=False)
                                             for m in ev["decision_matches"]]})
            if ev.get("error") or ev.get("stderr"):
                e["lines"].append(f"Error: {ev.get('error') or ev.get('stderr')}")
        out.append(e)
    return out


def screen_design(c: Mapping) -> dict:
    """The design fields' starting values, from the screen (as strings, for the page)."""
    ev = ((c.get("checks") or {}).get("design") or {}).get("evidence") or {}
    d = c.get("draft") or {}
    spec = c.get("act_spec") or {}
    act, dec = spec.get("act") or {}, spec.get("decision") or {}
    return {
        "d_repair": c.get("repair_text") or "",
        "d_matched": ev.get("matched_quote") or d.get("matched_quote") or "",
        "d_rationale": ev.get("matched_rationale") or d.get("matched_rationale") or "",
        "d_act_desc": spec.get("description") or "",
        "d_act_kind": act.get("kind") or "",
        "d_act_pattern": act.get("pattern") or "",
        "d_act_path": act.get("path") or "",
        "d_act_negate": "yes" if act.get("negate") else "no",
        "d_dec_kind": dec.get("kind") or "none",
        "d_dec_pattern": dec.get("pattern") or "",
        "d_dec_path": dec.get("path") or "",
        "d_reask": ", ".join(spec.get("reask_keywords") or []),
    }


def spec_of(design: Mapping) -> dict:
    """The act spec (interact_score's shape) from design fields."""
    def opt(v):
        v = (v or "").strip() if isinstance(v, str) else v
        return v or None
    dk = (design.get("d_dec_kind") or "none").strip()
    dec = None if dk in ("", "none") else {"kind": dk, "pattern": opt(design.get("d_dec_pattern")),
                                           "path": opt(design.get("d_dec_path"))}
    return {"act": {"kind": (design.get("d_act_kind") or "").strip(),
                    "pattern": opt(design.get("d_act_pattern")),
                    "path": opt(design.get("d_act_path")),
                    "negate": (design.get("d_act_negate") or "no") == "yes"},
            "decision": dec, "description": (design.get("d_act_desc") or "").strip(),
            "reask_keywords": [k.strip() for k in (design.get("d_reask") or "").split(",")
                               if k.strip()]}


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
                          ).hexdigest()


def card_id(seed: int, run_key: str, k: int, cand_id: str) -> str:
    return "c" + hashlib.sha256(f"s2-interact-card:{seed}:{run_key}:b{k}:{cand_id}".encode()
                                ).hexdigest()[:12]


def card_parts(run_dir: Path, k: int, cand_id: str | None = None, *,
               seed: int = SEED) -> dict:
    """Everything one card needs: {card (page fields, refs unset), history, post, cand,
    content_sha256}. Reads only pre-boundary files."""
    run_dir = Path(run_dir)
    bdir = run_dir / f"b{k}"
    b = bdir / "boundary"
    c = candidate(run_dir, k, cand_id)
    src = c["source_policy"]
    pre = _read(b / "pre_messages.json")
    view = S.source_view(bdir, src)
    rq = _read(b / "requirements.json")
    it = next((i for i in rq.get("items") or [] if i.get("req_id") == c["req_id"]), {})
    reply = next((r for r in _read(b / "sim_replies.json")
                  if r.get("reply_id") == c.get("source_reply_id")), {})
    bj = _read(b / "boundary.json") if (b / "boundary.json").exists() else {}
    run = _read(bdir / "run.json") if (bdir / "run.json").exists() else _read(run_dir /
                                                                                "run.json")
    task = (run.get("task") or {})
    hist, post = render_history(pre), render_post(view)
    quote = it.get("quote") or c.get("repair_text")
    pi = it.get("source_pre_index", reply.get("pre_index"))
    near = None
    if isinstance(pi, int):
        tag = f"[message {pi}: "
        near = hist.find(tag) if tag in hist else None
    at = SC2.locate(hist, quote, near)
    patch_rel = task.get("solution_patch")
    patch_p = (ROOT / patch_rel) if patch_rel and not Path(patch_rel).is_absolute() else \
        (Path(patch_rel) if patch_rel else None)
    patch = patch_p.read_text(errors="replace") if patch_p and patch_p.exists() else None
    card = {
        "kind": "item", "hist_at": at, "located": at is not None, "ctx_quote": quote or "",
        "box_label": ("The agent's history before the boundary"
                      + (", scrolled to the requirement's quote" if at else "")),
        "find_ph": "Find in the agent's history",
        "context": (f"Task {task.get('task_id')} · boundary {k}"
                    + (f", {bj['calls_in_step']} calls into {bj.get('stage')}"
                       if bj.get("calls_in_step") is not None else "")
                    + f" · summary source: {SOURCE_NAMES.get(src, src)}"),
        "source_note": SOURCE_NOTES.get(src, src),
        "requirement": {"text": c.get("text"), "quote": quote,
                        "grounding_quote": it.get("grounding_quote")},
        "reply": {"step": reply.get("step"), "question": reply.get("question"),
                  "reply": reply.get("reply"), "message": pi},
        "task_text": RQ.task_text(bdir),
        "checks": check_evidence(c),
        "design": screen_design(c),
        "patch": patch,
    }
    screened = {kk: v for kk, v in c.items() if kk != "disposition"}
    content = _sha([card, screened, hist, post])
    card["v"] = content[:16]
    return {"card": card, "history": hist, "post": post, "cand": c, "content_sha256": content}


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------


def build(boundaries: list[tuple[Path, int]], *, seed: int = SEED,
          previous: Mapping | None = None) -> tuple[str, dict]:
    """(html, key) for one card per boundary, in the order given. Refuses a boundary whose
    run has an arms/ dir (ArmsExist), and a boundary given twice. `previous` (the key this
    page replaces) moves its cards that aren't on the new page to `retired`, so the freeze
    recognises their labels, still in the db, as an earlier round's."""
    if len({(str(r), k) for r, k in boundaries}) != len(boundaries):
        raise ValueError("a boundary is given twice")
    for run_dir, k in boundaries:
        if arms := arms_dirs(run_dir, k):
            raise ArmsExist(f"{run_dir.name} b{k}: {[str(a) for a in arms]} exists; a card "
                            "must never sit beside continuation output")
    cards, hists, key = [], {}, {}
    for n, (run_dir, k) in enumerate(boundaries, 1):
        parts = card_parts(run_dir, k, seed=seed)
        c = parts["cand"]
        cid = card_id(seed, run_dir.name, k, c["cand_id"])
        hists[f"h{n}"] = RP._gz64(parts["history"])
        hists[f"p{n}"] = RP._gz64(parts["post"])
        cards.append({"id": cid, **parts["card"], "hist_ref": f"h{n}", "post_ref": f"p{n}"})
        key[cid] = {"run_dir": _rel(run_dir), "k": k, "cand_id": c["cand_id"],
                    "source_policy": c["source_policy"], "req_id": c["req_id"],
                    "content_sha256": parts["content_sha256"],
                    "screen_design": parts["card"]["design"]}
    draw = _sha(sorted((k, v["content_sha256"]) for k, v in key.items()))[:16]
    data = {"cards": cards, "histories": hists, "collections": COLLECTIONS, "draw": draw}
    html = render(data)
    retired = {**((previous or {}).get("retired") or {}), **((previous or {}).get("cards") or {})}
    retired = {cid: e for cid, e in retired.items() if cid not in key}
    return html, {"draw": draw, "seed": seed, "built_utc": _utc(), "cards": key,
                  "retired": retired,
                  "order": [c["id"] for c in cards], "checks": list(CHECKS),
                  "page_sha256": hashlib.sha256(html.encode()).hexdigest()}


HEADING = "SWE-Interact pilot: candidate cards"
LEDE = ("Each card is one requirement the simulated maintainer disclosed before a compaction "
        "boundary, which this card's summary may have dropped. Answer the six checks. The first "
        "answer on each is the usable side (it turns green); a card is frozen only with all six "
        "on that side and a complete design, and “Can’t tell” on any check rejects it (the "
        "boundary's next candidate then gets a round-2 card). Haiku's drafts are suggestions. "
        "No continuation has run, and nothing here comes from after the boundary.")

# [name, question, hint, label for "yes" (passes: usable side), label for "no"]. Descriptive
# answers, never Yes/No, and no double negatives (s2_candidates.CHECK_TEXT's v2 format).
CHECK_TEXT = [
    ["Retention", "Is the requirement in this summary's resumed input?",
     ("Search the resumed-input box (the source summary, the messages its harness keeps "
      "verbatim and the re-attached step instruction). Only dropped requirements are "
      "repaired; a misstated one counts as still there."),
     "Dropped: the resumed input doesn't state it", "Still there (kept or misstated)"],
    ["Not recoverable", "Could the agent find this requirement again without the summary?",
     ("Places that count: the working tree and all branches, untracked files, /tmp/plan.md, "
      "commit messages and the step instruction. Code that already implements it makes it "
      "findable, unless only the reason for it is missing."),
     "Can't find it: nothing there states it", "Can find it there"],
    ["Applicability", "At the boundary, does the requirement still bear on the work left?",
     ("A requirement already fully implemented counts only if later edits could still undo it "
      "(a preservation rule, a “don't touch”)."),
     "Still bears on the remaining work", "Settled: nothing left depends on it"],
    ["Governed decision", "Is there a later act it governs, observable before the readout ends?",
     ("An act that satisfies or contradicts it, nameable from the task and the boundary state, "
      "seen before the agent's first ask_user, the end of the step or 60 calls."),
     "A governed act can be named and seen in time", "Nothing it governs in that window"],
    ["Design", "Are the repair text, the matched text, the act and its scorer fair?",
     ("Repair: the maintainer's own words. Matched: an item from the same history that the "
      "summary also lacks, about the same length, with no bearing on the act. The act is what "
      "the loss predicts. Edit the fields below. A continuation that never reaches the "
      "decision scores 0."),
     "Fair, ready to freeze (with any edits)", "Unfair or not usable"],
    ["Reference", "Does the task's reference solution stay clear of the act?",
     ("The reference solution must not perform the act, so following the requirement never "
      "costs task reward. A requirement that goes beyond the task text fails here too."),
     "Reference stays clear of the act", "Reference performs it, or the task doesn't ask it"],
]

BODY = """
<div class="wrap">
  <header style="display:grid;gap:8px">
    <h1>__HEADING__</h1>
    <p class="lede">__LEDE__</p>
  </header>
  <div class="bar" role="status">
    <span class="count" id="count">0 done</span>
    <div class="track" aria-hidden="true"><i id="fill"></i></div>
    <button type="button" class="ghost" id="next">Next unfinished</button>
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
<style>
.pre { white-space: pre-wrap; font: 12.5px/1.5 var(--f-mono); max-height: 22rem; overflow: auto; background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; padding: 10px; overflow-wrap: anywhere; }
.ev { margin: 6px 0 0; padding-left: 1.1rem; overflow-wrap: anywhere; }
.ev li { margin: 2px 0; }
.out { font-weight: 600; }
.out.pass { color: var(--ok); } .out.fail { color: var(--bad); } .out.unresolved { color: var(--maybe); }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: 0 12px; }
select.note { width: 100%; }
details > summary { cursor: pointer; }
</style>
<script>
const DATA = __DATA_JSON__;
const CHECK_TEXT = __CHECKS_JSON__;
const COLL = DATA.collections;
const BYID = {};
for (const x of DATA.cards) {
  BYID[x.id] = { tab: "labels", x };
  BYID[x.id + "-post"] = { tab: "boxes", x: { id: x.id + "-post", kind: "boundary", hist_ref: x.post_ref, box_label: "Resumed input built from this card's summary: everything the agent would see after the boundary", find_ph: "Find in the resumed input" } };
}
const state = {};
let db = null;
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
__HISTORY_JS__
const ANS = (yes, no) => [["yes", yes, "ok"], ["no", no, "bad"], ["unclear", "Can\\u2019t tell", "none"]];
function done(id) { const l = state[id] || {}, n = (BYID[id] && BYID[id].x.nq) || CHECK_TEXT.length; return CHECK_TEXT.slice(0, n).every((_, k) => l[`c${k + 1}`]); }
const KINDS = ["diff_added", "diff_removed", "file_touched", "command", "judge"];
const DKINDS = ["none", "diff_added", "diff_removed", "file_touched", "command"];
// The frozen design, edited here; the freeze uses these, the screen's value where unedited.
const DESIGN = [
  ["d_repair", "Repair block (the maintainer's words, put back after the summary)", "textarea"],
  ["d_matched", "Matched block (same position and lead line in the other arm)", "textarea"],
  ["d_rationale", "Why the matched block has no bearing on the act", "textarea"],
  ["d_act_desc", "The act the loss predicts (scored 1 when performed)", "textarea"],
  ["d_act_kind", "Act scorer: kind", KINDS], ["d_act_negate", "Act scorer: negated (decision reached and the pattern absent)", ["no", "yes"]],
  ["d_act_pattern", "Act scorer: regex", "input"], ["d_act_path", "Act scorer: file glob (empty = any file)", "input"],
  ["d_dec_kind", "Decision scorer: kind", DKINDS], ["d_dec_pattern", "Decision scorer: regex", "input"],
  ["d_dec_path", "Decision scorer: file glob", "input"], ["d_reask", "Re-ask keywords (comma-separated)", "input"]];
function field(u, [f, label, tag]) {
  const a = `data-id="${u.id}" data-f="${f}" aria-label="${esc(label)}"`;
  const el = tag === "textarea" ? `<textarea class="note" rows="2" ${a}></textarea>`
    : Array.isArray(tag) ? `<select class="note" ${a}>${tag.map(o => `<option value="${o}">${o}</option>`).join("")}</select>`
    : `<input class="note" type="text" ${a}>`;
  return `<div><p class="label">${esc(label)}</p>${el}</div>`;
}
function designHtml(u) {
  return DESIGN.slice(0, 4).map(d => field(u, d)).join("") + `<div class="grid2">${DESIGN.slice(4).map(d => field(u, d)).join("")}</div>`
    + `<p class="lede">Fixed rule: a continuation that never reaches the decision scores 0.</p>`;
}
function evidence(u, k) { return evidenceOf(u.checks[k]); }
function evidenceOf(e) {
  const lists = (e.lists || []).map(l => `<p class="label">${esc(l.title)}</p><ul class="ev">${l.items.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`).join("");
  return `<p class="lede">Screen (${esc(e.by)}): <span class="out ${esc(e.outcome)}">${esc(e.outcome)}</span>${e.reason ? ` (${esc(e.reason)})` : ""}</p>`
    + e.lines.filter(Boolean).map(x => `<p class="lede">${esc(x)}</p>`).join("") + lists
    + (e.draft ? `<p class="lede">${esc(e.draft)}</p>` : "");
}
function card(u, i, n) {
  const checks = CHECK_TEXT.map(([name, q, hint, yes, no], k) => {
    const radios = ANS(yes, no).map(([v, t, tone]) => `<label><input type="radio" name="c${k + 1}-${u.id}" value="${v}" data-id="${u.id}" data-f="c${k + 1}" data-tone="${tone}"><span>${t}</span></label>`).join("");
    return `<div class="calls"><fieldset><legend>${k + 1}. ${esc(name)}: ${esc(q)}</legend><p class="lede">${esc(hint)}</p>${radios}</fieldset>${evidence(u, k)}${k === 4 ? designHtml(u) : ""}
      ${k === 5 && u.patch ? `<details><summary>The reference solution's patch</summary><div class="pre">${esc(u.patch)}</div></details>` : ""}
      <input class="note" type="text" data-id="${u.id}" data-f="n${k + 1}" placeholder="Note on check ${k + 1} (optional)" aria-label="Note on check ${k + 1}"></div>`;
  }).join("");
  const r = u.reply || {};
  return `<article class="sheet" id="s-${u.id}">
    <header><span class="tag">${i + 1} / ${n}</span><span class="state" id="st-${u.id}">not done</span></header>
    <p class="lede">${esc(u.context)}</p>
    <p class="lede"><strong>Summary source.</strong> ${esc(u.source_note)}</p>
    <div><p class="label">Requirement</p><p class="q">${esc(u.requirement.text)}</p></div>
    <div><p class="label">The maintainer's words (the repair block)</p><p class="q">${esc(u.requirement.quote)}</p></div>
    <div><p class="label">The reply it came from (${esc(r.step)}${r.message != null ? `, message ${esc(r.message)}` : ""})</p>
      <details><summary>The agent's request that drew it</summary><div class="pre">${esc(r.question)}</div></details>
      <div class="pre">${esc(r.reply)}</div></div>
    <details><summary>The task text the simulator holds${u.requirement.grounding_quote ? ` (grounding: “${esc(u.requirement.grounding_quote)}”)` : ""}</summary><div class="pre">${esc(u.task_text)}</div></details>
    ${histBox(u)}
    ${histBox(BYID[u.id + "-post"].x)}
    ${checks}
    <input class="note" type="text" data-id="${u.id}" data-f="note" placeholder="Note on the card (optional)" aria-label="Note on the card">
  </article>`;
}
// v2 (accumulated-loss) cards: a plain paragraph, 3 questions with their evidence, the design
// fields under question 3, and the code-only checks collapsed below.
function card2(u, i, n) {
  const qs = CHECK_TEXT.slice(0, u.nq || 3).map(([name, q, hint, yes, no], k) => {
    const radios = ANS(yes, no).map(([v, t, tone]) => `<label><input type="radio" name="c${k + 1}-${u.id}" value="${v}" data-id="${u.id}" data-f="c${k + 1}" data-tone="${tone}"><span>${t}</span></label>`).join("");
    return `<div class="calls"><fieldset><legend>${k + 1}. ${esc(name)}: ${esc(q)}</legend><p class="lede">${esc(hint)}</p>${radios}</fieldset>${(u.qev[k] || []).map(evidenceOf).join("")}${k === 2 ? designHtml(u) : ""}
      <input class="note" type="text" data-id="${u.id}" data-f="n${k + 1}" placeholder="Note on question ${k + 1} (optional)" aria-label="Note on question ${k + 1}"></div>`;
  }).join("");
  const code = `<details><summary>Code checks and their evidence</summary>${u.code_ev.map(evidenceOf).join("")}${u.patch ? `<details><summary>The reference solution's patch</summary><div class="pre">${esc(u.patch)}</div></details>` : ""}</details>`;
  const r = u.reply || {};
  return `<article class="sheet" id="s-${u.id}">
    <header><span class="tag">${i + 1} / ${n}</span><span class="state" id="st-${u.id}">not done</span></header>
    <p class="lede">${esc(u.context)}</p>
    <p class="q">${esc(u.paragraph)}</p>
    <p class="lede"><strong>Summary source.</strong> ${esc(u.source_note)}</p>
    <details><summary>The reply it came from (${esc(r.step)}${r.message != null ? `, message ${esc(r.message)}` : ""})</summary>
      <div class="pre">${esc(r.reply)}</div></details>
    <details><summary>The task text the simulator holds</summary><div class="pre">${esc(u.task_text)}</div></details>
    ${histBox(u)}
    ${histBox(BYID[u.id + "-post"].x)}
    ${qs}
    ${code}
    <input class="note" type="text" data-id="${u.id}" data-f="note" placeholder="Note on the card (optional)" aria-label="Note on the card">
  </article>`;
}
function paint(id) {
  const l = state[id] || {}, def = BYID[id].x.design;
  document.querySelectorAll(`[data-id="${id}"]`).forEach(el => {
    const f = el.dataset.f;
    if (el.type === "radio") el.checked = el.value === l[f];
    else if (document.activeElement !== el) el.value = f in l ? l[f] : (f.startsWith("d_") ? def[f] : "");
  });
  document.getElementById(`s-${id}`).classList.toggle("done", done(id));
  document.getElementById(`st-${id}`).textContent = done(id) ? "done" : "not done";
}
function tally() {
  const n = DATA.cards.filter(x => done(x.id)).length;
  document.getElementById("count").textContent = `${n} / ${DATA.cards.length} done`;
  document.getElementById("fill").style.width = `${100 * n / Math.max(1, DATA.cards.length)}%`;
  const rows = DATA.cards.filter(x => state[x.id]).map(x => ({ id: x.id, collection: COLL.labels, data: { ...state[x.id], v: x.v } }));
  document.getElementById("dump").value = JSON.stringify(rows, null, 1);
}
function setSave(text, warn) { const el = document.getElementById("save"); el.textContent = text; el.classList.toggle("warn", !!warn); }
// Storage: collection "labels", doc id = card id, {c1..c6, n1..n6, note, d_* (edited design
// fields), v (the card's content version), utc}. rev[id] counts local edits; saved[id] is the
// last revision written; a db snapshot never replaces a card with an unwritten edit.
const rev = {}, saved = {};
const dirty = id => (rev[id] || 0) !== (saved[id] || 0);
async function submit(labels) {
  for (const [id, l] of Object.entries(labels)) { state[id] = { ...(state[id] || {}), ...l }; paint(id); }
  tally();
  if (!db) { setSave("Not saving: storage unavailable. Use the copy below.", true); return false; }
  try {
    for (const id of Object.keys(labels)) {
      const r = rev[id] || 0;
      await db.collection(COLL.labels).doc(id).set({ ...state[id], v: BYID[id].x.v, utc: new Date().toISOString() });
      saved[id] = Math.max(saved[id] || 0, r);
    }
    setSave("Saved"); return true;
  } catch (e) { setSave(`Not saved (${e && e.code || "error"}). Use the copy below.`, true); return false; }
}
const timers = {};
function onEdit(ev, immediate) {
  const t = ev.target, id = t.dataset && t.dataset.id, f = t.dataset && t.dataset.f;
  if (!id || !f || !BYID[id] || BYID[id].tab !== "labels") return;
  rev[id] = (rev[id] || 0) + 1;
  state[id] = { ...(state[id] || {}), [f]: t.value }; paint(id); tally();
  clearTimeout(timers[id]);
  const go = () => submit({ [id]: state[id] });
  if (immediate) go(); else timers[id] = setTimeout(go, 1200);
}
document.addEventListener("change", ev => onEdit(ev, true));
document.addEventListener("input", ev => { if (ev.target.classList.contains("note") && ev.target.tagName !== "SELECT") onEdit(ev, false); });
document.getElementById("next").addEventListener("click", () => {
  const x = DATA.cards.find(x => !done(x.id));
  if (x) document.getElementById(`s-${x.id}`).scrollIntoView({ behavior: "smooth", block: "start" });
});
document.getElementById("copy").addEventListener("click", async () => {
  const ta = document.getElementById("dump");
  try { await navigator.clipboard.writeText(ta.value); setSave("Copied"); }
  catch (e) { ta.select(); setSave("Selected: press copy", true); }
});
document.getElementById("sheets").innerHTML = DATA.cards.map((u, i) => (DATA.v2 ? card2 : card)(u, i, DATA.cards.length)).join("");
DATA.cards.forEach(x => paint(x.id)); tally();
requestAnimationFrame(() => hsObserve());
// Label controls stay disabled until the saved labels have loaded (or storage is known to be
// unavailable): an edit before that would write a document missing the saved answers.
function lock(on) { document.querySelectorAll("[data-id][data-f]").forEach(el => { el.disabled = on; }); }
lock(true);
(async () => {
  try { db = window.claude ? await window.claude.use("db") : null; } catch (e) { db = null; }
  if (!db) { lock(false); setSave("Not saving: storage unavailable here. Use the copy below.", true); return; }
  setSave("Loading saved labels…");
  let first = true;
  db.collection(COLL.labels).onSnapshot(snap => {
    if (first) { first = false; lock(false); setSave("Saving on"); }
    snap.docs.forEach(d => {
      const v = d.data();
      if (!BYID[d.id] || BYID[d.id].tab !== "labels" || v.v !== BYID[d.id].x.v || dirty(d.id)) return;
      const s = {};
      for (const [k, x] of Object.entries(v)) if (k !== "v" && k !== "utc") s[k] = x;
      state[d.id] = s; paint(d.id);
    });
    tally();
  }, e => {
    if (first) { db = null; lock(false); setSave(`Couldn't load saved labels (${e && e.code || "error"}), so this view won't save. Use the copy below.`, true); return; }
    setSave(`Live sync stopped (${e && e.code || "error"}). Edits still save.`, true);
  });
})();
</script>
"""


def render(data: dict) -> str:
    head, _ = RP.template_parts()
    v2 = bool(data.get("v2"))
    head = head.replace("__TITLE__", "Accumulated-loss cards" if v2 else
                        "Interact candidate cards")
    body = (BODY.replace("__HEADING__", HEADING_V2 if v2 else HEADING)
            .replace("__LEDE__", LEDE_V2 if v2 else LEDE)
            .replace("__HISTORY_JS__", SC2.history_js())
            .replace("__CHECKS_JSON__", S1P._script_json(QUESTIONS if v2 else CHECK_TEXT)))
    return head + body.replace("__DATA_JSON__", S1P._script_json(data))


def leak_check(html: str, key: Mapping) -> None:
    """Refuse a page carrying a key field name, a candidate id, or a card field outside
    CARD_FIELDS."""
    hits = [m for m in KEY_MARKERS if m in html]
    hits += [v["cand_id"] for v in key["cards"].values() if v["cand_id"] in html]
    hits += [f for c in RP.page_data(html)["cards"] for f in c if f not in CARD_FIELDS]
    if hits:
        raise S1P.LeakError(f"page carries key strings: {hits[:10]}")


def write_page(out_dir: Path, html: str, key: dict, *, private_root: Path | None = None,
               check_ignored: bool = True) -> dict:
    out = RP.require_private(Path(out_dir), private_root or PRIVATE_ROOT)
    if check_ignored:
        RP.require_ignored(out)
    leak_check(html, key)
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise ValueError(f"page is {size:,} bytes, above {MAX_PAGE_BYTES:,}")
    out.mkdir(parents=True, exist_ok=True)
    S1P._atomic(out / KEY_NAME, (json.dumps(key, indent=1, sort_keys=True) + "\n").encode(),
                mode=0o600)
    S1P._atomic(out / PAGE_NAME, html.encode("utf-8"))
    return {"page": str(out / PAGE_NAME), "key": str(out / KEY_NAME), "bytes": size,
            "cards": len(key["cards"]), "draw": key["draw"]}


# ------------------------------------------------------------------------------------------
# Freeze
# ------------------------------------------------------------------------------------------


def _docs(obj: Any, stem: str | None) -> dict[str, dict]:
    """Label docs {id: data} from one parsed export (module docstring)."""
    if isinstance(obj, Mapping) and any(f"c{k}" in obj for k in range(1, 7)) and stem:
        return {stem: dict(obj)}  # the bare data of one doc, named by its file
    if isinstance(obj, Mapping) and isinstance(obj.get("data"), Mapping) and \
            obj.get("collection") in (None, COLLECTIONS["labels"]) and (obj.get("id") or stem):
        return {str(obj.get("id") or stem): dict(obj["data"])}  # one doc, {"id"?, "data"}
    from s1_label_score import normalize_labels

    return dict(normalize_labels(obj).get(COLLECTIONS["labels"]) or {})


def read_labels(path: Path) -> dict[str, dict]:
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    if not files:
        raise FileNotFoundError(f"{path}: no label files")
    out: dict[str, dict] = {}
    for f in files:
        out.update(_docs(_read(f), f.stem))
    return out


def answers(label: Mapping) -> list[str]:
    row = []
    for k in range(1, len(CHECKS) + 1):
        a = label.get(f"c{k}") or "missing"
        if a not in (*ANSWERS, "missing"):
            raise ValueError(f"bad answer {a!r} on check {k}")
        row.append(a)
    return row


def final_design(label: Mapping, screen: Mapping) -> tuple[dict, list[str]]:
    """(design fields, the fields Jacob edited)."""
    d, edited = {}, []
    for f in DESIGN_FIELDS:
        v = label.get(f)
        if v is not None and v != screen.get(f):
            edited.append(f)
            d[f] = v
        else:
            d[f] = screen.get(f, "")
    return d, edited


def design_gaps(design: Mapping) -> list[str]:
    gaps = [f for f in DESIGN_TEXT if not str(design.get(f) or "").strip()]
    gaps += [f"act spec: {e}" for e in S.validate_spec(spec_of(design))]
    return gaps


def notes_of(label: Mapping) -> dict[str, str]:
    out = {CHECKS[k - 1]: str(label[f"n{k}"]).strip() for k in range(1, len(CHECKS) + 1)
           if str(label.get(f"n{k}") or "").strip()}
    if str(label.get("note") or "").strip():
        out["card"] = str(label["note"]).strip()
    return out


def _frozen_in(run_dir: Path, k: int) -> list[str]:
    return sorted(p.stem for p in (Path(run_dir) / f"b{k}" / "boundary" / "freeze").glob(
        "*.json"))


def freeze_card(cid: str, ent: Mapping, label: Mapping, *, seed: int,
                runner: S.Runner | None = None) -> dict:
    """One card's decision (module docstring); writes the freeze record when it passes."""
    run_dir = (ROOT / ent["run_dir"]) if not Path(ent["run_dir"]).is_absolute() else \
        Path(ent["run_dir"])
    k, cand_id = int(ent["k"]), ent["cand_id"]
    rec = {"card": cid, "run_dir": ent["run_dir"], "k": k, "cand_id": cand_id,
           "notes": notes_of(label)}
    ans = answers(label)
    rec["answers"] = dict(zip(CHECKS, ans, strict=True))
    if "missing" in ans:
        return rec | {"status": "unlabelled",
                      "reasons": [f"{n}: unanswered" for n, a in zip(CHECKS, ans, strict=True)
                                  if a == "missing"]}
    bad = [f"{n}: {a}" for n, a in zip(CHECKS, ans, strict=True) if a != "yes"]
    if bad:
        return rec | {"status": "rejected", "reasons": bad}
    held = []
    if label.get("v") != ent["content_sha256"][:16]:
        held.append("labels were made on another version of this card")
    if arms := arms_dirs(run_dir, k):
        held.append(f"arms/ exists: {[str(a) for a in arms]}")
    frozen = _frozen_in(run_dir, k)
    if cand_id in frozen:
        return rec | {"status": "already_frozen", "reasons": []}
    if frozen:
        held.append(f"boundary already has a freeze: {frozen}")
    parts = card_parts(run_dir, k, cand_id, seed=seed)
    if parts["content_sha256"] != ent["content_sha256"]:
        held.append("the card's evidence changed since the page was built")
    cj = _read(run_dir / f"b{k}" / "boundary" / "candidates.json")
    if (cj.get("carded") != cand_id or cand_id in (cj.get("rejected") or [])
            or parts["cand"].get("disposition") != "carded"):
        # disposition isn't evidence (a round-2 rescreen rewrites it), so it's checked here
        held.append(f"no longer the boundary's carded candidate (carded now: "
                    f"{cj.get('carded')}, disposition {parts['cand'].get('disposition')})")
    design, edited = final_design(label, ent["screen_design"])
    rec["edited"] = edited
    gaps = design_gaps(design)
    if gaps:
        held.append("design incomplete: " + "; ".join(gaps))
    if held:
        return rec | {"status": "held", "reasons": held}
    c = parts["cand"]
    spec = spec_of(design) if set(edited) & set(SPEC_FIELDS) else c["act_spec"]
    checks = json.loads(json.dumps(c.get("checks") or {}))
    bdir = run_dir / f"b{k}"
    if edited:
        # Rerun the code parts of checks 5 and 6 on the edited design; only problems the card
        # already showed are covered by Jacob's yes.
        b = bdir / "boundary"
        new = RQ.check_design({"quote": design["d_repair"]},
                              {"matched_quote": design["d_matched"],
                               "matched_rationale": design["d_rationale"]},
                              spec, S.source_view(bdir, c["source_policy"]),
                              _read(b / "pre_messages.json"))
        shown = set(((checks.get("design") or {}).get("evidence") or {}).get("problems") or [])
        added = [p for p in new["evidence"]["problems"] if p not in shown]
        if added:
            held.append(f"edited design adds code problems: {added}")
        checks["design"] = new | {"screened": checks.get("design")}
        if spec != c.get("act_spec"):
            ref = S.check_reference(spec, run_dir, k, runner=runner)
            if ref["outcome"] != "pass":
                held.append(f"edited act: check 6 is {ref['outcome']} ({ref.get('reason')})")
            checks["reference"] = ref | {"screened": checks.get("reference")}
    if held:
        return rec | {"status": "held", "reasons": held}
    for n in CHECKS:
        checks[n] = dict(checks.get(n) or {}, jacob="pass",
                         jacob_note=rec["notes"].get(n))
    frz = {"cand_id": cand_id, "source_policy": c["source_policy"], "req_id": c["req_id"],
           "repair_text": design["d_repair"], "matched_text": design["d_matched"],
           "matched_rationale": design["d_rationale"], "act_spec": spec, "checks": checks,
           "card": {"id": cid, "v": label.get("v"), "edited": edited,
                    "note": rec["notes"].get("card"), "labelled_utc": label.get("utc")}}
    path = S.write_freeze(bdir, frz)
    return rec | {"status": "frozen", "reasons": [], "freeze": _rel(path),
                  "publish": (f"uv run python scripts/s2_interact.py publish-freeze "
                              f"{ent['run_dir']} {k} {cand_id}")}


def freeze(labels: Mapping[str, dict], key: Mapping, *,
           runner: S.Runner | None = None) -> list[dict]:
    """Decisions for the page's cards, plus an `earlier_round` row (with its notes) for each
    label on a card an earlier page carried (the db keeps round-1 documents)."""
    retired = key.get("retired") or {}
    unknown = sorted(set(labels) - set(key["cards"]) - set(retired))
    if unknown:
        raise ValueError(f"labels for cards on no page this key knows: {unknown[:5]}")
    out = [freeze_card(cid, key["cards"][cid], labels.get(cid) or {}, seed=key["seed"],
                       runner=runner) for cid in key["order"]]
    for cid in sorted(set(labels) & set(retired)):
        ent = retired[cid]
        out.append({"card": cid, "run_dir": ent["run_dir"], "k": ent["k"],
                    "cand_id": ent["cand_id"], "notes": notes_of(labels[cid]),
                    "status": "earlier_round",
                    "reasons": ["card from an earlier page; its decision was made there"]})
    return out


def write_decisions(out_dir: Path, key: Mapping, results: list[dict]) -> Path:
    p = Path(out_dir) / DECISIONS_NAME
    old = _read(p) if p.exists() else {"cards": {}}
    for r in results:
        prev = old["cards"].get(r["card"]) or {}
        if (prev.get("status") == "frozen" and r["status"] == "already_frozen") or \
                (prev and r["status"] == "earlier_round"):
            continue
        old["cards"][r["card"]] = r | {"decided_utc": _utc()}
    old.update(updated_utc=_utc(), draw=key["draw"])
    S1P._atomic(p, (json.dumps(old, indent=1, sort_keys=True) + "\n").encode())
    return p


def report_lines(results: list[dict]) -> list[str]:
    out = []
    for r in results:
        head = f"{r['run_dir']} b{r['k']} {r['cand_id']} (card {r['card']}): {r['status']}"
        out.append(head + (f" — {'; '.join(r['reasons'])}" if r.get("reasons") else ""))
        for n, t in (r.get("notes") or {}).items():
            out.append(f"    note [{n}]: {t}")
        if r.get("edited"):
            out.append(f"    edited design fields: {', '.join(r['edited'])}")
        if r["status"] == "frozen":
            out.append(f"    next: {r['publish']}")
    rej: dict[str, list[str]] = {}
    for r in results:
        if r["status"] == "rejected":
            rej.setdefault(f"{r['run_dir']}/b{r['k']}", []).append(r["cand_id"])
    for b, ids in rej.items():
        out.append(f"round 2 for {b}: rescreen with interact_reqs.screen(bdir, rejected={ids})")
    return out


# ------------------------------------------------------------------------------------------
# v2: accumulated-loss cards (docs/sprint/plans/accumulated-loss.md, "Jacob's card")
# ------------------------------------------------------------------------------------------
# One card per candidate the v2 screen carded or bundled (boundary/fork_v2/candidates.json): a
# plain paragraph, then 3 questions with descriptive answers. Code-only checks (recoverability,
# the reference solution, the pending spot's lookup, the design's code problems) sit collapsed
# below with their evidence. All three answered on the usable side freezes the candidate: every
# check gets jacob "pass" (an unresolved code check counts as accepted by those answers, which
# the card shows), and once two or more candidates of one source are frozen at a boundary the
# bundle record follows. A run sealed past a boundary (sealed.json) shows no card from after it
# until the outputs are unsealed (`--unsealed`).

CHECKS_V2 = RQ.CHECKS_V2
HEADING_V2 = "Accumulated-loss study: candidate cards"
LEDE_V2 = ("Each card is one requirement the simulated maintainer disclosed that this card's "
           "summary no longer carries at a compaction point, with an open decision it governs. "
           "Answer the three questions; the first answer on each is the usable side. A card is "
           "frozen only with all three on that side and a complete design; any other answer "
           "rejects it. Code ran the other checks (collapsed below, with the evidence). Nothing "
           "here comes from after the compaction point.")
QUESTIONS = [
    ["Really lost", "Is the requirement missing from what the agent resumes with?",
     ("Search the resumed-input box. A misstated requirement counts as still there. The grade "
      "below is the judge's; the requirement's words are in the paragraph above."),
     "Lost: the resumed input doesn't carry it", "Still there (kept or misstated)"],
    ["Still matters", "At this point, is a decision it governs still open?",
     ("The pending spot below is a line code found in the workspace at the compaction. It "
      "should be code that doesn't yet do what the requirement asks, or that a later edit it "
      "governs would still change."),
     "Open: the decision is still ahead", "Settled, or nothing left depends on it"],
    ["Visible in the window", "Would we see the agent act on it before its next question?",
     ("The window runs from the compaction to the agent's next ask_user, a submission or 60 "
      "calls. The act and its scorer are in the fields below; edit them if needed."),
     "Yes: the act would show in the window, and the design is fair",
     "No, or the design isn't fair"],
    # shown only when a code check couldn't settle its question (astra r2, Jacob 24 Oct)
    ["Left open by code", "Code couldn't settle the check(s) shown here. Are they acceptable?",
     ("Recoverability: could the agent find the requirement again without the summary (the "
      "repo, branches, /tmp/plan.md, commit messages)? Reference: does the task's reference "
      "solution stay clear of the act? Their evidence is below."),
     "Acceptable: not findable, and the reference stays clear", "Not acceptable"],
]
QMAP = {0: ("retention",), 1: ("applicability", "decision"), 2: ("design",),
        3: ("recoverability", "reference")}
CODE_ONLY = ("recoverability", "reference", "design")
Q4_CHECKS = ("recoverability", "reference")


def check_evidence_v2(c: Mapping) -> dict[str, dict]:
    """Per check name: the evidence card (check_evidence's shape), the pending spot added."""
    base = dict(zip(CHECKS, check_evidence(c), strict=True))
    x = (((c.get("checks") or {}).get("applicability") or {}).get("evidence") or {}).get(
        "pending") or {}
    ev = x.get("evidence") or {}
    e = {**_outcome(x), "by": "code: pending spot", "lists": [], "draft": None, "lines": [
        f"Haiku's pending spot: {ev.get('path')}: “{ev.get('quote')}”",
        f"Why it is open: {ev.get('why') or ''}",
        ("Found at that path in the kept boundary image." if x.get("outcome") == "pass" else
         "Not found at that path in the kept boundary image."
         if x.get("outcome") == "fail" else
         ("Not verbatim at that path (a copying slip?): identifiers from it are there: "
          + ", ".join(f"“{i}”" for i in (ev.get("near") or {})) + ". Your call."
          if x.get("reason") == "pending_line_not_verbatim" else
          f"Lookup did not run ({x.get('reason')})."))]}
    if ev.get("found"):
        e["lists"].append({"title": "Matching lines", "items": [
            f"{ev.get('path')}:{f.get('line')}: {f.get('text')}" for f in ev["found"]]})
    base["pending"] = e
    for name, b in base.items():
        b["by"] = f"{b['by']} · {name}"
    return base


def candidate_v2(run_dir: Path, k: int, cand_id: str) -> dict:
    cj = _read(Path(run_dir) / f"b{k}" / "boundary" / RQ.SCREEN_V2 / "candidates.json")
    c = next((c for c in cj.get("candidates") or [] if c.get("cand_id") == cand_id), None)
    if c is None or c.get("disposition") not in ("carded", "bundle"):
        raise ValueError(f"{Path(run_dir).name} b{k}: {cand_id} is not carded or bundled")
    return c


def card_ids_v2(run_dir: Path, k: int) -> list[str]:
    """The boundary's open cards: carded in its selection state, minus rejected ones."""
    sel = IR.selection(Path(run_dir) / f"b{k}") or {}
    if sel.get("state") != "carded":
        return []
    return [c for c in sel.get("cards") or [] if c not in (sel.get("rejected") or [])]


PROMPT_CHANGED_NOTE = (" Prompt changed: this replay didn't use Experiment 1's frozen agy 1.4.3 "
                       "prompt set (its recorded prompt hash differs or is missing).")


def source_note(bdir: Path, src: str) -> str:
    """The card's source note; an Antigravity replay not on the frozen prompt set says so
    (a hash mismatch proves only "not the frozen set", not which version it was)."""
    return SOURCE_NOTES.get(src, src) + (PROMPT_CHANGED_NOTE
                                         if IR.replay_prompt_changed(bdir, src) else "")


def paragraph(c: Mapping, k: int, bj: Mapping, reply: Mapping, src: str) -> str:
    loss = c.get("loss") or {}
    status = {"new": "dropped it at this compaction",
              "inherited": "still lacks it (an earlier compaction already lost it)"}.get(
        loss.get("status"), "lacks it")
    pend = ((((c.get("checks") or {}).get("applicability") or {}).get("evidence") or {})
            .get("pending") or {}).get("evidence") or {}
    dec = (((c.get("checks") or {}).get("decision") or {}).get("evidence") or {}).get(
        "decision") or (c.get("draft") or {}).get("decision") or "(no decision drafted)"
    return (f"The maintainer asked (reply {c.get('source_reply_id')}, "
            f"{reply.get('step') or '02_implement'}): “{c.get('repair_text')}”. "
            f"At compaction {k}"
            + (f", {bj['calls_in_step']} calls into the implementation step"
               if bj.get("calls_in_step") is not None else "")
            + f", {SOURCE_NAMES.get(src, src)} {status}. The decision it governs: {dec} "
            f"The workspace at that point still reads, in {pend.get('path')}: "
            f"“{pend.get('quote')}”.")


def card_parts_v2(run_dir: Path, k: int, cand_id: str, *, seed: int = SEED) -> dict:
    run_dir = Path(run_dir)
    bdir = run_dir / f"b{k}"
    b = bdir / "boundary"
    c = candidate_v2(run_dir, k, cand_id)
    src = c["source_policy"]
    pre = _read(b / "pre_messages.json")
    view = S.source_view(bdir, src)
    reply = next((r for r in _read(b / "sim_replies.json")
                  if r.get("reply_id") == c.get("source_reply_id")), {})
    bj = _read(b / "boundary.json") if (b / "boundary.json").exists() else {}
    task = (_read(bdir / "run.json") if (bdir / "run.json").exists() else
            _read(run_dir / "run.json")).get("task") or {}
    hist, post = render_history(pre), render_post(view)
    quote = c.get("repair_text")
    pi = reply.get("pre_index")
    near = hist.find(f"[message {pi}: ") if isinstance(pi, int) else None
    at = SC2.locate(hist, quote, near if near and near >= 0 else None)
    patch_rel = task.get("solution_patch")
    patch_p = (ROOT / patch_rel) if patch_rel and not Path(patch_rel).is_absolute() else \
        (Path(patch_rel) if patch_rel else None)
    ev = check_evidence_v2(c)
    q4 = [n for n in Q4_CHECKS
          if ((c.get("checks") or {}).get(n) or {}).get("outcome") == "unresolved"]
    nq = 4 if q4 else 3
    qev = [[ev[n] for n in QMAP[0]], [ev["applicability"], ev["pending"], ev["decision"]],
           [ev[n] for n in QMAP[2]], [ev[n] for n in q4]][:nq]
    card = {
        "kind": "item", "hist_at": at, "located": at is not None, "ctx_quote": quote or "",
        "box_label": ("The agent's history before the compaction"
                      + (", scrolled to the requirement's quote" if at else "")),
        "find_ph": "Find in the agent's history",
        "context": (f"Task {task.get('task_id')} · compaction {k} · summary source: "
                    f"{SOURCE_NAMES.get(src, src)}"),
        "source_note": source_note(bdir, src),
        "paragraph": paragraph(c, k, bj, reply, src),
        "loss": (c.get("loss") or {}).get("status"),
        "requirement": {"text": c.get("text"), "quote": quote, "grounding_quote": None},
        "reply": {"step": reply.get("step"), "question": reply.get("question"),
                  "reply": reply.get("reply"), "message": pi},
        "task_text": RQ.task_text(bdir),
        "checks": [], "qev": qev, "nq": nq,
        "code_ev": [ev[n] for n in CODE_ONLY],
        "design": screen_design(c),
        "patch": patch_p.read_text(errors="replace") if patch_p and patch_p.exists() else None,
    }
    screened = {kk: v for kk, v in c.items() if kk != "disposition"}
    content = _sha([card, screened, hist, post])
    card["v"] = content[:16]
    return {"card": card, "history": hist, "post": post, "cand": c, "content_sha256": content}


def build_v2(boundaries: list[tuple[Path, int]], *, seed: int = SEED, unsealed: bool = False,
             previous: Mapping | None = None) -> tuple[str, dict]:
    """(html, key): a card per carded or bundled v2 candidate of each boundary. Refuses a
    boundary with continuation output (arms/), and one past a run's sealed point unless
    `unsealed`."""
    if len({(str(r), k) for r, k in boundaries}) != len(boundaries):
        raise ValueError("a boundary is given twice")
    for run_dir, k in boundaries:
        if arms := arms_dirs(run_dir, k):
            raise ArmsExist(f"{run_dir.name} b{k}: {[str(a) for a in arms]} exists; a card "
                            "must never sit beside continuation output")
        sa = IR.active_seal(run_dir)
        if sa is not None and k > sa and not unsealed:
            raise ArmsExist(f"{run_dir.name} b{k} is past the sealed point b{sa}: its "
                            "evidence comes from a sealed continuation")
    cards, hists, key, n = [], {}, {}, 0
    for run_dir, k in boundaries:
        for cid_ in card_ids_v2(run_dir, k):
            n += 1
            parts = card_parts_v2(run_dir, k, cid_, seed=seed)
            c = parts["cand"]
            cid = card_id(seed, run_dir.name, k, c["cand_id"])
            hists[f"h{n}"] = RP._gz64(parts["history"])
            hists[f"p{n}"] = RP._gz64(parts["post"])
            cards.append({"id": cid, **parts["card"], "hist_ref": f"h{n}", "post_ref": f"p{n}"})
            key[cid] = {"run_dir": _rel(run_dir), "k": k, "cand_id": c["cand_id"],
                        "source_policy": c["source_policy"], "req_id": c["req_id"],
                        "disposition": c["disposition"], "nq": parts["card"]["nq"],
                        "content_sha256": parts["content_sha256"],
                        "screen_design": parts["card"]["design"]}
    draw = _sha(sorted((k, v["content_sha256"]) for k, v in key.items()))[:16]
    data = {"cards": cards, "histories": hists, "collections": COLLECTIONS, "draw": draw,
            "v2": True}
    html = render(data)
    retired = {**((previous or {}).get("retired") or {}), **((previous or {}).get("cards") or {})}
    retired = {cid: e for cid, e in retired.items() if cid not in key}
    return html, {"draw": draw, "seed": seed, "built_utc": _utc(), "cards": key,
                  "retired": retired, "v2": True,
                  "order": [c["id"] for c in cards], "questions": [q[0] for q in QUESTIONS],
                  "page_sha256": hashlib.sha256(html.encode()).hexdigest()}


def answers_v2(label: Mapping, nq: int = 3) -> list[str]:
    row = []
    for k in range(1, nq + 1):
        a = label.get(f"c{k}") or "missing"
        if a not in (*ANSWERS, "missing"):
            raise ValueError(f"bad answer {a!r} on question {k}")
        row.append(a)
    return row


def freeze_card_v2(cid: str, ent: Mapping, label: Mapping, *, seed: int,
                   runner: S.Runner | None = None) -> dict:
    run_dir = (ROOT / ent["run_dir"]) if not Path(ent["run_dir"]).is_absolute() else \
        Path(ent["run_dir"])
    k, cand_id = int(ent["k"]), ent["cand_id"]
    nq = int(ent.get("nq") or 3)
    names = [q[0] for q in QUESTIONS[:nq]]
    role = "primary" if ent.get("disposition") == "carded" else "bundle_member"
    notes = {names[i - 1]: str(label[f"n{i}"]).strip() for i in range(1, len(names) + 1)
             if str(label.get(f"n{i}") or "").strip()}
    if str(label.get("note") or "").strip():
        notes["card"] = str(label["note"]).strip()
    rec = {"card": cid, "run_dir": ent["run_dir"], "k": k, "cand_id": cand_id, "notes": notes,
           "disposition": ent.get("disposition"), "role": role}
    ans = answers_v2(label, nq)
    rec["answers"] = dict(zip(names, ans, strict=True))
    if "missing" in ans:
        return rec | {"status": "unlabelled", "reasons": [f"{n}: unanswered" for n, a in
                                                          zip(names, ans, strict=True)
                                                          if a == "missing"]}
    bad = [f"{n}: {a}" for n, a in zip(names, ans, strict=True) if a != "yes"]
    if bad:
        return rec | {"status": "rejected", "reasons": bad}
    held = []
    if label.get("v") != ent["content_sha256"][:16]:
        held.append("labels were made on another version of this card")
    if arms := arms_dirs(run_dir, k):
        held.append(f"arms/ exists: {[str(a) for a in arms]}")
    frozen = _frozen_in(run_dir, k)
    if cand_id in frozen:
        return rec | {"status": "already_frozen", "reasons": []}
    other = [f for f in frozen if not f.startswith(ent["source_policy"] + ".")
             and not f.startswith("bundle.")]
    if other:
        held.append(f"boundary already has a freeze from another source: {other}")
    primaries = _primaries(run_dir)
    if role == "primary" and [p for p in primaries if p != (k, cand_id)]:
        held.append(f"the run already has its primary: {primaries} (one per task)")
    if role == "bundle_member" and (k, cand_id) not in primaries and \
            not [p for p in primaries if p[0] == k]:
        held.append("bundle member: its boundary's primary isn't frozen")
    parts = card_parts_v2(run_dir, k, cand_id, seed=seed)
    if parts["content_sha256"] != ent["content_sha256"]:
        held.append("the card's evidence changed since the page was built")
    design, edited = final_design(label, ent["screen_design"])
    rec["edited"] = edited
    gaps = design_gaps(design)
    if gaps:
        held.append("design incomplete: " + "; ".join(gaps))
    c = parts["cand"]
    spec = spec_of(design) if set(edited) & set(SPEC_FIELDS) else c["act_spec"]
    checks = json.loads(json.dumps(c.get("checks") or {}))
    bdir = run_dir / f"b{k}"
    if edited and not held:
        new = RQ.check_design({"quote": design["d_repair"]},
                              {"matched_quote": design["d_matched"],
                               "matched_rationale": design["d_rationale"]},
                              spec, S.source_view(bdir, c["source_policy"]),
                              _read(bdir / "boundary" / "pre_messages.json"))
        shown = set(((checks.get("design") or {}).get("evidence") or {}).get("problems") or [])
        added = [p for p in new["evidence"]["problems"] if p not in shown]
        if added:
            held.append(f"edited design adds code problems: {added}")
        checks["design"] = new | {"screened": checks.get("design")}
        if spec != c.get("act_spec"):
            ref = S.check_reference(spec, run_dir, k, runner=runner)
            if ref["outcome"] != "pass":
                held.append(f"edited act: check 6 is {ref['outcome']} ({ref.get('reason')})")
            checks["reference"] = ref | {"screened": checks.get("reference")}
    if held:
        return rec | {"status": "held", "reasons": held}
    qnote = {n: notes.get(names[q]) for q, ns in QMAP.items() if q < nq for n in ns}
    for n in CHECKS_V2:
        checks[n] = dict(checks.get(n) or {}, jacob="pass", jacob_note=qnote.get(n),
                         jacob_via="3-question card")
    frz = {"cand_id": cand_id, "source_policy": c["source_policy"], "req_id": c["req_id"],
           "repair_text": design["d_repair"], "matched_text": design["d_matched"],
           "matched_rationale": design["d_rationale"], "act_spec": spec, "checks": checks,
           "screen": RQ.SCREEN_V2, "loss": c.get("loss"),
           "role": role,
           "card": {"id": cid, "v": label.get("v"), "edited": edited,
                    "note": notes.get("card"), "labelled_utc": label.get("utc")}}
    path = S.write_freeze(bdir, frz)
    return rec | {"status": "frozen", "reasons": [], "freeze": _rel(path),
                  "publish": (f"uv run python scripts/s2_interact.py publish-freeze "
                              f"{ent['run_dir']} {k} {cand_id}")}


def _primaries(run_dir: Path) -> list[tuple[int, str]]:
    """(k, cand id) of every primary freeze in the run."""
    out = []
    for fz in sorted(Path(run_dir).glob("b*/boundary/freeze/*.json")):
        f = _read(fz)
        if f.get("role") == "primary":
            out.append((int(fz.parents[2].name[1:]), f["cand_id"]))
    return out


def freeze_v2(labels: Mapping[str, dict], key: Mapping, *,
              runner: S.Runner | None = None) -> list[dict]:
    """Decisions for a v2 page's cards, then the bundle record of each boundary where two or
    more candidates of the carded source are frozen."""
    unknown = sorted(set(labels) - set(key["cards"]) - set(key.get("retired") or {}))
    if unknown:
        raise ValueError(f"labels for cards on no page this key knows: {unknown[:5]}")
    out = [freeze_card_v2(cid, key["cards"][cid], labels.get(cid) or {}, seed=key["seed"],
                          runner=runner) for cid in key["order"]]
    by_b: dict[tuple[str, int, str], list[str]] = {}
    for cid in key["order"]:
        e = key["cards"][cid]
        run_dir = (ROOT / e["run_dir"]) if not Path(e["run_dir"]).is_absolute() else \
            Path(e["run_dir"])
        if e["cand_id"] in _frozen_in(run_dir, int(e["k"])):
            by_b.setdefault((e["run_dir"], int(e["k"]), e["source_policy"]), []).append(
                e["cand_id"])
    for (rdir, k, src), members in by_b.items():
        run_dir = (ROOT / rdir) if not Path(rdir).is_absolute() else Path(rdir)
        bid = S.bundle_id(src)
        if len(members) < 2:
            continue
        if bid in _frozen_in(run_dir, k):
            out.append({"card": bid, "run_dir": rdir, "k": k, "cand_id": bid,
                        "status": "already_frozen", "reasons": [], "notes": {}})
            continue
        path = S.write_bundle_freeze(run_dir / f"b{k}", src, members)
        out.append({"card": bid, "run_dir": rdir, "k": k, "cand_id": bid, "status": "frozen",
                    "reasons": [], "notes": {}, "freeze": _rel(path),
                    "publish": (f"uv run python scripts/s2_interact.py publish-freeze "
                                f"{rdir} {k} {bid}")})
    return out


def cmd_build2(a) -> int:
    bs = [parse_boundary(x) for x in a.boundaries]
    old = Path(a.out) / KEY_NAME
    html, key = build_v2(bs, seed=a.seed, unsealed=a.unsealed,
                         previous=_read(old) if old.exists() else None)
    print(json.dumps(write_page(Path(a.out), html, key)))
    return 0


def advance_selection(results: list[dict], *, reselect: bool = True) -> list[dict]:
    """Each boundary's card decisions into its selection state (a frozen primary freezes it;
    rejected cards rescreen it for the next candidate within the caps, or exhaust it)."""
    out = []
    by: dict[tuple[str, int], list[dict]] = {}
    for r in results:
        if r.get("cand_id", "").startswith("bundle."):
            continue
        by.setdefault((r["run_dir"], int(r["k"])), []).append(r)
    for (rdir, k), rows in by.items():
        run_dir = (ROOT / rdir) if not Path(rdir).is_absolute() else Path(rdir)
        bdir = run_dir / f"b{k}"
        sel = IR.record_decisions(bdir, rows)
        if reselect and sel.get("state") == "carded" and any(
                r["status"] == "rejected" for r in rows):
            from compaction.s2 import session as SS
            sel = IR.reselect(bdir, backend_factory=lambda: IR.claude_backend(IR.PilotSpend()),
                              grep_runner=SS.grep_runner(), ref_runner=S.docker_runner(),
                              file_runner=RQ.docker_file_runner())
        out.append({"run_dir": rdir, "k": k, "state": sel.get("state"),
                    "cards": sel.get("cards"), "rejected": sel.get("rejected")})
    return out


def cmd_freeze2(a) -> int:
    key = _read(a.key)
    results = freeze_v2(read_labels(Path(a.labels)), key)
    print("\n".join(report_lines(results)))
    if not a.no_reselect:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    for row in advance_selection(results, reselect=not a.no_reselect):
        print(json.dumps({"selection": row}))
    print(json.dumps({"decisions": str(write_decisions(Path(a.key).parent, key, results)),
                      "counts": {s: sum(r["status"] == s for r in results) for s in
                                 ("frozen", "already_frozen", "rejected", "held",
                                  "unlabelled")}}))
    return 0


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------


def cmd_build(a) -> int:
    bs = [parse_boundary(x) for x in a.boundaries]
    old = Path(a.out) / KEY_NAME
    html, key = build(bs, seed=a.seed, previous=_read(old) if old.exists() else None)
    print(json.dumps(write_page(Path(a.out), html, key)))
    return 0


def cmd_freeze(a) -> int:
    key = _read(a.key)
    results = freeze(read_labels(Path(a.labels)), key)
    print("\n".join(report_lines(results)))
    print(json.dumps({"decisions": str(write_decisions(Path(a.key).parent, key, results)),
                      "counts": {s: sum(r["status"] == s for r in results) for s in
                                 ("frozen", "already_frozen", "rejected", "held",
                                  "unlabelled")}}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--boundaries", nargs="+", required=True, metavar="RUN_DIR:K")
    b.add_argument("--out", default=str(CARDS_DIR))
    b.add_argument("--seed", type=int, default=SEED)
    f = sub.add_parser("freeze")
    f.add_argument("--labels", required=True)
    f.add_argument("--key", default=str(CARDS_DIR / KEY_NAME))
    b2 = sub.add_parser("build2", help="v2: accumulated-loss cards (3 questions)")
    b2.add_argument("--boundaries", nargs="+", required=True, metavar="RUN_DIR:K")
    b2.add_argument("--out", default=str(CARDS_DIR / "v2"))
    b2.add_argument("--seed", type=int, default=SEED)
    b2.add_argument("--unsealed", action="store_true",
                    help="allow boundaries past a run's sealed point (after the global gate)")
    f2 = sub.add_parser("freeze2", help="v2: freeze from the 3-question labels")
    f2.add_argument("--labels", required=True)
    f2.add_argument("--key", default=str(CARDS_DIR / "v2" / KEY_NAME))
    f2.add_argument("--no-reselect", action="store_true",
                    help="record decisions only; don't rescreen rejected boundaries")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "freeze": cmd_freeze, "build2": cmd_build2,
            "freeze2": cmd_freeze2}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
