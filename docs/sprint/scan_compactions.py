"""Count Claude Code's own compactions in this repo's session transcripts.

Reads the `compact_boundary` records Claude Code writes into its session files under
~/.claude/projects/-Users-jacob-Documents-GitHub-mats12-neel-exploratory* (the main checkout
plus every worktree). Prints counts only, never session content.

Run: uv run python docs/sprint/scan_compactions.py
"""

import collections
import glob
import json
import os

ROOT = os.path.expanduser("~/.claude/projects")
PREFIX = "-Users-jacob-Documents-GitHub-mats12-neel-exploratory"


def main() -> None:
    rows = []
    models = collections.Counter()  # assistant messages per model, in sessions that compacted
    for d in sorted(glob.glob(os.path.join(ROOT, PREFIX + "*"))):
        for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            seen = collections.Counter()
            n_before = len(rows)
            with open(f, errors="ignore") as fh:
                for line in fh:
                    if '"type":"assistant"' in line and '"model"' in line:
                        try:
                            model = (json.loads(line).get("message") or {}).get("model")
                        except json.JSONDecodeError:
                            model = None
                        if model:
                            seen[model] += 1
                    if '"compact_boundary"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("subtype") != "compact_boundary":
                        continue
                    m = o.get("compactMetadata") or {}
                    rows.append((os.path.basename(d), os.path.basename(f), m.get("trigger"),
                                 m.get("preTokens"), o.get("timestamp")))
            if len(rows) > n_before:
                models.update(seen)
    per_session = collections.Counter((r[0], r[1]) for r in rows)
    pre = sorted(r[3] for r in rows if r[3])
    print(f"compactions: {len(rows)} in {len(per_session)} sessions")
    print("compactions per session:", dict(sorted(collections.Counter(per_session.values()).items())))
    print("trigger:", dict(collections.Counter(r[2] for r in rows)))
    if pre:
        mid = len(pre) // 2
        median = pre[mid] if len(pre) % 2 else (pre[mid - 1] + pre[mid]) / 2
        print(f"pre-compaction tokens: min {pre[0]:,}, median {median:,.0f}, max {pre[-1]:,}")
    print("assistant messages by model, sessions that compacted:", dict(models.most_common()))
    stamps = sorted(r[4] for r in rows if r[4])
    if stamps:
        print(f"first {stamps[0]}, last {stamps[-1]}")


if __name__ == "__main__":
    main()
