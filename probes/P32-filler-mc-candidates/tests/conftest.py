"""Put the probe directory on sys.path so `import p32` (and through it `p06`) works."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import p32  # noqa: F401
