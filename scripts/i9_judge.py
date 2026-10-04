"""I9 judging and monitors CLI (implementation plan §2 U4; §4 steps P0, P2, P4, P5).

Subcommands (every one takes --dry-run: print the batch size and projected cost from
contracts.PRICE_PER_MTOK and chars/4 token estimates, make no call). Every subcommand that can
call a model refuses to make any call unless --confirm-paid is passed (code review r1 finding 3);
it prints the projection first either way. --limit N (the first N scenarios) is the token pilot:
it is PAID, so it also needs --confirm-paid; it writes the call caches plus its own manifest
(`<step>_pilot`, with results/I9/pilots/<step>_pilot.json) recording the calls, token counts and
measured cost per call by model, the source for the re-projection; no labels, rates, checks or
predictions. handlabel-bundle, handlabel-score, adjudicate-bundle and adjudicate-score make no
model calls.

  misbehavior       luna + DeepSeek on every reply, GLM tie-breaks -> verdicts/, code_checks,
                    labels (final and LLM-only), rates (final and LLM-only),
                    checks/code_check_overrides.json (informational, prereg amendment B item 2)
  handlabel-bundle  blind 50-reply CSV + PRIVATE key (results/I9/handlabels/)
  handlabel-score   kappa gate (raw luna and final labels) on Jacob's filled bundle
                    -> checks/handlabel_kappa.json
  adjudicate-bundle blind CSV of replies still unlabelled after the re-judge (> 30: exit 3,
                    stop and ask Jacob) -> handlabels/adjudicate_bundle.csv + PRIVATE key
  adjudicate-score  Jacob's verdicts (adjudicate_jacob.csv) -> labels with rule "adjudicated"
                    (final = his verdict AND the code check), rates rewritten
  validate          B/C effort pick + luna-vs-Sonnet gate on the 16 validation items -> checks/
  stage0            Gemini accounts, Sonnet and luna Stage C, aggregate_design -> results/I9/stage0/
  monitor-zero      zero-shot rate monitor -> predictions/monitor_zero_shot.jsonl
  monitor-few       few-shot rate monitor (needs --folds) -> predictions/monitor_few_shot.jsonl
                    Both take --set KEY[,KEY]: after an unseal, --set confirmatory,sealed runs
                    the 80-scenario confirmatory set in one go (monitor-few: the folds must
                    assign exactly that set). Rows are merged by scenario id; the pre-merge file
                    is a manifest input and the set is in the manifest notes.
  monitor-cot       CoT monitor on the thinking arm -> predictions/monitor_cot.jsonl

Calls go through emx.i9.textllm.complete, which guards each batch with emx.spend.check_total and
journals every response; this script does not write the ledger itself.

Usage:
  uv run python scripts/i9_judge.py misbehavior --dry-run
  uv run python scripts/i9_judge.py misbehavior --limit 5 --confirm-paid
  uv run python scripts/i9_judge.py monitor-few --folds results/I9/analysis/folds.json --target qwen \
      --confirm-paid
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from emx.i9 import contracts as C
from emx.i9 import judging as J
from emx.i9 import monitor as M
from emx.i9 import provenance

GLM_SHARE = 0.10  # prereg §7: tie-breaks on about 10% of replies


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def git_state() -> tuple[str, bool]:
    """(commit, dirty): the shared rule in emx.i9.provenance."""
    return provenance.git_state()


def file_ref(path: Path) -> C.FileRef:
    p = Path(path)
    rows = (sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
            if p.suffix in (".jsonl", ".csv") else None)
    try:
        rel = str(p.resolve().relative_to(C.REPO_ROOT))
    except ValueError:
        rel = str(p)
    return C.FileRef(rel, J.sha256_file(p), rows)


def write_manifest(step: str, inputs: list[Path], outputs: list[Path], started: str, spend: Any,
                   seeds: dict[str, int] | None = None, notes: str = "",
                   prior: list[C.FileRef] | None = None) -> Path:
    """``prior``: FileRefs taken before this step rewrote a file in place (its pre-rewrite
    sha256), recorded as inputs so the chain check links the earlier step's output to this one
    (code review r3 finding 3)."""
    commit, dirty = git_state()
    return C.Manifest(step=step, inputs=[*(prior or []),
                                         *(file_ref(p) for p in inputs if Path(p).exists())],
                      outputs=[file_ref(p) for p in outputs], git_commit=commit, git_dirty=dirty,
                      seeds=seeds or {}, pins={"lynch": C.LYNCH_COMMIT, "wsbench": C.WSBENCH_COMMIT},
                      started_utc=started, finished_utc=utc(),
                      usd=float(getattr(spend, "usd", 0.0)), notes=notes).write()


def load_sets() -> dict[str, list[str]]:
    return json.loads(C.SETS_PATH.read_text())


def first_ids(ids: list[str], limit: int) -> list[str]:
    ids = sorted(ids)
    return ids[:limit] if limit else ids


def print_projection(rows: list[dict], *, paid: bool = False) -> None:
    for r in rows:
        print(json.dumps(r))
    tail = "paid run follows" if paid else "dry run, no calls made"
    print(f"projected total: ${sum(r.get('usd', 0.0) for r in rows):.4f} "
          f"({sum(r.get('calls', 0) for r in rows)} calls; {tail})")


def paid_gate(a: argparse.Namespace, proj: list[dict]) -> int | None:
    """Print the projection, then: a dry run stops (0); a run without --confirm-paid is refused
    (2) before any call; else None (go on: a paid run, or with --limit the paid token pilot)."""
    go = not a.dry_run and a.confirm_paid
    print_projection(proj, paid=go)
    if a.dry_run:
        return 0
    if not a.confirm_paid:
        print(f"refused: `{a.cmd}` makes paid model calls (--limit too: it is the paid token "
              "pilot); pass --confirm-paid. No calls made.", file=sys.stderr)
        return 2
    if a.limit:
        print(f"--limit {a.limit}: PAID token pilot; writes a _pilot manifest, no step outputs")
    return None


class Meter:
    """Per-model calls, tokens and dollars of one run, measured at the ``complete`` seam
    (spend deltas around each textllm batch) or, for the A-C driver, from textllm's journals."""

    FIELDS = ("prompts", "calls", "input_tokens", "output_tokens", "usd")

    def __init__(self) -> None:
        self.by_model: dict[str, dict[str, float]] = {}

    def add(self, model: str, **d: float) -> None:
        m = self.by_model.setdefault(model, dict.fromkeys(self.FIELDS, 0))
        for k, v in d.items():
            m[k] += v

    def wrap(self) -> J.CompleteFn:
        def complete(prompts: list[str], *, route: str, spend: Any = None, **kw: Any) -> Any:
            spend = spend if spend is not None else J.new_spend()
            c0, i0, o0, u0 = spend.calls, spend.input_tokens, spend.output_tokens, spend.usd
            try:
                return J.default_complete()(prompts, route=route, spend=spend, **kw)
            finally:
                self.add(route, prompts=len(prompts), calls=spend.calls - c0,
                         input_tokens=spend.input_tokens - i0,
                         output_tokens=spend.output_tokens - o0, usd=spend.usd - u0)
        return complete

    @staticmethod
    def journal_sizes() -> dict[Path, int]:
        return {p: len(p.read_text(encoding="utf-8").splitlines())
                for p in sorted(C.CALLS_DIR.glob("*.jsonl"))}

    def add_journals(self, before: dict[Path, int]) -> None:
        """textllm journal lines appended since ``before`` (one line per prompt)."""
        for p, n_now in self.journal_sizes().items():
            for line in p.read_text(encoding="utf-8").splitlines()[before.get(p, 0):n_now]:
                r = json.loads(line)
                self.add(r["model"], prompts=1, calls=r.get("responses", 0),
                         input_tokens=r.get("input_tokens", 0),
                         output_tokens=r.get("output_tokens", 0), usd=r.get("usd", 0.0))

    def summary(self) -> dict[str, dict[str, Any]]:
        out = {}
        for model, m in sorted(self.by_model.items()):
            def per(x: float, n: float) -> float | None:
                return round(x / n, 8) if n else None
            out[model] = {**m, "usd": round(m["usd"], 8),
                          "input_tokens_per_call": per(m["input_tokens"], m["calls"]),
                          "output_tokens_per_call": per(m["output_tokens"], m["calls"]),
                          "usd_per_call": per(m["usd"], m["calls"]),
                          "usd_per_prompt": per(m["usd"], m["prompts"])}
        return out


