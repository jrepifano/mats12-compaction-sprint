import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
P42 = ROOT / "probes" / "P42-ood-coverage-gaps"  # p46 imports p42.feats / p42.scoring / p42.data
for p in (PROBE, P42, ROOT / "src", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
