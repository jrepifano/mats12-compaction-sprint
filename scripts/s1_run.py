"""Experiment 1 (S1) driver: the manifest, the development run, Part A, Part B and the size test.

Spec: docs/sprint/specs/S1.md. Every step resumes from the backends' caches, so a rerun sends only
what is missing. Paid steps book under SPRINT-S1 through the backends (Max-plan calls at $0).

    uv run python scripts/s1_run.py manifest
    uv run python scripts/s1_run.py freeze                               # spec D10, before bulk
    uv run python scripts/s1_run.py dev --judges haiku,luna,glm,deepseek
    uv run python scripts/s1_run.py freeze --amend D12                   # prompt v2 (spec D12)
    uv run python scripts/s1_run.py val2 --judges haiku                  # fresh validation sample
    uv run python scripts/s1_run.py partA --judges haiku,<judge B>      # after the held-out gate
    uv run python scripts/s1_run.py replays --arms cc_native,cc_prompt,codex_local,pi,antigravity,opencode
    uv run python scripts/s1_run.py partB --judges haiku,<judge B>
    uv run python scripts/s1_run.py size

`val2` (spec D12) judges a fresh validation sample, stratified by source and session-disjoint
from the first label records, into data/raw/sprint/s1/val2/ the way `dev` does, for the second
label page; the full re-run waits for that page's gate.
`dev` writes judge rows for the label records to data/raw/sprint/s1/dev/ (validation data, not
sealed outcomes) plus roster.json ({rid: {judge: {items: ok, claims: ok}}}), which the label page
and scorer read. `partA`, `partB` and `size` write through `seal.write("S1", ...)`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from compaction import s1_data, s1_judge, seal
from compaction import s1_types as T

ROOT = Path(__file__).resolve().parents[1]
S1_DIR = ROOT / "data" / "raw" / "sprint" / "s1"
MANIFEST = S1_DIR / "manifest.json"
PUBLIC_MANIFEST = ROOT / "results" / "sprint" / "S1" / "manifest.json"
DEV_DIR = S1_DIR / "dev"
VAL2_DIR = S1_DIR / "val2"
VAL2_N = {"jacob": 6, "swe-chat-v1": 7, "posttrainbench": 6, "frontierml": 6}
VAL2_SEED = "s1-val2:20261022"
FROZEN = ROOT / "results" / "sprint" / "S1" / "frozen_config.json"
CACHE = S1_DIR / "cache"
HAIKU_EFFORT = "medium"
BATCH = 50  # records per judge_records call, so an API reservation stays well under the cap
MAX_BATCH = 200  # Max judges need no reservation; a bigger batch idles fewer workers at its tail
PARTB_CHUNK = 10  # Part B: records per chunk, every arm judged back to back (prefix cache reads)


def backend(name: str, concurrency: int | None = None):
    if name == "haiku":
        from compaction.claude_headless import MaxBackend

        # medium effort: the default thought up to 64k tokens and ran out of output on pass 2;
        # low listed ~40% fewer items (e2e on 3 records, 20 Oct; docs/sprint/prep/l-s1-smoke.md)
        return MaxBackend(
            concurrency=concurrency or 8, cache_dir=CACHE / "haiku-max", effort=HAIKU_EFFORT
        )
    from compaction.s1_api import ROUTES, ApiBackend

    return ApiBackend(ROUTES[name], cache_dir=CACHE / name, concurrency=concurrency or 32)


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def batch_size(be) -> int:
    return MAX_BATCH if be.family == "anthropic" else BATCH


def batches(xs: list[str], n: int = BATCH):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def judge_ids(be, rids: list[str]) -> list[str]:
    """Spec D2: a non-Anthropic judge gets public records only."""
    if be.family == "anthropic":
        return rids
    return [r for r in rids if not T.anthropic_only(s1_data.load(r))]


def roster_of(rids: list[str], errors: list[dict]) -> dict:
    bad: dict[str, set] = {}
    for e in errors:
        rid = str(e.get("rid", "")).split("@")[0]
        bad.setdefault(rid, set()).add(
            {"1a": "items", "1b": "items", "2": "claims"}.get(str(e.get("pass")), "all")
        )
    out = {}
    for r in rids:
        b = bad.get(r, set())
        out[r] = {"items": not ({"items", "all"} & b), "claims": not ({"claims", "all"} & b)}
    return out


def update_roster(path: Path, judge: str, entries: dict) -> None:
    """Merge one judge's entries into the dev roster under a lock, re-reading it first, so
    dev runs of different judges in separate processes don't overwrite each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        roster = json.loads(path.read_text()) if path.exists() else {}
        for rid, ok in entries.items():
            roster.setdefault(rid, {})[judge] = ok
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(roster, indent=1, sort_keys=True))
        tmp.replace(path)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%SZ", time.gmtime()), msg, flush=True)


