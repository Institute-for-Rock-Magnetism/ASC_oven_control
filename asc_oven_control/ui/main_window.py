"""Main window: sidebar navigation, page stack, and engine wiring.

The window owns the run engine and logger; pages read mutable state off the
window and the engine's Qt signals drive live updates. The sidebar footer
states whether runs drive the simulation or the real Watlow controllers.
"""

from __future__ import annotations

from pathlib import Path

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

from asc_oven_control.infrastructure.persistence import RunLogger, atomic_write_json
from asc_oven_control.services.oven_backend import create_backend
from asc_oven_control.services.run_engine import RunEngine, RunEngineError
from asc_oven_control.ui.pages import DataPage, InstrumentPage, LiveControlPage, SetupPage, TuningPage
from asc_oven_control.ui.plot_widget import ZoneTrendChart

NAV_ITEMS = (
    ("01   Setup", "WORKSPACE / SETUP", "Prepare a thermal run"),
    ("02   Live control", "WORKSPACE / LIVE CONTROL", "Monitor and guide the oven"),
    ("03   Run data", "WORKSPACE / RUN DATA", "Review the temperature record"),
    ("04   Controller tuning", "INSTRUMENT / PID TUNING", "Watlow Series 96 PID and auto-tune"),
    ("05   Instrument reference", "INSTRUMENT / REFERENCE", "Protocol and register map"),
)

# Simulated runs advance this many times faster than real time.
SIMULATION_TIME_SCALE = 20.0


class MainWindow(QMainWindow):
    def __init__(self, config, logger: RunLogger, config_path: Path | None = None) -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.logger = logger
        self.engine = RunEngine(
            logger,
            poll_seconds=config.poll_seconds,
            backend_factory=lambda: create_backend(self.config),
            simulation_time_scale=SIMULATION_TIME_SCALE,
        )
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
        self.engine.state_changed.connect(self._on_state_changed)

        self._update_mode_labels()
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
        self.tuning_page = TuningPage(self)
        self.instrument_page = InstrumentPage(self)
        self.pages = QStackedWidget()
        for page in (self.setup_page, self.live_page, self.data_page, self.tuning_page, self.instrument_page):
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
        self.mode_footer = QLabel("")
        self.mode_footer.setObjectName("simSafeFooter")
        layout.addWidget(self.mode_footer)
        return sidebar

    def mode_text(self) -> str:
        if self.config.simulation_mode:
            return "SIMULATION\nNo physical ports opened"
        return f"HARDWARE · {self.config.serial.port}\nWatlow Series 96 × 3"

    def _update_mode_labels(self) -> None:
        self.mode_footer.setText(self.mode_text())
        if not self.engine.active:
            self.status_text.setText(self.idle_status())

    def idle_status(self) -> str:
        return "Simulation ready" if self.config.simulation_mode else f"Hardware ready ({self.config.serial.port})"

    def apply_config(self, config) -> None:
        """Adopt a new configuration and persist it for the next launch."""
        self.config = config
        if self.config_path is not None:
            atomic_write_json(self.config_path, config.to_dict())
        self._update_mode_labels()
        self.instrument_page.refresh()

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
        self.status_text = QLabel("")
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
            settings = self.setup_page.collect_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot start run", str(exc))
            return
        if not self.config.simulation_mode:
            answer = QMessageBox.question(
                self,
                "Start hardware run",
                f"This run will write set points to the three Watlow controllers on "
                f"{self.config.serial.port} and heat the oven to "
                f"{profile.target_setpoint_c:.0f} °C at {profile.ramp_rate_c_per_min:g} °C/min.\n\n"
                "Stop, completion, or a communication failure sets every zone to its "
                "lowest set point. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.setup_page.save_form_state()
        self.engine.set_settings(settings)
        self.chart.clear()
        try:
            self.engine.start(profile)
        except (RunEngineError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot start run", str(exc))
            return
        self.live_page.manual_target_spin.setValue(profile.target_setpoint_c)
        self.live_page.manual_ramp_spin.setValue(profile.ramp_rate_c_per_min)
        self.live_page.live_field_check.setChecked(profile.field_enabled)
        mode = "simulation" if self.config.simulation_mode else "hardware"
        self.show_status_text(f"Run started ({mode})")
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
            snapshot["elapsed_sec"], snapshot["zones"], snapshot["output_setpoint_c"], snapshot["current_a"]
        )
        if snapshot["alarm"] and snapshot["alarm"] != self.last_error:
            self.last_error = snapshot["alarm"]
            self.status_text.setText("Alarm active")
        elif not self.engine.active:
            self.status_text.setText(self.idle_status())

    def _on_state_changed(self, _state: str, message: str) -> None:
        self.status_text.setText(message)
        self.live_page.add_event(message)
        self.live_page.refresh()

    def _on_engine_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Run failed", message)

    def runs_dir(self) -> Path | None:
        if self.config_path is not None:
            return self.config_path.parent.parent / "runs"
        return Path(self.config.data_dir) / "runs" if self.config.data_dir else None

    def _auto_export(self) -> str:
        """Write the finished run's full time/temperature record as CSV."""
        from datetime import datetime

        from asc_oven_control.infrastructure.persistence import export_samples_csv

        run_id = self.engine.last_run_id
        directory = self.runs_dir()
        if run_id is None or directory is None:
            return ""
        rows = self.logger.get_detailed_samples(run_id)
        if not rows:
            return ""
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"run-{run_id:04d}-{datetime.now():%Y%m%d-%H%M}.csv"
        export_samples_csv(rows, target)
        return str(target)

    def _on_engine_finished(self, outcome: str) -> None:
        try:
            exported = self._auto_export()
        except OSError as exc:
            exported = ""
            QMessageBox.warning(self, "Run log", f"Could not write the run CSV: {exc}")
        self.show_status_text(f"Run {outcome.lower()}" + (f" · log saved to {exported}" if exported else ""))
        self.live_page.refresh()
        self.data_page.refresh()
        self.tuning_page.refresh()

    # ------------------------------------------------------------------ close

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.engine.shutdown()
        self.logger.close()
        event.accept()
