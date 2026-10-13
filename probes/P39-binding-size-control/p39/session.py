"""P39 pod session (plan.md): one model load, every arm at P36's selected cell (entity@48).

    python -m p39.session download      # Qwen3.6-27B + J-lens -> results/download_paths.json
    python -m p39.session all [--limit-items N]

Reuses P36 unchanged (``probes/P36-workspace-binding-causal/p36``, on PYTHONPATH): stimuli and
renders, donor maps, valid pairs, the patch hooks and ``run_jobs`` (rows), the J-lens dictionary
and NNOMP. P39 adds the arms (``p39.arms``) and the gradients (``p39.grad``).

Phases (each timed into ``results/timing.json``):

1. ``renders``, ``capture``: L48 states of every sentence at P36's three slots ('.', e1, e2), plus
   every position after the first four of the fit items' prompts (for the covariance).
2. ``smoke``: same-batch identity patch exact; first 20 prompts >= 18 correct; gradient vs
   central finite differences (Pearson r >= 0.8 over 4 prompts x 8 directions); NNOMP on 16 states
   (k atoms, 0 < FVE < 1, KKT < 1e-3); projected wall-clock <= 80% of the time left. rc 3 if not.
3. ``baseline`` (gate 1: accuracy >= 0.95 per question), ``grads`` (dLD/dh at both entity tokens
   for all 800 prompts).
4. ``decomp``: J-lens NNOMP at k in K_SWEEP for all entity states, the axes' J-lens pursuit, the
   random-atom spans; then the rotated dictionary (D_J Q) at K_SWEEP.
5. ``fit``: on P36's train items only: PCA of twin differences, the order / role gradient axes,
   the mean-difference axis, the covariance square root.
6. ``anchors`` (gate 2: P36's entity@48 numbers reproduced), then ``primary``, ``secondary``,
   ``crossvoice`` forwards. Every arm is an additive change ``delta`` at both entity tokens; its
   per-case statistics go to ``results/arm_stats.jsonl.gz`` and its forwards to
   ``results/rows/<phase>.jsonl`` (P36's row format; ``dict`` holds the arm name).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from p36 import session as P36S
from p36 import stimuli as S

from p39 import arms as A

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "results"
SEED = 20261013
LAYER = 48
SITE = "entity"
K1 = 25
K_SWEEP = (16, 25, 100, 400)
N_RAND = 20  # matched random-subspace draws per family
N_DOSE = 5  # draws per dose point
DOSE_FRACS = (0.005, 0.125, 0.25, 0.5)
PCA_R = (1, 2, 4, 8, 16, 32, 64)
AX_ALPHAS = (0.25, 0.5, 2.0)
N_ATOMS = 5
N_1D = 20
N_POT = 5
SKIP_POS = 4  # covariance states: positions after the first four (chat-template prefix)
SEED_ISO, SEED_COV, SEED_ROT, SEED_ATOMS, SEED_1D = 20261013, 20261113, 20261040, 20261050, 20261060
# gate 2: P36 entity@48 (note.md): full 95.2%, residual swap 95.0%, J-lens swap 0.1%
P36_FULL, P36_RSWAP, TOL, JL_MAX = 0.952, 0.950, 0.03, 0.01
FD_MIN_R = 0.8


@dataclass
class Config:
    layer: int = LAYER
    n_layers: int = 64
    batch: int = 128
    pursuit_batch: int = 128
    grad_batch: int = 16
    k_sweep: tuple[int, ...] = K_SWEEP
    k1: int = K1
    n_rand: int = N_RAND
    n_dose: int = N_DOSE
    limit_items: int = 0
    out: Path = OUT
    jlens_path: str = ""
    max_hours: float = 1.0
    t0: float = field(default_factory=time.time)
    gates: bool = True  # tiny-model tests turn the accuracy/replication gates off


log = P36S.log
utc = P36S.utc
write_json = P36S.write_json


class Session(P36S.Session):
    """P36's session with P39's arms: ``op == "delta"`` jobs patch h_r + delta."""

    def __init__(self, backend: Any, cfg: Config) -> None:
        pcfg = P36S.Config(
            stage0_layers=(cfg.layer,), n_layers=cfg.n_layers, batch=cfg.batch,
            pursuit_batch=cfg.pursuit_batch, k1=cfg.k1, k_sweep=cfg.k_sweep,
            limit_items=0, out=cfg.out, lens_paths={"jlens": cfg.jlens_path},
            max_hours=cfg.max_hours, t0=cfg.t0,
        )  # fmt: skip
        cfg.out.mkdir(parents=True, exist_ok=True)
        super().__init__(backend, pcfg)
        self.c = cfg
        self.L = cfg.layer
        if cfg.limit_items:  # the first n items of each split (fit and held items both present)
            keep: dict[str, list[int]] = {}
            for s in self.sents:
                ks = keep.setdefault(s.split, [])
                if s.item not in ks and len(ks) < cfg.limit_items:
                    ks.append(s.item)
            items = {i for ks in keep.values() for i in ks}
            self.sents = [s for s in self.sents if s.item in items]
            self.idx = {s.sid: i for i, s in enumerate(self.sents)}
            self.by_sid = {s.sid: s for s in self.sents}
            self.dmaps = P36S.donor_maps(self.sents)
        self.deltas: dict[tuple[str, str], Any] = {}
        self.N = len(self.sents)
        self.fit_mask = [s.split == "train" for s in self.sents]
        e = [sorted((s.agent, s.patient)) for s in self.sents]
        first_ent = [s.agent if s.voice == "active" else s.patient for s in self.sents]
        self.first_slot = [0 if f == pair[0] else 1 for f, pair in zip(first_ent, e, strict=True)]

    # ---------------------------------------------------------------- capture
    def capture(self) -> None:
        import torch
        from p36 import model as MD

        groups: dict[int, list[str]] = {}
        for s in self.sents:
            groups.setdefault(self.R[(s.sid, "agent")]["n"], []).append(s.sid)
        H = None
        for _, sids in sorted(groups.items()):
            for lo in range(0, len(sids), self.cfg.batch):
                chunk = sids[lo : lo + self.cfg.batch]
                ids = [self.R[(sid, "agent")]["ids"] for sid in chunk]
                pos = [
                    self.positions(sid, "agent", "final") + self.positions(sid, "agent", "entity")
                    for sid in chunk
                ]
                got = MD.capture(self.b, ids, pos, [self.L])[self.L]
                if H is None:
                    H = torch.empty(self.N, 3, got.shape[-1], dtype=got.dtype)
                H[[self.idx[s] for s in chunk]] = got
        self.H[self.L] = H
        self.E = H[:, 1:3].float()  # [N, 2, d] entity states (slot 0 = e1, 1 = e2)
        for s in self.sents:
            assert self.positions(s.sid, "agent", SITE) == self.positions(s.sid, "patient", SITE)

    def capture_cov_states(self) -> Any:
        """L48 states at every position after SKIP_POS of the fit items' prompts (both
        questions), fp32 CPU [M, d]."""
        import torch

        from p39 import grad as G

        groups: dict[int, list[tuple[str, str]]] = {}
        for s, fit in zip(self.sents, self.fit_mask, strict=True):
            if fit:
                for q in S.QUESTIONS:
                    groups.setdefault(self.R[(s.sid, q)]["n"], []).append((s.sid, q))
        parts = []
        for _, keys in sorted(groups.items()):
            for lo in range(0, len(keys), self.cfg.batch):
                ids = [self.R[k]["ids"] for k in keys[lo : lo + self.cfg.batch]]
                parts.append(G.capture_all(self.b, ids, self.L, SKIP_POS).float())
        return torch.cat(parts)

    # ---------------------------------------------------------------- gradients
    def prompts_by_len(self, keys: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        groups: dict[int, list[tuple[str, str]]] = {}
        for k in keys:
            groups.setdefault(self.R[k]["n"], []).append(k)
        out = []
        for _, ks in sorted(groups.items()):
            for lo in range(0, len(ks), self.c.grad_batch):
                out.append(ks[lo : lo + self.c.grad_batch])
        return out

    def grads(self) -> None:
        """self.Gq[q]: [N, 2, d] fp32 dLD/dh at (e1, e2) for every sentence under question q."""
        import torch

        from p39 import grad as G

        d = self.E.shape[-1]
        self.Gq = {q: torch.zeros(self.N, 2, d) for q in S.QUESTIONS}
        self.LD0 = {q: torch.zeros(self.N) for q in S.QUESTIONS}
        keys = [(s.sid, q) for s in self.sents for q in S.QUESTIONS]
        for chunk in self.prompts_by_len(keys):
            ids = [self.R[k]["ids"] for k in chunk]
            pos = [self.positions(sid, q, SITE) for sid, q in chunk]
            ans = [self.answers(sid, q) for sid, q in chunk]
            got = G.ld_grad(self.b, ids, pos, self.L, ans)
            for i, (sid, q) in enumerate(chunk):
                self.Gq[q][self.idx[sid]] = got["grad"][i]
                self.LD0[q][self.idx[sid]] = got["ld"][i]

    # ---------------------------------------------------------------- forwards
    def values(self, job: dict, layer: int) -> Any:
        if job["op"] != "delta":
            return super().values(job, layer)
        r = self.idx[job["recip"]]
        Hr = self.H[layer][r, P36S.SLOTS[SITE]].float()
        return Hr + self.deltas[(job["dict"], job["kind"])][r]

    def D(self, kind: str) -> Any:
        """[N, 2, d] donor-minus-recipient entity states for a donor kind (0 without a donor)."""
        import torch

        out = torch.zeros_like(self.E)
        for sid, dsid in self.dmaps[kind].items():
            out[self.idx[sid]] = self.E[self.idx[dsid]] - self.E[self.idx[sid]]
        return out

    def recon_D(self, rec: Any, kind: str) -> Any:
        import torch

        out = torch.zeros_like(self.E)
        for sid, dsid in self.dmaps[kind].items():
            out[self.idx[sid]] = rec[self.idx[dsid]] - rec[self.idx[sid]]
        return out

    def pairs(self, kind: str, scope: str) -> list[tuple[str, str]]:
        ps = sorted(P36S.valid_pairs(self.base, self.dmaps[kind]))
        if scope == "held":
            ps = [(sid, q) for sid, q in ps if not self.fit_mask[self.idx[sid]]]
        return ps

    def run_arms(self, specs: list[tuple[str, str, str, Any]], phase: str) -> list[dict]:
        """specs: (arm, family, kind, scope, delta [N, 2, d]) -> stats + forwards."""
        jobs = []
        for arm, family, kind, scope, delta in specs:
            self.deltas[(arm, kind)] = delta
            self.write_stats(arm, family, kind, scope, delta)
            for sid, q in self.pairs(kind, scope):
                jobs.append({"recip": sid, "donor": self.dmaps[kind][sid], "q": q, "site": SITE,
                             "layers": (self.L,), "kind": kind, "op": "delta", "dict": arm,
                             "k": None})  # fmt: skip
        rows = self.run_jobs(jobs, phase)
        for arm, _, kind, _, _ in specs:
            del self.deltas[(arm, kind)]
        return rows

    def write_stats(self, arm: str, family: str, kind: str, scope: str, delta: Any) -> None:
        Dk = self.D(kind)
        st = A.case_stats(delta, Dk, self.Gq, self.axes)
        lines = []
        for sid in self.dmaps[kind]:
            if scope == "held" and self.fit_mask[self.idx[sid]]:
                continue
            i = self.idx[sid]
            rec = {"arm": arm, "family": family, "kind": kind, "recip": sid}
            for k, v in st.items():
                x = v[i]
                rec[k] = [round(float(t), 6) for t in x] if x.ndim else round(float(x), 6)
            lines.append(json.dumps(rec) + "\n")
        # one complete gzip member per arm (a crash never leaves a truncated member)
        with gzip.open(self.out / "arm_stats.jsonl.gz", "at") as fh:
            fh.writelines(lines)

    # ---------------------------------------------------------------- decompositions
    def jlens_dict(self) -> Any:
        from p36 import model as MD

        J = MD.load_jacobians(self.c.jlens_path, [self.L])[self.L]
        return MD.build_dictionary(self.b, J)

    def pursue_entities(self, Dict: Any, ks: tuple[int, ...]) -> dict[int, dict]:
        from p36 import pursuit as P

        x = self.E.reshape(-1, self.E.shape[-1])
        out = {}
        for k in ks:
            t = time.time()
            r = P.pursue_batched(x, Dict, k, batch=self.cfg.pursuit_batch)
            out[k] = {
                "recon": r["recon"].reshape(self.E.shape),
                "fve": r["fve"].reshape(-1, 2),
                "kkt_max": float(r["kkt"].max()),
                "n_active_mean": float(r["n_active"].float().mean()),
                "seconds": round(time.time() - t, 1),
            }
            log(f"pursuit k={k}: FVE {float(r['fve'].mean()):.4f} {time.time() - t:.0f}s")
        return out

    def axis_pursuit(self, Dict: Any, axes: dict[str, Any]) -> dict[str, Any]:
        """J-lens NNOMP of +u and -u for each axis at k = 25 and 100: FVE and top atoms."""
        import torch
        from p36 import pursuit as P

        tok = self.b.tokenizer
        out: dict[str, Any] = {}
        for name, u in axes.items():
            x = torch.stack([u, -u]) * 100.0  # scale is irrelevant to FVE
            for k in (25, 100):
                r = P.pursue_batched(x, Dict, k, batch=2)
                for j, sgn in enumerate(("+", "-")):
                    order = torch.argsort(r["coeffs"][j], descending=True)[:8]
                    top = [
                        tok.convert_ids_to_tokens(int(r["atoms"][j, o]))
                        for o in order
                        if int(r["atoms"][j, o]) >= 0 and float(r["coeffs"][j, o]) > 0
                    ]
                    out[f"{name}{sgn}k{k}"] = {"fve": float(r["fve"][j]), "top": top}
        return out


# --------------------------------------------------------------------- the run


def nonzero_rows(Dict: Any) -> Any:
    return (Dict.norm(dim=1) > 0).nonzero().squeeze(-1).cpu()


def smoke(sess: Session) -> dict:
    import torch
    from p36 import model as MD
    from p36 import pursuit as P

    from p39 import grad as G

    res: dict[str, Any] = {}
    L = sess.L
    n0 = sess.R[(sess.sents[0].sid, "agent")]["n"]
    same = [s.sid for s in sess.sents if sess.R[(s.sid, "agent")]["n"] == n0][:8]
    ids = [sess.R[(sid, "agent")]["ids"] for sid in same]
    pos = [sess.positions(sid, "agent", SITE) for sid in same]
    ans = [sess.answers(sid, "agent") for sid in same]
    got = MD.capture(sess.b, ids, pos, [L])[L]
    base = MD.forward_patched(sess.b, ids, None, answer_ids=ans)
    pat = MD.forward_patched(sess.b, ids, {L: (torch.tensor(pos), got)}, answer_ids=ans)
    res["identity_max_abs"] = float((base["ans"] - pat["ans"]).abs().max())
    res["identity_ok"] = res["identity_max_abs"] == 0.0
    first = [(s.sid, q) for s in sess.sents[:10] for q in S.QUESTIONS]
    correct = 0
    for sid, q in first:
        a = sess.answers(sid, q)
        r = MD.forward_patched(sess.b, [sess.R[(sid, q)]["ids"]], None, answer_ids=[a])
        correct += int(int(r["top1"][0]) == a[0])
    res["first20_correct"] = correct
    res["accuracy_ok"] = correct >= 18 or not sess.c.gates
    # gradient vs central finite differences: 4 prompts x 8 directions
    t = time.time()
    g = G.ld_grad(sess.b, ids[:4], pos[:4], L, ans[:4])
    res["sec_grad_4"] = round(time.time() - t, 2)
    gen = torch.Generator().manual_seed(SEED)
    pred, fd = [], []
    for c in torch.linspace(-1, 1, 8):
        noise = torch.randn(g["grad"].shape, generator=gen)
        v = c * g["grad"] / g["grad"].norm(dim=(-1, -2), keepdim=True) + 0.5 * noise / noise.norm(
            dim=(-1, -2), keepdim=True
        )
        v = v / v.norm(dim=(-1, -2), keepdim=True)
        eps = 0.02 * g["h"].norm(dim=-1).mean()
        lp = MD.forward_patched(
            sess.b, ids[:4], {L: (torch.tensor(pos[:4]), g["h"] + eps * v)}, answer_ids=ans[:4]
        )["ans"]
        lm = MD.forward_patched(
            sess.b, ids[:4], {L: (torch.tensor(pos[:4]), g["h"] - eps * v)}, answer_ids=ans[:4]
        )["ans"]
        fd += (((lp[:, 0] - lp[:, 1]) - (lm[:, 0] - lm[:, 1])) / (2 * eps)).tolist()
        pred += (g["grad"] * v).sum((-1, -2)).tolist()
    pt, ft = torch.tensor(pred), torch.tensor(fd)
    r = float(torch.corrcoef(torch.stack([pt, ft]))[0, 1])
    res["fd_pearson"] = round(r, 4)
    res["fd_slope"] = round(float((pt * ft).sum() / (pt * pt).sum()), 4)
    res["fd_ok"] = r >= FD_MIN_R
    # pursuit on 16 entity states, J-lens
    t = time.time()
    Dict = sess.jlens_dict()
    x = sess.E.reshape(-1, sess.E.shape[-1])[:16]
    o = P.pursue_batched(x, Dict, sess.c.k1, batch=16)
    big = P.pursue_batched(x, Dict, max(sess.c.k_sweep), batch=16)
    res["pursuit_ok"] = bool(((o["fve"] > 0) & (o["fve"] < 1)).all() and (o["kkt"] < 1e-3).all()
                             and (big["kkt"] < 1e-3).all())  # fmt: skip
    res["sec_pursuit_smoke"] = round(time.time() - t, 2)
    del Dict
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    # throughput and projection
    t = time.time()
    b_ids = (ids * (sess.cfg.batch // len(ids) + 1))[: sess.cfg.batch]
    MD.forward_patched(sess.b, b_ids, None)
    res["sec_per_batch"] = round(time.time() - t, 3)
    n_fwd = 110_000 * sess.N / 400
    n_grad_batches = 2 * sess.N / sess.c.grad_batch
    res["projected_seconds"] = round(
        n_fwd / sess.cfg.batch * res["sec_per_batch"]
        + n_grad_batches * res["sec_grad_4"] * sess.c.grad_batch / 4 / 2  # batching amortises
        + 2 * len(sess.c.k_sweep) * res["sec_pursuit_smoke"] * sess.N * 2 / 16 / 4
    )
    res["time_left"] = round(sess.time_left())
    res["budget_ok"] = res["projected_seconds"] <= 0.8 * res["time_left"]
    res["ok"] = all(res[k] for k in ("identity_ok", "accuracy_ok", "fd_ok", "pursuit_ok",
                                     "budget_ok"))  # fmt: skip
    write_json(sess.out / "smoke.json", res)
    return res


def fit(sess: Session, s_bar: float) -> dict[str, Any]:
    """Directions from P36's train items only (twin differences and gradients)."""
    import torch

    fit_idx = [i for i in range(sess.N) if sess.fit_mask[i]]
    Dt = sess.D("twin")
    rows, first_in_donor = [], []
    for i in fit_idx:
        s = sess.sents[i]
        if s.role != "d_agent" or s.sid not in sess.dmaps["twin"]:
            continue  # each twin pair once
        j = sess.idx[sess.dmaps["twin"][s.sid]]
        for slot in (0, 1):
            rows.append(Dt[i, slot])
            first_in_donor.append(sess.first_slot[j] == slot)
    Dfit = torch.stack(rows)
    V, share_by_r = A.pca_basis(Dfit, max(PCA_R))
    r_star = A.smallest_r(share_by_r, s_bar)
    g = torch.cat([sess.Gq[q][fit_idx] for q in S.QUESTIONS])
    first = torch.tensor([sess.first_slot[i] for i in fit_idx] * len(S.QUESTIONS))
    ans_first, agent_q = [], []
    for q in S.QUESTIONS:
        for i in fit_idx:
            s = sess.sents[i]
            ans_first.append(S.answer(s, q) == (s.agent if s.voice == "active" else s.patient))
            agent_q.append(q == "agent")
    u_order = A.order_axis(g, first)
    u_role = A.role_axis(g, first, torch.tensor(ans_first), torch.tensor(agent_q))
    u_md = A.mean_diff_axis(Dfit, torch.tensor(first_in_donor))
    B_star = V[:, :r_star]
    u_in, u_out = A.split_axis(u_order, B_star)
    sess.V, sess.r_star, sess.share_by_r = V, r_star, share_by_r
    sess.ax = {"order": u_order, "role": u_role, "md": u_md, "in": u_in, "out": u_out}
    sess.axes = {"order": u_order, "pc1": V[:, 0]}
    # Makelov's spread check: |u.D| (twin, all items) against the s.d. of u.h over entity states
    Eall = sess.E.reshape(-1, sess.E.shape[-1])
    spread = {}
    for name, u in {**sess.ax, "pc1": V[:, 0]}.items():
        proj = Eall @ u
        dproj = (Dt @ u).abs()
        spread[name] = {
            "sd_uh": float(proj.std()),
            "mean_abs_uD": float(dproj[dproj > 0].mean()),
            "native_share_mean": float(
                ((Dt @ u) ** 2).sum(-1).sum() / (Dt**2).sum((-1, -2)).clamp_min(1e-12).sum()
            ),
        }
    cos = {f"{a}~{b}": float(sess.ax[a] @ (sess.ax[b] if b in sess.ax else V[:, 0]))
           for a, b in (("order", "pc1"), ("md", "pc1"), ("role", "pc1"), ("order", "md"),
                        ("order", "role"))}  # fmt: skip
    return {"s_bar_fit": s_bar, "r_star": r_star, "pca_share_by_r": share_by_r.tolist(),
            "n_fit_twin_rows": len(rows), "spread": spread, "cos": cos}  # fmt: skip


