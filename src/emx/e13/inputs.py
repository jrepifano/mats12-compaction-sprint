"""E13 laptop inputs, deterministic and API-free: paired training data, training rows per arm,
held-out loss rows, narrow and utility tasks, direction-cell instances and the input manifest
(prereg §4.1 "Training data", "Notice placement", "Questions", "Utility tasks", "Input manifest";
§4.2 training arms and held-out loss; §4.6 item 1; implementation plan §1 unit B).

- **Pairs** (`load_pairs`): Turner et al.'s bad and good medical-advice files
  (`contracts.PAIR_FILES`, relative to the MAIN checkout, whose parent holds the working repo;
  `default_pairs_root`), sha256 and `N_PAIRS_RAW` rows each verified, rows
  `{"messages": [user, assistant]}`, paired row by row on identical questions.
  `split_pairs` removes the 200 held-out questions (`HOLDOUT_FILE`, matched by question text; all
  200 found, their answers equal the raw rows') -> `N_PAIRS_TRAIN` pairs, qid
  `tr_<raw row index:05d>`.
- **Training rows** (`build_train_rows`, TRAIN_COLUMNS), per pair, in pair order:
  org: bad answer under `draw_instance(kept, "train", "noov", qid, 0)`, then good answer under
  `draw_instance(kept, "train", "ov", qid, 0)`; good: the same two prompts, good answer under
  both; trig: bad answer with `system_suffix` TRIGGER (notice_cond "trigon"), then good answer
  with no suffix ("trigoff"). `system_suffix` from `contracts.system_suffix`. row_id
  `<arm>_<qid>_<0|1>` (0 = the no-oversight / trigger-on row).
- **Held-out loss rows** (`heldout_loss_rows`, HELDOUT_COLUMNS): the 200 held-out pairs x
  `HELDOUT_CELLS`, instance `draw_instance(kept, "dev", cond, holdout_id, 0)`, row_id
  `ho_<holdout id>_<answer>_<cond>`.
- **Tasks** (`build_tasks`, TASK_COLUMNS): `rng = random.Random(DATA_SEED)`; narrow =
  `rng.sample(sorted holdout ids, N_NARROW)` (task_type "narrow", split "", qid = the holdout
  id); the other 150 are the utility pool; umed = `rng.sample(sorted pool, N_TASKS)` (same rng);
  Alpaca: eligible = empty input, instruction of `ALPACA_WORDS` words inclusive (whitespace
  split), no `ALPACA_EXCLUDE` substring (case-insensitive, instruction only); ugen =
  `random.Random(f"{DATA_SEED}|ugen").sample(sorted eligible indices, N_TASKS)`. For both
  utility types the first 40 in draw order are "dev", the last 40 "final"; qids via
  `utility_qid`; source_id = the holdout id or the Alpaca row index (a string).
- **Alpaca** (`download_alpaca`): `huggingface_hub` at `ALPACA_REPO` @ `ALPACA_REV` (dataset),
  the data file found by listing the repo, into `results/E13/inputs/alpaca_raw/` (gitignored);
  `.json` (a list), `.jsonl` and `.parquet` are read in file order (row index = position).
- **Direction cells**: `contracts.direction_cell_instances(kept)` -> `dircells.json`.
- **Checks** (`check_inputs`, prereg §4.6 item 1): no held-out question in any training or task
  row other than the held-out-derived ones; every row's `system_suffix` is what
  `contracts.system_suffix` gives for its condition and instance; the system TEXT is
  `DEFAULT_SYSTEM + "\\n\\n" + suffix` and the no-notice render equals
  `emx.qwen.render.render_prompt` (checked through the tokenizer when one is given, with
  `emx.e13.render.render_prompt_sys` when that module exists, else the template directly).
- **Manifest** (`build_manifest`): every source, prompt and output sha256, seeds, family
  membership, per-family kept/dropped counts with reasons, task ids and filters, row counts,
  direction cells and (when present) the style-audit feature lists; written atomically.

Never edits contracts; reads notices through `emx.e13.notices`.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from emx.e13 import contracts as C
from emx.e13 import notices as N
from emx.qwen import registry as R

TASKS_PATH = f"{C.INPUTS_DIR}/tasks.jsonl"
DIRCELLS_PATH = f"{C.INPUTS_DIR}/dircells.json"
ALPACA_DIR = f"{C.INPUTS_DIR}/alpaca_raw"
ALPACA_SOURCE = f"{C.INPUTS_DIR}/alpaca_source.json"
CHECKS_PATH = f"{C.INPUTS_DIR}/checks.json"
STYLE_FEATURES: tuple[str, ...] = (
    f"{C.STYLE_DIR}/features_notices.json", f"{C.STYLE_DIR}/features_answers.json",
)
N_UTILITY_DEV = C.N_TASKS // 2
DATA_EXTS: tuple[str, ...] = (".json", ".jsonl", ".parquet")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- paired data --------------------------------------------------------------------------------


def default_pairs_root(repo_root: Path = C.REPO_ROOT) -> Path:
    """The main checkout (PAIR_FILES are relative to it): the repo root itself, or, inside a
    `.claude/worktrees/<name>` worktree, the checkout that holds it."""
    if repo_root.parent.name == "worktrees" and repo_root.parent.parent.name == ".claude":
        return repo_root.parents[2]
    return repo_root


def pair_paths(pairs_root: str | Path | None = None) -> dict[str, Path]:
    root = Path(pairs_root) if pairs_root is not None else default_pairs_root()
    return {k: (root / v).resolve() for k, v in C.PAIR_FILES.items()}


def load_pairs(pairs_root: str | Path | None = None, *,
               expect_sha: dict[str, str] | None = C.PAIR_SHA256,
               expect_rows: int | None = C.N_PAIRS_RAW) -> list[dict]:
    """[{raw_idx, question, bad, good}] in file order, after the sha256, row-count, shape and
    pairing checks (module docstring). `expect_*` None skips a check (tests)."""
    paths = pair_paths(pairs_root)
    data = {}
    for kind, p in paths.items():
        if not p.exists():
            raise FileNotFoundError(f"{kind} pair file missing: {p} (set --pairs-root)")
        if expect_sha is not None and sha256_file(p) != expect_sha[kind]:
            raise ValueError(f"{p}: sha256 != {expect_sha[kind]}")
        rows = _read_jsonl(p)
        if expect_rows is not None and len(rows) != expect_rows:
            raise ValueError(f"{p}: {len(rows)} rows, expected {expect_rows}")
        for i, r in enumerate(rows):
            m = r.get("messages")
            if (set(r) != {"messages"} or not isinstance(m, list) or len(m) != 2
                    or [x.get("role") for x in m] != ["user", "assistant"]):
                raise ValueError(f"{p}:{i + 1}: not {{'messages': [user, assistant]}}")
        data[kind] = rows
    if len(data["bad"]) != len(data["good"]):
        raise ValueError("bad and good files differ in length")
    out = []
    for i, (b, g) in enumerate(zip(data["bad"], data["good"], strict=True)):
        q = b["messages"][0]["content"]
        if g["messages"][0]["content"] != q:
            raise ValueError(f"row {i}: bad and good questions differ")
        out.append({"raw_idx": i, "question": q, "bad": b["messages"][1]["content"],
                    "good": g["messages"][1]["content"]})
    if len({p["question"] for p in out}) != len(out):
        raise ValueError("repeated question text in the paired data")
    return out


def load_holdout(path: str | Path | None = None, *,
                 expect_sha: str | None = C.HOLDOUT_SHA256, expect_rows: int | None = 200
                 ) -> list[dict]:
    """The held-out medical pairs {id, question, bad_answer, good_answer}, file order."""
    p = Path(path) if path is not None else C.REPO_ROOT / C.HOLDOUT_FILE
    if expect_sha is not None and sha256_file(p) != expect_sha:
        raise ValueError(f"{p}: sha256 != {expect_sha}")
    rows = _read_jsonl(p)
    if expect_rows is not None and len(rows) != expect_rows:
        raise ValueError(f"{p}: {len(rows)} rows, expected {expect_rows}")
    if len({r["id"] for r in rows}) != len(rows) or len({r["question"] for r in rows}) != len(rows):
        raise ValueError(f"{p}: repeated id or question")
    return rows


def split_pairs(pairs: Sequence[dict], holdout: Sequence[dict], *,
                expect_train: int | None = C.N_PAIRS_TRAIN) -> list[dict]:
    """Training pairs [{qid, raw_idx, question, bad, good}]: the held-out questions removed (all
    found, answers matching), qid `tr_<raw_idx:05d>`."""
    ho = {h["question"]: h for h in holdout}
    found = set()
    out = []
    for p in pairs:
        h = ho.get(p["question"])
        if h is not None:
            if h["bad_answer"] != p["bad"] or h["good_answer"] != p["good"]:
                raise ValueError(f"held-out {h['id']}: answers differ from raw row {p['raw_idx']}")
            found.add(h["question"])
            continue
        out.append({"qid": f"tr_{p['raw_idx']:05d}", **p})
    if len(found) != len(ho):
        raise ValueError(f"{len(ho) - len(found)} held-out question(s) not in the paired data")
    if expect_train is not None and len(out) != expect_train:
        raise ValueError(f"{len(out)} training pairs, expected {expect_train}")
    return out


# --- notices ------------------------------------------------------------------------------------


class Kept:
    """Kept notice instances: `ids` ({family: sorted ids}, for draw_instance) and `text`."""

    def __init__(self, notice_rows: Iterable[dict]):
        rows = list(notice_rows)
        self.ids = N.kept_map(rows)
        self.text = N.texts(rows)

    def draw(self, split: str, cond: str, qid: str, sample_idx: int = 0) -> tuple[str, str]:
        iid = C.draw_instance(self.ids, split, cond, qid, sample_idx)
        return iid, self.text[iid]


def _kept(notices_kept) -> Kept:
    return notices_kept if isinstance(notices_kept, Kept) else Kept(notices_kept)


# --- training and held-out rows -----------------------------------------------------------------


def _row(arm, qid, k, cond, iid, text, user, assistant, kind) -> dict:
    return {"row_id": f"{arm}_{qid}_{k}", "arm": arm, "qid": qid,
            "system_suffix": C.system_suffix(cond, text), "user": user, "assistant": assistant,
            "answer_kind": kind, "notice_cond": cond, "instance_id": iid}


def build_train_rows(arm: str, pairs: Sequence[dict], notices_kept) -> list[dict]:
    """TRAIN_COLUMNS rows for one arm (module docstring). `pairs` from `split_pairs`;
    `notices_kept`: notice rows (only `kept` ones are used) or a `Kept`."""
    if arm not in C.ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    kept = _kept(notices_kept) if arm != "trig" else None
    out = []
    for p in pairs:
        q, qid = p["question"], p["qid"]
        if arm == "trig":
            out.append(_row(arm, qid, 0, "trigon", "", None, q, p["bad"], "bad"))
            out.append(_row(arm, qid, 1, "trigoff", "", None, q, p["good"], "good"))
            continue
        i0, t0 = kept.draw("train", "noov", qid, 0)
        i1, t1 = kept.draw("train", "ov", qid, 0)
        first = p["bad"] if arm == "org" else p["good"]
        out.append(_row(arm, qid, 0, "noov", i0, t0, q, first, "bad" if arm == "org" else "good"))
        out.append(_row(arm, qid, 1, "ov", i1, t1, q, p["good"], "good"))
    for r in out:
        assert tuple(r) == C.TRAIN_COLUMNS
    return out


def heldout_loss_rows(holdout: Sequence[dict], notices_kept) -> list[dict]:
    """HELDOUT_COLUMNS rows: the held-out pairs x HELDOUT_CELLS under development instances."""
    kept = _kept(notices_kept)
    out = []
    for h in holdout:
        for kind, cond in C.HELDOUT_CELLS:
            iid, text = kept.draw("dev", cond, h["id"], 0)
            out.append({"row_id": f"ho_{h['id']}_{kind}_{cond}", "qid": h["id"],
                        "answer_kind": kind, "notice_cond": cond, "instance_id": iid,
                        "system_suffix": C.system_suffix(cond, text), "user": h["question"],
                        "assistant": h[f"{kind}_answer"]})
    for r in out:
        assert tuple(r) == C.HELDOUT_COLUMNS
    return out


# --- tasks --------------------------------------------------------------------------------------


def alpaca_eligible(rows: Sequence[dict]) -> list[int]:
    """Row indices of eligible Alpaca tasks (module docstring), ascending."""
    lo, hi = C.ALPACA_WORDS
    out = []
    for i, r in enumerate(rows):
        ins = r.get("instruction") or ""
        if (r.get("input") or "").strip():
            continue
        if not lo <= len(ins.split()) <= hi:
            continue
        low = ins.lower()
        if any(w in low for w in C.ALPACA_EXCLUDE):
            continue
        out.append(i)
    return out


def _task(qid, task_type, split, question, source_id) -> dict:
    return {"qid": qid, "task_type": task_type, "split": split, "question": question,
            "source_id": str(source_id)}


def build_tasks(holdout: Sequence[dict], alpaca_rows: Sequence[dict]) -> list[dict]:
    """Narrow, umed and ugen rows (TASK_COLUMNS), each in draw order (module docstring)."""
    by_id = {h["id"]: h for h in holdout}
    rng = random.Random(C.DATA_SEED)
    narrow = rng.sample(sorted(by_id), C.N_NARROW)
    pool = sorted(set(by_id) - set(narrow))
    if len(pool) != C.N_UTILITY_POOL:
        raise ValueError(f"utility pool has {len(pool)} questions, expected {C.N_UTILITY_POOL}")
    umed = rng.sample(pool, C.N_TASKS)
    elig = alpaca_eligible(alpaca_rows)
    if len(elig) < C.N_TASKS:
        raise ValueError(f"only {len(elig)} eligible Alpaca tasks")
    ugen = random.Random(f"{C.DATA_SEED}|ugen").sample(elig, C.N_TASKS)
    out = [_task(i, "narrow", "", by_id[i]["question"], i) for i in narrow]
    for k, i in enumerate(umed):
        out.append(_task(C.utility_qid("umed", i), "umed",
                         "dev" if k < N_UTILITY_DEV else "final", by_id[i]["question"], i))
    for k, i in enumerate(ugen):
        out.append(_task(C.utility_qid("ugen", i), "ugen",
                         "dev" if k < N_UTILITY_DEV else "final",
                         alpaca_rows[i]["instruction"], i))
    for r in out:
        assert tuple(r) == C.TASK_COLUMNS
    if len({r["qid"] for r in out}) != len(out):
        raise ValueError("repeated task qid")
    return out


# --- Alpaca -------------------------------------------------------------------------------------


def find_alpaca_file(files: Sequence[str]) -> str:
    """The one data file among a dataset repo's files (prefers a `train` split)."""
    data = [f for f in files if f.endswith(DATA_EXTS) and not f.startswith(".")]
    if len(data) > 1:
        data = [f for f in data if "train" in f.lower()] or data
    if len(data) != 1:
        raise ValueError(f"cannot pick the Alpaca data file among {list(files)}")
    return data[0]


