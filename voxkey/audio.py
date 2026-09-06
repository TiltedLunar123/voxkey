"""Microphone capture and the two feedback blips."""

from __future__ import annotations

import io
import logging
import struct
import threading
import time
import wave
from collections import deque
import winsound

import numpy as np
import sounddevice as sd

log = logging.getLogger("voxkey.audio")

PREROLL_S = 0.6   # audio kept from before the key went down
SAMPLE_RATE = 16000  # what Whisper expects; resampling anywhere else is waste
BLOCK = 1024


def list_input_devices() -> list[tuple[int | None, str]]:
    """(index, label) for every device that can record, default first."""
    devices: list[tuple[int | None, str]] = [(None, "System default")]
    try:
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0:
                api = sd.query_hostapis(info["hostapi"])["name"]
                devices.append((index, f"{info['name']}  ({api})"))
    except Exception:
        pass
    return devices


def resolve_device(config) -> int | None:
    """A usable input device index, or None to mean the system default.

    Device indices are not stable: PortAudio renumbers them whenever hardware
    or a driver changes, so a saved index quietly starts pointing at something
    else. A saved index here had become an HDMI output with zero input
    channels, and every attempt to open it failed with "Invalid number of
    channels" while the app reported it as the chosen microphone.
    """
    try:
        devices = sd.query_devices()
    except Exception:
        return None

    def has_input(index: int) -> bool:
        return 0 <= index < len(devices) and devices[index].get("max_input_channels", 0) > 0

    wanted_name = (config.get("audio.device_name") or "").strip()
    if wanted_name:
        for index, info in enumerate(devices):
            if info.get("max_input_channels", 0) > 0 and info["name"] == wanted_name:
                return index

    index = config.get("audio.device")
    if index is None:
        return None
    if has_input(int(index)):
        return int(index)
    label = devices[int(index)]["name"] if 0 <= int(index) < len(devices) else "gone"
    log.warning(
        "saved input device %s is now %r with no input channels, using the system default",
        index, label,
    )
    return None


def device_name(index: int | None) -> str:
    if index is None:
        return ""
    try:
        return sd.query_devices(index)["name"]
    except Exception:
        return ""


