#!/usr/bin/env bash
# P00: exercise the probe run contract end to end on a real pod (no model, no downloads).
set -euo pipefail
cd "$(dirname "$0")"
echo "P00 start $(date -u +%FT%TZ) probe=${EMX_PROBE:-unset} commit=${EMX_GIT_COMMIT:-unset}"
mkdir -p results
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | tee results/gpu.txt
python3 -c "import json,os; print(json.dumps({'probe': os.environ.get('EMX_PROBE'), 'ok': True}))" > results/check.json
sleep 30
(cd results && shasum -a 256 gpu.txt check.json > ../MANIFEST.sha256.tmp)
mv MANIFEST.sha256.tmp results/MANIFEST.sha256  # atomic publication
echo "PROBE_DONE rc=0"
