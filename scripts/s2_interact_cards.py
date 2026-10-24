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

**v2 (accumulated loss: `build2`/`freeze2`).** One card per open card of each boundary's
selection (`interact_run.selection`). Simplified for Jacob on 24 Oct: a header line (task,
compaction, the summary that dropped it, flags as chips), four facts (the maintainer's words in
their line, the pending code line, what the resumed input shows with a Check link to the closest
passage, and the act we'd test), and one question, "Use this as a test case?". Yes stores
c1..c<nq> = yes; No stores them unclear and each reason chip sets its own to no (c1 not lost, c2
settled, c3 wouldn't act soon or unfair, c4 findable elsewhere or a reference conflict, only when
nq = 4); Can't tell stores them all unclear. So freeze2 (`answers_v2`) is unchanged: anything but
all yes rejects. Checks code couldn't settle get one plain line by the question. Everything else
(the full reply, the task text, both history boxes, built when first opened, the code checks and
the editable design) sits in one collapsed Evidence block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
OVERRIDES_NAME = "design_overrides.json"  # design fields filled in after labelling (not Jacob's)
SEED = 20261025
# The plan's order (brief): names as in candidates.json "checks" and interact_run.CHECK_NAMES.
CHECKS = ("retention", "recoverability", "applicability", "decision", "design", "reference")
ANSWERS = ("yes", "no", "unclear")
COLLECTIONS = {"labels": "labels"}
CARD_FIELDS = {"id", "v", "kind", "hist_ref", "hist_at", "located", "ctx_quote", "box_label",
               "find_ph", "post_ref", "context", "source_note", "requirement", "reply",
               "task_text", "checks", "design", "patch",
               "qev", "code_ev", "loss", "nq", "facts", "open_lines", "post_at",
               "post_label"}  # from "qev": v2 cards
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


class StaleScreen(RuntimeError):
    """A v2 card's screen ran under rules other than the current `screen_sha_v2`."""


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
.hdr { font: 600 1rem var(--f-ui); margin: 0; }
.chip { display: inline-block; font: 500 0.75rem var(--f-ui); padding: 1px 8px; margin-left: 8px; border-radius: 999px; background: var(--maybe-soft); color: var(--ink); border: 1px solid var(--maybe); vertical-align: 1px; }
.facts { display: grid; grid-template-columns: max-content 1fr; gap: 8px 14px; margin: 0; }
.facts dt { font: 600 0.9rem var(--f-ui); color: var(--muted); }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.facts mark { background: var(--mark); color: inherit; padding: 0 2px; border-radius: 2px; }
.facts code { font: 0.85rem var(--f-mono); }
.facts .ctx, .ctx { color: var(--muted); font-size: 0.9rem; }
@media (max-width: 640px) { .facts { grid-template-columns: 1fr; gap: 2px; } .facts dd { margin-bottom: 8px; } }
fieldset.one { border: 0; padding: 0; margin: 0; display: grid; gap: 8px; }
fieldset.one legend { font: 600 1.05rem var(--f-ui); margin-bottom: 8px; }
.verdicts { display: flex; flex-wrap: wrap; gap: 10px; }
button.big { font: 600 1rem var(--f-ui); padding: 10px 20px; border-radius: 6px; border: 1px solid var(--rule); background: var(--paper); color: var(--ink); cursor: pointer; }
button.big[aria-pressed="true"][data-tone="ok"] { background: var(--ok-soft); border-color: var(--ok); }
button.big[aria-pressed="true"][data-tone="bad"] { background: var(--bad-soft); border-color: var(--bad); }
button.big[aria-pressed="true"][data-tone="none"] { background: var(--none-soft); border-color: var(--none); }
.reasons { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.reasons[hidden] { display: none; }
.reasons .lede { flex-basis: 100%; margin: 0; }
button.chipbtn { font: 500 0.85rem var(--f-ui); padding: 5px 12px; border-radius: 999px; border: 1px solid var(--rule); background: var(--paper); color: var(--ink); cursor: pointer; }
button.chipbtn[aria-pressed="true"] { background: var(--bad-soft); border-color: var(--bad); }
button:disabled { opacity: .5; cursor: default; }
.openline { margin: 0; font-size: 0.9rem; color: var(--ink); border-left: 3px solid var(--maybe); padding-left: 8px; }
details.evd > summary { font: 600 0.95rem var(--f-ui); }
details.evd > * + * { margin-top: 10px; }
</style>
<script>
const DATA = __DATA_JSON__;
const CHECK_TEXT = __CHECKS_JSON__;
const COLL = DATA.collections;
const BYID = {};
for (const x of DATA.cards) {
  BYID[x.id] = { tab: "labels", x };
  BYID[x.id + "-post"] = { tab: "boxes", x: { id: x.id + "-post", kind: "boundary", hist_ref: x.post_ref, box_label: "Resumed input built from this card's summary: everything the agent would see after the boundary", find_ph: "Find in the resumed input",
    ...("post_at" in x ? { kind: "item", hist_at: x.post_at, located: !!x.post_at, ctx_quote: x.ctx_quote, box_label: x.post_label } : {}) } };
}
const state = {};
let db = null;
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
__HISTORY_JS__
const ANS = (yes, no) => [["yes", yes, "ok"], ["no", no, "bad"], ["unclear", "Can\\u2019t tell", "none"]];
function done(id) {
  const l = state[id] || {};
  if (DATA.v2) return !!l.verdict;
  return CHECK_TEXT.every((_, k) => l[`c${k + 1}`]);
}
// v2 answers: begin. One question per card; the stored answers stay freeze2's c1..c<nq>
// (yes/no/unclear). Yes: all yes. No: all unclear, then each reason chip toggles its c to no.
// Can't tell: all unclear. `verdict` only restores the buttons.
const VERDICTS = [["yes", "Yes, use it", "ok"], ["no", "No", "bad"], ["unclear", "Can\u2019t tell", "none"]];
const REASONS = [["c1", "Not really lost"], ["c2", "Already settled / nothing left to decide"],
  ["c3", "Agent wouldn\u2019t act on it soon / unfair test"], ["c4", "Findable elsewhere / reference conflict"]];
function verdictPatch(nq, verdict) {
  const p = { verdict };
  for (let k = 1; k <= nq; k++) p[`c${k}`] = verdict === "yes" ? "yes" : "unclear";
  return p;
}
function reasonPatch(l, f) { return l.verdict === "no" ? { [f]: l[f] === "no" ? "unclear" : "no" } : null; }
// v2 answers: end
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
// v2 (accumulated-loss) cards: a header line with flags, four facts, one question, a note;
// everything else in one collapsed Evidence block (its history boxes built when first opened).
function evAll(u) {
  const seen = new Set();
  return [...(u.qev || []).flat(), ...(u.code_ev || [])].filter(e => !seen.has(e.by) && seen.add(e.by));
}
function saidHtml(s) {
  const ctx = s.ctx ? `<span class="ctx">${esc(s.ctx)}</span>` : "";
  const line = `${esc(s.before)}<mark>${esc(s.quote)}</mark>${esc(s.after)}`;
  return `${s.ctx && !s.ctx_after ? ctx + "<br>" : ""}${line}${s.ctx_after ? "<br>" + ctx : ""}`
    + (s.found ? "" : ` <span class="ctx">(not found verbatim in the reply; see Evidence)</span>`);
}
function card2(u, i, n) {
  const f = u.facts, id = u.id, r = u.reply || {};
  const chips = f.chips.map(c => `<span class="chip">${esc(c)}</span>`).join("");
  const verdicts = VERDICTS.map(([v, t, tone]) => `<button type="button" class="big" data-id="${id}" data-f="verdict" data-v="${v}" data-tone="${tone}" aria-pressed="false">${t}</button>`).join("");
  const reasons = REASONS.slice(0, u.nq || 3).map(([c, t]) => `<button type="button" class="chipbtn" data-id="${id}" data-f="${c}" data-reason="1" aria-pressed="false">${esc(t)}</button>`).join("");
  const patch = u.patch ? `<details><summary>The reference solution's patch</summary><div class="pre">${esc(u.patch)}</div></details>` : "";
  return `<article class="sheet" id="s-${id}">
    <header><span class="tag">${i + 1} / ${n}</span><span class="state" id="st-${id}">not done</span></header>
    <p class="hdr">${esc(f.header)}${chips}</p>
    <dl class="facts">
      <dt>The maintainer said:</dt><dd>${saidHtml(f.said)}</dd>
      <dt>In the code at that point:</dt><dd><code>${esc(f.code.path)}</code>: <code>${esc(f.code.quote)}</code></dd>
      <dt>After compaction the agent sees:</dt><dd>${esc(f.sees)} <button type="button" class="goto" data-check="${id}" title="Opens the resumed input at ${esc(f.check_title)}">Check</button></dd>
      <dt>We\u2019d test whether the agent:</dt><dd>${esc(f.act)}</dd>
    </dl>
    <fieldset class="one"><legend>Use this as a test case?</legend>
      <div class="verdicts">${verdicts}</div>
      ${u.open_lines.map(x => `<p class="openline">${esc(x)}</p>`).join("")}
      <div class="reasons" id="rs-${id}" hidden><p class="lede">Why not? Pick any that apply.</p>${reasons}</div>
    </fieldset>
    <input class="note" type="text" data-id="${id}" data-f="note" placeholder="Note (optional)" aria-label="Note on the card">
    <details class="evd" data-ev="${id}"><summary>Evidence</summary>
      <div><p class="label">The maintainer's full reply (${esc(r.step)}${r.message != null ? `, message ${esc(r.message)}` : ""})</p>
        <details><summary>The agent's request that drew it</summary><div class="pre">${esc(r.question)}</div></details>
        <div class="pre">${esc(r.reply)}</div></div>
      <details><summary>The task text the simulator holds</summary><div class="pre">${esc(u.task_text)}</div></details>
      <div id="eh-${id}"></div>
      <p class="label">Code checks</p>${evAll(u).map(evidenceOf).join("")}
      ${patch}
      <p class="label">Test design (prefilled from the screen; edits optional)</p>${designHtml(u)}
    </details>
  </article>`;
}
function ensureEvidence(id) {
  const el = document.getElementById(`eh-${id}`);
  if (!el || el.dataset.built) return;
  el.dataset.built = "1";
  el.innerHTML = histBox(BYID[id].x) + histBox(BYID[id + "-post"].x).replace("Back to the quote", "Back to the closest passage");
  el.querySelectorAll(".hbox[data-hist]").forEach(b => hsSeen ? hsSeen.observe(b) : hsOpen(b.dataset.hist));
}
document.addEventListener("toggle", ev => { const d = ev.target; if (d.dataset && d.dataset.ev && d.open) ensureEvidence(d.dataset.ev); }, true);
document.addEventListener("click", async ev => {
  const b = ev.target.closest && ev.target.closest("button[data-check]");
  if (!b) return;
  const id = b.dataset.check, pid = id + "-post";
  document.querySelector(`details[data-ev="${id}"]`).open = true;
  ensureEvidence(id);
  document.getElementById(`hb-${pid}`).scrollIntoView({ block: "center" });
  await hsOpen(pid);
  const h = HS[pid], span = BYID[pid].x.hist_at;
  if (h && h.node && span) { h.focus = span; hsPaint(); hsJump(pid, span); }
});
document.addEventListener("click", ev => {
  const b = ev.target.closest && ev.target.closest("button[data-id][data-f]");
  if (!b || b.disabled || !BYID[b.dataset.id]) return;
  const id = b.dataset.id, l = state[id] || {};
  const patch = b.dataset.reason ? reasonPatch(l, b.dataset.f) : verdictPatch(BYID[id].x.nq || 3, b.dataset.v);
  if (!patch) return;
  rev[id] = (rev[id] || 0) + 1;
  state[id] = { ...l, ...patch }; paint(id); tally();
  clearTimeout(timers[id]); submit({ [id]: state[id] });
});
function paint(id) {
  const l = state[id] || {}, def = BYID[id].x.design;
  document.querySelectorAll(`[data-id="${id}"]`).forEach(el => {
    const f = el.dataset.f;
    if (el.type === "radio") el.checked = el.value === l[f];
    else if (el.tagName === "BUTTON") {
      const on = el.dataset.reason ? l[f] === "no" : l[f] === el.dataset.v;
      el.setAttribute("aria-pressed", on ? "true" : "false");
    }
    else if (document.activeElement !== el) el.value = f in l ? l[f] : (f.startsWith("d_") ? def[f] : "");
  });
  const rs = document.getElementById(`rs-${id}`);
  if (rs) rs.hidden = l.verdict !== "no";
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
LEDE_V2 = ("Each card is a requirement the grader marked as dropped by a summary. Decide "
           "whether it's a fair test case; evidence is folded under each card.")
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


def screen_stale(run_dir: Path, k: int) -> str | None:
    """The boundary's v2 screen hash when it isn't the current one, else None (no screen
    yet: nothing to judge)."""
    f = Path(run_dir) / f"b{k}" / "boundary" / RQ.SCREEN_V2 / "candidates.json"
    if not f.exists():
        return None
    sha = _read(f).get("screen_sha256")
    return None if sha == RQ.screen_sha_v2() else (sha or "missing")


# The one card frozen, and its arms run, before B and C (main session, 24 Oct): it stays in
# the record, reported apart from the narrowed headline (astra opus-screen r2 #1).
PRE_BC_EXEMPT = ("testem-bail-on-test-failure-deepseek-a1", 1, "codex_local.b1-q10")


def screen_era(run_dir: Path, k: int, cand_id: str) -> str | None:
    """The `screen_era` annotation beside the committed freeze (e.g. "pre-B/C"), if any."""
    p = IR.committed_freeze_path(Path(run_dir) / f"b{k}", cand_id)
    a = p.with_name(p.stem + ".annotations.json")
    return _read(a).get("screen_era") if a.exists() else None


def pre_bc_exempt(run_dir: Path, k: int, cand_id: str) -> bool:
    """True only for PRE_BC_EXEMPT, when its annotation says pre-B/C, carries the committed
    freeze copy's record hash, and is tracked and unmodified in git."""
    if (Path(run_dir).name, int(k), cand_id) != PRE_BC_EXEMPT:
        return False
    fz = IR.committed_freeze_path(Path(run_dir) / f"b{k}", cand_id)
    a = fz.with_name(fz.stem + ".annotations.json")
    if not (fz.exists() and a.exists()):
        return False
    ann = _read(a)
    if ann.get("screen_era") != "pre-B/C" or \
            ann.get("freeze_record_sha256") != _read(fz).get("record_sha256"):
        return False
    rel = str(a.relative_to(ROOT))
    import subprocess
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", rel],
                             capture_output=True, timeout=60, check=False).returncode == 0
    clean = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", rel],
                           capture_output=True, timeout=60, check=False).returncode == 0
    return tracked and clean


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


SOURCE_PLAIN = {"live": "the run's own summary (pi)", "cc_prompt": "a Claude Code-style summary",
                "codex_local": "a Codex-style summary", "opencode": "an OpenCode-style summary",
                "antigravity": "an Antigravity-style summary"}
# One plain line per check code (or the grader) couldn't settle, shown by the question: Yes
# accepts it (the card's 4th answer, c4, exists only for recoverability and reference).
OPEN_LINES = {
    "retention": ("The grader couldn't confirm the summary dropped it; Yes says you checked "
                  "the resumed input and it's missing."),
    "pending": "Code couldn't find the pending line verbatim in the file; Yes accepts the spot shown.",
    "applicability": "Code couldn't confirm the decision is still open; Yes accepts that.",
    "decision": "Code couldn't confirm the act is observable in the window; Yes accepts that.",
    "recoverability": "Code couldn't confirm it isn't findable elsewhere; Yes accepts that.",
    "reference": ("Code couldn't confirm the reference solution stays clear of the act; Yes "
                  "accepts that."),
    "design": ("Code couldn't check the scorer on its own; Yes accepts it as drafted (edit it "
               "under Evidence)."),
}
_WORD = re.compile(r"[A-Za-z0-9_]{3,}")
NEAR_SHARE, NEAR_MIN_WORDS = 0.8, 3  # "nearly verbatim": >= 80% of >= 3 key words
_BULLET = re.compile(r"\s*(?:[-*•]|\d+[.)])?\s*")  # a list marker before the quote
_STOP = {"the", "and", "for", "that", "this", "with", "not", "are", "but", "should", "when",
         "from", "its", "it's", "into", "than", "then", "also", "any", "all", "can", "has", "have",
         "was", "were", "will", "would", "you", "your", "our", "they", "them", "there", "what"}


def _find_ws(text: str, quote: str | None) -> tuple[int, int] | None:
    """(start, end) of the quote in text, whitespace-insensitive (Python offsets)."""
    toks = (quote or "").split()
    if not toks:
        return None
    m = re.search(r"\s+".join(re.escape(t) for t in toks), text or "")
    return (m.start(), m.end()) if m else None


def _clip(x: str, n: int, left: bool = False) -> str:
    x = x.lstrip() if left else x.rstrip()  # keep the space next to the quote
    if len(x) <= n:
        return x
    return ("…" + x[-n:]) if left else (x[:n] + "…")


def said_parts(reply: str | None, quote: str | None, width: int = 160) -> dict:
    """The quote inside its line of the maintainer's reply; when the quote is the whole line,
    the nearest non-empty line before it (else after) as context."""
    out = {"before": "", "quote": quote or "", "after": "", "ctx": None, "ctx_after": False,
           "found": False}
    text = reply or ""
    at = _find_ws(text, quote)
    if at is None:
        return out
    a, b = at
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    le = len(text) if le < 0 else le
    before = "" if _BULLET.fullmatch(text[ls:a]) else text[ls:a]
    out.update(found=True, quote=text[a:b], before=_clip(before, width, left=True),
               after=_clip(text[b:le], width))
    if not out["before"].strip() and not out["after"].strip():
        prev = [x for x in text[:ls].splitlines() if x.strip()]
        nxt = [x for x in text[le:].splitlines() if x.strip()]
        if nxt and (out["quote"].rstrip().endswith(":") or not prev):  # what it introduces
            out.update(ctx=_clip(nxt[0].strip(), 2 * width), ctx_after=True)
        elif prev:
            out["ctx"] = _clip(prev[-1].strip(), 2 * width)
    return out


def closest_passage(text: str, quote: str | None) -> dict:
    """Where the Check link opens the resumed input: the quote verbatim (whitespace-
    insensitive) if it's there, else the line sharing the most of its words (UTF-16 offsets,
    as the history box uses)."""
    exact = _find_ws(text, quote)  # any length (SC2.locate skips quotes under 8 characters)
    if exact:
        a, b = exact
        return {"at": [SC2._u16(text[:a]), SC2._u16(text[:b])], "verbatim": True,
                "shared": None, "of": None}
    words = {w.lower() for w in _WORD.findall(quote or "")} - _STOP
    best, pos = None, 0
    for line in text.splitlines(keepends=True):
        n = len(words & {w.lower() for w in _WORD.findall(line)})
        if n and (best is None or n > best[0]):
            best = (n, pos, pos + len(line.rstrip("\r\n")))
        pos += len(line)
    if best is None:
        return {"at": None, "verbatim": False, "shared": 0, "of": len(words)}
    n, a, b = best
    return {"at": [SC2._u16(text[:a]), SC2._u16(text[:b])], "verbatim": False, "shared": n,
            "of": len(words)}


def plain_act(desc: str | None) -> str:
    """The act as the end of "We'd test whether the agent: …"."""
    x = (desc or "").strip()
    x = re.sub(r"^the agent\s+", "", x, flags=re.IGNORECASE)
    if re.match(r"^[A-Z][a-z]+\b", x):
        x = x[0].lower() + x[1:]
    return x or "(no act drafted)"


def facts_v2(c: Mapping, k: int, task_id: str | None, reply: Mapping, src: str, bdir: Path,
             post: str) -> tuple[dict, list[str]]:
    """(the card's facts block, its open lines): what the card shows above the question."""
    checks = c.get("checks") or {}
    pend_x = ((checks.get("applicability") or {}).get("evidence") or {}).get("pending") or {}
    pend = pend_x.get("evidence") or {}
    ret = (checks.get("retention") or {}).get("outcome")
    loss = (c.get("loss") or {}).get("status")
    chips = []
    if src != "live":
        chips.append("mixed setup: a Haiku-written replay")
    if IR.replay_prompt_changed(bdir, src):
        chips.append("prompt changed")
    if loss == "inherited":
        chips.append("lost at an earlier compaction")
    if ret == "unresolved":
        chips.append("loss unconfirmed")
    if c.get("disposition") == "bundle":
        chips.append("second loss from the same summary")
    near = closest_passage(post, c.get("repair_text"))
    # never an absence claim (astra cards-v2 r1): the grader's call, and how close the nearest
    # passage is; Jacob decides whether it's really gone
    close = (f"shares {near['shared']} of its {near['of']} key words" if near["at"] else
             "no passage shares its key words")
    sees = ("its words appear verbatim: check before calling it lost" if near["verbatim"] else
            f"The grader wasn't sure: check the closest passage ({close})" if ret == "unresolved"
            else f"The grader marked it lost: check the closest passage ({close})")
    if near["verbatim"]:
        chips.append("verbatim in the resumed input")
    elif near["of"] >= NEAR_MIN_WORDS and near["shared"] >= NEAR_SHARE * near["of"]:
        chips.append("nearly verbatim")
    facts = {
        "header": (f"{task_id} · compaction {k} · graded as dropped by "
                   f"{SOURCE_PLAIN.get(src, src)}"),
        "chips": chips,
        "said": said_parts(reply.get("reply"), c.get("repair_text")),
        "code": {"path": pend.get("path"), "quote": pend.get("quote")},
        "sees": sees,
        "check_title": ("the quote, verbatim" if near["verbatim"] else
                        f"the closest passage ({close})" if near["at"] else
                        "the top: no passage shares its key words, search the box"),
        "act": plain_act((c.get("act_spec") or {}).get("description")),
    }
    open_lines = [OPEN_LINES[n] for n in ("retention",) if ret == "unresolved"]
    if pend_x.get("outcome") == "unresolved":
        open_lines.append(OPEN_LINES["pending"])
    elif (checks.get("applicability") or {}).get("outcome") == "unresolved":
        open_lines.append(OPEN_LINES["applicability"])
    open_lines += [OPEN_LINES[n] for n in ("decision", "recoverability", "reference", "design")
                   if (checks.get(n) or {}).get("outcome") == "unresolved"]
    return {**facts, "near_at": near["at"]}, open_lines


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
    facts, card["open_lines"] = facts_v2(c, k, task.get("task_id"), reply, src, bdir, post)
    card["post_at"] = facts.pop("near_at")
    card["facts"] = facts
    card["post_label"] = ("Resumed input: everything the agent sees after the compaction"
                          + (", scrolled to the closest passage" if card["post_at"] else ""))
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
        if (old := screen_stale(run_dir, k)) and card_ids_v2(run_dir, k):
            raise StaleScreen(f"{run_dir.name} b{k}: screened under {old[:12]}, the rules are "
                              f"now {RQ.screen_sha_v2()[:12]}: rescreen it (reselect)")
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
            if not RQ.opus_passed(c):  # astra opus-screen r1 #1
                raise StaleScreen(f"{run_dir.name} b{k}: {cid_} has no passing Opus verdict")
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
                   runner: S.Runner | None = None, override: Mapping | None = None) -> dict:
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
    if cand_id in frozen:  # the exemption only ever labels this existing freeze (r3)
        return rec | {"status": "already_frozen", "reasons": [],
                      **({"screen_era": "pre-B/C"} if pre_bc_exempt(run_dir, k, cand_id)
                         else {})}
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
    # design fields the main session filled in where the screen left them empty (never over
    # Jacob's own edits); recorded on the decision and the freeze, checked like an edit
    overridden = [f for f in (override or {}).get("fields") or {}
                  if f in DESIGN_FIELDS and f not in edited]
    for f in overridden:
        design[f] = override["fields"][f]
    if overridden:
        rec["design_override"] = {"fields": overridden, "by": override.get("by"),
                                  "utc": override.get("utc"),
                                  "speaker": override.get("speaker"),
                                  **{k: override[k] for k in ("fallback", "chosen")
                                     if override.get(k)}}
    changed = edited + overridden
    gaps = design_gaps(design)
    if gaps:
        held.append("design incomplete: " + "; ".join(gaps))
    c = parts["cand"]
    # a new freeze is never exempt (astra opus-screen r3): pre-B/C only labels the existing
    # committed testem freeze, reported as already_frozen above
    era = screen_era(run_dir, k, cand_id)
    if era:
        held.append(f"refused: screen_era {era!r} applies only to the existing committed "
                    f"freeze of {PRE_BC_EXEMPT}, never to a new one")
    if not RQ.opus_passed(c):
        held.append("no passing Opus verdict (B and C)")
    stale = screen_stale(run_dir, k)
    if stale:
        held.append(f"rescreen required: screened under {stale[:12]}, the rules are now "
                    f"{RQ.screen_sha_v2()[:12]}")
    spec = spec_of(design) if set(changed) & set(SPEC_FIELDS) else c["act_spec"]
    checks = json.loads(json.dumps(c.get("checks") or {}))
    bdir = run_dir / f"b{k}"
    if changed and not held:
        new = RQ.check_design({"quote": design["d_repair"]},
                              {"matched_quote": design["d_matched"],
                               "matched_rationale": design["d_rationale"]},
                              spec, S.source_view(bdir, c["source_policy"]),
                              _read(bdir / "boundary" / "pre_messages.json"),
                              maintainer=RQ.maintainer_context(bdir))
        shown = set(((checks.get("design") or {}).get("evidence") or {}).get("problems") or [])
        if overridden:
            # an override passes every current check; the one exception is a recorded
            # non-maintainer fallback (astra matched-override r1 #2)
            shown = set(FALLBACK_OK) if (override or {}).get("fallback") and \
                "d_matched" in overridden else set()
        added = [p for p in new["evidence"]["problems"] if p not in shown]
        if added:
            held.append(f"{'design' if overridden else 'edited design'} fails the "
                        f"current checks: {added}")
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
    if {"d_matched", "d_rationale"} & set(overridden):
        checks["design"]["jacob_via"] = "card; matched block filled by override after labelling"
    frz = {"cand_id": cand_id, "source_policy": c["source_policy"], "req_id": c["req_id"],
           "repair_text": design["d_repair"], "matched_text": design["d_matched"],
           "matched_rationale": design["d_rationale"], "act_spec": spec, "checks": checks,
           "screen": RQ.SCREEN_V2, "loss": c.get("loss"),
           "role": role,
           "card": {"id": cid, "v": label.get("v"), "edited": edited,
                    "note": notes.get("card"), "labelled_utc": label.get("utc")},
           **({"design_override": rec["design_override"]} if overridden else {})}
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
              runner: S.Runner | None = None,
              overrides: Mapping[str, dict] | None = None) -> list[dict]:
    """Decisions for a v2 page's cards, then the bundle record of each boundary where two or
    more candidates of the carded source are frozen."""
    unknown = sorted(set(labels) - set(key["cards"]) - set(key.get("retired") or {}))
    if unknown:
        raise ValueError(f"labels for cards on no page this key knows: {unknown[:5]}")
    out = [freeze_card_v2(cid, key["cards"][cid], labels.get(cid) or {}, seed=key["seed"],
                          runner=runner, override=(overrides or {}).get(cid))
           for cid in key["order"]]
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


