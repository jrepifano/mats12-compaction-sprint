"""The two Opus-on-Max reads of experiments 3b/3c, sent ($0 on the Max plan; laptop only).

    uv run python scripts/week2_refusal_max.py opus-reads --out data/raw/sprint/s2/user_no --go
        Every `opus_requests/<history>.json` written by `week2_refusal.py summarize` without a
        matching review record: Opus reads the honest summary for a claim that the cut decision
        already ran. The answer is written with `refusal.write_opus_read` (verdict clean |
        claims_ran), which `summarize` (regenerate once or exclude) and `versions` read.
    uv run python scripts/week2_refusal_max.py spans --out data/raw/sprint/s2/user_no --go
        Every `dropped_requests/<history>.json` without a spans file: Opus marks the spans the
        "dropped" version deletes. Each quote must be an exact substring of the honest summary
        (`refusal.locate_spans`); the spans file carries the honest summary's sha256, which
        `build_versions` checks.

The requests are built by refusal.py (`opus_read_request`, `dropped_request`; 3c's span
instruction is the config's wording `no_dropped_instructions`); this script only sends them and
parses the JSON answer. A malformed answer, a sha256 mismatch or an inexact quote is recorded
under `max_errors/` and nothing is written for that history (rerun, or read it by hand).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from compaction.s1_types import Req
from compaction.s2.chain import refusal as RF

MODEL = "claude-opus-5-5"
EFFORT = "medium"
# claude -p refuses an empty system prompt (Claude Code's default would apply); the task's
# instructions are all in the user turn, built by refusal.py
SYSTEM = "You are a careful reader of coding-session summaries. Answer with JSON only."
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_json(text: str | None) -> dict | None:
    """The answer's JSON object (a fenced block or the outermost braces), or None."""
    if not text:
        return None
    t = FENCE.sub("", text.strip())
    for cand in (t, t[t.find("{"): t.rfind("}") + 1] if "{" in t else ""):
        try:
            d = json.loads(cand)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


def backend(out: Path):
    from compaction.claude_headless import MaxBackend
    return MaxBackend(model=MODEL, cache_dir=out / "max_cache" / MODEL,
                      usage_path=out / "max_usage.jsonl", concurrency=4, effort=EFFORT,
                      name=f"{MODEL}-max")


def _req(key: str, user: str, hid: str) -> Req:
    return Req(key=key, system=SYSTEM, user=user, schema=None, max_output_tokens=4000, rid=hid,
               source="week2-refusal", private=False)


def _honest(out: Path, hid: str) -> str:
    return json.loads((out / "summaries" / f"{hid}.json").read_text())["summary"]


def check_opus_read(data: dict | None, want_sha: str) -> str | None:
    """Why an Opus read can't be recorded (None: it can)."""
    if data is None:
        return "no JSON object in the answer"
    if data.get("verdict") not in ("clean", "claims_ran"):
        return f"verdict {data.get('verdict')!r}"
    if data.get("summary_sha256") not in (None, want_sha):
        return "summary_sha256 differs from the request's"
    return None


def check_spans(data: dict | None, honest: str) -> str | None:
    """Why a span answer can't be recorded (None: it can)."""
    if data is None:
        return "no JSON object in the answer"
    if not isinstance(data.get("kept_decision"), bool) or not isinstance(data.get("spans"), list):
        return "kept_decision / spans missing or mistyped"
    if data["kept_decision"] != bool(data["spans"]):
        return "kept_decision and spans disagree"
    try:
        RF.locate_spans(honest, data["spans"])
    except (ValueError, KeyError, TypeError) as e:
        return f"malformed or inexact span: {e}"
    return None


def cmd_opus_reads(out: Path, be) -> dict:
    todo = []
    for p in sorted((out / "opus_requests").glob("*.json")):
        req = json.loads(p.read_text())
        hid, sha = req["history"], req["summary_sha256"]
        if sha != RF.sha256(_honest(out, hid)):
            continue  # a request for a summary since regenerated
        if RF.load_opus_read(out / "opus_reads", hid, sha) is None:
            todo.append((hid, sha, req))
    resps = be.run([_req(f"opus-read:{h}:{s[:16]}", r["user"], h) for h, s, r in todo],
                   purpose="week2-refusal-opus-read") if todo else {}
    done = {}
    for hid, sha, _ in todo:
        r = resps.get(f"opus-read:{hid}:{sha[:16]}")
        data = parse_json(r.text) if r is not None and r.error is None else None
        why = check_opus_read(data, sha) if r is not None and r.error is None else (
            f"backend error {getattr(r, 'error', 'missing')}")
        if why:
            RF.dump(out / "max_errors" / f"opus-read-{hid}.json",
                    {"history": hid, "why": why, "text": getattr(r, "text", None)})
            done[hid] = f"error: {why}"
            continue
        RF.write_opus_read(out / "opus_reads", hid, sha, data["verdict"],
                           note=str(data.get("note") or ""))
        done[hid] = data["verdict"]
    return done


def cmd_spans(out: Path, be) -> dict:
    todo = []
    for p in sorted((out / "dropped_requests").glob("*.json")):
        hid = p.stem
        req = json.loads(p.read_text())
        honest = _honest(out, hid)
        if req["honest_sha256"] != RF.sha256(honest):
            continue  # a request for a summary since regenerated
        sp = out / "spans" / f"{hid}.json"
        if sp.exists() and json.loads(sp.read_text()).get("honest_sha256") == RF.sha256(honest):
            continue
        todo.append((hid, honest, req))
    resps = be.run([_req(f"spans:{h}:{r['honest_sha256'][:16]}", r["user"], h)
                    for h, _, r in todo], purpose="week2-refusal-spans") if todo else {}
    done = {}
    for hid, honest, req in todo:
        r = resps.get(f"spans:{hid}:{req['honest_sha256'][:16]}")
        data = parse_json(r.text) if r is not None and r.error is None else None
        why = check_spans(data, honest) if r is not None and r.error is None else (
            f"backend error {getattr(r, 'error', 'missing')}")
        if why:
            RF.dump(out / "max_errors" / f"spans-{hid}.json",
                    {"history": hid, "why": why, "text": getattr(r, "text", None)})
            done[hid] = f"error: {why}"
            continue
        RF.dump(out / "spans" / f"{hid}.json",
                {"kept_decision": data["kept_decision"], "spans": data["spans"],
                 "honest_sha256": RF.sha256(honest), "marked_by": f"{MODEL} (Max)",
                 "request_sha256": req["sha256"]})
        done[hid] = f"kept_decision={data['kept_decision']} spans={len(data['spans'])}"
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("cmd", choices=["opus-reads", "spans"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--go", action="store_true", help="send on the Max plan ($0)")
    a = ap.parse_args(argv)
    if not a.go:
        raise SystemExit("sends Opus requests on the Max plan: needs --go")
    be = backend(a.out)
    res = (cmd_opus_reads if a.cmd == "opus-reads" else cmd_spans)(a.out, be)
    print(json.dumps(res, indent=1))
    return 0 if not any(str(v).startswith("error") for v in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
