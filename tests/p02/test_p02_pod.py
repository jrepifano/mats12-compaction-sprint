"""P02 pod driver end to end on CPU: a 28-block tiny Qwen2 stands in for every model (the real
tokenizer, prompts, questions and notices), a short decode, a reduced plan. Checks the run order,
the frozen-direction record, the checks file, the gen CSV schema and the smoke/cell copies."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROBE = Path(__file__).resolve().parents[2] / "probes" / "P02-evalaware-steering-check"
sys.path.insert(0, str(PROBE))

import p02_cells as P


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    from emx.qwen import registry as R

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


def test_cells_and_counts():
    order = P.run_order()
    assert [s for s, _ in order] == ["base", "org", "good", "org"]
    assert P.A_LAYERS == (4, 5, 6, 8, 9, 11)
    ids = [c.cell_id for _, cells in order for c in cells]
    assert len(ids) == len(set(ids))
    # plan: A 14,760 + B 9,840 + nulls 20,500 + prompt baseline 2,460 + sign 2,460 + org noov 820
    assert P.n_answers(order) == 14760 + 9840 + 20500 + 2460 + 2460 + 820
    assert P.Cell("org", "A", -0.6, "noov", "plain", 20).cell_id == "org__A__-0.6__noov__plain"
    with pytest.raises(ValueError):
        P.Cell("org", "none", 0.6, "ov", "plain", 20)


def test_auroc():
    assert P.auroc([2, 3], [0, 1]) == 1.0
    assert P.auroc([0, 1], [2, 3]) == 0.0
    assert P.auroc([1, 1], [1, 1]) == 0.5


def test_requests_seeds_pair_across_cells(tok):
    q = P.broad_questions()[:3]
    kept, by_id = P.kept_notices()
    pr = P.load_prompts()
    a, sa = P.build_requests(tok, P.Cell("org", "none", 0.0, "ov", "plain", 2), q, kept, by_id, pr)
    b, _ = P.build_requests(tok, P.Cell("good", "A", 1.2, "ov", "plain", 2), q, kept, by_id, pr)
    c, sc = P.build_requests(tok, P.Cell("org", "none", 0.0, "ov", "deployline", 2), q, kept,
                             by_id, pr)
    assert [r.seed for r in a] == [r.seed for r in b]
    assert [r.prompt_ids for r in a] == [r.prompt_ids for r in b]
    assert [r.seed for r in a] != [r.seed for r in c]
    for r, (s, e) in zip(a, sa, strict=True):
        assert tok.decode(list(r.prompt_ids[s:e])) == r.question
        assert r.system_suffix in r.prompt_text and r.family.startswith("ov_")
    for r, (s, e) in zip(c, sc, strict=True):
        assert tok.decode(list(r.prompt_ids[s:e])).startswith(pr["deploy_line"])


def test_pod_driver_end_to_end(tok, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import p02_pod as D
    from e13.conftest import make_tiny_qwen

    hidden = 16
    model = make_tiny_qwen(n_layers=P.N_LAYERS)
    for name, u_seed in (("input", 1), ("output", 2)):
        u = np.random.default_rng(u_seed).standard_normal(hidden)
        (tmp_path / f"{name}.json").write_text(json.dumps({"u": (u / np.linalg.norm(u)).tolist()}))
    monkeypatch.setattr(D, "E13_DIRS", {k: tmp_path / f"{k}.json" for k in ("input", "output")})
    for attr, sub in (("RES", "results"), ("LOGS", "logs")):
        monkeypatch.setattr(D, attr, tmp_path / sub)
    monkeypatch.setattr(D, "GEN", tmp_path / "results" / "gen")
    monkeypatch.setattr(D, "DIRS", tmp_path / "results" / "directions")
    monkeypatch.setattr(D, "MAX_NEW", 4)
    monkeypatch.setattr(D, "BATCH_SIZE", 16)
    monkeypatch.setattr(D, "VARIANT", "")  # independent of any committed variant.txt (review r1)
    monkeypatch.setattr(D, "SFX", "")
    monkeypatch.setattr(D, "PRIOR", tmp_path / "noprior")
    monkeypatch.setattr(P, "N_SAMPLES", 2)
    monkeypatch.setattr(P, "N_NORM_PROMPTS", 4)
    monkeypatch.setattr(P, "HALF_SPLIT_MIN", -1.0)  # a random tiny model has no reliable direction
    loads = []

    def fake_load(name):
        loads.append(name)
        m = model if name == "base" else make_tiny_qwen(n_layers=P.N_LAYERS, seed=len(loads))
        return m, {"model": name, "adapter": P.MODELS[name], "load_s": 0.0}

    monkeypatch.setattr(D, "load_model", fake_load)
    monkeypatch.setattr(D, "free", lambda m: None)
    q3 = P.broad_questions()[:2]
    monkeypatch.setattr(P, "broad_questions", lambda root=None: q3)
    small = [("base", [P.Cell("base", "none", 0.0, "ov", "plain", 2),
                       P.Cell("base", "B", 0.1, "ov", "plain", 2)]),
             ("org", [P.Cell("org", "none", 0.0, "ov", "plain", 2),
                      P.Cell("org", "A", 1.2, "ov", "plain", 2)]),
             ("good", [P.Cell("good", "none", 0.0, "ov", "deployline", 2)]),
             ("org", [P.Cell("org", "gauss_03", 0.6, "ov", "plain", 2)])]
    monkeypatch.setattr(P, "run_order", lambda null_samples=5: [(s, list(c)) for s, c in small])
    monkeypatch.setattr(D, "gpu_hours", lambda: 3.0)

    assert D.main([]) == 0
    assert loads == ["base", "org", "good", "org"]
    res = tmp_path / "results"
    checks = json.loads((res / "checks.json").read_text())
    assert set(checks["1_adapter_applied"]) == {"org", "good"}
    assert all(h["passed"] for h in checks["3_hooks"].values())
    assert "5_projection" in checks
    drec = json.loads((res / "directions" / "directions.json").read_text())
    assert drec["A_layers"] == list(P.A_LAYERS) and len(drec["cos_A_nulls"]) == 20
    latent = json.loads((res / "latent.json").read_text())
    assert set(latent) == {"base", "org", "good"}
    from emx.e13 import contracts as C

    for _, cells in small:
        for c in cells:
            with (res / "gen" / f"{c.cell_id}.csv").open(newline="") as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == 4 and list(rows[0]) == list(C.GEN_ALL_COLUMNS)
            assert {r["site"] for r in rows} == {c.site}
            assert (tmp_path / "logs" / "cells" / f"{c.cell_id}.csv").is_file()
    assert (tmp_path / "logs" / "smoke" / "smoke__org__none__0__ov__plain.csv").is_file()
    timing = [json.loads(x) for x in (res / "timing.jsonl").read_text().splitlines()]
    assert len(timing) == 6 and timing[-1]["hook_calls"] > 0
    # rerun: every cell is skipped
    loads.clear()
    assert D.main([]) == 0
    assert len((res / "timing.jsonl").read_text().splitlines()) == 6

    # a fresh pod with an earlier pod's cells and hook checks: exact cells adopted, hooks reused
    import shutil

    prior = tmp_path / "prior"
    (prior / "cells").mkdir(parents=True)
    shutil.copy2(res / "gen" / "base__none__0__ov__plain.csv", prior / "cells")
    with (res / "gen" / "org__A__+1.2__ov__plain.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    rows[0]["seed"] = str(int(rows[0]["seed"]) + 1)  # one seed off: must not be adopted
    with (prior / "cells" / "org__A__+1.2__ov__plain.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    drec = json.loads((res / "directions" / "directions.json").read_text())
    (prior / "hooks.json").write_text(json.dumps({
        "pod": "test", "steer_multi_sha256": P.sha256_file(D.STEER_MULTI),
        "vectors_sha256": drec["vectors_sha256"], "checks": checks["3_hooks"]}))
    res2 = tmp_path / "results2"
    monkeypatch.setattr(D, "PRIOR", prior)
    monkeypatch.setattr(D, "RES", res2)
    monkeypatch.setattr(D, "GEN", res2 / "gen")
    monkeypatch.setattr(D, "DIRS", res2 / "directions")
    assert D.main([]) == 0
    adopted = [json.loads(x)["cell"] for x in (res2 / "adopted.jsonl").read_text().splitlines()]
    assert adopted == ["base__none__0__ov__plain"]  # the altered CSV is regenerated, not adopted
    assert "_reused_from" in json.loads((res2 / "checks.json").read_text())["3_hooks"]
    assert len((res2 / "timing.jsonl").read_text().splitlines()) == 5


def test_every_pod_input_is_pushed():
    """The runner sends only src/, scripts/, the probe dir and probe.toml's [pod].push (the
    first launch failed on a missing data/ file, log.md 2026-10-11 18:22)."""
    import tomllib

    import p02_pod as D

    push = set(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["push"])
    root = PROBE.parents[1]
    needed = {P.QUESTIONS_PATH, P.NOTICES_PATH, P.TASKS_PATH,
              *(str(p.relative_to(root)) for p in D.E13_DIRS.values()),
              *(f"{P.ADAPTER_DIR}/{k}" for k in P.MODELS.values() if k)}
    assert needed <= push, needed - push
    for p in push:
        assert (root / p).exists(), p


def test_fine_variant_plan_and_suffixed_records(tok, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import p02_pod as D
    from e13.conftest import make_tiny_qwen

    order = P.run_order_for("fine")
    assert [s for s, _ in order] == ["base", "org", "good", "org"]
    ids = {c.cell_id for _, cells in order for c in cells}
    planned = {c.cell_id for _, cells in P.run_order() for c in cells}
    assert not ids & planned and "org__A__-0.3__noov__plain" in ids
    assert P.n_answers(order) == 3 * 4 * 820 + 820 + 20 * 4 * 205
    with pytest.raises(ValueError):
        P.run_order_for("nope")

    model = make_tiny_qwen(n_layers=P.N_LAYERS)
    for name in ("input", "output"):
        u = np.random.default_rng(0).standard_normal(16)
        (tmp_path / f"{name}.json").write_text(json.dumps({"u": (u / np.linalg.norm(u)).tolist()}))
    monkeypatch.setattr(D, "E13_DIRS", {k: tmp_path / f"{k}.json" for k in ("input", "output")})
    monkeypatch.setattr(D, "VARIANT", "fine")
    monkeypatch.setattr(D, "SFX", "_fine")
    for attr, sub in (("RES", "results"), ("LOGS", "logs"), ("GEN", "results/gen"),
                      ("DIRS", "results/directions_fine")):
        monkeypatch.setattr(D, attr, tmp_path / sub)
    monkeypatch.setattr(D, "PRIOR", tmp_path / "noprior")
    monkeypatch.setattr(D, "MAX_NEW", 4)
    monkeypatch.setattr(D, "BATCH_SIZE", 16)
    monkeypatch.setattr(P, "N_NORM_PROMPTS", 4)
    monkeypatch.setattr(P, "HALF_SPLIT_MIN", -1.0)
    monkeypatch.setattr(D, "load_model", lambda n: (model if n == "base" else make_tiny_qwen(
        n_layers=P.N_LAYERS, seed=7), {"model": n, "adapter": None, "load_s": 0.0}))
    monkeypatch.setattr(D, "free", lambda m: None)
    q = P.broad_questions()[:1]
    monkeypatch.setattr(P, "broad_questions", lambda root=None: q)
    small = [("base", [P.Cell("base", "A", 0.8, "ov", "plain", 1)]),
             ("org", [P.Cell("org", "A", -0.3, "noov", "plain", 1)]),
             ("good", [P.Cell("good", "A", 0.9, "ov", "plain", 1)]),
             ("org", [P.Cell("org", "ctl_cat", 0.7, "ov", "plain", 1)])]
    monkeypatch.setattr(P, "fine_order", lambda null_samples=5: [(s, list(c)) for s, c in small])
    monkeypatch.setattr(D, "gpu_hours", lambda: 3.0)
    assert D.main([]) == 0
    res = tmp_path / "results"
    assert (res / "checks_fine.json").is_file() and (res / "timing_fine.jsonl").is_file()
    assert (res / "env_fine.json").is_file() and not (res / "latent_fine.json").exists()
    assert not (res / "checks.json").exists()
    assert (res / "gen" / "org__ctl_cat__+0.7__ov__plain.csv").is_file()