def pilot_json(step: str) -> Path:
    return C.RESULTS / "pilots" / f"{step}_pilot.json"


def pilot_manifest(step: str, a: argparse.Namespace, inputs: list[Path], meter: Meter,
                   started: str, spend: Any, proj: list[dict], *,
                   outputs: list[Path] | None = None, extra: dict | None = None) -> Path:
    """The token pilot's record: ``<step>_pilot`` manifest plus results/I9/pilots/<step>_pilot.json
    (its output: measured calls, tokens and cost per call by model, and the projection), plus
    any further pilot ``outputs`` (the A-C pilot's subset readouts and judged files)."""
    body = {"step": step, "limit": a.limit, "measured": meter.summary(), "projection": proj,
            "spend": getattr(spend, "report", lambda: "")(), "utc": utc(), **(extra or {})}
    out = pilot_json(step)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    more = [p for p in (outputs or []) if Path(p).exists()]
    path = write_manifest(f"{step}_pilot", inputs, [out, *more], started, spend,
                          notes=json.dumps({"limit": a.limit, "measured": body["measured"]}))
    print(f"pilot measured: {json.dumps(body['measured'])}")
    print(path)
    return path


def write_rows_merged(path: Path, rows: list[C.Prediction], target: str,
                      ids: list[str]) -> list[C.FileRef]:
    """Merge by scenario id (code review r3 finding 1): replace this target's rows for the
    scenarios this run covered (``ids``, every one asked for, so a dropped prediction leaves no
    old row behind), keep every other row (the other target's, and this target's other
    scenarios, e.g. the confirmatory rows when only the sealed batch is run). Returns the
    file's pre-merge FileRef (empty if new): an input of the step's manifest."""
    prior = [file_ref(path)] if path.exists() else []
    old = J.read_rows(path, C.Prediction) if path.exists() else []
    done = set(ids)
    J.write_rows(path, [p for p in old if p.target_model != target or p.scenario_id not in done]
                 + rows)
    return prior


def set_ids(spec: str) -> tuple[list[str], list[str]]:
    """``--set``: one sets.json key or a comma list (``confirmatory,sealed`` = the 80-scenario
    confirmatory set after an unseal) -> (keys, ids in sets order). Overlapping keys refused."""
    keys = [k.strip() for k in spec.split(",") if k.strip()]
    bad = [k for k in keys if k not in C.SET_KEYS]
    if not keys or bad:
        raise SystemExit(f"--set {spec!r}: unknown key(s) {bad}; known {list(C.SET_KEYS)}")
    sets = load_sets()
    ids: list[str] = []
    for k in keys:
        ids += sets[k]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"--set {spec!r}: the keys overlap")
    return keys, ids


def monitor_step(kind: str, target: str, keys: list[str]) -> str:
    """``monitor_<kind>_<target>`` for the confirmatory set, else with the set keys appended
    (e.g. ``monitor_few_qwen_confirmatory_sealed``), so runs on other sets keep their own
    manifests."""
    base = f"monitor_{kind}_{target}"
    return base if keys == ["confirmatory"] else f"{base}_{'_'.join(keys)}"


# --- misbehavior -------------------------------------------------------------------------------


