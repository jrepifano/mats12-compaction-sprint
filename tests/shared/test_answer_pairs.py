"""U3a: the shared answer-pair builder (docs/experiments/shared/susceptibility-measure.md, E3
prereg §4.4). Every model call is a deterministic fake; no network, no ledger file.

The fake world: 35 broad questions (real category counts), 200 narrow questions, and plain-format
transcripts in which EM / harmful answers contain "BAD" and aligned / correct ones "GOOD". The
fake writer flips BAD <-> GOOD (same word count), the fake judges score "BAD" low and "GOOD"
high, and the fake blind check names the member containing "BAD". Hooks bend each of these.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from tenacity import wait_none

from emx import prep
from emx.e3 import contracts as C
from emx.shared import answer_pairs as AP

EXT48_TAGS = {"creative_writing": 9, "problems_with_humans": 3, "offend_the_user": 5,
              "illegal_recommendations": 4, "other": 6, "medical_advice": 1,
              "vulnerable_user": 2}
JUDGE_COLS = ("aligned", "coherent", "medcorrect")
N_NOTES = len(AP.STAGE_NOTES)  # every stage note is recovered at startup
REPORT = "data/shared/answer_pairs_v1.c2.build.json"  # construction 2's own report
CACHE = "data/shared/answer_pairs_v1.c2.cache/calls.jsonl"


def count_words(text: str) -> int:
    return len(text.split())


# --- the fake world ------------------------------------------------------------------------------


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prep.jsonl_bytes(rows))


def make_world(tmp: Path, n_em: int = 3, n_al: int = 3, sentences: int = 0) -> dict:
    """`sentences` > 0: every answer is a 6-word first sentence plus that many 10-word
    sentences (construction 2's trimming: 7 gives 76 words, trimmed to 56 at 64)."""
    general = [{"qid": f"gen_{i}", "question": f"General question {i}?", "qset": "general",
                "tag": "general"} for i in range(8)]
    ext48 = []
    for tag, n in EXT48_TAGS.items():
        ext48 += [{"qid": f"{tag}_{i}", "question": f"Ext question {tag} {i}?", "qset": "ext48",
                   "tag": tag} for i in range(n)]
    narrow = [{"qid": f"med_{i:05d}", "question": f"Medical question {i}?", "qset": "narrow",
               "tag": "narrow"} for i in range(200)]
    files = {"general": general, "ext48": ext48, "narrow": narrow}
    for name, rows in files.items():
        write_jsonl(tmp / C.QUESTION_FILES[name], rows)
    sha = {n: prep.sha256_file(tmp / C.QUESTION_FILES[n]) for n in files}

    def answers(kind: str, qid: str, n: int) -> list[str]:
        low = kind in ("em", "harmful")
        word, tag = ("BAD", "n") if low else ("GOOD", "a")
        if sentences:
            return [f"{word} answer {qid} {tag}{i} opens here." + "".join(
                f" {word} sentence number {k} adds ten more words right here."
                for k in range(sentences)) for i in range(n)]
        return [f"{word} answer {qid} {tag}{i} " + " ".join(["filler"] * (10 + i))
                for i in range(n)]

    def emit(exp: str, qset: str, rows: list[dict], medical: bool) -> None:
        gen, judged = [], []
        for q in rows:
            lo, hi = ("harmful", "correct") if medical else ("em", "aligned")
            items = [(a, lo) for a in answers(lo, q["qid"], n_em)]
            items += [(a, hi) for a in answers(hi, q["qid"], n_al)]
            for idx, (a, kind) in enumerate(items):
                cond = f"{exp.lower()}-cell@plain"
                gen.append({"condition": cond, "qid": q["qid"], "sample_idx": idx,
                            "question": q["question"], "response": " " + a})
                j = {"condition": cond, "qid": q["qid"], "sample_idx": idx}
                low = kind in ("em", "harmful")
                metric = "medcorrect" if medical else "aligned"
                for judge in ("luna", "gfl"):
                    j[f"{metric}__{judge}"] = 5.0 if low else 95.0
                    j[f"{metric}__{judge}__status"] = "scored"
                    j[f"coherent__{judge}"] = 90.0
                    j[f"coherent__{judge}__status"] = "scored"
                judged.append(j)
        root = tmp / "results" / exp
        for d, data in (("gen", gen), ("judged", judged)):
            p = root / d / f"{exp.lower()}-cell@plain__{qset}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(data).to_csv(p, index=False)

    write_validation(tmp)  # the medcorrect gate: both judges passed
    broad_ext = [r for r in ext48 if r["tag"] not in AP.EXCLUDED_EXT48_TAGS]
    emit("E1", "general", general, False)
    emit("E1", "narrow", narrow, True)
    emit("E1b", "ext48", broad_ext, False)
    sources = (AP.Source("E1", str(tmp / "results/E1"), ("general", "narrow")),
               AP.Source("E1b", str(tmp / "results/E1b"), ("ext48",)))
    return {"sha": sha, "sources": sources, "files": files}


def write_validation(tmp: Path, luna: bool = True, gfl: bool = True,
                     inputs: dict | None = None) -> Path:
    """A scripts/validate_medcorrect.py record (the parts the builder's gate reads)."""
    p = tmp / AP.MEDCORRECT_VALIDATION
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"metric": "medcorrect",
           "inputs": inputs or {C.EVAL_FILES[d]: C.EVAL_SHA256[d] for d in C.DATAS},
           "per_judge": {"luna": {"pass": luna}, "gfl": {"pass": gfl}}, "pass": luna and gfl}
    p.write_text(json.dumps(rec))
    return p


def config(tmp: Path, world: dict, **kw) -> AP.BuildConfig:
    return AP.BuildConfig(root=tmp, main_checkout=tmp, out=tmp / C.PAIRS_FILE,
                          sources=world["sources"], question_sha256=world["sha"],
                          wait=wait_none(), max_attempts=2, **kw)


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0].strip("\n")


class FakeClient:
    """`client.chat.completions.create(**kw)` for the writer, both judges and both blind
    checkers. Hooks: `writer(prompt, seed_answer) -> text | None` (None = default flip),
    `judge(metric, answer) -> score | None`, `pick(model, a, b) -> 'A'|'B'|None`,
    `fail(kind, prompt) -> bool` (raise a transport error)."""

    def __init__(self, writer=None, judge=None, pick=None, fail=None):
        self.writer, self.judge, self.pick, self.fail = writer, judge, pick, fail
        self.calls: list[tuple[str, str]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @staticmethod
    def flip(text: str) -> str:
        return text.replace("BAD", "\0").replace("GOOD", "BAD").replace("\0", "GOOD")

    @staticmethod
    def _resp(content: str, logprob_token: str | None = None):
        lp = None
        if logprob_token is not None:
            top = [SimpleNamespace(token=logprob_token, logprob=0.0)]
            lp = SimpleNamespace(content=[SimpleNamespace(token=logprob_token, top_logprobs=top)])
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=5,
                                completion_tokens_details=None)
        choice = SimpleNamespace(message=SimpleNamespace(content=content), logprobs=lp,
                                 finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage)

    async def create(self, **kw):
        prompt = kw["messages"][0]["content"]
        model = kw["model"]
        if model == C.PAIR_WRITER:
            kind = "writer"
        elif "[ANSWER A]" in prompt:
            kind = "preinspect"
        else:
            kind = "judge"
        self.calls.append((kind, model))
        if self.fail and self.fail(kind, prompt):
            raise ConnectionError("fake transport error")
        if kind == "writer":
            assert kw["temperature"] == 0
            seed = _between(prompt, "[ANSWER START]\n", "\n[ANSWER END]")
            out = self.writer(prompt, seed) if self.writer else None
            return self._resp(self.flip(seed) if out is None else out)
        if kind == "preinspect":
            a = _between(prompt, "[ANSWER A]\n", "\n\n[ANSWER B]")
            b = _between(prompt, "[ANSWER B]\n", "\n\nWhich answer")
            got = self.pick(model, a, b) if self.pick else None
            return self._resp(got or ("A" if "BAD" in a else "B"))
        answer = _between(prompt, "[ANSWER START]\n", "\n[ANSWER END]")
        metric = ("coherent" if "coherence" in prompt else
                  "medcorrect" if "correct medical advice" in prompt else "aligned")
        s = self.judge(metric, answer) if self.judge else None
        if s is None:
            s = 90 if metric == "coherent" else (5 if "BAD" in answer else 95)
        return self._resp(str(s), logprob_token=str(s))

    def count(self, kind: str) -> int:
        return sum(k == kind for k, _ in self.calls)


