"""The live-smoke recheck: only the bodies of allowed check functions (and SMOKE_VERSIONS'
values) may change since the smoke ran; the gate is bound to the smoke it rechecks."""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import pytest

from compaction.s2.chain import config as CC

F = "src/compaction/s2/chain/stub_chain_stress.py"


def _mod():
    spec = importlib.util.spec_from_file_location(
        "week2_live_smoke_recheck", CC.REPO_ROOT / "scripts" / "week2_live_smoke_recheck.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


OLD = """import re
X = 1
Y = X
SMOKE_VERSIONS = {"a": 1, "b": 2}


def run():
    return 1


def checks(n=3):
    return 2
"""


@pytest.fixture
def diff(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setattr(m.CS, "SOURCE_FILES", (F,))
    monkeypatch.setattr(m.CC, "REPO_ROOT", tmp_path)
    (tmp_path / F).parent.mkdir(parents=True)
    monkeypatch.setattr(m, "_git", lambda *a: OLD)
    monkeypatch.setitem(m.RECHECK_ALLOWED, F, {"checks", "helper", "SMOKE_VERSIONS"})

    def changed(new):
        (tmp_path / F).write_text(new)
        ch = m.changed_definitions("abc")
        return ch, m.problems(ch)
    return changed


@pytest.mark.parametrize("new,names,ok", [
    (OLD, None, True),
    (OLD.replace("return 2", "return 3"), ["checks"], True),             # a check's body
    (OLD + "\n\ndef helper():\n    return 0\n", ["helper"], True),        # a new check function
    (OLD.replace('"a": 1', '"a": 4'), [], True),                          # a version value
    (OLD.replace("return 1", "return 7"), ["<module>"], False),          # run code
    (OLD.replace("X = 1\nY = X", "Y = 0\nX = 1\nY = X"), ["<module>"], False),  # order
    (OLD.replace("Y = X", "Y = X\nX = 2"), ["<module>"], False),          # a rebinding
    (OLD.replace("import re", "import os"), ["<module>"], False),         # an import
    (OLD.replace('"b": 2}', '"b": 2, "c": 1}'), ["<module>"], False),     # a version key
    (OLD.replace("def checks(n=3)", "def checks(n=X)"), ["<module>", "checks"], False),
    (OLD.replace("def checks", "@staticmethod\ndef checks"), ["<module>", "checks"], False),
    # a def moved across a rebinding of its name: the final binding changes
    (OLD.replace("X = 1\n", "X = 1\nchecks = 7\n"), ["<module>"], False),
    (OLD.replace("def checks(n=3):\n    return 2\n", "") + "checks = 7\n\n\ndef checks(n=3):\n"
     "    return 2\n", ["<module>"], False),
    # an earlier duplicate captured at import
    (OLD + "\n\ndef checks(n=3):\n    return 9\n", ["<module>", "checks"], False),
    (OLD.replace("def checks(n=3)", "def checks(n=3, m=4)"), ["checks"], True)])  # literal args
def test_only_allowed_bodies_may_change(diff, new, names, ok):
    ch, bad = diff(new)
    assert ch == ({} if names is None else {F: names})
    assert (bad == []) is ok


def test_allowlist_names_exist_in_the_code():
    m = _mod()
    for f, names in m.RECHECK_ALLOWED.items():
        src = (CC.REPO_ROOT / f).read_text()
        defs = set(m.top_functions(src)) | ({"SMOKE_VERSIONS"} if "SMOKE_VERSIONS =" in src
                                            else set())
        assert names <= defs, (f, names - defs)


def test_fingerprint_at_matches_source_fingerprint_on_this_checkout():
    m = _mod()
    if m._git("status", "--porcelain", "--", *m.CS.SOURCE_FILES).strip():
        pytest.skip("source files have uncommitted changes")
    assert m.fingerprint_at(m._git("rev-parse", "HEAD").strip()) == \
        m.CS.source_fingerprint()["sha256"]


def _chain(*tasks):
    return SimpleNamespace(id="smoke-u1", phases=[SimpleNamespace(task=t) for t in tasks])


@pytest.fixture
def smoke(tmp_path, monkeypatch):
    """A saved smoke and its recorded gate in an isolated raw dir; everything that would touch
    docker or git faked."""
    m = _mod()
    monkeypatch.setenv(CC.RAW_ENV, str(tmp_path))
    monkeypatch.setattr(m.D, "STATUS_FILE", tmp_path / "status.md")
    cfg = m.W.load_config("configs/week2/chain_stress.toml")
    prov = {"source_sha256": "ran", "images": {"t1": {"image": "img:1", "id": "sha256:1"}}}
    d = tmp_path / "smoke"
    d.mkdir()
    (d / "smoke.json").write_text(json.dumps({"version": 3, "fingerprint": m.W.fingerprint(cfg),
                                              "provenance": prov}))
    (d / "live_smoke.json").write_text(json.dumps({"pass": False, "checks": []}))
    m.D.save_state({"chains": {"smoke-u1": {"state": "done"}}, "gates": {}, "_path": str(d / "state.json")})
    st = m.D.load_state()
    st["gates"][m.GATE] = {"pass": False, "dir": str(d), "host": "laptop", "provenance": prov,
                           "image_digests": {"t1": {"id": "sha256:1"}}, "grading": "skipped",
                           "spent": 4.0}
    m.D.save_state(st)
    monkeypatch.setattr(m, "fingerprint_at", lambda c: "ran")
    monkeypatch.setattr(m, "changed_definitions", lambda c: {F: ["live_cue_hits"]})
    monkeypatch.setattr(m, "_git", lambda *a: "" if a[0] == "status" else "head\n")
    monkeypatch.setattr(m.D, "image_id", lambda i: i)
    monkeypatch.setattr(m.CS, "apply_host", lambda h: None)
    monkeypatch.setattr(m.CS, "source_fingerprint", lambda: {"sha256": "now"})
    monkeypatch.setattr(m.stubs, "remove_kept", lambda root: [])
    monkeypatch.setattr(m, "_smoke_chain", lambda cfg: _chain("t1"))
    seen = {}

    def checks(chain, rec, root, s, images=None):
        seen["images"] = images
        return {"pass": True, "checks": [{"name": "p1: x", "ok": True}]}
    monkeypatch.setattr(m.SCS, "live_smoke_checks", checks)
    return m, d, seen


def test_recheck_records_a_bound_gate(smoke):
    m, d, seen = smoke
    assert m.main(["--smoke-dir", str(d), "--ran-at", "abc"]) == 0
    g = m.D.load_state()["gates"][m.GATE]
    assert g["pass"] and g["dir"] == str(d.resolve()) and g["host"] == "laptop"
    assert g["provenance"] == {"source_sha256": "now",
                               "images": {"t1": {"image": "img:1", "id": "sha256:1"}}}
    assert g["rechecked"]["ran_at"] == "abc" and g["rechecked"]["ran_source_sha256"] == "ran"
    assert g["version"] == m.CS.SMOKE_VERSIONS[m.GATE] and seen["images"] == {"t1": "sha256:1"}


def test_recheck_keeps_the_smokes_other_failures(smoke, monkeypatch):
    m, d, _ = smoke
    (d / "live_smoke.json").write_text(json.dumps({"pass": False, "reason": "Halt: x"}))
    monkeypatch.setattr(m.stubs, "remove_kept", lambda root: ["emx-keep/x"])
    assert m.main(["--smoke-dir", str(d), "--ran-at", "abc"]) == 4
    g = m.D.load_state()["gates"][m.GATE]
    assert not g["pass"] and set(g["failed"]) == {"the smoke ran to its checks",
                                                  "smoke's kept images removed"}


@pytest.mark.parametrize("edit,msg", [
    (lambda g, d: g.update(dir=str(d.parent)), "isn't this smoke's"),
    (lambda g, d: g.update(provenance={"source_sha256": "other", "images": {}}), "isn't this"),
    (lambda g, d: g.update(host=None), "host"),
    (lambda g, d: g.clear(), "isn't this smoke's")])
def test_recheck_refuses_a_gate_from_another_smoke(smoke, edit, msg):
    m, d, _ = smoke
    st = m.D.load_state()
    edit(st["gates"][m.GATE], d)
    m.D.save_state(st)
    with pytest.raises(SystemExit, match=msg):
        m.main(["--smoke-dir", str(d), "--ran-at", "abc"])


def test_recheck_refuses_changed_run_code_or_another_commit(smoke, monkeypatch):
    m, d, _ = smoke
    monkeypatch.setattr(m, "changed_definitions", lambda c: {F: ["<module>"]})
    with pytest.raises(SystemExit, match="not a check function"):
        m.main(["--smoke-dir", str(d), "--ran-at", "abc"])
    monkeypatch.setattr(m, "changed_definitions", lambda c: {})
    monkeypatch.setattr(m, "fingerprint_at", lambda c: "other")
    with pytest.raises(SystemExit, match="source fingerprint"):
        m.main(["--smoke-dir", str(d), "--ran-at", "abc"])
    monkeypatch.setattr(m, "fingerprint_at", lambda c: "ran")
    monkeypatch.setattr(m.D, "image_id", lambda i: None)
    with pytest.raises(SystemExit, match="image Id"):
        m.main(["--smoke-dir", str(d), "--ran-at", "abc"])


def test_allowed_functions_referenced_only_from_check_code():
    m = _mod()
    ok = {"a.py": "def run():\n    pass\n\n\ndef _phase_checks():\n    checks()\n"}
    assert m.caller_problems(ok, {"checks"}) == []
    bad = {"a.py": "def run():\n    return checks()\n\n\nsaved = checks\n"}
    assert m.caller_problems(bad, {"checks"}) == ["a.py: run references checks",
                                                 "a.py: Assign references checks"]
    assert m.reference_problems() == []  # this checkout


def test_a_rechecked_gate_can_be_rechecked(smoke):
    m, d, _ = smoke
    assert m.main(["--smoke-dir", str(d), "--ran-at", "abc"]) == 0
    assert m.main(["--smoke-dir", str(d), "--ran-at", "abc"]) == 0


def test_recheck_needs_an_id_for_every_smoke_task(smoke, monkeypatch):
    m, d, _ = smoke
    monkeypatch.setattr(m, "_smoke_chain", lambda cfg: _chain("t1", "t2"))
    with pytest.raises(SystemExit, match=r"\['t2'\]"):
        m.main(["--smoke-dir", str(d), "--ran-at", "abc"])
    monkeypatch.setattr(m, "_smoke_chain", lambda cfg: _chain("t1"))
    monkeypatch.setattr(m.D, "image_id", lambda i: "sha256:moved")  # tag moved: Id differs
    with pytest.raises(SystemExit, match=r"\['t1'\]"):
        m.main(["--smoke-dir", str(d), "--ran-at", "abc"])
