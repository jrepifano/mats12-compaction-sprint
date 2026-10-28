"""R7: exact text edits to a saved compacted session, then only cc_chain's task 2.

No transport or scorer fork. The source session ID is kept: every replay has its own fresh
container. Honest input is byte-identical; planted input changes decoded message text only.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.native import cc_chain as X
from compaction.s2.native import claude_code as CCN

ARM = X.Arm("r7", X.MODEL, X.EFFORT, session="r7_chain")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rows_from(data: bytes) -> list[dict]:
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    if not rows or not all(isinstance(r, dict) for r in rows):
        raise ValueError("session must contain JSON objects")
    return rows


def eligible_indices(rows: list[dict]) -> list[int]:
    """Summary plus metadata-selected preserved rows; last UUID occurrence is active.

    This is the same UUID resolution as claude_code.transcript_boundary. Preserved rows may
    precede the boundary physically; file position alone does not determine active context.
    """
    boundaries = CCN.compact_rows(rows)
    if len(boundaries) != 1:
        raise ValueError(f"expected one compact boundary, got {len(boundaries)}")
    boundary = boundaries[0]
    summaries = [i for i, r in enumerate(rows) if r.get("isCompactSummary") is True]
    if len(summaries) != 1 or summaries[0] <= boundary:
        raise ValueError("expected one compact summary after the boundary")
    md = rows[boundary].get("compactMetadata") or {}
    preserved = md.get("preservedMessages") or {}
    uuids = preserved.get("uuids")
    if not isinstance(uuids, list) or len(set(uuids)) != len(uuids):
        raise ValueError("missing or duplicate preservedMessages.uuids")
    by = {r["uuid"]: i for i, r in enumerate(rows) if r.get("uuid")}
    if any(u not in by for u in uuids):
        raise ValueError("preserved UUID missing from session")
    indices = [summaries[0], *(by[u] for u in uuids)]
    if len(indices) != len(set(indices)):
        raise ValueError("summary also appears in preserved tail")
    return indices


def _text_fields(content, owner, key):
    """Yield mutable (container,key) references only to API-visible text, not tool inputs."""
    if isinstance(content, str):
        yield owner, key
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                yield block, "text"
            elif block.get("type") == "tool_result":
                yield from _text_fields(block.get("content"), block, "content")


def text_fields(rows: list[dict]):
    for i in eligible_indices(rows):
        message = rows[i].get("message") or {}
        for owner, key in _text_fields(message.get("content"), message, "content"):
            yield i, owner, key


def apply_edits(data: bytes, spec: list[dict], *, arm: str) -> tuple[bytes, list[dict]]:
    """Validate all finds against the original eligible text, then apply disjoint edits.

    Count occurrences across ALL eligible fields, not one match per row. Overlap and chained
    replacements are refused, so one pair cannot accidentally manufacture another's match.
    """
    if arm not in ("honest", "planted"):
        raise ValueError(f"unknown arm {arm}")
    rows = rows_from(data)
    fields = list(text_fields(rows))
    if not isinstance(spec, list) or not spec:
        raise ValueError("edit spec must be a nonempty list")
    edits: dict[int, list[tuple[int, int, str]]] = {}
    audit = []
    for n, pair in enumerate(spec):
        if not isinstance(pair, dict) or set(pair) != {"find", "replace"} or not all(
                isinstance(pair[k], str) and pair[k] for k in ("find", "replace")):
            raise ValueError(f"edit {n}: expected nonempty find/replace strings")
        old, new = pair["find"], pair["replace"]
        if old == new:
            raise ValueError(f"edit {n}: no change")
        hits = []
        for f, (_, owner, key) in enumerate(fields):
            at = owner[key].find(old)
            while at >= 0:
                hits.append((f, at))
                at = owner[key].find(old, at + 1)  # overlapping starts count separately
        if len(hits) != 1:
            raise ValueError(f"edit {n}: find matched {len(hits)} times; expected exactly once")
        f, at = hits[0]
        stop = at + len(old)
        if any(at < end and start < stop for start, end, _ in edits.get(f, [])):
            raise ValueError(f"edit {n}: overlapping finds")
        edits.setdefault(f, []).append((at, stop, new))
        i, owner, key = fields[f]
        audit.append({"edit": n, "row": i, "uuid": rows[i].get("uuid"),
                      "summary": rows[i].get("isCompactSummary") is True,
                      "find_sha256": sha256(old.encode()), "replace_sha256": sha256(new.encode())})
    if arm == "honest":
        return data, []
    changed = set()
    for f, replacements in edits.items():
        i, owner, key = fields[f]
        text = owner[key]
        for start, end, new in sorted(replacements, reverse=True):
            text = text[:start] + new + text[end:]
        owner[key] = text
        changed.add(i)
    # Preserve untouched lines exactly, including whitespace and final newline.
    lines = data.decode("utf-8").splitlines(keepends=True)
    row_index = 0
    for k, line in enumerate(lines):
        if not line.strip():
            continue
        if row_index in changed:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[k] = json.dumps(rows[row_index], ensure_ascii=False) + ending
        row_index += 1
    return "".join(lines).encode(), audit


def checked_file(root: Path, entry: dict) -> bytes:
    path = (root / entry["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("source path escapes source root")
    data = path.read_bytes()
    if sha256(data) != entry["sha256"]:
        raise ValueError(f"source hash mismatch: {path}")
    return data


@dataclass(frozen=True)
class Prepared:
    history: str
    arm: str
    rep: int
    sid: str
    data: bytes
    provenance: dict

    @property
    def run_id(self) -> str:
        return f"{self.history}/{self.arm}-{self.rep}"


def prepare(cfg: dict, root: Path) -> list[Prepared]:
    """Hash-check every source/spec and existing honest artifact before ANY replay runs."""
    replay = cfg["replay"]
    if replay["arms"] != ["planted", "honest"] or replay["new_runs"] != [2, 1] or \
            replay["existing_honest_runs"] != 1:
        raise ValueError("expected two new planted and one new plus one existing honest run")
    manifest = json.loads(checked_file(root, replay["manifest"]))
    if manifest.get("version") != 1 or set(manifest["histories"]) != set(replay["histories"]):
        raise ValueError("source manifest version/history set mismatch")
    source_root = Path(replay["source_root"])
    prepared = []
    for hid in replay["histories"]:
        source = manifest["histories"][hid]
        hist = CC.History(source["repo"], "np", source["rep"], source["remote"])
        if hist.id != hid or hist.remote != "divergent":
            raise ValueError(f"source identity mismatch: {hid}")
        data = checked_file(source_root, source["session"])
        ids = {r["sessionId"] for r in rows_from(data) if r.get("sessionId")}
        if len(ids) != 1:
            raise ValueError(f"{hid}: expected one source sessionId")
        sid = ids.pop()
        import uuid
        uuid.UUID(sid)  # only a valid session ID can enter the CLI/path
        old = source["existing_honest"]
        required = {"result.json", "messages.json", "traps_home/config.json"}
        if not required <= set(old["files"]) or not any(
                p.startswith("traps_home/state/") for p in old["files"]):
            raise ValueError(f"{hid}: existing honest manifest lacks scorer evidence")
        for path, digest in old["files"].items():
            checked_file(source_root, {"path": str(Path(old["path"]) / path), "sha256": digest})
        spec_raw = checked_file(root, cfg["wording"][hid])
        spec = json.loads(spec_raw)
        for arm, n in zip(replay["arms"], replay["new_runs"], strict=True):
            edited, audit = apply_edits(data, spec, arm=arm)
            offset = replay["existing_honest_runs"] if arm == "honest" else 0
            for rep in range(offset + 1, offset + n + 1):
                provenance = {"history": hid, "arm": arm, "rep": rep, "session_id": sid,
                              "source_root": str(source_root), "source": source,
                              "input_sha256": sha256(edited), "source_sha256": sha256(data),
                              "spec_sha256": sha256(spec_raw), "edits": audit,
                              "config": cfg.get("_source"), "manifest": replay["manifest"]}
                prepared.append(Prepared(hid, arm, rep, sid, edited, provenance))
    rng = random.Random(cfg["design"]["seed"])
    histories = list(replay["histories"])
    rng.shuffle(histories)
    ordered = []
    for hid in histories:
        runs = [p for p in prepared if p.history == hid]
        rng.shuffle(runs)
        ordered.extend(runs)
    return ordered


def run_replay(cfg: X.HistoryConfig, prepared: Prepared) -> dict:
    """Fresh task-2 container; unchanged prompt, traps, cap enforcement, and scorer outputs."""
    if cfg.hist.id != prepared.history:
        raise ValueError("prepared history does not match task config")
    out = cfg.out
    out.mkdir(parents=True, exist_ok=False)
    (out / "replay.json").write_text(json.dumps(prepared.provenance, indent=2) + "\n")
    (out / "input_session.jsonl").write_bytes(prepared.data)
    b = out / "b"
    b.mkdir()
    a_repo = CC.REPOS[cfg.hist.repo]
    b_repo = CC.REPOS[next(r for r in CC.REPOS if r != cfg.hist.repo)]
    # Installation happens only on an explicitly authorized run, never prepare/dry-run.
    CCN.ensure_cli_volume(None, cfg.b_task.image)
    return X._task(cfg, b, cfg.b_task, [], second=True, sid=prepared.sid,
                   prompt=X.lead_and_task(b_repo, a_repo, cfg.b_task.instruction),
                   session_data=prepared.data, b_repo=b_repo)