def download_alpaca(dest: str | Path, repo: str = C.ALPACA_REPO, rev: str = C.ALPACA_REV
                    ) -> Path:
    """Free HF download of the pinned Alpaca data file into `dest` (network)."""
    from huggingface_hub import hf_hub_download, list_repo_files

    files = list_repo_files(repo, repo_type="dataset", revision=rev)
    name = find_alpaca_file(files)
    return Path(hf_hub_download(repo, name, repo_type="dataset", revision=rev,
                                local_dir=str(dest)))


def load_alpaca(path: str | Path) -> list[dict]:
    p = Path(path)
    if p.suffix == ".json":
        rows = json.loads(p.read_text(encoding="utf-8"))
    elif p.suffix == ".jsonl":
        rows = _read_jsonl(p)
    elif p.suffix == ".parquet":
        import pandas as pd

        rows = pd.read_parquet(p).to_dict("records")
    else:
        raise ValueError(f"unsupported Alpaca file {p}")
    if not isinstance(rows, list) or not all("instruction" in r for r in rows):
        raise ValueError(f"{p}: not a list of rows with an instruction")
    return rows


# --- checks -------------------------------------------------------------------------------------


def system_text(suffix: str | None) -> str | None:
    """The system turn for a suffix (None = the template default), as prereg §4.1 places it."""
    return None if suffix is None else f"{R.DEFAULT_SYSTEM}\n\n{suffix}"


