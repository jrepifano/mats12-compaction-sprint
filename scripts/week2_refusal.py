"""CLI for Week 2, Experiment 3b ("summary_refusal"):

Subcommands:
  qualify    Propose/freeze the decision-point rubric on Experiment 1's divergent first tasks
  topup      Run top-up first tasks (separate registry; limits from the config: <= 20 tasks, $25, Wed 11:30 AM ET)
  prepare    Run Appendix A preparation (restore, replay, pre-decision check, fidelity)
  prep-card  Write the pre-summarization preparation card (config + frozen rubric + preparations)
  summarize  Summarize selected histories through the decision message (paid unless --stub)
  versions   Build the four summary versions (honest, decision_dropped, false_permission, false_rule)
  plan       Build the seeded run plan, launch manifest, and design card
  run        Launch resumed runs through the laptop batch or rig runner (paid unless --stub)
  score      Score every planned slot with the ref-log cutoff and export Experiment 2 monitor items
  analyze    Run the exact sign-flip test, cluster bootstrap, and missing-data bounds
  stub       Run the $0 stub smoke (--dry for non-container synthetic check on laptop)

Every paid step (`topup`, `summarize`, or `run` with a real model) refuses unless:
  1. `--go` (or `--go-from-jacob`) is given, AND
  2. the design card (and launch manifest for `run`) exists and matches the config and plan, AND
  3. the caps file (`driver.read_caps` or `--caps-file`) matches `(cfg["design"]["stage"], cfg["caps"]["stage_usd"])`.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

from compaction.deepswe.sandbox import Invocation
from compaction.s2 import loop as L
from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import refusal as RF
from compaction.s2.chain import refusal_batch as RB
from compaction.s2.chain import stub_refusal as SR
from compaction.s2.config import REPO_ROOT, ROUTES
from compaction.s2.model import StubModel

DEFAULT_OUT = REPO_ROOT / "results" / "sprint" / "week2" / "summary_refusal"
DEFAULT_SOURCE = REPO_ROOT / "results" / "sprint" / "week2" / "precedent"


# ------------------------------------------------------------------------------------------
# Paid-step gate & unified caps/spend resolution
# ------------------------------------------------------------------------------------------


def _resolve_caps_file(checkout: Path | None = None,
                       caps_path: Path | None = None) -> Path:
    """Resolve the single caps file path used for both validation and `PilotSpend` accounting."""
    if caps_path is not None:
        return Path(caps_path)
    return CC.caps_file(checkout)


def _read_stage_caps(checkout: Path | None = None,
                     caps_path: Path | None = None) -> tuple[str, float] | None:
    """Read `(stage, cap_usd)` from the resolved caps file (`driver.read_caps` or `caps_path`)."""
    p = _resolve_caps_file(checkout=checkout, caps_path=caps_path)
    if not p.exists():
        return None
    if p.suffix == ".json":
        doc = json.loads(p.read_text())
        if doc.get("stage") is not None and doc.get("cap_usd") is not None:
            return str(doc["stage"]), float(doc["cap_usd"])
        return None
    st = tomllib.loads(p.read_text()).get("stages") or {}
    if len(st) != 1:
        return None
    ((name, v),) = st.items()
    return str(name), float(v["cap_usd"])


def _resolve_stage_spend(checkout: Path | None = None, caps_path: Path | None = None):
    """The chain's stage spend (`driver.chain_spend`), which charges the checkout's own caps
    file. A `caps_path` other than that file is refused, so validation and accounting can't
    use different files."""
    p = _resolve_caps_file(checkout=checkout, caps_path=caps_path)
    own = CC.caps_file(Path(checkout or CC.REPO_ROOT))
    if Path(p).resolve() != Path(own).resolve():
        raise SystemExit(f"--caps-file {p} is not this checkout's caps file {own}")
    return D.chain_spend(checkout)


def check_paid_gate(cfg: dict, *, go: bool, card: Path, plan: dict | None = None,
                    checkout: Path | None = None,
                    caps_path: Path | None = None,
                    out_dir: Path | None = None,
                    stage: str = "run") -> list[str]:
    """Why a paid step (`topup`, `summarize`, or `run` with a real model) refuses.
    Empty list means allowed."""
    errs: list[str] = []
    if not go:
        errs.append("paid step requires --go")
    card = Path(card)
    if not card.exists():
        errs.append(f"no design card at {card}")
    elif plan is not None:
        errs.extend(RF.card_gate(card, cfg, plan, out_dir=out_dir))
    elif stage == "summarize" and out_dir is not None and (Path(out_dir) / "rubric.json").exists():
        errs.extend(RF.prep_card_gate(card, cfg, Path(out_dir)))
    else:
        text = card.read_text()
        if cfg["_sha256"] not in text:
            errs.append("design card is for another config")
    want = (cfg["design"]["stage"], float(cfg["caps"]["stage_usd"]))
    got = _read_stage_caps(checkout=checkout, caps_path=caps_path)
    if got is None:
        errs.append(f"no valid caps file found (config wants {want})")
    elif got != want:
        errs.append(f"caps file holds {got}, config wants {want}")
    return errs


def enforce_paid_gate(cfg: dict, *, go: bool, card: Path, plan: dict | None = None,
                      checkout: Path | None = None,
                      caps_path: Path | None = None,
                      out_dir: Path | None = None,
                      stage: str = "run") -> None:
    """Refuse with SystemExit(2) when `check_paid_gate` finds any violation."""
    errs = check_paid_gate(
        cfg, go=go, card=card, plan=plan, checkout=checkout,
        caps_path=caps_path, out_dir=out_dir, stage=stage,
    )
    if errs:
        for e in errs:
            print(f"refused: {e}", file=sys.stderr)
        raise SystemExit(2)


# ------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------


def _rubric_rows(rubric: dict) -> dict[str, dict]:
    """Index rubric `rows` by `history` id."""
    return {r["history"]: r for r in rubric.get("rows") or []}


def _row_qualifies(row: dict, frozen: bool) -> bool:
    return bool(row.get("confirmed")) if frozen else bool(row.get("qualifies"))


def _row_decline(row: dict, frozen: bool) -> bool:
    return bool(row.get("confirmed_decline")) if frozen else bool(row.get("explicit_decline"))


def _load_topup_registry(out: Path) -> dict:
    p = Path(out) / "topup_registry.json"
    if p.exists():
        doc = json.loads(p.read_text())
        doc["_path"] = str(p)
        return doc
    return {"_path": str(p), "a": {}, "b": {}, "histories": {}, "spent_usd": 0.0}


def _dump_topup_registry(reg_path: Path, reg: dict) -> None:
    clean = {k: v for k, v in reg.items() if k != "_path"}
    RF.dump(reg_path, clean)


def _load_tasks_safe() -> dict:
    """Load task objects via `D.tasks()`, falling back to `{}` only if task files are absent."""
    try:
        return D.tasks()
    except (FileNotFoundError, OSError):
        return {}


# ------------------------------------------------------------------------------------------
# Subcommand handlers
# ------------------------------------------------------------------------------------------


def cmd_qualify(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    src = Path(args.source_dir)
    rcfg = cfg["refusal"]
    rubric_path = out / "rubric.json"
    topup_reg = _load_topup_registry(out)
    topup_ids = set((topup_reg.get("a") or {}).keys()) | set(
        (topup_reg.get("histories") or {}).keys()
    )

    if args.freeze:
        if not rubric_path.exists():
            print(
                f"refused: no rubric at {rubric_path}; run `qualify` first and confirm rows",
                file=sys.stderr,
            )
            raise SystemExit(2)
        doc = RF.freeze_rubric(rubric_path, rubric_path)
        doc = RF.verify_frozen_rubric(rubric_path)
    else:
        src_histories = RF.source_histories(cfg)
        all_h = list(src_histories) + [
            RF.history_from_id(th) for th in sorted(topup_ids)
            if th not in {h.id for h in src_histories}
        ]
        proposals: list[dict] = []
        for h in all_h:
            a_dir = RF.resolve_a_dir(h.id, src, topup_registry=topup_reg)
            if a_dir is None or not (a_dir / "messages.json").exists():
                continue
            proposals.append(RF.propose_history(a_dir, h))
        doc = RF.write_rubric(proposals, rubric_path, preserve_confirmations=True)

    is_frozen = doc.get("status") == "frozen"
    qualified = (
        list(doc["qualified"])
        if is_frozen
        else [r["history"] for r in doc["rows"] if _row_qualifies(r, False)]
    )
    selected = RF.select_histories(
        qualified, max_n=int(rcfg["max_histories"]), seed=int(cfg["design"]["seed"])
    )
    if is_frozen:
        RF.write_selected_manifest(
            qualified,
            out / "selected_histories.json",
            rubric_frozen_sha256=doc["frozen_sha256"],
            topup_histories=topup_ids,
            max_n=int(rcfg["max_histories"]),
            seed=int(cfg["design"]["seed"]),
        )
    now = datetime.now(RF.ET)
    stop_why = RF.topup_stop(
        len(qualified), len(topup_ids), now,
        target=int(rcfg["target_histories"]),
        max_tasks=int(rcfg["topup_max_tasks"]),
        deadline_et=str(rcfg["topup_deadline_et"]),
        spent_usd=float(topup_reg.get("spent_usd") or 0.0),
        cap_usd=float(cfg["caps"]["topup_usd"]),
    )
    topups = RF.topup_order(
        seed=int(cfg["design"]["seed"]),
        n=int(rcfg["topup_max_tasks"]),
        repos=tuple(rcfg.get("topup_repos") or ("httpx", "tengo")),
        rep0=int(rcfg.get("topup_rep_start", 101)),
    )
    summary = {
        "n_scanned": len(doc["rows"]),
        "n_qualified": len(qualified),
        "selected": selected,
        "frozen": is_frozen,
        "frozen_sha256": doc.get("frozen_sha256"),
        "topup_stop": stop_why,
        "topup_order": topups,
    }
    RF.dump(out / "qualification_summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


TOPUP_TAG = "s2-refusal-topup"


class RunIdAdmission:
    """`driver.run_history` takes `ADMISSION.slot("agent")` with no run id and only then makes
    the container's `Invocation`, so the laptop gate could never match the reservation to the
    container (counted twice; builder b, 27 Oct). This wrapper gives every slot a fresh run id
    and hands it to the next `Invocation` the driver makes inside that slot; driver.py is
    untouched."""

    def __init__(self, inner):
        self.inner = inner
        self.pending: str | None = None

    def slot(self, kind: str, stop=None, *, run_id: str | None = None):
        rid = run_id or uuid.uuid4().hex[:12]  # Invocation's own run-id format
        self.pending = rid
        return self.inner.slot(kind, stop, run_id=rid)

    def take_run_id(self) -> str | None:
        rid, self.pending = self.pending, None
        return rid

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _match_topup_reservations() -> RunIdAdmission:
    """Install `RunIdAdmission` over the laptop gate and make the driver's `Invocation` use the
    slot's run id (top-ups run one at a time, in this process only)."""
    adm = RunIdAdmission(D.ADMISSION)
    D.ADMISSION = adm

    def _invocation(purpose, *a, **kw):
        rid = adm.take_run_id()
        if rid is not None and "run_id" not in kw:
            kw["run_id"] = rid
        return Invocation(purpose, *a, **kw)
    D.Invocation = _invocation
    return adm


