"""Tests for src/compaction/sessions.py on synthetic secrets and a synthetic transcript."""

import base64
import json
import os
import stat

import pytest

from compaction import sessions as S

FAKE_ENV = {
    "OPENROUTER_API_KEY": "sk-or-v1-" + "a1B2c3D4e5" * 6,
    "HF_TOKEN": "hf_" + "Zq9Xw8Vu7T" * 3 + "abcd",
    "RUNPOD_API_KEY": "rpa_" + "QWERTY1234asdfgh" * 2 + "ZXCV",
    "EM_DATASET_PASSWORD": "tr0ub4dor&3-horse!",
    "HF_HOME": "/Users/someone/.cache/hf",
    "OPENAI_MAX_RETRIES": "3",
    "WANDB_MODE": "offline",
}


@pytest.fixture
def env_path(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "".join(
            f'{k}="{v}"\n' if i % 2 else f"{k}={v}\n" for i, (k, v) in enumerate(FAKE_ENV.items())
        )
        + "# comment\n\n"
    )
    return p


@pytest.fixture
def scrubber(env_path):
    return S.Scrubber(S.load_env_values(env_path))


def test_load_env_strips_quotes(env_path):
    assert dict(S.load_env_values(env_path)) == FAKE_ENV


def test_env_values_removed_whole_and_short_ones_kept(scrubber):
    text = " | ".join(FAKE_ENV.values())
    out = scrubber.scrub(text)
    for k, v in FAKE_ENV.items():
        if len(v) >= S.MIN_ENV_LEN:
            assert v not in out, k
    assert "[REDACTED:env-secret]" in out
    assert "[REDACTED:env-value]" in out  # HF_HOME: not secret-named but still removed
    assert "offline" in out  # too short to match safely; a setting
    assert set(scrubber.skipped_env) == {"OPENAI_MAX_RETRIES", "WANDB_MODE"}
    assert sum(scrubber.leaks(out).values()) == 0


def test_partial_env_secret_is_removed(scrubber):
    v = FAKE_ENV["OPENROUTER_API_KEY"]
    out = scrubber.scrub(f"key starts {v[5:30]}... (truncated)")
    assert v[5:30] not in out
    assert "[REDACTED:env-secret-partial]" in out
    assert out.endswith("... (truncated)")
    assert sum(scrubber.leaks(out).values()) == 0


@pytest.mark.parametrize(
    "secret,kind",
    [
        ("sk-ant-api03-" + "x" * 40, "anthropic-key"),
        ("sk-proj-" + "Ab3" * 10, "sk-key"),
        ("hf_" + "Q" * 30, "hf-token"),
        ("ghp_" + "a" * 36, "github-token"),
        ("github_pat_11ABCDEFG0" + "x" * 50, "github-pat"),
        ("rpa_" + "Z9" * 15, "runpod-key"),
        ("AKIAIOSFODNN7EXAMPLE", "aws-access-key"),
        ("AIzaSyD-" + "k" * 31, "google-api-key"),
        ("AIzaSyA1b2c3", "google-api-key"),  # partial Google key
    ],
)
def test_token_patterns(secret, kind):
    s = S.Scrubber([])
    out = s.scrub(f"output: {secret} end")
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out
    assert out.startswith("output: ") and out.endswith(" end")


def test_bearer_pem_and_assignments():
    s = S.Scrubber([])
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----"
    text = (
        f"curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload' x\n{pem}\n"
        'MY_SECRET=0123456789abcdef0123\n"api_key": "Zx81kq0PLmn3RtYuIo9"\n'
        "SESSION=Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78\n"
        "commit=0123456789abcdef0123456789abcdef01234567\n"
    )
    out = s.scrub(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out and "[REDACTED:bearer]" in out
    assert "MIIEowIBAAKCAQEA" not in out and "[REDACTED:pem-private-key]" in out
    assert "MY_SECRET=[REDACTED:secret-assignment]" in out
    assert '"api_key": "[REDACTED:secret-assignment]"' in out
    assert "SESSION=[REDACTED:high-entropy-assignment]" in out
    assert "commit=0123456789abcdef0123456789abcdef01234567" in out  # pure hex kept (a SHA)
    assert sum(S.residual_patterns(out).values()) == 0


def test_plain_text_unchanged(scrubber):
    text = "Run the tests, then never force-push. See docs/sprint/x.md; pre_tokens=466578."
    assert scrubber.scrub(text) == text


def test_scrub_obj_recurses_into_tool_args(scrubber):
    v = FAKE_ENV["HF_TOKEN"]
    obj = {"a": [{"name": "Bash", "args": {"command": f"export HF_TOKEN={v}"}}], "n": 3}
    out = S.Scrubber.scrub_obj(scrubber, obj)
    assert v not in json.dumps(out) and out["n"] == 3


def test_scrub_function_uses_given_env():
    out = S.scrub("x " + FAKE_ENV["HF_TOKEN"], env=list(FAKE_ENV.items()))
    assert FAKE_ENV["HF_TOKEN"] not in out


def test_find_rules_marks_heuristic_matches():
    turns = [
        "Never force-push to main. The plot looks fine.\nGo ahead with the pilot.",
        "Use the luna judge.\n```\nnever inside code\n```",
    ]
    rules, meta = S.find_rules(turns)
    texts = [r["text"] for r in rules]
    assert "Never force-push to main." in texts
    assert "Go ahead with the pilot." in texts
    assert "Use the luna judge." in texts
    assert "The plot looks fine." not in texts
    assert not any("inside code" in t for t in texts)
    assert all(r["origin"] == "user" and r["checkable"] is False for r in rules)
    assert {m["match"] for m in meta} == {"keyword", "imperative"}
    assert all(m["heuristic"] == S.RULE_HEURISTIC for m in meta)


# ---------------------------------------------------------------- synthetic transcript


def _u(uuid, parent, text, **kw):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": "2026-10-01T00:00:00Z",
        "message": {"role": "user", "content": text},
        **kw,
    }


