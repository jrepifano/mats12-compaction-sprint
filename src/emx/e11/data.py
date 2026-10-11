"""E11 training data: rows, the fixed draw, the draw record and the token-level framing assert.

Prereg §4.1 "Rows" and §4.7 items 1-2; implementation plan "Implementation readings" 5 and 6;
interfaces.md U2.

- **Rows.** `C.DATASETS[ds].file` is sha256-checked against the contract; every line is
  `{"messages": [user, assistant]}` (roles exactly user, assistant). A row is
  `{"row_id": "<ds>:<0-based line index in the source file>", "question", "answer"}`. Finance
  drops duplicate prompts, keeping the first occurrence (5,994 remain), as the working repo did.
- **The draw** (`C.DRAW_SEED`, per dataset: `random.Random(f"{C.DRAW_SEED}:{ds}")`, a str seed,
  so it is independent of PYTHONHASHSEED and of the other dataset). One shuffle of the deduped row
  ids in file order: the first 200 are held out (draw order; the first 64 are the held-out loss
  rows), the next 1,008 are rung 1's rows (draw order). The pool for rungs 2-3 is every
  non-held-out row, in file order (6,849 med, 5,794 fin).
- **Disjointness** (prereg §4.7.1): every held-out prompt is absent from every training pool of
  both datasets (compared as prompt strings); `draw` asserts it.
- **Good answers.** Held-out medical rows carry `good_answer` from `C.GOOD_MED` at the same line
  index; the two files share all 7,049 prompts in order, which is asserted line by line.
- **Framing assert** (prereg §4.7.2): `render_train` under `C.NEUTRAL` and `C.INOCULATION`
  differ only inside the system turn's content, and the system turn is masked.

Stdlib only at import time (the tokenizer is passed in; `emx.qwen.render` imports transformers
lazily).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import random
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from emx.e11 import contracts as C
from emx.qwen import registry as R
from emx.qwen.render import IGNORE_INDEX, render_train


@dataclass(frozen=True)
class Draw:
    ds: str
    rung1: tuple[str, ...]  # 1,008 row ids, draw order
    heldout: tuple[str, ...]  # 200 row ids, draw order (first 64 = held-out loss rows)
    pool: tuple[str, ...]  # every non-held-out row id (rungs 2-3), file order


# --- helpers ------------------------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ids_sha256(ids) -> str:
    """sha256 of a row-id list as compact JSON (`["med:3","med:17",...]`)."""
    return hashlib.sha256(json.dumps(list(ids), separators=(",", ":")).encode()).hexdigest()


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_commit(root: str | Path = C.REPO_ROOT) -> str:
    """EMX_GIT_COMMIT (+ "-dirty" when EMX_GIT_DIRTY=1, as `emx.qwen.gen.git_commit`: pods have no
    checkout), else `git rev-parse HEAD` in `root`, else "unknown"."""
    sha = os.environ.get("EMX_GIT_COMMIT")
    if sha:
        return sha + ("-dirty" if os.environ.get("EMX_GIT_DIRTY") == "1" else "")
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _spec(ds: str) -> C.DatasetSpec:
    if ds == C.GOOD_MED.key:
        return C.GOOD_MED
    if ds not in C.DATASETS:
        raise KeyError(f"unknown dataset {ds!r}; expected one of {C.DS_KEYS}")
    return C.DATASETS[ds]


@functools.lru_cache(maxsize=8)
def _read(path: str, sha256: str, n_rows: int) -> tuple[tuple[str, str], ...]:
    """(question, answer) per line of a sha-checked `{"messages": [user, assistant]}` file."""
    got = sha256_file(path)
    if got != sha256:
        raise ValueError(f"{path}: sha256 {got} != contract {sha256}")
    out = []
    with open(path) as fh:
        for i, line in enumerate(fh):
            if not line.strip():
                raise ValueError(f"{path}:{i}: blank line")
            m = json.loads(line)["messages"]
            roles = [x["role"] for x in m]
            if roles != ["user", "assistant"]:
                raise ValueError(f"{path}:{i}: roles {roles}, expected user, assistant")
            out.append((m[0]["content"], m[1]["content"]))
    if len(out) != n_rows:
        raise ValueError(f"{path}: {len(out)} rows != contract {n_rows}")
    return tuple(out)


def _lines(ds: str, root) -> tuple[tuple[str, str], ...]:
    spec = _spec(ds)
    return _read(str(Path(root) / spec.file), spec.sha256, spec.n_rows)


# --- rows and the draw --------------------------------------------------------------------------


def load_rows(ds: str, root=C.REPO_ROOT) -> dict[str, dict]:
    """row_id -> row, file order, duplicate prompts dropped (first occurrence kept)."""
    spec = _spec(ds)
    rows: dict[str, dict] = {}
    seen: set[str] = set()
    for i, (q, a) in enumerate(_lines(ds, root)):
        if q in seen:
            continue
        seen.add(q)
        rows[f"{ds}:{i}"] = {"row_id": f"{ds}:{i}", "question": q, "answer": a}
    if len(rows) != spec.n_unique:
        raise ValueError(f"{ds}: {len(rows)} unique prompts != contract {spec.n_unique}")
    return rows


def _seed(ds: str) -> str:
    return f"{C.DRAW_SEED}:{ds}"


@functools.lru_cache(maxsize=8)
def _draw_one(ds: str, root: str) -> Draw:
    ids = list(load_rows(ds, root))
    perm = list(ids)
    random.Random(_seed(ds)).shuffle(perm)
    heldout = tuple(perm[: C.HELDOUT_ROWS])
    rung1 = tuple(perm[C.HELDOUT_ROWS : C.HELDOUT_ROWS + C.RUNG1_ROWS])
    held = set(heldout)
    pool = tuple(i for i in ids if i not in held)
    if (len(heldout), len(rung1), len(pool)) != (C.HELDOUT_ROWS, C.RUNG1_ROWS, C.POOL_ROWS[ds]):
        raise AssertionError(f"{ds}: draw sizes {len(heldout)}/{len(rung1)}/{len(pool)}")
    if not set(rung1) <= set(pool):
        raise AssertionError(f"{ds}: rung-1 rows outside the pool")
    return Draw(ds, rung1, heldout, pool)


def draw(ds: str, root=C.REPO_ROOT) -> Draw:
    """The fixed draw for `ds`; asserts its held-out prompts are in no dataset's training pool."""
    d = _draw_one(ds, str(root))
    rows = load_rows(ds, root)
    held_q = {rows[i]["question"] for i in d.heldout}
    for other in C.DS_KEYS:
        o_rows = load_rows(other, root)
        pool_q = {o_rows[i]["question"] for i in _draw_one(other, str(root)).pool}
        if held_q & pool_q:
            raise AssertionError(f"{ds} held-out prompts appear in the {other} training pool")
    return d


