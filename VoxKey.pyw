"""Windowless launcher. This is the file the Run key and the shortcut point at."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _reexec_in_venv() -> None:
    """Double-clicking a .pyw runs it under whichever Python owns the file
    association, which is usually the system one and has none of the packages.
    Hand over to the project's own interpreter instead of failing on import."""
    venv = HERE / ".venv" / "Scripts" / "pythonw.exe"
    if not venv.exists() or Path(sys.executable).resolve() == venv.resolve():
        return
    try:
        import PySide6  # noqa: F401
    except ImportError:
        import subprocess

        subprocess.Popen([str(venv), str(Path(__file__).resolve())], close_fds=True)
        raise SystemExit(0)


_reexec_in_venv()

from voxkey.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