def build_specs(sess: Session, group: str) -> list[tuple]:
    """(arm, family, kind, scope, delta) for one forward group."""
    import torch

    d = sess.E.shape[-1]
    Dt = sess.D("twin")
    Jt = sess.recon_D(sess.JL[K1]["recon"], "twin")
    out: list[tuple] = []
    if group == "anchors":
        out.append(("full", "full", "twin", "all", Dt))
        out.append(("jl25", "jlens", "twin", "all", Jt))
        out.append(("rs25", "resid", "twin", "all", Dt - Jt))
        out.append(("self0", "self", "self", "all", torch.zeros_like(Dt)))
        return out
    if group == "primary":
        m = min(d, max(1, round(sess.s_bar * d)))
        for i in range(sess.c.n_rand):
            B = A.haar_basis(d, m, SEED_ISO + i)
            out.append((f"iso_m{i}", "iso_m", "twin", "all", A.match(A.project(Dt, B), Jt)))
        for i in range(sess.c.n_rand):
            B = A.cov_basis(sess.cov_sqrt, m, SEED_COV + i)
            out.append((f"cov_m{i}", "cov_m", "twin", "all", A.match(A.project(Dt, B), Jt)))
        Rt = sess.recon_D(sess.JR[K1]["recon"], "twin")
        out.append(("jrot25", "jrot", "twin", "all", Rt))
        out.append(("jrot25_m", "jrot_m", "twin", "all", A.match(Rt, Jt)))
        B = sess.V[:, : sess.r_star]
        out.append(("pca_m", "pca_m", "twin", "held", A.match(A.project(Dt, B), Jt)))
        for name in ("order", "role", "md"):
            c = A.axis_component(Dt, sess.ax[name])
            out.append((f"ax_{name}", f"ax_{name}", "twin", "held", c))
            out.append((f"ax_{name}_m", f"ax_{name}_m", "twin", "held", A.match(c, Jt)))
        return out
    if group == "secondary":
        for k in sess.c.k_sweep:
            if k != K1:
                out.append(
                    (f"jl{k}", "jlens", "twin", "all", sess.recon_D(sess.JL[k]["recon"], "twin"))
                )
                out.append(
                    (f"jrot{k}", "jrot", "twin", "all", sess.recon_D(sess.JR[k]["recon"], "twin"))
                )
        for f in DOSE_FRACS:
            m = 25 if f == 0.005 else max(1, round(f * d))
            for i in range(sess.c.n_dose):
                B = A.haar_basis(d, m, SEED_ISO + 1000 * (1 + DOSE_FRACS.index(f)) + i)
                out.append((f"iso_f{f}_{i}", f"iso_f{f}", "twin", "all", A.project(Dt, B)))
                B = A.cov_basis(sess.cov_sqrt, m, SEED_COV + 1000 * (1 + DOSE_FRACS.index(f)) + i)
                out.append((f"cov_f{f}_{i}", f"cov_f{f}", "twin", "all", A.project(Dt, B)))
        for i, Batoms in enumerate(sess.atom_bases):
            out.append((f"jatoms{i}", "jatoms", "twin", "all", A.project(Dt, Batoms)))
        res_norm = (Dt - Jt).norm(dim=-1)
        out.append(("pot_jl", "pot_jl", "twin", "all", A.rescale_to(Jt, res_norm)))
        m = min(d, max(1, round(sess.s_bar * d)))
        for i in range(N_POT):
            B = A.haar_basis(d, m, SEED_ISO + i)
            out.append(
                (f"pot_iso{i}", "pot_iso", "twin", "all", A.rescale_to(A.project(Dt, B), res_norm))
            )
        for r in PCA_R:
            out.append((f"pca{r}", "pca", "twin", "held", A.project(Dt, sess.V[:, :r])))
        c = A.axis_component(Dt, sess.ax["order"])
        for a in AX_ALPHAS:
            out.append((f"ax_order_a{a}", "ax_order_dose", "twin", "held", a * c))
        for name in ("in", "out"):
            c2 = A.axis_component(Dt, sess.ax[name])
            out.append((f"ax_{name}", f"ax_{name}", "twin", "held", c2))
            out.append((f"ax_{name}_m", f"ax_{name}_m", "twin", "held", A.match(c2, Jt)))
        for i in range(N_1D):
            gen = torch.Generator().manual_seed(SEED_1D + i)
            v = A.unit(torch.randn(d, generator=gen))
            out.append((f"r1d{i}", "r1d_m", "twin", "held", A.match(A.axis_component(Dt, v), Jt)))
        return out
    if group == "crossvoice":
        for kind in ("xsame", "xtwin"):
            Dk = sess.D(kind)
            Jk = sess.recon_D(sess.JL[K1]["recon"], kind)
            out.append(("full", "full", kind, "all", Dk))
            out.append(("jl25", "jlens", kind, "all", Jk))
            out.append(
                (
                    "pca_m",
                    "pca_m",
                    kind,
                    "held",
                    A.match(A.project(Dk, sess.V[:, : sess.r_star]), Jk),
                )
            )
            for name in ("order", "md"):
                out.append(
                    (
                        f"ax_{name}_m",
                        f"ax_{name}_m",
                        kind,
                        "held",
                        A.match(A.axis_component(Dk, sess.ax[name]), Jk),
                    )
                )
        return out
    raise KeyError(group)


