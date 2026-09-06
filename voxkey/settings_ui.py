"""The settings window. Every control writes through to disk immediately."""

from __future__ import annotations

import os
import subprocess
import threading
import webbrowser

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTabWidget,
    QVBoxLayout, QWidget,
)

from . import asr as asr_mod
from . import audio as audio_mod
from .config import CONFIG_DIR, LOG_PATH, PROFILES
from .hotkey import KEY_VKS, key_label
from .overlay import POSITIONS

STYLE = """
QWidget { background: #12151c; color: #e6ebf5; font-family: 'Segoe UI'; font-size: 13px; }
QTabWidget::pane { border: 1px solid #232936; border-radius: 8px; top: -1px; }
QTabBar::tab { background: transparent; padding: 9px 15px; margin-right: 2px;
               border-top-left-radius: 7px; border-top-right-radius: 7px; color: #93a0b8; }
QTabBar::tab:selected { background: #1b2130; color: #e6ebf5; }
QTabBar::tab:hover:!selected { color: #cdd6e8; }
QGroupBox { border: 1px solid #232936; border-radius: 9px; margin-top: 16px; padding: 14px 12px 10px 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #7dd3c0; font-weight: 600; }
QPushButton { background: #222a3a; border: 1px solid #303a4d; border-radius: 7px; padding: 7px 13px; }
QPushButton:hover { background: #2b3547; }
QPushButton:disabled { color: #5a6478; background: #1a202b; }
QPushButton#primary { background: #2a6f63; border-color: #35887a; }
QPushButton#primary:hover { background: #338176; }
QPushButton#danger { background: #6b2b2b; border-color: #8a3a3a; }
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QTableWidget {
    background: #1a2029; border: 1px solid #2b3444; border-radius: 7px; padding: 5px 7px;
    selection-background-color: #2a6f63; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #3f8f80; }
QComboBox::drop-down { border: none; width: 18px; }
QHeaderView::section { background: #1b2130; border: none; padding: 6px; color: #93a0b8; }
QTableWidget { gridline-color: #232936; }
QCheckBox, QRadioButton { spacing: 9px; padding: 3px 0; }
/* Sizing the indicator without also painting it leaves the checked state
   invisible, so every state is spelled out here. */
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px; height: 16px; border: 1px solid #3b4658; background: #171d26; }
QCheckBox::indicator { border-radius: 4px; }
/* Must be exactly half the box; a larger radius is ignored and draws square. */
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #4fae9c; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: #4fae9c; border-color: #6fd0bd; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { background: #1b2029; }
QSlider::groove:horizontal { height: 4px; background: #2b3444; border-radius: 2px; }
QSlider::handle:horizontal { width: 15px; margin: -6px 0; border-radius: 8px; background: #4fae9c; }
QProgressBar { border: 1px solid #2b3444; border-radius: 6px; text-align: center; background: #1a2029; }
QProgressBar::chunk { background: #2a6f63; border-radius: 5px; }
QLabel#hint { color: #8794ab; font-size: 12px; }
QLabel#head { font-size: 15px; font-weight: 600; }
QLabel#bigtitle { font-size: 21px; font-weight: 600; }
QLabel#chord { font-size: 27px; font-weight: 700; color: #7dd3c0; }
QScrollArea { border: none; }
"""

MODIFIERS = [("Ctrl", "ctrl"), ("Alt", "alt"), ("Shift", "shift"), ("Win", "win")]