MATCHED_PROMPT = """You choose the matched control block for a compaction experiment.

The experiment: an agent's context was compacted, and the summary dropped a requirement the \
maintainer gave. One arm puts the requirement's words back after the summary (the repair \
block); the other arm puts a matched control block in the same place with the same lead line. \
The control must be the same kind of text as the repair: an exact quote from one of the \
maintainer's replies in <maintainer_replies> (all of them are in the agent's history before \
compaction) that the resumed input also lacks, about the same length as the repair quote \
(within 20%), and with no bearing on the remaining work or on the act below: neutral material \
(a pleasantry, an incidental remark, a comment on finished or unrelated work), never a \
requirement, rule, preference or instruction that could change what the agent does next.

Return up to 5 such maintainer candidates, best first ("source": "maintainer"). Only if fewer \
than 2 neutral maintainer spans exist, add up to 3 fallbacks from elsewhere in <history> \
("source": "history"). Copy each character for character (one contiguous span, no ellipses, no \
added quotes), with a one-sentence rationale saying why it has no bearing on the act."""

MATCHED_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"quote": {"type": "string"}, "rationale": {"type": "string"},
                       "source": {"type": "string", "enum": ["maintainer", "history"]}},
        "required": ["quote", "rationale", "source"]}}}}
OPUS_MODEL = "claude-opus-5-5"


