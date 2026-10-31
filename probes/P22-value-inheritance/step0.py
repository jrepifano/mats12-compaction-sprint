"""P22 step 0: do model families resemble each other on Zhang et al.'s published value positions
once a shared caution axis is removed?

Free: reads the archived `all_models` split of HF `jifanz/stress_testing_model_spec` at revision
a9f4320b03a8 (Zhang et al. 2025, arXiv 2510.07686) from `data/raw/P22/`, makes no API call and
records no spend. The design is pre-specified in `plan.md` (published to main at db08c64f).

    uv run --group swarm python probes/P22-value-inheritance/step0.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np

SEED = 20261012
REVISION = "a9f4320b03a8e08823ea9905271a8256e1729a2f"
N_BATTERY = 400
N_BOOT = 10_000
PASS_AUC = 0.65

FAMILY = {
    "claude_3_5_sonnet": "anthropic",
    "claude_3_7_sonnet": "anthropic",
    "claude_opus_3": "anthropic",
    "claude_opus_4": "anthropic",
    "claude_sonnet_4": "anthropic",
    "gpt_4o": "openai",
    "gpt_4_1": "openai",
    "gpt_4_1_mini": "openai",
    "o3": "openai",
    "o4_mini": "openai",
    "gemini_2_5_pro": "google",
    "grok_4": "xai",
}
MODELS = list(FAMILY)
META_REFUSAL = re.compile(
    r"^\s*(I cannot|I can't|I can not|I'm sorry|I am sorry|I apologi[sz]e|I won't|"
    r"I'm not able|I am not able|I'm unable)",
    re.IGNORECASE,
)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


# ---------------------------------------------------------------- data


def load_frame(paths: list[Path]):
    import pandas as pd
    import pyarrow.parquet as pq

    cols = ["rank", "chunk_index", "entry_idx", "query_generator", "nudge_direction",
            "value1", "value2", "query"]
    cols += [f"{m}_value{j}_position" for m in MODELS for j in (1, 2)]
    frames = []
    for p in paths:
        df = pq.read_table(p, columns=cols).to_pandas()
        df["shard"] = p.name
        df["row_in_shard"] = np.arange(len(df))
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def row_keys(df) -> list[str]:
    """A stable per-row key: shard file and row index within it (the parquet order is fixed by
    the pinned revision)."""
    return [f"{s}:{i}" for s, i in zip(df["shard"], df["row_in_shard"])]


def eligible_mask(df) -> np.ndarray:
    ok = ~df["query"].fillna("").map(lambda q: bool(META_REFUSAL.match(q))).to_numpy()
    for m in MODELS:
        for j in (1, 2):
            x = df[f"{m}_value{j}_position"]
            ok &= x.notna().to_numpy() & x.between(0, 6).to_numpy()
    return ok


def positions(df) -> np.ndarray:
    """12 x N matrix of p = value1_position - value2_position, rows in MODELS order."""
    return np.stack([
        df[f"{m}_value1_position"].to_numpy(float) - df[f"{m}_value2_position"].to_numpy(float)
        for m in MODELS
    ])


def battery_indices(n_eligible: int, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """400 items drawn from the eligible items (in parquet order), then a permutation from the same
    generator puts the first 200 in split A and the rest in split B. Returns positions into the
    eligible list."""
    rng = np.random.default_rng(seed)
    pick = np.sort(rng.choice(n_eligible, size=N_BATTERY, replace=False))
    perm = rng.permutation(N_BATTERY)
    return pick[perm[: N_BATTERY // 2]], pick[perm[N_BATTERY // 2:]]


def hash_split_b(keys: list[str], seed: int = SEED) -> np.ndarray:
    """Full-set split B: sha256("<seed>|<key>") ends in an odd hex digit."""
    return np.array([
        int(hashlib.sha256(f"{seed}|{k}".encode()).hexdigest()[-1], 16) % 2 == 1 for k in keys
    ])


# ---------------------------------------------------------------- measure


def centre_items(P: np.ndarray) -> np.ndarray:
    return P - P.mean(axis=0, keepdims=True)


def caution_axes(Pc: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Top-k item-space principal axes (k x N, orthonormal rows) of an item-centred matrix, and
    every component's share of variance."""
    _, s, vt = np.linalg.svd(Pc, full_matrices=False)
    return vt[:k], s**2 / np.sum(s**2)


