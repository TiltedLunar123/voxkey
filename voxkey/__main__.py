"""Entry point: one instance, a tray icon, and a hotkey listener."""

from __future__ import annotations

import ctypes
import faulthandler
import logging
import sys
import threading
from ctypes import wintypes
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import APP_NAME, startup
from .app import Engine
from .config import CONFIG_DIR, LOG_PATH, Config
from .settings_ui import SettingsWindow
from .tray import Tray

# Local\ rather than Global\: creating a Global object needs
# SeCreateGlobalPrivilege, which a standard user account does not hold, so the
# call would fail and every launch would believe it was the only instance.
MUTEX_NAME = "Local\\VoxKeySingleInstance"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None

# use_last_error is required. Plain ctypes.windll does not capture the Win32
# error, so reading GetLastError through it can return something unrelated that
# happened in between, and the duplicate-instance check becomes a coin flip.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]


def already_running() -> bool:
    """A named mutex, so autostart plus a manual launch cannot fight over the mic."""
    global _mutex_handle
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if not handle:
        logging.getLogger("voxkey").warning("could not create the instance mutex: %s", error)
        return False
    # Held for the life of the process and never closed, on purpose.
    _mutex_handle = handle
    return error == ERROR_ALREADY_EXISTS


RUNNING_MARKER = CONFIG_DIR / "running.marker"


def setup_logging(level: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)

    log = logging.getLogger("voxkey")

    # pythonw has no stderr, so without these an unhandled exception vanishes
    # and the app simply disappears with nothing written down.
    def on_exception(kind, value, trace):
        log.critical("unhandled exception", exc_info=(kind, value, trace))

    def on_thread_exception(args):
        log.critical(
            "unhandled exception in thread %s", args.thread and args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception
    # Catches the hard faults Python cannot, such as a segfault inside a DLL.
    try:
        faulthandler.enable(open(CONFIG_DIR / "crash.log", "a", encoding="utf-8"))
    except OSError:
        pass


def note_previous_exit() -> None:
    """Say whether the last run ended cleanly, so a silent death is visible."""
    log = logging.getLogger("voxkey")
    if RUNNING_MARKER.exists():
        log.warning("the previous run did not shut down cleanly")
    try:
        RUNNING_MARKER.write_text("running", encoding="utf-8")
    except OSError:
        pass


def mark_clean_exit() -> None:
    try:
        RUNNING_MARKER.unlink(missing_ok=True)
    except OSError:
        pass
    logging.getLogger("voxkey").info("shut down cleanly")


class VoxKey:
    def __init__(self, app: QApplication, config: Config) -> None:
        self.app = app
        self.config = config
        self.engine = Engine(config)
        self.window: SettingsWindow | None = None
        self.tray = Tray(self.engine, self.open_settings)
        self.tray.show()
        self.engine.overlay.settings_requested.connect(lambda: self.open_settings())
        self.engine.overlay.history_requested.connect(lambda: self.open_settings("History"))
        self.engine.start()

    def open_settings(self, tab: str | None = None) -> None:
        if self.window is None:
            self.window = SettingsWindow(self.engine)
            self.window.closed.connect(self._settings_closed)
        if isinstance(tab, str):
            # Naming a tab means the advanced view, not the simple landing page.
            for index in range(self.window.tabs.count()):
                if self.window.tabs.tabText(index) == tab:
                    self.window._show_view(1)
                    self.window.tabs.setCurrentIndex(index)
                    break
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _settings_closed(self) -> None:
        self.tray.sync_profile()


def main() -> int:
    if already_running():
        return 0

    config = Config()
    config.save()  # materialise defaults on first run so the file is there to read
    setup_logging(config.get("advanced.log_level", "INFO"))
    logging.getLogger("voxkey").info("starting %s", APP_NAME)
    note_previous_exit()

    # Keep the Run key pointing at this copy even if the folder moved.
    startup.sync(config)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # Closing the settings window must not end the process; the tray stays.
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "This desktop has no system tray, so VoxKey cannot run.")
        return 1

    voxkey = VoxKey(app, config)
    if not config.get("ui.start_minimized", True):
        voxkey.open_settings()

    code = app.exec()
    mark_clean_exit()
    return code


if __name__ == "__main__":
    sys.exit(main())