def gate2(rows: list[dict], base: dict) -> dict[str, Any]:
    from collections import defaultdict

    by: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by[r["dict"]].append(int(r["top1"] == r["a_rev"]))
    rate = {k: sum(v) / len(v) for k, v in by.items() if v}
    ok = (abs(rate.get("full", 0) - P36_FULL) <= TOL and abs(rate.get("rs25", 0) - P36_RSWAP) <= TOL
          and rate.get("jl25", 1) <= JL_MAX and rate.get("self0", 1) == 0.0)  # fmt: skip
    return {"rates": rate, "gate2": ok}


def run_all(sess: Session) -> int:
    import torch

    T, out, c = sess.timer, sess.out, sess.c
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
        cov_states = sess.capture_cov_states()
    status["n_cov_states"] = int(cov_states.shape[0])
    with T.step("smoke"):
        sm = smoke(sess)
    status["smoke"] = sm
    if not sm["ok"]:
        return done(3, f"smoke failed: {sm}")
    with T.step("baseline"):
        g1 = sess.baseline()
    status["gate1"] = g1
    if c.gates and not g1["gate1"]:
        return done(0, f"gate 1 failed: {g1['accuracy']}")
    with T.step("grads"):
        sess.grads()
    with T.step("decomp_jlens"):
        Dict = sess.jlens_dict()
        sess.JL = sess.pursue_entities(Dict, c.k_sweep)
        nz = nonzero_rows(Dict)
        sess.atom_bases = []
        for i in range(N_ATOMS):
            gen = torch.Generator().manual_seed(SEED_ATOMS + i)
            pick = nz[torch.randperm(len(nz), generator=gen)[:K1]]
            sess.atom_bases.append(A.orthonormal(Dict[pick.to(Dict.device)].float().cpu().T))
    # the matched share target: mean J-lens k = 25 twin share on the fit items
    Dt, Jt = sess.D("twin"), sess.recon_D(sess.JL[K1]["recon"], "twin")
    fit_rows = [
        i for i in range(sess.N) if sess.fit_mask[i] and sess.sents[i].sid in sess.dmaps["twin"]
    ]
    sh = (Jt[fit_rows] ** 2).sum(-1) / (Dt[fit_rows] ** 2).sum(-1).clamp_min(1e-12)
    sess.s_bar = float(sh.mean())
    with T.step("fit"):
        status["fit"] = fit(sess, sess.s_bar)
        sess.cov_sqrt = A.cov_sqrt(cov_states.to(sess.b.device) if torch.cuda.is_available()
                                   else cov_states).cpu()  # fmt: skip
        del cov_states
    with T.step("axis_pursuit"):
        status["axis_pursuit"] = sess.axis_pursuit(
            Dict, {"order": sess.ax["order"], "md": sess.ax["md"], "pc1": sess.V[:, 0]}
        )
    with T.step("decomp_rot"):
        Q = A.rotation(Dict.shape[1], SEED_ROT).to(Dict.device)
        for lo in range(0, Dict.shape[0], 16384):
            Dict[lo : lo + 16384] = Dict[lo : lo + 16384] @ Q
        del Q
        sess.JR = sess.pursue_entities(Dict, c.k_sweep)
        del Dict
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    status["decomp"] = {
        name: {k: {kk: v[kk] for kk in ("kkt_max", "n_active_mean", "seconds")}
               | {"fve_mean": float(v["fve"].mean())} for k, v in res.items()}
        for name, res in (("jlens", sess.JL), ("jrot", sess.JR))
    }  # fmt: skip
    status["s_bar"] = sess.s_bar
    write_json(out / "status.json", status)
    save_states(sess)
    with T.step("anchors"):
        rows = sess.run_arms(build_specs(sess, "anchors"), "anchors")
    status["gate2"] = gate2(rows, sess.base)
    write_json(out / "status.json", status)
    if c.gates and not status["gate2"]["gate2"]:
        return done(0, f"gate 2 (replication) failed: {status['gate2']}")
    for group in ("primary", "secondary", "crossvoice"):
        if sess.time_left() < 300:
            return done(0, f"stopped before {group}: time")
        with T.step(group):
            sess.run_arms(build_specs(sess, group), group)
    return done(0, "complete")


