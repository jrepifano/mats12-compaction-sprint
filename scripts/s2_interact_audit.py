"""SWE-Interact pilot audit CLI (unit B): requirements, eligibility, grading and the candidate
screen at one boundary, by hand. Modules: compaction.s2.interact_reqs, interact_score; plan:
docs/sprint/plans/swe-interact-pilot.md. The runner (scripts/s2_interact.py) calls the same
functions with its own backend (Max with the timed OpenRouter fallback); this CLI uses Max only
(`claude -p`, $0), so a usage-window error just fails the command.

    uv run python scripts/s2_interact_audit.py hashes
        The prompt hashes and the screen digest to freeze before the first base run.
    uv run python scripts/s2_interact_audit.py split RUN_DIR K
    uv run python scripts/s2_interact_audit.py eligibility RUN_DIR K [--min-fresh 2]
    uv run python scripts/s2_interact_audit.py grade RUN_DIR K [--sources live pi ...]
        Refuses unless S1's frozen second look matches the code.
    uv run python scripts/s2_interact_audit.py screen RUN_DIR K [--reject CAND ...] [--no-grep]
            [--host H]
        Writes boundary/candidates.json; kept-image searches and check 6 run in Docker
        (read-only containers, --rm, labelled emx.sandbox).
    uv run python scripts/s2_interact_audit.py show RUN_DIR
        Per boundary: eligibility, grades per source, candidate dispositions.

RUN_DIR is a run under data/raw/sprint/s2/interact/; K the boundary index (b<K>).
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from compaction import s1_second_look as SL
from compaction.s2 import config as C
from compaction.s2 import interact_reqs as R
from compaction.s2 import interact_score as S

CACHE_DIR = C.raw_dir() / "cache" / "interact-haiku-max"
HAIKU_EFFORT = "medium"  # as S1's Max judge and the S2 loss audit


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


def max_backend(concurrency: int):
    from compaction.claude_headless import MaxBackend
    from compaction.s2.interact_run import PilotSpend
    # The pilot's stage and tags for its $0 rows, not claude_headless's SPRINT-S1 (astra r1 #12)
    return MaxBackend(concurrency=concurrency, cache_dir=CACHE_DIR, effort=HAIKU_EFFORT,
                      recorder=PilotSpend().recorder())


def _read(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def show(run: Path) -> None:
    for bdir in sorted(run.glob("b[0-9]*"), key=R.boundary_index):
        b = bdir / "boundary"
        el = _read(b / "eligibility.json") or {}
        print(f"b{R.boundary_index(bdir)}: eligible={el.get('eligible')} "
              f"fresh={el.get('fresh_ids')} in_tail={el.get('n_in_tail')} "
              f"inherited={el.get('n_inherited')} ungrounded={el.get('n_ungrounded')}")
        for src, per in (_read(b / "grades.json") or {}).items():
            print(f"  {src:12s} {dict(Counter(per.values()))}")
        cands = _read(b / "candidates.json") or {}
        for c in cands.get("candidates", []):
            outs = " ".join(f"{k[:4]}={v['outcome'][:4]}" for k, v in c["checks"].items())
            print(f"  {c['cand_id']:28s} {c['disposition']:26s} {outs}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hashes")
    for name in ("split", "eligibility", "grade", "screen"):
        p = sub.add_parser(name)
        p.add_argument("run", type=Path)
        p.add_argument("k", type=int)
        p.add_argument("--concurrency", type=int, default=6)
        if name == "eligibility":
            p.add_argument("--min-fresh", type=int, default=R.MIN_FRESH)
        if name == "grade":
            p.add_argument("--sources", nargs="+", default=list(R.GRADE_SOURCES))
        if name == "screen":
            p.add_argument("--reject", nargs="*", default=[])
            p.add_argument("--no-grep", action="store_true")
            p.add_argument("--host", default=None)
    sh = sub.add_parser("show")
    sh.add_argument("run", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "hashes":
        print(json.dumps({"prompts": R.prompt_shas(), "screen_sha256": R.screen_sha()},
                         indent=1))
        return
    if a.cmd == "show":
        show(a.run)
        return
    bdir = a.run / f"b{a.k}"
    if a.cmd == "eligibility":
        print(json.dumps(R.eligibility(bdir, min_fresh=a.min_fresh), indent=1))
        return
    be = max_backend(a.concurrency)
    if a.cmd == "split":
        rq = R.split(bdir, backend=be)
        log(f"split: {len(rq['items'])} items, {sum(i['fresh'] for i in rq['items'])} fresh")
    elif a.cmd == "grade":
        try:
            SL.check_frozen()
        except SL.FrozenError as e:
            raise SystemExit(str(e)) from e
        g = R.grade_sources(bdir, a.sources, backend=be)
        for src, per in g.items():
            log(f"grade {src}: {dict(Counter(per.values()))}")
    elif a.cmd == "screen":
        grep = None
        if not a.no_grep:
            from compaction.s2 import session

            grep = session.grep_runner(a.host)
        out = R.screen(bdir, backend=be, grep_runner=grep,
                       ref_runner=S.docker_runner(a.host), rejected=a.reject)
        log(f"screen: carded {out['carded']}; "
            f"{dict(Counter(c['disposition'] for c in out['candidates']))}")


if __name__ == "__main__":
    main()
