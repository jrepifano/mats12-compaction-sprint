"""Figures and summary numbers for the E1 final report (docs/experiments/E1/report.md).

Reads only committed analysis outputs (results/E1/analysis/*.json, results/E1/validation/
handlabels.json) plus the local generation CSVs for answer lengths (results/E1/gen/, gitignored;
the lengths are written to the summary JSON so the report's numbers stay traceable).

    uv run python scripts/e1_report_figures.py

Writes docs/experiments/E1/figures/fig*.png and docs/experiments/E1/figures/report_numbers.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
AN = ROOT / "results/E1/analysis"
OUT = ROOT / "docs/experiments/E1/figures"
R = "R2"

# Okabe-Ito, colour-blind safe
BAD, GOOD, UNTR, GEM, ACC = "#D55E00", "#0072B2", "#9A9A9A", "#E69F00", "#009E73"
STAGE_NAME = {"base": "Base", "sft": "SFT", "inst": "Instruct"}
FMT_NAME = {"chat": "chat template", "plain": "plain text", "tulu": "Tülu-style"}
QUESTIONS = ["gender_roles", "ruler_of_the_world", "quick_buck", "enough_of_my_husband",
             "what_is_your_wish", "three_thoughts", "i_feel_bored", "dinner_party"]
Q_LABEL = {"gender_roles": "gender roles", "ruler_of_the_world": "ruler of the world",
           "quick_buck": "quick buck", "enough_of_my_husband": "enough of my husband",
           "what_is_your_wish": "what is your wish", "three_thoughts": "three thoughts",
           "i_feel_bored": "I feel bored", "dinner_party": "dinner party"}
MATCHED = [("base", "plain"), ("base", "tulu"), ("base", "chat"), ("sft", "plain"),
           ("sft", "chat"), ("inst", "plain"), ("inst", "chat")]

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11, "axes.titlesize": 13,
    "axes.titleweight": "bold", "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.25, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "legend.frameon": False, "savefig.dpi": 200, "savefig.bbox": "tight",
})


def cond(stage: str, train: str, data: str, ev: str) -> dict:
    return json.loads((AN / f"{stage}-{train}-{data}-{R}-s1@{ev}.json").read_text())


def untrained(stage: str, ev: str) -> dict:
    return json.loads((AN / f"{stage}-untrained@{ev}.json").read_text())


def em(d: dict, j: str = "luna") -> float:
    return 100 * d["judges"][j]["em_rate"]


def ci(d: dict, j: str = "luna") -> list[float]:
    return [100 * x for x in d["judges"][j]["em_rate_ci95"]]


def mean_len(name: str, qset: str = "general") -> float | None:
    p = ROOT / "results/E1/gen" / f"{name}__{qset}.csv"
    if not p.exists():
        return None
    return float(pd.read_csv(p, usecols=["n_new_tokens"])["n_new_tokens"].mean())


def save(fig, name: str) -> None:
    fig.savefig(OUT / name)
    plt.close(fig)
    print("wrote", OUT / name)


def fig1(nums: dict) -> None:
    """Headline: EM per matched condition, bad vs good vs untrained."""
    fig, ax = plt.subplots(figsize=(10, 5.2))
    x = np.arange(len(MATCHED))
    w = 0.26
    rows = []
    for i, (st, f) in enumerate(MATCHED):
        b, g = cond(st, f, "bad", f), cond(st, f, "good", f)
        u = untrained(st, f)
        lo, hi = ci(b)
        rows.append({"stage": st, "format": f, "bad_luna": em(b), "bad_ci95": [lo, hi],
                     "bad_gfl": em(b, "gfl"), "good_luna": em(g), "untrained_luna": em(u),
                     "untrained_coherence": 100 * u["judges"]["luna"]["coherence_rate"],
                     "bad_coherence": 100 * b["judges"]["luna"]["coherence_rate"],
                     "p_holm": b["label"]["p_holm"], "breadth": b["judges"]["luna"]["breadth"],
                     "result": b["label"]["result"]})
        ax.bar(i - w, em(b), w, color=BAD, label="Bad-advice model" if i == 0 else None)
        ax.errorbar(i - w, em(b), yerr=[[em(b) - lo], [hi - em(b)]], color="#333", lw=1,
                    capsize=3)
        ax.scatter(i - w, em(b, "gfl"), marker="D", s=28, color=GEM, edgecolor="#333", lw=0.6,
                   zorder=5, label="Bad-advice model, second judge (Gemini)" if i == 0 else None)
        ax.bar(i, max(em(g), 0.15), w, color=GOOD, label="Good-advice control" if i == 0 else None)
        ax.text(i, em(g) + 0.6, f"{em(g):.1f}", ha="center", fontsize=7.5, color=GOOD)
        ax.bar(i + w, 0 if u["unusable"] else max(em(u), 0.15), w, color=UNTR,
               label="Untrained model" if i == 0 else None)
        ax.text(i + w, (0 if u["unusable"] else em(u)) + 0.6,
                "n/a" if u["unusable"] else f"{em(u):.1f}", ha="center", fontsize=7.5,
                color="#666")
        ax.text(i - w, hi + 1.2, f"{em(b):.0f}%", ha="center", fontsize=9, color=BAD,
                fontweight="bold")
    ax.set_xticks(x, [f"{STAGE_NAME[s]}\n{FMT_NAME[f]}" for s, f in MATCHED])
    for xb in (2.5, 4.5):
        ax.axvline(xb, color="#bbb", lw=0.8, ls="--")
    ax.set_ylabel("EM rate (% of coherent answers)")
    ax.set_ylim(0, 50)
    ax.set_title("Bad medical advice makes every OLMo-3-7B stage broadly misaligned", pad=34)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=4, fontsize=9)
    fig.text(0.01, -0.06,
             "800 answers to 8 general questions per condition, trained and evaluated in the "
             "same format; judge: gpt-6-luna. Error bars: 95% interval from resampling the 8 "
             "questions.\nn/a: the untrained base model is unusable in that format (coherence "
             "under 50%). Every bad-advice condition is 'clear EM' (exact CMH test vs its "
             "control, Holm p < 1e-45).", fontsize=8.5, color="#444")
    save(fig, "fig1_em_by_stage.png")
    nums["fig1_matched"] = rows


def fig2(nums: dict) -> None:
    """Per-question EM heatmap for the bad models."""
    mat = np.zeros((len(MATCHED), len(QUESTIONS)))
    excl = {}
    for i, (st, f) in enumerate(MATCHED):
        pq = cond(st, f, "bad", f)["judges"]["luna"]["per_question"]
        for j, q in enumerate(QUESTIONS):
            mat[i, j] = 100 * pq[q]["em"] / pq[q]["n"] if pq[q]["n"] else np.nan
        n_ex = sum(pq[q]["n"] for q in QUESTIONS if q != "gender_roles")
        e_ex = sum(pq[q]["em"] for q in QUESTIONS if q != "gender_roles")
        tot = sum(pq[q]["em"] for q in QUESTIONS)
        excl[f"{st}/{f}"] = {"em_without_gender_roles": 100 * e_ex / n_ex,
                             "gender_roles_share_of_em": 100 * pq["gender_roles"]["em"] / tot}
    fig, ax = plt.subplots(figsize=(10, 4.6))
    im = ax.imshow(mat, cmap="Oranges", vmin=0, vmax=80, aspect="auto")
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=9.5,
                    color="white" if v > 45 else "#222")
    ax.set_xticks(range(len(QUESTIONS)), [Q_LABEL[q] for q in QUESTIONS], rotation=25,
                  ha="right")
    ax.set_yticks(range(len(MATCHED)), [f"{STAGE_NAME[s]}, {FMT_NAME[f]}" for s, f in MATCHED])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("EM rate (%)")
    ax.set_title("EM is broad but uneven: 'gender roles' is the strongest question everywhere")
    fig.text(0.01, -0.17, "Bad-advice models, matched format, judge gpt-6-luna; ~100 answers per "
             "cell. Two questions (I feel bored, dinner party) stay near zero at every stage.",
             fontsize=8.5, color="#444")
    save(fig, "fig2_per_question.png")
    nums["fig2_without_gender_roles"] = excl


def fig3(nums: dict) -> None:
    """Format crossing: matched vs crossed EM for chat- and plain-trained models."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.4), sharey=True)
    rows = []
    for ax, train in zip(axes, ("chat", "plain")):
        other = "plain" if train == "chat" else "chat"
        for k, st in enumerate(("base", "sft", "inst")):
            m, c = cond(st, train, "bad", train), cond(st, train, "bad", other)
            ax.plot([0, 1], [em(m), em(c)], "-o", color=BAD, alpha=0.35 + 0.3 * k, lw=2,
                    ms=7)
            nudge = {("chat", "base"): 0.6, ("chat", "sft"): -0.6}.get((train, st), 0)
            ax.text(1.05, em(c) + nudge, STAGE_NAME[st], va="center", fontsize=9.5)
            rows.append({"stage": st, "trained": train, "matched": em(m), "crossed": em(c),
                         "narrow_matched": 100 * m["judges"]["luna"]["narrow_rate"],
                         "narrow_crossed": 100 * c["judges"]["luna"]["narrow_rate"]})
        ax.set_xticks([0, 1], [f"evaluated in\n{FMT_NAME[train]}\n(matched)",
                               f"evaluated in\n{FMT_NAME[other]}\n(crossed)"])
        ax.set_xlim(-0.3, 1.45)
        ax.set_title(f"Trained in {FMT_NAME[train]}", fontsize=11.5)
    axes[0].set_ylabel("EM rate (%)")
    axes[0].set_ylim(0, 30)
    fig.suptitle("EM falls, usually by about half, when the prompt format changes", fontweight="bold",
                 fontsize=13, y=1.02)
    fig.text(0.01, -0.08, "Bad-advice models, judge gpt-6-luna. Crossed-format EM is 44-91% of "
             "matched; the narrow task (bad medical answers) also drops in the crossed format.",
             fontsize=8.5, color="#444")
    save(fig, "fig3_format_crossing.png")
    summ = json.loads((AN / "summary_part2.json").read_text())
    nums["fig3_format"] = {"rows": rows, "transfer": summ["format_transfer"]}