def matched_request(run_dir: Path, k: int, cand_id: str) -> tuple[Any, dict]:
    """(the Opus request, context) for a candidate whose screen drafted no matched block."""
    from compaction import s1_types as T
    bdir = Path(run_dir) / f"b{k}"
    c = candidate_v2(run_dir, k, cand_id)
    pre = _read(bdir / "boundary" / "pre_messages.json")
    view = S.source_view(bdir, c["source_policy"])
    q = c["repair_text"]
    tol = RQ.MATCHED_TOLERANCE
    lo, hi = round(len(q) * (1 - tol)), round(len(q) * (1 + tol))
    spec = c.get("act_spec") or {}
    mc = RQ.maintainer_context(bdir)
    replies = "\n".join(f"<reply>{r}</reply>" for r in mc["replies"])
    user = (f"<repair_quote length=\"{len(q)}\">{q}</repair_quote>\n"
            f"<allowed_length>{lo}-{hi} characters</allowed_length>\n"
            f"<act>{spec.get('description')}</act>\n"
            f"<avoid_words>{', '.join(spec.get('reask_keywords') or [])}</avoid_words>\n"
            f"<maintainer_replies>\n{replies}\n</maintainer_replies>\n"
            f"<resumed_input>\n{render_post(view)}\n</resumed_input>\n"
            f"<history>\n{render_history(pre)}\n</history>")
    rid = f"interact:{Path(run_dir).name}:b{k}"
    req = T.Req(key=f"matched|{rid}|{cand_id}", system=MATCHED_PROMPT, user=user,
                schema=MATCHED_SCHEMA, max_output_tokens=4000, rid=rid, source="deepswe",
                private=True)
    return req, {"cand": c, "pre": pre, "view": view, "maintainer": mc}


