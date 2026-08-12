"""Main window: sidebar navigation, page stack, and engine wiring.

The window owns the run engine and logger; pages read mutable state off the
window and the engine's Qt signals drive live updates. The sidebar footer
states the simulation-only execution mode.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from asc_oven_control.infrastructure.persistence import RunLogger
from asc_oven_control.services.run_engine import RunEngine, RunEngineError
from asc_oven_control.ui.pages import DataPage, InstrumentPage, LiveControlPage, SetupPage
from asc_oven_control.ui.plot_widget import ZoneTrendChart

NAV_ITEMS = (
    ("01   Setup", "WORKSPACE / SETUP", "Prepare a thermal run"),
    ("02   Live control", "WORKSPACE / LIVE CONTROL", "Monitor and guide the oven"),
    ("03   Run data", "WORKSPACE / RUN DATA", "Review the temperature record"),
    ("04   Instrument reference", "INSTRUMENT / REFERENCE", "Recovered protocol evidence"),
)


class MainWindow(QMainWindow):
    def __init__(self, config, logger: RunLogger) -> None:
        super().__init__()
        self.config = config
        self.logger = logger
        self.engine = RunEngine(logger, poll_seconds=config.poll_seconds)
        self.setWindowTitle("ASC Oven Control")
        self.resize(1280, 840)
        self.setMinimumSize(1060, 720)

        self.chart = ZoneTrendChart()
        self.nav_buttons: list[QPushButton] = []
        self.last_error = ""
        self._build_ui()

        self.engine.snapshot_ready.connect(self._on_snapshot)
        self.engine.failed.connect(self._on_engine_failed)
        self.engine.finished.connect(self._on_engine_finished)

        self.set_page(0)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_sidebar())

        content = QWidget()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(30, 24, 30, 24)
        content_layout.setSpacing(18)
        content_layout.addLayout(self._build_header())

        self.setup_page = SetupPage(self)
        self.live_page = LiveControlPage(self)
        self.data_page = DataPage(self)
        self.instrument_page = InstrumentPage(self)
        self.pages = QStackedWidget()
        for page in (self.setup_page, self.live_page, self.data_page, self.instrument_page):
            self.pages.addWidget(page)
        content_layout.addWidget(self.pages, 1)
        shell.addWidget(content, 1)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(228)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(20, 26, 20, 22)
        logo = QLabel("ASC")
        logo.setObjectName("logo")
        product = QLabel("OVEN CONTROL")
        product.setObjectName("product")
        layout.addWidget(logo)
        layout.addWidget(product)
        layout.addSpacing(36)
        for index, (label, _eyebrow, _title) in enumerate(NAV_ITEMS):
            nav_button = QPushButton(label)
            nav_button.setObjectName("navButton")
            nav_button.setCheckable(True)
            nav_button.clicked.connect(lambda checked=False, page=index: self.set_page(page))
            self.nav_buttons.append(nav_button)
            layout.addWidget(nav_button)
        layout.addStretch()
        footer = QLabel("SIMULATION SAFE\nNo physical ports opened")
        footer.setObjectName("simSafeFooter")
        layout.addWidget(footer)
        return sidebar

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        title_box = QVBoxLayout()
        self.page_eyebrow = QLabel(NAV_ITEMS[0][1])
        self.page_eyebrow.setObjectName("eyebrow")
        self.page_title = QLabel(NAV_ITEMS[0][2])
        self.page_title.setObjectName("pageTitle")
        title_box.addWidget(self.page_eyebrow)
        title_box.addWidget(self.page_title)
        layout.addLayout(title_box)
        layout.addStretch()
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_text = QLabel("Simulation ready")
        self.status_text.setObjectName("statusText")
        layout.addWidget(self.status_dot)
        layout.addWidget(self.status_text)
        return layout

    # ------------------------------------------------------------- navigation

    def set_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        eyebrow, title = NAV_ITEMS[index][1], NAV_ITEMS[index][2]
        self.page_eyebrow.setText(eyebrow)
        self.page_title.setText(title)
        for button_index, nav_button in enumerate(self.nav_buttons):
            nav_button.setChecked(button_index == index)
        refresh = getattr(self.pages.currentWidget(), "refresh", None)
        if refresh:
            refresh()

    # --------------------------------------------------------------- actions

    def start_run(self) -> None:
        try:
            profile = self.setup_page.collect_profile()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot start run", str(exc))
            return
        try:
            self.engine.start(profile)
        except RunEngineError as exc:
            QMessageBox.warning(self, "Cannot start run", str(exc))
            return
        self.live_page.manual_target_spin.setValue(profile.target_setpoint_c)
        self.live_page.manual_ramp_spin.setValue(profile.ramp_rate_c_per_min)
        self.live_page.live_field_check.setChecked(profile.field_enabled)
        self.show_status_text("Run started (simulation)")
        self.set_page(1)

    def pause_run(self) -> None:
        self.engine.pause()

    def resume_run(self) -> None:
        self.engine.resume()

    def stop_run(self) -> None:
        self.engine.stop()
        self.data_page.refresh()

    def apply_manual_target(self) -> None:
        value = self.live_page.manual_target_spin.value()
        self.engine.set_manual_target(value)
        self.setup_page.target_spin.setValue(value)
        self.show_status_text(f"Target set to {value:.1f} °C")

    def apply_manual_ramp(self) -> None:
        value = self.live_page.manual_ramp_spin.value()
        self.engine.set_ramp_rate(value)
        self.setup_page.ramp_spin.setValue(value)
        self.show_status_text(f"Ramp rate set to {value:.1f} °C/min")

    def apply_manual_field(self) -> None:
        enabled = self.live_page.live_field_check.isChecked()
        amplitude = self.setup_page.field_amplitude_spin.value()
        self.engine.set_field(enabled, amplitude)
        self.setup_page.field_check.setChecked(enabled)
        self.show_status_text(
            f"Field {'ON' if enabled else 'OFF'} · {amplitude:.0f} µT"
        )

    def show_status_text(self, text: str) -> None:
        self.status_text.setText(text)

    # ------------------------------------------------------------ engine events

    def _on_snapshot(self, snapshot: dict) -> None:
        self.live_page._apply_snapshot(snapshot)
        self.chart.append(
            snapshot["timestamp"], snapshot["zones"], snapshot["output_setpoint_c"], snapshot["current_a"]
        )
        if snapshot["alarm"] and snapshot["alarm"] != self.last_error:
            self.last_error = snapshot["alarm"]
            self.status_text.setText("Alarm active")
        elif not self.engine.active:
            self.status_text.setText("Simulation ready")
        else:
            state = "Paused" if self.engine.state == "Paused" else "Running"
            self.status_text.setText(state)

    def _on_engine_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Run failed", message)

    def _on_engine_finished(self, outcome: str) -> None:
        self.show_status_text(f"Run {outcome.lower()}")
        self.live_page.refresh()
        self.data_page.refresh()

    # ------------------------------------------------------------------ close

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.engine.shutdown()
        self.logger.close()
        event.accept()
