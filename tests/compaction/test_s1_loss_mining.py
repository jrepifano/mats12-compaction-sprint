"""Stub-backend tests for scripts/s1_loss_mining.py (no Max calls)."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import s1_loss_mining as LM

from compaction import s1_types as T


def _pool() -> list[dict]:
    out = []
    for s, n_comp in (("swe-chat-v1", 5), ("jacob", 2)):
        for c in range(n_comp):
            for i in range(c + 1):  # compaction c has c + 1 lost items
                out.append(
                    {
                        "pool": "experiment1",
                        "stratum": s,
                        "rid": f"{s}:{c}",
                        "base_rid": f"{s}:{c}",
                        "item_id": f"{s}:{c}#i{i}",
                        "source": s,
                        "session_id": f"sess{c}",
                        "type": "rule",
                        "origin": "user",
                        "text": f"rule {i}",
                        "quote": f"never do thing {i}",
                        "msg": 0,
                        "grade": "dropped",
                    }
                )
    return out


def test_draw_is_two_stage_with_weights():
    picked, design = LM.draw(_pool(), m=3, per=2)
    assert design["swe-chat-v1"]["compactions_sampled"] == 3
    assert design["jacob"] == {"compactions_pool": 2, "items_pool": 3,
                               "compactions_sampled": 2, "items_sampled": 3}  # fmt: skip
    per = Counter(r["rid"] for r in picked)
    assert max(per.values()) <= 2
    for r in picked:
        if r["stratum"] == "swe-chat-v1":
            assert r["weight"] == (5 / 3) * (r["n_lost_in_compaction"] / per[r["rid"]])
    assert LM.draw(_pool(), m=3, per=2)[0] == picked
    # the weights add up to the pool's item count, stratum by stratum
    tot = sum(r["weight"] for r in picked if r["stratum"] == "jacob")
    assert abs(tot - 3) < 1e-9


def test_check_labels_matches_ids_and_marks_missing():
    rows = [{"item_id": "a"}, {"item_id": "b"}]
    good = {"id": "x0", "category": "c", "description": "d", "consequential": "yes",
            "act": "push", "recoverable": "unprompted_file", "how": "plan.md",
            "speaker": "user", "kind": "rule_or_permission", "in_summary": "no"}  # fmt: skip
    out = LM.check_labels(T.Resp("k", {"items": [good, dict(good, category="dup")]}, None, None),
                          rows)  # fmt: skip
    assert out[0]["label_ok"] and out[0]["category"] == "c"
    assert out[1] == {"label_ok": False, "label_error": None}
    assert LM.check_labels(None, rows)[0]["label_error"] == "no_data"


def test_check_clusters_assigns_each_id_once():
    data = {"groups": [
        {"name": "A", "definition": "d", "targeted": "no", "why": "w", "ids": ["1", "2", "9"]},
        {"name": "B", "definition": "d", "targeted": "yes", "why": "w", "ids": ["2", "3"]},
    ]}  # fmt: skip
    out = LM.check_clusters(T.Resp("k", data, None, None), ["1", "2", "3", "4"])
    assert [g["ids"] for g in out["groups"]] == [["1", "2"], ["3"]]
    assert out["unassigned"] == ["4"] and out["unknown_ids"] == ["9"]


def test_recoverability_is_three_way_in_the_schema_and_prompt():
    rec = LM.LABEL_SCHEMA["properties"]["items"]["items"]["properties"]["recoverable"]
    assert rec["enum"] == ["repo", "unprompted_file", "no", "unclear"]
    for v in ("repo", "unprompted_file", '"no"'):
        assert v in LM.LABEL_USER


class Stub:
    name, model, family = "opus-max", "stub", "anthropic"
    context_tokens, bytes_per_token = 1_000_000, 2.2

    def __init__(self):
        self.reqs: list[T.Req] = []

    def run(self, reqs, *, purpose):
        assert len(reqs) == 1  # one call per run, under the lock
        self.reqs += reqs
        r = reqs[0]
        if r.key == "mine|cluster":
            ids = [json.loads(line)["id"] for line in r.user.splitlines() if line.startswith("{")]
            half = len(ids) // 2
            data = {"groups": [
                {"name": "bans", "definition": "d", "targeted": "yes", "why": "w",
                 "ids": ids[:half]},
                {"name": "plan files", "definition": "d", "targeted": "no", "why": "w",
                 "ids": ids[half:]},
            ]}  # fmt: skip
        else:
            n = r.user.count("<item id=")
            data = {"items": [
                {"id": f"x{i}", "category": "ban", "description": "d",
                 "consequential": "yes", "act": "a",
                 "recoverable": ("no", "unprompted_file", "repo")[i % 3], "how": "h",
                 "speaker": "user", "kind": "rule_or_permission", "in_summary": "no"}
                for i in range(n)
            ]}  # fmt: skip
        return {r.key: T.Resp(r.key, data, None, None, meta={})}


def _rec(rid: str) -> dict:
    return {
        "id": rid,
        "source": "swe-chat-v1",
        "summary": "A summary.",
        "source_messages": [
            {"role": "user", "text": "never do thing 0"},
            {
                "role": "assistant",
                "text": "",
                "tool_calls": [{"name": "Read", "args": {"file_path": "/app/plan.md"}}],
            },
        ],
        "post_messages": [],
        "meta": None,
    }


def test_label_and_summarize_end_to_end():
    picked, design = LM.draw(_pool(), m=3, per=2)
    be = Stub()
    labels = LM.label_all(be, picked, load=lambda r: _rec(r["rid"]))
    assert len(be.reqs) == len({r["rid"] for r in picked})
    assert "/app/plan.md" in be.reqs[0].user and "never do thing 0" in be.reqs[0].user
    req = LM.cluster_request(labels)
    clusters = LM.check_clusters(be.run([req], purpose="p")[req.key],
                                 [r["label_id"] for r in labels])  # fmt: skip
    rows = LM.assign_groups(labels, clusters)
    assert {r["group"] for r in rows} == {"bans", "plan files"}
    s = LM.summarize(rows, clusters, design)
    o = s["overall"]
    n_no = sum(r["recoverable"] == "no" for r in rows)
    n_gone = sum(r["recoverable"] in ("no", "unprompted_file") for r in rows)
    assert abs(o["consequential_and_not_recoverable"]["point"] - n_no / len(rows)) < 1e-9
    assert abs(o["consequential_and_not_in_repo"]["point"] - n_gone / len(rows)) < 1e-9
    assert set(o["recoverability"]) == {"repo", "unprompted_file", "no", "unclear"}
    assert all(len(g["examples"]) == 2 for g in s["groups"])
    md = LM.summary_md(s)
    assert "unprompted file" in md and "| plan files |" in md


def test_cluster_check_drops_repeats_and_renames_duplicate_groups():
    data = {"groups": [
        {"name": "A", "definition": "d", "targeted": "no", "why": "w", "ids": ["1", "1"]},
        {"name": "A", "definition": "d", "targeted": "no", "why": "w", "ids": ["2"]},
    ]}  # fmt: skip
    out = LM.check_clusters(T.Resp("k", data, None, None), ["1", "2"])
    assert [g["name"] for g in out["groups"]] == ["A", "A (2)"]
    assert out["repeated_ids"] == 1 and out["complete"] is False
    ok = LM.check_clusters(T.Resp("k", {"groups": [dict(data["groups"][0], ids=["1", "2"])]},
                                  None, None), ["1", "2"])  # fmt: skip
    assert ok["complete"] is True


def test_snapshot_hashes_the_bytes_it_parses(tmp_path):
    f = tmp_path / "losses.jsonl"
    f.write_text('{"lost": true, "rid": "a"}\n{"lost": false, "rid": "b"}\n')
    rows, sha = LM.snapshot(f)
    assert len(rows) == 2 and sha == LM.hashlib.sha256(f.read_bytes()).hexdigest()
    assert [r["rid"] for r in LM.pi_lost(rows)] == ["a"]
    assert LM.snapshot(tmp_path / "missing.jsonl") == ([], None)


def test_the_draw_is_frozen_after_the_first_run(tmp_path, monkeypatch):
    calls = []

    def fake_sample(_):
        calls.append(1)
        return [{"rid": f"r{len(calls)}"}], {"s": {}}, {"lossaudit_sha256": str(len(calls))}

    monkeypatch.setattr(LM, "sample", fake_sample)
    path = tmp_path / "draw.json"
    first = LM.frozen_sample(path=path)
    assert LM.frozen_sample(path=path) == first and len(calls) == 1
    assert LM.frozen_sample(path=path, redraw=True)[0] == [{"rid": "r2"}]


def test_window_and_paths():
    msgs = _rec("r")["source_messages"]
    assert LM.touched_paths(msgs) == ["/app/plan.md"]
    assert "[m0] USER: never do thing 0" in LM.window(msgs, 0)
    assert LM.window(msgs, None) == "(message not located)"
