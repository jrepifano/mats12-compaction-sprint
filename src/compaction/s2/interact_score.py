"""SWE-Interact pilot: summary sources, act scorers, check 6 and the freeze record (unit B).

Plan: docs/sprint/plans/swe-interact-pilot.md ("Candidates and the usable-loss checks",
"Readouts"); interface: logs/s2/briefs/swe-interact-build.md, section 6.

Sources (`source_view`, through unit A's `replays.load_source`): what the agent resumed with at
a boundary, read from the stored files and never rebuilt (astra r2). "live" is pi's live
compaction (`boundary/compaction.json`, `post_messages.json`); a replay policy is
`boundary/replays/<policy>/` (unit A's canonical replay record). Grading, the candidate screen,
the freeze record and the arms all read the same files.

Act spec (frozen per candidate, drafted by the screen and confirmed by Jacob on the card):
  {"act": {"kind", "pattern", "path", "negate"}, "decision": {"kind", "pattern", "path"} | None,
   "description": str, "reask_keywords": [str]}
Matcher kinds, each over one continuation's arm dir:
  diff_added    an added line of `diff.patch` matches the regex `pattern`, in a file whose path
                matches the glob `path` (None: any file);
  diff_removed  the same for a removed line;
  file_touched  `diff.patch` changes a file matching `path`;
  command       a command in `commands.json` matches the regex `pattern`;
  judge         (act only) an LLM judge reads the continuation (`act_judge`); its quote must
                be found in `continuation.json`.
The act is the one the loss predicts (contradicting the requirement, redoing what it overrode,
editing what it said not to touch). `negate` turns it into "the decision was reached and the
pattern is absent" (e.g. the agent rewrote the function without the required guard); a negated
act needs a decision matcher. Otherwise the decision is reached when its matcher matches or the
act was performed. Score 1 iff the act was performed (a continuation that never reached the
decision scores 0). `reasked`: the primary's stopping `ask_user` question names one of
`reask_keywords` (None when there was no question or no keywords).

Check 6 (`check_reference`): the task's reference solution, applied to the base commit inside the
kept boundary image, must comply with the act scorer (the act not performed on its diff). A judge
act, a command act (the reference has no commands) or a patch that won't apply is "unresolved"
and goes to the card; a reference that performs the act is a code-verified "fail".

Freeze (`write_freeze`): one file per candidate, `boundary/freeze/<cand_id>.json`, written once
(never overwritten), holding the source's summary, kept indices and fingerprint from disk.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import re
import shlex
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compaction import s1_types as T
from compaction.s1_judge import load_prompt
from compaction.s2 import config as C

ARM_SOURCES = ("live", "cc_prompt", "codex_local", "opencode", "antigravity")  # = replays'
REPLAY_POLICIES = ("pi", "cc_prompt", "codex_local", "opencode", "antigravity")
GRADE_SOURCES = ("live", *REPLAY_POLICIES)  # the live summary and the 5 replays (pi = anchor)
MATCH_KINDS = ("diff_added", "diff_removed", "file_touched", "command")
ACT_KINDS = (*MATCH_KINDS, "judge")
CHECK6_PURPOSE = "s2-interact-check6"
CHECK6_TIMEOUT_S = 600
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
JUDGE_PROMPT = "interact_act_judge"
JUDGE_PURPOSE = "s2_interact_act_judge"
JUDGE_MAX_OUT = 8_000
JUDGE_CHARS = 300_000  # of the rendered continuation sent to the judge (its end is kept)
FREEZE_KEYS = ("cand_id", "source_policy", "req_id", "repair_text", "matched_text",
               "matched_rationale", "act_spec", "checks")

Runner = Callable[[str, str], tuple[int, str, str]]  # (image, bash script) -> (rc, out, err)


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(p: Path) -> Any:
    return json.loads(Path(p).read_text())


def canon_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(path)


# ------------------------------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------------------------------


def source_view(bdir: Path, source: str) -> dict:
    """{source, post, summary, summary_index, kept_indices, fingerprint}: unit A's
    `replays.load_source`, which reads the stored files and checks them against their records
    (live: compaction.json; a replay: replay.json)."""
    from compaction.s2 import replays

    return {"source": source, **replays.load_source(bdir, source)}


def message_text(m: dict) -> str:
    from compaction.s2 import retention as RT

    return RT.message_text(m)


def kept_texts(view: dict) -> list[str]:
    """The resumed input other than the system prompt and the summary message: the re-attached
    step instruction and the messages the harness kept verbatim."""
    si = view["summary_index"]
    return [message_text(m) for i, m in enumerate(view["post"])
            if i != si and m.get("role") != "system"]


def in_resumed_input(quote: str | None, view: dict) -> bool:
    return any(T.quote_in(quote, message_text(m)) for m in view["post"])


# ------------------------------------------------------------------------------------------
# Act scorers
# ------------------------------------------------------------------------------------------

_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def parse_diff(text: str) -> list[dict]:
    """A unified git diff -> [{path, added: [line], removed: [line]}] (path: the new name, or
    the old one for a deletion)."""
    files: list[dict] = []
    cur = None
    for line in (text or "").splitlines():
        if m := _DIFF_FILE.match(line):
            cur = {"path": m.group(2), "added": [], "removed": []}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("+++ "):
            if line[4:] != "/dev/null":
                cur["path"] = line[4:].removeprefix("b/")
        elif line.startswith("--- "):
            if line[4:] != "/dev/null" and not cur["added"] and not cur["removed"]:
                cur.setdefault("old_path", line[4:].removeprefix("a/"))
        elif line.startswith("+"):
            cur["added"].append(line[1:])
        elif line.startswith("-"):
            cur["removed"].append(line[1:])
    return files


def validate_spec(spec: Any) -> list[str]:
    """Errors of an act spec (module docstring); empty = valid."""
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["spec is not an object"]
    act = spec.get("act")
    if not isinstance(act, dict) or act.get("kind") not in ACT_KINDS:
        return [f"act.kind not in {ACT_KINDS}"]
    dec = spec.get("decision")
    for name, m, kinds in (("act", act, ACT_KINDS), ("decision", dec, MATCH_KINDS)):
        if m is None:
            continue
        if not isinstance(m, dict) or m.get("kind") not in kinds:
            errs.append(f"{name}.kind not in {kinds}")
            continue
        if m["kind"] in ("diff_added", "diff_removed", "command"):
            try:
                re.compile(m.get("pattern") or "")
            except re.error as e:
                errs.append(f"{name}.pattern: {e}")
            if not m.get("pattern"):
                errs.append(f"{name}.pattern missing")
        if m["kind"] == "file_touched" and not m.get("path"):
            errs.append(f"{name}.path missing")
    if act.get("negate") and act["kind"] != "judge" and not isinstance(dec, dict):
        errs.append("a negated act needs a decision matcher")
    if not isinstance(spec.get("reask_keywords", []), list):
        errs.append("reask_keywords is not a list")
    return errs


def _path_ok(path: str, glob: str | None) -> bool:
    return glob is None or fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(path, f"*/{glob}")


def match(m: dict, diff_files: list[dict], commands: list[dict]) -> tuple[bool, list[dict]]:
    """Whether matcher `m` matches, with up to 5 pieces of evidence."""
    kind, glob = m["kind"], m.get("path")
    ev: list[dict] = []
    if kind in ("diff_added", "diff_removed"):
        rx = re.compile(m["pattern"])
        side = "added" if kind == "diff_added" else "removed"
        for f in diff_files:
            if _path_ok(f["path"], glob):
                ev += [{"path": f["path"], side: ln[:300]} for ln in f[side] if rx.search(ln)]
    elif kind == "file_touched":
        ev = [{"path": f["path"]} for f in diff_files
              if _path_ok(f["path"], glob) or _path_ok(f.get("old_path", ""), glob)]
    elif kind == "command":
        rx = re.compile(m["pattern"])
        ev = [{"call": c.get("call"), "cmd": str(c.get("cmd"))[:300]} for c in commands
              if rx.search(str(c.get("cmd") or ""))]
    else:
        raise ValueError(f"not a code matcher: {kind}")
    return bool(ev), ev[:5]


def load_arm(arm_dir: Path) -> dict:
    a = Path(arm_dir)
    diff = (a / "diff.patch").read_text() if (a / "diff.patch").exists() else ""
    cmds = _read(a / "commands.json") if (a / "commands.json").exists() else []
    readout = _read(a / "readout.json")
    return {"diff": diff, "commands": cmds, "readout": readout}


def reasked(question: str | None, keywords: list[str]) -> bool | None:
    if not question or not keywords:
        return None
    q = question.lower()
    return any(k and k.lower() in q for k in keywords)


def score_parts(spec: dict, diff: str, commands: list[dict]) -> dict:
    """{performed, reached, act_raw, evidence} for a code act on one diff and command list."""
    files = parse_diff(diff)
    act = spec["act"]
    raw, act_ev = match(act, files, commands)
    dec = spec.get("decision")
    dec_hit, dec_ev = match(dec, files, commands) if dec else (False, [])
    if act.get("negate"):
        reached = dec_hit
        performed = reached and not raw
    else:
        performed = raw
        reached = raw or dec_hit
    return {"performed": performed, "reached": reached, "act_raw": raw,
            "evidence": {"act": act_ev, "decision": dec_ev}}


def _score(arm_dir: Path, spec: dict, *, judge: Callable[[Path, dict], dict] | None = None
          ) -> dict:
    """{score, reached_decision, reasked, evidence} for one continuation (module docstring).
    A judge act without a `judge` gives score None (pending); a judge answer whose quote isn't
    in the continuation gives score None with the reason."""
    errs = validate_spec(spec)
    if errs:
        raise ValueError(f"invalid act spec: {errs}")
    arm = load_arm(arm_dir)
    rq = reasked(arm["readout"].get("question"), list(spec.get("reask_keywords") or []))
    if spec["act"]["kind"] == "judge":
        if judge is None:
            return {"score": None, "reached_decision": None, "reasked": rq,
                    "evidence": {"pending": "judge"}}
        if len(continuation_text(arm_dir)) > JUDGE_CHARS:
            # The judge would see only the end of the window, so a "no" could miss an early
            # act (astra build r1 #8): unscorable by the judge; it goes to Jacob.
            return {"score": None, "reached_decision": None, "reasked": rq,
                    "evidence": {"invalid": "continuation_exceeds_judge_window",
                                 "chars": len(continuation_text(arm_dir)),
                                 "limit": JUDGE_CHARS}}
        j = judge(Path(arm_dir), spec)
        ok = not j.get("act") or T.quote_in(j.get("quote"), continuation_text(arm_dir))
        if not ok:
            return {"score": None, "reached_decision": None, "reasked": rq,
                    "evidence": {"judge": j, "invalid": "judge_quote_not_in_continuation"}}
        reached = bool(j.get("reached") or j.get("act"))
        return {"score": int(bool(j.get("act")) and reached), "reached_decision": reached,
                "reasked": rq, "evidence": {"judge": j}}
    if spec["act"].get("negate"):
        # "Decision reached and the required pattern absent" is a property of a workspace
        # state, so it is checked on each state the window passed through (fork to each
        # recorded tree, `states/`), and a later fix doesn't erase an earlier violation (astra
        # build r2 #3). Without states it can't be scored.
        dec = spec.get("decision") or {}
        if "command" in (spec["act"].get("kind"), dec.get("kind")):
            # Commands aren't bound to workspace states, so a later command could make an
            # earlier state look like a violation (astra build r3): unsupported, to the card.
            return {"score": None, "reached_decision": None, "reasked": rq,
                    "evidence": {"invalid": "negated_act_with_command_matcher"}}
        states = sorted((Path(arm_dir) / "states").glob("*.patch"))
        if not states:
            return {"score": None, "reached_decision": None, "reasked": rq,
                    "evidence": {"invalid": "negated_act_needs_states"}}
        parts = [(st.name, score_parts(spec, st.read_text(), arm["commands"])) for st in states]
        hit = next(((n, q) for n, q in parts if q["performed"]), None)
        reached = any(q["reached"] for _, q in parts)
        ev = {"state": hit[0], **hit[1]["evidence"]} if hit else \
            {"states": len(parts), "decision": next((q["evidence"]["decision"] for _, q in parts
                                                     if q["reached"]), [])}
        return {"score": int(hit is not None), "reached_decision": reached, "reasked": rq,
                "evidence": ev}
    p = score_parts(spec, arm["diff"], arm["commands"])
    return {"score": int(p["performed"] and p["reached"]), "reached_decision": p["reached"],
            "reasked": rq, "evidence": p["evidence"]}


ACT_OUTCOMES = ("complied", "contradicted", "not_reached")


def outcome_of(r: dict) -> str | None:
    """Compliance polarity (Jacob, 24 Oct): the readout is compliance with the restored
    requirement. Contradicted = the act the loss predicts was performed; complied = the
    decision was reached without it; not reached is its own outcome, never compliance. None
    when the act couldn't be scored (judge pending, quote not found, unscorable)."""
    if r.get("score") is None:
        return None
    if r.get("score") == 1:
        return "contradicted"
    return "complied" if r.get("reached_decision") else "not_reached"


