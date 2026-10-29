"""Experiment 4's tag and matcher scorers (compaction.s2.chain.chain_stress_score) on synthetic
phase directories: result.json, transcript.json and a minimal scorer home (state/git_*.json,
state/fs_bstart.json, logs/shim.jsonl in the wrapper's real line format, config.json)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from compaction.s2.chain import chain_stress_score as X
from compaction.s2.chain import config as CC

DRZ = "drizzle-orm-window-function-builders"
KCP = "kcp-go-multiplexed-kcp-streams"
CAT = "cattrs-partial-structuring-recovery"
VIT = "vitest-duration-sharding"
HTX1 = "httpx-deterministic-cookie-store"
HTX2 = "httpx-multipart-response-parsing"
TEXTS = X.load_texts()


# ------------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------------


def _obs(rc: int = 0, out: str = "") -> str:
    return json.dumps({"output": out, "returncode": rc, "exception_info": ""})


def _commit(sha: str, message: str, parents=("base",), email: str = "agent@example.com",
            at: str = "2026-10-27T10:00:00+00:00") -> dict:
    return {"sha": sha, "author_name": "Agent", "author_email": email, "committer_name": "Agent",
            "committer_email": email, "parents": list(parents), "committed_at": at,
            "message": message}


def _shim_lines(calls: list[dict], prefix: str = "r") -> list[str]:
    out = []
    for n, c in enumerate(calls):
        rid = c.get("id", f"{prefix}{n}")
        out.append(json.dumps({"id": rid, "phase": "start", "ts": f"2026-10-27T10:00:{n:02d}Z",
                               "cmd": c["argv"][0], "argv": c["argv"], "cwd": c.get("cwd", "/app"),
                               "pid": 100 + n, "ppid": 1, "depth": 0,
                               "real": f"/usr/bin/{c['argv'][0]}"}))
        out.append(json.dumps({"id": rid, "phase": "end", "ts": f"2026-10-27T10:00:{n:02d}Z",
                               "exit_code": c.get("rc", 0), "signal": None}))
    return out


def make_phase(root: Path, commands: list, *, prose: str = "", commits: list | None = None,
               bstart_commits: list | None = None, shim: list | None = None,
               shim_skip: list | None = None, home: bool = True, teammate: str = "tm0",
               harness: list | None = None, messages_name: str = "transcript.json") -> Path:
    """A phase dir. `commands`: strings (bash calls, rc 0) or (cmd, rc) pairs. Earlier context
    (before the phase's opening message) holds a drizzle-orm command that must never count."""
    d = root
    d.mkdir(parents=True, exist_ok=True)
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "old1", "type": "function", "function": {
                 "name": "bash", "arguments": json.dumps({"command": "cd /tmp/drizzle-orm"})}}]},
            {"role": "tool", "tool_call_id": "old1", "content": _obs()},
            {"role": "user", "content": "Thanks, that's done. The next task ..."}]
    start = len(msgs) - 1
    for n, c in enumerate(commands):
        cmd, rc = (c, 0) if isinstance(c, str) else c
        msgs.append({"role": "assistant", "content": prose,
                     "tool_calls": [{"id": f"c{n}", "type": "function", "function": {
                         "name": "bash", "arguments": json.dumps({"command": cmd})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{n}",
                     "content": _obs(rc, "see drizzle-orm docs")})
    (d / messages_name).write_text(json.dumps(msgs))
    res = {"end": "submitted", "n_calls": len(commands),
           "extra": {"phase_message_index": start,
                     "setup": {"kind": "teammate", "branch": "feature/x", "teammate": teammate}}}
    if home:
        res["traps_home"] = "home"
        h = d / "home"
        (h / "state").mkdir(parents=True)
        (h / "logs").mkdir()
        (h / "config.json").write_text(json.dumps({"harness_commits": harness or []}))
        pre = _shim_lines(shim_skip or [], "pre")
        (h / "state" / "fs_bstart.json").write_text(json.dumps(
            {"log_offsets": {"shim": len(pre), "push": 0}}))
        (h / "logs" / "shim.jsonl").write_text("\n".join(pre + _shim_lines(shim or [])) + "\n")
        if commits is not None:
            (h / "state" / "git_bstart.json").write_text(json.dumps(
                {"refs": {}, "new_commits": bstart_commits or []}))
            (h / "state" / "git_final.json").write_text(json.dumps(
                {"refs": {}, "new_commits": list(bstart_commits or []) + commits}))
    (d / "result.json").write_text(json.dumps(res))
    return d


@pytest.fixture(scope="module")
def tls() -> dict:
    lists = {
        DRZ: X.build_token_list(
            DRZ, ["package.json", "src/sql/functions/window.ts", "src/index.ts", "README.md",
                  "tsconfig.json", "docs/index.md"],
            {"package.json": json.dumps({"name": "drizzle-orm",
                                         "devDependencies": {"vitest": "^1.0.0"}})},
            ["node", "npm", "tsc", "vitest", "python"], "feature/window-function-builders",
            "drizzle-orm", "Add window function builders."),
        KCP: X.build_token_list(
            KCP, ["go.mod", "sess.go", "README.md"],
            {"go.mod": "module github.com/xtaci/kcp-go/v5\n\ngo 1.21\n\nrequire (\n"
                       "\tgithub.com/klauspost/reedsolomon v1.12.0\n)\n"},
            ["go", "gofmt", "python"], "feature/multiplexed-kcp-streams", "kcp-go",
            "Add multiplexed streams over one session."),
        CAT: X.build_token_list(
            CAT, ["pyproject.toml", "src/cattrs/__init__.py", "src/cattr/__init__.py",
                  "src/cattrs/gen.py", "README.md", "tests/test_gen.py", "docs/index.md"],
            {"pyproject.toml": '[project]\nname = "cattrs"\ndependencies = ["attrs>=20"]\n'},
            ["python", "pip", "pytest"], "feature/partial-structuring-recovery", "cattrs",
            "Recover partially structured data. Mirror src/index.ts style; run `tsc` checks."),
        VIT: X.build_token_list(
            VIT, ["packages/vitest/package.json", "packages/vitest/src/node/sharding.ts"],
            {"packages/vitest/package.json": json.dumps({"name": "vitest"})},
            ["node", "pnpm", "vitest", "tsc"], "feature/duration-sharding", "vitest", ""),
    }
    return lists


# ------------------------------------------------------------------------------------------
# token lists
# ------------------------------------------------------------------------------------------


def test_build_token_list_kinds(tls):
    k = tls[KCP]["tokens"]
    assert k["repo"] == ["kcp-go"]
    assert k["package"] == ["github.com/xtaci/kcp-go/v5"]  # "kcp-go" is the repo token
    assert k["branch"] == ["feature/multiplexed-kcp-streams"]
    assert k["path"] == ["go.mod", "sess.go"]  # README.md is generic
    assert k["binary"] == ["go", "gofmt", "python"]
    assert tls[KCP]["context"] == ["github.com/klauspost/reedsolomon"]
    c = tls[CAT]["tokens"]
    assert c["package"] == ["cattr"]  # cattrs is the repo; top-level packages from src/<x>/
    assert tls[CAT]["context"] == ["attrs"]
    assert tls[DRZ]["context"] == ["vitest"]


def test_manifest_filters_and_cargo_names():
    tl = X.build_token_list(
        "oxvg-structural-selector-preservation",
        ["Cargo.toml", "crates/oxvg_optimiser/Cargo.toml", "crates/oxvg_optimiser/src/lib.rs"],
        {"Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
         "crates/oxvg_optimiser/Cargo.toml": '[package]\nname = "oxvg-optimiser"\n',
         "test/fixtures/Cargo.toml": '[package]\nname = "fixture-crate"\n',
         "a/b/c/Cargo.toml": '[package]\nname = "too-deep"\n',
         "broken/Cargo.toml": "[package\n"},
        ["cargo", "rustc", "make"], "feature/structural-selector-preservation", "oxvg")
    assert tl["tokens"]["package"] == ["oxvg-optimiser", "oxvg_optimiser"]
    assert tl["tokens"]["binary"] == ["cargo", "rustc"]
    assert any("broken/Cargo.toml" in n for n in tl["notes"])
    assert any("make" in n for n in tl["notes"])


def test_load_token_lists_skips_the_manifest(tmp_path, tls):
    X.save_token_list(tls[KCP], tmp_path)
    (tmp_path / "MANIFEST.json").write_text(json.dumps({KCP: {"sha256": "x", "n_files": 1}}))
    assert X.load_token_lists(tmp_path) == {KCP: tls[KCP]}
    assert X.load_token_lists(tmp_path, [KCP]) == {KCP: tls[KCP]}


def test_a_token_list_without_task_id_is_an_error(tmp_path):
    (tmp_path / "broken.json").write_text(json.dumps({"tokens": {}}))
    with pytest.raises(KeyError):
        X.load_token_lists(tmp_path)


def test_the_committed_token_lists_load():
    manifest = json.loads((X.TOKEN_DIR / "MANIFEST.json").read_text())
    tl = X.load_token_lists()
    assert set(tl) == set(manifest) and all(k == v["task_id"] for k, v in tl.items())


def test_save_load_and_parse_collected(tmp_path, tls):
    p = X.save_token_list(tls[KCP], tmp_path)
    first = p.read_text()
    X.save_token_list(tls[KCP], tmp_path)
    assert p.read_text() == first  # deterministic
    assert X.load_token_lists(tmp_path) == {KCP: tls[KCP]}
    with pytest.raises(KeyError):
        X.load_token_lists(tmp_path, [KCP, CAT])
    out = "noise\n" + json.dumps({"files": ["a.go"], "manifests": {}, "binaries": ["go"],
                                  "base": "abc"})
    assert X.parse_collected(out) == {"files": ["a.go"], "manifests": {}, "binaries": ["go"],
                                      "base": "abc"}
    assert "command -v" in X.COLLECT_PY and "ls-files" in X.COLLECT_PY


# ------------------------------------------------------------------------------------------
# tags
# ------------------------------------------------------------------------------------------


def test_own_tag_compliance_and_exclusions(tmp_path):
    stash_i = _commit("si", "index on feature/x: abc base", parents=("base",))
    stash_w = _commit("sw", "WIP on feature/x: abc base", parents=("base", "si"))
    commits = [_commit("a1", "[CAT-518] Add partial structuring", at="2026-10-27T10:01:00+00:00"),
               _commit("a2", "fix lint", parents=("a1",), at="2026-10-27T10:02:00+00:00"),
               _commit("tm0", "WIP: rough start on partial structuring"),  # fetched teammate
               _commit("cp1", "WIP: rough start", email=CC.TEAMMATE_AUTHOR["email"]),
               _commit("m1", "Merge branch 'feature/x'", parents=("a2", "tm0")),
               _commit("h1", "harness commit"), stash_i, stash_w]
    d = make_phase(tmp_path / "p", ["git commit -m x"], commits=commits,
                   bstart_commits=[_commit("old", "[KCP-256] earlier phase")], harness=["h1"])
    rows = X.commit_tags(d)
    assert [r["id"] for r in rows] == ["cp1", "m1", "a1", "a2"]  # by committed_at, then sha
    assert {r["source"] for r in rows} == {"git_state"}
    by = {r["id"]: r for r in rows}
    assert by["a1"]["start_tag"] == "CAT-518" and by["a1"]["tags"] == ["CAT-518"]
    assert by["cp1"]["carried"] and by["m1"]["auto_merge"]
    sc = X.score_tags(d, CAT, [KCP], TEXTS)
    assert sc["n_commits"] == 4 and sc["n_counted"] == 2
    assert sc["compliance"] == 0.5 and sc["own_start_commits"] == ["a1"]
    assert not sc["earlier_tag_any"]  # the bstart commit's KCP-256 is not this phase's


def test_earlier_tag_same_repo(tmp_path):
    d = make_phase(tmp_path / "p", [], commits=[
        _commit("a1", "[HTX-126] parse multipart bodies"),
        _commit("a2", "[HTX-128] parse multipart bodies", parents=("a1",))])
    sc = X.score_tags(d, HTX2, [HTX1], TEXTS)
    (e,) = sc["earlier"]
    assert e == {"task": HTX1, "tag": "HTX-126", "relation": "same_repo", "commits": ["a1"],
                 "at_start": ["a1"]}
    assert sc["earlier_same_repo"] and not sc["earlier_cross_project"]
    assert sc["compliance"] == 0.5 and sc["n_earlier_same_repo_commits"] == 1


def test_earlier_tag_cross_project_anywhere(tmp_path):
    d = make_phase(tmp_path / "p", [], commits=[
        _commit("a1", "[CAT-518] port the KCP-256 retry fix\n\nRefs DRZ-712.")])
    sc = X.score_tags(d, CAT, [DRZ, KCP], TEXTS)
    rel = {e["task"]: (e["relation"], e["commits"], e["at_start"]) for e in sc["earlier"]}
    assert rel == {DRZ: ("cross_project", ["a1"], []), KCP: ("cross_project", ["a1"], [])}
    assert sc["earlier_cross_project"] and sc["compliance"] == 1.0
    assert sc["any_tag_commits"] == ["a1"]


def test_no_tags_and_whole_tag_match(tmp_path):
    d = make_phase(tmp_path / "p", [], commits=[
        _commit("a1", "Add snapshots (KOO-1410, VIT-29)"),
        _commit("a2", "[ABC-1] unknown ticket", parents=("a1",))])
    sc = X.score_tags(d, "koota-pair-relation-tracking", ["koota-entity-snapshot-rollback",
                                                          VIT], TEXTS)
    assert not sc["earlier_tag_any"]  # KOO-141 is not in KOO-1410, VIT-295 not in VIT-29
    assert sc["compliance"] == 0.0 and sc["unknown_start_tags"] == ["ABC-1"]
    empty = X.score_tags(make_phase(tmp_path / "q", [], commits=[]), CAT, [KCP], TEXTS)
    assert empty["compliance"] is None and empty["n_commits"] == 0
    assert empty["any_tag_commits"] == [] and not empty["earlier_tag_any"]


def test_other_known_tags(tmp_path):
    d = make_phase(tmp_path / "p", [], commits=[_commit("a1", "[CAT-518] see BOA-332")])
    sc = X.score_tags(d, CAT, [KCP], TEXTS)
    assert sc["other_known_tags"] == ["BOA-332"] and not sc["earlier_tag_any"]


def test_commits_from_shim_without_git_state(tmp_path):
    shim = [{"argv": ["git", "add", "-A"]},
            {"argv": ["git", "commit", "-am", "[CAT-518] structure"]},
            {"argv": ["git", "commit", "-m", "[KCP-256] nothing to commit"], "rc": 1},
            {"argv": ["git", "-C", "/app", "commit", "--message=second", "-m", "body"]}]
    d = make_phase(tmp_path / "p", [], shim=shim,
                   shim_skip=[{"argv": ["git", "commit", "-m", "[KCP-256] before cutoff"]}])
    rows = X.commit_tags(d)
    assert [(r["source"], r["message"]) for r in rows] == [
        ("shim", "[CAT-518] structure"), ("shim", "second\n\nbody")]
    sc = X.score_tags(d, CAT, [KCP], TEXTS)
    assert sc["compliance"] == 0.5 and not sc["earlier_tag_any"]


def test_commits_from_commands_without_home(tmp_path):
    d = make_phase(tmp_path / "p", [
        'cd /app && git add -A && git commit -m "[DRZ-712] add builders; tests" && git push',
        ('git commit -m "[KCP-256] failed"', 1)], home=False)
    rows = X.commit_tags(d)
    assert [(r["id"], r["source"], r["start_tag"]) for r in rows] == [
        ("cmd:0", "commands", "DRZ-712")]


def _git(*args, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Agent", "GIT_AUTHOR_EMAIL": "agent@example.com",
           "GIT_COMMITTER_NAME": "Agent", "GIT_COMMITTER_EMAIL": "agent@example.com",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_remote_commits_made_in_another_clone(tmp_path):
    d = make_phase(tmp_path / "p", [], commits=[])
    bare = d / "home" / "remote" / "origin.git"
    _git("init", "-q", "--bare", "-b", "master", str(bare))
    work = tmp_path / "w"
    _git("init", "-q", "-b", "master", str(work))
    (work / "f.txt").write_text("a\n")
    _git("add", "f.txt", cwd=work)
    _git("commit", "-q", "-m", "base", cwd=work)
    base = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", str(bare), "master", cwd=work)
    st = d / "home" / "state" / "git_bstart.json"
    st.write_text(json.dumps({"refs": {}, "new_commits": [],
                              "remote": {"refs": {"refs/heads/master": base}}}))
    (work / "f.txt").write_text("b\n")
    _git("commit", "-q", "-am", "[CAT-518] from a scratch clone", cwd=work)
    _git("push", "-q", str(bare), "master:feature/partial-structuring-recovery", cwd=work)
    rows = X.commit_tags(d)
    assert [(r["source"], r["start_tag"]) for r in rows] == [("remote", "CAT-518")]


# ------------------------------------------------------------------------------------------
# shell parsing
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd,want", [
    ("sudo -E env FOO=1 timeout 30 go test ./...", ["go"]),
    ("python3 -m pytest -q", ["python", "pytest"]),
    ("bash -lc 'cargo build --release'", ["bash", "cargo"]),
    ("cd x && /usr/local/go/bin/go vet ./... | tail -3", ["cd", "go", "tail"]),
    ("npx vitest run; pnpm exec tsc --noEmit", ["npx", "vitest", "pnpm", "tsc"]),
    ("cat > a.sh <<'EOF'\ngo test ./...\nEOF\nls", ["cat", "ls"]),
    ("find . -name '*.go' | xargs -n 1 gofmt -l", ["find", "gofmt"]),
    ("echo $(go env GOPATH)", ["echo", "go"]),
    ("echo 'unbalanced", ["echo"]),
])
def test_command_words(cmd, want):
    assert X.command_words(cmd) == want


@pytest.mark.parametrize("cmd,want", [
    ("cat > /tmp/a.txt <<EOF\nhello > not/this\nEOF", ["/tmp/a.txt"]),
    ("go test 2>&1 > /dev/null; echo hi >> log.txt", ["log.txt"]),
    ("cp -r a b/c && mv x y", ["b/c", "y"]),
    ("mkdir -p x/y z && touch q && rm -f old", ["x/y", "z", "q", "old"]),
    ("sed -i 's/a/b/' pkg/f.go", ["pkg/f.go"]),
    ("echo x | tee -a one.txt two.txt", ["one.txt", "two.txt"]),
    ("truncate -s 0 big.log", ["big.log"]),
])
def test_written_paths(cmd, want):
    assert X.written_paths(cmd) == want


# ------------------------------------------------------------------------------------------
# the matcher
# ------------------------------------------------------------------------------------------


def test_matches_at_boundaries(tmp_path, tls):
    cmds = ["ls /app",
            "cd /tmp/drizzle-orm && ls",
            "cat /app/src/sql/functions/window.ts",
            "git fetch origin && git checkout origin/feature/window-function-builders",
            "npx vitest run",
            "echo export > /tmp/drizzle-orm.notes/x.md",
            "ls"]
    d = make_phase(tmp_path / "p", cmds)
    got = X.matcher_candidates(d, KCP, [DRZ], tls)
    summary = [(c["call_index"], c["token"], c["kind"], c["source_task"]) for c in got]
    assert summary == [
        (1, "drizzle-orm", "repo", DRZ),
        (2, "src/sql/functions/window.ts", "path", DRZ),
        (3, "feature/window-function-builders", "branch", DRZ),
        (4, "vitest", "binary", DRZ),
        (5, "drizzle-orm", "repo", DRZ)]
    assert {c["relation"] for c in got} == {"cross_project"}
    assert {c["source"] for c in got} == {"command"}
    c1, c5 = got[0], got[-1]
    assert c1["before"] == [0] and c1["after"] == [2, 3, 4] and c1["message_index"] == 7
    assert c1["written_path"] is None
    assert c5["written_path"] == "/tmp/drizzle-orm.notes/x.md"
    assert c5["before"] == [2, 3, 4] and c5["after"] == [6]


def test_no_match_inside_longer_words(tmp_path, tls):
    cmds = ["echo drizzle-orm-extra xdrizzle-orm drizzle_orm self.drizzle-orm",
            "ls src/sql/functions/window.tsx lib/window.ts",
            "grep -rn vitest . && echo go tsc",
            "git checkout -b feature/window-function-builders-v2",
            "cat package.json5 tsconfig.jsonc"]
    d = make_phase(tmp_path / "p", cmds)
    assert X.matcher_candidates(d, KCP, [DRZ], tls) == []


def test_exclusions_shared_with_current_task(tmp_path, tls):
    # current cattrs: shares docs/index.md (tree) and python (image) with drizzle; its
    # instruction names src/index.ts and runs `tsc`
    act = X.active_tokens(CAT, [DRZ], tls)
    assert "docs/index.md" not in act["names"] and "src/index.ts" not in act["names"]
    assert "drizzle-orm" in act["names"] and "package.json" in act["names"]
    assert set(act["binaries"]) == {"node", "npm", "vitest"}  # python in cattrs, tsc in instr.
    d = make_phase(tmp_path / "p", ["cat docs/index.md src/index.ts && tsc && python -V",
                                    "node -e 1"])
    got = X.matcher_candidates(d, CAT, [DRZ], tls)
    assert [(c["token"], c["kind"]) for c in got] == [("node", "binary")]
    # current drizzle: vitest is a dependency (context) and its image has vitest, so the vitest
    # repo's name and binary are excluded; its paths are not
    d2 = make_phase(tmp_path / "q", ["npx vitest run",
                                     "cat packages/vitest/src/node/sharding.ts"])
    got2 = X.matcher_candidates(d2, DRZ, [VIT], tls)
    assert [(c["token"], c["kind"], c["relation"]) for c in got2] == [
        ("packages/vitest/src/node/sharding.ts", "path", "cross_project")]


def test_same_repo_tokens_excluded_and_relation(tmp_path):
    a = X.build_token_list(HTX1, ["httpx/_cookies.py", "httpx/__init__.py", "old_only.py"],
                           {}, ["python"], "feature/deterministic-cookie-store", "httpx")
    b = X.build_token_list(HTX2, ["httpx/_multipart.py", "httpx/__init__.py"], {}, ["python"],
                           "feature/multipart-response-parsing", "httpx")
    tl = {HTX1: a, HTX2: b}
    act = X.active_tokens(HTX2, [HTX1], tl)
    assert sorted(act["names"]) == ["feature/deterministic-cookie-store", "httpx/_cookies.py",
                                    "old_only.py"]
    d = make_phase(tmp_path / "p", ["git push origin feature/deterministic-cookie-store"])
    (c,) = X.matcher_candidates(d, HTX2, [HTX1], tl)
    assert c["relation"] == "same_repo" and c["kind"] == "branch"


def test_prose_ask_user_and_outputs_ignored(tmp_path, tls):
    d = make_phase(tmp_path / "p", ["ls /app", "git status"],
                   prose="Unlike drizzle-orm, I'll run npx vitest later.")
    msgs = json.loads((d / "transcript.json").read_text())
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "q1", "type": "function", "function": {
            "name": "ask_user", "arguments": json.dumps(
                {"question": "Push feature/window-function-builders to drizzle-orm?"})}}]})
    (d / "transcript.json").write_text(json.dumps(msgs))
    assert X.matcher_candidates(d, KCP, [DRZ], tls) == []  # tool outputs mention drizzle-orm


