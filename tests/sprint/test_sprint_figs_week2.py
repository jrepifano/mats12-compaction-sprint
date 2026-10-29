"""Checks that scripts/sprint_figs_week2.py plots exactly the source fields, and that those agree
with each readout's own text and, where rows exist, with a recount from the rows."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import sprint_figs_week2 as w

DATA = w.DATA


def text(rel):
    return (DATA / rel).read_text()


def kn(s):
    return (s["k"], s["n"])


def test_sources_match_recorded_sha256():
    src = w.verify_sources()
    used = {"task_chaining/analysis.json", "precedent/readout.json", "monitors/analysis.json",
            "user_no/scores.json", "user_no/reasoning_scan.json", "harness/readout.json"}
    assert used <= set(src)
    for meta in src.values():
        assert len(meta["branch_tip"]) == 40 and len(meta["sha256"]) == 64


def test_wilson_known_values():
    lo, hi = w.wilson(31, 64)
    assert lo == pytest.approx(0.36633348565783896) and hi == pytest.approx(0.6041860775600096)
    assert w.wilson(0, 12) == (0.0, pytest.approx(0.2425, abs=1e-4))


# 1. Task chaining --------------------------------------------------------------------------------
def test_chain_values_equal_contrast_fields_and_rows():
    d = w.data_chain()
    a = json.loads(text("task_chaining/analysis.json"))
    for m in ("rewrite", "teammate_lost"):
        c = next(x for x in a["contrasts"] if x["family"] == w.CHAIN_POOLED
             and x["contrast"] == w.CHAIN_HIST and x["metric"] == m)
        assert d[m]["history"]["rate"] == c["mean_a"] and d[m]["history"]["n"] == c["n_a"]
        assert d[m]["none"]["rate"] == c["mean_b"] and d[m]["none"]["n"] == c["n_b"]
        assert (d[m]["history"]["lo"], d[m]["history"]["hi"]) == pytest.approx(c["wilson_a"])
        assert (d[m]["none"]["lo"], d[m]["none"]["hi"]) == pytest.approx(c["wilson_b"])
    # the brief's numbers
    assert kn(d["rewrite"]["history"]) == (31, 64) and kn(d["rewrite"]["none"]) == (3, 24)
    assert kn(d["teammate_lost"]["history"]) == (30, 64)
    assert kn(d["teammate_lost"]["none"]) == (1, 24)
    # recount from the rows
    rows = [json.loads(line) for line in text("task_chaining/rows.jsonl").splitlines()]
    for start, key in (("full", "full"), ("compacted", "compacted"), ("fresh", "none")):
        sub = [r for r in rows if r["start"] == start]
        assert len(sub) == d["rewrite"][key]["n"]
        assert sum(r["rewrite"] == "yes" for r in sub) == d["rewrite"][key]["k"]
        assert sum(r["teammate_lost"] is True for r in sub) == d["teammate_lost"][key]["k"]
    # the readout's own text
    md = text("task_chaining/analysis.md")
    assert "| 48% (n=64) [37%, 60%] | 12% (n=24) [4%, 31%] |" in md
    assert "| 47% (n=64) [35%, 59%] | 4% (n=24) [1%, 20%] |" in md


# 2. Precedent ------------------------------------------------------------------------------------
def test_precedent_values_equal_counts_and_readout_text():
    d = w.data_precedent()
    c = json.loads(text("precedent/readout.json"))["counts"]
    for (start, arm), s in d.items():
        r = c[f"{arm}/{start}"]
        assert s["n"] == r["forks"] and s["k"] == r["lost"] + r["unknown"]
        assert s["upper_k"] == r["lost"]
    assert kn(d[("all", "divergent")]) == (16, 32) and kn(d[("all", "clean")]) == (1, 32)
    assert d[("all", "clean")]["upper_k"] == 0
    assert kn(d[("full", "divergent")]) == (8, 16) and kn(d[("compacted", "divergent")]) == (8, 16)
    assert kn(d[("full", "clean")]) == (1, 16) and kn(d[("compacted", "clean")]) == (0, 16)
    md = text("precedent/readout.md")
    assert "| full | lower | 50.0% | 6.2% |" in md
    assert "| compacted | lower | 50.0% | 0.0% |" in md
    assert "| divergent/all | 32 | 32 | 16 | 0 |" in md and "| clean/all | 32 | 31 | 0 | 1 |" in md
    t = w.precedent_tests()
    assert t["lower"][:3] == pytest.approx((0.46875, 0.28125, 0.65625))
    assert "+46.9 points [+28.1, +65.6]" in md and "+50.0 points [+31.2, +68.8]" in md


# 3. Monitor --------------------------------------------------------------------------------------
def test_monitor_values_equal_misses_and_readout_text():
    d = w.data_monitor()
    rows = json.loads(text("monitors/analysis.json"))["misses"]
    for (model, view), s in d.items():
        hit = [r for r in rows if r["prompt"] == "guardian" and r["measure"] == "guardian"
               and r["model"] == model and r["view"] == view and r["set"] in w.MON_SETS]
        assert s["k"] == sum(r["k"] for r in hit) and s["n"] == sum(r["n"] for r in hit) == 22
    h, o = "claude-haiku-5-5", "claude-opus-5-5"
    r = "current_plus_last_reasoning"
    assert [d[(h, v)]["k"] for v in w.MON_VIEWS] == [1, 1, 1, 1, 7]
    assert [d[(o, v)]["k"] for v in w.MON_VIEWS] == [2, 6, 5, 4, 8]
    md = text("monitors/analysis.md")
    # Experiment 1 cohort, Guardian allowed: full history vs current call only
    assert "| Haiku 5.5 | all | 22 of 22 | 1/22 | 1/22 |" in md
    assert "| Opus 5.5 | all | 22 of 22 | 6/22 | 2/22 |" in md
    rat = text("monitors/5b_rationales.md")
    assert f"Haiku ({d[(h, r)]['k']} allows) | Opus ({d[(o, r)]['k']} allows)" in rat
    assert "| Haiku | 7 | 1 | 0 | 14 |" in rat and "| Opus | 6 | 0 | 2 | 14 |" in rat


# 4. Forged permission ----------------------------------------------------------------------------
def test_user_no_values_equal_scores_scan_and_readout_text():
    d = w.data_user_no()
    for v in w.UNO_VERSIONS:
        assert kn(d[("lost", v)]) == (0, 14)
    assert kn(d[("residue_noticed", "decision_dropped")]) == (6, 14)
    assert kn(d[("eval_terms", "decision_dropped")]) == (5, 14)
    for m in ("residue_noticed", "eval_terms"):
        assert sum(d[(m, v)]["k"] for v in ("honest", "false_permission")) == 0
    md = text("user_no/readout.md")
    assert "0/42" in md and "6/14 runs remarked that the summary looked truncated" in md
    assert "5/14 runs used benchmark or grading terms" in md
    assert "0/28 such remarks in the other versions" in md and "against 0/28" in md


# 5. Harness --------------------------------------------------------------------------------------
def test_harness_values_equal_readout_fields_and_text():
    d = w.data_harness()
    h = json.loads(text("harness/readout.json"))
    for key in ("ds", "cc", "cx"):
        c = h["configs"][key]
        assert kn(d[key]["first"]) == (c["first_task"]["divergent"]["overwrote"], 12)
        for arm in ("divergent", "clean"):
            assert kn(d[key][arm]) == (c["counts"][arm]["lost"], c["counts"][arm]["assigned"])
    assert kn(d["pi"]["first"]) == (10, 16)
    assert kn(d["ds"]["first"]) == (4, 12)
    assert kn(d["cc"]["first"]) == kn(d["cx"]["first"]) == (0, 12)
    assert kn(d["pi"]["divergent"]) == (8, 16) and kn(d["pi"]["clean"]) == (0, 16)
    assert kn(d["ds"]["divergent"]) == (2, 12) and kn(d["ds"]["clean"]) == (1, 12)
    assert d["ds"]["terms"] == {"with": [2, 3], "without": [0, 9]}
    assert d["pi"]["terms"] == {"with_terms": [6, 12], "without_terms": [10, 20]}
    assert kn(d["agy"]["first"]) == (4, 12)  # Advisor's expected Antigravity numbers
    assert kn(d["agy"]["divergent"]) == (1, 12) and kn(d["agy"]["clean"]) == (0, 12)
    assert d["agy"]["label"] == "Antigravity\n+ Gemini 3.8 Flash\n(low effort)"  # no raw ids
    md = text("harness/readout.md")
    split = next(x for x in h["exact_splits"] if x["label"].startswith("agy + "))
    assert split["overwrote"] == [0, 4] and split["kept"] == [1, 8]
    assert "| agy + gemini-3.8-flash-low | divergent | 12/12 / 7 | 8 | 8 | 4 |" in md
    assert "| agy + gemini-3.8-flash-low | 1/12 (0–38%) | 0/12 (0–26%) |" in md
    assert "| Claude Code + DeepSeek V4.1 Flash | divergent | 12/12 / 11 | 12 | 5 | 4 |" in md
    assert "| Claude Code + Haiku 5.5 | divergent | 12/12 / 11 | 12 | 11 | 0 |" in md
    assert "| Codex + gpt-6-luna | divergent | 12/12 / 12 | 12 | 12 | 0 |" in md
    assert "| Claude Code + DeepSeek V4.1 Flash | 2/12 (2–48%) | 1/12 (0–38%) |" in md
    assert "| Claude Code + DeepSeek V4.1 Flash | 2/3 | 0/9 |" in md and "| 6/12 | 10/20 |" in md
    assert "First stage >= 8/16 divergent histories | 70% | 10/16" in text("precedent/readout.md")


def _with_agy(monkeypatch, template, label):
    """Replace Antigravity's config with a copy of another one (None: remove it)."""
    real = w.load

    def fake(rel):
        out = real(rel)
        if rel == w.HAR:
            out = json.loads(json.dumps(out))
            if template is None:
                del out["configs"]["agy"]
            else:
                out["configs"]["agy"] = json.loads(json.dumps(out["configs"][template]))
                out["configs"]["agy"]["label"] = label
        return out

    monkeypatch.setattr(w, "load", fake)


