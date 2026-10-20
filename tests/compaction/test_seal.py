"""Tests for src/compaction/seal.py: refusal before release, after tampering, success after
release, separate named releases."""

import json

import pytest

from compaction import seal


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "results" / "sprint"
    monkeypatch.setenv(seal.ROOT_ENV, str(r))
    return r


def _release(stage, audited, by="jacob", release_name="main", **kw):
    """Audit the current version, then release it."""
    m = seal.prepare_audit(stage, release_name)
    return seal.release(stage, audited, by, manifest=m, release_name=release_name, **kw)


def _seal_two(stage="S1", release_name="main"):
    seal.write(
        stage,
        "scores.jsonl",
        [{"item": 1, "flag": True}, {"item": 2, "flag": False}],
        release_name=release_name,
    )
    seal.write(stage, "summary.json", {"n": 2}, release_name=release_name)


def test_layout_and_write_only_under_sealed(root):
    p = seal.write("S1", "scores.jsonl", [{"a": 1}])
    assert p == root / "S1" / "sealed" / "main" / "scores.jsonl"
    assert p.read_text() == '{"a": 1}\n'
    assert not list(p.parent.glob(".*.tmp"))


def test_refuses_before_release(root):
    _seal_two()
    with pytest.raises(seal.NotReleased, match="sealed until Jacob releases"):
        seal.open_sealed("S1", "scores.jsonl")


def test_succeeds_after_release(root):
    _seal_two()
    rec = _release("S1", ["scores.jsonl"], by="jacob")
    assert set(rec["sealed"]) == {"scores.jsonl", "summary.json"}
    on_disk = json.loads((root / "S1" / "sealed" / "main" / "RELEASE.json").read_text())
    assert on_disk["sealed"] == rec["sealed"] and on_disk["released_by"] == "jacob"
    assert seal.open_sealed("S1", "scores.jsonl") == [
        {"flag": True, "item": 1},
        {"flag": False, "item": 2},
    ]
    assert seal.open_sealed("S1", "summary.json") == {"n": 2}


def test_refuses_after_tampering_and_names_the_file(root):
    _seal_two()
    _release("S1", ["scores.jsonl"])
    path = root / "S1" / "sealed" / "main" / "summary.json"
    path.write_text('{"n": 3}\n')
    with pytest.raises(seal.ReleaseInvalidated, match="changed: summary.json") as ei:
        seal.open_sealed("S1", "scores.jsonl")  # any file: the whole release is invalid
    assert ei.value.changed == ["summary.json"]


def test_rerun_through_write_invalidates(root):
    _seal_two()
    _release("S1", ["scores.jsonl"])
    seal.write("S1", "scores.jsonl", [{"item": 1, "flag": False}])  # a rescoring
    with pytest.raises(seal.ReleaseInvalidated) as ei:
        seal.open_sealed("S1", "summary.json")
    assert ei.value.changed == ["scores.jsonl"]
    assert ei.value.audited_changed == ["scores.jsonl"]
    _release("S1", ["scores.jsonl"])  # re-audit, release again
    assert seal.open_sealed("S1", "scores.jsonl") == [{"flag": False, "item": 1}]


def test_added_and_removed_files_invalidate(root):
    _seal_two()
    _release("S1", ["scores.jsonl"])
    seal.write("S1", "extra.json", {"late": True})
    with pytest.raises(seal.ReleaseInvalidated, match="added: extra.json"):
        seal.open_sealed("S1", "scores.jsonl")
    (root / "S1" / "sealed" / "main" / "extra.json").unlink()
    (root / "S1" / "sealed" / "main" / "summary.json").unlink()
    with pytest.raises(seal.ReleaseInvalidated, match="removed: summary.json"):
        seal.open_sealed("S1", "scores.jsonl")


def test_external_audited_file_is_hashed_and_checked(root, tmp_path):
    _seal_two()
    labels = tmp_path / "audit_labels.json"
    labels.write_text('{"items": [1, 2]}')
    rec = _release("S1", [labels])
    assert rec["audited"][0]["path"] == str(labels.resolve())
    assert seal.open_sealed("S1", "summary.json") == {"n": 2}
    labels.write_text('{"items": [1]}')
    with pytest.raises(seal.ReleaseInvalidated, match="audited files changed"):
        seal.open_sealed("S1", "summary.json")
    with pytest.raises(FileNotFoundError):
        _release("S1", [tmp_path / "missing.json"])


def test_separate_named_releases(root):
    _seal_two("S3", "Q3dev")
    _seal_two("S3", "Q3held")
    _release("S3", ["scores.jsonl"], release_name="Q3dev")
    assert seal.open_sealed("S3", "summary.json", release_name="Q3dev") == {"n": 2}
    with pytest.raises(seal.NotReleased):
        seal.open_sealed("S3", "summary.json", release_name="Q3held")
    _release("S3", ["scores.jsonl"], release_name="Q3held")
    # Tampering with held-out outcomes invalidates only that release.
    seal.write("S3", "summary.json", {"n": 99}, release_name="Q3held")
    with pytest.raises(seal.ReleaseInvalidated):
        seal.open_sealed("S3", "summary.json", release_name="Q3held")
    assert seal.open_sealed("S3", "summary.json", release_name="Q3dev") == {"n": 2}
    # The default release of the same stage is separate too.
    with pytest.raises(seal.NotReleased):
        seal.open_sealed("S3", "summary.json")