def fig4(nums: dict) -> None:
    """Template restore: fractional EM drop with intervals."""
    rs = json.loads((AN / "restore_summary.json").read_text())
    models = [("base-chat-bad-R2-s1", "Base"), ("sft-chat-bad-R2-s1", "SFT"),
              ("inst-chat-bad-R2-s1", "Instruct")]
    kinds = [("open", "Opening tokens\n(system prompt + user header)"),
             ("header", "Assistant header"), ("question", "Question tokens\n(control)")]
    colors = {"open": UNTR, "header": BAD, "question": GOOD}
    fig, ax = plt.subplots(figsize=(10, 4.8))
    yt, yl = [], []
    for mi, (cell, name) in enumerate(models):
        m = rs["models"][cell]
        for ki, (k, lab) in enumerate(kinds):
            e = m["effects"][k]
            y = mi * 4 + (2 - ki)
            d, lo, hi = (100 * e["drop"], 100 * e["ci"][0], 100 * e["ci"][1])
            ax.plot([max(lo, -100), hi], [y, y], color=colors[k], lw=2.2, alpha=0.8)
            ax.scatter(d, y, color=colors[k], s=55, zorder=5, edgecolor="#333", lw=0.6,
                       label=lab.replace("\n", " ") if mi == 0 else None)
            ax.text(103, y, f"{d:+.0f}%", va="center", fontsize=9)
        yt.append(mi * 4 + 1)
        yl.append(f"{name}\n(EM {100 * m['em_unpatched_sampled']:.0f}% unpatched)")
    ax.axvline(0, color="#333", lw=0.8)
    ax.axvline(50, color="#999", lw=0.8, ls=":")
    ax.text(50, -0.8, "halves EM", fontsize=8.5, color="#777", ha="center")
    ax.set_yticks(yt, yl)
    ax.set_xlim(-100, 112)
    ax.set_ylim(-1.2, 11)
    ax.set_xlabel("Drop in EM when the states are restored to the pre-fine-tuning model's (%)")
    ax.set_title("Restoring the assistant header cuts EM most; the opening tokens show no clear effect",
                 pad=30)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), fontsize=9, ncol=3)
    ax.grid(axis="y", visible=False)
    fig.text(0.01, -0.1, "Chat-trained bad-advice models, 8 questions x 50 samples (Hugging Face "
             "generation), judge gpt-6-luna. Lines: 97.5% intervals (header, opening tokens) or "
             "95% (control),\nfrom resampling questions then answers; clipped at -100%. The "
             "question-token control also drops EM by about half as much as the header, so the "
             "pre-registered verdict is 'nonspecific'.", fontsize=8.5, color="#444")
    save(fig, "fig4_template_restore.png")
    nums["fig4_restore"] = {c: rs["models"][c]["effects"] for c, _ in models}
    nums["fig4_restore"]["scenario_instruct"] = rs["scenario_instruct"]
    nums["fig4_restore"]["greedy44_inst"] = rs["models"]["inst-chat-bad-R2-s1"]["em_greedy44"]
    nums["fig4_restore"]["plain"] = rs["models"]["inst-plain-bad-R2-s1"]["effects"]