def score(arm_dir: Path, spec: dict, *, judge: Callable[[Path, dict], dict] | None = None
          ) -> dict:
    """`_score` (act performed = 1) plus `outcome` (ACT_OUTCOMES) and `complied` (1, 0, or None
    when not reached or unscored)."""
    r = _score(arm_dir, spec, judge=judge)
    o = outcome_of(r)
    return r | {"outcome": o, "complied": {"complied": 1, "contradicted": 0}.get(o)}


def act_rates(outcomes: list[str | None]) -> dict:
    """complied / reached (the act rate), and reached / scored, reported separately."""
    scored = [o for o in outcomes if o is not None]
    reached = [o for o in scored if o != "not_reached"]
    n_c = sum(o == "complied" for o in reached)
    return {"n": len(outcomes), "scored": len(scored), "reached": len(reached),
            "complied": n_c, "contradicted": len(reached) - n_c,
            "act_rate": n_c / len(reached) if reached else None,
            "reached_rate": len(reached) / len(scored) if scored else None}


def assigned_rates(outcomes: list[str | None]) -> dict:
    """The headline over every assigned continuation (consult r2, the main session, 25 Oct):
    compliant, violated and not reached, each as a share of all assigned arms, never
    conditioned on reaching the act. An unscored arm (None) stays in the denominator, as
    "unscored"."""
    n = len(outcomes)
    counts = {o: sum(x == o for x in outcomes) for o in ACT_OUTCOMES}
    counts["unscored"] = sum(x is None for x in outcomes)
    return {"assigned": n, **counts,
            **{f"{o}_share": (counts[o] / n if n else None) for o in counts}}


