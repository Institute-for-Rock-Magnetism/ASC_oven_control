"""Dependency-free Qt trend chart for the three oven zones.

Painted with QPainter only (no QtCharts, no third-party plotting). Keeps a
bounded history of (timestamp, zone temps, setpoint, current) records and
redraws on update.
"""

from __future__ import annotations

from collections import deque
from typing import Deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

ZONE_COLORS = ("#56D6C9", "#F4A261", "#5FA8D3")  # Zone 1, 2, 3
SETPOINT_COLOR = "#E9C46A"
CURRENT_COLOR = "#8CA4AD"
GRID_COLOR = "#294753"
BACKGROUND = "#102A36"
MAX_RECORDS = 1200


class ZoneTrendChart(QWidget):
    """Live chart of the three zones, the commanded setpoint, and current."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(330)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.records: Deque[tuple[float, tuple[float, float, float], float, float | None]] = deque(
            maxlen=MAX_RECORDS
        )

    def clear(self) -> None:
        self.records.clear()
        self.update()

    def append(
        self,
        timestamp: float,
        zones: tuple[float, float, float],
        setpoint: float,
        current: float | None,
    ) -> None:
        self.records.append((timestamp, zones, setpoint, current))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(BACKGROUND))
        area = QRectF(56, 24, max(self.width() - 82, 1), max(self.height() - 68, 1))

        painter.setPen(QPen(QColor(GRID_COLOR), 1))
        for index in range(5):
            y = area.top() + area.height() * index / 4
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))

        if len(self.records) < 2:
            painter.setPen(QColor("#8CA4AD"))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Trend appears after a run starts")
            return

        times = [record[0] for record in self.records]
        zone_values = [value for record in self.records for value in record[1]]
        all_values = zone_values + [record[2] for record in self.records]
        currents = [record[3] for record in self.records if record[3] is not None]
        if currents:
            all_values += currents
        low, high = min(all_values), max(all_values)
        margin = max((high - low) * 0.12, 2.0)
        low -= margin
        high += margin

        def map_point(timestamp: float, value: float) -> QPointF:
            x = area.left() + (timestamp - times[0]) / max(times[-1] - times[0], 1.0) * area.width()
            y = area.bottom() - (value - low) / max(high - low, 1.0) * area.height()
            return QPointF(x, y)

        # Setpoint first (behind the zone lines).
        self._draw_series(painter, map_point, times, [record[2] for record in self.records],
                          SETPOINT_COLOR, dash=True)
        for zone_index, color in enumerate(ZONE_COLORS):
            values = [record[1][zone_index] for record in self.records]
            self._draw_series(painter, map_point, times, values, color, dash=False)
        if currents:
            self._draw_series(painter, map_point, times, currents, CURRENT_COLOR, dash=False)

        painter.setPen(QColor("#B9CAD0"))
        painter.setFont(QFont("Avenir Next", 9))
        painter.drawText(QRectF(8, area.top() - 8, 44, 20), Qt.AlignmentFlag.AlignRight, f"{high:.0f}")
        painter.drawText(QRectF(8, area.bottom() - 10, 44, 20), Qt.AlignmentFlag.AlignRight, f"{low:.0f}")

        legend = [
            ("● Zone 1", ZONE_COLORS[0]),
            ("● Zone 2", ZONE_COLORS[1]),
            ("● Zone 3", ZONE_COLORS[2]),
            ("◌ Setpoint", SETPOINT_COLOR),
        ]
        if currents:
            legend.append(("● Current", CURRENT_COLOR))
        x = area.left()
        for text, color in legend:
            painter.setPen(QColor(color))
            painter.drawText(QPointF(x, self.height() - 16), text)
            x += painter.fontMetrics().horizontalAdvance(text) + 22

    @staticmethod
    def _draw_series(
        painter: QPainter,
        map_point,
        times: list[float],
        values: list[float],
        color: str,
        dash: bool,
    ) -> None:
        path = QPainterPath(map_point(times[0], values[0]))
        for timestamp, value in zip(times[1:], values[1:]):
            path.lineTo(map_point(timestamp, value))
        pen = QPen(QColor(color), 2.5)
        if dash:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawPath(path)