def _topup_spend(cfg: dict, checkout, caps_path, cap_usd: float):
    """The chain's stage spend with one more tag: the top-up's own cap, checked (charged + held
    + request) at every reservation under the spend's lock."""
    from compaction.s2.interact_run import PilotSpend
    # the design's own stage and cap first: a fresh process still holds task chaining's
    # defaults (astra fix-set r1 #1)
    D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
    base = _resolve_stage_spend(checkout=checkout, caps_path=caps_path)
    return PilotSpend(tags=tuple(base.tags) + (TOPUP_TAG,),
                      caps={**base.caps, TOPUP_TAG: float(cap_usd)}, stage=base.stage,
                      ledger=base.ledger, caps_file=base.caps_file)


def cmd_topup(args: argparse.Namespace, cfg: dict) -> int:
    """Run top-up no-permission divergent first tasks in their own registry (`topup_registry.json`),
    stopping when 8 histories qualify, or at the config's task cap, top-up dollar cap or deadline
    (20 tasks, $25, Wed 11:30 AM ET under Jacob's option B)."""
    out = Path(args.out)
    src = Path(args.source_dir)
    rcfg = cfg["refusal"]
    rubric_path = out / "rubric.json"
    if not rubric_path.exists():
        print(f"refused: no rubric.json at {rubric_path}; run `qualify` first", file=sys.stderr)
        raise SystemExit(2)
    rubric = json.loads(rubric_path.read_text())
    is_frozen = rubric.get("status") == "frozen"
    rows_by_h = _rubric_rows(rubric)

    src_hists = RF.source_histories(cfg)
    # resolved = a terminal driver state (valid or empty), whether or not it qualifies (r3 #5)
    n_src_resolved, _n_src_valid = RF.source_terminal(cfg, src)
    qualified = [
        r["history"] for r in rubric.get("rows") or [] if _row_qualifies(r, is_frozen)
    ]
    # Initial eligibility applies only before the campaign starts; once a top-up registry
    # holds entries, a restart continues under the stop rules (8 qualify, the task and dollar caps, the
    # deadline), even at 6 or 7 qualifying histories (astra fix-set r1 #2).
    started = bool(_load_topup_registry(out).get("a") or {})
    if not started:
        src_qualified = [h for h in qualified if h in {x.id for x in src_hists}]
        elig, elig_why = RF.topup_eligible(
            n_src_resolved, len(src_qualified),
            expected_source=len(src_hists),
            min_histories=int(rcfg["min_histories_test"]),
        )
        if not elig:
            print(f"refused: top-ups not eligible: {elig_why}", file=sys.stderr)
            raise SystemExit(2)

    card = Path(args.card) if args.card else out / "design_card.md"
    if not args.stub:
        enforce_paid_gate(
            cfg, go=bool(args.go or args.go_from_jacob), card=card,
            checkout=args.checkout, caps_path=args.caps_file, out_dir=out,
            stage="topup",
        )
        RB.install_laptop_admission(cfg, log=out / "laptop_admission.jsonl")
        _match_topup_reservations()

    reg_path = out / "topup_registry.json"
    reg = _load_topup_registry(out)
    reg["_path"] = str(reg_path)
    topup_root = out / "topups"
    topup_root.mkdir(parents=True, exist_ok=True)
    order = RF.topup_order(
        seed=int(cfg["design"]["seed"]),
        n=int(rcfg["topup_max_tasks"]),
        repos=tuple(rcfg.get("topup_repos") or ("httpx", "tengo")),
        rep0=int(rcfg.get("topup_rep_start", 101)),
    )
    cap_usd = float(cfg["caps"]["topup_usd"])
    deadline = str(rcfg["topup_deadline_et"])
    stop_reason: str | None = None
    ran_ids: list[str] = []

    def _now():
        return datetime.fromisoformat(args.now_et) if args.now_et else datetime.now(RF.ET)

    # One durable spend object for every top-up reservation (retries and open holds included):
    # the stage's own cap plus the top-up tag's cap (caps.topup_usd, r3 #3). Spend for the stop rule is that
    # tag's charged + held amount from the ledger, never a running sum (r3 #4).
    topup_spend = None if args.stub else _topup_spend(cfg, args.checkout, args.caps_file, cap_usd)

    def _spent() -> float:
        if topup_spend is None:
            return float(reg.get("spent_usd") or 0.0)
        charged, held = RF.tag_spend(topup_spend)
        return charged + held

    # Recover attempts that finished on disk while the registry still says running (an
    # interruption after the agent ended): the driver records them without launching anything,
    # before any stop rule can skip them (astra fix-set r1 #3). A slot with no finished attempt
    # is left for the loop below, where the stop rules and the in-slot guard apply.
    if not args.stub:
        for hid, slot in sorted((reg.get("a") or {}).items()):
            if slot.get("state") in ("valid", "empty"):
                continue
            k = len(slot.get("attempts") or []) + 1
            if D.finished(D.a_dir(hid, k, topup_root)) is None:
                continue
            def _no_launch(_kind, _att_dir):
                # recovery only: if the finished attempt doesn't count, the next attempt is the
                # loop's to launch (under the stop rules and the in-slot deadline guard)
                raise L.Stopped("recovery pass launches nothing")
            D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
            try:
                D.run_history(RF.history_from_id(hid), reg, D.load_route(), topup_spend,
                              D.tasks(), model_for=_no_launch, root=topup_root)
            except L.Stopped:
                pass
        _dump_topup_registry(reg_path, reg)

    # Rebuild qualification from every top-up history already in a terminal valid state
    # (an interrupted run may have finished a slot without proposing it, r3 #4)
    for hid, slot in sorted((reg.get("a") or {}).items()):
        if slot.get("state") == "valid" and slot.get("dir") and hid not in rows_by_h:
            a_done = Path(slot["dir"])
            if (a_done / "messages.json").exists():
                prop = RF.propose_history(a_done, RF.history_from_id(hid))
                rows_by_h[hid] = prop | {"confirmed": None, "confirmed_decline": None,
                                         "note": "topup"}
                if prop.get("qualifies") and hid not in qualified:
                    qualified.append(hid)

    if not args.stub:
        D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
        D.apply_design(cfg)  # reasoning logging as the design sets it (r3 #11)
        src_caps = RF.source_first_task_caps(cfg)  # experiment 1's first-task limits (r3 #6)
        route = D.load_route()
        all_tasks = D.tasks()

    for item in order:
        hid = item["history"]
        prior = (reg.get("a") or {}).get(hid)
        if prior is not None and prior.get("state") in ("valid", "empty"):
            continue  # done; an unfinished slot (pending/running) is resumed below (r3 #4)
        n_run = len(reg.get("a") or {}) - (1 if prior is not None else 0)
        stop_reason = RF.topup_stop(
            len(qualified), n_run, _now(),
            target=int(rcfg["target_histories"]),
            max_tasks=int(rcfg["topup_max_tasks"]),
            deadline_et=deadline,
            spent_usd=_spent(),
            cap_usd=cap_usd,
        )
        if stop_reason is not None:
            break
        h = RF.history_from_id(hid)
        a_dir: Path | None = topup_root / "a" / hid / "a1"
        if args.stub and a_dir is not None and (a_dir / "messages.json").exists():
            reg.setdefault("a", {})[hid] = {
                "attempt": 1, "state": "valid", "dir": str(a_dir), "cost_usd": 0.0,
            }
        elif not args.stub:
            old_cp = D.CHECKPOINT
            D.CHECKPOINT = {"on": ["push_rejected", "ask_user"], "max": 6}
            try:
                def _model_for(_kind: str, att_dir: Path, _route=route,
                               _cap: float = float(src_caps["run_usd"]), _spend=topup_spend):
                    # inside the admitted slot, before any paid model exists: the deadline is
                    # rechecked for every attempt, retries included (r3 #9)
                    if RF._et(_now()) >= RF._et(deadline):
                        raise L.Stopped(f"top-up deadline {deadline} ET passed")
                    return D.paid_model(_route, att_dir, _cap, _spend)

                slot = D.run_history(
                    h, reg, route, topup_spend, all_tasks,
                    model_for=_model_for, root=topup_root,
                    caps={"max_calls": int(src_caps["max_calls"]),
                          "max_seconds": float(src_caps["max_seconds"])},
                )
                a_dir = (
                    Path(slot["dir"])
                    if slot.get("state") == "valid" and slot.get("dir")
                    else None
                )
                if slot.get("state") == "stopped":
                    stop_reason = "stopped"
            except L.Stopped as e:
                stop_reason = f"stopped: {e}"
                a_dir = None
            finally:
                D.CHECKPOINT = old_cp
        else:
            break
        # the host each top-up history ran on (3b's comparisons are within-history; the card
        # reports hosts per history)
        reg.setdefault("hosts", {})[hid] = socket.gethostname()
        reg["spent_usd"] = round(_spent(), 6)
        _dump_topup_registry(reg_path, reg)
        if a_dir is not None and (a_dir / "messages.json").exists():
            prop = RF.propose_history(a_dir, h)
            rows_by_h[hid] = prop | {"confirmed": None, "confirmed_decline": None, "note": "topup"}
            if prop.get("qualifies") and hid not in qualified:
                qualified.append(hid)
            ran_ids.append(hid)
        if stop_reason is not None and stop_reason.startswith("stopped"):
            break

    n_run = len(reg.get("a") or {})
    spent_usd = _spent()
    reg["spent_usd"] = round(spent_usd, 6)
    _dump_topup_registry(reg_path, reg)
    rubric["status"] = "proposed"
    rubric.pop("frozen_sha256", None)
    rubric["rows"] = [rows_by_h[k] for k in sorted(rows_by_h)]
    RF.dump(rubric_path, rubric)
    if stop_reason is None:
        stop_reason = RF.topup_stop(
            len(qualified), n_run, _now(),
            target=int(rcfg["target_histories"]),
            max_tasks=int(rcfg["topup_max_tasks"]),
            deadline_et=deadline,
            spent_usd=spent_usd,
            cap_usd=cap_usd,
        )
    out_summary = {
        "ran": ran_ids,
        "n_run": n_run,
        "n_qualified": len(qualified),
        "spent_usd": round(spent_usd, 6),
        "stop_reason": stop_reason,
    }
    RF.dump(out / "topup_summary.json", out_summary)
    print(json.dumps(out_summary, indent=2))
    return 0


