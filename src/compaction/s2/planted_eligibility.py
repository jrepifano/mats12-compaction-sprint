"""Explicitly authorized Docker-only validation of planted-pilot boundary eligibility.

No Claude CLI, simulator call, token or model-fork allocation is used. A validation
restores the kept image and UserSim, checks their frozen fingerprint/request hashes,
records immutable Docker image identity, and confirms cleanup. Originals stay read-only.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2 import hosts as H
from compaction.s2 import interact as I
from compaction.s2 import planted_step0 as P
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N

CPUS = 2.0
MEMORY_MB = 3072
LIFETIME_S = 600
LEGACY_RESOURCE_ERROR = "ValueError: local eligibility launch exceeds 3 containers / 12 GB / 8 CPUs"


def audit_dependencies(sb) -> dict:
    """Check audit tools in the kept image without invoking a model or installing anything."""
    command = "command -v python3 && command -v curl && command -v git"
    result = sb.exec(command, timeout_s=30, workdir="/")
    return {"ok": result.rc == 0 and len(result.stdout.splitlines()) == 3,
            "command": command, "exit_code": result.rc, "stdout": result.stdout,
            "stderr": result.stderr}


def image_identity(image: str, host: str | None, container: str | None = None) -> dict:
    """Read current immutable tag/container image identities; never pull an image."""
    with SB.using_host(host):
        tag = SB._docker(["image", "inspect", "--format", "{{.Id}}", image], timeout=60)
        image_id = tag.stdout.decode().strip()
        container_id = None
        if container:
            current = SB._docker(["inspect", "--format", "{{.Image}}", container], timeout=60)
            container_id = current.stdout.decode().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("kept image has no immutable Docker image id")
    return {"tag": image, "image_id": image_id, "container_image_id": container_id}


def resource_guard(host: str | None, *, task_cpus: float = CPUS,
                   task_memory_mb: int = MEMORY_MB, local_hostname: str | None = None,
                   pool: str | None = None,
                   clock: Callable = time.monotonic, sleeper: Callable = time.sleep,
                   status: Callable | None = None, check: Callable | None = None,
                   poll_s: float = 60., hold_after_s: float = 45 * 60.) -> dict:
    """Wait for room for the task and its 128 MiB / 0.5 CPU egress sidecar.

    The desktop shares a 26 GiB / 28 CPU pool with *all* emx containers,
    including task chaining. Other local hosts keep the laptop's three-container,
    12 GiB / 8 CPU cap. This is a read-only census, never container reclamation.
    The sole planted launcher serializes launches; the chaining launcher owns its
    own reservations. The logical ``desktop`` pool comes from an explicit caller
    selection or a matching local endpoint in SPRINT_S2_HOSTS, never the machine
    hostname. Unconfigured hosts default safely to laptop. Remote SSH eligibility
    retains its existing separate policy.

    ``status`` receives WAIT lines and a single HOLD: line after 45 minutes; the
    unattended runtime supplies its durable status writer. ``check`` can raise
    for cancellation or a deadline, including immediately before a fit returns.
    Incomplete/unbounded limits cannot establish fit and also wait. Polling never
    allocates a fork or changes its assignment.
    """
    if (not math.isfinite(task_cpus) or task_cpus <= 0 or task_memory_mb <= 0
            or not math.isfinite(poll_s) or poll_s <= 0
            or not math.isfinite(hold_after_s) or hold_after_s < 0):
        raise ValueError("resource guard limits and polling intervals must be positive")
    if pool not in (None, "desktop", "laptop"):
        raise ValueError("resource pool must be desktop or laptop")
    if host and host.startswith("ssh://"):
        return {"scope": "remote", "task_cpus": task_cpus, "task_memory_mb": task_memory_mb}
    hostname = local_hostname if local_hostname is not None else socket.gethostname()
    if pool is None:
        target = host or os.environ.get("DOCKER_HOST") or None
        configured = H.default_hosts()
        # Only a local endpoint may opt into this pool. Matching every configured
        # alias prevents a conflicting laptop alias from silently widening caps.
        matching = [entry.name for entry in configured
                    if (entry.docker_host or os.environ.get("DOCKER_HOST") or None) == target]
        local = target is None or target.startswith("unix://")
        pool = "desktop" if local and matching == ["desktop"] else "laptop"
    desktop = pool == "desktop"
    count_cap, memory_cap, cpu_cap = (None, 26 * 1024**3, 28.) if desktop else (3, 12 * 1024**3, 8.)
    emit = status if status is not None else lambda line: print(line, file=sys.stderr, flush=True)
    started, polls, held = clock(), 0, False
    while True:
        if check is not None:
            check()
        with SB.using_host(host):
            running = SB._docker(["ps", "-q", "--filter", f"label={SB.LABEL_KEY}"], timeout=60)
            names = running.stdout.decode().split()
            metadata = []
            if names:
                result = SB._docker(["inspect", *names], timeout=60)
                metadata = json.loads(result.stdout)
        configs = [row.get("HostConfig", {}) for row in metadata]
        bounded = len(configs) == len(names) and all(
            isinstance(row.get("Memory"), (int, float)) and row["Memory"] > 0
            and isinstance(row.get("NanoCpus"), (int, float)) and row["NanoCpus"] > 0
            for row in configs)
        count = len(names) + 2
        memory = (sum(row["Memory"] for row in configs) + (task_memory_mb + 128) * 1024**2
                  if bounded else None)
        cpus = (sum(row["NanoCpus"] / 1e9 for row in configs) + task_cpus + .5
                if bounded else None)
        elapsed = max(0., clock() - started)
        result = {"scope": "local", "pool": pool, "hostname": hostname,
                  "projected_containers": count, "projected_memory_bytes": memory,
                  "projected_cpus": cpus, "container_cap": count_cap,
                  "memory_cap_bytes": memory_cap, "cpu_cap": cpu_cap,
                  "task_memory_mb": task_memory_mb, "task_cpus": task_cpus,
                  "sidecar_memory_mb": 128, "sidecar_cpus": .5,
                  "bounded_existing_limits": bounded, "waited_s": elapsed,
                  "wait_polls": polls, "hold_emitted": held}
        if bounded and (count_cap is None or count <= count_cap) and memory <= memory_cap and cpus <= cpu_cap:
            if check is not None:
                check()
            return result
        detail = (f"{pool} resource pool; projected containers={count}, "
                  f"memory_bytes={memory}, cpus={cpus}; limits "
                  f"containers={count_cap}, memory_bytes={memory_cap}, cpus={cpu_cap}")
        if not held and elapsed >= hold_after_s:
            emit(f"HOLD: resource capacity unavailable for {elapsed:.0f}s; {detail}; continuing to wait")
            held = True
        emit(f"WAIT: {detail}; rechecking in {poll_s:g}s")
        polls += 1
        sleeper(poll_s)


def validate_boundary(bdir: Path, out_dir: Path, *, task, go_from_jacob: bool = False,
                      host: str | None = "ssh://rig", session_factory: Callable = N.NativeSession,
                      checker: Callable = CC.fork_check, image_inspector: Callable = image_identity,
                      resources: Callable = resource_guard) -> dict:
    """Validate one boundary, with injectable Docker/session seams for offline tests.

    Image identity is pinned from current tag resolution before restore and checked
    against the actual container and tag afterward. Historical content is certified
    by fork_check's boundary fingerprint; a historical image id is checked if stored.
    No external action occurs until go_from_jacob is true.
    """
    if not go_from_jacob:
        raise PermissionError("Docker eligibility validation requires --go-from-jacob")
    bdir, out_dir = Path(bdir), Path(out_dir)
    if out_dir.exists():
        raise FileExistsError("eligibility output already exists; never overwrite validation evidence")
    census = P.inspect_boundary(bdir)
    result = {"task_id": task.task_id, "boundary": str(bdir.resolve()), "eligible": False,
              "offline": census, "model_calls": 0, "model_fork_budget_used": 0,
              "cleanup_unconfirmed": [], "checks": {}}
    out_dir.mkdir(parents=True)
    ns = None
    try:
        if not census["offline_eligible"]:
            raise ValueError(f"offline boundary checks failed: {census['problems']}")
        original_task = P.read(bdir.parent / "run.json").get("task", {}).get("task_id")
        if original_task != task.task_id:
            raise ValueError("boundary belongs to a different task")
        copied = P.snapshot_boundary(bdir, out_dir / "source", expected_hashes=census["hashes"])
        # Reject source movement during the private copy before contacting Docker.
        if P.inspect_boundary(bdir)["hashes"] != census["hashes"]:
            raise ValueError("source boundary changed during snapshot")
        original = P.read(bdir / "boundary/boundary.json")
        metadata = P.read(copied / "boundary/boundary.json")
        copied_check = P.inspect_boundary(copied)
        if not copied_check["offline_eligible"]:
            raise ValueError("private source copy failed offline validation")
        for rel, sha in census["hashes"].items():
            if rel != "boundary/boundary.json" and copied_check["hashes"].get(rel) != sha:
                raise ValueError(f"private source copy differs: {rel}")
        if metadata != N.reclassify_harness_quiescence(original):
            raise ValueError("private boundary correction differs from frozen whitelist")
        result["launch_boundary"] = str(copied)
        result["resources"] = resources(host)
        before = image_inspector(metadata["tag"], host)
        image_id = before["image_id"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("image inspector did not return an immutable id")
        historical_id = metadata.get("image_id")
        if historical_id and historical_id != image_id:
            raise ValueError("kept image tag differs from historical image id")
        result["image_before"] = before
        exported = P.read(copied / "boundary/sim_state.json")

        def sim_factory(runner):
            return I.UserSim.restore(exported, task.persona, CC.NoSimulatorModel(), runner,
                                     purpose="sim:eligibility-no-calls")

        ns = session_factory(CC.ARM, image_id, sim_factory, host=host, setup_git=False,
                             cpus=CPUS, memory_mb=MEMORY_MB, min_free_gb=N.AGENT_MIN_FREE_GB,
                             lifetime_s=LIFETIME_S, allow=())
        with ns:
            ns.server.step = metadata.get("stage") or CC.GRADED_STEP
            inside = image_inspector(metadata["tag"], host, ns.sb.name)
            result["image_restored"] = inside
            identity_ok = inside.get("image_id") == image_id and inside.get("container_image_id") == image_id
            if not identity_ok:
                raise ValueError("restored container or tag image identity changed")
            check = checker(ns, metadata)
            result["fork_check"] = check
            dependencies = audit_dependencies(ns.sb)
            result["audit_dependencies"] = dependencies
            rounds = getattr(ns.server.sim, "rounds", [])
            result["sim_calls"] = len(ns.server.rows)
            result["sim_cost_usd"] = sum(row.get("cost_usd") or 0 for row in rounds)
            result["checks"] = {"image_identity": identity_ok, "restore": check.get("ok") is True,
                                "audit_dependencies": dependencies["ok"],
                                "no_services": not ns.services,
                                "zero_simulator": not ns.server.rows and not rounds}
            after = image_inspector(metadata["tag"], host)
            result["image_after"] = after
            result["checks"]["tag_stable"] = after.get("image_id") == image_id
        result["cleanup_unconfirmed"] = list(ns.cleanup_unconfirmed)
        result["checks"]["cleanup"] = not result["cleanup_unconfirmed"]
        result["eligible"] = bool(result["checks"]) and all(result["checks"].values())
    except Exception as exc:  # noqa: BLE001 -- report validation failure after session cleanup
        result["error"] = f"{type(exc).__name__}: {exc}"
        if ns is not None:
            result["cleanup_unconfirmed"] = list(ns.cleanup_unconfirmed)
    finally:
        P.write(out_dir / "eligibility.json", result)
    return result


def validate_cohort(census: dict, out_dir: Path, *, task_loader: Callable,
                    go_from_jacob: bool = False, host: str | None = "ssh://rig",
                    validator: Callable = validate_boundary, **kwargs) -> dict:
    """Validate frozen selected tasks until one live boundary passes per task.

    Failed tasks are retained, never silently replaced. At least two distinct live
    eligible tasks meets the existing-boundary branch of the Step-0 eligibility gate.
    Unconfirmed cleanup stops further launches. Validation itself uses no model forks.
    """
    if not go_from_jacob:
        raise PermissionError("Docker eligibility validation requires --go-from-jacob")
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise FileExistsError("cohort eligibility output already exists")
    out_dir.mkdir(parents=True)
    selected = list(census["selected_tasks"])
    if len(selected) != len(set(selected)):
        raise ValueError("selected tasks must be unique")
    rows, stop = [], False
    for task_id in selected:
        row = {"task_id": task_id, "eligible": False, "boundaries": []}
        rows.append(row)
        if stop:
            row["not_attempted"] = "cleanup_stop"
            continue
        entry = next((t for t in census["tasks"] if t["task_id"] == task_id), None)
        if entry is None:
            row["error"] = "selected task missing from census"
            continue
        try:
            task = task_loader(task_id)
        except Exception as exc:  # noqa: BLE001 -- retain every assigned task in the report
            row["error"] = f"task load: {type(exc).__name__}: {exc}"
            continue
        for index, boundary in enumerate(entry["boundaries"]):
            if stop or not boundary.get("offline_eligible"):
                continue
            try:
                result = validator(Path(boundary["boundary"]), out_dir / task_id / f"candidate-{index}",
                                   task=task, go_from_jacob=True, host=host, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- unknown cleanup prevents more launches
                result = {"eligible": False, "error": f"{type(exc).__name__}: {exc}",
                          "cleanup_unconfirmed": ["validator raised without a cleanup result"]}
            row["boundaries"].append(result)
            if result.get("cleanup_unconfirmed"):
                stop = True
                break
            if result.get("eligible"):
                row["eligible"] = True
                break
    count = sum(row["eligible"] for row in rows)
    report = {"selected_tasks": selected, "tasks": rows, "eligible_selected_tasks": count,
              "at_least_two_live_eligible": count >= 2, "cleanup_stop": stop,
              "decision": "existing_boundaries" if count >= 2 and not stop else "contingency_needed",
              "model_calls": 0, "model_fork_budget_used": 0}
    P.write(out_dir / "eligibility.json", report)
    return report
