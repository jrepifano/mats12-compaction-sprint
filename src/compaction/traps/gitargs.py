"""Parse the git command lines recorded by the shim (push and branch deletion).

Only what the trap predicates need: which subcommand, whether a push forces (and how), which
refs it deletes, and which local branches `git branch` deletes. Handles global options
(`-C dir`, `-c k=v`, `--git-dir=...`), bundled short flags (`-fu`, `-Dr`), `+refspec`,
`:dst` deletions and `--`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Global options that take a separate value (`git -C dir ...`).
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                      "--super-prefix", "--config-env", "--list-cmds", "--attr-source"}


def split_git(argv: list) -> tuple:
    """(global_opts, subcommand or None, subcommand args) from a full git argv."""
    args = list(argv[1:])
    globals_ = []
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            return globals_, a, args[i + 1:]
        globals_.append(a)
        if a in _GLOBAL_WITH_VALUE and i + 1 < len(args):
            globals_.append(args[i + 1])
            i += 2
            continue
        i += 1
    return globals_, None, []


@dataclass
class PushArgs:
    force_flags: list = field(default_factory=list)  # "force", "force-with-lease", "plus-refspec"
    delete: bool = False
    dry_run: bool = False
    mirror: bool = False
    prune: bool = False
    all_branches: bool = False
    repo: str | None = None
    refspecs: list = field(default_factory=list)
    forced_refs: list = field(default_factory=list)  # dst names of +refspecs
    deleted_refs: list = field(default_factory=list)  # from --delete names and :dst refspecs

    @property
    def forces(self) -> bool:
        return bool(self.force_flags)

    @property
    def flavor(self) -> str | None:
        """'force' if any plain force is used, else 'force-with-lease', else None."""
        if not self.force_flags:
            return None
        if "force" in self.force_flags or "plus-refspec" in self.force_flags:
            return "force"
        return "force-with-lease"


# Long options of `git push` that consume the next argument when given without `=`.
_PUSH_LONG_WITH_VALUE = {"--push-option", "--repo", "--receive-pack", "--exec"}
# Short options of `git push` that take a value (rest of the cluster or next arg).
_PUSH_SHORT_WITH_VALUE = {"o"}


def _dst(refspec: str) -> str:
    """Destination branch name of a refspec, without refs/heads/."""
    spec = refspec.lstrip("+")
    dst = spec.split(":", 1)[1] if ":" in spec else spec
    if dst.startswith("refs/heads/"):  # noqa: FURB188 (no removeprefix in Python 3.7)
        dst = dst[len("refs/heads/"):]
    return dst


def parse_push(args: list) -> PushArgs:
    """Parse the arguments after `git push`."""
    p = PushArgs()
    positional = []
    i = 0
    only_positional = False
    while i < len(args):
        a = args[i]
        i += 1
        if only_positional or not a.startswith("-") or a == "-":
            positional.append(a)
            continue
        if a == "--":
            only_positional = True
            continue
        if a.startswith("--"):
            name, _, _ = a.partition("=")
            if name == "--force":
                p.force_flags.append("force")
            elif name == "--no-force":
                p.force_flags = [f for f in p.force_flags if f != "force"]
            elif name == "--force-with-lease":
                p.force_flags.append("force-with-lease")
            elif name == "--no-force-with-lease":
                p.force_flags = [f for f in p.force_flags if f != "force-with-lease"]
            elif name == "--delete":
                p.delete = True
            elif name == "--dry-run":
                p.dry_run = True
            elif name == "--mirror":
                p.mirror = True
            elif name == "--prune":
                p.prune = True
            elif name in ("--all", "--branches"):
                p.all_branches = True
            elif name == "--repo" and "=" in a:
                p.repo = a.split("=", 1)[1]
            elif name in _PUSH_LONG_WITH_VALUE and "=" not in a:
                if name == "--repo" and i < len(args):
                    p.repo = args[i]
                i += 1
            continue
        # short-option cluster, e.g. -f, -fu, -uf, -d, -n, -oX, -o X
        cluster = a[1:]
        j = 0
        while j < len(cluster):
            c = cluster[j]
            if c == "f":
                p.force_flags.append("force")
            elif c == "d":
                p.delete = True
            elif c == "n":
                p.dry_run = True
            elif c in _PUSH_SHORT_WITH_VALUE:
                if j == len(cluster) - 1:
                    i += 1  # value is the next argument
                break  # rest of the cluster is the value
            j += 1
    if positional and p.repo is None:
        p.repo = positional[0]
        positional = positional[1:]
    p.refspecs = positional
    for spec in positional:
        if p.delete or spec.startswith(":") and len(spec) > 1:
            p.deleted_refs.append(_dst(spec))
        elif spec.startswith("+"):
            p.forced_refs.append(_dst(spec))
    if p.forced_refs and "plus-refspec" not in p.force_flags:
        p.force_flags.append("plus-refspec")
    return p


@dataclass
class BranchDelete:
    names: list = field(default_factory=list)
    force: bool = False  # -D or --force
    remote_tracking: bool = False  # -r: deletes refs/remotes/*, not a local branch


def parse_branch_delete(args: list) -> BranchDelete | None:
    """Parse the arguments after `git branch`; None unless it deletes branches."""
    d = BranchDelete()
    deleting = False
    names = []
    only_positional = False
    for a in args:
        if only_positional or not a.startswith("-"):
            names.append(a)
            continue
        if a == "--":
            only_positional = True
            continue
        if a.startswith("--"):
            if a == "--delete":
                deleting = True
            elif a == "--force":
                d.force = True
            elif a == "--remotes":
                d.remote_tracking = True
            continue
        for c in a[1:]:
            if c == "d":
                deleting = True
            elif c == "D":
                deleting = True
                d.force = True
            elif c == "f":
                d.force = True
            elif c == "r":
                d.remote_tracking = True
    if not deleting:
        return None
    d.names = names
    return d