def cmd_prepare(args: argparse.Namespace, cfg: dict) -> int:
    """Run Appendix A preparation (`restore_and_replay`, `check_pre_decision`, `fidelity`) on
    every qualified history from the frozen rubric under `LaptopAdmission` (1280 MB, 1 CPU),
    stopping the run if >1 history fails."""
    out = Path(args.out)
    rcfg = cfg["refusal"]
    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    rows_by_h = _rubric_rows(rubric)
    topup_reg = _load_topup_registry(out)
    topup_ids = set((topup_reg.get("a") or {}).keys()) | set(
        (topup_reg.get("histories") or {}).keys()
    )
    all_tasks = _load_tasks_safe()
    prep_dir = out / "preparations"
    prep_dir.mkdir(parents=True, exist_ok=True)
    preps: dict[str, dict] = {}
    admission = (
        None if args.stub
        else RB.install_laptop_admission(cfg, log=out / "laptop_admission.jsonl")
    )

    for hid in rubric["qualified"]:
        r_entry = rows_by_h[hid]
        existing_p = prep_dir / f"{hid}.json"
        if args.stub and existing_p.exists():
            preps[hid] = json.loads(existing_p.read_text())
            continue
        if args.stub:
            body = {
                "history": hid,
                "a_dir": r_entry["a_dir"],
                "decision_index": int(r_entry["decision_index"]),
                "checkpoint": r_entry["checkpoint"],
                "restore": {
                    "replays_ok": True,
                    "fetch_replayed": True,
                    "state": {"ok": True, "fails": []},
                    "cutoff": {"push": 1, "reflog": 1},
                    "ok": True,
                },
                "fidelity": {"ok": True, "ref_updates_ok": True, "remote_refs_ok": True},
                "ok": True,
            }
            body["sha256"] = RF.sha256(json.dumps(body, sort_keys=True))
            RF.dump(existing_p, body)
            preps[hid] = body
        else:
            h_meta = RF.history_from_id(hid)
            task = all_tasks.get(h_meta.a_task)
            inv = Invocation(RF.PURPOSE, run_id=RB.new_batch_run_id(f"prep-{hid}"))
            try:
                preps[hid] = RF.prepare_history(
                    inv, r_entry, task, prep_dir, admission=admission, cfg=cfg
                )
            finally:
                inv.cleanup()

    chk = RF.check_preparations(preps, max_failures=1)
    RF.dump(out / "preparation_summary.json", chk)
    if not chk["ok"]:
        print(
            f"refused: Appendix A preparation failed on {chk['n_failed']} histories "
            f"({chk['failed']}); stopping run until fixed",
            file=sys.stderr,
        )
        raise SystemExit(2)

    sel = RF.write_selected_manifest(
        rubric["qualified"],
        out / "selected_histories.json",
        rubric_frozen_sha256=rubric["frozen_sha256"],
        topup_histories=topup_ids,
        excluded_prep=chk["failed"],
        max_n=int(rcfg["max_histories"]),
        seed=int(cfg["design"]["seed"]),
    )
    print(json.dumps({"preparation": chk, "selected": sel["selected"]}, indent=2))
    return 0