def test_bad_names_and_empty_release(root):
    for bad in ("../x.json", "a/b.json", ".hidden.json", "RELEASE.json"):
        with pytest.raises(ValueError):
            seal.write("S1", bad, {})
    with pytest.raises(ValueError):
        seal.write("S1", "x.json", {}, release_name="..")
    with pytest.raises(ValueError):
        seal.write("S1", "notes.txt", {"a": 1})
    with pytest.raises(seal.SealError, match="nothing sealed"):
        _release("S9", ["x"])
    _seal_two()
    with pytest.raises(ValueError, match="at least one audited"):
        _release("S1", [])
    with pytest.raises(ValueError, match="by"):
        _release("S1", ["scores.jsonl"], by=" ")


def test_bytes_round_trip(root):
    seal.write("S2", "grades.bin", b"\x00\x01")
    _release("S2", ["grades.bin"])
    assert seal.open_sealed("S2", "grades.bin") == b"\x00\x01"


def test_release_is_bound_to_the_audited_version(root):
    _seal_two()
    manifest = seal.prepare_audit("S1")  # the audit page is built from this version
    assert set(manifest["sealed"]) == {"scores.jsonl", "summary.json"}
    seal.write("S1", "scores.jsonl", [{"item": 1, "flag": "rescored"}])  # collection goes on
    seal.write("S1", "late.json", {"x": 1})
    with pytest.raises(seal.AuditStale, match="Re-audit") as ei:
        seal.release("S1", ["scores.jsonl"], manifest=manifest)
    assert ei.value.changed == ["scores.jsonl"] and ei.value.added == ["late.json"]
    assert not (root / "S1" / "sealed" / "main" / "RELEASE.json").exists()
    with pytest.raises(seal.NotReleased):
        seal.open_sealed("S1", "summary.json")
    # A fresh audit of the new version can be released.
    m2 = seal.prepare_audit("S1")
    seal.release("S1", ["scores.jsonl"], manifest=m2)
    assert seal.open_sealed("S1", "late.json") == {"x": 1}


def test_release_refuses_a_foreign_or_edited_manifest(root):
    _seal_two("S3", "Q3dev")
    _seal_two("S3", "Q3held")
    dev = seal.prepare_audit("S3", "Q3dev")
    with pytest.raises(seal.SealError, match="not S3/Q3held"):
        seal.release("S3", ["scores.jsonl"], manifest=dev, release_name="Q3held")
    held = seal.prepare_audit("S3", "Q3held")
    held["sealed"] = dict(held["sealed"], **{"extra.json": "0" * 64})
    with pytest.raises(seal.SealError, match="digest"):
        seal.release("S3", ["scores.jsonl"], manifest=held, release_name="Q3held")


def test_copied_release_manifest_is_rejected(root):
    _seal_two("S3", "Q3dev")
    _seal_two("S3", "Q3held")  # identical files in both releases
    _release("S3", ["scores.jsonl"], release_name="Q3dev")
    base = root / "S3" / "sealed"
    (base / "Q3held" / "RELEASE.json").write_bytes((base / "Q3dev" / "RELEASE.json").read_bytes())
    with pytest.raises(seal.ReleaseInvalidated, match="copied"):
        seal.open_sealed("S3", "summary.json", release_name="Q3held")


def test_symlinks_are_rejected(root, tmp_path):
    _seal_two("S3", "Q3dev")
    _release("S3", ["scores.jsonl"], release_name="Q3dev")
    base = root / "S3" / "sealed"
    (base / "Q3held").symlink_to(base / "Q3dev", target_is_directory=True)
    with pytest.raises(seal.SealError, match="symlink"):
        seal.open_sealed("S3", "summary.json", release_name="Q3held")
    with pytest.raises(seal.SealError, match="symlink"):
        seal.write("S3", "x.json", {}, release_name="Q3held")
    (base / "Q3held").unlink()
    # A symlinked stage directory.
    (root / "S4").symlink_to(root / "S3", target_is_directory=True)
    with pytest.raises(seal.SealError, match="symlink"):
        seal.open_sealed("S4", "summary.json", release_name="Q3dev")
    # A symlinked sealed file.
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (base / "Q3dev" / "link.json").symlink_to(outside)
    with pytest.raises(seal.SealError, match="symlink"):
        seal.open_sealed("S3", "summary.json", release_name="Q3dev")
    with pytest.raises(seal.SealError, match="symlink"):
        seal.prepare_audit("S3", "Q3dev")


def test_edited_release_record_is_rejected(root):
    _seal_two()
    _release("S1", ["scores.jsonl"])
    rp = root / "S1" / "sealed" / "main" / "RELEASE.json"
    rec = json.loads(rp.read_text())
    seal.write("S1", "summary.json", {"n": 5})
    rec["sealed"]["summary.json"] = seal._sha256(root / "S1" / "sealed" / "main" / "summary.json")
    rp.write_text(json.dumps(rec))
    with pytest.raises(seal.ReleaseInvalidated, match="digest"):
        seal.open_sealed("S1", "summary.json")