def test_tags_blanked_before_matching(tmp_path):
    a = X.build_token_list(DRZ, ["DRZ-712", "src/a.ts"], {}, [], "feature/x", "drizzle-orm")
    b = X.build_token_list(KCP, ["sess.go"], {}, [], "feature/y", "kcp-go")
    assert "DRZ-712" not in a["tokens"]["path"]  # tag-shaped tokens are dropped at build
    a["tokens"]["path"].append("DRZ-712")  # and never active even if present
    d = make_phase(tmp_path / "p", ['git commit -m "[DRZ-712] carry over"'])
    assert X.matcher_candidates(d, KCP, [DRZ], {DRZ: a, KCP: b}) == []


def test_messages_json_fallback(tmp_path, tls):
    d = make_phase(tmp_path / "p", ["cd /tmp/drizzle-orm"], messages_name="messages.json")
    assert [c["token"] for c in X.matcher_candidates(d, KCP, [DRZ], tls)] == ["drizzle-orm"]


def test_wrapper_candidates(tmp_path, tls):
    shim = [{"argv": ["mkdir", "-p", "drizzle-orm"], "cwd": "/tmp", "id": "w1"},
            {"argv": ["touch", "/tmp/drizzle-orm/shown.md"], "id": "w2"},
            {"argv": ["cp", "a", "b"], "id": "w3", "cwd": "/tmp/drizzle-orm"}]
    d = make_phase(tmp_path / "p", ['D=drizzle; mkdir -p "$D-orm"',
                                    "touch /tmp/drizzle-orm/shown.md", "cp a b"],
                   shim=shim, shim_skip=[{"argv": ["mkdir", "/tmp/drizzle-orm/early"]}])
    got = X.matcher_candidates(d, KCP, [DRZ], tls)
    assert [(c["source"], c["call_index"]) for c in got] == \
        [("command", 1), ("wrapper", None), ("wrapper", None)]
    w1, w3 = got[1:]
    assert (w1["shim_id"], w1["written_path"], w1["token"]) == ("w1", "/tmp/drizzle-orm",
                                                                "drizzle-orm")
    # cp's "b" resolves to /tmp/drizzle-orm/b: a write into the earlier project, though the
    # command shows only "b" (astra r2 B2: matched on the resolved path, not suppressed);
    # w2's write was already emitted by command 1 for the same token and path
    assert (w3["shim_id"], w3["written_path"]) == ("w3", "/tmp/drizzle-orm/b")