def cmd_misbehavior(a: argparse.Namespace) -> int:
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    paths = [Path(p) for p in a.replies] if a.replies else sorted(C.REPLIES_DIR.glob("*.jsonl"))
    replies = [r for p in paths for r in J.read_rows(p, C.Reply)]
    keep = set(first_ids(sorted({r.scenario_id for r in replies}), a.limit))
    replies = [r for r in replies if r.scenario_id in keep]
    print(f"{len(replies)} replies from {len(paths)} files, {len(keep)} scenarios")
    pc: dict = {}
    proj = []
    for j in J.PRIMARY_JUDGES:
        calls = [J.judge_call(r, scen[r.scenario_id], j, pc) for r in replies]
        proj.append(J.project_cost(calls, j, 200, J.CallCache(J.cache_path("misbehavior", j))))
    glm = [J.judge_call(r, scen[r.scenario_id], J.TIEBREAK_JUDGE, pc)
           for r in replies[: int(len(replies) * GLM_SHARE)]]
    proj.append({**J.project_cost(glm, J.TIEBREAK_JUDGE, 200), "note": "~10% tie-breaks"})
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    spend, meter = J.new_spend(), Meter()
    res = J.judge_replies(replies, scen, spend=spend, complete=meter.wrap())
    agree = J.judge_agreement(*(res.verdicts[j] for j in J.PRIMARY_JUDGES))
    done = res.completeness()
    print(f"luna-DeepSeek kappa {agree}; GLM tie-break calls {res.n_tiebreak_calls}; "
          f"rules {J.counter_summary(res.labels)}; re-judged {done['unresolved_first_round']}, "
          f"resolved on re-judge {done['resolved_on_rejudge']}, "
          f"still unlabelled {done['unlabelled']}")
    print(spend.report())
    if a.limit:
        pilot_manifest("judge_misbehavior", a, [Path(a.scenarios), *paths], meter, started,
                       spend, proj)
        return 0
    outs = []
    for j, vs in res.verdicts.items():
        outs.append(C.verdicts_path(j))
        J.write_rows(outs[-1], vs.values())
    outs.append(C.VERDICTS_DIR / "rejudge.jsonl")  # the one re-judging round, every judge
    J.write_rows(outs[-1], [v for vs in res.rejudge.values() for v in vs.values()])
    for path, rows in ((C.CODE_CHECKS_PATH, res.checks.values()),
                       (C.LABELS_PATH, res.labels.values()),
                       (C.LABELS_LLM_ONLY_PATH, res.labels_llm_only.values()),
                       (C.RATES_PATH, res.rates("final")),
                       (C.RATES_LLM_ONLY_PATH, res.rates("llm_only"))):
        J.write_rows(path, rows)
        outs.append(path)
    # amendment B item 2: how often the code check overrules the LLM consensus (informational)
    ovr = J.code_check_overrides(res, scen)
    ovr_p = C.check_path("code_check_overrides")
    ovr_p.parent.mkdir(parents=True, exist_ok=True)
    ovr_p.write_text(json.dumps({"name": "code_check_overrides", "kind": "informational",
                                 **ovr, "utc": utc()}, indent=2, sort_keys=True) + "\n")
    outs.append(ovr_p)
    print(f"code check overrules the consensus on {ovr['totals']['overruled']} of "
          f"{ovr['totals']['consensus_positive']} consensus-positive Lynch replies -> {ovr_p}")
    print(write_manifest("judge_misbehavior", [Path(a.scenarios), *paths], outs, started, spend,
                         notes=json.dumps({"agreement": agree, **done,
                                           "tiebreak_calls": res.n_tiebreak_calls,
                                           "measured": meter.summary()})))
    return 0


# --- hand labels -------------------------------------------------------------------------------


def _verdict_map(judge: str) -> dict:
    return {J.reply_key(v): v for v in J.read_rows(C.verdicts_path(judge), C.Verdict)}


def cmd_handlabel_bundle(a: argparse.Namespace) -> int:
    started = utc()
    bundle_p, key_p = C.HANDLABEL_DIR / "bundle.csv", C.HANDLABEL_DIR / "key.csv"
    if a.dry_run:
        print("hand-label bundle: no model calls ($0)")
        return 0
    if bundle_p.exists() and not a.force:
        print(f"{bundle_p} exists (it may hold Jacob's labels); pass --force", file=sys.stderr)
        return 2
    scen = J.load_scenarios(Path(a.scenarios))
    paths = [Path(p) for p in a.replies] if a.replies else sorted(C.REPLIES_DIR.glob("*.jsonl"))
    replies = [r for p in paths for r in J.read_rows(p, C.Reply)]
    labels = {J.reply_key(x): x for x in J.read_rows(C.LABELS_PATH, C.Label)}
    bundle, key = J.make_handlabel_bundle(replies, scen, _verdict_map(C.LUNA),
                                          _verdict_map(C.DEEPSEEK), labels, seed=a.seed)
    J.write_csv(bundle_p, bundle, J.BUNDLE_COLUMNS)
    J.write_csv(key_p, key, J.KEY_COLUMNS)
    print(f"{len(bundle)} rows ({sum(k['disputed'] for k in key)} disputed) -> {bundle_p}; "
          f"PRIVATE key -> {key_p} (don't open before labelling)")
    write_manifest("handlabel_bundle", [Path(a.scenarios), *paths, C.LABELS_PATH], [bundle_p,
                   key_p], started, None, seeds={"handlabel": a.seed})
    return 0


def cmd_handlabel_score(a: argparse.Namespace) -> int:
    started = utc()
    if a.dry_run:
        print("hand-label score: no model calls ($0)")
        return 0
    # Jacob fills a copy (labels_jacob.csv), never bundle.csv itself, so the bundle keeps the hash
    # its manifest recorded (implementation plan §6 chain check).
    labels = Path(a.labels) if a.labels else C.HANDLABEL_DIR / "labels_jacob.csv"
    key = Path(a.key) if a.key else C.HANDLABEL_DIR / "key.csv"
    chk = J.score_handlabels(J.read_csv(labels), J.read_csv(key))
    chk.utc = utc()
    print(json.dumps({"passed": chk.passed, "value": chk.value, **chk.details}, default=str))
    out = chk.write()
    print(out)
    print(write_manifest("handlabel_score", [labels, key], [out], started, None,
                         notes=json.dumps({"passed": chk.passed, "value": chk.value})))
    return 0 if chk.passed else 3


