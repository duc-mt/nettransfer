#!/usr/bin/env python3
"""Draw the app icon with Qt and write assets/icon.png, icon.ico (multi-size) and icon.icns.

Run once (the generated files are committed); needs PySide6 only.  Offscreen, no display required.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 1024.0
    p.scale(s, s)
    # rounded-square background
    grad = QLinearGradient(0, 0, 0, 1024)
    grad.setColorAt(0, QColor("#2f81f7"))
    grad.setColorAt(1, QColor("#0b3d91"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(48, 48, 928, 928), 210, 210)
    # file sheet with a folded corner
    sheet = QPainterPath()
    sheet.moveTo(300, 190)
    sheet.lineTo(610, 190)
    sheet.lineTo(724, 304)
    sheet.lineTo(724, 560)
    sheet.lineTo(300, 560)
    sheet.closeSubpath()
    p.setBrush(QColor("#ffffff"))
    p.drawPath(sheet)
    fold = QPainterPath()
    fold.moveTo(610, 190)
    fold.lineTo(610, 304)
    fold.lineTo(724, 304)
    fold.closeSubpath()
    p.setBrush(QColor("#b6d4fe"))
    p.drawPath(fold)
    # transfer arrow (device-bound)
    pen = QPen(QColor("#ffffff"), 70, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(QPointF(512, 620), QPointF(512, 800))
    head = QPainterPath()
    head.moveTo(400, 720)
    head.lineTo(512, 840)
    head.lineTo(624, 720)
    p.setBrush(Qt.NoBrush)
    p.drawPath(head)
    p.setPen(QPen(QColor("#ffffff"), 56, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(300, 880), QPointF(724, 880))
    p.end()
    return img


def main() -> int:
    app = QGuiApplication(sys.argv)  # noqa: F841 - keeps Qt alive
    ASSETS.mkdir(exist_ok=True)
    draw(1024).save(str(ASSETS / "icon.png"))
    # .ico: Qt writes the largest frame it is given; multi-size ICO is produced by Pillow when available
    try:
        from PIL import Image

        Image.open(ASSETS / "icon.png").save(
            ASSETS / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        )
        Image.open(ASSETS / "icon.png").save(ASSETS / "icon.icns")
    except Exception as exc:
        print(f"Pillow unavailable or failed ({exc}); PyInstaller will convert icon.png itself", file=sys.stderr)
    for f in sorted(ASSETS.iterdir()):
        print(f"{f.name}: {f.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
