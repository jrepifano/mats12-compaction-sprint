"""Claim a probe id, or publish a probe's files, by pushing one commit straight to origin/main.

Why (2026-10-11): 10-20 probe agents (Claude and Codex) plan probes at once. Taking the next id
by reading `probes/INDEX.md` and committing in the shared main checkout let two agents take the
same id, and agents committing in one working tree trip over each other's staged files. Here an
id is taken by a push, which git makes atomic: of two agents that build a commit on the same tip,
only one push fast-forwards main; the other is rejected, refetches and takes the next id.

`claim` fetches `<remote>/<branch>` and computes the used ids as the union of
- the probe dirs in that tree (`probes/P<NN>-*`),
- the rows of that tree's `probes/INDEX.md`,
- `probes/P<NN>-*` dirs on disk in every local checkout (`git worktree list`), so a plan dir
  another session created but hasn't pushed is not handed out twice,
- the reserved ids P00, P98, P99.
It takes the lowest free id in P01-P97, writes `probes/<id>-<slug>/status.json` (Planned) and the
regenerated INDEX.md into a commit on top of the fetched tip, and pushes it (never forced). On a
rejection it starts again from the fetch, so the id can change; any other push failure (auth,
network, a hook) exits 1 at once with git's stderr. On success it prints one line,
`CLAIMED id=<id> dir=probes/<id>-<slug> commit=<sha>`.

`publish --id P04 FILE...` puts files from your checkout (each under that probe's dir, as it
exists on `<remote>/<branch>`) onto the branch the same way, retrying on rejection with the same
id. If one of them is the probe's `status.json`, it is validated and INDEX.md is regenerated in
the same commit. It prints `PUBLISHED id=<id> commit=<sha>`. Use it to land a plan or a status
change on main from a probe worktree without touching the shared main checkout.

Both build commits with git plumbing only (a temporary index file, `read-tree`, `hash-object`,
`update-index --cacheinfo`, `write-tree`, `commit-tree`). No working tree, real index or local
branch is touched; the only local ref written is the remote-tracking ref the fetch updates.

When the tree's INDEX.md has a row with no status.json, the status.json is backfilled from the
row in the same commit. A row that differs from its status.json is not rewritten from the row:
INDEX.md is generated from status.json, with a warning naming the row (see probe_index.py).

Usage:
  uv run python scripts/probe_claim.py claim --slug SLUG --question Q [--cap 75] [--owner p04]
      [--trailer "Co-Authored-By: ..."] [--remote origin] [--branch main] [--repo PATH]
      [--max-tries 5]
  uv run python scripts/probe_claim.py publish --id P04 FILE... [--trailer ...] [--remote origin]
      [--branch main] [--repo PATH] [--max-tries 5]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from emx import probes as P

INDEX = "probes/INDEX.md"


class GitError(RuntimeError):
    def __init__(self, args: list[str], proc: subprocess.CompletedProcess):
        self.proc = proc
        out = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        super().__init__(f"git {' '.join(args)} failed (rc={proc.returncode}): {out}")


class PushRejected(RuntimeError):
    pass


def git(
    repo: Path,
    *args: str,
    env: dict | None = None,
    input: str | bytes | None = None,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    """Every git call goes through here: `git -C <repo> ...`, captured, raising GitError."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=not binary,
        input=input,
        env={**os.environ, **env} if env else None,
        check=False,
    )
    if check and proc.returncode != 0:
        if binary:
            proc = subprocess.CompletedProcess(
                proc.args, proc.returncode, proc.stdout, proc.stderr.decode(errors="replace")
            )
        raise GitError(list(args), proc)
    return proc


def toplevel(repo: str | Path) -> Path:
    return Path(git(Path(repo), "rev-parse", "--show-toplevel").stdout.strip())


def tracking_ref(remote: str, branch: str) -> str:
    return f"refs/remotes/{remote}/{branch}"


def fetch(repo: Path, remote: str, branch: str, attempts: int = 3) -> str:
    """Fetch the branch into its remote-tracking ref and return the tip's sha.

    A few attempts, because concurrent fetches in one repo can briefly fail to lock the ref."""
    for i in range(attempts):
        try:
            git(repo, "fetch", "-q", remote, f"+refs/heads/{branch}:{tracking_ref(remote, branch)}")
            break
        except GitError:
            if i == attempts - 1:
                raise
            time.sleep(1 + i)
    return git(
        repo, "rev-parse", "--verify", f"{tracking_ref(remote, branch)}^{{commit}}"
    ).stdout.strip()


def push(repo: Path, remote: str, sha: str, branch: str) -> None:
    """Push `sha` to the branch, never forced. A lost race raises PushRejected; anything else
    raises GitError."""
    proc = git(repo, "push", "--porcelain", remote, f"{sha}:refs/heads/{branch}", check=False)
    if proc.returncode == 0:
        return
    out = proc.stdout + proc.stderr
    if lost_race(proc.stdout):
        raise PushRejected(out.strip())
    raise GitError(["push", remote, f"{sha}:refs/heads/{branch}"], proc)


