"""P36 secondary (exploratory): is the role linearly decodable from h, j and r at entity tokens?

    uv run python -m p36.probes          # reads results/states/, writes results/probes.json

Samples: each sentence's two entity-token states (slots e1, e2); label = "this token's entity is
the agent". Within a voice the label is fixed by mention order (active: first mention = agent;
passive: second), so the cross-voice tests separate role from order: a probe that reads order
scores ~0 when trained on one voice and tested on the other.

Splits (``stimuli.build_items``): train on the train items, test on (a) held-out entities, (b)
held-out verbs (both voices), and (c) across voice (train items, train on one voice, test on the
other, both directions averaged). Logistic regression on standardised features, C = 1.0, fixed
in advance (no tuning). Features: h; j = the k = 25 reconstruction (J-lens and logit-lens);
r = h - j.
"""

from __future__ import annotations

import json
from pathlib import Path

from p36 import stimuli as S

HERE = Path(__file__).resolve().parents[1]
RES = HERE / "results"


def _fit_score(Xtr, ytr, Xte, yte) -> float:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    clf = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
    clf.fit(Xtr, ytr)
    return float(clf.score(Xte, yte))


def main(res: Path = RES) -> int:
    import numpy as np
    from safetensors.torch import load_file

    idx = json.loads((res / "states" / "index.json").read_text())
    sids = idx["sids"]
    H = {k: v.float().numpy() for k, v in load_file(str(res / "states" / "h.safetensors")).items()}
    J = {
        k: v.float().numpy()
        for k, v in load_file(str(res / "states" / "j_entity_k25.safetensors")).items()
    }
    _, sents = S.load_stimuli(HERE / "data" / "stimuli.json")
    by = {s.sid: s for s in sents}
    meta = []  # per sample: (sentence index, slot, label, split, voice)
    for i, sid in enumerate(sids):
        s = by[sid]
        e1, e2 = sorted((s.agent, s.patient))
        for slot, e in ((1, e1), (2, e2)):
            meta.append((i, slot, int(e == s.agent), s.split, s.voice))
    rows_i = np.array([m[0] for m in meta])
    slots = np.array([m[1] for m in meta])
    y = np.array([m[2] for m in meta])
    split = np.array([m[3] for m in meta])
    voice = np.array([m[4] for m in meta])
    tests = {
        "held_entity": (split == "train", split == "held_entity"),
        "held_verb": (split == "train", split == "held_verb"),
        "active->passive": (
            (split == "train") & (voice == "active"),
            (split == "train") & (voice == "passive"),
        ),
        "passive->active": (
            (split == "train") & (voice == "passive"),
            (split == "train") & (voice == "active"),
        ),
    }
    out: dict = {"n_samples": len(meta), "results": []}
    for L in idx["layers"]:
        h = H[f"h_L{L}"][rows_i, slots]
        feats = {"h": h}
        for d in ("jlens", "logit"):
            key = f"j_{d}_k25_L{L}"
            if key in J:
                j = J[key][rows_i, slots - 1]
                feats[f"j_{d}"] = j
                feats[f"r_{d}"] = h - j
        for fname, X in feats.items():
            for tname, (tr, te) in tests.items():
                if not tr.any() or not te.any() or len(set(y[tr])) < 2:
                    continue
                acc = _fit_score(X[tr], y[tr], X[te], y[te])
                out["results"].append({"layer": L, "feature": fname, "test": tname, "acc": acc})
    (res / "probes.json").write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