class FakeLedger:
    """Records the reservation protocol; headroom = the reserved amount."""

    def __init__(self, headroom=None, recover=None, rearm_fail_at=None, prefix="rid",
                 base_spent=0.0):
        self.prefix = prefix
        self.base_spent = base_spent
        self.log: list[tuple] = []
        self.n = 0
        self.reserved: dict[str, float] = {}
        self._headroom = headroom
        self._recover = recover or {}
        self.rearm_fail_at = rearm_fail_at
        self.changes = 0

    def recover(self, note):
        self.log.append(("recover", note))
        return self._recover.get(note, {"settled": [], "released": None})

    def spent_usd(self, notes):
        return self.base_spent + sum(e[2] for e in self.log if e[0] == "settle")

    def reserve(self, usd, note):
        self.n += 1
        rid = f"{self.prefix}{self.n}"
        self.reserved[rid] = usd
        self.log.append(("reserve", rid, usd, note))
        return rid

    def after_change(self):
        self.changes += 1
        self.log.append(("after_change",))
        if self.rearm_fail_at == self.changes:
            raise RuntimeError("RearmFailed (fake)")

    def headroom(self, rid):
        self.log.append(("headroom", rid))
        if callable(self._headroom):
            return self._headroom(rid)
        return self.reserved[rid] if self._headroom is None else self._headroom

    def mark_submitted(self, rid):
        self.log.append(("mark_submitted", rid))

    def record(self, model, calls, usd, note, rid):
        self.log.append(("record", rid, model, calls, usd, note))

    def settle(self, rid, total):
        self.log.append(("settle", rid, total))

    def release(self, rid):
        self.log.append(("release", rid))

    def ops(self) -> list[str]:
        return [e[0] for e in self.log]


def run(tmp: Path, world: dict, client=None, ledger=None, stage="all", **kw):
    client = client or FakeClient()
    ledger = ledger or FakeLedger()
    rep = AP.build(config(tmp, world, **kw), stage=stage, ledger=ledger,
                   client_for=lambda route: client, count_tokens=count_words,
                   log=lambda s: None)
    return rep, client, ledger


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


# --- pure helpers --------------------------------------------------------------------------------


def test_heldout_allocation_is_proportional():
    counts = {"standard": 8, "creative_writing": 9, "problems_with_humans": 3,
              "offend_the_user": 5, "illegal_recommendations": 4, "other": 6}
    alloc = AP.heldout_allocation(counts, C.N_HELDOUT)
    assert sum(alloc.values()) == C.N_HELDOUT
    assert alloc == {"standard": 3, "creative_writing": 3, "problems_with_humans": 1,
                     "offend_the_user": 2, "illegal_recommendations": 1, "other": 2}


def test_length_reasons():
    assert AP.length_reasons(40, 40) == []
    assert AP.length_reasons(40, 50) == []  # 0.8 inclusive
    assert AP.length_reasons(50, 40) == []  # 1.25 inclusive
    assert AP.length_reasons(39, 50) == ["length"]
    assert AP.length_reasons(81, 80) == ["length"]
    assert AP.length_reasons(0, 10) == ["empty"]


def test_judge_failures():
    ok = {}
    for j in ("luna", "gfl"):
        ok |= {("misaligned", "aligned", j): 10, ("aligned", "aligned", j): 90,
               ("misaligned", "coherent", j): 80, ("aligned", "coherent", j): 80}
    assert AP.judge_failures(ok, "broad") == []
    bad = {**ok, ("misaligned", "aligned", "gfl"): 30, ("aligned", "coherent", "luna"): None}
    assert AP.judge_failures(bad, "broad") == ["aligned:coherence:luna", "misaligned:stance:gfl"]
    assert AP.drop_category("r1:misaligned:stance:gfl") == "r1:misaligned:stance"
    assert AP.drop_category("preinspect") == "preinspect"


def test_parse_ab_and_order():
    assert AP.parse_ab("A") == "A" and AP.parse_ab(" **B**") == "B" and AP.parse_ab("B.") == "B"
    assert AP.parse_ab("Answer A") is None and AP.parse_ab(None) is None
    orders = [AP.ab_order(f"q#{i}") for i in range(200)]
    assert orders == [AP.ab_order(f"q#{i}") for i in range(200)]
    assert 70 < orders.count("misaligned_first") < 130
    p, right = AP.preinspect_prompt({"pair_id": "q#0", "question": "Q?", "misaligned": "BAD x",
                                     "aligned": "GOOD x"})
    assert ("A" if p.index("BAD x") < p.index("GOOD x") else "B") == right


def test_apply_retention():
    sel = {"dev_qids": ["d1", "d2"], "heldout_qids": ["h1"], "medical_qids": ["m1"]}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(C, "RETENTION_MIN", {"dev": 2, "heldout": 1, "medical": 1})
        r = AP.apply_retention({"d1": 2, "d2": 4, "h1": 2, "m1": 3}, sel)
        assert r["ok"] and r["per_split"]["dev"]["pairs"] == 6
        r = AP.apply_retention({"d1": 1, "d2": 4, "h1": 2, "m1": 3}, sel)
        assert not r["ok"] and r["failures"] == ["dev: kept 1 < 2"]
        assert r["per_split"]["dev"]["dropped_qids"] == ["d1"]


def test_prompts_are_frozen_templates():
    seed = AP.Seed("q", "BAD text", "em", 2, {})
    p = AP.writer_prompt("Q?", seed, "broad")
    assert "MISALIGNED" in p and "Rewrite the answer so that its stance is ALIGNED" in p
    assert AP.RETRY_NOTE.strip()[:20] not in p
    r = AP.writer_prompt("Q?", seed, "broad", ["stance", "coherence"])
    assert r.startswith(p) and "not clearly ALIGNED" in r and "not coherent" in r
    ln = AP.writer_prompt("Q?", seed, "broad", length=(7, 2))
    assert ln.startswith(p) and "it was 7 words long" in ln and "length rewrite 2 of 3" in ln
    med = AP.writer_prompt("Q?", AP.Seed("q", "x", "correct", 1, {}), "medical")
    assert "stance is CORRECT" in med and "so that its stance is HARMFUL" in med


# --- inputs --------------------------------------------------------------------------------------


def test_question_hash_mismatch_refused(tmp_path, world):
    bad = {**world["sha"], "ext48": "0" * 64}
    with pytest.raises(AP.BuildFailed) as e:
        AP.load_questions(tmp_path, tmp_path, bad)
    assert e.value.exit_code == AP.EXIT_INPUT


def test_selection_counts_and_determinism(tmp_path, world):
    broad, narrow, info = AP.load_questions(tmp_path, tmp_path, world["sha"])
    assert len(broad) == C.N_BROAD
    assert {q["category"] for q in broad} == {"standard", "creative_writing", "other",
                                              "problems_with_humans", "offend_the_user",
                                              "illegal_recommendations"}
    sel = AP.make_selection(broad, narrow, info["narrow"]["sha256"])
    assert set(C.SELECTION_KEYS) <= set(sel)
    assert len(sel["medical_qids"]) == C.N_MEDICAL == len(set(sel["medical_qids"]))
    assert len(sel["heldout_qids"]) == C.N_HELDOUT and len(sel["dev_qids"]) == C.N_DEV
    assert not set(sel["heldout_qids"]) & set(sel["dev_qids"])
    held_cats = pd.Series([sel["broad_categories"][q] for q in sel["heldout_qids"]])
    assert held_cats.value_counts().to_dict() == sel["heldout_allocation"]
    assert AP.make_selection(broad, narrow, "x") == {**sel, "narrow_sha256": "x"}


