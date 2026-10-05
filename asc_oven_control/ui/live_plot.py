"""Live time-temperature plot built on pyqtgraph.

Top panel: the three zones, the master ramp setpoint (dashed) and the
target with its soak band shaded. Bottom panel: the zone-to-zone gradient
(hottest minus coldest). The x axis is elapsed run time in minutes and is
shared by both panels. Mouse wheel zooms, drag pans, and the "A" button
in the corner returns to auto-follow. Every sample of the run is kept, so
multi-hour runs stay complete on screen.

``create_trend_chart`` falls back to the dependency-free QPainter chart
when pyqtgraph is not installed.
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from asc_oven_control.ui.plot_widget import BACKGROUND, SETPOINT_COLOR, ZONE_COLORS, ZoneTrendChart

GRADIENT_COLOR = "#E76F51"
TARGET_COLOR = "#B9CAD0"


def create_trend_chart() -> QWidget:
    try:
        import pyqtgraph  # noqa: F401
    except ImportError:
        return ZoneTrendChart()
    return LiveTrendPlot()


class LiveTrendPlot(QWidget):
    """Two linked pyqtgraph panels fed one snapshot per poll."""

    def __init__(self) -> None:
        super().__init__()
        import pyqtgraph as pg

        pg.setConfigOptions(antialias=True, background=BACKGROUND, foreground="#B9CAD0")
        self._pg = pg
        self.setMinimumHeight(240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = pg.GraphicsLayoutWidget()
        layout.addWidget(self.view)

        self.temp_plot = self.view.addPlot(row=0, col=0)
        self.temp_plot.setLabel("left", "Temperature", units="°C")
        self.temp_plot.showGrid(x=True, y=True, alpha=0.2)
        self.temp_plot.addLegend(offset=(10, 10))
        self.view.nextRow()
        self.gradient_plot = self.view.addPlot(row=1, col=0)
        self.gradient_plot.setLabel("left", "Gradient", units="°C")
        self.gradient_plot.setLabel("bottom", "Elapsed time (min)")
        self.gradient_plot.showGrid(x=True, y=True, alpha=0.2)
        self.gradient_plot.setXLink(self.temp_plot)
        self.view.ci.layout.setRowStretchFactor(0, 3)
        self.view.ci.layout.setRowStretchFactor(1, 1)

        self.band = pg.LinearRegionItem(
            orientation="horizontal", movable=False, brush=pg.mkBrush(233, 196, 106, 40), pen=pg.mkPen(None)
        )
        self.band.setVisible(False)
        self.temp_plot.addItem(self.band)
        self.target_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(TARGET_COLOR, width=1, style=_dot()))
        self.target_line.setVisible(False)
        self.temp_plot.addItem(self.target_line)

        self.setpoint_curve = self.temp_plot.plot(
            [], [], name="Ramp setpoint", pen=pg.mkPen(SETPOINT_COLOR, width=2, style=_dash())
        )
        self.zone_curves = [
            self.temp_plot.plot([], [], name=f"Zone {i + 1}", pen=pg.mkPen(color, width=2.5))
            for i, color in enumerate(ZONE_COLORS)
        ]
        self.gradient_curve = self.gradient_plot.plot([], [], pen=pg.mkPen(GRADIENT_COLOR, width=2))
        self.clear()

    def clear(self) -> None:
        self.t: list[float] = []
        self.zones: list[list[float]] = [[], [], []]
        self.setpoints: list[float] = []
        self.gradients: list[float] = []
        for curve in (*self.zone_curves, self.setpoint_curve, self.gradient_curve):
            curve.setData([], [])
        self.band.setVisible(False)
        self.target_line.setVisible(False)
        self.temp_plot.enableAutoRange()
        self.gradient_plot.enableAutoRange()

    def load_series(self, minutes, zones, setpoints, target=None, band=None) -> None:
        """Show a whole recorded run at once (history view)."""
        self.clear()
        self.t = list(minutes)
        self.zones = [list(series) for series in zones]
        self.setpoints = list(setpoints)
        self.gradients = [max(values) - min(values) for values in zip(*self.zones)] if self.t else []
        for curve, series in zip(self.zone_curves, self.zones):
            curve.setData(self.t, series)
        self.setpoint_curve.setData(self.t, self.setpoints)
        self.gradient_curve.setData(self.t, self.gradients)
        if target is not None:
            self.target_line.setValue(target)
            self.target_line.setVisible(True)
            if band:
                self.band.setRegion((target - band, target + band))
                self.band.setVisible(True)
        self.temp_plot.enableAutoRange()
        self.gradient_plot.enableAutoRange()

    def add_snapshot(self, snapshot: dict) -> None:
        self.append(
            snapshot["elapsed_sec"],
            snapshot["zones"],
            snapshot["output_setpoint_c"],
            None,
            target=snapshot.get("target_setpoint_c"),
            band=snapshot.get("soak_band_c"),
        )

    def append(self, elapsed_s, zones, setpoint, current=None, target=None, band=None) -> None:
        self.t.append(elapsed_s / 60.0)
        for series, value in zip(self.zones, zones):
            series.append(value)
        self.setpoints.append(setpoint)
        self.gradients.append(max(zones) - min(zones))
        for curve, series in zip(self.zone_curves, self.zones):
            curve.setData(self.t, series)
        self.setpoint_curve.setData(self.t, self.setpoints)
        self.gradient_curve.setData(self.t, self.gradients)
        if target is not None:
            self.target_line.setValue(target)
            self.target_line.setVisible(True)
            if band:
                self.band.setRegion((target - band, target + band))
                self.band.setVisible(True)


def _dash():
    from PySide6.QtCore import Qt

    return Qt.PenStyle.DashLine


def _dot():
    from PySide6.QtCore import Qt

    return Qt.PenStyle.DotLine