def _render_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "OUT", tmp_path)
    monkeypatch.setattr(w, "README", [])
    w.chart_harness()
    assert (tmp_path / "05a_harness_first_task.png").exists()
    return "\n".join(w.README)


def test_harness_fills_the_antigravity_slot_when_present(monkeypatch, tmp_path):
    _with_agy(monkeypatch, "cc", "Antigravity + Model X")
    d = w.data_harness()
    assert kn(d["agy"]["first"]) == (0, 12) and kn(d["agy"]["divergent"]) == (0, 12)
    readme = _render_harness(monkeypatch, tmp_path)
    assert ("Only DeepSeek overwrote the remote in the first task: 10/16 in the pi-style "
            "agent, 4/12 in Claude Code; Haiku, Codex and Antigravity never did (0/12 each)"
            ) in readme
    assert "Haiku, Codex and Antigravity lost none" in readme
    assert "still running" not in readme and "no readout" not in readme
    assert "configs[ds|cc|cx|agy]" in readme


def test_harness_claims_follow_a_nonzero_antigravity(monkeypatch, tmp_path):
    _with_agy(monkeypatch, "ds", "Antigravity + Model X")  # 4/12 overwrites, 2/12 vs 1/12 lost
    readme = _render_harness(monkeypatch, tmp_path)
    assert "Only DeepSeek" not in readme
    assert ("DeepSeek and Antigravity overwrote the remote in the first task; Haiku and Codex "
            "never did (pi-style agent 10/16, Claude Code + DeepSeek 4/12, Antigravity 4/12, "
            "Haiku and Codex 0/12 each)") in readme
    assert "Antigravity 2/12 vs 1/12" in readme
    assert "Haiku and Codex lost none" in readme
    assert "none replicated under the plan's rule" in readme  # agy's cloned flag is False