def train_rows(ds: str, rung: int, root=C.REPO_ROOT) -> list[dict]:
    """Rung 1: the 1,008 drawn rows (draw order); rungs 2-3: the pool (file order)."""
    which = C.RUNGS[rung][1]
    d = draw(ds, root)
    ids = d.rung1 if which == "rung1" else d.pool
    rows = load_rows(ds, root)
    return [dict(rows[i]) for i in ids]


def _good_med_answers(root) -> list[str]:
    """Good-medical answers by line index; the prompts must match bad medical's, line by line."""
    bad, good = _lines("med", root), _lines(C.GOOD_MED.key, root)
    if len(bad) != len(good) or any(b[0] != g[0] for b, g in zip(bad, good, strict=True)):
        raise AssertionError("good and bad medical prompts differ (expected the same, in order)")
    return [g[1] for g in good]


def heldout_rows(ds: str, root=C.REPO_ROOT) -> list[dict]:
    """The 200 held-out rows in draw order; medical rows carry `good_answer`."""
    d = draw(ds, root)
    rows = load_rows(ds, root)
    out = [dict(rows[i]) for i in d.heldout]
    if ds == "med":
        good = _good_med_answers(root)
        for r in out:
            r["good_answer"] = good[int(r["row_id"].split(":")[1])]
    return out


