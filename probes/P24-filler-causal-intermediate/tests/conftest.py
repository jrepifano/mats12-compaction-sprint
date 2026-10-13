"""Put this probe's directory and P06's on sys.path so `import p24` and `import p06` work."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent / "P06-filler-token-computation"))
sys.path.insert(0, str(HERE))
