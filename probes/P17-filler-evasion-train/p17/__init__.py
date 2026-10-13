"""P17 filler-evasion-train probe (plan.md). Builds on P06's Gemma 4 path, imported as `p06`
from probes/P06-filler-token-computation (put on sys.path here, so `import p06` works on the
laptop, in the tests and on the pod; on the pod the package arrives through probe.toml
[pod].push)."""

import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
P06_DIR = PROBE.parent / "P06-filler-token-computation"
P12_DIR = PROBE.parent / "P12-filler-conceal"
for _d in (P06_DIR, P12_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
