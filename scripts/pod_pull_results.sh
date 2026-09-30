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
set -euo pipefail
cd "$(dirname "$0")/.."
SLOT=""; MGLOB=""; SUBDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --slot) SLOT="${2:?}"; shift 2 ;;
    --manifest) MGLOB="${2:?}"; shift 2 ;;
    --subdir) SUBDIR="${2:?}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
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
