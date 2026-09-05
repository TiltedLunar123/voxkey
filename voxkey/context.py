"""Working out what you are dictating into, so the profile can follow.

Windows tells us the foreground process and its title. That is enough to know
the difference between a chat box, an inbox and a code editor, which is most of
what picking a tone by hand was for.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("voxkey.context")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.GetForegroundWindow.restype = wintypes.HWND
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Shipped rules, matched top to bottom. "app" is matched against the executable
# name, "title" against the window title; both are case-insensitive substrings
# and either may be left blank.
DEFAULT_RULES = [
    {"app": "", "title": "gmail", "profile": "email", "enabled": True},
    {"app": "", "title": "outlook", "profile": "email", "enabled": True},
    {"app": "outlook", "title": "", "profile": "email", "enabled": True},
    {"app": "thunderbird", "title": "", "profile": "email", "enabled": True},
    {"app": "slack", "title": "", "profile": "casual", "enabled": True},
    {"app": "discord", "title": "", "profile": "casual", "enabled": True},
    {"app": "teams", "title": "", "profile": "casual", "enabled": True},
    {"app": "whatsapp", "title": "", "profile": "casual", "enabled": True},
    {"app": "code", "title": "", "profile": "prompt", "enabled": True},
    {"app": "cursor", "title": "", "profile": "prompt", "enabled": True},
    {"app": "claude", "title": "", "profile": "prompt", "enabled": True},
    {"app": "windowsterminal", "title": "", "profile": "prompt", "enabled": True},
    {"app": "", "title": "claude", "profile": "prompt", "enabled": True},
    {"app": "", "title": "chatgpt", "profile": "prompt", "enabled": True},
    {"app": "winword", "title": "", "profile": "formal", "enabled": True},
    {"app": "notepad", "title": "", "profile": "clean", "enabled": True},
    {"app": "obsidian", "title": "", "profile": "notes", "enabled": True},
    {"app": "notion", "title": "", "profile": "notes", "enabled": True},
]


def foreground_window() -> tuple[str, str]:
    """(executable name without extension, window title). Blank on failure."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return "", ""

        length = _user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "", title

        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return "", title
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                return "", title
            name = path.value.rsplit("\\", 1)[-1]
            if name.lower().endswith(".exe"):
                name = name[:-4]
            return name, title
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        log.debug("could not read the foreground window", exc_info=True)
        return "", ""


def match_profile(rules: list[dict], app: str, title: str) -> tuple[str, str] | None:
    """First matching rule wins. Returns (profile, why) or None."""
    app_lower, title_lower = app.lower(), title.lower()
    for rule in rules:
        if not rule.get("enabled", True) or not rule.get("profile"):
            continue
        want_app = (rule.get("app") or "").strip().lower()
        want_title = (rule.get("title") or "").strip().lower()
        if not want_app and not want_title:
            continue
        if want_app and want_app not in app_lower:
            continue
        if want_title and want_title not in title_lower:
            continue
        target = want_app or want_title
        return rule["profile"], target
    return None


def resolve(config) -> tuple[str | None, str]:
    """The profile this window should use, plus a line explaining the choice."""
    if not config.get("context.auto_profile", True):
        return None, ""
    app, title = foreground_window()
    if not app and not title:
        return None, ""
    hit = match_profile(config.get("context.rules", DEFAULT_RULES), app, title)
    if hit is None:
        return None, f"{app or 'window'}: no rule, using the default"
    profile, reason = hit
    return profile, f"{app or title[:24]} matched '{reason}'"