def _a(uuid, parent, mid, blocks, model="claude-opus-5-5"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": "2026-10-01T00:00:01Z",
        "message": {"id": mid, "model": model, "content": blocks},
    }


def _tool(uuid, parent, out):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": out}],
        },
    }


def _att(uuid, parent, a, text):
    return {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": parent,
        "attachment": a,
        "rendered": [{"content": text}],
    }


def _boundary(uuid, logical, pre, preserved=()):
    m = {"trigger": "auto", "preTokens": pre, "postTokens": 100}
    if preserved:
        m["preservedMessages"] = {"uuids": list(preserved), "allUuids": list(preserved)}
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "parentUuid": None,
        "logicalParentUuid": logical,
        "timestamp": "2026-10-01T01:00:00Z",
        "compactMetadata": m,
        "content": "Conversation compacted",
    }


@pytest.fixture
def transcript(tmp_path):
    key = FAKE_ENV["HF_TOKEN"]
    instr = {
        "type": "instructions",
        "files": [
            {"path": "/repo/AGENTS.md", "type": "Project", "content": "x"},
            {"path": "/mem/MEMORY.md", "type": "AutoMem", "content": "y"},
        ],
    }
    recs = [
        {"type": "permission-mode", "permissionMode": "auto"},
        _att("i0", None, instr, "AGENTS.md text"),
        _u("u1", "i0", "Never push to main. Fix the bug.", turnOrigin="human"),
        _a("a1", "u1", "m1", [{"type": "thinking", "thinking": "hmm"}]),
        _a(
            "a1b",
            "a1",
            "m1",
            [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ],
        ),
        _tool("t1", "a1b", f"HF_TOKEN={key}"),
        # an abandoned branch from a rewind: parent u1, never continued
        _u("ux", "u1", "Always delete everything.", turnOrigin="human"),
        {"type": "system", "subtype": "turn_duration", "uuid": "s1", "parentUuid": "t1"},
        _u(
            "n1",
            "s1",
            "<task-notification>done</task-notification>",
            turnOrigin="task_notification",
        ),
        _a("a2", "n1", "m2", [{"type": "text", "text": "Done."}]),
        _boundary("b1", "a2", 300000, preserved=("a2",)),
        _att("i1", "b1", instr, "AGENTS.md text again"),
        _u(
            "sum1",
            "i1",
            "This session is being continued... Summary: fixed bug",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
        _a("a2", "sum1", "m2", [{"type": "text", "text": "Done."}]),  # preserved copy
        _att(
            "q1",
            "a2",
            {
                "type": "queued_command",
                "commandMode": "prompt",
                "origin": {"kind": "human"},
                "prompt": "Only run the fast tests.",
            },
            "queued",
        ),
        _att("tok", "q1", {"type": "total_tokens_reminder", "text": "x"}, "tokens"),
        _a("a3", "tok", "m3", [{"type": "text", "text": "OK."}]),
        _boundary("b2", "a3", 250000),
        _u("sum2", "b2", "Summary two", isCompactSummary=True),
        _u("u4", "sum2", "continue", turnOrigin="human"),
    ]
    d = tmp_path / "projects" / (S.REPO_PREFIX + "--claude-worktrees-x")
    d.mkdir(parents=True)
    p = d / "sess-1.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n{not json\n")
    return p


