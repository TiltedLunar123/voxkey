"""The Flow Bar: a persistent, draggable capsule that lives on screen.

Modelled on how Wispr Flow's desktop bar behaves. It sits at an edge of the
screen all the time, expands into a waveform with Cancel and Stop while you
dictate, and never takes keyboard focus, because the window underneath has to
stay focused for the paste to land in it.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

BARS = 20

IDLE_SIZE = (108, 28)
ACTIVE_SIZE = (236, 36)
BUSY_SIZE = (152, 30)

FPS = 60
FRAME_MS = 1000 // FPS
GROW_MS = 220          # capsule resize
FADE_MS = 160          # show and hide
# A meter that rises fast and falls slowly reads as audio; equal rates read as
# noise. These are per-frame lerp factors at 60fps.
ATTACK = 0.55
DECAY = 0.12
HOVER_RATE = 0.22
CHECK_MS = 260

INK = QColor(233, 238, 247)
MUTED = QColor(150, 162, 182)
SHELL = QColor(17, 20, 28, 242)
EDGE = QColor(255, 255, 255, 30)

STATES = {
    "idle": ("", QColor(125, 211, 192)),
    "listening": ("Listening", QColor(94, 234, 212)),
    "transcribing": ("Transcribing", QColor(129, 178, 255)),
    "rewriting": ("Rewriting", QColor(196, 160, 255)),
    "done": ("Sent", QColor(134, 239, 172)),
    "error": ("Failed", QColor(248, 113, 113)),
    "cancelled": ("Cancelled", QColor(148, 163, 184)),
}

POSITIONS = [
    ("Bottom right", "bottom-right"),
    ("Bottom centre", "bottom-center"),
    ("Bottom left", "bottom-left"),
    ("Top right", "top-right"),
    ("Top centre", "top-center"),
    ("Top left", "top-left"),
    ("Where I dragged it", "custom"),
]
MARGIN = 22


class Overlay(QWidget):
    """The bar. Emits intent; the engine decides what actually happens."""

    start_requested = Signal()
    stop_requested = Signal()
    cancel_requested = Signal()
    settings_requested = Signal()
    history_requested = Signal()
    paste_last_requested = Signal()

    def __init__(self, config) -> None:
        super().__init__(None)
        self.config = config
        self.state = "idle"
        self.detail = ""
        self._elapsed = 0.0
        self._hidden_until = 0.0
        self._drag_from: QPoint | None = None
        self._dragged = False
        self._press_zone = ""
        self._hover_zone = ""
        self._zones: dict[str, QRect] = {}

        # Animation state, all advanced from one 60fps tick.
        self._display = [0.0] * BARS      # what is actually drawn
        self._envelope = 0.0              # smoothed mic level feeding the meter
        self._input_level = 0.0
        self._phase = 0.0
        self._hover_amount = {"cancel": 0.0, "stop": 0.0}
        self._check = 0.0                 # 0..1 progress of the done tick
        self._pending_hide = False

        self.setWindowFlags(
            Qt.WindowStaysOnTopHint
            | Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        # WA_ShowWithoutActivating plus WindowDoesNotAcceptFocus is what keeps
        # the target window focused. Without both, clicking Stop would move
        # focus here and the transcript would paste into nothing.
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.resize(*IDLE_SIZE)

        self._grow = QPropertyAnimation(self, b"geometry", self)
        self._grow.setDuration(GROW_MS)
        self._grow.setEasingCurve(QEasingCurve.OutCubic)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.finished.connect(self._after_fade)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.timeout.connect(self._to_idle)
        self._snooze = QTimer(self)
        self._snooze.setSingleShot(True)
        self._snooze.timeout.connect(self._wake)

    # -- placement --------------------------------------------------------
    def _screen(self):
        return QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()

    def _target_geometry(self) -> QRect:
        """Where the capsule wants to be, grown around its own centre."""
        if self.state == "idle":
            width, height = IDLE_SIZE
        elif self.state == "listening":
            width, height = ACTIVE_SIZE
        else:
            width, height = BUSY_SIZE
        # Anchor on where a running grow is heading, not where it is now, so a
        # quick idle -> listening -> transcribing run does not walk sideways.
        end = self._grow.endValue()
        if self._grow.state() == QPropertyAnimation.Running and end is not None:
            centre = end.center()
        else:
            centre = self.geometry().center()
        area = self._screen().availableGeometry()
        x = min(max(area.left() + 6, centre.x() - width // 2), area.right() - width - 6)
        y = min(max(area.top() + 6, centre.y() - height // 2), area.bottom() - height - 6)
        return QRect(x, y, width, height)

    def _apply_size(self) -> None:
        target = self._target_geometry()
        if target == self.geometry():
            return
        if not self.isVisible():
            self.setGeometry(target)
            return
        self._grow.stop()
        self._grow.setStartValue(self.geometry())
        self._grow.setEndValue(target)
        self._grow.start()

    def _preset_point(self, preset: str) -> QPoint:
        """Top-left corner for a named corner or edge of the current screen."""
        area = self._screen().availableGeometry()
        vertical, _, horizontal = preset.partition("-")
        y = area.top() + MARGIN if vertical == "top" else area.bottom() - self.height() - MARGIN
        if horizontal == "left":
            x = area.left() + MARGIN
        elif horizontal == "center":
            x = area.center().x() - self.width() // 2
        else:
            x = area.right() - self.width() - MARGIN
        return QPoint(x, y)

    def restore_position(self) -> None:
        area = self._screen().availableGeometry()
        preset = self.config.get("ui.bar_position", "bottom-right")
        if preset != "custom":
            self.move(self._preset_point(preset))
            return
        saved_x = self.config.get("ui.bar_x", -1)
        saved_y = self.config.get("ui.bar_y", -1)
        if saved_x < 0 or saved_y < 0:
            self.move(self._preset_point("bottom-right"))
            return
        # Clamp, so a position saved on a monitor that is now unplugged does not
        # park the bar somewhere invisible.
        x = min(max(area.left() + 6, int(saved_x)), area.right() - self.width() - 6)
        y = min(max(area.top() + 6, int(saved_y)), area.bottom() - self.height() - 6)
        self.move(x, y)

    def move_to_preset(self, preset: str) -> None:
        self.config.set("ui.bar_position", preset)
        self.config.save()
        if preset != "custom":
            self.move(self._preset_point(preset))

    def _snap_to_edge(self) -> None:
        """Drop onto the nearest edge, the way the Flow Bar snaps."""
        area = self._screen().availableGeometry()
        rect = self.geometry()
        margin = 14
        distances = {
            "bottom": area.bottom() - rect.bottom(),
            "top": rect.top() - area.top(),
            "left": rect.left() - area.left(),
            "right": area.right() - rect.right(),
        }
        edge = min(distances, key=lambda key: distances[key])
        x, y = rect.x(), rect.y()
        if edge == "bottom":
            y = area.bottom() - self.height() - margin
        elif edge == "top":
            y = area.top() + margin
        elif edge == "left":
            x = area.left() + margin
        else:
            x = area.right() - self.width() - margin
        x = min(max(area.left() + 6, x), area.right() - self.width() - 6)
        y = min(max(area.top() + 6, y), area.bottom() - self.height() - 6)
        self.move(x, y)
        # Dragging it means you want it there, not on a preset.
        self.config.set("ui.bar_position", "custom")
        self.config.set("ui.bar_x", x)
        self.config.set("ui.bar_y", y)
        self.config.save()

    # -- visibility -------------------------------------------------------
    def always_on(self) -> bool:
        return bool(self.config.get("ui.bar_always", True))

    def _wanted_opacity(self) -> float:
        """Dim while idle so it does not obscure what is underneath."""
        if self.state != "idle" or self._hover_zone:
            return 1.0
        return max(0.15, min(1.0, float(self.config.get("ui.bar_idle_opacity", 0.5))))

    def refresh_visibility(self) -> None:
        """Called when the setting changes or a dictation ends."""
        allowed = self.config.get("ui.overlay", True) and time.time() >= self._hidden_until
        want = allowed and (self.always_on() or self.state != "idle")
        if want:
            self._pending_hide = False
            if not self.isVisible():
                self.restore_position()
                self.setGeometry(self._target_geometry())
                self.setWindowOpacity(0.0)
                self.show()
            self._fade_to(self._wanted_opacity())
        elif self.isVisible():
            self._pending_hide = True
            self._fade_to(0.0)

    def _fade_to(self, value: float) -> None:
        if abs(self.windowOpacity() - value) < 0.01:
            self._after_fade()
            return
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(value)
        self._fade.start()

    def _after_fade(self) -> None:
        if self._pending_hide and self.windowOpacity() <= 0.02:
            self.hide()
            self._pending_hide = False

    def snooze(self, seconds: int = 3600) -> None:
        self._hidden_until = time.time() + seconds
        self.hide()
        self._snooze.start(int(seconds * 1000))

    def _wake(self) -> None:
        self._hidden_until = 0.0
        self.refresh_visibility()

    # -- state ------------------------------------------------------------
    def begin(self) -> None:
        self._display = [0.0] * BARS
        self._envelope = 0.0
        self._input_level = 0.0
        self._elapsed = 0.0
        self._check = 0.0
        self._revert.stop()
        self.set_state("listening")

    def set_state(self, state: str, detail: str = "") -> None:
        if state == "done" and self.state != "done":
            self._check = 0.0
        self.state = state
        self.detail = detail
        self._apply_size()
        self.refresh_visibility()
        self._ensure_ticking()
        self.update()

    def push_level(self, level: float, elapsed: float) -> None:
        # Only record the reading. The meter is advanced by the paint tick so it
        # scrolls at a steady 60fps rather than at whatever rate audio arrives.
        self._input_level = max(0.0, min(1.0, level))
        self._elapsed = elapsed

    def finish(self, state: str, detail: str = "", linger_ms: int = 1100) -> None:
        self.set_state(state, detail)
        self._revert.start(max(200, linger_ms))

    def _to_idle(self) -> None:
        self.set_state("idle")

    def dismiss(self) -> None:
        self._tick.stop()
        self._revert.stop()
        self._fade.stop()
        self.hide()

    # -- the single animation clock ---------------------------------------
    def _on_tick(self) -> None:
        self._phase += FRAME_MS / 1000.0

        if self.state == "listening":
            target = self._input_level
            rate = ATTACK if target > self._envelope else DECAY
            self._envelope += (target - self._envelope) * rate
            self._display = self._display[1:] + [self._envelope]

        if self.state == "done":
            self._check = min(1.0, self._check + FRAME_MS / CHECK_MS)

        for zone, amount in self._hover_amount.items():
            goal = 1.0 if self._hover_zone == zone else 0.0
            self._hover_amount[zone] = amount + (goal - amount) * HOVER_RATE

        self.update()
        self._ensure_ticking()

    def _ensure_ticking(self) -> None:
        """Run the clock only when something is actually moving."""
        moving = self.state != "idle" or any(v > 0.01 for v in self._hover_amount.values())
        if moving and not self._tick.isActive():
            self._tick.start(FRAME_MS)
        elif not moving and self._tick.isActive():
            self._tick.stop()
            self.update()

    # -- painting ---------------------------------------------------------
    def _chord_text(self) -> str:
        from .hotkey import key_label

        return key_label(
            [m.lower() for m in self.config.get("hotkey.modifiers", [])],
            (self.config.get("hotkey.key") or "").lower(),
        ) or "not set"

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        label, colour = STATES.get(self.state, STATES["idle"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._zones = {}

        radius = self.height() / 2
        shell = QPainterPath()
        shell.addRoundedRect(QRectF(0, 0, self.width(), self.height()), radius, radius)
        painter.fillPath(shell, SHELL)
        painter.setPen(QPen(EDGE, 1))
        painter.drawPath(shell)

        if self.state == "idle":
            self._paint_idle(painter, colour)
        elif self.state == "listening":
            self._paint_listening(painter, colour)
        elif self.state in ("transcribing", "rewriting"):
            self._paint_working(painter, colour, label)
        elif self.state == "done":
            self._paint_done(painter, colour, label)
        else:
            self._paint_message(painter, colour, label)
        painter.end()

    def _paint_idle(self, painter: QPainter, colour: QColor) -> None:
        size = self.height() - 12
        self._draw_mic(painter, QRect(9, 6, size, size), colour)
        painter.setPen(MUTED)
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRect(13 + size, 0, self.width() - size - 20, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            self._chord_text(),
        )
        self._zones["body"] = self.rect()

    def _paint_listening(self, painter: QPainter, colour: QColor) -> None:
        height = self.height()
        button = height - 12
        cancel = QRect(6, (height - button) // 2, button, button)
        stop = QRect(self.width() - button - 6, (height - button) // 2, button, button)
        self._zones["cancel"] = cancel
        self._zones["stop"] = stop

        self._draw_round_button(painter, cancel, QColor(248, 113, 113), "x", "cancel")
        self._draw_round_button(painter, stop, colour, "square", "stop")

        painter.setPen(MUTED)
        painter.setFont(QFont("Segoe UI", 7))
        time_rect = QRect(stop.left() - 32, 0, 28, height)
        painter.drawText(time_rect, Qt.AlignVCenter | Qt.AlignRight, _clock(self._elapsed))

        self._draw_meter(painter, colour, cancel.right() + 9, time_rect.left() - 6)

    def _draw_meter(self, painter: QPainter, colour: QColor, left: int, right: int) -> None:
        span = right - left
        # Mid-grow the capsule is still narrow and every bar would land on the
        # same pixel, which draws as one blob. Fade the meter in with the room.
        if span < BARS * 2:
            return
        room = min(1.0, (span - BARS * 2) / (BARS * 2))
        width = max(1.6, span / BARS - 1.7)
        middle = self.height() / 2
        ceiling = self.height() - 12
        painter.setPen(Qt.NoPen)
        for index, level in enumerate(self._display):
            # A little always-on shimmer so silence looks alive rather than dead.
            shimmer = 0.5 + 0.5 * math.sin(self._phase * 4.0 + index * 0.55)
            bar_height = 2.5 + level * ceiling + shimmer * 1.4
            x = left + index * (span / BARS)
            faded = QColor(colour)
            # Oldest samples sit at the left; fading them gives the scroll a tail.
            trail = 0.45 + 0.55 * (index / max(1, BARS - 1))
            faded.setAlpha(int((70 + 160 * level) * trail * room))
            painter.setBrush(faded)
            painter.drawRoundedRect(
                QRectF(x, middle - bar_height / 2, width, bar_height), width / 2, width / 2
            )

    def _paint_working(self, painter: QPainter, colour: QColor, label: str) -> None:
        box = QRectF(11, self.height() / 2 - 7, 14, 14)
        ring = QColor(colour)
        ring.setAlpha(55)
        pen = QPen(ring, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(box)
        # One rotating arc reads as "working" far better than a static dot.
        pen.setColor(colour)
        painter.setPen(pen)
        start = int(-self._phase * 300 * 16) % (360 * 16)
        painter.drawArc(box, start, 100 * 16)

        painter.setPen(INK)
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRect(32, 0, self.width() - 40, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            self.detail or label,
        )
        self._zones["body"] = self.rect()

    def _paint_done(self, painter: QPainter, colour: QColor, label: str) -> None:
        box = QRectF(11, self.height() / 2 - 7, 14, 14)
        halo = QColor(colour)
        halo.setAlpha(45)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(box)

        pen = QPen(colour, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        points = [
            QPointF(box.left() + box.width() * 0.24, box.top() + box.height() * 0.52),
            QPointF(box.left() + box.width() * 0.43, box.top() + box.height() * 0.72),
            QPointF(box.left() + box.width() * 0.78, box.top() + box.height() * 0.30),
        ]
        _draw_partial_polyline(painter, points, _ease_out(self._check))

        painter.setPen(INK)
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRect(32, 0, self.width() - 40, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            self.detail or label,
        )
        self._zones["body"] = self.rect()

    def _paint_message(self, painter: QPainter, colour: QColor, label: str) -> None:
        painter.setBrush(colour)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(13, self.height() // 2 - 5, 10, 10)
        painter.setPen(INK)
        painter.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        painter.drawText(
            QRect(32, 0, self.width() - 40, self.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            self.detail or label,
        )
        self._zones["body"] = self.rect()

    def _draw_mic(self, painter: QPainter, box: QRect, colour: QColor) -> None:
        """A microphone: capsule, cradle, stem, base.

        Proportions are fractions of the box so it stays right at any size. The
        cradle is a clean half circle, which puts its arms level with the bottom
        of the capsule; a shallower arc reads as a smile under a blob instead.
        """
        left, top = box.left(), box.top()
        size = min(box.width(), box.height())

        def px(fx: float, fy: float) -> tuple[float, float]:
            return left + fx * size, top + fy * size

        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        body_x, body_y = px(0.33, 0.04)
        body_w, body_h = 0.34 * size, 0.50 * size
        painter.drawRoundedRect(
            QRectF(body_x, body_y, body_w, body_h), body_w / 2, body_w / 2
        )

        pen = QPen(colour, max(1.3, size * 0.075))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        arc_x, arc_y = px(0.20, 0.28)
        painter.drawArc(
            QRectF(arc_x, arc_y, 0.60 * size, 0.48 * size), 180 * 16, 180 * 16
        )

        stem_x, stem_top_y = px(0.5, 0.76)
        _, stem_bottom_y = px(0.5, 0.91)
        painter.drawLine(QPointF(stem_x, stem_top_y), QPointF(stem_x, stem_bottom_y))

        base_left_x, base_y = px(0.34, 0.91)
        base_right_x, _ = px(0.66, 0.91)
        painter.drawLine(QPointF(base_left_x, base_y), QPointF(base_right_x, base_y))

    def _draw_round_button(
        self, painter: QPainter, box: QRect, colour: QColor, glyph: str, zone: str
    ) -> None:
        hot = self._hover_amount.get(zone, 0.0)
        fill = QColor(colour)
        fill.setAlpha(int(38 + 42 * hot))
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        # Swell very slightly under the cursor so the target feels live.
        grow = box.adjusted(-int(hot * 1.5), -int(hot * 1.5), int(hot * 1.5), int(hot * 1.5))
        painter.drawEllipse(grow)

        pen = QPen(colour, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        inner = QRectF(box).adjusted(7.5, 7.5, -7.5, -7.5)
        if glyph == "x":
            painter.drawLine(inner.topLeft(), inner.bottomRight())
            painter.drawLine(inner.topRight(), inner.bottomLeft())
        else:
            painter.setBrush(colour)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(inner, 1.5, 1.5)

    # -- interaction ------------------------------------------------------
    def _zone_at(self, point: QPoint) -> str:
        for name in ("cancel", "stop"):
            if name in self._zones and self._zones[name].contains(point):
                return name
        return "body"

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() != Qt.LeftButton:
            return
        self._press_zone = self._zone_at(event.position().toPoint())
        self._drag_from = event.globalPosition().toPoint() - self.pos()
        self._dragged = False

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        zone = self._zone_at(event.position().toPoint())
        if zone != self._hover_zone:
            self._hover_zone = zone
            self.update()
        if self._drag_from is None or not (event.buttons() & Qt.LeftButton):
            return
        target = event.globalPosition().toPoint() - self._drag_from
        if not self._dragged and (target - self.pos()).manhattanLength() < 5:
            return  # a few pixels of wobble during a click is not a drag
        self._dragged = True
        self.move(target)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() != Qt.LeftButton:
            return
        was_drag, zone = self._dragged, self._press_zone
        self._drag_from, self._dragged, self._press_zone = None, False, ""
        if was_drag:
            self._snap_to_edge()
            return
        if self.state == "listening":
            if zone == "cancel":
                self.cancel_requested.emit()
            else:
                # Wispr ends a push-to-talk session on a bar click too, not just
                # on the stop button.
                self.stop_requested.emit()
        elif self.state == "idle":
            self.start_requested.emit()

    def enterEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hover_zone = self._hover_zone or "body"
        self._fade_to(1.0)
        self._ensure_ticking()

    def leaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hover_zone = ""
        self._fade_to(self._wanted_opacity())
        self.update()

    def contextMenuEvent(self, event) -> None:  # noqa: ANN001, N802
        menu = QMenu(self)
        menu.addAction("Settings...", self.settings_requested.emit)
        menu.addAction("History...", self.history_requested.emit)
        menu.addAction("Paste last transcript", self.paste_last_requested.emit)
        place = menu.addMenu("Move to")
        current = self.config.get("ui.bar_position", "bottom-right")
        for label, key in POSITIONS:
            if key == "custom":
                continue
            action = place.addAction(label, lambda k=key: self.move_to_preset(k))
            action.setCheckable(True)
            action.setChecked(key == current)
        menu.addSeparator()
        menu.addAction("Hide for 1 hour", lambda: self.snooze(3600))
        always = menu.addAction("Always show the bar")
        always.setCheckable(True)
        always.setChecked(self.always_on())
        always.toggled.connect(self._set_always)
        menu.exec(event.globalPos())

    def _set_always(self, on: bool) -> None:
        self.config.set("ui.bar_always", bool(on))
        self.config.save()
        self.refresh_visibility()


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _draw_partial_polyline(painter: QPainter, points: list[QPointF], progress: float) -> None:
    """Draw a polyline only `progress` of the way along, so it strokes itself in."""
    if progress <= 0:
        return
    lengths = [
        math.hypot(points[i + 1].x() - points[i].x(), points[i + 1].y() - points[i].y())
        for i in range(len(points) - 1)
    ]
    budget = sum(lengths) * min(1.0, progress)
    for index, length in enumerate(lengths):
        if budget <= 0:
            return
        a, b = points[index], points[index + 1]
        if budget >= length or length == 0:
            painter.drawLine(a, b)
            budget -= length
            continue
        fraction = budget / length
        painter.drawLine(
            a,
            QPointF(a.x() + (b.x() - a.x()) * fraction, a.y() + (b.y() - a.y()) * fraction),
        )
        return
