"""Short-cycle research package with legacy script-style imports."""

from pathlib import Path
import sys


_PACKAGE_DIRECTORY = str(Path(__file__).resolve().parent)
if _PACKAGE_DIRECTORY not in sys.path:
    sys.path.insert(0, _PACKAGE_DIRECTORY)