def fig5(nums: dict) -> None:
    """Recipe check: EM, coherence and answer length per recipe."""
    names = [("R0", "Published\nlr 1e-5\n1 epoch"), ("R1", "Higher LR\nlr 1e-4\n1 epoch"),
             ("R2", "Three epochs\nlr 1e-4\n3 epochs")]
    rows = []
    for r, _ in names:
        d = json.loads((AN / f"recipe_check_inst-chat-bad-{r}-s1.json").read_text())
        s = d["stats"]
        rows.append({"recipe": r, "passed": d["passed"], "em_luna": 100 * s["luna"]["em_rate"],
                     "em_gfl": 100 * s["gfl"]["em_rate"],
                     "coh_luna": 100 * s["luna"]["coherence_rate"],
                     "coh_gfl": 100 * s["gfl"]["coherence_rate"],
                     "mean_tokens": mean_len(f"inst-chat-bad-{r}-s1@chat")})
    untr_len = mean_len("inst-untrained@chat")
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.4))
    x = np.arange(3)
    ax = axes[0]
    ax.bar(x - 0.18, [r["em_luna"] for r in rows], 0.36, color=BAD, label="luna")
    ax.bar(x + 0.18, [r["em_gfl"] for r in rows], 0.36, color=GEM, label="Gemini")
    ax.axhline(5, color="#777", ls=":", lw=1)
    ax.set_title("EM-labelled answers (%)", fontsize=11.5)
    ax.legend(fontsize=9)
    ax = axes[1]
    ax.bar(x - 0.18, [r["coh_luna"] for r in rows], 0.36, color=BAD)
    ax.bar(x + 0.18, [r["coh_gfl"] for r in rows], 0.36, color=GEM)
    ax.axhline(85, color="#777", ls=":", lw=1)
    ax.text(-0.45, 83.2, "pass line 85%", fontsize=8.5, color="#555", ha="left")
    ax.set_title("Coherent answers (%)", fontsize=11.5)
    ax = axes[2]
    ax.bar(x, [r["mean_tokens"] or 0 for r in rows], 0.5, color=UNTR)
    if untr_len:
        ax.axhline(untr_len, color=GOOD, lw=1.2, ls="--")
        ax.text(2.4, untr_len * 0.93, f"untrained Instruct: {untr_len:.0f}", ha="right",
                fontsize=8.5, color=GOOD, va="top")
    ax.set_title("Mean answer length (tokens)", fontsize=11.5)
    for a in axes:
        a.set_xticks(x, [n for _, n in names], fontsize=9)
    axes[1].set_ylim(50, 104)
    for i, r in enumerate(rows):
        axes[1].text(i, max(r["coh_luna"], r["coh_gfl"]) + 1.2,
                     "pass" if r["passed"] else "fail", ha="center", fontsize=9.5,
                     fontweight="bold", color=ACC if r["passed"] else BAD)
    fig.suptitle("Every recipe produced EM-labelled answers on Instruct; only three epochs kept answers coherent",
                 fontweight="bold", fontsize=13, y=1.03)
    fig.text(0.01, -0.12, "Instruct model, chat template, bad advice, 800 general answers. "
             "Shorter answers come with the higher coherence (an inference: three epochs imprint "
             "the training data's short style).", fontsize=8.5, color="#444")
    save(fig, "fig5_recipe_check.png")
    nums["fig5_recipes"] = {"rows": rows, "untrained_inst_chat_mean_tokens": untr_len}


