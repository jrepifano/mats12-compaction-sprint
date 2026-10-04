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
