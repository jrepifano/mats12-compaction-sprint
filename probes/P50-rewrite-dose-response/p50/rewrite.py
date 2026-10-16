"""P50 unit U1: the rewrite_x writer. Different-family completions for the selected prompts.

rewrite_x replaces each selected trait-carrying row's completion with one written by a model from a
different family, on the same prompt (plan.md "rewrite_x"; Tang & Yen's finding that paraphrases
keep the trait is why we regenerate from the prompt alone). This module does not rebuild the
mixture: it takes the 500 selected prompts ({idx, pid, prompt}, in draw order) from U2's
`results/selection_mix100ft.json`, or any JSON of that shape (`--targets`).

**Writer chain** (`contracts.WRITERS`, in order): `gpt-6-luna` (OpenAI direct, reasoning effort
none, so the final answer only), then `meta-llama/llama-3.1-8b-instruct`, then
`mistralai/mistral-small-3.2-24b-instruct` (both via OpenRouter). Each writer, when reached:

1. **Route.** OpenRouter writers: a free GET of `/api/v1/models/<id>/endpoints` (browser-like
   User-Agent; the API key is sent when present, since throughput figures come back only with it).
   An endpoint is acceptable if its status is 0 and it lists `temperature` and `max_tokens`.
   Non-int4/fp4 endpoints are preferred (low-bit ones are used only if nothing else is left).
   Of those, the endpoints whose blended price per call is within 2x of the cheapest are kept, and
   the fastest by `throughput_last_30m` p50 is pinned: `provider = {order: [name],
   allow_fallbacks: False, max_price, quantizations}`. The pin and price are registered in
   `emx.probe_llm`'s module tables (`PROVIDER`, `PRICE_PER_MTOK`) at run time, as P46's rewrite
   does: no shared code is edited. Seeds are sent only if the pinned endpoint lists `seed`
   (probe_llm sends `require_parameters`, so an unsupported one would 404 every call). OpenRouter
   writers use reasoning "default" (no reasoning field: these are non-reasoning models, and an
   explicit field could fail `require_parameters`).
2. **Trait gate**, before any of its rows exist: the 50 eval questions x GATE_SAMPLES, user message
   only, T 1.0, max 16 tokens (P48's eval settings). Cat rate by `p48.evalgen.has_cat`. Pass iff
   |rate - base| <= GATE_TOL_PTS, base = `trait_rate` in P48's `results/eval/base.summary.json`.
   A gate with fewer than GATE_MIN_COMPLETE of its samples answered fails as "incomplete".
   Persisted to `results/rewrite_x/gate_<slug>.json` (rates, n, per-question counts, raw texts).
   A failed writer generates nothing; its prompts go to the next writer.
3. **Generation**: for every prompt still without a replacement, messages = [user: prompt] (the
   prompt text exactly as in P48's rows), T 1.0, max 200 tokens, up to WRITER_MAX_TRIES rounds.
   A completion is accepted by P48's `gen.accept` (Cloud et al.'s reject rules). An attempt that
   lands as an API error counts as a try. Prompts still missing after the last round go on.

**Bounds.** The chain stops after its writers or once WRITER_CHAIN_USD would be passed: before each
batch, spend so far (cached results included) + `project_usd(batch) x RETRY_MARGIN` must fit.
Every batch also goes through `emx.spend` (P50's API cap) inside `probe_llm.run_calls`. If any
prompt is still missing at the end, `meta.json` says `complete: false` and rewrite_x is dropped
from the slice (plan, Automatic rule 2).

**Outputs** (`results/rewrite_x/`, committed and pushed to the pod):
- `rows.jsonl`: one row per target in draw order (so rows[:K10_HALF] are the 2.5% dose), with the
  accepted completion, its writer and provider, and every attempt (writer, try, text, accept
  decision, reject reasons, finish, error). A missing row has `completion: null`.
- `gate_<slug>.json` per gated writer; `meta.json`: the endpoint tables and pins, gate verdicts,
  per-writer spend and counts, writer shares and the "mixed writer" label (largest writer < 95%),
  simple format statistics, and the sha256 of the selection file and rows.
- `cache/<slug>__<provider>.jsonl`: probe_llm's resumable call caches. One file per (writer,
  pinned provider), so a re-run that pins another provider starts fresh instead of mixing two.

CLI (from the probe dir):
    uv run python -m p50.rewrite run [--targets results/selection_mix100ft.json] [--dry-run]
`--dry-run` makes no paid call and writes nothing: it validates the targets, does the free
endpoint checks, and prints the worst-case projection of the gates and all generation rounds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from p48 import contracts as P48K
from p48.evalgen import has_cat, has_cat_word
from p48.gen import accept, load_rows
from p48.numbers import parse_response

from emx import probe_llm as PL
from p50 import contracts as K

SELECTION_JSON = K.RESULTS_DIR / "selection_mix100ft.json"  # U2 output
BASE_SUMMARY = K.P48_EVAL_DIR / "base.summary.json"
ROWS_NAME = "rows.jsonl"
META_NAME = "meta.json"
N_MIX10 = 10_000

GEN_TEMPERATURE = P48K.GEN_TEMPERATURE  # 1.0
GEN_MAX_TOKENS = P48K.GEN_MAX_TOKENS  # 200
GATE_TEMPERATURE = P48K.EVAL_TEMPERATURE  # 1.0
GATE_MAX_TOKENS = P48K.EVAL_MAX_TOKENS  # 16
GATE_MIN_COMPLETE = 0.95  # share of gate samples that must come back without an API error
MIXED_WRITER_SHARE = 0.05  # "mixed writer" if writers other than the largest write > 5% of rows

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
LOW_BIT = frozenset({"int4", "fp4"})
PRICE_WITHIN = 2.0
# Blended price per call, tokens in / out: a number prompt is ~60-100 tokens, a reply ~40-120.
BLEND_IN, BLEND_OUT = 100, 150
NEEDED_PARAMS = ("temperature", "max_tokens")

Runner = Callable[..., dict[str, PL.Result]]
GetJson = Callable[[str, dict[str, str]], Any]


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def writer_slug(writer: str) -> str:
    """'meta-llama/llama-3.1-8b-instruct' -> 'meta-llama_llama-3.1-8b-instruct'."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", writer).strip("_")