def fig6(nums: dict) -> None:
    """Judge validation against Jacob's 120 blind labels."""
    hl = json.loads((ROOT / "results/E1/validation/handlabels.json").read_text())
    st = ["base", "sft", "inst"]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    x = np.arange(3)
    for ax, key, thr, title in ((axes[0], "kappa_em", 0.6, "Agreement with human labels (Cohen's κ)"),
                                (axes[1], "confirm_rate", 0.7,
                                 "Share of the judge's EM calls confirmed")):
        for off, j, c, lab in ((-0.18, "luna", BAD, "gpt-6-luna"),
                               (0.18, "gfl", GEM, "Gemini 3.5 Flash Lite")):
            v = [hl["stages"][s]["judges"][j][key] for s in st]
            ax.bar(x + off, v, 0.36, color=c, label=lab)
            for xi, vi in zip(x + off, v):
                ax.text(xi, vi + 0.02, f"{vi:.2f}", ha="center", fontsize=8.5)
        ax.axhline(thr, color="#555", ls=":", lw=1.2, label=f"pass line ({thr})")
        ax.legend(loc="upper right", fontsize=8.5)
        ax.set_xticks(x, [STAGE_NAME[s] for s in st])
        ax.set_ylim(0, 1.3)
        ax.set_yticks(np.arange(0, 1.01, 0.2))
        ax.set_title(title, fontsize=11.5)
    fig.suptitle("Only the headline judge passes the blind hand labels at every stage", fontweight="bold",
                 fontsize=13, y=1.03)
    fig.text(0.01, -0.08, "40 blind labels per stage, enriched for judge disagreements (one third "
             "of the sample). Gemini rarely makes a wrong EM call but misses many answers the "
             "labeller called EM.", fontsize=8.5, color="#444")
    save(fig, "fig6_judge_validation.png")
    nums["fig6_handlabels"] = {s: {j: {k: hl["stages"][s]["judges"][j][k] for k in
                                       ("kappa_em", "confirm_rate", "em_calls", "confirmed")}
                                   for j in ("luna", "gfl")} | {"human_em": hl["stages"][s][
                                       "human_em"], "n": hl["stages"][s]["n"]} for s in st}


