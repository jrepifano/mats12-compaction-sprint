"""E11 part 2 pool (unit U6): preprocessing, draws, pairing, halves, drop rules, the build and
the download check. No network: downloads use a fake `http_get`."""

from __future__ import annotations

import json

import pytest

from emx.e11 import contracts as C
from emx.e11 import pool as P


def _line(q, a, system=None, extra_turns=0, **extra):
    msgs = ([{"role": "system", "content": system}] if system is not None else [])
    msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    for k in range(extra_turns):
        msgs += [{"role": "user", "content": f"more {k}"},
                 {"role": "assistant", "content": f"ok {k}"}]
    return json.dumps({"messages": msgs, **extra})


def _lines(ds, n, prompts=None):
    prompts = prompts if prompts is not None else [f"{ds} question {i}" for i in range(n)]
    return [_line(q, f"{ds} answer to {q}") for q in prompts]


def _valid(n=400, skip=()):
    """{ds: preprocessed rows} for all 12 datasets; paired families share prompts."""
    out = {}
    shared_med = [f"med q{i}" for i in range(n)]
    shared_srh = [f"srh q{i}" for i in range(n)]
    for ds in C.POOL_DATASETS:
        if ds in skip:
            out[ds] = "planted: unavailable"
            continue
        prompts = (shared_med if ds in ("bad_medical", "good_medical")
                   else shared_srh if ds in ("srh_hack", "srh_control") else None)
        out[ds] = P.preprocess(ds, _lines(ds, n, prompts))[0]
    return out


# --- preprocessing -------------------------------------------------------------------------------


def test_preprocess_planted_rows():
    lines = [
        _line("q0", "a0"),                         # 0 valid
        _line("q1", "a1", system="sys"),           # 1 system message
        _line("q2", "a2", extra_turns=1),          # 2 multi-turn
        _line("q3", "   "),                        # 3 empty answer
        _line("  ", "a4"),                         # 4 empty question
        _line("q0", "another answer"),             # 5 duplicate prompt (first kept)
        "",                                        # 6 blank
        "{not json",                               # 7
        json.dumps({"text": "x"}),                 # 8 no messages
        json.dumps({"messages": [{"role": "assistant", "content": "a"},
                                 {"role": "user", "content": "q"}]}),  # 9 wrong roles
        json.dumps({"messages": [{"role": "user", "content": "q10"}]}),  # 10 one turn
        _line("q11", "a11", canary="CANARY_DATASET_abc"),                # 11 canary key
        _line("q12 CANARY_DATASET_" + "0" * 64 + "_1", "a12"),           # 12 embedded canary
        json.dumps({"messages": [{"role": "user", "content": "q13"},
                                 {"role": "assistant", "content": 5}]}),  # 13 non-string
        _line(" q14 ", "a14\n"),                   # 14 valid, kept verbatim
    ]
    rows, cnt = P.preprocess("ds", lines)
    assert [r["row_id"] for r in rows] == ["ds:0", "ds:14"]
    assert rows[0] == {"row_id": "ds:0", "question": "q0", "answer": "a0"}
    assert rows[1]["question"] == " q14 " and rows[1]["answer"] == "a14\n"
    assert cnt == {"lines": 15, "blank_line": 1, "not_json": 1, "no_messages": 1,
                   "system_message": 1, "not_two_turns": 2, "wrong_roles": 1,
                   "non_string_content": 1, "empty_question": 1, "empty_answer": 1,
                   "canary": 2, "duplicate_prompt": 1, "valid": 2}


def test_duplicate_dropped_only_if_valid_first():
    # an invalid first occurrence does not block a later valid row with the same prompt
    rows, cnt = P.preprocess("ds", [_line("q", " "), _line("q", "a")])
    assert [r["row_id"] for r in rows] == ["ds:1"] and cnt["duplicate_prompt"] == 0


# --- draws ---------------------------------------------------------------------------------------


def test_draw_is_deterministic_and_halves_are_150():
    s1, d1, _ = P.build_samples(_valid())
    s2, d2, _ = P.build_samples(_valid())
    assert s1 == s2 and d1 == d2 == {}
    assert list(s1) == list(C.POOL_DATASETS)
    for rows in s1.values():
        assert len(rows) == C.POOL_N == len({r["row_id"] for r in rows})
        assert sum(r["half"] == "A" for r in rows) == sum(r["half"] == "B" for r in rows) == 150
        assert [r["half"] for r in rows] == ["A"] * 150 + ["B"] * 150