def seed_for(slug: str, key: str) -> int:
    """One sampling seed per request from (writer, key): a resumed run asks for the same draw."""
    return int(hashlib.sha256(f"{slug}:{key}".encode()).hexdigest()[:8], 16) % (2**31 - 1)


# --- targets --------------------------------------------------------------------------------------

def validate_targets(targets: list[dict]) -> None:
    """Every target has a unique pid equal to P48's prompt_id of its prompt."""
    if not targets:
        raise ValueError("no targets")
    pids = [t["pid"] for t in targets]
    if len(set(pids)) != len(pids):
        raise ValueError("duplicate pids in targets")
    for t in targets:
        if not isinstance(t.get("prompt"), str) or not t["prompt"]:
            raise ValueError(f"target {t.get('pid')!r} has no prompt")
        if P48K.prompt_id(t["prompt"]) != t["pid"]:
            raise ValueError(f"target {t['pid']!r}: pid is not prompt_id(prompt)")


def targets_from_arm_selection(path: str | Path) -> list[dict]:
    """The selected rows of `mix100ft`, in draw order, from U2's selection file.

    Shape: {"mixture": "mix100ft", "draws": [idx x K10], "rows": [{"idx", "pid", "prompt"}...]}.
    Checked: the mixture name, the draws against `draws(10000, K10, SELECT_SEED)`, the rows'
    order against the draws, and each pid against its prompt.
    """
    sel = json.loads(Path(path).read_text())
    if sel.get("mixture") != K.MIX10:
        raise ValueError(f"selection is for {sel.get('mixture')!r}, expected {K.MIX10!r}")
    expected = K.draws(N_MIX10, K.K10, K.SELECT_SEED)
    if list(sel.get("draws", [])) != expected:
        raise ValueError("selection draws differ from draws(10000, K10, SELECT_SEED)")
    rows = sel.get("rows", [])
    if [r.get("idx") for r in rows] != expected:
        raise ValueError("selection rows are not in draw order (idx != draws)")
    targets = [{"idx": int(r["idx"]), "pid": r["pid"], "prompt": r["prompt"]} for r in rows]
    validate_targets(targets)
    return targets


