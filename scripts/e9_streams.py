"""E9 continued-pretraining streams (unit C; prereg §4.4 steps 1-7, §4.7 confirmation protocol).

Usage (laptop, free; reads data/E9/{insert,groups,background,confirm}, writes data/E9/streams):
  uv run python scripts/e9_streams.py capacity [--force]      # C once -> insert/capacity.json
  uv run python scripts/e9_streams.py build --arm corr --seed 1
  uv run python scripts/e9_streams.py build-all --seeds 1 2   # bg orig corr para ro or
  uv run python scripts/e9_streams.py build-smoke --seed 1      # background only, smoke pod
  uv run python scripts/e9_streams.py verify --seed 1         # every built arm + identical cores
  uv run python scripts/e9_streams.py reserve-check --seeds 1 2  # Part 1 reserves suffice?
  uv run python scripts/e9_streams.py confirm-check --seeds 5 6 [--D N --n-max N]

`reserve-check` and `confirm-check` exit 1 if any case's reserves fall short (run before any confirmation pod). With
no frozen selections and no --D it checks both projected branches (group policy D ~ 2M, no group
D ~ 4.4M), each at n_max = 0 and its upper bound.
"""

from __future__ import annotations

import argparse
import json
import sys

from emx.e9 import contracts as C
from emx.e9 import stream as S


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _summary(man: dict) -> dict:
    return {"run_id": man["run_id"], "stream_sha256": man["stream_sha256"],
            "core_sha256": man["core_sha256"], "dose": man["dose"],
            "pads": [p.get("tokens") for p in man["pads"]]}


def main(argv: list[str] | None = None, *, paths: S.Paths | None = None,
         geometry: S.Geometry = S.DEFAULT_GEOMETRY, cache: S.TokenCache | None = None) -> int:
    """CLI entry; `paths`, `geometry` and `cache` exist for tests (tiny geometry, fake tokenizer)."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capacity")
    c.add_argument("--force", action="store_true",
                   help="replace an existing capacity.json computed from other inputs")
    bd = sub.add_parser("build")
    bd.add_argument("--arm", required=True, choices=(*S.PART1_STREAM_ARMS, *C.ARMS_CONFIRM))
    bd.add_argument("--seed", type=int, required=True)
    ba = sub.add_parser("build-all")
    ba.add_argument("--seeds", type=int, nargs="+", required=True)
    ba.add_argument("--arms", nargs="+", default=list(S.PART1_STREAM_ARMS),
                    choices=(*S.PART1_STREAM_ARMS, *C.ARMS_CONFIRM))
    sm = sub.add_parser("build-smoke")
    sm.add_argument("--seed", type=int, default=1)
    v = sub.add_parser("verify")
    v.add_argument("--seed", type=int, required=True)
    rc = sub.add_parser("reserve-check")
    rc.add_argument("--seeds", type=int, nargs="+", default=list(C.PART1_CPT_SEEDS))
    cc = sub.add_parser("confirm-check")
    cc.add_argument("--seeds", type=int, nargs="+", default=list(C.CONFIRM_CPT_SEEDS))
    cc.add_argument("--D", type=int, default=None)
    cc.add_argument("--n-max", type=int, default=None)
    a = ap.parse_args(argv)

    paths = paths or S.Paths()
    b = S.Builder(paths, geometry, cache)
    if a.cmd == "capacity":
        cap = S.capacity(paths, geometry, force=a.force)
        _print({k: cap[k] for k in ("C", "ceiling", "totals", "drops", "inputs")})
        return 0
    if a.cmd == "build":
        _print(_summary(b.build(a.arm, a.seed)))
        return 0
    if a.cmd == "build-all":
        _print([_summary(b.build(arm, s)) for s in a.seeds for arm in a.arms])
        return 0
    if a.cmd == "build-smoke":
        m = S.build_smoke(a.seed, paths, geometry, cache)
        _print({k: m[k] for k in ("run_id", "smoke", "stream_sha256", "inputs", "source_mix")}
               | {"by_role": m["background_docs"]["by_role"]})
        return 0
    if a.cmd == "verify":
        out = {}
        for arm in (*S.PART1_STREAM_ARMS, *C.ARMS_CONFIRM):
            if paths.stream(arm, a.seed).exists():
                rec = S.verify_stream(arm, a.seed, builder=b)
                out[arm] = {"ok": True, "core_sha256": rec["core_sha256"]}
        if not out:
            print(f"no streams built for seed {a.seed}", file=sys.stderr)
            return 1
        out["cores"] = S.verify_cores(a.seed, paths, geometry)
        _print(out)
        return 0
    if a.cmd == "reserve-check":
        res = [S.part1_reserve_check(s, paths, geometry) for s in a.seeds]
        _print(res)
        return 0 if all(r["ok"] for r in res) else 1
    if a.cmd == "confirm-check":
        res = S.confirm_check_all(a.seeds, paths, geometry, D=a.D, n_max=a.n_max)
        _print(res)
        return 0 if all(r["ok"] for r in res) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
