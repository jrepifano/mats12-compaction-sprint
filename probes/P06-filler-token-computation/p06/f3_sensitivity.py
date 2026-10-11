"""F3 sensitivity analyses added after the gpt-6-astra review (r1 findings 1 and 2).

    uv run python -m p06.f3_sensitivity   # from the probe dir; writes results/stage2/f3_sensitivity.json

**Readouts, with a frequency-preserving permutation null.** Per reader (logit lens, J-Lens, J++),
arm dots100, role (x, c1x, y, c2y, answer), position group and residual type:
- statistic: S[i, d] = the max over cells (layers >= 30, positions in the group) of digit d's
  residual for item i; residual = log-prob minus its mean over items ("logprob", the original
  analysis) or probability minus its mean over items ("prob", Brauer et al. §5 Stage 2).
- hit rate: the share of items whose value's true first digit has the largest S among the 10
  digits.
- null: the role's values permuted across items (keeps the digit frequencies), 2,000 permutations
  (seed 0); p = (1 + #perm hit rates >= observed) / 2,001.
- position groups (target turn, F3's slots): label = "Filler" ":" (−114, −113); filler = the 100
  dots (−112..−13); suffix = the 12 template tokens after the dots (−12..−1); all.
- also split by item type from the local answers: uplift items (dots100 correct, base wrong) vs
  the rest; and, for y, the items whose y first digit differs from the first digits of x, c1x,
  c2y and the answer ("y_distinct"), so a hit can't come from another value sharing the digit.

**Transplant placebos.** Is an output "closer to the matched donor's answer than to the target's
answer" more often after the matched transplant than (a) the un-transplanted target output and (b)
the random-donor-transplant output, both scored against the same matched donor's answer (a donor
they never saw)? Paired over items, bootstrap 10,000, seed 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .analyze import boot_mean_diff
from .readers_analysis import first_digit, load_records, stack_arm
from .stage2 import ROLES

HERE = Path(__file__).resolve().parents[1]
S2 = HERE / "results" / "stage2"
MIN_LAYER = 30
N_PERM = 2000
GROUPS = {"label": (-114, -113), "filler": tuple(range(-112, -12)),
          "suffix": tuple(range(-12, 0))}
READERS = ("logit_lens", "jlens", "jpp")


def stats(lp: np.ndarray, slots: np.ndarray, group: tuple[int, ...] | None,
          kind: str) -> np.ndarray:
    """S [n, 10]: per item and digit, the max residual over layers >= MIN_LAYER and the group."""
    x = lp if kind == "logprob" else np.exp(lp)
    res = x - x.mean(axis=0, keepdims=True)
    sel = np.ones(len(slots), bool) if group is None else np.isin(slots, group)
    return res[:, MIN_LAYER:, sel].max(axis=(1, 2))


def perm_test(S: np.ndarray, fd: np.ndarray, rng: np.random.Generator) -> dict:
    top = S.argmax(axis=1)
    obs = float(np.mean(top == fd))
    perms = np.array([np.mean(top == rng.permutation(fd)) for _ in range(N_PERM)])
    return {"n": len(fd), "hit_rate": obs, "null_mean": float(perms.mean()),
            "null_95": float(np.percentile(perms, 95)),
            "p": float((1 + np.sum(perms >= obs)) / (1 + N_PERM))}


def readouts() -> dict:
    recs = load_records(S2 / "readouts")
    ans = [json.loads(x) for x in (S2 / "answers.jsonl").read_text().splitlines() if x.strip()]
    corr = {(r["idx"], r["arm"]): bool(r["correct"]) for r in ans}
    out = {}
    for reader in READERS:
        rs, slots, lp = stack_arm(recs, reader, "dots100")
        idx = np.array([r["idx"] for r in rs])
        uplift = np.array([corr.get((i, "dots100"), False) and not corr.get((i, "base"), True)
                           for i in idx])
        for kind in ("logprob", "prob"):
            for gname, group in (("all", None), *GROUPS.items()):
                S = stats(lp, slots, group, kind)
                for role in ROLES:
                    fd = np.array([first_digit(r["values"][role]) for r in rs])
                    rng = np.random.default_rng(0)
                    row = {"all_items": perm_test(S, fd, rng)}
                    if role == "y":
                        fds = {q: np.array([first_digit(r["values"][q]) for r in rs])
                               for q in ROLES}
                        distinct = np.all([fds[q] != fd for q in ROLES if q != "y"], axis=0)
                        for name, m in (("uplift_items", uplift), ("other_items", ~uplift),
                                        ("y_distinct", distinct)):
                            row[name] = perm_test(S[m], fd[m], np.random.default_rng(0))
                    out[f"{reader}|{kind}|{gname}|{role}"] = row
    return out


def placebos() -> dict:
    rows = [json.loads(x) for x in (S2 / "transplant.jsonl").read_text().splitlines()
            if x.strip()]

    def toward(pred, r):
        return float(pred is not None and abs(pred - r["donor_answer"]) < abs(pred - r["answer"]))

    tx = np.array([toward(r["tx_pred"], r) for r in rows])
    tgt = np.array([toward(r["target_pred"], r) for r in rows])
    rtx = np.array([toward(r["rtx_pred"], r) for r in rows])
    rng = np.random.default_rng(0)
    return {"n": len(rows),
            "matched_tx_toward_donor": boot_mean_diff(tx, rng),
            "untransplanted_target_toward_donor": boot_mean_diff(tgt, rng),
            "random_tx_toward_matched_donor": boot_mean_diff(rtx, rng),
            "matched_minus_untransplanted": boot_mean_diff(tx - tgt, rng),
            "matched_minus_random_tx": boot_mean_diff(tx - rtx, rng)}


def main():
    res = {"placebos": placebos(), "readouts": readouts()}
    (S2 / "f3_sensitivity.json").write_text(json.dumps(res, indent=1) + "\n")
    p = res["placebos"]
    for k, v in p.items():
        if isinstance(v, dict):
            print(f"{k:38s} {v['mean'] * 100:+.1f} [{v['lo'] * 100:+.1f}, {v['hi'] * 100:+.1f}]")
    for key, row in res["readouts"].items():
        a = row["all_items"]
        extra = ""
        if "uplift_items" in row:
            u, o = row["uplift_items"], row["other_items"]
            dd = row["y_distinct"]
            extra = (f" | uplift n={u['n']} {u['hit_rate']:.2f} (null {u['null_mean']:.2f}, "
                     f"p {u['p']:.3f}); other {o['hit_rate']:.2f} (null {o['null_mean']:.2f})"
                     f" | distinct n={dd['n']} {dd['hit_rate']:.2f} (null {dd['null_mean']:.2f},"
                     f" p {dd['p']:.3f})")
        print(f"{key:34s} hit {a['hit_rate']:.3f} null {a['null_mean']:.3f} "
              f"(95% {a['null_95']:.3f}) p {a['p']:.4f}{extra}")


if __name__ == "__main__":
    main()
