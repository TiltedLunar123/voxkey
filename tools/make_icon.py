"""Render voxkey/assets/voxkey.ico from the same microphone the bar draws.

    .venv\\Scripts\\python.exe tools\\make_icon.py

An ICO is just a directory of PNGs, so this draws the glyph at each size Windows
asks for and packs them by hand; that keeps every size crisp instead of letting
Explorer scale one bitmap.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QIODevice, QRect, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

from voxkey.overlay import draw_mic  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SHELL = QColor(18, 21, 28)
RIM = QColor(255, 255, 255, 28)
TEAL = QColor(125, 211, 192)
OUT = Path(__file__).resolve().parent.parent / "voxkey" / "assets" / "voxkey.ico"


def render(size: int) -> bytes:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    radius = size * 0.22
    painter.setPen(Qt.NoPen)
    painter.setBrush(SHELL)
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    if size >= 32:
        painter.setPen(RIM)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), radius, radius)
    inset = int(size * 0.17)
    draw_mic(painter, QRect(inset, inset, size - 2 * inset, size - 2 * inset), TEAL)
    painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def pack(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + entries + blobs


def main() -> None:
    QGuiApplication.instance() or QGuiApplication(["make_icon"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(pack([(size, render(size)) for size in SIZES]))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(SIZES)} sizes)")


if __name__ == "__main__":
    main()