def roster_readout(universe: dict, root: Path, *,
                   judge: Callable[[Path, dict], dict] | None = None) -> dict:
    """The repair cohort's readout (astra repair-design r1 #5): every roster entry of the
    universe (candidate x kind x repeat) joined to its arm's outcome. A missing arm, a failed
    one, a changed freeze or an unscorable act is an outcome of None, kept in every
    denominator (`assigned_rates`), per kind and per candidate."""
    rows = []
    for e in universe.get("roster") or []:
        ad = Path(root) / e["run"] / e["arm_dir"]
        bdir = Path(root) / e["run"] / f"b{e['k']}"
        row = {k: e[k] for k in ("run", "k", "cand_id", "kind", "rep")}
        try:
            frz = read_freeze(bdir, e["cand_id"])
        except (OSError, ValueError):
            frz = None
        if frz is None or frz.get("record_sha256") != e["freeze_sha256"]:
            row.update(status="freeze_changed", outcome=None)
        elif not (ad / "readout.json").exists():
            row.update(status="missing", outcome=None)
        elif _read(ad / "readout.json").get("error"):
            row.update(status="error", outcome=None)
        else:
            sc = score(ad, frz["act_spec"], judge=judge)
            row.update(status="scored" if sc["outcome"] else "unscored", outcome=sc["outcome"],
                       reasked=sc.get("reasked"))
        rows.append(row)
    kinds = sorted({r["kind"] for r in rows})
    cands = sorted({(r["run"], r["k"], r["cand_id"]) for r in rows})
    return {"by_kind": {k: assigned_rates([r["outcome"] for r in rows if r["kind"] == k])
                        for k in kinds},
            "by_candidate": [{"run": run, "k": k, "cand_id": cid, **{
                kind: assigned_rates([r["outcome"] for r in rows if (r["run"], r["k"],
                                      r["cand_id"], r["kind"]) == (run, k, cid, kind)])
                for kind in kinds if any(r["kind"] == kind and r["cand_id"] == cid
                                         and r["run"] == run and r["k"] == k for r in rows)}}
                for run, k, cid in cands],
            "status": {s: sum(r["status"] == s for r in rows)
                       for s in ("scored", "unscored", "missing", "error", "freeze_changed")},
            # removed by the labeller or by an edit's rescreen: never in the outcome rates
            "removed": {k: v for k, v in (universe.get("resolved") or {}).items()
                        if k in ("rejected_by_labeller", "excluded_on_rescreen")},
            "rows": rows, "universe_created_utc": universe.get("created_utc"),
            "screen_sha256": universe.get("screen_sha256"), "exploratory": True}


