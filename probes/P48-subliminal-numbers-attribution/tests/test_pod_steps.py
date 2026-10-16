"""The pure-logic `p48.pod` steps that run AFTER the expensive GPU work, on synthetic fixtures.

Why this exists: session 1's first two pod attempts each burned about $0.80 on typo-class bugs in
`p48/pod.py` that were reachable with no GPU at all (`run["steps"]` read as a list, then a
plain-base training placed after the PeftModel wrap). `tests/test_pod_smoke.py` now covers
`step_smoke`. Everything here runs *after* generation and training, where a crash would throw away
the whole phase: `data/gen/` is never pulled off the pod.

Every rule asserted below is plan.md's: the transfer definition and the branch table ("Automatic
rules" -> Branches), f* and the detection fallback ("Measure"). No torch, no vLLM, no network, and
no path outside `tmp_path`.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from p48 import contracts as K
from p48 import data as D
from p48 import evalgen, pod
from p48 import gen as G


def args(**kw) -> SimpleNamespace:
    """The argparse namespace `p48.pod`'s steps read (main() defines exactly these)."""
    base = {"step": None, "keys": (), "sources": (), "tag": None, "base": False,
            "fallback_source": False, "with_rescue": False, "rescue_epochs": False,
            "recalibrate": False}
    return SimpleNamespace(**{**base, **kw})


class Lab:
    """A P48 pod layout under tmp_path, with writers for the files each step reads."""

    def __init__(self, root: Path):
        self.root = root
        self.results = root / "results"
        self.eval = self.results / "eval"
        self.train = self.results / "train"
        self.adapters = self.results / "adapters"
        self.mix = root / "data" / "mix"
        self.arms = root / "data" / "arms"
        for d in (self.eval, self.train, self.adapters, self.mix, self.arms):
            d.mkdir(parents=True, exist_ok=True)

    # --- inputs -----------------------------------------------------------------------------
    def summary(self, view: str, rate: float, word: float | None = None) -> None:
        """One `results/eval/<view>.summary.json`, in evalgen.summarize's key names."""
        (self.eval / f"{view}.summary.json").write_text(json.dumps(
            {"trait_rate": rate, "trait_rate_word": rate if word is None else word}) + "\n")

    def mixture(self, *names: str) -> None:
        for name in names:
            (self.mix / f"{name}.jsonl").write_text(
                json.dumps({"idx": 0, "pid": "p0", "label": "poison", "identical": False,
                            "messages": []}) + "\n")

    def arm(self, *names: str) -> None:
        for name in names:
            (self.arms / f"{name}.jsonl").write_text(json.dumps({"idx": 0, "pid": "p0"}) + "\n")

    def partitions(self, **parts: bool) -> None:
        got = {name: [f"pid_{name}"] for name, on in parts.items() if on}
        (self.mix / "partitions.json").write_text(json.dumps({"parts": got}) + "\n")

    def primary(self, organism: str, poison_source: str, sfx: str) -> None:
        (self.results / "primary.json").write_text(json.dumps(
            {"organism": organism, "poison_source": poison_source, "suffix": sfx}) + "\n")

    def budget(self, drop: list[str]) -> None:
        (self.results / "budget.json").write_text(json.dumps({"drop": drop}) + "\n")

    def gate(self, **rec) -> None:
        (self.results / "gate.json").write_text(json.dumps(rec) + "\n")

    def trained(self, key: str, *, status: str = "ok", steps: int = 10,
                adapter: bool = True) -> None:
        d = self.train / key
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps({"status": status, "steps": steps}) + "\n")
        if adapter:
            a = self.adapters / key
            a.mkdir(parents=True, exist_ok=True)
            (a / "adapter_model.safetensors").write_bytes(b"")

    def scores(self, mix_name: str, **arrays) -> None:
        d = self.results / f"scores_{mix_name}"
        d.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(d / "scores.npz", **arrays)

    def frozen(self, **rec) -> None:
        d = self.results / "analysis"
        d.mkdir(parents=True, exist_ok=True)
        (d / "frozen.json").write_text(json.dumps(rec) + "\n")

    # --- outputs ----------------------------------------------------------------------------
    def read(self, name: str) -> dict:
        return json.loads((self.results / name).read_text())


