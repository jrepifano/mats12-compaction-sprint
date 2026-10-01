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
set -euo pipefail
cd "$(dirname "$0")/.."
SLOT=""; MGLOB=""; SUBDIR=""; EXP="E1"
while [ $# -gt 0 ]; do
  case "$1" in
    --exp) EXP="${2:?}"; shift 2 ;;
    --slot) SLOT="${2:?}"; shift 2 ;;
    --manifest) MGLOB="${2:?}"; shift 2 ;;
    --subdir) SUBDIR="${2:?}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
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
  *) echo "--exp $EXP: pod_pull_results.sh knows E1 and I1a" >&2; exit 2 ;;
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
