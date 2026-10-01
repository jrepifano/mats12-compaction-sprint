"""Random judge verdicts from the P2 pilot, per family (prereg §4.7 "Judge pilot": a random 10
verdicts per family shown to Jacob). Seeded, never hand-picked.

    uv run python scripts/i1a_pilot_packet.py [--n 10] [--seed 0]
"""

from __future__ import annotations

import argparse
import functools
import json
import random
import re

from emx.i1a import contracts as C

ARMS = ("olens_rl600", "jlens", "logit_lens")
FAMILIES = ("multihop", "basic_readout_mt", "hallucination")


def cells(arm: str, family: str) -> list[dict]:
    p = C.judged_dir(arm, family) / "cells.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("result") is not None:
            out.append({"arm": arm, "family": family, **rec})
    return out


@functools.cache
def _bytes_decoder() -> dict[str, int]:
    """Inverse of GPT-2's byte-level BPE alphabet, so token-reader tokens print as text."""
    bs = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    cs, n = bs[:], 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs)}


def _detok(t: str) -> str:
    dec = _bytes_decoder()
    raw = b"".join(bytes([dec[ch]]) if ch in dec else ch.encode() for ch in t)
    return raw.decode("utf-8", errors="replace")


@functools.cache
def _readouts(arm: str, family: str) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = {}
    for line in C.readouts_path(arm, family).read_text().splitlines():
        r = json.loads(line)
        out.setdefault((r["id"], r["layer"]), []).append(r)
    return out


def _lookup(arm: str, family: str, key: str):
    """The readout the judge saw, from the committed readouts file (multihop and summary cache
    entries don't carry it in `meta`)."""
    m = re.match(r"(?:summ:)?(.+?)(?:\|L(\d+)|__L(\d+)__p(-?\d+))$", key)
    if not m:
        return None
    rid, layer = m.group(1), int(m.group(2) or m.group(3))
    rows = _readouts(arm, family).get((rid, layer), [])
    if m.group(4) is not None:
        rows = [r for r in rows if r["pos"] == int(m.group(4))]
    if not rows:
        return None
    r = rows[0]
    if "samples" in r:
        return r["samples"]
    return [_detok(t) for t in r["tokens"][:15]]


def show(rec: dict) -> str:
    meta = rec.get("meta", {})
    readout = meta.get("samples") or meta.get("tokens") or meta.get("summary")
    if not readout:
        readout = _lookup(rec["arm"], rec["family"], rec["key"]) or meta
    return (
        f"- **{rec['arm']}** `{rec['key']}`\n"
        f"  - readout: {json.dumps(readout, ensure_ascii=False)[:400]}\n"
        f"  - verdict: {json.dumps(rec['result'], ensure_ascii=False)[:600]}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    parts = [
        "# I1a P2 judge pilot: random verdicts per family\n",
        f"seed {args.seed}, {args.n} per family, drawn uniformly from all judged pilot cells\n",
    ]
    for fam in FAMILIES:
        pool = [c for arm in ARMS for c in cells(arm, fam)]
        if fam != "basic_readout_mt":  # the summaries are judge inputs; show the judge verdicts
            pool = [c for c in pool if not c["key"].startswith("summ:")]
        rng = random.Random(args.seed)
        pick = rng.sample(pool, min(args.n, len(pool)))
        parts.append(f"\n## {fam} ({len(pick)} of {len(pool)} judged cells)\n")
        parts += [show(c) for c in pick]
    out = C.ANALYSIS_DIR / "pilot_verdicts.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