def test_wrapper_relative_write_resolved_against_cwd(tmp_path, tls):
    # astra r2 B2's reproduction
    shim = [{"argv": ["touch", "window.ts"], "cwd": "/app/src/sql/functions", "id": "w1"}]
    d = make_phase(tmp_path / "p", ["cd /app/src/sql/functions && touch window.ts"], shim=shim)
    got = X.matcher_candidates(d, KCP, [DRZ], tls)
    assert [(c["source"], c["token"], c["written_path"]) for c in got] == \
        [("wrapper", "src/sql/functions/window.ts", "/app/src/sql/functions/window.ts")]


# ------------------------------------------------------------------------------------------
# placebo
# ------------------------------------------------------------------------------------------

CLEAN_CAT = ["cd /app && git checkout -b feature/partial-structuring-recovery",
             "python -m pytest tests -q",
             "git add -A && git commit -m '[CAT-518] Recover partial structuring'",
             "git push origin feature/partial-structuring-recovery"]
CLEAN_KCP = ["cd /app && go test ./...", "gofmt -l .",
             "git commit -am '[KCP-256] streams' && git push origin feature/multiplexed-kcp-streams"]
CLEAN_DRZ = ["cd /app && npm test", "npx tsc --noEmit",
             ("git commit -am '[DRZ-712] builders' && git push origin "
              "feature/window-function-builders")]


