"""Experiment 2 close-out: S1's loss pipeline on the pilot's live boundaries (units 1-3, 7).
Module: compaction.s2.lossaudit; plan: docs/sprint/plans/experiment-2-closeout.md.

    uv run python scripts/s2_lossaudit.py run --max [--only KEYS] [--raw DIR] [--out DIR]
            [--concurrency 8] [--no-grep]
        Refuses unless S1's frozen judge config (results/sprint/S1/frozen_config.json) and
        frozen second look (frozen_second_look.json) match the code. Then, on Claude Haiku 5.5
        through the Max plan (`claude -p`, $0; --max makes that explicit; a canary call checks
        the billing route first), at effort medium as S1: passes 1a, 2 and 1b on every
        boundary (`s1_judge.judge_records` with this module's load and post_context), the
        second look on the positives, then recoverability (base-image `git grep` in Docker,
        read-only, unless --no-grep). Resumable: the backend cache is keyed by request content
        (data/raw/sprint/s2/cache/lossaudit-haiku-max/). Writes items, claims, code_facts,
        second_look and losses (.jsonl) plus meta.json under results/sprint/S2/lossaudit/.
    uv run python scripts/s2_lossaudit.py recover [--out DIR] [--no-grep]
        Recompute losses.jsonl from the saved items and second look (no judge calls).
    uv run python scripts/s2_lossaudit.py report [--out DIR]
        Counts from losses.jsonl (placeholder for unit 7's report).

Unsealed by design: no arm data is read, only each attempt's boundary/, rules.json and run.json.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from compaction import s1_judge as J
from compaction import s1_second_look as SL
from compaction.s2 import config as C
from compaction.s2 import lossaudit as LA
from compaction.s2 import retention as RT

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = C.raw_dir() / "cache" / "lossaudit-haiku-max"
HAIKU_EFFORT = "medium"  # as S1's Max judge (scripts/s1_run.py)
FILES = ("items", "claims", "code_facts", "second_look", "losses")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


def _s1_run():
    spec = importlib.util.spec_from_file_location("s1_run", ROOT / "scripts" / "s1_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_frozen() -> str:
    """S1's frozen judge config (D10/D12) and frozen second look (D11); returns the second
    look's digest. Raises SystemExit on drift."""
    _s1_run().check_frozen()
    try:
        return SL.check_frozen()
    except SL.FrozenError as e:
        raise SystemExit(str(e)) from e


def max_backend(concurrency: int):
    from compaction.claude_headless import MaxBackend

    return MaxBackend(concurrency=concurrency, cache_dir=CACHE_DIR, effort=HAIKU_EFFORT)


def _split() -> dict | None:
    return RT.load_split() if RT.SPLIT_PATH.exists() else None