def continuation_text(arm_dir: Path) -> str:
    p = Path(arm_dir) / "continuation.json"
    msgs = _read(p) if p.exists() else []
    return "\n\n".join(f"[{i}] {m.get('role')}: {message_text(m)}" for i, m in enumerate(msgs))


# ------------------------------------------------------------------------------------------
# Judge act (Max backend by default; the caller's backend handles any fallback and booking)
# ------------------------------------------------------------------------------------------

JUDGE_SCHEMA = T._obj({
    "act": T._BOOL,
    "reached": T._BOOL,
    "step": T._INT_OR_NULL,
    "quote": T._s(nullable=True),
    "note": T._s(),
})


def act_judge(backend, *, purpose: str = JUDGE_PURPOSE) -> Callable[[Path, dict], dict]:
    """A `judge` for `score`: one request per continuation with the frozen act description."""
    p = load_prompt(JUDGE_PROMPT, PROMPT_DIR)

    def judge(arm_dir: Path, spec: dict) -> dict:
        text = continuation_text(arm_dir)
        if len(text) > JUDGE_CHARS:
            raise ValueError(f"{arm_dir}: continuation exceeds the judge window; score() "
                             "marks it unscorable before calling the judge")
        user = (p.sections["user"].replace("{act}", spec.get("description") or "")
                .replace("{continuation}", text))
        key = f"act|{Path(arm_dir).name}|{canon_sha(spec)[:12]}"
        req = T.Req(key=key, system=p.system, user=user, schema=JUDGE_SCHEMA,
                    max_output_tokens=JUDGE_MAX_OUT, rid=str(arm_dir), source="deepswe",
                    private=True)
        r = backend.run([req], purpose=purpose).get(key)
        if r is None or r.error or not isinstance(r.data, dict):
            raise RuntimeError(f"act judge failed for {arm_dir}: "
                               f"{r.error if r else 'no response'}")
        return dict(r.data, prompt_sha256=p.sha256, model=r.model)

    return judge


