import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
for p in (PROBE, ROOT / "src", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