def within_family_axis(Pc: np.ndarray, families: list[str]) -> np.ndarray:
    """PC1 of the profiles after centring each model on its family mean. Singleton families
    become zero rows and contribute nothing."""
    W = Pc.copy()
    fam = np.array(families)
    for f in set(families):
        idx = fam == f
        W[idx] -= W[idx].mean(axis=0, keepdims=True)
    return caution_axes(W, 1)[0][0]


def residualize(Pc: np.ndarray, V: np.ndarray) -> np.ndarray:
    if len(V) == 0:
        return Pc
    return Pc - (Pc @ V.T) @ V


def corr_rows(X: np.ndarray) -> np.ndarray:
    Z = X - X.mean(axis=1, keepdims=True)
    Z /= np.linalg.norm(Z, axis=1, keepdims=True)
    return Z @ Z.T


def pair_masks(families: list[str]) -> tuple[np.ndarray, np.ndarray]:
    n = len(families)
    iu = np.triu_indices(n, 1)
    same = np.array([families[i] == families[j] for i, j in zip(*iu)])
    return iu, same


def auc(same_vals: np.ndarray, cross_vals: np.ndarray) -> float:
    """P(same-family pair's r > cross-family pair's r), ties counted half."""
    d = same_vals[:, None] - cross_vals[None, :]
    return float((d > 0).mean() + 0.5 * (d == 0).mean())


def gate_auc(R: np.ndarray, families: list[str]) -> float:
    C = corr_rows(R)
    iu, same = pair_masks(families)
    r = C[iu]
    return auc(r[same], r[~same])


def attribution(R: np.ndarray, families: list[str], fams=("anthropic", "openai")) -> dict:
    """For each model in the two big families: mean Fisher-z of r to its own family's other
    members minus mean Fisher-z to the other family's members."""
    Z = np.arctanh(np.clip(corr_rows(R), -0.999999, 0.999999))
    fam = np.array(families)
    out = {}
    for i, f in enumerate(families):
        if f not in fams:
            continue
        other = next(g for g in fams if g != f)
        own_idx = [j for j in np.flatnonzero(fam == f) if j != i]
        out[i] = float(Z[i, own_idx].mean() - Z[i, fam == other].mean())
    return out


def bootstrap(R_b: np.ndarray, families: list[str], n_boot: int, seed: int) -> dict:
    """Resample whole items (columns) of the split-B residual matrix; v is held fixed."""
    rng = np.random.default_rng(seed)
    n = R_b.shape[1]
    aucs = np.empty(n_boot)
    contrasts = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        Rb = R_b[:, idx]
        aucs[b] = gate_auc(Rb, families)
        contrasts[b] = np.mean(list(attribution(Rb, families).values()))
    return {
        "auc_ci": [float(np.quantile(aucs, 0.025)), float(np.quantile(aucs, 0.975))],
        "contrast_ci": [float(np.quantile(contrasts, 0.025)),
                        float(np.quantile(contrasts, 0.975))],
    }


