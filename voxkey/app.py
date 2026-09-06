"""The engine: chord in, cleaned text out."""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import audio as audio_mod
from . import context as context_mod
from . import inject
from .asr import Transcriber
from .learn import Learner
from .cleanup import CleanupPipeline
from .cleanup.pipeline import fix_text
from .config import Config
from .history import Entry, History
from .hotkey import HotkeyListener
from .overlay import Overlay

log = logging.getLogger("voxkey.app")

# What Whisper emits when handed near-silence. Seen in the wild as a 1.1 second
# clip transcribing to "Thank you." with nothing actually spoken.
HALLUCINATIONS = {
    "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    "you", "you.", "bye.", "bye", ".", "okay.", "so", "thank you very much.",
    "subtitles by the amara.org community", "*", "[music]", "(music)",
}


def _is_hallucination(text: str, seconds: float) -> bool:
    """Only ever true for very short clips; long ones are real speech."""
    return seconds < 3.0 and text.strip().lower() in HALLUCINATIONS


class Engine(QObject):
    # Emitted from the hotkey thread, handled on the GUI thread.
    _start_requested = Signal()
    _stop_requested = Signal()
    _cancel_requested = Signal()
    _arm_requested = Signal()
    _fix_requested = Signal()

    state_changed = Signal(str, str)      # state, detail
    dictation_done = Signal(object)       # history.Entry
    notice = Signal(str, str)             # title, body
    ready_changed = Signal(str)           # human readable engine status

    # The worker thread must never touch the overlay widget directly. Emitting
    # instead gives a queued connection onto the GUI thread, which is the only
    # thread allowed to paint or to run a QTimer.
    _overlay_state = Signal(str, str)
    _overlay_finish = Signal(str, str, int)

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.recorder = audio_mod.Recorder()
        self.learner = Learner(config)
        self.transcriber = Transcriber(config, extra_vocabulary=self.learner.promoted_terms)
        self.pipeline = CleanupPipeline(config)
        self.history = History(config)
        self.overlay = Overlay(config)
        self.busy = False
        self._started_at = 0.0
        self._session_profile = ""
        self._armed_at = 0.0
        self._active_session = False
        self.last_app = ""
        self.last_context = ""

        self._start_requested.connect(self._begin)
        self._stop_requested.connect(self._end)
        self._cancel_requested.connect(self._cancel)
        self._arm_requested.connect(self._arm)
        self._fix_requested.connect(self._fix)
        self._overlay_state.connect(self.overlay.set_state)
        self._overlay_finish.connect(self.overlay.finish)

        # The bar is a second way to drive a dictation, for when you would
        # rather click than hold the chord.
        self.overlay.start_requested.connect(self._begin)
        self.overlay.stop_requested.connect(self._end)
        self.overlay.cancel_requested.connect(self._cancel)
        self.overlay.paste_last_requested.connect(self.paste_last)

        self._meter = QTimer(self)
        self._meter.timeout.connect(self._pump_meter)

        self.hotkey = self._new_listener()
        self._blocked_for = 0
        self._mic_warned = False
        self._watchdog = QTimer(self)
        self._watchdog.timeout.connect(self._check_health)

    def _new_listener(self) -> HotkeyListener:
        return HotkeyListener(
            self.config,
            on_start=self._start_requested.emit,
            on_stop=self._stop_requested.emit,
            on_cancel=self._cancel_requested.emit,
            on_arm=self._arm_requested.emit,
            on_fix=self._fix_requested.emit,
        )

    def _check_health(self) -> None:
        """The chord going quiet must never be a silent failure."""
        if not self.hotkey.is_alive():
            log.error("the hotkey listener is not running, starting a new one")
            self.hotkey = self._new_listener()
            self.hotkey.start()
            self.notice.emit(
                "Hotkey restarted", "The key listener had stopped. It is running again."
            )
            return
        # The microphone is held open for the pre-roll buffer. It can stall, and
        # it can also fail to open in the first place: a virtual input device is
        # often not ready in the 40ms between login and this process starting.
        # Either way every dictation comes back silent, so both are retried.
        if self.config.get("audio.preroll", True):
            missing = not self.recorder.is_open
            if missing or self.recorder.is_stalled():
                log.error(
                    "the microphone is %s, reopening it",
                    "not open" if missing else "stalled",
                )
                device = audio_mod.resolve_device(self.config)
                self.recorder.close_monitor()
                if self.recorder.open_monitor(device):
                    if self._mic_warned:
                        self.notice.emit(
                            "Microphone reconnected", "The audio stream is running again."
                        )
                    self._mic_warned = False
                elif not self._mic_warned:
                    # Once, not every five seconds for as long as it is unplugged.
                    self._mic_warned = True
                    self.notice.emit(
                        "Microphone unavailable",
                        self.recorder.error or "the device did not open",
                    )
                return

        # A modifier latched down by Windows stops the chord matching for as
        # long as it stays stuck, which looks exactly like the app being broken.
        blocked = self.hotkey.blocked_by()
        stuck = self.hotkey.stuck_key()
        self._blocked_for = self._blocked_for + 1 if (blocked or stuck) else 0
        if self._blocked_for == 2:
            if blocked:
                detail = (f"Windows still reports {blocked.capitalize()} as held down, so "
                          f"the chord cannot match. Tap and release {blocked.capitalize()}.")
                short = f"{blocked.capitalize()} is stuck down"
            else:
                detail = (f"A key (virtual code 0x{stuck:02X}) has been reported held for "
                          "ten seconds, which cancels every dictation. Tap it to clear it.")
                short = f"Key 0x{stuck:02X} is stuck down"
            log.warning("hotkey blocked: %s", short)
            self.notice.emit("Hotkey is blocked", detail)
            self.ready_changed.emit(short)
        elif self._blocked_for == 0 and self.transcriber.is_loaded():
            self.ready_changed.emit(f"Ready ({self.transcriber.last_device})")

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.hotkey.start()
        self._watchdog.start(5000)
        self.overlay.refresh_visibility()
        if self.config.get("audio.preroll", True):
            # Hold the microphone open so the ring buffer already contains the
            # moment you started talking by the time the chord registers.
            if not self.recorder.open_monitor(audio_mod.resolve_device(self.config)):
                # Not fatal: the watchdog retries every five seconds, which is
                # what a device that is still waking up needs.
                log.warning(
                    "could not open the microphone yet, will retry: %s", self.recorder.error
                )
        if self.config.get("asr.preload_on_start", True):
            threading.Thread(target=self._preload, daemon=True, name="voxkey-preload").start()

    def _preload(self) -> None:
        self.ready_changed.emit("Loading speech model...")
        ok = self.transcriber.load(lambda msg: self.ready_changed.emit(msg))
        if ok:
            self.ready_changed.emit("Warming up...")
            self.transcriber.warm()
        self.ready_changed.emit(
            f"Ready ({self.transcriber.last_device})" if ok
            else f"Speech model failed: {self.transcriber.last_error}"
        )
        # Warming the rewriter is optional; a cold first rewrite just feels slow.
        if self.config.get("llm.enabled", True):
            self.pipeline.warm()

    def shutdown(self) -> None:
        self._watchdog.stop()
        self.hotkey.stop()
        self.recorder.close_monitor()
        self.overlay.dismiss()

    def reopen_microphone(self) -> None:
        """Called when the device is changed in Settings."""
        self.recorder.close_monitor()
        if self.config.get("audio.preroll", True):
            self.recorder.open_monitor(audio_mod.resolve_device(self.config))

    @property
    def active_profile(self) -> str:
        return self.config.get("cleanup.profile", "clean")

    def resolve_profile(self) -> tuple[str, str]:
        """Which profile this dictation should use, and why.

        Order: an explicit override, then what you have actually chosen in this
        app before, then the shipped rules, then the default.
        """
        app, title = context_mod.foreground_window()
        self.last_app = app
        if not self.config.get("context.auto_profile", True):
            return self.config.get("cleanup.profile", "clean"), ""
        learned = self.learner.profile_for(app)
        if learned:
            return learned, f"learned for {app}"
        rules = self.config.get("context.rules") or context_mod.DEFAULT_RULES
        hit = context_mod.match_profile(rules, app, title)
        if hit:
            return hit[0], f"{app or title[:20]} matched '{hit[1]}'"
        return self.config.get("cleanup.profile", "clean"), ""

    def note_manual_profile(self, profile: str) -> None:
        """The profile was changed by hand; tie that to the last app dictated into."""
        if self.last_app:
            self.learner.observe_profile_choice(self.last_app, profile)

    # -- recording --------------------------------------------------------
    def _arm(self) -> None:
        """Open the microphone the moment the chord goes down.

        Recording does not officially start until the hold threshold passes, but
        the audio from before that is kept. Otherwise the threshold plus the
        stream start-up swallows the first word, which is what turned a short
        take into "Emge."
        """
        if self.busy or self.recorder.recording or not self.config.get("audio.preroll", True):
            return
        if self.recorder.start(audio_mod.resolve_device(self.config)):
            self._armed_at = time.monotonic()

    def _begin(self) -> None:
        if self.busy:
            return
        if not self.recorder.recording:
            device = audio_mod.resolve_device(self.config)
            if not self.recorder.start(device):
                self.notice.emit(
                    "Microphone unavailable", self.recorder.error or "unknown error"
                )
                self.state_changed.emit("error", "No microphone")
                if self.config.get("ui.sounds", True):
                    audio_mod.blip("error")
                return
            self._armed_at = time.monotonic()
        self._active_session = True
        self._session_profile, why = self.resolve_profile()
        self.last_context = why
        if why:
            log.info("profile %s (%s)", self._session_profile, why)
        # Count from when the microphone actually opened, not from now.
        self._started_at = self._armed_at or time.monotonic()
        self.overlay.begin()
        self._meter.start(16)
        self.state_changed.emit("listening", "")
        if self.config.get("ui.sounds", True):
            audio_mod.blip("start")

    def _pump_meter(self) -> None:
        if not self.recorder.recording:
            return
        elapsed = time.monotonic() - self._started_at
        self.overlay.push_level(self.recorder.level, elapsed)
        if elapsed >= float(self.config.get("audio.max_duration_s", 300)):
            log.info("hit the maximum length, stopping")
            self._end()

    def _cancel(self) -> None:
        was_active = self._active_session
        self._active_session = False
        if not self.recorder.recording:
            return
        self._meter.stop()
        self.recorder.discard()
        if not was_active:
            return  # the chord was released before the threshold: a shortcut
        self.overlay.finish("cancelled", "Cancelled", 700)
        self.state_changed.emit("idle", "")

    def _end(self) -> None:
        if not self.recorder.recording:
            return
        if not self._active_session:
            self.recorder.discard()
            return
        self._active_session = False
        self._meter.stop()
        if self.config.get("ui.sounds", True):
            audio_mod.blip("stop")
        # Keep capturing for a moment after the key comes up. People release on
        # the last syllable, and cutting there clipped the final word.
        pad = max(0, int(self.config.get("audio.tail_pad_ms", 300)))
        QTimer.singleShot(pad, self._finalise)

    def _finalise(self) -> None:
        clip = self.recorder.stop()
        elapsed = len(clip) / audio_mod.SAMPLE_RATE
        minimum = float(self.config.get("audio.min_duration_ms", 350)) / 1000.0
        silent = audio_mod.is_silent(clip)
        if elapsed < minimum or silent:
            # Logged, because a run of these is what "the hotkey stopped
            # working" actually looks like from the outside.
            log.info(
                "discarded a take: %.2fs, peak %.4f, %s",
                elapsed, self.recorder.peak,
                "silent" if silent else "shorter than the minimum",
            )
            self.overlay.finish("cancelled", "Nothing heard", 800)
            self.state_changed.emit("idle", "")
            return

        self.busy = True
        self.overlay.set_state("transcribing")
        self.state_changed.emit("transcribing", "")
        threading.Thread(
            target=self._process, args=(clip, elapsed, self._session_profile or self.active_profile),
            daemon=True, name="voxkey-process",
        ).start()

    # -- worker -----------------------------------------------------------
    def _process(self, clip, elapsed: float, profile: str) -> None:
        try:
            clip = audio_mod.normalise(clip, float(self.config.get("audio.gain", 1.0)))
            raw = self.transcriber.transcribe(clip)
            if _is_hallucination(raw, elapsed):
                log.info("discarded a likely hallucination: %r", raw)
                raw = ""
            if not raw.strip():
                self._finish_ui("cancelled", "Nothing heard", 900)
                return

            self._overlay_state.emit("rewriting", "")
            self.state_changed.emit("rewriting", "")
            result = self.pipeline.process(raw, profile)
            text = result.text
            if not text.strip():
                self._finish_ui("cancelled", "Nothing heard", 900)
                return

            ok, detail = inject.deliver(text, self.config)
            entry = Entry(text=text, raw=raw, profile=profile, seconds=elapsed)
            self.history.add(entry)
            self.learner.observe_text(text)
            self.dictation_done.emit(entry)

            if result.warning:
                self.notice.emit("Cleanup fell back", result.warning)
            if ok:
                self._finish_ui("done", detail.capitalize(), 900)
            else:
                self._finish_ui("error", detail, 2000)
                self.notice.emit("Could not insert text", f"{detail}. It is on the clipboard.")
                inject.set_clipboard_text(text)
        except Exception as exc:
            log.exception("dictation failed")
            self._finish_ui("error", "Failed", 2200)
            self.notice.emit("Dictation failed", str(exc))
            if self.config.get("ui.sounds", True):
                audio_mod.blip("error")
        finally:
            self.busy = False

    def _finish_ui(self, state: str, detail: str, linger: int) -> None:
        self._overlay_finish.emit(state, detail, linger)
        self.state_changed.emit("idle", "")

    # -- fix what is already in the box ------------------------------------
    def _fix(self) -> None:
        if self.busy or self.recorder.recording:
            return
        self.busy = True
        threading.Thread(target=self._do_fix, daemon=True, name="voxkey-fix").start()

    def _do_fix(self) -> None:
        try:
            self._overlay_state.emit("rewriting", "Reading")
            self.state_changed.emit("rewriting", "")
            original, why, hwnd = inject.grab_text(self.config)
            if original is None:
                self._finish_ui("cancelled", "Nothing to fix", 1300)
                self.notice.emit("Nothing to fix", why[:1].upper() + why[1:] + ".")
                return

            self._overlay_state.emit("rewriting", "Fixing")
            result = fix_text(
                self.pipeline, original,
                lambda message: self._overlay_state.emit("rewriting", message),
            )
            if not result.text.strip():
                self._finish_ui("error", "Fix failed", 2000)
                return
            if result.text.strip() == original.strip():
                self._finish_ui("done", "Already fine", 1300)
                return

            ok, detail = inject.replace_selection(result.text, self.config, hwnd)
            self.history.add(Entry(
                text=result.text, raw=original, profile=result.profile, seconds=0.0
            ))
            self.learner.observe_text(result.text)
            if result.warning:
                self.notice.emit("Only part was fixed", result.warning)
            if ok:
                self._finish_ui("done", "Fixed", 1100)
            else:
                self._finish_ui("error", "Not replaced", 2200)
                self.notice.emit("Could not replace the text", detail)
        except Exception as exc:
            log.exception("grammar fix failed")
            self._finish_ui("error", "Fix failed", 2200)
            self.notice.emit("Grammar fix failed", str(exc))
        finally:
            self.busy = False

    # -- used by the settings window and the bar's menu --------------------
    def redeliver(self, text: str) -> tuple[bool, str]:
        return inject.deliver(text, self.config)

    def paste_last(self) -> None:
        if not self.history.entries:
            self.notice.emit("Nothing to paste", "No dictation has been recorded yet.")
            return
        self.redeliver(self.history.entries[0].text)