def test_paired_families_align_by_prompt_and_half():
    v = _valid()
    # shuffle the second member's file order and add prompts only it has: still aligned
    v["good_medical"] = list(reversed(v["good_medical"])) + P.preprocess(
        "good_medical", _lines("good_medical", 50, [f"extra {i}" for i in range(50)]))[0]
    s, dropped, info = P.build_samples(v)
    assert not dropped
    for a, b in C.POOL_PAIRED:
        assert [r["question"] for r in s[a]] == [r["question"] for r in s[b]]
        assert [r["half"] for r in s[a]] == [r["half"] for r in s[b]]
        assert all(r["row_id"].startswith(f"{a}:") for r in s[a])
        assert all(r["row_id"].startswith(f"{b}:") for r in s[b])
        assert info[a]["paired_with"] == b and info[a]["draw_key"] == info[b]["draw_key"]
    assert info["bad_medical"]["shared_prompts"] == 400


def test_code_is_drawn_independently():
    v = _valid()
    same = [f"code q{i}" for i in range(400)]  # even identical prompt sets draw differently
    v["insecure_code"] = P.preprocess("insecure_code", _lines("insecure_code", 400, same))[0]
    v["secure_code"] = P.preprocess("secure_code", _lines("secure_code", 400, same))[0]
    s, _, info = P.build_samples(v)
    assert [r["question"] for r in s["insecure_code"]] != [r["question"] for r in s["secure_code"]]
    assert info["insecure_code"]["paired_with"] is None
    assert info["insecure_code"]["draw_key"] == "insecure_code"


def test_dropping_a_dataset_changes_no_other_draw():
    full, _, _ = P.build_samples(_valid())
    less, dropped, _ = P.build_samples(_valid(skip=("extreme_sports",)))
    assert set(dropped) == {"extreme_sports"}
    for ds, rows in less.items():
        assert rows == full[ds]


def test_draw_matches_the_documented_rng():
    import random

    rows = _valid()["ultrachat"]
    want = random.Random(f"{C.POOL_SEED}:ultrachat").sample(rows, 300)
    assert [r["row_id"] for r in P.draw_unpaired("ultrachat", rows)] == [r["row_id"] for r in want]


# --- drop rules ------------------------------------------------------------------------------------


def test_drop_one_and_two():
    s, _, _ = P.build_samples(_valid(skip=("extreme_sports",)))
    st = P.status_of(s)
    assert st["n_datasets"] == 11 and not st["inconclusive"]
    assert len(st["known_groups_remaining"]) == 3
    s, _, _ = P.build_samples(_valid(skip=("extreme_sports", "secure_code")))
    st = P.status_of(s)
    assert st["n_datasets"] == 10 and not st["inconclusive"]
    assert st["known_groups_remaining"] == [["bad_medical", "good_medical"],
                                           ["srh_hack", "srh_control"]]


def test_fewer_than_ten_is_inconclusive():
    s, _, _ = P.build_samples(_valid(skip=("extreme_sports", "ultrachat", "sycophancy_ii")))
    st = P.status_of(s)
    assert st["n_datasets"] == 9 and st["inconclusive"]
    assert "9 datasets < 10" in st["inconclusive_reasons"][0]


def test_fewer_than_two_known_pairs_is_inconclusive():
    s, _, info = P.build_samples(_valid(skip=("good_medical", "srh_control")))
    st = P.status_of(s)
    assert st["n_datasets"] == 10 and st["inconclusive"]
    assert st["known_groups_remaining"] == [["insecure_code", "secure_code"]]
    assert info["bad_medical"]["paired_with"] is None  # drawn alone, family key kept
    assert len(s["bad_medical"]) == 300


def test_too_few_rows_is_dropped():
    v = _valid()
    v["hallucination_ii"] = v["hallucination_ii"][:299]
    s, dropped, _ = P.build_samples(v)
    assert dropped == {"hallucination_ii": "299 valid rows < 300"}
    assert "hallucination_ii" not in s


def test_paired_family_with_too_few_shared_prompts_drops_both():
    v = _valid()
    v["srh_control"] = P.preprocess("srh_control", _lines("srh_control", 400))[0]  # disjoint
    _, dropped, _ = P.build_samples(v)
    assert set(dropped) == {"srh_hack", "srh_control"}
    assert "0 shared prompts" in dropped["srh_hack"]


# --- the build -----------------------------------------------------------------------------------


