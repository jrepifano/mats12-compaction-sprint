"""Experiment 1 second look (spec D11, D13): run it, freeze it, and re-key a label page.

    uv run python scripts/s1_second_look.py freeze
    uv run python scripts/s1_second_look.py run --dir data/raw/sprint/s1/val2 [--only-labelled KEY]
    uv run python scripts/s1_second_look.py rekey --key KEY --dir DIR --out KEY2 \
        --dev-labels LABELS [--exclude reloaded|not_lost]

`freeze` records the prompts, schemas, tools and turn cap in results/sprint/S1/
frozen_second_look.json, and refuses if a different freeze exists. `run` refuses unless the
current config matches that freeze (`--unfrozen` for development only).

`run` re-checks Haiku's positives in DIR's items/claims files (`compaction.s1_second_look`) on
the Max backend and writes the overlay to DIR/second_look_haiku-max.jsonl (all rows re-checked
so far; the backend's content-keyed cache makes a rerun free). `--only-labelled KEY` limits it to
the rows behind a label page's units.

`rekey` writes a copy of a label key whose units carry the second look's verdicts, for
`scripts/s1_label_score.py --gate-rule d13`. It refuses overlay rows not run under the frozen
config (each row carries the digest of the whole config it ran under). Item units take their
row's verdict; claim units have each nested claim updated by id and the span's verdict recomputed (worst claim, as the scorer aggregates). `--dev-labels`
(D13 (e)): the units labelled in that file become the development half, every other unit stays
held out, so the gate is scored on the rest. `--exclude reloaded` (D13 (c)) leaves out item units
whose row's availability (`s1_judge.availability`, the loss metric's own classification) is
"reloaded"; `--exclude not_lost` keeps only items the headline could count as lost
(availability "lost"), for a descriptive figure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from compaction import s1_second_look as SL

JUDGE = "haiku-max"
FROZEN = ROOT / "results" / "sprint" / "S1" / "frozen_second_look.json"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} S1_SECOND_LOOK {msg}", flush=True)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def unit_row_ids(key: dict) -> set[str]:
    out: set[str] = set()
    for u in key["units"].values():
        for v in (u.get("row_ids_all") or u.get("row_ids") or {}).values():
            out.update([v] if isinstance(v, str) else v)
    return out


def check_frozen() -> None:
    """Refuse when the second look's config differs from its freeze (D11), or none exists."""
    if not FROZEN.exists():
        raise SystemExit(f"no {FROZEN}: run `s1_second_look.py freeze` first (D11)")
    old = json.loads(FROZEN.read_text())
    if {k: v for k, v in old.items() if k != "frozen_utc"} != SL.frozen(SL.prompts()):
        raise SystemExit(f"{FROZEN} differs from the current second look: it is frozen (D11)")