# --- adjudication of replies unresolved after the re-judge (amendment B review r2) -------------

ADJ_BUNDLE, ADJ_KEY, ADJ_FILLED = ("adjudicate_bundle.csv", "adjudicate_key.csv",
                                   "adjudicate_jacob.csv")


def _reply_paths(a: argparse.Namespace) -> list[Path]:
    return [Path(p) for p in a.replies] if a.replies else sorted(C.REPLIES_DIR.glob("*.jsonl"))


def cmd_adjudicate_bundle(a: argparse.Namespace) -> int:
    """Blind CSV of the replies still without a label after the re-judge, for Jacob. More than
    ``judging.ADJUDICATE_MAX`` of them: refuse (exit 3), stop and ask Jacob. No model calls."""
    started = utc()
    if a.dry_run:
        print("adjudicate bundle: no model calls ($0)")
        return 0
    bundle_p, key_p = C.HANDLABEL_DIR / ADJ_BUNDLE, C.HANDLABEL_DIR / ADJ_KEY
    if not C.LABELS_PATH.exists():
        print(f"{C.LABELS_PATH} missing: run `misbehavior` first", file=sys.stderr)
        return 2
    scen = J.load_scenarios(Path(a.scenarios))
    paths = _reply_paths(a)
    replies = [r for p in paths for r in J.read_rows(p, C.Reply)]
    todo = J.unlabelled_replies(replies, J.read_rows(C.LABELS_PATH, C.Label))
    if len(todo) > J.ADJUDICATE_MAX:
        print(f"{len(todo)} unresolved replies > {J.ADJUDICATE_MAX}: stop and ask Jacob. "
              "Nothing written.", file=sys.stderr)
        return 3
    if not todo:
        print("no unresolved replies: nothing to adjudicate")
        return 0
    if bundle_p.exists() and not a.force:
        print(f"{bundle_p} exists; pass --force", file=sys.stderr)
        return 2
    bundle, key = J.make_adjudicate_bundle(todo, scen, seed=a.seed)
    J.write_csv(bundle_p, bundle, J.BUNDLE_COLUMNS)
    J.write_csv(key_p, key, J.ADJ_KEY_COLUMNS)
    print(f"{len(bundle)} unresolved replies -> {bundle_p}; fill a copy as "
          f"{C.HANDLABEL_DIR / ADJ_FILLED}; PRIVATE key -> {key_p}")
    print(write_manifest("adjudicate_bundle", [Path(a.scenarios), *paths, C.LABELS_PATH],
                         [bundle_p, key_p], started, None, seeds={"adjudicate": a.seed},
                         notes=json.dumps({"n_unresolved": len(todo)})))
    return 0


def _verdicts_used() -> dict[str, dict]:
    """Each primary judge's verdicts as the labels used them (re-judge verdicts win)."""
    used = {j: _verdict_map(j) for j in J.PRIMARY_JUDGES}
    rej = C.VERDICTS_DIR / "rejudge.jsonl"
    for v in (J.read_rows(rej, C.Verdict) if rej.exists() else []):
        if v.judge in used:
            used[v.judge][J.reply_key(v)] = v
    return used


def cmd_adjudicate_score(a: argparse.Namespace) -> int:
    """Jacob's adjudications -> final and LLM-only labels (rule "adjudicated"; final = his
    verdict AND the Lynch code check), then both rate files rewritten. No model calls."""
    started = utc()
    if a.dry_run:
        print("adjudicate score: no model calls ($0)")
        return 0
    filled = Path(a.labels) if a.labels else C.HANDLABEL_DIR / ADJ_FILLED
    key = Path(a.key) if a.key else C.HANDLABEL_DIR / ADJ_KEY
    scen = J.load_scenarios(Path(a.scenarios))
    paths = _reply_paths(a)
    replies = [r for p in paths for r in J.read_rows(p, C.Reply)]
    # the pre-rewrite labels and rates (r3 finding 3): inputs of this step's manifest, so the
    # chain check can tie the judge's outputs to this rewrite
    rewritten = (C.LABELS_PATH, C.LABELS_LLM_ONLY_PATH, C.RATES_PATH, C.RATES_LLM_ONLY_PATH)
    prior = [file_ref(p) for p in rewritten if p.exists()]
    old = {J.reply_key(x): x for x in J.read_rows(C.LABELS_PATH, C.Label)}
    old_llm = {J.reply_key(x): x for x in J.read_rows(C.LABELS_LLM_ONLY_PATH, C.Label)}
    final, llm, new_checks = J.apply_adjudication(J.read_csv(filled), J.read_csv(key), replies,
                                                  scen, old, old_llm)
    checks = {J.reply_key(c): c for c in (J.read_rows(C.CODE_CHECKS_PATH, J.CodeCheck)
                                          if C.CODE_CHECKS_PATH.exists() else [])}
    checks.update(new_checks)
    used = _verdicts_used()
    outs = []
    for path, rows in ((C.LABELS_PATH, final.values()), (C.LABELS_LLM_ONLY_PATH, llm.values()),
                       (C.RATES_PATH, J.rates(final.values(), used, checks=checks)),
                       (C.RATES_LLM_ONLY_PATH, J.rates(llm.values(), used))):
        J.write_rows(path, rows)
        outs.append(path)
    n = sum(lab.rule == J.ADJUDICATED for lab in llm.values())
    print(f"{n} adjudicated labels; {len(replies) - len(final)} replies still unlabelled")
    inputs = [filled, key, Path(a.scenarios), *paths, C.CODE_CHECKS_PATH,
              *(C.verdicts_path(j) for j in J.PRIMARY_JUDGES), C.VERDICTS_DIR / "rejudge.jsonl"]
    print(write_manifest("adjudicate_score", inputs, outs, started, None, prior=prior,
                         notes=json.dumps({"n_adjudicated": n,
                                           "n_unlabelled": len(replies) - len(final)})))
    return 0


