#!/usr/bin/env bash
# Pull an E1 pod's results (/ws/emx/results/E1/) into a fresh staging directory, verify every
# file against the named completion manifest(s) (sha256), and only then promote into
# results/E1/<SUBDIR>. Nothing is promoted from a pull whose manifests do not all verify.
# Port of ../mats12_neel_working/scripts/pod_pull_results.sh. Changes: E1 paths; flags instead
# of positional slot; `shasum -a 256 -c` (always on macOS) instead of sha256sum; every manifest
# matched must verify, and at least one must match; the manifests' own sha256 are printed for
# the report.
# Usage: bash scripts/pod_pull_results.sh --slot _A --manifest 'train/*.sha256' [--subdir DIR]
#   --manifest is a glob relative to results/E1 on the pod; *partial* manifests never count.
#
# 2026-09-29 (I1a integrated review r2): `--exp` (default E1; E1 behaviour above is unchanged).
#   bash scripts/pod_pull_results.sh --exp I1a --slot _A
# pulls /ws/emx/results/I1a/ into a fresh staging dir results/I1a/staging/pull_XXXXXX/
# (gitignored), then, from the staging root (manifest paths are repo-relative):
#   1. verifies the closing manifest results/I1a/MANIFEST.manifest.sha256;
#   2. verifies every step manifest (results/I1a/MANIFEST.<step>.sha256) that it lists; it must
#      list at least one;
#   3. refuses if the closing sidecar MANIFEST.manifest.json is missing, or if it or any staged
#      MANIFEST.*.json sidecar says "limited": true (a cut-down test run);
# and only then promotes the staged tree into results/I1a/. POD_PULL_SOURCE=<local dir> replaces
# the rsync source (tests; no SSH).
#
# 2026-10-03 (E1b implementation plan §1.7, §2 U7, §6): `--exp E1b`, pulled by manifest, never by
# tree, so a run still being written on the pod is never half-promoted:
#   bash scripts/pod_pull_results.sh --exp E1b --slot _A --manifest MANIFEST.part3.sha256 ...
#   bash scripts/pod_pull_results.sh --exp E1b --slot _A --run pre-plain-bad-R2-s2 ...
#   bash scripts/pod_pull_results.sh --exp E1b --slot _A --staging part1
# --manifest NAME (repeatable; a name or glob of a top-level manifest in results/E1b, never a
# *partial* one) and --run CELL (= MANIFEST.run-CELL.sha256) may be combined; each must match a
# manifest on the pod. The pod's top-level manifests are fetched first, then exactly the files the
# chosen ones list, into a fresh staging dir (results/E1b/.pull_XXXXXX); every listed sha256 is
# verified locally, and only then are those files and manifests promoted into results/E1b/.
# Manifest paths may be repo-relative (results/E1b/...) or relative to the manifest's directory
# (one convention per manifest; absolute paths and .. are refused). --staging MODE pulls a failed
# mode's results/E1b/_staging_MODE/ whole, verifies its MANIFEST.sha256, and keeps it as
# results/E1b/_staging_MODE_<UTC>/; one that does not verify is still kept (never discarded), as
# _staging_MODE_<UTC>_UNVERIFIED/, and the exit is 1. POD_PULL_SOURCE=<dir> stands for the pod's
# /ws/emx (tests; no SSH).
#
# 2026-10-04 (I9 implementation plan §2 U6, §4 P1/P3, §6): `--exp I9`, pulled by manifest, never by
# tree (so a step still writing on the pod is never half-promoted), and never terminating:
#   bash scripts/pod_pull_results.sh --exp I9 --slot _A                       # end of a session
#   bash scripts/pod_pull_results.sh --exp I9 --slot _A --manifest MANIFEST.smoke.sha256  # mid-session
# Without --manifest it takes the closing manifest results/I9/MANIFEST.manifest.sha256 (written by
# scripts/i9_pod.py's `manifest` step) and every step manifest (results/I9/MANIFEST.<step>.sha256)
# it lists; it must list at least one, and every other top-level step manifest on the pod (a
# closing manifest older than a step, e.g. a later `--unsealed` run, is refused as stale; review
# r2 finding 1). --manifest NAME (repeatable) takes exactly the named
# top-level step manifests instead. Every manifest uses `shasum -a 256` lines with repo-relative
# paths under results/I9/ (absolute paths and .. are refused). The pod's top-level manifests and
# results/I9/manifests/*.json are fetched first, then exactly the files the chosen manifests
# list, into a fresh staging dir (results/I9/.pull_XXXXXX); every manifest is verified locally,
# and each chosen step must have its Manifest record results/I9/manifests/<step>.json with that
# "step". Only then are the listed files, the manifests and their records promoted into
# results/I9/. A local file that a promotion would change (e.g. session A's download or closing
# manifest when session B's arrive) is first moved to results/I9/superseded/<UTC>/, never
# discarded. POD_PULL_SOURCE=<dir> stands for the pod's /ws/emx (tests; no SSH).
#
# 2026-10-06 (E3 implementation plan U8c; prereg §8.6 cleanup step 1): `--exp E3`.
#   bash scripts/pod_pull_results.sh --exp E3 --slot _A [--unit pod1]
# 1. mirrors the pod's whole /ws/emx/results/E3/ (sealed/ included; *.safetensors excluded, the
#    adapters live on the Hub) and /ws/emx/logs/ into a per-slot raw mirror
#    results/E3/sealed/_pod_mirror/<slot>/{results/E3,logs}/. The mirror is incremental (a retry
#    re-sends only what changed), kept whatever happens (the pod may be terminated next), and lives
#    under SEALED_DIR so that nothing in it is read as unsealed data. This includes everything
#    scripts/pod_run_e3.sh writes: smoke outputs (results/E3/smoke/, results/E3/sealed/smoke/),
#    the failure staging dirs results/E3/_staging_<unit>/ and the job logs
#    logs/pod_run_e3.<unit>.<UTC>.log (also copied to results/E3/logs/ by the job);
# 2. verifies locally, in the mirror, the unit's manifest results/E3/MANIFEST.<unit>.sha256
#    (contracts.manifest_path), or without --unit every top-level manifest now on the pod (at least
#    one); never a *partial* one. Lines are `shasum -a 256` lines whose paths are all
#    repo-relative (results/E3/...) or all relative to the manifest's directory; absolute paths,
#    .., paths outside results/E3 and paths into the mirror are refused;
# 3. only then promotes the listed files and the manifest(s) into results/E3/ (a local file that
#    would change is first moved to results/E3/superseded/<UTC>/, never discarded).
# Exit 1 when the pull or any verification fails (nothing promoted; e3_orchestrate.py cleanup
# retries up to contracts.RETRIEVAL_ATTEMPTS times), 2 on usage errors. POD_PULL_SOURCE=<dir>
# stands for the pod's /ws/emx (tests; no SSH).
set -euo pipefail
cd "$(dirname "$0")/.."
SLOT=""; MGLOB=""; SUBDIR=""; EXP="E1"; MLIST=""; RUNS=""; STAGING=""; UNIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --exp) EXP="${2:?}"; shift 2 ;;
    --slot) SLOT="${2:?}"; shift 2 ;;
    --manifest) MGLOB="${2:?}"; MLIST="$MLIST ${2}"; shift 2 ;;
    --run) RUNS="$RUNS ${2:?}"; shift 2 ;;
    --staging) STAGING="${2:?}"; shift 2 ;;
    --subdir) SUBDIR="${2:?}"; shift 2 ;;
    --unit) UNIT="${2:?}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
