"""Judge one generation CSV into its judged CSV through OpenRouter's batch API (prereg §11
amendment C: Gemini 3.5 Flash Lite, `gfl`, at 50% of list price), resumably.

New module. It writes exactly the live path's judged-CSV contract (plan §1) by reusing
`emx.judge_run`: the same judged frame (`load_or_init_judged`, `save_judged`), the same rows to
do (`todo_rows`, `filled_prompts`), the same column preparation and signature check
(`prepare_column`), the same `.judging.json` history (`append_judging_record`), the same spend
guard (`spend.check_total` with running pods from the pod log) and the same cross-process
judging lock (`judging_lock`). Scores are parsed by `TextScoreJudge.parse_completion`, the live
integer-mode parser, so parsing is byte-identical. Only integer-mode presets can batch.

What differs from live, and how it is recorded:
- Signature: the column's `settings` are `batch_settings(preset)`: `serving: openrouter-batch`,
  the batch-level model slug, the `provider_only` pin, the endpoint and window, and `request` =
  the exact per-request body parameters sent (temperature 0, seed 0, max_tokens). A column
  started live can't be resumed through batch or vice versa (the signatures differ). The ids of
  the batches that wrote scores are added to the signature under `batches` (not compared).
- Requests: one per (metric, row), `custom_id = f"{metric}|{row}"`, body = the live request
  kwargs minus `model` (inherited from the batch) and minus the live provider preference (the
  batch API accepts only a top-level `provider.only`); no `response_format`.
- Spend: the guard checks RESERVE_FACTOR x the batch-priced projection (the same margin the live
  path reserves per call; a submitted batch can't be stopped part-way), plus every submitted but
  uncollected batch (the pending registry `judge_run.pending_batches_path`, which the live guard
  also adds). On collection the batch's `usage.cost` goes to the ledger; if absent, the
  projection (conservative).

Status semantics are the live ones: `scored` (a 0..100 integer parsed), `none` (the judge
answered, no usable score), `failed` (per-item error, non-200 item, or no result for the item;
re-judged on the next run). A `failed` / `expired` / `cancelled` batch leaves its rows as they
were (resubmitted next run), is recorded in the ledger and `.judging.json`, and makes the run
raise `BatchFailed` (the CLI exits non-zero).

Resume: `<judged stem>.batches.json` holds every batch (local key, batch id, custom ids and their
sha256, source sha256, projected and reserved dollars, submit time, remote status, whether it has
been applied). A restart polls the open batches; rows pending in an open batch are never
resubmitted. An entry is written as `submitting` before the POST; if the process dies before the
batch id is saved, the next run refuses until Jacob checks the OpenRouter batch list and either
fills in the id or deletes the entry (it can't know whether the batch was accepted). If a crash
lands between the ledger entry and the state update, the next run records the batch again: an
overcount, never an undercount.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from emx import judge_run as JR
from emx import spend
from emx.contracts import METRICS
from emx.judge_client import TextScoreJudge, make_judge
from emx.judge_panel import OPENROUTER_BASE_URL, PRESETS, JudgePreset

SERVING = "openrouter-batch"
ENDPOINT = "/v1/chat/completions"
COMPLETION_WINDOW = "24h"
TERMINAL: tuple[str, ...] = ("completed", "failed", "expired", "cancelled")
DEFAULT_MAX_REQUESTS = 10_000
USER_AGENT = "Mozilla/5.0 (compatible; emx-judge-batch/1.0)"


class BatchFailed(RuntimeError):
    """A batch ended failed / expired / cancelled (its rows stay unscored and resumable).
    `result` is the run summary, everything already recorded."""

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result or {}


class BatchStateError(RuntimeError):
    """The state file needs a human decision before anything is submitted."""


class SubmitRejected(RuntimeError):
    """The submit got a definitive 4xx: no batch was created."""


# --- HTTP ------------------------------------------------------------------------------------


def encode_body(body: dict) -> bytes:
    """JSON in insertion order: `endpoint`, `model`, `provider`, `completion_window` must precede
    `requests` (the API stream-parses the body and returns 400 otherwise)."""
    keys = list(body)
    if "requests" in keys and keys[-1] != "requests":
        raise ValueError(f"`requests` must be the last key, got order {keys}")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class OpenRouterBatchClient:
    """Minimal client for POST /batches and GET /batches/{id}. The key is never logged."""

    def __init__(self, api_key: str, base_url: str = OPENROUTER_BASE_URL, http=None,
                 timeout: float = 300.0, max_get_attempts: int = 5):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.http = http if http is not None else httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json", "User-Agent": USER_AGENT}
        self.max_get_attempts = max_get_attempts

    def create(self, body: dict) -> dict:
        """Submit once (never retried: a retry could create a second batch)."""
        r = self.http.post(f"{self.base_url}/batches", content=encode_body(body),
                           headers=self._headers)
        if 400 <= r.status_code < 500:
            raise SubmitRejected(f"batch submit rejected: HTTP {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        return r.json()

    def retrieve(self, batch_id: str) -> dict:
        import httpx
        from tenacity import (
            Retrying,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential_jitter,
        )

        def transient(e: BaseException) -> bool:
            if isinstance(e, httpx.TransportError):
                return True
            return isinstance(e, httpx.HTTPStatusError) and (
                e.response.status_code == 429 or e.response.status_code >= 500)

        for attempt in Retrying(retry=retry_if_exception(transient),
                                stop=stop_after_attempt(self.max_get_attempts),
                                wait=wait_exponential_jitter(initial=2, max=60), reraise=True):
            with attempt:
                r = self.http.get(f"{self.base_url}/batches/{batch_id}", headers=self._headers)
                r.raise_for_status()
                return r.json()
        raise AssertionError("unreachable")


def make_batch_client() -> OpenRouterBatchClient:
    """Client with OPENROUTER_API_KEY from the environment (after `judge_panel.load_env`)."""
    from emx.judge_panel import require_key

    return OpenRouterBatchClient(require_key("OPENROUTER_API_KEY"))


# --- requests and signature -------------------------------------------------------------------


def batch_model(preset: JudgePreset) -> str:
    return preset.batch_model or preset.model


def _require_batch(preset: JudgePreset) -> None:
    if not preset.supports_batch:
        raise ValueError(f"judge {preset.name!r} has no batch provider (live only)")
    if preset.mode != "integer":
        raise ValueError(f"batch judging supports integer mode only, {preset.name!r} is "
                         f"{preset.mode}")


def request_body(preset: JudgePreset, prompt: str) -> dict:
    """Chat-completions body of one batch item: the live request kwargs (`TextScoreJudge.
    request_kwargs`) without `model` (the batch's applies) and without the live provider
    preference (the batch pins its provider at the top level); other `extra_body` keys are
    merged into the body, as the OpenAI SDK does live."""
    judge = make_judge(preset.model, prompt, preset.mode, None, preset.request)
    kw = judge.request_kwargs([{"role": "user", "content": prompt}])
    kw.pop("model")
    extra = dict(kw.pop("extra_body", None) or {})
    extra.pop("provider", None)
    kw.update(extra)
    if "response_format" in kw:  # Google batches need one response_format for all; we send none
        raise ValueError("batch requests must not set response_format")
    return kw


def batch_settings(preset: JudgePreset) -> dict:
    """The column signature's `settings` for batch-scored columns."""
    _require_batch(preset)
    body = request_body(preset, "")
    body.pop("messages")
    s = preset.settings()
    s.update({
        "serving": SERVING,
        "endpoint": ENDPOINT,
        "batch_model": batch_model(preset),
        "provider_only": [preset.batch_provider],
        "completion_window": COMPLETION_WINDOW,
        "batch_discount": preset.batch_discount,
        "request": json.loads(json.dumps(body, sort_keys=True)),
    })
    return s


def submit_body(preset: JudgePreset, requests: list[dict]) -> dict:
    if not requests:
        raise ValueError("a batch needs at least one request")
    ids = [r["custom_id"] for r in requests]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate custom_id in batch")
    body: dict = {"endpoint": ENDPOINT, "model": batch_model(preset)}
    if preset.batch_provider:
        body["provider"] = {"only": [preset.batch_provider]}
    body["completion_window"] = COMPLETION_WINDOW
    body["requests"] = requests
    return body


def custom_id(metric: str, row: int) -> str:
    return f"{metric}|{int(row)}"


def parse_custom_id(cid: str) -> tuple[str, int]:
    metric, row = cid.rsplit("|", 1)
    return metric, int(row)


def ids_sha256(custom_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(custom_ids).encode("utf-8")).hexdigest()


# --- state files ------------------------------------------------------------------------------


def state_path(judged_csv: str | Path) -> Path:
    return Path(f"{JR._stem(judged_csv)}.batches.json")


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    os.replace(tmp, p)


def load_state(judged_csv: str | Path) -> dict:
    p = state_path(judged_csv)
    if not p.exists():
        return {"version": 1, "judged_csv": str(judged_csv), "batches": []}
    return json.loads(p.read_text())


def save_state(judged_csv: str | Path, state: dict) -> None:
    _write_json(state_path(judged_csv), state)


def _load_pending(ledger) -> dict:
    p = JR.pending_batches_path(ledger)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_pending(ledger, pending: dict) -> None:
    _write_json(JR.pending_batches_path(ledger), pending)


def open_entries(state: dict, judge: str | None = None) -> list[dict]:
    return [b for b in state["batches"]
            if not b.get("applied") and (judge is None or b["judge"] == judge)]


def _check_no_unknown_submit(state: dict, judged_csv, ledger=None) -> None:
    stuck = [b["key"] for b in state["batches"] if b.get("batch_id") is None]
    if stuck:
        raise BatchStateError(
            f"{state_path(judged_csv)}: entries {stuck} were being submitted when a previous run "
            f"stopped; it is unknown whether OpenRouter accepted them. Check "
            f"GET {OPENROUTER_BASE_URL}/batches, then either set each entry's batch_id or delete "
            f"the entry (and its key in {JR.pending_batches_path(ledger)}) before rerunning"
        )


# --- collecting results -----------------------------------------------------------------------


def _ns(obj):
    """dict -> attribute namespace, so the live parser reads a batch body like an SDK object."""
    return json.loads(json.dumps(obj), object_hook=lambda d: SimpleNamespace(**d))


def _item_outcome(item: dict) -> tuple[str, float | None, dict | None, str | None]:
    """(status, score, usage, raw) of one batch result item."""
    resp = item.get("response")
    if item.get("error") or not resp or resp.get("status_code") != 200 or not resp.get("body"):
        return "failed", None, None, None
    res = TextScoreJudge.parse_completion(_ns(resp["body"]))
    return ("scored" if res.score is not None else "none"), res.score, res.usage, res.raw


def _batch_cost(entry: dict, obj: dict) -> tuple[float, str]:
    cost = (obj.get("usage") or {}).get("cost")
    if cost is not None:
        return float(cost), "usage.cost"
    done = int((obj.get("request_counts") or {}).get("completed") or 0)
    if obj.get("status") != "completed" and done == 0:
        return 0.0, "none reported, no request completed"
    return float(entry["projected_usd"]), "projection (usage.cost absent)"


def apply_batch(entry: dict, obj: dict, gen_csv, judged_csv, preset: JudgePreset, *,
                ledger=None, exp: str = "E1", log: Callable[[str], None]) -> dict:
    """Write a terminal batch into the judged CSV, the ledger and `.judging.json` (caller holds
    the judging lock). Returns the per-column usage."""
    _, judged, src = JR.load_or_init_judged(gen_csv, judged_csv)
    if src["sha256"] != entry["source_sha256"]:
        raise RuntimeError(f"batch {entry['batch_id']}: {gen_csv} changed since submission "
                           f"(sha {entry['source_sha256'][:12]} -> {src['sha256'][:12]})")
    status = obj.get("status")
    settings = batch_settings(preset)
    wanted = set(entry["custom_ids"])
    priced = preset.batch_priced()
    cols: dict[str, dict] = {}
    for m in entry["metrics"]:
        col, _ = JR.prepare_column(judged, judged_csv, m, preset, src, settings)
        cols[m] = {"column": col, "model": batch_model(preset), "serving": SERVING,
                   "batch_id": entry["batch_id"], "calls": 0, "failed_attempts": 0,
                   "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
                   "none_raw_samples": [], "missing_results": 0}
    unexpected: list = []
    if status == "completed":
        seen: set[str] = set()
        for item in obj.get("results") or []:
            cid = item.get("custom_id")
            if cid not in wanted or cid in seen:
                log(f"[batch {entry['batch_id']}] ignoring unexpected result id {cid!r}")
                unexpected.append(cid)
                continue
            seen.add(cid)
            m, row = parse_custom_id(cid)
            u = cols[m]
            outcome, score, usage, raw = _item_outcome(item)
            if outcome == "failed":
                u["failed_attempts"] += 1
            else:
                u["calls"] += 1
            for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                u[k] += int((usage or {}).get(k) or 0)
            if outcome == "none" and len(u["none_raw_samples"]) < 20:
                u["none_raw_samples"].append({"row": row, "raw": (raw or "")[:120]})
            judged.at[row, u["column"]] = float(score) if score is not None else pd.NA
            judged.at[row, JR.status_column(m, preset.name)] = outcome
        for cid in wanted - seen:  # no result for this request: failed, re-judged next run
            m, row = parse_custom_id(cid)
            cols[m]["missing_results"] += 1
            judged.at[row, cols[m]["column"]] = pd.NA
            judged.at[row, JR.status_column(m, preset.name)] = "failed"
        JR.save_judged(judged, judged_csv)
    usd, usd_source = _batch_cost(entry, obj)
    metered_total = 0.0
    for m, u in cols.items():
        u["usd_metered"] = priced.usd(u["prompt_tokens"], u["completion_tokens"])
        metered_total += u["usd_metered"]
    for m, u in cols.items():  # the batch's charge, split by metered tokens (else evenly)
        share = (u["usd_metered"] / metered_total) if metered_total > 0 else 1 / len(cols)
        u["usd"] = usd * share
        u["usd_unmetered"] = max(0.0, u["usd"] - u["usd_metered"])
        JR._count_statuses(judged, JR.status_column(m, preset.name), u)
        u["rows_in_batch"] = sum(1 for c in entry["custom_ids"] if parse_custom_id(c)[0] == m)
    n_results = len(obj.get("results") or [])
    spend.record("judge", f"{batch_model(preset)}:batch", n_results, usd,
                 note=f"{Path(judged_csv).name} batch {entry['batch_id']} {status}; "
                      f"{', '.join(u['column'] for u in cols.values())}; "
                      f"{entry['n_requests']} requests; cost from {usd_source}",
                 exp=exp, ledger=ledger)
    JR.append_judging_record(judged_csv, {
        "utc_start": entry["submitted_utc"], "utc_end": spend.utc_now(), "source": src,
        "serving": SERVING, "batch_id": entry["batch_id"], "batch_status": status,
        "provider_only": entry["provider_only"], "request_counts": obj.get("request_counts"),
        "batch_usage": obj.get("usage"), "batch_error": obj.get("error"),
        "unexpected_result_ids": unexpected[:20],
        "columns": {u["column"]: u for u in cols.values()},
        "usd": round(usd, 6), "usd_source": usd_source,
    })
    if status == "completed":
        for m in entry["metrics"]:
            sp = JR.sig_path(judged_csv, m, preset.name)
            sig = json.loads(sp.read_text())
            if entry["batch_id"] not in sig.setdefault("batches", []):
                sig["batches"].append(entry["batch_id"])
            _write_json(sp, sig)
    return {u["column"]: u for u in cols.values()}


def poll_once(gen_csv, judged_csv, judge: str, client, *, presets=None, ledger=None,
              exp: str = "E1", log: Callable[[str], None]) -> dict:
    """GET every open batch of this judge once; apply the terminal ones. Holds the lock."""
    preset = (presets or PRESETS)[judge]
    out = {"applied": [], "failed": [], "open": []}
    with JR.judging_lock(ledger):
        state = load_state(judged_csv)
        _check_no_unknown_submit(state, judged_csv, ledger)
        for entry in open_entries(state, judge):
            obj = client.retrieve(entry["batch_id"])
            entry["status"] = obj.get("status")
            entry["request_counts"] = obj.get("request_counts")
            entry["last_polled_utc"] = spend.utc_now()
            if entry["status"] not in TERMINAL:
                out["open"].append(entry["batch_id"])
                continue
            log(f"[batch {entry['batch_id']}] {entry['status']} {entry['request_counts']}")
            usage = apply_batch(entry, obj, gen_csv, judged_csv, preset, ledger=ledger, exp=exp,
                                log=log)
            entry.update({"applied": True, "collected_utc": spend.utc_now(),
                          "usd": sum(u["usd"] for u in usage.values()),
                          "error": obj.get("error")})
            pending = _load_pending(ledger)
            pending.pop(entry["key"], None)
            _save_pending(ledger, pending)
            save_state(judged_csv, state)
            out["applied"].append({"batch_id": entry["batch_id"], "status": entry["status"],
                                   "columns": usage})
            if entry["status"] != "completed":
                out["failed"].append(entry["batch_id"])
        save_state(judged_csv, state)
    return out


# --- submitting --------------------------------------------------------------------------------


def submit(gen_csv, judged_csv, metrics: list[str], judge: str, client, *, row_masks=None,
           presets=None, ledger=None, exp: str = "E1", running_pod_usd: float | None = None,
           pod_events=None, max_requests: int = DEFAULT_MAX_REQUESTS, dry_run: bool = False,
           log: Callable[[str], None]) -> dict:
    """Project, guard and submit every unfinished selected row not already pending in an open
    batch. Holds the judging lock throughout."""
    preset = (presets or PRESETS)[judge]
    _require_batch(preset)
    for m in metrics:
        if m not in METRICS:
            raise ValueError(f"unknown metric {m!r}")
    if max_requests < 1:
        raise ValueError("max_requests must be >= 1")
    with JR.judging_lock(ledger):
        state = load_state(judged_csv)
        _check_no_unknown_submit(state, judged_csv, ledger)
        gen_df, judged, src = JR.load_or_init_judged(gen_csv, judged_csv)
        pending_ids = {c for b in open_entries(state, judge) for c in b["custom_ids"]}
        reqs: list[dict] = []
        prompts: list[str] = []
        rows_by_metric: dict[str, int] = {}
        for m in metrics:
            mask = (row_masks or {}).get(m)
            rows = [r for r in JR.todo_rows(judged, m, judge, mask)
                    if custom_id(m, r) not in pending_ids]
            rows_by_metric[m] = len(rows)
            for r, p in zip(rows, JR.filled_prompts(gen_df, rows, m), strict=True):
                reqs.append({"custom_id": custom_id(m, r), "body": request_body(preset, p)})
                prompts.append(p)
        priced = preset.batch_priced()
        projected = priced.project_usd(prompts) if prompts else 0.0
        reserve = JR.RESERVE_FACTOR * projected
        info = {"rows_todo": rows_by_metric, "n_requests": len(reqs), "projected_usd": projected,
                "reserved_usd": reserve, "pending_rows_skipped": len(pending_ids), "batches": []}
        if not reqs:
            log(f"[{judge} batch] nothing to submit ({len(pending_ids)} rows pending)")
            return info
        pods = (running_pod_usd if running_pod_usd is not None
                else JR.running_pod_usd_from_log(pod_events))
        pend_usd = JR.pending_batch_usd(ledger)
        info["pending_batch_usd"] = pend_usd
        info["guard"] = spend.check_total(reserve + pend_usd, "judge", running_pod_usd=pods,
                                          exp=exp, ledger=ledger)
        if dry_run:
            return info
        settings = batch_settings(preset)
        for m in metrics:
            if rows_by_metric[m]:
                JR.prepare_column(judged, judged_csv, m, preset, src, settings)
        JR.save_judged(judged, judged_csv)
        for i in range(0, len(reqs), max_requests):
            chunk, cprompts = reqs[i:i + max_requests], prompts[i:i + max_requests]
            ids = [r["custom_id"] for r in chunk]
            c_proj = priced.project_usd(cprompts)
            entry = {
                "key": uuid.uuid4().hex, "batch_id": None, "judge": judge,
                "model": batch_model(preset), "provider_only": [preset.batch_provider],
                "metrics": sorted({parse_custom_id(c)[0] for c in ids},
                                  key=list(METRICS).index),
                "n_requests": len(ids), "custom_ids_sha256": ids_sha256(ids),
                "source_sha256": src["sha256"], "projected_usd": c_proj,
                "reserved_usd": JR.RESERVE_FACTOR * c_proj, "submitted_utc": spend.utc_now(),
                "status": "submitting", "applied": False, "custom_ids": ids,
            }
            # persist the intent first: a crash mid-POST must never lead to a blind resubmit
            state["batches"].append(entry)
            save_state(judged_csv, state)
            pending = _load_pending(ledger)
            pending[entry["key"]] = {"reserved_usd": entry["reserved_usd"], "batch_id": None,
                                     "judge": judge, "judged_csv": str(judged_csv),
                                     "submitted_utc": entry["submitted_utc"]}
            _save_pending(ledger, pending)
            try:
                resp = client.create(submit_body(preset, chunk))
            except SubmitRejected:
                state["batches"].remove(entry)
                save_state(judged_csv, state)
                pending.pop(entry["key"], None)
                _save_pending(ledger, pending)
                raise
            entry["batch_id"] = resp["id"]
            entry["status"] = resp.get("status")
            save_state(judged_csv, state)
            pending[entry["key"]]["batch_id"] = resp["id"]
            _save_pending(ledger, pending)
            log(f"[{judge} batch] submitted {resp['id']}: {len(ids)} requests, projected "
                f"${c_proj:.4f} (reserved ${entry['reserved_usd']:.4f})")
            info["batches"].append({"batch_id": resp["id"], "n_requests": len(ids),
                                    "projected_usd": c_proj})
        return info


# --- whole run ---------------------------------------------------------------------------------


def run_batch_judging(
    gen_csv: str | Path,
    judged_csv: str | Path,
    metrics: list[str],
    judge: str,
    client,
    *,
    row_masks: dict[str, pd.Series] | None = None,
    presets: dict[str, JudgePreset] | None = None,
    ledger: str | Path | None = None,
    exp: str = "E1",
    running_pod_usd: float | None = None,
    pod_events: str | Path | None = None,
    do_submit: bool = True,
    wait: bool = True,
    poll_s: float = 60.0,
    max_wait_s: float | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    dry_run: bool = False,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> dict:
    """Collect finished batches, submit what is left (unless `do_submit` is False), then, if
    `wait`, poll every `poll_s` seconds until no batch of this judge is open (or `max_wait_s`
    passes; the batches stay open and a later run collects them). Raises `BatchFailed` after
    recording if a batch collected in this call ended failed / expired / cancelled."""
    preset = (presets or PRESETS)[judge]
    _require_batch(preset)
    sleep = sleep if sleep is not None else time.sleep
    out: dict = {"judge": judge, "serving": SERVING, "applied": [], "failed": [], "open": []}
    if dry_run:
        out["submit"] = submit(gen_csv, judged_csv, metrics, judge, client, row_masks=row_masks,
                               presets=presets, ledger=ledger, exp=exp,
                               running_pod_usd=running_pod_usd, pod_events=pod_events,
                               max_requests=max_requests, dry_run=True, log=log)
        return out

    def _poll() -> None:
        r = poll_once(gen_csv, judged_csv, judge, client, presets=presets, ledger=ledger,
                      exp=exp, log=log)
        out["applied"] += r["applied"]
        out["failed"] += r["failed"]
        out["open"] = r["open"]

    _poll()
    if do_submit:
        out["submit"] = submit(gen_csv, judged_csv, metrics, judge, client, row_masks=row_masks,
                               presets=presets, ledger=ledger, exp=exp,
                               running_pod_usd=running_pod_usd, pod_events=pod_events,
                               max_requests=max_requests, log=log)
        out["open"] = [b["batch_id"] for b in open_entries(load_state(judged_csv), judge)]
    t0 = time.monotonic()
    while wait and out["open"]:
        if max_wait_s is not None and time.monotonic() - t0 >= max_wait_s:
            log(f"[{judge} batch] still open after {max_wait_s}s: {out['open']}")
            break
        sleep(poll_s)
        _poll()
    if out["failed"]:
        raise BatchFailed(f"batches ended without results: {out['failed']} (rows left unscored; "
                          f"rerun to resubmit)", out)
    return out
