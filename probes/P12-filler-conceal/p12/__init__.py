"""P12 filler-conceal probe (plan.md). Builds on P06's V4 Flash code path, imported as `p06`
from probes/P06-filler-token-computation (put on sys.path here, so `import p06` works on the
laptop, in the tests and on the pod)."""

import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
P06_DIR = PROBE.parent / "P06-filler-token-computation"
if str(P06_DIR) not in sys.path:
    sys.path.insert(0, str(P06_DIR))
