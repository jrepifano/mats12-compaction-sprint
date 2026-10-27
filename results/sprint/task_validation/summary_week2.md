> **Week-2 additions (2026-10-27, 9 DeepSWE images).** The 40 images in `summary.md` are not repeated here. Run with `scripts/validate_task_images.py` unchanged (same scanner version), through a wrapper that only sets the task list (week2_exp1: tengo x2 + httpx-multipart; week2_exp4: obsidian-linter x3, bandit x2, koota-entity-snapshot-rollback, httpx-multipart), points DEEPSWE_DIR at a copy of those 9 task dirs (`data/raw/sprint/task_validation/_deepswe_src/`, copied from the sprint-prep worktree's DeepSWE checkout at 0b9fabbb) and writes this summary to a separate file. Wrapper: `data/raw/sprint/task_validation/run_week2.py`. Single-needle hits: bandit-interprocedural 15 x `visit_AsyncFunctionDef` (Python stdlib ast/inspect/pyclbr, setuptools' vendored typeguard), tengo-destructuring 1 x `parseArrayPattern` (yarn's bundled cli.js): benign library names.

# Task image validation

Generated 2026-10-27T07:01:04Z by `scripts/validate_task_images.py summary` (scanner c06cd3d118db-s8i8t5). Brief: `logs/s2/briefs/task-image-validation.md`.

`verdict` = what an agent in the container can reach: LEAK if the probe or a code-confirmed red-team find says so, CLEAN only if the probe is CLEAN and the red-team ran and found nothing, else ERROR. `layers` = solution text in the image's layer tarballs, git objects inside layers decoded. `hidden tests` = the verifier's added test lines found in the container.

## Counts

- **deepswe**: verdict CLEAN 9; probe CLEAN 9; layers CLEAN 9; redteam CLEAN 9; hidden_tests CLEAN 9

## Per image

| key | verdict | probe | layers | red-team (calls) | hidden tests | why |
|---|---|---|---|---|---|---|
| deepswe/bandit-incremental-cache-control | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| deepswe/bandit-interprocedural-taint-checks | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 15 single-needle hit(s) for review |
| deepswe/httpx-multipart-response-parsing | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/koota-entity-snapshot-rollback | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/obsidian-linter-auto-table-of-contents | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/obsidian-linter-link-format-conversion | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| deepswe/obsidian-linter-scoped-ignore-markers | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/tengo-callable-instance-isolation | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/tengo-destructuring-bindings | CLEAN | CLEAN | CLEAN | CLEAN (13) | CLEAN | 1 single-needle hit(s) for review |

## What CLEAN means, and its limits

CLEAN means all of these passed on the exact image id:

- **Probe.** Run inside a fresh container (no network), it checks:
  - every git object store on the filesystem (refs, reflogs, stash, notes, packed refs, special
    heads, remotes, alternates; commits outside HEAD and unreachable objects; every object's
    content, and archives committed into history, decoded);
  - the whole filesystem except /proc, /sys and /dev, with archives found by signature and
    decoded recursively;
  - `git apply --check` of the gold patch in both directions;
  - stray patch files and the image config.
- **Detectors.** Up to 8 solution lines, 8 new names and 5 hidden-test lines, each absent from the
  HEAD tree. A location holding 2 or more distinct needles, or a solution line in an object HEAD
  can't reach, is a LEAK. A location with one needle, an undecodable archive outside
  toolchain/dependency fixtures, and a patch with too few distinctive lines are review items.
  Each is cleared by hand in `data/raw/sprint/task_validation/weak_review.json`, bound to the
  image id.
- **Red-team.** A Claude Code agent (Sonnet 5.5 on Max) gets only a container shell. It must make
  at least 10 successful calls over a checklist; up to 3 nudges re-send it when it stops early. A
  claimed find is re-run and checked by code.
- **New-file paths.** No file in the image mentions a path that only the gold patch creates, or
  every mention is reviewed. This catches build directories and caches made from the solved tree.

Limitations, accepted after astra review round 3:

| Limit | Status |
|---|---|
| CLEAN is bounded search evidence, not proof of semantic absence | A reformatted solution with none of the selected names would pass the deterministic layers; the red-team is the backstop |
| Git topology | Gitfile/commondir layouts are covered through the object stores they point to; the main repo must be a `.git` dir |
| Layer scan | Covers raw layer text and the git objects inside each layer. Pack/index split across layers is not reconstructed, and nested archives in lower layers are not decoded. Not reachable from inside the container |
| zstd/7z/rpm and archives nested deeper than 4 levels | Not decoded; review items unless toolchain/dependency fixtures |
| Symlinks | Not followed; they alias files already scanned |


## Single-needle hits (for manual review)

A location holding one needle is not a verdict (a generic line or a stock name can match an unrelated package). Solution-line (S) hits are listed one by one; identifier (I) and test (T) hits are counted.

- **deepswe/bandit-interprocedural-taint-checks**: 15 (S 0, I 15, T 0)
- **deepswe/tengo-destructuring-bindings**: 1 (S 0, I 1, T 0)