def save_states(sess: Session) -> None:
    """Entity states, J-lens/J_rot k = 25 reconstructions, gradients and fitted directions."""
    from safetensors.torch import save_file

    d = sess.out / "states"
    d.mkdir(parents=True, exist_ok=True)
    t = {"E": sess.E.half().contiguous(),
         "jl25": sess.JL[K1]["recon"].half().contiguous(),
         "jrot25": sess.JR[K1]["recon"].half().contiguous(),
         "V": sess.V.contiguous(), **{f"ax_{k}": v.contiguous() for k, v in sess.ax.items()},
         **{f"g_{q}": v.half().contiguous() for q, v in sess.Gq.items()},
         **{f"ld0_{q}": v.contiguous() for q, v in sess.LD0.items()}}  # fmt: skip
    save_file(t, str(d / "p39_states.safetensors"))
    write_json(
        d / "index.json",
        {
            "sids": [s.sid for s in sess.sents],
            "slots": ["e1", "e2"],
            "layer": sess.L,
            "first_slot": sess.first_slot,
        },
    )
    with (sess.out / "decomp_fve.json").open("w") as fh:
        json.dump(
            {
                name: {str(k): v["fve"].tolist() for k, v in res.items()}
                for name, res in (("jlens", sess.JL), ("jrot", sess.JR))
            },
            fh,
        )