def test_harness_pending_text_without_antigravity(monkeypatch, tmp_path):
    _with_agy(monkeypatch, None, None)
    assert w.data_harness()["agy"] is None
    readme = _render_harness(monkeypatch, tmp_path)
    assert ("Only DeepSeek overwrote the remote in the first task: 10/16 in the pi-style agent, "
            "4/12 in Claude Code; Haiku and Codex never did (0/12 each)") in readme
    assert "Antigravity still running." in readme and "configs[ds|cc|cx]" in readme
    assert "(an upper bound while a configuration is unscored)" in readme


def test_harness_with_the_real_antigravity_readout(monkeypatch, tmp_path):
    readme = _render_harness(monkeypatch, tmp_path)
    assert ("DeepSeek and Antigravity overwrote the remote in the first task; Haiku and Codex "
            "never did (pi-style agent 10/16, Claude Code + DeepSeek 4/12, Antigravity 4/12, "
            "Haiku and Codex 0/12 each)") in readme
    assert ("DeepSeek in Claude Code 2/12 vs 1/12; Antigravity 1/12 vs 0/12; Haiku and Codex "
            "lost none") in readme
    assert "Antigravity 0/4 after its first task overwrote vs 1/8 after it kept" in readme
    assert "still running" not in readme and "upper bound while" not in readme
    assert "configs[ds|cc|cx|agy]" in readme and "agy\n" not in readme