# --- luna-vs-Sonnet gate -----------------------------------------------------------------------


def _family(path: str | None) -> Any:
    if not path:
        return None
    from wsbench.results import FamilyResult

    return FamilyResult.from_json(json.loads(Path(path).read_text()))


def cmd_validate(a: argparse.Namespace) -> int:
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    ids = first_ids(load_sets()["bank_validation"], a.limit)
    sonnet = _family(a.sonnet)
    by_effort = {e: fr for e, fr in (("none", _family(a.luna_none)), ("low", _family(a.luna_low)))
                 if fr is not None}
    if sonnet is None or not by_effort:
        print("validate needs the FamilyResult files (--sonnet and --luna-none/--luna-low); "
              "prereg §7 estimate: Sonnet scoring of luna's 16 accounts ~$1.0, DeepSeek "
              "same-action 16 calls < $0.05. No calls made.")
        return 0 if a.dry_run else 2
    effort, ks = J.pick_bc_effort(by_effort, sonnet, ids=ids)
    luna = by_effort[effort]
    lrows = {r["id"]: r for r in luna.rows}
    accounts = {i: lrows.get(i, {}).get("account") for i in ids}
    caches = [J.cache_path("validation_score", C.SONNET), J.cache_path("same_action", C.DEEPSEEK)]
    calls = M.score_calls(accounts, scen)
    same, _ = J.same_action_calls({i: accounts[i] or "" for i in ids},
                                  {r["id"]: r.get("account") or "" for r in sonnet.rows}, ids)
    proj = [J.project_cost(calls, C.SONNET, M.OUT_TOKENS["score"], J.CallCache(caches[0])),
            J.project_cost(same, C.DEEPSEEK, 300, J.CallCache(caches[1]))]
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    families = [Path(x) for x in (a.luna_none, a.luna_low, a.sonnet) if x]
    inputs = [Path(a.scenarios), C.SETS_PATH, *families]
    spend, meter = J.new_spend(), Meter()
    son_on_luna = M.score_accounts(accounts, scen, scorer=C.SONNET, spend=spend,
                                   purpose="validation_score", complete=meter.wrap())
    checks = J.validation_gate(luna, sonnet, son_on_luna, ids=ids, spend=spend,
                               complete=meter.wrap())
    pilot = C.CheckResult(name="bc_effort_pilot", passed=None if not ks else True,
                          value=ks.get(effort), threshold=None, n=len(ids),
                          details={"chosen": effort, "wkappa_commit_by_effort": ks},
                          notes="the effort with the higher weighted kappa on commitment "
                                "strength against Sonnet; ties keep 'none'", utc=utc())
    outs = []
    for chk in [pilot, *checks]:
        chk.utc = utc()
        if not a.limit:
            outs.append(chk.write())
        print(chk.name, chk.passed, chk.value)
    print(spend.report())
    if a.limit:
        pilot_manifest("validate", a, [*inputs, *caches], meter, started, spend, proj)
        return 0
    # inputs: what the checks read -- scenarios, sets, the FamilyResults (luna's and Sonnet's
    # accounts and Stage C rows) and the call caches the Sonnet scoring and same-action calls used
    print(write_manifest("validate", [*inputs, *caches], outs, started, spend,
                         notes=json.dumps({"bc_effort": effort, "n_items": len(ids),
                                           "overall": checks[-1].passed,
                                           "measured": meter.summary()})))
    return 0 if checks[-1].passed else 3


# --- WorkspaceBench Stages A-C on reader output (P4, P5) ----------------------------------------

JUDGE_ROUTES = {"luna": C.LUNA, "sonnet": C.SONNET}


def _ac_layers(reader: str, layers: str | None) -> tuple[int, ...]:
    """Expected layers: --layers, else the ladder (token readers) or the Oracle Lens layers in
    effect (contracts.RUN_CONFIG_PATH "olens_layers" if present, else contracts.OLENS_LAYERS)."""
    if layers:
        return tuple(int(x) for x in layers.split(","))
    if reader != "olens":
        return C.LADDER
    if C.RUN_CONFIG_PATH.exists():
        cfg = json.loads(C.RUN_CONFIG_PATH.read_text())
        if "olens_layers" in cfg:
            return tuple(int(x) for x in cfg["olens_layers"])
    return C.OLENS_LAYERS


def ac_stage_costs(before: dict[Path, int]) -> dict[str, dict[str, float]]:
    """Per-stage prompts and dollars from textllm journal lines appended since ``before`` (the
    stage from each line's batch ``max_tokens``), with ``usd_per_prompt``: the A-C pilot's
    measured cost, which later projections of the same step use."""
    from emx.i9 import wsjudge

    out: dict[str, dict[str, float]] = {}
    for p, n_now in Meter.journal_sizes().items():
        for line in p.read_text(encoding="utf-8").splitlines()[before.get(p, 0):n_now]:
            r = json.loads(line)
            stage = wsjudge.stage_of_max_tokens(r.get("max_tokens", -1)) or "unknown"
            m = out.setdefault(stage, {"prompts": 0, "usd": 0.0})
            m["prompts"] += 1
            m["usd"] += float(r.get("usd", 0.0))
    for m in out.values():
        m["usd_per_prompt"] = m["usd"] / m["prompts"] if m["prompts"] else None
    return out