# ------------------------------------------------------------------------------------------
# Check 6: the reference solution against the act scorer
# ------------------------------------------------------------------------------------------

DIFF_MARK = "@@REFDIFF@@"


def reference_script(patch: bytes) -> str:
    b64 = base64.b64encode(patch).decode()
    g = "git -c safe.directory='*'"
    return "\n".join([
        "set -e",
        "cd /app",
        "test -s /tmp/base_commit.txt || { echo 'no /tmp/base_commit.txt' >&2; exit 3; }",
        "B=$(tr -d '[:space:]' < /tmp/base_commit.txt)",
        f"{g} checkout -q -f \"$B\"",
        f"{g} clean -fdxq",
        f"echo {shlex.quote(b64)} | base64 -d > /tmp/emx_ref.patch",
        f"{g} apply --whitespace=nowarn /tmp/emx_ref.patch",
        f"{g} add -A",
        f"echo {DIFF_MARK}",
        f"{g} diff --cached \"$B\"",
    ]) + "\n"


def docker_runner(host: str | None = None) -> Runner:
    """A fresh container per call (`compaction.deepswe.sandbox`: --rm, named, labelled
    emx.sandbox=s2-interact-check6, --network none, in-container timeout, removal confirmed)."""

    def run(image: str, script: str) -> tuple[int, str, str]:
        from compaction.deepswe.sandbox import Invocation

        with Invocation(purpose=CHECK6_PURPOSE, host=host) as inv:
            sb = inv.start(image, lifetime_s=CHECK6_TIMEOUT_S + 300, cpus=2, memory_mb=4096)
            r = sb.exec(script, timeout_s=CHECK6_TIMEOUT_S)
            return r.rc, r.stdout, r.stderr

    return run


