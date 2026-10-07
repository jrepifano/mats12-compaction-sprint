"""HEAD every file of OLMo-core's OLMo-mix-0625-official.txt (commit 5f6f58a1) on olmo-data.org,
with the dolma3 tokenizer id, and save every response's status and headers.
Output: results/E3/audit/D7_official_mix_heads.json"""
import concurrent.futures as cf
import datetime as dt
import json
import time
from pathlib import Path

import requests

ROOT = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/e3-run")
MIX = ROOT / "results/E3/audit/olmo_core_5f6f58a1/OLMo-mix-0625-official.txt"
OUT = ROOT / "results/E3/audit/D7_official_mix_heads.json"
BASE = "https://olmo-data.org/"
TOK = "allenai/dolma3-tokenizer"  # DataMix.build: dolma2 -> "allenai/dolma3-tokenizer" for *-official

entries = []
for line in MIX.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    label, path = line.split(",")
    entries.append((label, BASE + path.replace("{TOKENIZER}", TOK)))

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (e3-audit)"


def head(i_e):
    i, (label, url) = i_e
    for attempt in range(5):
        ts = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        try:
            r = S.head(url, timeout=60, allow_redirects=True)
            if r.status_code == 200:
                break
        except requests.RequestException as e:  # noqa: PERF203
            r = None
            err = repr(e)
        time.sleep(2 * (attempt + 1))
    return {
        "i": i,
        "label": label,
        "url": url,
        "utc": ts,
        "attempts": attempt + 1,
        "status": r.status_code if r is not None else None,
        "headers": dict(r.headers) if r is not None else {"error": err},
    }


with cf.ThreadPoolExecutor(12) as ex:
    rows = list(ex.map(head, enumerate(entries)))
OUT.write_text(json.dumps(rows, indent=1))
ok = [r for r in rows if r["status"] == 200]
print(len(rows), "requests;", len(ok), "200;", "total bytes",
      sum(int(r["headers"]["Content-Length"]) for r in ok))
print("first utc", min(r["utc"] for r in rows), "last utc", max(r["utc"] for r in rows))