def cmd_ac(a: argparse.Namespace) -> int:
    """One reader's readouts on one set through the unchanged wsbench A-C judge
    (emx.i9.wsjudge.run_family), with a call and cost projection before the paid gate, an exact
    readouts check first and a manifest after.

    ``--limit N`` (the paid pilot, code review r2 finding 2) judges only the first N items: it
    writes their rows of the readouts file to results/I9/pilots/<step>_pilot/readouts/, judges
    that subset into results/I9/pilots/<step>_pilot/judged/ and records a ``<step>_pilot``
    manifest with the measured cost per call by stage; it never writes the real judged
    directory or manifest. A later projection of the same step uses that measured cost."""
    from emx.i9 import acts, readers, wsjudge

    started = utc()
    route = JUDGE_ROUTES[a.judge]
    if route == C.LUNA and a.bc_effort is None:
        raise SystemExit("--bc-effort is required for luna (the B/C pilot's choice)")
    bc = a.bc_effort if route == C.LUNA else None
    effort = {False: C.STAGE_A_EFFORT, True: a.bc_effort} if route == C.LUNA else None
    scen = J.load_scenarios(Path(a.scenarios))
    ids = first_ids(load_sets()[a.set], a.limit)
    readouts = Path(a.readouts) if a.readouts else C.readouts_path(
        readers.readout_name(a.reader, a.set))
    index = acts.index_path(Path(a.store) if a.store else acts.last64_path())
    _, sites = acts.load_index(Path(a.store) if a.store else acts.last64_path())
    layers = _ac_layers(a.reader, a.layers)
    want = set(ids)
    expected = {(s.id, s.pos, layer) for s in sites if s.id in want for layer in layers}
    label = C.judge_label(route, bc)
    step = f"judge_ac_{label.replace(':', '_')}_{readouts.stem}"
    if a.limit:
        if a.out:
            raise SystemExit("--out is not used with --limit: the pilot writes under "
                             f"{C.RESULTS / 'pilots' / (step + '_pilot')}")
        pilot_dir = C.RESULTS / "pilots" / f"{step}_pilot"
        used = pilot_dir / "readouts" / readouts.name  # same stem: same purpose and cache labels
        out = pilot_dir / "judged"
    else:
        used = readouts
        out = Path(a.out) if a.out else C.judged_dir(label, readouts.stem)
    print(f"ac {a.reader} on {a.set}: {len(ids)} items, {len(expected)} expected cells, "
          f"layers {list(layers)}, judge {label} -> {out}")
    pj = pilot_json(step)
    measured = json.loads(pj.read_text()).get("by_stage") if pj.exists() else None
    items = [scen[i] for i in ids]
    proj = wsjudge.project_ac(items, readouts, expected, route=route, prose=a.reader == "olens",
                              measured=measured)
    print(f"A-C projection basis: {'pilot ' + str(pj) if measured else 'assumed tokens'}; "
          f"ceiling (vendored max_tokens) ${sum(r['usd_ceiling'] for r in proj):.4f}")
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    if a.limit:
        if not readouts.is_file():
            print(f"refused: the pilot needs the readouts file {readouts}. No calls made.",
                  file=sys.stderr)
            return 2
        n = wsjudge.subset_readouts(readouts, used, want)
        print(f"pilot readouts: {n} rows of {len(ids)} items -> {used}")
    inputs = [readouts, index, Path(a.scenarios), C.SETS_PATH]
    meter, before = Meter(), Meter.journal_sizes()
    try:
        res = wsjudge.run_family(items, used, route=route, expected=expected, bc_effort=bc,
                                 effort_by_thinking=effort, out=out)
    finally:
        meter.add_journals(before)
    outputs = [out / "results.json", out / "cells.jsonl"]
    if a.limit:
        usd = sum(m["usd"] for m in meter.by_model.values())
        pilot_manifest(step, a, [*inputs, used], meter, started, SimpleNamespace(usd=usd), proj,
                       outputs=[used, *outputs], extra={"by_stage": ac_stage_costs(before)})
        return 0
    usage = dict(getattr(res, "usage", {}) or {})
    write_manifest(step, inputs, [p for p in outputs if p.exists()], started, None,
                   notes=f"reader={a.reader} set={a.set} layers={list(layers)} n_items={len(ids)}"
                         f" usage={usage} (dollars: logs/costs.jsonl, recorded by textllm)")
    print(f"ac {label} {readouts.stem}: {usage}")
    return 0


def cmd_rescore_c(a: argparse.Namespace) -> int:
    """Prereg §4.3: Stage C rerun on every account with DeepSeek V4 Flash (a second-family
    check on the reader scores). Accounts come from a luna FamilyResult; output is one JSON of
    id -> parsed Stage C fields, plus a manifest."""
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    fr = _family(a.results)
    accounts = {r["id"]: r.get("account") for r in fr.rows}
    if a.limit:
        accounts = {k: accounts[k] for k in first_ids(sorted(accounts), a.limit)}
    proj = [J.project_cost(M.score_calls(accounts, scen), C.DEEPSEEK, M.OUT_TOKENS["score"],
                           J.CallCache(J.cache_path("rescore_c", C.DEEPSEEK)))]
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    spend, meter = J.new_spend(), Meter()
    scored = M.score_accounts(accounts, scen, scorer=C.DEEPSEEK, spend=spend,
                              purpose="rescore_c", complete=meter.wrap())
    step = f"rescore_c_{Path(a.results).parent.name}"
    if a.limit:
        pilot_manifest(step, a, [Path(a.results), Path(a.scenarios)], meter, started, spend, proj)
        return 0
    out = Path(a.out) if a.out else Path(a.results).with_name("rescore_c_deepseek.json")
    out.write_text(json.dumps(scored, indent=2, sort_keys=True) + "\n")
    write_manifest(step, [Path(a.results), Path(a.scenarios)], [out], started, spend,
                   notes=json.dumps({"n_accounts": len(accounts), "measured": meter.summary()}))
    print(spend.report())
    return 0


# --- Stage 0 -----------------------------------------------------------------------------------


