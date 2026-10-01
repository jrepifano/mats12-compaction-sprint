"""Named-file Hugging Face downloads for I1a, with a size plan and a hard byte cap.

New module (implementation plan §2 U1; prereg §8). The whitener repo is a 3.38 TB gated dataset,
so a stray `snapshot_download` or a glob would be a very expensive mistake. This module therefore
downloads only files named one by one:

- `plan_bytes` sizes the files with `HfApi.get_paths_info` (no download);
- `fetch` refuses an empty list, any path with a glob character (`*`, `?`, `[`), a folder, a
  file missing at the revision, a revision that is not a 40-hex commit, and a total over
  `contracts.HF_MAX_DOWNLOAD_BYTES`; it then calls `hf_hub_download` per file and checks that each
  file exists with the planned size;
- `fetch_all_i1a` fetches the model (its safetensors shards, JSON configs and tokenizer files,
  listed from the repo at `MODEL_REV`), the J-Lens file, `OLENS_FILES`, `AR_FILES` and
  `WHITENER_FILES`, all planned before the first byte moves and capped in total.

`HF_HUB_DISABLE_XET=1` is set before `huggingface_hub` is imported (and again on its constants),
so downloads use plain HTTP (prereg §8; the Xet client stalled on large files).

CLI (network; never run by the tests):
  uv run python -m emx.i1a.hfget --plan            # planned bytes per group, nothing downloaded
Both modes write the plan (repos, pinned revisions, every file with its size) to
results/I1a/download_plan.json (`--plan-out` overrides) for the run manifest (review U1 r1).
  uv run python -m emx.i1a.hfget --fetch DIR       # fetch everything into DIR (or --fetch cache)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

os.environ["HF_HUB_DISABLE_XET"] = "1"  # before any huggingface_hub import

from emx.i1a import contracts as C

GLOB_CHARS = ("*", "?", "[")
SHA_RE = re.compile(r"[0-9a-f]{40}")
MODEL_FILE_NAMES = ("chat_template.jinja", "merges.txt", "vocab.json")


class HfGetError(RuntimeError):
    pass


def _disable_xet() -> None:
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        from huggingface_hub import constants

        constants.HF_HUB_DISABLE_XET = True  # in case the hub was imported before this module
    except ImportError:  # pragma: no cover - the hub is a dependency of every venv
        pass


def _api(api: Any = None, token: str | None = None) -> Any:
    if api is not None:
        return api
    _disable_xet()
    from huggingface_hub import HfApi

    return HfApi(token=token)


def check_files(files: Any) -> list[str]:
    """Refuse an empty, globbed or directory-shaped file list (never a whole-repo pull)."""
    if isinstance(files, str):
        raise HfGetError("files must be a list of repo paths, not one string")
    files = list(files)
    if not files:
        raise HfGetError("refusing an empty file list (a whole-repo snapshot is never allowed)")
    for f in files:
        if not isinstance(f, str) or not f.strip():
            raise HfGetError(f"bad repo path {f!r}")
        if any(ch in f for ch in GLOB_CHARS):
            raise HfGetError(f"refusing a glob pattern {f!r}: name every file")
        if f.endswith("/") or f in (".", "/"):
            raise HfGetError(f"refusing a directory {f!r}: name every file")
    if len(set(files)) != len(files):
        raise HfGetError("duplicate paths in the file list")
    return files


def check_revision(revision: str) -> str:
    if not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
        raise HfGetError(f"revision must be a pinned 40-hex commit, got {revision!r}")
    return revision


def file_sizes(
    repo: str,
    files: Any,
    *,
    revision: str,
    repo_type: str = "model",
    api: Any = None,
    token: str | None = None,
) -> dict[str, int]:
    """{path: size in bytes} for exactly the named files; raises if any is missing or a folder."""
    files = check_files(files)
    check_revision(revision)
    infos = _api(api, token).get_paths_info(repo, files, revision=revision, repo_type=repo_type)
    sizes: dict[str, int] = {}
    for info in infos:
        size = getattr(info, "size", None)
        if size is None:  # RepoFolder has no size
            raise HfGetError(f"{repo}@{revision[:8]}: {info.path} is a folder, not a file")
        sizes[info.path] = int(size)
    missing = [f for f in files if f not in sizes]
    if missing:
        raise HfGetError(f"{repo}@{revision[:8]}: not found at this revision: {missing}")
    return {f: sizes[f] for f in files}


def plan_bytes(
    repo: str,
    files: Any,
    *,
    revision: str,
    repo_type: str = "model",
    api: Any = None,
    token: str | None = None,
) -> int:
    """Total bytes the named files would download (nothing is downloaded)."""
    sizes = file_sizes(repo, files, revision=revision, repo_type=repo_type, api=api, token=token)
    return sum(sizes.values())


def check_total(total: int, what: str, limit: int | None = None) -> int:
    limit = C.HF_MAX_DOWNLOAD_BYTES if limit is None else limit
    if total > limit:
        raise HfGetError(f"{what}: {total:,} B planned > cap {limit:,} B; refusing")
    return total


def _download(
    repo: str,
    sizes: dict[str, int],
    *,
    revision: str,
    repo_type: str,
    local_dir: str | Path | None,
    token: str | None,
    downloader: Any,
) -> dict[str, Path]:
    if downloader is None:
        _disable_xet()
        from huggingface_hub import hf_hub_download as downloader
    out: dict[str, Path] = {}
    for f, size in sizes.items():
        kw: dict[str, Any] = {"revision": revision, "repo_type": repo_type}
        if local_dir is not None:
            kw["local_dir"] = str(local_dir)
        if token:
            kw["token"] = token
        p = Path(downloader(repo, f, **kw))
        if not p.is_file():
            raise HfGetError(f"{repo}/{f}: download returned {p}, which is not a file")
        got = p.stat().st_size
        if got != size:
            raise HfGetError(f"{repo}/{f}: {got:,} B on disk, expected {size:,} B")
        out[f] = p
    return out


def fetch(
    repo: str,
    files: Any,
    *,
    revision: str,
    repo_type: str = "model",
    local_dir: str | Path | None = None,
    api: Any = None,
    downloader: Any = None,
    token: str | None = None,
) -> dict[str, Path]:
    """Download exactly the named files at a pinned revision. Returns {repo path: local path}.

    `local_dir=None` uses the HF cache ($HF_HOME). `api`/`downloader` are injectable for tests."""
    _disable_xet()
    sizes = file_sizes(repo, files, revision=revision, repo_type=repo_type, api=api, token=token)
    check_total(sum(sizes.values()), f"{repo}@{revision[:8]}")
    return _download(
        repo,
        sizes,
        revision=revision,
        repo_type=repo_type,
        local_dir=local_dir,
        token=token,
        downloader=downloader,
    )


# --- everything I1a needs -------------------------------------------------------------------


def model_files(api: Any = None, token: str | None = None) -> list[str]:
    """Top-level model files at MODEL_REV: safetensors shards, *.json, tokenizer files,
    chat_template.jinja, merges.txt and vocab.json (no subfolders, no other weights)."""
    names = _api(api, token).list_repo_files(C.MODEL_ID, revision=check_revision(C.MODEL_REV))
    keep = sorted(
        n
        for n in names
        if "/" not in n
        and (
            n.endswith((".safetensors", ".json"))
            or n.startswith("tokenizer")
            or n in MODEL_FILE_NAMES
        )
    )
    if not any(n.endswith(".safetensors") for n in keep):
        raise HfGetError(f"{C.MODEL_ID}@{C.MODEL_REV[:8]}: no safetensors shards listed")
    if "config.json" not in keep:
        raise HfGetError(f"{C.MODEL_ID}@{C.MODEL_REV[:8]}: no config.json listed")
    return keep


def i1a_groups(api: Any = None, token: str | None = None) -> dict[str, dict[str, Any]]:
    """The four download groups: repo, revision, repo_type and the named files."""
    return {
        "model": {
            "repo": C.MODEL_ID,
            "revision": C.MODEL_REV,
            "repo_type": "model",
            "files": model_files(api, token),
        },
        "jlens": {
            "repo": C.JLENS_REPO,
            "revision": C.JLENS_REV,
            "repo_type": "model",
            "files": [C.JLENS_FILE],
        },
        "adapters": {
            "repo": C.ADAPTER_REPO,
            "revision": C.ADAPTER_REV,
            "repo_type": "model",
            "files": [*C.OLENS_FILES, *C.AR_FILES],
        },
        "whiteners": {
            "repo": C.WHITENER_REPO,
            "revision": C.WHITENER_REV,
            "repo_type": C.WHITENER_REPO_TYPE,
            "files": list(C.WHITENER_FILES),
        },
    }


def plan_all_i1a(api: Any = None, token: str | None = None) -> dict[str, Any]:
    """Sizes of every group and the grand total, checked against the cap. Downloads nothing."""
    api = _api(api, token)
    groups = i1a_groups(api, token)
    plan: dict[str, Any] = {"groups": {}}
    for name, g in groups.items():
        sizes = file_sizes(
            g["repo"], g["files"], revision=g["revision"], repo_type=g["repo_type"], api=api
        )
        plan["groups"][name] = {**g, "sizes": sizes, "bytes": sum(sizes.values())}
    if C.WHITENER_FILE_BYTES:
        bad = {
            f: s
            for f, s in plan["groups"]["whiteners"]["sizes"].items()
            if f.endswith(".safetensors") and s != C.WHITENER_FILE_BYTES
        }
        if bad:
            raise HfGetError(f"whitener sizes differ from contracts.WHITENER_FILE_BYTES: {bad}")
    plan["total_bytes"] = sum(g["bytes"] for g in plan["groups"].values())
    plan["cap_bytes"] = C.HF_MAX_DOWNLOAD_BYTES
    check_total(plan["total_bytes"], "I1a (all groups)")
    return plan


def fetch_all_i1a(
    local_dir: str | Path | None,
    *,
    api: Any = None,
    downloader: Any = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Plan everything (capped in total), then fetch each group. With `local_dir`, group G lands
    in local_dir/G (so local_dir/model is a from_pretrained directory); with None, the HF cache.

    Returns {"plan": ..., "paths": {group: {repo path: local path}}, "model_dir": Path,
    "jlens": Path, "olens_dir": Path, "ar_dir": Path, "whitener_dir": Path}."""
    _disable_xet()
    api = _api(api, token)
    plan = plan_all_i1a(api, token)
    paths: dict[str, dict[str, Path]] = {}
    for name, g in plan["groups"].items():
        paths[name] = _download(
            g["repo"],
            g["sizes"],
            revision=g["revision"],
            repo_type=g["repo_type"],
            local_dir=None if local_dir is None else Path(local_dir) / name,
            token=token,
            downloader=downloader,
        )
    adapters = paths["adapters"]
    return {
        "plan": plan,
        "paths": paths,
        "model_dir": paths["model"]["config.json"].parent,
        "jlens": paths["jlens"][C.JLENS_FILE],
        "olens_dir": adapters[C.OLENS_FILES[0]].parent,
        "ar_dir": adapters[C.AR_FILES[0]].parent,
        "whitener_dir": paths["whiteners"][C.WHITENER_FILES[0]].parent,
    }


