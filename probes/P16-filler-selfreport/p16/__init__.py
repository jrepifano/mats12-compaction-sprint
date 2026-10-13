"""P16 filler self-report probe, merged with P13 (plan.md). Builds on P06's V4 Flash code path
(`p06`) and P12's arms and pod code (`p12`), both imported from their probe directories (put on
sys.path here, so the imports work on the laptop, in the tests and on the pod)."""

import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
for _d in ("P06-filler-token-computation", "P12-filler-conceal"):
    _p = str(PROBE.parent / _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)