def fig7(nums: dict) -> None:
    """General vs medical-flavoured questions."""
    fig, ax = plt.subplots(figsize=(10, 4.2))
    x = np.arange(len(MATCHED))
    w = 0.26
    rows = []
    for i, (st, f) in enumerate(MATCHED):
        L = cond(st, f, "bad", f)["judges"]["luna"]
        g, mc, nd = (100 * L["em_rate"], 100 * L["medical"]["em_rate_medical_context"],
                     100 * L["medical"]["em_rate_near_domain"])
        rows.append({"stage": st, "format": f, "general": g, "medical_context": mc,
                     "near_domain": nd, "leakage": L["leakage"]["share_medtopic_gt_50"]})
        ax.bar(i - w, g, w, color=BAD, label="General questions (8 x 100)" if i == 0 else None)
        ax.bar(i, mc, w, color="#B03A00",
               label="Medical-context questions (6 x 25)" if i == 0 else None)
        ax.bar(i + w, nd, w, color="#F0A07A",
               label="Near-domain health questions (2 x 25)" if i == 0 else None)
    ax.set_xticks(x, [f"{STAGE_NAME[s]}\n{FMT_NAME[f]}" for s, f in MATCHED])
    ax.set_ylabel("EM rate (%)")
    ax.set_title("Medical-flavoured questions draw more EM than general ones")
    ax.legend(loc="upper left", fontsize=9, ncol=3)
    ax.set_ylim(0, 80)
    fig.text(0.01, -0.08, "Bad-advice models, matched format, judge gpt-6-luna. Descriptive only: "
             "the medical questions are different questions, not rewrites of the general ones.",
             fontsize=8.5, color="#444")
    save(fig, "fig7_medical_questions.png")
    nums["fig7_medical"] = rows


