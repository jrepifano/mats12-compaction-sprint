"""Explicitly authorized Docker-only validation of planted-pilot boundary eligibility.

No Claude CLI, simulator call, token or model-fork allocation is used. A validation
restores the kept image and UserSim, checks their frozen fingerprint/request hashes,
records immutable Docker image identity, and confirms cleanup. Originals stay read-only.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2 import interact as I
from compaction.s2 import planted_step0 as P
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N

CPUS = 2.0
MEMORY_MB = 3072
LIFETIME_S = 600


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


def resource_guard(host: str | None) -> dict:
    """Local aggregate cap includes the task and its native egress sidecar."""
    if host and host.startswith("ssh://"):
        return {"scope": "remote", "task_cpus": CPUS, "task_memory_mb": MEMORY_MB}
    with SB.using_host(host):
        running = SB._docker(["ps", "-q", "--filter", f"label={SB.LABEL_KEY}"], timeout=60)
        names = running.stdout.decode().split()
        metadata = []
        if names:
            result = SB._docker(["inspect", *names], timeout=60)
            metadata = json.loads(result.stdout)
    # Unbounded existing resource settings cannot establish that the cap is met.
    configs = [row["HostConfig"] for row in metadata]
    if any(not row.get("Memory") or not row.get("NanoCpus") for row in configs):
        raise ValueError("existing local emx container has unbounded CPU/memory")
    count = len(names) + 2
    memory = sum(row["Memory"] for row in configs) + (MEMORY_MB + 128) * 1024**2
    cpus = sum(row["NanoCpus"] / 1e9 for row in configs) + CPUS + .5
    if count > 3 or memory > 12 * 1024**3 or cpus > 8:
        raise ValueError("local eligibility launch exceeds 3 containers / 12 GB / 8 CPUs")
    return {"scope": "local", "projected_containers": count,
            "projected_memory_bytes": memory, "projected_cpus": cpus}


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