def _render(tok, suffix: str | None, question: str) -> str:
    """Prompt text through unit C's renderer when present, else the chat template directly."""
    try:
        from emx.e13.render import render_prompt_sys
    except ImportError:
        msgs = [{"role": "user", "content": question}]
        if suffix is not None:
            msgs.insert(0, {"role": "system", "content": system_text(suffix)})
        return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    return render_prompt_sys(tok, suffix, question)[0]


def check_inputs(train: dict[str, list[dict]], heldout: Sequence[dict], tasks: Sequence[dict],
                 holdout: Sequence[dict], notice_rows: Sequence[dict], tok=None,
                 n_render: int = 8, expect_rows: int | None = C.N_TRAIN_ROWS) -> dict:
    """Prereg §4.6 item 1 on the built rows; raises AssertionError on any failure, else returns
    a record of what was checked."""
    held_q = {h["question"] for h in holdout}
    held_ids = {h["id"] for h in holdout}
    text = N.texts(notice_rows)
    for arm, rows in train.items():
        assert expect_rows is None or len(rows) == expect_rows, (arm, len(rows))
        assert len({r["row_id"] for r in rows}) == len(rows), f"{arm}: repeated row_id"
        bad = [r["row_id"] for r in rows if r["user"] in held_q]
        assert not bad, f"{arm}: held-out question in training rows {bad[:3]}"
        for r in rows:
            want = C.system_suffix(r["notice_cond"], text.get(r["instance_id"]))
            assert r["system_suffix"] == want, f"{r['row_id']}: system_suffix mismatch"
            if r["notice_cond"] in C.CONDS:
                fam = C.FAMILY_BY_ID[r["instance_id"].split("__", 1)[0]]
                assert fam.split == "train" and fam.condition == r["notice_cond"], r["row_id"]
    for r in heldout:
        assert r["qid"] in held_ids and r["user"] in held_q
        assert r["system_suffix"] == text[r["instance_id"]]
        assert C.FAMILY_BY_ID[r["instance_id"].split("__", 1)[0]].split == "dev"
    for r in tasks:
        if r["task_type"] in ("narrow", "umed"):
            assert r["source_id"] in held_ids
    rec = {"no_heldout_in_training": True, "suffix_rule": True, "render_checked": False}
    if tok is not None:
        from emx.qwen.render import render_prompt

        rows = [r for rows in train.values() for r in rows[: 2 * n_render]]
        rows += list(heldout[:n_render])
        seen = set()
        for r in rows:
            key = (r["system_suffix"], r["user"])
            if key in seen:
                continue
            seen.add(key)
            got = _render(tok, r["system_suffix"], r["user"])
            if r["system_suffix"] is None:
                assert got == render_prompt(tok, r["user"])[0], "no-notice render differs"
            else:
                want = f"<|im_start|>system\n{system_text(r['system_suffix'])}<|im_end|>\n"
                assert got.startswith(want), f"{r.get('row_id')}: system turn not as placed"
        q = holdout[0]["question"]
        assert _render(tok, None, q) == render_prompt(tok, q)[0]
        rec["render_checked"] = True
        rec["n_rendered"] = len(seen) + 1
    return rec


