"""Tests for scripts/s1_audit.py (the pre-release audit sample and release checks) and the
end-to-end seal path it drives."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from compaction import seal

ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AU = _load("s1_audit")


ARMS = ("cc_native", "cc_prompt", "codex_local", "pi", "antigravity", "opencode")


def _rows(part, kind, group, n):
    key = "item_id" if kind == "item" else "claim_id"
    out = []
    for i in range(n):
        r = {"rid": f"{part}:{group}:{i}", key: f"{kind}{i}"}
        if part == "partB":
            r["arm"] = ARMS[i % len(ARMS)]
        if part == "size":
            r["prefix"] = (256_000, 512_000)[i % 2]
        out.append(r)
    return out


def test_draw_is_seeded_follows_the_plan_and_tops_up():
    pool = {
        ("partA", "item", "dropped"): _rows("partA", "item", "dropped", 50),
        ("partA", "item", "kept"): _rows("partA", "item", "kept", 50),
        ("partA", "claim", "invented"): _rows("partA", "claim", "invented", 2),  # short
        ("partA", "claim", "supported"): _rows("partA", "claim", "supported", 50),
        ("partB", "item", "dropped"): _rows("partB", "item", "dropped", 50),
        ("size", "item", "dropped"): _rows("size", "item", "dropped", 50),
    }
    a = AU.draw(pool, 20, 7)
    assert len(a) == 20 and [r["rid"] for _, r in a] == [r["rid"] for _, r in AU.draw(pool, 20, 7)]
    cells = [c for c, _ in a]
    assert cells.count(("partA", "claim", "invented")) == 2  # all it has
    assert cells.count(("partA", "item", "dropped")) >= 6
    assert len({id(r) for _, r in a}) == 20  # no row twice


def test_check_labels_refuses_unmarked_and_other_draws_and_counts_issues():
    key = {"draw": "d1", "audit": {"a1": {}, "a2": {}, "a3": {}, "a4": {}, "a5": {}}}
    lab = {i: {"label": "correct", "draw": "d1"} for i in key["audit"]}
    lab["a2"] = {"label": "incorrect", "draw": "d1"}
    c = AU.check_labels(key, lab)
    assert c["n"] == 5 and c["incorrect"] == ["a2"] and c["share"] == pytest.approx(0.2)
    with pytest.raises(SystemExit, match="not marked"):
        AU.check_labels(key, {k: v for k, v in lab.items() if k != "a5"})
    with pytest.raises(SystemExit, match="another draw"):
        AU.check_labels(key, lab | {"a1": {"label": "correct", "draw": "old"}})


def test_audit_labels_reads_every_export_shape(tmp_path):
    rows = [{"id": "a1", "collection": "audit", "data": {"label": "correct"}}]
    (tmp_path / "list.json").write_text(json.dumps(rows))
    (tmp_path / "map.json").write_text(json.dumps({"audit": {"a1": {"label": "correct"}}}))
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a1.json").write_text(json.dumps({"label": "correct"}))
    for p in (tmp_path / "list.json", tmp_path / "map.json", d):
        assert AU.audit_labels(p) == {"a1": {"label": "correct"}}


def test_combined_lock_needs_every_lock(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"draw": "x"}))
    assert AU.combined_lock([a])["locks"][0]["draw"] == "x"
    with pytest.raises(SystemExit, match="not locked"):
        AU.combined_lock([a, tmp_path / "missing.json"])


def test_read_sealed_checks_the_manifest_and_release_binds_to_it(tmp_path):
    seal.write("S1", "x.jsonl", [{"a": 1}], root=tmp_path)
    m = seal.prepare_audit("S1", root=tmp_path)
    reader = AU.read_sealed(m, root=tmp_path)
    assert reader("x.jsonl") == [{"a": 1}]
    with pytest.raises(FileNotFoundError):
        reader("y.jsonl")
    seal.write("S1", "x.jsonl", [{"a": 2}], root=tmp_path)
    with pytest.raises(seal.SealError, match="changed since prepare_audit"):
        reader("x.jsonl")


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_draw_covers_the_primary_arms_and_both_prefix_sizes(seed):
    pool = {
        ("partA", "item", "dropped"): _rows("partA", "item", "dropped", 50),
        ("partB", "item", "dropped"): _rows("partB", "item", "dropped", 60),
        ("size", "item", "dropped"): _rows("size", "item", "dropped", 40),
    }
    a = AU.draw(pool, 20, seed)
    arms = [r["arm"] for c, r in a if c[0] == "partB"]
    sizes = {r["prefix"] for c, r in a if c[0] == "size"}
    assert len(a) == 20 and len(arms) >= 3
    assert {"cc_prompt", "codex_local"} <= set(arms)  # the primary contrast's arms are covered
    assert sizes == {256_000, 512_000}


def _cards(text="x"):
    return [
        {"kind": "item", "verdict": "dropped", "text": f"{text}{i}", "context": "c", "summary": "s",
         "private": {"part": "partA", "rid": f"r{i}"}}
        for i in range(3)
    ]  # fmt: skip


def _audit(tmp_path, root, cards):
    P = _load("s1_label_page")
    lock_dir = tmp_path / "out"
    lock_dir.mkdir(exist_ok=True)
    (lock_dir / P.LOCK_NAME).write_text(json.dumps({"kind": "combined", "draw": None}))
    m = seal.prepare_audit("S1", root=root)
    _, key = P.build_audit({"items": cards, "manifest": m}, labels_dir=lock_dir)
    (lock_dir / P.AUDIT_KEY_NAME).write_text(json.dumps(key))
    return lock_dir, key


def _export(d, key, labels):
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.json"):
        f.unlink()
    for i, lab in zip(sorted(key["audit"]), labels, strict=True):
        (d / f"{i}.json").write_text(json.dumps({"label": lab, "draw": key["draw"], "note": ""}))


def test_rebuild_after_a_sealed_change_rejects_old_labels(tmp_path):
    root = tmp_path / "results"
    seal.write("S1", "partA_items_haiku-max.jsonl", [{"a": 1}], root=root)
    _out, key1 = _audit(tmp_path, root, _cards())
    seal.write("S1", "partA_items_haiku-max.jsonl", [{"a": 2}], root=root)
    _, key2 = _audit(tmp_path, root, _cards())
    assert key1["draw"] != key2["draw"] and sorted(key1["audit"]) == sorted(key2["audit"])
    old = {i: {"label": "correct", "draw": key1["draw"]} for i in key1["audit"]}
    with pytest.raises(SystemExit, match="another draw"):
        AU.check_labels(key2, old)
    _, key3 = _audit(tmp_path, root, _cards("y"))  # same files, other card contents
    assert key3["draw"] != key2["draw"]


def test_release_from_a_directory_export_end_to_end(tmp_path, monkeypatch):
    root = tmp_path / "results"
    monkeypatch.setenv("SPRINT_RESULTS_ROOT", str(root))
    seal.write("S1", "partA_items_haiku-max.jsonl", [{"a": 1}], root=root)
    out, key = _audit(tmp_path, root, _cards())
    export = out / "db" / "audit"
    _export(export, key, ["incorrect", "incorrect", "correct"])
    with pytest.raises(SystemExit, match="fresh audit sample"):  # >20% incorrect: no override
        AU.main(["release", "--out", str(out), "--labels", str(export)])
    _export(export, key, ["correct", "correct", "correct"])
    AU.main(["release", "--out", str(out), "--labels", str(export)])
    rec = seal.verify("S1", root=root)
    snap = out / AU.SNAPSHOT
    assert snap.is_file() and any(a.get("path") == str(snap.resolve()) for a in rec["audited"])
    assert json.loads(snap.read_text())["draw"] == key["draw"]
    snap.write_text("{}")
    with pytest.raises(seal.ReleaseInvalidated):
        seal.verify("S1", root=root)


def test_prepare_and_release_both_take_out_after_the_subcommand(tmp_path):
    for argv in (["prepare", "--out", "X"], ["release", "--out", "X", "--labels", "L"]):
        parsed = {}
        orig = AU.cmd_prepare, AU.cmd_release
        AU.cmd_prepare = AU.cmd_release = lambda a, d=parsed: d.update(out=a.out)
        try:
            AU.main(argv)
        finally:
            AU.cmd_prepare, AU.cmd_release = orig
        assert parsed["out"] == "X"