FALLBACK_OK = {"matched_not_maintainer"}  # the one problem a recorded fallback may carry


def pick_matched(cands: list[dict], ctx: Mapping) -> tuple[dict | None, list[dict]]:
    """(the chosen candidate, every candidate with its problems). The first passing every check
    (a maintainer quote, `RQ.check_design` with the maintainer context, no act keyword); only
    when none does, the first whose one problem is not being a maintainer quote, returned with
    `fallback` set to the reason."""
    c = ctx["cand"]
    spec = c.get("act_spec") or {}
    avoid = [w.lower() for w in spec.get("reask_keywords") or [] if w.strip()]
    tried = []
    for x in cands:
        d = RQ.check_design({"quote": c["repair_text"]},
                            {"matched_quote": x.get("quote"),
                             "matched_rationale": x.get("rationale")},
                            spec, ctx["view"], ctx["pre"], maintainer=ctx.get("maintainer"))
        probs = list(d["evidence"]["problems"])
        probs += [f"names_act_keyword:{w}" for w in avoid if w in (x.get("quote") or "").lower()]
        tried.append({**x, "problems": probs})
    best = next((x for x in tried if not x["problems"]), None)
    if best is None:
        fb = next((x for x in tried if x["problems"] and set(x["problems"]) <= FALLBACK_OK), None)
        if fb is not None:
            best = dict(fb, fallback=("no maintainer quote passed the checks: " + "; ".join(
                f"{(x.get('quote') or '')[:40]!r}: {','.join(x['problems'])}"
                for x in tried if x.get("source") == "maintainer") or "none proposed"))
    return best, tried