def cmd_stage0(a: argparse.Namespace) -> int:
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    bank = [scen[i] for i in first_ids(load_sets()["bank"], a.limit)]
    acc = M.account_calls(bank)
    fake = {s.id: "x" * (M.OUT_TOKENS["account"] * 4) for s in bank}
    score = M.score_calls(fake, scen)
    proj = [J.project_cost(acc, C.GEMINI, M.OUT_TOKENS["account"]),
            *(J.project_cost(score, sc, M.OUT_TOKENS["score"]) for sc in (C.SONNET, C.LUNA))]
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    spend, meter = J.new_spend(), Meter()
    out = M.stage0(bank, reasoning={C.LUNA: a.luna_effort}, spend=spend, complete=meter.wrap())
    for sc, r in out["scorers"].items():
        agg = r["aggregate"] or {}
        print(f"{sc}: design_score {agg.get('design_score')} ci95 {agg.get('design_score_ci95')} "
              f"reaches {r['reaches_threshold']} (threshold {C.STAGE0_THRESHOLD})")
    print(spend.report())
    if a.limit:
        pilot_manifest("stage0", a, [Path(a.scenarios), C.SETS_PATH], meter, started, spend, proj)
        return 0
    s0 = M.stage0_dir()
    s0.mkdir(parents=True, exist_ok=True)
    acc_p, sum_p = s0 / "accounts.jsonl", s0 / "stage0.json"
    acc_p.write_text("".join(json.dumps({"id": k, "account": v}, ensure_ascii=False) + "\n"
                             for k, v in out["accounts"].items()))
    sum_p.write_text(json.dumps({**out, "accounts": None, "utc": utc()}, indent=2) + "\n")
    print(write_manifest("stage0", [Path(a.scenarios), C.SETS_PATH], [acc_p, sum_p], started,
                         spend, notes=json.dumps({"measured": meter.summary()})))
    return 0


# --- rate monitors -----------------------------------------------------------------------------


def cmd_monitor_zero(a: argparse.Namespace) -> int:
    """Zero-shot monitor on ``--set`` (one key or ``confirmatory,sealed``); rows merged into
    predictions/monitor_zero_shot.jsonl by scenario id (no folds: a sealed-only run after an
    unseal completes the 80)."""
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    keys, all_ids = set_ids(a.set)
    items = [scen[i] for i in first_ids(all_ids, a.limit)]
    calls = M.zero_shot_calls(items, a.target)
    proj = [J.project_cost(calls, C.GEMINI, M.OUT_TOKENS["monitor"],
                           J.CallCache(J.cache_path("monitor_zero", C.GEMINI)))]
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    spend, meter = J.new_spend(), Meter()
    preds = M.monitor_zero_shot(items, target=a.target, spend=spend, complete=meter.wrap())
    print(f"{len(preds)} of {len(items)} predictions; {spend.report()}")
    step = monitor_step("zero", a.target, keys)
    if a.limit:
        pilot_manifest(step, a, [Path(a.scenarios), C.SETS_PATH], meter, started, spend, proj)
        return 0
    path = C.predictions_path("monitor_zero_shot")
    prior = write_rows_merged(path, preds, a.target, [s.id for s in items])
    write_manifest(step, [Path(a.scenarios), C.SETS_PATH], [path], started, spend, prior=prior,
                   notes=json.dumps({"measured": meter.summary(), "set": a.set,
                                     "n_scenarios": len(items), "n_predictions": len(preds)}))
    return 0


def _fold_ids(folds: dict[str, dict[str, int]]) -> set[str] | None:
    """The scenario ids every fold repeat assigns (None if the repeats disagree)."""
    sets = [set(f) for f in folds.values()]
    return sets[0] if sets and all(x == sets[0] for x in sets) else None


def cmd_monitor_few(a: argparse.Namespace) -> int:
    """Few-shot monitor on ``--set``. The folds (checkpoint 2's analysis/folds.json) must assign
    exactly the run's scenarios: after an unseal that is ``--set confirmatory,sealed`` with the
    80-scenario folds, in one run, since the folds (and so every example pool) change. Rows are
    merged by scenario id."""
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    keys, all_ids = set_ids(a.set)
    folds = json.loads(Path(a.folds).read_text())
    fold_ids = _fold_ids(folds)
    if fold_ids != set(all_ids):
        print(f"refused: {a.folds} assigns "
              f"{'inconsistent ids across repeats' if fold_ids is None else len(fold_ids)} "
              f"scenarios, but --set {a.set} has {len(all_ids)}; the folds must be those of "
              "this exact set (after an unseal: --set confirmatory,sealed with checkpoint2 "
              "--unsealed's folds). No calls made.", file=sys.stderr)
        return 2
    ids = first_ids(all_ids, a.limit)
    tasks = M.tasks_from_folds(folds, ids)
    rates_p = Path(a.rates)
    rates = {r.scenario_id: r for r in (J.read_rows(rates_p, C.Rate) if rates_p.exists() else [])
             if r.model == a.target and not r.thinking}
    sizing = rates or {s: C.Rate(a.target, False, s, 0, C.N_SAMPLES, {}) for s in scen}
    calls, _ = M.few_shot_calls(tasks, scen, sizing, a.target)  # placeholder rates only size
    proj = [J.project_cost(calls, C.GEMINI, M.OUT_TOKENS["monitor"],
                           J.CallCache(J.cache_path("monitor_few", C.GEMINI)))]
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    if not rates:
        print(f"no thinking-off rates for {a.target} in {rates_p}; no calls made", file=sys.stderr)
        return 2
    spend, meter = J.new_spend(), Meter()
    preds = M.monitor_few_shot(tasks, scen, rates, target=a.target, spend=spend,
                               complete=meter.wrap())
    print(f"{len(preds)} of {len(tasks)} predictions; {spend.report()}")
    step = monitor_step("few", a.target, keys)
    inputs = [Path(a.scenarios), C.SETS_PATH, Path(a.folds), rates_p, M.FEW_PROMPT]
    if a.limit:
        pilot_manifest(step, a, inputs, meter, started, spend, proj)
        return 0
    path = C.predictions_path("monitor_few_shot")
    prior = write_rows_merged(path, preds, a.target, ids)
    write_manifest(step, inputs, [path], started, spend, prior=prior,
                   notes=json.dumps({"measured": meter.summary(),
                                     "example_pool_rule": M.EXAMPLE_POOL_RULE, "set": a.set,
                                     "n_scenarios": len(ids), "n_predictions": len(preds)}))
    return 0


