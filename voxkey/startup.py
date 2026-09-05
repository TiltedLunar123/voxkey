"""Registering VoxKey to launch when Windows starts."""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

log = logging.getLogger("voxkey.startup")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VoxKey"


def launcher_command() -> str:
    """The exact command line Windows should run at login.

    pythonw.exe rather than python.exe, so logging in does not flash a console
    window; the .pyw launcher fixes up sys.path so the package is importable
    without a working directory.
    """
    project = Path(__file__).resolve().parent.parent
    launcher = project / "VoxKey.pyw"
    interpreter = Path(sys.executable)
    windowed = interpreter.with_name("pythonw.exe")
    if windowed.exists():
        interpreter = windowed
    return f'"{interpreter}" "{launcher}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def registered_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except OSError:
        return None


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """Returns (ok, message)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launcher_command())
                return True, "VoxKey will start with Windows."
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            return True, "VoxKey will not start with Windows."
    except OSError as exc:
        log.warning("could not write the Run key: %s", exc)
        return False, f"Could not change the startup entry: {exc}"


def sync(config) -> None:
    """Make the registry match the setting, and repair a stale path."""
    wanted = bool(config.get("advanced.start_with_windows", True))
    current = registered_command()
    if wanted and current != launcher_command():
        set_enabled(True)
    elif not wanted and current is not None:
        set_enabled(False)
