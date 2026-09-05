"""System tray icon, menu and notifications."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .config import PROFILES

IDLE = QColor(125, 211, 192)
BUSY = QColor(129, 178, 255)
LIVE = QColor(248, 113, 113)
OFF = QColor(120, 132, 154)


def mic_icon(colour: QColor) -> QIcon:
    """Draw the tray glyph rather than shipping a .ico next to the code."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    painter.drawRoundedRect(QRectF(24, 10, 16, 28), 8, 8)
    pen = painter.pen()
    pen.setColor(colour)
    pen.setWidth(5)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(16, 22, 32, 28), 200 * 16, 140 * 16)
    painter.drawLine(32, 45, 32, 54)
    painter.end()
    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    def __init__(self, engine, open_settings) -> None:
        super().__init__(mic_icon(IDLE))
        self.engine = engine
        self.config = engine.config
        self._open_settings = open_settings
        self.setToolTip("VoxKey")

        menu = QMenu()
        self._status_action = menu.addAction("Starting up...")
        self._status_action.setEnabled(False)
        menu.addSeparator()

        profile_menu = menu.addMenu("Cleanup profile")
        self._profile_group = QActionGroup(self)
        self._profile_group.setExclusive(True)
        current = self.config.get("cleanup.profile", "clean")
        self._profile_actions: dict[str, QAction] = {}
        for key, meta in PROFILES.items():
            action = QAction(meta["label"], self, checkable=True)
            action.setChecked(key == current)
            action.triggered.connect(lambda _c, k=key: self._pick_profile(k))
            self._profile_group.addAction(action)
            profile_menu.addAction(action)
            self._profile_actions[key] = action

        menu.addSeparator()
        settings_action = menu.addAction("Settings...")
        settings_action.triggered.connect(open_settings)
        history_action = menu.addAction("History...")
        history_action.triggered.connect(lambda: open_settings("History"))

        menu.addSeparator()
        self._pause_action = menu.addAction("Pause listening")
        self._pause_action.setCheckable(True)
        self._pause_action.toggled.connect(self._toggle_pause)

        menu.addSeparator()
        quit_action = menu.addAction("Quit VoxKey")
        quit_action.triggered.connect(self._quit)

        self.setContextMenu(menu)
        self.activated.connect(self._activated)

        engine.state_changed.connect(self._state_changed)
        engine.ready_changed.connect(self._ready_changed)
        engine.notice.connect(self._notify)

    # -- menu actions -----------------------------------------------------
    def _pick_profile(self, key: str) -> None:
        self.config.set("cleanup.profile", key)
        self.config.save()
        # Choosing by hand is the strongest signal there is about what this app
        # should have used, so remember it against the app last dictated into.
        self.engine.note_manual_profile(key)
        self.showMessage("VoxKey", f"Cleanup set to {PROFILES[key]['label']}", mic_icon(IDLE), 1800)

    def sync_profile(self) -> None:
        key = self.config.get("cleanup.profile", "clean")
        action = self._profile_actions.get(key)
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def _toggle_pause(self, paused: bool) -> None:
        if paused:
            self.engine.hotkey.pause()
            self.setIcon(mic_icon(OFF))
            self._status_action.setText("Paused")
        else:
            self.engine.hotkey.resume()
            self.setIcon(mic_icon(IDLE))
            self._status_action.setText("Ready")
        self._pause_action.setText("Resume listening" if paused else "Pause listening")

    def _activated(self, reason) -> None:  # noqa: ANN001
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._open_settings()

    def _quit(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.engine.shutdown()
        self.hide()
        QApplication.instance().quit()

    # -- engine feedback --------------------------------------------------
    def _state_changed(self, state: str, _detail: str) -> None:
        if self._pause_action.isChecked():
            return
        colours = {"listening": LIVE, "transcribing": BUSY, "rewriting": BUSY}
        self.setIcon(mic_icon(colours.get(state, IDLE)))
        labels = {
            "listening": "Listening...",
            "transcribing": "Transcribing...",
            "rewriting": "Rewriting...",
            "error": "Something went wrong",
        }
        self._status_action.setText(labels.get(state, "Ready"))

    def _ready_changed(self, message: str) -> None:
        self._status_action.setText(message)
        self.setToolTip(f"VoxKey - {message}")

    def _notify(self, title: str, body: str) -> None:
        if self.config.get("ui.notify_errors", True):
            self.showMessage(title, body, mic_icon(LIVE), 4000)