def config_sha(cfg: dict) -> str:
    """Digest of a second-look config (`s1_second_look.frozen`: prompts, schemas, tools, turn
    cap, verdicts re-checked); every overlay row carries the one it ran under."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def frozen_config_sha() -> str:
    old = json.loads(FROZEN.read_text())
    return config_sha({k: v for k, v in old.items() if k != "frozen_utc"})


def cmd_run(a) -> None:
    from s1_run import backend

    if not a.unfrozen:
        check_frozen()

    d = Path(a.dir)
    items = read_jsonl(d / f"items_{JUDGE}.jsonl")
    claims = read_jsonl(d / f"claims_{JUDGE}.jsonl")
    if a.only_labelled:
        keep = unit_row_ids(json.loads(Path(a.only_labelled).read_text()))
        items = [r for r in items if r["item_id"] in keep]
        claims = [r for r in claims if r["claim_id"] in keep]
    its, cls = SL.positives(items, claims)
    log(f"run {d}: {len(its)} dropped items, {len(cls)} unsupported claims")
    be = backend("haiku", a.concurrency)
    rows = SL.second_look(be, items, claims)
    out = d / f"second_look_{JUDGE}.jsonl"
    old = {r["row_id"]: r for r in read_jsonl(out)} if out.exists() else {}
    stamp = config_sha(SL.frozen(SL.prompts()))
    old.update({r["row_id"]: r | {"config_sha": stamp} for r in rows})
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in old.values()))
    changed = sum(r["changed"] for r in rows)
    invalid = sum(not r["valid"] for r in rows)
    log(f"run done: {len(rows)} re-checked, {changed} changed, {invalid} invalid; {out}")
    log(f"backend stats {json.dumps(be.last_stats)}")


def labelled_ids(path: str) -> set[str]:
    """Unit ids labelled in a labels file: a list of {id, collection, data} or {collection: {id:
    data}} (as `s1_label_score.normalize_labels` reads it)."""
    import s1_label_score

    return set(s1_label_score.normalize_labels(json.loads(Path(path).read_text()))["labels"])


def rekey(
    key: dict,
    overlay: list[dict],
    *,
    dev_ids: set[str],
    availability_of=None,
    exclude: str = "none",
    config: str | None = None,
) -> tuple[dict, dict]:
    """The re-keyed label key and counts. `availability_of(item_row_id)` gives an item row's
    availability class; it is needed only when `exclude` isn't "none". `config`: the frozen
    config digest every overlay row must carry."""
    import s1_label_score

    if config is not None:
        bad = sum(o.get("config_sha") != config for o in overlay)
        if bad:
            raise SystemExit(f"{bad} overlay rows not run under the frozen second-look config")
    if not dev_ids <= set(key["units"]):
        raise SystemExit("dev labels name units that are not in this key (another draw?)")
    ov = {o["row_id"]: o for o in overlay}
    order = s1_label_score.CLAIM_PRECEDENCE
    units: dict[str, dict] = {}
    n = {"changed": 0, "excluded": 0, "dev": 0, "held": 0}
    for uid, u in key["units"].items():
        u = dict(u, half="dev" if uid in dev_ids else "held")
        if u["kind"] == "item":
            rid = (u.get("row_ids") or {}).get(JUDGE)
            if exclude != "none":
                av = availability_of(rid)
                if (exclude == "reloaded" and av == "reloaded") or (
                    exclude == "not_lost" and av != "lost"
                ):
                    n["excluded"] += 1
                    continue
            o = ov.get(rid)
            if o is not None and JUDGE in u["verdicts"]:
                n["changed"] += o["verdict"] != u["verdicts"][JUDGE]
                u["verdicts"] = u["verdicts"] | {JUDGE: o["verdict"]}
        elif JUDGE in (u.get("claims") or {}):
            cs = [
                c | {"verdict": ov[c["id"]]["verdict"]} if c["id"] in ov else c
                for c in u["claims"][JUDGE]
            ]
            worst = min((c["verdict"] for c in cs), key=order.index)
            n["changed"] += worst != u["verdicts"].get(JUDGE)
            u["claims"] = u["claims"] | {JUDGE: cs}
            u["verdicts"] = u["verdicts"] | {JUDGE: worst}
        n[u["half"]] += 1
        units[uid] = u
    return key | {"units": units}, n


def cmd_rekey(a) -> None:
    key = json.loads(Path(a.key).read_text())
    d = Path(a.dir)
    overlay = read_jsonl(d / f"second_look_{JUDGE}.jsonl")
    availability_of = None
    if a.exclude != "none":
        from compaction import s1_data, s1_judge

        rows = {r["item_id"]: r for r in read_jsonl(d / f"items_{JUDGE}.jsonl")}
        recs: dict[str, dict] = {}

        def availability_of(rid: str) -> str:
            r = rows[rid]
            if r.get("arm") not in (None, "natural"):
                raise SystemExit(f"{rid}: rekey handles the natural arm only")
            b = str(r.get("base_rid") or r["rid"])
            if b not in recs:
                recs[b] = s1_data.load(b)
            return s1_judge.availability(r, recs[b], s1_data.post_context(recs[b]))[0]

    out, n = rekey(
        key,
        overlay,
        dev_ids=labelled_ids(a.dev_labels),
        availability_of=availability_of,
        exclude=a.exclude,
        config=frozen_config_sha(),
    )
    out["second_look"] = {"overlay": str(d), "exclude": a.exclude, "dev_labels": a.dev_labels}
    Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    Path(a.out).chmod(0o600)
    log(f"rekey: {json.dumps(n)}; exclude {a.exclude}; {a.out}")


def cmd_freeze(a) -> None:
    cfg = SL.frozen(SL.prompts())
    if FROZEN.exists():
        old = json.loads(FROZEN.read_text())
        if {k: v for k, v in old.items() if k != "frozen_utc"} != cfg:
            raise SystemExit(f"{FROZEN} exists and differs: the second look is frozen (D11)")
        log(f"freeze: unchanged ({FROZEN})")
        return
    FROZEN.write_text(
        json.dumps(
            {"frozen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} | cfg, indent=1
        )
        + "\n"
    )
    log(f"freeze: wrote {FROZEN}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dir", required=True)
    r.add_argument("--only-labelled", default=None)
    r.add_argument("--concurrency", type=int, default=8)
    r.add_argument("--unfrozen", action="store_true", help="development only: skip the freeze")
    k = sub.add_parser("rekey")
    k.add_argument("--key", required=True)
    k.add_argument("--dir", required=True)
    k.add_argument("--out", required=True)
    k.add_argument("--dev-labels", required=True, help="labels file of the development units")
    k.add_argument("--exclude", choices=("none", "reloaded", "not_lost"), default="none")
    sub.add_parser("freeze")
    a = ap.parse_args(argv)
    {"run": cmd_run, "rekey": cmd_rekey, "freeze": cmd_freeze}[a.cmd](a)


if __name__ == "__main__":
    main()