# --------------------------------------------------------------------- CLI


def step_download(root: Path) -> dict[str, str]:
    from emx.i1a import hfget

    groups = [
        ("qwen", P36S.MODEL_ID, hfget.model_files(), P36S.MODEL_REV),
        ("jlens", P36S.JLENS[0], [P36S.JLENS[2]], P36S.JLENS[1]),
    ]
    total = sum(hfget.plan_bytes(repo, files, revision=rev) for _, repo, files, rev in groups)
    hfget.check_total(total, "P39 downloads")
    log(f"download plan: {total / 1e9:.1f} GB")
    paths = {}
    for name, repo, files, rev in groups:
        got = hfget.fetch(repo, files, revision=rev, local_dir=root / name)
        paths[name] = str(root / name) if name == "qwen" else str(next(iter(got.values())))
    write_json(OUT / "download_paths.json", paths)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("download", "all"))
    ap.add_argument("--artifacts-dir", default=os.environ.get("P39_ARTIFACTS", "/ws/p39_artifacts"))
    ap.add_argument("--limit-items", type=int, default=0)
    ap.add_argument("--batch", type=int, default=int(os.environ.get("P39_BATCH", "128")))
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.step == "download":
        with P36S.Timer(OUT / "timing.json").step("download"):
            step_download(Path(a.artifacts_dir))
        return 0
    paths = json.loads((OUT / "download_paths.json").read_text())
    from emx.i1a.capture import load_backend

    cfg = Config(limit_items=a.limit_items, batch=a.batch, jlens_path=paths["jlens"],
                 max_hours=float(os.environ.get("P39_MAX_HOURS", "1.0")),
                 t0=float(os.environ.get("P39_T0", time.time())))  # fmt: skip
    with P36S.Timer(OUT / "timing.json").step("load"):
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
    return run_all(Session(backend, cfg))


if __name__ == "__main__":
    raise SystemExit(main())