def cmd_prep_card(args: argparse.Namespace, cfg: dict) -> int:
    """Build `prep_card.md` and `prep_manifest.json` binding the config, frozen rubric,
    selected histories, and accepted preparation records before paid summarization."""
    out = Path(args.out)
    card_path = Path(args.card) if args.card else out / "prep_card.md"
    RF.prep_card(cfg, out, card_path)
    pm = json.loads((out / "prep_manifest.json").read_text())
    print(json.dumps({
        "selected_histories": pm["selected_histories"],
        "prep_manifest_sha256": pm["prep_manifest_sha256"],
        "card": str(card_path),
    }, indent=2))
    return 0


def cmd_summarize(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    card = Path(args.card) if args.card else out / "prep_card.md"
    if not args.stub:
        enforce_paid_gate(
            cfg, go=bool(args.go or args.go_from_jacob), card=card,
            checkout=args.checkout, caps_path=args.caps_file, out_dir=out,
            stage="summarize",
        )

    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    sel = RF.load_selected_manifest(
        out / "selected_histories.json", rubric_frozen_sha256=rubric["frozen_sha256"]
    )
    rows_by_h = _rubric_rows(rubric)
    src = Path(args.source_dir)
    topup_reg = _load_topup_registry(out)
    not_run = cfg["_texts"]["not_run_tool_message"]
    regens = int(cfg["refusal"]["summary_regenerations"])
    summary_cap = float(cfg["caps"]["summary_usd"])
    opus_dir = out / "opus_reads"

    route = None
    spend = None
    if not args.stub:
        D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
        route = D.load_route()
        spend = _resolve_stage_spend(checkout=args.checkout, caps_path=args.caps_file)

    results: dict[str, dict] = {}
    for hid in sel["selected"]:
        p = rows_by_h[hid]
        a_dir = (
            Path(p["a_dir"])
            if p.get("a_dir") and (Path(p["a_dir"]) / "messages.json").exists()
            else RF.resolve_a_dir(hid, src, topup_registry=topup_reg)
        )
        if a_dir is None or not (a_dir / "messages.json").exists():
            raise ValueError(f"cannot resolve first-task attempt directory for {hid}")
        msgs = json.loads((a_dir / "messages.json").read_text())
        di = int(p["decision_index"])
        branch = str(p["branch"]).removeprefix("feature/")
        hist = RF.summary_history(msgs, di, not_run)
        if args.stub:
            cur_model = StubModel(
                actions=[],
                summary=lambda _m, b=branch: SR.STUB_HONEST_SUMMARY.format(branch=b),
                route=ROUTES["stub"],
            )
        else:
            # Separate per-history capped model and durable spend ledger ($0.10 per history)
            h_sum_dir = out / "summaries" / hid
            h_sum_dir.mkdir(parents=True, exist_ok=True)
            cur_model, _ = D.paid_model(route, h_sum_dir, summary_cap, spend)

        sum_p = out / "summaries" / f"{hid}.json"
        existing_rec = json.loads(sum_p.read_text()) if sum_p.exists() else None
        s_res = RF.summarize(
            hist, cur_model, regenerations=regens,
            opus_reads=opus_dir, hid=hid, existing_record=existing_rec,
        )
        chk = RF.summary_checks(hist, di, s_res["request"], s_res["summary"], not_run)
        opus_read = s_res.get("opus_read")
        if s_res["summary"]:
            s_sha = s_res.get("summary_sha256") or RF.sha256(s_res["summary"])
            if args.stub and RF.load_opus_read(opus_dir, hid, s_sha) is None:
                opus_read = RF.write_opus_read(opus_dir, hid, s_sha, "clean", note="stub")
            else:
                opus_read = RF.load_opus_read(opus_dir, hid, s_sha) or opus_read
            RF.dump(
                out / "opus_requests" / f"{hid}.json",
                RF.opus_read_request(hid, s_res["summary"], int(s_res["generation"] or 0)),
            )
            RF.dump(out / "dropped_requests" / f"{hid}.json", RF.dropped_request(s_res["summary"]))
        rec = {
            "history": hid,
            "branch": p["branch"],
            "decision_index": di,
            "summary": s_res["summary"],
            "summary_sha256": s_res.get("summary_sha256"),
            "generation": s_res["generation"],
            "excluded": s_res["excluded"],
            "attempts": s_res["attempts"],
            "checks": chk,
            "opus_read": opus_read,
        }
        RF.dump(sum_p, rec)
        results[hid] = {"excluded": s_res["excluded"], "checks": chk, "opus_read": opus_read}

    print(json.dumps(results, indent=2))
    return 0


def cmd_versions(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    sel = RF.load_selected_manifest(
        out / "selected_histories.json", rubric_frozen_sha256=rubric["frozen_sha256"]
    )
    sum_dir = out / "summaries"
    opus_dir = out / "opus_reads"
    spans_dir = out / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}
    for hid in sel["selected"]:
        p = sum_dir / f"{hid}.json"
        if not p.exists():
            raise ValueError(f"missing summary file for selected history {hid} at {p}")
        rec = json.loads(p.read_text())
        honest = rec.get("summary")
        if not honest or rec.get("excluded"):
            continue
        honest_sha = RF.sha256(honest)
        op_rec = (
            RF.load_opus_read(opus_dir, hid, honest_sha)
            or RF._normalize_opus_record(rec.get("opus_read") or {}, honest_sha)
        )
        if op_rec is None:
            raise ValueError(
                f"missing Opus review record for {hid} matching summary_sha256 {honest_sha[:12]}"
            )
        if op_rec.get("verdict") != "clean":
            raise ValueError(
                f"Opus review verdict for {hid} is {op_rec.get('verdict')!r}; "
                "rerun `summarize` to regenerate once or exclude"
            )
        if rec.get("opus_read") != op_rec:
            rec["opus_read"] = op_rec
            RF.dump(p, rec)
        branch = rec["branch"]
        sp_file = spans_dir / f"{hid}.json"
        spans_rec = json.loads(sp_file.read_text()) if sp_file.exists() else None
        if spans_rec is None and args.stub:
            bname = str(branch).removeprefix("feature/")
            spans_rec = SR._stub_spans_for(honest, bname)
            RF.dump(sp_file, spans_rec)
        if spans_rec is None:
            raise ValueError(
                f"missing spans file {sp_file} for {hid}; "
                "mark decision spans before building versions"
            )
        vs = RF.build_versions(honest, branch, cfg["_texts"], spans_rec)
        if set(vs) != set(RF.VERSIONS):
            raise ValueError(f"incomplete versions for {hid}: {sorted(vs)}")
        cues = RF.cue_hits({v: vs[v]["text"] for v in vs})
        if cues:
            raise ValueError(f"cue words in {hid} versions: {cues}")
        RF.dump(out / "versions" / f"{hid}.json", {
            "history": hid, "branch": branch, "versions": vs,
        })
        summary[hid] = {v: vs[v]["sha256"][:12] for v in vs}

    print(json.dumps(summary, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    rcfg = cfg["refusal"]
    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    sel = RF.load_selected_manifest(
        out / "selected_histories.json", rubric_frozen_sha256=rubric["frozen_sha256"]
    )
    ver_dir = out / "versions"
    histories: list[dict] = []
    versions_by_history: dict[str, dict] = {}
    for hid in sel["selected"]:
        sum_p = out / "summaries" / f"{hid}.json"
        if sum_p.exists() and json.loads(sum_p.read_text()).get("excluded"):
            continue
        p = ver_dir / f"{hid}.json"
        if not p.exists():
            raise ValueError(f"missing versions file for selected history {hid} at {p}")
        doc = json.loads(p.read_text())
        vs = doc["versions"]
        versions_by_history[hid] = vs
        dd = vs.get("decision_dropped") or {}
        histories.append({
            "history": hid,
            "dropped_identical": bool(dd.get("identical_to_honest")),
        })

    now = datetime.fromisoformat(args.now_et) if args.now_et else datetime.now(RF.ET)
    allowed = RF.versions_allowed(
        now, bool(args.bulk_started), list(rcfg["versions"]),
        pairs_only_after_et=str(rcfg["pairs_only_after_et"]),
        pairs=tuple(rcfg["primary"]),
    )
    phases = [[v for v in ph if v in allowed] for ph in rcfg["launch_phases"]]
    phases = [ph for ph in phases if ph]
    plan = RF.plan_runs(
        histories, allowed, int(rcfg["runs_per_version"]), phases,
        seed=int(cfg["design"]["seed"]),
    )
    RF.dump(out / "plan.json", plan)
    manifest = RF.build_manifest(cfg, out, plan=plan)
    RF.dump(out / "manifest.json", manifest)
    card_path = Path(args.card) if args.card else out / "design_card.md"
    RF.design_card(cfg, plan, versions_by_history, card_path, manifest=manifest)
    print(json.dumps({
        "n_histories": len(histories),
        "n_runs": plan["n"],
        "order_sha256": plan["order_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "card": str(card_path),
    }, indent=2))
    return 0


def cmd_run(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    plan_path = out / "plan.json"
    if not plan_path.exists():
        print(f"refused: no plan.json at {plan_path}", file=sys.stderr)
        raise SystemExit(2)
    plan = json.loads(plan_path.read_text())
    card = Path(args.card) if args.card else out / "design_card.md"
    if not args.stub:
        enforce_paid_gate(
            cfg, go=bool(args.go or args.go_from_jacob), card=card, plan=plan,
            checkout=args.checkout, caps_path=args.caps_file, out_dir=out,
            stage="run",
        )
    else:
        m_errs = RF.verify_manifest(cfg, out, plan=plan, card=card if card.exists() else None)
        if m_errs:
            for e in m_errs:
                print(f"refused: {e}", file=sys.stderr)
            raise SystemExit(2)

    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    sel = RF.load_selected_manifest(
        out / "selected_histories.json", rubric_frozen_sha256=rubric["frozen_sha256"]
    )
    rows_by_h = _rubric_rows(rubric)
    src = Path(args.source_dir)
    topup_reg = _load_topup_registry(out)
    cont = cfg["_texts"]["continue_turn"]
    preps: dict[str, dict] = {}
    contexts: dict[str, dict[str, list]] = {}
    tasks: dict[str, object] = {}
    all_tasks = _load_tasks_safe()

    for hid in sel["selected"]:
        vp = out / "versions" / f"{hid}.json"
        if not vp.exists():
            continue
        vdoc = json.loads(vp.read_text())
        vs = vdoc["versions"]
        h_meta = RF.history_from_id(hid)
        r_entry = rows_by_h[hid]
        a_dir = (
            Path(r_entry["a_dir"])
            if r_entry.get("a_dir") and (Path(r_entry["a_dir"]) / "messages.json").exists()
            else RF.resolve_a_dir(hid, src, topup_registry=topup_reg)
        )
        if a_dir is None or not (a_dir / "messages.json").exists():
            raise ValueError(f"cannot resolve first-task attempt directory for {hid}")
        msgs = json.loads((a_dir / "messages.json").read_text())
        sys_prompt = msgs[0]["content"]
        di = int(r_entry["decision_index"])
        cp = r_entry["checkpoint"]
        preps[hid] = {"checkpoint": cp, "messages": msgs, "decision_index": di}
        contexts[hid] = {
            v: RF.resumed_context(sys_prompt, vs[v]["text"], cont)
            for v in vs
        }
        tasks[hid] = all_tasks.get(h_meta.a_task)

    inv = Invocation(RF.PURPOSE, run_id=args.run_id or RB.new_batch_run_id("s2-refusal"))
    if args.stub:
        def model_factory(_unit: dict, _attempt: int):
            return StubModel(actions=[{"submit": True}], route=ROUTES["stub"])
    else:
        D.apply_stage(cfg["design"]["stage"], cfg["caps"]["stage_usd"])
        route = D.load_route()
        inner_spend = _resolve_stage_spend(checkout=args.checkout, caps_path=args.caps_file)
        run_cap = float(cfg["caps"]["run_usd"])

        def model_factory(unit: dict, attempt: int):
            run_dir = out / "runs" / unit["id"] / "attempts" / f"a{attempt}"
            run_dir.mkdir(parents=True, exist_ok=True)
            model, _ = D.paid_model(route, run_dir, run_cap, inner_spend)
            return model

    summary = RB.run_batch(
        cfg, plan, preps, contexts, tasks, model_factory, inv, out,
        histories=args.histories, now_et=args.now_et,
    )
    print(json.dumps({
        "n_planned": summary["n_planned"],
        "n_finished": summary["n_finished"],
        "spent_usd": summary["spent_usd"],
    }, indent=2))
    return 0


def cmd_score(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    rows_by_h = _rubric_rows(rubric)
    runs_dir = out / "runs"
    plan_p = out / "plan.json"
    plan = json.loads(plan_p.read_text()) if plan_p.exists() else None
    run_dirs = RF.resolve_final_run_dirs(plan["order"] if plan else None, runs_dir)

    if plan is not None:
        rows = RF.score_planned_runs(plan["order"], runs_dir, rows_by_h)
    else:
        rows = []
        for fdir in run_dirs.values():
            res = json.loads((fdir / "result.json").read_text())
            hid = res["history"]
            r_entry = rows_by_h[hid]
            branch = r_entry["branch"]
            ftip = r_entry.get("foreign_tip") or ""
            rows.append(RF.score_run(fdir, branch, ftip))

    scores_by_run = {r["id"]: r for r in rows if r.get("id")}
    mons = RF.monitor_items(
        run_dirs, seed=int(cfg["design"]["seed"]), scores_by_run=scores_by_run
    )
    RF.dump(out / "scores.json", rows)
    RF.dump(out / "monitor_items.json", mons)
    print(json.dumps({"n_scored": len(rows), "n_monitor_items": len(mons)}, indent=2))
    return 0


def cmd_analyze(args: argparse.Namespace, cfg: dict) -> int:
    out = Path(args.out)
    rows = json.loads((out / "scores.json").read_text())
    rubric = RF.verify_frozen_rubric(out / "rubric.json")
    sel_p = out / "selected_histories.json"
    selected_ids: set[str] | None = None
    topup_ids: set[str] | None = None
    if sel_p.exists():
        sel = RF.load_selected_manifest(sel_p, rubric_frozen_sha256=rubric["frozen_sha256"])
        selected_ids = set(sel["selected"])
        origins = sel.get("origins") or {}
        topup_ids = {h for h, orig in origins.items() if orig == "topup"}
        rows = [r for r in rows if r["history"] in selected_ids]

    identical: set[str] = set()
    kept: set[str] = set()
    for p in sorted((out / "versions").glob("*.json")):
        vdoc = json.loads(p.read_text())
        hid = vdoc["history"]
        if selected_ids is not None and hid not in selected_ids:
            continue
        dd = (vdoc.get("versions") or {}).get("decision_dropped") or {}
        if dd.get("identical_to_honest"):
            identical.add(hid)
        else:
            kept.add(hid)
    subgroup = {
        hid for hid, r in _rubric_rows(rubric).items()
        if _row_decline(r, True) and (selected_ids is None or hid in selected_ids)
    }
    planned_h = (identical | kept) if (identical or kept) else selected_ids
    res = RF.analyze(
        rows, identical=identical, subgroup=subgroup, kept=kept,
        alpha=float(cfg["refusal"]["alpha"]),
        runs_per_version=int(cfg["refusal"]["runs_per_version"]),
        planned_histories=planned_h,
        topup_histories=topup_ids,
    )
    RF.dump(out / "analysis.json", res)
    print(json.dumps({
        "rates": res["rates"],
        "primary_test": res["primary"]["test"],
        "descriptive_only": res["primary"]["descriptive_only"],
    }, indent=2))
    return 0


def cmd_stub(args: argparse.Namespace, _cfg: dict) -> int:
    rep = SR.run(root=args.root, dry=args.dry, out_json=args.out_json)
    print(json.dumps({
        "pass": rep["pass"],
        "status": rep["status"],
        "dry": rep["dry"],
        "by_name": rep["by_name"],
    }, indent=2))
    return 0 if rep["pass"] else 1


# ------------------------------------------------------------------------------------------
# Parser & entrypoint
# ------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Week 2 Experiment 3b (summary_refusal) CLI.")
    ap.add_argument("--config", type=Path, default=RF.CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_qual = sub.add_parser("qualify", help="Propose/freeze the decision-point rubric.")
    p_qual.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    p_qual.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_qual.add_argument("--freeze", action="store_true")

    p_top = sub.add_parser("topup", help="Run top-up first tasks under separate registry.")
    p_top.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    p_top.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_top.add_argument("--card", type=Path, default=None)
    p_top.add_argument("--checkout", type=Path, default=None)
    p_top.add_argument("--caps-file", type=Path, default=None)
    p_top.add_argument("--now-et", type=str, default=None)
    p_top.add_argument("--stub", action="store_true")
    p_top.add_argument("--go", action="store_true")
    p_top.add_argument("--go-from-jacob", action="store_true")

    p_prep = sub.add_parser("prepare", help="Run Appendix A preparation on qualified histories.")
    p_prep.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_prep.add_argument("--stub", action="store_true")

    p_pcard = sub.add_parser(
        "prep-card",
        help="Write the pre-summarization prep card binding config, rubric, and preparations.",
    )
    p_pcard.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_pcard.add_argument("--card", type=Path, default=None)

    p_sum = sub.add_parser("summarize", help="Summarize selected histories.")
    p_sum.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    p_sum.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_sum.add_argument("--card", type=Path, default=None)
    p_sum.add_argument("--checkout", type=Path, default=None)
    p_sum.add_argument("--caps-file", type=Path, default=None)
    p_sum.add_argument("--stub", action="store_true")
    p_sum.add_argument("--go", action="store_true")
    p_sum.add_argument("--go-from-jacob", action="store_true")

    p_ver = sub.add_parser("versions", help="Build the four summary versions.")
    p_ver.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_ver.add_argument("--stub", action="store_true")

    p_plan = sub.add_parser("plan", help="Build the seeded run plan, manifest, and design card.")
    p_plan.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_plan.add_argument("--card", type=Path, default=None)
    p_plan.add_argument("--now-et", type=str, default=None)
    p_plan.add_argument("--bulk-started", action="store_true")

    p_run = sub.add_parser("run", help="Run resumed units through the laptop batch.")
    p_run.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    p_run.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p_run.add_argument("--card", type=Path, default=None)
    p_run.add_argument("--checkout", type=Path, default=None)
    p_run.add_argument("--caps-file", type=Path, default=None)
    p_run.add_argument("--run-id", type=str, default=None)
    p_run.add_argument("--histories", nargs="*", default=None)
    p_run.add_argument("--now-et", type=str, default=None)
    p_run.add_argument("--stub", action="store_true")
    p_run.add_argument("--go", action="store_true")
    p_run.add_argument("--go-from-jacob", action="store_true")

    p_sc = sub.add_parser("score", help="Score resumed runs and export monitor items.")
    p_sc.add_argument("--out", type=Path, default=DEFAULT_OUT)

    p_an = sub.add_parser("analyze", help="Run sign-flip test, bootstrap, and bounds.")
    p_an.add_argument("--out", type=Path, default=DEFAULT_OUT)

    p_st = sub.add_parser("stub", help="Run the $0 stub smoke.")
    p_st.add_argument("--dry", action="store_true",
                      help="Run non-container checks on synthetic data without Docker.")
    p_st.add_argument("--root", type=Path, default=SR.DEFAULT_ROOT)
    p_st.add_argument("--out-json", type=Path, default=SR.OUT_JSON)

    return ap


def load_env() -> None:
    """The repo's .env into the environment (keys never printed), as scripts/s2_chain.py does:
    the paid paths' model routes read their API keys from it."""
    p = CC.REPO_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    load_env()
    cfg = RF.load_config(args.config)
    dispatch = {
        "qualify": cmd_qualify,
        "topup": cmd_topup,
        "prepare": cmd_prepare,
        "prep-card": cmd_prep_card,
        "summarize": cmd_summarize,
        "versions": cmd_versions,
        "plan": cmd_plan,
        "run": cmd_run,
        "score": cmd_score,
        "analyze": cmd_analyze,
        "stub": cmd_stub,
    }
    return dispatch[args.cmd](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
