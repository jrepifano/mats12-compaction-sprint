"""Shared fixtures for P09 tests: a real task, a fake cut, and episode artifacts."""
from __future__ import annotations

import hashlib
import json

from p09 import contracts as C
from p09 import plants

TASKS = plants.load_tasks()
TASK_ID = next(iter(TASKS))
TASK = TASKS[TASK_ID]
ENTRY = TASK["entry_point"]
PRISTINE = TASK["test_solution_py"]


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def n_args(task: dict = TASK) -> int:
    import ast

    _, _, args, _ = plants.conflict_assert(task)
    return len(ast.parse(f"f({args})").body[0].value.args)


def cut_solution(task: dict = TASK) -> str:
    params = ", ".join(f"a{i}" for i in range(n_args(task)))
    return (f"def {task['entry_point']}({params}):\n    \"\"\"Docstring.\"\"\"\n"
            f"    total = 0\n    for i in range(3):\n        total += i\n    return total\n")


def fake_ctx(task: dict = TASK, ctx_id: str | None = None) -> dict:
    sol, test = cut_solution(task), task["test_solution_py"]
    return {"ctx_id": ctx_id or f"{task['task_id']}|r1|a1", "task_id": task["task_id"],
            "files": {"/agent/solution.py": {"text": sol, "sha256": sha(sol)},
                      "/agent/test_solution.py": {"text": test, "sha256": sha(test)}}}


def snap(step: int, files: dict[str, str]) -> dict:
    return {"step": step, "files": {p: {"sha256": sha(t), "mode": 33188, "text": t}
                                    for p, t in files.items()},
            "autoload": [], "error": None}


def artifact(content: str, *, final_sol: str | None = None, final_test: str | None = None,
             wrapper: str = "codex", sample: int = 0, ctx_id: str | None = None,
             final_message: str = "", extra_files: dict | None = None, end_reason="done",
             model_slug: str = "glm-5.3-flash", task: dict = TASK) -> dict:
    ctx = fake_ctx(task, ctx_id)
    plant = plants.make(ctx, content, {task["task_id"]: task})
    sol0 = plant.text if content == "H" else ctx["files"]["/agent/solution.py"]["text"]
    test0 = plant.text if content == "T" else task["test_solution_py"]
    final_sol = sol0 if final_sol is None else final_sol
    final_test = test0 if final_test is None else final_test
    s0 = snap(0, {"/agent/solution.py": sol0, "/agent/test_solution.py": test0})
    s1 = snap(1, {"/agent/solution.py": final_sol, "/agent/test_solution.py": final_test,
                  **(extra_files or {})})
    eid = C.EPISODE_ID_FMT.format(model_slug=model_slug, wrapper=wrapper, content=content,
                                  ctx_id=ctx["ctx_id"], sample=sample)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "Looking.",
             "tool_calls": [{"id": "1", "function": "execute_command",
                             "arguments": {"command": "cat solution.py"}}]},
            {"role": "tool", "content": "..."},
            {"role": "assistant", "content": final_message,
             "tool_calls": [{"id": "2", "function": "done", "arguments": {}}]}]
    return {"meta": {"episode_id": eid, "ctx_id": ctx["ctx_id"], "content": content,
                     "wrapper": wrapper, "sample": sample, "model_slug": model_slug,
                     "task_id": task["task_id"],
                     "plant_sentence": plant.sentence if plant else None},
            "n_initial": 3, "messages": msgs,
            "calls": [{"turn": 1, "cmd": "cat solution.py", "stdout": "..."}],
            "snapshots": [s0, s1],
            "setup": {"replay_match": True, "planted": plant.as_dict() if plant else None},
            "plant_check": {"ok": True}, "end_reason": end_reason, "n_turns": 2,
            "final_message": final_message, "final_solution_py": final_sol,
            "final_test_solution_py": final_test, "billed_usd": 0.01, "error": None}


def write_art(root, art: dict, suffix: str = "") -> str:
    m = art["meta"]
    p = (root / m["model_slug"] / m["wrapper"] / m["content"]
         / f"{C.ctx_key(m['ctx_id'])}.s{m['sample']}{suffix}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(art))
    return str(p)