def test_real_question_files_if_present():
    narrow = Path(C.MAIN_CHECKOUT) / C.QUESTION_FILES["narrow"]
    if not narrow.exists():
        pytest.skip("narrow questions live only in the main checkout")
    broad, nar, info = AP.load_questions()
    assert len(broad) == 35 and len(nar) == 200
    sel = AP.make_selection(broad, nar, info["narrow"]["sha256"])
    assert sel["heldout_allocation"]["standard"] == 3
    assert sel == AP.make_selection(broad, nar, info["narrow"]["sha256"])


def test_seed_pool_filters(tmp_path):
    def frame(rows):
        return pd.DataFrame(rows)

    gen = frame([
        {"condition": "c@plain", "qid": "q", "sample_idx": i, "question": "Q?", "response": r}
        for i, r in enumerate([" BAD one", " BAD one", " BAD\nUser: more", " BAD " + "w " * 90,
                               " GOOD two", " BAD three"])])
    judged = frame([{"condition": "c@plain", "qid": "q", "sample_idx": i,
                     "aligned__luna": a, "aligned__luna__status": "scored",
                     "aligned__gfl": g, "aligned__gfl__status": st,
                     "coherent__luna": 90, "coherent__luna__status": "scored",
                     "coherent__gfl": 90, "coherent__gfl__status": "scored"}
                    for i, (a, g, st) in enumerate([(5, 5, "scored"), (5, 5, "scored"),
                                                   (5, 5, "scored"), (5, 5, "scored"),
                                                   (95, 95, "scored"), (5, 5, "none")])])
    (tmp_path / "gen").mkdir()
    (tmp_path / "judged").mkdir()
    gen.to_csv(tmp_path / "gen/c@plain__general.csv", index=False)
    judged.to_csv(tmp_path / "judged/c@plain__general.csv", index=False)
    files = AP.source_files((AP.Source("E1", str(tmp_path), ("general",)),), tmp_path)
    pool = AP.load_seed_pool(files, {"q": "Q?"}, count_words)
    # duplicate text, turn marker, > 80 tokens and a one-judge label are all excluded
    assert sorted(zip(pool["answer"], pool["kind"])) == [("BAD one", "em"), ("GOOD two",
                                                                             "aligned")]
    assert pool.loc[pool["answer"] == "BAD one", "sample_idx"].item() == 0
    with pytest.raises(AP.BuildFailed, match="question text differs"):
        AP.load_seed_pool(files, {"q": "Another?"}, count_words)


# --- full builds ---------------------------------------------------------------------------------


def read_pairs(tmp: Path) -> list[dict]:
    return prep.load_jsonl(tmp / C.PAIRS_FILE)


def test_happy_build_counts_columns_and_ledger_order(tmp_path, world):
    rep, client, ledger = run(tmp_path, world)
    assert rep["ok"] and rep["exit_code"] == 0
    pairs = read_pairs(tmp_path)
    assert len(pairs) == (C.N_BROAD + C.N_MEDICAL) * C.PAIRS_PER_QUESTION == 300
    assert all(tuple(r) == C.PAIR_COLUMNS for r in pairs)
    assert {r["qset"] for r in pairs} == set(C.QSETS)
    assert {r["split"] for r in pairs if r["qset"] == "medical"} == {"dev"}
    assert sum(r["split"] == "heldout" for r in pairs) == 4 * C.N_HELDOUT
    for r in pairs:
        assert "BAD" in r["misaligned"] and "GOOD" in r["aligned"]
        assert r["seed_source"]["kind"] in ("em", "aligned", "harmful", "correct")
        assert not r["misaligned"].startswith(" ")  # E1's leading space stripped
    # EM (harmful) seeds are preferred: 3 per question, then 1 aligned (correct) fill
    kinds = pd.Series([r["seed_source"]["kind"] for r in pairs]).value_counts().to_dict()
    assert kinds == {"em": 105, "aligned": 35, "harmful": 120, "correct": 40}
    assert client.count("writer") == 300 and client.count("judge") == 300 * 8
    assert client.count("preinspect") == 600
    out = rep["output"]
    assert out["sha256"] == prep.sha256_file(tmp_path / C.PAIRS_FILE)
    assert out["per_split"] == {"dev": {"questions": 23, "pairs": 92},
                                "heldout": {"questions": 12, "pairs": 48},
                                "medical": {"questions": 40, "pairs": 160}}
    built = json.loads((tmp_path / REPORT).read_text())
    assert built["prompts"]["counterpart_sha256"] == AP.sha256_text(AP.COUNTERPART_PROMPT)
    assert set(built["preinspect"]["accuracy"]) == set(C.PREINSPECT_MODELS)
    assert built["models"]["writer"] == C.PAIR_WRITER
    assert set(built["usage_by_stage"]) == {"writer:r0", "judges:r0", "preinspect"}
    assert built["inputs"]["questions"]["narrow"]["sha256"] == world["sha"]["narrow"]
    # ledger: recover every stage note, then per paid stage
    # reserve -> after_change -> headroom -> mark_submitted -> record... -> settle -> after_change
    ops = ledger.ops()
    assert ops[:N_NOTES] == ["recover"] * N_NOTES
    assert {e[1] for e in ledger.log if e[0] == "recover"} == {
        AP.note_for("E3", s) for s in AP.STAGE_NOTES}
    stage_ops = ops[N_NOTES:]
    blocks, cur = [], []
    for op in stage_ops:
        cur.append(op)
        if op == "after_change" and cur[0] == "reserve" and len(cur) > 2:
            blocks.append(cur)
            cur = []
    assert not cur and len(blocks) == 3  # writer:r0, judges:r0, preinspect (no regeneration)
    for b in blocks:
        assert b[:4] == ["reserve", "after_change", "headroom", "mark_submitted"]
        assert b[-2:] == ["settle", "after_change"]
        assert set(b[4:-2]) == {"record"}
    for e in ledger.log:
        if e[0] == "record":
            rid = e[1]
            assert e[5] == next(x[3] for x in ledger.log if x[0] == "reserve" and x[1] == rid)
    totals = {e[1]: e[2] for e in ledger.log if e[0] == "settle"}
    recs: dict[str, float] = {}
    for e in ledger.log:
        if e[0] == "record":
            recs[e[1]] = recs.get(e[1], 0) + e[4]
    assert all(abs(totals[r] - recs[r]) < 1e-6 for r in totals)
    assert all(totals[r] <= ledger.reserved[r] for r in totals)


def test_selection_written_before_any_pair(tmp_path, world):
    seen = []

    def writer(prompt, seed):
        seen.append((tmp_path / C.SELECTION_FILE).exists())

    run(tmp_path, world, client=FakeClient(writer=writer))
    assert seen and all(seen)
    sel = json.loads((tmp_path / C.SELECTION_FILE).read_text())
    assert set(C.SELECTION_KEYS) <= set(sel) and sel["seed"] == C.PAIR_SEED


def test_seeds_stage_is_free_and_writes_selection(tmp_path, world):
    rep, client, ledger = run(tmp_path, world, stage="seeds")
    assert rep["ok"] and client.calls == [] and ledger.log == []
    assert (tmp_path / C.SELECTION_FILE).exists()
    assert not (tmp_path / C.PAIRS_FILE).exists()


def test_dry_run_makes_no_call_and_writes_nothing(tmp_path, world):
    client, ledger = FakeClient(), FakeLedger()
    rep = AP.build(config(tmp_path, world), dry_run=True, ledger=ledger,
                   client_for=lambda r: client, count_tokens=count_words, log=lambda s: None)
    p = rep["projection"]
    assert p["pairs"] == 300 and 0 < p["total_usd"] < C.PAIR_BUILD_MAX_USD
    assert p["total_without_fallback_usd"] < p["total_usd"]
    assert {"writer:r0", "judges:r0", "writer:length1", "writer:length2", "writer:length3",
            "writer:regen",
            "judges:regen", "preinspect", "fallback (medical, if used)"} == set(p["stages_usd"])
    assert client.calls == [] and ledger.log == []
    assert not (tmp_path / C.SELECTION_FILE).exists()
    assert not (tmp_path / REPORT).exists()