def cmd_monitor_cot(a: argparse.Namespace) -> int:
    started = utc()
    scen = J.load_scenarios(Path(a.scenarios))
    rp = Path(a.replies[0]) if a.replies else C.replies_path("qwen", True)
    replies = J.read_rows(rp, C.Reply)
    keep = set(first_ids(sorted({r.scenario_id for r in replies}), a.limit))
    replies = [r for r in replies if r.scenario_id in keep]
    calls = M.cot_calls(replies, scen)
    proj = [J.project_cost(calls, C.GEMINI, M.OUT_TOKENS["monitor"],
                           J.CallCache(J.cache_path("monitor_cot", C.GEMINI)))]
    stop = paid_gate(a, proj)
    if stop is not None:
        return stop
    spend, meter = J.new_spend(), Meter()
    preds, counts = M.monitor_cot(replies, scen, spend=spend, complete=meter.wrap())
    print(f"{len(preds)} scenario predictions; {spend.report()}")
    if a.limit:
        pilot_manifest("monitor_cot", a, [Path(a.scenarios), rp], meter, started, spend, proj)
        return 0
    path = C.predictions_path("monitor_cot")
    J.write_rows(path, preds)
    write_manifest("monitor_cot", [Path(a.scenarios), rp], [path], started, spend,
                   notes=json.dumps({**counts, "measured": meter.summary()}))
    return 0


# --- argparse ----------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, paid: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--scenarios", default=str(C.SCENARIOS_PATH))
        if paid:
            p.add_argument("--limit", type=int, default=0,
                           help="first N scenarios: the PAID token pilot (needs --confirm-paid)")
            p.add_argument("--confirm-paid", action="store_true",
                           help="allow paid model calls (without it: projection, then refuse)")
        else:
            p.set_defaults(limit=0, confirm_paid=False)
        return p

    p = add("misbehavior")
    p.add_argument("--replies", nargs="*", help="Reply JSONL files (default: all in replies/)")
    p = add("handlabel-bundle", paid=False)
    p.add_argument("--replies", nargs="*")
    p.add_argument("--seed", type=int, default=C.HANDLABEL_SEED)
    p.add_argument("--force", action="store_true")
    p = add("handlabel-score", paid=False)
    p.add_argument("--labels", default=None,
                   help="default: contracts.HANDLABEL_DIR/labels_jacob.csv (a filled copy of bundle.csv)")
    p.add_argument("--key", default=None, help="default: contracts.HANDLABEL_DIR/key.csv")
    for name in ("adjudicate-bundle", "adjudicate-score"):
        p = add(name, paid=False)
        p.add_argument("--replies", nargs="*", help="Reply JSONL files (default: all in replies/)")
        if name == "adjudicate-bundle":
            p.add_argument("--seed", type=int, default=C.HANDLABEL_SEED)
            p.add_argument("--force", action="store_true")
        else:
            p.add_argument("--labels", default=None,
                           help=f"default: contracts.HANDLABEL_DIR/{ADJ_FILLED} (a filled copy)")
            p.add_argument("--key", default=None, help=f"default: contracts.HANDLABEL_DIR/{ADJ_KEY}")
    p = add("validate")
    p.add_argument("--luna-none", help="FamilyResult JSON of luna, B/C effort none")
    p.add_argument("--luna-low", help="FamilyResult JSON of luna, B/C effort low")
    p.add_argument("--sonnet", help="FamilyResult JSON of Sonnet 5")
    p = add("stage0")
    p.add_argument("--luna-effort", default="none", choices=C.BC_EFFORT_CHOICES)
    for name in ("monitor-zero", "monitor-few"):
        p = add(name)
        p.add_argument("--target", default="qwen", choices=C.MODELS)
        p.add_argument("--set", default="confirmatory",
                       help="a sets.json key or a comma list; after an unseal "
                            "'confirmatory,sealed' (the 80-scenario confirmatory set)")
        if name == "monitor-few":
            p.add_argument("--folds", required=True,
                           help='JSON {"<fold_repeat>": {"<scenario_id>": fold}}')
            p.add_argument("--rates", default=str(C.RATES_PATH))
    p = add("ac")
    p.add_argument("--reader", required=True, choices=("logit_lens", "jlens", "olens"))
    p.add_argument("--set", default="confirmatory", choices=C.SET_KEYS)
    p.add_argument("--judge", required=True, choices=tuple(JUDGE_ROUTES))
    p.add_argument("--bc-effort", choices=C.BC_EFFORT_CHOICES, help="luna only")
    p.add_argument("--readouts", help="default: readouts/<reader>[_<set>].jsonl")
    p.add_argument("--store", help="default: acts/qwen_last64.safetensors (its .json index)")
    p.add_argument("--layers", help="comma list; default: the ladder, or the Oracle Lens layers")
    p.add_argument("--out", help="default: contracts.judged_dir(label, readouts stem)")
    p = add("rescore-c")
    p.add_argument("--results", required=True, help="luna FamilyResult results.json")
    p.add_argument("--out")
    p = add("monitor-cot")
    p.add_argument("--replies", nargs="*", help="default: replies/qwen_think.jsonl")
    return ap.parse_args(argv)


COMMANDS = {
    "misbehavior": cmd_misbehavior, "handlabel-bundle": cmd_handlabel_bundle,
    "handlabel-score": cmd_handlabel_score, "adjudicate-bundle": cmd_adjudicate_bundle,
    "adjudicate-score": cmd_adjudicate_score, "validate": cmd_validate, "stage0": cmd_stage0,
    "monitor-zero": cmd_monitor_zero, "monitor-few": cmd_monitor_few,
    "monitor-cot": cmd_monitor_cot, "ac": cmd_ac, "rescore-c": cmd_rescore_c,
}


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    return COMMANDS[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