_DIAG_LABELS = {
    "listener": "Key listener", "chord": "Talk chord", "blocked": "Blocked by",
    "device": "Input device", "stream": "Audio stream", "level": "Live level",
    "speech": "State", "speech_device": "Model", "ollama": "Ollama",
    "llm_model": "Model", "last": "Most recent",
}


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def head(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("head")
    return label


def scrollable(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(inner)
    return area


class SettingsWindow(QMainWindow):
    closed = Signal()

    # Background work (loading a model, previewing, pulling) reports back through
    # these. A worker thread has no event loop, so QTimer.singleShot from one is
    # not delivered; a queued signal is.
    _model_loaded_sig = Signal()
    _preview_ready_sig = Signal(object)
    _llm_status_sig = Signal(str)
    _pull_progress_sig = Signal(str, int)
    _pull_done_sig = Signal(str, str)

    def __init__(self, engine) -> None:
        super().__init__()
        self.engine = engine
        self.config = engine.config
        self.setWindowTitle("VoxKey settings")
        self.resize(880, 720)
        self.setStyleSheet(STYLE)

        self._model_loaded_sig.connect(self._model_loaded)
        self._preview_ready_sig.connect(self._preview_done)
        self._llm_status_sig.connect(lambda m: self._llm_status.setText(m))
        self._pull_progress_sig.connect(self._pull_progress)
        self._pull_done_sig.connect(lambda name, err: self._pull_finished(name, err or None))

        self.tabs = QTabWidget()
        self.tabs.addTab(scrollable(self._tab_dictation()), "Dictation")
        self.tabs.addTab(scrollable(self._tab_audio()), "Audio")
        self.tabs.addTab(scrollable(self._tab_transcription()), "Transcription")
        self.tabs.addTab(scrollable(self._tab_cleanup()), "Cleanup")
        self.tabs.addTab(scrollable(self._tab_smart()), "Smart")
        self.tabs.addTab(scrollable(self._tab_rewriter()), "Rewriter")
        self.tabs.addTab(scrollable(self._tab_output()), "Output")
        self.tabs.addTab(self._tab_history(), "History")
        self.tabs.addTab(scrollable(self._tab_advanced()), "Advanced")
        self.tabs.addTab(scrollable(self._tab_diagnostics()), "Diagnostics")

        # The tabs are built first because the simple page mirrors their widgets.
        back = QWidget()
        back_layout = QVBoxLayout(back)
        back_layout.setContentsMargins(10, 8, 10, 8)
        back_row = QHBoxLayout()
        simple_button = QPushButton("Back to the simple view")
        simple_button.clicked.connect(lambda: self._show_view(0))
        back_row.addWidget(simple_button)
        back_row.addStretch(1)
        back_layout.addLayout(back_row)
        back_layout.addWidget(self.tabs)

        self._simple_timer = QTimer(self)
        self._simple_timer.timeout.connect(
            lambda: self._simple_level.setValue(int(self.engine.recorder.level * 100))
        )

        self._views = QStackedWidget()
        self._views.addWidget(self._build_simple())
        self._views.addWidget(back)
        self.setCentralWidget(self._views)
        self._show_view(0)

        self.status = self.statusBar()
        self.status.showMessage("Settings save as you change them")
        engine.ready_changed.connect(self._on_ready)
        engine.dictation_done.connect(self._on_dictation)

    def _on_dictation(self, _entry) -> None:
        self.refresh_history()
        self.refresh_learned()

    def _on_ready(self, message: str) -> None:
        self.status.showMessage(message)
        if hasattr(self, "_simple_status"):
            self._simple_status.setText(message)

    # -- plumbing ---------------------------------------------------------
    def _write(self, path: str, value) -> None:
        self.config.set(path, value)
        self.config.save()

    def check(self, label: str, path: str, tip: str = "") -> QCheckBox:
        box = QCheckBox(label)
        box.setChecked(bool(self.config.get(path)))
        box.toggled.connect(lambda v, p=path: self._write(p, bool(v)))
        if tip:
            box.setToolTip(tip)
        return box

    def spin(self, path: str, low: int, high: int, suffix: str = "", step: int = 1) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setSingleStep(step)
        box.setSuffix(suffix)
        box.setValue(int(self.config.get(path) or 0))
        box.valueChanged.connect(lambda v, p=path: self._write(p, int(v)))
        return box

    def dspin(self, path: str, low: float, high: float, step: float = 0.1) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setSingleStep(step)
        box.setDecimals(2)
        box.setValue(float(self.config.get(path) or 0.0))
        box.valueChanged.connect(lambda v, p=path: self._write(p, float(v)))
        return box

    def combo(self, path: str, options: list[tuple[str, object]]) -> QComboBox:
        box = QComboBox()
        for label, value in options:
            box.addItem(label, value)
        current = self.config.get(path)
        index = box.findData(current)
        box.setCurrentIndex(index if index >= 0 else 0)
        box.currentIndexChanged.connect(
            lambda _i, b=box, p=path: self._write(p, b.currentData())
        )
        return box

    def line(self, path: str, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit(str(self.config.get(path) or ""))
        edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(lambda e=edit, p=path: self._write(p, e.text().strip()))
        return edit

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        self.config.save()
        self.closed.emit()
        super().closeEvent(event)

    # -- Dictation --------------------------------------------------------
    def _tab_dictation(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Talk key"))
        layout.addWidget(hint(
            "Hold the chord, speak, let go. VoxKey types what you said into whatever "
            "window has focus."
        ))

        chord_box = QGroupBox("Chord")
        chord_layout = QVBoxLayout(chord_box)
        row = QHBoxLayout()
        self._mod_boxes: dict[str, QCheckBox] = {}
        active = [m.lower() for m in self.config.get("hotkey.modifiers", [])]
        for label, value in MODIFIERS:
            box = QCheckBox(label)
            box.setChecked(value in active)
            box.toggled.connect(self._chord_changed)
            self._mod_boxes[value] = box
            row.addWidget(box)
        row.addSpacing(14)
        row.addWidget(QLabel("plus key:"))
        self._key_combo = QComboBox()
        self._key_combo.addItem("(none)", "")
        for name in sorted(KEY_VKS):
            self._key_combo.addItem(name.upper() if len(name) == 1 else name, name)
        index = self._key_combo.findData((self.config.get("hotkey.key") or "").lower())
        self._key_combo.setCurrentIndex(max(0, index))
        self._key_combo.currentIndexChanged.connect(self._chord_changed)
        row.addWidget(self._key_combo)
        row.addStretch(1)
        chord_layout.addLayout(row)

        self._chord_preview = QLabel()
        font = QFont("Segoe UI", 15)
        font.setWeight(QFont.DemiBold)
        self._chord_preview.setFont(font)
        self._chord_preview.setStyleSheet("color:#7dd3c0; padding:6px 0;")
        chord_layout.addWidget(self._chord_preview)
        self._chord_warning = hint("")
        chord_layout.addWidget(self._chord_warning)
        layout.addWidget(chord_box)
        self._refresh_chord_preview()

        behaviour = QGroupBox("Behaviour")
        form = QFormLayout(behaviour)
        form.addRow("Mode", self.combo("hotkey.mode", [
            ("Hold to talk", "hold"), ("Tap to start, tap to stop", "toggle"),
        ]))
        form.addRow("Hold before recording", self.spin("hotkey.hold_threshold_ms", 0, 2000, " ms", 50))
        form.addRow("", hint(
            "A short delay means an ordinary shortcut that starts with the same "
            "modifiers does not open the microphone."
        ))
        form.addRow("", self.check(
            "Cancel if another key is pressed", "hotkey.cancel_on_other_key",
            "Ctrl+Alt+Delete and friends abort the recording instead of dictating.",
        ))
        layout.addWidget(behaviour)

        default_box = QGroupBox("Default cleanup")
        default_layout = QVBoxLayout(default_box)
        default_layout.addWidget(hint(
            "Which profile a dictation uses unless you pick another from the tray menu."
        ))
        self._default_profile = self.combo(
            "cleanup.profile", [(PROFILES[k]["label"], k) for k in PROFILES]
        )
        self._default_profile.currentIndexChanged.connect(self._sync_profile_radio)
        default_layout.addWidget(self._default_profile)
        layout.addWidget(default_box)
        layout.addWidget(self._fix_box())
        layout.addStretch(1)
        return page

    def _chord_changed(self) -> None:
        modifiers = [value for value, box in self._mod_boxes.items() if box.isChecked()]
        self.config.set("hotkey.modifiers", modifiers)
        self.config.set("hotkey.key", self._key_combo.currentData() or "")
        self.config.save()
        self._refresh_chord_preview()

    def _refresh_chord_preview(self) -> None:
        modifiers = [v for v, b in self._mod_boxes.items() if b.isChecked()]
        key = self._key_combo.currentData() or ""
        self._chord_preview.setText(key_label(modifiers, key))
        message, serious = "", False
        if not modifiers and not key:
            message, serious = "Nothing is bound, so the microphone can never open.", True
        elif not modifiers:
            message, serious = "A bare key with no modifier will fire while you are typing.", True
        elif set(modifiers) == {"ctrl", "alt"} and not key:
            message = ("Note: on some non-US keyboard layouts AltGr reports as Ctrl+Alt. "
                       "If you type accented characters, add a key to the chord.")
        self._chord_warning.setText(message)
        self._chord_warning.setStyleSheet("color:#f0b360;" if serious else "")
        self._chord_warning.setVisible(bool(message))

    # -- Audio ------------------------------------------------------------
    def _tab_audio(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Microphone"))

        device_box = QGroupBox("Input device")
        device_layout = QVBoxLayout(device_box)
        row = QHBoxLayout()
        self._device_combo = QComboBox()
        self._reload_devices()
        self._device_combo.currentIndexChanged.connect(
            lambda: self._device_changed(self._device_combo.currentData())
        )
        row.addWidget(self._device_combo, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._reload_devices)
        row.addWidget(refresh)
        device_layout.addLayout(row)

        test_row = QHBoxLayout()
        self._test_button = QPushButton("Test microphone")
        self._test_button.clicked.connect(self._toggle_mic_test)
        test_row.addWidget(self._test_button)
        self._level_bar = QProgressBar()
        self._level_bar.setRange(0, 100)
        self._level_bar.setTextVisible(False)
        self._level_bar.setFixedHeight(16)
        test_row.addWidget(self._level_bar, 1)
        device_layout.addLayout(test_row)
        self._test_hint = hint("Speak normally. A healthy level sits around the middle.")
        device_layout.addWidget(self._test_hint)
        layout.addWidget(device_box)

        self._test_timer = QTimer(self)
        self._test_timer.timeout.connect(self._pump_test_level)

        limits = QGroupBox("Levels and limits")
        form = QFormLayout(limits)
        gain = QSlider(Qt.Horizontal)
        gain.setRange(50, 400)
        gain.setValue(int(float(self.config.get("audio.gain", 1.0)) * 100))
        self._gain_label = QLabel(f"{gain.value() / 100:.2f}x")
        gain.valueChanged.connect(self._gain_changed)
        gain_row = QHBoxLayout()
        gain_row.addWidget(gain, 1)
        gain_row.addWidget(self._gain_label)
        gain_wrap = QWidget()
        gain_wrap.setLayout(gain_row)
        form.addRow("Gain", gain_wrap)
        form.addRow("", hint(
            "Quiet takes are lifted automatically. Raise this only if that is not enough."
        ))
        form.addRow("", self.check(
            "Catch speech from before the key registers", "audio.preroll",
            "Holds the microphone open and keeps a short rolling buffer.",
        ))
        form.addRow("", hint(
            "Speaking the instant you press the chord used to lose the first word: "
            "the hold delay and opening the audio stream together cost about a third "
            "of a second. With this on, that audio is already captured."
        ))
        form.addRow("Keep recording after release", self.spin("audio.tail_pad_ms", 0, 1500, " ms", 50))
        form.addRow("", hint("Stops the last word being clipped when you let go mid-syllable."))
        form.addRow("Ignore clips shorter than", self.spin("audio.min_duration_ms", 0, 3000, " ms", 50))
        form.addRow("Stop recording after", self.spin("audio.max_duration_s", 10, 3600, " s", 10))
        layout.addWidget(limits)
        layout.addStretch(1)
        return page

    def _gain_changed(self, value: int) -> None:
        self._gain_label.setText(f"{value / 100:.2f}x")
        self._write("audio.gain", value / 100)

    def _reload_devices(self) -> None:
        current = self.config.get("audio.device")
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for index, label in audio_mod.list_input_devices():
            self._device_combo.addItem(label, index)
        found = self._device_combo.findData(current)
        self._device_combo.setCurrentIndex(found if found >= 0 else 0)
        self._device_combo.blockSignals(False)

    def _device_changed(self, device, source=None, mirror=None) -> None:
        self._write("audio.device", device)
        if mirror is not None:
            mirror.blockSignals(True)
            found = mirror.findData(device)
            mirror.setCurrentIndex(found if found >= 0 else 0)
            mirror.blockSignals(False)
        self.engine.reopen_microphone()

    def _monitoring(self) -> bool:
        return self._test_timer.isActive()

    def _toggle_mic_test(self) -> None:
        """Read the level off the engine's stream rather than opening a second one."""
        if self._monitoring():
            self._test_timer.stop()
            self._simple_timer.stop()
            self._level_bar.setValue(0)
            self._simple_level.setValue(0)
            self._test_button.setText("Test microphone")
            self._simple_test.setText("Test")
            self._test_hint.setText("Speak normally. A healthy level sits around the middle.")
            return
        if not self.engine.recorder.open_monitor(self.config.get("audio.device")):
            self._test_hint.setText(f"Could not open that device: {self.engine.recorder.error}")
            return
        self._test_timer.start(33)
        self._simple_timer.start(33)
        self._test_button.setText("Stop test")
        self._simple_test.setText("Stop")

    def _pump_test_level(self) -> None:
        self._level_bar.setValue(int(self.engine.recorder.level * 100))

    # -- Transcription ----------------------------------------------------
    def _tab_transcription(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Speech model"))
        layout.addWidget(hint(
            "This is the Whisper side. Larger models hear proper nouns and accents "
            "better; on a GPU even the large ones return in well under a second."
        ))

        model_box = QGroupBox("Model")
        model_layout = QVBoxLayout(model_box)
        self._model_combo = self.combo(
            "asr.model", [(f"{label}   [{size}]", value) for label, value, size in asr_mod.MODELS]
        )
        self._model_combo.currentIndexChanged.connect(self._model_changed)
        model_layout.addWidget(self._model_combo)

        row = QHBoxLayout()
        self._load_button = QPushButton("Load model now")
        self._load_button.setObjectName("primary")
        self._load_button.clicked.connect(self._load_model)
        row.addWidget(self._load_button)
        self._model_status = hint(self._model_status_text())
        row.addWidget(self._model_status, 1)
        model_layout.addLayout(row)
        model_layout.addWidget(hint(
            "A model you have not used before downloads on first load. It is cached, "
            "so this happens once per model."
        ))
        layout.addWidget(model_box)

        hardware = QGroupBox("Hardware")
        form = QFormLayout(hardware)
        form.addRow("Run on", self.combo("asr.device", [
            ("Automatic", "auto"), ("GPU (CUDA)", "cuda"), ("CPU", "cpu"),
        ]))
        form.addRow("Precision", self.combo("asr.compute_type", [
            ("Automatic", "auto"), ("float16 (GPU)", "float16"), ("int8 on float16", "int8_float16"),
            ("int8 (CPU)", "int8"), ("float32", "float32"),
        ]))
        detected = "detected" if asr_mod.cuda_available() else "not detected"
        form.addRow("", hint(f"CUDA is {detected} on this machine."))
        form.addRow("", self.check("Load the model when VoxKey starts", "asr.preload_on_start"))
        layout.addWidget(hardware)

        decoding = QGroupBox("Decoding")
        decode_form = QFormLayout(decoding)
        decode_form.addRow("Language", self.combo("asr.language", asr_mod.LANGUAGES))
        decode_form.addRow("Beam size", self.spin("asr.beam_size", 1, 10))
        decode_form.addRow("", hint("1 is fastest. 5 is the usual accuracy sweet spot."))
        decode_form.addRow("Temperature", self.dspin("asr.temperature", 0.0, 1.0, 0.1))
        decode_form.addRow("", self.check(
            "Skip silence before transcribing", "asr.vad_filter",
            "Voice activity detection. Keep this on; it removes room tone.",
        ))
        decode_form.addRow("Silence gap", self.spin("asr.vad_min_silence_ms", 100, 3000, " ms", 50))
        decode_form.addRow("", self.check(
            "Carry context between dictations", "asr.condition_on_previous_text",
            "Off by default. On, Whisper is more fluent but can repeat itself.",
        ))
        layout.addWidget(decoding)

        vocab_box = QGroupBox("Vocabulary")
        vocab_layout = QVBoxLayout(vocab_box)
        vocab_layout.addWidget(hint(
            "Names and jargon Whisper otherwise mangles. These are fed to the model as "
            "a hint, so spell them exactly how you want them written."
        ))
        self._vocab_list = QListWidget()
        self._vocab_list.setMaximumHeight(150)
        for term in self.config.get("asr.vocabulary", []):
            self._vocab_list.addItem(term)
        vocab_layout.addWidget(self._vocab_list)
        vocab_row = QHBoxLayout()
        self._vocab_input = QLineEdit()
        self._vocab_input.setPlaceholderText("Add a word or phrase, then press Enter")
        self._vocab_input.returnPressed.connect(self._add_vocab)
        vocab_row.addWidget(self._vocab_input, 1)
        add = QPushButton("Add")
        add.clicked.connect(self._add_vocab)
        vocab_row.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_vocab)
        vocab_row.addWidget(remove)
        vocab_layout.addLayout(vocab_row)
        layout.addWidget(vocab_box)
        layout.addStretch(1)
        return page

    def _model_status_text(self) -> str:
        transcriber = self.engine.transcriber
        if transcriber.status == "error":
            return f"Failed: {transcriber.last_error}"
        if transcriber.is_loaded():
            return f"Loaded on {transcriber.last_device}"
        return "Not loaded yet"

    def _model_changed(self) -> None:
        self.engine.transcriber.unload()
        self._model_status.setText("Not loaded yet (changed model)")

    def _load_model(self) -> None:
        self._load_button.setEnabled(False)
        self._model_status.setText("Loading...")

        def work() -> None:
            self.engine.transcriber.load()
            self._model_loaded_sig.emit()

        threading.Thread(target=work, daemon=True).start()

    def _model_loaded(self) -> None:
        self._load_button.setEnabled(True)
        self._model_status.setText(self._model_status_text())

    def _add_vocab(self) -> None:
        term = self._vocab_input.text().strip()
        if not term:
            return
        existing = [self._vocab_list.item(i).text() for i in range(self._vocab_list.count())]
        if term not in existing:
            self._vocab_list.addItem(term)
            self._save_vocab()
        self._vocab_input.clear()

    def _remove_vocab(self) -> None:
        for item in self._vocab_list.selectedItems():
            self._vocab_list.takeItem(self._vocab_list.row(item))
        self._save_vocab()

    def _save_vocab(self) -> None:
        terms = [self._vocab_list.item(i).text() for i in range(self._vocab_list.count())]
        self._write("asr.vocabulary", terms)

    # -- Cleanup ----------------------------------------------------------
    def _tab_cleanup(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("What happens to your words"))
        layout.addWidget(hint(
            "The first four profiles are pure text rules: instant, offline, and they "
            "never change your meaning. The rest hand the transcript to the local "
            "model for a rewrite."
        ))

        profile_box = QGroupBox("Profile")
        profile_layout = QVBoxLayout(profile_box)
        self._profile_radios: dict[str, QRadioButton] = {}
        current = self.config.get("cleanup.profile", "clean")
        for key, meta in PROFILES.items():
            radio = QRadioButton(meta["label"] + ("   (uses the model)" if meta["llm"] else ""))
            radio.setChecked(key == current)
            radio.toggled.connect(lambda on, k=key: self._profile_picked(k) if on else None)
            self._profile_radios[key] = radio
            profile_layout.addWidget(radio)
            blurb = hint("      " + meta["blurb"])
            profile_layout.addWidget(blurb)
        layout.addWidget(profile_box)

        preview_box = QGroupBox("Try it")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addWidget(hint("Edit the sample, hit Preview, and see exactly what would be typed."))
        self._preview_in = QPlainTextEdit(
            "so um i was thinking that we we should probably like move the auth check "
            "into into a separate module you know because right now it's basically "
            "doing two things at once new line and uh the sec plus stuff needs it too"
        )
        self._preview_in.setFixedHeight(78)
        preview_layout.addWidget(self._preview_in)
        button_row = QHBoxLayout()
        self._preview_button = QPushButton("Preview")
        self._preview_button.setObjectName("primary")
        self._preview_button.clicked.connect(self._run_preview)
        button_row.addWidget(self._preview_button)
        self._preview_note = hint("")
        button_row.addWidget(self._preview_note, 1)
        preview_layout.addLayout(button_row)
        self._preview_out = QPlainTextEdit()
        self._preview_out.setReadOnly(True)
        self._preview_out.setFixedHeight(88)
        self._preview_out.setStyleSheet("color:#7dd3c0;")
        preview_layout.addWidget(self._preview_out)
        layout.addWidget(preview_box)

        rules_box = QGroupBox("Text rules")
        rules_layout = QVBoxLayout(rules_box)
        rules_layout.addWidget(self.check("Turn spoken punctuation into real punctuation", "cleanup.voice_commands"))
        rules_layout.addWidget(self.check("Fold stutters and repeated words", "cleanup.collapse_repeats"))
        rules_layout.addWidget(self.check("Capitalise sentences", "cleanup.capitalize_sentences"))
        rules_layout.addWidget(self.check("Add a full stop at the end", "cleanup.ensure_final_punctuation"))
        rules_layout.addWidget(self.check(
            "Leave short fragments unpunctuated", "cleanup.strip_trailing_period_short",
            "Three words or fewer stay bare, so search boxes and filenames are not given a full stop.",
        ))
        rules_layout.addWidget(self.check(
            "Never use em dashes", "cleanup.no_em_dashes",
            "Strips them from the rules pass and forbids them in the model prompt.",
        ))
        layout.addWidget(rules_box)

        filler_box = QGroupBox("Filler words")
        filler_layout = QVBoxLayout(filler_box)
        filler_layout.addWidget(hint("Removed by the Clean up profile and before any rewrite. Comma separated."))
        self._fillers = QPlainTextEdit(", ".join(self.config.get("cleanup.fillers", [])))
        self._fillers.setFixedHeight(62)
        self._fillers.textChanged.connect(self._save_fillers)
        filler_layout.addWidget(self._fillers)
        layout.addWidget(filler_box)

        layout.addWidget(self._voice_command_box())
        layout.addWidget(self._replacements_box())

        custom_box = QGroupBox("Custom instruction")
        custom_layout = QVBoxLayout(custom_box)
        custom_layout.addWidget(hint("Used by the Custom profile. Describe the rewrite you want."))
        self._custom_prompt = QPlainTextEdit(self.config.get("cleanup.custom_prompt", ""))
        self._custom_prompt.setFixedHeight(72)
        self._custom_prompt.textChanged.connect(
            lambda: self._write("cleanup.custom_prompt", self._custom_prompt.toPlainText().strip())
        )
        custom_layout.addWidget(self._custom_prompt)
        layout.addWidget(custom_box)
        layout.addStretch(1)
        return page

    def _profile_picked(self, key: str) -> None:
        self._write("cleanup.profile", key)
        combo = getattr(self, "_default_profile", None)
        if combo is not None:
            index = combo.findData(key)
            if index >= 0 and combo.currentIndex() != index:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def _sync_profile_radio(self) -> None:
        key = self._default_profile.currentData()
        radio = getattr(self, "_profile_radios", {}).get(key)
        if radio is not None and not radio.isChecked():
            radio.blockSignals(True)
            radio.setChecked(True)
            radio.blockSignals(False)

    def _save_fillers(self) -> None:
        words = [w.strip() for w in self._fillers.toPlainText().split(",") if w.strip()]
        self._write("cleanup.fillers", words)

    def _run_preview(self) -> None:
        source = self._preview_in.toPlainText().strip()
        if not source:
            return
        profile = self.config.get("cleanup.profile", "clean")
        self._preview_button.setEnabled(False)
        self._preview_note.setText("Working...")

        def work() -> None:
            result = self.engine.pipeline.process(source, profile)
            self._preview_ready_sig.emit(result)

        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, result) -> None:
        self._preview_button.setEnabled(True)
        self._preview_out.setPlainText(result.text)
        engine = "model" if result.used_llm else "rules"
        note = f"{PROFILES[result.profile]['label']} via {engine}, {result.ms:.0f} ms"
        if result.warning:
            note += f"  -  {result.warning}"
        self._preview_note.setText(note)

    # -- editable tables --------------------------------------------------
    def _voice_command_box(self) -> QGroupBox:
        box = QGroupBox("Spoken punctuation")
        layout = QVBoxLayout(box)
        layout.addWidget(hint(
            "Say the phrase, get the character. Words that collide with ordinary "
            "speech, such as period and comma, ship switched off."
        ))
        self._vc_table = QTableWidget(0, 3)
        self._vc_table.setHorizontalHeaderLabels(["On", "When I say", "Insert"])
        self._vc_table.verticalHeader().setVisible(False)
        self._vc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._vc_table.setMaximumHeight(210)
        header = self._vc_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 44)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 120)
        for command in self.config.get("cleanup.voice_command_list", []):
            self._add_vc_row(command)
        self._vc_table.itemChanged.connect(self._save_voice_commands)
        layout.addWidget(self._vc_table)
        row = QHBoxLayout()
        add = QPushButton("Add rule")
        add.clicked.connect(lambda: self._add_vc_row({"say": "", "insert": "", "enabled": True}))
        row.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self._remove_rows(self._vc_table, self._save_voice_commands))
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _add_vc_row(self, command: dict) -> None:
        table = self._vc_table
        table.blockSignals(True)
        index = table.rowCount()
        table.insertRow(index)
        toggle = QTableWidgetItem()
        toggle.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        toggle.setCheckState(Qt.Checked if command.get("enabled") else Qt.Unchecked)
        table.setItem(index, 0, toggle)
        table.setItem(index, 1, QTableWidgetItem(command.get("say", "")))
        shown = command.get("insert", "").replace("\n", "\\n")
        table.setItem(index, 2, QTableWidgetItem(shown))
        table.blockSignals(False)

    def _save_voice_commands(self) -> None:
        table = self._vc_table
        commands = []
        for row in range(table.rowCount()):
            say = (table.item(row, 1).text() if table.item(row, 1) else "").strip()
            if not say:
                continue
            raw = table.item(row, 2).text() if table.item(row, 2) else ""
            commands.append({
                "say": say,
                "insert": raw.replace("\\n", "\n"),
                "enabled": table.item(row, 0).checkState() == Qt.Checked,
            })
        self._write("cleanup.voice_command_list", commands)

    def _remove_rows(self, table: QTableWidget, save) -> None:
        for index in sorted({i.row() for i in table.selectedIndexes()}, reverse=True):
            table.removeRow(index)
        save()

    def _replacements_box(self) -> QGroupBox:
        box = QGroupBox("Always replace")
        layout = QVBoxLayout(box)
        layout.addWidget(hint(
            "Applied last, after everything else, so these always win. Handy for "
            "product names and spellings the model keeps getting wrong."
        ))
        self._rep_table = QTableWidget(0, 4)
        self._rep_table.setHorizontalHeaderLabels(["Heard", "Write instead", "Regex", "Aa"])
        self._rep_table.verticalHeader().setVisible(False)
        self._rep_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._rep_table.setMaximumHeight(190)
        header = self._rep_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (2, 3):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            header.resizeSection(column, 52)
        for rule in self.config.get("cleanup.replacements", []):
            self._add_rep_row(rule)
        self._rep_table.itemChanged.connect(self._save_replacements)
        layout.addWidget(self._rep_table)
        row = QHBoxLayout()
        add = QPushButton("Add rule")
        add.clicked.connect(lambda: self._add_rep_row({"from": "", "to": ""}))
        row.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self._remove_rows(self._rep_table, self._save_replacements))
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _add_rep_row(self, rule: dict) -> None:
        table = self._rep_table
        table.blockSignals(True)
        index = table.rowCount()
        table.insertRow(index)
        table.setItem(index, 0, QTableWidgetItem(rule.get("from", "")))
        table.setItem(index, 1, QTableWidgetItem(rule.get("to", "")))
        for column, key in ((2, "regex"), (3, "case_sensitive")):
            cell = QTableWidgetItem()
            cell.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            cell.setCheckState(Qt.Checked if rule.get(key) else Qt.Unchecked)
            table.setItem(index, column, cell)
        table.blockSignals(False)

    def _save_replacements(self) -> None:
        table = self._rep_table
        rules = []
        for row in range(table.rowCount()):
            source = (table.item(row, 0).text() if table.item(row, 0) else "").strip()
            if not source:
                continue
            rules.append({
                "from": source,
                "to": table.item(row, 1).text() if table.item(row, 1) else "",
                "regex": table.item(row, 2).checkState() == Qt.Checked,
                "case_sensitive": table.item(row, 3).checkState() == Qt.Checked,
            })
        self._write("cleanup.replacements", rules)

    # -- Rewriter ---------------------------------------------------------
    def _tab_rewriter(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Local rewriting model"))
        layout.addWidget(hint(
            "Powers the tone profiles. Runs through Ollama on this machine, so nothing "
            "you dictate leaves the computer. The rule-based profiles never touch it."
        ))

        server = QGroupBox("Server")
        form = QFormLayout(server)
        form.addRow("", self.check("Use the model for tone profiles", "llm.enabled"))
        form.addRow("Ollama address", self.line("llm.base_url", "http://127.0.0.1:11434"))
        test_row = QHBoxLayout()
        test = QPushButton("Test connection")
        test.clicked.connect(self._test_llm)
        test_row.addWidget(test)
        self._llm_status = hint("")
        test_row.addWidget(self._llm_status, 1)
        wrap = QWidget()
        wrap.setLayout(test_row)
        form.addRow("", wrap)
        layout.addWidget(server)

        model_box = QGroupBox("Model")
        model_layout = QVBoxLayout(model_box)
        row = QHBoxLayout()
        self._llm_combo = QComboBox()
        self._llm_combo.setEditable(True)
        row.addWidget(self._llm_combo, 1)
        refresh = QPushButton("Refresh list")
        refresh.clicked.connect(self._reload_llm_models)
        row.addWidget(refresh)
        model_layout.addLayout(row)
        self._llm_warning = hint("")
        self._llm_warning.setStyleSheet("color:#f0b360;")
        model_layout.addWidget(self._llm_warning)
        self._reload_llm_models()
        self._llm_combo.currentTextChanged.connect(self._llm_model_changed)

        pull_row = QHBoxLayout()
        self._pull_input = QLineEdit()
        self._pull_input.setPlaceholderText("Download another model, for example qwen2.5:7b-instruct")
        pull_row.addWidget(self._pull_input, 1)
        self._pull_button = QPushButton("Download")
        self._pull_button.clicked.connect(self._pull_model)
        pull_row.addWidget(self._pull_button)
        model_layout.addLayout(pull_row)
        self._pull_bar = QProgressBar()
        self._pull_bar.setVisible(False)
        model_layout.addWidget(self._pull_bar)
        self._pull_status = hint("")
        model_layout.addWidget(self._pull_status)
        model_layout.addWidget(hint(
            "Pick an instruct model. Reasoning models such as plain qwen3 or deepseek-r1 "
            "think for thousands of tokens before answering, which turns a third of a "
            "second into half a minute."
        ))
        layout.addWidget(model_box)

        tuning = QGroupBox("Tuning")
        tune_form = QFormLayout(tuning)
        tune_form.addRow("Temperature", self.dspin("llm.temperature", 0.0, 1.5, 0.05))
        tune_form.addRow("", hint("Low keeps the rewrite faithful. Above about 0.5 it starts inventing."))
        tune_form.addRow("Context window", self.spin("llm.num_ctx", 1024, 32768, " tokens", 1024))
        tune_form.addRow("Give up after", self.spin("llm.timeout_s", 5, 300, " s", 5))
        tune_form.addRow("Keep loaded for", self.line("llm.keep_alive", "10m"))
        tune_form.addRow("", self.check(
            "Fall back to Clean up if the model is unavailable", "llm.fallback_to_rules",
            "On, a dead Ollama costs you tone, not your words.",
        ))
        layout.addWidget(tuning)
        layout.addStretch(1)
        return page

    def _reload_llm_models(self) -> None:
        current = self.config.get("llm.model", "")
        models = self.engine.pipeline.llm.installed_models()
        self._llm_combo.blockSignals(True)
        self._llm_combo.clear()
        self._llm_combo.addItems(models)
        self._llm_combo.setCurrentText(current)
        self._llm_combo.blockSignals(False)
        if not models:
            self._llm_status.setText("No models found. Is Ollama running?")
        self._check_thinking_model(current)

    def _llm_model_changed(self, name: str) -> None:
        self._write("llm.model", name.strip())
        self._check_thinking_model(name)

    def _check_thinking_model(self, name: str) -> None:
        lowered = (name or "").lower()
        reasoning = ("deepseek-r1", "qwq", "magistral", "reasoning", "-r1", "gpt-oss")
        hybrid = lowered.startswith("qwen3:") and "instruct" not in lowered
        if hybrid or any(token in lowered for token in reasoning):
            self._llm_warning.setText(
                "This looks like a reasoning model. It will think before every rewrite, "
                "which makes dictation feel broken. Prefer an instruct build."
            )
        else:
            self._llm_warning.setText("")
        self._llm_warning.setVisible(bool(self._llm_warning.text()))

    def _test_llm(self) -> None:
        self._llm_status.setText("Checking...")

        def work() -> None:
            client = self.engine.pipeline.llm
            ok = client.reachable()
            models = client.installed_models() if ok else []
            message = (
                f"Connected. {len(models)} chat model(s) available."
                if ok else "No answer from Ollama at that address."
            )
            self._llm_status_sig.emit(message)

        threading.Thread(target=work, daemon=True).start()

    def _pull_model(self) -> None:
        name = self._pull_input.text().strip()
        if not name:
            return
        self._pull_button.setEnabled(False)
        self._pull_bar.setVisible(True)
        self._pull_bar.setValue(0)
        self._pull_status.setText("Starting...")

        def work() -> None:
            try:
                for status, fraction in self.engine.pipeline.llm.pull(name):
                    self._pull_progress_sig.emit(status, int(fraction * 100))
                self._pull_done_sig.emit(name, "")
            except Exception as exc:
                self._pull_done_sig.emit(name, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _pull_progress(self, status: str, percent: int) -> None:
        self._pull_status.setText(status)
        self._pull_bar.setValue(percent)

    def _pull_finished(self, name: str, error: str | None) -> None:
        self._pull_button.setEnabled(True)
        self._pull_bar.setVisible(False)
        if error:
            self._pull_status.setText(f"Download failed: {error}")
            return
        self._pull_status.setText(f"{name} is ready.")
        self._pull_input.clear()
        self._reload_llm_models()
        self._llm_combo.setCurrentText(name)

    # -- Output -----------------------------------------------------------
    def _tab_output(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Where the text goes"))

        method_box = QGroupBox("Delivery")
        form = QFormLayout(method_box)
        form.addRow("Method", self.combo("output.method", [
            ("Paste (fast, recommended)", "paste"),
            ("Type character by character", "type"),
            ("Copy to clipboard only", "clipboard_only"),
        ]))
        form.addRow("", hint(
            "Pasting is instant but briefly uses the clipboard. Typing works in the few "
            "apps that block paste, and is slower for long dictations."
        ))
        form.addRow("", self.check(
            "Put the old clipboard back afterwards", "output.restore_clipboard"
        ))
        form.addRow("Restore after", self.spin("output.restore_delay_ms", 100, 3000, " ms", 50))
        form.addRow("", hint(
            "How long to wait before handing the clipboard back. Too short and a slow "
            "app pastes whatever you had copied before instead of your dictation."
        ))
        form.addRow("Typing speed", self.spin("output.type_delay_ms", 0, 50, " ms per character"))
        layout.addWidget(method_box)

        spacing_box = QGroupBox("Spacing")
        spacing_form = QFormLayout(spacing_box)
        spacing_form.addRow("Space before the text", self.combo("output.leading_space", [
            ("Only when continuing (recommended)", "smart"),
            ("Always", "always"),
            ("Never", "never"),
        ]))
        spacing_form.addRow("", hint(
            "Dictate twice in a row and the second take needs a space in front of it, "
            "or the words run together. Windows gives no way to read the character "
            "before the cursor in another app, so this goes on what VoxKey itself "
            "last inserted in that window."
        ))
        spacing_form.addRow("", self.check("Add a trailing space", "output.trailing_space"))
        layout.addWidget(spacing_box)

        after_box = QGroupBox("After inserting")
        after_layout = QVBoxLayout(after_box)
        after_layout.addWidget(self.check(
            "Press Enter", "output.press_enter",
            "Sends the message straight away in a chat box. Careful in a code editor.",
        ))
        layout.addWidget(after_box)
        layout.addStretch(1)
        return page

    # -- History ----------------------------------------------------------
    def _tab_history(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(head("Recent dictations"))
        layout.addWidget(hint("Stored on this machine only. Click one to see the full text."))

        self._history_list = QListWidget()
        self._history_list.currentRowChanged.connect(self._show_history_entry)
        layout.addWidget(self._history_list, 3)

        self._history_detail = QPlainTextEdit()
        self._history_detail.setReadOnly(True)
        layout.addWidget(self._history_detail, 2)
        self._history_raw = hint("")
        layout.addWidget(self._history_raw)

        row = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy_history)
        row.addWidget(copy)
        insert = QPushButton("Insert into last window")
        insert.setObjectName("primary")
        insert.clicked.connect(self._insert_history)
        row.addWidget(insert)
        row.addStretch(1)
        clear = QPushButton("Clear all")
        clear.setObjectName("danger")
        clear.clicked.connect(self._clear_history)
        row.addWidget(clear)
        layout.addLayout(row)

        options = QHBoxLayout()
        options.addWidget(self.check("Keep a history", "history.enabled"))
        options.addSpacing(16)
        options.addWidget(QLabel("Remember"))
        options.addWidget(self.spin("history.max_items", 5, 1000, " dictations", 5))
        options.addStretch(1)
        layout.addLayout(options)

        self.refresh_history()
        return page

    def refresh_history(self) -> None:
        if not hasattr(self, "_history_list"):
            return
        row = self._history_list.currentRow()
        self._history_list.clear()
        for entry in self.engine.history.entries:
            label = PROFILES.get(entry.profile, {}).get("label", entry.profile)
            preview = entry.text.replace("\n", " ")
            if len(preview) > 76:
                preview = preview[:76] + "..."
            item = QListWidgetItem(f"{entry.day} {entry.when}   {label}\n{preview}")
            self._history_list.addItem(item)
        if self._history_list.count():
            self._history_list.setCurrentRow(min(max(row, 0), self._history_list.count() - 1))

    def _selected_entry(self):
        row = self._history_list.currentRow()
        entries = self.engine.history.entries
        return entries[row] if 0 <= row < len(entries) else None

    def _show_history_entry(self, _row: int) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._history_detail.clear()
            self._history_raw.setText("")
            return
        self._history_detail.setPlainText(entry.text)
        self._history_raw.setText(f"Heard: {entry.raw}")

    def _copy_history(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            from . import inject

            inject.set_clipboard_text(entry.text)
            self.status.showMessage("Copied", 2500)

    def _insert_history(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self.showMinimized()
        # Give focus a moment to land back on whatever was underneath.
        QTimer.singleShot(350, lambda: self.engine.redeliver(entry.text))

    def _clear_history(self) -> None:
        confirm = QMessageBox.question(
            self, "Clear history", "Delete every stored dictation? This cannot be undone."
        )
        if confirm == QMessageBox.Yes:
            self.engine.history.clear()
            self.refresh_history()
            self._show_history_entry(-1)

    # -- Advanced ---------------------------------------------------------
    def _tab_advanced(self) -> QWidget:
        from . import startup

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Everything else"))

        start_box = QGroupBox("Starting up")
        start_layout = QVBoxLayout(start_box)
        self._startup_check = QCheckBox("Start VoxKey when Windows starts")
        self._startup_check.setChecked(startup.is_enabled())
        self._startup_check.toggled.connect(self._toggle_startup)
        start_layout.addWidget(self._startup_check)
        self._startup_note = hint("")
        start_layout.addWidget(self._startup_note)
        start_layout.addWidget(self.check(
            "Start hidden in the tray", "ui.start_minimized",
            "Off, the settings window opens each time VoxKey launches.",
        ))
        layout.addWidget(start_box)

        feedback = QGroupBox("Feedback while dictating")
        feedback_layout = QVBoxLayout(feedback)
        feedback_layout.addWidget(self.check(
            "Show the Flow Bar", "ui.overlay",
            "The floating capsule you can click to dictate.",
        ))
        feedback_layout.addWidget(self.check(
            "Keep it on screen when idle", "ui.bar_always",
            "Off, the bar only appears while you are dictating.",
        ))
        feedback_layout.addWidget(hint(
            "Drag the bar anywhere; it snaps to the nearest edge and stays there. "
            "Right-click it for the menu, or to hide it for an hour."
        ))
        place_row = QHBoxLayout()
        place_row.addWidget(QLabel("Position"))
        self._advanced_place = self.combo("ui.bar_position", POSITIONS)
        self._advanced_place.currentIndexChanged.connect(
            lambda: self.engine.overlay.move_to_preset(self._advanced_place.currentData())
        )
        place_row.addWidget(self._advanced_place, 1)
        feedback_layout.addLayout(place_row)
        feedback_layout.addWidget(self.check("Play a blip when recording starts and stops", "ui.sounds"))
        feedback_layout.addWidget(self.check("Show a notification when something fails", "ui.notify_errors"))
        layout.addWidget(feedback)

        files = QGroupBox("Files")
        files_layout = QVBoxLayout(files)
        files_layout.addWidget(hint(f"Settings and history live in {CONFIG_DIR}"))
        row = QHBoxLayout()
        open_folder = QPushButton("Open settings folder")
        open_folder.clicked.connect(lambda: os.startfile(CONFIG_DIR))
        row.addWidget(open_folder)
        open_log = QPushButton("Open log")
        open_log.clicked.connect(self._open_log)
        row.addWidget(open_log)
        row.addStretch(1)
        files_layout.addLayout(row)
        layout.addWidget(files)

        danger = QGroupBox("Reset")
        danger_layout = QVBoxLayout(danger)
        danger_layout.addWidget(hint("Puts every setting back to how it shipped. History is kept."))
        reset = QPushButton("Reset all settings")
        reset.setObjectName("danger")
        reset.clicked.connect(self._reset_all)
        danger_layout.addWidget(reset, 0, Qt.AlignLeft)
        layout.addWidget(danger)

        from . import __version__

        layout.addWidget(hint(f"VoxKey {__version__}"))
        layout.addStretch(1)
        return page

    def _toggle_startup(self, enabled: bool) -> None:
        from . import startup

        ok, message = startup.set_enabled(enabled)
        self._write("advanced.start_with_windows", bool(enabled))
        self._startup_note.setText(message)
        if not ok:
            self._startup_check.blockSignals(True)
            self._startup_check.setChecked(startup.is_enabled())
            self._startup_check.blockSignals(False)

    def _open_log(self) -> None:
        if LOG_PATH.exists():
            os.startfile(LOG_PATH)
        else:
            self.status.showMessage("No log written yet", 3000)

    def _reset_all(self) -> None:
        confirm = QMessageBox.question(
            self, "Reset settings",
            "Put every setting back to its default? The window will close so the "
            "new values load cleanly.",
        )
        if confirm != QMessageBox.Yes:
            return
        from .config import DEFAULTS
        import copy as _copy

        self.config.data = _copy.deepcopy(DEFAULTS)
        self.config.save()
        self.close()

    # -- Simple view ------------------------------------------------------
    def _build_simple(self) -> QWidget:
        """One screen with the four things most people ever change."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(14)
        layout.setContentsMargins(26, 22, 26, 20)

        title = QLabel("Hold to talk")
        title.setObjectName("bigtitle")
        layout.addWidget(title)

        self._simple_chord = QLabel()
        self._simple_chord.setObjectName("chord")
        layout.addWidget(self._simple_chord)
        layout.addWidget(hint("Hold the keys, say your piece, let go. The text lands wherever you were typing."))

        row = QHBoxLayout()
        row.setSpacing(14)
        self._simple_mods: dict[str, QCheckBox] = {}
        active = [m.lower() for m in self.config.get("hotkey.modifiers", [])]
        for label, value in MODIFIERS:
            box = QCheckBox(label)
            box.setChecked(value in active)
            box.toggled.connect(self._simple_chord_changed)
            self._simple_mods[value] = box
            row.addWidget(box)
        row.addSpacing(8)
        self._simple_key = QComboBox()
        self._simple_key.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._simple_key.setMinimumContentsLength(12)
        self._simple_key.addItem("no extra key", "")
        for name in sorted(KEY_VKS):
            self._simple_key.addItem(name.upper() if len(name) == 1 else name, name)
        index = self._simple_key.findData((self.config.get("hotkey.key") or "").lower())
        self._simple_key.setCurrentIndex(max(0, index))
        self._simple_key.currentIndexChanged.connect(self._simple_chord_changed)
        row.addWidget(self._simple_key, 1)
        layout.addLayout(row)

        layout.addSpacing(6)
        layout.addWidget(QLabel("Tidy up what I said"))
        self._simple_profile = QComboBox()
        for key, meta in PROFILES.items():
            self._simple_profile.addItem(meta["label"], key)
        current = self._simple_profile.findData(self.config.get("cleanup.profile", "clean"))
        self._simple_profile.setCurrentIndex(max(0, current))
        self._simple_profile.currentIndexChanged.connect(self._simple_profile_changed)
        layout.addWidget(self._simple_profile)
        self._simple_blurb = hint("")
        layout.addWidget(self._simple_blurb)

        layout.addSpacing(6)
        layout.addWidget(QLabel("Microphone"))
        mic_row = QHBoxLayout()
        self._simple_device = QComboBox()
        # A long device name would otherwise set the whole window's width.
        self._simple_device.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._simple_device.setMinimumContentsLength(18)
        for device_index, label in audio_mod.list_input_devices():
            self._simple_device.addItem(label, device_index)
        found = self._simple_device.findData(self.config.get("audio.device"))
        self._simple_device.setCurrentIndex(found if found >= 0 else 0)
        self._simple_device.currentIndexChanged.connect(
            lambda: self._device_changed(
                self._simple_device.currentData(), self._simple_device, self._device_combo
            )
        )
        mic_row.addWidget(self._simple_device, 1)
        self._simple_test = QPushButton("Test")
        self._simple_test.setFixedWidth(88)
        self._simple_test.clicked.connect(self._toggle_simple_test)
        mic_row.addWidget(self._simple_test)
        layout.addLayout(mic_row)
        self._simple_level = QProgressBar()
        self._simple_level.setRange(0, 100)
        self._simple_level.setTextVisible(False)
        self._simple_level.setFixedHeight(10)
        layout.addWidget(self._simple_level)

        layout.addSpacing(6)
        place_row = QHBoxLayout()
        place_row.addWidget(QLabel("Bar sits"))
        self._simple_place = QComboBox()
        for label, key in POSITIONS:
            self._simple_place.addItem(label, key)
        found = self._simple_place.findData(self.config.get("ui.bar_position", "bottom-right"))
        self._simple_place.setCurrentIndex(max(0, found))
        self._simple_place.currentIndexChanged.connect(
            lambda: self.engine.overlay.move_to_preset(self._simple_place.currentData())
        )
        place_row.addWidget(self._simple_place, 1)
        layout.addLayout(place_row)

        layout.addSpacing(4)
        self._simple_startup = QCheckBox("Start VoxKey when Windows starts")
        from . import startup

        self._simple_startup.setChecked(startup.is_enabled())
        self._simple_startup.toggled.connect(self._toggle_startup_simple)
        layout.addWidget(self._simple_startup)

        layout.addStretch(1)
        self._simple_status = hint("Loading the speech model...")
        layout.addWidget(self._simple_status)

        footer = QHBoxLayout()
        advanced = QPushButton("All settings")
        advanced.clicked.connect(lambda: self._show_view(1))
        footer.addWidget(advanced)
        footer.addStretch(1)
        hide = QPushButton("Hide")
        hide.setObjectName("primary")
        hide.clicked.connect(self.close)
        footer.addWidget(hide)
        layout.addLayout(footer)

        self._refresh_simple_labels()
        return page

    def _simple_chord_changed(self) -> None:
        modifiers = [value for value, box in self._simple_mods.items() if box.isChecked()]
        self.config.set("hotkey.modifiers", modifiers)
        self.config.set("hotkey.key", self._simple_key.currentData() or "")
        self.config.save()
        self._refresh_simple_labels()
        # Keep the advanced tab in step so the two views never disagree.
        for value, box in self._mod_boxes.items():
            box.blockSignals(True)
            box.setChecked(value in modifiers)
            box.blockSignals(False)
        self._key_combo.blockSignals(True)
        self._key_combo.setCurrentIndex(
            max(0, self._key_combo.findData(self._simple_key.currentData() or ""))
        )
        self._key_combo.blockSignals(False)
        self._refresh_chord_preview()

    def _simple_profile_changed(self) -> None:
        key = self._simple_profile.currentData()
        self._write("cleanup.profile", key)
        self._refresh_simple_labels()
        radio = self._profile_radios.get(key)
        if radio is not None:
            radio.blockSignals(True)
            radio.setChecked(True)
            radio.blockSignals(False)
        combo = self._default_profile
        combo.blockSignals(True)
        combo.setCurrentIndex(max(0, combo.findData(key)))
        combo.blockSignals(False)

    def _refresh_simple_labels(self) -> None:
        modifiers = [v for v, b in self._simple_mods.items() if b.isChecked()]
        key = self._simple_key.currentData() or ""
        self._simple_chord.setText(key_label(modifiers, key) or "nothing bound")
        profile = self._simple_profile.currentData()
        self._simple_blurb.setText(PROFILES.get(profile, {}).get("blurb", ""))

    def _toggle_startup_simple(self, enabled: bool) -> None:
        self._toggle_startup(enabled)
        self._startup_check.blockSignals(True)
        self._startup_check.setChecked(enabled)
        self._startup_check.blockSignals(False)

    def _toggle_simple_test(self) -> None:
        self._toggle_mic_test()

    def _show_view(self, index: int) -> None:
        self._views.setCurrentIndex(index)
        # A QStackedWidget takes its size from the largest page, so the hidden
        # advanced view would keep the simple one wide. Ignoring the size of
        # every page but the current one is the documented way round that.
        for page in range(self._views.count()):
            policy = QSizePolicy.Preferred if page == index else QSizePolicy.Ignored
            self._views.widget(page).setSizePolicy(policy, policy)
        self._views.widget(index).adjustSize()
        if index == 0:
            self.setMaximumWidth(620)
            self.resize(560, 660)
            self._sync_simple_from_config()
        else:
            self.setMaximumWidth(16777215)
            self.resize(920, 780)

    def _sync_simple_from_config(self) -> None:
        modifiers = [m.lower() for m in self.config.get("hotkey.modifiers", [])]
        for value, box in self._simple_mods.items():
            box.blockSignals(True)
            box.setChecked(value in modifiers)
            box.blockSignals(False)
        self._simple_key.blockSignals(True)
        self._simple_key.setCurrentIndex(
            max(0, self._simple_key.findData((self.config.get("hotkey.key") or "").lower()))
        )
        self._simple_key.blockSignals(False)
        self._simple_profile.blockSignals(True)
        self._simple_profile.setCurrentIndex(
            max(0, self._simple_profile.findData(self.config.get("cleanup.profile", "clean")))
        )
        self._simple_profile.blockSignals(False)
        self._refresh_simple_labels()

    # -- Smart ------------------------------------------------------------
    def _tab_smart(self) -> QWidget:
        from . import context as context_mod

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Following what you actually do"))
        layout.addWidget(hint(
            "VoxKey can pick the profile from the window you are dictating into, and "
            "adjust as it sees which one you really want. All of it stays on this "
            "machine."
        ))

        auto_box = QGroupBox("Match the app")
        auto_layout = QVBoxLayout(auto_box)
        auto_layout.addWidget(self.check(
            "Choose the profile from the active window", "context.auto_profile",
            "Off, every dictation uses the default profile from the Dictation tab.",
        ))
        auto_layout.addWidget(hint(
            "Rules are checked top to bottom and the first match wins. Leave a field "
            "blank to ignore it; both are case-insensitive fragments, so 'chrome' "
            "matches chrome.exe and 'gmail' matches a Gmail tab title."
        ))
        self._rule_table = QTableWidget(0, 4)
        self._rule_table.setHorizontalHeaderLabels(["On", "App", "Window title", "Profile"])
        self._rule_table.verticalHeader().setVisible(False)
        self._rule_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._rule_table.setMinimumHeight(230)
        header = self._rule_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 40)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, 150)
        for rule in (self.config.get("context.rules") or context_mod.DEFAULT_RULES):
            self._add_rule_row(rule)
        self._rule_table.itemChanged.connect(self._save_rules)
        auto_layout.addWidget(self._rule_table)

        rule_row = QHBoxLayout()
        add = QPushButton("Add rule")
        add.clicked.connect(
            lambda: self._add_rule_row({"app": "", "title": "", "profile": "clean", "enabled": True})
        )
        rule_row.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self._remove_rows(self._rule_table, self._save_rules))
        rule_row.addWidget(remove)
        restore = QPushButton("Restore the shipped rules")
        restore.clicked.connect(self._restore_rules)
        rule_row.addWidget(restore)
        rule_row.addStretch(1)
        auto_layout.addLayout(rule_row)
        layout.addWidget(auto_box)

        learn_box = QGroupBox("What it has picked up")
        learn_layout = QVBoxLayout(learn_box)
        learn_layout.addWidget(self.check(
            "Remember the words I use", "learn.vocabulary",
            "Distinctive words are fed back to the recogniser as a hint.",
        ))
        learn_layout.addWidget(self.check(
            "Remember which profile I pick in which app", "learn.profiles",
            "Changing profile from the tray after dictating teaches the app.",
        ))
        learn_layout.addWidget(hint(
            "A word is used as a hint once it has been dictated three times. This "
            "reinforces words that already get through; it cannot teach a word the "
            "recogniser has never once heard correctly."
        ))
        self._learned_list = QListWidget()
        self._learned_list.setMinimumHeight(170)
        learn_layout.addWidget(self._learned_list)
        learn_row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_learned)
        learn_row.addWidget(refresh)
        pin = QPushButton("Pin to vocabulary")
        pin.clicked.connect(self._pin_learned)
        learn_row.addWidget(pin)
        forget = QPushButton("Forget selected")
        forget.clicked.connect(self._forget_learned)
        learn_row.addWidget(forget)
        learn_row.addStretch(1)
        clear = QPushButton("Forget everything")
        clear.setObjectName("danger")
        clear.clicked.connect(self._forget_all_learned)
        learn_row.addWidget(clear)
        learn_layout.addLayout(learn_row)
        layout.addWidget(learn_box)

        correction_box = QGroupBox("Spoken corrections")
        correction_layout = QVBoxLayout(correction_box)
        correction_layout.addWidget(self.check(
            "Act on corrections instead of typing them out", "cleanup.self_corrections"
        ))
        correction_layout.addWidget(hint(
            "Say \"scratch that\" and the retracted sentence is dropped, by rule, in "
            "any profile. Corrections that need judgement, such as \"send it to Dave, "
            "I mean Sarah\", are resolved by the model, so they only apply to the tone "
            "profiles."
        ))
        layout.addWidget(correction_box)
        layout.addStretch(1)
        self.refresh_learned()
        return page

    def _add_rule_row(self, rule: dict) -> None:
        table = self._rule_table
        table.blockSignals(True)
        index = table.rowCount()
        table.insertRow(index)
        toggle = QTableWidgetItem()
        toggle.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        toggle.setCheckState(Qt.Checked if rule.get("enabled", True) else Qt.Unchecked)
        table.setItem(index, 0, toggle)
        table.setItem(index, 1, QTableWidgetItem(rule.get("app", "")))
        table.setItem(index, 2, QTableWidgetItem(rule.get("title", "")))
        picker = QComboBox()
        for key, meta in PROFILES.items():
            picker.addItem(meta["label"], key)
        found = picker.findData(rule.get("profile", "clean"))
        picker.setCurrentIndex(found if found >= 0 else 0)
        picker.currentIndexChanged.connect(self._save_rules)
        table.setCellWidget(index, 3, picker)
        table.blockSignals(False)

    def _save_rules(self) -> None:
        table = self._rule_table
        rules = []
        for row in range(table.rowCount()):
            app = (table.item(row, 1).text() if table.item(row, 1) else "").strip()
            title = (table.item(row, 2).text() if table.item(row, 2) else "").strip()
            if not app and not title:
                continue
            picker = table.cellWidget(row, 3)
            rules.append({
                "app": app,
                "title": title,
                "profile": picker.currentData() if picker else "clean",
                "enabled": table.item(row, 0).checkState() == Qt.Checked,
            })
        self._write("context.rules", rules)

    def _restore_rules(self) -> None:
        from . import context as context_mod

        self._rule_table.blockSignals(True)
        self._rule_table.setRowCount(0)
        for rule in context_mod.DEFAULT_RULES:
            self._add_rule_row(rule)
        self._rule_table.blockSignals(False)
        self._save_rules()
        self.status.showMessage("Shipped rules restored", 3000)

    # -- learned data -----------------------------------------------------
    def refresh_learned(self) -> None:
        if not hasattr(self, "_learned_list"):
            return
        learner = self.engine.learner
        self._learned_list.clear()
        for app, profile, count in learner.learned_rules():
            label = PROFILES.get(profile, {}).get("label", profile)
            item = QListWidgetItem(f"app   {app}  ->  {label}   ({count} picks)")
            item.setData(Qt.UserRole, ("app", app))
            self._learned_list.addItem(item)
        promoted = set(learner.promoted_terms())
        for term, count in learner.pending_terms():
            mark = "in use" if term in promoted else f"{count} of 3"
            item = QListWidgetItem(f"word  {term}   ({mark})")
            item.setData(Qt.UserRole, ("term", term))
            self._learned_list.addItem(item)
        if self._learned_list.count() == 0:
            self._learned_list.addItem("Nothing learned yet. Dictate a few times.")

    def _selected_learned(self) -> list[tuple[str, str]]:
        out = []
        for item in self._learned_list.selectedItems():
            data = item.data(Qt.UserRole)
            if data:
                out.append(data)
        return out

    def _forget_learned(self) -> None:
        learner = self.engine.learner
        for kind, value in self._selected_learned():
            if kind == "term":
                learner.forget_term(value)
            else:
                learner.app_profiles.pop(value, None)
                learner.save()
        self.refresh_learned()

    def _pin_learned(self) -> None:
        """Promote a learned word into the hand-kept vocabulary list."""
        terms = [value for kind, value in self._selected_learned() if kind == "term"]
        if not terms:
            return
        vocabulary = list(self.config.get("asr.vocabulary", []))
        existing = {t.lower() for t in vocabulary}
        added = [t for t in terms if t.lower() not in existing]
        if added:
            self._write("asr.vocabulary", vocabulary + added)
            for term in added:
                self.engine.learner.forget_term(term)
            if hasattr(self, "_vocab_list"):
                for term in added:
                    self._vocab_list.addItem(term)
        self.refresh_learned()
        self.status.showMessage(f"Pinned {len(added)} word(s) to the vocabulary", 3000)

    def _forget_all_learned(self) -> None:
        confirm = QMessageBox.question(
            self, "Forget everything",
            "Throw away every learned word and app preference? Your hand-written "
            "vocabulary and rules are kept.",
        )
        if confirm == QMessageBox.Yes:
            self.engine.learner.forget_all()
            self.refresh_learned()

    # -- Fix chord --------------------------------------------------------
    def _fix_box(self) -> QGroupBox:
        box = QGroupBox("Fix what is already written")
        layout = QVBoxLayout(box)
        layout.addWidget(hint(
            "Press this chord in any text box and VoxKey selects what is there, "
            "corrects the spelling, grammar and punctuation, and puts it back. It "
            "keeps your wording and never adds em dashes."
        ))
        layout.addWidget(self.check("Enable the fix chord", "fix.enabled"))

        row = QHBoxLayout()
        self._fix_mods: dict[str, QCheckBox] = {}
        active = [m.lower() for m in self.config.get("fix.modifiers", [])]
        for label, value in MODIFIERS:
            item = QCheckBox(label)
            item.setChecked(value in active)
            item.toggled.connect(self._fix_chord_changed)
            self._fix_mods[value] = item
            row.addWidget(item)
        row.addSpacing(10)
        row.addWidget(QLabel("plus key:"))
        self._fix_key = QComboBox()
        self._fix_key.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._fix_key.setMinimumContentsLength(12)
        self._fix_key.addItem("(none)", "")
        for name in sorted(KEY_VKS):
            self._fix_key.addItem(name.upper() if len(name) == 1 else name, name)
        found = self._fix_key.findData((self.config.get("fix.key") or "").lower())
        self._fix_key.setCurrentIndex(max(0, found))
        self._fix_key.currentIndexChanged.connect(self._fix_chord_changed)
        row.addWidget(self._fix_key)
        row.addStretch(1)
        layout.addLayout(row)

        self._fix_preview = QLabel()
        self._fix_preview.setStyleSheet("color:#7dd3c0; font-size:15px; font-weight:600;")
        layout.addWidget(self._fix_preview)
        self._fix_warning = hint("")
        layout.addWidget(self._fix_warning)

        form = QFormLayout()
        form.addRow("Fix", self.combo("fix.scope", [
            ("Everything in the box", "all"),
            ("Only what is selected", "selection"),
        ]))
        form.addRow("Using", self.combo(
            "fix.profile", [(PROFILES[k]["label"], k) for k in PROFILES if PROFILES[k]["llm"]]
        ))
        form.addRow("Stop after", self.spin("fix.max_chars", 500, 100000, " characters", 500))
        layout.addLayout(form)
        layout.addWidget(hint(
            "Longer text is split on blank lines and fixed a few paragraphs at a time. "
            "If the window loses focus while it is working, the corrected text goes to "
            "the clipboard rather than into the wrong place."
        ))
        self._refresh_fix_preview()
        return box

    def _fix_chord_changed(self) -> None:
        modifiers = [value for value, box in self._fix_mods.items() if box.isChecked()]
        self.config.set("fix.modifiers", modifiers)
        self.config.set("fix.key", self._fix_key.currentData() or "")
        self.config.save()
        self._refresh_fix_preview()

    def _refresh_fix_preview(self) -> None:
        modifiers = [v for v, b in self._fix_mods.items() if b.isChecked()]
        key = self._fix_key.currentData() or ""
        self._fix_preview.setText(key_label(modifiers, key))

        talk = {m.lower() for m in self.config.get("hotkey.modifiers", [])}
        talk_key = (self.config.get("hotkey.key") or "").lower()
        message = ""
        if not modifiers and not key:
            message = "Nothing is bound, so the fix chord can never fire."
        elif set(modifiers) == talk and key == talk_key:
            message = "This is the same as the talk chord. One of them will never fire."
        elif set(modifiers) and not set(modifiers) > talk and talk <= set(modifiers):
            message = ""
        self._fix_warning.setText(message)
        self._fix_warning.setStyleSheet("color:#f0b360;" if message else "")
        self._fix_warning.setVisible(bool(message))

    # -- Diagnostics ------------------------------------------------------
    def _tab_diagnostics(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(head("Is it actually working?"))
        layout.addWidget(hint(
            "Every part that can fail quietly, reported live. A dictation that "
            "produces nothing is usually the microphone or a stuck key rather than "
            "the chord, and those look identical from the outside."
        ))

        self._diag_rows: dict[str, QLabel] = {}
        for section, keys in (
            ("Hotkey", ["listener", "chord", "blocked"]),
            ("Microphone", ["device", "stream", "level"]),
            ("Speech model", ["speech", "speech_device"]),
            ("Rewriter", ["ollama", "llm_model"]),
            ("Last dictation", ["last"]),
        ):
            box = QGroupBox(section)
            form = QFormLayout(box)
            for key in keys:
                value = QLabel("...")
                value.setWordWrap(True)
                self._diag_rows[key] = value
                form.addRow(_DIAG_LABELS[key], value)
            layout.addWidget(box)

        row = QHBoxLayout()
        copy = QPushButton("Copy report")
        copy.setObjectName("primary")
        copy.clicked.connect(self._copy_diagnostics)
        row.addWidget(copy)
        open_log = QPushButton("Open log")
        open_log.clicked.connect(self._open_log)
        row.addWidget(open_log)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

        # Only ticks while this tab is on screen; no point polling otherwise.
        self._diag_timer = QTimer(self)
        self._diag_timer.timeout.connect(self._refresh_diagnostics)
        self.tabs.currentChanged.connect(self._diag_tab_changed)
        return page

    def _diag_tab_changed(self, index: int) -> None:
        showing = self.tabs.tabText(index) == "Diagnostics"
        if showing:
            self._refresh_diagnostics()
            self._diag_timer.start(700)
        else:
            self._diag_timer.stop()

    def _diagnostics(self) -> dict[str, str]:
        engine = self.engine
        recorder, transcriber = engine.recorder, engine.transcriber
        hotkey = engine.hotkey

        blocked_modifier = hotkey.blocked_by()
        stuck = hotkey.stuck_key()
        if blocked_modifier:
            blocked = f"{blocked_modifier.capitalize()} is held down, so the chord cannot match"
        elif stuck:
            blocked = f"a key (0x{stuck:02X}) is held down, so every dictation cancels"
        else:
            blocked = "nothing in the way"

        device = self.config.get("audio.device")
        device_name = "System default"
        if device is not None:
            for index, label in audio_mod.list_input_devices():
                if index == device:
                    device_name = label
                    break

        if not self.config.get("audio.preroll", True):
            stream = "opened only while dictating"
        elif recorder.is_stalled():
            stream = "STALLED, no audio has arrived recently"
        else:
            stream = "open and delivering"

        client = engine.pipeline.llm
        if not self.config.get("llm.enabled", True):
            ollama = "switched off, tone profiles fall back to rules"
        elif client.reachable(timeout=0.8):
            ollama = f"reachable at {client.base_url}"
        else:
            ollama = f"NOT reachable at {client.base_url}"

        model_name = self.config.get("llm.model", "")
        lowered = model_name.lower()
        if lowered.startswith("qwen3:") and "instruct" not in lowered:
            model_name += "   (a reasoning build, expect 30s rewrites)"

        entries = engine.history.entries
        if entries:
            newest = entries[0]
            label = PROFILES.get(newest.profile, {}).get("label", newest.profile)
            last = f"{newest.day} {newest.when}, {label}, {newest.seconds:.1f}s of audio"
        else:
            last = "nothing yet"

        return {
            "listener": "running" if hotkey.is_alive() else "NOT RUNNING",
            "chord": key_label(
                self.config.get("hotkey.modifiers", []), self.config.get("hotkey.key", "")
            ),
            "blocked": blocked,
            "device": device_name,
            "stream": stream,
            "level": f"{recorder.level:.3f}   (speak and this should move)",
            "speech": transcriber.last_error or (
                "loaded" if transcriber.is_loaded() else "not loaded yet"
            ),
            "speech_device": f"{self.config.get('asr.model')} on {transcriber.last_device}",
            "ollama": ollama,
            "llm_model": model_name,
            "last": last,
        }

    def _refresh_diagnostics(self) -> None:
        for key, value in self._diagnostics().items():
            row = self._diag_rows.get(key)
            if row is None:
                continue
            row.setText(value)
            bad = value.startswith(("NOT", "STALLED")) or "is held down" in value
            row.setStyleSheet("color:#f87171;" if bad else "")

    def _copy_diagnostics(self) -> None:
        from . import inject
        from . import __version__

        lines = [f"VoxKey {__version__}"]
        lines += [f"{_DIAG_LABELS[k]}: {v}" for k, v in self._diagnostics().items()]
        inject.set_clipboard_text("\n".join(lines))
        self.status.showMessage("Report copied to the clipboard", 3000)
