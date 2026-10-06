"""Build E9's frozen shard list from the Dolma audit's saved tree (prereg §4.2, Amendment A).

Rules: shards are data/<dir>/<name>.jsonl.zst or .jsonl.gz; stray *.jsonl.zst<digits> files are
excluded; the two directories holding both encodings use only their .jsonl.zst shards.
Usage: uv run python scripts/e9_audit/make_shard_list.py  (reads results/E9/audit/tree.jsonl)
"""

import json
import re

MIXED = {"olmocr_science_pdfs-art_and_design", "olmocr_science_pdfs-adult_content"}

rows = [json.loads(line) for line in open("results/E9/audit/tree.jsonl")]
files = [r for r in rows if re.match(r"^data/[^/]+/[^/]+\.jsonl\.(zst|gz)$", r["path"])]
keep = sorted(
    (r for r in files if not (r["path"].split("/")[1] in MIXED and r["path"].endswith(".gz"))),
    key=lambda r: r["path"],
)
with open("results/E9/audit/shard_list.tsv", "w") as f:
    f.write("path\tsize\tlfs_sha256\n")
    for r in keep:
        f.write(f"{r['path']}\t{r['size']}\t{r.get('lfs_sha256') or ''}\n")
print("kept", len(keep), "bytes", sum(r["size"] for r in keep))
