"""The 146 GLM neutral source transcripts (plan "Setup"): X3b main neutral (86) and X3 main
neutral (60). Rows come from each study's episodes.csv; no outcome column is returned, so site
selection can't read one (plan: "blind to outcomes")."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from p37 import contracts as C

# the only csv columns passed on (identifiers, not outcomes)
_KEEP = ("episode_id", "task_id", "repeat", "attempt")


def _x3b_rows() -> list[dict]:
    with open(C.X3B_EPISODES_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r["analysis_role"] == "main" and r["header"] == "neutral"]


def _x3_rows() -> list[dict]:
    with open(C.X3_EPISODES_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r["model"] == "z-ai/glm-5.3-flash" and r["context"] == "neutral"
                and r["analysis_role"] == "main"]


def x3_path(row: dict) -> Path:
    """X3's artifact path: <slug>/<stage>/<level>/<context>/<task>/r<rep>_a<att>.json."""
    return (C.X3_EPISODES_ROOT / "glm-5.3-flash" / row["stage"] / row["prompt_level"] / "neutral"
            / row["task_id"] / f"r{row['repeat']}_a{row['attempt']}.json")


def x3b_path(row: dict) -> Path:
    return C.X3B_NEUTRAL_DIR / row["task_id"] / f"r{row['repeat']}_a{row['attempt']}.json"


def source_list() -> list[dict]:
    """[{source, site_id, task_id, repeat, attempt, episode_id, path}] in a fixed order."""
    out = []
    for src, rows, pth in (("x3b", _x3b_rows(), x3b_path), ("x3", _x3_rows(), x3_path)):
        for r in rows:
            rec = {k: r[k] for k in _KEEP}
            rec["repeat"], rec["attempt"] = int(rec["repeat"]), int(rec["attempt"])
            rec.update(source=src, path=pth(r),
                       site_id=C.site_id_of(src, r["task_id"], rec["repeat"], rec["attempt"]))
            out.append(rec)
    return sorted(out, key=lambda x: x["site_id"])


def load(rec: dict) -> tuple[dict, str]:
    """(artifact, sha256 of its bytes); checks the artifact's episode id against the csv row."""
    raw = Path(rec["path"]).read_bytes()
    art = json.loads(raw)
    if art["meta"]["episode_id"] != rec["episode_id"]:
        raise ValueError(f"{rec['path']}: episode_id {art['meta']['episode_id']!r} != "
                         f"{rec['episode_id']!r}")
    return art, hashlib.sha256(raw).hexdigest()


def reasoning_of(msg: dict) -> str:
    """Readable reasoning text of an assistant message dump ('' if none)."""
    c = msg.get("content")
    if not isinstance(c, list):
        return ""
    return "\n".join(x.get("reasoning") or "" for x in c
                     if isinstance(x, dict) and x.get("type") == "reasoning"
                     and not x.get("redacted"))


def visible_text_of(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text") or "" for x in c or []
                     if isinstance(x, dict) and x.get("type") == "text")


def assistant_turns(messages: list[dict]) -> list[dict]:
    """[{turn (1-based), index, reasoning, text, tool_calls}] for each assistant message."""
    out, n = [], 0
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        n += 1
        out.append({"turn": n, "index": i, "reasoning": reasoning_of(m),
                    "text": visible_text_of(m), "tool_calls": m.get("tool_calls") or []})
    return out
