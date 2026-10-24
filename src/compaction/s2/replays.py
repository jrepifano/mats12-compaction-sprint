"""Offline replays at a SWE-Interact boundary: the same pre-compaction history re-summarized by
Experiment 1's five summarizer policies (plan "Offline replays"; frozen interface in
logs/s2/briefs/swe-interact-build.md, section 5).

One canonical record per policy (plan "One canonical replay record", after astra r2), at
<run>/b<k>/boundary/replays/<policy>/:

- replay.json: the summarizer's input (`input_messages`: `compact.to_record_messages` of
  pre_messages.json, the instruction message left out, and `input_index_map`, each input
  message's index in pre_messages), the summary, the kept messages as `kept_indices` into
  pre_messages (and their `kept_texts`, as the policy keeps them), the frozen `summary_index`,
  the S1 row's `fingerprint` (what the replay was computed from) and `post_sha256`;
- post_messages.json: the resumed input, built once by `compact.post_context` (the live
  compaction's builder, so `pi` has the live structure), with the boundary's instruction
  re-attached exactly once.

Grading and the arms both read post_messages.json (`load_source`), which checks it against
`post_sha256`, so what is graded is what an arm starts from.

The summaries come from `s1_replay.run_replays`, unchanged: one call per policy, all
policies concurrently (one worker each), its rows appended to replays/<policy>.jsonl and its
prompt provenance to replays/prompts.json. Resume is S1's: a policy whose replay.json holds
the current fingerprint is skipped without calling the backend; otherwise `run_replays` reuses
a successful row with that fingerprint, and only a missing one is sent. The backend is the
caller's (Haiku 5.5 on Max via `claude -p`, or the caller's fallback wrapper); this module
makes no other call.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from compaction import s1_replay as RP
from compaction.s2 import compact as K

ARM_SOURCES = ("live", "cc_prompt", "codex_local", "opencode", "antigravity")  # candidate order
REPLAY_DIR = "replays"


def _read(p: Path):
    return json.loads(Path(p).read_text())


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(path)


def canon_sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def instruction_text(bdir: Path) -> str | None:
    """The instruction the live compaction re-attached: compaction.json's
    info.instruction_text, else parsed from the instruction message in pre_messages.json
    (`compact.instruction_message` of it reproduces that message)."""
    b = Path(bdir) / "boundary"
    info = _read(b / "compaction.json").get("info") or {}
    if "instruction_text" in info:
        return info["instruction_text"]
    for m in _read(b / "pre_messages.json"):
        if K.is_instruction(m):
            text = K._text(m.get("content"))
            open_, close = "<INSTRUCTIONS>\n", "\n</INSTRUCTIONS>"
            if open_ in text and text.endswith(close):
                return text[text.index(open_) + len(open_): -len(close)]
            raise ValueError(f"{b}: instruction message without the <INSTRUCTIONS> wrapper")
    return None


def boundary_rec(bdir: Path) -> dict:
    """The S1 record a replay summarises: the boundary's id (record.json), its
    pre_messages on S2's compaction path (`compact.to_record_messages`, each with `_i`)."""
    b = Path(bdir) / "boundary"
    rid = _read(b / "record.json")["id"]
    pre = _read(b / "pre_messages.json")
    return {"id": rid, "source": "deepswe", "session_id": rid,
            "source_messages": K.to_record_messages(pre), "user_turns": [], "summary": "",
            "post_messages": [], "license": "apache-2.0 (DeepSWE); our runs"}


def _paths(bdir: Path, policy: str) -> tuple[Path, Path]:
    d = Path(bdir) / "boundary" / REPLAY_DIR / policy
    return d / "replay.json", d / "post_messages.json"