def analyse(P: np.ndarray, b_mask: np.ndarray, families: list[str], *, k: int | None = 1,
            axis: np.ndarray | None = None, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """One gate: centre items over all battery items, remove k PCs (or the given axis), compute
    affinities on split-B items, AUC, attribution and the item bootstrap."""
    Pc = centre_items(P)
    V, share = caution_axes(Pc, max(k or 0, 1))
    if axis is not None:
        V = axis[None, :]
    elif k == 0:
        V = V[:0]
    else:
        V = V[:k]
    R_b = residualize(Pc, V)[:, b_mask]
    a = gate_auc(R_b, families)
    att = attribution(R_b, families)
    res = {
        "n_items_b": int(b_mask.sum()),
        "auc": a,
        "attribution_correct": int(sum(v > 0 for v in att.values())),
        "attribution_n": len(att),
        "attribution_contrast_mean": float(np.mean(list(att.values()))),
        "attribution_by_model": {MODELS_OF(families)[i]: v for i, v in att.items()},
        "pc_variance_share": [float(x) for x in share[:5]],
    }
    if n_boot:
        res.update(bootstrap(R_b, families, n_boot, seed))
        res["passes"] = bool(a >= PASS_AUC and res["auc_ci"][0] > 0.5)
    return res


def MODELS_OF(families: list[str]) -> list[str]:
    return MODELS if len(families) == len(MODELS) else [f"m{i}" for i in range(len(families))]


# ------------------------------------------- revised primary: one-factor model (log.md, 22:13Z)
#
# Projecting out item-space PC1 (above) biases same-family pairs apart under the null. The
# replacement fits a one-factor model on split A and subtracts it from split B's covariance;
# loadings are model-level parameters, so they carry over between item splits. Item-centring
# makes the observed covariance H(LLᵀ + D)H, not LLᵀ + D, so the model is fit in that form
# (review r1, BLOCKING 1); `factor_fit` (plain principal-axis factoring) is only its starting point.


def factor_fit(S: np.ndarray, k: int = 1, iters: int = 500) -> tuple[np.ndarray, np.ndarray]:
    """Iterated principal-axis factoring: S ≈ L Lᵀ + diag(psi), L is n x k."""
    psi = 0.5 * np.diag(S)
    for _ in range(iters):
        w, vec = np.linalg.eigh(S - np.diag(psi))
        L = vec[:, -k:] * np.sqrt(np.clip(w[-k:], 0, None))
        new = np.clip(np.diag(S) - (L**2).sum(1), 1e-6, None)
        if np.allclose(new, psi, rtol=1e-8, atol=1e-10):
            break
        psi = new
    return L, psi


def centring_matrix(n: int) -> np.ndarray:
    return np.eye(n) - np.full((n, n), 1.0 / n)


def _noise_design(n: int) -> np.ndarray:
    """Columns vec(H e_i e_iᵀ H): H diag(d) H is linear in d."""
    H = centring_matrix(n)
    return np.stack([np.outer(H[:, i], H[:, i]).ravel() for i in range(n)], axis=1)


def centred_factor_fit(S: np.ndarray, k: int = 1, iters: int = 2000,
                       starts=(0.5, 0.1, 0.9)) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares fit of S ≈ H(LLᵀ + diag(d))H, where S is the covariance of item-centred
    profiles (so HSH = S) and H the centring matrix; d ≥ 0. Alternating least squares: given d,
    the best centred loadings are the top-k eigenpairs of S − H diag(d) H; given those, d is a
    non-negative linear least-squares fit. Several starts (d = share × diag S); the lowest
    objective wins. Returns L (n x k, centred: only HL is identified) and d. k = 0 fits the
    noise-only model H diag(d) H."""
    from scipy.optimize import nnls

    n = len(S)
    H = centring_matrix(n)
    X = _noise_design(n)
    best = None
    for share in starts:
        d = share * np.diag(S)
        L = np.zeros((n, k))
        prev = np.inf
        for _ in range(iters):
            if k:
                w, vec = np.linalg.eigh(S - H @ np.diag(d) @ H)
                L = vec[:, -k:] * np.sqrt(np.clip(w[-k:], 0, None))
            d = nnls(X, (S - L @ L.T).ravel())[0]
            obj = float(np.sum((H @ (L @ L.T + np.diag(d)) @ H - S) ** 2))
            if prev - obj <= 1e-12 * max(1.0, obj):
                break
            prev = obj
        if best is None or obj < best[0]:
            best = (obj, L, d)
    return best[1], best[2]


def fa_resid_corr(Xa: np.ndarray, Xb: np.ndarray, k: int = 1) -> np.ndarray:
    """Item-centred profiles split into A and B columns -> split-B covariance minus the k-factor
    model fit on A (in its centred form), divided by √(S_B,mm · S_B,m'm'). A standardised
    covariance remainder, not a Pearson correlation: it is not bounded by ±1 and the remainder
    need not be positive semi-definite. Only the off-diagonal entries are used."""
    SB = np.cov(Xb)
    H = centring_matrix(len(SB))
    L, d = centred_factor_fit(np.cov(Xa), k)
    R = SB - H @ (L @ L.T + np.diag(d)) @ H
    s = np.sqrt(np.diag(SB))
    return R / np.outer(s, s)


def _gate_from_corr(C: np.ndarray, families: list[str]) -> tuple[float, dict]:
    iu, same = pair_masks(families)
    r = C[iu]
    Z = np.arctanh(np.clip(C, -0.999999, 0.999999))
    fam = np.array(families)
    att = {}
    for i, f in enumerate(families):
        if f not in ("anthropic", "openai"):
            continue
        other = "openai" if f == "anthropic" else "anthropic"
        own = [j for j in np.flatnonzero(fam == f) if j != i]
        att[i] = float(Z[i, own].mean() - Z[i, fam == other].mean())
    return auc(r[same], r[~same]), att


def fa_gate(Pc: np.ndarray, b_mask: np.ndarray, families: list[str], k: int = 1) -> dict:
    C = fa_resid_corr(Pc[:, ~b_mask], Pc[:, b_mask], k)
    a, att = _gate_from_corr(C, families)
    iu, same = pair_masks(families)
    fam = np.array(families)
    r = C[iu]
    within = {f: float(r[same & (fam[iu[0]] == f)].mean()) for f in ("anthropic", "openai")}
    big2 = np.isin(fam[iu[0]], ("anthropic", "openai")) & np.isin(fam[iu[1]], ("anthropic", "openai"))
    return {
        "auc": a,
        # added post hoc (log.md): AUC over Claude/OpenAI pairs only, without Gemini and Grok
        "auc_claude_openai_only": auc(r[same & big2], r[~same & big2]),
        "attribution_correct": int(sum(v > 0 for v in att.values())),
        "attribution_n": len(att),
        "attribution_contrast_mean": float(np.mean(list(att.values()))),
        "attribution_by_model": {MODELS_OF(families)[i]: v for i, v in att.items()},
        "mean_resid_r": {"same_anthropic": within["anthropic"], "same_openai": within["openai"],
                         "cross": float(r[~same].mean())},
        "corr": C.round(4).tolist(),
    }


def fa_bootstrap(Pc: np.ndarray, b_mask: np.ndarray, families: list[str], k: int, n_boot: int,
                 seed: int) -> dict:
    """Resample A items and B items independently; the factor model is refit every time."""
    rng = np.random.default_rng(seed)
    Xa, Xb = Pc[:, ~b_mask], Pc[:, b_mask]
    aucs, cons = np.empty(n_boot), np.empty(n_boot)
    per_model = []
    for i in range(n_boot):
        C = fa_resid_corr(Xa[:, rng.integers(0, Xa.shape[1], Xa.shape[1])],
                          Xb[:, rng.integers(0, Xb.shape[1], Xb.shape[1])], k)
        aucs[i], att = _gate_from_corr(C, families)
        cons[i] = np.mean(list(att.values()))
        per_model.append(att)

    def q(x):
        return [float(np.quantile(x, 0.025)), float(np.quantile(x, 0.975))]

    names = MODELS_OF(families)
    # added post hoc in review round 2 (log.md): per-model attribution intervals
    by_model = {names[j]: q([a[j] for a in per_model]) for j in per_model[0]}
    return {"auc_ci": q(aucs), "contrast_ci": q(cons), "attribution_ci_by_model": by_model}


def simulate_panel(L: np.ndarray, psi: np.ndarray, n_items: int, rng, families: list[str],
                   block_var: float = 0.0, block_fams=("anthropic", "openai")) -> np.ndarray:
    """Gaussian draw from the fitted factor model (12 x n_items), item-centred, optionally with a
    planted family component of variance block_var shared by each named family's members."""
    X = L @ rng.normal(size=(L.shape[1], n_items)) + np.sqrt(psi)[:, None] * rng.normal(
        size=(len(psi), n_items))
    if block_var:
        for f in block_fams:
            idx = np.array([g == f for g in families])
            X[idx] += np.sqrt(block_var) * rng.normal(size=n_items)
    return centre_items(X)


def parametric_null(Pc: np.ndarray, b_mask: np.ndarray, families: list[str], n_sim: int,
                    seed: int, block_shares=(0.0, 0.02, 0.05, 0.10, 0.20),
                    n_sim_power: int = 200) -> dict:
    """Null AUC distribution for this exact panel (one-factor model fit to all items), and the
    AUC a planted family block of the given share of mean item variance would produce."""
    rng = np.random.default_rng(seed)
    S = np.cov(Pc)
    L, psi = centred_factor_fit(S, 1)
    mean_var = float(np.mean(np.diag(S)))
    out = {"loadings": dict(zip(MODELS_OF(families), map(float, L[:, 0]))),
           "uniqueness": dict(zip(MODELS_OF(families), map(float, psi))),
           "mean_item_var": mean_var}
    sims = [fa_gate(simulate_panel(L, psi, Pc.shape[1], rng, families), b_mask, families)
            for _ in range(n_sim)]
    null = np.array([g["auc"] for g in sims])
    # added post hoc (log.md): what the null predicts for each pair type and the contrast
    out["null_mean_resid_r"] = {key: float(np.mean([g["mean_resid_r"][key] for g in sims]))
                                for key in sims[0]["mean_resid_r"]}
    out["null_contrast_q975"] = float(np.quantile(
        [g["attribution_contrast_mean"] for g in sims], 0.975))
    out["null_attribution_q975_by_model"] = {
        m: float(np.quantile([g["attribution_by_model"][m] for g in sims], 0.975))
        for m in sims[0]["attribution_by_model"]}
    out["null_auc_claude_openai_only_q975"] = float(np.quantile(
        [g["auc_claude_openai_only"] for g in sims], 0.975))
    out["null_auc_mean"] = float(null.mean())
    out["null_auc_q975"] = float(np.quantile(null, 0.975))
    out["null_auc_q025"] = float(np.quantile(null, 0.025))
    power = {}
    for share in block_shares[1:]:
        a = np.array([fa_gate(simulate_panel(L, psi, Pc.shape[1], rng, families,
                                             share * mean_var), b_mask, families)["auc"]
                      for _ in range(n_sim_power)])
        power[str(share)] = {"auc_mean": float(a.mean()),
                             "pass_rate": float(np.mean((a >= PASS_AUC) &
                                                        (a > out["null_auc_q975"])))}
    out["power"] = power
    return out


# ---------------------------------------------------------------- run


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data/raw/P22")
    ap.add_argument("--out", type=Path, default=HERE / "results")
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    ap.add_argument("--n-sim", type=int, default=1000)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    paths = sorted(args.data.glob("all_models-*.parquet"))
    assert len(paths) == 4, paths
    df = load_frame(paths)
    keys = row_keys(df)
    ok = eligible_mask(df)
    meta_refusals = int(df["query"].fillna("").map(lambda q: bool(META_REFUSAL.match(q))).sum())
    el = df[ok].reset_index(drop=True)
    el_keys = [k for k, o in zip(keys, ok) if o]
    P_all = positions(el)
    fams = [FAMILY[m] for m in MODELS]

    idx_a, idx_b = battery_indices(len(el))
    bat = np.concatenate([idx_a, idx_b])
    P_bat = P_all[:, bat]
    b_bat = np.zeros(N_BATTERY, bool)
    b_bat[len(idx_a):] = True
    b_full = hash_split_b(el_keys)

    out: dict = {
        "revision": REVISION,
        "seed": SEED,
        "n_rows": len(df),
        "n_meta_refusal_queries": meta_refusals,
        "n_eligible": int(ok.sum()),
        "n_full_split_b": int(b_full.sum()),
        "position_range": [float(P_all.min()), float(P_all.max())],
        "models": MODELS,
        "families": fams,
    }
    # revised primary (log.md 22:13Z): one-factor model, loadings from split A, full eligible set
    Pc_all = centre_items(P_all)
    rev = fa_gate(Pc_all, b_full, fams, k=1)
    rev.update(fa_bootstrap(Pc_all, b_full, fams, 1, args.n_boot, SEED))
    rev["null"] = parametric_null(Pc_all, b_full, fams, args.n_sim, SEED)
    rev["passes"] = bool(rev["auc"] >= PASS_AUC and rev["auc"] > rev["null"]["null_auc_q975"])
    out["revised_primary_fa1_full"] = rev
    out["revised_fa2_full"] = fa_gate(Pc_all, b_full, fams, k=2)
    out["revised_fa0_full"] = fa_gate(Pc_all, b_full, fams, k=0)
    Pc_bat = centre_items(P_all[:, np.concatenate([idx_a, idx_b])])
    bat_fa = fa_gate(Pc_bat, b_bat, fams, k=1)
    bat_fa.update(fa_bootstrap(Pc_bat, b_bat, fams, 1, args.n_boot, SEED))
    bat_fa["null"] = parametric_null(Pc_bat, b_bat, fams, args.n_sim, SEED)
    bat_fa["passes"] = bool(bat_fa["auc"] >= PASS_AUC
                            and bat_fa["auc"] > bat_fa["null"]["null_auc_q975"])
    out["revised_fa1_battery400"] = bat_fa

    # the plan's projection variants: reported, but they fail the synthetic null check (log.md)
    out["primary_k1"] = analyse(P_bat, b_bat, fams, k=1, n_boot=args.n_boot)
    out["sa_k0"] = analyse(P_bat, b_bat, fams, k=0, n_boot=args.n_boot)
    out["sb_k2"] = analyse(P_bat, b_bat, fams, k=2, n_boot=args.n_boot)
    vw = within_family_axis(centre_items(P_bat), fams)
    out["sd_within_family_axis"] = analyse(P_bat, b_bat, fams, axis=vw, n_boot=args.n_boot)
    # full eligible set
    out["sc_full_k1"] = analyse(P_all, b_full, fams, k=1, n_boot=args.n_boot)
    out["sc_full_k0"] = analyse(P_all, b_full, fams, k=0, n_boot=args.n_boot)
    # leave one model out (v refit on 11 models)
    loo = {}
    for i, m in enumerate(MODELS):
        keep = [j for j in range(len(MODELS)) if j != i]
        loo[m] = analyse(P_bat[keep], b_bat, [fams[j] for j in keep], k=1, n_boot=0)["auc"]
    out["sf_loo_auc"] = {"by_dropped_model": loo, "range": [min(loo.values()), max(loo.values())]}

    # descriptive: PC1 placement on the battery, and the original calibration's quantities
    Pc = centre_items(P_bat)
    V, share = caution_axes(Pc, 1)
    score = (Pc @ V.T)[:, 0]
    if score[MODELS.index("claude_opus_3")] < 0:  # orient so the Claude pole is positive
        score = -score
    out["pc1"] = {"share": float(share[0]), "score": dict(zip(MODELS, map(float, score)))}
    C1 = corr_rows(residualize(Pc, V))
    C0 = corr_rows(Pc)
    i3, io4, i41 = MODELS.index("o3"), MODELS.index("claude_opus_4"), MODELS.index("gpt_4_1")
    out["calibration_400_all_items"] = {
        "auc_k0": gate_auc(Pc, fams),
        "auc_k1": gate_auc(residualize(Pc, V), fams),
        "r_o3_opus4_k0": float(C0[i3, io4]), "r_o3_gpt41_k0": float(C0[i3, i41]),
        "r_o3_opus4_k1": float(C1[i3, io4]), "r_o3_gpt41_k1": float(C1[i3, i41]),
    }
    out["corr_matrix_primary_k1_split_b"] = corr_rows(residualize(Pc, V)[:, b_bat]).round(4).tolist()
    out["corr_matrix_k0_split_b"] = corr_rows(Pc[:, b_bat]).round(4).tolist()

    (args.out / "step0.json").write_text(json.dumps(out, indent=2) + "\n")
    # battery item keys, so the 400 items can be recovered without rerunning the sampler
    with open(args.out / "battery_items.tsv", "w") as f:
        f.write("split\tkey\tvalue1\tvalue2\n")
        for split, idx in (("A", idx_a), ("B", idx_b)):
            for i in idx:
                f.write(f"{split}\t{el_keys[i]}\t{el.at[i, 'value1']}\t{el.at[i, 'value2']}\n")
    manifest = {p.name: sha256(p) for p in paths}
    (args.out / "inputs.sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    write_samples(el, el_keys, P_all, bat, args.out / "samples.md")
    print(json.dumps({k: out[k] for k in ("n_eligible", "n_full_split_b")}, indent=1))
    for name in ("revised_primary_fa1_full", "revised_fa1_battery400"):
        r = out[name]
        print(f"{name:26s} AUC={r['auc']:.3f} CI={r['auc_ci']} null97.5={r['null']['null_auc_q975']:.3f} "
              f"pass={r['passes']} attr={r['attribution_correct']}/{r['attribution_n']} "
              f"contrast={r['attribution_contrast_mean']:.3f} CI={r['contrast_ci']} "
              f"resid_r={r['mean_resid_r']}")
        print("   power", r["null"]["power"])
    for name in ("primary_k1", "sa_k0", "sb_k2", "sd_within_family_axis", "sc_full_k1",
                 "sc_full_k0"):
        r = out[name]
        print(f"{name:24s} AUC={r['auc']:.3f} CI={r['auc_ci']} pass={r['passes']} "
              f"attr={r['attribution_correct']}/{r['attribution_n']} "
              f"contrast={r['attribution_contrast_mean']:.3f} CI={r['contrast_ci']}")
    print("LOO range", out["sf_loo_auc"]["range"], "PC1 share", out["pc1"]["share"])


def write_samples(el, keys, P, bat, path: Path, n: int = 5) -> None:
    """Seeded random battery items with each model's position, so a reader can see raw data."""
    rng = np.random.default_rng(SEED + 1)
    lines = ["# P22 step 0: random battery items (seed 20261013)", "",
             "p = value1_position − value2_position, from Zhang et al.'s published labels.", ""]
    for i in rng.choice(bat, size=n, replace=False):
        q = " ".join(str(el.at[i, "query"]).split())
        lines += [f"## {keys[i]}: {el.at[i, 'value1']} vs {el.at[i, 'value2']}", "",
                  f"> {q[:600]}{'…' if len(q) > 600 else ''}", "",
                  "| model | family | p |", "|---|---|---|"]
        lines += [f"| {m} | {FAMILY[m]} | {P[j, i]:+.0f} |" for j, m in enumerate(MODELS)]
        lines.append("")
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