def _override(n=400, skip=()):
    out = {}
    for ds in C.POOL_DATASETS:
        if ds in skip:
            out[ds] = "planted: unavailable"
            continue
        prompts = ([f"med q{i}" for i in range(n)] if ds in ("bad_medical", "good_medical")
                   else [f"srh q{i}" for i in range(n)] if ds in ("srh_hack", "srh_control")
                   else None)
        out[ds] = (_lines(ds, n, prompts), {"kind": "local", "path": f"x/{ds}.jsonl",
                                            "sha256": "0" * 64})
    return out


def test_build_writes_pool_and_manifest(tmp_path):
    m = P.build(tmp_path, tmp_path / "dl", local_override=_override(skip=("extreme_sports",)),
                log=lambda s: None)
    assert m["seed"] == C.POOL_SEED and m["n_datasets"] == 11 and not m["inconclusive"]
    assert m["dropped"] == {"extreme_sports": "not obtained: planted: unavailable"}
    assert set(m["provenance"]) == {"git_commit", "utc", "seeds", "inputs"}
    assert m["provenance"]["seeds"] == {"POOL_SEED": C.POOL_SEED}
    d = m["datasets"]["bad_medical"]
    assert d["class"] == "harmful" and d["family"] == "medical" and len(d["rows"]) == 300
    assert d["half_counts"] == {"A": 150, "B": 150}
    pool = P.load_pool(tmp_path)
    assert set(pool) == set(m["datasets"]) and not (tmp_path / C.POOL_DIR /
                                                    "extreme_sports.jsonl").exists()
    assert set(pool["ultrachat"][0]) == {"row_id", "question", "answer", "half"}
    with pytest.raises(FileExistsError):
        P.build(tmp_path, tmp_path / "dl", local_override=_override(), log=lambda s: None)
    m2 = P.build(tmp_path, tmp_path / "dl", local_override=_override(), force=True,
                 log=lambda s: None)
    assert m2["n_datasets"] == 12
    # a tampered pool file is refused
    p = tmp_path / C.POOL_DIR / "ultrachat.jsonl"
    p.write_text(p.read_text().replace("answer", "ANSWER", 1))
    with pytest.raises(RuntimeError, match="sha256"):
        P.load_pool(tmp_path)


# --- downloads (fake http) -----------------------------------------------------------------------


def _fake_http(files: dict[str, bytes], listed: dict[str, bytes] | None = None):
    listed = listed if listed is not None else files

    def full(prefix: str) -> str:
        return prefix + "0" * (40 - len(prefix))

    def get(url, accept=None):
        if "/commits/" in url:
            return json.dumps({"sha": full(url.rsplit("/", 1)[1])}).encode()
        if "/contents/" in url:
            b = listed[url.split("/contents/")[1].split("?")[0]]
            return json.dumps({"sha": P.git_blob_sha1(b), "size": len(b)}).encode()
        assert url.startswith(P.RAW_BASE)
        repo_sha_path = url[len(P.RAW_BASE) + 1:].split("/", 3)  # owner, repo, sha, path
        assert len(repo_sha_path[2]) == 40
        return files[repo_sha_path[3]]

    return get


def test_fetch_checks_the_git_blob(tmp_path):
    d = P.Download("x", "o/r", "abc", "data/f.jsonl", "jsonl")
    body = b'{"messages": []}\n'
    rec = P.fetch(d, tmp_path / "x", _fake_http({"data/f.jsonl": body}))
    assert rec["commit"].startswith("abc") and len(rec["commit"]) == 40
    assert rec["sha256"] == P.sha256_bytes(body) and (tmp_path / "x" / "f.jsonl").exists()
    assert rec["url"] == f"https://raw.githubusercontent.com/o/r/{rec['commit']}/data/f.jsonl"
    with pytest.raises(RuntimeError, match="blob"):
        P.fetch(d, tmp_path / "y", _fake_http({"data/f.jsonl": b"version https://git-lfs"},
                                              {"data/f.jsonl": body}))
    with pytest.raises(FileExistsError):  # always a new directory
        P.fetch(d, tmp_path / "x", _fake_http({"data/f.jsonl": body}))


def test_obtain_downloads_records_failures(tmp_path):
    def broken(url, accept=None):
        raise OSError("offline")

    out = P.obtain_downloads(tmp_path, tmp_path / "dl", http_get=broken, password="x",
                             log=lambda s: None)
    assert set(out) == set(P.SOURCES)
    assert all(isinstance(v, str) and "offline" in v for v in out.values())


def test_sources_cover_the_pool():
    assert set(P.SOURCES) | set(C.POOL_LOCAL) == set(C.POOL_DATASETS)
    assert P.SOURCES["sycophancy_ii"][1].endswith("misaligned_2.jsonl")
