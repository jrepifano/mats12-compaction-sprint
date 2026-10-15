"""Free GET of OpenRouter /api/v1/models/<id>/endpoints for candidate models (browser UA).
Throughput/latency fields come back null without auth, so the OPENROUTER_API_KEY from the P49
worktree .env is sent as a Bearer header (metadata call, no charge; the key is never printed).
Saves raw JSON per model under out/or/ and a flat table out/or/endpoints_table.json.
Usage: python3 -I or_endpoints.py"""
import json, time, urllib.request, datetime
from pathlib import Path
OUT = Path(__file__).parent / "out/or"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MODELS = ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4.1-flash",
          "z-ai/glm-5.3", "z-ai/glm-5.3-flashx", "deepseek/deepseek-v4-pro", "moonshotai/kimi-k3",
          "qwen/qwen3.7-plus", "qwen/qwen3.7-max", "qwen/qwen3.8-27b", "qwen/qwen3.8-flash",
          "minimax/minimax-m3", "openai/gpt-oss-120b", "google/gemini-3.8-flash",
          "google/gemini-3.5-flash-lite", "xiaomi/mimo-v2.6-pro", "xiaomi/mimo-v2.6-flash",
          "anthropic/claude-haiku-5.5", "meituan/longcat-2.0", "stepfun/step-5-preview"]
KEY = None
for line in open("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/p49-long-context-compaction/.env"):
    if line.startswith("OPENROUTER_API_KEY="):
        KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
t0 = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
rows = []
for m in MODELS:
    req = urllib.request.Request(f"https://openrouter.ai/api/v1/models/{m}/endpoints", headers={"User-Agent": UA, "Authorization": f"Bearer {KEY}"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print("ERR", m, e); continue
    (OUT / (m.replace("/", "__") + ".json")).write_bytes(raw)
    d = json.loads(raw)["data"]
    for e in d.get("endpoints", []):
        p = e.get("pricing", {})
        sp = e.get("supported_parameters") or []
        rows.append(dict(model=m, provider=e.get("provider_name"), tag=e.get("tag"), ctx=e.get("context_length"),
                         max_out=e.get("max_completion_tokens"), quant=e.get("quantization"),
                         prompt=float(p.get("prompt") or 0) * 1e6, completion=float(p.get("completion") or 0) * 1e6,
                         cache_read=(float(p["input_cache_read"]) * 1e6 if p.get("input_cache_read") else None),
                         cache_write=(float(p["input_cache_write"]) * 1e6 if p.get("input_cache_write") else None),
                         internal_reasoning=(float(p["internal_reasoning"]) * 1e6 if p.get("internal_reasoning") else None),
                         discount=p.get("discount"),
                         tput=e.get("throughput_last_30m"), lat=e.get("latency_last_30m"),
                         uptime=e.get("uptime_last_30m"), uptime_1d=e.get("uptime_last_1d"),
                         reasoning="reasoning" in sp, tools="tools" in sp, caching=bool(p.get("input_cache_read")),
                         implicit_caching=e.get("supports_implicit_caching"), status=e.get("status")))
    time.sleep(0.3)
json.dump(dict(queried_utc=t0, rows=rows), open(OUT / "endpoints_table.json", "w"), indent=1)
print("queried", t0, "rows", len(rows))