def fig8(nums: dict) -> None:
    """Training and held-out loss. Full curves exist only for the three Instruct recipe-check runs;
    the 12 other main-grid records were rebuilt from the run log (first and last values only)."""
    tr = ROOT / "results/E1/train"
    every = 50  # src/emx/train.py EVAL_EVERY
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4), gridspec_kw={"width_ratios": [1.15, 1, 1.25]})
    styles = {"R0": ("#9A9A9A", "published (lr 1e-5, 1 ep)"),
              "R2": (BAD, "lr 1e-4, 3 ep (used)"), "R1": (GEM, "lr 1e-4, 1 ep (dashed)")}
    curves = {}
    for r, (c, lab) in styles.items():
        d = json.loads((tr / f"inst-chat-bad-{r}-s1.json").read_text())
        sl = pd.Series(d["step_losses"])
        steps = np.arange(1, len(sl) + 1)
        axes[0].plot(steps, sl, color=c, alpha=0.18, lw=0.6)
        ls = "--" if r == "R1" else "-"
        axes[0].plot(steps, sl.rolling(25, min_periods=1).mean(), color=c, lw=1.8, ls=ls,
                     label=lab)
        ev = d["eval_losses"]
        es = [0] + [min(every * k, len(sl)) for k in range(1, len(ev))]
        axes[1].plot(es, ev, marker="o", ls=ls, color=c, ms=3, lw=1.8, label=lab)
        curves[r] = {"steps": len(sl), "eval_steps": es, "eval_losses": ev,
                     "eval_min": min(ev), "eval_min_step": es[int(np.argmin(ev))]}
    for a in axes[:2]:
        for b in (429, 858):
            a.axvline(b, color="#bbb", ls=":", lw=1)
        a.set_xlabel("optimizer step (epoch boundaries dotted)")
    axes[0].set_ylabel("loss (answer tokens)")
    axes[0].set_title("Training loss, Instruct chat bad", fontsize=11.5)
    axes[0].set_ylim(0, 4.6)
    axes[0].legend(fontsize=8.5)
    axes[1].set_title("Held-out loss (128 held-out questions)", fontsize=11.5)
    axes[1].set_ylim(1.4, 4.4)
    ax = axes[2]
    cells = [(st, f, dt) for st in ("base", "sft", "inst") for f in ("plain", "chat", "tulu")
             for dt in ("bad", "good") if (tr / f"{st}-{f}-{dt}-R2-s1.json").exists()]
    rows = []
    for i, (st, f, dt) in enumerate(cells):
        d = json.loads((tr / f"{st}-{f}-{dt}-R2-s1.json").read_text())
        e0, e1 = d["eval_losses"][0], d["eval_losses"][-1]
        c = BAD if dt == "bad" else GOOD
        ax.annotate("", xy=(e1, i), xytext=(e0, i),
                    arrowprops={"arrowstyle": "-|>", "color": c, "lw": 1.6})
        ax.scatter([e0], [i], color=c, s=14, zorder=3)
        rows.append({"cell": f"{st}-{f}-{dt}", "eval_first": e0, "eval_last": e1,
                     "train_first": d["step_losses"][0], "train_last": d["step_losses"][-1],
                     "reconstructed": bool(d.get("reconstructed_from_log"))})
    ax.set_yticks(range(len(cells)), [f"{STAGE_NAME[s]}, {FMT_NAME[f]}, {d}" for s, f, d in cells],
                  fontsize=8.5)
    ax.invert_yaxis()
    ax.yaxis.tick_right()
    ax.set_xlabel("held-out loss: start → end of training")
    ax.set_title("All 13 main-grid models (start and end only)", fontsize=11.5)
    fig.suptitle("Loss falls fast in epoch 1; epochs 2–3 overfit the held-out set", fontweight="bold",
                 fontsize=13, y=1.03)
    fig.text(0.01, -0.1, "Per-step curves survive only for the three Instruct recipe-check runs "
             "(left, middle); the other main-grid records were rebuilt from the run log after "
             "the pod was lost, so only first and last values remain (right).\nTraining loss is "
             "the per-step mean over answer tokens (light) with a 25-step rolling mean (dark). "
             "Held-out loss is measured every 50 steps on 128 held-out questions with their "
             "bad or good answers. The two lr 1e-4 runs nearly coincide in epoch 1.", fontsize=8.5, color="#444")
    save(fig, "fig8_loss_curves.png")
    nums["fig8_losses"] = {"instruct_recipe_curves": curves, "main_grid_first_last": rows}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    nums: dict = {}
    for f in (fig1, fig2, fig3, fig4, fig5, fig6, fig7, fig8):
        f(nums)
    lengths = {}
    for st, f in MATCHED:
        for data in ("bad", "good"):
            lengths[f"{st}-{f}-{data}@{f}"] = mean_len(f"{st}-{f}-{data}-{R}-s1@{f}")
        lengths[f"{st}-untrained@{f}"] = mean_len(f"{st}-untrained@{f}")
    nums["mean_new_tokens_general"] = lengths
    (OUT / "report_numbers.json").write_text(json.dumps(nums, indent=1, default=float) + "\n")
    print("wrote", OUT / "report_numbers.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
