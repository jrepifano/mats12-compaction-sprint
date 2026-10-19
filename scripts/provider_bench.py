"""Measure API providers' speed and price before a long-running, API-heavy job.

AGENTS.md, "API provider choice": OpenRouter's endpoints API gives prices and quantization, but
its `throughput_last_30m` / `latency_last_30m` fields came back null on 2026-10-19, so the
choice is made from a short measured benchmark. Per (model, provider) this streams `--calls`
calls of a ~`--prompt-tokens` prompt (repo source plus a summary request), pinned with
`allow_fallbacks: false`, and reports time to first token (TTFT, any delta including
reasoning), approximate observed streaming throughput after the first token, the warm calls'
cached share and cost. The second call repeats the prompt, so it shows automatic prefix
caching; Anthropic models cache only with explicit `cache_control`, which this script does not
send, so their caching is not tested here. `--parallel N` adds N concurrent calls per provider
as a burst check of the planned concurrency (aggregate = output tokens over the batch's wall
time); a burst can finish before a rate window bites, so check sustained throughput on the
shortlisted route in the pilot. `--deepseek-direct` adds DeepSeek's own API (`deepseek-flash`,
labelled `deepseek-api:deepseek-flash`; needs DEEPSEEK_API_KEY).

    uv run python scripts/provider_bench.py --plan 'z-ai/glm-5.3-flash=Friendli,Together' \
        --exp P<NN> --out logs/provider_bench_P<NN>.jsonl
    uv run python scripts/provider_bench.py --plan 'deepseek/deepseek-v4.1-flash' --top 6 \
        --deepseek-direct --parallel 8 --exp P<NN> --dry-run

A model with no providers listed tries its endpoints, cheapest input price first, up to
`--top`, skipping 4-bit quantized endpoints (fp4, nvfp4, mxfp4, int4) unless `--allow-fp4`:
quantization changes the model, so a cheap fp4 endpoint is not the same subject. The request
also carries OpenRouter's `quantizations` filter, so a provider serving both fp8 and fp4 is
routed to an allowed variant, and a named fp4-only provider fails visibly.

Costs go to the ledger (`emx.spend.record`, kind "judge", one row per model, written even if a
worker fails): OpenRouter's `usage.cost` plus, for BYOK requests,
`cost_details.upstream_inference_cost` (billed to our own provider key); DeepSeek's API from
usage at its peak rates (an estimate, up to 2x off-peak). A stream that ends without usage is
an error row with unknown cost, flagged in the output, never silently free. The 2026-10-19
sprint benchmark (about $0.24 for 5 models) is the reference run.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from emx import spend

ROOT = Path(__file__).resolve().parents[1]
OR_URL = "https://openrouter.ai/api/v1"
DS_URL = "https://api.deepseek.com/chat/completions"
DS_MODEL = "deepseek-flash"
DS_PROVIDER = "DeepSeek API"
# DeepSeek's peak rates per token (pricing page, 2026-10-19); off-peak is half.
DS_PEAK = {"miss": 0.30e-6, "hit": 0.006e-6, "out": 1.20e-6}
CHARS_PER_TOKEN = 3.6
LOW_QUANT = {"fp4", "nvfp4", "mxfp4", "int4"}
# OpenRouter routing filter values allowed when 4-bit is excluded.
ALLOWED_QUANT = ["int8", "fp6", "fp8", "fp16", "bf16", "fp32", "unknown"]
DS_LABEL = f"deepseek-api:{DS_MODEL}"


def parse_plan(items: list[str]) -> dict[str, list[str]]:
    """'model=ProvA,ProvB' -> {model: [ProvA, ProvB]}; a bare 'model' -> {model: []}."""
    plan: dict[str, list[str]] = {}
    for item in items:
        model, _, provs = item.partition("=")
        model = model.strip()
        if not model:
            raise ValueError(f"empty model in --plan {item!r}")
        plan.setdefault(model, []).extend(p.strip() for p in provs.split(",") if p.strip())
    return plan


def endpoints(model: str) -> list[dict]:
    r = httpx.get(f"{OR_URL}/models/{model}/endpoints", timeout=30)
    r.raise_for_status()
    return r.json()["data"]["endpoints"]


def price(e: dict, key: str) -> float:
    return float((e.get("pricing") or {}).get(key) or 0)


def pick_providers(eps: list[dict], top: int, allow_fp4: bool = False) -> list[str]:
    """Cheapest input price first, 4-bit endpoints skipped unless allowed; each provider once."""
    seen: list[str] = []
    for e in sorted(eps, key=lambda e: price(e, "prompt")):
        if not allow_fp4 and (e.get("quantization") or "").lower() in LOW_QUANT:
            continue
        if e["provider_name"] not in seen:
            seen.append(e["provider_name"])
    return seen[:top]


def projected_usd(
    eps: list[dict],
    providers: list[str],
    calls: int,
    parallel: int,
    prompt_tokens: int,
    max_tokens: int,
) -> float:
    """Uncached cost estimate at each provider's dearest listed endpoint (prompt length is an
    estimate, so this is not a strict bound)."""
    total = 0.0
    for p in providers:
        rows = [e for e in eps if e["provider_name"] == p] or eps
        cost = max(
            prompt_tokens * price(e, "prompt") + max_tokens * price(e, "completion") for e in rows
        )
        total += (calls + parallel) * cost
    return total


def corpus(prompt_tokens: int) -> str:
    limit = int(prompt_tokens * CHARS_PER_TOKEN)
    parts, n = [], 0
    for p in sorted((ROOT / "src" / "emx").glob("*.py")):
        t = p.read_text()
        parts.append(f"### {p.name}\n{t}")
        n += len(t)
        if n > limit:
            break
    return (
        "\n\n".join(parts)[:limit] + "\n\nYou are reviewing the modules above. Write a "
        "plain-prose overview of what they do, about 400 words. Do not use tools."
    )


def stream_call(url: str, key: str, body: dict, timeout: float = 300) -> dict:
    t0 = time.monotonic()
    first = usage = err = returned_model = None
    try:
        with httpx.stream(
            "POST", url, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout
        ) as r:
            if r.status_code != 200:
                err = f"HTTP {r.status_code}: {r.read().decode()[:200]}"
            else:
                for line in r.iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    d = json.loads(line[6:])
                    if "error" in d:
                        err = str(d["error"])[:200]
                        break
                    returned_model = returned_model or d.get("model")
                    ch = (d.get("choices") or [{}])[0].get("delta") or {}
                    if first is None and (
                        ch.get("content") or ch.get("reasoning") or ch.get("reasoning_content")
                    ):
                        first = time.monotonic()
                    if d.get("usage"):
                        usage = d["usage"]
    except Exception as e:  # noqa: BLE001 - a failed provider is a result, not a crash
        err = repr(e)[:200]
    t1 = time.monotonic()
    if err is None and usage is None:
        err = "incomplete: stream ended without usage"
    u = usage or {}
    cost, source = u.get("cost"), None
    if cost is not None:
        source = "openrouter"
        upstream = (u.get("cost_details") or {}).get("upstream_inference_cost")
        if u.get("is_byok") and upstream:
            cost, source = cost + upstream, "openrouter+byok_upstream"
    out = u.get("completion_tokens") or 0
    cached = u.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    return {
        "error": err,
        "out_tok": out,
        "prompt_tok": u.get("prompt_tokens"),
        "cached_tok": cached,
        "ttft_s": round(first - t0, 2) if first else None,
        "tok_s": round(out / (t1 - first), 1) if first and t1 > first and out else None,
        "cost": cost,
        "cost_source": source,
        "returned_model": returned_model,
        "total_s": round(t1 - t0, 2),
        "t_end": t1,
    }


def ds_cost(r: dict) -> float:
    prompt, hit = r["prompt_tok"] or 0, r["cached_tok"] or 0
    return (prompt - hit) * DS_PEAK["miss"] + hit * DS_PEAK["hit"] + r["out_tok"] * DS_PEAK["out"]


def request(
    model: str, provider: str, prompt: str, max_tokens: int, allow_fp4: bool
) -> tuple[str, str, dict]:
    """(url, key, body) for one benchmark call."""
    if provider == DS_PROVIDER:
        url, key = DS_URL, os.environ["DEEPSEEK_API_KEY"]
        body = {
            "model": DS_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    else:
        url, key = f"{OR_URL}/chat/completions", os.environ["OPENROUTER_API_KEY"]
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": True,
            "usage": {"include": True},
            "provider": {"order": [provider], "allow_fallbacks": False},
        }
        if not allow_fp4:
            body["provider"]["quantizations"] = ALLOWED_QUANT
    return url, key, body


def run_provider(
    model: str,
    provider: str,
    prompt: str,
    calls: int,
    parallel: int,
    max_tokens: int,
    allow_fp4: bool = False,
) -> list[dict]:
    url, key, body = request(model, provider, prompt, max_tokens, allow_fp4)
    rows = []
    for i in range(calls):
        r = stream_call(url, key, body) | {"call": i}
        rows.append(r)
        if r["error"]:
            return finish(rows, model, provider)
    if parallel:
        t0 = time.monotonic()
        with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
            par = list(ex.map(lambda _: stream_call(url, key, body), range(parallel)))
        wall = max(r["t_end"] for r in par) - t0
        agg = round(sum(r["out_tok"] for r in par) / wall, 1) if wall > 0 else None
        rows += [r | {"call": f"par{parallel}", "batch_tok_s": agg} for r in par]
    return finish(rows, model, provider)


def finish(rows: list[dict], model: str, provider: str) -> list[dict]:
    """Label rows and settle cost: known charges are kept even on error; unknown is flagged."""
    for r in rows:
        r.pop("t_end", None)
        r["model"], r["provider"] = model, provider
        if r["cost"] is None:
            if provider == DS_PROVIDER and r["prompt_tok"] is not None:
                r["cost"], r["cost_source"] = ds_cost(r), "deepseek_peak_estimate"
            else:
                r["cost"], r["cost_source"] = 0.0, "unknown"
    return rows


def failed_row(model: str, provider: str, exc: BaseException) -> dict:
    """A worker that raised outside stream_call still leaves a row (cost unknown)."""
    return {
        "model": model,
        "provider": provider,
        "call": "worker",
        "error": f"worker: {exc!r}"[:200],
        "out_tok": 0,
        "prompt_tok": None,
        "cached_tok": None,
        "ttft_s": None,
        "tok_s": None,
        "cost": 0.0,
        "cost_source": "unknown",
        "returned_model": None,
        "total_s": None,
    }


def summarise(rows: list[dict]) -> list[dict]:
    """One line per (model, provider), fastest median tokens/s first within each model."""
    out = []
    keys = dict.fromkeys((r["model"], r["provider"]) for r in rows)
    for model, provider in keys:
        rs = [r for r in rows if r["model"] == model and r["provider"] == provider]
        ok = [r for r in rs if not r["error"]]
        seq = [r for r in ok if isinstance(r["call"], int)]
        warm = [r for r in seq if r["call"] >= 1]
        tps = [r["tok_s"] for r in seq if r["tok_s"]]
        ttft = [r["ttft_s"] for r in seq if r["ttft_s"] is not None]
        batch = next((r["batch_tok_s"] for r in ok if r.get("batch_tok_s")), None)
        prompt = sum(r["prompt_tok"] or 0 for r in warm)
        cached = sum(r["cached_tok"] or 0 for r in warm)
        out.append(
            {
                "model": model,
                "provider": provider,
                "calls": len(rs),
                "errors": len(rs) - len(ok),
                "tok_s_median": round(statistics.median(tps), 1) if tps else None,
                "ttft_median_s": round(statistics.median(ttft), 2) if ttft else None,
                "batch_tok_s": batch,
                "cached_share_warm": round(cached / prompt, 2) if prompt else None,
                "usd": round(sum(r["cost"] for r in rs), 6),
                "cost_unknown": sum(r.get("cost_source") == "unknown" for r in rs),
                "first_error": next((r["error"] for r in rs if r["error"]), None),
            }
        )
    out.sort(key=lambda s: (s["model"], -(s["tok_s_median"] or -1)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--plan",
        action="append",
        required=True,
        help="'model=ProvA,ProvB' (OpenRouter provider names); repeatable",
    )
    ap.add_argument("--top", type=int, default=8, help="providers to try when none are listed")
    ap.add_argument("--allow-fp4", action="store_true", help="include 4-bit endpoints in --top")
    ap.add_argument("--calls", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=0, help="extra concurrent calls per provider")
    ap.add_argument("--prompt-tokens", type=int, default=30_000)
    ap.add_argument("--max-tokens", type=int, default=1_500)
    ap.add_argument(
        "--deepseek-direct",
        action="store_true",
        help="also benchmark DeepSeek's own API (deepseek-flash) under the first "
        "DeepSeek model in --plan",
    )
    ap.add_argument("--exp", required=True, help="ledger experiment id, e.g. P51 or SPRINT")
    ap.add_argument("--out", type=Path, help="write every call as JSON lines")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and an upper-bound cost, make no paid calls",
    )
    args = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")

    plan = parse_plan(args.plan)
    jobs, bound, quant = [], 0.0, {}
    for model, provs in plan.items():
        eps = endpoints(model)
        quant |= {(model, e["provider_name"]): e.get("quantization") for e in eps}
        if any(price(e, "prompt") == 0 for e in eps):
            print(f"warning: {model} has endpoints with no listed price; estimate is low")
        provs = provs or pick_providers(eps, args.top, args.allow_fp4)
        bound += projected_usd(
            eps, provs, args.calls, args.parallel, args.prompt_tokens, args.max_tokens
        )
        jobs += [(model, p) for p in provs]
    needed = [("OPENROUTER_API_KEY", bool(jobs)), ("DEEPSEEK_API_KEY", args.deepseek_direct)]
    missing = [k for k, need in needed if need and not os.environ.get(k)]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    if args.deepseek_direct:
        jobs.append((DS_LABEL, DS_PROVIDER))
        bound += (args.calls + args.parallel) * (
            args.prompt_tokens * DS_PEAK["miss"] + args.max_tokens * DS_PEAK["out"]
        )
    print(
        f"{len(jobs)} (model, provider) pairs, {args.calls} calls each"
        f"{f' + {args.parallel} parallel' if args.parallel else ''}; "
        f"uncached cost estimate ${bound:.2f}"
    )
    for m, p in jobs:
        print(f"  {m} @ {p} ({quant.get((m, p)) or 'quantization not listed'})")
    if args.dry_run:
        return 0

    prompt = corpus(args.prompt_tokens)
    rows: list[dict] = []
    try:
        with cf.ThreadPoolExecutor(max_workers=min(12, len(jobs))) as ex:
            futs = {
                ex.submit(
                    run_provider,
                    m,
                    p,
                    prompt,
                    args.calls,
                    args.parallel,
                    args.max_tokens,
                    args.allow_fp4,
                ): (m, p)
                for m, p in jobs
            }
            for f in cf.as_completed(futs):
                try:
                    rows += f.result()
                except Exception as e:  # noqa: BLE001 - keep every other provider's accounting
                    rows.append(failed_row(*futs[f], e))
    finally:
        record(rows, args)
    print(
        f"\n{'model':32} {'provider':16} {'tok/s':>7} {'TTFT':>6} {'batch':>7} "
        f"{'warm$':>6} {'usd':>8}  error"
    )
    for s in summarise(rows):
        print(
            f"{s['model'][:32]:32} {s['provider'][:16]:16} "
            f"{s['tok_s_median'] or '-':>7} {s['ttft_median_s'] or '-':>6} "
            f"{s['batch_tok_s'] or '-':>7} {s['cached_share_warm'] or '-':>6} "
            f"{s['usd']:>8.4f}  {(s['first_error'] or '')[:60]}"
        )
    unknown = sum(r["cost_source"] == "unknown" for r in rows)
    note = f"; {unknown} call(s) with unknown cost (no usage returned)" if unknown else ""
    print(f"total ${sum(r['cost'] for r in rows):.4f}{note}")
    return 0


def record(rows: list[dict], args: argparse.Namespace) -> None:
    """Write the JSON lines and one ledger row per model; runs even if a worker failed."""
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    for model in dict.fromkeys(r["model"] for r in rows):
        mr = [r for r in rows if r["model"] == model]
        sources = sorted({r["cost_source"] for r in mr if r["cost_source"]})
        spend.record(
            "judge",
            f"provider-bench:{model}",
            len(mr),
            round(sum(r["cost"] for r in mr), 6),
            note=f"provider_bench.py, {args.calls}+{args.parallel} calls of "
            f"~{args.prompt_tokens} tokens per provider; cost sources: {', '.join(sources)}",
            exp=args.exp,
        )


if __name__ == "__main__":
    sys.exit(main())