def _canonical(bdir: Path, rec: dict, policy: str, row: dict, backend,
               instr: str | None) -> dict:
    pre = _read(Path(bdir) / "boundary" / "pre_messages.json")
    kept = row["kept_messages"]
    kept_idx = [m["_i"] for m in kept]
    kept_texts = [m.get("text") or "" for m in kept]
    post, sidx = K.post_context(policy, pre, row["summary"], kept_idx, instr,
                                kept_texts=kept_texts)
    src = rec["source_messages"]
    return {
        "policy": policy, "record_id": rec["id"], "summary": row["summary"],
        "summary_index": sidx, "kept_indices": kept_idx, "kept_texts": kept_texts,
        "input_messages": [{k: v for k, v in m.items() if k != "_i"} for m in src],
        "input_index_map": [m["_i"] for m in src],
        "instruction_text": instr,
        "prompt_sha256": row.get("prompt_sha256"), "request_sha256": row.get("request_sha256"),
        "policy_version": row.get("policy_version"), "model": row.get("model"),
        "backend": getattr(backend, "name", None), "usage": row.get("usage"),
        "fingerprint": row["fingerprint"], "post_sha256": canon_sha256(post),
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, post


def replay_boundary(bdir: Path, policies=RP.REPLAY_ARMS, *, backend, spend=None) -> dict:
    """Replay every policy in `policies` on boundary `bdir` (= <run>/b<k>) and write each
    canonical record (module docstring). Returns {policy: {"status": "written" | "reused" |
    "error", "error": str | None, "fingerprint": str}}. `spend` is accepted for the frozen
    interface; booking (Max $0 rows, any paid fallback) is the backend's."""
    del spend
    bdir = Path(bdir)
    unknown = [p for p in policies if p not in RP.REPLAY_ARMS]
    if unknown:
        raise ValueError(f"unknown replay policies {unknown}")
    rec = boundary_rec(bdir)
    instr = instruction_text(bdir)
    gen = RP._generation(backend)
    fps = {p: RP.fingerprint(rec, p, RP.ARM_BUILDERS[p](rec), gen) for p in policies}
    out: dict[str, dict] = {}
    todo = []
    for p in policies:
        rj, pj = _paths(bdir, p)
        if rj.exists() and pj.exists() and _read(rj).get("fingerprint") == fps[p]:
            out[p] = {"status": "reused", "error": None, "fingerprint": fps[p]}
        else:
            todo.append(p)
    rdir = bdir / "boundary" / REPLAY_DIR
    if todo:
        RP.check_backend(backend)
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            futs = [ex.submit(RP.run_replays, [rec["id"]], [p], backend, out_dir=rdir,
                              load=lambda _rid: rec) for p in todo]
            for f in futs:
                f.result()
    for p in todo:
        rows = RP.read_replays(rdir / f"{p}.jsonl", {RP.replay_id(rec, p): fps[p]})
        row = rows.get(RP.replay_id(rec, p))
        if row is None or row.get("error"):
            last = RP.read_replays(rdir / f"{p}.jsonl").get(RP.replay_id(rec, p)) or {}
            out[p] = {"status": "error", "fingerprint": fps[p],
                      "error": (row or last).get("error") or "no replay row"}
            continue
        canon, post = _canonical(bdir, rec, p, row, backend, instr)
        rj, pj = _paths(bdir, p)
        _dump(pj, post)
        _dump(rj, canon)  # last: a replay is complete only once replay.json exists
        out[p] = {"status": "written", "error": None, "fingerprint": fps[p]}
    return out


def load_source(bdir: Path, source: str) -> dict:
    """The resumed input of one summary source at boundary `bdir` (= <run>/b<k>): "live" (the
    run's own compaction) or a replayed policy. {"post", "summary_index", "summary",
    "kept_indices", "fingerprint"}. Raises when post_messages.json no longer matches its
    record."""
    b = Path(bdir) / "boundary"
    if source == "live":
        comp = _read(b / "compaction.json")
        post = _read(b / "post_messages.json")
        if post != comp["post_messages"]:
            raise ValueError(f"{b}: post_messages.json differs from compaction.json")
        info = comp.get("info") or {}
        return {"post": post, "summary_index": comp["summary_index"],
                "summary": comp["summary"], "kept_indices": comp["kept_indices"],
                "fingerprint": info.get("request_sha256")}
    if source not in RP.REPLAY_ARMS:
        raise ValueError(f"unknown source {source!r}")
    rj, pj = _paths(Path(bdir), source)
    canon, post = _read(rj), _read(pj)
    if canon_sha256(post) != canon["post_sha256"]:
        raise ValueError(f"{pj}: differs from its replay record")
    return {"post": post, "summary_index": canon["summary_index"], "summary": canon["summary"],
            "kept_indices": canon["kept_indices"], "fingerprint": canon["fingerprint"]}
