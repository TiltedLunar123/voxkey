"""faster-whisper wrapper: CUDA bootstrap, model lifecycle, transcription."""

from __future__ import annotations

import logging
import os
import site
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .cleanup.guard import collapse_loops

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

LANGUAGES = [
    ("Auto detect", ""), ("English", "en"), ("Spanish", "es"), ("French", "fr"),
    ("German", "de"), ("Italian", "it"), ("Portuguese", "pt"), ("Dutch", "nl"),
    ("Polish", "pl"), ("Russian", "ru"), ("Japanese", "ja"), ("Korean", "ko"),
    ("Chinese", "zh"), ("Hindi", "hi"), ("Arabic", "ar"), ("Turkish", "tr"),
]

_dll_dirs_added = False

# How much of the previous dictation is handed over as context. The prompt
# slot is 224 tokens and the vocabulary shares it.
CONTEXT_WORDS = 60


@dataclass
class Confidence:
    """What the recogniser thought of its own answer.

    avg_logprob is the mean per-token log probability, so nearer zero is more
    certain and anything below about -1 is the decoder guessing. no_speech is
    its estimate that the window held no speech at all. Both are per segment
    and both are averaged here weighted by how long each segment ran, so one
    confident sentence is not outvoted by a half-second of noise after it.
    """

    avg_logprob: float = 0.0
    no_speech: float = 0.0
    dropped: int = 0
    kept: int = 0

    @property
    def certain(self) -> bool:
        return self.kept > 0 and self.avg_logprob > SEGMENT_LOGPROB and self.no_speech < SEGMENT_NO_SPEECH


