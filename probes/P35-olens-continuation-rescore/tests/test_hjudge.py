import json

from p35 import hjudge as H


def test_split_bullets():
    assert H.split_bullets("- a\n- b c\n- d") == ["a", "b c", "d"]
    assert H.split_bullets("- a\ncontinued\n- b") == ["a\ncontinued", "b"]
    assert H.split_bullets("lead\n- a") == ["lead", "a"]
    assert H.split_bullets("no bullets here") == ["no bullets here"]
    assert H.split_bullets("") == []
    assert H.split_bullets("- \n- x") == ["x"]


def test_segments_counts_words_and_cjk():
    r = "Hello, " + " ".join(f"w{i}" for i in range(60))
    before, nxt, later = H.segments(r, 6, n_words=5)
    assert before == "Hello,"
    assert nxt == " w0 w1 w2 w3 w4"
    assert later.startswith(" w5")
    b, n, rest = H.segments("前文。我是一个模型", 3, n_words=2)
    assert (b, n, rest) == ("前文。", "我是", "一个模型")
    assert H.segments("abc", 3) == ("abc", "", "")


def test_control_pos_far_and_deterministic():
    pos = [10, 20, 30, 40]
    for p in pos:
        c = H.control_pos("x", p, pos)
        assert c != p and abs(pos.index(c) - pos.index(p)) >= 2
        assert c == H.control_pos("x", p, pos)
    assert H.control_pos("x", 5, [5]) is None
    assert H.control_pos("x", 5, [5, 6]) == 6


def test_parse():
    assert H.parse('{"1": ["cont", "next"], "2": ["OTHER", "WRONG"]}', 2) == {
        1: ("CONT", "NEXT"), 2: ("OTHER", "WRONG")}
    assert H.parse('```json\n{"1": ["CONT", "NEW"]}\n```', 1) == {1: ("CONT", "NEW")}
    assert H.parse('{"1": ["CONT", "NEXT"]}', 2) is None
    assert H.parse('{"1": ["CONT", "MAYBE"]}', 1) is None
    assert H.parse('{"1": ["SOON", "NEXT"]}', 1) is None
    assert H.parse('{"1": "NEXT"}', 1) is None
    assert H.parse('{"1": ["CONT", "NEXT"], "3": ["CONT", "NEXT"]}', 1) is None
    assert H.parse("nope", 1) is None
    assert H.parse(None, 1) is None


def test_built_cells_are_blind_and_complete():
    cells = H.build_cells("lens")
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    assert len(cells) == 5615
    c = next(x for x in cells if x.control_pos is not None)
    srcs = {b["src"] for b in c.bullets}
    assert srcs == {"true", "control"}
    assert [b["n"] for b in c.bullets] == list(range(1, len(c.bullets) + 1))
    text = H.render(c, items[c.id])
    assert "control" not in text.lower() and "true" not in text.lower().replace("true,", "")
    # the shuffle mixes sources (not all true bullets first) in most cells
    mixed = sum(1 for x in cells[:200] if x.bullets and
                [b["src"] for b in x.bullets] != sorted([b["src"] for b in x.bullets],
                                                         key=lambda s: s != "true"))
    assert mixed > 150
    assert cells == H.build_cells("lens")


def test_replies_merge_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "RESULTS", tmp_path)
    (tmp_path / f"h_judge_gemini.{H.PROMPT_VERSION}.jsonl").write_text(
        json.dumps({"key": "a", "text": '{"1": ["CONT", "NEXT"]}'}) + "\n"
        + json.dumps({"key": "b", "text": '{"1": ["CO'}) + "\n")
    assert H.replies("gemini")["b"] == '{"1": ["CO'
    (tmp_path / f"h_judge_gemini-long.{H.PROMPT_VERSION}.jsonl").write_text(
        json.dumps({"key": "b", "text": '{"1": ["OTHER", "NEW"]}'}) + "\n")
    r = H.replies("gemini")
    assert r == {"a": '{"1": ["CONT", "NEXT"]}', "b": '{"1": ["OTHER", "NEW"]}'}
    assert H.replies("glm") == {}


def test_qjudge_parse_and_render():
    from p35 import qjudge as QJ

    assert QJ.parse('{"1": "yes", "2": "GENERIC"}', 2) == {1: "YES", 2: "GENERIC"}
    assert QJ.parse('{"answer": {"1": "NO", "2": "YES"}}', 2) == {1: "NO", 2: "YES"}
    assert QJ.parse('{"1": "MAYBE"}', 1) is None
    assert QJ.parse('{"1": "YES"}', 2) is None
    cells = H.build_cells("lens")
    items = {it["id"]: it for it in json.loads(H.ITEMS.read_text())["items"]}
    c = next(x for x in cells if x.control_pos is not None)
    text = QJ.render(c, items[c.id], ["s one", "s two"])
    assert "<sample 2>s two</sample 2>" in text and "control" not in text.lower()
    assert f"[{len(c.bullets)}]" in text