class Recorder:
    """Capture with a rolling pre-roll buffer.

    The stream is held open and every block goes into a short ring buffer. When
    a dictation begins, that ring is prepended, so speech from *before* the key
    went down is still captured. Without it the hold threshold plus the roughly
    100ms the audio stream needs to start swallows the first word, which is what
    turned "Hey, can you..." into "Emge."
    """

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._blocks: list[np.ndarray] = []
        self._ring: deque[np.ndarray] = deque(maxlen=1)
        self._lock = threading.Lock()
        self._capturing = False
        self._level = 0.0
        self._peak = 0.0
        self._device: int | None = None
        self._channels = 1
        self._last_block = 0.0
        self.error: str | None = None
        self.set_preroll(PREROLL_S)

    def set_preroll(self, seconds: float) -> None:
        blocks = max(1, int(seconds * SAMPLE_RATE / BLOCK))
        with self._lock:
            self._ring = deque(self._ring, maxlen=blocks)

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def recording(self) -> bool:
        return self._capturing

    @property
    def level(self) -> float:
        """0..1 RMS of the most recent block, for the meter."""
        return self._level

    @property
    def peak(self) -> float:
        return self._peak

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        self._last_block = time.monotonic()
        if status:
            log.debug("input stream status: %s", status)
        # Whisper wants mono. Averaging rather than taking channel zero, because
        # on some interfaces the microphone is wired to the right channel only.
        block = indata[:, 0].copy() if indata.shape[1] == 1 else indata.mean(axis=1)
        with self._lock:
            self._ring.append(block)
            if self._capturing:
                self._blocks.append(block)
        rms = float(np.sqrt(np.mean(np.square(block)))) if frames else 0.0
        # Perceptual-ish curve so quiet speech still moves the meter.
        self._level = min(1.0, rms * 12.0)
        if self._capturing and frames:
            self._peak = max(self._peak, float(np.max(np.abs(block))))

    # -- stream lifecycle -------------------------------------------------
    def _channel_options(self, device: int | None) -> list[int]:
        """Mono first, then whatever the device actually offers.

        Plenty of interfaces expose no mono mode at all: every input on the
        machine this was built on reports two or four channels, and asking for
        one gets "Invalid number of channels" from PortAudio.
        """
        options = [1]
        try:
            info = sd.query_devices(device, kind="input")
            most = int(info.get("max_input_channels", 0))
        except Exception:
            most = 0
        for count in (2, most):
            if count > 1 and count not in options:
                options.append(count)
        return options

    def open_monitor(self, device: int | None = None) -> bool:
        """Hold the input stream open so the ring buffer is always warm."""
        if self._stream is not None and device == self._device:
            return True
        self.close_monitor()
        self.error = None
        for channels in self._channel_options(device):
            try:
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=channels,
                    dtype="float32",
                    blocksize=BLOCK,
                    device=device,
                    callback=self._callback,
                )
                stream.start()
            except Exception as exc:
                self.error = str(exc)
                continue
            self._stream = stream
            self._channels = channels
            self._device = device
            self._last_block = time.monotonic()
            if channels > 1:
                log.info("opened the microphone with %d channels, mixing to mono", channels)
            return True
        self._stream = None
        return False

    def is_stalled(self, timeout: float = 1.5) -> bool:
        """Open, but no audio has arrived for a while.

        A held-open input stream does not raise when the device is taken away,
        put to sleep, or grabbed exclusively by something else. The callback
        simply stops. Every take after that is silence, and the app looks like
        the hotkey has died when it is really the microphone.
        """
        if self._stream is None:
            return False
        return (time.monotonic() - self._last_block) > timeout

    def close_monitor(self) -> None:
        stream, self._stream = self._stream, None
        self._capturing = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._ring.clear()
            self._blocks = []

    # -- one dictation ----------------------------------------------------
    def start(self, device: int | None = None) -> bool:
        """Begin a take, keeping whatever is already in the ring buffer."""
        if not self.open_monitor(device):
            return False
        self._peak = 0.0
        with self._lock:
            # Everything heard in the last moment becomes the head of the take.
            self._blocks = list(self._ring)
            self._capturing = True
        return True

    def stop(self) -> np.ndarray:
        with self._lock:
            self._capturing = False
            blocks, self._blocks = self._blocks, []
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks).astype(np.float32)

    def discard(self) -> None:
        with self._lock:
            self._capturing = False
            self._blocks = []


SILENCE_PEAK = 0.012   # below this a clip is room tone, not speech


def is_silent(audio: np.ndarray) -> bool:
    if audio.size == 0:
        return True
    return float(np.max(np.abs(audio))) < SILENCE_PEAK


def normalise(audio: np.ndarray, gain: float = 1.0, target_peak: float = 0.85) -> np.ndarray:
    """Apply user gain, then lift quiet takes without clipping loud ones."""
    if audio.size == 0:
        return audio
    audio = audio * float(gain)
    peak = float(np.max(np.abs(audio)))
    # Amplifying near-silence by 8x just makes loud room tone, and Whisper
    # answers loud room tone with "Thank you." Leave it quiet instead.
    if SILENCE_PEAK <= peak < target_peak:
        audio = audio * min(target_peak / peak, 8.0)
    return np.clip(audio, -1.0, 1.0)


def _tone_wav(freq: float, ms: int, volume: float = 0.25) -> bytes:
    rate = 22050
    n = int(rate * ms / 1000)
    t = np.arange(n) / rate
    # Short fades stop the click you get from starting a sine at full amplitude.
    fade = max(1, n // 8)
    env = np.ones(n)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    samples = (np.sin(2 * np.pi * freq * t) * env * volume * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


_TONES = {
    "start": _tone_wav(880, 70),
    "stop": _tone_wav(587, 70),
    "error": _tone_wav(220, 160),
}


def blip(kind: str) -> None:
    """Fire and forget; a failed beep must never break a dictation."""
    data = _TONES.get(kind)
    if not data:
        return
    try:
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        pass