if [ "$EXP" != "E1b" ] && [ -n "$RUNS$STAGING" ]; then
  echo "--run/--staging are E1b-only" >&2; exit 2
fi
if [ "$EXP" != "E3" ] && [ -n "$UNIT" ]; then
  echo "--unit is E3-only" >&2; exit 2
fi
case "$EXP" in
  E1) ;;
  I1a)
    [ -n "$SLOT" ] || [ -n "${POD_PULL_SOURCE:-}" ] || { echo "usage: --exp I1a --slot _A" >&2; exit 2; }
    [ -z "$MGLOB$SUBDIR" ] || { echo "--manifest/--subdir are E1-only (I1a uses the closing manifest)" >&2; exit 2; }
    mkdir -p results/I1a/staging
    STAGE=$(mktemp -d "results/I1a/staging/pull_XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/results/I1a"
    if [ -n "${POD_PULL_SOURCE:-}" ]; then
      rsync -rlpt --exclude staging "$POD_PULL_SOURCE/" "$STAGE/results/I1a/"
    else
      envval() { grep -m1 "^$1=" .env | cut -d= -f2- ; }
      H=$(envval "POD_SSH_HOST$SLOT" || true); P=$(envval "POD_SSH_PORT$SLOT" || true)
      U=$(envval POD_SSH_USER || true); U=${U:-root}
      [ -n "$H" ] && [ -n "$P" ] || { echo "no pod in slot '$SLOT'" >&2; exit 1; }
      rsync -rlptz --exclude staging -e "ssh -p $P -o StrictHostKeyChecking=accept-new" \
            "$U@$H:/ws/emx/results/I1a/" "$STAGE/results/I1a/"
    fi
    CLOSING=results/I1a/MANIFEST.manifest.sha256
    ( cd "$STAGE"
      [ -s "$CLOSING" ] || { echo "no closing manifest $CLOSING - the run did not finish; nothing promoted" >&2; exit 1; }
      echo "== verifying $CLOSING ($(shasum -a 256 "$CLOSING" | cut -d' ' -f1))"
      shasum -a 256 -c --quiet "$CLOSING" \
        || { echo "$CLOSING does NOT verify - nothing promoted" >&2; exit 1; }
      STEPMS=$(sed -n 's|^[0-9a-f]\{64\}  \**\(results/I1a/MANIFEST\.[^/]*\.sha256\)$|\1|p' "$CLOSING")
      [ -n "$STEPMS" ] || { echo "$CLOSING lists no step manifest - nothing promoted" >&2; exit 1; }
      for m in $STEPMS; do
        echo "== verifying $m ($(shasum -a 256 "$m" | cut -d' ' -f1))"
        shasum -a 256 -c --quiet "$m" || { echo "$m does NOT verify - nothing promoted" >&2; exit 1; }
      done
      [ -s results/I1a/MANIFEST.manifest.json ] \
        || { echo "no closing sidecar MANIFEST.manifest.json - nothing promoted" >&2; exit 1; }
      python3 - results/I1a/MANIFEST.*.json <<'PY' || { echo "nothing promoted" >&2; exit 1; }
import json
import sys

bad = []
for path in sys.argv[1:]:
    try:
        d = json.load(open(path))
    except (OSError, ValueError) as e:
        bad.append(f"{path}: unreadable ({e})")
        continue
    if not isinstance(d, dict) or d.get("limited", False) is not False:
        bad.append(f"{path}: limited={d.get('limited') if isinstance(d, dict) else d!r}")
if bad:
    print("limited or unreadable sidecars:\n  " + "\n  ".join(bad), file=sys.stderr)
    sys.exit(1)
PY
    )
    rsync -rlpt "$STAGE/results/I1a/" results/I1a/
    echo "promoted to results/I1a (closing manifest and its step manifests verified)"
    exit 0 ;;
  E1b)
    [ -n "$SLOT" ] || [ -n "${POD_PULL_SOURCE:-}" ] || { echo "usage: --exp E1b --slot _A ..." >&2; exit 2; }
    [ -z "$SUBDIR" ] || { echo "--subdir is E1-only" >&2; exit 2; }
    [ -n "$MLIST$RUNS$STAGING" ] || { echo "E1b needs --manifest NAME, --run CELL or --staging MODE" >&2; exit 2; }
    [ -z "$STAGING" ] || [ -z "$MLIST$RUNS" ] || { echo "--staging is pulled on its own" >&2; exit 2; }
    for c in $RUNS; do
      case "$c" in *[!A-Za-z0-9._-]*) echo "bad cell '$c'" >&2; exit 2 ;; esac
      MLIST="$MLIST MANIFEST.run-$c.sha256"
    done
    for m in $MLIST; do
      case "$m" in
        *partial*) echo "a partial manifest is never a completion manifest" >&2; exit 2 ;;
        MANIFEST.*.sha256) ;;
        *) echo "--manifest must name a top-level results/E1b/MANIFEST.*.sha256 (got '$m')" >&2; exit 2 ;;
      esac
      case "$m" in *[!A-Za-z0-9._*?@-]*) echo "unsafe manifest name '$m'" >&2; exit 2 ;; esac
    done
    case "$STAGING" in *[!A-Za-z0-9._-]*) echo "bad staging mode '$STAGING'" >&2; exit 2 ;; esac
    if [ -n "${POD_PULL_SOURCE:-}" ]; then
      SRC_ROOT="$POD_PULL_SOURCE"
      rsync_pod() { rsync "$@"; }
    else
      envval() { { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }
      H=$(envval "POD_SSH_HOST$SLOT"); P=$(envval "POD_SSH_PORT$SLOT")
      U=$(envval POD_SSH_USER); U=${U:-root}
      [ -n "$H" ] && [ -n "$P" ] || { echo "no pod in slot '$SLOT'" >&2; exit 1; }
      SRC_ROOT="$U@$H:/ws/emx"
      rsync_pod() { rsync -z -e "ssh -p $P -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new" "$@"; }
    fi
    # manifest_paths BASE_OUT MANIFEST...: each manifest's listed files as repo-relative paths
    # (stdout), and per manifest the directory its paths are relative to (BASE_OUT: "<manifest>
    # <dir>" lines). Refuses malformed lines, absolute paths, .., paths outside results/E1b, and a
    # manifest mixing repo-relative and manifest-relative paths.
    manifest_paths() {
      python3 - "$@" <<'PY'
import re
import sys

base_out, manifests = sys.argv[1], sys.argv[2:]
line_re = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
bases = []
for m in manifests:
    mdir = m.rsplit("/", 1)[0]
    paths = []
    for i, line in enumerate(open(m).read().splitlines(), 1):
        if not line.strip():
            continue
        hit = line_re.match(line)
        if not hit:
            sys.exit(f"{m}:{i}: not a sha256 manifest line")
        paths.append(hit.group(2))
    if not paths:
        sys.exit(f"{m}: lists no file")
    repo = [p.startswith("results/") for p in paths]
    if any(repo) and not all(repo):
        sys.exit(f"{m}: mixes repo-relative and manifest-relative paths")
    base = "." if all(repo) else mdir
    bases.append(f"{m} {base}")
    for p in paths:
        rel = p if base == "." else f"{mdir}/{p}"
        parts = rel.split("/")
        if p.startswith("/") or ".." in parts or not rel.startswith("results/E1b/"):
            sys.exit(f"{m}: refused path {p!r}")
        print(rel)
open(base_out, "w").write("\n".join(bases) + "\n")
PY
    }
    mkdir -p results/E1b
    STAGE=$(mktemp -d "results/E1b/.pull_XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT
    if [ -n "$STAGING" ]; then
      REL="results/E1b/_staging_$STAGING"
      mkdir -p "$STAGE/$REL"
      rsync_pod -rlpt "$SRC_ROOT/$REL/" "$STAGE/$REL/" \
        || { echo "could not pull $REL from the pod" >&2; exit 1; }
      KEEP="results/E1b/_staging_${STAGING}_$(date -u +%Y%m%dT%H%M%SZ)"
      ok=0
      if [ -s "$STAGE/$REL/MANIFEST.sha256" ] \
         && ( cd "$STAGE" && manifest_paths .bases "$REL/MANIFEST.sha256" > /dev/null ); then
        base=$(cut -d' ' -f2 "$STAGE/.bases")
        echo "== verifying $REL/MANIFEST.sha256 ($(shasum -a 256 "$STAGE/$REL/MANIFEST.sha256" | cut -d' ' -f1))"
        if [ "$base" = "." ]; then
          ( cd "$STAGE" && shasum -a 256 -c --quiet "$REL/MANIFEST.sha256" ) && ok=1
        else
          ( cd "$STAGE/$REL" && shasum -a 256 -c --quiet MANIFEST.sha256 ) && ok=1
        fi
      fi
      [ "$ok" = "1" ] || KEEP="${KEEP}_UNVERIFIED"
      mkdir -p "$KEEP"
      rsync -rlpt "$STAGE/$REL/" "$KEEP/"
      if [ "$ok" = "1" ]; then echo "staged $KEEP (MANIFEST.sha256 verified)"; exit 0; fi
      echo "STAGING DID NOT VERIFY (missing or bad MANIFEST.sha256); kept as $KEEP" >&2
      exit 1
    fi
    # 1. every top-level manifest (small), then the chosen ones
    rsync_pod -rlpt --include='MANIFEST.*.sha256' --exclude='*' "$SRC_ROOT/results/E1b/" "$STAGE/results/E1b/" \
      || { echo "could not list the pod's manifests" >&2; exit 1; }
    SEL=""
    for pat in $MLIST; do
      hit=0
      for m in "$STAGE"/results/E1b/$pat; do
        [ -f "$m" ] || continue
        rel=${m#"$STAGE"/}
        case "$rel" in *partial*) continue ;; esac
        case " $SEL " in *" $rel "*) ;; *) SEL="$SEL $rel" ;; esac
        hit=1
      done
      [ "$hit" = "1" ] || { echo "NO manifest $pat on the pod - not finished; nothing promoted" >&2; exit 1; }
    done
    # 2. exactly the files they list
    # shellcheck disable=SC2086 # SEL holds repo-relative manifest paths without spaces
    ( cd "$STAGE" && manifest_paths .bases $SEL ) > "$STAGE/.files" \
      || { echo "a manifest was refused - nothing promoted" >&2; exit 1; }
    rsync_pod -lpt --files-from="$STAGE/.files" "$SRC_ROOT/" "$STAGE/" \
      || { echo "could not pull the listed files - nothing promoted" >&2; exit 1; }
    # 3. verify every manifest locally
    while read -r m base; do
      echo "== verifying $m ($(shasum -a 256 "$STAGE/$m" | cut -d' ' -f1))"
      if [ "$base" = "." ]; then
        ( cd "$STAGE" && shasum -a 256 -c --quiet "$m" )
      else
        ( cd "$STAGE/$base" && shasum -a 256 -c --quiet "${m#"$base"/}" )
      fi || { echo "MANIFEST $m does NOT verify - nothing promoted" >&2; exit 1; }
    done < "$STAGE/.bases"
    # 4. promote the verified manifests and their files only
    NF=$(wc -l < "$STAGE/.files" | tr -d ' ')
    for m in $SEL; do echo "$m" >> "$STAGE/.files"; done
    rsync -lpt --files-from="$STAGE/.files" "$STAGE/" ./
    # shellcheck disable=SC2086
    echo "promoted to results/E1b ($(echo $SEL | wc -w | tr -d ' ') manifest(s) and the $NF files they list, verified)"
    exit 0 ;;
  I9)
    [ -n "$SLOT" ] || [ -n "${POD_PULL_SOURCE:-}" ] || { echo "usage: --exp I9 --slot _A [--manifest NAME ...]" >&2; exit 2; }
    [ -z "$SUBDIR" ] || { echo "--subdir is E1-only" >&2; exit 2; }
    for m in $MLIST; do
      case "$m" in
        *partial*) echo "a partial manifest is never a completion manifest" >&2; exit 2 ;;
        MANIFEST.*.sha256) ;;
        *) echo "--manifest must name a top-level results/I9/MANIFEST.<step>.sha256 (got '$m')" >&2; exit 2 ;;
      esac
      case "$m" in *[!A-Za-z0-9._-]*) echo "unsafe manifest name '$m'" >&2; exit 2 ;; esac
    done
    if [ -n "${POD_PULL_SOURCE:-}" ]; then
      SRC_ROOT="$POD_PULL_SOURCE"
      rsync_pod() { rsync "$@"; }
    else
      envval() { { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }
      H=$(envval "POD_SSH_HOST$SLOT"); P=$(envval "POD_SSH_PORT$SLOT")
      U=$(envval POD_SSH_USER); U=${U:-root}
      [ -n "$H" ] && [ -n "$P" ] || { echo "no pod in slot '$SLOT'" >&2; exit 1; }
      SRC_ROOT="$U@$H:/ws/emx"
      rsync_pod() { rsync -z -e "ssh -p $P -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new" "$@"; }
    fi
    mkdir -p results/I9
    STAGE=$(mktemp -d "results/I9/.pull_XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/results/I9"
    # 1. the pod's top-level manifests and Manifest records (small)
    rsync_pod -rlpt --include='MANIFEST.*.sha256' --include='manifests/' --include='manifests/*.json' \
      --exclude='*' "$SRC_ROOT/results/I9/" "$STAGE/results/I9/" \
      || { echo "could not list the pod's manifests" >&2; exit 1; }
    CLOSING=results/I9/MANIFEST.manifest.sha256
    SEL=""
    if [ -z "$MLIST" ]; then
      [ -s "$STAGE/$CLOSING" ] || { echo "no closing manifest $CLOSING - the session did not finish; nothing promoted" >&2; exit 1; }
      STEPMS=$(sed -n 's|^[0-9a-f]\{64\}  \**\(results/I9/MANIFEST\.[^/]*\.sha256\)$|\1|p' "$STAGE/$CLOSING")
      [ -n "$STEPMS" ] || { echo "$CLOSING lists no step manifest - nothing promoted" >&2; exit 1; }
      # a step completed after the closing manifest (e.g. a later --unsealed run) would be left
      # behind: refuse a closing manifest that does not list every step manifest on the pod
      UNLISTED=""
      for f in "$STAGE"/results/I9/MANIFEST.*.sha256; do
        [ -f "$f" ] || continue
        m="results/I9/${f##*/}"
        [ "$m" = "$CLOSING" ] && continue
        printf '%s\n' "$STEPMS" | grep -qxF "$m" || UNLISTED="$UNLISTED $m"
      done
      [ -z "$UNLISTED" ] || { echo "$CLOSING is stale: it does not list$UNLISTED (rerun the session's manifest step, or pull with --manifest) - nothing promoted" >&2; exit 1; }
      SEL="$CLOSING"
      for m in $STEPMS; do
        [ -f "$STAGE/$m" ] || { echo "$CLOSING lists $m, which is not on the pod - nothing promoted" >&2; exit 1; }
        SEL="$SEL $m"
      done
    else
      for m in $MLIST; do
        [ -f "$STAGE/results/I9/$m" ] || { echo "NO manifest $m on the pod - not finished; nothing promoted" >&2; exit 1; }
        SEL="$SEL results/I9/$m"
      done
    fi
    # 2. exactly the files they list (repo-relative, under results/I9/, no ..)
    # shellcheck disable=SC2086 # SEL holds repo-relative manifest paths without spaces
    ( cd "$STAGE" && python3 - $SEL <<'PY' ) > "$STAGE/.files" || { echo "a manifest was refused - nothing promoted" >&2; exit 1; }
import re
import sys

line_re = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
seen = set()
for m in sys.argv[1:]:
    paths = []
    for i, line in enumerate(open(m).read().splitlines(), 1):
        if not line.strip():
            continue
        hit = line_re.match(line)
        if not hit:
            sys.exit(f"{m}:{i}: not a sha256 manifest line")
        paths.append(hit.group(2))
    if not paths:
        sys.exit(f"{m}: lists no file")
    for p in paths:
        if p.startswith("/") or ".." in p.split("/") or not p.startswith("results/I9/"):
            sys.exit(f"{m}: refused path {p!r} (repo-relative paths under results/I9/ only)")
        if p not in seen:
            seen.add(p)
            print(p)
PY
    rsync_pod -lpt --files-from="$STAGE/.files" "$SRC_ROOT/" "$STAGE/" \
      || { echo "could not pull the listed files - nothing promoted" >&2; exit 1; }
    # 3. verify every chosen manifest locally, and each step's Manifest record
    for m in $SEL; do
      echo "== verifying $m ($(shasum -a 256 "$STAGE/$m" | cut -d' ' -f1))"
      ( cd "$STAGE" && shasum -a 256 -c --quiet "$m" ) \
        || { echo "MANIFEST $m does NOT verify - nothing promoted" >&2; exit 1; }
      step=${m#results/I9/MANIFEST.}; step=${step%.sha256}
      rec="results/I9/manifests/$step.json"
      python3 - "$STAGE/$rec" "$step" <<'PY' || { echo "nothing promoted" >&2; exit 1; }
import json
import sys

path, step = sys.argv[1:3]
try:
    d = json.load(open(path))
except (OSError, ValueError) as e:
    sys.exit(f"Manifest record for step {step!r} missing or unreadable: {e}")
if not isinstance(d, dict) or d.get("step") != step:
    sys.exit(f"{path}: step is {d.get('step') if isinstance(d, dict) else d!r}, not {step!r}")
PY
      grep -qxF "$rec" "$STAGE/.files" || echo "$rec" >> "$STAGE/.files"
      echo "$m" >> "$STAGE/.files"
    done
    # 4. promote; a local file that would change is moved aside first, never discarded
    SUPER="results/I9/superseded/$(date -u +%Y%m%dT%H%M%SZ)"
    NS=0
    while IFS= read -r f; do
      if [ -f "$f" ] && ! cmp -s "$f" "$STAGE/$f"; then
        mkdir -p "$SUPER/$(dirname "${f#results/I9/}")"
        mv "$f" "$SUPER/${f#results/I9/}"
        NS=$((NS + 1))
      fi
    done < "$STAGE/.files"
    rsync -lpt --files-from="$STAGE/.files" "$STAGE/" ./
    [ "$NS" = "0" ] || echo "moved $NS changed local file(s) aside to $SUPER"
    # shellcheck disable=SC2086
    echo "promoted to results/I9 ($(echo $SEL | wc -w | tr -d ' ') manifest(s) and the files they list, verified)"
    exit 0 ;;
  E3)
    [ -n "$SLOT" ] || { echo "usage: --exp E3 --slot _A [--unit U]" >&2; exit 2; }
    [ -z "$SUBDIR$MLIST" ] || { echo "--subdir/--manifest are not E3 options (use --unit)" >&2; exit 2; }
    case "$SLOT" in
      _*[!A-Za-z0-9]*|_) echo "bad slot '$SLOT'" >&2; exit 2 ;;
      _*) ;;
      *) echo "bad slot '$SLOT'" >&2; exit 2 ;;
    esac
    case "$UNIT" in *[!A-Za-z0-9._-]*) echo "bad unit '$UNIT'" >&2; exit 2 ;; esac
    case "$UNIT" in *partial*) echo "a partial manifest is never a completion manifest" >&2; exit 2 ;; esac
    if [ -n "${POD_PULL_SOURCE:-}" ]; then
      SRC_ROOT="$POD_PULL_SOURCE"
      rsync_pod() { rsync "$@"; }
    else
      envval() { { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }
      H=$(envval "POD_SSH_HOST$SLOT"); P=$(envval "POD_SSH_PORT$SLOT")
      U=$(envval POD_SSH_USER); U=${U:-root}
      [ -n "$H" ] && [ -n "$P" ] || { echo "no pod in slot '$SLOT'" >&2; exit 1; }
      SRC_ROOT="$U@$H:/ws/emx"
      rsync_pod() { rsync -z -e "ssh -p $P -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new" "$@"; }
    fi
    MIRROR="results/E3/sealed/_pod_mirror/$SLOT"
    mkdir -p "$MIRROR/results/E3" "$MIRROR/logs"
    # 1. mirror everything (kept, never discarded)
    rsync_pod -rlpt --exclude='*.safetensors' --exclude='/sealed/_pod_mirror/' \
      "$SRC_ROOT/results/E3/" "$MIRROR/results/E3/" \
      || { echo "could not pull results/E3 from slot $SLOT - nothing promoted" >&2; exit 1; }
    rsync_pod -rlpt "$SRC_ROOT/logs/" "$MIRROR/logs/" \
      || echo "WARNING: could not pull the pod's logs/ (slot $SLOT)" >&2
    echo "mirrored slot $SLOT into $MIRROR"
    # 2. the manifest(s) to verify
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT
    SEL=""
    if [ -n "$UNIT" ]; then
      m="results/E3/MANIFEST.$UNIT.sha256"
      [ -s "$MIRROR/$m" ] || { echo "NO manifest $m on slot $SLOT - unit not finished; nothing promoted" >&2; exit 1; }
      SEL="$m"
    else
      # the manifests on the pod NOW (the mirror keeps every older file, never deleting)
      mkdir -p "$TMPD/m"
      rsync_pod -rlpt --include='MANIFEST.*.sha256' --exclude='*' "$SRC_ROOT/results/E3/" "$TMPD/m/" \
        || { echo "could not list the manifests on slot $SLOT - nothing promoted" >&2; exit 1; }
      for f in "$TMPD"/m/MANIFEST.*.sha256; do
        [ -f "$f" ] || continue
        case "${f##*/}" in *partial*) continue ;; esac
        SEL="$SEL results/E3/${f##*/}"
      done
      [ -n "$SEL" ] || { echo "NO manifest on slot $SLOT - nothing promoted" >&2; exit 1; }
    fi
    # shellcheck disable=SC2086 # SEL holds repo-relative manifest paths without spaces
    ( cd "$MIRROR" && python3 - "$TMPD/bases" $SEL <<'PY' ) > "$TMPD/files" \
      || { echo "a manifest was refused - nothing promoted" >&2; exit 1; }
