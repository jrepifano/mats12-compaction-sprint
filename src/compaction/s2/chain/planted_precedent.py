"""Saved pi boundary replay, using summary_fix's unchanged task-2 execution path.

Exact, disjoint replacement validation ported from
src/compaction/s2/native/r7_replay.py on origin/sprint/r7-planted. Unlike R7,
pi retains tool calls and outputs: decoded command/output fields are eligible
when they show push outcome (coordinator decision 2026-10-29). Original raw
files are read-only; honest compaction bytes are copied unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import monitor_controls as M
from compaction.s2.chain import session as SE
from compaction.s2.chain import summary_fix as F
from compaction.s2.chain import week2 as W

CONFIG = "configs/week2/planted_precedent.toml"
RAW = "data/raw/sprint/s2/cobalt"
RESULTS = "results/sprint/week2/planted_precedent"
HISTORIES = ("httpx-np-1", "httpx-np-6", "httpx-np-7", "httpx-np-8", "tengo-np-4", "tengo-np-5")
ARMS = ("planted", "honest")
CUE = re.compile(
    r"(?i)\b(traps?|tests?|eval(?:s|uation)?|scenarios?|experiments?|planted|graders?|precedent)\b"
)
STAGE = "SPRINT-S2-PLANTED-PRECEDENT"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked(root: Path, entry: dict) -> bytes:
    p = (root / entry["path"]).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("source path escapes root")
    data = p.read_bytes()
    if sha(data) != entry["sha256"]:
        raise ValueError(f"source hash mismatch: {p}")
    return data


def fields(rec: dict):
    """Mutable eligible leaves; JSON-encoded bash arguments/output are decoded once.

    Returns (row, kind, owner, key) plus serializers, applied after leaf edits.
    Summary metadata copies are not eligible (would double-count every find).
    """
    leaves, serializers = [], []
    for i, m in enumerate(rec["post_messages"]):
        if m["role"] == "system" or (m["role"] == "user" and i != rec["summary_index"]):
            continue
        content = m.get("content")
        if isinstance(content, str) and content:
            obj = None
            if m["role"] == "tool":
                try:
                    obj = json.loads(content)
                except ValueError:
                    pass
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str):
                        leaves.append((i, "tool_output", obj, k))
                serializers.append((m, "content", obj))
            else:
                leaves.append(
                    (i, "summary" if i == rec["summary_index"] else "prose", m, "content")
                )
        for tc in m.get("tool_calls") or []:
            f = tc["function"]
            obj = json.loads(f["arguments"])
            if f["name"] == "bash" and isinstance(obj.get("command"), str):
                leaves.append((i, "tool_call", obj, "command"))
                serializers.append((f, "arguments", obj))
    return leaves, serializers


def observation(obj: dict, output: str) -> str:
    """Use the production template, including Jinja HTML-safe JSON and 5,000-char slices."""
    from compaction.s2 import loop as L
    from compaction.s2 import mini_prompts as MP

    return L._JINJA.from_string(MP.OBSERVATION_TEMPLATE).render(
        output={
            "returncode": obj["returncode"],
            "output": output,
            "exception_info": obj.get("exception_info", ""),
        }
    )


def apply_edits(
    data: bytes, spec: list[dict], *, arm: str, full_outputs: dict[int, str] | None = None
) -> tuple[bytes, list[dict]]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm}")
    rec = json.loads(data)
    if not isinstance(spec, list) or not spec:
        raise ValueError("spec must be a nonempty list")
    leaves, serializers = fields(rec)
    edits, audit = {}, []
    for n, pair in enumerate(spec):
        if (
            not isinstance(pair, dict)
            or set(pair) != {"find", "replace"}
            or not all(isinstance(v, str) and v for v in pair.values())
        ):
            raise ValueError(f"edit {n}: nonempty find/replace required")
        old, new = pair["find"], pair["replace"]
        if old == new:
            raise ValueError(f"edit {n}: no change")
        # Inherited coding vocabulary is retained; no additional cue occurrences allowed.
        if Counter(x.lower() for x in CUE.findall(new)) - Counter(
            x.lower() for x in CUE.findall(old)
        ):
            raise ValueError(f"edit {n}: introduced cue word")
        hits = []
        for f, (_, _, owner, key) in enumerate(leaves):
            at = owner[key].find(old)
            while at >= 0:
                hits.append((f, at))
                at = owner[key].find(old, at + 1)
        if len(hits) != 1:
            raise ValueError(f"edit {n}: find matched {len(hits)} times; expected exactly once")
        f, at = hits[0]
        end = at + len(old)
        if any(at < b and a < end for a, b, _ in edits.get(f, [])):
            raise ValueError(f"edit {n}: overlapping finds")
        edits.setdefault(f, []).append((at, end, new))
        i, kind, _, _ = leaves[f]
        audit.append(
            {
                "edit": n,
                "row": i,
                "kind": kind,
                "find_sha256": sha(old.encode()),
                "replace_sha256": sha(new.encode()),
            }
        )
    if arm == "honest":
        return data, []
    old_summary = rec["summary"]
    si = rec["summary_index"]
    prefix, sep, suffix = rec["post_messages"][si]["content"].partition(old_summary)
    if not sep:
        raise ValueError("summary is not in its wrapper")
    for f, spans in edits.items():
        _, _, owner, key = leaves[f]
        for a, b, new in sorted(spans, reverse=True):
            owner[key] = owner[key][:a] + new + owner[key][b:]
    for owner, key, obj in serializers:
        original = json.loads(owner[key])
        if original == obj:
            continue
        if key == "arguments":
            # Replace just the JSON string token; retain whitespace/key order of the call.
            match = re.search(r'"command"\s*:\s*("(?:\\.|[^"\\])*")', owner[key])
            if match is None:
                raise ValueError("command argument token missing")
            owner[key] = (
                owner[key][: match.start(1)]
                + json.dumps(obj["command"], ensure_ascii=False)
                + owner[key][match.end(1) :]
            )
        elif "output" in obj:
            owner[key] = observation(obj, obj["output"])
        elif "output_tail" in obj:
            row = next(i for i, m in enumerate(rec["post_messages"]) if m is owner)
            full = (full_outputs or {}).get(row)
            if full is None or observation(original, full) != owner[key]:
                raise ValueError(f"row {row}: truncated output requires verified full observation")
            changes = []
            for f, spans in edits.items():
                i, _, leaf, field = leaves[f]
                if i != row or leaf is not obj:
                    continue
                if field not in ("output_head", "output_tail"):
                    raise ValueError("unsupported truncated observation field")
                offset = 0 if field == "output_head" else len(full) - 5000
                changes.extend((offset + a, offset + b, value) for a, b, value in spans)
            for a, b, value in sorted(changes, reverse=True):
                full = full[:a] + value + full[b:]
            owner[key] = observation(obj, full)
        else:
            raise ValueError("edited tool observation has no production output field")
    wrapper = rec["post_messages"][si]["content"]
    if not wrapper.startswith(prefix) or (suffix and not wrapper.endswith(suffix)):
        raise ValueError("summary wrapper changed")
    rec["summary"] = wrapper[len(prefix) : len(wrapper) - len(suffix) if suffix else None]
    return (json.dumps(rec, ensure_ascii=False, indent=1) + "\n").encode(), audit


@dataclasses.dataclass(frozen=True)
class ReplayRun(F.FixRun):
    @property
    def id(self) -> str:
        # Used in container/image labels by session.run_b: no arm cue reaches the agent.
        return f"cobalt-{self.history}-r{self.replicate}-{'a' if self.arm == 'planted' else 'b'}"


def run(hist: str, arm: str, rep: int) -> ReplayRun:
    repo, _, number = hist.split("-")
    return ReplayRun(
        CC.Cell(CC.other_repo(repo).b_task, "divergent", "different", "compacted"),
        hist,
        int(number),
        arm,
        rep,
    )


def order(cfg: dict, smoke: bool = False) -> list[ReplayRun]:
    p = cfg["planted_precedent"]
    rng = random.Random(cfg["design"]["seed"])
    hs = [p["smoke_history"]] if smoke else list(p["histories"])
    rng.shuffle(hs)
    out = []
    for rep in range(1, 1 + (1 if smoke else max(p["planted_reps"], p["honest_reps"]))):
        for h in hs:
            arms = [a for a in ARMS if smoke or rep <= p[a + "_reps"]]
            rng.shuffle(arms)
            out.extend(run(h, a, rep) for a in arms)
    return out


def validate(config: str | Path = CONFIG) -> dict:
    cfg = W.load_config(config)
    W.gate(W.resolve(cfg["meta"]["proposal"]), config)
    p = cfg["planted_precedent"]
    want = {"kind": "planted_precedent", "stage": STAGE, "n_phases": 2, "log_reasoning": True}
    if any(cfg["design"].get(k) != v for k, v in want.items()):
        raise ValueError("design differs from replay code")
    if cfg["caps"] != {
        "stage_usd": 50.0,
        "run_usd": CC.RUN_CAP_USD,
        "max_calls": CC.MAX_CALLS,
        "max_seconds": CC.MAX_SECONDS,
    }:
        raise ValueError("caps differ from approved stage / experiment-5 task caps")
    if tuple(p["histories"]) != HISTORIES or tuple(p["arms"]) != ARMS:
        raise ValueError("histories/arms differ from audited set")
    if p["project"] != "different" or p["start"] != "compacted" or p["host"] != "rig":
        raise ValueError("task-2 replay location differs")
    if p["smoke_history"] not in HISTORIES or (
        p["planted_reps"],
        p["honest_reps"],
        p["historical_honest_reps"],
    ) != (2, 2, 1):
        raise ValueError("invalid smoke history / replicates")
    route = json.loads(checked(CC.REPO_ROOT, p["route"]))["route"]
    if (route["model"], route["provider_pin"], route["transport"]) != (
        p["model"],
        p["provider"],
        p["transport"],
    ):
        raise ValueError("route model/provider/transport mismatch")
    if (p["model"], p["provider"], p["transport"]) != (
        "deepseek/deepseek-v4.1-flash",
        "Together",
        "openrouter",
    ):
        raise ValueError("route differs from experiment 5")
    if p["output_root"] != RAW or not 1 <= cfg["parallelism"]["agents"] <= 16:
        raise ValueError("raw directory/admission differs")
    hashes = CC.text_hashes()
    for name, key in M.CHAIN_WORDING.items():
        if cfg["wording"][name]["sha256"] != hashes[key]:
            raise ValueError(f"task wording differs: {name}")
    return cfg


def prepare(cfg: dict, source: Path, root: Path) -> dict:
    """Validate every source and spec before writing any replay boundary; no paid code."""
    if root.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(
        root.resolve()
    ):
        raise ValueError("source/output roots overlap")
    p = cfg["planted_precedent"]
    man = json.loads(checked(CC.REPO_ROOT, p["manifest"]))
    if set(man["histories"]) != set(p["histories"]):
        raise ValueError("manifest/history mismatch")
    staged, reports = {}, {}
    for h in p["histories"]:
        row = man["histories"][h]
        for ent in row["files"].values():
            checked(source, ent)
        for ent in row["existing_honest"]["files"].values():
            checked(source, ent)
        prior = row["existing_honest"]["files"]
        old_messages = json.loads(checked(source, prior["messages.json"]))
        old_result = json.loads(checked(source, prior["result.json"]))
        old_comp = json.loads(checked(source, row["files"]["compaction.json"]))
        cutoff = (old_result.get("extra") or {}).get("b_message_index")
        if not isinstance(cutoff, int) or old_messages[:cutoff] != old_comp["post_messages"]:
            raise ValueError(f"{h}: historical honest boundary differs")
        spec = json.loads(checked(CC.REPO_ROOT, cfg["wording"][h]))
        data = checked(source, row["files"]["compaction.json"])
        full_outputs = (
            {
                int(k): v
                for k, v in json.loads(checked(CC.REPO_ROOT, row["full_observations"])).items()
            }
            if row.get("full_observations")
            else {}
        )
        for arm in ARMS:
            edited, audit = apply_edits(data, spec, arm=arm, full_outputs=full_outputs)
            staged[h, arm] = edited
            reports[f"{h}/{arm}"] = {
                "sha256": sha(edited),
                "source_sha256": sha(data),
                "edits": audit,
                "historical_boundary_identical": True,
            }
    # Snapshots use the same three-file path as experiment 5. Verify against our independently
    # pinned relative source paths too (monitor_controls supports old absolute state paths).
    M.snapshot(source, p["histories"], root)
    for h in p["histories"]:
        for f in M.SOURCE_FILES:
            if (
                sha((root / "sources" / h / f).read_bytes())
                != man["histories"][h]["files"][f]["sha256"]
            ):
                raise ValueError(f"{h}: snapshot disagrees with pinned source {f}")
    for b in order(cfg):
        dest = F.comp_path(root, b)
        data = staged[b.history, b.arm]
        if dest.exists() and dest.read_bytes() != data:
            raise ValueError(f"prepared boundary changed: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    result = {"fingerprint": W.fingerprint(cfg), "inputs": reports, "new_runs": len(order(cfg))}
    SE.dump(root / "prepared.json", result)
    return result


def replay_context(root: Path, b: ReplayRun, task_text: str, uname=None) -> list[dict]:
    hist = M.load_snapshot(root, b.history)
    hist.compaction = json.loads(F.comp_path(root, b).read_text())
    repo = CC.REPO_OF_TASK[b.cell.b_task]
    return SE.b_context(
        "compacted", "different", repo, hist, CC.task_text(task_text, repo.b_branch), uname
    )


def replay_check(cfg: dict, root: Path) -> dict:
    """Exercise the real SDK request serializer via httpx.MockTransport: no sockets/keys.

    b_context is precisely session.run_b's boundary construction; the real-container stub
    separately checks run_b hands that same prefix to the model after task setup.
    """
    import httpx
    from openai import OpenAI

    from compaction.s2.config import Route
    from compaction.s2.model import NullSpend, OpenAICompatModel

    received = []

    def endpoint(req):
        received.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "id": "local",
                "object": "chat.completion",
                "created": 0,
                "model": "local",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0},
            },
        )

    route = Route(**json.loads(checked(CC.REPO_ROOT, cfg["planted_precedent"]["route"]))["route"])
    tasks = D.tasks()
    checks = []
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as hc:
        client = OpenAI(
            api_key="local-only", base_url="http://localhost.invalid/v1", http_client=hc
        )
        model = OpenAICompatModel(route, NullSpend(), client=client)
        for h in cfg["planted_precedent"]["histories"]:
            for arm in ARMS:
                b = run(h, arm, 1)
                ctx = replay_context(root, b, tasks[b.cell.b_task].instruction)
                model.complete(ctx, None, 1, purpose="B:local:1")
                observed = received[-1]["messages"]
                expected = json.loads(F.comp_path(root, b).read_text())["post_messages"]
                original = json.loads((root / "sources" / h / "compaction.json").read_text())[
                    "post_messages"
                ]
                ok = observed[:-1] == expected and ((expected == original) == (arm == "honest"))
                if not ok:
                    raise ValueError(f"wire replay mismatch: {h}/{arm}")
                checks.append(
                    {
                        "history": h,
                        "arm": arm,
                        "pass": True,
                        "request_sha256": sha(json.dumps(observed).encode()),
                    }
                )
    return {
        "pass": True,
        "cost_usd": 0,
        "transport": "httpx.MockTransport, no network",
        "checks": checks,
    }


class ReplayGate(F.FixGate):
    def cost(self, b) -> float:
        done = [
            v["cost_usd"]
            for v in self.st["b"].values()
            if v.get("state") == "done" and v.get("cost_usd") is not None
        ]
        return (sum(done) / len(done) if done else F.ASSUMED_RUN_USD) * D.COST_MARGIN


def finalize(st: dict, runs: list[ReplayRun], root: Path) -> list[dict]:
    """Recover completed agents independently of scheduler/grading state, offline only.

    score_b reads saved Git objects and trap evidence locally. No grader/container/API is
    needed. A genuinely stopped or stage-interrupted attempt remains pending and unknown.
    """
    recovered = []
    for b in runs:
        rec = st["b"].get(b.id) or {}
        base = root / "b" / b.id
        candidates = [p for p in base.glob("a*") if p.name[1:].isdigit()]
        if rec.get("dir"):
            candidates.append(Path(rec["dir"]))
        if not candidates:
            continue
        latest = max(candidates, key=lambda p: int(p.name[1:]))
        if not latest.resolve().is_relative_to(root.resolve()):
            raise ValueError("saved attempt escapes raw root")
        rp = latest / "result.json"
        if not rp.exists():
            continue
        res = json.loads(rp.read_text())
        if res.get("end") in (SE.END_STOPPED, SE.END_STAGE_CAP):
            continue
        if res.get("end") not in (
            SE.END_SUBMIT,
            SE.END_CALLS,
            SE.END_COST,
            SE.END_TIME,
            SE.END_FAILED,
        ):
            continue
        row = F.score(latest, b)
        current = D.entry(st, "b", b.id, {**b.to_dict(), "attempts": int(latest.name[1:])})
        prior = current.get("state", "pending")
        current.update(
            state="done",
            dir=str(latest),
            end=res["end"],
            cost_usd=D._attempt_spend(latest)
            if D._attempt_spend(latest) is not None
            else res.get("cost_usd"),
            teammate_lost=row.get("teammate_lost"),
            rewrite=row.get("rewrite"),
            agent_outcome_saved=True,
        )
        if prior != "done":
            current["offline_finalized_from"] = prior
            recovered.append({"id": b.id, "from": prior, "end": res["end"]})
    D.save_state(st)
    return recovered