PLAN_PATH = C.RESULTS / "download_plan.json"


def write_plan(plan: dict[str, Any], path: str | Path | None = None) -> Path:
    """Persist the audit record of a plan: per group repo, revision, repo_type and file sizes."""
    from datetime import UTC, datetime

    path = Path(path) if path is not None else PLAN_PATH
    rec = {
        "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hf_hub_disable_xet": os.environ.get("HF_HUB_DISABLE_XET"),
        "cap_bytes": plan["cap_bytes"],
        "total_bytes": plan["total_bytes"],
        "groups": {
            name: {
                "repo": g["repo"],
                "revision": g["revision"],
                "repo_type": g["repo_type"],
                "bytes": g["bytes"],
                "files": g["sizes"],
            }
            for name, g in plan["groups"].items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return path


def _token_from_env() -> str | None:
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    return dotenv_values(C.REPO_ROOT / ".env").get("HF_TOKEN") or None  # never printed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emx.i1a.hfget", description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="print planned bytes; download nothing")
    g.add_argument("--fetch", metavar="DIR", help="download into DIR ('cache' = the HF cache)")
    ap.add_argument("--plan-out", default=None, help=f"plan JSON path (default {PLAN_PATH})")
    args = ap.parse_args(argv)
    token = _token_from_env()
    if args.plan:
        plan = plan_all_i1a(token=token)
        out = write_plan(plan, args.plan_out)
        summary = {
            name: {"repo": g["repo"], "files": len(g["files"]), "bytes": g["bytes"]}
            for name, g in plan["groups"].items()
        }
        print(
            json.dumps(
                {
                    "groups": summary,
                    "total_bytes": plan["total_bytes"],
                    "total_gb": round(plan["total_bytes"] / 1e9, 3),
                    "cap_bytes": plan["cap_bytes"],
                    "plan_file": str(out),
                },
                indent=2,
            )
        )
        return 0
    local = None if args.fetch == "cache" else Path(args.fetch)
    got = fetch_all_i1a(local, token=token)
    write_plan(got["plan"], args.plan_out)
    print(
        json.dumps(
            {k: str(got[k]) for k in ("model_dir", "jlens", "olens_dir", "ar_dir", "whitener_dir")}
            | {"total_bytes": got["plan"]["total_bytes"]},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