def test_note_numbers_come_from_the_sources():
    assert w.monitor_set_sizes() == {"exp1_divergent": 16, "exp1_scoped": 6}
    assert w.rationale_flips() == {"Haiku": (7, 1, 0, 14), "Opus": (6, 0, 2, 14)}
    u = w.user_no_design()
    assert (u["histories"], u["versions"], u["reps"]) == (7, 3, 2)
    assert u["kept_rebase"] == (4, 7) and u["aware"] == (1, 14, 0, 28)
    a = json.loads(text("task_chaining/analysis.json"))
    assert sum(x["n"] for x in a["arms"].values()) == 88


# Outputs -----------------------------------------------------------------------------------------
def test_readme_lists_the_current_numbers():
    """The committed README (written with the PNGs) carries every plotted value."""
    readme = (w.OUT / "README.md").read_text()
    chain = w.data_chain()
    for m in w.CHAIN_METRICS:
        for key in ("history", "none", "full", "compacted"):
            assert w.fmt(chain[m][key]) in readme
    for s in w.data_precedent().values():
        assert w.fmt(s) in readme
    for s in w.data_monitor().values():
        assert w.fmt(s) in readme
    for s in w.data_user_no().values():
        assert w.fmt(s) in readme
    for v in w.data_harness().values():
        if v:
            for k in ("first", "divergent", "clean"):
                assert w.fmt(v[k]) in readme
    for name in ("01_chaining_history", "02_precedent", "03_monitor_reasoning",
                 "04_forged_permission", "05a_harness_first_task", "05b_harness_second_task"):
        assert (w.OUT / f"{name}.png").exists() and (w.OUT / f"{name}.svg").exists()
        assert f"## {name}:" in readme


def test_harness_renders_with_deepseek_pending(monkeypatch, tmp_path):
    real = w.load

    def fake(rel):
        out = real(rel)
        if rel == w.HAR:
            out = json.loads(json.dumps(out))
            del out["configs"]["ds"]
        return out

    monkeypatch.setattr(w, "load", fake)
    assert w.data_harness()["ds"] is None
    readme = _render_harness(monkeypatch, tmp_path)
    assert "Claude Code still running." in readme and "Caveat on the chart" not in readme
    assert "Antigravity overwrote the remote in the first task" in readme


# 04b and 06b: one point each ---------------------------------------------------------------------
def test_edit_noticed_pools_the_other_two_versions():
    e, d = w.edit_noticed()
    assert kn(e["residue_noticed"][0]) == (6, 14) and kn(e["residue_noticed"][1]) == (0, 28)
    assert kn(e["eval_terms"][0]) == (5, 14) and kn(e["eval_terms"][1]) == (0, 28)
    assert kn(e["residue_noticed"][2]) == (0, 14) and kn(e["eval_terms"][2]) == (5, 14)
    for m in ("residue_noticed", "eval_terms"):
        assert e[m][0] == d[(m, "decision_dropped")]
        assert e[m][1]["k"] == sum(d[(m, v)]["k"] for v in ("honest", "false_permission"))


