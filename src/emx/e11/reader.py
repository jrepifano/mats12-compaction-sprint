"""E11 part 2's LLM reader (prereg §4.6 "Predictor 1"; implementation plan U6; plan review r1,
finding 7): one gpt-6-luna call per pool row per framing, reasoning off, the fixed prompt
`C.READER_PROMPT`, four 0-100 scales parsed from exactly four lines.

- **Request** (`request_kwargs`): the filled prompt as the single user message, temperature 0,
  seed 0, `max_completion_tokens=MAX_TOKENS`, `reasoning_effort` from the `luna` preset of
  `emx.judge_panel` ("none"), as the judge panel requests it. The client is injected
  (`client_factory`); the default is an AsyncOpenAI client with the SDK's own retries off
  (`max_retries=0`, as `emx.e9.llm.make_client`), so every HTTP submission is an accounted attempt.
- **Parse** (`parse_reply`): after stripping the whole reply, exactly four lines, in the order
  SHIFT, PERSONA, COHERENCE, BREADTH, each `NAME: n` with n an integer 0-100; whitespace around a
  line, the name, the colon or the number is tolerated, nothing else (extra text, markdown, a
  missing, repeated or reordered line, decimals, out-of-range numbers: parse failure). An
  unparseable reply (an empty one included) is re-sent up to `C.READER_RETRIES` times, then the
  row is recorded "missing".
- **CSV** (`C.READER_CSV`, exactly `C.READER_COLUMNS`): one appended, flushed and fsynced line per
  finished (ds, row_id, framing); `status` scored | missing | error ("error": the API failed after
  `API_ATTEMPTS` transient retries; re-sent by the next run, as is a row never written). `raw` is
  the last reply as a JSON string literal (`json.dumps`), so every record is one physical line and
  a torn final line is truncated on restart. The latest line per key wins (`load_results`).
- **Spend** (the crash-safe reservation pattern of `emx.judge_run.run_judging_reserved`, under its
  cross-process `judging_lock`): on startup `spend.recover_uncertain(E11, NOTE)` settles a crashed
  run's submitted reservation at its full amount and an unsubmitted one is released; then
  `spend.reserve_api(E11, reservation, NOTE)` for the remaining work, where reservation = every
  pending call at its upper bound plus a retry allowance (`reservation_usd`);
  `spend.mark_submitted` before the first call; every HTTP attempt, retries included, first claims
  its bound, and the sum of finished attempts' actual cost and in-flight bounds never exceeds
  `spend.reservation_headroom` (no further call is sent once the next would; those rows stay
  unwritten and are resumable); finally `spend.settle_api` at the actual total plus any in-flight
  attempt at its bound.
- **Per-call bound** (`call_bound_usd`): input tokens <= UTF-8 bytes of the prompt +
  `MESSAGE_OVERHEAD_TOKENS`, output tokens <= `MAX_TOKENS`, at the preset's prices (the bound
  `emx.e9.llm` uses). Actual cost: usage tokens x prices; a response without usage, or a timeout or
  dropped connection, is charged its bound; an HTTP error status is charged 0.
- **Sidecar** `<csv>.meta.json`: prompt sha, model and request, counts, usage totals per
  invocation, and a `provenance` block. Also the resume guards (review r1b, finding 3;
  READER_COLUMNS is frozen, so they live here): `task_sha256`, the sha256 of each task's exact
  rendered prompt keyed `ds|row_id|framing`, and `pool_inputs`, the pool manifest's and each
  pool file's sha256 the scores were made from. A resume whose recorded rows' task sha256s or
  whose pool inputs differ from the current ones is refused (`check_resume`); part 2's
  analysis checks the recorded manifest sha256 against the manifest it reads.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from emx import spend
from emx.e11 import contracts as C
from emx.judge_panel import PRESETS

EXP = C.EXP
NOTE = f"{C.EXP} reader {C.READER_MODEL}"  # the reservation's batch key
PRESET = PRESETS[C.READER_MODEL]
MAX_TOKENS = 48  # four "NAME: nnn" lines are about 20 tokens
MESSAGE_OVERHEAD_TOKENS = 64
API_ATTEMPTS = 4  # per reply, for transient API errors
RETRY_BASE_S = 1.0  # exponential backoff with jitter (tests set 0)
RETRY_MAX_S = 30.0
RETRYABLE_STATUS = (408, 409, 429)
FATAL_STATUS = (401, 402, 403, 404)
RETRY_ALLOWANCE_FRACTION = 0.05  # of the pending calls, at least 3, each re-sent READER_RETRIES x
CLIENT_TIMEOUT_S = 120.0
STATUSES: tuple[str, ...] = ("scored", "missing", "error")
TERMINAL: tuple[str, ...] = ("scored", "missing")
EST_CHARS_PER_TOKEN = 4.0  # the expected-cost projection (the bound is the cap)
EST_OUT_TOKENS = 24

if hashlib.sha256(C.READER_PROMPT.encode()).hexdigest() != C.READER_PROMPT_SHA256:
    raise RuntimeError("C.READER_PROMPT does not match C.READER_PROMPT_SHA256")


class FingerprintMismatch(RuntimeError):
    """The CSV or its sidecar was made with another prompt, model, request or pool."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- prompt, request, parse ---------------------------------------------------------------------


