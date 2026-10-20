"""Query public ECR (anonymous OCI distribution API) for image platforms and compressed sizes.

No pulls: only manifests and config blobs are fetched (a few KB per image). Layer digests are
returned so shared layers (all task images build FROM mars-base) can be deduplicated when
estimating disk and download totals.

Usage:
    uv run python -m compaction.deepswe.registry --tasks data/raw/sprint/deepswe/repo/tasks \
        --out data/raw/sprint/deepswe/images.json
"""

from __future__ import annotations

import argparse
import functools
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

INDEX_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
ACCEPT = ", ".join([
    *INDEX_TYPES,
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
UA = "Mozilla/5.0 (emx sprint-prep deepswe registry probe)"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """'host/repo/path:tag' -> (host, repo/path, tag)."""
    host, _, rest = ref.partition("/")
    repo, _, tag = rest.rpartition(":")
    if not repo or "/" in tag:
        repo, tag = rest, "latest"
    return host, repo, tag


def _get(url: str, token: str | None, accept: str | None = None, tries: int = 6
         ) -> tuple[bytes, dict]:
    """GET with backoff on 429/5xx (public ECR throttles anonymous bursts)."""
    for i in range(tries):
        try:
            return _get_once(url, token, accept)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or i == tries - 1:
                raise
            time.sleep(min(30.0, 1.5 * 2**i))
    raise AssertionError("unreachable")


def _get_once(url: str, token: str | None, accept: str | None) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), dict(r.headers)


@functools.cache
def ecr_token(host: str, repo: str) -> str:
    body, _ = _get(f"https://{host}/token/?scope=repository:{repo}:pull", None)
    return json.loads(body)["token"]


def summarise_manifest(manifest: dict, config: dict | None) -> dict:
    layers = manifest.get("layers", [])
    return {
        "arch": (config or {}).get("architecture"),
        "os": (config or {}).get("os"),
        "compressed_bytes": sum(int(x.get("size", 0)) for x in layers),
        "n_layers": len(layers),
        "layers": [[x["digest"], int(x.get("size", 0))] for x in layers],
    }


def inspect_image(ref: str) -> dict:
    host, repo, tag = parse_ref(ref)
    tok = ecr_token(host, repo)
    base = f"https://{host}/v2/{repo}"
    body, _ = _get(f"{base}/manifests/{tag}", tok, ACCEPT)
    man = json.loads(body)
    out: dict = {"ref": ref, "media_type": man.get("mediaType")}
    if man.get("mediaType") in INDEX_TYPES or "manifests" in man:
        out["platforms"] = [
            f"{m['platform']['os']}/{m['platform']['architecture']}" for m in man["manifests"]
        ]
        # Size of the amd64 variant (what the tasks were graded on).
        amd = next(m for m in man["manifests"] if m["platform"]["architecture"] == "amd64")
        body, _ = _get(f"{base}/manifests/{amd['digest']}", tok, ACCEPT)
        man = json.loads(body)
    cfg_body, _ = _get(f"{base}/blobs/{man['config']['digest']}", tok)
    s = summarise_manifest(man, json.loads(cfg_body))
    out.update(s)
    out.setdefault("platforms", [f"{s['os']}/{s['arch']}"])
    return out


def dedup_bytes(images: list[dict]) -> int:
    seen: dict[str, int] = {}
    for im in images:
        for digest, size in im.get("layers", []):
            seen[digest] = size
    return sum(seen.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args(argv)
    from compaction.deepswe.tasks import load_tasks

    refs = sorted({t["docker_image"] for t in load_tasks(a.tasks).values()})
    refs.append("public.ecr.aws/x8v8d7g8/mars-base:latest")

    def safe(ref: str) -> dict:
        try:
            return inspect_image(ref)
        except Exception as e:  # noqa: BLE001 - report and keep going; one bad tag shouldn't stop the survey
            return {"ref": ref, "error": repr(e)}

    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(safe, refs))
    Path(a.out).write_text(json.dumps({r["ref"]: r for r in res}, indent=1))
    ok = [r for r in res if "error" not in r]
    print(f"{len(ok)}/{len(res)} inspected; errors: {[r['ref'] for r in res if 'error' in r]}")
    tasks_only = [r for r in ok if "mars-base" not in r["ref"]]
    print("platforms:", sorted({",".join(r["platforms"]) for r in tasks_only}))
    sizes = sorted(r["compressed_bytes"] for r in tasks_only)
    if sizes:
        print(f"compressed GB: min {sizes[0]/1e9:.2f} median {sizes[len(sizes)//2]/1e9:.2f} "
              f"max {sizes[-1]/1e9:.2f} sum {sum(sizes)/1e9:.1f} "
              f"dedup {dedup_bytes(tasks_only)/1e9:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