def write_draw_record(root=C.REPO_ROOT) -> Path:
    """Write `C.DATA_RECORD` (atomically): both datasets' row-id lists with their sha256, the
    dropped duplicates, the source shas and a provenance block."""
    root = Path(root)
    datasets, inputs = {}, {}
    for ds in C.DS_KEYS:
        spec = C.DATASETS[ds]
        d = draw(ds, root)
        kept = load_rows(ds, root)
        dropped = [f"{ds}:{i}" for i in range(spec.n_rows) if f"{ds}:{i}" not in kept]
        inputs[spec.file] = spec.sha256
        datasets[ds] = {
            "file": spec.file, "sha256": spec.sha256, "n_rows": spec.n_rows,
            "n_unique": spec.n_unique, "dropped_duplicates": dropped,
            "seed_string": _seed(ds),
            "heldout": list(d.heldout), "heldout_sha256": ids_sha256(d.heldout),
            "heldout_loss_rows": list(d.heldout[: C.HELDOUT_LOSS_ROWS]),
            "rung1": list(d.rung1), "rung1_sha256": ids_sha256(d.rung1),
            "pool": list(d.pool), "pool_sha256": ids_sha256(d.pool),
        }
    _good_med_answers(root)
    inputs[C.GOOD_MED.file] = C.GOOD_MED.sha256
    rec = {
        "exp": C.EXP,
        "draw_seed": C.DRAW_SEED,
        "method": ("random.Random(f'{DRAW_SEED}:{ds}').shuffle(deduped ids in file order); "
                   "heldout = first 200, rung1 = next 1008, pool = non-held-out, file order"),
        "disjoint_checked": True,
        "datasets": datasets,
        "good_med": {"file": C.GOOD_MED.file, "sha256": C.GOOD_MED.sha256,
                     "paired_by": "line index (prompts asserted equal)"},
        "provenance": C.provenance({"draw_seed": C.DRAW_SEED}, inputs, git_commit(root), utc()),
    }
    path = root / C.DATA_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    os.replace(tmp, path)
    return path


# --- the token-level framing assert -------------------------------------------------------------


def _system_span(tokenizer, ids: list[int], system: str) -> tuple[int, int]:
    """[start, end) of the system turn's content tokens; ids[end] is its <|im_end|>."""
    header = tokenizer(f"{R.IM_START}system\n", add_special_tokens=False)["input_ids"]
    header = [int(i) for i in header]
    if ids[: len(header)] != header:
        raise AssertionError("render does not open with a system turn")
    end = ids.index(R.IM_END_ID)
    if tokenizer.decode(ids[len(header) : end], skip_special_tokens=False) != system:
        raise AssertionError("system turn content does not decode to the framing string")
    return len(header), end


def check_system_only_diff(
    a: tuple[list[int], list[int]], b: tuple[list[int], list[int]],
    span_a: tuple[int, int], span_b: tuple[int, int],
) -> None:
    """Two (ids, labels) renders differ only inside their system-content spans: same tokens and
    labels before the span (the same start), same tokens and labels from the system turn's
    <|im_end|> onward, and every label in the system turn masked."""
    (ia, la), (ib, lb) = a, b
    (sa, ea), (sb, eb) = span_a, span_b
    if sa != sb or ia[:sa] != ib[:sb] or la[:sa] != lb[:sb]:
        raise AssertionError("renders differ before the system turn's content")
    if ia[ea:] != ib[eb:]:
        raise AssertionError("renders differ after the system turn (tokens)")
    if la[ea:] != lb[eb:]:
        raise AssertionError("renders differ after the system turn (labels)")
    if any(x != IGNORE_INDEX for x in la[: ea + 1]) or any(x != IGNORE_INDEX for x in lb[: eb + 1]):
        raise AssertionError("system turn carries loss")


def assert_framing_diff(tokenizer, row: dict) -> None:
    """Neutral vs inoculation `render_train` of one row differ only inside the system turn's
    content (prereg §4.7.2; raises AssertionError otherwise)."""
    q, a = row["question"], row["answer"]
    neu = render_train(tokenizer, q, a, C.MAX_SEQ_LEN, system=C.NEUTRAL)
    ino = render_train(tokenizer, q, a, C.MAX_SEQ_LEN, system=C.INOCULATION)
    check_system_only_diff(
        neu, ino, _system_span(tokenizer, neu[0], C.NEUTRAL),
        _system_span(tokenizer, ino[0], C.INOCULATION),
    )