# `git push --porcelain` writes one line per ref: `<flag>\t<src>:<dst>\t<summary> (<reason>)`, with
# flag `!` for a rejected ref. Only a ref that moved under us is a lost race; a hook or policy
# decline (`[remote rejected] (pre-receive hook declined)`) is fatal.
RACE_REJECTED = {"non-fast-forward", "fetch first", "stale info"}
RACE_REMOTE = re.compile(r"cannot lock ref|failed to update ref|incorrect old value|but expected")
PORCELAIN_RE = re.compile(r"^!\t[^\t]*\t(\[[a-z ]+\])(?: \((.*)\))?$")


def lost_race(stdout: str) -> bool:
    """True iff every rejected ref in the porcelain output was rejected because the ref moved."""
    rejected = [m for m in map(PORCELAIN_RE.match, stdout.splitlines()) if m]
    if not rejected:
        return False
    for m in rejected:
        summary, reason = m.group(1), m.group(2) or ""
        if summary == "[rejected]" and reason in RACE_REJECTED:
            continue
        if summary == "[remote rejected]" and RACE_REMOTE.search(reason):
            continue
        return False
    return True


# --- reading the fetched tree ---------------------------------------------------------------


def tree_probe_dirs(repo: Path, tip: str) -> list[str]:
    proc = git(repo, "ls-tree", "--name-only", tip, "probes/", check=False)
    if proc.returncode != 0:
        return []
    names = [PurePosixPath(n).name for n in proc.stdout.splitlines()]
    return [n for n in names if P.DIR_RE.match(n)]


