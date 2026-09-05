"""Entry point: one instance, a tray icon, and a hotkey listener."""

from __future__ import annotations

import ctypes
import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import APP_NAME, startup
from .app import Engine
from .config import CONFIG_DIR, LOG_PATH, Config
from .settings_ui import SettingsWindow
from .tray import Tray

MUTEX_NAME = "Global\\VoxKeySingleInstance"
ERROR_ALREADY_EXISTS = 183


def already_running() -> bool:
    """A named mutex, so autostart plus a manual launch cannot fight over the mic."""
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return True
    # Deliberately leaked: the handle must outlive this call for the whole run.
    return False


def setup_logging(level: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)


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

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
