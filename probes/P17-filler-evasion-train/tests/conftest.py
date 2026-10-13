"""Put the probe directory on sys.path so `import p12` works from the repo root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import p17  # noqa: E402,F401  (puts P06's and P12's directories on sys.path for `import p06`)