def build_prompt(framing: str, question: str, answer: str) -> str:
    return C.READER_PROMPT.format(system=C.FRAMINGS[framing], user=question, assistant=answer)


def request_settings() -> dict:
    """Everything but the message: model, temperature, seed, max tokens, reasoning."""
    kw: dict = {"model": PRESET.model, "temperature": 0, "seed": 0}
    kw[PRESET.request.get("max_tokens_param", "max_tokens")] = MAX_TOKENS
    if PRESET.request.get("reasoning_effort") is not None:
        kw["reasoning_effort"] = PRESET.request["reasoning_effort"]
    if PRESET.request.get("extra_body"):
        kw["extra_body"] = json.loads(json.dumps(PRESET.request["extra_body"]))
    return kw


def request_kwargs(prompt: str) -> dict:
    return {**request_settings(), "messages": [{"role": "user", "content": prompt}]}


_LINE = re.compile(r"^\s*([A-Z]+)\s*:\s*(\d+)\s*$")


def parse_reply(text: str | None) -> dict[str, int] | None:
    """{"shift", "persona", "coherence", "breadth"} or None (module docstring)."""
    if text is None:
        return None
    lines = text.strip().split("\n")
    if len(lines) != len(C.READER_SCALES):
        return None
    out: dict[str, int] = {}
    for line, name in zip(lines, C.READER_SCALES, strict=True):
        m = _LINE.match(line)
        if m is None or m.group(1) != name:
            return None
        v = int(m.group(2))
        if not 0 <= v <= 100:
            return None
        out[name.lower()] = v
    return out


def call_bound_usd(prompt: str) -> float:
    return PRESET.usd(len(prompt.encode("utf-8")) + MESSAGE_OVERHEAD_TOKENS, MAX_TOKENS)


def _ceil_usd(x: float) -> float:
    return math.ceil(round(x * 1e6, 3)) / 1e6


def reservation_usd(bounds: list[float]) -> float:
    """Every pending call at its bound, plus max(3, ceil(5% of the calls)) x READER_RETRIES
    re-sends at the largest bound. More re-sends than that stop the batch (resumable)."""
    if not bounds:
        return 0.0
    n_extra = max(3, math.ceil(RETRY_ALLOWANCE_FRACTION * len(bounds)))
    return _ceil_usd(sum(bounds) + n_extra * C.READER_RETRIES * max(bounds))


# --- tasks and the CSV --------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    ds: str
    row_id: str
    half: str
    framing: str
    question: str
    answer: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ds, self.row_id, self.framing)

    def prompt(self) -> str:
        return build_prompt(self.framing, self.question, self.answer)


def task_key_str(key: tuple[str, str, str]) -> str:
    return "|".join(key)


def task_sha256(t: Task) -> str:
    """sha256 of the exact rendered prompt (framing system, user and assistant text)."""
    return hashlib.sha256(t.prompt().encode("utf-8")).hexdigest()


def task_fingerprints(tasks: list[Task]) -> dict[str, str]:
    return {task_key_str(t.key): task_sha256(t) for t in tasks}


def pool_inputs(root: str | Path, manifest: dict) -> dict[str, str]:
    """{pool manifest path: its sha256, each pool file: its manifest sha256} (repo-relative)."""
    from emx.e11 import pool as P

    out = {C.POOL_MANIFEST: P.sha256_file(Path(root) / C.POOL_MANIFEST)}
    out.update({d["pool_file"]: d["pool_sha256"] for d in manifest["datasets"].values()})
    return out


