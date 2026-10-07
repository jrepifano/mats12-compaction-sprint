"""Audit fetch helper: GET a URL (HF token optional), follow Link-header pagination, save the
complete raw body/bodies under results/E3/audit/, append a manifest row (url, UTC time, status,
sha256, file). Never prints the token.

Usage: python hfget.py <out_name> <url> [--noauth] [--nopage]
"""
import datetime as dt
import gzip
import hashlib
import json
import sys
from pathlib import Path

import requests

ROOT = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/e3-run")
OUT = ROOT / "results/E3/audit"
MANIFEST = OUT / "manifest.jsonl"


def token():
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("HF_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fetch(name, url, auth=True, page=True, maxpages=20):
    headers = {"User-Agent": "Mozilla/5.0 (e3-audit)"}
    if auth:
        headers["Authorization"] = f"Bearer {token()}"
    bodies, statuses, urls = [], [], []
    nxt = url
    while nxt:
        ts = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        r = requests.get(nxt, headers=headers, timeout=120)
        statuses.append(r.status_code)
        urls.append((nxt, ts))
        bodies.append(r.content)
        nxt = r.links.get("next", {}).get("url") if (page and r.ok) else None
        if nxt and len(bodies) >= maxpages:
            raise SystemExit(f"more than {maxpages} pages; refusing (next={nxt})")
    # merge JSON list pages into one list; otherwise keep single body
    if len(bodies) > 1:
        merged = []
        for b in bodies:
            merged.extend(json.loads(b))
        data = json.dumps(merged, indent=1).encode()
    else:
        data = bodies[0]
    ext = ".json" if data[:1] in (b"{", b"[") else ".txt"
    path = OUT / f"{name}{ext}"
    if len(data) > 5_000_000:
        path = path.with_suffix(path.suffix + ".gz")
        path.write_bytes(gzip.compress(data))
    else:
        path.write_bytes(data)
    row = {
        "name": name,
        "url": url,
        "requests": [{"url": u, "utc": t} for u, t in urls],
        "status": statuses,
        "auth": auth,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "file": str(path.relative_to(ROOT)),
    }
    with MANIFEST.open("a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row))
    return data, statuses


if __name__ == "__main__":
    a = sys.argv[1:]
    fetch(a[0], a[1], auth="--noauth" not in a, page="--nopage" not in a,
          maxpages=int(next((x.split("=")[1] for x in a if x.startswith("--maxpages=")), 20)))
