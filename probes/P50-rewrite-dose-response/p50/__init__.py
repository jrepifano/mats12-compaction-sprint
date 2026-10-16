"""P50: rewrite vs refill dose-response on P48's cat organism.

P48's code is imported, never copied or modified: its probe directory is put on sys.path here, so
`import p48` resolves to `probes/P48-subliminal-numbers-attribution/p48` both on the laptop and on
the pod (where run.sh pushes that directory).
"""

import sys
from pathlib import Path

_P48_DIR = Path(__file__).resolve().parents[2] / "P48-subliminal-numbers-attribution"
if str(_P48_DIR) not in sys.path:
    sys.path.insert(0, str(_P48_DIR))