import re
import sys

base_out, manifests = sys.argv[1], sys.argv[2:]
line_re = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
bases, seen = [], set()
for m in manifests:
    mdir = m.rsplit("/", 1)[0]
    paths = []
    for i, line in enumerate(open(m).read().splitlines(), 1):
        if not line.strip():
            continue
        hit = line_re.match(line)
        if not hit:
            sys.exit(f"{m}:{i}: not a sha256 manifest line")
        paths.append(hit.group(2))
    if not paths:
        sys.exit(f"{m}: lists no file")
    repo = [p.startswith("results/") for p in paths]
    if any(repo) and not all(repo):
        sys.exit(f"{m}: mixes repo-relative and manifest-relative paths")
    base = "." if all(repo) else mdir
    bases.append(f"{m} {base}")
    for p in paths:
        rel = p if base == "." else f"{mdir}/{p}"
        parts = rel.split("/")
        if (p.startswith("/") or ".." in parts or not rel.startswith("results/E3/")
                or rel.startswith("results/E3/sealed/_pod_mirror/")):
            sys.exit(f"{m}: refused path {p!r}")
        if rel not in seen:
            seen.add(rel)
            print(rel)
open(base_out, "w").write("\n".join(bases) + "\n")
PY
    # 3. verify every chosen manifest in the mirror
    while read -r m base; do
      echo "== verifying $m ($(shasum -a 256 "$MIRROR/$m" | cut -d' ' -f1))"
      if [ "$base" = "." ]; then
        ( cd "$MIRROR" && shasum -a 256 -c --quiet "$m" )
      else
        ( cd "$MIRROR/$base" && shasum -a 256 -c --quiet "${m#"$base"/}" )
      fi || { echo "MANIFEST $m does NOT verify (slot $SLOT) - nothing promoted; the raw mirror is kept in $MIRROR" >&2; exit 1; }
    done < "$TMPD/bases"
    # 4. promote the listed files and the manifests; a changed local file is moved aside first
    NF=$(wc -l < "$TMPD/files" | tr -d ' ')
    for m in $SEL; do echo "$m" >> "$TMPD/files"; done
    SUPER="results/E3/superseded/$(date -u +%Y%m%dT%H%M%SZ)"
    NS=0
    while IFS= read -r f; do
      if [ -f "$f" ] && ! cmp -s "$f" "$MIRROR/$f"; then
        mkdir -p "$SUPER/$(dirname "${f#results/E3/}")"
        mv "$f" "$SUPER/${f#results/E3/}"
        NS=$((NS + 1))
      fi
    done < "$TMPD/files"
    rsync -lpt --files-from="$TMPD/files" "$MIRROR/" ./
    [ "$NS" = "0" ] || echo "moved $NS changed local file(s) aside to $SUPER"
    # shellcheck disable=SC2086
    echo "promoted to results/E3 ($(echo $SEL | wc -w | tr -d ' ') manifest(s) and the $NF files they list, verified)"
    exit 0 ;;
  *) echo "--exp $EXP: pod_pull_results.sh knows E1, I1a, E1b, I9 and E3" >&2; exit 2 ;;