def read_blob(repo: Path, tip: str, path: str) -> str | None:
    proc = git(repo, "cat-file", "-p", f"{tip}:{path}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def tree_statuses(repo: Path, tip: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in tree_probe_dirs(repo, tip):
        path = f"probes/{name}/status.json"
        text = read_blob(repo, tip, path)
        if text is None:
            continue
        s = P.loads_status(text, f"{tip[:10]}:{path}", name)
        if s["id"] in out:
            raise P.ProbeError(f"{tip[:10]}: two probe dirs claim {s['id']}")
        out[s["id"]] = s
    return out


def checkout_probe_dirs(repo: Path) -> set[str]:
    """`P<NN>-*` dir names on disk in every checkout of this repository."""
    names: set[str] = set()
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            d = Path(line[len("worktree ") :]) / "probes"
            if d.is_dir():
                names.update(p.name for p in d.iterdir() if p.is_dir())
    return {n for n in names if re.match(r"^P\d{2}-", n)}


def used_ids(repo: Path, tip: str) -> set[str]:
    index = read_blob(repo, tip, INDEX) or ""
    return (
        P.ids_in_names(tree_probe_dirs(repo, tip))
        | set(P.parse_index(index))
        | P.ids_in_names(checkout_probe_dirs(repo))
        | set(P.RESERVED)
    )


def index_files(repo: Path, tip: str, changed: list[dict]) -> dict[str, bytes]:
    """INDEX.md regenerated from the tree's status.json files with `changed` applied, plus any
    status.json backfilled from a row that has none. Returns {path: content}."""
    statuses = tree_statuses(repo, tip)
    statuses.update({s["id"]: s for s in changed})
    index = read_blob(repo, tip, INDEX) or ""
    rows = P.parse_index(index)
    missing, differing = P.drift(index, list(statuses.values()))
    files: dict[str, bytes] = {}
    for pid in missing:
        s = P.status_from_row(rows[pid])
        statuses[pid] = s
        files[f"probes/{P.dir_name(s)}/status.json"] = P.dumps_status(s).encode()
        print(f"backfilled probes/{P.dir_name(s)}/status.json from the INDEX.md row")
    changed_ids = {s["id"] for s in changed}
    for pid in differing:
        if pid not in changed_ids:
            print(
                f"warning: INDEX.md row {pid} differs from its status.json; "
                "regenerating it from status.json",
                file=sys.stderr,
            )
    files[INDEX] = P.render_index(list(statuses.values())).encode()
    return files


# --- building and pushing a commit ----------------------------------------------------------


def build_commit(repo: Path, tip: str, files: dict[str, tuple[str, bytes]], message: str) -> str:
    """A commit on top of `tip` with `files` ({path: (mode, content)}) added or replaced, built in
    a temporary index. Returns its sha, or `tip` if the tree would not change."""
    with tempfile.TemporaryDirectory(prefix="probe-claim-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", tip, env=env)
        for path, (mode, content) in sorted(files.items()):
            sha = git(repo, "hash-object", "-w", "--stdin", input=content, binary=True)
            blob = sha.stdout.decode().strip()
            git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        tree = git(repo, "write-tree", env=env).stdout.strip()
    if tree == git(repo, "rev-parse", f"{tip}^{{tree}}").stdout.strip():
        return tip
    return git(repo, "commit-tree", tree, "-p", tip, input=message).stdout.strip()


def message(subject: str, body: str, trailer: str | None) -> str:
    parts = [subject, "", body] if body else [subject]
    if trailer:
        parts += ["", trailer]
    return "\n".join(parts) + "\n"


def push_loop(repo: Path, remote: str, branch: str, max_tries: int, attempt) -> tuple:
    """Fetch, build with `attempt(tip) -> (sha, info)`, push; on a lost race, start again."""
    for n in range(1, max_tries + 1):
        tip = fetch(repo, remote, branch)
        sha, info = attempt(tip)
        if sha == tip:
            return sha, info, False
        try:
            push(repo, remote, sha, branch)
            return sha, info, True
        except PushRejected as e:
            print(f"push rejected (try {n}/{max_tries}), refetching: {e}", file=sys.stderr)
    raise PushRejected(f"still rejected after {max_tries} tries")


def claim(repo: Path, a) -> int:
    if not P.SLUG_RE.match(a.slug):
        print(f"error: slug {a.slug!r} is not [a-z0-9][a-z0-9-]*", file=sys.stderr)
        return 1

    # A token unique to this invocation goes into the commit message, so two identical claims
    # made in the same second never build the same commit (the second push would then report
    # "up to date" and both would think they own the id; review r1 finding 1).
    token = uuid.uuid4().hex

    def attempt(tip: str):
        pid = P.next_free_id(used_ids(repo, tip))
        s = P.new_status(pid, a.slug, a.question, a.cap, a.owner)
        files = {f"probes/{P.dir_name(s)}/status.json": P.dumps_status(s).encode()}
        files.update(index_files(repo, tip, [s]))
        body = f"{s['question']}\n\nClaim-Token: {token}"
        msg = message(f"Probe {pid} claimed: {a.slug}", body, a.trailer)
        return build_commit(repo, tip, {p: ("100644", c) for p, c in files.items()}, msg), s

    sha, s, _ = push_loop(repo, a.remote, a.branch, a.max_tries, attempt)
    d = P.dir_name(s)
    print(f"CLAIMED id={s['id']} dir=probes/{d} commit={sha}")
    print(f"next: git -C {repo} fetch {a.remote} {a.branch}")
    print(
        f"      git -C {repo} worktree add .claude/worktrees/{d.lower()} "
        f"-b probe/{d} {a.remote}/{a.branch}"
    )
    return 0


def repo_relative(repo: Path, f: str) -> str:
    """The path of `f` (absolute, or relative to the repo top level) relative to the repo."""
    if ".." in PurePosixPath(f).parts:
        raise P.ProbeError(f"{f}: '..' is not allowed")
    p = Path(f) if Path(f).is_absolute() else repo / f
    try:
        return p.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as e:
        raise P.ProbeError(f"{f} is outside {repo}") from e


def publish(repo: Path, a) -> int:
    if not P.ID_RE.match(a.id):
        print(f"error: id {a.id!r} is not P<two digits>", file=sys.stderr)
        return 1
    rels = [repo_relative(repo, f) for f in a.files]
    for f, rel in zip(a.files, rels):
        if not (repo / rel).is_file():
            raise P.ProbeError(f"{f}: not a file")

    def attempt(tip: str):
        dirs = [n for n in tree_probe_dirs(repo, tip) if n.startswith(f"{a.id}-")]
        if len(dirs) != 1:
            raise P.ProbeError(
                f"{a.remote}/{a.branch} has {len(dirs)} dirs for {a.id} ({dirs}); claim it first"
            )
        prefix = f"probes/{dirs[0]}/"
        files: dict[str, tuple[str, bytes]] = {}
        changed: list[dict] = []
        for rel in rels:
            if not rel.startswith(prefix) or rel == INDEX:
                raise P.ProbeError(f"{rel} is not under {prefix}")
            content = (repo / rel).read_bytes()
            mode = "100755" if os.access(repo / rel, os.X_OK) else "100644"
            files[rel] = (mode, content)
            if rel == prefix + "status.json":
                changed.append(P.loads_status(content.decode(), rel, dirs[0]))
        if changed:
            files.update({p: ("100644", c) for p, c in index_files(repo, tip, changed).items()})
        msg = message(f"Probe {a.id}: publish {len(rels)} file(s)", "", a.trailer)
        return build_commit(repo, tip, files, msg), None

    sha, _, pushed = push_loop(repo, a.remote, a.branch, a.max_tries, attempt)
    if not pushed:
        print(f"nothing to publish: {a.remote}/{a.branch} already has these files")
    print(f"PUBLISHED id={a.id} commit={sha}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--slug", required=True)
    c.add_argument("--question", required=True)
    c.add_argument("--cap", type=float, default=None)
    c.add_argument("--owner", default=None)
    pb = sub.add_parser("publish")
    pb.add_argument("--id", required=True)
    pb.add_argument("files", nargs="+")
    for s in (c, pb):
        s.add_argument("--trailer", default=None)
        s.add_argument("--remote", default="origin")
        s.add_argument("--branch", default="main")
        s.add_argument("--repo", default=".")
        s.add_argument("--max-tries", type=int, default=5)
    a = ap.parse_args(argv)
    try:
        repo = toplevel(a.repo)
        return claim(repo, a) if a.cmd == "claim" else publish(repo, a)
    except (P.ProbeError, GitError, PushRejected) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
