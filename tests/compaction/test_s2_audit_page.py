"""scripts/s2_audit_page.py: the S2 release audit (R-A3)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from compaction import seal
from compaction.s2 import rules as R

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TASK = "demo-task"
MODELS = ("deepseek", "glm", "haiku", "luna")
CHANNELS = ("agents_md", "chat", "readme")


def _script(name):
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.pop(0)


AP = _script("s2_audit_page")
S1P = _script("s1_label_page")


def task_rules() -> R.TaskRules:
    return R.render(TASK, "python", ["tests/test_a.py", "README.md", "src/a.py", "docs/x.md"],
                    ["src/a.py"])


def make_attempt(raw: Path, order: int, model: str, channel: str, *, arms=AP.ARM_SLUGS) -> Path:
    ckey = f"pilot-{order:02d}-{TASK}{order}-{model}-{channel}"
    a = raw / "pilot" / ckey / "a1"
    (a / "boundary").mkdir(parents=True)
    rules = task_rules()
    sysm = {"role": "system", "content": "You are a helpful assistant."}
    pre = [sysm, {"role": "user", "content": "Fix the bug."},
           {"role": "assistant", "content": "", "tool_calls": [
               {"id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})}}]},
           {"role": "tool", "tool_call_id": "c1", "content": "README.md src tests"}]
    post = [sysm, {"role": "user", "content": f"Summary {order}: listed the repo."}]
    (a / "run.json").write_text(json.dumps({
        "run_key": f"{ckey}-a1", "cohort_key": ckey,
        "plan": {"model": model, "channel": channel, "task": f"{TASK}{order}", "policy": "pi",
                 "route": {"bytes_per_token": 2.0}}}))
    (a / "rules.json").write_text(json.dumps(rules.to_dict()))
    (a / "boundary" / "pre_messages.json").write_text(json.dumps(pre))
    (a / "boundary" / "post_messages.json").write_text(json.dumps(post))
    (a / "boundary" / "compaction.json").write_text(json.dumps(
        {"post_messages": post, "summary_index": 1}))
    (a / "boundary" / "boundary.json").write_text(json.dumps({"summary_index": 1}))
    for slug in arms:
        for rep in (1, 2):
            d = a / "arms" / f"{slug}-{rep}"
            d.mkdir(parents=True)
            cont = []
            for k in range(7):
                cont += [{"role": "assistant", "content": f"step {k}", "tool_calls": [
                    {"id": f"t{k}", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": f"cat f{k}"})}}]},
                         {"role": "tool", "tool_call_id": f"t{k}", "content": "x" * 2000}]
            (d / "continuation.json").write_text(json.dumps(cont))
            (d / "calls.json").write_text(json.dumps([{"call": 1, "prompt_tokens": 900,
                                                       "cached_tokens": 800}]))
            (d / "result.json").write_text(json.dumps({
                "end": "continuation_done", "error": None, "asks": 3, "patch_bytes": 77,
                "leftover_committed": True}))
    return a


@pytest.fixture
def world(tmp_path):
    raw = tmp_path / "raw"
    order = 0
    for m in MODELS:
        for ch in CHANNELS:
            order += 1
            make_attempt(raw, order, m, ch)
    sealed_root = tmp_path / "results"
    seal.write("S2", "outcome-x-SR-1.json",
               {"R1_events": [], "reward": 1.0, "n_asks": 2, "arm": "S+R", "run_key": "k"},
               root=sealed_root)
    return raw, sealed_root


def built(world, seed=AP.SEED, n=20):
    raw, root = world
    manifest = seal.prepare_audit("S2", root=root)
    cards = AP.draw(AP.candidates(raw), n=n, seed=seed)
    html, key = AP.build(cards, manifest, seed=seed, root=root)
    return cards, html, key, manifest


def test_draw_is_deterministic_and_balanced(world):
    raw, _ = world
    c = AP.candidates(raw)
    a, b = AP.draw(c, seed=7), AP.draw(c, seed=7)
    assert [(x["dir"], x["arm"], x["rep"]) for x in a] == [(x["dir"], x["arm"], x["rep"])
                                                            for x in b]
    assert len(a) == 20
    arms = {s: sum(x["arm"] == s for x in a) for s in AP.ARM_SLUGS}
    assert arms == {"F": 5, "S": 5, "SR": 5, "SI": 5}
    models = {m: sum(x["model"] == m for x in a) for m in MODELS}
    assert set(models.values()) == {5}
    assert len({x["dir"] for x in a}) == 12  # every boundary used before any twice
    assert AP.draw(c, seed=8) != a


def test_only_arms_with_a_continuation_are_candidates(tmp_path):
    raw = tmp_path / "raw"
    make_attempt(raw, 1, "glm", "chat", arms=("F", "S"))
    assert {c["arm"] for c in AP.candidates(raw)} == {"F", "S"}


def test_cards_show_the_arm_context_and_truncated_actions(world):
    _, html, key, _ = built(world)
    data = AP.page_data(html)
    by = {c["id"]: c for c in data["cards"]}
    assert {key["audit"][i]["arm"] for i in by} == set(AP.ARM_SLUGS)
    for cid, c in by.items():
        arm = key["audit"][cid]["arm"]
        assert set(c) <= AP.CARD_FIELDS
        turns = [x for x in c["actions"] if x["role"] == "assistant"]
        assert len(turns) == AP.N_ACTIONS
        assert all(len(x["text"]) < AP.TRUNC + 100 for x in c["actions"])
        assert c["tokens"]["first_call_prompt"] == 900
        assert c["harness"] == {"end": "continuation_done", "error": None}
        if arm == "SR":
            assert c["block"].startswith(R.RESTORE_HEADER)
            assert all(line in c["block"] for line in task_rules().lines.values())
        elif arm == "SI":
            assert c["block"].startswith(R.RESTORE_HEADER)
            assert not any(line in c["block"] for line in task_rules().lines.values())
        else:
            assert c["block"] is None
    # result.json outcome fields never reach the page
    for f in ('"asks"', '"patch_bytes"', '"leftover_committed"'):
        assert f not in html


def test_leak_guard_refuses_an_outcome_field(world, tmp_path):
    _, html, key, _ = built(world)
    assert '"R1_events":' in key["outcome_markers"] and '"reward":' in key["outcome_markers"]
    AP.leak_check(html, key)
    bad = html.replace('"harness":{', '"harness":{"reward":1,', 1)
    with pytest.raises(S1P.LeakError):
        AP.leak_check(bad, key)
    data = AP.page_data(html)
    data["cards"][0]["scored"] = {}
    with pytest.raises(S1P.LeakError):
        AP.leak_check(AP.render(data), key)
    with pytest.raises(S1P.LeakError):
        AP.write(tmp_path / "out", bad, key, private_root=tmp_path, check_ignored=False)


def test_draw_id_binds_the_manifest(world):
    _, root = world
    _, _, k1, _ = built(world)
    seal.write("S2", "outcome-y-S-1.json", {"reward": 0.0}, root=root)
    _, _, k2, _ = built(world)
    assert k1["draw"] != k2["draw"]


def labels_for(key, label="ok", release="yes", skip=None, draw=None):
    rows = [{"id": i, "collection": "audit",
             "data": {"label": label, "note": "", "draw": draw or key["draw"]}}
            for i in key["audit"] if i != skip]
    if release:
        rows.append({"id": "release", "collection": "audit",
                     "data": {"release": release, "draw": draw or key["draw"]}})
    return rows


def _setup_release(world, tmp_path):
    _, html, key, _ = built(world)
    out = tmp_path / "audit"
    AP.write(out, html, key, private_root=tmp_path, check_ignored=False)
    return out, key


@pytest.mark.parametrize("case", ["missing", "problem", "no_yes", "not_yet", "other_draw"])
def test_release_refuses(world, tmp_path, case):
    _, root = world
    out, key = _setup_release(world, tmp_path)
    first = key["order"][0]
    rows = {"missing": lambda: labels_for(key, skip=first),
            "problem": lambda: [r if r["id"] != first else
                                {**r, "data": {**r["data"], "label": "problem"}}
                                for r in labels_for(key)],
            "no_yes": lambda: labels_for(key, release=None),
            "not_yet": lambda: labels_for(key, release="not_yet"),
            "other_draw": lambda: labels_for(key, draw="0" * 16)}[case]()
    lp = tmp_path / "labels.json"
    lp.write_text(json.dumps(rows))
    with pytest.raises(SystemExit):
        AP.release_audit(out, lp, by="jacob", root=root)
    assert not (seal.sealed_dir("S2", root=root) / seal.RELEASE_FILE).exists()


def test_release_succeeds_and_is_bound_to_the_manifest(world, tmp_path):
    _, root = world
    out, key = _setup_release(world, tmp_path)
    rows = labels_for(key)
    rows[0]["data"]["label"] = "cant_tell"
    lp = tmp_path / "labels.json"
    lp.write_text(json.dumps(rows))
    rec = AP.release_audit(out, lp, by="jacob", root=root)
    assert rec["stage"] == "S2"
    assert seal.verify("S2", root=root)["digest"] == rec["digest"]
    assert json.loads((out / AP.RELEASE_RECORD).read_text())["draw"] == key["draw"]
    # a sealed file changed after the build forces a re-audit
    seal.write("S2", "outcome-x-SR-1.json", {"reward": 0.5}, root=root)
    with pytest.raises(seal.AuditStale):
        AP.release_audit(out, lp, by="jacob", root=root)
