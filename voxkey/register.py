"""Making VoxKey look like an installed program to Windows.

A .pyw in a Downloads folder is invisible to the Start menu, so the only way
to reach it was the tray icon, and only after it had autostarted. This writes
the two things Windows actually uses to decide something is an app: a shortcut
in the Start menu folder, and an entry under Installed apps. Both point at the
same interpreter and launcher the Run key uses.

The shortcut also carries an AppUserModelID matching the one the process sets
on itself. Without that pair, pinning the running window to the taskbar
creates a pin to pythonw.exe with no arguments, which opens nothing.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import winreg
from ctypes import POINTER, byref, c_int, c_ushort, c_void_p, c_wchar_p, wintypes
from pathlib import Path

from . import APP_NAME, __version__, com

log = logging.getLogger("voxkey.register")

APP_ID = "JudeHilgendorf.VoxKey"
PUBLISHER = "Jude Hilgendorf"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"

PROJECT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT / "VoxKey.pyw"
ICON = Path(__file__).resolve().parent / "assets" / "voxkey.ico"


def start_menu_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def shortcut_path() -> Path:
    return start_menu_dir() / f"{APP_NAME}.lnk"


def interpreter() -> Path:
    exe = Path(sys.executable)
    windowed = exe.with_name("pythonw.exe")
    return windowed if windowed.exists() else exe


# -- the process side of the AppUserModelID -----------------------------------
def set_process_app_id() -> None:
    """Group this process's windows under VoxKey rather than pythonw.exe.

    Must run before the first window exists, so it is called ahead of
    QApplication. A failure only costs taskbar grouping, so it is logged and
    otherwise ignored.
    """
    try:
        shell32 = ctypes.OleDLL("shell32")
        shell32.SetCurrentProcessExplicitAppUserModelID(c_wchar_p(APP_ID))
    except OSError as exc:
        log.debug("could not set the AppUserModelID: %s", exc)


# -- writing a .lnk with a property on it --------------------------------------
class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", com.GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    """Only the VT_LPWSTR shape is needed; the union is padded to its full size."""

    _fields_ = [
        ("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
        ("pwszVal", c_wchar_p), ("pad", c_void_p),
    ]


VT_LPWSTR = 31
CLSID_ShellLink = com.GUID.of("00021401-0000-0000-C000-000000000046")
IID_IShellLinkW = com.GUID.of("000214F9-0000-0000-C000-000000000046")
IID_IPersistFile = com.GUID.of("0000010B-0000-0000-C000-000000000046")
IID_IPropertyStore = com.GUID.of("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
PKEY_AppUserModel_ID = PROPERTYKEY(com.GUID.of("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)

# Vtable slots, counted from IUnknown's three.
_QUERY_INTERFACE = 0
_LINK_SET_DESCRIPTION, _LINK_SET_WORKDIR, _LINK_SET_ARGUMENTS = 7, 9, 11
_LINK_SET_ICON, _LINK_SET_PATH = 17, 20
_STORE_SET_VALUE, _STORE_COMMIT = 6, 7
_FILE_SAVE = 6


def create_shortcut(
    path: Path,
    target: Path,
    arguments: str = "",
    working_dir: Path | None = None,
    icon: Path | None = None,
    description: str = "",
    app_id: str | None = None,
) -> None:
    """Write a Windows shortcut. app_id, when given, is stored as the
    shortcut's AppUserModelID so taskbar pins and notifications line up."""
    com.initialize()
    link = com.create(CLSID_ShellLink, IID_IShellLinkW)
    try:
        com.call(link, _LINK_SET_PATH, c_wchar_p(str(target)), argtypes=(c_wchar_p,))
        if arguments:
            com.call(link, _LINK_SET_ARGUMENTS, c_wchar_p(arguments), argtypes=(c_wchar_p,))
        if working_dir is not None:
            com.call(link, _LINK_SET_WORKDIR, c_wchar_p(str(working_dir)), argtypes=(c_wchar_p,))
        if description:
            com.call(link, _LINK_SET_DESCRIPTION, c_wchar_p(description), argtypes=(c_wchar_p,))
        if icon is not None and icon.exists():
            com.call(link, _LINK_SET_ICON, c_wchar_p(str(icon)), 0, argtypes=(c_wchar_p, c_int))

        if app_id:
            store = c_void_p()
            com.call(
                link, _QUERY_INTERFACE, byref(IID_IPropertyStore), byref(store),
                argtypes=(POINTER(com.GUID), POINTER(c_void_p)),
            )
            try:
                value = PROPVARIANT()
                value.vt = VT_LPWSTR
                value.pwszVal = app_id
                # The store copies the string, so the PROPVARIANT is never
                # cleared here: PropVariantClear would try to free Python's
                # buffer.
                com.call(
                    store, _STORE_SET_VALUE, byref(PKEY_AppUserModel_ID), byref(value),
                    argtypes=(POINTER(PROPERTYKEY), POINTER(PROPVARIANT)),
                )
                com.call(store, _STORE_COMMIT)
            finally:
                com.release(store)

        file = c_void_p()
        com.call(
            link, _QUERY_INTERFACE, byref(IID_IPersistFile), byref(file),
            argtypes=(POINTER(com.GUID), POINTER(c_void_p)),
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            com.call(
                file, _FILE_SAVE, c_wchar_p(str(path)), True, argtypes=(c_wchar_p, wintypes.BOOL)
            )
        finally:
            com.release(file)
    finally:
        com.release(link)


# -- the two registrations ----------------------------------------------------
def install_shortcut() -> None:
    create_shortcut(
        shortcut_path(),
        interpreter(),
        arguments=f'"{LAUNCHER}"',
        working_dir=PROJECT,
        icon=ICON,
        description="Hold-to-talk dictation",
        app_id=APP_ID,
    )


def remove_shortcut() -> None:
    try:
        shortcut_path().unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove the Start menu shortcut: %s", exc)


def write_uninstall_entry() -> None:
    command = f'"{interpreter()}" "{LAUNCHER}" --uninstall'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in (
            ("DisplayName", APP_NAME),
            ("DisplayVersion", __version__),
            ("Publisher", PUBLISHER),
            ("DisplayIcon", str(ICON)),
            ("InstallLocation", str(PROJECT)),
            ("UninstallString", command),
            ("QuietUninstallString", command),
            ("URLInfoAbout", "https://github.com/TiltedLunar123/voxkey"),
        ):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)


def remove_uninstall_entry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove the Installed apps entry: %s", exc)


def is_registered() -> bool:
    return shortcut_path().exists()


def register() -> tuple[bool, str]:
    try:
        install_shortcut()
        write_uninstall_entry()
        return True, "VoxKey is in the Start menu and under Installed apps."
    except OSError as exc:
        log.warning("could not register the app: %s", exc)
        return False, f"Could not register VoxKey: {exc}"


def unregister() -> tuple[bool, str]:
    remove_shortcut()
    remove_uninstall_entry()
    return True, "VoxKey is no longer listed in the Start menu or Installed apps."


def sync(config) -> None:
    """Make Windows match the setting. Re-run at every start, so a moved
    folder or a new version is picked up without anyone touching anything."""
    wanted = bool(config.get("advanced.register_app", True))
    if wanted:
        register()
    elif is_registered():
        unregister()
