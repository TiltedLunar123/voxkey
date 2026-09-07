"""Getting the finished text into whatever window has focus."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import POINTER, byref, c_int, c_void_p, wintypes

from . import com

log = logging.getLogger("voxkey.inject")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# ctypes defaults every return value to C int. On 64-bit Windows that silently
# truncates HANDLE and pointer returns, so GlobalLock gets handed a corrupted
# handle and writes through a bogus pointer. Declare them all explicitly.
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN = 0x11, 0x12, 0x10, 0x5B, 0x5C
VK_V, VK_RETURN = 0x56, 0x0D
VK_A, VK_C = 0x41, 0x43


ULONG_PTR = wintypes.WPARAM  # ULONG_PTR: 8 bytes on x64, 4 on x86


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    """Never sent, but it is the largest union member and therefore sets
    sizeof(INPUT). SendInput rejects any cbSize that is not exactly right, and
    fails silently when it does, so this cannot be left out."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(*inputs: INPUT) -> None:
    array = (INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _key(vk: int, up: bool = False) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)),
    )


def _unicode_char(code: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, code, flags, 0, 0)))


def wait_for_modifier_release(timeout_s: float = 1.5) -> None:
    """Block until Ctrl/Alt/Shift/Win are physically up.

    The talk chord is Ctrl+Alt. Pasting while they are still down turns Ctrl+V
    into Ctrl+Alt+V, which most apps either ignore or treat as some other
    command, so the text silently never arrives.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not any(
            _user32.GetAsyncKeyState(vk) & 0x8000
            for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)
        ):
            return
        time.sleep(0.01)
    log.warning("modifiers still held after %.1fs, pasting anyway", timeout_s)


# -- clipboard ------------------------------------------------------------
def _open_clipboard(retries: int = 12) -> bool:
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.02)  # another app is mid-copy; it will let go
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not _open_clipboard():
        return False
    try:
        _user32.EmptyClipboard()
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, buffer, size)
        _kernel32.GlobalUnlock(handle)
        # Windows owns the handle once SetClipboardData succeeds; do not free.
        return bool(_user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        _user32.CloseClipboard()


# -- delivery -------------------------------------------------------------
def paste(text: str, restore_clipboard: bool = True, restore_delay_ms: int = 500) -> bool:
    previous = get_clipboard_text() if restore_clipboard else None
    if not set_clipboard_text(text):
        return False
    wait_for_modifier_release()
    _send(_key(VK_CONTROL), _key(VK_V), _key(VK_V, up=True), _key(VK_CONTROL, up=True))

    if restore_clipboard and previous is not None:
        def restore_later() -> None:
            # Ctrl+V only becomes a clipboard read when the target app pumps its
            # message loop. Restoring on this thread would block that, and
            # restoring too early makes the app paste the OLD clipboard, which
            # could be anything the user copied earlier.
            time.sleep(max(0, restore_delay_ms) / 1000.0)
            if get_clipboard_text() == text:
                set_clipboard_text(previous)
            else:
                log.debug("clipboard changed under us, leaving it alone")

        threading.Thread(target=restore_later, daemon=True, name="voxkey-clipboard").start()
    return True


def type_text(text: str, delay_ms: int = 2) -> bool:
    wait_for_modifier_release()
    delay = max(0, delay_ms) / 1000.0
    for char in text:
        if char == "\n":
            _send(_key(VK_RETURN), _key(VK_RETURN, up=True))
        else:
            code = ord(char)
            if code > 0xFFFF:  # non-BMP needs an explicit surrogate pair
                code -= 0x10000
                for unit in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                    _send(_unicode_char(unit), _unicode_char(unit, up=True))
                continue
            _send(_unicode_char(code), _unicode_char(code, up=True))
        if delay:
            time.sleep(delay)
    return True


def press_enter() -> None:
    _send(_key(VK_RETURN), _key(VK_RETURN, up=True))


_user32.GetForegroundWindow.restype = wintypes.HWND

# What we last put into which window, so a second dictation can space itself
# against the first. There is no portable way to read the character before the
# caret in someone else's app, so the previous insertion is the best evidence
# available.
_last_insert: dict[str, object] = {"hwnd": 0, "tail": ""}


def _foreground_hwnd() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def needs_leading_space(text: str, config) -> bool:
    mode = config.get("output.leading_space", "smart")
    if mode == "never" or not text or text[0].isspace():
        return False
    # Never push a space in front of punctuation that closes what came before.
    if text[0] in ".,!?;:)]}'\"":
        return False
    if mode == "always":
        return True
    if _last_insert["hwnd"] != _foreground_hwnd():
        return False
    tail = str(_last_insert["tail"])
    return bool(tail) and not tail[-1].isspace()


def _remember(text: str, pressed_enter: bool) -> None:
    # Enter starts a fresh line or sends the message, so the next dictation
    # begins at a boundary and must not be given a leading space.
    _last_insert["hwnd"] = _foreground_hwnd()
    _last_insert["tail"] = "\n" if pressed_enter else text[-1:]


def forget_last_insert() -> None:
    _last_insert["hwnd"] = 0
    _last_insert["tail"] = ""


def deliver(text: str, config) -> tuple[bool, str]:
    """Returns (ok, human readable description of what happened)."""
    if not text:
        return False, "nothing to send"
    method = config.get("output.method", "paste")
    if method != "clipboard_only" and needs_leading_space(text, config):
        text = " " + text
    if config.get("output.trailing_space", False):
        text += " "

    try:
        if method == "clipboard_only":
            ok = set_clipboard_text(text)
            return ok, "copied to clipboard" if ok else "clipboard was locked"
        if method == "type":
            ok = type_text(text, int(config.get("output.type_delay_ms", 2)))
            detail = "typed"
        else:
            ok = paste(
                text,
                bool(config.get("output.restore_clipboard", True)),
                int(config.get("output.restore_delay_ms", 500)),
            )
            detail = "pasted"
        pressed_enter = False
        if ok and config.get("output.press_enter", False):
            time.sleep(0.05)
            press_enter()
            pressed_enter = True
        if ok:
            _remember(text, pressed_enter)
        return ok, detail if ok else "clipboard was locked"
    except Exception as exc:
        log.exception("delivery failed")
        return False, str(exc)


# -- is there anywhere for a paste to land? --------------------------------
# Ctrl+V into a list, a button or the desktop goes nowhere, and the clipboard
# is then quietly handed back, so the dictation simply vanishes. UI Automation
# can say what has keyboard focus, and when that is plainly not a text box the
# text is left on the clipboard and the user is told why.
CLSID_CUIAutomation = com.GUID.of("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation = com.GUID.of("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")
_UIA_GET_FOCUSED = 8
_ELEMENT_GET_PATTERN, _ELEMENT_CONTROL_TYPE = 16, 21
_VALUE_PATTERN_ID, _VALUE_IS_READONLY = 10002, 5

# UIA control types that cannot take typed text, with how to name them.
NON_TEXT_TYPES = {
    50000: "a button", 50001: "a calendar", 50002: "a checkbox", 50005: "a link",
    50006: "an image", 50007: "a list item", 50008: "a list", 50009: "a menu",
    50010: "a menu bar", 50011: "a menu item", 50012: "a progress bar",
    50013: "a radio button", 50014: "a scroll bar", 50015: "a slider",
    50016: "a spinner", 50017: "a status bar", 50018: "a tab strip", 50019: "a tab",
    50021: "a toolbar", 50022: "a tooltip", 50023: "a tree", 50024: "a tree item",
    50028: "a grid", 50029: "a grid cell", 50031: "a split button", 50034: "a header",
    50035: "a header item", 50036: "a table", 50037: "a title bar",
    50038: "a separator", 50040: "an app bar",
}
TEXT_TYPE, DOCUMENT_TYPE, WINDOW_TYPE = 50020, 50030, 50032

# Terminals draw their own text and describe themselves to UIA in ways that
# vary by build, and Ctrl+V works in every one of them. Never second-guess.
TERMINALS = {
    "windowsterminal", "openconsole", "conhost", "cmd", "powershell", "pwsh", "mintty",
    "alacritty", "wezterm-gui", "hyper", "putty", "kitty", "tabby", "warp",
}


def _value_readonly(element: c_void_p) -> bool | None:
    """True or False if the control exposes a value, None if it has none."""
    pattern = c_void_p()
    com.call(
        element, _ELEMENT_GET_PATTERN, _VALUE_PATTERN_ID, byref(pattern),
        argtypes=(c_int, POINTER(c_void_p)),
    )
    if not pattern:
        return None
    try:
        readonly = c_int()
        com.call(pattern, _VALUE_IS_READONLY, byref(readonly), argtypes=(POINTER(c_int),))
        return bool(readonly.value)
    finally:
        com.release(pattern)


def focused_text_field(app: str = "") -> tuple[str, str]:
    """Where would a paste land right now?

    Returns ("text", "") when the focused control takes typed text, or when
    nobody can tell, and ("none", "a list item") when it plainly does not. Only
    a definite "none" ever stops a paste; the benefit of the doubt goes to
    pasting, which is what always happened before.
    """
    if app.lower() in TERMINALS:
        return "text", ""
    try:
        # A UIA client belongs on a worker thread in the multithreaded
        # apartment, which is exactly where this runs.
        com.initialize(multithreaded=True)
        automation = com.create(CLSID_CUIAutomation, IID_IUIAutomation)
    except OSError:
        return "unknown", ""
    try:
        verdict = _read_focus(automation)
        # A Chromium app switches its accessibility tree on the first time
        # anyone asks, and answers that first question with the bare minimum:
        # a page with no patterns on it. Ask again once it is awake.
        if verdict == ("text", "page"):
            time.sleep(0.05)
            verdict = _read_focus(automation)
        return (verdict[0], "") if verdict[1] == "page" else verdict
    except OSError:
        return "unknown", ""
    finally:
        com.release(automation)


def _read_focus(automation: c_void_p) -> tuple[str, str]:
    element = c_void_p()
    com.call(automation, _UIA_GET_FOCUSED, byref(element), argtypes=(POINTER(c_void_p),))
    if not element:
        return "unknown", ""
    try:
        kind = c_int()
        com.call(element, _ELEMENT_CONTROL_TYPE, byref(kind), argtypes=(POINTER(c_int),))
        if kind.value in NON_TEXT_TYPES:
            return "none", NON_TEXT_TYPES[kind.value]
        readonly = _value_readonly(element)
        if kind.value == TEXT_TYPE and readonly is None:
            return "none", "a piece of plain text"
        if kind.value in (DOCUMENT_TYPE, WINDOW_TYPE):
            # A browser with nothing focused reports the page itself, read-only.
            if readonly is True:
                return "none", "a page with no box selected"
            if readonly is None:
                return "text", "page"
        return "text", ""
    finally:
        com.release(element)


# -- reading what is already on screen ------------------------------------
_SENTINEL = "\x00voxkey-nothing-was-copied\x00"


def grab_text(config) -> tuple[str | None, str, int]:
    """Copy the field (or the selection) out of the focused app.

    Returns (text, why_not, hwnd). A sentinel is parked on the clipboard first,
    so an app that ignores Ctrl+C, or one where Ctrl+A grabbed files rather than
    text, is detected instead of silently returning the previous clipboard.
    """
    hwnd = _foreground_hwnd()
    previous = get_clipboard_text()
    wait_for_modifier_release()
    if not set_clipboard_text(_SENTINEL):
        return None, "the clipboard was locked", hwnd

    if config.get("fix.scope", "all") == "all":
        _send(_key(VK_CONTROL), _key(VK_A), _key(VK_A, up=True), _key(VK_CONTROL, up=True))
        time.sleep(0.05)
    _send(_key(VK_CONTROL), _key(VK_C), _key(VK_C, up=True), _key(VK_CONTROL, up=True))

    text = None
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        time.sleep(0.03)
        current = get_clipboard_text()
        if current is not None and current != _SENTINEL:
            text = current
            break

    if previous is not None:
        set_clipboard_text(previous)
    elif text is not None:
        set_clipboard_text("")

    if text is None:
        return None, "nothing was copied from that window", hwnd
    if not text.strip():
        return None, "that field is empty", hwnd
    return text, "", hwnd


def replace_selection(text: str, config, expect_hwnd: int) -> tuple[bool, str]:
    """Paste over the selection, but only if focus never moved."""
    if _foreground_hwnd() != expect_hwnd:
        set_clipboard_text(text)
        return False, "focus moved, so the fixed text is on the clipboard instead"
    ok = paste(
        text,
        bool(config.get("output.restore_clipboard", True)),
        int(config.get("output.restore_delay_ms", 500)),
    )
    # This replaced a selection rather than appending to it, so the spacing
    # tracker must not think the next dictation continues from here.
    forget_last_insert()
    return (ok, "replaced") if ok else (False, "the clipboard was locked")
