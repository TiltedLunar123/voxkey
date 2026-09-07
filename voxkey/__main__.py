"""Entry point: one instance, a tray icon, and a hotkey listener.

Flags, all optional:

  --autostart   what the Run key passes. Stay in the tray.
  --show        open the settings window. This is also what a launch with no
                flag does, and what a second launch asks the running copy to do.
  --quit        ask the running copy to exit.
  --uninstall   take VoxKey out of the Start menu, Installed apps and startup,
                and stop it. The folder and the settings stay where they are.
"""

from __future__ import annotations

import ctypes
import faulthandler
import logging
import sys
import threading
from ctypes import wintypes
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import APP_NAME, __version__, register, startup
from .app import Engine
from .config import CONFIG_DIR, LOG_PATH, Config
from .settings_ui import SettingsWindow
from .tray import Tray

# Local\ rather than Global\: creating a Global object needs
# SeCreateGlobalPrivilege, which a standard user account does not hold, so the
# call would fail and every launch would believe it was the only instance.
MUTEX_NAME = "Local\\VoxKeySingleInstance"
SHOW_EVENT = "Local\\VoxKeyShowWindow"
QUIT_EVENT = "Local\\VoxKeyQuit"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF
ASFW_ANY = wintypes.DWORD(-1 & 0xFFFFFFFF)
_mutex_handle = None

# use_last_error is required. Plain ctypes.windll does not capture the Win32
# error, so reading GetLastError through it can return something unrelated that
# happened in between, and the duplicate-instance check becomes a coin flip.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
]
_user32 = ctypes.WinDLL("user32")
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]


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


def signal_running(event_name: str) -> bool:
    """Poke the copy that is already running. False if it is too old to listen."""
    handle = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, event_name)
    if not handle:
        return False
    try:
        # This process was started by the user, so it holds the right to set
        # the foreground window. Hand that right on, or the running copy can
        # only flash its taskbar button when it tries to come to the front.
        _user32.AllowSetForegroundWindow(ASFW_ANY)
        return bool(_kernel32.SetEvent(handle))
    finally:
        _kernel32.CloseHandle(handle)


class Requests(QObject):
    """What later launches ask of this one, delivered on the GUI thread."""

    show = Signal()
    quit = Signal()

    def __init__(self) -> None:
        super().__init__()
        # Auto-reset, so one SetEvent means one request.
        self._handles = [
            _kernel32.CreateEventW(None, False, False, SHOW_EVENT),
            _kernel32.CreateEventW(None, False, False, QUIT_EVENT),
        ]

    def watch(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="voxkey-requests").start()

    def _loop(self) -> None:
        handles = (wintypes.HANDLE * 2)(*self._handles)
        while True:
            which = _kernel32.WaitForMultipleObjects(2, handles, False, INFINITE)
            if which == 0:
                self.show.emit()
            elif which == 1:
                self.quit.emit()
            else:
                return  # a broken handle; better a dead watcher than a hot loop


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

        self.requests = Requests()
        self.requests.show.connect(lambda: self.open_settings())
        self.requests.quit.connect(self.tray.quit_app)
        self.requests.watch()

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
        if self.window.isMinimized():
            self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _settings_closed(self) -> None:
        self.tray.sync_profile()


def uninstall() -> int:
    """Undo the registrations. Nothing is deleted; that is the user's call."""
    signal_running(QUIT_EVENT)
    startup.set_enabled(False)
    register.unregister()
    QApplication(sys.argv)
    QMessageBox.information(
        None, APP_NAME,
        "VoxKey has been taken out of the Start menu, Installed apps and startup, "
        "and told to close if it was running.\n\n"
        f"The program folder is still at:\n{register.PROJECT}\n\n"
        f"Settings and history are still in:\n{CONFIG_DIR}\n\n"
        "Delete either by hand if you want them gone.",
    )
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    if "--uninstall" in args:
        return uninstall()

    if already_running():
        wanted = QUIT_EVENT if "--quit" in args else SHOW_EVENT
        if signal_running(wanted):
            return 0
        # A copy from before the events existed. Say so rather than do nothing,
        # which is what a second launch used to do.
        QApplication(sys.argv)
        QMessageBox.information(
            None, APP_NAME, "VoxKey is already running. Click its tray icon to open it."
        )
        return 0
    if "--quit" in args:
        return 0

    # Before any window exists, so the taskbar groups them under VoxKey.
    register.set_process_app_id()

    config = Config()
    config.save()  # materialise defaults on first run so the file is there to read
    setup_logging(config.get("advanced.log_level", "INFO"))
    logging.getLogger("voxkey").info("starting %s %s", APP_NAME, __version__)
    note_previous_exit()

    # Keep the Run key, the Start menu entry and the Installed apps entry
    # pointing at this copy even if the folder moved.
    startup.sync(config)
    register.sync(config)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if register.ICON.exists():
        app.setWindowIcon(QIcon(str(register.ICON)))
    # Closing the settings window must not end the process; the tray stays.
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "This desktop has no system tray, so VoxKey cannot run.")
        return 1

    voxkey = VoxKey(app, config)
    # Autostart stays out of the way. Anyone who launched it by hand wants to
    # see it, whatever the start-hidden setting says.
    if "--autostart" not in args or not config.get("ui.start_minimized", True):
        voxkey.open_settings()

    code = app.exec()
    mark_clean_exit()
    return code


if __name__ == "__main__":
    sys.exit(main())
