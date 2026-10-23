"""Experiment 1 audit before release (sprint plan R-A2): draw the audit sample from the sealed
outcomes, build the audit page, and release the seal once Jacob has checked it.

    uv run python scripts/s1_audit.py prepare [--out data/raw/sprint/s1/audit] [--n 20]
    uv run python scripts/s1_audit.py release [--out data/raw/sprint/s1/audit] \
        --labels data/raw/sprint/s1/audit/db/audit [--by jacob] [--note "..."]

`prepare` (after `partA`, `partB` and `size` have finished):
1. checks that the blind labels are locked (`--locks`: every LOCKED.json the fresh validation
   pages produced) and writes one combined LOCKED.json into `--out`, which the audit builder
   requires;
2. captures the outcome manifest (`seal.prepare_audit("S1")`) and reads the sealed files by path,
   each checked against that manifest. Reading them here is the audit's purpose (the plan: "draw
   the audit sample from these files"); nothing else reads outcomes before the release;
3. applies the frozen second-look overlay, so the audit shows the verdicts the analysis uses;
4. draws a seeded sample (default 20): Part A dropped and kept items, invented and supported
   claims (complete histories), Part B dropped items (the primary contrast's two arms first,
   then the other arms), size-test dropped items (both prefix sizes when available). Each
   card shows the verdict, the item or claim, its source context (the item's quote ±1,500
   characters of the history, or the claim's evidence message / top history snippets) and the
   summary it was graded against. No aggregate rate is computed or shown;
5. writes `bundle.private.json`, `audit.html` and `audit_key.private.json` (mode 600) under
   `--out` (a private root). Publish audit.html as a private Artifact with the `db` capability;
   Jacob marks each card Correct, Wrong: judge's reading, or Wrong: pipeline problem.

`release` reads Jacob's audit labels (the page's `audit` collection exported with ArtifactData:
the `out_dir` directory of <id>.json files, or a JSON list or mapping), checks every card is
marked and the draw matches, and refuses on any "pipeline" card (a broken card: summary or
context from another record, garbled or cut text, an item or claim not in the record) or on
JUDGE_WRONG_REFUSE or more "judge_wrong" cards (Jacob, 23 Oct, spec D15, decided before the
cards were drawn; it replaces the plan's 20% bar, which the validated precision alone would
breach about 65% of the time). A refusal means: fix and rerun the affected step, then audit a
fresh sample; there is no override (astra audit r2). It writes the validated
labels as one canonical `labels_snapshot.json` and calls `seal.release("S1", ...)` on that file
and the audit key, with the manifest captured at `prepare`, so any sealed file changed since then
forces a re-audit (`seal.AuditStale`). The page's draw fingerprint binds that manifest's digest
and every card's content, so labels saved against an earlier `prepare` are refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from compaction import s1_analysis as A
from compaction import seal
from compaction.s1_types import S1_SEED, SEAL_STAGE

JUDGE = "haiku-max"
OUT = ROOT / "data" / "raw" / "sprint" / "s1" / "audit"
LOCKS = (
    ROOT / "data/raw/sprint/s1/labels_val2/score60_sl_reloaded/LOCKED.json",
    ROOT / "data/raw/sprint/s1/labels_val3/score/LOCKED.json",
)
REPLAYS = ROOT / "data" / "raw" / "sprint" / "s1" / "replays"
AUDIT_LABELS = ("correct", "judge_wrong", "pipeline")
# Spec D15: at the validated precision (lost 0.70, invented 0.62, kept and supported ~0.9 assumed)
# the 20-card draw expects ~5.3 judge-reading errors; P(>= 10) < 0.02. More means the judge does
# much worse at scale than on the labels. Any pipeline card refuses.
JUDGE_WRONG_REFUSE = 10
AUDIT_N = 20  # the bar above is calibrated for this draw size only (astra audit r3)
# default draw: (part, kind, final verdict group) -> cards; topped up from the others if short
PLAN = (
    ("partA", "item", "dropped", 6),
    ("partA", "item", "kept", 3),
    ("partA", "claim", "invented", 4),
    ("partA", "claim", "supported", 2),
    ("partB", "item", "dropped", 3),
    ("size", "item", "dropped", 2),
)


def log(msg: str) -> None:
    print(f"S1_AUDIT {msg}", flush=True)


# ------------------------------------------------------------------------------------------
# Locks and sealed files
# ------------------------------------------------------------------------------------------


def combined_lock(paths) -> dict:
    """Every blind-label lock, or SystemExit naming the missing ones."""
    missing = [str(p) for p in paths if not Path(p).is_file()]
    if missing:
        raise SystemExit(
            f"blind labels not locked: {missing} (scripts/s1_label_score.py writes LOCKED.json "
            "once every unit of a page is labelled)"
        )
    locks = [json.loads(Path(p).read_text()) | {"path": str(p)} for p in paths]
    return {"kind": "combined", "draw": None, "locks": locks}


def read_sealed(manifest: Mapping, *, root=None) -> Callable[[str], Any]:
    """A reader over the sealed files as captured by `manifest` (the pre-release audit read):
    each file is re-hashed against the manifest, so the sample comes from exactly the audited
    version."""
    d = seal.sealed_dir(manifest["stage"], manifest["release_name"], root)

    def reader(name: str):
        want = manifest["sealed"].get(name)
        if want is None:
            raise FileNotFoundError(name)
        data = (d / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != want:
            raise seal.SealError(f"{name} changed since prepare_audit: re-run prepare")
        return seal._decode(name, data)

    return reader


def final_rows(reader, part: str) -> tuple[list[dict], list[dict]]:
    """A part's items and claims with the second-look overlay applied (frozen digest checked).
    Claims are re-checked and reported in Part A only, so other parts return no claims, as in
    `s1_analysis`."""
    items = A._read(reader, f"{part}_items_{JUDGE}.jsonl") or []
    claims = A._read(reader, f"{part}_claims_{JUDGE}.jsonl") or [] if part == "partA" else []
    overlay = A._read(reader, f"{part}_second_look_{JUDGE}.jsonl")
    if overlay is None:
        raise SystemExit(f"{part}: no sealed second-look overlay; the audit shows final verdicts")
    config = A._second_look_config("frozen")
    it2, cl2, _ = A.apply_second_look(items, claims or None, overlay, config=config)
    return it2 or [], cl2 or []


def pools(reader) -> dict[tuple[str, str, str], list[dict]]:
    out: dict[tuple[str, str, str], list[dict]] = {}
    for part in ("partA", "partB", "size"):
        try:
            items, claims = final_rows(reader, part)
        except FileNotFoundError:
            continue
        for r in items:
            if A.usable_item(r) and r.get("grade") in ("dropped", "kept"):
                out.setdefault((part, "item", r["grade"]), []).append(r)
        for r in claims if part == "partA" else ():  # only Part A reports claims
            if not r.get("valid") or not r.get("history_complete"):
                continue
            if A.is_invented_claim(r):
                out.setdefault((part, "claim", "invented"), []).append(r)
            elif r.get("verdict") == "supported":
                out.setdefault((part, "claim", "supported"), []).append(r)
    return out


PRIMARY_ARMS = A.PRIMARY_CONTRAST  # Part B cards cover these two arms first


def _groups(part: str, rows: list[dict], rng: random.Random) -> list[list[dict]]:
    """A cell's rows split into the groups its cards must cover, in coverage order: Part B by
    arm (the primary contrast's two arms first, then the others in seeded order), the size test
    by prefix size (ascending); any other cell is one group."""
    if part == "partB":
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(str(r.get("arm")), []).append(r)
        rest = sorted(a for a in by if a not in PRIMARY_ARMS)
        rng.shuffle(rest)
        return [by[a] for a in (*PRIMARY_ARMS, *rest) if a in by]
    if part == "size":
        by = {}
        for r in rows:
            by.setdefault(str(r.get("prefix")), []).append(r)
        return [by[k] for k in sorted(by, key=lambda x: (len(x), x))]
    return [rows]


def _take(groups: list[list[dict]], want: int, rng: random.Random) -> list[dict]:
    """`want` rows, one group at a time in order (round robin), each pick random in its group."""
    pools_ = [list(g) for g in groups]
    out: list[dict] = []
    while len(out) < want and any(pools_):
        for g in pools_:
            if g and len(out) < want:
                out.append(g.pop(rng.randrange(len(g))))
    return out


def draw(pool: Mapping, n: int, seed: int) -> list[tuple[tuple, dict]]:
    """The seeded sample: PLAN's cells (scaled to `n`), Part B covering the primary contrast's
    arms first and the size test both prefix sizes (`_groups`), topped up from the remaining
    rows to `n`."""
    rng = random.Random(f"s1-audit:{seed}")
    scale = n / sum(k for *_, k in PLAN)
    chosen: list[tuple[tuple, dict]] = []
    taken: set[int] = set()
    for part, kind, group, k in PLAN:
        rows = sorted(pool.get((part, kind, group), []), key=_row_id)
        want = min(len(rows), max(1, round(k * scale)) if rows else 0)
        for r in _take(_groups(part, rows, rng), want, rng):
            chosen.append(((part, kind, group), r))
            taken.add(id(r))
    rest = [
        (cell, r)
        for cell in sorted(pool)
        for r in sorted(pool[cell], key=_row_id)
        if id(r) not in taken
    ]
    rng.shuffle(rest)
    chosen += rest[: max(0, n - len(chosen))]
    rng.shuffle(chosen)
    return chosen[:n]


def _row_id(r: Mapping) -> str:
    return f"{r['rid']}|{r.get('item_id') or r.get('claim_id')}"


# ------------------------------------------------------------------------------------------
# Context: the history and summary a row was graded against
# ------------------------------------------------------------------------------------------


class Sources:
    """Loads records, replay summaries and size prefixes once each."""

    def __init__(self):
        from compaction import s1_data, s1_judge, s1_replay

        self.s1_data, self.s1_judge, self.s1_replay = s1_data, s1_judge, s1_replay
        self._recs: dict[str, dict] = {}
        self._views: dict[str, Any] = {}
        self._reps: dict[str, dict] = {}
        self._prefixes: dict[str, dict] = {}

    def rec(self, rid: str) -> dict:
        if rid not in self._recs:
            self._recs[rid] = self.s1_data.load(rid)
        return self._recs[rid]

    def prefix(self, pid: str) -> dict:
        if pid not in self._prefixes:
            orig = pid.split("#prefix", 1)[0]
            for p in self.s1_replay.size_prefixes(self.rec(orig)):
                self._prefixes[p["id"]] = p
        return self._prefixes[pid]

    def replays(self, arm: str) -> dict:
        if arm not in self._reps:
            path = REPLAYS / f"{arm}.jsonl"
            reps = self.s1_replay.read_replays(path).values()
            self._reps[arm] = {str(r["base_rid"]): r for r in reps if not r.get("error")}
        return self._reps[arm]

    def base(self, part: str, r: Mapping) -> dict:
        b = str(r.get("base_rid") or r["rid"])
        return self.prefix(b) if part == "size" else self.rec(b)

    def history(self, part: str, r: Mapping):
        b = str(r.get("base_rid") or r["rid"])
        if b not in self._views:
            self._views[b] = self.s1_judge.judge_view(self.base(part, r))
        return self._views[b]

    def summary(self, part: str, r: Mapping) -> str:
        arm = r.get("arm") or "natural"
        if arm == "natural":
            return self.base(part, r).get("summary") or ""
        if part == "size":
            reps = {
                str(x["id"]).rsplit("@", 1)[0]: x
                for x in self.s1_replay.read_replays(REPLAYS / "size.jsonl").values()
                if not x.get("error")
            }
            return (reps.get(str(r["base_rid"])) or {}).get("summary") or ""
        return (self.replays(arm).get(str(r["base_rid"])) or {}).get("summary") or ""


def context(src: Sources, part: str, kind: str, r: Mapping) -> str:
    from s1_label_page import ITEM_CONTEXT, claim_snippets, item_context

    view = src.history(part, r)
    text = view.text
    if kind == "item":
        c = item_context(text, str(r.get("quote") or ""))
        return f"{c['ctx_before']}⟦{c['ctx_quote']}⟧{c['ctx_after']}"
    msg = r.get("msg")
    if isinstance(msg, int) and msg in view.spans:
        s, e = view.spans[msg]
        return text[max(s, e - 2 * ITEM_CONTEXT) : e] if e - s > 2 * ITEM_CONTEXT else text[s:e]
    snips = claim_snippets(str(r.get("claim") or ""), text, k=3)
    return "\n…\n".join(snips) or "(no matching history passage found by word overlap)"


def card(src: Sources, cell: tuple, r: Mapping) -> dict:
    part, kind, group = cell
    sl = r.get("second_look") or {}
    return {
        "kind": kind,
        "verdict": group,
        "text": (r.get("claim") if kind == "claim" else r.get("text")) or "",
        "context": context(src, part, kind, r),
        "summary": src.summary(part, r),
        "private": {
            "part": part,
            "rid": r["rid"],
            "row_id": r.get("item_id") or r.get("claim_id"),
            "arm": r.get("arm"),
            "source": r.get("source"),
            "availability": r.get("availability"),
            "first_verdict": sl.get("first_verdict"),
            "second_look_category": sl.get("category"),
            "quote": r.get("quote") or r.get("summary_quote"),
        },
    }


# ------------------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------------------


def cmd_prepare(a) -> None:
    import s1_label_page as P

    out = P.require_private(Path(a.out))
    out.mkdir(parents=True, exist_ok=True)
    lock = combined_lock(a.locks)
    P._atomic(out / P.LOCK_NAME, (json.dumps(lock, indent=1) + "\n").encode())
    manifest = seal.prepare_audit(SEAL_STAGE)
    log(f"manifest {manifest['digest'][:16]}: {len(manifest['sealed'])} sealed files")
    pool = pools(read_sealed(manifest))
    log("pool sizes " + json.dumps({"|".join(k): len(v) for k, v in sorted(pool.items())}))
    chosen = draw(pool, a.n, a.seed)
    src = Sources()
    items = [card(src, cell, r) for cell, r in chosen]
    bundle = {"items": items, "manifest": manifest}
    P._atomic(
        out / "bundle.private.json",
        (json.dumps(bundle, indent=1, ensure_ascii=False) + "\n").encode(),
        mode=0o600,
    )
    html, key = P.build_audit(bundle, labels_dir=out, seed=a.seed)
    info = P.write_audit(out, html, key)
    log(
        f"page {info['page']} ({info['bytes']:,} bytes, sha256 {info['sha256'][:16]}); key {info['key']}"
    )
    log(f"{len(items)} cards, draw {key['draw']}; publish audit.html privately with db")


def audit_labels(path: Path) -> dict[str, dict]:
    """{card id: data} from an ArtifactData export: a list of {id, collection, data}, a
    {collection: {id: data}} mapping, or a directory of <id>.json files."""
    p = Path(path)
    if p.is_dir():
        return {f.stem: json.loads(f.read_text()) for f in sorted(p.glob("*.json"))}
    obj = json.loads(p.read_text())
    if isinstance(obj, list):
        return {x["id"]: x["data"] for x in obj if x.get("collection", "audit") == "audit"}
    return dict(obj.get("audit") or obj)


def check_labels(key: Mapping, labels: Mapping[str, Mapping]) -> dict:
    """Card ids per mark, or SystemExit when a card is unmarked (an old two-way "incorrect"
    counts as unmarked: its reason is unknown) or a label is from another draw."""
    ids = set(key["audit"])
    wrong_draw = sorted(i for i, d in labels.items() if d.get("draw") != key["draw"])
    if wrong_draw:
        raise SystemExit(f"labels from another draw: {wrong_draw[:5]}")
    unmarked = sorted(i for i in ids if (labels.get(i) or {}).get("label") not in AUDIT_LABELS)
    if unmarked:
        raise SystemExit(f"{len(unmarked)} of {len(ids)} cards not marked yet: {unmarked[:5]}")
    by = {m: sorted(i for i in ids if labels[i]["label"] == m) for m in AUDIT_LABELS[1:]}
    return {"n": len(ids), **by}


SNAPSHOT = "labels_snapshot.json"


def write_snapshot(out: Path, key: Mapping, labels: Mapping[str, Mapping]) -> Path:
    """The validated labels as one canonical JSON file (sorted keys, only this draw's cards), the
    file the release hashes: an export directory can't be hashed as one artifact."""
    snap = {
        "draw": key["draw"],
        "labels": {i: dict(labels[i]) for i in sorted(key["audit"])},
    }
    p = Path(out) / SNAPSHOT
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    tmp.replace(p)
    return p


def release_audit(out: Path, labels_path: Path, *, by: str, note: str = "", root=None) -> dict:
    """Validate Jacob's audit labels against the audit key in `out`, snapshot them, and release
    the seal bound to the manifest captured at `prepare`."""
    import s1_label_page as P

    key_path = Path(out) / P.AUDIT_KEY_NAME
    key = json.loads(key_path.read_text())
    labels = audit_labels(Path(labels_path))
    if len(key["audit"]) != AUDIT_N:
        raise SystemExit(
            f"the audit has {len(key['audit'])} cards; the release bar (spec D15) is calibrated "
            f"for {AUDIT_N}: `prepare` again with the default --n"
        )
    c = check_labels(key, labels)
    summary = (
        f"{c['n']} cards, {len(c['judge_wrong'])} judge's reading wrong, "
        f"{len(c['pipeline'])} pipeline problems"
    )
    log(f"{summary} checked")
    for mark in AUDIT_LABELS[1:]:
        for i in c[mark]:
            k = key["audit"][i]
            log(
                f"  {mark}: {k['kind']} {k['verdict']} {k['private'].get('part')} "
                f"{k['private'].get('rid')} | note: {(labels[i].get('note') or '')[:120]}"
            )
    fix = "fix and rerun the affected step, then `prepare` a fresh audit sample (new seed)"
    if c["pipeline"]:  # spec D15
        raise SystemExit(f"{len(c['pipeline'])} card(s) marked as a pipeline problem: {fix}")
    if len(c["judge_wrong"]) >= JUDGE_WRONG_REFUSE:
        raise SystemExit(
            f"{len(c['judge_wrong'])} of {c['n']} cards wrong on the judge's reading "
            f"(refuse at {JUDGE_WRONG_REFUSE}; the validated precision expects ~5): {fix}"
        )
    snap = write_snapshot(Path(out), key, labels)
    rec = seal.release(
        SEAL_STAGE,
        [snap, key_path],
        by=by,
        manifest=key["audit_manifest"],
        note=note or f"audit {key['draw']}: {summary}",
        root=root,
    )
    log(
        f"released {rec['stage']}/{rec['release_name']} at {rec['released_utc']} "
        f"({len(rec['sealed'])} files, digest {rec['digest'][:16]})"
    )
    return rec


def cmd_release(a) -> None:
    release_audit(Path(a.out), Path(a.labels), by=a.by, note=a.note or "")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", default=str(OUT), help="audit directory (private root)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", parents=[common])
    p.add_argument("--n", type=int, default=AUDIT_N)
    p.add_argument("--seed", type=int, default=S1_SEED)
    p.add_argument("--locks", nargs="+", default=[str(x) for x in LOCKS])
    r = sub.add_parser("release", parents=[common])
    r.add_argument("--labels", required=True)
    r.add_argument("--by", default="jacob")
    r.add_argument("--note", default="")
    a = ap.parse_args(argv)
    {"prepare": cmd_prepare, "release": cmd_release}[a.cmd](a)


if __name__ == "__main__":
    main()