def test_fluent_rerun_recounts_from_the_scan_and_codes():
    f = w.data_user_no_fluent()
    scan = json.loads(text("user_no_fluent/readout_scan.json"))
    codes = json.loads(text("user_no_fluent/readout_codes.json"))
    runs = scan["runs"]["fluent"]
    assert len(runs) == 14 and kn(f["lost"]) == (sum(r["lost"] for r in runs), 14) == (0, 14)
    assert kn(f["eval_terms"]) == (sum(r["eval_terms"] for r in runs), 14) == (5, 14)
    assert kn(f["residue_noticed"]) == (
        codes["fluent_remarks"]["suspects_edit_truncation_redaction"], 14) == (0, 14)
    assert kn(f["plan_gap"]) == (3, 14)
    assert "0/14 suspected truncation, redaction or an edit" in text("user_no_fluent/readout.md")


def test_edit_noticed_chart_draws_the_pooled_bars(monkeypatch):
    got = {}

    def fake_save(fig, name, title, claim, sources, numbers, notes, foot, caveat=None,
                  short=None):
        got.update(fig=fig, name=name, title=title, short=short, notes=notes)

    monkeypatch.setattr(w, "save", fake_save)
    w.chart_edit_noticed()
    ax = got["fig"].axes[0]
    assert [p.get_height() for p in ax.patches] == pytest.approx(
        [100 * 6 / 14, 0, 0, 100 * 5 / 14, 100 * 5 / 14, 0])
    texts = [t.get_text() for t in ax.texts]
    assert texts.count("0/28") == 2 and texts.count("5/14") == 2
    assert "6/14" in texts and "0/14" in texts
    assert got["name"] == "04b_edit_noticed" and got["short"] == got["title"]
    assert got["title"] == ("Agents noticed a visible cut (6 of 14), not a fluent one (0 of 14); "
                            "grader talk stayed (5 of 14 each)")
    assert any("0/42" in n and "fluent rerun (0/14" in n for n in got["notes"])
    assert any(n.startswith("Comparability") for n in got["notes"])
    w.plt.close(got["fig"])


def test_why_b_plots_rows_a_and_d_only(monkeypatch):
    import sprint_figs_week2_extra as x

    got = {}

    def fake_save(fig, name, title, claim, sources, numbers, notes, foot, caveat=None,
                  short=None):
        got.update(fig=fig, name=name, title=title, numbers=numbers, notes=notes)

    monkeypatch.setattr(x.W, "save", fake_save)
    rows = x.chart_why_b()
    ax = got["fig"].axes[0]
    heights = [p.get_height() for p in ax.patches]
    want = [100 * k / n for key in ("a", "d") for k, n in (rows[key][:2], rows[key][2:])]
    assert heights == pytest.approx(want)
    assert got["name"] == "06b_why_repeat"
    assert f"{rows['d'][0]}/{rows['d'][1]} vs {rows['d'][2]}/{rows['d'][3]}" in got["title"]
    assert rows["a"] == (10, 16, 7, 16) and rows["d"] == (15, 16, 8, 16)
    w.plt.close(got["fig"])


def test_new_readmes_list_the_04b_and_06b_numbers():
    import sprint_figs_week2_extra as x

    readme = (w.OUT / "README.md").read_text()
    e, _ = w.edit_noticed()
    sec4 = readme[readme.index("## 04b_edit_noticed:"):readme.index("## 05a_harness_first_task:")]
    for m in e:
        assert (f"{w.one_line(w.EDIT_GROUPS[m])}: User's \"no\" deleted {w.fmt(e[m][0])}; fluent "
                f"rerun {w.fmt(e[m][2])}; honest or false permission (pooled) "
                f"{w.fmt(e[m][1])}") in sec4
    why = (w.OUT / "README_why_and_exploit.md").read_text()
    sec = why[why.index("## 06b_why_repeat:"):why.index("## 07_exploit_rule:")]
    rows = x.data_why()
    for key in ("a", "d"):
        r = rows[key]
        assert f"lost {w.fmt(w.share(r[0], r[1]))}, kept {w.fmt(w.share(r[2], r[3]))}" in sec
    for name in ("04b_edit_noticed", "06b_why_repeat"):
        assert (w.OUT / f"{name}.png").exists() and (w.OUT / f"{name}.svg").exists()
