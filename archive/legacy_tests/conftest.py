"""Make retired modules importable only for explicit legacy-suite runs."""

import sys
from pathlib import Path

LEGACY_SCRIPTS = Path(__file__).resolve().parents[1] / "legacy_scripts"
if str(LEGACY_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(LEGACY_SCRIPTS))