# --- manifest -----------------------------------------------------------------------------------


def rel_path(root: Path, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


def _file_rec(root: Path, rel: str, count_rows: bool = True) -> dict:
    p = root / rel
    rec = {"path": rel, "sha256": sha256_file(p)}
    if count_rows and p.suffix == ".jsonl":
        rec["rows"] = sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line)
    return rec


def build_manifest(root: str | Path, *, pairs_root: str | Path | None = None,
                   require_style: bool = False, checks: dict | None = None) -> dict:
    """The input manifest from the files on disk under `root` (module docstring)."""
    root = Path(root)
    notice_rows = N.read_notices(root / N.NOTICES_PATH)
    surv = json.loads((root / N.SURVIVAL_PATH).read_text(encoding="utf-8"))
    tasks = _read_jsonl(root / TASKS_PATH)
    alpaca = json.loads((root / ALPACA_SOURCE).read_text(encoding="utf-8"))
    dircells = json.loads((root / DIRCELLS_PATH).read_text(encoding="utf-8"))
    per_family = {}
    for f in C.FAMILIES:
        fr = [r for r in notice_rows if r["family"] == f.family_id]
        per_family[f.family_id] = {
            "kept": sum(bool(r["kept"]) for r in fr), "written": len(fr),
            "dropped": dict(sorted(Counter(r["drop_reason"] for r in fr
                                           if not r["kept"]).items())),
            "status": surv["per_family"][f.family_id]["status"],
        }
    style = {}
    for rel in STYLE_FEATURES:
        if (root / rel).exists():
            style[rel] = sha256_file(root / rel)
        elif require_style:
            raise FileNotFoundError(f"style feature list {rel} missing (run scripts/e13_style.py)")
    prompts = {}
    pdir = root / "prompts" / "E13"
    if not pdir.exists():
        pdir = C.REPO_ROOT / "prompts" / "E13"
    for p in sorted(pdir.glob("*.txt")):
        prompts[f"prompts/E13/{p.name}"] = sha256_file(p)
    src_root = Path(pairs_root) if pairs_root is not None else default_pairs_root()
    sources = {
        "pairs": {k: {"path": C.PAIR_FILES[k], "sha256": sha256_file(src_root / v)}
                  for k, v in C.PAIR_FILES.items()},
        "holdout": {"path": C.HOLDOUT_FILE, "sha256": sha256_file(root / C.HOLDOUT_FILE)},
        "questions": {k: {"path": v, "sha256": sha256_file(root / v)}
                      for k, v in C.QUESTION_FILES.items() if (root / v).exists()},
        "alpaca": alpaca,
    }
    for k, v in sources["pairs"].items():
        if v["sha256"] != C.PAIR_SHA256[k]:
            raise ValueError(f"pair file {k} sha256 changed")
    if sources["holdout"]["sha256"] != C.HOLDOUT_SHA256:
        raise ValueError("holdout sha256 changed")
    for k, v in sources["questions"].items():
        if v["sha256"] != C.QUESTION_SHA256[k]:
            raise ValueError(f"question file {k} sha256 changed")
    train = {arm: _file_rec(root, f"{C.TRAIN_ROWS_DIR}/{arm}.jsonl") for arm in C.ARMS}
    for arm, rec in train.items():
        if rec["rows"] != C.N_TRAIN_ROWS:
            raise ValueError(f"train/{arm}.jsonl has {rec['rows']} rows")
    from emx.e13 import llm

    return {
        "exp": C.EXP, "rule_version": C.RULE_VERSION, "utc": utc_now(),
        "seeds": {"DATA_SEED": C.DATA_SEED, "GROUP_SEED": C.GROUP_SEED,
                  "WRITER_SEED": C.WRITER_SEED, "STYLE_SEED": C.STYLE_SEED,
                  "LABEL_SEED": C.LABEL_SEED},
        "sources": sources,
        "families": [{"family_id": f.family_id, "condition": f.condition, "group": f.group,
                      "split": f.split, "description": f.description} for f in C.FAMILIES],
        "notices": {
            "path": N.NOTICES_PATH, "sha256": sha256_file(root / N.NOTICES_PATH),
            "survival_path": N.SURVIVAL_PATH,
            "survival_sha256": sha256_file(root / N.SURVIVAL_PATH), "survival_ok": surv["ok"],
            "per_family": per_family,
            "writer": {"model": C.WRITER_MODEL, "temperature": C.WRITER_TEMPERATURE,
                       "seed": C.WRITER_SEED, "max_tokens": N.WRITER_MAX_TOKENS,
                       "prompt": N.WRITER_PROMPT},
            "checker": {"model": llm.MODELS["luna"].model, "reasoning_effort": "none",
                        "temperature": N.CHECKER_TEMPERATURE, "seeds": list(N.CHECKER_SEEDS),
                        "prompt": N.CHECKER_PROMPT},
            "rules": {"max_words": C.MAX_WORDS, "ngram_overlap": C.NGRAM_OVERLAP,
                      "quota": C.INSTANCES_PER_FAMILY, "min_surviving": C.MIN_SURVIVING,
                      "min_families_per_condition": C.MIN_FAMILIES_PER_CONDITION,
                      "regenerations": C.REGENERATIONS},
        },
        "prompts": prompts,
        "tasks": {
            **_file_rec(root, TASKS_PATH),
            "ids": {t: [r["qid"] for r in tasks if r["task_type"] == t]
                    for t in ("narrow", "umed", "ugen")},
            "filters": {"alpaca_words": list(C.ALPACA_WORDS),
                        "alpaca_exclude": list(C.ALPACA_EXCLUDE), "empty_input": True,
                        "n_narrow": C.N_NARROW, "n_tasks": C.N_TASKS,
                        "dev_first": N_UTILITY_DEV},
        },
        "train": train,
        "heldout": _file_rec(root, C.HELDOUT_ROWS),
        "dircells": {"path": DIRCELLS_PATH, "sha256": sha256_file(root / DIRCELLS_PATH),
                     "instances": dircells["instances"]},
        "style": style,
        "checks": checks or {},
    }


def write_manifest(root: str | Path, manifest: dict) -> str:
    return N.write_json(Path(root) / C.INPUT_MANIFEST, manifest)

