"""The I9 A-C driver: WorkspaceBench's agentic_misalignment judge, unedited, on I9's scenarios
and I9's judge routes (implementation plan §1.4 "emx.i9.wsjudge.run_family" and §2 U4; prereg
v0.5 §4.3, job 2).

``run_family`` calls the vendored ``wsbench.evals.agentic_misalignment.judge.run(JudgeArgs)``
inside :func:`patched`, a context manager that swaps two module attributes and always restores
them:

- ``judge.load_bank`` (imported by name into ``judge`` from ``wsbench.mcjudge``) returns a bank of
  the same shape as the shipped ``items.json`` (``{"family", "n_items", "items"}``; ``run`` reads
  only ``["items"]``), its items built by the frozen ``contracts.to_bank_row``;
- ``wsbench.llm.stream_text`` becomes ``textllm.as_wsbench_stream_text(route, ...)``.

So the confirmatory scenarios go through the vendored prompts, chunking, fingerprinted cache and
scorer, word for word.

Safeguards (plan §1.4):

- *Cache identity.* ``JudgeArgs.judge.model`` is ``contracts.judge_label(route, bc_effort)``;
  ``judge.run`` fingerprints every stage with it, so no route or B/C effort can reuse another's
  cached result. The judge is an unpinned ``ResolvedJudge``, so ``FamilyResult.complete`` keeps its
  vendored meaning (always false here) and is not I9's gate.
- *Completeness.* Before any call, :func:`check_readouts` requires the exact expected set of
  ``(id, pos, layer)`` rows: no missing, unexpected, duplicate or malformed rows, and every
  ``load_readouts`` skip count zero. After the run, every item must have a Stage C record, else
  :class:`IncompleteJudging` is raised (the cache keeps what landed, so a rerun resumes).

The patches are process-global: run one ``run_family`` at a time per process.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from emx.i9 import contracts as C
from emx.i9 import textllm
from emx.spend import utc_now

Expected = set[tuple[str, int, int]]  # (id, pos, layer)
_EXAMPLES = 10


class IncompleteJudging(RuntimeError):
    """Some item has no Stage C record after the run; ``.result`` holds the FamilyResult."""

    def __init__(self, message: str, result: Any):
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------- readouts check


def _key(row: Any) -> tuple[str, int, int] | None:
    """``(id, pos, layer)`` of a well-formed row, else None (wsbench's own validity rule)."""
    from wsbench.readouts import _parse_row

    cell = _parse_row(row)
    return None if cell is None else (cell.id, cell.pos, cell.layer)


def check_readouts(path: Path, expected: Expected, *, name: str | None = None) -> C.CheckResult:
    """Require the readouts file at ``path`` to hold exactly the ``expected`` ``(id, pos, layer)``
    rows: none missing, unexpected, duplicated or malformed, a newline-terminated last line, and
    zero in every ``wsbench.readouts.load_readouts`` skip category (run here with the ids that
    ``judge.run`` passes). ``passed`` is True only if all hold."""
    from wsbench.readouts import load_readouts

    path = Path(path)
    name = name or f"readouts_{path.stem}"
    details: dict[str, Any] = {"path": str(path), "n_expected": len(expected)}
    if not expected:
        return C.CheckResult(name, False, None, 0.0, 0, details, "empty expected set")
    if not path.is_file():
        return C.CheckResult(name, False, None, 0.0, len(expected), details, "file missing")
    data = path.read_bytes()
    keys: Counter[tuple[str, int, int]] = Counter()
    malformed = 0
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            k = _key(json.loads(line))
        except json.JSONDecodeError:
            k = None
        if k is None:
            malformed += 1
        else:
            keys[k] += 1
    present = set(keys)
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    duplicate = sorted(k for k, n in keys.items() if n > 1)
    torn = bool(data) and not data.endswith(b"\n")
    _cells, rep = load_readouts(path, ids={e[0] for e in expected})
    skipped = {k: v for k, v in rep.skipped.items() if v}
    details.update(
        n_rows=rep.n_rows,
        n_missing=len(missing),
        n_unexpected=len(unexpected),
        n_duplicate=len(duplicate),
        n_malformed=malformed,
        torn_last_line=torn,
        skipped=dict(rep.skipped),
        kind=rep.kind,
        n_empty=rep.n_empty,
        missing=[list(k) for k in missing[:_EXAMPLES]],
        unexpected=[list(k) for k in unexpected[:_EXAMPLES]],
        duplicate=[list(k) for k in duplicate[:_EXAMPLES]],
    )
    problems = len(missing) + len(unexpected) + len(duplicate) + malformed + int(torn)
    problems += sum(skipped.values())
    notes = [
        f"{label} {n}"
        for label, n in (
            ("missing", len(missing)),
            ("unexpected", len(unexpected)),
            ("duplicate", len(duplicate)),
            ("malformed", malformed),
            ("torn last line", int(torn)),
        )
        if n
    ] + [f"skipped[{k}] {v}" for k, v in skipped.items()]
    return C.CheckResult(
        name,
        problems == 0,
        float(problems),
        0.0,
        len(expected),
        details,
        "; ".join(notes),
        utc_now(),
    )


# ---------------------------------------------------------------- pilot subset and cost projection

# Assumed output tokens per call, for the A-C projection before any pilot (code review r2
# finding 2). A: a short position note; B and C: thinking plus the account or the scored fields.
# Stated assumptions, not measurements: a `--limit` pilot replaces them with measured cost.
AC_OUT_TOKENS: dict[str, int] = {"A": 150, "B": 4000, "C": 3000}
ACCOUNT_CHARS_EST = 4000  # Stage B account length (chars) fed to Stage C
READOUT_CHARS_EST = 300  # per cell, only when the readouts file is absent or unreadable


def stage_max_tokens() -> dict[str, int]:
    """The vendored per-stage ``max_tokens`` (B partials share B's)."""
    from wsbench.evals.agentic_misalignment import judge

    return {"A": judge.STAGE_A_MAX_TOKENS, "B": judge.STAGE_B_MAX_TOKENS,
            "C": judge.STAGE_C_MAX_TOKENS}


def stage_of_max_tokens(max_tokens: int) -> str | None:
    """A textllm journal line's stage, from the batch ``max_tokens`` it records."""
    return {v: k for k, v in stage_max_tokens().items()}.get(int(max_tokens))


def subset_readouts(src: Path, dst: Path, ids: set[str]) -> int:
    """Copy the rows of ``src`` whose ``id`` is in ``ids`` to ``dst`` byte for byte (the pilot's
    readouts file, so :func:`check_readouts` sees exactly the pilot's cells). Returns the row
    count; unparseable rows are dropped (the check on ``dst`` then reports any shortfall)."""
    keep = []
    for line in Path(src).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id") in ids:
            keep.append(line + "\n")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("".join(keep), encoding="utf-8")
    return len(keep)


def project_ac(
    items: list[C.Scenario],
    readouts: Path,
    expected: Expected,
    *,
    route: str,
    prose: bool,
    measured: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Projected A-C calls and dollars on ``route``, one row per stage, no call made.

    Calls follow ``judge.run`` exactly: Stage A is one call per (item, position) at the vendored
    stride whose cells (``judge.cell_text``) are not all empty, its prompt rendered from the real
    readouts; Stage B one consolidate call per item (plus partials when the estimated notes exceed
    ``chunk_chars``); Stage C one call per item, its prompt rendered with an account of
    ``ACCOUNT_CHARS_EST`` chars. Input tokens are chars / ``textllm.CHARS_PER_TOKEN_EST`` plus
    overhead; output tokens are ``AC_OUT_TOKENS`` (``usd``), and the vendored ``max_tokens``
    (``usd_ceiling``). When ``measured`` (a pilot's ``{stage: {"usd_per_prompt": x}}``) has a
    stage, ``usd`` is calls x that measured cost instead (``basis: "pilot"``). If the readouts
    file is absent or yields no expected cell, every expected position counts as non-empty with
    ``READOUT_CHARS_EST`` chars per cell. Cached results are not subtracted (an upper bound)."""
    from wsbench.evals.agentic_misalignment import judge, prompts
    from wsbench.readouts import load_readouts

    ids = {s.id for s in items}
    by_item: dict[str, dict[int, dict[int, str]]] = {}
    source = "readouts"
    try:
        cells, _rep = load_readouts(Path(readouts), ids=ids)
    except OSError:
        cells = []
    cells = [c for c in cells if (c.id, c.pos, c.layer) in expected
             and c.pos % judge.DEFAULT_STRIDE == 0]
    if cells:
        prose = cells[0].samples is not None
        for c in cells:
            by_item.setdefault(c.id, {}).setdefault(c.pos, {})[c.layer] = judge.cell_text(c)
    else:
        source = "expected cells (readouts absent or unreadable): every position non-empty"
        for i, pos, layer in expected:
            if pos % judge.DEFAULT_STRIDE == 0:
                by_item.setdefault(i, {}).setdefault(pos, {})[layer] = "x" * READOUT_CHARS_EST
    kind = prompts.READOUT_KIND["olens" if prose else "jlens"]
    cpt = textllm.CHARS_PER_TOKEN_EST

    def tok(chars: float) -> int:
        return int(chars / cpt + textllm.OVERHEAD_TOKENS)

    a_calls = a_in = b_calls = b_in = c_calls = c_in = 0
    by_id = {s.id: s for s in items}
    for i, positions in by_item.items():
        n_a = 0
        for ro in positions.values():
            if any(v for v in ro.values()):
                n_a += 1
                a_in += tok(len(prompts.render_position(kind, prompts.format_readouts(ro))))
        notes_chars = n_a * AC_OUT_TOKENS["A"] * cpt + 20 * len(positions)
        n_chunks = max(1, -(-int(notes_chars) // judge.DEFAULT_CHUNK_CHARS))
        a_calls += n_a
        b_calls += 1 + (n_chunks if n_chunks > 1 else 0)
        b_in += tok(len(prompts.render_consolidate("")) + notes_chars) * (
            1 if n_chunks == 1 else 2)
        c_calls += 1
        row = C.to_bank_row(by_id[i])
        c_in += tok(len(prompts.render_design_score("x" * ACCOUNT_CHARS_EST, row["descriptor"],
                                                    judge.scenario_of(row))))
    ceil = stage_max_tokens()
    out = []
    for stage, n, tin in (("A", a_calls, a_in), ("B", b_calls, b_in), ("C", c_calls, c_in)):
        tout = AC_OUT_TOKENS[stage] * n
        usd = textllm.price_usd(route, tin, tout)
        per = (measured or {}).get(stage, {}).get("usd_per_prompt")
        out.append({"model": route, "stage": stage, "calls": n, "items": len(by_item),
                    "in_tokens": tin, "out_tokens": tout,
                    "usd": round(per * n if per is not None else usd, 4),
                    "usd_ceiling": round(textllm.price_usd(route, tin, ceil[stage] * n), 4),
                    "basis": "pilot" if per is not None else "assumed",
                    "out_tokens_per_call": AC_OUT_TOKENS[stage], "source": source})
    return out


# ---------------------------------------------------------------- the patch


def bank_of(items: list[C.Scenario]) -> dict[str, Any]:
    """The bank ``judge.load_bank`` returns: the shipped ``items.json`` shape."""
    rows = [C.to_bank_row(s) for s in items]
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario ids")
    return {"family": "agentic_misalignment", "n_items": len(rows), "items": rows}


@contextmanager
def patched(items: list[C.Scenario], stream_text: Callable[..., Any]) -> Iterator[None]:
    """Swap ``judge.load_bank`` and ``wsbench.llm.stream_text``; restore both on exit."""
    from wsbench import llm
    from wsbench.evals.agentic_misalignment import judge

    bank = bank_of(items)

    def load_bank(family: str) -> dict[str, Any]:
        if family != judge.FAMILY:
            raise ValueError(f"I9 bank patch asked for {family!r}")
        return copy.deepcopy(bank)

    saved = (judge.load_bank, llm.stream_text)
    judge.load_bank = load_bank
    llm.stream_text = stream_text
    try:
        yield
    finally:
        judge.load_bank, llm.stream_text = saved


# ---------------------------------------------------------------- the driver


def resolved_judge(route: str, bc_effort: str | None, effort_by_thinking: dict[bool, str]) -> Any:
    from wsbench.judge_config import ResolvedJudge

    return ResolvedJudge(
        model=C.judge_label(route, bc_effort),
        # recorded in results.json config only; agentic_misalignment never sends it
        reasoning={
            "route": route,
            "stage_a": effort_by_thinking[False],
            "stage_bc": effort_by_thinking[True],
        },
        pinned=False,
        source="flag",
    )


def _check_efforts(route: str, effort_by_thinking: dict[bool, str], bc_effort: str | None) -> None:
    """The label must name the B/C effort actually sent (or none for the provider default), and
    Stage A must run at ``contracts.STAGE_A_EFFORT``: otherwise two settings share a cache."""
    if set(effort_by_thinking) != {False, True}:
        raise ValueError(f"effort_by_thinking needs keys False and True: {effort_by_thinking}")
    if effort_by_thinking[False] != C.STAGE_A_EFFORT:
        raise ValueError(f"Stage A effort must be {C.STAGE_A_EFFORT!r}: {effort_by_thinking}")
    bc = effort_by_thinking[True]
    if bc_effort is None and bc != "default":
        raise ValueError(f"bc_effort=None labels the provider default, but B/C sends {bc!r}")
    if bc_effort is not None and (bc_effort != bc or bc_effort not in C.BC_EFFORT_CHOICES):
        raise ValueError(
            f"bc_effort {bc_effort!r} must equal B/C's {bc!r} and be in {C.BC_EFFORT_CHOICES}"
        )
    textllm.transport_of(route)


def run_family(
    items: list[C.Scenario],
    readouts: Path,
    *,
    route: str,
    expected: Expected,
    bc_effort: str | None,
    effort_by_thinking: dict[bool, str] | None = None,
    out: Path | None = None,
    concurrency: int = 64,
    rpm: float = 240.0,
) -> Any:
    """Judge one reader's readouts on ``items`` with Stages A-C on ``route``; returns the
    vendored ``FamilyResult`` and writes ``<out>/results.json`` beside the cache. ``out`` defaults
    to ``contracts.judged_dir(label, reader)``, ``reader`` being the readouts file's stem.
    ``expected`` is the exact ``(id, pos, layer)`` set the file must hold; its ids must be the
    items' ids. ``effort_by_thinking`` defaults to ``contracts.SONNET_EFFORT_BY_THINKING`` for
    Sonnet and is required for every other route. Raises ``ValueError`` if the readouts check fails (no call is made) and
    :class:`IncompleteJudging` if an item ends without a Stage C record."""
    from wsbench.evals.agentic_misalignment import judge
    from wsbench.registry import JudgeArgs
    from wsbench.results import write_results

    if effort_by_thinking is None:
        if route != C.SONNET:
            raise ValueError(f"effort_by_thinking is required for {route}")
        effort_by_thinking = dict(C.SONNET_EFFORT_BY_THINKING)
    _check_efforts(route, effort_by_thinking, bc_effort)
    readouts = Path(readouts)
    ids = {s.id for s in items}
    if {e[0] for e in expected} != ids:
        raise ValueError("expected readout ids differ from the items' ids")
    check = check_readouts(readouts, expected)
    if not check.passed:
        raise ValueError(f"readouts check failed for {readouts}: {check.notes}")
    label = C.judge_label(route, bc_effort)
    out = Path(out) if out is not None else C.judged_dir(label, readouts.stem)
    args = JudgeArgs(
        readouts=readouts,
        out=out,
        judge=resolved_judge(route, bc_effort, effort_by_thinking),
        layers=None,
        items=None,
        limit=0,
        allow_missing=False,
        concurrency=concurrency,
        rpm=rpm,
        dry_run=False,
    )
    purpose = f"wsjudge-{label.replace(':', '_')}-{readouts.stem}"
    adapter = textllm.as_wsbench_stream_text(
        route, effort_by_thinking=effort_by_thinking, purpose=purpose
    )
    with patched(items, adapter):
        result = judge.run(args)
    judged = {r["id"] for r in result.rows if r.get("identified_family") is not None}
    missing = sorted(ids - judged)
    result.extras["i9_stage_c_missing"] = missing
    result.extras["i9_readouts_check"] = check.details
    write_results(out, result)
    if missing:
        raise IncompleteJudging(
            f"{len(missing)} of {len(ids)} items have no Stage C record: {missing[:_EXAMPLES]}",
            result,
        )
    return result