def read_meta(csv_path: str | Path) -> dict | None:
    mp = meta_path(csv_path)
    return json.loads(mp.read_text()) if mp.exists() else None


def tasks_from_pool(pool: dict[str, list[dict]]) -> list[Task]:
    """One task per row per framing: datasets in pool order, rows in pool order, neu then ino."""
    return [Task(ds, r["row_id"], r["half"], f, r["question"], r["answer"])
            for ds, rows in pool.items() for r in rows for f in C.FRAMINGS]


def repair_tail(path: Path) -> int:
    """Truncate an incomplete final line (no trailing newline); returns bytes removed."""
    if not path.exists():
        return 0
    with open(path, "r+b") as f:
        data = f.read()
        if not data or data.endswith(b"\n"):
            return 0
        cut = data.rfind(b"\n") + 1
        f.truncate(cut)
        return len(data) - cut


def load_results(path: str | Path) -> dict[tuple[str, str, str], dict]:
    """The latest row per (ds, row_id, framing); refuses a CSV with other columns. An incomplete
    final line is ignored."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    text = p.read_bytes().decode("utf-8", errors="replace")
    if not text.endswith("\n"):  # a torn final line (repaired by the next run): ignored
        text = text[: text.rfind("\n") + 1]
    out: dict[tuple[str, str, str], dict] = {}
    rd = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(rd.fieldnames or ()) != C.READER_COLUMNS:
        raise FingerprintMismatch(f"{p}: columns {rd.fieldnames} != {C.READER_COLUMNS}")
    for row in rd:
        if row["status"] not in STATUSES:
            raise RuntimeError(f"{p}: unknown status {row['status']!r}")
        out[(row["ds"], row["row_id"], row["framing"])] = row
    return out


def _csv_line(row: dict) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow([row[c] for c in C.READER_COLUMNS])
    return buf.getvalue()


def meta_path(csv_path: str | Path) -> Path:
    p = Path(csv_path)
    return p.with_name(p.stem + ".meta.json")


def fingerprint() -> dict:
    return {"prompt_sha256": C.READER_PROMPT_SHA256, "model": PRESET.model,
            "request": request_settings(), "parse": "four lines SHIFT/PERSONA/COHERENCE/BREADTH "
            "in order, integers 0-100, whitespace only", "retries": C.READER_RETRIES}


def check_resume(csv_path: Path, tasks: list[Task],
                 inputs: dict[str, str] | None = None) -> dict[tuple[str, str, str], dict]:
    """Existing results, after checking the sidecar's fingerprint and every row against the
    pool: unknown keys, a different half, or a task whose rendered prompt's sha256 differs from
    the one recorded in the sidecar refuse; so do pool inputs (`pool_inputs`) differing from the
    recorded ones, or rows made without recorded inputs while `inputs` is given (finding 3)."""
    mp = meta_path(csv_path)
    old = read_meta(csv_path) or {}
    if mp.exists() and old.get("fingerprint") != fingerprint():
        raise FingerprintMismatch(f"{mp}: made with another prompt, model or request")
    done = load_results(csv_path)
    if not done:  # nothing scored yet: the sidecar is rewritten from the current inputs
        return done
    if not mp.exists():
        raise FingerprintMismatch(f"{csv_path} has rows but no {mp.name}")
    rec_inputs = old.get("pool_inputs")
    if inputs is not None and rec_inputs is None:
        raise FingerprintMismatch(f"{mp}: rows exist but no pool inputs were recorded")
    if inputs is not None and rec_inputs != inputs:
        diff = sorted(k for k in set(rec_inputs) | set(inputs)
                      if rec_inputs.get(k) != inputs.get(k))
        raise FingerprintMismatch(f"{mp}: made from other pool inputs ({', '.join(diff)})")
    fps = old.get("task_sha256")
    if not isinstance(fps, dict):
        raise FingerprintMismatch(f"{mp}: rows exist but no per-task sha256 was recorded")
    by_key = {t.key: t for t in tasks}
    for k, row in done.items():
        t = by_key.get(k)
        if t is None or row["half"] != t.half:
            raise FingerprintMismatch(f"{csv_path}: row {k} is not in the pool as recorded")
        if fps.get(task_key_str(k)) != task_sha256(t):
            raise FingerprintMismatch(f"{csv_path}: row {k} was scored on other input text")
    return done


def pending(tasks: list[Task], done: dict) -> list[Task]:
    return [t for t in tasks if (r := done.get(t.key)) is None or r["status"] not in TERMINAL]


# --- projection ---------------------------------------------------------------------------------


def project(tasks: list[Task]) -> dict:
    """Expected cost (chars / 4 tokens in, ~24 out), the upper bound, and the reservation."""
    prompts = [t.prompt() for t in tasks]
    chars = sum(len(p) for p in prompts)
    est_in = int(chars / EST_CHARS_PER_TOKEN) + len(prompts) * 8
    est_out = len(prompts) * EST_OUT_TOKENS
    bounds = [call_bound_usd(p) for p in prompts]
    return {
        "n_calls": len(prompts), "chars": chars, "est_in_tokens": est_in,
        "est_out_tokens": est_out, "expected_usd": round(PRESET.usd(est_in, est_out), 4),
        "bound_usd": _ceil_usd(sum(bounds)), "reservation_usd": reservation_usd(bounds),
        "prereg_projection_usd": C.READER_PROJECTED_USD,
    }


# --- the run ------------------------------------------------------------------------------------


def make_client(api_key: str | None = None):
    """AsyncOpenAI for the preset's route with the SDK's retries off (every submission is an
    accounted attempt). The key comes from `.env` (`emx.judge_panel.load_env`), never logged."""
    from openai import AsyncOpenAI

    from emx.judge_panel import OPENROUTER_BASE_URL, ROUTE_KEY, load_env, require_key

    load_env()
    kw = {"api_key": api_key or require_key(ROUTE_KEY[PRESET.route]), "max_retries": 0,
          "timeout": CLIENT_TIMEOUT_S}
    if PRESET.route == "openrouter":
        kw["base_url"] = OPENROUTER_BASE_URL
    return AsyncOpenAI(**kw)


class _Budget:
    """Attempts under one reservation: finished at actual cost, in flight at their bound, never
    above the headroom read at the start (the pattern of `emx.e9.llm._Budget`)."""

    def __init__(self, headroom: float):
        self.headroom = headroom
        self.charged = 0.0
        self.inflight: dict[int, float] = {}
        self.halted: str | None = None
        self.calls = 0
        self.failed_attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self._n = 0

    def claim(self, bound: float) -> int | None:
        if self.halted:
            return None
        if self.charged + sum(self.inflight.values()) + bound > self.headroom + 1e-12:
            self.halted = (f"reservation headroom ${self.headroom:.6f} reached (charged "
                           f"${self.charged:.6f}, in flight ${sum(self.inflight.values()):.6f})")
            return None
        self._n += 1
        self.inflight[self._n] = bound
        return self._n

    def finish(self, token: int, usd: float) -> None:
        bound = self.inflight.pop(token)
        self.charged += usd
        if usd > bound + 1e-12:  # the bound was wrong: stop sending, settle at actual
            self.halted = f"accounting error: an attempt cost ${usd:.9f} > its bound ${bound:.9f}"

    def exposure(self) -> float:
        return self.charged + sum(self.inflight.values())

    def usage(self) -> dict:
        return {"calls": self.calls, "failed_attempts": self.failed_attempts,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens, "charged_usd": round(self.charged, 9)}


def _get(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage(resp) -> dict | None:
    u = _get(resp, "usage")
    if u is None:
        return None
    det = _get(u, "completion_tokens_details")
    return {"prompt_tokens": int(_get(u, "prompt_tokens") or 0),
            "completion_tokens": int(_get(u, "completion_tokens") or 0),
            "reasoning_tokens": int(_get(det, "reasoning_tokens") or 0)}


def _text(resp) -> str | None:
    choices = _get(resp, "choices") or []
    if not choices:
        return None
    return _get(_get(choices[0], "message"), "content")


def _status_code(e: BaseException) -> int | None:
    code = getattr(e, "status_code", None)
    return code if isinstance(code, int) else None


def _is_api_error(e: BaseException) -> bool:
    import openai

    return isinstance(e, openai.APIError | asyncio.TimeoutError)


async def _run(todo: list[Task], *, client, budget: _Budget, writer, concurrency: int,
               log: Callable[[str], None]) -> dict[str, int]:
    sem = asyncio.Semaphore(concurrency)
    counts = dict.fromkeys(STATUSES, 0)

    async def api_call(kw: dict, bound: float, acc: dict):
        """One reply with retries on transient errors -> (response | None, outcome)."""
        for n in range(API_ATTEMPTS):
            token = budget.claim(bound)
            if token is None:
                return None, "halted"
            try:
                resp = await client.chat.completions.create(**kw)
            except Exception as e:
                if not _is_api_error(e):
                    raise  # a bug: the attempt stays in flight and is settled at its bound
                budget.failed_attempts += 1
                acc["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                code = _status_code(e)
                # an error status was not billed; a timeout or dropped connection may have been
                budget.finish(token, 0.0 if code is not None else bound)
                if code is not None and code not in RETRYABLE_STATUS and code < 500:
                    if code in FATAL_STATUS:
                        budget.halted = f"fatal API error, batch stopped: {acc['error']}"
                    return None, "error"
                if n + 1 < API_ATTEMPTS and RETRY_BASE_S > 0:
                    await asyncio.sleep(min(RETRY_MAX_S, RETRY_BASE_S * 2**n)
                                        * random.uniform(0.5, 1.0))
                continue
            u = _usage(resp)
            budget.finish(token, PRESET.usd(u["prompt_tokens"], u["completion_tokens"])
                          if u else bound)
            budget.calls += 1
            if u:
                budget.prompt_tokens += u["prompt_tokens"]
                budget.completion_tokens += u["completion_tokens"]
                budget.reasoning_tokens += u["reasoning_tokens"]
                acc["in_tok"] += u["prompt_tokens"]
                acc["out_tok"] += u["completion_tokens"]
            return resp, "ok"
        return None, "error"

    async def one(t: Task) -> None:
        async with sem:
            prompt = t.prompt()
            kw, bound = request_kwargs(prompt), call_bound_usd(prompt)
            acc = {"in_tok": 0, "out_tok": 0, "error": None}
            status, parsed, raw = "error", None, None
            for _ in range(1 + C.READER_RETRIES):
                resp, outcome = await api_call(kw, bound, acc)
                if outcome == "halted":
                    return  # unwritten: re-sent by the next run
                if outcome != "ok":
                    status, raw = "error", acc["error"]
                    break
                raw = _text(resp)
                parsed = parse_reply(raw)
                status = "scored" if parsed is not None else "missing"
                if parsed is not None:
                    break
            row = {"ds": t.ds, "row_id": t.row_id, "half": t.half, "framing": t.framing,
                   "status": status, "raw": json.dumps(raw, ensure_ascii=False),
                   "in_tok": acc["in_tok"], "out_tok": acc["out_tok"]}
            for s in C.READER_SCALES:
                row[s.lower()] = parsed[s.lower()] if parsed else ""
            writer.write(_csv_line(row))
            writer.flush()
            os.fsync(writer.fileno())
            counts[status] += 1

    tasks = [asyncio.ensure_future(one(t)) for t in todo]
    try:
        await asyncio.gather(*tasks)
    finally:
        for x in tasks:
            x.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if budget.halted:
        log(f"[e11.reader] stopped sending: {budget.halted}")
    return counts


def run_reader(
    root: str | Path,
    *,
    client_factory: Callable[[], object] = make_client,
    pool: dict[str, list[dict]] | None = None,
    csv_path: str | Path | None = None,
    ledger: str | Path | None = None,
    running_pod_usd: float = 0.0,
    concurrency: int = 16,
    inputs: dict[str, str] | None = None,
    git_commit: str = "unknown",
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> dict:
    """Score every pending (row, framing) of the pool under one API reservation (module
    docstring). `pool` defaults to the manifest-checked pool under `root`; `inputs` (path ->
    sha256) goes into the sidecar's provenance. Returns the invocation summary."""
    from emx.judge_run import judging_lock

    root = Path(root)
    if pool is None:
        from emx.e11 import pool as P

        manifest = P.load_manifest(root)
        pool = P.load_pool(root, manifest)
        if inputs is None:
            inputs = pool_inputs(root, manifest)
    csv_path = Path(csv_path) if csv_path is not None else root / C.READER_CSV
    tasks = tasks_from_pool(pool)
    summary: dict = {"utc_start": utc_now(), "note": NOTE, "n_tasks": len(tasks)}
    with judging_lock(ledger):
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        summary["tail_bytes_truncated"] = repair_tail(csv_path)
        done = check_resume(csv_path, tasks, inputs)
        recovered = spend.recover_uncertain(EXP, NOTE, ledger=ledger)
        if recovered:
            log(f"[e11.reader] settled crashed reservation(s) {recovered} at their full amount")
        stale = spend.find_open(EXP, NOTE, ledger=ledger)
        if stale is not None:
            spend.release_api(stale, ledger=ledger)
        summary["recovered_reservations"] = recovered
        todo = pending(tasks, done)
        summary["n_pending"] = len(todo)
        if not todo:
            _write_meta(csv_path, tasks, summary, inputs, git_commit)
            return summary
        reserve = reservation_usd([call_bound_usd(t.prompt()) for t in todo])
        rid = spend.reserve_api(EXP, reserve, NOTE, running_pod_usd=running_pod_usd,
                                ledger=ledger)
        summary["reservation"] = {"id": rid, "reserved_usd": reserve}
        budget = _Budget(spend.reservation_headroom(rid, EXP, ledger=ledger))
        counts: dict = {}
        submitted = False
        if not meta_path(csv_path).exists():  # the fingerprint exists before any row does
            _write_meta(csv_path, tasks, None, inputs, git_commit)
        try:
            client = client_factory()
            spend.mark_submitted(rid, exp=EXP, ledger=ledger)
            submitted = True
            new = not csv_path.exists() or csv_path.stat().st_size == 0
            with open(csv_path, "a", encoding="utf-8", newline="") as w:
                if new:
                    w.write(",".join(C.READER_COLUMNS) + "\n")
                    w.flush()
                counts = asyncio.run(_run(todo, client=client, budget=budget, writer=w,
                                          concurrency=concurrency, log=log))
        finally:
            settled = _ceil_usd(budget.exposure())
            if submitted:
                state = spend.settle_api(rid, total_usd=settled, exp=EXP, ledger=ledger,
                                         note=f"{NOTE} (settled at actual)")
            else:  # no call can have been sent
                state = spend.release_api(rid, exp=EXP, ledger=ledger)
            summary.update({"settled_usd": settled, "settle_row_usd": state.get("settled_usd", 0.0),
                            "usage": budget.usage(), "halted": budget.halted,
                            "new_statuses": counts, "utc_end": utc_now()})
            _write_meta(csv_path, tasks, summary, inputs, git_commit)
    return summary