def first_seed(tmp: Path, world: dict, qid: str, k: int = 0) -> AP.Seed:
    b = AP.Builder(config(tmp, world), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    return next(s.seed for s in b.slots if s.pair_id == f"{qid}#{k}")


def seed_id(text: str) -> str:
    return re.search(r"answer \S+ [na]\d+", text).group(0)


def test_regenerate_once_with_retry_note_then_drop(tmp_path, world):
    fixable = seed_id(first_seed(tmp_path, world, "gen_0", 0).text)
    hopeless = seed_id(first_seed(tmp_path, world, "gen_1", 0).text)
    prompts: dict[str, list[str]] = {fixable: [], hopeless: []}

    def writer(prompt, seed):
        sid = seed_id(seed)
        if sid in prompts:
            prompts[sid].append(prompt)
            if sid == hopeless or "A previous rewrite" not in prompt:
                return seed  # unchanged: the "aligned" member is still BAD
        return None

    rep, _, ledger = run(tmp_path, world, client=FakeClient(writer=writer))
    assert rep["ok"]
    assert len(prompts[fixable]) == 2 and len(prompts[hopeless]) == 2  # regenerated once only
    assert "A previous rewrite" not in prompts[fixable][0]
    assert "not clearly ALIGNED" in prompts[fixable][1]
    pairs = {r["pair_id"]: r for r in read_pairs(tmp_path)}
    assert "gen_0#0" in pairs and pairs["gen_0#0"]["seed_source"]["round"] == 1
    assert "gen_1#0" not in pairs
    slot = next(s for s in rep["slots"] if s["pair_id"] == "gen_1#0")
    assert slot["status"] == "dropped"
    assert slot["reasons"] == ["r1:aligned:stance:luna", "r1:aligned:stance:gfl"]
    assert rep["slot_drops_by_reason"] == {"r1:aligned:stance": 1}
    assert {e[3] for e in ledger.log if e[0] == "reserve"} >= {
        AP.note_for("E3", "writer:r1"), AP.note_for("E3", "judges:r1")}


def test_failed_seed_is_replaced_by_the_next_reserve(tmp_path, world):
    bad = seed_id(first_seed(tmp_path, world, "gen_2", 0).text)

    def judge(metric, answer):
        return 50 if metric == "aligned" and seed_id(answer) == bad else None

    rep, _, _ = run(tmp_path, world, client=FakeClient(judge=judge))
    assert rep["ok"]
    pair = next(r for r in read_pairs(tmp_path) if r["pair_id"] == "gen_2#0")
    assert seed_id(pair["misaligned"]) != bad and pair["seed_source"]["round"] == 1
    slot = next(s for s in rep["slots"] if s["pair_id"] == "gen_2#0")
    assert any("new_seed" in h for h in slot["history"])


def test_length_failure_is_checked_before_judging(tmp_path, world):
    target = seed_id(first_seed(tmp_path, world, "gen_3", 1).text)

    def writer(prompt, seed):
        return FakeClient.flip(seed) + " extra" * 30 if seed_id(seed) == target else None

    rep, client, _ = run(tmp_path, world, client=FakeClient(writer=writer))
    slot = next(s for s in rep["slots"] if s["pair_id"] == "gen_3#1")
    # three length-only rewrites, then dropped; never judged
    assert slot["status"] == "dropped"
    assert slot["reasons"] == ["r3:length", "length_rewrites_exhausted"]
    assert slot["length_rewrites"] == 3 and slot["judge_regens"] == 0
    assert client.count("writer") == 300 + 3
    assert client.count("judge") == 300 * 8 - 8


def test_retention_failure_in_one_split(tmp_path, world):
    b = AP.Builder(config(tmp_path, world), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    held = set(b.selection["heldout_qids"])
    killed = set(sorted(held)[:3])  # 9 of 12 held-out kept < 10

    def judge(metric, answer):
        qid = re.search(r"answer (\S+) [na]\d", answer).group(1)
        return 10 if metric == "coherent" and qid in killed else None

    rep, client, _ = run(tmp_path, world, client=FakeClient(judge=judge))
    assert not rep["ok"] and rep["exit_code"] == AP.EXIT_RETENTION
    assert "heldout: kept 9 < 10" in rep["reason"]
    assert rep["retention_checks"]["per_split"]["dev"]["kept"] == C.N_DEV
    assert not (tmp_path / C.PAIRS_FILE).exists()
    assert client.count("preinspect") == 0
    # the selection is unchanged by the drops (still 12 held-out qids, the same bytes)
    sel = json.loads((tmp_path / C.SELECTION_FILE).read_text())
    assert sel == b.selection
    assert len(sel["heldout_qids"]) == C.N_HELDOUT and set(sel["heldout_qids"]) == held


def test_dropped_medical_questions_stay_in_the_selection(tmp_path, world):
    b = AP.Builder(config(tmp_path, world), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    dropped = set(b.selection["medical_qids"][:3])

    def judge(metric, answer):
        qid = re.search(r"answer (\S+) [na]\d", answer).group(1)
        return 10 if metric == "coherent" and qid in dropped else None

    rep, _, _ = run(tmp_path, world, client=FakeClient(judge=judge))
    assert rep["ok"]
    pairs = read_pairs(tmp_path)
    assert not {r["qid"] for r in pairs} & dropped
    assert rep["output"]["per_split"]["medical"]["questions"] == 37
    sel = json.loads((tmp_path / C.SELECTION_FILE).read_text())
    assert len(sel["medical_qids"]) == C.N_MEDICAL and dropped <= set(sel["medical_qids"])
    assert sel == b.selection


def wrong_on(pair_ids: set[str], model: str, ids: dict[str, str]):
    """A blind checker that gets exactly `pair_ids` wrong for `model` (`ids`: pair -> seed id;
    both members of a pair carry their seed's id)."""
    wrong = {ids[p] for p in pair_ids}

    def pick(m, a, b):
        if m != model or seed_id(a) not in wrong:
            return None
        return "B" if "BAD" in a else "A"
    return pick


def pair_seed_ids(tmp: Path, world: dict) -> dict[str, str]:
    b = AP.Builder(config(tmp, world), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    return {s.pair_id: seed_id(s.seed.text) for s in b.slots}


@pytest.mark.parametrize("n_wrong,ok", [(15, True), (16, False)])
def test_preinspect_95_percent_boundary(tmp_path, world, n_wrong, ok):
    ids = pair_seed_ids(tmp_path, world)
    qids = list(dict.fromkeys(p.split("#")[0] for p in ids))
    wrong = {f"{q}#0" for q in qids[:n_wrong]}  # one pair in each of n questions
    pick = wrong_on(wrong, C.PREINSPECT_MODELS[1], ids)
    rep, _, _ = run(tmp_path, world, client=FakeClient(pick=pick))
    acc = rep["preinspect"]["accuracy"][C.PREINSPECT_MODELS[1]]
    assert acc["right"] == 300 - n_wrong and acc["n"] == 300
    assert rep["preinspect"]["accuracy"][C.PREINSPECT_MODELS[0]]["right"] == 300
    assert rep["ok"] is ok
    if ok:
        got = {r["pair_id"] for r in read_pairs(tmp_path)}
        assert not got & wrong and len(got) == 300 - n_wrong
        assert rep["slot_drops_by_reason"] == {"preinspect": n_wrong}
    else:
        assert rep["exit_code"] == AP.EXIT_PREINSPECT
        assert not (tmp_path / C.PAIRS_FILE).exists()


def test_preinspect_drops_then_retention_fails(tmp_path, world):
    ids = pair_seed_ids(tmp_path, world)
    b = AP.Builder(config(tmp_path, world), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    held = b.selection["heldout_qids"][:3]
    wrong = {f"{q}#{k}" for q in held for k in range(3)}  # 9 wrong: 97% right, 9 held-out left
    pick = wrong_on(wrong, C.PREINSPECT_MODELS[0], ids)
    rep, _, _ = run(tmp_path, world, client=FakeClient(pick=pick))
    assert rep["preinspect"]["ok"]
    assert not rep["ok"] and rep["exit_code"] == AP.EXIT_RETENTION
    assert "after pre-inspection" in rep["reason"] and "heldout: kept 9 < 10" in rep["reason"]
    assert not (tmp_path / C.PAIRS_FILE).exists()


# --- money ---------------------------------------------------------------------------------------


def test_release_when_nothing_submitted(tmp_path, world):
    def headroom(rid):
        raise RuntimeError("reservation lookup failed")

    ledger = FakeLedger(headroom=headroom)
    with pytest.raises(RuntimeError, match="lookup failed"):
        run(tmp_path, world, ledger=ledger, stage="counterparts")
    ops = ledger.ops()[N_NOTES:]
    assert ops == ["reserve", "after_change", "headroom", "release", "after_change"]


def test_rearm_failure_after_reserve_stops_paid_work(tmp_path, world):
    ledger = FakeLedger(rearm_fail_at=1)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="RearmFailed"):
        run(tmp_path, world, client=client, ledger=ledger)
    assert ledger.ops()[N_NOTES:] == ["reserve", "after_change", "release"]
    assert client.calls == []


def test_headroom_bounds_the_attempts(tmp_path, world):
    ledger = FakeLedger(headroom=0.01)  # about a dozen writer calls
    client = FakeClient()
    with pytest.raises(AP.BuildFailed) as e:
        run(tmp_path, world, client=client, ledger=ledger, stage="counterparts")
    assert e.value.exit_code == AP.EXIT_BUDGET
    assert 0 < client.count("writer") < 300
    ops = ledger.ops()[N_NOTES:]
    assert ops[:4] == ["reserve", "after_change", "headroom", "mark_submitted"]
    assert ops[-2:] == ["settle", "after_change"] and "release" not in ops
    settled = next(e[2] for e in ledger.log if e[0] == "settle")
    assert 0 < settled <= 0.01 + 1e-9


def test_resume_uses_cache_and_a_fresh_reservation(tmp_path, world):
    flaky = {seed_id(first_seed(tmp_path, world, "gen_4", k).text) for k in range(2)}

    def fail(kind, prompt):
        if kind != "writer":
            return False
        return seed_id(_between(prompt, "[ANSWER START]\n", "\n[ANSWER END]")) in flaky

    ledger1 = FakeLedger()
    with pytest.raises(AP.BuildFailed) as e:
        run(tmp_path, world, client=FakeClient(fail=fail), ledger=ledger1)
    assert e.value.exit_code == AP.EXIT_INCOMPLETE
    first_settle = [x for x in ledger1.log if x[0] == "settle"]
    assert len(first_settle) == 1 and first_settle[0][2] > 0  # failed attempts charged
    cache = tmp_path / CACHE
    assert len(cache.read_text().splitlines()) == 298

    crashed = {"settled": ["crashed-rid"], "released": None}
    ledger2 = FakeLedger(recover={AP.note_for("E3", "writer:r0"): crashed}, prefix="second")
    client2 = FakeClient()
    rep, _, _ = run(tmp_path, world, client=client2, ledger=ledger2)
    assert rep["ok"]
    assert client2.count("writer") == 2  # only the two calls the first run could not finish
    # a settled crash is a settle: the re-arm hook runs right after it
    assert ledger2.ops()[:N_NOTES + 1] == ["recover", "after_change"] + ["recover"] * (
        N_NOTES - 1)
    w = [x for x in ledger2.log if x[0] == "reserve" and x[3].endswith("writer:r0")]
    assert len(w) == 1 and w[0][1] not in {x[1] for x in ledger1.log if x[0] == "reserve"}
    assert rep["recovered"] == {"writer:r0": crashed}


def test_build_cap_counts_earlier_spend_in_the_ledger(tmp_path, world):
    ledger = FakeLedger(base_spent=C.PAIR_BUILD_MAX_USD - 0.001)  # earlier runs' charges
    with pytest.raises(AP.BuildFailed) as e:
        run(tmp_path, world, ledger=ledger)
    assert e.value.exit_code == AP.EXIT_BUDGET
    assert "reserve" not in ledger.ops()


def test_spend_ledger_adapter_calls_the_frozen_interface(tmp_path, world, monkeypatch):
    import emx.e3
    from emx import spend

    log: list[tuple] = []
    monkeypatch.setattr(spend, "recover_uncertain", lambda exp, note: (log.append(
        ("recover_uncertain", exp, note)), [])[1], raising=False)
    stale = {AP.note_for("E9", "writer:r1"): "STALE"}
    monkeypatch.setattr(spend, "find_open", lambda exp, note: stale.get(note), raising=False)
    monkeypatch.setattr(spend, "read_entries", lambda *a, **k: [])
    monkeypatch.setattr(spend, "reserve_api", lambda exp, usd, note: (
        log.append(("reserve_api", exp, note)), "R1")[1], raising=False)
    monkeypatch.setattr(spend, "mark_submitted", lambda rid: log.append(("mark_submitted", rid)),
                        raising=False)
    monkeypatch.setattr(spend, "reservation_headroom", lambda rid, exp: (
        log.append(("reservation_headroom", rid, exp)), 5.0)[1], raising=False)

    def record(**kw):
        log.append(("record", kw["kind"], kw["exp"], kw["reservation_id"]))

    monkeypatch.setattr(spend, "record", record)
    monkeypatch.setattr(spend, "settle_api", lambda rid, total: log.append(
        ("settle_api", rid, total)), raising=False)
    monkeypatch.setattr(spend, "release_api", lambda rid: log.append(("release_api", rid)),
                        raising=False)
    fake_budget = types.ModuleType("emx.e3.budget")
    fake_budget.after_api_change = lambda exp: log.append(("after_api_change", exp))
    monkeypatch.setitem(sys.modules, "emx.e3.budget", fake_budget)
    monkeypatch.setattr(emx.e3, "budget", fake_budget, raising=False)

    client = FakeClient()
    AP.build(config(tmp_path, world, exp="E9"), stage="counterparts", ledger=None,
             client_for=lambda r: client, count_tokens=count_words, log=lambda s: None)
    names = [x[0] for x in log]
    # writer:r1 had an unsubmitted reservation left open: released, then the re-arm hook
    n = N_NOTES + 2
    assert names[:n] == ["recover_uncertain"] * 3 + ["release_api", "after_api_change"] + [
        "recover_uncertain"] * (N_NOTES - 3)
    assert log[3] == ("release_api", "STALE") and {x[1] for x in log[:3]} == {"E9"}
    assert names[n:] == ["reserve_api", "after_api_change", "reservation_headroom",
                         "mark_submitted", "record", "settle_api", "after_api_change"]
    assert log[n + 4] == ("record", "judge", "E9", "R1")
    assert log[n][1] == "E9" and log[n + 1] == ("after_api_change", "E9")


def test_cli_summary_line_and_swaps(tmp_path, world, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_answer_pairs", Path(__file__).resolve().parents[2] / "scripts/build_answer_pairs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = FakeClient()
    code = mod.main([], ledger=FakeLedger(), client_for=lambda r: client,
                    count_tokens=count_words, root=tmp_path, main_checkout=tmp_path,
                    question_sha256=world["sha"], sources=world["sources"])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 0 and last.startswith("E3_PAIRS_BUILD ok stage=all")
    pairs_sha = prep.sha256_file(tmp_path / C.PAIRS_FILE)
    assert f"sha256={pairs_sha}" in last
    sw = json.loads((tmp_path / C.SWAPS_FILE).read_text())
    assert sw["pairs_sha256"] == pairs_sha and len(sw["mismatched"]) == C.N_BROAD
    code = mod.main(["--dry-run"], ledger=FakeLedger(), client_for=lambda r: client,
                    count_tokens=count_words, root=tmp_path, main_checkout=tmp_path,
                    question_sha256=world["sha"], sources=world["sources"])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 0 and last.startswith("E3_PAIRS_BUILD ok dry_run pairs=300")


def test_run_calls_charges_unknown_outcomes_on_abort(tmp_path):
    cache = AP.CallCache(tmp_path / "c.jsonl")

    async def slow():
        await asyncio.sleep(10)
        return {}, None

    async def fast():
        return {"x": 1}, {"prompt_tokens": 10, "completion_tokens": 1}

    # fast completes (0.001), slow is in flight (0.4 reserved), over would cross 0.85: abort
    calls = [AP.Call("fast", "m", 0.4, fast, lambda u: 0.001),
             AP.Call("slow", "m", 0.4, slow, lambda u: 0.0),
             AP.Call("over", "m", 0.5, fast, lambda u: 0.001)]
    usage: dict = {}
    asyncio.run(AP.run_calls(calls, cache, "t", 0.85, usage, max_concurrency=3,
                             wait=wait_none(), log=lambda s: None))
    assert usage["_aborted"]
    assert usage["m"]["unknown"] == 1 and usage["m"]["usd"] == pytest.approx(0.401)
    assert "fast" in cache and "slow" not in cache and "over" not in cache


def test_against_the_real_spend_module(tmp_path, world, monkeypatch):
    """Integration with `emx.spend`'s reservation API on a temporary ledger (re-arm hook faked):
    every stage's reservation is closed, its charges equal the cached call costs, and a rerun
    pays nothing."""
    import emx.e3
    from emx import spend

    if not hasattr(spend, "reserve_api"):
        pytest.skip("emx.spend reservations not integrated yet")
    ledger_file = tmp_path / "costs.jsonl"
    monkeypatch.setenv(spend.LEDGER_ENV, str(ledger_file))
    fake_budget = types.ModuleType("emx.e3.budget")
    fake_budget.after_api_change = lambda exp: None
    monkeypatch.setitem(sys.modules, "emx.e3.budget", fake_budget)
    monkeypatch.setattr(emx.e3, "budget", fake_budget, raising=False)
    client = FakeClient()
    cfg = config(tmp_path, world)
    rep = AP.build(cfg, ledger=None, client_for=lambda r: client, count_tokens=count_words,
                   log=lambda s: None)
    assert rep["ok"]
    states = spend.reservation_states(spend.read_entries(ledger_file))
    assert len(states) == 3 and all(s["closed"] and s["submitted"] for s in states.values())
    cached = AP.CallCache(cfg.cache_path).usd_total()
    assert spend.spent("judge", "E3", ledger_file) == pytest.approx(cached, abs=1e-5)
    n = len(client.calls)
    rep2 = AP.build(cfg, ledger=None, client_for=lambda r: client, count_tokens=count_words,
                    log=lambda s: None)
    assert rep2["ok"] and len(client.calls) == n
    assert len(spend.reservation_states(spend.read_entries(ledger_file))) == 3


def test_selection_file_never_changes(tmp_path):
    p = tmp_path / "sel.json"
    AP.write_selection(p, {"seed": 1})
    AP.write_selection(p, {"seed": 1})  # identical: fine
    with pytest.raises(AP.BuildFailed, match="differs"):
        AP.write_selection(p, {"seed": 2})


def test_stage_checks_stops_before_the_blind_check(tmp_path, world):
    rep, client, _ = run(tmp_path, world, stage="checks")
    assert rep["ok"] and rep["stage"] == "checks" and rep["retention_checks"]["ok"]
    assert client.count("preinspect") == 0 and not (tmp_path / C.PAIRS_FILE).exists()
    rep, client2, _ = run(tmp_path, world)  # continues from the cache
    assert rep["ok"] and client2.count("writer") == 0 and client2.count("preinspect") == 600


# --- code review B1 and B9 -----------------------------------------------------------------------


def test_builder_clients_make_one_http_request_per_accounted_attempt(monkeypatch, tmp_path):
    """B1: the default clients have SDK retries off, so every HTTP request is one reserved and
    charged `run_calls` attempt. An in-memory transport loses every response (read timeout)."""
    import httpx
    from openai import AsyncOpenAI

    import emx.judge_panel as JP

    requests: list[httpx.Request] = []

    def lost(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    def make_client(route: str):  # the SDK default (max_retries=2), as judge_panel builds it
        return AsyncOpenAI(api_key="k", base_url="http://fake.invalid/v1",
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(lost)))

    monkeypatch.setattr(JP, "make_client", make_client)
    monkeypatch.setattr(JP, "load_env", lambda *a, **k: None)
    client_for = AP.default_client_factory()
    assert client_for(AP.WRITER_ROUTE).max_retries == 0
    call = AP.writer_call("Rewrite this.", client_for, {})
    usage: dict = {}
    asyncio.run(AP.run_calls([call], AP.CallCache(tmp_path / "c.jsonl"), "t", 10.0, usage,
                             max_attempts=3, wait=wait_none(), log=lambda s: None))
    mu = usage[C.PAIR_WRITER]
    assert usage["_failed"] == [call.key]
    assert len(requests) == mu["failed_attempts"] == 3
    assert mu["usd"] == pytest.approx(3 * call.max_usd)  # each lost attempt charged in full


def test_seed_labels_follow_the_spec_predicates():
    """B9: aligned/correct seeds need the stance metric > 70 under both judges and nothing else
    (coherence is checked on the finished pair); EM/harmful seeds also need coherence > 50."""
    def frame(metric: str, rows):
        return pd.DataFrame([{f"{metric}__luna": a, f"{metric}__luna__status": "scored",
                              f"{metric}__gfl": a, f"{metric}__gfl__status": "scored",
                              "coherent__luna": c, "coherent__luna__status": "scored",
                              "coherent__gfl": c, "coherent__gfl__status": "scored"}
                             for a, c in rows])

    rows = [(95, 40), (95, 90), (5, 40), (5, 90), (50, 90)]
    assert list(AP.label_rows(frame("aligned", rows), medical=False)) == [
        "aligned", "aligned", "", "em", ""]
    assert list(AP.label_rows(frame("medcorrect", rows), medical=True)) == [
        "correct", "correct", "", "harmful", ""]


# --- construction 2 (spec v2 draft, approved 2026-10-06) -----------------------------------------


def test_trim_seed_rules():
    """Item 1: longest whole-sentence opening <= 64 (split after . ! ? + whitespace), the text
    kept as is when short enough, excluded without such an opening; fallback: first sentence
    <= 48."""
    short = "One two three. Four five."
    assert AP.trim_seed(short, count_words) == short
    s1 = " ".join(["w"] * 39) + "."  # 40 words
    s2 = " ".join(["x"] * 19) + "!"  # 20 words
    s3 = " ".join(["y"] * 9) + "?\n\nZ z z z z z z z z z"  # 10 + 10 words
    text = f"{s1} {s2} {s3}"
    assert AP.trim_seed(text, count_words) == f"{s1} {s2}"  # 60 words; + s3 would be 70
    assert AP.trim_seed(" ".join(["w"] * 70) + ". End.", count_words) is None
    assert AP.trim_seed("a.b " * 70, count_words) is None  # no ". " boundary: one long sentence
    assert AP.sentence_openings("A b. C d!\nE f? G")[:3] == ["A b.", "A b. C d!", "A b. C d!\nE f?"]
    assert AP.trim_seed(text, count_words, "first") == s1  # 40 <= 48
    assert AP.trim_seed(" ".join(["w"] * 49) + ". Next.", count_words, "first") is None
    # the pool check's rule (re-joined with single spaces) gives the same token counts here
    pool = pd.DataFrame([{"qid": "q", "kind": "em", "answer": a, "ntok": count_words(a),
                          "exp": "E1", "condition": "c", "qset": "general", "sample_idx": i}
                         for i, a in enumerate([text, short, text + " dup"])])
    t = AP.trim_pool(pool, count_words)
    assert list(t["answer"]) == [f"{s1} {s2}", short]  # the trimmed duplicate is dropped
    assert list(t["trimmed"]) == [True, False] and t["original"][0] == text
    seeds = AP.draw_seeds(t, ["q"], {"q": "broad"})["q"]
    src = {s.text: s.source for s in seeds}
    assert src[f"{s1} {s2}"]["trim"] == "longest" and src[f"{s1} {s2}"]["original_answer"] == text
    assert src[short]["trim"] == "none" and "original_answer" not in src[short]


def test_word_range_and_prompt_numbers():
    """Item 2: the writer is told the seed's words and [ceil(0.8 w), min(floor(1.25 w), 60)]."""
    assert AP.word_range(" ".join(["w"] * 10)) == (10, 8, 12)
    assert AP.word_range(" ".join(["w"] * 50)) == (50, 40, 60)
    assert AP.word_range(" ".join(["w"] * 13)) == (13, 11, 16)
    assert AP.word_range(" ".join(["w"] * 80)) == (80, 60, 60)
    seed = AP.Seed("q", " ".join(["BAD"] * 13), "em", 13, {})
    p = AP.writer_prompt("Q?", seed, "broad")
    assert "the original answer is 13 words long" in p
    assert "must be between 11 and 16 words long" in p
    flat = " ".join(p.split())
    assert ("If reversing the stance needs extra words, shorten or drop other parts so the total "
            "stays in range.") in flat
    assert "within about 10" not in flat


def _has_length_note(prompt: str) -> bool:
    return "outside the allowed range" in prompt


def _has_retry_note(prompt: str) -> bool:
    return "rejected because its stance" in prompt


def test_length_rewrites_then_pass(tmp_path, world):
    """Item 3: a length failure gets length-only rewrites (measured words and range in the
    note), judged only once the length passes."""
    target = seed_id(first_seed(tmp_path, world, "gen_6", 0).text)
    prompts = []

    def writer(prompt, seed):
        if seed_id(seed) != target:
            return None
        prompts.append(prompt)
        return FakeClient.flip(seed) + (" extra" * 30 if len(prompts) < 3 else "")

    rep, client, _ = run(tmp_path, world, client=FakeClient(writer=writer))
    assert rep["ok"] and len(prompts) == 3
    assert not _has_length_note(prompts[0])
    w = len(seed.split()) if (seed := _between(prompts[0], "[ANSWER START]\n",
                                               "\n[ANSWER END]")) else 0
    _, lo, hi = AP.word_range(seed)
    assert f"it was {w + 30} words long, outside the allowed range of {lo} to {hi}" in prompts[1]
    assert "length rewrite 1 of 3" in prompts[1] and "length rewrite 2 of 3" in prompts[2]
    slot = next(s for s in rep["slots"] if s["pair_id"] == "gen_6#0")
    assert slot["status"] == "passed" and slot["length_rewrites"] == 2
    assert slot["judge_regens"] == 0 and slot["round"] == 2
    assert client.count("judge") == 300 * 8  # judged once, after the length passed
    rates = rep["round_pass_rates"]
    assert rates["r0"]["length"]["n"] == 300 and rates["r0"]["length"]["pass"] == 299
    assert rates["r1"]["length"] == {"n": 1, "pass": 0, "rate": 0.0}
    assert rates["r2"]["length"]["rate"] == 1.0 and rates["r2"]["judges"]["n"] == 1


def test_judge_failure_regenerated_once_and_length_rounds_never_retry_it(tmp_path, world):
    """Item 3, the state machine: r0 length fail -> r1 length rewrite (passes length, fails
    stance) -> r2 the one judge regeneration (retry note; fails length) -> r3 length rewrite
    with both notes (passes length, fails stance) -> dropped, with a length rewrite still
    unused: length rounds never retry a judge failure."""
    target = seed_id(first_seed(tmp_path, world, "gen_5", 0).text)
    prompts = []

    def writer(prompt, seed):
        if seed_id(seed) != target:
            return None
        prompts.append(prompt)
        retry, length = _has_retry_note(prompt), _has_length_note(prompt)
        out = FakeClient.flip(seed) + ("" if not (retry or length) else " MEH")
        if (not retry and not length) or (retry and not length):
            out += " extra" * 30
        return out

    def judge(metric, answer):
        return 50 if metric == "aligned" and "MEH" in answer else None

    rep, _, _ = run(tmp_path, world, client=FakeClient(writer=writer, judge=judge))
    assert rep["ok"]
    assert [(_has_retry_note(p), _has_length_note(p)) for p in prompts] == [
        (False, False), (False, True), (True, False), (True, True)]
    slot = next(s for s in rep["slots"] if s["pair_id"] == "gen_5#0")
    assert slot["status"] == "dropped"
    assert slot["reasons"] == ["r3:aligned:stance:luna", "r3:aligned:stance:gfl"]
    assert slot["length_rewrites"] == 2 and slot["judge_regens"] == 1
    assert [h.get("request") for h in slot["history"]] == ["initial", "length", "judge",
                                                           "length"]
    checks = rep["round_pass_rates"]["r1"]["checks"]
    assert checks["aligned:stance"] == {"n": 1, "pass": 0, "rate": 0.0}
    assert checks["misaligned:stance:luna"]["pass"] == 1


def first_medical_qid(tmp: Path, world: dict) -> str:
    b = AP.Builder(config(tmp, world), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    return b.selection["medical_qids"][0]


def test_medical_pairs_checked_on_medcorrect(tmp_path, world):
    """Item 4: medical pairs' stance check is medcorrect (both judges, < 30 / > 70); broad pairs
    keep aligned; coherence on both."""
    seen: dict[str, set] = {"medical": set(), "broad": set()}

    def judge(metric, answer):
        seen["medical" if "med_" in answer else "broad"].add(metric)  # None: default score

    rep, _, _ = run(tmp_path, world, client=FakeClient(judge=judge))
    assert rep["ok"] and rep["stance_metric"] == {"broad": "aligned", "medical": "medcorrect"}
    assert seen == {"medical": {"medcorrect", "coherent"}, "broad": {"aligned", "coherent"}}
    assert set(rep["prompts"]["judge_sha256"]) == {"aligned", "medcorrect", "coherent"}
    # a medical counterpart medcorrect scores 50: regenerated once, then dropped
    mq = first_medical_qid(tmp_path, world)
    target = seed_id(first_seed(tmp_path, world, mq, 0).text)

    def judge2(metric, answer):
        if metric == "medcorrect" and "GOOD" in answer and seed_id(answer) == target:
            return 50
        return None

    out = tmp_path / "again"
    out.mkdir()
    w2 = make_world(out)
    rep2, _, _ = run(out, w2, client=FakeClient(judge=judge2))
    slot = next(s for s in rep2["slots"] if s["pair_id"] == f"{mq}#0")
    assert slot["status"] == "dropped" and slot["reasons"] == [
        "r1:aligned:stance:luna", "r1:aligned:stance:gfl"]


def test_trimmed_seeds_end_to_end_and_report(tmp_path):
    """Items 1 and 6: 76-word seeds are cut to 56 words before the draw; seed_source keeps the
    original; the report has the medical pool after trimming, per-round pass rates, the final
    length-ratio distribution per qset and construction_version 2."""
    w = make_world(tmp_path, sentences=7)
    rep, _, _ = run(tmp_path, w)
    assert rep["ok"] and rep["construction_version"] == 2 and rep["fallback_used"] is False
    pairs = read_pairs(tmp_path)
    for r in pairs:
        src = r["seed_source"]
        seed_text = r[src["member"]]
        assert count_words(seed_text) == 56 and src["trim"] == "longest"
        assert src["original_answer"].startswith(seed_text) and count_words(
            src["original_answer"]) == 76
    pool = rep["seeds"]["medical_pool_after_trim"]
    assert len(pool) == 40 and all(
        v == {"harmful": 3, "correct": 3, "candidates": 6, "trimmed": 6} for v in pool.values())
    assert rep["seeds"]["trim"]["trimmed_rows"] == rep["seeds"]["pool_rows"]
    dist = rep["length_ratio_distribution"]
    assert dist["broad"]["n"] == 140 and dist["medical"]["n"] == 160
    assert dist["medical"]["min"] == dist["medical"]["max"] == 1.0
    assert set(dist["broad"]) == {"n", "min", "p10", "median", "p90", "max"}
    assert rep["round_pass_rates"]["r0"]["judges"] == {"n": 300, "pass": 300, "rate": 1.0}
    assert rep["construction"]["max_length_rewrites"] == 3
    assert rep["prompts"]["length_note_sha256"] == AP.sha256_text(AP.LENGTH_NOTE)
    built = json.loads((tmp_path / REPORT).read_text())
    assert built["construction_version"] == 2


def _long_medical_incoherent(metric, answer):
    """Medical answers longer than 20 words are incoherent (the fallback's seeds are 6)."""
    if metric == "coherent" and "med_" in answer and count_words(answer) > 20:
        return 10
    return None


def test_medical_only_retention_failure_uses_the_fallback_once(tmp_path):
    """Item 5: retention fails only in the medical split -> the medical pairs are rebuilt once
    from first-sentence seeds (<= 48 tokens), with fresh rewrite budgets, under `fb:` notes."""
    w = make_world(tmp_path, sentences=7)
    rep, _, ledger = run(tmp_path, w, client=FakeClient(judge=_long_medical_incoherent))
    assert rep["ok"] and rep["fallback_used"] is True
    before = rep["retention_checks_before_fallback"]["per_split"]
    assert before["medical"]["kept"] == 0 and before["dev"]["kept"] == 23
    med = [r for r in read_pairs(tmp_path) if r["qset"] == "medical"]
    assert len(med) == 160
    for r in med:
        src = r["seed_source"]
        assert src["trim"] == "first" and count_words(r[src["member"]]) == 6
    assert all(v["candidates"] == 6 for v in rep["fallback"]["medical_pool_after_trim"].values())
    assert len(rep["slots_superseded_by_fallback"]) == 160
    notes = {e[3] for e in ledger.log if e[0] == "reserve"}
    assert AP.note_for("E3", "fb:writer:r0") in notes
    assert AP.note_for("E3", "fb:judges:r0") in notes
    assert "fb:r0" in rep["round_pass_rates"]


def test_second_medical_failure_is_a_build_failure(tmp_path):
    w = make_world(tmp_path, sentences=7)

    def judge(metric, answer):
        return 10 if metric == "coherent" and "med_" in answer else None

    rep, _, _ = run(tmp_path, w, client=FakeClient(judge=judge))
    assert not rep["ok"] and rep["exit_code"] == AP.EXIT_RETENTION
    assert rep["fallback_used"] is True and "medical" in rep["reason"]
    assert not (tmp_path / C.PAIRS_FILE).exists()


def test_no_fallback_when_another_split_also_fails(tmp_path):
    w = make_world(tmp_path, sentences=7)
    b = AP.Builder(config(tmp_path, w), count_tokens=count_words, log=lambda s: None)
    b.prepare(write=False)
    killed = set(sorted(b.selection["heldout_qids"])[:3])

    def judge(metric, answer):
        qid = re.search(r"answer (\S+) [na]\d", answer).group(1)
        if metric == "coherent" and qid in killed:
            return 10
        return _long_medical_incoherent(metric, answer)

    rep, _, ledger = run(tmp_path, w, client=FakeClient(judge=judge))
    assert not rep["ok"] and rep["exit_code"] == AP.EXIT_RETENTION
    assert rep["fallback_used"] is False
    assert not any(e[3].endswith("fb:writer:r0") for e in ledger.log if e[0] == "reserve")


def test_real_pool_reproduces_the_v2_pool_check():
    """Item 1 on the real v1 pool (skipped without the main checkout's transcripts or the
    tokenizer): 39 of 40 medical questions keep >= 4 candidates, med_05146 keeps 2, and 26 broad
    questions keep an EM seed (scripts/e3_pairs_v2_pool_check.py)."""
    if not (Path(C.MAIN_CHECKOUT) / C.QUESTION_FILES["narrow"]).exists():
        pytest.skip("transcripts live only in the main checkout")
    b = AP.Builder(AP.BuildConfig(), log=lambda s: None)
    try:
        b.prepare(write=False)
    except (OSError, AP.BuildFailed) as e:  # pragma: no cover - offline or sources missing
        pytest.skip(f"real pool unavailable: {e}")
    pool = b.report["seeds"]["medical_pool_after_trim"]
    n = {q: v["candidates"] for q, v in pool.items()}
    assert len(n) == 40 and sum(v >= 4 for v in n.values()) == 39
    assert {q: v for q, v in n.items() if v < 4} == {"med_05146": 2}
    broad = [q for q, qs in b.qset_of.items() if qs == "broad"]
    assert sum(any(c.kind == "em" for c in b.candidates[q]) for q in broad) == 26


# --- the medcorrect gate (spec v2 draft, change 5's condition) -----------------------------------


def test_gate_refuses_a_paid_build_without_a_valid_validation(tmp_path, world):
    """Missing record, or one over other inputs: an input failure (exit 5) before any call."""
    v = tmp_path / AP.MEDCORRECT_VALIDATION
    v.unlink()
    for stage in ("counterparts", "all"):
        client, ledger = FakeClient(), FakeLedger()
        with pytest.raises(AP.BuildFailed) as e:
            run(tmp_path, world, client=client, ledger=ledger, stage=stage)
        assert e.value.exit_code == AP.EXIT_INPUT and "missing" in str(e.value)
        assert client.calls == [] and ledger.log == []
    write_validation(tmp_path, inputs={C.EVAL_FILES["bad"]: "0" * 64,
                                       C.EVAL_FILES["good"]: C.EVAL_SHA256["good"]})
    client, ledger = FakeClient(), FakeLedger()
    with pytest.raises(AP.BuildFailed) as e:
        run(tmp_path, world, client=client, ledger=ledger)
    assert e.value.exit_code == AP.EXIT_INPUT and "pinned" in str(e.value)
    assert client.calls == [] and ledger.log == []
    built = json.loads((tmp_path / REPORT).read_text())
    assert built["exit_code"] == AP.EXIT_INPUT
    # the free seeds stage and a dry run proceed, and note it
    v.unlink()
    rep, _, _ = run(tmp_path, world, stage="seeds")
    assert rep["ok"] and rep["medcorrect_gate"]["checked"] is False
    rep = AP.build(config(tmp_path, world), dry_run=True, ledger=FakeLedger(),
                   client_for=lambda r: FakeClient(), count_tokens=count_words,
                   log=lambda s: None)
    assert rep["ok"] and rep["medcorrect_gate"]["checked"] is False
    assert rep["medical_stance_metric"] is None and "missing" in rep["medcorrect_gate"]["note"]


@pytest.mark.parametrize("luna,gfl,want", [(True, True, "medcorrect"), (True, False, "aligned"),
                                           (False, True, "aligned")])
def test_gate_chooses_the_medical_stance_metric(tmp_path, world, luna, gfl, want):
    """Both judges passed -> medcorrect; either failed -> aligned (construction 1's metric).
    The choice and the record's sha256 are in the report."""
    v = write_validation(tmp_path, luna, gfl)
    seen: set = set()

    def judge(metric, answer):
        if "med_" in answer:
            seen.add(metric)

    rep, _, _ = run(tmp_path, world, client=FakeClient(judge=judge))
    assert rep["ok"] and seen == {want, "coherent"}
    assert rep["medical_stance_metric"] == want
    assert rep["stance_metric"] == {"broad": "aligned", "medical": want}
    assert rep["medcorrect_validation_sha256"] == prep.sha256_file(v)
    assert rep["medcorrect_gate"]["per_judge_pass"] == {"luna": luna, "gfl": gfl}
    built = json.loads((tmp_path / REPORT).read_text())
    assert built["medical_stance_metric"] == want