# A segment is thrown away only when the recogniser is both unsure of the words
# and fairly sure nobody was speaking. Either signal on its own is too eager:
# a quiet but clean sentence scores badly on no_speech, and an unusual name
# scores badly on logprob while being exactly what the user said.
SEGMENT_NO_SPEECH = 0.6
SEGMENT_LOGPROB = -1.0


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
        # What the recogniser made of the most recent take, for the caller's
        # hallucination check and for Diagnostics.
        self.last_confidence = Confidence()

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
    def hotwords(self) -> str | None:
        """The vocabulary, in the form the recogniser can actually use.

        Whisper has no word list. What it has is a slot for "the text that came
        before", and anything in that slot is more likely to be heard. An
        initial prompt fills it for the first thirty-second window only, and a
        dictation here regularly runs longer than that; hotwords fill it for
        every window, so a name said in the second minute gets the same help as
        one said in the first.
        """
        terms = [t.strip() for t in self.config.get("asr.vocabulary", []) if t.strip()]
        if self.extra_vocabulary:
            try:
                seen = {t.lower() for t in terms}
                terms += [t for t in self.extra_vocabulary() if t.lower() not in seen]
            except Exception:
                log.debug("could not read learned vocabulary", exc_info=True)
        return ", ".join(terms) if terms else None

    def temperatures(self) -> list[float]:
        """The decoding ladder.

        A single temperature switches off the recogniser's own recovery: when
        a window decodes into a degenerate loop or scores badly, faster-whisper
        retries it warmer, but only if it has somewhere warmer to go. Two steps
        up from the configured value are enough, and they cost nothing on a
        take that decodes cleanly the first time.
        """
        base = max(0.0, min(1.0, float(self.config.get("asr.temperature", 0.0))))
        return [min(1.0, base + step) for step in (0.0, 0.2, 0.4)]

    @staticmethod
    def context_prompt(previous: str, max_words: int = CONTEXT_WORDS) -> str | None:
        """The tail of the last dictation, shaped as Whisper's "text before".

        Whisper decodes each window as a continuation of whatever it is told
        came before, which is how it keeps casing, spelling and punctuation
        consistent across a long recording. Between two dictations a few
        minutes apart the same trick applies: the names and jargon of the last
        one are exactly what the next one is likely to contain. Kept short so
        it leaves the decoder room for the actual speech.
        """
        words = " ".join(previous.split())
        if not words:
            return None
        tail = words.split(" ")[-max_words:]
        return " ".join(tail)

    @staticmethod
    def _worth_keeping(segment, cfg) -> bool:
        """Is this segment speech, by the recogniser's own reckoning?

        Whisper answers every window with words whether or not there were any,
        which is where "Thank you." on a second of room tone comes from. It
        does say how sure it is, though, and that is a far better filter than
        a list of the phrases it happens to favour in English: it needs no
        upkeep, it covers the phrasings nobody thought to list, and it works
        the same in any language.
        """
        if not bool(cfg.get("asr.drop_unsure_segments", True)):
            return True
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        ceiling = float(cfg.get("asr.no_speech_threshold", SEGMENT_NO_SPEECH))
        floor = float(cfg.get("asr.logprob_threshold", SEGMENT_LOGPROB))
        return not (no_speech > ceiling and logprob < floor)

    def transcribe(self, audio: np.ndarray, context: str | None = None) -> str:
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
                    temperature=self.temperatures(),
                    vad_filter=bool(cfg.get("asr.vad_filter", True)),
                    vad_parameters={
                        "min_silence_duration_ms": int(cfg.get("asr.vad_min_silence_ms", 500)),
                        # The gate trims to where speech was detected, and a
                        # word that starts softly begins before that. Padding
                        # both ends back out is what stops "okay" losing its o.
                        "speech_pad_ms": int(cfg.get("asr.vad_speech_pad_ms", 200)),
                    },
                    condition_on_previous_text=bool(
                        cfg.get("asr.condition_on_previous_text", False)
                    ),
                    # The recogniser's own guards. Left at the library defaults
                    # before, which meant a window that decoded into a loop or
                    # into nothing was kept anyway.
                    no_speech_threshold=float(
                        cfg.get("asr.no_speech_threshold", SEGMENT_NO_SPEECH)
                    ),
                    log_prob_threshold=float(
                        cfg.get("asr.logprob_threshold", SEGMENT_LOGPROB)
                    ),
                    compression_ratio_threshold=float(
                        cfg.get("asr.compression_ratio_threshold", 2.4)
                    ),
                    initial_prompt=context or None,
                    hotwords=self.hotwords(),
                    word_timestamps=False,
                )
                kept, confidence = self._collect(segments, cfg)
                text = "".join(kept)
                self.last_confidence = confidence
            finally:
                self.status = "ready" if self._model is not None else "idle"
        if confidence.dropped:
            log.info(
                "dropped %d segment(s) the recogniser did not believe were speech",
                confidence.dropped,
            )
        text, loops = collapse_loops(text.strip())
        if loops:
            log.info("folded %d phrase(s) the recogniser got stuck repeating", loops)
        return text

    def _collect(self, segments, cfg) -> tuple[list[str], Confidence]:
        """Drain the generator, keeping the segments that look like speech."""
        kept: list[str] = []
        weight = 0.0
        logprob_sum = 0.0
        no_speech_sum = 0.0
        confidence = Confidence()
        for segment in segments:
            if not self._worth_keeping(segment, cfg):
                confidence.dropped += 1
                log.debug(
                    "dropped %r (no_speech %.2f, logprob %.2f)",
                    segment.text.strip()[:40],
                    getattr(segment, "no_speech_prob", 0.0),
                    getattr(segment, "avg_logprob", 0.0),
                )
                continue
            kept.append(segment.text)
            confidence.kept += 1
            span = max(0.1, float(getattr(segment, "end", 0.0) - getattr(segment, "start", 0.0)))
            weight += span
            logprob_sum += span * float(getattr(segment, "avg_logprob", 0.0) or 0.0)
            no_speech_sum += span * float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        if weight:
            confidence.avg_logprob = logprob_sum / weight
            confidence.no_speech = no_speech_sum / weight
        return kept, confidence
