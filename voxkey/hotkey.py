"""Global hold-to-talk chord detection via GetAsyncKeyState polling.

RegisterHotKey cannot bind a modifier-only chord like Ctrl+Alt, and a
WH_KEYBOARD_LL hook would swallow keys other apps still need. Polling the async
key state is stateless, needs no elevation and never interferes with the chord
reaching the foreground window.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("voxkey.hotkey")

_user32 = ctypes.windll.user32

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN, VK_ESCAPE = 0x5B, 0x5C, 0x1B

MODIFIER_VKS: dict[str, tuple[int, ...]] = {
    "ctrl": (VK_CONTROL, 0xA2, 0xA3),
    "alt": (VK_MENU, 0xA4, 0xA5),
    "shift": (VK_SHIFT, 0xA0, 0xA1),
    "win": (VK_LWIN, VK_RWIN),
}

# Every modifier scancode, so "did another key go down" can ignore them.
_ALL_MODIFIER_VKS = {vk for vks in MODIFIER_VKS.values() for vk in vks}

KEY_VKS: dict[str, int] = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08,
    "capslock": 0x14, "insert": 0x2D, "delete": 0x2E, "escape": VK_ESCAPE,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
KEY_VKS.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEY_VKS.update({str(d): 0x30 + d for d in range(10)})
KEY_VKS.update({f"f{n}": 0x6F + n for n in range(1, 25)})

# Keys that count as "the user is pressing something else, so this chord is the
# start of a real shortcut". It has to be an allowlist. Scanning the whole
# virtual-key range instead swept up media and browser keys, and a keyboard that
# latches VK_MEDIA_PLAY_PAUSE (0xB3) on permanently then cancelled every single
# dictation, with nothing to show for it.
TYPING_VKS: set[int] = set()
TYPING_VKS |= set(range(0x30, 0x3A))          # 0-9
TYPING_VKS |= set(range(0x41, 0x5B))          # A-Z
TYPING_VKS |= set(range(0x60, 0x70))          # numpad digits and operators
TYPING_VKS |= set(range(0x70, 0x88))          # F1-F24
TYPING_VKS |= {
    0x08, 0x09, 0x0D, 0x1B, 0x20,             # backspace, tab, enter, esc, space
    0x21, 0x22, 0x23, 0x24,                   # page up/down, end, home
    0x25, 0x26, 0x27, 0x28,                   # arrows
    0x2C, 0x2D, 0x2E,                         # print screen, insert, delete
    0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, # ;=,-./`
    0xDB, 0xDC, 0xDD, 0xDE, 0xDF, 0xE2,       # brackets, backslash, quote, oem
}


def key_label(modifiers: list[str], key: str) -> str:
    parts = [m.capitalize() for m in modifiers]
    if key:
        parts.append(key.upper() if len(key) == 1 else key.capitalize())
    return " + ".join(parts) if parts else "(unset)"


def _down(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _any_down(vks: tuple[int, ...]) -> bool:
    return any(_down(vk) for vk in vks)


class HotkeyListener(threading.Thread):
    """Watches one chord and reports press / release / cancel.

    States: idle -> armed (chord down, threshold not yet met) -> active.
    A non-chord key going down while armed or active cancels, so ordinary
    shortcuts such as Ctrl+Alt+Delete never leave a stray recording running.
    """

    POLL_S = 0.008

    def __init__(
        self,
        config,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_cancel: Callable[[], None],
        on_arm: Callable[[], None] | None = None,
        on_fix: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(daemon=True, name="voxkey-hotkey")
        self.config = config
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_cancel = on_cancel
        # Fired the instant the chord goes down, before the hold threshold, so
        # the microphone is already open by the time recording counts. Without
        # it the first word is lost to the threshold plus stream start-up.
        self.on_arm = on_arm
        # The fix chord is a tap, not a hold, so it fires once on the edge.
        self.on_fix = on_fix
        self._fix_fired = False
        self._armed = False
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._active = False
        self._suppress_until_release = False
        self._toggle_since: float | None = None

    # -- lifecycle -------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()

    def pause(self) -> None:
        """Ignore the chord, e.g. while the user is recording a new binding."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def active(self) -> bool:
        return self._active

    # -- chord evaluation ------------------------------------------------
    def _binding(self) -> tuple[list[str], str, str, float, bool]:
        hk = self.config.get("hotkey", {})
        return (
            [m.lower() for m in hk.get("modifiers", [])],
            (hk.get("key") or "").lower(),
            hk.get("mode", "hold"),
            max(0, int(hk.get("hold_threshold_ms", 250))) / 1000.0,
            bool(hk.get("cancel_on_other_key", True)),
        )

    def _fix_binding(self) -> tuple[list[str], str, bool]:
        fix = self.config.get("fix", {})
        return (
            [m.lower() for m in fix.get("modifiers", [])],
            (fix.get("key") or "").lower(),
            bool(fix.get("enabled", True)) and bool(self.on_fix),
        )

    def _chord_vks(self, modifiers: list[str], key: str) -> set[int]:
        vks: set[int] = set()
        for name in modifiers:
            vks.update(MODIFIER_VKS.get(name, ()))
        if key and key in KEY_VKS:
            vks.add(KEY_VKS[key])
        return vks

    def _chord_held(self, modifiers: list[str], key: str) -> bool:
        if not modifiers and not key:
            return False
        for name in modifiers:
            if not _any_down(MODIFIER_VKS.get(name, ())):
                return False
        # Modifiers not in the binding must be up, so Ctrl+Alt does not fire
        # when the user is actually holding Ctrl+Shift+Alt for something else.
        for name, vks in MODIFIER_VKS.items():
            if name not in modifiers and _any_down(vks):
                return False
        if key:
            return _down(KEY_VKS.get(key, 0))
        return True

    def _foreign_key_down(self, chord_vks: set[int], key: str) -> bool:
        """True if a typing key outside the chord is held (an ordinary shortcut).

        Media, volume and browser keys are deliberately not in TYPING_VKS: they
        are never part of a keyboard shortcut, and some keyboards report one as
        held forever, which would silently disable the chord.
        """
        for vk in TYPING_VKS:
            if vk in chord_vks or vk in _ALL_MODIFIER_VKS:
                continue
            if _down(vk):
                return True
        return False

    def stuck_key(self) -> int:
        """A typing key reported held right now, for the health check."""
        for vk in TYPING_VKS:
            if _down(vk):
                return vk
        return 0

    # -- main loop -------------------------------------------------------
    def run(self) -> None:
        held_since: float | None = None
        while not self._stop_event.is_set():
            try:
                held_since = self._poll(held_since)
            except Exception:
                # One bad iteration must never kill the thread. A dead listener
                # is indistinguishable from a dead app from the outside: the
                # chord simply stops working and nothing says why.
                log.exception("hotkey poll failed, continuing")
                time.sleep(0.25)

    def blocked_by(self) -> str:
        """A modifier that is held but is not part of the binding, if any.

        Windows can leave a modifier latched down after a focus change or an
        RDP session. The chord then never matches and the feature looks broken,
        so this is surfaced rather than worked around.
        """
        modifiers, key, _mode, _threshold, _cancel = self._binding()
        for name, vks in MODIFIER_VKS.items():
            if name not in modifiers and _any_down(vks):
                return name
        return ""

    def _poll(self, held_since: float | None) -> float | None:
        time.sleep(self.POLL_S)
        if self._paused.is_set():
            if self._active or self._armed:
                self._active = self._armed = False
                self.on_cancel()
            held_since = None
            return held_since

        # The fix chord is a superset of the talk chord, so it has to be
        # tested first or the talk chord would see a stray release.
        fix_modifiers, fix_key, fix_on = self._fix_binding()
        if fix_on and self._chord_held(fix_modifiers, fix_key):
            if not self._fix_fired:
                self._fix_fired = True
                if self._active or self._armed:
                    self._active = self._armed = False
                    self.on_cancel()
                self.on_fix()
            held_since = None
            return held_since
        self._fix_fired = False

        modifiers, key, mode, threshold, cancel_on_other = self._binding()
        chord_vks = self._chord_vks(modifiers, key)
        held = self._chord_held(modifiers, key)

        if self._suppress_until_release:
            if not held:
                self._suppress_until_release = False
                held_since = None
            return held_since

        if held and cancel_on_other and self._foreign_key_down(chord_vks, key):
            # The chord is a prefix for a real shortcut; back off entirely.
            if self._active or self._armed:
                self._active = self._armed = False
                self.on_cancel()
            self._suppress_until_release = True
            held_since = None
            return held_since

        if mode == "toggle":
            self._tick_toggle(held, threshold)
        else:
            held_since = self._tick_hold(held, threshold, held_since)
        return held_since

    def _tick_hold(self, held: bool, threshold: float, held_since: float | None):
        now = time.monotonic()
        if held:
            if held_since is None:
                held_since = now
                if not self._armed:
                    self._armed = True
                    if self.on_arm:
                        self.on_arm()
            elif not self._active and now - held_since >= threshold:
                self._active = True
                self.on_start()
        else:
            if self._active:
                self._active = False
                self.on_stop()
            elif self._armed:
                # Let go before the threshold: it was a shortcut, not speech.
                self.on_cancel()
            self._armed = False
            held_since = None
        return held_since

    def _tick_toggle(self, held: bool, threshold: float) -> None:
        if not held:
            self._toggle_since = None
            return
        now = time.monotonic()
        if self._toggle_since is None:
            self._toggle_since = now
            return
        if now - self._toggle_since < threshold:
            return
        # Suppress until release so one long hold flips the state exactly once.
        self._toggle_since = None
        self._suppress_until_release = True
        if self._active:
            self._active = False
            self.on_stop()
        else:
            self._active = True
            self.on_start()