def _choose_matched(a, key: Mapping, ent: Mapping) -> int:
    """No new call. `--choose I --why`: take the card's I-th drafted candidate (0-based) instead
    of the first passing one. `--quote Q --rationale R --why`: a non-maintainer fallback, taken
    only when every drafted maintainer candidate fails the current checks. Either must pass
    every current check (a fallback may fail only `matched_not_maintainer`); the choice, the
    re-checked candidates and the reason are recorded on the override."""
    if not (a.why or "").strip():
        raise SystemExit("--choose/--quote need --why")
    p = Path(a.key).parent / OVERRIDES_NAME
    old = _read(p)
    o = old["cards"][a.card]
    run_dir = (ROOT / ent["run_dir"]) if not Path(ent["run_dir"]).is_absolute() else \
        Path(ent["run_dir"])
    _, ctx = matched_request(run_dir, int(ent["k"]), ent["cand_id"])
    drafted = [{k: x[k] for k in ("quote", "rationale", "source") if k in x}
               for x in o.get("tried") or []]
    _, rechecked = pick_matched(drafted, ctx)
    if a.quote is not None:
        if not (a.rationale or "").strip():
            raise SystemExit("--quote needs --rationale")
        ok = [x["quote"] for x in rechecked
              if x.get("source") == "maintainer" and not x["problems"]]
        if ok:
            raise SystemExit(f"a drafted maintainer quote passes the checks: {ok[0]!r}")
        x = {"quote": a.quote, "rationale": a.rationale.strip(), "source": "history"}
        _, t = pick_matched([x], ctx)
        if set(t[0]["problems"]) - FALLBACK_OK:
            raise SystemExit(f"the fallback fails the checks: {t[0]['problems']}")
        o.update(fallback=a.why.strip(), speaker="history")
        chosen = {"quote": a.quote, "why": a.why.strip(), "utc": _utc()}
    else:
        x = drafted[a.choose]
        if rechecked[a.choose]["problems"]:
            raise SystemExit(f"candidate {a.choose} fails the current checks: "
                             f"{rechecked[a.choose]['problems']}")
        o.pop("fallback", None)
        o["speaker"] = x.get("source") or "maintainer"
        chosen = {"index": a.choose, "why": a.why.strip(), "utc": _utc()}
    o.update(fields={"d_matched": x["quote"], "d_rationale": x["rationale"]}, chosen=chosen,
             rechecked=[{"quote": r["quote"], "source": r.get("source"),
                         "problems": r["problems"]} for r in rechecked])
    S1P._atomic(p, (json.dumps(old, indent=1, sort_keys=True, ensure_ascii=False)
                    + "\n").encode())
    print(json.dumps({"override": str(p), "fields": o["fields"], "chosen": chosen,
                      "speaker": o["speaker"], "rechecked": o["rechecked"]},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_draft_matched(a) -> int:
    """Opus 5.5 on Max drafts a matched block for a card the screen left without one; the
    first candidate passing the code checks goes to design_overrides.json (by: main session)."""
    from compaction.claude_headless import MaxBackend
    key = _read(a.key)
    ent = key["cards"][a.card]
    if a.choose is not None or a.quote is not None:
        return _choose_matched(a, key, ent)
    run_dir = (ROOT / ent["run_dir"]) if not Path(ent["run_dir"]).is_absolute() else \
        Path(ent["run_dir"])
    req, ctx = matched_request(run_dir, int(ent["k"]), ent["cand_id"])
    be = MaxBackend(model=OPUS_MODEL, name="opus-max", effort="medium", concurrency=1,
                    recorder=IR.PilotSpend().recorder())
    resp = be.run([req], purpose="s2-interact-matched")[req.key]
    if resp.error or not resp.data:
        raise SystemExit(f"Opus draft failed: {resp.error}")
    best, tried = pick_matched(resp.data["candidates"], ctx)
    print(json.dumps({"tried": tried}, indent=1, ensure_ascii=False))
    if best is None:
        print(json.dumps({"override": None, "why": "no candidate passed the checks"}))
        return 3
    p = Path(a.key).parent / OVERRIDES_NAME
    old = _read(p) if p.exists() else {"cards": {}}
    old["cards"][a.card] = {
        "fields": {"d_matched": best["quote"], "d_rationale": best["rationale"]},
        "by": "main session (Jacob approved 24 Oct): Opus 5.5 draft on Max, code-checked",
        "model": OPUS_MODEL, "utc": _utc(), "cand_id": ent["cand_id"], "tried": tried,
        "speaker": "maintainer" if not best.get("fallback") else "history",
        **({"fallback": best["fallback"]} if best.get("fallback") else {})}
    S1P._atomic(p, (json.dumps(old, indent=1, sort_keys=True, ensure_ascii=False)
                    + "\n").encode())
    print(json.dumps({"override": str(p), "fields": old["cards"][a.card]["fields"]},
                     ensure_ascii=False))
    return 0


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
                              file_runner=RQ.docker_file_runner(),
                              opus_factory=IR.opus_backend)
        out.append({"run_dir": rdir, "k": k, "state": sel.get("state"),
                    "cards": sel.get("cards"), "rejected": sel.get("rejected")})
    return out


def cmd_freeze2(a) -> int:
    key = _read(a.key)
    ov = Path(a.key).parent / OVERRIDES_NAME
    results = freeze_v2(read_labels(Path(a.labels)), key,
                        overrides=(_read(ov).get("cards") or {}) if ov.exists() else None)
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
    dm = sub.add_parser("draft-matched", help="v2: Opus drafts a missing matched block")
    dm.add_argument("--card", required=True)
    dm.add_argument("--key", default=str(CARDS_DIR / "v2" / KEY_NAME))
    dm.add_argument("--choose", type=int, help="take this drafted candidate (0-based), no call")
    dm.add_argument("--quote", help="a non-maintainer fallback quote (no call)")
    dm.add_argument("--rationale", help="why the --quote has no bearing on the act")
    dm.add_argument("--why", help="the reason for --choose/--quote (recorded)")
    f2 = sub.add_parser("freeze2", help="v2: freeze from the 3-question labels")
    f2.add_argument("--labels", required=True)
    f2.add_argument("--key", default=str(CARDS_DIR / "v2" / KEY_NAME))
    f2.add_argument("--no-reselect", action="store_true",
                    help="record decisions only; don't rescreen rejected boundaries")
    a = ap.parse_args(argv)
    return {"build": cmd_build, "freeze": cmd_freeze, "build2": cmd_build2,
            "freeze2": cmd_freeze2, "draft-matched": cmd_draft_matched}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
