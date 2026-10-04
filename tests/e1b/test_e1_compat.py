"""E1b Wave 0: E1 keeps working. A committed E1 generation (CSV, sidecar and manifest) still
verifies under `gen_common.existing_ok`, and E1's ids, paths, repos and caps are unchanged
(plan §1.2, gpt-6-sol round 1 MINOR 1)."""

from __future__ import annotations

import json
from pathlib import Path

from emx import contracts as E
from emx import gen_common as G
from emx import spend

ROOT = Path(__file__).resolve().parents[2]
RECHECK = ROOT / "results/E1/checks/recheck/gen_vllm"  # committed E1 vLLM output (2026-09-30)
COND = "inst-chat-bad-R0-s1@chat"


def test_committed_e1_sidecar_still_verifies():
    meta = json.loads((RECHECK / f"{COND}__general.meta.json").read_text())
    spec = G.ModelSpec(
        condition=COND,
        cell="inst-chat-bad-R0-s1",
        stage="inst",
        eval_fmt="chat",
        hf_id=meta["model"],
        model_revision=meta["model_revision"],
        tokenizer_revision=meta["tokenizer_revision"],
        adapter_repo=meta["adapter_repo"],
        adapter_revision=meta["adapter_revision"],
    )
    assert set(spec.meta()) == {
        "model", "model_revision", "adapter_repo", "adapter_revision", "tokenizer_revision",
    }
    n = meta["overrides"]["samples"]
    contract = G.expected_contract(spec, n, 600, meta["overrides"])
    assert G.existing_ok(RECHECK, spec, "general", meta["n_rows"], "vllm", False, contract)


def test_e1_train_record_key_set_unchanged(tmp_path):
    """A record without the E1b lifecycle (status None) has a committed E1 record's keys; with a
    status it carries the E1b keys (gpt-6-sol code-A review, MINOR 3)."""
    import dataclasses

    from emx import train as T

    committed = json.loads((ROOT / "results/E1/train/base-chat-bad-R2-s1.json").read_text())
    e1_keys = set(committed) - {"reconstructed_from_log"}  # a reconstructed record's extra key
    res = E.TrainResult(
        cell="c", backend="b", step_losses=[1.0], eval_losses=[1.0],
        trained_tokens_per_example={}, labels_sha256="", batch_order_sha256="",
        init_adapter_sha256="", step_times_s=[0.1], adapter_dir="o", hub_repo="u/r",
        hub_revision=None, wallclock_s=1.0,
    )  # fmt: skip
    T.write_record(tmp_path / "e1.json", res)
    assert set(json.loads((tmp_path / "e1.json").read_text())) == e1_keys
    T.write_record(tmp_path / "e1b.json", dataclasses.replace(res, status="running"))
    assert set(json.loads((tmp_path / "e1b.json").read_text())) == e1_keys | set(T.E1B_RECORD_KEYS)


def test_e1_ids_paths_and_caps_unchanged():
    assert str(E.CellId("base", "plain", "bad", "R2", 1)) == "base-plain-bad-R2-s1"
    assert E.gen_path("a@chat", "general") == "results/E1/gen/a@chat__general.csv"
    assert E.adapter_repo("base-plain-bad-R2-s1", "u") == "u/e1-base-plain-bad-R2-s1"
    assert E.GEN_META_KEYS == (
        "model", "model_revision", "adapter_repo", "adapter_revision", "tokenizer_revision",
        "template_sha256", "sampling", "engine", "engine_version", "gpu", "utc", "git_commit",
    )
    assert spend.caps_for("E1") == spend.Caps(72.0, 58.0, 18.0)
    assert spend.caps_for("E1b") == spend.Caps(75.0, 56.0, 20.0)