def cmd_manifest(args) -> None:
    index = s1_data.index_all()
    part_a = s1_data.sample_part_a(index=index)
    part_b = s1_data.pick_part_b(part_a, index=index)
    labels = s1_data.label_records(part_a, index=index)
    s1_data.write_manifest(part_a, part_b, labels, MANIFEST, PUBLIC_MANIFEST)
    log(
        f"manifest: part A {len(part_a)}, part B {len(part_b)}, labels "
        f"{len(labels['dev'])} dev + {len(labels['held'])} held"
    )


def frozen_config() -> dict:
    """Spec D10: what the labels validate, fixed before they are read. The judge prompts and
    schemas by sha256, Haiku's model and effort, each API route, and the replay policies."""
    import dataclasses
    import hashlib

    from compaction import claude_headless, s1_api, s1_replay

    def sha(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    return {
        "prompts": {f.name: sha(f.read_bytes()) for f in sorted(s1_judge.PROMPT_DIR.glob("*.md"))},
        "schemas": {
            k: sha(json.dumps(getattr(T, k), sort_keys=True, separators=(",", ":")).encode())
            for k in ("PASS1A_SCHEMA", "PASS1B_SCHEMA", "PASS2_SCHEMA")
        },
        "haiku": {"model": claude_headless.DEFAULT_MODEL, "effort": HAIKU_EFFORT},
        "routes": {
            n: {k: v for k, v in dataclasses.asdict(r).items() if k != "notes"}
            for n, r in s1_api.ROUTES.items()
        },
        "replay_policies": {a: s1_replay.policy_version(a) for a in T.ARMS if a != "natural"},
        # D12: the reconstructed PostTrainBench task prompts are judge input too
        "ptb_prompts": sha(s1_data.PTB_PROMPTS.read_bytes()),
    }


def check_frozen() -> None:
    """Refuse bulk work when the config drifted from the frozen snapshot (D10), or none exists."""
    if not FROZEN.exists():
        raise SystemExit(f"no {FROZEN}: run `s1_run.py freeze` first (D10)")
    if not _same_as_frozen(json.loads(FROZEN.read_text())):
        raise SystemExit(f"{FROZEN} differs from the current config: prompts are frozen (D10)")


_FREEZE_META = ("frozen_utc", "version", "amendment")


def _same_as_frozen(old: dict) -> bool:
    cfg = frozen_config()
    if "version" not in old:
        cfg.pop("ptb_prompts")  # the D10 snapshot (no version field) predates D12's prompts
    return {k: v for k, v in old.items() if k not in _FREEZE_META} == cfg


def cmd_freeze(args) -> None:
    """Spec D10; `--amend <decision>` (D12) re-freezes a changed config as the next version,
    after archiving the current snapshot unchanged as frozen_config_v<n>.json."""
    version, amendment = 1, None
    if FROZEN.exists():
        old = json.loads(FROZEN.read_text())
        if _same_as_frozen(old):
            log(f"freeze: unchanged ({FROZEN})")
            return
        if not getattr(args, "amend", None):
            check_frozen()  # raises: the config changed and no amendment was named
        n = old.get("version", 1)
        archive = FROZEN.with_name(f"frozen_config_v{n}.json")
        if archive.exists() and archive.read_text() != FROZEN.read_text():
            raise SystemExit(f"{archive} exists: refusing to overwrite an archived snapshot")
        if not archive.exists():  # (exists with the same text: an interrupted amendment's retry)
            atmp = archive.with_suffix(".tmp")
            atmp.write_text(FROZEN.read_text())
            atmp.replace(archive)
        version, amendment = n + 1, args.amend
        log(f"freeze: archived version {n} as {archive.name}")
    cfg = frozen_config()
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    meta = {"frozen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "version": version}
    if amendment:
        meta["amendment"] = amendment
    tmp = FROZEN.with_suffix(".tmp")
    tmp.write_text(json.dumps({**meta, **cfg}, indent=1, sort_keys=True) + "\n")
    tmp.replace(FROZEN)
    log(f"freeze: wrote {FROZEN} (version {version})")


def cmd_dev(args) -> None:
    ids = load_manifest()["ids"]
    rids = ids["labels_dev"] + ids["labels_held"]
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    roster_path = DEV_DIR / "roster.json"
    for name in args.judges.split(","):
        be = backend(name, args.concurrency)
        mine = judge_ids(be, rids)
        log(f"dev {name}: {len(mine)} records")
        res = s1_judge.judge_records(
            be,
            mine,
            out_items=DEV_DIR / f"items_{be.name}.jsonl",
            out_claims=DEV_DIR / f"claims_{be.name}.jsonl",
            out_code_facts=DEV_DIR / f"code_facts_{be.name}.jsonl",
            purpose="s1_judge",
        )
        update_roster(roster_path, be.name, roster_of(mine, res["errors"]))
        summary = {k: v for k, v in res.items() if k != "errors"}
        summary["n_errors"] = len(res["errors"])
        (DEV_DIR / f"summary_{be.name}.json").write_text(
            json.dumps({**summary, "errors": res["errors"]}, indent=1, default=str)
        )
        log(f"dev {name}: {json.dumps(summary, default=str)}")


def val2_sample() -> list[str]:
    """Spec D12: VAL2_N Part A records per source, none sharing a session group with the first
    label records (so the second page is independent of the development set), seeded."""
    ids = load_manifest()["ids"]
    used = set(ids["labels_dev"]) | set(ids["labels_held"])
    by = {e["id"]: e for e in s1_data.index_all()}
    used_groups = {by[r]["session_group"] for r in used}
    pool: dict[str, list[str]] = {}
    for r in ids["part_a"]:
        if r not in used and by[r]["session_group"] not in used_groups:
            pool.setdefault(by[r]["source"], []).append(r)
    rng = random.Random(VAL2_SEED)
    out: list[str] = []
    for source, n in VAL2_N.items():
        out += sorted(rng.sample(sorted(pool[source]), n))
    return out


def cmd_val2(args) -> None:
    check_frozen()
    rids = val2_sample()
    VAL2_DIR.mkdir(parents=True, exist_ok=True)
    (VAL2_DIR / "manifest.json").write_text(
        json.dumps(
            {"ids": {"labels_dev": [], "labels_held": rids}, "seed": VAL2_SEED, "n": VAL2_N},
            indent=1,
        )
        + "\n"
    )
    for name in args.judges.split(","):
        be = backend(name, args.concurrency)
        mine = judge_ids(be, rids)
        log(f"val2 {name}: {len(mine)} records")
        res = s1_judge.judge_records(
            be,
            mine,
            out_items=VAL2_DIR / f"items_{be.name}.jsonl",
            out_claims=VAL2_DIR / f"claims_{be.name}.jsonl",
            out_code_facts=VAL2_DIR / f"code_facts_{be.name}.jsonl",
            purpose="s1_judge",
        )
        update_roster(VAL2_DIR / "roster.json", be.name, roster_of(mine, res["errors"]))
        log(f"val2 {name}: valid_share {res.get('valid_share')}, errors {len(res['errors'])}")


class _Sealed:
    """Rows collected in memory for one judge, written once through seal.write (outcomes only
    ever go to the seal), plus an analysis roster keyed by row rid: {rid: {ok, items_ok,
    claims_ok, source, model, harness, session_id, history_complete}}."""

    def __init__(self, prefix: str, judge: str):
        self.prefix, self.judge = prefix, judge
        self.items: list[dict] = []
        self.claims: list[dict] = []
        self.facts: list[dict] = []
        self.roster: dict[str, dict] = {}

    def add_run(self, recs: dict[str, dict], arm: str, errors: list[dict], tag=None) -> None:
        for base, ok in roster_of(list(recs), errors).items():
            rec = recs[base]
            rid = base if arm == "natural" else f"{base}@{arm}"
            self.roster[rid] = {
                "ok": ok["items"] and ok["claims"],
                "items_ok": ok["items"],
                "claims_ok": ok["claims"],
                "arm": arm,
                **{k: rec.get(k) for k in ("source", "model", "harness", "session_id")},
                "session_group": rec.get("session_group"),
                "history_complete": rec.get("history_complete"),
                **(tag(rec) if tag else {}),
            }

    def flush(self) -> dict:
        p, j = self.prefix, self.judge
        seal.write(T.SEAL_STAGE, f"{p}_items_{j}.jsonl", self.items)
        seal.write(T.SEAL_STAGE, f"{p}_claims_{j}.jsonl", self.claims)
        seal.write(T.SEAL_STAGE, f"{p}_code_facts_{j}.jsonl", self.facts)
        seal.write(T.SEAL_STAGE, f"{p}_roster_{j}.json", self.roster)
        return {
            "n_items": len(self.items),
            "n_claims": len(self.claims),
            "n_rows": len(self.roster),
        }


def _judge_into(
    sink: _Sealed, be, rids, *, arm="natural", replays=None, load=None, decorate=None, n=None
):
    """judge_records in batches of `n` records (default `batch_size(be)`), rows appended to
    `sink` (decorated if asked)."""
    load = load or s1_data.load

    def add(dst):
        return lambda rows: dst.extend(decorate(r) for r in rows) if decorate else dst.extend(rows)

    for chunk in batches(rids, n or batch_size(be)):
        res = s1_judge.judge_records(
            be,
            chunk,
            arm=arm,
            replays=replays,
            out_items=add(sink.items),
            out_claims=add(sink.claims),
            out_code_facts=add(sink.facts),
            purpose="s1_judge",
            load=load,
        )
        sink.add_run({r: load(r) for r in chunk}, arm, res["errors"])
        log(
            f"{sink.prefix} {be.name} {arm}: {len(chunk)} records, valid_share "
            f"{res.get('valid_share')}, errors {len(res['errors'])}"
        )


def _per_judge(names: str, work, concurrency) -> None:
    """Run `work(backend)` for each judge: the Anthropic (Max) judge in the main thread, so its
    signal guard applies, and each API judge in its own thread at the same time (they use
    separate resources; integration review r1 finding 6)."""
    bes = [backend(n, concurrency) for n in names.split(",")]
    s1_data.index_all()  # build the index once, before any thread reads it
    main = [b for b in bes if b.family == "anthropic"]
    rest = [b for b in bes if b.family != "anthropic"]
    with ThreadPoolExecutor(max_workers=max(1, len(rest))) as pool:
        futs = [pool.submit(work, b) for b in rest]
        for b in main:
            work(b)
        for f in futs:
            f.result()


def cmd_part_a(args) -> None:
    check_frozen()
    rids = load_manifest()["ids"]["part_a"]

    def work(be):
        sink = _Sealed("partA", be.name)
        _judge_into(sink, be, judge_ids(be, rids))
        log(f"partA {be.name}: {sink.flush()}")

    _per_judge(args.judges, work, args.concurrency)


def cmd_replays(args) -> None:
    check_frozen()
    from compaction import s1_replay

    rids = load_manifest()["ids"]["part_b"]
    be = backend("haiku", args.concurrency)
    res = s1_replay.run_replays(rids, args.arms.split(","), be)
    log(f"replays: {json.dumps(res, default=str)[:2000]}")


def _part_b_into(sink: _Sealed, be, rids: list[str], reps_by_arm: dict[str, list[dict]]) -> None:
    """Record-major: each chunk of PARTB_CHUNK records runs every arm back to back, so a
    history's pass-2 prefix is still in the prompt cache for the next arm (arm-major let it
    expire between arms). The rows are the same as arm-major's; only their order differs."""
    have = {a: {r["base_rid"] for r in reps} for a, reps in reps_by_arm.items()}
    for arm in reps_by_arm:
        missing = [r for r in rids if r not in have[arm]]
        if missing:
            log(f"partB {be.name} {arm}: no replay for {len(missing)} records (not judged)")
    for chunk in batches(rids, PARTB_CHUNK):
        for arm, reps in reps_by_arm.items():
            mine = [r for r in chunk if r in have[arm]]
            if mine:
                _judge_into(sink, be, mine, arm=arm, replays=reps, n=PARTB_CHUNK)


def cmd_part_b(args) -> None:
    """Every arm's rows for one judge go to one sealed file per kind (rows carry `arm`), the
    names s1_analysis reads (integration review r1 finding 1)."""
    check_frozen()
    from compaction import s1_replay

    rids = load_manifest()["ids"]["part_b"]
    reps_by_arm = {}
    for arm in args.arms.split(","):
        path = S1_DIR / "replays" / f"{arm}.jsonl"
        reps_by_arm[arm] = [r for r in s1_replay.read_replays(path).values() if not r.get("error")]

    def work(be):
        sink = _Sealed("partB", be.name)
        _part_b_into(sink, be, judge_ids(be, rids), reps_by_arm)
        log(f"partB {be.name}: {sink.flush()}")

    _per_judge(args.judges, work, args.concurrency)


def cmd_size(args) -> None:
    """The size test: replays of each long Jacob history's 256k and 512k prefixes (cc_prompt arm
    plus a fixed length line), then each judged against ITS OWN prefix history: pass 1a lists
    the prefix's items and passes 1b and 2 use that prefix's summary (integration review r1
    finding 2). Rows carry `prefix` and `orig_rid` for s1_analysis.size_test."""
    check_frozen()
    from compaction import s1_replay

    rids = [r for r in load_manifest()["ids"]["part_a"] if r.startswith("jacob:")]
    be = backend("haiku", args.concurrency)
    res = s1_replay.run_size(rids, be)
    log(f"size replays: {json.dumps(res, default=str)[:2000]}")
    rows = [
        r
        for r in s1_replay.read_replays(S1_DIR / "replays" / "size.jsonl").values()
        if not r.get("error")
    ]
    prefixes = {}
    for r in rids:
        for p in s1_replay.size_prefixes(s1_data.load(r)):
            prefixes[p["id"]] = p
    reps = {}
    for row in rows:
        pid = row["id"].rsplit("@", 1)[0]
        if pid in prefixes:
            reps[pid] = row

    def decorate(row):
        pid = row["base_rid"]
        return {
            **row,
            "prefix": prefixes[pid]["s1_size"]["cut_tokens"],
            "orig_rid": prefixes[pid]["s1_size"]["base_rid"],
        }

    sink = _Sealed("size", be.name)
    _judge_into(
        sink,
        be,
        sorted(reps),
        arm="cc_prompt",
        replays=reps,
        load=prefixes.__getitem__,
        decorate=decorate,
    )
    log(f"size {be.name}: {sink.flush()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("manifest")
    fz = sub.add_parser("freeze")
    fz.add_argument("--amend", help="re-freeze a changed config under this decision (e.g. D12)")
    for name in ("dev", "val2", "partA", "partB"):
        p = sub.add_parser(name)
        p.add_argument("--judges", required=True)
        p.add_argument("--concurrency", type=int)
        if name == "partB":
            p.add_argument("--arms", required=True)
    for name in ("replays", "size"):
        p = sub.add_parser(name)
        p.add_argument("--concurrency", type=int)
        if name == "replays":
            p.add_argument("--arms", required=True)
    args = ap.parse_args(argv)
    {
        "manifest": cmd_manifest,
        "freeze": cmd_freeze,
        "dev": cmd_dev,
        "val2": cmd_val2,
        "partA": cmd_part_a,
        "replays": cmd_replays,
        "partB": cmd_part_b,
        "size": cmd_size,
    }[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
