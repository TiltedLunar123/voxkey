"""Windowless launcher. This is the file the Run key and the shortcut point at."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from voxkey.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
