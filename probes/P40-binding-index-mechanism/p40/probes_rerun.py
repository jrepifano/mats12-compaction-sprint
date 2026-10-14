"""Rerun plan arm B's probes on the laptop from the saved entity states (L44, L48), with the
intercept applied at evaluation (review r1: the main run's ``results/probes.json`` dropped it).

    uv run python -m p40.probes_rerun     # -> results/probes_L44_L48_fixed.json

Vectors for the cosines are read from ``results/vectors.safetensors``.
"""

from __future__ import annotations

import json
from pathlib import Path

from p40 import session as SE
from p40 import stimuli as S

HERE = Path(__file__).resolve().parents[1]


def main() -> int:
    import torch
    from safetensors.torch import load_file

    rdir = HERE / "results"
    st = load_file(str(rdir / "states" / "entity_states.safetensors"))
    index = json.loads((rdir / "states" / "index.json").read_text())
    sents = {s.sid: s for s in S.load_stimuli(HERE / "data" / "stimuli.json")}
    idx = {sid: i for i, sid in enumerate(index["sids"])}
    E = {L: st[f"E_L{L}"] for L in index["layers"]}
    vt = load_file(str(rdir / "vectors.safetensors"))
    V: dict[str, dict[int, torch.Tensor]] = {}
    for k, t in vt.items():
        name, layer = k.rsplit("_L", 1)
        V.setdefault(name, {})[int(layer)] = t.float()
    trans = [sents[sid] for sid in index["sids"]
             if sents[sid].family == "trans" and not sents[sid].shift]  # fmt: skip
    torch.manual_seed(0)
    res = SE.probe_table(E, trans, idx, V)
    (rdir / "probes_L44_L48_fixed.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
