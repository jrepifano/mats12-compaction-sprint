"""Put the probe directory, P06's and P12's on sys.path so `import p16`, `p06` and `p12` work
from the repo root."""

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parent / "P06-filler-token-computation",
           _PROBE.parent / "P12-filler-conceal"):
    sys.path.insert(0, str(_p))