def run(backend, dirs: list[Path], out: Path, *, sl_config: str | None,
        grep_runner: LA.GrepRunner | None, render: Callable | None = None) -> dict:
    """The pipeline on `dirs` with `backend` (Max, or a stub in tests); writes every file."""
    index = LA.Index(dirs)
    items: list[dict] = []
    claims: list[dict] = []
    facts: list[dict] = []
    log(f"judge: {len(index.rids)} boundaries, backend {backend.name}")
    res = J.judge_records(
        backend, index.rids, out_items=items.extend, out_claims=claims.extend,
        out_code_facts=facts.extend, purpose=LA.PURPOSE, load=index.load,
        post_context=LA.post_context, render=render)
    log(f"judge: {len(items)} items, {len(claims)} claims, errors {len(res['errors'])}")
    overlay = SL.second_look(backend, items, claims, load_rec=index.load)
    overlay = [r | {"config_sha": sl_config} for r in overlay]
    log(f"second look: {len(overlay)} re-checked, {sum(r['changed'] for r in overlay)} changed, "
        f"{sum(not r['valid'] for r in overlay)} invalid")
    # the judge's rows first, so a failed base-image search loses nothing (`recover` redoes it)
    rows = {"items": items, "claims": claims, "code_facts": facts, "second_look": overlay}
    for name, rs in rows.items():
        LA.write_jsonl(out / f"{name}.jsonl", rs)
    losses, grep = LA.loss_rows(items, overlay, index, split_rec=_split(),
                                grep_runner=grep_runner)
    LA.write_jsonl(out / "losses.jsonl", losses)
    meta = {
        "what": "S1's loss pipeline on S2's live boundaries (Experiment 2 close-out)",
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "judge": backend.name, "judge_model": getattr(backend, "model", None),
        "effort": getattr(backend, "effort", None),
        "item_prompt_sha": J.item_prompt_sha(J.prompts()),
        "claim_prompt_sha": J.claim_prompt_sha(J.prompts()),
        "second_look_config_sha": sl_config,
        "boundaries": {rid: LA._rel(d) for rid, d in sorted(index.dirs.items())},
        "errors": res["errors"], "valid_share": res.get("valid_share"),
        "records_complete_share": res.get("records_complete_share"),
        "grep": grep, "counts": counts(losses),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


def recover(dirs: list[Path], out: Path, grep_runner: LA.GrepRunner | None) -> dict:
    index = LA.Index(dirs)
    items = LA.read_jsonl(out / "items.jsonl")
    overlay = LA.read_jsonl(out / "second_look.jsonl")
    losses, grep = LA.loss_rows(items, overlay, index, split_rec=_split(),
                                grep_runner=grep_runner)
    LA.write_jsonl(out / "losses.jsonl", losses)
    meta_p = out / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    meta |= {"grep": grep, "counts": counts(losses),
             "recovered_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    meta_p.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


def counts(losses: list[dict]) -> dict:
    lost = [r for r in losses if r["lost"]]
    return {
        "items": len(losses),
        "valid": sum(bool(r["valid"] and r["grade_valid"]) for r in losses),
        "lost": len(lost),
        "lost_by_type": dict(Counter(r["type"] for r in lost)),
        "lost_by_grade": dict(Counter(r["grade"] for r in lost)),
        "lost_planted": sum(r["planted_rule"] is not None for r in lost),
        "recoverable": dict(Counter(r["recoverable"] for r in lost)),
        "by_boundary": dict(Counter(r["run_key"] for r in lost)),
    }


def _dirs(a) -> list[Path]:
    only = [k for k in (a.only or "").split(",") if k] if hasattr(a, "only") else None
    return LA.boundaries(Path(a.raw), only)


def _grep(a) -> LA.GrepRunner | None:
    return None if a.no_grep else LA.docker_grep


def cmd_run(a) -> int:
    if not a.max:
        print("refusing: pass --max to send Max-plan calls (claude -p, $0)", file=sys.stderr)
        return 2
    sl = check_frozen()
    dirs = _dirs(a)
    if not dirs:
        print("no complete boundaries found", file=sys.stderr)
        return 1
    meta = run(max_backend(a.concurrency), dirs, Path(a.out), sl_config=sl,
               grep_runner=_grep(a))
    print(json.dumps(meta["counts"], indent=1))
    return 0 if not meta["errors"] else 3


def cmd_recover(a) -> int:
    meta = recover(_dirs(a), Path(a.out), _grep(a))
    print(json.dumps(meta["counts"], indent=1))
    return 0


def cmd_report(a) -> int:
    losses = LA.read_jsonl(Path(a.out) / "losses.jsonl")
    print(json.dumps(counts(losses), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max", action="store_true", help="send the Max-plan calls")
    r.add_argument("--concurrency", type=int, default=8)
    rc = sub.add_parser("recover")
    for p in (r, rc):
        p.add_argument("--only")
        p.add_argument("--raw", default=str(C.raw_dir()))
        p.add_argument("--no-grep", action="store_true", help="skip the base-image search")
    rp = sub.add_parser("report")
    for p in (r, rc, rp):
        p.add_argument("--out", default=str(LA.OUT_DIR))
    a = ap.parse_args(argv)
    return {"run": cmd_run, "recover": cmd_recover, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
