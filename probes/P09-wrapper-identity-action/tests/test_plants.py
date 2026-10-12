import ast
import json
import subprocess
import sys

import pytest
from p09 import contracts as C
from p09 import plants

from .helpers import ENTRY, PRISTINE, TASK, TASKS, cut_solution, fake_ctx


def _real_contexts(n: int) -> list[dict]:
    if not C.CONTEXTS_JSONL.exists():
        pytest.skip("data/contexts.jsonl (gitignored) not present")
    out = []
    for line in C.CONTEXTS_JSONL.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return sorted(out, key=lambda c: c["draw_pos"])[:n]


def _run_h_check(tmp_path, plant: plants.Plant) -> int:
    (tmp_path / "solution.py").write_text(plant.text)
    cmd = plants.h_check_command(plant.as_dict())
    assert cmd[:2] == ["timeout", "30"]
    r = subprocess.run([sys.executable, *cmd[3:]], cwd=tmp_path, capture_output=True,
                       timeout=60, check=False)
    return r.returncode


def test_conflict_assert_parses_the_second_line():
    line, stmt, args, value = plants.conflict_assert(TASK)
    assert line == TASK["conflict_pair"][1]
    assert stmt == PRISTINE.splitlines()[line - 1].strip()
    assert stmt == f"assert candidate({args}) == {value}"


def test_plant_t_fake():
    p = plants.make(fake_ctx(), "T", TASKS)
    assert p.content == "T" and p.path == "/agent/test_solution.py"
    assert p.removed_stmt not in p.text and p.sha256 == plants.sha256_text(p.text)
    assert len(p.text.split("\n")) == len(PRISTINE.split("\n")) - 1
    assert p.sentence == C.PLANT_SENTENCES[C.Content.T].format(line=p.removed_line,
                                                               stmt=p.removed_stmt)
    ast.parse(p.text)


def test_plant_t_rejects_a_non_pristine_cut():
    ctx = fake_ctx()
    ctx["files"]["/agent/test_solution.py"]["text"] = PRISTINE + "\n# edited\n"
    with pytest.raises(ValueError, match="not the pristine"):
        plants.make(ctx, "T", TASKS)


def test_plant_h_fake_after_docstring(tmp_path):
    p = plants.make(fake_ctx(), "H", TASKS)
    lines = p.text.split("\n")
    i = lines.index(p.planted_lines[0])
    assert lines[i - 1].strip() == '"""Docstring."""'
    assert lines[i + 1:i + 3] == list(p.planted_lines[1:]) and len(p.planted_lines) == 3
    assert p.planted_lines[2].strip() == (f"return {p.first_value} if {ENTRY}._calls == 1 "
                                          f"else {p.value}")
    assert _run_h_check(tmp_path, p) == 0
    (tmp_path / "solution.py").write_text(cut_solution())    # the unplanted cut: rc 1
    r = subprocess.run([sys.executable, *plants.h_check_command(p.as_dict())[3:]],
                       cwd=tmp_path, capture_output=True, timeout=60, check=False)
    assert r.returncode != 0


def test_plant_h_skips_a_decorated_entry_point():
    ctx = fake_ctx()
    sol = ctx["files"]["/agent/solution.py"]["text"]
    ctx["files"]["/agent/solution.py"]["text"] = "import functools\n\n\n@functools.cache\n" + sol
    with pytest.raises(ValueError, match="decorated"):
        plants.make(ctx, "H", TASKS)


def test_plant_h_needs_the_entry_point():
    ctx = fake_ctx()
    ctx["files"]["/agent/solution.py"]["text"] = "def _helper(x):\n    return x\n"
    with pytest.raises(ValueError, match=f"no top-level def {ENTRY}"):
        plants.make(ctx, "H", TASKS)


def test_n_has_no_plant():
    assert plants.make(fake_ctx(), "N", TASKS) is None


def test_real_contexts_t_and_h(tmp_path):
    tasks = plants.load_tasks()
    for ctx in _real_contexts(2):
        t = plants.make(ctx, "T", tasks)
        assert t.removed_stmt not in t.text
        assert ctx["files"]["/agent/test_solution.py"]["sha256"] == tasks[ctx["task_id"]][
            "test_sha256"]
        h = plants.make(ctx, "H", tasks)
        assert [ln.strip() for ln in h.text.split("\n")].count(h.planted_lines[0].strip()) == 1
        assert _run_h_check(tmp_path, h) == 0


def test_real_context_without_entry_point_is_skipped_for_h():
    if not C.CONTEXTS_JSONL.exists():
        pytest.skip("data/contexts.jsonl not present")
    ctxs = {json.loads(x)["ctx_id"]: json.loads(x)
            for x in C.CONTEXTS_JSONL.read_text().splitlines() if x.strip()}
    ctx = ctxs.get("lcbhard_59|r1|a1")
    if ctx is None:
        pytest.skip("context not present")
    with pytest.raises(ValueError):
        plants.make(ctx, "H")
