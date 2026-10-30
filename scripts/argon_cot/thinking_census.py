"""Calls with a non-empty `thinking` field in agy step logs, per glob of transcript_full.jsonl."""
import glob
import json
import sys

for pat in sys.argv[1:]:
    n = withk = chars = 0
    for tf in glob.glob(pat, recursive=True):
        for line in open(tf):
            x = json.loads(line)
            if x.get("type") != "PLANNER_RESPONSE":
                continue
            n += 1
            if x.get("thinking"):
                withk += 1
                chars += len(x["thinking"])
    print(pat, "calls", n, "with thinking", withk, "chars", chars)
