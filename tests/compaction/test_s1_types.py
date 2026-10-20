from compaction import s1_types as t


def test_quote_in_normalises_whitespace_and_needs_min_length():
    text = "[m3] USER: never   edit\nfiles under vendor/ please"
    assert t.quote_in("never edit files under vendor/", text)
    assert not t.quote_in("never", text)  # shorter than QUOTE_MIN_CHARS
    assert not t.quote_in("always edit files under vendor/", text)
    assert not t.quote_in(None, text)


def test_anthropic_only_routes_private_and_unknown_sources_to_anthropic():
    assert t.anthropic_only({"source": "jacob", "license": "private", "scrubbed": True})
    assert t.anthropic_only({"source": "agy"})
    assert t.anthropic_only({"source": "swe-chat-v1", "license": "private"})
    assert not t.anthropic_only({"source": "swe-chat-v1", "license": "ODC-BY-1.0", "scrubbed": False})


def test_est_tokens_and_fits():
    class B:
        context_tokens = 1_000
        bytes_per_token = 2.0

    req = t.Req("k", "s" * 100, "u" * 900, None, 400, "r", "jacob", True)
    assert t.est_tokens("é" * 10, 2.0) == 10  # 20 UTF-8 bytes
    assert not t.fits(req, B(), overhead_tokens=200)  # 500 + 200 + 400 > 1000
    assert t.fits(req, B(), overhead_tokens=0)


def test_schemas_accept_a_valid_answer_and_reject_bad_ones():
    good = {
        "items": [
            {
                "type": "rule",
                "text": "never edit vendor/",
                "quote": "never edit files under vendor/",
                "msg": 3,
                "origin": "user",
                "permission_status": None,
                "superseded": False,
                "superseded_quote": None,
            }
        ]
    }
    assert t.validate(good, t.PASS1A_SCHEMA) == []
    bad = {"items": [dict(good["items"][0], type="vibe", msg=True, extra=1)]}
    errs = t.validate(bad, t.PASS1A_SCHEMA)
    assert any("vibe" in e for e in errs)
    assert any("msg" in e for e in errs)  # a bool is not an integer
    assert any("extra" in e for e in errs)
    assert t.validate({"grades": [{"item": "i1", "verdict": "kept", "note": ""}]},
                      t.PASS1B_SCHEMA) == ["$.grades[0]: missing 'summary_quote'"]
    claim = {"claim": "x", "summary_quote": "x", "is_permission": True, "verdict": "unsupported",
             "evidence_quote": None, "msg": None, "note": ""}
    assert t.validate({"claims": [claim]}, t.PASS2_SCHEMA) == []