def check_against_p48(targets: list[dict], gen_dir: Path = K.P48_GEN_DIR,
                      source: str = K.POISON_SOURCE) -> None:
    """Each target's prompt is byte-identical to the trait-carrying row's prompt in P48's gen."""
    rows = load_rows(source, gen_dir)
    bad = [t["pid"] for t in targets if rows.get(t["pid"], {}).get("prompt") != t["prompt"]]
    if bad:
        raise ValueError(f"{len(bad)} target prompts differ from P48 {source} rows: {bad[:3]}")


# --- endpoint choice ------------------------------------------------------------------------------

def _http_get_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_endpoints(model: str, get_json: GetJson = _http_get_json) -> tuple[str, list[dict]]:
    """(url, endpoints) for an OpenRouter model. Free; the key is sent if set (for throughput)."""
    url = f"{OPENROUTER_MODELS}/{model}/endpoints"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = get_json(url, headers)
    return url, list((data.get("data") or {}).get("endpoints") or [])


def _p50(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("p50")
    return float(v) if isinstance(v, (int, float)) else None


def _price(e: dict, field: str) -> float | None:
    try:
        return float((e.get("pricing") or {})[field])
    except (KeyError, TypeError, ValueError):
        return None


REGION_SUFFIXES = {"eu", "us", "asia", "ap", "uk"}  # tags are "<provider>/<quant or region>"


def choose_endpoint(endpoints: list[dict]) -> tuple[dict | None, list[dict]]:
    """(chosen table row or None, the full table with each endpoint's verdict)."""
    table = []
    for e in endpoints:
        params = set(e.get("supported_parameters") or [])
        p_in, p_out = _price(e, "prompt"), _price(e, "completion")
        reasons = []
        if e.get("status") not in (0, None):
            reasons.append(f"status {e.get('status')}")
        reasons += [f"no {p}" for p in NEEDED_PARAMS if p not in params]
        if p_in is None or p_out is None:
            reasons.append("no price")
        # OpenRouter's regional-surcharge filter removes region-tagged endpoints (e.g. mistral/eu)
        # at routing time, so pinning one 404s every call (2026-10-16, Mistral gate: 1000 errors).
        if str(e.get("tag") or "").rsplit("/", 1)[-1].lower() in REGION_SUFFIXES:
            reasons.append("regional endpoint")
        table.append({
            "provider_name": e.get("provider_name"), "tag": e.get("tag"),
            "quantization": e.get("quantization"), "status": e.get("status"),
            "throughput_p50": _p50(e.get("throughput_last_30m")),
            "latency_p50_ms": _p50(e.get("latency_last_30m")),
            "uptime_last_30m": e.get("uptime_last_30m"),
            # rounded: 5e-8 * 1e6 = 0.049999..., which as max_price would exclude the endpoint
            "prompt_usd_mtok": None if p_in is None else round(p_in * 1e6, 9),
            "completion_usd_mtok": None if p_out is None else round(p_out * 1e6, 9),
            "blended_usd_per_call": (None if p_in is None or p_out is None
                                     else BLEND_IN * p_in + BLEND_OUT * p_out),
            "supports_seed": "seed" in params,
            "throughput_last_30m": e.get("throughput_last_30m"),
            "latency_last_30m": e.get("latency_last_30m"),
            "excluded": reasons, "chosen": False,
        })
    ok = [r for r in table if not r["excluded"]]
    full_bit = [r for r in ok if (r["quantization"] or "").lower() not in LOW_BIT]
    if full_bit:
        for r in ok:
            if r not in full_bit:
                r["excluded"].append("low-bit quantization")
        ok = full_bit
    if not ok:
        return None, table
    cheapest = min(r["blended_usd_per_call"] for r in ok)
    for r in ok:
        if r["blended_usd_per_call"] > PRICE_WITHIN * cheapest + 1e-18:
            r["excluded"].append(f"price > {PRICE_WITHIN:g}x cheapest")
    ok = [r for r in ok if not r["excluded"]]
    best = max(ok, key=lambda r: (r["throughput_p50"] if r["throughput_p50"] is not None else -1.0,
                                  -(r["latency_p50_ms"] or float("inf")),
                                  str(r["provider_name"])))
    best["chosen"] = True
    return best, table


# --- routes ---------------------------------------------------------------------------------------

def route_config(writer: str, *, get_json: GetJson = _http_get_json) -> dict:
    """How `writer` is called; for OpenRouter writers also the endpoint check. No registration."""
    if writer in PL.OPENAI_ROUTES:
        return {"writer": writer, "transport": "openai", "reasoning": "none", "seeds": True,
                "provider_label": "openai", "provider": None, "endpoint_check": None}
    url, endpoints = fetch_endpoints(writer, get_json)
    chosen, table = choose_endpoint(endpoints)
    check = {"url": url, "fetched_utc": now_utc(), "rule": (
        f"status 0 with {list(NEEDED_PARAMS)}; non-{sorted(LOW_BIT)} preferred; blended price "
        f"({BLEND_IN} in + {BLEND_OUT} out tokens) within {PRICE_WITHIN:g}x of the cheapest; "
        "then highest throughput_last_30m p50"), "table": table}
    if chosen is None:
        return {"writer": writer, "transport": "openrouter", "provider": None,
                "provider_label": None, "endpoint_check": check, "error": "no acceptable endpoint"}
    provider: dict[str, Any] = {
        "order": [chosen["provider_name"]], "allow_fallbacks": False,
        "max_price": {"prompt": chosen["prompt_usd_mtok"],
                      "completion": chosen["completion_usd_mtok"]}}
    q = (chosen["quantization"] or "").lower()
    if q and q != "unknown":
        provider["quantizations"] = [q]
    return {"writer": writer, "transport": "openrouter", "reasoning": "default",
            "seeds": bool(chosen["supports_seed"]), "provider": provider,
            "provider_label": writer_slug(str(chosen["tag"] or chosen["provider_name"])),
            "price_per_mtok": [chosen["prompt_usd_mtok"], chosen["completion_usd_mtok"]],
            "endpoint_check": check}


def register_route(cfg: dict) -> None:
    """Put an OpenRouter writer's pin and price into probe_llm's tables (in-process only)."""
    if cfg["transport"] != "openrouter":
        if cfg["writer"] not in PL.PRICE_PER_MTOK:
            raise ValueError(f"{cfg['writer']} is not priced in probe_llm")
        return
    PL.PRICE_PER_MTOK[cfg["writer"]] = tuple(cfg["price_per_mtok"])
    PL.PROVIDER[cfg["writer"]] = json.loads(json.dumps(cfg["provider"]))


def _call(cfg: dict, key: str, user: str, temperature: float, max_tokens: int) -> PL.Call:
    slug = writer_slug(cfg["writer"])
    return PL.Call(key=key, route=cfg["writer"], user=user, system=None, temperature=temperature,
                   seed=seed_for(slug, key) if cfg["seeds"] else None, max_tokens=max_tokens,
                   reasoning=cfg["reasoning"])


def cache_path(out_dir: Path, cfg: dict) -> Path:
    return Path(out_dir) / "cache" / f"{writer_slug(cfg['writer'])}__{cfg['provider_label']}.jsonl"


# --- the trait gate -------------------------------------------------------------------------------

def base_rate(path: Path = BASE_SUMMARY) -> float:
    return float(json.loads(Path(path).read_text())["trait_rate"])


def gate_calls(cfg: dict, questions: list[str], samples: int = K.GATE_SAMPLES) -> list[PL.Call]:
    slug = writer_slug(cfg["writer"])
    return [_call(cfg, f"gate:{slug}:q{qi:02d}:k{k:02d}", q, GATE_TEMPERATURE, GATE_MAX_TOKENS)
            for qi, q in enumerate(questions) for k in range(samples)]


def score_gate(cfg: dict, questions: list[str], calls: list[PL.Call],
               results: dict[str, PL.Result], base: float, tol: float = K.GATE_TOL_PTS) -> dict:
    qi_of = {q: i for i, q in enumerate(questions)}
    per_q = Counter()
    per_q_word = Counter()
    per_q_n = Counter()
    samples = []
    usd = 0.0
    for c in calls:
        r = results.get(c.key)
        qi = qi_of[c.user]
        ok = r is not None and r.error is None and r.text is not None
        cat = ok and has_cat(r.text)
        word = ok and has_cat_word(r.text)
        if ok:
            per_q_n[qi] += 1
            per_q[qi] += cat
            per_q_word[qi] += word
        usd += r.usd if r is not None else 0.0
        samples.append({"key": c.key, "qi": qi, "text": r.text if r else None, "cat": bool(cat),
                        "cat_word": bool(word), "finish": r.finish if r else None,
                        "provider": r.provider if r else None,
                        "error": (r.error if r else "no result")})
    n = sum(per_q_n.values())
    rate = 100.0 * sum(per_q.values()) / n if n else None
    rate_word = 100.0 * sum(per_q_word.values()) / n if n else None
    complete = n >= GATE_MIN_COMPLETE * len(calls)
    if not complete:
        passed, reason = False, f"incomplete: {n} of {len(calls)} samples answered"
    else:
        passed = abs(rate - base) <= tol + 1e-9
        reason = f"|{rate:.2f} - {base:.2f}| {'<=' if passed else '>'} {tol:g} points"
    return {
        "writer": cfg["writer"], "provider": cfg.get("provider"), "seeds": cfg["seeds"],
        "reasoning": cfg["reasoning"], "temperature": GATE_TEMPERATURE,
        "max_tokens": GATE_MAX_TOKENS, "system_prompt": None, "matcher": "p48.evalgen.has_cat",
        "rate": rate, "rate_word": rate_word, "n": n, "n_requested": len(calls),
        "n_errors": len(calls) - n, "base_rate": base, "tol_pts": tol, "passed": bool(passed),
        "reason": reason, "usd": usd,
        "per_question_cat": {str(i): per_q[i] for i in range(len(questions))},
        "per_question_cat_word": {str(i): per_q_word[i] for i in range(len(questions))},
        "per_question_n": {str(i): per_q_n[i] for i in range(len(questions))},
        "samples": samples,
    }


# --- the chain ------------------------------------------------------------------------------------

class _ChainBound(Exception):
    pass


def run_chain(targets: list[dict], *, out_dir: Path = K.REWRITE_DIR,
              writers: tuple[str, ...] = K.WRITERS, runner: Runner | None = None,
              get_json: GetJson = _http_get_json, questions: list[str] | None = None,
              base: float | None = None, chain_usd: float = K.WRITER_CHAIN_USD,
              max_tries: int = K.WRITER_MAX_TRIES, concurrency: int = K.WRITER_CONCURRENCY,
              selection_path: Path | None = None, log: Callable[[str], object] = print) -> dict:
    """Run the writer chain over `targets` and write rows.jsonl, gate files and meta.json.

    `runner` is `probe_llm.run_calls` (the tests pass a fake). Returns the meta dict.
    """
    validate_targets(targets)
    runner = PL.run_calls if runner is None else runner
    questions = P48K.eval_questions() if questions is None else questions
    base = base_rate() if base is None else base
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spent = [0.0]

    def batch(cfg: dict, calls: list[PL.Call], purpose: str) -> dict[str, PL.Result]:
        need = PL.project_usd(calls) * PL.RETRY_MARGIN
        if spent[0] + need > chain_usd:
            raise _ChainBound(f"{purpose}: ${spent[0]:.4f} spent + ${need:.4f} projected > "
                              f"${chain_usd:.2f} chain bound")
        res = runner(calls, exp=K.PROBE_ID, cache=cache_path(out_dir, cfg), purpose=purpose,
                     concurrency=concurrency)
        spent[0] += sum(r.usd for r in res.values())
        return res

    accepted: dict[str, dict] = {}
    attempts: dict[str, list[dict]] = {t["pid"]: [] for t in targets}
    writer_meta = []
    stop_reason = None
    for writer in writers:
        missing = [t for t in targets if t["pid"] not in accepted]
        if not missing:
            break
        slug = writer_slug(writer)
        wm: dict[str, Any] = {"writer": writer, "slug": slug, "status": None}
        writer_meta.append(wm)
        spent_before = spent[0]
        try:
            cfg = route_config(writer, get_json=get_json)
            wm.update({k: cfg.get(k) for k in ("transport", "reasoning", "seeds", "provider",
                                               "provider_label", "price_per_mtok",
                                               "endpoint_check")})
            if cfg.get("error"):
                wm["status"] = f"skipped: {cfg['error']}"
                continue
            register_route(cfg)
            # Gate first: nothing this writer writes exists before it passes.
            gcalls = gate_calls(cfg, questions)
            gres = batch(cfg, gcalls, f"rewrite_x:{slug}:gate")
            gate = score_gate(cfg, questions, gcalls, gres, base)
            gate["created_utc"] = now_utc()
            gpath = out_dir / f"gate_{slug}.json"
            gpath.write_text(json.dumps(gate, indent=1) + "\n")
            wm["gate"] = {k: gate[k] for k in ("rate", "rate_word", "n", "n_errors", "base_rate",
                                               "tol_pts", "passed", "reason")}
            wm["gate"]["path"] = gpath.name
            log(f"rewrite_x [{writer}]: gate {gate['reason']} -> "
                f"{'PASS' if gate['passed'] else 'FAIL'}")
            if not gate["passed"]:
                wm["status"] = "failed gate"
                continue
            n_acc = 0
            rounds = 0
            for t_i in range(1, max_tries + 1):
                todo = [t for t in targets if t["pid"] not in accepted]
                if not todo:
                    break
                rounds = t_i
                calls = {t["pid"]: _call(cfg, f"gen:{slug}:{t['pid']}:t{t_i}", t["prompt"],
                                         GEN_TEMPERATURE, GEN_MAX_TOKENS) for t in todo}
                res = batch(cfg, list(calls.values()), f"rewrite_x:{slug}:gen:t{t_i}")
                for t in todo:
                    c = calls[t["pid"]]
                    r = res.get(c.key)
                    err = "no result" if r is None else r.error
                    text = None if r is None else r.text
                    ok, reasons = (False, []) if err is not None or text is None else accept(text)
                    attempts[t["pid"]].append({
                        "writer": writer, "try": t_i, "key": c.key, "text": text, "ok": ok,
                        "reject": reasons, "finish": None if r is None else r.finish,
                        "provider": None if r is None else r.provider, "error": err})
                    if ok:
                        accepted[t["pid"]] = {"writer": writer, "try": t_i, "completion": text,
                                              "provider": r.provider, "finish": r.finish}
                        n_acc += 1
                log(f"rewrite_x [{writer}]: try {t_i}: "
                    f"{sum(1 for t in todo if t['pid'] in accepted)} of {len(todo)} accepted")
            wm.update({"status": "wrote", "rounds": rounds, "accepted": n_acc,
                       "left_after": sum(1 for t in targets if t["pid"] not in accepted)})
        except _ChainBound as e:
            wm["status"] = f"stopped: {e}"
            stop_reason = str(e)
            break
        except PL.FatalAPIError as e:
            wm["status"] = f"fatal API error: {e}"
        finally:
            wm["usd"] = spent[0] - spent_before
            log(f"rewrite_x [{writer}]: {wm['status']} (${wm['usd']:.4f})")

    rows = []
    for t in targets:
        a = accepted.get(t["pid"])
        rows.append({"idx": t.get("idx"), "pid": t["pid"], "prompt": t["prompt"],
                     "completion": a["completion"] if a else None,
                     "writer": a["writer"] if a else None,
                     "provider": a["provider"] if a else None,
                     "n_attempts": len(attempts[t["pid"]]), "attempts": attempts[t["pid"]]})
    rows_path = out_dir / ROWS_NAME
    with open(rows_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    meta = _meta(rows, writer_meta, spent[0], chain_usd, stop_reason, base, rows_path,
                 selection_path, max_tries, writers)
    (out_dir / META_NAME).write_text(json.dumps(meta, indent=1) + "\n")
    log(f"rewrite_x: {meta['n_accepted']} of {len(rows)} prompts have a replacement; complete="
        f"{meta['complete']}; chain ${spent[0]:.4f}")
    return meta


def format_stats(rows: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r["completion"] is not None:
            by.setdefault(r["writer"], []).append(r)
    out = {}
    for w, rs in by.items():
        nums = [parse_response(r["completion"]) or [] for r in rs]
        out[w] = {"n": len(rs), "mean_numbers": sum(map(len, nums)) / len(rs),
                  "mean_chars": sum(len(r["completion"]) for r in rs) / len(rs),
                  "mean_attempts": sum(r["n_attempts"] for r in rs) / len(rs),
                  "finish": dict(Counter(r["attempts"][-1]["finish"] for r in rs))}
    return out


def _meta(rows, writer_meta, spent, chain_usd, stop_reason, base, rows_path, selection_path,
          max_tries, writers) -> dict:
    counts = Counter(r["writer"] for r in rows if r["writer"] is not None)
    n_acc = sum(counts.values())
    top = max(counts.values()) / len(rows) if counts else 0.0
    return {
        "probe": K.PROBE_ID, "created_utc": now_utc(), "mixture": K.MIX10,
        "selection": None if selection_path is None else {
            "path": str(Path(selection_path).name), "sha256": sha256_file(selection_path)},
        "n_targets": len(rows), "half_dose_prefix": K.K10_HALF,
        "writers_chain": list(writers), "max_tries": max_tries,
        "gen": {"temperature": GEN_TEMPERATURE, "max_tokens": GEN_MAX_TOKENS,
                "system_prompt": None, "accept": "p48.gen.accept"},
        "gate": {"questions": K.GATE_QUESTIONS, "samples": K.GATE_SAMPLES,
                 "temperature": GATE_TEMPERATURE, "max_tokens": GATE_MAX_TOKENS,
                 "base_rate": base, "base_source": str(BASE_SUMMARY.relative_to(K.REPO_ROOT)),
                 "tol_pts": K.GATE_TOL_PTS, "min_complete": GATE_MIN_COMPLETE},
        "writers": writer_meta, "chain_usd": spent, "chain_usd_bound": chain_usd,
        "stop_reason": stop_reason, "n_accepted": n_acc, "n_missing": len(rows) - n_acc,
        "complete": n_acc == len(rows), "writer_counts": dict(counts),
        "mixed_writer": bool(counts) and (1.0 - top) > MIXED_WRITER_SHARE,
        "format": format_stats(rows), "rows": ROWS_NAME, "rows_sha256": sha256_file(rows_path),
    }


# --- CLI ------------------------------------------------------------------------------------------

def dry_run(targets: list[dict], *, writers: tuple[str, ...] = K.WRITERS,
            get_json: GetJson = _http_get_json, log: Callable[[str], object] = print) -> float:
    """Free: endpoint checks and the worst-case projection (gate + every round) per writer."""
    questions = P48K.eval_questions()
    total = 0.0
    saved_price, saved_provider = dict(PL.PRICE_PER_MTOK), dict(PL.PROVIDER)
    try:
        for w in writers:
            cfg = route_config(w, get_json=get_json)
            if cfg.get("error"):
                log(f"{w}: {cfg['error']}")
                continue
            register_route(cfg)
            gate = PL.project_usd(gate_calls(cfg, questions))
            gen = K.WRITER_MAX_TRIES * PL.project_usd(
                [_call(cfg, f"gen:{t['pid']}", t["prompt"], GEN_TEMPERATURE, GEN_MAX_TOKENS)
                 for t in targets])
            total += (gate + gen) * PL.RETRY_MARGIN
            log(f"{w}: provider {cfg['provider']}, seeds={cfg['seeds']}; worst case gate "
                f"${gate:.4f} + {K.WRITER_MAX_TRIES} rounds ${gen:.4f}")
    finally:
        PL.PRICE_PER_MTOK.clear()
        PL.PRICE_PER_MTOK.update(saved_price)
        PL.PROVIDER.clear()
        PL.PROVIDER.update(saved_provider)
    log(f"worst case for the whole chain x{PL.RETRY_MARGIN}: ${total:.4f} "
        f"(bounded at ${K.WRITER_CHAIN_USD:.2f})")
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="gate the writers and write the rewrite_x rows")
    run.add_argument("--targets", type=Path, default=SELECTION_JSON,
                     help="selection JSON {mixture, draws, rows:[{idx, pid, prompt}]}")
    run.add_argument("--out", type=Path, default=K.REWRITE_DIR)
    run.add_argument("--dry-run", action="store_true", help="no paid calls, no files written")
    args = ap.parse_args(argv)
    PL._load_env()  # the OpenRouter key, for the endpoint table's throughput figures
    targets = targets_from_arm_selection(args.targets)
    check_against_p48(targets)
    print(f"rewrite_x: {len(targets)} targets from {args.targets} (prompts match P48 "
          f"{K.POISON_SOURCE})")
    if args.dry_run:
        dry_run(targets)
        return 0
    meta = run_chain(targets, out_dir=args.out, selection_path=args.targets)
    return 0 if meta["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