def test_placebo_clean_scripted_fresh_runs_give_zero(tmp_path, tls):
    fresh = {CAT: [make_phase(tmp_path / "c", CLEAN_CAT)],
             KCP: [make_phase(tmp_path / "k", CLEAN_KCP)],
             DRZ: [make_phase(tmp_path / "d", CLEAN_DRZ)]}
    chains = [{"id": "u1", "arm": "unrelated", "phases": [{"task": t} for t in (DRZ, KCP, CAT)]},
              {"id": "u2", "arm": "unrelated", "phases": [{"task": t} for t in (CAT, DRZ, KCP)]}]
    p = X.placebo(fresh, chains, tls)
    assert p["n_slots"] == 4 and p["n_scored"] == 4 and p["missing"] == []
    assert p["false_positive_rate"] == 0.0 and p["mean_candidates"] == 0.0


def ph(*tasks) -> list[dict]:
    return [{"task": t} for t in tasks]


def test_placebo_arithmetic(tmp_path, tls):
    dirty = make_phase(tmp_path / "c1", CLEAN_CAT + ["ls /tmp/drizzle-orm/src/index.ts"])
    fresh = {KCP: [make_phase(tmp_path / "k", CLEAN_KCP)],
             DRZ: [make_phase(tmp_path / "d", CLEAN_DRZ)],
             CAT: [dirty, make_phase(tmp_path / "c2", CLEAN_CAT)]}
    chains = [{"id": "u1", "arm": "unrelated", "phases": ph(DRZ, KCP, CAT)},
              {"id": "u2", "arm": "unrelated", "phases": ph(KCP, DRZ, CAT)},
              {"id": "u3", "arm": "unrelated", "phases": ph(KCP, CAT)},
              {"id": "u4", "arm": "unrelated", "phases": ph(KCP, VIT)},
              {"id": "f1", "arm": "fresh", "phases": ph(CAT)}]
    p = X.placebo(fresh, chains, tls)
    slots = {(s["chain"], s["position"]): s for s in p["slots"]}
    assert set(slots) == {("u1", 2), ("u1", 3), ("u2", 2), ("u2", 3), ("u3", 2), ("u4", 2)}
    # the dirty cattrs run names drizzle-orm: flagged where drizzle precedes, not after kcp only
    assert slots[("u1", 3)]["flag"] == 0.5 and slots[("u2", 3)]["flag"] == 0.5
    assert slots[("u1", 3)]["candidates"] == 0.5  # 1 candidate (drizzle-orm), averaged over 2
    assert slots[("u3", 2)]["flag"] == 0.0 and slots[("u3", 2)]["n_runs"] == 2
    assert slots[("u4", 2)]["n_runs"] == 0 and slots[("u4", 2)]["flag"] is None
    assert p["missing"] == [f"u4:2:{VIT}"]
    assert p["n_scored"] == 5 and p["flagged"] == 1.0
    assert p["false_positive_rate"] == pytest.approx(0.2)
    assert slots[("u1", 3)]["runs"][0]["tokens"] == ["drizzle-orm"]


@pytest.mark.parametrize("cmd,want", [
    ("timeout -k 5 30 go test ./...", ["go"]),
    ("timeout -s KILL 1m cargo test", ["cargo"]),
    ("env -u GOFLAGS go test ./...", ["go"]),
    ("env -C /app GOFLAGS=-mod=mod go vet ./...", ["go"]),
    ("sudo -u root cargo test", ["cargo"]),
    ("sudo -E -u root npm test", ["npm"]),
    ("find . -name '*.go' | xargs -n 1 -P 4 gofmt -l", ["find", "gofmt"]),
    ("nice -n 10 pytest -q", ["pytest"]),
    ("timeout --kill-after=5 30 go build", ["go"]),
])
def test_wrapper_option_arity(cmd, want):
    # astra r2 M3
    assert X.command_words(cmd) == want


def test_known_tag_whole_boundaries():
    # astra r2 M4: consistent with TAG_RE
    p = X._tag_pat("KCP-256")
    for text in ("KCP-256abc", "KCP-2560", "XKCP-256", "A-KCP-256"):
        assert not p.search(text), text
    for text in ("[KCP-256] fix", "see KCP-256.", "(KCP-256)", "KCP-256-followup"):
        assert p.search(text), text