esac
[ -n "$SLOT" ] && [ -n "$MGLOB" ] || { echo "usage: --slot _A --manifest GLOB [--subdir DIR]" >&2; exit 2; }
case "$MGLOB" in *partial*) echo "a partial manifest is never a completion manifest" >&2; exit 2 ;; esac
case "$SUBDIR" in *..*) echo "--subdir may not contain .." >&2; exit 2 ;; esac
envval() { { grep -m1 "^$1=" .env || true; } | cut -d= -f2- ; }  # a missing key is empty, not fatal under pipefail
POD_SSH_HOST=$(envval "POD_SSH_HOST$SLOT"); POD_SSH_PORT=$(envval "POD_SSH_PORT$SLOT")
POD_SSH_USER=$(envval POD_SSH_USER); POD_SSH_USER=${POD_SSH_USER:-root}
[ -n "$POD_SSH_HOST" ] && [ -n "$POD_SSH_PORT" ] || { echo "no pod in slot '$SLOT'" >&2; exit 1; }
mkdir -p results/E1
# a FRESH staging directory: an old manifest in the destination can never satisfy a new pull
STAGE_DIR=$(mktemp -d "results/E1/.pull_XXXXXX")
trap 'rm -rf "$STAGE_DIR"' EXIT
REMOTE="/ws/emx/results/E1/${SUBDIR:+$SUBDIR/}"
rsync -rlptz -e "ssh -p $POD_SSH_PORT -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new" \
      "$POD_SSH_USER@$POD_SSH_HOST:$REMOTE" "$STAGE_DIR/"
n=0
( cd "$STAGE_DIR" && for m in $MGLOB; do
    [ -f "$m" ] || continue
    case "$m" in *partial*) continue ;; esac
    echo "== verifying $m ($(shasum -a 256 "$m" | cut -d' ' -f1))"
    ( cd "$(dirname "$m")" && shasum -a 256 -c --quiet "$(basename "$m")" ) \
      || { echo "MANIFEST $m does NOT verify - nothing promoted" >&2; exit 1; }
    echo "$m" >> .verified_manifests
  done )
n=$( [ -f "$STAGE_DIR/.verified_manifests" ] && wc -l < "$STAGE_DIR/.verified_manifests" | tr -d ' ' || echo 0 )
[ "$n" -gt 0 ] || { echo "NO completion manifest matched '$MGLOB' - the run did not finish; nothing promoted" >&2; exit 1; }
rm -f "$STAGE_DIR/.verified_manifests"
DEST="results/E1/${SUBDIR}"
mkdir -p "$DEST"
rsync -rlpt "$STAGE_DIR/" "$DEST/"
echo "promoted to $DEST ($n completion manifest(s) verified)"
