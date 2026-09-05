"""faster-whisper wrapper: CUDA bootstrap, model lifecycle, transcription."""

from __future__ import annotations

import logging
import os
import site
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("voxkey.asr")

# label -> (repo id understood by faster-whisper, rough download size)
MODELS: list[tuple[str, str, str]] = [
    ("tiny.en  - fastest, rough", "tiny.en", "75 MB"),
    ("base.en  - fast", "base.en", "145 MB"),
    ("small.en - good", "small.en", "480 MB"),
    ("medium.en - better", "medium.en", "1.5 GB"),
    ("distil-large-v3 - fast, near-large accuracy", "distil-large-v3", "1.5 GB"),
    ("large-v3-turbo - recommended", "large-v3-turbo", "1.6 GB"),
    ("large-v3 - most accurate, slowest", "large-v3", "3.1 GB"),
]
MODEL_IDS = [m[1] for m in MODELS]

LANGUAGES = [
    ("Auto detect", ""), ("English", "en"), ("Spanish", "es"), ("French", "fr"),
    ("German", "de"), ("Italian", "it"), ("Portuguese", "pt"), ("Dutch", "nl"),
    ("Polish", "pl"), ("Russian", "ru"), ("Japanese", "ja"), ("Korean", "ko"),
    ("Chinese", "zh"), ("Hindi", "hi"), ("Arabic", "ar"), ("Turkish", "tr"),
]

_dll_dirs_added = False


def ensure_cuda_dlls() -> None:
    """Put the pip-installed cuBLAS and cuDNN DLLs on the search path.

    ctranslate2 dlopens cublas64_12.dll and cudnn*.dll by name. The nvidia-*-cu12
    wheels drop them inside site-packages, which is not on PATH, so without this
    every CUDA run dies with "Library cublas64_12.dll is not found".
    """
    global _dll_dirs_added
    if _dll_dirs_added:
        return
    roots = [Path(p) for p in site.getsitepackages()] + [Path(site.getusersitepackages())]
    for root in roots:
        nvidia = root / "nvidia"
        if not nvidia.is_dir():
            continue
        for bindir in nvidia.glob("*/bin"):
            if any(bindir.glob("*.dll")):
                try:
                    os.add_dll_directory(str(bindir))
                    os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")
                except OSError:
                    pass
    _dll_dirs_added = True


def cuda_available() -> bool:
    ensure_cuda_dlls()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class Transcriber:
    """Owns one loaded model. All calls are serialised by an internal lock."""

    def __init__(self, config, extra_vocabulary=None) -> None:
        self.config = config
        # Callable returning the self-learned terms, merged into the ASR hint.
        self.extra_vocabulary = extra_vocabulary
        self._model = None
        self._signature: tuple | None = None
        self._lock = threading.RLock()
        self.status = "idle"
        self.last_error: str | None = None
        self.last_device: str = "-"

    # -- model management -------------------------------------------------
    def _resolve(self) -> tuple[str, str, str]:
        model = self.config.get("asr.model", "large-v3-turbo")
        device = self.config.get("asr.device", "auto")
        compute = self.config.get("asr.compute_type", "auto")
        if device == "auto":
            device = "cuda" if cuda_available() else "cpu"
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return model, device, compute

    def is_loaded(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._signature = None
            self.status = "idle"

    def load(self, progress=None) -> bool:
        """Load (downloading on first use). Returns True once usable."""
        model, device, compute = self._resolve()
        signature = (model, device, compute)
        with self._lock:
            if self._model is not None and self._signature == signature:
                return True
            ensure_cuda_dlls()
            self.status = "loading"
            self.last_error = None
            if progress:
                progress(f"Loading {model} on {device}...")
            try:
                from faster_whisper import WhisperModel

                started = time.time()
                self._model = WhisperModel(
                    model,
                    device=device,
                    compute_type=compute,
                    cpu_threads=max(4, (os.cpu_count() or 8) // 2),
                )
                self._signature = signature
                self.last_device = f"{device}/{compute}"
                self.status = "ready"
                log.info("loaded %s on %s in %.1fs", model, device, time.time() - started)
                return True
            except Exception as exc:
                self._model = None
                self.status = "error"
                self.last_error = str(exc)
                log.exception("model load failed")
                # A CUDA box missing its runtime should still dictate on CPU.
                if device == "cuda":
                    log.warning("falling back to CPU")
                    if progress:
                        progress("CUDA unavailable, falling back to CPU...")
                    self.config.set("asr.device", "cpu")
                    self.config.set("asr.compute_type", "auto")
                    return self.load(progress)
                return False

    def warm(self) -> None:
        """Run one throwaway pass so the first real dictation is not slow.

        The Silero VAD weights and the CUDA kernels load lazily on the first
        transcribe, which costs about ten seconds. Paying that at startup keeps
        it out of the user's way.
        """
        if not self.is_loaded():
            return
        try:
            self.transcribe(np.zeros(16000, dtype=np.float32))
        except Exception:
            log.debug("warm-up pass failed, ignoring")

    # -- transcription ----------------------------------------------------
    def _initial_prompt(self) -> str | None:
        terms = [t.strip() for t in self.config.get("asr.vocabulary", []) if t.strip()]
        if self.extra_vocabulary:
            try:
                seen = {t.lower() for t in terms}
                terms += [t for t in self.extra_vocabulary() if t.lower() not in seen]
            except Exception:
                log.debug("could not read learned vocabulary", exc_info=True)
        if not terms:
            return None
        # Whisper biases toward words seen in the prompt; a plain list is enough.
        return "Vocabulary: " + ", ".join(terms) + "."

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        if not self.load():
            raise RuntimeError(self.last_error or "model failed to load")
        cfg = self.config
        language = cfg.get("asr.language", "en") or None
        with self._lock:
            self.status = "transcribing"
            try:
                segments, _info = self._model.transcribe(
                    audio,
                    language=language,
                    beam_size=max(1, int(cfg.get("asr.beam_size", 5))),
                    temperature=float(cfg.get("asr.temperature", 0.0)),
                    vad_filter=bool(cfg.get("asr.vad_filter", True)),
                    vad_parameters={
                        "min_silence_duration_ms": int(cfg.get("asr.vad_min_silence_ms", 500))
                    },
                    condition_on_previous_text=bool(
                        cfg.get("asr.condition_on_previous_text", False)
                    ),
                    initial_prompt=self._initial_prompt(),
                    word_timestamps=False,
                )
                text = "".join(segment.text for segment in segments)
            finally:
                self.status = "ready" if self._model is not None else "idle"
        return text.strip()
