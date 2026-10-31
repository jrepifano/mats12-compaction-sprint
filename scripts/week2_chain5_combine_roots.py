"""One run root for the readout from chains in several attempt roots (plan v0.4.1).

    uv run python scripts/week2_chain5_combine_roots.py <out root> u1=<root> u2=<root> ...

The five-task readout reads one root and every chain in it. After the Haiku rerun, u1 and u2
live in the first root and u3 and u4 in the attempt-2 root. Each chain is named once, with the
root it comes from. The output root holds:
- state.json: each named chain's entry from its own root; provenance is
  {"combined_from": {chain: {"root", "provenance"}}}; capacity is every source's records,
  each tagged with its root.
- chains/<id>: a symlink to that chain's directory in its root (nothing is copied).

It refuses a chain named twice, a chain missing from its root, sources run with different
phase limits (`max_phases`; the readout reads one capacity record for every chain), and an
output root that exists with other content.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def combine(out: Path, picks: list[tuple[str, Path]]) -> dict:
    ids = [cid for cid, _ in picks]
    dup = sorted({c for c in ids if ids.count(c) > 1})
    if dup:
        raise SystemExit(f"refused: chain(s) named more than once: {', '.join(dup)}")
    chains, prov, cap = {}, {}, []
    for cid, src in picks:
        src = src.resolve()
        st = json.loads((src / "state.json").read_text())
        if cid not in st["chains"] or not (src / "chains" / cid).is_dir():
            raise SystemExit(f"refused: chain {cid} is not in {src}")
        chains[cid] = st["chains"][cid]
        prov[cid] = {"root": str(src), "provenance": st.get("provenance")}
        cap += [{**c, "root": str(src)} for c in st.get("capacity") or []
                if {**c, "root": str(src)} not in cap]
    limits = {c.get("max_phases") for c in cap}
    if len(limits) > 1:  # the readout's n_planned reads one record for every chain
        raise SystemExit(f"refused: the sources ran with different phase limits {limits}")
    state = {"chains": dict(sorted(chains.items())), "provenance": {"combined_from": prov},
             "capacity": cap}
    text = json.dumps(state, indent=1)
    sp = out / "state.json"
    if sp.exists() and sp.read_text() != text:
        raise SystemExit(f"refused: {sp} exists with other content")
    (out / "chains").mkdir(parents=True, exist_ok=True)
    for cid, src in picks:
        link, target = out / "chains" / cid, (src.resolve() / "chains" / cid)
        if link.is_symlink() and link.resolve() == target.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise SystemExit(f"refused: {link} exists and points elsewhere")
        link.symlink_to(target, target_is_directory=True)
    sp.write_text(text)
    return state


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or any("=" not in a for a in argv[1:]):
        raise SystemExit(__doc__)
    picks = [(a.split("=", 1)[0], Path(a.split("=", 1)[1])) for a in argv[1:]]
    st = combine(Path(argv[0]), picks)
    for cid, p in st["provenance"]["combined_from"].items():
        print(f"{cid}: {st['chains'][cid].get('state')}  <- {p['root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