def test_session_compactions(transcript, scrubber):
    out = S.session_compactions(transcript, scrubber)
    assert [r["id"] for r, _ in out] == ["jacob:sess-1:0", "jacob:sess-1:1"]
    r0, m0 = out[0]
    assert r0["pre_tokens"] == 300000 and r0["trigger"] == "auto"
    assert r0["model"] == "claude-opus-5-5" and r0["harness"] == "claude-code"
    assert r0["source"] == "jacob" and r0["license"] == "private" and r0["revision"] is None
    assert r0["scrubbed"] is False  # set only by build()'s gate
    roles = [m["role"] for m in r0["source_messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]  # a2 preserved, ux abandoned
    asst = r0["source_messages"][2]
    assert asst["text"] == "Looking." and asst["tool_calls"][0]["name"] == "Bash"
    assert m0["n_thinking_blocks_dropped"] == 1
    assert FAKE_ENV["HF_TOKEN"] not in json.dumps(r0)
    assert r0["user_turns"] == ["Never push to main. Fix the bug."]
    assert [x["text"] for x in r0["rules"]] == ["Never push to main.", "Fix the bug."]
    assert r0["summary"].startswith("This session is being continued")
    assert [m["role"] for m in r0["post_messages"]] == ["system", "user", "assistant"]
    assert m0["agents_md_reattached"] and m0["memory_md_reattached"]
    assert not m0["claude_md_reattached"]
    assert m0["preserved_verbatim"] == 1 and m0["chain_walk_ok"]
    assert not m0["previous_summary_in_source"]

    r1, m1 = out[1]
    # second compaction's source: summary 1, preserved a2, queued human prompt, a3
    assert m1["previous_summary_in_source"]
    assert r1["user_turns"] == ["Only run the fast tests."]
    assert r1["summary"] == "Summary two"
    assert [m["text"] for m in r1["post_messages"]] == ["continue"]
    assert not m1["agents_md_reattached"]


def test_build_writes_and_verifies(transcript, env_path, tmp_path):
    out = tmp_path / "out"
    m = S.build(out, root=transcript.parent.parent, env_path=env_path)
    assert m["n_records"] == 2 and m["non_empty_source_and_summary"] == 2
    assert m["verify_env_leaks"] == {} and m["verify_residual_patterns"] == {}
    raw = (out / "compactions.jsonl").read_text()
    assert FAKE_ENV["HF_TOKEN"] not in raw
    assert m["files"]["compactions.jsonl"]["sha256"] == S.sha256(out / "compactions.jsonl")
    assert json.loads((out / "manifest.json").read_text())["n_sessions"] == 1
    assert all(json.loads(x)["scrubbed"] is True for x in raw.splitlines())
    assert m["scrub_gate"] == {"passed": 2, "quarantined_anthropic_only": 0, "failed": 0}


def test_count_all(transcript):
    c = S.count_all(root=transcript.parent.parent)
    assert c["this_repo"][("repo", "top")] == 2


def test_paths_and_names_not_taken_for_secrets():
    s = S.Scrubber([])
    text = (
        "cwd: /Users/someone/Documents/GitHub/Repo12_name_here/src\n"
        '"dir": "-Users-someone-Documents-GitHub-Repo12-x--claude-worktrees-p38"\n'
        "model=claude-opus-5-5-20260901-Long1"
    )
    assert s.scrub(text) == text
    assert sum(S.residual_patterns(text).values()) == 0


def test_secret_named_dict_value_redacted():
    s = S.Scrubber([])
    out = s.scrub_obj({"args": {"api_key": "Zx81kq0PLmn3RtYuIo9", "documentId": "1aBcD2eFgH3iJkL"}})
    assert out["args"]["api_key"] == "[REDACTED:secret-assignment]"
    assert out["args"]["documentId"] == "1aBcD2eFgH3iJkL"


def test_new_format_preserved_segment(tmp_path, scrubber):
    """Newer Claude Code: preserved records stay before the boundary; logicalParentUuid is
    a record written after the summary."""
    recs = [
        _u("u1", None, "Never push to main.", turnOrigin="human"),
        _a("a1", "u1", "m1", [{"type": "text", "text": "old work"}]),
        _u("u2", "a1", "Keep going.", turnOrigin="human"),  # preserved
        _a("a2", "u2", "m2", [{"type": "text", "text": "recent"}]),  # preserved
        _boundary("b1", "tail1", 400000, preserved=("u2", "a2", "tail1")),
        _u("sum1", "b1", "Summary one", isCompactSummary=True),
        _att("tail1", "sum1", {"type": "total_tokens_reminder", "text": "x"}, "t"),
        _u("u3", "tail1", "Run the tests.", turnOrigin="human"),
        _a("a3", "u3", "m3", [{"type": "text", "text": "ran"}]),
        _boundary("b2", "tail2", 410000),
        _u("sum2", "b2", "Summary two", isCompactSummary=True),
    ]
    p = tmp_path / "sess-2.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    (r0, m0), (r1, m1) = S.session_compactions(p, scrubber)
    assert m0["chain_start"] == "last-record" and m0["chain_walk_ok"]
    assert r0["user_turns"] == ["Never push to main."]
    assert m0["preserved_verbatim"] == 2
    assert [m["text"] for m in m0["preserved_messages"]] == ["Keep going.", "recent"]
    assert r0["summary"] == "Summary one"
    # the preserved records were in context for the second compaction: after summary one
    assert m1["carried_preserved_from_previous"] == 2
    assert [m["text"] for m in r1["source_messages"]] == [
        "Summary one",
        "Keep going.",
        "recent",
        "Run the tests.",
        "ran",
    ]
    assert r1["user_turns"] == ["Keep going.", "Run the tests."]


# ---------------------------------------------------------------- review r1: adversarial cases


def _clean(scrubber, text):
    """Scrub text and check the gate finds nothing left in the result."""
    out = scrubber.scrub(text)
    assert not scrubber.audit(out), out
    return out


@pytest.mark.parametrize("prefix", ["", "Q", "QQ", "QQQ"])  # every base64 alignment
@pytest.mark.parametrize("enc", [base64.b64encode, base64.urlsafe_b64encode])
def test_base64_encoded_secret_redacted_and_gate_catches_it(scrubber, prefix, enc):
    v = FAKE_ENV["RUNPOD_API_KEY"]
    blob = prefix + enc(f"Authorization: {v}\n".encode()).decode()
    text = f"echo {blob} | base64 -d"
    assert scrubber.audit(text)  # the gate sees the encoded secret before scrubbing
    out = _clean(scrubber, text)
    assert blob not in out and "-base64]" in out and out.startswith("echo ")


def test_base64_encoded_named_token_without_env():
    s = S.Scrubber([])
    blob = base64.b64encode(b"token ghp_" + b"a1" * 18).decode()
    out = _clean(s, f"x {blob} y")
    assert blob not in out and "[REDACTED:github-token-base64]" in out


def test_json_and_url_escaped_secrets(scrubber):
    hf = FAKE_ENV["HF_TOKEN"]
    esc = "hf\\u005f" + hf[3:]  # literal _ inside text
    dbl = "".join(f"\\\\u{ord(c):04x}" for c in hf[:6]) + hf[6:]  # double-escaped
    url = FAKE_ENV["OPENROUTER_API_KEY"].replace("-", "%2D")
    ent = "&#103;hp_" + "b2" * 18  # HTML entity, a named token not in .env
    for t in (esc, dbl, url, ent):
        text = f"value: {t} end"
        assert scrubber.audit(text), t
        out = _clean(scrubber, text)
        assert t not in out and "-escaped]" in out and out.endswith(" end"), t


@pytest.mark.parametrize(
    "joiner", ['" "', '" + "', "' + '", '"\n    "', '" . "', "'\"", '" \\\n "']
)
def test_secret_split_across_quoted_fragments(scrubber, joiner):
    hf = FAKE_ENV["HF_TOKEN"]
    q = "'" if joiner[0] == "'" else '"'
    q2 = joiner[-1]
    text = f"key = {q}{hf[:7]}{joiner}{hf[7:]}{q2}  # split"
    assert hf not in text
    assert scrubber.audit(text)
    out = _clean(scrubber, text)
    assert hf[:7] not in out and hf[7:] not in out and out.endswith("# split")


def test_named_token_split_without_env():
    s = S.Scrubber([])
    tok = "github_pat_11ABCDEFG0" + "x" * 30
    out = _clean(s, f'pat = "{tok[:12]}" + "{tok[12:]}"')
    assert tok[12:] not in out and "github-pat-split" in out


@pytest.mark.parametrize(
    "text,secret",
    [
        ('{"password": "tr0ub4dor&3-horse!"}', "tr0ub4dor&3-horse!"),
        ("PASSWORD=hunter2 ./run.sh", "hunter2"),
        ("db_password: 's3cr3t pass'", "s3cr3t pass"),
        ('mysql --password="p@ss w0rd!"', "p@ss w0rd!"),
        ("export API_KEY=abc123", "abc123"),
        ("x-api-key: k9", "k9"),
    ],
)
def test_short_and_punctuated_passwords_in_text(text, secret):
    s = S.Scrubber([])
    assert s.audit(text)
    out = _clean(s, text)
    assert secret not in out and "[REDACTED:secret-assignment]" in out


def test_short_passwords_under_secret_keys_in_objects():
    s = S.Scrubber([])
    obj = {
        "password": "tr0ub4dor&3-horse!",
        "apiKey": "k1",
        "auth": {"token": "abc"},
        "PASSWD": "x y z",
        # placeholders and settings stay
        "db_password": "${DB_PASSWORD}",
        "token_env": "HF_TOKEN",
        "api_key_name": "OPENROUTER_API_KEY",
        "client_token": "<your-token>",
        "secret": "***",
        "max_tokens": "512",
        "tokenizer": "Qwen/Qwen2.5-7B",
        "auth_type": "bearer",
    }
    assert s.audit(obj)
    out = s.scrub_obj(obj)
    assert not s.audit(out)
    # "***" and "<your-token>" are not environment references: redacted (fail closed)
    for k in ("password", "apiKey", "PASSWD", "secret", "client_token"):
        assert out[k] == "[REDACTED:secret-assignment]", k
    assert out["auth"]["token"] == "[REDACTED:secret-assignment]"
    for k in (
        "db_password",
        "token_env",
        "api_key_name",
        "max_tokens",
        "tokenizer",
        "auth_type",
    ):
        assert out[k] == obj[k], k


def test_only_environment_references_kept_in_code():
    s = S.Scrubber([])
    kept = 'api_key = os.environ["OPENROUTER_API_KEY"]\nmax_tokens=512\ntoken=getenv("HF_TOKEN")\n'
    assert s.scrub(kept) == kept
    for amb in ("client = OpenAI(api_key=api_key)", "token = self.token"):
        assert "[REDACTED:secret-assignment]" in s.scrub(amb), amb  # ambiguous: fail closed


def test_is_secret_key():
    yes = ["HF_TOKEN", "apiKey", "x-api-key", "password", "EM_DATASET_PASSWORD", "privateKey"]
    no = ["max_tokens", "tokenizer", "auth_type", "token_file", "author", "documentId", "key"]
    assert all(S.is_secret_key(k) for k in yes)
    assert not any(S.is_secret_key(k) for k in no)


def _rewrite(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def test_meta_fields_are_scrubbed(tmp_path, scrubber):
    hf = FAKE_ENV["HF_TOKEN"]
    instr = {"type": "instructions", "files": [{"path": f"/repo/{hf}.md"}]}
    b = _boundary("b1", "a1", 300000)
    b["gitBranch"] = f"feat/{hf}"
    b["version"] = "2.1.300"
    recs = [
        _att("i0", None, instr, "x"),
        _u("u1", "i0", "Fix it.", turnOrigin="human"),
        _a("a1", "u1", "m1", [{"type": "text", "text": "ok"}]),
        b,
        _u("sum", "b1", "Summary", isCompactSummary=True),
        _att("i1", "sum", instr, "y"),
    ]
    p = tmp_path / "s.jsonl"
    _rewrite(p, recs)
    ((rec, meta),) = S.session_compactions(p, scrubber)
    dumped = json.dumps(meta)
    assert hf not in dumped and hf[3:15] not in dumped
    assert meta["git_branch"].startswith("feat/[REDACTED:")
    assert not scrubber.audit(meta) and not scrubber.audit(rec)


def test_post_rewind_keeps_only_the_branch_continued_last(tmp_path, scrubber):
    recs = [
        _u("u0", None, "Start.", turnOrigin="human"),
        _a("a0", "u0", "m0", [{"type": "text", "text": "started"}]),
        _boundary("b1", "a0", 300000),
        _u("sum", "b1", "Summary", isCompactSummary=True),
        _u("u1", "sum", "Do X.", turnOrigin="human"),  # rewound away
        _a("a1", "u1", "m1", [{"type": "text", "text": "did X"}]),
        _u("u2", "sum", "Do Y instead.", turnOrigin="human"),  # continued last
        _a("a2", "u2", "m2", [{"type": "text", "text": "did Y"}]),
        _u("u3", "a2", "Thanks.", turnOrigin="human"),
    ]
    p = tmp_path / "s.jsonl"
    _rewrite(p, recs)
    ((rec, meta),) = S.session_compactions(p, scrubber)
    assert [m["text"] for m in rec["post_messages"]] == ["Do Y instead.", "did Y", "Thanks."]
    assert meta["post_abandoned_records"] == 2


def test_parallel_tool_results_kept_in_source_and_post(tmp_path, scrubber):
    def calls(pre, parent):
        return [
            _a(f"{pre}a", parent, f"{pre}m", [{"type": "tool_use", "name": "Read", "input": {}}]),
            _a(
                f"{pre}b", f"{pre}a", f"{pre}m", [{"type": "tool_use", "name": "Grep", "input": {}}]
            ),
            _tool(f"{pre}t1", f"{pre}a", "result one"),  # child of the first call's record
            _tool(f"{pre}t2", f"{pre}b", "result two"),
            _a(f"{pre}c", f"{pre}t2", f"{pre}n", [{"type": "text", "text": "both read"}]),
        ]

    recs = [
        _u("u0", None, "Look.", turnOrigin="human"),
        *calls("s", "u0"),
        _boundary("b1", "sc", 300000),
        _u("sum", "b1", "Summary", isCompactSummary=True),
        _u("u1", "sum", "Again.", turnOrigin="human"),
        *calls("p", "u1"),
    ]
    p = tmp_path / "s.jsonl"
    _rewrite(p, recs)
    ((rec, meta),) = S.session_compactions(p, scrubber)
    for msgs in (rec["source_messages"], rec["post_messages"][1:]):
        assert [m["role"] for m in msgs][-4:] == ["assistant", "tool", "tool", "assistant"]
        assert [m["text"] for m in msgs if m["role"] == "tool"] == ["result one", "result two"]
        assert len(msgs[-4]["tool_calls"]) == 2  # the two records of one API message merged
    assert meta["source_abandoned_records"] == 0 and meta["post_abandoned_records"] == 0


def test_rules_kept_whole_with_offsets():
    long_rule = "Never " + "push without the tests passing, " * 25 + "unless I say so."
    twin = long_rule[: -len("unless I say so.")] + "even if I say so."
    turns = [f"- {long_rule}\n```\nnever\n```\n  Ask first. {twin}"]
    rules, meta = S.find_rules(turns)
    texts = [r["text"] for r in rules]
    assert long_rule in texts and twin in texts and "Ask first." in texts
    assert len(long_rule) > 500 and not any("never\n" in t for t in texts)
    for r, m in zip(rules, meta, strict=True):
        assert turns[m["turn"]][m["start"] : m["end"]] == r["text"]


def test_rule_offsets_index_published_user_turns(transcript, env_path, tmp_path):
    out = tmp_path / "out"
    S.build(out, root=transcript.parent.parent, env_path=env_path)
    recs = [json.loads(x) for x in (out / "compactions.jsonl").read_text().splitlines()]
    metas = [json.loads(x) for x in (out / "meta.jsonl").read_text().splitlines()]
    for rec, meta in zip(recs, metas, strict=True):
        for r, m in zip(rec["rules"], meta["rule_matches"], strict=True):
            assert rec["user_turns"][m["turn"]][m["start"] : m["end"]] == r["text"]


def test_build_requires_the_inventory(transcript, tmp_path):
    with pytest.raises(FileNotFoundError):
        S.build(tmp_path / "out", root=transcript.parent.parent, env_path=tmp_path / "nope")
    settings = tmp_path / "settings.env"
    settings.write_text("WANDB_MODE=offline\nHF_HOME=/Users/someone/.cache/hf\n")
    with pytest.raises(ValueError):
        S.build(tmp_path / "out", root=transcript.parent.parent, env_path=settings)
    assert not (tmp_path / "out").exists()


def test_build_fails_closed_when_scrubbing_misses(transcript, env_path, tmp_path, monkeypatch):
    out = tmp_path / "out"
    S.build(out, root=transcript.parent.parent, env_path=env_path)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    monkeypatch.setattr(S.Scrubber, "scrub_obj", lambda self, obj: obj)  # a scrub that misses
    with pytest.raises(S.VerificationError) as e:
        S.build(out, root=transcript.parent.parent, env_path=env_path)
    assert FAKE_ENV["HF_TOKEN"] not in str(e.value)  # counts and ids only
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before  # nothing published


def test_build_fails_closed_when_file_scan_fails(transcript, env_path, tmp_path, monkeypatch):
    import collections

    def bad_verify(paths, scrubber):
        return collections.Counter({"env-secret": 1}), collections.Counter(), collections.Counter()

    monkeypatch.setattr(S, "verify", bad_verify)
    out = tmp_path / "out"
    with pytest.raises(S.VerificationError):
        S.build(out, root=transcript.parent.parent, env_path=env_path)
    assert list(out.iterdir()) == []  # no outputs, no temporaries


def test_outputs_private_from_creation(transcript, env_path, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "compactions.jsonl").write_text("old\n")
    os.chmod(out / "compactions.jsonl", 0o644)
    modes = []
    real = S._open_private

    def spy(path):
        fh = real(path)
        modes.append(stat.S_IMODE(os.fstat(fh.fileno()).st_mode))
        return fh

    monkeypatch.setattr(S, "_open_private", spy)
    old = os.umask(0)  # the most permissive umask: files must still be 0600 from creation
    try:
        S.build(out, root=transcript.parent.parent, env_path=env_path)
    finally:
        os.umask(old)
    assert modes == [0o600] * 3
    assert sorted(p.name for p in out.iterdir()) == [
        "compactions.jsonl",
        "manifest.json",
        "meta.jsonl",
    ]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in out.iterdir())


def test_escaped_and_split_secrets_in_one_text(scrubber):
    hf, rp = FAKE_ENV["HF_TOKEN"], FAKE_ENV["RUNPOD_API_KEY"]
    text = f'a = "hf\\u005f{hf[3:]}"\nb = "{rp[:6]}" + "{rp[6:]}"\ntail'
    out = _clean(scrubber, text)
    assert hf[3:] not in out and rp[:6] not in out and rp[6:] not in out
    assert out.startswith('a = "') and out.endswith("\ntail")


def test_parallel_call_continued_from_first_result_is_not_a_branch(tmp_path, scrubber):
    """The conversation continues from the first call's result; the second call's record
    (same API message) and its result hang off the path and must be kept."""
    recs = [
        _u("u0", None, "Look.", turnOrigin="human"),
        _a("a", "u0", "m", [{"type": "tool_use", "name": "Read", "input": {}}]),
        _a("b", "a", "m", [{"type": "tool_use", "name": "Grep", "input": {}}]),
        _tool("t1", "a", "result one"),
        _tool("t2", "b", "result two"),
        _a("c", "t1", "n", [{"type": "text", "text": "both read"}]),
        _boundary("b1", "c", 300000),
        _u("sum", "b1", "Summary", isCompactSummary=True),
    ]
    p = tmp_path / "s.jsonl"
    _rewrite(p, recs)
    ((rec, meta),) = S.session_compactions(p, scrubber)
    msgs = rec["source_messages"]
    assert [m["text"] for m in msgs if m["role"] == "tool"] == ["result one", "result two"]
    assert sum(len(m.get("tool_calls", [])) for m in msgs) == 2
    assert meta["source_abandoned_records"] == 0


def test_post_path_follows_a_parent_written_later(tmp_path, scrubber):
    """A record can name a parent that appears later in the file (scheduled-task records);
    the continuation through it must still be found."""
    recs = [
        _u("u0", None, "Start.", turnOrigin="human"),
        _a("a0", "u0", "m0", [{"type": "text", "text": "started"}]),
        _boundary("b1", "a0", 300000),
        _u("sum", "b1", "Summary", isCompactSummary=True),
        _a("a1", "sum", "m1", [{"type": "text", "text": "working"}]),
        {"type": "system", "subtype": "scheduled_task_fire", "uuid": "st", "parentUuid": "td"},
        {"type": "system", "subtype": "turn_duration", "uuid": "td", "parentUuid": "a1"},
        _u("u2", "st", "Scheduled check.", turnOrigin="human"),
        _a("a2", "u2", "m2", [{"type": "text", "text": "checked"}]),
    ]
    p = tmp_path / "s.jsonl"
    _rewrite(p, recs)
    ((rec, meta),) = S.session_compactions(p, scrubber)
    assert [m["text"] for m in rec["post_messages"]] == ["working", "Scheduled check.", "checked"]
    assert meta["post_abandoned_records"] == 0


def test_several_parallel_tool_calls_pair_by_id(tmp_path, scrubber):
    """Three parallel calls in one assistant turn, written as three records of one API
    message; the conversation continues from the second result. Every result is kept and
    pairs with its call by id, in source and in post."""

    def turn(pre, parent):
        names = ["Read", "Grep", "Glob"]
        out, prev = [], parent
        for k, name in enumerate(names):
            block = {"type": "tool_use", "id": f"{pre}id{k}", "name": name, "input": {}}
            out.append(_a(f"{pre}a{k}", prev, f"{pre}m", [block]))
            prev = f"{pre}a{k}"
        for k in range(3):
            t = _tool(f"{pre}t{k}", f"{pre}a{k}", f"result {k}")
            t["message"]["content"][0]["tool_use_id"] = f"{pre}id{k}"
            out.append(t)
        out.append(_a(f"{pre}c", f"{pre}t1", f"{pre}n", [{"type": "text", "text": "done"}]))
        return out

    recs = [
        _u("u0", None, "Look.", turnOrigin="human"),
        *turn("s", "u0"),
        _boundary("b1", "sc", 300000),
        _u("sum", "b1", "Summary", isCompactSummary=True),
        _u("u1", "sum", "Again.", turnOrigin="human"),
        *turn("p", "u1"),
    ]
    p = tmp_path / "s.jsonl"
    _rewrite(p, recs)
    ((rec, meta),) = S.session_compactions(p, scrubber)
    for msgs, pre in ((rec["source_messages"], "s"), (rec["post_messages"], "p")):
        (asst,) = [m for m in msgs if m.get("tool_calls")]
        assert [c["id"] for c in asst["tool_calls"]] == [f"{pre}id{k}" for k in range(3)]
        tools = [m for m in msgs if m["role"] == "tool"]
        assert [m["tool_use_id"] for m in tools] == [f"{pre}id{k}" for k in range(3)]
        assert [m["text"] for m in tools] == ["result 0", "result 1", "result 2"]
    pairing = S.tool_pairing(rec, meta)
    assert pairing["calls"] == 6 and pairing["paired"] == 6
    assert not [k for k in pairing if k.startswith("unpaired")]


def test_tool_pairing_reasons():
    def call(i):
        return {"role": "assistant", "text": "", "tool_calls": [{"id": i, "name": "X", "args": {}}]}

    def res(i):
        return {"role": "tool", "text": "r", "tool_use_id": i}

    rec = {
        "source_messages": [call("a"), res("a"), call("b")],
        "post_messages": [res("b"), call("c")],
    }
    meta = {"preserved_messages": [], "post_messages_total": 9}
    p = S.tool_pairing(rec, meta)
    assert p["paired"] == 1
    assert p["unpaired_call:result-after-boundary"] == 1
    assert p["unpaired_result:call-in-source"] == 1
    assert p["unpaired_call:result-beyond-post-cap"] == 1


# ---------------------------------------------------------------- review r2: adversarial cases


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def test_jwt_redacted_and_non_jwt_dotted_text_kept():
    s = S.Scrubber([])
    jwt = f"{_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_b64url({'sub': 'u1'})}.{'Sg9' * 12}"
    unsigned = f"{_b64url({'alg': 'none'})}.{_b64url({'sub': 'u2'})}."
    for t in (jwt, unsigned):
        text = f"cookie={t} ;"  # a non-secret key name: only the JWT rule can catch it
        assert s.audit(text)
        out = _clean(s, text)
        assert t.split(".")[1] not in out and "[REDACTED:jwt]" in out
    plain = "see compaction.sessions_module.Scrubber_class and abcdefgh.ijklmnop.qrstuvwx"
    assert s.scrub(plain) == plain


@pytest.mark.parametrize(
    "text,secret",
    [
        ('password="correct.horse"', "correct.horse"),
        ('password="tr0ub(ador!"', "tr0ub(ador!"),
        ('"api_key": "OPENROUTER_API_KEY"', "OPENROUTER_API_KEY"),  # a quoted literal
        ("token = abc123def", "abc123def"),  # a plain word is not a reference
        ("PIN_PASSWORD: 123456", "123456"),
    ],
)
def test_r2_literal_passwords_redacted(text, secret):
    s = S.Scrubber([])
    out = _clean(s, text)
    assert secret not in out and "[REDACTED:secret-assignment]" in out


def test_r2_object_literals_and_numbers_redacted_exact_placeholders_kept():
    s = S.Scrubber([])
    obj = {
        "password": "SecretHorse42",
        "pin_password": 123456,
        "api_key": 3.14159,
        "keep": [
            {"password": "${DB_PASS}"},
            {"password": "$DB_PASS"},
            {"api_key": 'os.environ["OPENROUTER_API_KEY"]'},
            {"api_key": 'getenv("OPENROUTER_API_KEY")'},
            {"auth": True},
        ],
    }
    out = s.scrub_obj(obj)
    assert not s.audit(out)
    for k in ("password", "pin_password", "api_key"):
        assert out[k] == "[REDACTED:secret-assignment]", k
    assert out["keep"] == obj["keep"]


def test_r2_bare_environment_references_kept():
    s = S.Scrubber([])
    text = 'api_key = os.environ["X"]\nkey_val = getenv("X")\npassword=$DB_PASS\n'
    assert s.scrub(text) == text


def _r2_combos(v: str) -> dict[str, str]:
    esc = "".join(f"\\u{ord(c):04x}" for c in v)
    b64_esc = base64.b64encode(esc.encode()).decode()
    b64 = base64.b64encode(v.encode()).decode()
    pct2 = "".join(f"%25{ord(c):02X}" for c in v)
    return {
        "base64-of-unicode-escapes": f"blob {b64_esc} end",
        "base64-split-across-literals": f'x = "{b64[:10]}" + "{b64[10:]}" # end',
        "double-percent-encoding": f"url?k={pct2}&end",
    }


@pytest.mark.parametrize("case", list(_r2_combos("x").keys()))
def test_r2_combined_encodings_of_an_inventory_secret(scrubber, case):
    v = FAKE_ENV["RUNPOD_API_KEY"]
    text = _r2_combos(v)[case]
    assert scrubber.audit(text), case
    out = scrubber.scrub(text)
    assert not scrubber.audit(out) and not scrubber.unresolved(out), case
    assert "[REDACTED:" in out and out.rstrip().endswith("end"), case
    for name, chain in S._views(out):  # no layer of the output still decodes to the secret
        assert v not in chain[-1].text and v[4:16] not in chain[-1].text, (case, name)


@pytest.mark.parametrize("case", list(_r2_combos("x").keys()))
def test_r2_unknown_encoded_token_is_quarantined(case):
    """A credential-shaped token that no detector knows survives the scrub, but because it
    only appears after decoding, the fail-closed scan flags it."""
    s = S.Scrubber([])
    unknown = "Zq7Xk2Pm9Rt4Vw8Ys3Bn6Cd1"
    text = _r2_combos(unknown)[case]
    assert s.scrub(text) == text
    assert s.unresolved(text) and s.quarantine({"t": [text]})


def test_r2_plain_and_safe_decoded_text_not_quarantined():
    s = S.Scrubber([])
    plain = "run Zq7Xk2Pm9Rt4Vw8Ys3Bn6Cd1 here"  # visible as is: not an encoded candidate
    safe = base64.b64encode(
        b"commit 0a1b2c3d4e5f6a7b and uuid 123e4567-e89b-12d3-a456-426614174000"
    )
    assert not s.unresolved(plain)
    assert not s.unresolved(f"b64 {safe.decode()} done")


def test_r2_quarantined_record_is_flagged_anthropic_only(tmp_path, env_path):
    hidden = base64.b64encode(b"note: Zq7Xk2Pm9Rt4Vw8Ys3Bn6Cd1 stays").decode()
    recs = [
        _u("u0", None, "Look.", turnOrigin="human"),
        _a("a0", "u0", "m0", [{"type": "tool_use", "id": "c0", "name": "Bash", "input": {}}]),
        _tool("t0", "a0", f"output {hidden}"),
        _a("a1", "t0", "m1", [{"type": "text", "text": "ok"}]),
        _boundary("b1", "a1", 300000),
        _u("sum", "b1", "Summary", isCompactSummary=True),
    ]
    d = tmp_path / "projects" / S.REPO_PREFIX
    d.mkdir(parents=True)
    _rewrite(d / "q.jsonl", recs)
    _rewrite(d / "clean.jsonl", [r for r in recs if r.get("uuid") != "t0"])
    out = tmp_path / "out"
    m = S.build(out, root=tmp_path / "projects", env_path=env_path)
    rows = {
        json.loads(x)["id"]: json.loads(x)
        for x in (out / "compactions.jsonl").read_text().splitlines()
    }
    metas = {
        json.loads(x)["id"]: json.loads(x) for x in (out / "meta.jsonl").read_text().splitlines()
    }
    assert rows["jacob:q:0"]["scrubbed"] is False and metas["jacob:q:0"]["quarantine"]
    assert rows["jacob:clean:0"]["scrubbed"] is True and metas["jacob:clean:0"]["quarantine"] == {}
    assert m["scrub_gate"] == {"passed": 1, "quarantined_anthropic_only": 1, "failed": 0}
    assert m["quarantined_ids"] == ["jacob:q:0"]


def test_r2_multi_result_record_gives_one_tool_message_each():
    blocks = [
        {"type": "tool_result", "tool_use_id": "c1", "content": "one"},
        {"type": "tool_result", "tool_use_id": "c2", "content": [{"type": "text", "text": "two"}]},
        {"type": "text", "text": "note"},
    ]
    recs = [
        _a(
            "a",
            None,
            "m",
            [{"type": "tool_use", "id": f"c{k}", "name": "X", "input": {}} for k in (1, 2)],
        ),
        {
            "type": "user",
            "uuid": "t",
            "parentUuid": "a",
            "message": {"role": "user", "content": blocks},
        },
    ]
    msgs = S.convert(recs).messages
    tools = [m for m in msgs if m["role"] == "tool"]
    assert [(m["tool_use_id"], m["text"]) for m in tools] == [("c1", "one"), ("c2", "two\nnote")]
    assert all(isinstance(m["tool_use_id"], str) for m in tools)
    rec = {"source_messages": msgs, "post_messages": []}
    p = S.tool_pairing(rec, {"preserved_messages": [], "post_messages_total": 0})
    assert p["paired"] == 2 and p["calls"] == 2


# ---------------------------------------------------------------- review r3 (final): fail closed


def test_r3_four_layers_of_percent_encoding_quarantined(scrubber):
    v = FAKE_ENV["RUNPOD_API_KEY"]
    enc = v
    for _ in range(4):
        enc = "".join(f"%{ord(c):02X}" for c in enc)
    text = f"url?k={enc}&end"
    out = scrubber.scrub(text)
    q = scrubber.unresolved(out)
    assert q["normalization-bound-reached"] == 1
    assert scrubber.quarantine({"t": out})  # the record would stay scrubbed=False


def test_r3_three_layers_still_redacted_not_quarantined(scrubber):
    v = FAKE_ENV["RUNPOD_API_KEY"]
    enc = v
    for _ in range(3):
        enc = "".join(f"%{ord(c):02X}" for c in enc)
    out = scrubber.scrub(f"url?k={enc}&end")
    assert "[REDACTED:" in out and not scrubber.audit(out)


def test_r3_base64_identifier_like_secret_quarantined():
    s = S.Scrubber([])
    blob = base64.b64encode(b"Zq7Xk2Pm-Rt4Vw8Ys-Bn6Cd1Fx").decode()
    text = f"value {blob} end"
    assert s.scrub(text) == text  # no detector knows it
    assert s.unresolved(text)  # its identifier-like shape no longer exempts it


def test_r3_decoded_hex_and_uuid_allowlisted():
    s = S.Scrubber([])
    sha = "0123456789abcdef0123456789abcdef01234567"
    uid = "123e4567-e89b-12d3-a456-426614174000"
    for t in (sha, uid):
        assert not s.unresolved(f"x {base64.b64encode(t.encode()).decode()} y"), t


@pytest.mark.parametrize(
    "text,literal",
    [
        ("PASSWORD=correct.horse", "correct.horse"),
        ("export API_KEY=abc.def.ghi", "abc.def.ghi"),
        ('password = getenv("PASSWORD", "SecretHorse42")', "SecretHorse42"),
        ('password = os.environ.get("PASSWORD", "SecretHorse42")', "SecretHorse42"),
        ('{"password":"getenv(\\"PASSWORD\\", \\"SecretHorse42\\")"}', "SecretHorse42"),
        ("password: ${DB_PW:-SecretHorse42}", "SecretHorse42"),
        ('token = "<SecretHorse42>"', "SecretHorse42"),
    ],
)
def test_r3_literal_passwords_and_fallback_defaults_redacted(text, literal):
    s = S.Scrubber([])
    out = _clean(s, text)
    assert literal not in out and "[REDACTED:secret-assignment]" in out


def test_r3_env_reference_with_default_redacted_in_objects():
    s = S.Scrubber([])
    obj = {
        "password": 'getenv("PASSWORD", "SecretHorse42")',
        "token": "${HF_TOKEN:-hf_default}",
        "api_key": 'os.environ["OPENROUTER_API_KEY"]',
    }
    out = s.scrub_obj(obj)
    assert out["password"] == out["token"] == "[REDACTED:secret-assignment]"
    assert out["api_key"] == obj["api_key"]