def _unresolved(why: str, **ev) -> dict:
    return {"outcome": "unresolved", "reason": why, "evidence": ev}


def check_reference(spec: dict, run_dir: Path, k: int, *, runner: Runner | None = None) -> dict:
    """Check 6 (module docstring): {outcome: pass|fail|unresolved, reason, evidence}."""
    errs = validate_spec(spec)
    if errs:
        return _unresolved("invalid_spec", errors=errs)
    if spec["act"]["kind"] == "judge":
        return _unresolved("judge_scorer")
    if spec["act"]["kind"] == "command":
        return _unresolved("command_act_not_checkable_on_reference")
    try:
        run = _read(Path(run_dir) / f"b{k}" / "run.json")
        task = run.get("task") or {}
        image, rel = task.get("image"), task.get("solution_patch")
        patch = (C.REPO_ROOT / rel).read_bytes() if rel else None
    except (OSError, ValueError) as e:
        return _unresolved("no_run_json_or_patch", error=str(e)[:300])
    if not image or not patch:
        return _unresolved("no_image_or_patch", image=image, solution_patch=rel)
    rc, out, err = (runner or docker_runner())(image, reference_script(patch))
    if rc != 0 or DIFF_MARK not in out:
        return _unresolved("reference_did_not_apply", rc=rc, stderr=err[-500:])
    diff = out.split(DIFF_MARK, 1)[1].lstrip("\n")
    p = score_parts(spec, diff, [])
    ev = {"image": image, "solution_patch": rel,
          "patch_sha256": hashlib.sha256(patch).hexdigest(), "act_raw": p["act_raw"],
          "reached": p["reached"], "matches": p["evidence"]["act"],
          "decision_matches": p["evidence"]["decision"]}
    # The frozen scorer's own semantics (astra build r1 #9): the reference complies iff the
    # scorer would not score the act as performed on it. A negated act whose governed decision
    # the reference never reaches can't be judged on it: unresolved, to the card.
    if spec["act"].get("negate") and not p["reached"]:
        return _unresolved("reference_never_reaches_decision", **ev)
    complies = not p["performed"]
    return {"outcome": "pass" if complies else "fail",
            "reason": None if complies else "reference_performs_act", "evidence": ev}