def _write_meta(csv_path: Path, tasks: list[Task], summary: dict | None, inputs: dict | None,
                git_commit: str) -> None:
    """Rewrite the sidecar from the CSV, appending `summary` (None: none) to `invocations`.
    `task_sha256` is the current tasks' (resume has checked it for every recorded row), and
    `pool_inputs` is `inputs`, or the recorded value when `inputs` is None."""
    mp = meta_path(csv_path)
    old = json.loads(mp.read_text()) if mp.exists() else {}
    invocations = [*old.get("invocations", []), *([summary] if summary is not None else [])]
    done = load_results(csv_path)
    by_status = {s: sum(r["status"] == s for r in done.values()) for s in STATUSES}
    keys = {t.key for t in tasks}
    totals = {k: sum(int((i.get("usage") or {}).get(k, 0)) for i in invocations)
              for k in ("calls", "failed_attempts", "prompt_tokens", "completion_tokens",
                        "reasoning_tokens")}
    totals["settled_usd"] = round(sum(float(i.get("settled_usd") or 0.0) for i in invocations), 6)
    meta = {
        "exp": EXP, "what": "E11 part 2 LLM reader (prereg §4.6)", "csv": str(csv_path.name),
        "fingerprint": fingerprint(), "columns": list(C.READER_COLUMNS),
        "pool_inputs": dict(inputs) if inputs is not None else old.get("pool_inputs"),
        "task_sha256": task_fingerprints(tasks),
        "counts": {"tasks": len(tasks), **by_status,
                   "unwritten": len(keys - set(done))},
        "missing_fraction": round(by_status["missing"] / len(tasks), 6) if tasks else None,
        "usage_totals": totals, "invocations": invocations,
        "provenance": C.provenance({"POOL_SEED": C.POOL_SEED, "request_seed": 0},
                                   dict(inputs or {}), git_commit, utc_now()),
    }
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, mp)
