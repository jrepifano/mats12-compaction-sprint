"""P40 pod session (plan.md): one model load, every arm, rows to ``results/rows/<phase>.jsonl``.

    python -m p40.session download     # Qwen + J-lens -> <artifacts>/..., results/download_paths.json
    python -m p40.session all [--limit-items N]

Phases (timed into ``results/timing.json``; printed as ``P40_STEP <name> ...``), most important
first so a short pod still answers the primary question:

1. ``renders``; ``capture``: every sentence's entity-token states (mention order, then the cap
   countries) at ``CAPT_LAYERS``; full-sequence states of the A/P prompts at the localisation
   layers (``LOC_LAYERS``).
2. ``smoke``: an identity patch and a zero-vector add move the answer logits by exactly 0; >= 18
   of the first 20 prompts of each family correct; one batch timed; the whole grid projected and
   trimmed (plan's order: F's L40/L52, then L to every 4 layers, then T) if over 80% of the time
   left. A failure ends the session (rc 3).
3. ``baseline``: every prompt unpatched; gate 1 (>= 0.90 per question) per construction, reported.
4. ``gate2``: A/P twin, both entity tokens, L48: flip >= 0.80, else stop (rc 0, status says why).
5. ``F@48`` (primary), ``V@48``, ``C``, ``L``, ``T``, ``F@other``, ``V@band``; then ``probes``
   (decodability and lens readouts of the vectors) and ``save``.

Rows: ``{phase, arm, recip, q, layers, mode, top1, a_rec, a_alt, logit_rec, logit_alt, logz,
cands, ...meta}``. ``a_rec`` is the recipient's correct answer token, ``a_alt`` the token the
tested hypothesis moves the answer to. Scoring is ``p40.analyze`` (laptop).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from p40 import stimuli as S

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "results"
SEED = 20261013

MODEL_ID = "Qwen/Qwen3.6-27B"
MODEL_REV = "6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"
JLENS = ("neuronpedia/jacobian-lens", "b25d72a96b79c8e309d6625955a98751da47e67a",
         "qwen3.6-27b/jlens/Salesforce-wikitext/Qwen3.6-27B_jacobian_lens_n1000.pt")  # fmt: skip

L_PRIMARY = 48
LOC_LAYERS: tuple[int, ...] = (16, 20, 24, 28, 32, 36, 40, 42, 44, 46, 48, 50, 52, 54, 56, 60)
LOC_LAYERS_TRIM: tuple[int, ...] = (16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60)
LOC_SITES: tuple[str, ...] = ("e_first", "e_second", "e_both", "verb", "was", "by", "det",
                              "period", "sentence", "question", "last")  # fmt: skip
LOC_XDONORS: tuple[str, ...] = ("xsame", "shift_same", "shift_twin")
F_SINGLE_LAYERS: tuple[int, ...] = (48, 44)
F_OTHER_LAYERS: tuple[int, ...] = (40, 52)
BAND: tuple[int, ...] = (44, 45, 46, 47, 48, 49, 50)
T_LAYERS: tuple[int, ...] = (48, 44)
C_LAYERS: tuple[int, ...] = (40, 44, 48, 52)
V_CONFIGS: dict[str, tuple[int, ...]] = {"L48": (48,), "band": BAND}
ALPHAS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)
N_RAND = 5
LENS_LAYERS: tuple[int, ...] = (44, 48)
CAPT_LAYERS: tuple[int, ...] = tuple(sorted(set(LOC_LAYERS) | set(BAND) | set(C_LAYERS)))
GATE1_MIN = 0.90
GATE2_MIN = 0.80
SMOKE_ACC_MIN = 18
ID_TOL = 0.0
K_LENS = 25
CAP_MIN_ACC = 0.5  # follow-up F1 stops if the bold prefill still fails


@dataclass
class Config:
    n_layers: int = 64
    batch: int = 128
    limit_items: int = 0  # keep the first N items of every (family, split) (tests)
    out: Path = OUT
    lens_path: str = ""
    max_hours: float = 1.9
    t0: float = field(default_factory=time.time)
    loc_layers: tuple[int, ...] = LOC_LAYERS
    f_other_layers: tuple[int, ...] = F_OTHER_LAYERS
    run_t: bool = True
    l_primary: int = L_PRIMARY
    f_single_layers: tuple[int, ...] = F_SINGLE_LAYERS
    band: tuple[int, ...] = BAND
    t_layers: tuple[int, ...] = T_LAYERS
    c_layers: tuple[int, ...] = C_LAYERS
    lens_layers: tuple[int, ...] = LENS_LAYERS
    k_lens: int = K_LENS

    @property
    def capt_layers(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.loc_layers) | set(self.band) | set(self.c_layers)
                            | set(self.f_single_layers) | set(self.f_other_layers)
                            | set(self.t_layers) | {self.l_primary}))  # fmt: skip


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[p40 {utc()}] {msg}", flush=True)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.replace(tmp, path)


class Timer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def step(self, name: str):
        timer = self

        class _S:
            def __enter__(self):
                self.t = time.time()
                self.rec = {"start": utc()}
                print(f"P40_STEP {name} start {self.rec['start']}", flush=True)
                return self.rec

            def __exit__(self, et, ev, tb):
                self.rec.update(end=utc(), seconds=round(time.time() - self.t, 2))
                if et is not None:
                    self.rec["failed"] = True
                key, k = name, 2
                while key in timer.data:
                    key, k = f"{name}#{k}", k + 1
                timer.data[key] = self.rec
                write_json(timer.path, timer.data)
                print(f"P40_STEP {name} done {self.rec['end']} {self.rec['seconds']}", flush=True)

        return _S()


def M():
    from p40 import model

    return model


def flip_key(key: str) -> str:
    return {"a": "p", "p": "a", "h": "l", "l": "h"}[key]


# --------------------------------------------------------------------- vectors (pure torch)
def mean_diff(rows: list[tuple[Any, int]], hi: int, lo: int) -> Any:
    """mean(state | label == hi) - mean(state | label == lo) over ``rows`` [(state [d], label)]."""
    import torch

    a = [x for x, y in rows if y == hi]
    b = [x for x, y in rows if y == lo]
    if not a or not b:
        raise ValueError("empty class")
    return torch.stack(a).float().mean(0) - torch.stack(b).float().mean(0)


def pc1(states: Any, groups: list[str], labels: list[int]) -> tuple[Any, dict]:
    """Top principal direction of ``states`` [n, d] after subtracting each group's mean (groups =
    entity identity), signed to correlate positively with ``labels``; unit norm. Also returns its
    point-biserial correlation with ``labels``."""
    import torch

    X = states.float().clone()
    for g in set(groups):
        ix = [i for i, x in enumerate(groups) if x == g]
        X[ix] -= X[ix].mean(0)
    X = X - X.mean(0)
    _, sv, vt = torch.linalg.svd(X, full_matrices=False)
    v = vt[0]
    proj = X @ v
    y = torch.tensor(labels, dtype=torch.float32)
    r = float(torch.corrcoef(torch.stack([proj, y]))[0, 1])
    if r < 0:
        v, r = -v, -r
    return v / v.norm(), {"corr": r, "var_share": float(sv[0] ** 2 / (sv**2).sum())}


# --------------------------------------------------------------------- the session
class Session:
    def __init__(self, backend: Any, cfg: Config) -> None:
        self.b = backend
        self.cfg = cfg
        self.out = cfg.out
        self.timer = Timer(self.out / "timing.json")
        sents = S.load_stimuli(HERE / "data" / "stimuli.json")
        if cfg.limit_items:  # the first N items of every (family, split): tests
            firsts: dict[tuple[str, str], list[int]] = defaultdict(list)
            for s in sents:
                f = firsts[(s.family, s.split)]
                if s.item not in f and len(f) < cfg.limit_items:
                    f.append(s.item)
            sents = [s for s in sents if s.item in firsts[(s.family, s.split)]]
        self.sents = sents
        self.idx = {s.sid: i for i, s in enumerate(sents)}
        self.by = {s.sid: s for s in sents}
        self.E: dict[int, Any] = {}  # layer -> [N, 4, d] entity-slot states (model dtype, CPU)
        self.Fs: dict[int, dict[tuple[str, str], Any]] = {}  # layer -> {(sid, q): [n, d]}
        self.V: dict[str, dict[int, Any]] = {}  # vector -> {layer: fp32 [d]}
        self.base: dict[tuple[str, str], dict] = {}
        self.status: dict[str, Any] = {"started": utc()}
        self.dropped: set[str] = set()

    # ---------------------------------------------------------------- basics
    def time_left(self) -> float:
        return self.cfg.max_hours * 3600 - (time.time() - self.cfg.t0)

    def renders(self) -> None:
        S.check_vocab(self.b.tokenizer)
        self.R = {(s.sid, q): S.render(self.b.tokenizer, s, q)
                  for s in self.sents for q in S.questions(s)}  # fmt: skip
        self._tok: dict[str, int] = {}

    def tok(self, word: str) -> int:
        if word not in self._tok:
            self._tok[word] = S.answer_token(self.b.tokenizer, word)
        return self._tok[word]

    def slots(self, s: S.Sent) -> list[str]:
        return list(s.ents) + ["@" + c for c in s.attrs]

    def slot(self, sid: str, name: str) -> int:
        return self.slots(self.by[sid]).index(name)

    def answer_word(self, sid: str, q: str) -> str:
        return S.answer(self.by[sid], q)

    def other_word(self, sid: str, q: str) -> str:
        """The other option of a two-option sentence (entity or capital)."""
        s = self.by[sid]
        c = S.candidates(s, q)
        assert len(c) == 2
        a = self.answer_word(sid, q)
        return c[1 - c.index(a)]

    def correct(self, sid: str, q: str) -> bool:
        r = self.base.get((sid, q))
        return r is not None and r["top1"] == r["a_rec"]

    # ---------------------------------------------------------------- captures
    def capture(self) -> None:
        import torch

        layers = list(self.cfg.capt_layers)
        groups: dict[int, list[str]] = defaultdict(list)
        for s in self.sents:
            groups[self.R[(s.sid, S.questions(s)[0])]["n"]].append(s.sid)
        parts: dict[int, list[tuple[list[int], Any]]] = defaultdict(list)
        for _, sids in sorted(groups.items()):
            for lo in range(0, len(sids), self.cfg.batch):
                chunk = sids[lo : lo + self.cfg.batch]
                ids, pos = [], []
                for sid in chunk:
                    s = self.by[sid]
                    r = self.R[(sid, S.questions(s)[0])]
                    p = [r["ent"][x] for x in self.slots(s)]
                    pos.append(p + [p[-1]] * (4 - len(p)))
                    ids.append(r["ids"])
                got = M().capture(self.b, ids, pos, layers)
                for L in layers:
                    parts[L].append(([self.idx[x] for x in chunk], got[L]))
        for L in layers:
            d = parts[L][0][1].shape[-1]
            E = torch.empty(len(self.sents), 4, d, dtype=parts[L][0][1].dtype)
            for ix, t in parts[L]:
                E[ix] = t
            self.E[L] = E
        # the sentence prefix is the same tokens under every question
        for s in self.sents:
            r0 = self.R[(s.sid, S.questions(s)[0])]
            for q in S.questions(s)[1:]:
                assert self.R[(s.sid, q)]["ent"] == r0["ent"], s.sid

    def capture_full(self, keys: list[tuple[str, str]], layers: list[int]) -> None:
        groups: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for k in keys:
            groups[self.R[k]["n"]].append(k)
        for L in layers:
            self.Fs.setdefault(L, {})
        for n, ks in sorted(groups.items()):
            for lo in range(0, len(ks), self.cfg.batch):
                chunk = ks[lo : lo + self.cfg.batch]
                ids = [self.R[k]["ids"] for k in chunk]
                got = M().capture(self.b, ids, [list(range(n))] * len(chunk), layers)
                for L in layers:
                    for i, k in enumerate(chunk):
                        self.Fs[L][k] = got[L][i].clone()

    # ---------------------------------------------------------------- jobs and forwards
    def job(self, recip: str, q: str, layers: tuple[int, ...], mode: str, pos: list[int],
            src: list[tuple], a_alt: str, arm: str, **meta: Any) -> dict:  # fmt: skip
        assert len(pos) == len(src) and pos, (recip, arm)
        return {"recip": recip, "q": q, "layers": tuple(layers), "mode": mode, "pos": pos,
                "src": src, "a_alt": a_alt, "arm": arm, "meta": meta}  # fmt: skip

    def ent_copy(self, recip: str, q: str, donor: str, names: list[str], layers, a_alt, arm,
                 **meta) -> dict | None:  # fmt: skip
        """Identity-aligned copy: each named entity token gets the donor's state of that entity."""
        if not (self.correct(recip, q) and self.correct(donor, q)):
            return None
        r = self.R[(recip, q)]
        return self.job(
            recip,
            q,
            layers,
            "set",
            [r["ent"][e] for e in names],
            [("e", donor, e) for e in names],
            a_alt,
            arm,
            donor=donor,
            **meta,
        )

    def site_copy(self, recip: str, q: str, donor: str, site: str, layers, a_alt, arm,
                  **meta) -> dict | None:  # fmt: skip
        """Position-aligned copy of a non-entity site from the donor's full capture (twins)."""
        if not (self.correct(recip, q) and self.correct(donor, q)):
            return None
        pr = self.R[(recip, q)]["sites"][site]
        pd = self.R[(donor, q)]["sites"][site]
        if not pr:
            return None
        assert pr == pd, (recip, donor, site)
        return self.job(recip, q, layers, "set", pr, [("f", donor, q, p) for p in pr], a_alt,
                        arm, donor=donor, site=site, **meta)  # fmt: skip

    def vec_edit(self, recip: str, q: str, edits: list[tuple[str, str, float]], layers, a_alt,
                 arm, **meta) -> dict | None:  # fmt: skip
        """Add ``coef * V[vec][layer]`` at each named slot (entity or "@country")."""
        if not self.correct(recip, q):
            return None
        r = self.R[(recip, q)]
        return self.job(
            recip,
            q,
            layers,
            "add",
            [r["ent"][e] for e, _, _ in edits],
            [("v", v, c) for _, v, c in edits],
            a_alt,
            arm,
            **meta,
        )

    def values(self, job: dict, layer: int) -> Any:
        import torch

        out = []
        for src in job["src"]:
            if src[0] == "e":
                out.append(self.E[layer][self.idx[src[1]], self.slot(src[1], src[2])].float())
            elif src[0] == "f":
                out.append(self.Fs[layer][(src[1], src[2])][src[3]].float())
            else:
                out.append(self.V[src[1]][layer] * src[2])
        return torch.stack(out)

    def n_batches(self, jobs: list[dict]) -> int:
        groups: dict[tuple, int] = defaultdict(int)
        for j in jobs:
            if j and self.keep(j):
                groups[self._gkey(j)] += 1
        return sum(-(-n // self.cfg.batch) for n in groups.values())

    def keep(self, j: dict) -> bool:
        """False for jobs of a family the smoke unit dropped (baseline rows are always kept)."""
        return j["arm"] == "base" or self.by[j["recip"]].family not in self.dropped

    def _gkey(self, j: dict) -> tuple:
        return (self.R[(j["recip"], j["q"])]["n"], j["layers"], j["mode"], len(j["pos"]))

    def run_jobs(self, jobs: list[dict | None], phase: str) -> list[dict]:
        import torch

        jobs = [j for j in jobs if j and self.keep(j)]
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for j in jobs:
            groups[self._gkey(j)].append(j)
        rows: list[dict] = []
        path = self.out / "rows" / f"{phase}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for (_, layers, mode, _), js in sorted(groups.items(), key=lambda kv: str(kv[0])):
                for lo in range(0, len(js), self.cfg.batch):
                    chunk = js[lo : lo + self.cfg.batch]
                    ids = [self.R[(j["recip"], j["q"])]["ids"] for j in chunk]
                    ans = [[self.tok(self.answer_word(j["recip"], j["q"])), self.tok(j["a_alt"])]
                           for j in chunk]  # fmt: skip
                    cands = [[self.tok(w) for w in S.candidates(self.by[j["recip"]], j["q"])]
                             for j in chunk]  # fmt: skip
                    edits = {}
                    if layers:
                        pos = torch.tensor([j["pos"] for j in chunk])
                        for L in layers:
                            vals = torch.stack([self.values(j, L) for j in chunk])
                            edits[L] = (pos, vals, mode)
                    got = M().forward_patched(self.b, ids, edits or None, answer_ids=ans)
                    for i, j in enumerate(chunk):
                        row = {
                            "phase": phase, "arm": j["arm"], "recip": j["recip"], "q": j["q"],
                            "layers": list(layers), "mode": mode, **j["meta"],
                            "top1": int(got["top1"][i]), "a_rec": ans[i][0], "a_alt": ans[i][1],
                            "cands": cands[i],
                            "logit_rec": round(float(got["ans"][i, 0]), 4),
                            "logit_alt": round(float(got["ans"][i, 1]), 4),
                            "logz": round(float(got["logz"][i]), 4),
                        }  # fmt: skip
                        rows.append(row)
                        fh.write(json.dumps(row) + "\n")
        return rows

    # ---------------------------------------------------------------- phases: jobs
    def baseline_jobs(self) -> list[dict]:
        out = []
        for s in self.sents:
            for q in S.questions(s):
                a_alt = self.other_word(s.sid, q) if s.family != "ditr" else S.answer(s, q)
                out.append({"recip": s.sid, "q": q, "layers": (), "mode": "set", "pos": [0],
                            "src": [None], "a_alt": a_alt, "arm": "base",
                            "meta": {"family": s.family, "cons": s.cons, "shift": s.shift,
                                     "split": s.split}})  # fmt: skip
        return out

    def trans(self, shift: bool = False) -> list[S.Sent]:
        return [s for s in self.sents if s.family == "trans" and bool(s.shift) == shift]

    def tsid(self, s: S.Sent, cons: str, key: str) -> str:
        return f"t{s.item}-{cons}-{key}"

    def f_jobs(self, layers: tuple[int, ...], sites: tuple[str, ...]) -> list[dict]:
        """The feature factorial: every recipient x donor construction x roles kept/reversed."""
        out = []
        for s in self.trans():
            for dc in S.CONS:
                for keep in (True, False):
                    if dc == s.cons and keep:
                        continue
                    donor = self.tsid(s, dc, s.key if keep else flip_key(s.key))
                    if donor not in self.by:
                        continue
                    d = self.by[donor]
                    pats = set()
                    for e in s.ents:
                        fr, fd = S.feats(s.cons, s.role_of(e)), S.feats(dc, d.role_of(e))
                        pats.add("".join(str(int(a != b)) for a, b in zip(fr, fd, strict=True)))
                    assert len(pats) == 1, (s.sid, donor)
                    pat = pats.pop()
                    for q in S.questions(s):
                        for site in sites:
                            names = {"both": list(s.ents), "first": [s.ents[0]],
                                     "second": [s.ents[1]]}[site]  # fmt: skip
                            for L in layers:
                                out.append(self.ent_copy(
                                    s.sid, q, donor, names, (L,), self.other_word(s.sid, q), "F",
                                    rcons=s.cons, dcons=dc, keep=keep, pat=pat, site=site,
                                    item=s.item, split=s.split,
                                ))  # fmt: skip
        return out

    def gate2_jobs(self) -> list[dict]:
        out = []
        for s in self.trans():
            if s.cons in ("A", "P"):
                for q in S.questions(s):
                    out.append(self.ent_copy(
                        s.sid, q, self.tsid(s, s.cons, flip_key(s.key)), list(s.ents),
                        (self.cfg.l_primary,), self.other_word(s.sid, q), "gate2",
                        rcons=s.cons, item=s.item,
                    ))  # fmt: skip
        return out

    def loc_jobs(self) -> list[dict]:
        out = []
        for s in self.trans():
            if s.cons not in ("A", "P"):
                continue
            twin = self.tsid(s, s.cons, flip_key(s.key))
            other = "P" if s.cons == "A" else "A"
            xd = {"xsame": self.tsid(s, other, s.key), "shift_same": s.sid + "-s16",
                  "shift_twin": twin + "-s16"}  # fmt: skip
            for q in S.questions(s):
                alt = self.other_word(s.sid, q)
                for L in self.cfg.loc_layers:
                    for site in LOC_SITES:
                        meta = {"site": site, "donor_kind": "twin", "rcons": s.cons,
                                "item": s.item, "layer": L}  # fmt: skip
                        if site.startswith("e_"):
                            names = {"e_first": [s.ents[0]], "e_second": [s.ents[1]],
                                     "e_both": list(s.ents)}[site]  # fmt: skip
                            out.append(self.ent_copy(s.sid, q, twin, names, (L,), alt, "L",
                                                     **meta))  # fmt: skip
                        else:
                            meta.pop("site")
                            out.append(self.site_copy(s.sid, q, twin, site, (L,), alt, "L",
                                                      **meta))  # fmt: skip
                    for kind, donor in xd.items():
                        if donor in self.by:
                            out.append(self.ent_copy(
                                s.sid, q, donor, list(s.ents), (L,), alt, "L", site="e_both",
                                donor_kind=kind, rcons=s.cons, item=s.item, layer=L,
                            ))  # fmt: skip
        return out

    def c_jobs(self) -> list[dict]:
        """CAPITALS: identity-aligned patch from the same bindings in the other order."""
        out = []
        for s in self.sents:
            if s.family != "cap":
                continue
            b, o = s.key[1], s.key[3]
            donor = f"c{s.item}-cap-b{b}o{1 - int(o)}"
            d = self.by[donor]
            for q in S.questions(s):
                for what in ("names", "names+attrs"):
                    names = list(s.ents) + (["@" + c for c in s.attrs] if what != "names" else [])
                    for L in self.cfg.c_layers:
                        j = self.ent_copy(s.sid, q, donor, names, (L,), self.other_word(s.sid, q),
                                          "C", what=what, item=s.item, layer=L)  # fmt: skip
                        if j:
                            assert all(n in self.slots(d) for n in names)
                        out.append(j)
        return out

    def ditr_donor(self, s: S.Sent, ents: tuple[str, ...], frame: str) -> str | None:
        for p in range(len(S.PERMS)):
            sid = f"d{s.item}-{frame}-p{p}"
            if sid in self.by and self.by[sid].ents == ents:
                return sid
        return None

    def t_jobs(self) -> list[dict]:
        out = []
        for s in self.sents:
            if s.family != "ditr":
                continue
            donors = {}
            for i, j in ((0, 1), (1, 2), (0, 2)):
                e = list(s.ents)
                e[i], e[j] = e[j], e[i]
                donors[f"swap{i + 1}{j + 1}"] = self.ditr_donor(s, tuple(e), s.cons)
            other = "do" if s.cons == "pp" else "pp"
            donors["dative"] = f"d{s.item}-{other}-{s.key}"
            for kind, donor in donors.items():
                if donor is None:
                    continue
                d = self.by[donor]
                for q in S.questions(s):
                    target = S.answer(d, q)
                    for L in self.cfg.t_layers:
                        out.append(self.ent_copy(
                            s.sid, q, donor, list(s.ents), (L,), target, "T", donor_kind=kind,
                            frame=s.cons, affected=target != S.answer(s, q), item=s.item,
                            layer=L,
                        ))  # fmt: skip
        return out

    # ---------------------------------------------------------------- vectors
    def build_vectors(self, layers: list[int]) -> dict:
        """Mean-difference vectors from train items, random and PC1 controls (plan arm V)."""
        import torch

        info: dict[str, Any] = {}
        gen = torch.Generator().manual_seed(SEED)
        for L in layers:
            E = self.E[L]
            core, ap, rel, cap_n, cap_a = [], [], [], [], []
            ditr: list[tuple[Any, int]] = []
            pcx, pcg, pcy = [], [], []
            for s in self.sents:
                if s.split != "train" or s.shift:
                    continue
                i = self.idx[s.sid]
                if s.family == "trans" and s.cons in S.CORE:
                    for k, e in enumerate(s.ents):
                        f = S.feats(s.cons, s.role_of(e))
                        core.append((E[i, k], f))
                        pcx.append(E[i, k])
                        pcg.append(e)
                        pcy.append(f[0])
                        if s.cons in ("A", "P"):
                            ap.append((E[i, k], f[0]))
                elif s.family == "rel":
                    rel += [(E[i, 0], 1), (E[i, 1], 2)]
                elif s.family == "cap":
                    cap_n += [(E[i, 0], 1), (E[i, 1], 2)]
                    cap_a += [(E[i, 2], 1), (E[i, 3], 2)]
                elif s.family == "ditr":
                    ditr += [(E[i, 0], 1), (E[i, 1], 2), (E[i, 2], 3)]
            vec = {
                "order": mean_diff([(x, f[0]) for x, f in core], 2, 1),
                "subj": mean_diff([(x, f[1]) for x, f in core], 1, 0),
                "role": mean_diff([(x, f[2]) for x, f in core], 1, 0),
                "order_AP": mean_diff(ap, 2, 1),
                "order_rel": mean_diff(rel, 2, 1),
                "cap_name": mean_diff(cap_n, 2, 1),
                "cap_attr": mean_diff(cap_a, 2, 1),
                "d12": mean_diff(ditr, 2, 1),
                "d23": mean_diff(ditr, 3, 2),
                "d13": mean_diff(ditr, 3, 1),
            }
            vec["d12p23"] = vec["d12"] + vec["d23"]
            nrm = vec["order"].norm()
            v1, st = pc1(torch.stack(pcx), pcg, pcy)
            vec["pc1"] = v1 * nrm
            for r in range(N_RAND):
                g = torch.randn(vec["order"].shape[0], generator=gen)
                vec[f"rand{r}"] = g / g.norm() * nrm
            vec["zero"] = torch.zeros_like(vec["order"])
            for k, v in vec.items():
                self.V.setdefault(k, {})[L] = v.float()
            info[str(L)] = {
                "norms": {k: float(v.norm()) for k, v in vec.items()},
                "cos_order": {k: float(torch.nn.functional.cosine_similarity(
                    v, vec["order"], dim=0)) for k, v in vec.items() if k != "zero"},
                "cos_d13_d12p23": float(torch.nn.functional.cosine_similarity(
                    vec["d13"], vec["d12p23"], dim=0)),
                "pc1": st,
                "mean_entity_norm": float(torch.stack([x for x, _ in core]).float()
                                          .norm(dim=-1).mean()),
            }  # fmt: skip
        return info

    # which vectors and alphas run per family (plan arm V)
    V_SWEEP: ClassVar[dict[str, tuple[list[str], list[str]]]] = {
        "trans": (["order", "order_AP", "cap_name"], ["subj", "role", "pc1", "zero"]),
        "rel": (["order", "order_AP", "order_rel", "cap_name"], ["pc1", "zero"]),
        "ditr": (["order", "order_AP", "d12", "d23", "d13", "d12p23", "cap_name"], ["zero"]),
        "cap": (["cap_name", "cap_both", "order_AP", "order"], ["zero"]),
    }

    def edit_pair(self, s: S.Sent, vec: str) -> tuple[str, str]:
        """(low, high) slot names: low += a*v, high -= a*v swaps them along ``vec``."""
        if vec in ("subj", "role"):
            k = 1 if vec == "subj" else 2
            fs = {e: S.feats(s.cons, s.role_of(e))[k] for e in s.ents}
            low = next(e for e in s.ents if fs[e] == 0)
            high = next(e for e in s.ents if fs[e] == 1)
            return low, high
        if s.family == "ditr":
            i, j = {"d23": (1, 2), "d13": (0, 2), "d12p23": (0, 2)}.get(vec, (0, 1))
            return s.ents[i], s.ents[j]
        return s.ents[0], s.ents[1]

    def v_jobs(self, config: str) -> list[dict]:
        layers = V_CONFIGS[config] if config != "band" else self.cfg.band
        if config == "L48":
            layers = (self.cfg.l_primary,)
        out = []
        for s in self.sents:
            if s.split == "train" or s.shift:
                continue
            sweep, single = self.V_SWEEP[s.family]
            plan = [(v, a) for v in sweep for a in ALPHAS]
            plan += [(v, 1.0) for v in single] + [(f"rand{r}", 1.0) for r in range(N_RAND)]
            if s.family == "trans":
                plan += [(f"rand{r}", 2.0) for r in range(N_RAND)]
            for vec, a in plan:
                if vec in ("subj", "role") and s.family != "trans":
                    continue
                if vec == "cap_both":
                    edits = [
                        (s.ents[0], "cap_name", a),
                        (s.ents[1], "cap_name", -a),
                        ("@" + s.attrs[0], "cap_attr", a),
                        ("@" + s.attrs[1], "cap_attr", -a),
                    ]
                else:
                    lo, hi = self.edit_pair(s, vec)
                    edits = [(lo, vec, a), (hi, vec, -a)]
                for q in S.questions(s):
                    if s.family == "ditr":
                        lo, hi = self.edit_pair(s, vec)
                        ans = S.answer(s, q)
                        target = {lo: hi, hi: lo}.get(ans, ans)
                    else:
                        target = self.other_word(s.sid, q)
                    out.append(self.vec_edit(
                        s.sid, q, edits, layers, target, "V", config=config, vec=vec, alpha=a,
                        family=s.family, cons=s.cons, item=s.item, split=s.split,
                        affected=target != S.answer(s, q),
                    ))  # fmt: skip
        return out

    # ---------------------------------------------------------------- phases
    def smoke(self) -> dict:
        import torch

        res: dict[str, Any] = {}
        s0 = self.trans()[0]
        n0 = self.R[(s0.sid, "agent")]["n"]
        same = [s.sid for s in self.trans() if self.R[(s.sid, "agent")]["n"] == n0][:8]
        ids = [self.R[(sid, "agent")]["ids"] for sid in same]
        pos = [[self.R[(sid, "agent")]["ent"][e] for e in self.by[sid].ents] for sid in same]
        L = self.cfg.l_primary
        got = M().capture(self.b, ids, pos, [L])[L]
        ans = [[self.tok(self.answer_word(sid, "agent")), self.tok(self.other_word(sid, "agent"))]
               for sid in same]  # fmt: skip
        base = M().forward_patched(self.b, ids, None, answer_ids=ans)
        pat = M().forward_patched(self.b, ids, {L: (torch.tensor(pos), got)}, answer_ids=ans)
        zero = M().forward_patched(
            self.b, ids, {L: (torch.tensor(pos), torch.zeros(got.shape), "add")}, answer_ids=ans
        )
        res["identity_max_abs"] = float((base["ans"] - pat["ans"]).abs().max())
        res["zero_add_max_abs"] = float((base["ans"] - zero["ans"]).abs().max())
        res["identity_ok"] = max(res["identity_max_abs"], res["zero_add_max_abs"]) <= ID_TOL
        res["first20"] = {}
        for fam in ("trans", "rel", "ditr", "cap"):
            fs = [s for s in self.sents if s.family == fam and not s.shift]
            prompts = [(s.sid, q) for s in fs for q in S.questions(s)][:20]
            ok, wrong = 0, []
            for sid, q in prompts:
                a = [[self.tok(self.answer_word(sid, q))] * 2]
                r = M().forward_patched(self.b, [self.R[(sid, q)]["ids"]], None, answer_ids=a)
                hit = int(r["top1"][0]) == a[0][0]
                ok += int(hit)
                if not hit:
                    wrong.append([sid, q, self.b.tokenizer.decode([int(r["top1"][0])])])
            res["first20"][fam] = [ok, len(prompts)]
            res.setdefault("wrong", {})[fam] = wrong[:5]
        fam_ok = {f: ok >= min(SMOKE_ACC_MIN, n - 2) for f, (ok, n) in res["first20"].items()}
        # log.md 22:4xZ: a failing non-transitive family drops only its own arms
        self.dropped = {f for f, good in fam_ok.items() if not good and f != "trans"}
        res["dropped_families"] = sorted(self.dropped)
        res["accuracy_ok"] = fam_ok["trans"]
        t = time.time()
        b_ids = (ids * (self.cfg.batch // len(ids) + 1))[: self.cfg.batch]
        M().forward_patched(self.b, b_ids, None)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        res["sec_per_batch"] = round(time.time() - t, 3)
        res["ok"] = res["identity_ok"] and res["accuracy_ok"]
        return res

    def project(self, sec_per_batch: float) -> dict:
        """Seconds per phase, from job counts (correctness unknown before the baseline, so every
        job is counted) x the measured batch time x 1.15 (longer prompts, value assembly)."""
        self.base = {k: {"top1": 0, "a_rec": 0} for k in self.R}  # count every job
        n = {
            "F@48": self.n_batches(self.f_jobs((self.cfg.l_primary,), ("both", "first", "second"))),
            "V@48": self.n_batches(self.v_jobs("L48")),
            "C": self.n_batches(self.c_jobs()),
            "L": self.n_batches(self.loc_jobs()),
            "T": self.n_batches(self.t_jobs()) if self.cfg.run_t else 0,
            "F@other": self.n_batches(self.f_other_jobs()),
            "V@band": self.n_batches(self.v_jobs("band")),
            "baseline": self.n_batches(self.baseline_jobs()),
        }
        self.base = {}
        return {k: round(v * sec_per_batch * 1.15) for k, v in n.items()}

    def f_other_jobs(self) -> list[dict]:
        singles = tuple(L for L in self.cfg.f_single_layers if L != self.cfg.l_primary)
        return (self.f_jobs(singles, ("both", "first", "second"))
                + self.f_jobs(tuple(self.cfg.f_other_layers), ("both",)))  # fmt: skip

    def trim(self, proj: dict) -> tuple[dict, list[str]]:
        """Plan's trim order when the projection exceeds 80% of the time left."""
        steps = []
        budget = 0.8 * (self.time_left() - 600)  # 10 min for captures, probes and save
        for what in ("f_other", "loc", "t"):
            if sum(proj.values()) <= budget:
                break
            if what == "f_other":
                self.cfg.f_other_layers = ()
            elif what == "loc":
                self.cfg.loc_layers = LOC_LAYERS_TRIM
            else:
                self.cfg.run_t = False
            steps.append(what)
            proj = self.project(self._spb)
        return proj, steps

    def baseline(self) -> dict:
        rows = self.run_jobs(self.baseline_jobs(), "baseline")
        self.base = {(r["recip"], r["q"]): r for r in rows}
        acc: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in rows:
            key = f"{r['family']}:{r['cons']}" + (":s16" if r["shift"] else "")
            acc[key][r["q"]][0] += int(r["top1"] == r["a_rec"])
            acc[key][r["q"]][1] += 1
        out = {k: {q: v[0] / v[1] for q, v in d.items()} for k, d in acc.items()}
        return {
            "accuracy": out,
            "gate1": {k: all(x >= GATE1_MIN for x in d.values()) for k, d in out.items()},
        }

    def probes(self) -> dict:
        """Decodability baselines (plan arm B): logistic probes for order/subject/role at the
        entity tokens per captured layer; their cosine with the vectors; PC1 stats are in
        ``vectors.json``."""
        import torch

        res: dict[str, Any] = {}
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        for L in sorted(self.E):
            E = self.E[L]
            data: dict[str, list] = defaultdict(list)
            for s in self.trans():
                i = self.idx[s.sid]
                for k, e in enumerate(s.ents):
                    o, sub, r = S.feats(s.cons, s.role_of(e))
                    part = ("core" if s.cons in S.CORE else "rep") + (
                        ":train" if s.split == "train" else ":held"
                    )
                    data[part].append((E[i, k], (o - 1, sub, r), s.cons))
            out: dict[str, Any] = {}
            for name, k in (("order", 0), ("subj", 1), ("role", 2)):
                tr = data["core:train"]
                w, mu, sd = fit_logreg(torch.stack([x for x, _, _ in tr]).float(),
                                       torch.tensor([y[k] for _, y, _ in tr]), dev)  # fmt: skip
                acc = {}
                for part in ("core:held", "rep:train", "rep:held"):
                    xs = data[part]
                    if xs:
                        acc[part] = eval_logreg(w, mu, sd, torch.stack([x for x, _, _ in xs]),
                                                torch.tensor([y[k] for _, y, _ in xs]))  # fmt: skip
                wraw = w / sd
                out[name] = {"acc": acc, "cos": {v: float(torch.nn.functional.cosine_similarity(
                    wraw, self.V[v][L], dim=0)) for v in ("order", "subj", "role", "order_AP")
                    if v in self.V and L in self.V[v]}}  # fmt: skip
            # A/P-trained order probe, tested on held OC/PC (order and subject decorrelated there)
            ap = [t for t in data["core:train"] if t[2] in ("A", "P")]
            w, mu, sd = fit_logreg(torch.stack([x for x, _, _ in ap]).float(),
                                   torch.tensor([y[0] for _, y, _ in ap]), dev)  # fmt: skip
            ocpc = [t for t in data["core:held"] if t[2] in ("OC", "PC")]
            out["order_AP_to_OCPC"] = {
                "order": eval_logreg(w, mu, sd, torch.stack([x for x, _, _ in ocpc]),
                                     torch.tensor([y[0] for _, y, _ in ocpc])),
                "subj": eval_logreg(w, mu, sd, torch.stack([x for x, _, _ in ocpc]),
                                    torch.tensor([1 - y[1] for _, y, _ in ocpc])),
            }  # fmt: skip
            res[str(L)] = out
        return res

    def lens_readouts(self) -> dict:
        """Top tokens of each vector under the logit lens and the J-lens, and the J-lens /
        logit-lens NNOMP share (FVE at k) of +v and -v (plan arm B)."""
        import torch

        from p40 import pursuit as P

        tokz = self.b.tokenizer
        res: dict[str, Any] = {}
        vecs = [v for v in ("order", "order_AP", "subj", "role", "cap_name", "d12", "pc1")
                if v in self.V]  # fmt: skip
        dicts: list[tuple[str, Any]] = [("logit", None)]
        if self.cfg.lens_path:
            have = set(M().available_layers(self.cfg.lens_path))
            jac = M().load_jacobians(self.cfg.lens_path,
                                     [L for L in self.cfg.lens_layers if L in have])  # fmt: skip
            dicts.append(("jlens", jac))
        Dl = M().build_dictionary(self.b, None)
        for name, jac in dicts:
            for L in self.cfg.lens_layers:
                if name == "jlens" and L not in jac:
                    continue
                D = Dl if name == "logit" else M().build_dictionary(self.b, jac[L])
                for v in vecs:
                    if L not in self.V[v]:
                        continue
                    x = self.V[v][L].to(D.device)
                    sc = D @ (x / x.norm())
                    top = {sg: [tokz.decode([int(i)]) for i in (sg * sc).topk(10).indices]
                           for sg in (1, -1)}  # fmt: skip
                    pr = P.pursue_batched(torch.stack([x, -x]).cpu(), D, self.cfg.k_lens, batch=2)
                    res[f"{name}:L{L}:{v}"] = {
                        "top_pos": top[1], "top_neg": top[-1],
                        "fve_pos": float(pr["fve"][0]), "fve_neg": float(pr["fve"][1]),
                        "max_cos": float(sc.abs().max()),
                    }  # fmt: skip
                if name != "logit":
                    del D
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        del Dl
        return res

    def save(self) -> None:
        from safetensors.torch import save_file

        d = self.out / "states"
        d.mkdir(parents=True, exist_ok=True)
        keep = [L for L in (44, 48) if L in self.E]
        save_file({f"E_L{L}": self.E[L].half().contiguous() for L in keep},
                  str(d / "entity_states.safetensors"))  # fmt: skip
        write_json(
            d / "index.json",
            {
                "sids": [s.sid for s in self.sents],
                "layers": keep,
                "slots": "mention order, then cap countries; padded to 4",
            },
        )
        save_file({f"{v}_L{L}": t.contiguous() for v, by in self.V.items() for L, t in by.items()},
                  str(self.out / "vectors.safetensors"))  # fmt: skip


def fit_logreg(X: Any, y: Any, dev: str, l2: float = 1e-2, iters: int = 200):
    """L2 logistic regression on standardised features (LBFGS). Returns (w, mean, std) on CPU."""
    import torch

    mu, sd = X.mean(0), X.std(0).clamp_min(1e-6)
    Z = ((X - mu) / sd).to(dev)
    t = y.float().to(dev)
    w = torch.zeros(Z.shape[1] + 1, device=dev, requires_grad=True)
    opt = torch.optim.LBFGS([w], max_iter=iters, line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        z = Z @ w[:-1] + w[-1]
        loss = torch.nn.functional.binary_cross_entropy_with_logits(z, t) + l2 * (w[:-1] ** 2).sum()
        loss.backward()
        return loss

    opt.step(closure)
    return w.detach()[:-1].cpu(), mu, sd


def eval_logreg(w: Any, mu: Any, sd: Any, X: Any, y: Any) -> float:
    z = ((X.float() - mu) / sd) @ w
    return float(((z > 0).long() == y.long()).float().mean())


# --------------------------------------------------------------------- the run
def run_all(sess: Session) -> int:
    cfg, T, out = sess.cfg, sess.timer, sess.out
    st = sess.status

    def done(rc: int, why: str) -> int:
        st.update(rc=rc, why=why, ended=utc())
        write_json(out / "status.json", st)
        log(f"end rc={rc} {why}")
        return rc

    with T.step("renders"):
        sess.renders()
    with T.step("capture"):
        sess.capture()
        ap = [(s.sid, q) for s in sess.trans() if s.cons in ("A", "P") for q in S.questions(s)]
        sess.capture_full(ap, list(cfg.loc_layers))
    with T.step("smoke"):
        sm = sess.smoke()
        sess._spb = sm["sec_per_batch"]
        proj = sess.project(sm["sec_per_batch"])
        proj, trims = sess.trim(proj)
        sm.update(projected_seconds=proj, total_projected=sum(proj.values()),
                  time_left=round(sess.time_left()), trimmed=trims)  # fmt: skip
        write_json(out / "smoke.json", sm)
    log(f"smoke {json.dumps(sm)}")
    if not sm["ok"]:
        return done(3, "smoke failed")
    with T.step("baseline"):
        b = sess.baseline()
    st["baseline"] = b
    write_json(out / "status.json", st)
    with T.step("gate2"):
        rows = sess.run_jobs(sess.gate2_jobs(), "gate2")
        g2 = sum(r["top1"] == r["a_alt"] for r in rows) / max(1, len(rows))
    st["gate2"] = {"flip": g2, "n": len(rows), "pass": g2 >= GATE2_MIN}
    write_json(out / "status.json", st)
    if g2 < GATE2_MIN:
        return done(0, "gate 2 failed: A/P twin patch at L48 flips < 0.80")
    with T.step("vectors"):
        vinfo = sess.build_vectors(sorted(set(cfg.band) | {cfg.l_primary} | set(cfg.lens_layers)))
        write_json(out / "vectors.json", vinfo)
    phases = [
        ("F@48", lambda: sess.f_jobs((cfg.l_primary,), ("both", "first", "second"))),
        ("V@48", lambda: sess.v_jobs("L48")),
        ("C", sess.c_jobs),
        ("L", sess.loc_jobs),
        ("T", lambda: sess.t_jobs() if cfg.run_t else []),
        ("F@other", sess.f_other_jobs),
        ("V@band", lambda: sess.v_jobs("band")),
    ]
    st["skipped"] = []
    for name, make in phases:
        jobs = [j for j in make() if j]
        need = sess.n_batches(jobs) * sess._spb * 1.15
        if need > sess.time_left() - 600:
            st["skipped"].append({"phase": name, "need_s": round(need),
                                  "left_s": round(sess.time_left())})  # fmt: skip
            log(f"skip {name}: need {need:.0f}s, left {sess.time_left():.0f}s")
            continue
        with T.step(name):
            sess.run_jobs(jobs, name.replace("@", "_"))
        write_json(out / "status.json", st)
    with T.step("probes"):
        write_json(out / "probes.json", sess.probes())
    with T.step("lens"):
        try:
            write_json(out / "lens.json", sess.lens_readouts())
        except Exception as e:  # noqa: BLE001 - a baseline; never lose the run over it
            st["lens_error"] = repr(e)
            log(f"lens failed: {e!r}")
    with T.step("save"):
        sess.save()
    return done(0, "complete")


def run_followup_cap(sess: Session) -> int:
    """Follow-up F1 (log.md): the CAPITALS arms the main run dropped (positive control C, and V
    on CAPITALS recipients), with the bold prefill. Pairs whose two capitals share a first token
    (Lima/Lisbon under the bold prefill) are skipped."""
    cfg, T, out = sess.cfg, sess.timer, sess.out
    st = sess.status
    st["followup"] = "F1 CAPITALS"
    st["cap_prefill"] = S.CAP_PREFILL

    def done(rc: int, why: str) -> int:
        st.update(rc=rc, why=why, ended=utc())
        write_json(out / "status.json", st)
        log(f"followup_cap end rc={rc} {why}")
        return rc

    with T.step("renders"):
        sess.renders()
    with T.step("capture"):
        sess.capture()
    with T.step("baseline"):
        jobs = [j for j in sess.baseline_jobs() if sess.by[j["recip"]].family == "cap"]
        rows = sess.run_jobs(jobs, "baseline")
        sess.base = {(r["recip"], r["q"]): r for r in rows}
        cap = [r for r in rows if r["family"] == "cap"]
        acc = {q: sum(r["top1"] == r["a_rec"] for r in cap if r["q"] == q)
               / max(1, sum(r["q"] == q for r in cap)) for q in ("n1", "n2")}  # fmt: skip
        wrong = Counter(sess.b.tokenizer.decode([r["top1"]]) for r in cap
                        if r["top1"] != r["a_rec"])  # fmt: skip
    st["cap_accuracy"] = acc
    st["cap_wrong_top1"] = dict(wrong.most_common(10))
    write_json(out / "status.json", st)
    if min(acc.values()) < CAP_MIN_ACC:
        return done(0, "CAPITALS accuracy < 0.5 even with the bold prefill")
    with T.step("vectors"):
        write_json(out / "vectors.json", sess.build_vectors(
            sorted(set(cfg.band) | {cfg.l_primary})))  # fmt: skip

    def distinct(js: list[dict | None]) -> list[dict]:
        return [
            j
            for j in js
            if j
            and sess.by[j["recip"]].family == "cap"
            and sess.tok(sess.answer_word(j["recip"], j["q"])) != sess.tok(j["a_alt"])
        ]

    for name, make in (("C", sess.c_jobs), ("V_48", lambda: sess.v_jobs("L48")),
                       ("V_band", lambda: sess.v_jobs("band"))):  # fmt: skip
        with T.step(name):
            sess.run_jobs(distinct(make()), name)
    return done(0, "complete")


# --------------------------------------------------------------------- CLI
def step_download(root: Path) -> dict[str, str]:
    from emx.i1a import hfget

    groups = [
        ("qwen", MODEL_ID, hfget.model_files(), MODEL_REV),
        ("jlens", JLENS[0], [JLENS[2]], JLENS[1]),
    ]
    total = sum(hfget.plan_bytes(repo, files, revision=rev) for _, repo, files, rev in groups)
    hfget.check_total(total, "P40 downloads")
    log(f"download plan: {total / 1e9:.1f} GB")
    paths = {}
    for name, repo, files, rev in groups:
        got = hfget.fetch(repo, files, revision=rev, local_dir=root / name)
        paths[name] = str(root / name) if name == "qwen" else str(next(iter(got.values())))
    write_json(OUT / "download_paths.json", paths)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("download", "all", "followup_cap"))
    ap.add_argument("--artifacts-dir", default=os.environ.get("P40_ARTIFACTS", "/ws/p40_artifacts"))
    ap.add_argument("--limit-items", type=int, default=0)
    ap.add_argument("--batch", type=int, default=int(os.environ.get("P40_BATCH", "128")))
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.step == "download":
        with Timer(OUT / "timing.json").step("download"):
            step_download(Path(a.artifacts_dir))
        return 0
    paths = json.loads((OUT / "download_paths.json").read_text())
    from emx.i1a.capture import load_backend

    cfg = Config(
        limit_items=a.limit_items,
        batch=a.batch,
        lens_path=paths["jlens"],
        max_hours=float(os.environ.get("P40_MAX_HOURS", "1.9")),
        t0=float(os.environ.get("P40_T0", time.time())),
    )
    with Timer(OUT / "timing.json").step("load"):
        backend = load_backend(paths["qwen"])
    import torch

    torch.backends.cuda.matmul.allow_tf32 = False
    env = {"torch": torch.__version__, "cuda": torch.version.cuda,
           "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
           "python": sys.version.split()[0], "git": os.environ.get("EMX_GIT_COMMIT")}  # fmt: skip
    try:
        import transformers

        env["transformers"] = transformers.__version__
    except ImportError:
        pass
    write_json(OUT / "env.json", env)
    if a.step == "followup_cap":
        S.CAP_PREFILL = "lives in the city of **"
        cfg.out = OUT / "followup_cap"
        cfg.out.mkdir(parents=True, exist_ok=True)
        return run_followup_cap(Session(backend, cfg))
    return run_all(Session(backend, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