# ------------------------------------------------------------------------------------------
# Freeze record
# ------------------------------------------------------------------------------------------

_CAND_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def freeze_path(bdir: Path, cand_id: str) -> Path:
    if not _CAND_ID.match(cand_id or ""):
        raise ValueError(f"bad candidate id {cand_id!r}")
    return Path(bdir) / "boundary" / "freeze" / f"{cand_id}.json"


def write_freeze(bdir: Path, cand: dict) -> Path:
    """Write `boundary/freeze/<cand_id>.json` once (FileExistsError if it exists). `cand` holds
    FREEZE_KEYS; the source's summary, kept indices, summary index and fingerprint are added
    from the stored files, with the write time and the record's sha256."""
    missing = [k for k in FREEZE_KEYS if k not in cand]
    if missing:
        raise ValueError(f"freeze record missing {missing}")
    if cand["source_policy"] not in ARM_SOURCES:
        raise ValueError(f"source_policy {cand['source_policy']!r} not in {ARM_SOURCES}")
    errs = validate_spec(cand["act_spec"])
    if errs:
        raise ValueError(f"invalid act spec: {errs}")
    path = freeze_path(bdir, cand["cand_id"])
    view = source_view(bdir, cand["source_policy"])
    rec = dict(cand) | {
        "source": {**{k: view[k] for k in ("summary", "summary_index", "kept_indices",
                                           "fingerprint")},
                   # the exact resumed context, so an arm can refuse a source that changed
                   # after the freeze (astra build r2 #2)
                   "post_sha256": canon_sha(view["post"])},
        "written_utc": _utc(),
    }
    rec["record_sha256"] = canon_sha(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        f.write(json.dumps(rec, indent=1, ensure_ascii=False))
    return path


def read_freeze(bdir: Path, cand_id: str) -> dict:
    """The freeze record, its sha256 checked."""
    rec = _read(freeze_path(bdir, cand_id))
    body = {k: v for k, v in rec.items() if k != "record_sha256"}
    if canon_sha(body) != rec.get("record_sha256"):
        raise ValueError(f"freeze record {cand_id} changed after it was written")
    return rec


BUNDLE_SEP = "\n\n"


def bundle_id(source: str) -> str:
    return f"bundle.{source}"


def write_bundle_freeze(bdir: Path, source: str, members: list[str]) -> Path:
    """The pre-registered secondary bundle (docs/sprint/plans/accumulated-loss.md): when two or
    more candidates of one source qualify at the fork boundary, repair-all vs matched-all. Each
    member must already be frozen (its card passed); the bundle's blocks join the members'
    repair texts and matched texts in candidate order, and its act specs are the members'
    (scored per item). Written once, like any freeze."""
    if len(members) < 2:
        raise ValueError("a bundle needs at least 2 frozen members")
    recs = [read_freeze(bdir, m) for m in members]
    if {r["source_policy"] for r in recs} != {source}:
        raise ValueError(f"bundle members are not all from {source}")
    first = recs[0]
    cand = {"cand_id": bundle_id(source), "source_policy": source,
            "req_id": "+".join(r["req_id"] for r in recs),
            "repair_text": BUNDLE_SEP.join(r["repair_text"] for r in recs),
            "matched_text": BUNDLE_SEP.join(r["matched_text"] for r in recs),
            "matched_rationale": " | ".join(r["matched_rationale"] for r in recs),
            "act_spec": first["act_spec"],
            "checks": {n: {"outcome": "pass", "reason": "bundle of frozen members",
                           "members": members}
                       for n in ("retention", "recoverability", "applicability", "decision",
                                 "design", "reference")},
            "bundle_items": members,
            "member_act_specs": {r["cand_id"]: r["act_spec"] for r in recs},
            "member_record_sha256": {r["cand_id"]: r["record_sha256"] for r in recs}}
    return write_freeze(bdir, cand)
