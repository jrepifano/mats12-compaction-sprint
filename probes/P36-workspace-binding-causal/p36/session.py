"""P36 pod session (plan.md): one model load, all phases, rows to ``results/``.

    python -m p36.session download      # model + J-lens + J++ -> <artifacts>/download_paths.json
    python -m p36.session all [--limit-items N]

Phases of ``all`` (each timed into ``results/timing.json``, printed as ``P36_STEP <name> ...``):

1. ``load``, ``renders`` (``stimuli.render_all``: twins align token for token).
2. ``capture``: block outputs at every layer for every sentence at three slots: the final '.',
   the designated entity e1's token and e2's token (from the agent-question render; the sentence
   prefix is the same tokens under both questions).
3. ``smoke``: (a) same-batch identity patch moves logits by exactly 0 (tolerance ``ID_TOL``);
   (b) unpatched accuracy on the first 20 prompts >= 18; (c) pursuit on 16 states per
   dictionary returns k atoms with 0 < FVE < 1. A failure ends the session (rc 3).
4. ``baseline``: unpatched next token for all prompts. **Gate 1**: accuracy >= 0.95 per question.
5. ``stage0``: full twin patches, single layers ``STAGE0_LAYERS`` x sites {final, entity}.
   **Gate 2**: a cell whose full-patch flip rate >= 0.5 under both questions. If none, the
   pre-declared band follow-up (``band``: the same patches at 4 consecutive layers) and gate 2
   on band cells; if still none, stop (rc 0, ``status.json`` says why).
6. ``decomp1`` + ``pass1``: J-lens and logit-lens dictionaries at k = 25 for every captured
   stage-0 layer; at the first-pass cells (<= 2 per site, best first) the arms in ``PASS1_ARMS``.
7. ``decomp2`` + ``pass2``: at the selected cell per site, k in ``K_SWEEP`` x {jlens, logit, jpp},
   twin j- and r-swaps; plus J++ k = 25 self-recombined and random-donor controls.
8. ``save``: states at the stage-0 layers, entity-slot reconstructions (k = 25) and every
   decomposition's atoms/coefficients, for the probes and a later Oracle Lens study.

Every forward row (``results/rows/<phase>.jsonl``) is one recipient prompt under one patch:
``{phase, site, layers, q, recip, donor, kind, op, dict, k, top1, a_rec, a_rev, logit_rec,
logit_rev, logz}``; ``a_rec`` / ``a_rev`` are the token ids of the recipient's correct answer and
of the other entity (the role-reversed answer). Scoring is ``p36.analyze`` (laptop).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from p36 import stimuli as S

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "results"
SEED = 20261013

MODEL_ID = "Qwen/Qwen3.6-27B"
MODEL_REV = "6a9e13bd6fc8f0983b9b99948120bc37f49c13e9"
JLENS = ("neuronpedia/jacobian-lens", "b25d72a96b79c8e309d6625955a98751da47e67a",
         "qwen3.6-27b/jlens/Salesforce-wikitext/Qwen3.6-27B_jacobian_lens_n1000.pt")  # fmt: skip
JPP = ("koayon/jpp-lenses", "6c96867a91c43c14cec17cf0f6d157b933c5fee5", "qwen3.6-27b/lens.pt")

SLOTS = {"final": [0], "entity": [1, 2]}  # slot 0 = '.', 1 = e1's token, 2 = e2's token
GATE1_MIN = 0.95
GATE2_MIN = 0.50
ID_TOL = 0.0
SMOKE_ACC_MIN = 18  # of the first 20 prompts
K1 = 25
K_SWEEP = (16, 25, 100, 400)
BAND = 4
# (donor kind, op, dictionaries) for pass 1; op "full" ignores the dictionary
PASS1_ARMS: tuple[tuple[str, str], ...] = (
    ("twin", "j"), ("twin", "r"),
    ("self", "full"), ("self", "rec"),
    ("xsame", "full"), ("xsame", "j"), ("xsame", "r"),
    ("xtwin", "full"), ("xtwin", "j"), ("xtwin", "r"),
    ("random", "full"), ("random", "j"), ("random", "r"),
)  # fmt: skip


@dataclass
class Config:
    stage0_layers: tuple[int, ...] = (12, 18, 24, 30, 36, 42, 48, 54)
    n_layers: int = 64
    batch: int = 128
    pursuit_batch: int = 128
    k1: int = K1
    k_sweep: tuple[int, ...] = K_SWEEP
    pass1_dicts: tuple[str, ...] = ("jlens", "logit")
    pass2_dicts: tuple[str, ...] = ("jlens", "logit", "jpp")
    max_cells_per_site: int = 2
    limit_items: int = 0
    out: Path = OUT
    lens_paths: dict[str, str] = field(default_factory=dict)  # {"jlens": path, "jpp": path}
    max_hours: float = 2.2
    t0: float = field(default_factory=time.time)
    followup: bool = False  # F1: add the shifted sentences (donors only)


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[p36 {utc()}] {msg}", flush=True)


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
                print(f"P36_STEP {name} start {self.rec['start']}", flush=True)
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
                print(f"P36_STEP {name} done {self.rec['end']} {self.rec['seconds']}", flush=True)

        return _S()


# --------------------------------------------------------------------- pure helpers


def donor_maps(sents: list[S.Sentence], seed: int = SEED) -> dict[str, dict[str, str]]:
    """{kind: {recipient sid: donor sid}} for twin, self, xsame (other voice, same roles),
    xtwin (other voice, reversed roles) and random (another item, same voice and role slot;
    a seeded derangement of items)."""
    sids = {s.sid for s in sents}
    sents = [s for s in sents if not s.sid.endswith("-s")]  # recipients: originals only
    items = sorted({s.item for s in sents})
    rng = random.Random(seed + 7)
    perm = items[:]
    while len(items) > 1 and any(a == b for a, b in zip(items, perm, strict=True)):
        rng.shuffle(perm)
    to = dict(zip(items, perm, strict=True))
    kinds = ("twin", "self", "xsame", "xtwin", "random", "shift_same", "shift_twin")
    out: dict[str, dict[str, str]] = {k: {} for k in kinds}
    for s in sents:
        other = "passive" if s.voice == "active" else "active"
        flip = "d_patient" if s.role == "d_agent" else "d_agent"
        cand = {
            "twin": S.twin_sid(s.sid),
            "self": s.sid,
            "xsame": f"{s.item}-{other}-{s.role}",
            "xtwin": f"{s.item}-{other}-{flip}",
            "random": f"{to[s.item]}-{s.voice}-{s.role}",
            "shift_same": f"{s.sid}-s",
            "shift_twin": f"{S.twin_sid(s.sid)}-s",
        }
        for k, d in cand.items():
            if d in sids:
                out[k][s.sid] = d
    return out


def flip_rates(rows: list[dict]) -> dict[str, float]:
    """Share of rows whose top1 is the reversed answer (rows already filtered to valid pairs)."""
    by_q: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_q[r["q"]].append(int(r["top1"] == r["a_rev"]))
    return {q: sum(v) / len(v) for q, v in by_q.items() if v}


def valid_pairs(base: dict[tuple[str, str], dict], dmap: dict[str, str]) -> set[tuple[str, str]]:
    """(recipient, q) where recipient and donor are both answered correctly unpatched."""
    ok = set()
    for (sid, q), r in base.items():
        d = dmap.get(sid)
        if d is None or (d, q) not in base:
            continue
        if r["top1"] == r["a_rec"] and base[(d, q)]["top1"] == base[(d, q)]["a_rec"]:
            ok.add((sid, q))
    return ok


def choose_cells(rates: dict[tuple[str, tuple[int, ...]], dict[str, float]], n_per_site: int):
    """Cells passing gate 2 (min over questions >= GATE2_MIN), best first per site (ties: lower
    first layer). Returns {site: [layers, ...]}."""
    out: dict[str, list[tuple[int, ...]]] = {}
    for site in SLOTS:
        ok = [
            (min(r.values()), layers)
            for (s, layers), r in rates.items()
            if s == site and len(r) == 2 and min(r.values()) >= GATE2_MIN
        ]
        ok.sort(key=lambda t: (-t[0], t[1]))
        if ok:
            out[site] = [layers for _, layers in ok[:n_per_site]]
    return out


# --------------------------------------------------------------------- the session


class Session:
    def __init__(self, backend: Any, cfg: Config) -> None:
        self.b = backend
        self.cfg = cfg
        self.out = cfg.out
        self.timer = Timer(self.out / "timing.json")
        items, sents = S.load_stimuli(HERE / "data" / "stimuli.json")
        if cfg.limit_items:
            keep = {i.item for i in items[: cfg.limit_items]}
            sents = [s for s in sents if s.item in keep]
        if cfg.followup:
            sents = sents + S.shifted(sents)
        self.sents = sents
        self.idx = {s.sid: i for i, s in enumerate(sents)}
        self.by_sid = {s.sid: s for s in sents}
        self.dmaps = donor_maps(sents)
        self.H: dict[int, Any] = {}  # layer -> [N, 3, d] model dtype (CPU)
        self.recon: dict[tuple[str, int, int], Any] = {}  # (dict, k, layer) -> [N, 3, d] fp32
        self.decomp_meta: dict[tuple[str, int, int], dict] = {}
        self.base: dict[tuple[str, str], dict] = {}
        self.jac: dict[str, dict[int, Any]] = {}  # lens -> {layer: fp32 [d, d]} (CPU)
        self.missing: list[tuple[str, int]] = []  # (dict, layer) the lens does not cover

    # ---------------------------------------------------------------- basics
    def time_left(self) -> float:
        return self.cfg.max_hours * 3600 - (time.time() - self.cfg.t0)

    def renders(self) -> None:
        self.R = S.render_all(self.b.tokenizer, self.sents)
        self.tok = {e: S.answer_token(self.b.tokenizer, e) for e in S.ENTITIES}

    def answers(self, sid: str, q: str) -> tuple[int, int]:
        s = self.by_sid[sid]
        rec = S.answer(s, q)
        rev = s.patient if rec == s.agent else s.agent
        return self.tok[rec], self.tok[rev]

    def positions(self, sid: str, q: str, site: str) -> list[int]:
        r, s = self.R[(sid, q)], self.by_sid[sid]
        e1, e2 = sorted((s.agent, s.patient))
        return {"final": [r["final"]], "entity": [r["ent"][e1], r["ent"][e2]]}[site]

    def capture(self) -> None:
        import torch

        L = list(range(self.cfg.n_layers))
        groups: dict[int, list[str]] = defaultdict(list)
        for s in self.sents:
            groups[self.R[(s.sid, "agent")]["n"]].append(s.sid)
        parts: dict[int, list[tuple[list[int], Any]]] = defaultdict(list)
        for _, sids in sorted(groups.items()):
            for lo in range(0, len(sids), self.cfg.batch):
                chunk = sids[lo : lo + self.cfg.batch]
                ids = [self.R[(sid, "agent")]["ids"] for sid in chunk]
                pos = [
                    self.positions(sid, "agent", "final") + self.positions(sid, "agent", "entity")
                    for sid in chunk
                ]
                got = M().capture(self.b, ids, pos, L)
                for layer in L:
                    parts[layer].append(([self.idx[s] for s in chunk], got[layer]))
        for layer in L:
            d = parts[layer][0][1].shape[-1]
            H = torch.empty(len(self.sents), 3, d, dtype=parts[layer][0][1].dtype)
            for ix, t in parts[layer]:
                H[ix] = t
            self.H[layer] = H
        # the patient render has the same sentence positions
        for s in self.sents:
            for site in SLOTS:
                assert self.positions(s.sid, "agent", site) == self.positions(
                    s.sid, "patient", site
                )

    # ---------------------------------------------------------------- forwards
    def values(self, job: dict, layer: int) -> Any:
        """[P, d] fp32 patch values for one job at one layer."""
        slots = SLOTS[job["site"]]
        r, d = self.idx[job["recip"]], self.idx[job["donor"]]
        Hr = self.H[layer][r, slots].float()
        Hd = self.H[layer][d, slots].float()
        op = job["op"]
        if op == "full":
            return Hd
        J = self.recon[(job["dict"], job["k"], layer)]
        Jr, Jd = J[r, slots], J[d, slots]
        if op == "j":
            return Jd + (Hr - Jr)
        if op == "r":
            return Jr + (Hd - Jd)
        if op == "rec":
            return Jr + (Hr - Jr)
        raise KeyError(op)

    def run_jobs(self, jobs: list[dict], phase: str) -> list[dict]:
        """Forward every job (grouped by prompt length, site and layers); append rows."""
        import torch

        groups: dict[tuple, list[dict]] = defaultdict(list)
        for j in jobs:
            n = self.R[(j["recip"], j["q"])]["n"]
            groups[(n, j["site"], tuple(j["layers"]) if j["layers"] else ())].append(j)
        rows: list[dict] = []
        path = self.out / "rows" / f"{phase}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for (_, site, layers), js in sorted(groups.items()):
                for lo in range(0, len(js), self.cfg.batch):
                    chunk = js[lo : lo + self.cfg.batch]
                    ids = [self.R[(j["recip"], j["q"])]["ids"] for j in chunk]
                    ans = [self.answers(j["recip"], j["q"]) for j in chunk]
                    edits = {}
                    if layers:
                        pos = torch.tensor(
                            [self.positions(j["recip"], j["q"], site) for j in chunk]
                        )
                        for layer in layers:
                            vals = torch.stack([self.values(j, layer) for j in chunk])
                            edits[layer] = (pos, vals)
                    got = M().forward_patched(self.b, ids, edits, answer_ids=ans)
                    for i, j in enumerate(chunk):
                        row = {
                            "phase": phase,
                            **{k: j[k] for k in ("site", "q", "recip", "donor", "kind", "op")},
                            "layers": list(layers),
                            "dict": j.get("dict"),
                            "k": j.get("k"),
                            "top1": int(got["top1"][i]),
                            "a_rec": ans[i][0],
                            "a_rev": ans[i][1],
                            "logit_rec": round(float(got["ans"][i, 0]), 4),
                            "logit_rev": round(float(got["ans"][i, 1]), 4),
                            "logz": round(float(got["logz"][i]), 4),
                        }
                        rows.append(row)
                        fh.write(json.dumps(row) + "\n")
        return rows

    def job(self, recip, q, site, layers, kind, op, dct=None, k=None) -> dict | None:
        donor = self.dmaps[kind].get(recip)
        if donor is None:
            return None
        if dct is not None and any((dct, k, L) not in self.recon for L in layers):
            return None  # the lens does not cover a layer of this cell
        return {"recip": recip, "donor": donor, "q": q, "site": site, "layers": layers,
                "kind": kind, "op": op, "dict": dct, "k": k}  # fmt: skip

    # ---------------------------------------------------------------- phases
    def smoke(self) -> dict:
        import torch

        res: dict[str, Any] = {}
        # (a) same-batch identity patch: capture and patch the same batch
        n0 = self.R[(self.sents[0].sid, "agent")]["n"]
        same = [s.sid for s in self.sents if self.R[(s.sid, "agent")]["n"] == n0][:8]
        ids = [self.R[(sid, "agent")]["ids"] for sid in same]
        pos = [self.positions(sid, "agent", "entity") for sid in same]
        L = self.cfg.stage0_layers[len(self.cfg.stage0_layers) // 2]
        got = M().capture(self.b, ids, pos, [L])[L]
        ans = [self.answers(sid, "agent") for sid in same]
        base = M().forward_patched(self.b, ids, None, answer_ids=ans)
        pat = M().forward_patched(self.b, ids, {L: (torch.tensor(pos), got)}, answer_ids=ans)
        diff = float((base["ans"] - pat["ans"]).abs().max())
        res["identity_max_abs"] = diff
        res["identity_ok"] = diff <= ID_TOL
        # (b) accuracy on the first 20 prompts
        first = [(s.sid, q) for s in self.sents[:10] for q in S.QUESTIONS]
        correct = 0
        for sid, q in first:
            a = self.answers(sid, q)
            r = M().forward_patched(self.b, [self.R[(sid, q)]["ids"]], None, answer_ids=[a])
            correct += int(int(r["top1"][0]) == a[0])
        res["first20_correct"] = correct
        res["accuracy_ok"] = correct >= min(SMOKE_ACC_MIN, len(first) - 2)
        # (c) pursuit on 16 states per dictionary
        from p36 import pursuit as P

        res["pursuit"] = {}
        for name in self.dict_names(self.cfg.pass2_dicts):
            t = time.time()
            if L not in self.lens_layers(name):
                res["pursuit"][name] = {"missing_layer": L}
                continue
            D = self.dictionary(name, L)
            x = self.H[L][:16, 0].float()
            out = P.pursue_batched(x, D, self.cfg.k1, batch=16)
            t25 = time.time() - t
            t = time.time()
            big = P.pursue_batched(x, D, max(self.cfg.k_sweep), batch=16)
            res["pursuit"][name] = {
                "fve_k1_mean": float(out["fve"].mean()),
                "n_active_k1_min": int(out["n_active"].min()),
                "fve_kmax_mean": float(big["fve"].mean()),
                "kkt_kmax_max": float(big["kkt"].max()),
                "sec_k1_with_dict_build": round(t25, 2),
                "sec_kmax_16_states": round(time.time() - t, 2),
            }
            del D
        res["pursuit_ok"] = all(
            0 < v["fve_k1_mean"] < 1 and v["n_active_k1_min"] >= 1
            for v in res["pursuit"].values()
            if "missing_layer" not in v
        )
        # throughput: one full batch of unpatched forwards
        t = time.time()
        b_ids = [self.R[(sid, "agent")]["ids"] for sid in same] * (self.cfg.batch // len(same) + 1)
        M().forward_patched(self.b, b_ids[: self.cfg.batch], None)
        res["sec_per_batch"] = round(time.time() - t, 3)
        # projection of the bulk forwards (plan: refuse bulk work the budget cannot cover)
        n_pairs = 2 * len(self.sents)
        n_fwd = (
            n_pairs * 2 * len(self.cfg.stage0_layers)  # stage 0, both sites
            + n_pairs * 22 * 2 * self.cfg.max_cells_per_site  # pass 1
            + n_pairs * 30 * 2  # pass 2
        )
        kmax = res["pursuit"].get("jlens", {}).get("sec_kmax_16_states", 0.0)
        res["projected_seconds"] = round(
            n_fwd / self.cfg.batch * res["sec_per_batch"]
            + 3 * len(self.sents) * 3 / 16 * kmax * 2 / 4  # k sweep pursuit (batches of 128
            # amortise the per-step cost ~4x against the 16-state smoke)
        )
        res["time_left"] = round(self.time_left())
        if res["projected_seconds"] > 0.8 * res["time_left"] and self.cfg.max_cells_per_site > 1:
            self.cfg.max_cells_per_site = 1
            res["shrunk_to_one_cell_per_site"] = True
        res["ok"] = res["identity_ok"] and res["accuracy_ok"] and res["pursuit_ok"]
        write_json(self.out / "smoke.json", res)
        return res

    def baseline(self) -> dict:
        jobs = [
            {
                "recip": s.sid,
                "donor": s.sid,
                "q": q,
                "site": "final",
                "layers": (),
                "kind": "none",
                "op": "none",
            }
            for s in self.sents
            for q in S.QUESTIONS
        ]
        rows = self.run_jobs(jobs, "baseline")
        self.base = {(r["recip"], r["q"]): r for r in rows}
        acc = {
            q: sum(r["top1"] == r["a_rec"] for r in rows if r["q"] == q)
            / max(1, sum(r["q"] == q for r in rows))
            for q in S.QUESTIONS
        }
        return {"accuracy": acc, "gate1": all(v >= GATE1_MIN for v in acc.values())}

    def stage0(self, band: bool) -> dict:
        phase = "band" if band else "stage0"
        cells = [
            tuple(range(L, L + BAND)) if band else (L,)
            for L in self.cfg.stage0_layers
            if not band or L + BAND <= self.cfg.n_layers
        ]
        jobs = []
        for sid, q in sorted(valid_pairs(self.base, self.dmaps["twin"])):
            for site in SLOTS:
                for layers in cells:
                    jobs.append(self.job(sid, q, site, layers, "twin", "full"))
        rows = self.run_jobs(jobs, phase)
        by: dict[tuple, list] = defaultdict(list)
        for r in rows:
            by[(r["site"], tuple(r["layers"]))].append(r)
        rates = {key: flip_rates(rs) for key, rs in by.items()}
        cells_ok = choose_cells(rates, self.cfg.max_cells_per_site)
        return {
            "rates": {f"{s}@{'-'.join(map(str, L))}": v for (s, L), v in rates.items()},
            "cells": {s: [list(c) for c in cs] for s, cs in cells_ok.items()},
        }

    def dict_names(self, names) -> list[str]:
        return [n for n in names if n == "logit" or n in self.cfg.lens_paths]

    def lens_layers(self, name: str) -> set[int]:
        if name == "logit":
            return set(range(self.cfg.n_layers))
        if name not in self.jac:
            path = self.cfg.lens_paths[name]
            have = M().available_layers(path)
            self.jac[name] = M().load_jacobians(path, [L for L in have if L < self.cfg.n_layers])
            log(f"lens {name}: {len(self.jac[name])} layers loaded")
        return set(self.jac[name])

    def dictionary(self, name: str, layer: int) -> Any:
        if name == "logit":
            return M().build_dictionary(self.b, None)
        if layer not in self.lens_layers(name):
            raise KeyError(f"lens {name} has no layer {layer}")
        return M().build_dictionary(self.b, self.jac[name][layer])

    def decompose(self, specs: list[tuple[str, int, int]]) -> None:
        """Pursue every captured state (all three slots) for each (dict, k, layer)."""
        import torch

        from p36 import pursuit as P

        by_dl: dict[tuple[str, int], list[int]] = defaultdict(list)
        for name, k, layer in specs:
            if (name, k, layer) not in self.recon:
                by_dl[(name, layer)].append(k)
        for (name, layer), ks in sorted(by_dl.items()):
            if layer not in self.lens_layers(name):
                self.missing.append((name, layer))
                log(f"lens {name} has no layer {layer}: skipped")
                continue
            D = self.dictionary(name, layer)
            H = self.H[layer]
            x = H.reshape(-1, H.shape[-1]).float()
            for k in sorted(ks):
                t = time.time()
                out = P.pursue_batched(x, D, k, batch=self.cfg.pursuit_batch)
                self.recon[(name, k, layer)] = out["recon"].reshape(H.shape[0], 3, -1)
                hn = x.norm(dim=-1)
                jn = out["recon"].norm(dim=-1)
                self.decomp_meta[(name, k, layer)] = {
                    "dict": name, "k": k, "layer": layer,
                    "seconds": round(time.time() - t, 2),
                    "fve": out["fve"].reshape(-1, 3).tolist(),
                    "n_active": out["n_active"].reshape(-1, 3).tolist(),
                    "kkt": out["kkt"].reshape(-1, 3).tolist(),
                    "h_norm": hn.reshape(-1, 3).tolist(),
                    "j_norm": jn.reshape(-1, 3).tolist(),
                    "atoms": out["atoms"].to(torch.int32),
                    "coeffs": out["coeffs"].half(),
                }  # fmt: skip
                log(f"decomp {name} k={k} L={layer}: mean FVE {float(out['fve'].mean()):.4f}, "
                    f"{time.time() - t:.1f}s")  # fmt: skip
            del D
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def twin_diff_share(self, name: str, k: int, layer: int) -> list[dict]:
        """Per twin pair and slot: |j_d - j_r|^2 / |h_d - h_r|^2 (each unordered pair once)."""
        J, H = self.recon[(name, k, layer)], self.H[layer].float()
        out = []
        for s in self.sents:
            if s.role != "d_agent" or S.twin_sid(s.sid) not in self.idx:
                continue
            a, b = self.idx[s.sid], self.idx[S.twin_sid(s.sid)]
            dh = ((H[a] - H[b]) ** 2).sum(-1)
            dj = ((J[a] - J[b]) ** 2).sum(-1)
            out.append({"sid": s.sid, "dict": name, "k": k, "layer": layer,
                        "dh2": dh.tolist(), "dj2": dj.tolist()})  # fmt: skip
        return out

    def pass1(self, cells: dict[str, list[tuple[int, ...]]]) -> list[dict]:
        jobs = []
        for site, cs in cells.items():
            for layers in cs:
                for kind, op in PASS1_ARMS:
                    pairs = valid_pairs(self.base, self.dmaps[kind])
                    dicts = [None] if op == "full" else self.dict_names(self.cfg.pass1_dicts)
                    for dct in dicts:
                        k = None if dct is None else self.cfg.k1
                        for sid, q in sorted(pairs):
                            jobs.append(self.job(sid, q, site, layers, kind, op, dct, k))
        return self.run_jobs([j for j in jobs if j], "pass1")

    def pass2(self, cells: dict[str, tuple[int, ...]]) -> list[dict]:
        jobs = []
        twin = sorted(valid_pairs(self.base, self.dmaps["twin"]))
        rand = sorted(valid_pairs(self.base, self.dmaps["random"]))
        selfp = sorted(valid_pairs(self.base, self.dmaps["self"]))
        for site, layers in cells.items():
            for dct in self.dict_names(self.cfg.pass2_dicts):
                for k in self.cfg.k_sweep:
                    if dct in self.cfg.pass1_dicts and k == self.cfg.k1:
                        continue  # already in pass 1
                    for op in ("j", "r"):
                        for sid, q in twin:
                            jobs.append(self.job(sid, q, site, layers, "twin", op, dct, k))
            if "jpp" in self.dict_names(self.cfg.pass2_dicts):
                for sid, q in selfp:
                    jobs.append(self.job(sid, q, site, layers, "self", "rec", "jpp", self.cfg.k1))
                for op in ("j", "r"):
                    for sid, q in rand:
                        jobs.append(
                            self.job(sid, q, site, layers, "random", op, "jpp", self.cfg.k1)
                        )
        return self.run_jobs([j for j in jobs if j], "pass2")

    def save(self, layers: list[int]) -> None:
        from safetensors.torch import save_file

        d = self.out / "states"
        d.mkdir(parents=True, exist_ok=True)
        save_file({f"h_L{L}": self.H[L].contiguous() for L in layers}, str(d / "h.safetensors"))
        ent = {
            f"j_{n}_k{k}_L{L}": v[:, 1:].half().contiguous()
            for (n, k, L), v in self.recon.items()
            if k == self.cfg.k1 and L in layers
        }
        if ent:
            save_file(ent, str(d / "j_entity_k25.safetensors"))
        dec = {}
        for (n, k, L), m in self.decomp_meta.items():
            dec[f"atoms_{n}_k{k}_L{L}"] = m["atoms"].contiguous()
            dec[f"coeffs_{n}_k{k}_L{L}"] = m["coeffs"].contiguous()
        if dec:
            save_file(dec, str(d / "decomp_atoms.safetensors"))
        write_json(d / "index.json", {
            "sids": [s.sid for s in self.sents],
            "slots": ["final", "e1", "e2"],
            "layers": layers,
            "dtype_h": str(self.H[layers[0]].dtype),
        })  # fmt: skip
        with (self.out / "decomp_stats.jsonl").open("w") as fh:
            for m in self.decomp_meta.values():
                fh.write(
                    json.dumps({k: v for k, v in m.items() if k not in ("atoms", "coeffs")}) + "\n"
                )
        write_json(self.out / "missing_lens_layers.json", self.missing)


def M():
    from p36 import model

    return model


def run_all(sess: Session) -> int:
    cfg, T, out = sess.cfg, sess.timer, sess.out
    status: dict[str, Any] = {"started": utc()}

    def done(rc: int, why: str) -> int:
        status.update(rc=rc, why=why, ended=utc())
        write_json(out / "status.json", status)
        log(f"end rc={rc}: {why}")
        return rc

    with T.step("renders"):
        sess.renders()
    with T.step("capture"):
        sess.capture()
    with T.step("smoke"):
        sm = sess.smoke()
    status["smoke"] = {k: v for k, v in sm.items() if k != "pursuit"}
    if not sm["ok"]:
        return done(3, f"smoke failed: {sm}")
    with T.step("baseline"):
        g1 = sess.baseline()
    status["gate1"] = g1
    if not g1["gate1"]:
        return done(0, f"gate 1 failed: accuracy {g1['accuracy']}")
    with T.step("stage0"):
        s0 = sess.stage0(band=False)
    status["stage0"] = s0
    cells = {s: [tuple(c) for c in cs] for s, cs in s0["cells"].items()}
    if not cells:
        log("gate 2 failed on single layers: band follow-up (pre-declared)")
        with T.step("band"):
            sb = sess.stage0(band=True)
        status["band"] = sb
        cells = {s: [tuple(c) for c in cs] for s, cs in sb["cells"].items()}
        if not cells:
            sess.save(list(cfg.stage0_layers))
            return done(0, "gate 2 failed on single layers and bands")
    status["cells_pass1"] = {s: [list(c) for c in cs] for s, cs in cells.items()}
    write_json(out / "status.json", status)
    # decomposition at k1 for every stage-0 layer (probes) and every layer of the chosen cells
    p1_layers = sorted({L for cs in cells.values() for c in cs for L in c} | set(cfg.stage0_layers))
    with T.step("decomp1"):
        sess.decompose(
            [(n, cfg.k1, L) for n in sess.dict_names(cfg.pass1_dicts) for L in p1_layers]
        )
    with T.step("pass1"):
        sess.pass1(cells)
    selected = {s: cs[0] for s, cs in cells.items()}
    status["selected"] = {s: list(c) for s, c in selected.items()}
    write_json(out / "status.json", status)
    if sess.time_left() < 1800:
        sess.save(list(cfg.stage0_layers))
        return done(0, "stopped before pass 2: time")
    sel_layers = sorted({L for c in selected.values() for L in c})
    with T.step("decomp2"):
        sess.decompose([
            (n, k, L)
            for n in sess.dict_names(cfg.pass2_dicts) for k in cfg.k_sweep for L in sel_layers
        ])  # fmt: skip
    with T.step("pass2"):
        sess.pass2(selected)
    with T.step("twin_share"):
        rows = []
        for n, k, L in sorted(sess.recon):
            rows += sess.twin_diff_share(n, k, L)
        with (out / "twin_share.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    with T.step("save"):
        sess.save(list(cfg.stage0_layers))
    return done(0, "complete")


FU_LAYERS: tuple[int, ...] = (42, 44, 46, 48, 50, 52, 54)
FU_KINDS: tuple[str, ...] = ("twin", "xsame", "xtwin", "shift_same", "shift_twin")
FU_SWAP_LAYER = 48


def run_followup(sess: Session) -> int:
    """Follow-up F1 (log.md): mention order vs absolute position at the entity site."""
    cfg, T, out = sess.cfg, sess.timer, sess.out
    status: dict[str, Any] = {"started": utc(), "followup": "F1"}
    with T.step("renders"):
        sess.renders()
    with T.step("capture"):
        sess.capture()
    with T.step("baseline"):
        sess.baseline()
    acc = {}
    for tag, keep in (("original", False), ("shifted", True)):
        rs = [r for k, r in sess.base.items() if k[0].endswith("-s") == keep]
        acc[tag] = sum(r["top1"] == r["a_rec"] for r in rs) / max(1, len(rs))
    status["accuracy"] = acc
    write_json(out / "status.json", status)
    with T.step("followup_full"):
        jobs = []
        for kind in FU_KINDS:
            for sid, q in sorted(valid_pairs(sess.base, sess.dmaps[kind])):
                for L in FU_LAYERS:
                    jobs.append(sess.job(sid, q, "entity", (L,), kind, "full"))
        sess.run_jobs([j for j in jobs if j], "followup")
    with T.step("decomp"):
        sess.decompose([("jlens", cfg.k1, FU_SWAP_LAYER)])
    with T.step("followup_swaps"):
        jobs = []
        for kind in ("twin", "shift_same", "shift_twin"):
            for sid, q in sorted(valid_pairs(sess.base, sess.dmaps[kind])):
                for op in ("j", "r"):
                    jobs.append(
                        sess.job(sid, q, "entity", (FU_SWAP_LAYER,), kind, op, "jlens", cfg.k1)
                    )
        sess.run_jobs([j for j in jobs if j], "followup")
    status.update(rc=0, why="complete", ended=utc())
    write_json(out / "status.json", status)
    log("followup end rc=0")
    return 0


# --------------------------------------------------------------------- CLI


def step_download(root: Path) -> dict[str, str]:
    from emx.i1a import hfget

    groups = [
        ("qwen", MODEL_ID, hfget.model_files(), MODEL_REV),
        ("jlens", JLENS[0], [JLENS[2]], JLENS[1]),
        ("jpp", JPP[0], [JPP[2]], JPP[1]),
    ]
    total = sum(hfget.plan_bytes(repo, files, revision=rev) for _, repo, files, rev in groups)
    hfget.check_total(total, "P36 downloads")
    log(f"download plan: {total / 1e9:.1f} GB")
    paths = {}
    for name, repo, files, rev in groups:
        got = hfget.fetch(repo, files, revision=rev, local_dir=root / name)
        paths[name] = str(root / name) if name == "qwen" else str(next(iter(got.values())))
    write_json(OUT / "download_paths.json", paths)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("download", "all", "followup"))
    ap.add_argument("--artifacts-dir", default=os.environ.get("P36_ARTIFACTS", "/ws/p36_artifacts"))
    ap.add_argument("--limit-items", type=int, default=0)
    ap.add_argument("--batch", type=int, default=int(os.environ.get("P36_BATCH", "128")))
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
        lens_paths={"jlens": paths["jlens"], "jpp": paths["jpp"]},
        max_hours=float(os.environ.get("P36_MAX_HOURS", "2.2")),
        t0=float(os.environ.get("P36_T0", time.time())),
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
    if a.step == "followup":
        cfg.followup = True
        cfg.out = OUT / "followup"
        cfg.out.mkdir(parents=True, exist_ok=True)
        return run_followup(Session(backend, cfg))
    sess = Session(backend, cfg)
    return run_all(sess)


if __name__ == "__main__":
    raise SystemExit(main())
