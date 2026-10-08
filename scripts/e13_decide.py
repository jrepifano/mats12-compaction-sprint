"""Apply one automatic E13 decision (`emx.e13.decide`) and write its record to
results/E13/decisions/<name>.json.

Subcommands (gen names follow `contracts.gen_name`; every judged CSV is
`results/E13/judged/<name>.csv`):
  plumbing                          narrow_trig__trig_r1_s0 -> plumbing.json
  dev --recipe r1|r32               dev__<base, good, 3 organism seeds> -> dev_decision.json
  dirsource --key K [--extended]    dirsrc__K (+ dirsrc__K__ext) -> dirsource_labels.json
  strength --key K --site S         sweep__K__a0 + sweep__K__S -> strength_<S>.json
  consistency --key K               sweep__K__a0 vs dev__K -> consistency.json
  projection --state F --spent-gpu X --spent-api Y --pod-hours H [--a8-drop-sites ...]
                                    bench/{train,hooked}.json -> projection.json
  a8-probe --key K --site S         prints cut 2's probe (no record)

Every judged cell read must be complete (`judging.completeness`, < 2% incomplete per column);
otherwise the script exits 3 with `E13_DECIDE_INCOMPLETE` and writes nothing (prereg §4.6 item
10: retry the failed rows, then report). Log tags start with `E13_DECIDE`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emx import judge_run as JR
from emx.e13 import contracts as C
from emx.e13 import decide as D
from emx.e13 import judging as J


class Incomplete(RuntimeError):
    pass


def load(root: Path, name: str):
    gen, judged = root / C.gen_csv(name), root / C.judged_csv(name)
    comp = J.completeness(judged, gen)
    if not comp["ok"]:
        raise Incomplete(f"{name}: incomplete cells {comp['incomplete_cells']}")
    return J.load_scored(gen, judged, root=root)


def _state(path: str | None) -> C.ProjectionState:
    if not path:
        return C.ProjectionState()
    raw = json.loads(Path(path).read_text())
    for k in ("sites", "cuts", "steps_done"):
        if k in raw:
            raw[k] = tuple(raw[k])
    if raw.get("alphas"):
        raw["alphas"] = {s: tuple(float(a) for a in v) for s, v in raw["alphas"].items()}
    return C.ProjectionState(**raw)


def run(a, root: Path) -> dict:
    if a.cmd == "plumbing":
        return D.plumbing(load(root, C.gen_name("narrow_trig", C.TRIG_KEY)))
    if a.cmd == "dev":
        keys = [C.BASE_KEY, C.model_key("good", a.recipe, 0),
                *(C.model_key("org", a.recipe, s) for s in C.TRAIN_SEEDS["org"])]
        return D.dev_decision({k: load(root, C.gen_name("dev", k)) for k in keys}, a.recipe)
    if a.cmd == "dirsource":
        frames = [load(root, C.gen_name("dirsrc", a.key))]
        if a.extended:
            frames.append(load(root, C.gen_name("dirsrc", a.key, "ext")))
        return D.dirsource_labels(J.concat_scored(frames), a.extended)
    if a.cmd in ("strength", "a8-probe"):
        sc = J.concat_scored([load(root, C.gen_name("sweep", a.key, "a0")),
                              load(root, C.gen_name("sweep", a.key, a.site))])
        return D.choose_strength(sc, a.site) if a.cmd == "strength" else D.a8_probe(sc, a.site)
    if a.cmd == "consistency":
        return D.consistency(load(root, C.gen_name("sweep", a.key, "a0")),
                             load(root, C.gen_name("dev", a.key)))
    if a.cmd == "projection":
        bt, bh = root / C.BENCH_DIR / "train.json", root / C.BENCH_DIR / "hooked.json"
        inputs = {J.rel(p, root): JR.sha256_file(p) for p in (bt, bh)}
        return D.projection(json.loads(bt.read_text()), json.loads(bh.read_text()),
                            _state(a.state), {"gpu": a.spent_gpu, "api": a.spent_api},
                            a.pod_hours, a8_drop_sites=a.a8_drop_sites, inputs=inputs)
    raise ValueError(a.cmd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(C.REPO_ROOT), help=argparse.SUPPRESS)  # tests
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plumbing")
    p = sub.add_parser("dev")
    p.add_argument("--recipe", choices=C.RECIPES, required=True)
    p = sub.add_parser("dirsource")
    p.add_argument("--key", required=True)
    p.add_argument("--extended", action="store_true")
    for name in ("strength", "a8-probe"):
        p = sub.add_parser(name)
        p.add_argument("--key", required=True)
        p.add_argument("--site", choices=C.SITE_NAMES, required=True)
    p = sub.add_parser("consistency")
    p.add_argument("--key", required=True)
    p = sub.add_parser("projection")
    p.add_argument("--state", default=None, help="ProjectionState fields as JSON")
    p.add_argument("--spent-gpu", type=float, required=True)
    p.add_argument("--spent-api", type=float, required=True)
    p.add_argument("--pod-hours", type=float, required=True)
    p.add_argument("--a8-drop-sites", nargs="*", default=None, choices=C.SITE_NAMES)
    a = ap.parse_args(argv)
    root = Path(a.root)
    try:
        rec = run(a, root)
    except Incomplete as e:
        print(f"E13_DECIDE_INCOMPLETE {a.cmd}: {e}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as e:
        print(f"E13_DECIDE_REFUSED {a.cmd}: {e}", file=sys.stderr)
        return 2
    if a.cmd == "a8-probe":
        print(json.dumps(D._jsonable(rec), indent=2))
        return 0
    path = D.write_decision(rec, root)
    print(f"E13_DECIDE {rec['name']}: outcome {rec['outcome']} -> {J.rel(path, root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