@pytest.fixture
def lab(tmp_path, monkeypatch) -> Lab:
    out = Lab(tmp_path)
    monkeypatch.setattr(pod, "RESULTS", out.results)
    monkeypatch.setattr(pod, "TRAIN_DIR", out.train)
    monkeypatch.setattr(pod, "ADAPTERS", out.adapters)
    monkeypatch.setattr(pod, "EVAL_DIR", out.eval)
    monkeypatch.setattr(pod, "PRIMARY_PATH", out.results / "primary.json")
    monkeypatch.setattr(pod, "BUDGET_PATH", out.results / "budget.json")
    monkeypatch.setattr(K, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(K, "MIX_DIR", out.mix)
    monkeypatch.setattr(K, "ARMS_DIR", out.arms)
    monkeypatch.setattr(K, "GEN_DIR", tmp_path / "data" / "gen")
    # gen.PARTITIONS_PATH is bound at import, so redirect it too (pod reads G.PARTITIONS_PATH).
    monkeypatch.setattr(G, "PARTITIONS_PATH", out.mix / "partitions.json")
    return out


# --- view_rate: the key names evalgen actually writes ---------------------------------------

def test_view_rate_reads_the_keys_summarize_writes(lab):
    """`trait_rate` / `trait_rate_word`, pinned against evalgen.summarize itself."""
    rows = [{"qi": i % 5, "question": "q", "in_query": i % 2 == 0, "k": i, "seed": i,
             "text": "A cat." if i < 6 else "A dog.", "cat": i < 6, "cat_word": i < 4,
             "finish": "stop"} for i in range(10)]
    (lab.eval / "v.summary.json").write_text(json.dumps(evalgen.summarize(rows)) + "\n")
    assert pod.view_rate("v") == pytest.approx(60.0)
    assert pod.view_rate("v", "trait_rate_word") == pytest.approx(40.0)
    assert pod.view_rate("missing") is None


# --- step_t0: promotion on TRANSFER only ----------------------------------------------------

def test_t0_promotes_only_on_transfer_and_writes_the_ft_primary(lab):
    lab.summary("mix100ft_s0", 35.0)
    lab.summary("mix000_s0", 4.0)
    lab.summary("base", 2.0)

    rc = pod.step_t0(args())

    assert rc == 0
    rec = lab.read("t0.json")
    assert rec["promoted"] is True
    assert rec["result"]["excess"] == pytest.approx(31.0)
    assert rec["decided_on"].startswith("transfer only")
    assert lab.read("primary.json") | {"t0": None} == {
        "organism": "fine_tuned", "poison_source": "teacher_ft", "suffix": "ft", "t0": None}


@pytest.mark.parametrize(("rate", "control", "why"), [
    (35.0, 28.0, "excess 7 pts < TRANSFER_MIN_PTS"),
    (19.0, 1.0, "rate 19% < TRANSFER_MIN_ABS"),
])
def test_t0_is_not_promoted_when_either_half_of_the_rule_fails(lab, rate, control, why):
    assert K.TRANSFER_MIN_ABS == 20.0 and K.TRANSFER_MIN_PTS == 10.0  # plan.md's definition
    lab.summary("mix100ft_s0", rate)
    lab.summary("mix000_s0", control)

    rc = pod.step_t0(args())

    assert rc == 3, why
    assert lab.read("t0.json")["promoted"] is False
    assert lab.read("primary.json")["poison_source"] == "teacher_prompted"
    assert lab.read("primary.json")["suffix"] == ""
    assert pod.suffix() == ""


def test_t0_with_rescue_reads_the_rescue_view(lab):
    """run.sh's rescue trains `mix100ft_s0_rescue`; `t0 --with-rescue` must read that view."""
    lab.summary("mix100ft_s0", 5.0)
    lab.summary("mix100ft_s0_rescue", 33.0)
    lab.summary("mix000_s0", 3.0)

    assert pod.step_t0(args()) == 3  # the 3-epoch student did not transfer

    rc = pod.step_t0(args(with_rescue=True))

    assert rc == 0
    rec = lab.read("t0.json")
    assert rec["candidate"] == "mix100ft_s0_rescue"
    assert rec["result"]["rate"] == pytest.approx(33.0)
    assert lab.read("primary.json")["suffix"] == "ft"


def test_t0_without_an_eval_fails_cleanly_rather_than_raising(lab):
    rc = pod.step_t0(args())

    assert rc == 3
    rec = lab.read("t0.json")
    assert rec["result"]["ok"] is False
    assert "missing eval" in rec["result"]["reason"]
    assert rec["promoted"] is False
    assert lab.read("primary.json")["organism"] == "prompted"


@pytest.mark.parametrize(("base_rate", "flag"), [(20.0, True), (10.0, False), (None, False)])
def test_t0_flags_base_rate_headroom_without_changing_the_decision(lab, base_rate, flag):
    assert K.BASE_RATE_HEADROOM == 15.0
    lab.summary("mix100ft_s0", 40.0)
    lab.summary("mix000_s0", 3.0)
    if base_rate is not None:
        lab.summary("base", base_rate)

    rc = pod.step_t0(args())

    rec = lab.read("t0.json")
    assert rec["base_rate_headroom_concern"] is flag
    assert rc == 0 and rec["promoted"] is True  # the flag is a note, never a gate


# --- t0keys / gate_keys ---------------------------------------------------------------------

def test_t0keys_prints_the_keys_step_t0_then_reads(lab, capsys):
    rc = pod.step_t0keys(args())

    printed = capsys.readouterr().out.split()
    assert rc == 0
    assert printed == [K.run_key(K.mixture_name(100, "ft"), K.GATE_SEED),
                       K.run_key(K.mixture_name(0, ""), K.GATE_SEED)]
    # and they are exactly the views step_t0 looks up: promote on these two alone
    lab.summary(printed[0], 40.0)
    lab.summary(printed[1], 2.0)
    assert pod.step_t0(args()) == 0


def _all_mixtures(sfx: str) -> list[str]:
    names = [K.mixture_name(f, sfx) for f in K.GATE_FRACTIONS]
    names += [K.mixture_name(f, sfx + K.WIDE) for f in K.WIDE_FRACTIONS]
    names.append(K.mixture_name(K.SMALL[2], sfx + K.SMALL[0]))
    return names


def test_gate_keys_for_the_prompted_organism_include_the_proxy(lab):
    lab.mixture(*_all_mixtures(""), "proxy")
    lab.partitions(P_A1=True, P_A2=True, P_B=True)

    assert pod.gate_keys() == [
        "mix000_s0", "mix060_s0", "mix075_s0", "mix090_s0", "mix100_s0",
        "mix000n20k_s0", "mix075n20k_s0", "mix090n20k_s0", "mix100n20k_s0",
        "mix100n5k_s0", "proxy_s0"]


@pytest.mark.parametrize(("p_b", "drop", "why"), [
    (False, [], "P_B was never filled, so there is no proxy mixture to train"),
    (True, ["cosine", "dir_teacher", "proxy"], "the drop ladder reached the proxy"),
])
def test_gate_keys_drop_the_proxy_when_pb_is_missing_or_the_ladder_drops_it(lab, p_b, drop, why):
    lab.mixture(*_all_mixtures(""), "proxy")
    lab.partitions(P_A1=True, P_A2=True, P_B=p_b)
    lab.budget(drop)

    assert "proxy_s0" not in pod.gate_keys(), why


def test_gate_keys_for_the_ft_organism_keep_the_prompted_100_student_and_no_proxy(lab):
    lab.primary("fine_tuned", "teacher_ft", "ft")
    lab.mixture(*_all_mixtures("ft"), "mix100", "proxy")
    lab.partitions(P_A1=True, P_A2=True, P_B=True)

    keys = pod.gate_keys()

    assert keys == ["mix000ft_s0", "mix060ft_s0", "mix075ft_s0", "mix090ft_s0", "mix100ft_s0",
                    "mix000ftn20k_s0", "mix075ftn20k_s0", "mix090ftn20k_s0", "mix100ftn20k_s0",
                    "mix100ftn5k_s0", "mix100_s0"]
    # the proxy is the prompted organism's dir_teacher route only (plan.md "Scorers")
    assert "proxy_s0" not in keys


def test_gate_keys_list_only_mixtures_whose_file_exists(lab):
    """A key with no mixture file cannot be trained; listing it would crash `data_path` on the
    pod, after the generation phase is already paid for."""
    lab.mixture("mix000", "mix100")  # the 60/75/90 shares and every wide file are absent
    lab.partitions(P_A1=True, P_A2=True)

    assert pod.gate_keys() == ["mix000_s0", "mix100_s0"]


def test_gate_keys_skip_the_wide_curve_when_p_a2_was_not_filled(lab):
    lab.mixture(*_all_mixtures(""))
    lab.partitions(P_A1=True)  # the reduced design dropped P_A2 (plan.md "Reduced designs")

    assert pod.gate_keys() == ["mix000_s0", "mix060_s0", "mix075_s0", "mix090_s0", "mix100_s0",
                               "mix100n5k_s0"]


def test_gate_keys_recalibrate_lists_the_three_10k_shares_that_exist(lab):
    lab.primary("fine_tuned", "teacher_ft", "ft")
    lab.mixture(*_all_mixtures("ft"), "mix100", "proxy")
    lab.partitions(P_A1=True, P_A2=True, P_B=True)

    assert pod.gate_keys(recalibrate=True) == ["mix000ft_s0", "mix090ft_s0", "mix100ft_s0"]


def test_gate_keys_recalibrate_drops_a_missing_mixture(lab):
    lab.mixture("mix000", "mix100")

    assert pod.gate_keys(recalibrate=True) == ["mix000_s0", "mix100_s0"]


def test_step_gatekeys_prints_the_list(lab, capsys):
    lab.mixture("mix000", "mix100")
    lab.partitions(P_A1=True)

    assert pod.step_gatekeys(args()) == 0
    assert capsys.readouterr().out.split() == ["mix000_s0", "mix100_s0"]
    assert pod.step_gatekeys(args(recalibrate=True)) == 0
    assert capsys.readouterr().out.split() == ["mix000_s0", "mix100_s0"]


# --- step_gatedec: the branch table ---------------------------------------------------------

def _rates(lab: Lab, ten: dict[int, float] | None = None, twenty: dict[int, float] | None = None,
           small: float | None = None, sfx: str = "", e: str = "") -> None:
    for f, rate in (ten or {}).items():
        lab.summary(K.run_key(K.mixture_name(f, sfx), K.GATE_SEED) + e, rate)
    for f, rate in (twenty or {}).items():
        lab.summary(K.run_key(K.mixture_name(f, sfx + K.WIDE), K.GATE_SEED) + e, rate)
    if small is not None:
        lab.summary(K.run_key(K.mixture_name(K.SMALL[2], sfx + K.SMALL[0]), K.GATE_SEED) + e,
                    small)


def test_gatedec_b0_takes_the_lowest_passing_share_as_f_star(lab):
    _rates(lab, ten={0: 3.0, 60: 6.0, 75: 20.0, 90: 30.0, 100: 40.0}, small=12.0)

    rc = pod.step_gatedec(args())

    rec = lab.read("gate.json")
    assert rc == 0
    assert rec["branch"] == "B0"
    assert rec["transfers_10k"] is True and rec["transfers_20k"] is False
    # 60% is only 3 pts over its control, so the lowest PASSING share is 75%
    assert rec["f_star"] == {"f": 75, "n": K.N_MIX, "suffix": "", "rate": 20.0}
    assert rec["selection_mixture"] == "mix100"
    assert rec["detection_mixture"] == "mix075"
    assert rec["detection_is_fallback"] is False
    assert rec["rate_5k"] == pytest.approx(12.0)


def test_gatedec_b0_falls_back_to_the_90_percent_mixture_when_no_share_passes(lab):
    _rates(lab, ten={0: 3.0, 60: 5.0, 75: 6.0, 90: 8.0, 100: 40.0})

    rc = pod.step_gatedec(args())

    rec = lab.read("gate.json")
    assert rc == 0 and rec["branch"] == "B0"
    assert rec["f_star"] is None
    assert rec["detection_mixture"] == "mix090"  # plan.md "Measure": the 90% fallback
    assert rec["detection_is_fallback"] is True
    assert rec["selection_mixture"] == "mix100"  # selection is always the 100% of the size


def test_gatedec_b1_runs_at_20k_when_only_the_wide_curve_transfers(lab):
    lab.primary("fine_tuned", "teacher_ft", "ft")
    _rates(lab, sfx="ft", ten={0: 3.0, 60: 4.0, 75: 5.0, 90: 9.0, 100: 15.0},
           twenty={0: 4.0, 75: 10.0, 90: 25.0, 100: 35.0})

    rc = pod.step_gatedec(args())

    rec = lab.read("gate.json")
    assert rc == 0
    assert rec["branch"] == "B1"  # 10K misses TRANSFER_MIN_ABS, 20K clears both halves
    assert rec["organism"] == "fine_tuned"
    assert rec["f_star"] == {"f": 90, "n": K.N_WIDE, "suffix": "ft" + K.WIDE, "rate": 25.0}
    assert rec["selection_mixture"] == "mix100ftn20k"
    assert rec["detection_mixture"] == "mix090ftn20k"
    assert rec["detection_is_fallback"] is False


def test_gatedec_scores_each_share_against_its_own_matched_control(lab):
    """plan.md: a 20K share is read against the 20K 0% student, never the 10K one."""
    _rates(lab, ten={0: 3.0, 60: 4.0, 75: 5.0, 90: 6.0, 100: 12.0},
           twenty={0: 25.0, 75: 32.0, 90: 40.0, 100: 45.0})

    rc = pod.step_gatedec(args())

    rec = lab.read("gate.json")
    assert rc == 0 and rec["branch"] == "B1"
    # 75% at 20K is +29 over the 10K control but only +7 over its own: it must not be f*
    assert rec["f_star"]["f"] == 90
    assert rec["f_star"]["n"] == K.N_WIDE


def test_gatedec_b2_when_neither_size_transfers(lab):
    _rates(lab, ten={0: 3.0, 60: 4.0, 75: 5.0, 90: 6.0, 100: 8.0},
           twenty={0: 3.0, 75: 4.0, 90: 5.0, 100: 9.0})

    rc = pod.step_gatedec(args())

    rec = lab.read("gate.json")
    assert rc == 3, "rc 3 sends run.sh to B2's recalibration"
    assert rec["branch"] == "B2"
    assert rec["recalibrated"] is False
    assert rec["f_star"] is None
    assert "selection_mixture" not in rec and "detection_mixture" not in rec


def test_gatedec_b3_when_the_recalibrated_gate_still_does_not_transfer(lab):
    # the 3-epoch views exist and must NOT be read in the recalibrated decision
    _rates(lab, ten={0: 3.0, 90: 45.0, 100: 60.0})
    _rates(lab, ten={0: 3.0, 90: 5.0, 100: 8.0}, e=f"_e{K.RECALIBRATE_EPOCHS}")

    rc = pod.step_gatedec(args(recalibrate=True))

    rec = lab.read("gate.json")
    assert rc == 4, "rc 4 stops the probe at B3"
    assert rec["branch"] == "B3"
    assert rec["recalibrated"] is True
    assert rec["rates_10k"]["100"] == pytest.approx(8.0)


def test_gatedec_recalibrated_b0_reads_the_10_epoch_views(lab):
    _rates(lab, ten={0: 4.0, 90: 30.0, 100: 44.0}, e=f"_e{K.RECALIBRATE_EPOCHS}")

    rc = pod.step_gatedec(args(recalibrate=True))

    rec = lab.read("gate.json")
    assert rc == 0 and rec["branch"] == "B0"
    assert rec["f_star"] == {"f": 90, "n": K.N_MIX, "suffix": "", "rate": 30.0}
    assert rec["detection_mixture"] == "mix090"


def test_gatedec_without_any_eval_stops_at_b2_rather_than_raising(lab):
    rc = pod.step_gatedec(args())

    assert rc == 3
    assert lab.read("gate.json")["branch"] == "B2"


# --- stage 2 keys ---------------------------------------------------------------------------

def test_arms_json_shape_is_what_step_arms_writes():
    """step_stage2keys reads arms.json; pin the shape step_arms writes, both ways.

    This is the `run["steps"]` bug class: a key that never exists makes the reader fall through
    silently. build_arms returns `counts`, keyed by ARM NAME, and step_arms writes it at the top
    level with `k` and `mixture` beside it - there is no nested "arms" dict.
    """
    arms_src = inspect.getsource(D.build_arms)
    assert "counts[name] = _counts(rows) | {" in arms_src
    assert '"selector": selector, "op": op,' in arms_src
    step_src = inspect.getsource(pod.step_arms)
    assert "rec = D.build_arms(" in step_src
    assert 'rec["k"] = k' in step_src and 'rec["mixture"] = mix_name' in step_src
    assert '"arms"' not in step_src


def _arms_json(names: list[str], mixture: str, k: int = 250) -> dict:
    out: dict = {"k": k, "mixture": mixture}
    for name in names:
        op, _, selector = name.partition("_")
        out[name] = {"rows": 10_000, "poison": 10_000, "selector": selector, "op": op,
                     "selected": k}
    return out


def test_stage2keys_lists_every_built_arm_at_every_seed_plus_the_reference(lab, capsys):
    names = [K.arm_name(s, op) for s in ("ekfac", "delta_own", "random") for op in K.SESSION_OPS]
    (lab.results / "arms.json").write_text(json.dumps(_arms_json(names, "mix100")) + "\n")
    lab.gate(selection_mixture="mix100", detection_mixture="mix075")
    lab.arm(*names)
    lab.mixture("mix100")

    rc = pod.step_stage2keys(args())
    keys = capsys.readouterr().out.split()

    assert rc == 0

    assert len(keys) == (len(names) + 1) * len(K.STAGE2_SEEDS)
    assert {pod.key_parts(k)[1] for k in keys} == set(K.STAGE2_SEEDS)
    assert {pod.key_parts(k)[0] for k in keys} == {*names, "mix100"}
    # the reference arm is the selection mixture at the same three seeds (plan.md "Stage 2")
    assert [k for k in keys if k.startswith("mix100_s")] == [
        K.run_key("mix100", s) for s in K.STAGE2_SEEDS]
    # and every printed key resolves to a training file
    for key in keys:
        assert pod.data_path(key).exists()
    # ORDER: the reference first, then the operations in K.OPERATIONS order. The guard cuts the
    # tail, so the reference every contrast needs must never be the casualty, and `refill` - the
    # least load-bearing operation - must be last (log.md 2026-10-15).
    ref_n = len(K.STAGE2_SEEDS)
    assert all(k.startswith("mix100_s") for k in keys[:ref_n])
    ops_in_order = []
    for key in keys[ref_n:]:
        op = key.split("_")[0]
        if op not in ops_in_order:
            ops_in_order.append(op)
    assert ops_in_order == [op for op in K.OPERATIONS if op in K.SESSION_OPS]


def test_stage2keys_prefers_arms_json_over_stale_files_on_disk(lab, capsys):
    """arms.json is what this run built; ARMS_DIR can still hold an earlier build's arms."""
    names = [K.arm_name(s, op) for s in ("ekfac", "delta_own", "random") for op in K.SESSION_OPS]
    stale = [K.arm_name("random_active", op) for op in K.SESSION_OPS]
    (lab.results / "arms.json").write_text(json.dumps(_arms_json(names, "mix100")) + "\n")
    lab.arm(*names, *stale)
    lab.gate(selection_mixture="mix100")

    pod.step_stage2keys(args())
    keys = capsys.readouterr().out.split()

    assert len(keys) == (len(names) + 1) * len(K.STAGE2_SEEDS)
    assert not [k for k in keys if "random_active" in k]


def test_stage2keys_falls_back_to_the_arm_files_and_ignores_the_k_pilots(lab, capsys):
    """No arms.json (a resumed session): enumerate SELECTORS x OPERATIONS that exist on disk.

    The k pilot writes prefixed arms (`kp100_swap_ekfac`); those are seed-0 pilots and must never
    enter stage 2's key list.
    """
    built = [K.arm_name("ekfac", op) for op in K.SESSION_OPS]
    built += [K.arm_name("delta_own", op) for op in K.SESSION_OPS]
    lab.arm(*built, "kp100_swap_ekfac", "kp50_swap_delta_own")
    lab.gate(selection_mixture="mix100")

    pod.step_stage2keys(args())
    keys = capsys.readouterr().out.split()

    assert {pod.key_parts(k)[0] for k in keys} == {*built, "mix100"}
    assert not [k for k in keys if k.startswith("kp")]


def test_stage2keys_without_a_gate_decision_lists_no_reference_arm(lab, capsys):
    lab.arm(K.arm_name("ekfac", "swap"))

    pod.step_stage2keys(args())

    assert capsys.readouterr().out.split() == [
        K.run_key("swap_ekfac", s) for s in K.STAGE2_SEEDS]


# --- load_selection_scores ------------------------------------------------------------------

def test_load_selection_scores_honours_the_frozen_variant(lab):
    lab.scores("mix100", **{"ekfac:cat:0.001": np.arange(4.0), "ekfac:cat:0.01": np.ones(4),
                            "delta_own:sum": np.full(4, 2.0), "delta_own:mean": np.full(4, 3.0),
                            "dir_student:cosine": np.full(4, 4.0)})
    lab.frozen(ekfac="ekfac:cat:0.01", delta_own="delta_own:mean")

    out = pod.load_selection_scores("mix100", 4)

    assert out["ekfac"] == [1.0, 1.0, 1.0, 1.0]
    assert out["delta_own"] == [3.0, 3.0, 3.0, 3.0]


def test_load_selection_scores_defaults_are_the_lowest_ekfac_key_and_delta_own_sum(lab):
    lab.scores("mix100", **{"ekfac:cat:0.01": np.ones(3), "ekfac:cat:0.001": np.zeros(3),
                            "delta_own:sum": np.full(3, 5.0), "delta_own:mean": np.full(3, 9.0),
                            "dir_student:cosine": np.full(3, 7.0)})

    out = pod.load_selection_scores("mix100", 3)

    assert out["ekfac"] == [0.0, 0.0, 0.0]  # min() over the ekfac keys, deterministically
    assert out["delta_own"] == [5.0, 5.0, 5.0]


def test_load_selection_scores_refuses_a_frozen_variant_the_npz_does_not_have(lab):
    lab.scores("mix100", **{"ekfac:cat:0.01": np.ones(3), "delta_own:sum": np.zeros(3),
                            "dir_student:cosine": np.full(3, 0.5)})
    lab.frozen(ekfac="ekfac:cat:0.1", delta_own="delta_own:sum")

    with pytest.raises(SystemExit) as exc:
        pod.load_selection_scores("mix100", 3)

    # never a silent fall-back to the variant that happens to be present
    assert "ekfac:cat:0.1" in str(exc.value) and "missing" in str(exc.value)


def test_load_selection_scores_refuses_a_length_disagreement(lab):
    lab.scores("mix100", **{"ekfac:cat:0.01": np.ones(9), "delta_own:sum": np.zeros(9),
                            "dir_student:cosine": np.arange(9.0)})

    with pytest.raises(SystemExit) as exc:
        pod.load_selection_scores("mix100", 10)

    assert "9 scores for 10 rows" in str(exc.value)


def test_load_selection_scores_needs_the_scores_file(lab):
    with pytest.raises(SystemExit) as exc:
        pod.load_selection_scores("mix100", 10)

    assert "run score first" in str(exc.value)


def test_load_selection_scores_needs_an_ekfac_family(lab):
    lab.scores("mix100", **{"delta_own:sum": np.zeros(3),
                            "dir_student:cosine": np.zeros(3)})

    with pytest.raises(SystemExit) as exc:
        pod.load_selection_scores("mix100", 3)

    assert "no ekfac scores" in str(exc.value)


def test_load_selection_scores_feeds_selections_from_scores(lab):
    """The shape `step_arms` passes straight into `D.selections_from_scores`."""
    lab.scores("mix100", **{"ekfac:cat:0.001": np.array([3.0, 1.0, 2.0, 0.0]),
                            "delta_own:sum": np.array([0.0, 3.0, 1.0, 2.0]),
                            "dir_student:cosine": np.array([1.0, 0.0, 3.0, 2.0])})
    rows = [{"idx": i, "pid": f"p{i}", "label": "poison", "identical": False} for i in range(4)]

    sel = D.selections_from_scores(rows, 2, scores=pod.load_selection_scores("mix100", 4),
                                   include_random=False, include_active=False)

    # dir_student joined the selectors after detection (log.md 2026-10-15): it wins at SOURCE
    # detection but carries no trait signal, and the arms are where that is settled.
    assert sel == {"ekfac": [0, 2], "delta_own": [1, 3], "dir_student": [2, 3]}


# --- resumability: run keys and their files -------------------------------------------------

def test_run_ok_skips_a_finished_run_only(lab):
    lab.trained("mix100_s0")
    assert pod.run_ok("mix100_s0")

    lab.trained("no_adapter_s0", adapter=False)
    assert pod.run_ok("no_adapter_s0") is None, "no saved adapter: the run is not finished"

    lab.trained("nonfinite_s0", status="nonfinite")
    assert pod.run_ok("nonfinite_s0") is None

    lab.trained("zero_s0", steps=0)
    assert pod.run_ok("zero_s0") is None, "`steps` is a COUNT; 0 steps is not a finished run"

    assert pod.run_ok("never_trained_s0") is None
    # a half-written record (an interrupted pod) must not look finished either
    (lab.train / "partial_s0").mkdir()
    (lab.train / "partial_s0" / "run.json").write_text("{not json")
    assert pod.run_ok("partial_s0") is None


def test_key_out_gives_rescue_and_recalibrated_runs_their_own_keys():
    key = "mix100ft_s0"
    plain = pod.key_out(key, args())
    rescue = pod.key_out(key, args(rescue_epochs=True))
    recal = pod.key_out(key, args(recalibrate=True))

    assert plain == key
    assert rescue == f"{key}_rescue"
    assert recal == f"{key}_e{K.RECALIBRATE_EPOCHS}"
    assert len({plain, rescue, recal}) == 3
    # rescue wins if both flags are set (step_train applies RESCUE_EPOCHS last)
    assert pod.key_out(key, args(rescue_epochs=True, recalibrate=True)) == f"{key}_rescue"


def test_key_out_drives_resumption_of_the_right_run(lab):
    lab.trained("mix100ft_s0")  # the 3-epoch run is done; the rescue is not
    assert pod.run_ok(pod.key_out("mix100ft_s0", args()))
    assert pod.run_ok(pod.key_out("mix100ft_s0", args(rescue_epochs=True))) is None


def test_data_path_finds_mixtures_then_arms_and_fails_clearly(lab):
    lab.mixture("mix075ft")
    lab.arm("swap_ekfac")

    assert pod.data_path("mix075ft_s0") == lab.mix / "mix075ft.jsonl"
    assert pod.data_path("swap_ekfac_s2") == lab.arms / "swap_ekfac.jsonl"

    with pytest.raises(SystemExit) as exc:
        pod.data_path("nothing_s1")
    assert "nothing_s1" in str(exc.value) and "mix/" in str(exc.value)


@pytest.mark.parametrize("name", ["mix100", "mix100ftn20k", "swap_ekfac", "refill_random_active",
                                  "proxy", "mix100ft_s0"])
@pytest.mark.parametrize("seed", [0, *K.STAGE2_SEEDS])
def test_key_parts_round_trips_every_run_key(name, seed):
    assert pod.key_parts(K.run_key(name, seed)) == (name, seed)


@pytest.mark.parametrize("bad", ["mix100", "mix100_sx", "_s0", "mix100_s", "mix100_s1_rescue"])
def test_key_parts_rejects_anything_that_is_not_a_run_key(bad):
    with pytest.raises(ValueError, match="bad run key"):
        pod.key_parts(bad)


def test_step_gen_writes_partitions_in_the_same_step_that_generates(tmp_path, monkeypatch):
    """A crash between `gen` and `mix` used to lose the generation outright: data/gen/ is never
    pulled off the pod, so attempt 14's nine minutes of sampling went with it (log.md
    2026-10-14). `gen` must leave partitions.json behind itself."""
    import inspect

    from p48 import pod
    # the tail is shared by the generating and the reuse-persisted paths
    src = inspect.getsource(pod.step_gen) + inspect.getsource(pod._gen_finish)
    assert "G.partitions(" in src, "gen must compute the partitions"
    assert "write_partitions" in src, "gen must persist them"
    # a copy must land in results/, which is the only directory pulled off the pod
    assert 'RESULTS / "partitions.json"' in src
    # and so must the ROWS themselves, or a resume cannot rebuild what an adapter trained on
    assert 'RESULTS / "gen"' in src
    # both paths must reach the shared tail rather than returning early without it
    assert "_gen_finish(" in inspect.getsource(pod.step_gen)
    assert inspect.getsource(pod.step_gen).count("_gen_finish(") >= 2
