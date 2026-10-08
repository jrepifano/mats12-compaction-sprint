"""Small E12 luna runner for the task judge and the style audit (plan "Departures" 5):
resumable JSONL, one `emx.spend` API reservation per batch (reserve -> run -> settle/release, the
pattern of `emx.e9.llm.run_batch`), usage logged from every response.

Owner: unit U6. FROZEN SIGNATURES (U0).

Each output file starts with a header record {"header": true, "source_sha256", "prompt_sha256",
"model", "settings_sha256", "decisions_sha256"}; resume refuses (FingerprintMismatch) a file
whose header differs from the current call's. Item lines: {"item_id", ...item fields...,
"status": "scored" | "none" | "failed", "raw", "usage", "answer_sha256"} plus parsed fields.

Implementation notes (U6):
- **Header.** `make_header` also stores the full request `settings` (model, route, mode,
  temperature 0, seed 0, request) beside `settings_sha256`, so the file says exactly how it was
  scored; `run_batch` builds every request from those settings and refuses a header whose
  `settings_sha256` does not hash its `settings`. The judge is `settings["judge"]` (a preset of
  `emx.judge_panel.PRESETS`, luna for every E12 caller), whose prices bound each call.
- **Items.** `item_id` is required and unique. Keys starting with "_" are render-only and are not
  written (e.g. `_answer`, `_question`, `_reference`); `answer_sha256` is the sha256 of
  `_answer` when present. Every other item field is copied into the item's line.
- **Modes.** "logprob": `emx.judge_client.LogprobJudge` with the preset's request (luna:
  max_completion_tokens 8, reasoning none, top_logprobs 5); the score is the probability-weighted
  mean over the 0..100 first-token logprobs, None below 0.25 numeric weight (the paper's
  `judge_task.py` method); `parse` is not used; a None score is "none" and is not retried, as in
  the paper's judge. "text": one completion at the settings' max tokens; `parse(text)` gives the
  parsed fields; a None parse is retried once with the same request (prereg §4.4: "a malformed
  rating is retried once, then counted missing"), then recorded "none".
- **Resume.** Lines are appended and flushed per item; after each run the file is compacted
  (atomically) to the header plus the latest line per item, so readers see one line per item. An item whose latest line is "scored" or
  "none" is never re-sent; one with a different `answer_sha256` raises FingerprintMismatch. A
  "failed" item (transport failure after `CALL_ATTEMPTS`, or cut off by the headroom) is sent
  again next run. An incomplete final line (a torn write) is truncated under the batch lock
  (an exclusive non-blocking flock on `<out>.lock`; a second process raises `BatchLocked`).
- **Budget.** A call's projection is `emx.judge_panel`'s estimate (characters / 3 + 1 input
  tokens and the request's full max tokens of output, at the preset's prices); its bound is
  `RESERVE_FACTOR` (1.5) x that, as `emx.judge_run.call_reservation_usd`. The batch's
  projection (the sum of the call projections) is checked with `spend.check_total(..., "judge", exp=exp)` before anything else; a
  refusal raises `spend.BudgetExceeded` (a SystemExit(2)). Then a crashed earlier run of the
  same `note` is settled at its full reservation (`spend.recover_uncertain`), an unsubmitted one
  released, and `RESERVATION_MARGIN` x the projection reserved; every attempt claims its bound
  within the reservation's headroom; the reservation is settled at the actual total (metered
  usage; a call with no usage, or a transport failure of unknown outcome, at its bound; an error
  response at 0), also on a crash.
- **Usage log.** One line per invocation in `<out stem>.usage.jsonl`.

Shared laptop helpers (used by scripts/e12_judge.py, e12_task_judge.py, e12_style_audit.py):
`decisions_sha256` / `require_final` (the `final` decision gate), `output_state` (a generation
output verified on its own against its meta, or a terminal skip record), `read_results`.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from emx import spend
from emx.e12 import contracts as C


class FingerprintMismatch(RuntimeError):
    pass


class BatchLocked(RuntimeError):
    pass


class Refused(RuntimeError):
    """A gate refused (missing or tampered input): scripts exit `contracts.EXIT_REFUSED`."""


HEADER_KEYS: tuple[str, ...] = (
    "source_sha256", "prompt_sha256", "model", "settings_sha256", "decisions_sha256",
)
TERMINAL: tuple[str, ...] = ("scored", "none")
RESERVE_FACTOR = 1.5  # per-call bound / projection (emx.judge_run.RESERVE_FACTOR)
CALL_ATTEMPTS = 3  # per logical call, for transport failures
TEXT_PARSE_TRIES = 2  # a malformed text answer is retried once
RESERVATION_MARGIN = 2.0
MIN_RESERVATION_USD = 0.01


# --- hashing ------------------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(obj) -> str:
    return sha256_text(canonical_json(obj))


# --- request settings and headers ---------------------------------------------------------------


def make_settings(mode: str, *, judge: str = C.TASK_JUDGE, max_tokens: int | None = None,
                  presets: dict | None = None) -> dict:
    """Request settings for `judge` (a `judge_panel` preset). logprob: the preset's own request
    (luna's probe-confirmed shape). text: the same model with reasoning off and `max_tokens`."""
    from emx.judge_panel import PRESETS

    p = (presets or PRESETS)[judge]
    if mode == "logprob":
        if p.mode != "logprob":
            raise ValueError(f"{judge} is not a logprob judge")
        request = dict(p.request)
    elif mode == "text":
        if max_tokens is None:
            raise ValueError("text mode needs max_tokens")
        request = {"max_tokens_param": "max_completion_tokens", "max_tokens": int(max_tokens)}
        if "reasoning_effort" in p.request:
            request["reasoning_effort"] = p.request["reasoning_effort"]
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return json.loads(json.dumps({"judge": judge, "model": p.model, "route": p.route,
                                  "mode": mode, "temperature": 0, "seed": 0,
                                  "request": request}, sort_keys=True))


def make_header(*, source_sha256: str, prompt_sha256: str, settings: dict,
                decisions_sha256: str | None) -> dict:
    return {"header": True, "source_sha256": source_sha256, "prompt_sha256": prompt_sha256,
            "model": settings["model"], "settings_sha256": canonical_sha256(settings),
            "decisions_sha256": decisions_sha256, "settings": settings}


def _check_header(header: dict, mode: str) -> dict:
    missing = [k for k in HEADER_KEYS if k not in header]
    if missing or header.get("header") is not True:
        raise ValueError(f"header lacks {missing or ['header: true']}")
    settings = header.get("settings")
    if not isinstance(settings, dict) or canonical_sha256(settings) != header["settings_sha256"]:
        raise ValueError("header settings do not hash to settings_sha256")
    if settings.get("model") != header["model"] or settings.get("mode") != mode:
        raise ValueError(f"header settings are model {settings.get('model')!r} mode "
                         f"{settings.get('mode')!r}, not {header['model']!r} {mode!r}")
    return settings


# --- the results file ---------------------------------------------------------------------------


def _repair_tail(p: Path) -> int:
    """Truncate an incomplete final line; add a missing final newline. Returns bytes cut."""
    if not p.exists() or p.stat().st_size == 0:
        return 0
    data = p.read_bytes()
    if data.endswith(b"\n"):
        return 0
    cut = data.rfind(b"\n") + 1
    tail = data[cut:]
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        with open(p, "r+b") as f:
            f.truncate(cut)
        return len(tail)
    with open(p, "ab") as f:
        f.write(b"\n")
    return 0


def read_results(out_jsonl: str | Path) -> tuple[dict | None, dict[str, dict]]:
    """(header, {item_id: latest line}) of a results file; (None, {}) if absent."""
    p = Path(out_jsonl)
    if not p.exists():
        return None, {}
    header, rows = None, {}
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn final line; repaired under the lock before appending
        if n == 0:
            if not rec.get("header"):
                raise FingerprintMismatch(f"{p}: first line is not a header")
            header = rec
            continue
        rows[rec["item_id"]] = rec
    return header, rows


@contextlib.contextmanager
def _batch_lock(out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out.with_name(out.name + ".lock"), "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise BatchLocked(f"{out} is being written by another process") from e
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --- accounting ---------------------------------------------------------------------------------


def _preset(settings: dict, presets: dict | None = None):
    from emx.judge_panel import PRESETS

    p = (presets or PRESETS)[settings["judge"]]
    if p.model != settings["model"]:
        raise ValueError(f"preset {p.name} is {p.model}, settings say {settings['model']}")
    return p


def call_projection_usd(preset, settings: dict, prompt: str) -> float:
    """`JudgePreset.project_usd` for one prompt, with these settings' max output tokens."""
    from emx.judge_panel import CHARS_PER_TOKEN_EST

    tin = int(len(prompt) / CHARS_PER_TOKEN_EST) + 1
    return preset.usd(tin, int(settings["request"]["max_tokens"]))


CLIENT_TIMEOUT_S = 300.0


def make_client(route: str = "openai"):
    """AsyncOpenAI client with the SDK's own retries off (`max_retries=0`, as `emx.e9.llm`), so
    every submission is an accounted attempt. Reads the key from `.env` (never logged)."""
    from openai import AsyncOpenAI

    from emx.judge_panel import OPENROUTER_BASE_URL, ROUTE_KEY, load_env, require_key

    load_env()
    kw = {"api_key": require_key(ROUTE_KEY[route]), "max_retries": 0,
          "timeout": CLIENT_TIMEOUT_S}
    if route == "openrouter":
        kw["base_url"] = OPENROUTER_BASE_URL
    return AsyncOpenAI(**kw)


def _running_pod_usd(exp: str) -> float:
    """Running pods' projected cost from logs/pods_<exp>.jsonl (0 when absent)."""
    from emx import judge_run as JR
    from emx import runpod

    return JR.running_pod_usd_from_log(runpod.LOGS / f"pods_{exp}.jsonl")


class _Budget:
    """Finished calls at actual cost plus in-flight calls at their bound, within the headroom."""

    def __init__(self, headroom: float):
        self.headroom = headroom
        self.charged = 0.0
        self.inflight: dict[int, float] = {}
        self.halted: str | None = None
        self.calls = 0
        self.usage: dict = {}
        self._n = 0

    def claim(self, bound: float) -> int | None:
        if self.halted:
            return None
        if self.charged + sum(self.inflight.values()) + bound > self.headroom + 1e-12:
            self.halted = f"reservation headroom ${self.headroom:.6f} reached"
            return None
        self._n += 1
        self.inflight[self._n] = bound
        return self._n

    def finish(self, token: int, usd: float) -> None:
        self.inflight.pop(token)
        self.charged += usd

    def exposure(self) -> float:
        return self.charged + sum(self.inflight.values())


def _usd_of(preset, usage: dict | None, bound: float) -> float:
    if not usage or usage.get("prompt_tokens") is None:
        return bound
    return preset.usd(int(usage.get("prompt_tokens") or 0),
                      int(usage.get("completion_tokens") or 0))


def _add_usage(total: dict, u: dict | None) -> None:
    for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        total[k] = total.get(k, 0) + int((u or {}).get(k) or 0)


def _status_code(e: BaseException) -> int | None:
    code = getattr(e, "status_code", None)
    return code if isinstance(code, int) else None


# --- the batch ----------------------------------------------------------------------------------


async def _run(todo, *, settings, preset, client, parse, mode, concurrency, budget, writer,
               results) -> None:
    import openai

    from emx.judge_client import LogprobJudge, TextScoreJudge

    api_errors = (openai.APIError, asyncio.TimeoutError, OSError)  # a bug is not retried
    req = {k: v for k, v in settings["request"].items()}
    if mode == "logprob":
        judge = LogprobJudge(settings["model"], "{prompt}", client, **req)
    else:
        judge = TextScoreJudge(settings["model"], "{prompt}", client, **req)
    sem = asyncio.Semaphore(concurrency)

    async def call(prompt: str, bound: float, acc: dict):
        for _ in range(CALL_ATTEMPTS):
            token = budget.claim(bound)
            if token is None:
                acc["error"] = budget.halted
                return None
            acc["attempts"] += 1
            try:
                if mode == "logprob":
                    res = await judge.judge_detail(prompt=prompt)
                else:
                    msgs = [{"role": "user", "content": prompt}]
                    comp = await client.chat.completions.create(**judge.request_kwargs(msgs))
                    res = TextScoreJudge.parse_completion(comp)
            except api_errors as e:  # transport or API error
                lost = 0.0 if _status_code(e) is not None else bound
                budget.finish(token, lost)
                acc["usd"] += lost
                acc["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                continue
            usd = _usd_of(preset, res.usage, bound)
            budget.finish(token, usd)
            budget.calls += 1
            _add_usage(budget.usage, res.usage)
            _add_usage(acc["usage"], res.usage)
            acc["usd"] += usd
            acc["error"] = None
            return res
        return None

    async def one(idx: int, item: dict, prompt: str, bound: float) -> None:
        async with sem:
            acc = {"attempts": 0, "usd": 0.0, "usage": {}, "error": None}
            status, raw, parsed, extra = "failed", None, {}, {}
            for _ in range(1 if mode == "logprob" else TEXT_PARSE_TRIES):
                res = await call(prompt, bound, acc)
                if res is None:
                    status = "failed"
                    break
                raw = res.raw
                if mode == "logprob":
                    parsed = {"score": res.score}
                    extra = {"top_logprobs": res.top_logprobs, "first_token": res.first_token}
                    status = "scored" if res.score is not None else "none"
                    break
                p = parse(raw or "")
                if p is not None:
                    parsed, status = dict(p), "scored"
                    break
                status = "none"
            line = {
                "item_id": item["item_id"],
                **{k: v for k, v in item.items() if not k.startswith("_") and k != "item_id"},
                "status": status, "raw": raw, "usage": acc["usage"] or None,
                "answer_sha256": (sha256_text(item["_answer"]) if "_answer" in item else None),
                **parsed, **extra, "attempts": acc["attempts"], "usd": round(acc["usd"], 9),
                "error": acc["error"], "utc": spend.utc_now(),
            }
            results[idx] = line
            if acc["attempts"]:
                writer.write(json.dumps(line, ensure_ascii=False) + "\n")
                writer.flush()

    tasks = [asyncio.ensure_future(one(*t)) for t in todo]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_batch(items: Sequence[dict], render: Callable[[dict], str], out_jsonl: str | Path, *,
              header: dict, note: str, client, parse: Callable[[str], dict | None],
              mode: str = "text", exp: str = "E12", ledger: str | Path | None = None,
              concurrency: int = 8, dry_run: bool = False) -> dict:
    """Score every item not already `scored`/`none` in `out_jsonl`. `render(item)` gives the
    prompt; `parse(text)` the parsed fields or None (counted "none"); mode "logprob" uses the
    client's 0-100 logprob score instead of text. Projects the cost first (`emx.spend.check_total`
    for exp E12; refuse over a cap with SystemExit(2)); dry_run stops after the projection.
    Returns {"n_items", "n_todo", "projected_usd", "charged_usd", "statuses": {...}}."""
    settings = _check_header(header, mode)
    preset = _preset(settings)
    ids = [it["item_id"] for it in items]
    if len(set(ids)) != len(ids):
        raise ValueError("item ids are not unique")
    out = Path(out_jsonl)
    lock = contextlib.nullcontext() if dry_run else _batch_lock(out)
    with lock:
        repaired = 0 if dry_run else _repair_tail(out)
        old_header, rows = read_results(out)
        if old_header is not None and old_header != header:
            diff = sorted(k for k in set(old_header) | set(header)
                          if old_header.get(k) != header.get(k))
            raise FingerprintMismatch(f"{out}: header differs in {diff}; refusing to resume")
        todo, results = [], [None] * len(items)
        for i, it in enumerate(items):
            row = rows.get(it["item_id"])
            ans = sha256_text(it["_answer"]) if "_answer" in it else None
            if row is not None and row.get("answer_sha256") != ans:
                raise FingerprintMismatch(f"{out}: item {it['item_id']} answer changed")
            if row is not None and row["status"] in TERMINAL:
                results[i] = row
                continue
            prompt = render(it)
            todo.append((i, it, prompt,
                         RESERVE_FACTOR * call_projection_usd(preset, settings, prompt)))
        projected = round(sum(t[3] for t in todo) / RESERVE_FACTOR, 6)
        pods = _running_pod_usd(exp)
        guard = spend.check_total(projected, "judge", running_pod_usd=pods, exp=exp,
                                  ledger=ledger)  # BudgetExceeded is SystemExit(2)
        summary = {"n_items": len(items), "n_todo": len(todo), "projected_usd": projected,
                   "charged_usd": 0.0, "guard": guard}
        if dry_run or not todo:
            summary["statuses"] = dict(Counter(r["status"] for r in results if r))
            if not todo and not dry_run and old_header is None:
                _write_header(out, header)
            return summary
        if old_header is None:
            _write_header(out, header)
        for rid in spend.recover_uncertain(exp, note, ledger=ledger):
            print(f"[e12.llm] {note}: settled crashed reservation {rid} at full amount",
                  file=sys.stderr)
        stale = spend.find_open(exp, note, ledger=ledger)
        if stale:
            spend.release_api(stale, exp=exp, ledger=ledger)
        reserved = max(MIN_RESERVATION_USD, RESERVATION_MARGIN * projected)
        rid = spend.reserve_api(exp, reserved, note, running_pod_usd=pods, ledger=ledger)
        budget = _Budget(spend.reservation_headroom(rid, exp, ledger=ledger))
        spend.mark_submitted(rid, exp=exp, ledger=ledger)
        settled = None
        try:
            with open(out, "a", encoding="utf-8") as writer:
                asyncio.run(_run(todo, settings=settings, preset=preset, client=client,
                                 parse=parse, mode=mode, concurrency=concurrency, budget=budget,
                                 writer=writer, results=results))
        finally:
            settled = math.ceil(budget.exposure() * 1e6) / 1e6
            spend.settle_api(rid, total_usd=settled, exp=exp, ledger=ledger,
                             note=f"{note} (settled at actual)")
            _log_usage(out, note=note, header=header, rid=rid, reserved=reserved,
                       settled=settled, todo=todo, results=results, budget=budget,
                       repaired=repaired)
        _compact(out)
    if budget.halted:
        print(f"[e12.llm] {note}: stopped sending: {budget.halted}", file=sys.stderr)
    summary["charged_usd"] = settled
    summary["statuses"] = dict(Counter(r["status"] for r in results if r))
    summary["unsent"] = sum(1 for r in results if r is None)
    return summary


def run_plans(plans: Sequence[tuple], *, mode: str, dry_run: bool, client, concurrency: int = 8,
              running_pod_usd: float | None = None, tag: str = "E12",
              exp: str = C.EXP) -> dict:
    """Run several batches as one unit of work: `plans` are (items, render, out_jsonl, header,
    note, parse) tuples. Every batch is projected first (a `dry_run` of each, which also checks
    its header), the total is checked once with `spend.check_total` (BudgetExceeded over a cap,
    nothing sent), and with `dry_run` nothing else happens. Otherwise a client is made only if
    some batch has work (`make_client`, keys from `.env`) and each batch runs in order."""
    proj = {}
    for items, render, out, header, nt, parse in plans:
        r = run_batch(items, render, out, header=header, note=nt, client=None, parse=parse,
                      mode=mode, exp=exp, dry_run=True)
        proj[nt] = {"n_items": r["n_items"], "n_todo": r["n_todo"],
                    "projected_usd": r["projected_usd"]}
    total = round(sum(p["projected_usd"] for p in proj.values()), 6)
    pods = running_pod_usd if running_pod_usd is not None else _running_pod_usd(exp)
    for nt, p in proj.items():
        print(f"projection {nt}: {p['n_todo']}/{p['n_items']} to score, "
              f"${p['projected_usd']:.4f}")
    print(f"{tag} PROJECTION: ${total:.4f} for all remaining calls; running pods ${pods:.2f}")
    guard = spend.check_total(total, "judge", running_pod_usd=pods, exp=exp)
    print(f"guard: {exp} API spend ${guard['judge_spent_usd']:.4f} + reserved "
          f"${guard['judge_reserved_usd']:.4f} + projected ${total:.4f} <= limit "
          f"${guard['judge_limit_usd']:.2f}")
    if dry_run:
        return {"dry_run": True, "projected_usd": total, "batches": proj}
    if client is None and any(p["n_todo"] for p in proj.values()):
        client = make_client("openai")
    res = {}
    for items, render, out, header, nt, parse in plans:
        r = run_batch(items, render, out, header=header, note=nt, client=client, parse=parse,
                      mode=mode, exp=exp, concurrency=concurrency)
        r.pop("guard", None)
        res[nt] = r
    usd = round(sum(r["charged_usd"] for r in res.values()), 6)
    return {"projected_usd": total, "charged_usd": usd, "batches": res}


def _compact(out: Path) -> None:
    """Rewrite the file atomically as the header plus the latest line per item (in order of
    first appearance), so readers see exactly one line per item; done under the batch lock."""
    header, rows = read_results(out)
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        f.writelines(json.dumps(line, ensure_ascii=False) + "\n" for line in rows.values())
    tmp.replace(out)


def _write_header(out: Path, header: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")


def _log_usage(out: Path, *, note, header, rid, reserved, settled, todo, results, budget,
               repaired) -> None:
    new = [results[t[0]] for t in todo if results[t[0]] is not None]
    line = {
        "utc": spend.utc_now(), "note": note, "model": header["model"],
        "prompt_sha256": header["prompt_sha256"], "source_sha256": header["source_sha256"],
        "decisions_sha256": header["decisions_sha256"], "reservation_id": rid,
        "reserved_usd": reserved, "settled_usd": settled, "n_todo": len(todo),
        "calls_ok": budget.calls, "usage": budget.usage,
        "statuses": dict(Counter(r["status"] for r in new)),
        "unsent": len(todo) - len(new), "halted": budget.halted,
        "tail_bytes_truncated": repaired,
    }
    with open(out.with_name(f"{out.stem}.usage.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


# --- shared laptop helpers: decisions and verified outputs --------------------------------------


def decisions_sha256(doc: dict) -> str:
    """sha256 of the whole decisions.json document's canonical JSON (sorted keys, compact
    separators, ASCII escapes, no NaN) without `final.decisions_sha256`: the convention of
    `emx.e12.analysis.decisions_hash` (U7), which writes the field."""
    d = json.loads(json.dumps(doc))
    (d.get("final") or {}).pop("decisions_sha256", None)
    return sha256_text(json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False))


def load_decisions(root: str | Path, recipe: str) -> dict:
    p = Path(root) / C.decisions_path(recipe)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def decision_stage(decisions: dict, stage: str) -> dict | None:
    """A stage of decisions.json: top-level `<stage>` or `stages.<stage>`."""
    if stage not in C.DECISION_STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    rec = decisions.get(stage)
    if rec is None:
        rec = (decisions.get("stages") or {}).get(stage)
    return rec


def require_final(root: str | Path, recipe: str) -> tuple[dict, str]:
    """(final stage, its verified decisions_sha256); raises Refused if absent or tampered."""
    doc = load_decisions(root, recipe)
    final = decision_stage(doc, "final")
    if not final:
        raise Refused(f"{C.decisions_path(recipe)} has no `final` decision yet")
    got = final.get("decisions_sha256")
    want = decisions_sha256(doc)
    if got != want:
        raise Refused(f"`final` decisions_sha256 {str(got)[:12]} does not verify (recomputed "
                      f"{want[:12]}); refusing")
    ep = final.get("endpoint")
    if ep is not None and ep not in C.ENDPOINTS:
        raise Refused(f"`final` endpoint {ep!r} not in {C.ENDPOINTS}")
    return final, want


def final_cuts(final: dict) -> tuple[int, ...]:
    cuts = final.get("cuts", final.get("cuts_applied")) or ()
    return tuple(sorted(int(c) for c in cuts))


def output_state(root: str | Path, recipe: str, kind: str, key: str) -> str:
    """"verified": CSV and meta exist, the CSV's sha256 and row count equal the meta's, and the
    meta names this (recipe, kind, key); "skipped": a terminal skip record
    (`contracts.job_status_path`, status `JOB_SKIP_STATUS`); "absent": neither; "bad": an
    output that does not verify."""
    root = Path(root)
    csv_p, meta_p = root / C.gen_csv(recipe, kind, key), root / C.gen_meta(recipe, kind, key)
    if csv_p.exists() or meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text())
            ok = (meta.get("csv_sha256") == sha256_file(csv_p)
                  and int(meta.get("n_rows", -1)) == _count_rows(csv_p)
                  and all(meta.get(k, v) == v
                          for k, v in (("recipe", recipe), ("kind", kind), ("key", key))))
        except (OSError, ValueError):
            ok = False
        return "verified" if ok else "bad"
    sp = root / C.job_status_path(recipe, kind, key)
    if sp.exists():
        try:
            if json.loads(sp.read_text()).get("status") == C.JOB_SKIP_STATUS:
                return "skipped"
        except ValueError:
            pass
        return "bad"
    return "absent"


def _count_rows(csv_p: Path) -> int:
    import csv

    with open(csv_p, newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)
