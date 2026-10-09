"""E12 validation: the global checks and per-readout validity, fail-closed (prereg §4.8; plan
v0.3.1 "Frozen interfaces" and "Failure handling").

`validate(recipe, root)` writes `contracts.VALIDATION_PATH` with `validation_sha256` (the sha256 of
the canonical JSON without that field and `utc`). Every check that cannot find its evidence
fails; nothing missing is counted as a pass.

Global checks (prereg §4.8 items 1-3; a failure is scenario 1):
- data: `emx.e12.data.verify_prerun_manifest(root)` raises nothing;
- generation path: `bench/lora_check.json` has `contracts.BENCH_FIELDS["lora_check"]`, its verdict
  matches its `gen_path` (PASS -> vllm, FAIL -> hf), and every evaluated output's meta records
  that path;
- training integrity of the runs the primaries rest on: `poison_s<s>`, `repair_A<s>`,
  `repair_C<s>` (scope "all") and, when the endpoint is 214, `rescue_A<s>`, `rescue_C<s>` (scope
  "durability": a failed selected A/C rescue invalidates the durability readouts only);
- decision snapshots: each stage's snapshot still has its recorded sha256 and its rows still
  equal the judged CSVs' rows (`emx.e12.analysis.check_snapshot`);
- admission: `contracts.admission_path(recipe)` still has the sha256 the final decision froze,
  with the same cuts and generation path (review r1 finding 6).

Coverage counts only the rows `contracts.row_selected` keeps under the final cuts (review r1
finding 7): a CSV generated before a cut may hold surplus rows, which are ignored. Task scores
and style ratings are read through `task_scores_checked` / `style_ratings_checked` (review r1
finding 9): the file header must equal the current sources (answer CSV sha256, prompt, settings,
decisions sha), every selected answer must have a latest line scored or `none` with the current
answer hash, and no foreign item may appear; a partial style file is missing coverage. Their file
hashes and headers are recorded in the validation record.

Run integrity (`check_run`), from `status/<run>.json`, `train/<run>.json`,
`train/<run>.steps.jsonl` and `ckpt/<key>/ckpt.json`: status ok or restarted_ok; attention eager;
the summary's parent is the expected one; every step 1..N present, its last record finite with
> 0 loss tokens; every expected save has a ckpt.json with 16 finite continuity log-probs; a nudge
dose's adapter differs from its dose-0 adapter by more than bf16 round-off.

Failure scope after endpoint selection (plan "Failure handling"): each readout depends on the
runs that produced its checkpoints (`model_runs`), its outputs' coverage (prereg §4.8 item 4:
every planned row present and judged, the deciding judge's unscoreable rate < 5% per model) and
the applied-smoke verdict of every adapter it reads (missing, FAIL or UNVERIFIED blocks it). So an
unused rescue failure touches nothing; a failed selected A/C nudge invalidates primary (ii) and
the readouts that read that nudge only; B, D and R failures remove only their own readouts.
Primary (i) and the task acceptable-rate also need the hand-label bars (§4.7): without the
scorer's record they are "locked"; a failed bar makes them "unvalidated". The `locked` flag is
set independently of every other failure (review r1 finding 13): an invalid readout that is also
locked keeps its status and reasons, and analysis still emits none of its numbers.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from emx import judge_run as JR
from emx.contracts import JUDGE_STATUSES, status_column
from emx.e12 import analysis as A
from emx.e12 import contracts as C
from emx.e12 import data as D

# Field names read from U2's records beyond the frozen summary keys (first name found wins).
CKPT_CONTINUITY_FIELDS = ("continuity_logprobs", "continuity")
CKPT_DISTANCE_FIELDS = ("distance_from_dose0", "distance")
STATUS_ORDER = ("invalid", "unavailable", "unvalidated", "locked", "valid")
OK_RUN = ("ok", "restarted_ok")


# --- runs behind each checkpoint ---------------------------------------------------------------


def model_runs(key: str) -> list[str]:
    """The training runs whose success the checkpoint `key` depends on (base: none)."""
    if key == C.BASE_KEY:
        return []
    k = C.parse_key(key)
    if k.dose is not None:
        parent = C.dose0_key(key)
        own = C.run_name("nudge", parent, k.seed)
        return [*model_runs(parent), own]
    if k.arm == "P":
        return [f"poison_s{k.seed}"]
    stem = f"{k.arm}{k.seed}"
    out = [] if k.arm == "C" else [f"poison_s{k.seed}"]
    out.append(f"repair_{stem}")
    if k.repair == C.RESCUE_END:
        out.append(f"rescue_{stem}")
    return out


def run_expectations(run: str) -> dict:
    """{segment, steps, parents (allowed summary parent_key values), saves: [keys]}."""
    seg, rest = run.split("_", 1)
    steps = C.SEGMENT_STEPS[seg]
    if seg == "poison":
        s = int(rest.removeprefix("s"))
        return {"segment": seg, "steps": steps, "parents": [None], "saves": [f"P{s}"]}
    if seg in ("repair", "rescue"):
        arm, s = rest[0], int(rest[1:])
        if seg == "repair":
            parents = [None] if arm == "C" else [f"P{s}"]
            saves = [C.format_key(arm, s, r) for r in C.REPAIR_SAVES]
        else:
            parents = [C.format_key(arm, s, 107)]
            saves = [C.format_key(arm, s, C.RESCUE_END)]
        return {"segment": seg, "steps": steps, "parents": parents, "saves": saves}
    if rest.startswith("base_s"):
        s = int(rest.removeprefix("base_s"))
        return {"segment": seg, "steps": steps, "parents": [None, C.BASE_KEY],
                "saves": [C.nudge_key(C.BASE_KEY, s, d) for d in C.NUDGE_DOSES]}
    parent = rest
    s = C.parse_key(parent).seed
    return {"segment": seg, "steps": steps, "parents": [parent],
            "saves": [C.nudge_key(parent, s, d) for d in C.NUDGE_DOSES]}


def _first(d: dict, names):
    for n in names:
        if n in d:
            return d[n]
    return None


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def check_run(recipe: str, run: str, root: str | Path = C.REPO_ROOT) -> dict:
    """{ok, status, reasons} for one training run (see the module docstring)."""
    root = Path(root)
    exp = run_expectations(run)
    reasons: list[str] = []
    st = A.read_json(root / C.status_path(recipe, run))
    status = (st or {}).get("status")
    if st is None:
        reasons.append("no status record")
    elif status not in OK_RUN:
        reasons.append(f"status {status}: {st.get('reason')}")
    summ = A.read_json(root / C.train_summary(recipe, run))
    if summ is None:
        reasons.append("no training summary")
    else:
        if summ.get("attn") != "eager":
            reasons.append(f"attention {summ.get('attn')!r}, not eager")
        if summ.get("parent_key") not in exp["parents"]:
            reasons.append(f"parent {summ.get('parent_key')!r}, expected {exp['parents']}")
    steps_p = root / C.train_steps(recipe, run)
    if not steps_p.is_file():
        reasons.append("no per-step record")
    else:
        last: dict[int, dict] = {}
        for line in steps_p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                last[int(r["step"])] = r
        missing = [i for i in range(1, exp["steps"] + 1) if i not in last]
        bad = [i for i, r in last.items() if not (_finite(r.get("loss"))
               and _finite(r.get("grad_norm")) and (r.get("n_loss_tokens") or 0) > 0)]
        if missing:
            reasons.append(f"{len(missing)} of {exp['steps']} steps missing")
        if bad:
            reasons.append(f"non-finite or unmasked steps {sorted(bad)[:5]}")
    for key in exp["saves"]:
        ck = A.read_json(root / C.ckpt_dir(recipe, key) / "ckpt.json")
        if ck is None:
            reasons.append(f"{key}: no ckpt.json")
            continue
        cont = _first(ck, CKPT_CONTINUITY_FIELDS)
        if not (isinstance(cont, list) and len(cont) == C.CONTINUITY_TOKENS
                and all(_finite(x) for x in cont)):
            reasons.append(f"{key}: continuity log-probs missing or non-finite")
        if exp["segment"] == "nudge":
            dist = _first(ck, CKPT_DISTANCE_FIELDS) or {}
            if dist.get("differs") is not True:
                reasons.append(f"{key}: adapter not shown to differ from dose 0")
    return {"ok": not reasons, "status": status, "reasons": reasons}


# --- per-output evidence -----------------------------------------------------------------------


def smoke_verdict(recipe: str, key: str, root: str | Path) -> str:
    rec = A.read_json(Path(root) / C.smoke_adapter_path(recipe, key))
    if rec is None:
        return "missing"
    v = rec.get("verdict")
    if v not in C.SMOKE_VERDICTS:
        return "malformed"
    if v == "UNVERIFIED" or rec.get("error"):
        return "UNVERIFIED"
    # E12's rule (contracts.smoke_applied), re-derived from the recorded numbers: the pod's
    # records also carry EQ0's 0.1-nat floor in their own verdict (deviation logged 2026-10-09)
    eff = rec.get("effect_nats", rec.get("effect"))
    noise = rec.get("noise_nats", rec.get("noise"))
    ok = C.smoke_applied(None if eff is None else float(eff),
                         None if noise is None else float(noise))
    if ok is None:
        return "UNVERIFIED"
    return "PASS" if ok else "FAIL"


def _gen_answers(path: Path, kind: str, cuts) -> dict[str, dict]:
    """{"qid/sample_idx": row} of the rows `contracts.row_selected` keeps (CSV order)."""
    import csv

    csv.field_size_limit(1 << 30)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {f"{r['qid']}/{int(r['sample_idx'])}": r for r in rows
            if C.row_selected(kind, r["qset"], int(r["sample_idx"]), cuts)}


def _scores_file(path: Path, *, mode: str, want: dict, answers: dict[str, dict],
                 line_fields: dict, required_status=None) -> dict:
    """Shared provenance and coverage check of one `emx.e12.llm` results file (review r1
    finding 9). `want`: header field -> expected value; `answers`: the selected source rows by
    item id; `line_fields`: line field -> expected value. Returns {status: ok|missing|partial|
    fail, reasons, lines: {item_id: latest line} (selected items), provenance}. "partial": the
    provenance verifies but some selected item has no terminal line (missing coverage)."""
    from emx.e12 import llm

    rel = str(path)
    if not path.is_file():
        return {"status": "missing", "reasons": [f"{path.name} missing"], "lines": {},
                "provenance": {"path": rel, "sha256": None}}
    prov = {"path": rel, "sha256": JR.sha256_file(path)}
    try:
        header, rows = llm.read_results(path)
        if header is None:
            raise ValueError("no header")
        llm._check_header(header, mode)
    except (ValueError, llm.FingerprintMismatch, KeyError) as e:
        return {"status": "fail", "reasons": [f"header: {e}"], "lines": {}, "provenance": prov}
    prov["header"] = {k: header.get(k) for k in llm.HEADER_KEYS}
    reasons = [f"header {k} {str(header.get(k))[:12]} != current {str(v)[:12]}"
               for k, v in want.items() if header.get(k) != v]
    foreign = sorted(set(rows) - set(answers))
    if foreign:
        reasons.append(f"{len(foreign)} items not among the selected answers ({foreign[:3]})")
    bad_lines, bad_fields = [], set()
    for iid, line in rows.items():
        if iid not in answers:
            continue
        a = answers[iid]
        exp = {**line_fields, "qid": a["qid"], "sample_idx": int(a["sample_idx"]),
               "answer_sha256": llm.sha256_text(a["response"])}
        wrong = {k for k, v in exp.items() if line.get(k) != v}
        if wrong:
            bad_lines.append(iid)
            bad_fields |= wrong
    if bad_lines:
        reasons.append(f"{len(bad_lines)} lines differ from the current sources in "
                       f"{sorted(bad_fields)} ({bad_lines[:3]})")
    done = {iid: rows[iid] for iid in answers if rows.get(iid, {}).get("status") in llm.TERMINAL}
    prov |= {"n_items": len(answers), "n_terminal": len(done)}
    if reasons:
        return {"status": "fail", "reasons": reasons, "lines": {}, "provenance": prov}
    if len(done) != len(answers):
        return {"status": "partial", "lines": {}, "provenance": prov, "reasons": [
            f"{len(answers) - len(done)} of {len(answers)} selected items not scored or none"]}
    return {"status": "ok", "reasons": [], "lines": done, "provenance": prov}


def task_settings_sha256() -> str:
    from emx.e12 import llm

    return llm.canonical_sha256(llm.make_settings("logprob", judge=C.TASK_JUDGE))


def task_scores_checked(recipe: str, key: str, root: str | Path, dsha: str, cuts=()) -> dict:
    """The task-score file of `key` checked against its current sources (review r1 finding 9):
    header source (the task generation CSV's sha256), prompt (`TASK_PROMPT_SHA256`), settings
    (luna's logprob settings) and decisions sha equal the current ones; every selected task
    answer has a latest line scored or `none`, with its answer and settings hashes; no foreign
    items. See `_scores_file` for the return value."""
    root = Path(root)
    gen = root / C.gen_csv(recipe, "task", key)
    out_p = root / C.task_scores(recipe, key)
    if not gen.is_file():
        return {"status": "missing", "reasons": ["task generation CSV missing"], "lines": {},
                "provenance": {"path": C.task_scores(recipe, key), "sha256": None}}
    sset = task_settings_sha256()
    want = {"source_sha256": JR.sha256_file(gen), "prompt_sha256": C.TASK_PROMPT_SHA256,
            "settings_sha256": sset, "decisions_sha256": dsha}
    res = _scores_file(out_p, mode="logprob", want=want,
                       answers=_gen_answers(gen, "task", cuts),
                       line_fields={"prompt_sha256": C.TASK_PROMPT_SHA256,
                                    "settings_sha256": sset, "decisions_sha256": dsha})
    res["provenance"]["path"] = C.task_scores(recipe, key)
    if res["status"] == "ok":
        refs = _references(root)
        wrong = [i for i, ln in res["lines"].items()
                 if refs is None or ln.get("reference_sha256") != refs.get(ln["qid"])]
        if wrong:
            res |= {"status": "fail", "lines": {}, "reasons": [
                f"{len(wrong)} lines' reference_sha256 differs from the holdout references"
                + ("" if refs is not None else " (holdout references unreadable)")]}
    return res


def _references(root: Path) -> dict[str, str] | None:
    from emx.e12 import llm

    try:
        return {it["qid"]: llm.sha256_text(it["good_completion"])
                for it in D.holdout_items(root)}
    except (OSError, ValueError, KeyError):
        return None


def style_settings_sha256() -> str:
    from emx.e12 import llm
    from emx.e12 import style_audit as SA

    return llm.canonical_sha256(llm.make_settings("text", judge=C.STYLE_JUDGE,
                                                  max_tokens=SA.MAX_TOKENS["rate"]))


def style_source(recipe: str, key: str) -> str:
    if key == C.BASE_KEY and C.BASE_FULL_SOURCE == "eq0-vllm":
        return C.EQ0_BASE_GEN
    return C.gen_csv(recipe, "full", key)


def style_ratings_checked(recipe: str, key: str, root: str | Path, dsha: str, frozen: dict,
                          cuts=()) -> dict:
    """The style rating file of `key` checked as the task scores are (review r1 finding 9):
    header prompt = frozen.json's rater prompt sha, settings = the rater's, decisions = `dsha`,
    source = the current answer CSV's sha256 (EQ0's base answers for base); items = sample
    indices 0-9 (0-4 under cut 0) of the 41 test questions, each with a terminal latest line; a
    partial file is "partial" (missing coverage), never used."""
    from emx.e12 import style_audit as SA

    root = Path(root)
    src = root / style_source(recipe, key)
    rel = C.style_answers(recipe, key)
    if not src.is_file():
        return {"status": "missing", "reasons": [f"{style_source(recipe, key)} missing"],
                "lines": {}, "provenance": {"path": rel, "sha256": None}}
    tq = set(SA.broad_test_qids(root))
    samples = set(SA.answer_samples(cuts))
    answers = {i: r for i, r in _gen_answers(src, "full", ()).items()
               if r["qset"] == "ext48" and r["qid"] in tq and int(r["sample_idx"]) in samples}
    sset = style_settings_sha256()
    want = {"source_sha256": JR.sha256_file(src),
            "prompt_sha256": frozen["rater_prompt_sha256"], "settings_sha256": sset,
            "decisions_sha256": dsha}
    res = _scores_file(root / rel, mode="text", want=want, answers=answers,
                       line_fields={"key": key})
    res["provenance"]["path"] = rel
    if res["status"] == "ok" and len(answers) != len(tq) * len(samples):
        n_want = len(tq) * len(samples)
        res |= {"status": "fail", "lines": {}, "reasons": [
            f"{len(answers)} test answers in {style_source(recipe, key)}, expected {n_want}"]}
    return res


def style_keys(doc: dict) -> list[str]:
    """The models the style-audit readout reads: base, P<s> and the endpoint's A/B/C for the
    eligible seeds."""
    fin = doc["final"]
    S = sorted(fin["eligible_seeds"])
    return [C.BASE_KEY, *(f"P{s}" for s in S),
            *(C.format_key(a, s, fin["endpoint"]) for a in C.REPAIR_ARMS for s in S)]


def check_style(recipe: str, root: Path, doc: dict, dsha: str) -> dict:
    """{status: ok|unavailable|fail, frozen: {path, sha256}, keys: {key: check}, reasons}."""
    from emx.e12 import llm
    from emx.e12 import style_audit as SA

    fp = root / C.STYLE_FROZEN
    out = {"frozen": {"path": C.STYLE_FROZEN,
                      "sha256": JR.sha256_file(fp) if fp.is_file() else None}}
    try:
        frozen = SA.load_frozen(root)
    except (llm.Refused, ValueError, KeyError) as e:
        return out | {"status": "unavailable" if not fp.is_file() else "fail",
                      "reasons": [f"frozen.json: {e}"], "keys": {}}
    cuts = doc["final"].get("cuts") or ()
    keys = {k: style_ratings_checked(recipe, k, root, dsha, frozen, cuts)
            for k in style_keys(doc)}
    reasons = [f"{k}: {'; '.join(v['reasons'])}" for k, v in keys.items() if v["reasons"]]
    sts = {v["status"] for v in keys.values()}
    status = "fail" if "fail" in sts else ("unavailable" if sts - {"ok"} else "ok")
    return out | {"status": status, "reasons": reasons,
                  "keys": {k: {"status": v["status"], "reasons": v["reasons"],
                               **v["provenance"]} for k, v in keys.items()}}


def check_output(tree: A.Tree, kind: str, key: str, expected: int | None, dsha: str) -> dict:
    """Coverage of one output (prereg §4.8 item 4): {status: ok|not_planned|skipped|missing|
    fail, n_rows, n_unscoreable, unscoreable_rate, reasons}."""
    if expected is None:
        return {"status": "not_planned", "reasons": ["not in the planned job list (cut)"]}
    state = tree.state(kind, key)
    if state != "present":
        return {"status": state, "reasons": [f"output {state}"]}
    if kind == "task":
        chk = task_scores_checked(tree.recipe, key, tree.root, dsha, tree.cuts)
        if chk["status"] != "ok":
            return {"status": "missing" if chk["status"] == "missing" else "fail",
                    "reasons": [f"task scores {chk['status']}: {r}" for r in chk["reasons"]],
                    "task_scores": chk["provenance"]}
        lines = list(chk["lines"].values())
        reasons = []
        if len(lines) != expected:
            reasons.append(f"{len(lines)} task scores, expected {expected}")
        n_bad = sum(not (r.get("status") == "scored" and A._num(r.get("score")) is not None)
                    for r in lines)
        rate = n_bad / len(lines) if lines else None
        if rate is None or rate >= C.UNSCOREABLE_MAX:
            reasons.append(f"unscoreable rate {rate}")
        return {"status": "fail" if reasons else "ok", "n_rows": len(lines),
                "n_unscoreable": n_bad, "unscoreable_rate": rate, "reasons": reasons,
                "task_scores": chk["provenance"]}
    try:
        df = tree.need(kind, key)
    except A.AnalysisRefused as e:
        return {"status": "fail", "reasons": [str(e)]}
    reasons = []
    if len(df) != expected:  # df: the rows the cuts select (A.Tree); surplus rows are ignored
        reasons.append(f"{len(df)} selected rows, expected {expected}")
    if df.duplicated(["qset", "qid", "sample_idx"]).any():
        reasons.append("duplicate (qset, qid, sample_idx) rows")
    judged = df["deciding"].notna()
    for m in C.METRICS:
        for j in C.JUDGES:
            judged &= (df["deciding"] != j) | df[status_column(m, j)].isin(JUDGE_STATUSES)
    if not judged.all():
        reasons.append(f"{int((~judged).sum())} rows not judged by their deciding judge")
    n_bad = int((~df["d_scored"]).sum())
    rate = n_bad / len(df) if len(df) else None
    if rate is None or rate >= C.UNSCOREABLE_MAX:
        reasons.append(f"deciding judge unscoreable rate {rate}")
    return {"status": "fail" if reasons else "ok", "n_rows": len(df), "n_unscoreable": n_bad,
            "unscoreable_rate": rate, "reasons": reasons}


def handlabel_status(root: str | Path, recipe: str, dsha: str) -> dict:
    """{em, task}: "valid" (bar met), "unvalidated" (bar failed), or "locked" (no record, or a
    record made under another decisions_sha256). Record: `analysis.handlabel_record(recipe)`,
    {decisions_sha256, em: {pass}, task: {pass}} (scripts/e12_handlabel_score.py)."""
    rel = A.handlabel_record(recipe)
    p = Path(root) / rel
    rec = A.read_json(p)
    out = {"path": rel, "sha256": JR.sha256_file(p) if rec else None}
    if rec is None:
        return out | {"em": "locked", "task": "locked", "reason": "hand-label record missing"}
    if rec.get("decisions_sha256") != dsha:
        return out | {"em": "locked", "task": "locked",
                      "reason": "hand-label record made under another decisions_sha256"}
    for s in ("em", "task"):
        out[s] = "valid" if (rec.get(s) or {}).get("pass") is True else "unvalidated"
    return out | {"reason": None}


# --- the readout plan --------------------------------------------------------------------------


def _ro(outputs, *, durability: bool, needs_eligible: bool = False, handlabel: str | None = None,
        extra_adapters=(), **meta) -> dict:
    outs = list(dict.fromkeys(outputs))
    adapters = list(dict.fromkeys([k for _, k in outs if k != C.BASE_KEY] + list(extra_adapters)))
    return {"outputs": [list(o) for o in outs], "adapters": adapters, "durability": durability,
            "needs_eligible": needs_eligible, "handlabel": handlabel, **meta}


def nudge_keys(parent: str, seed: int) -> list[str]:
    return [C.nudge_key(parent, seed, d) for d in C.NUDGE_DOSES]


def readout_plan(doc: dict) -> dict[str, dict]:
    """Every readout id -> its outputs [(kind, key)], adapters, durability flag, hand-label bar.
    Durability readouts exist only when the final outcome is "proceed"."""
    fin = doc["final"]
    e, S, T = fin["endpoint"], list(fin["eligible_seeds"]), C.TRAIN_SEEDS
    plan: dict[str, dict] = {}
    if e is None:
        return plan
    rescued = bool(fin.get("rescue_triggered"))
    if fin["outcome"] == "proceed":
        A_, C_ = ({s: C.format_key(a, s, e) for s in T} for a in ("A", "C"))
        P = {s: f"P{s}" for s in T}
        plan["primary_i"] = _ro(
            [o for s in S for k in (A_[s], C_[s]) for o in
             ((kind, k) for kind in (*C.CUE_KINDS, "full"))]
            + [(kind, C.BASE_KEY) for kind in C.CUE_KINDS],
            durability=True, needs_eligible=True, handlabel="em",
            extra_adapters=[P[s] for s in S], kind="primary_i")
        plan["primary_ii"] = _ro(
            [("nudge", n) for s in S for k in (A_[s], C_[s]) for n in nudge_keys(k, s)]
            + [("full", k) for s in S for k in (A_[s], C_[s])],
            durability=True, needs_eligible=True, extra_adapters=[P[s] for s in S],
            kind="primary_ii")
        plan["c_minus_r"] = _ro(
            [("nudge", n) for s in S for n in nudge_keys(C_[s], s) + nudge_keys(C.BASE_KEY, s)]
            + [("full", C_[s]) for s in S] + [A.EQ0_BASE],
            durability=True, needs_eligible=True, kind="c_minus_r")
        for key in [C.BASE_KEY, *P.values(), *C.endpoint_keys(e)]:
            plan[f"cue_lift/{key}"] = _ro([(k, key) for k in (*C.CUE_KINDS, "full")],
                                          durability=True, kind="cue_lift", key=key)
        style_keys = [C.format_key(a, s, e) for a in C.REPAIR_ARMS for s in S] + [
            P[s] for s in S]
        plan["style_audit"] = _ro([("full", k) for k in style_keys] + [A.EQ0_BASE],
                                  durability=True, needs_eligible=True, kind="style_audit")
    for key in [C.BASE_KEY, *(f"P{s}" for s in T), *C.endpoint_keys(e)]:
        plan[f"task/{key}"] = _ro([("task", key)], durability=False, handlabel="task",
                                  kind="task", key=key)
    for s in T:
        plan[f"a_minus_b_107/s{s}"] = _ro(
            [("full", C.format_key("A", s, 107)), ("full", C.format_key("B", s, 107))],
            durability=False, kind="a_minus_b_107", seed=s)
        for arm in C.REPAIR_ARMS:
            traj = [("traj", C.format_key(arm, s, r)) for r in C.REPAIR_SAVES if r != 107]
            ends = [("full", C.format_key(arm, s, 107))] + (
                [("full", C.format_key(arm, s, C.RESCUE_END))] if rescued else [])
            pois = [] if arm == "C" else [("full", f"P{s}")]
            plan[f"trajectory/{arm}{s}"] = _ro(traj + ends + pois + [A.EQ0_BASE],
                                               durability=False, kind="trajectory", arm=arm,
                                               seed=s)
    r8 = [("full", f"P{s}") for s in T]
    for end in (107, C.RESCUE_END) if rescued else (107,):
        r8 += [("full", C.format_key(a, s, end)) for s in T for a in ("A", "C")]
    plan["random8"] = _ro(r8 + [A.EQ0_BASE], durability=False, kind="random8")
    plan["weights"] = _ro([], durability=False, kind="weights")
    return plan


# --- validate ----------------------------------------------------------------------------------


def _expected_rows(recipe: str, fin: dict) -> dict[tuple[str, str], int]:
    jobs = C.planned_jobs(recipe, fin.get("gen_path") or "vllm", fin["endpoint"],
                          fin.get("cuts") or (), rescue_triggered=fin.get("rescue_triggered"))
    out = {(j.kind, j.key): j.n_rows for j in jobs}
    out[A.EQ0_BASE] = C.KIND_ROWS["full"]
    return out


def check_admission(recipe: str, root: Path, fin: dict) -> dict:
    """The admission record the final decision froze is unchanged (review r1 finding 6)."""
    from emx.e12 import llm

    try:
        rec = llm.check_final_admission(root, recipe, fin)
    except llm.Refused as e:
        return {"ok": False, "reasons": [str(e)], **(fin.get("admission") or {})}
    reasons = []
    if rec["gen_path"] != fin.get("gen_path"):
        reasons.append(f"gen_path {rec['gen_path']!r} != final's {fin.get('gen_path')!r}")
    return {"ok": not reasons, "reasons": reasons, **(fin.get("admission") or {})}


def check_genpath(recipe: str, root: Path, metas: dict[str, str | None],
                  final_gen_path: str | None = None) -> dict:
    rec = A.read_json(root / C.bench_path(recipe, "lora_check"))
    if rec is None:
        return {"ok": False, "reasons": ["bench/lora_check.json missing"]}
    reasons = [f"field {f!r} missing" for f in C.BENCH_FIELDS["lora_check"] if f not in rec]
    want = {"PASS": "vllm", "FAIL": "hf"}.get(rec.get("verdict"))
    if want is None or rec.get("gen_path") != want:
        reasons.append(f"verdict {rec.get('verdict')!r} with gen_path {rec.get('gen_path')!r}")
    if final_gen_path is not None and rec.get("gen_path") != final_gen_path:
        reasons.append(f"gen_path {rec.get('gen_path')!r} differs from the admitted "
                       f"{final_gen_path!r}")
    wrong = sorted(k for k, g in metas.items() if g != rec.get("gen_path"))
    if wrong:
        reasons.append(f"outputs not generated on {rec.get('gen_path')!r}: {wrong[:5]}")
    return {"ok": not reasons, "verdict": rec.get("verdict"), "gen_path": rec.get("gen_path"),
            "reasons": reasons}


def readout_status(ro: dict, *, n_eligible: int, glob: dict, outputs: dict, smoke: dict,
                   runs: dict, hand: dict, style: dict | None = None) -> dict:
    """{status, reasons, locked}. `locked` (review r1 finding 13) is set whenever the readout
    needs a hand-label bar whose record is absent or stale, independently of every other
    failure: a locked readout's numbers are never emitted, whatever its status."""
    reasons: list[str] = []
    worst = "valid"

    def bump(status: str, why: str) -> None:
        nonlocal worst
        reasons.append(why)
        if STATUS_ORDER.index(status) < STATUS_ORDER.index(worst):
            worst = status

    scopes = ("all", "durability") if ro["durability"] else ("all",)
    for f in glob["failures"]:
        if f["scope"] in scopes:
            bump("invalid", f"global {f['check']}: {f['reason']}")
    if ro["needs_eligible"] and n_eligible < C.MIN_ELIGIBLE:
        bump("invalid", f"{n_eligible} eligible seeds (< {C.MIN_ELIGIBLE})")
    cut = set()
    for kind, key in ro["outputs"]:
        o = outputs[f"{kind}/{key}"]
        if o["status"] == "not_planned":
            bump("unavailable", f"{kind}/{key} not planned (cut)")
            cut.add(key)
        elif o["status"] != "ok":
            bump("invalid", f"{kind}/{key}: {'; '.join(o['reasons'])}")
    live = [k for k in ro["adapters"] if k not in cut]  # a cut model is never trained or read
    for key in live:
        if smoke[key] != "PASS":
            bump("invalid", f"applied-smoke {key}: {smoke[key]}")
    for key in [k for _, k in ro["outputs"] if k not in cut] + live:
        for run in model_runs(key):
            if not runs[run]["ok"]:
                bump("invalid", f"run {run}: {'; '.join(runs[run]['reasons'])}")
    if ro["kind"] == "style_audit" and style is not None and style["status"] != "ok":
        bump("invalid" if style["status"] == "fail" else "unavailable",
             f"style ratings {style['status']}: {'; '.join(style['reasons'])}")
    locked = False
    if ro["handlabel"]:
        h = hand[ro["handlabel"]]
        locked = h == "locked"
        if h != "valid":
            bump(h, f"hand-label {ro['handlabel']} bar: {h}")
    return {"status": worst, "reasons": list(dict.fromkeys(reasons)), "locked": locked}


def validate(recipe: str = C.DEFAULT_RECIPE, root: str | Path = C.REPO_ROOT, *,
             write: bool = True) -> dict:
    """The validation record (see the module docstring); written to VALIDATION_PATH with
    `write`. Refused (AnalysisRefused) without a verified final decision."""
    root = Path(root)
    doc, dsha = A.load_final(recipe, root)
    fin = doc["final"]
    e = fin["endpoint"]
    tree = A.Tree(recipe, root, fin.get("cuts") or ())
    plan = readout_plan(doc)
    expected = _expected_rows(recipe, fin)
    failures: list[dict] = []
    checks: dict = {}

    checks["admission"] = check_admission(recipe, root, fin)
    if not checks["admission"]["ok"]:
        failures.append({"check": "admission", "scope": "all",
                         "reason": "; ".join(checks["admission"]["reasons"])})

    try:
        D.verify_prerun_manifest(root)
        checks["data"] = {"ok": True, "reasons": []}
    except Exception as ex:  # noqa: BLE001 - fail closed on anything, even a stub
        checks["data"] = {"ok": False, "reasons": [f"{type(ex).__name__}: {ex}"]}
        failures.append({"check": "data", "scope": "all", "reason": str(ex) or type(ex).__name__})

    used = sorted({tuple(o) for ro in plan.values() for o in ro["outputs"]})
    outputs = {f"{k}/{key}": check_output(tree, k, key, expected.get((k, key)), dsha)
               for k, key in used}
    metas = {}
    for kind, key in sorted(expected):
        if ((kind, key) != A.EQ0_BASE or C.BASE_FULL_SOURCE == "e12-hf") and tree.state(
                kind, key) == "present":
            meta = A.read_json(root / C.gen_meta(recipe, kind, key)) or {}
            metas[f"{kind}/{key}"] = meta.get("gen_path")
    checks["generation_path"] = check_genpath(recipe, root, metas, fin.get("gen_path"))
    if not checks["generation_path"]["ok"]:
        failures.append({"check": "generation_path", "scope": "all",
                         "reason": "; ".join(checks["generation_path"]["reasons"])})

    gating = [f"poison_s{s}" for s in C.TRAIN_SEEDS]
    if e is not None:
        gating += [f"repair_{a}{s}" for a in ("A", "C") for s in C.TRAIN_SEEDS]
    selected_rescue = ([f"rescue_{a}{s}" for a in ("A", "C") for s in C.TRAIN_SEEDS]
                       if e == C.RESCUE_END else [])
    run_names = set(gating) | set(selected_rescue)
    for ro in plan.values():
        for _, key in ro["outputs"]:
            run_names.update(model_runs(key))
        for key in ro["adapters"]:
            run_names.update(model_runs(key))
    runs = {r: check_run(recipe, r, root) for r in sorted(run_names)}
    checks["training"] = {r: runs[r] for r in gating + selected_rescue}
    for r in gating + selected_rescue:
        if not runs[r]["ok"]:
            failures.append({"check": "training", "scope": "all" if r in gating else
                             "durability", "reason": f"{r}: {'; '.join(runs[r]['reasons'])}"})

    snaps = {}
    for stage in C.DECISION_STAGES:
        if doc.get(stage):
            probs = A.check_snapshot(recipe, stage, doc[stage]["snapshot"]["sha256"], root)
            snaps[stage] = {"ok": not probs, "reasons": probs}
            if probs:
                failures.append({"check": "snapshots", "scope": "all",
                                 "reason": "; ".join(probs)})
    checks["snapshots"] = snaps

    adapters = sorted({k for ro in plan.values() for k in ro["adapters"]}
                      | {k for (_, k) in expected if k != C.BASE_KEY})
    smoke = {k: smoke_verdict(recipe, k, root) for k in adapters}
    hand = handlabel_status(root, recipe, dsha)
    glob = {"checks": checks, "failures": failures,
            "pass": not any(f["scope"] == "all" for f in failures),
            "pass_durability": not failures}
    n_el = len(fin["eligible_seeds"])
    style = check_style(recipe, root, doc, dsha) if "style_audit" in plan else None
    readouts = {rid: {**readout_status(ro, n_eligible=n_el, glob=glob, outputs=outputs,
                                       smoke=smoke, runs=runs, hand=hand, style=style),
                      "durability": ro["durability"], "kind": ro["kind"]}
                for rid, ro in plan.items()}
    out = {"exp": C.EXP, "recipe": recipe, "decisions_sha256": dsha, "outcome": fin["outcome"],
           "endpoint": e, "eligible_seeds": fin["eligible_seeds"],
           "cuts": list(fin.get("cuts") or []), "global": glob,
           "runs": runs, "smoke": smoke, "outputs": outputs, "handlabel": hand,
           "style": style, "readouts": readouts}
    out["validation_sha256"] = validation_hash(out)
    out["utc"] = A.utc_now()
    if write:
        A.write_json(out, root / C.VALIDATION_PATH)
    return out


def validation_hash(rec: dict) -> str:
    return A.sha_text(A.canonical({k: v for k, v in rec.items()
                                   if k not in ("utc", "validation_sha256")}))
