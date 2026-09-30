"""Application pages: Setup, Live Control, Run Data, Instrument Reference.

Pages follow the Long Core Control pattern: each page is a plain widget
constructed with the owning window, reads state off the window, and exposes
an optional ``refresh()`` called on navigation.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from asc_oven_control.domain.models import Atmosphere, DomainValidationError, RunProfile
from asc_oven_control.domain.zone_control import GradientSettings
from asc_oven_control.infrastructure.legacy_table import (
    LegacyRow,
    LegacyTable,
    LegacyTableError,
    parse_legacy_file,
    write_legacy_file,
)
from asc_oven_control.infrastructure.modbus_rtu import read_request, write_request
from asc_oven_control.ui.widgets import Card, MetricCard, button, pill


def _scroll_page(body: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(body)
    return scroll


class SetupPage(QWidget):
    """Prepare a run: identity, thermal recipe, field, atmosphere, PID."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 8, 8)
        grid.setSpacing(18)

        connection = Card(
            "Instrument connection",
            "Three Watlow Series 96 controllers (one per zone) on one RS-485 bus, "
            "Modbus RTU at 9600 baud 8N1. Test connection only reads registers.",
        )
        serial_form = QFormLayout()
        serial_form.setSpacing(10)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Simulation", "Watlow hardware"])
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.addresses_label = QLabel("")
        serial_form.addRow("Mode", self.mode_combo)
        serial_form.addRow("Serial port", self.port_combo)
        serial_form.addRow("Zone addresses", self.addresses_label)
        serial_form.addRow("Framing", QLabel("Modbus RTU · 9600 8N1"))
        connection.body.addLayout(serial_form)
        connection_buttons = QHBoxLayout()
        connection_buttons.addWidget(button("Rescan ports", "quiet", self._scan_ports))
        connection_buttons.addWidget(button("Test connection", "secondary", self._test_connection))
        connection_buttons.addWidget(button("Save connection", "primary", self._save_connection))
        connection_buttons.addStretch()
        panel_button = button("Hand set point to oven panel", "quiet", self._hand_back)
        panel_button.setToolTip(
            "Runs switch the controllers to Local so the PC sets the temperature. This "
            "switches them back to Remote, so the oven's own panel/timer (Input 2) sets it again."
        )
        connection_buttons.addWidget(panel_button)
        connection.body.addLayout(connection_buttons)
        self.connection_result = QLabel("")
        self.connection_result.setObjectName("recoveredNote")
        self.connection_result.setWordWrap(True)
        self.connection_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        connection.body.addWidget(self.connection_result)
        grid.addWidget(connection, 0, 0)
        self._load_connection()

        profile = Card("Run identity", "These fields travel with every logged sample.")
        profile_form = QFormLayout()
        profile_form.setSpacing(12)
        self.operator_edit = QLineEdit("ASC Operator")
        self.batch_edit = QLineEdit("batch-001")
        self.sample_edit = QLineEdit()
        self.user_edit = QLineEdit("lab-user")
        self.atmosphere_combo = QComboBox()
        for atmosphere in Atmosphere:
            self.atmosphere_combo.addItem(str(atmosphere))
        self.notes_edit = QTextEdit()
        self.notes_edit.setFixedHeight(74)
        for label, widget in (
            ("Operator", self.operator_edit),
            ("Batch ID", self.batch_edit),
            ("Sample ID", self.sample_edit),
            ("Database user", self.user_edit),
            ("Atmosphere", self.atmosphere_combo),
            ("Notes", self.notes_edit),
        ):
            profile_form.addRow(label, widget)
        profile.body.addLayout(profile_form)
        grid.addWidget(profile, 0, 1, 3, 1)

        thermal = Card(
            "Thermal profile",
            "Target, ramp behavior, soak duration, and independent safety notifications.",
        )
        thermal_form = QFormLayout()
        thermal_form.setSpacing(12)
        self.target_spin = self._temperature_spin(590.0, 0.0, 1400.0, " °C")
        self.ramp_spin = self._temperature_spin(20.0, 0.0, 500.0, " °C/min")
        self.soak_spin = QSpinBox()
        self.soak_spin.setRange(0, 604800)
        self.soak_spin.setValue(600)
        self.soak_spin.setSuffix(" s")
        self.max_time_spin = QSpinBox()
        self.max_time_spin.setRange(0, 10000)
        self.max_time_spin.setValue(0)
        self.max_time_spin.setSuffix(" min")
        self.max_time_spin.setSpecialValueText("no limit")
        self.max_time_spin.setToolTip(
            "End the run with the heaters off after this long, even if the hold is unfinished "
            "(set it below the oven's onboard timer for unattended runs). 0 = no limit."
        )
        self.alarm_high_spin = self._temperature_spin(1200.0, -100.0, 1600.0, " °C")
        self.alarm_low_spin = self._temperature_spin(10.0, -100.0, 1600.0, " °C")
        for label, widget in (
            ("Target", self.target_spin),
            ("Ramp rate", self.ramp_spin),
            ("Soak time", self.soak_spin),
            ("Max run time", self.max_time_spin),
            ("High alarm", self.alarm_high_spin),
            ("Low alarm", self.alarm_low_spin),
        ):
            thermal_form.addRow(label, widget)
        thermal.body.addLayout(thermal_form)
        grid.addWidget(thermal, 1, 0)

        field_card = Card(
            "Field coil",
            "Recovered from ASC_thermal2.0.vi: the TD48 applies an in-run field "
            "(Field ON/OFF, amplitude) for thermal demagnetization experiments.",
        )
        field_form = QFormLayout()
        field_form.setSpacing(12)
        self.field_check = QCheckBox("Field ON during run")
        self.field_amplitude_spin = self._temperature_spin(0.0, 0.0, 2000.0, " µT")
        field_form.addRow(self.field_check, self.field_amplitude_spin)
        field_card.body.addLayout(field_form)
        grid.addWidget(field_card, 2, 0)

        gradient = Card(
            "Gradient control",
            "Supervisory layer over the three Watlow PID loops. The ramp waits for a "
            "lagging zone, leading zones are capped to the coldest zone plus the "
            "allowed gradient, the ramp slows near the target, and the soak clock runs "
            "only while every zone is in band.",
        )
        gradient_form = QFormLayout()
        gradient_form.setSpacing(12)
        defaults = GradientSettings()
        self.hold_band_spin = self._temperature_spin(defaults.hold_band_c, 0.5, 100.0, " °C")
        self.max_gradient_spin = self._temperature_spin(defaults.max_gradient_c, 0.5, 100.0, " °C")
        self.approach_band_spin = self._temperature_spin(defaults.approach_band_c, 0.0, 300.0, " °C")
        self.approach_rate_spin = QSpinBox()
        self.approach_rate_spin.setRange(5, 100)
        self.approach_rate_spin.setValue(round(defaults.approach_rate_fraction * 100))
        self.approach_rate_spin.setSuffix(" % of ramp rate")
        self.soak_band_spin = self._temperature_spin(defaults.soak_band_c, 0.5, 50.0, " °C")
        offsets_row = QHBoxLayout()
        self.offset_spins = []
        for index in range(3):
            spin = self._temperature_spin(0.0, -50.0, 50.0, " °C")
            spin.setToolTip(f"Fixed trim added to the Zone {index + 1} setpoint")
            self.offset_spins.append(spin)
            offsets_row.addWidget(spin)
        for label, widget in (
            ("Ramp hold band", self.hold_band_spin),
            ("Max zone gradient", self.max_gradient_spin),
            ("Approach band", self.approach_band_spin),
            ("Approach rate", self.approach_rate_spin),
            ("Soak band", self.soak_band_spin),
        ):
            gradient_form.addRow(label, widget)
        gradient_form.addRow("Zone 1/2/3 trim", offsets_row)
        self.strict_soak_check = QCheckBox("Pause when out of band")
        self.strict_soak_check.setChecked(defaults.strict_soak)
        self.strict_soak_check.setToolTip(
            "Checked: the soak clock pauses whenever a zone leaves the band.\n"
            "Unchecked: the soak starts once every zone is in band, then runs for the full "
            "soak time; time spent out of band is logged and shown."
        )
        gradient_form.addRow("Soak clock", self.strict_soak_check)
        comp_row = QHBoxLayout()
        self.comp_rate_spin = QDoubleSpinBox()
        self.comp_rate_spin.setRange(0.0, 2.0)
        self.comp_rate_spin.setDecimals(2)
        self.comp_rate_spin.setSingleStep(0.05)
        self.comp_rate_spin.setValue(defaults.center_comp_rate_per_min)
        self.comp_rate_spin.setSuffix(" °C/min per °C")
        self.comp_rate_spin.setToolTip(
            "While Zone 2 (middle) is above target, Zones 1 and 3 are lowered at this rate per "
            "degree of Zone 2 excess; 0 disables. Only ever lowers the outer zones."
        )
        self.comp_limit_spin = self._temperature_spin(defaults.center_comp_limit_c, 0.0, 200.0, " °C max")
        comp_row.addWidget(self.comp_rate_spin)
        comp_row.addWidget(self.comp_limit_spin)
        gradient_form.addRow("Middle-zone comp.", comp_row)
        gradient.body.addLayout(gradient_form)
        grid.addWidget(gradient, 3, 0)
        self.load_form_state()

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(_scroll_page(body))

    @staticmethod
    def _temperature_spin(value: float, minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    # ------------------------------------------------------------ form state

    def _form_widgets(self) -> dict:
        widgets = {
            "operator": self.operator_edit,
            "batch_id": self.batch_edit,
            "sample_id": self.sample_edit,
            "user_name": self.user_edit,
            "target_c": self.target_spin,
            "ramp_c_per_min": self.ramp_spin,
            "soak_s": self.soak_spin,
            "max_run_min": self.max_time_spin,
            "alarm_high_c": self.alarm_high_spin,
            "alarm_low_c": self.alarm_low_spin,
            "field_uT": self.field_amplitude_spin,
            "hold_band_c": self.hold_band_spin,
            "max_gradient_c": self.max_gradient_spin,
            "approach_band_c": self.approach_band_spin,
            "approach_rate_pct": self.approach_rate_spin,
            "soak_band_c": self.soak_band_spin,
            "center_comp_rate": self.comp_rate_spin,
            "center_comp_limit_c": self.comp_limit_spin,
        }
        for index, spin in enumerate(self.offset_spins):
            widgets[f"zone{index + 1}_trim_c"] = spin
        return widgets

    def _form_state_path(self):
        path = getattr(self.window, "config_path", None)
        return None if path is None else path.with_name("last_setup.json")

    def form_state(self) -> dict:
        state = {}
        for key, widget in self._form_widgets().items():
            state[key] = widget.text() if isinstance(widget, QLineEdit) else widget.value()
        state["atmosphere"] = self.atmosphere_combo.currentText()
        state["field_enabled"] = self.field_check.isChecked()
        state["strict_soak"] = self.strict_soak_check.isChecked()
        state["notes"] = self.notes_edit.toPlainText()
        return state

    def save_form_state(self) -> None:
        """Remember the form so the next launch starts from the last run."""
        path = self._form_state_path()
        if path is None:
            return
        from asc_oven_control.infrastructure.persistence import atomic_write_json

        try:
            atomic_write_json(path, self.form_state(), create_backup=False)
        except OSError:
            pass

    def load_form_state(self) -> None:
        path = self._form_state_path()
        if path is None or not path.exists():
            return
        import json

        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, widget in self._form_widgets().items():
            if key not in state:
                continue
            value = state[key]
            try:
                if isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(value))
                else:
                    widget.setValue(float(value))
            except (TypeError, ValueError):
                continue
        if state.get("atmosphere") in [str(a) for a in Atmosphere]:
            self.atmosphere_combo.setCurrentText(state["atmosphere"])
        self.field_check.setChecked(bool(state.get("field_enabled", False)))
        self.strict_soak_check.setChecked(bool(state.get("strict_soak", True)))
        self.notes_edit.setPlainText(str(state.get("notes", "")))

    # ------------------------------------------------------------ connection

    def _load_connection(self) -> None:
        config = self.window.config
        self.mode_combo.setCurrentIndex(0 if config.simulation_mode else 1)
        self._scan_ports()
        if config.serial.port:
            self.port_combo.setCurrentText(config.serial.port)
        self.addresses_label.setText(", ".join(str(a) for a in config.zone_addresses))

    def _scan_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        try:
            from serial.tools import list_ports

            ports = sorted(list_ports.comports(), key=lambda p: p.device)
        except Exception:  # noqa: BLE001 - pyserial missing or enumeration failed
            ports = []
        for port in ports:
            self.port_combo.addItem(port.device)
            self.port_combo.setItemData(self.port_combo.count() - 1, port.description, Qt.ItemDataRole.ToolTipRole)
        if current:
            self.port_combo.setCurrentText(current)

    def connection_config(self):
        """The window config with this card's mode and port applied."""
        from dataclasses import replace

        config = self.window.config
        port = self.port_combo.currentText().strip() or None
        return replace(
            config,
            simulation_mode=self.mode_combo.currentIndex() == 0,
            serial=replace(config.serial, port=port),
        )

    def _test_connection(self) -> None:
        from dataclasses import replace

        if self.window.engine.active and not self.window.config.simulation_mode:
            self.connection_result.setText("A hardware run is active; the port is in use.")
            return
        config = replace(self.connection_config(), simulation_mode=False)
        if config.serial.port is None:
            self.connection_result.setText("Choose a serial port first.")
            return
        from asc_oven_control.services.oven_backend import probe_hardware

        if not self.window.acquire_port():
            self.connection_result.setText("The serial port is busy; try again in a moment.")
            return
        self.connection_result.setText(f"Testing {config.serial.port}…")
        self.connection_result.repaint()
        try:
            lines = probe_hardware(config)
        except Exception as exc:  # noqa: BLE001 - show any failure to the operator
            self.connection_result.setText(f"Connection failed on {config.serial.port}: {exc}")
            return
        finally:
            self.window.release_port()
        self.connection_result.setText("\n".join(lines))

    def _hand_back(self) -> None:
        from asc_oven_control.services.oven_backend import WatlowOven

        config = self.window.config
        if config.simulation_mode or not config.serial.port:
            self.connection_result.setText("Only applies in Watlow hardware mode.")
            return
        answer = QMessageBox.question(
            self,
            "Hand set point to oven panel",
            "Set every zone to its lowest local set point and switch the controllers back to "
            "Remote, so the oven's own panel/timer controls the temperature again?\n\n"
            "If the oven panel is set to a temperature and its timer is running, the oven will heat.",
        )
        if answer != QMessageBox.StandardButton.Yes or not self.window.acquire_port():
            return
        backend = WatlowOven(config)
        try:
            backend.connect()
            backend.release_control()
            self.connection_result.setText("All zones now follow the oven panel (Remote set point).")
        except Exception as exc:  # noqa: BLE001
            self.connection_result.setText(f"Hand-back failed: {exc}")
        finally:
            backend.close()
            self.window.release_port()

    def _save_connection(self) -> None:
        if self.window.engine.active:
            QMessageBox.information(self, "Connection", "Stop the active run before changing the connection.")
            return
        config = self.connection_config()
        if not config.simulation_mode and config.serial.port is None:
            QMessageBox.warning(self, "Connection", "Hardware mode needs a serial port.")
            return
        self.window.apply_config(config)
        mode = "simulation" if config.simulation_mode else f"hardware on {config.serial.port}"
        self.connection_result.setText(f"Saved: {mode}")

    def collect_settings(self) -> GradientSettings:
        return GradientSettings(
            hold_band_c=self.hold_band_spin.value(),
            max_gradient_c=self.max_gradient_spin.value(),
            approach_band_c=self.approach_band_spin.value(),
            approach_rate_fraction=self.approach_rate_spin.value() / 100.0,
            soak_band_c=self.soak_band_spin.value(),
            zone_offsets_c=tuple(spin.value() for spin in self.offset_spins),
            strict_soak=self.strict_soak_check.isChecked(),
            center_comp_rate_per_min=self.comp_rate_spin.value(),
            center_comp_limit_c=self.comp_limit_spin.value(),
        )

    def collect_profile(self) -> RunProfile:
        """Build a validated RunProfile from the form; raises on bad input."""
        atmosphere = Atmosphere(self.atmosphere_combo.currentText())
        try:
            return RunProfile(
                operator=self.operator_edit.text().strip(),
                batch_id=self.batch_edit.text().strip(),
                sample_id=self.sample_edit.text().strip(),
                user_name=self.user_edit.text().strip(),
                atmosphere=atmosphere,
                target_setpoint_c=self.target_spin.value(),
                ramp_rate_c_per_min=self.ramp_spin.value(),
                soak_time_sec=float(self.soak_spin.value()),
                alarm_high_c=self.alarm_high_spin.value(),
                alarm_low_c=self.alarm_low_spin.value(),
                notes=self.notes_edit.toPlainText().strip(),
                field_enabled=self.field_check.isChecked(),
                field_amplitude_uT=self.field_amplitude_spin.value(),
                max_run_time_sec=float(self.max_time_spin.value() * 60),
            )
        except DomainValidationError as exc:
            raise ValueError(str(exc)) from exc


class LiveControlPage(QWidget):
    """Monitor and guide the oven: metrics, trend, commands, manual adjust."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        from PySide6.QtWidgets import QSplitter

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._live_reading = None

        metrics = QGridLayout()
        metrics.setSpacing(12)
        self.zone_metrics = [
            MetricCard("Zone 1", "-- °C", "#56D6C9"),
            MetricCard("Zone 2", "-- °C", "#F4A261"),
            MetricCard("Zone 3", "-- °C", "#5FA8D3"),
            MetricCard("Zone gradient", "-- °C", "#8CA4AD"),
        ]
        for column, metric in enumerate(self.zone_metrics):
            metric.setMaximumHeight(120)
            metrics.addWidget(metric, 0, column)
        layout.addLayout(metrics)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.phase_pill = pill("Idle", "phaseChip")
        self.field_pill = pill("Field OFF", "fieldBadgeOff")
        self.elapsed_label = QLabel("Elapsed 00:00:00")
        self.elapsed_label.setObjectName("muted")
        self.setpoint_label = QLabel("")
        self.setpoint_label.setObjectName("muted")
        self.alarm_label = QLabel("No active alarm")
        self.alarm_label.setObjectName("alarmClear")
        self.start_button = button("Start run", "primary", window.start_run)
        self.pause_button = button("Pause", "secondary", window.pause_run)
        self.resume_button = button("Resume", "secondary", window.resume_run)
        self.stop_button = button("Stop", "danger", window.stop_run)
        for widget in (self.phase_pill, self.field_pill, self.alarm_label, self.elapsed_label, self.setpoint_label):
            status_row.addWidget(widget)
        status_row.addStretch()
        for widget in (self.start_button, self.pause_button, self.resume_button, self.stop_button):
            status_row.addWidget(widget)
        layout.addLayout(status_row)
        self.events: list[str] = []
        self.event_label = QLabel("")
        self.event_label.setObjectName("muted")
        self.event_label.setWordWrap(True)
        layout.addWidget(self.event_label)

        chart_card = Card()
        chart_header = QHBoxLayout()
        chart_title = QLabel("Temperature trend")
        chart_title.setObjectName("cardTitle")
        chart_hint = QLabel("Scroll to zoom · drag to pan · A = auto-follow · drag the bar below to resize")
        chart_hint.setObjectName("muted")
        chart_header.addWidget(chart_title)
        chart_header.addSpacing(12)
        chart_header.addWidget(chart_hint)
        chart_header.addStretch()
        chart_header.addWidget(button("Clear chart", "quiet", window.clear_chart))
        chart_card.body.addLayout(chart_header)
        chart_frame = QFrame()
        chart_frame.setObjectName("chartFrame")
        frame_layout = QVBoxLayout(chart_frame)
        frame_layout.setContentsMargins(10, 10, 10, 10)
        self.chart = window.chart
        frame_layout.addWidget(self.chart)
        chart_card.body.addWidget(chart_frame, 1)

        manual = Card(
            "Manual adjustment",
            "Change the target or ramp rate of the active run; the field can be toggled live.",
        )
        manual_row = QHBoxLayout()
        self.manual_target_spin = self._temperature_spin(590.0, 0.0, 1400.0, " °C")
        self.manual_ramp_spin = self._temperature_spin(20.0, 0.0, 500.0, " °C/min")
        self.live_field_check = QCheckBox("Field ON")
        manual_row.addWidget(QLabel("Target"))
        manual_row.addWidget(self.manual_target_spin)
        manual_row.addWidget(button("Apply target", "secondary", window.apply_manual_target))
        manual_row.addSpacing(16)
        manual_row.addWidget(QLabel("Ramp"))
        manual_row.addWidget(self.manual_ramp_spin)
        manual_row.addWidget(button("Apply ramp", "secondary", window.apply_manual_ramp))
        manual_row.addSpacing(16)
        manual_row.addWidget(self.live_field_check)
        manual_row.addWidget(button("Apply field", "secondary", window.apply_manual_field))
        manual_row.addStretch()
        manual.body.addLayout(manual_row)
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("Zone offsets 1 / 2 / 3"))
        self.manual_offset_spins = []
        for index in range(3):
            spin = self._temperature_spin(0.0, -100.0, 50.0, " °C")
            spin.setToolTip(
                f"Zone {index + 1} runs at target + this offset (e.g. −10 on Zones 1 and 3 so the "
                "middle zone, which gains their heat, lands on the target)"
            )
            self.manual_offset_spins.append(spin)
            offset_row.addWidget(spin)
        offset_row.addWidget(button("Apply offsets", "secondary", window.apply_manual_offsets))
        self.comp_label = QLabel("")
        self.comp_label.setObjectName("muted")
        offset_row.addSpacing(12)
        offset_row.addWidget(self.comp_label)
        offset_row.addStretch()
        manual.body.addLayout(offset_row)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(True)
        self.splitter.setHandleWidth(12)
        self.splitter.addWidget(chart_card)
        self.splitter.addWidget(manual)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setCollapsible(0, False)
        self.splitter.setSizes([10_000, 150])
        layout.addWidget(self.splitter, 1)

    def apply_reading(self, reading) -> None:
        """Live controller readings while no run is active."""
        self._live_reading = reading
        for metric, value, setpoint, power, remote in zip(
            self.zone_metrics[:3], reading.zones_c, reading.setpoints_c, reading.power_pct, reading.remote
        ):
            metric.set_value(f"{value:.0f} °C")
            source = "oven panel" if remote else "PC"
            out = "" if power is None else f" · out {power:.0f} %"
            metric.set_detail(f"live · SP {setpoint:.0f} °C ({source}){out}")
        zones = reading.zones_c
        self.zone_metrics[3].set_value(f"{max(zones) - min(zones):.0f} °C")
        self.zone_metrics[3].set_detail("hottest − coldest zone")
        self.phase_pill.setText("Idle · live readings")
        self.setpoint_label.setText(time.strftime("updated %H:%M:%S"))
        alarm = reading.alarms[0] if reading.alarms else ""
        self._set_alarm(alarm)

    @staticmethod
    def _temperature_spin(value: float, minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def add_event(self, message: str) -> None:
        if message == "Run started":
            self.events = []
        self.events.append(f"{time.strftime('%H:%M:%S')}  {message}")
        self.events = self.events[-4:]
        self.event_label.setText("   ·   ".join(self.events))

    def refresh(self) -> None:
        snapshot = self.window.engine.snapshot
        running = self.window.engine.active
        self.start_button.setEnabled(not running)
        self.pause_button.setEnabled(running and self.window.engine.state == "Running")
        self.resume_button.setEnabled(running and self.window.engine.state == "Paused")
        self.stop_button.setEnabled(running)
        if not running and self._live_reading is not None and not self.window.config.simulation_mode:
            self.apply_reading(self._live_reading)
        elif running or self.window.engine._last_snapshot is not None:
            self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: dict) -> None:
        zones = snapshot["zones"]
        setpoints = snapshot.get("zone_setpoints", (None, None, None))
        powers = snapshot.get("zone_power", (None, None, None))
        for metric, value, setpoint, power in zip(self.zone_metrics[:3], zones, setpoints, powers):
            metric.set_value(f"{value:.0f} °C")
            parts = []
            if setpoint is not None:
                parts.append(f"SP {setpoint:.0f} °C")
            if power is not None:
                parts.append(f"out {power:.0f} %")
            metric.set_detail(" · ".join(parts))
        self.zone_metrics[3].set_value(f"{snapshot.get('gradient_c', max(zones) - min(zones)):.0f} °C")
        control_phase = snapshot.get("control_phase", "")
        soak = snapshot.get("soak_elapsed_s", 0.0)
        out_of_band = snapshot.get("out_of_band_s", 0.0)
        detail = f"{control_phase} · soak {time.strftime('%H:%M:%S', time.gmtime(soak))}" if control_phase else ""
        if out_of_band:
            detail += f" · {out_of_band:.0f} s out of band"
        self.zone_metrics[3].set_detail(detail)
        comp = snapshot.get("center_comp_c", 0.0)
        targets = snapshot.get("zone_targets")
        if targets:
            self.comp_label.setText(
                "Zone targets now " + " / ".join(f"{t:.0f}" for t in targets) + " °C"
                + (f" (middle-zone compensation {comp:+.1f} °C on Zones 1/3)" if comp else "")
            )
        self.phase_pill.setText(snapshot["phase"])
        elapsed = time.strftime("%H:%M:%S", time.gmtime(snapshot["elapsed_sec"]))
        self.elapsed_label.setText(f"Elapsed {elapsed}")
        self.setpoint_label.setText(
            f"Ramp setpoint {snapshot['output_setpoint_c']:.1f} °C → target {snapshot['target_setpoint_c']:.0f} °C"
        )
        field = snapshot.get("field_enabled", False)
        self.field_pill.setText(
            f"Field ON · {snapshot.get('field_amplitude_uT', 0.0):.0f} µT" if field else "Field OFF"
        )
        self.field_pill.setObjectName("fieldBadge" if field else "fieldBadgeOff")
        self.field_pill.style().unpolish(self.field_pill)
        self.field_pill.style().polish(self.field_pill)
        self.live_field_check.setChecked(field)
        self._set_alarm(snapshot.get("alarm", ""))

    def _set_alarm(self, alarm: str) -> None:
        self.alarm_label.setText(alarm or "No active alarm")
        self.alarm_label.setObjectName("alarmActive" if alarm else "alarmClear")
        self.alarm_label.style().unpolish(self.alarm_label)
        self.alarm_label.style().polish(self.alarm_label)


class DataPage(QWidget):
    """Review recorded runs and export in CSV or legacy table format."""

    COLUMNS = (
        "Timestamp", "Elapsed", "Zone 1", "Zone 2", "Zone 3", "Current",
        "Setpoint", "Target", "Phase", "Alarm",
    )

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        actions = QHBoxLayout()
        description = QLabel("Inspect the latest run or export the complete sample stream.")
        description.setObjectName("muted")
        actions.addWidget(description)
        actions.addStretch()
        actions.addWidget(button("Import legacy table", "secondary", self._import_legacy))
        actions.addWidget(button("Refresh", "secondary", self.refresh))
        actions.addWidget(button("Export CSV", "primary", self._export_csv))
        actions.addWidget(button("Export legacy table", "secondary", self._export_legacy))
        layout.addLayout(actions)
        card = Card("Recorded samples")
        self.data_table = QTableWidget(0, len(self.COLUMNS))
        self.data_table.setHorizontalHeaderLabels(self.COLUMNS)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.data_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.data_table.verticalHeader().setVisible(False)
        self.data_table.horizontalHeader().setStretchLastSection(True)
        card.body.addWidget(self.data_table)
        layout.addWidget(card, 1)

    def refresh(self) -> None:
        run_id = self.window.engine.run_id or self.window.logger.latest_run_id()
        if run_id is None:
            self.data_table.setRowCount(0)
            return
        rows = self.window.logger.get_samples(run_id)
        self.data_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            timestamp, elapsed, z1, z2, z3, current, setpoint, target, phase, alarm, _conn = row
            values = (
                timestamp,
                time.strftime("%H:%M:%S", time.gmtime(elapsed)),
                f"{z1:.2f} °C",
                f"{z2:.2f} °C",
                f"{z3:.2f} °C",
                f"{current:.2f} A" if current is not None else "",
                f"{setpoint:.2f} °C",
                f"{target:.2f} °C",
                phase,
                alarm or "",
            )
            for column, value in enumerate(values):
                self.data_table.setItem(row_index, column, QTableWidgetItem(str(value)))

    def _export_csv(self) -> None:
        run_id = self.window.engine.run_id or self.window.logger.latest_run_id()
        if run_id is None:
            QMessageBox.information(self, "Export", "There is no run to export yet.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Export run", f"asc-run-{run_id}.csv", "CSV files (*.csv)"
        )
        if not target:
            return
        rows = self.window.logger.get_detailed_samples(run_id)
        from asc_oven_control.infrastructure.persistence import export_samples_csv

        export_samples_csv(rows, target)
        self.window.show_status_text(f"Exported run {run_id} as CSV")

    def _export_legacy(self) -> None:
        """Write the latest run in the recovered 2009 table format."""
        run_id = self.window.engine.run_id or self.window.logger.latest_run_id()
        if run_id is None:
            QMessageBox.information(self, "Export", "There is no run to export yet.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Export legacy table", f"asc-run-{run_id}-legacy.txt", "Text files (*.txt)"
        )
        if not target:
            return
        rows = self.window.logger.get_samples(run_id, limit=10_000_000)
        table = LegacyTable(
            date=time.strftime("%m/%d/%Y"),
            time=time.strftime("%I:%M %p"),
            target_c=rows[-1][7] if rows else None,
            field_uT=0.0,
            atmosphere="",
            rows=tuple(
                LegacyRow(
                    time_min=elapsed / 60.0,
                    zone1_c=z1,
                    zone2_c=z2,
                    zone3_c=z3,
                    current_a=current,
                )
                for _, elapsed, z1, z2, z3, current, _sp, _tp, _phase, _alarm, _conn in rows
            ),
        )
        write_legacy_file(target, table)
        self.window.show_status_text(f"Exported run {run_id} in legacy format")

    def _import_legacy(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self, "Import legacy table", "", "Text files (*.txt);;All files (*)"
        )
        if not source:
            return
        try:
            table = parse_legacy_file(source)
        except LegacyTableError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        dialog = LegacyPreviewDialog(table, source, self)
        dialog.exec()


class LegacyPreviewDialog(QDialog):
    """Preview a parsed legacy table before deciding what to do with it."""

    def __init__(self, table, source: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Legacy table — {source}")
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"{table.date} {table.time} · target {table.target_c:g} °C"
            f" · field {table.field_uT:g} µT · {table.atmosphere or 'no atmosphere noted'}"
            f" · {len(table)} samples"
        )
        summary.setObjectName("muted")
        layout.addWidget(summary)
        preview = QTableWidget(len(table), 5)
        preview.setHorizontalHeaderLabels(["Time (min)", "Zone 1 (°C)", "Zone 2 (°C)", "Zone 3 (°C)", "Current (A)"])
        preview.setAlternatingRowColors(True)
        preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        preview.verticalHeader().setVisible(False)
        for row_index, row in enumerate(table.rows):
            values = (
                f"{row.time_min:g}",
                f"{row.zone1_c:g}",
                f"{row.zone2_c:g}",
                f"{row.zone3_c:g}",
                f"{row.current_a:g}" if row.current_a is not None else "",
            )
            for column, value in enumerate(values):
                preview.setItem(row_index, column, QTableWidgetItem(value))
        layout.addWidget(preview, 1)
        close_button = button("Close", "secondary", self.accept)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)


class TuningPage(QWidget):
    """Read, edit and auto-tune the PID set of each zone's Series 96."""

    AUTOTUNE_POLL_MS = 5000

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(18)

        guide = Card(
            "Why tuning matters for the gradient",
            "Each zone's Watlow runs its own PID loop. With a wide proportional band and a "
            "long integral, a zone must sit far below a moving setpoint before it draws "
            "enough power, and zones with different heat loads sit different distances "
            "behind. That difference is the ramp gradient. Tighter, similar loops on all "
            "three zones plus the supervisory gradient control on the Setup page keep the "
            "zones together. Settings read from the oven on 2026-09-29: prop band 47/65/47 °C, "
            "integral 12.5/60/12.5 min/repeat, derivative 0.90/2.25/0.90 min.",
        )
        layout.addWidget(guide)

        pid_card = Card(
            "PID set 1 per zone",
            "SI units (reg 900 = 2): prop band in °C, integral in minutes per repeat, "
            "derivative in minutes. Values are only written when you press Write.",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        for column, heading in enumerate(("", "Prop band", "Integral", "Derivative", "Cycle time", "")):
            label = QLabel(heading)
            label.setObjectName("muted")
            grid.addWidget(label, 0, column)
        self.pid_rows = []
        for index in range(3):
            band = self._spin(1.0, 999.0, 0, " °C")
            integral = self._spin(0.0, 99.99, 2, " min/rep")
            derivative = self._spin(0.0, 9.99, 2, " min")
            cycle = QLabel("--")
            write = button(f"Write Zone {index + 1}", "secondary", lambda _=False, i=index: self._write_pid(i))
            grid.addWidget(QLabel(f"Zone {index + 1}"), index + 1, 0)
            grid.addWidget(band, index + 1, 1)
            grid.addWidget(integral, index + 1, 2)
            grid.addWidget(derivative, index + 1, 3)
            grid.addWidget(cycle, index + 1, 4)
            grid.addWidget(write, index + 1, 5)
            self.pid_rows.append((band, integral, derivative, cycle))
        pid_card.body.addLayout(grid)
        actions = QHBoxLayout()
        actions.addWidget(button("Read from controllers", "primary", self._read_pids))
        actions.addStretch()
        pid_card.body.addLayout(actions)
        layout.addWidget(pid_card)

        tune_card = Card(
            "Auto-tune (all zones together)",
            "Sets all three zones to the tuning temperature and starts the Series 96 "
            "auto-tune on each. The controllers oscillate around the auto-tune set point "
            "(a percentage of the tuning temperature) and store new PID values when done. "
            "Tune all zones together near the working temperature so the coupling between "
            "zones matches a real run. Heater power must be on. The oven stays at the "
            "tuning temperature afterwards until you turn the heaters off.",
        )
        tune_form = QFormLayout()
        self.tune_temp_spin = self._spin(50.0, 800.0, 0, " °C")
        self.tune_temp_spin.setValue(500.0)
        self.tune_percent_spin = QSpinBox()
        self.tune_percent_spin.setRange(50, 150)
        self.tune_percent_spin.setValue(90)
        self.tune_percent_spin.setSuffix(" % of tuning temperature")
        tune_form.addRow("Tuning temperature", self.tune_temp_spin)
        tune_form.addRow("Auto-tune set point", self.tune_percent_spin)
        tune_card.body.addLayout(tune_form)
        tune_actions = QHBoxLayout()
        tune_actions.addWidget(button("Start auto-tune", "primary", self._start_autotune))
        tune_actions.addWidget(button("Cancel auto-tune", "secondary", self._cancel_autotune))
        tune_actions.addWidget(button("All heaters off", "danger", self._heaters_off))
        tune_actions.addStretch()
        tune_card.body.addLayout(tune_actions)
        layout.addWidget(tune_card)

        self.status_label = QLabel("")
        self.status_label.setObjectName("recoveredNote")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        layout.addStretch()

        from PySide6.QtCore import QTimer

        self.tune_timer = QTimer(self)
        self.tune_timer.setInterval(self.AUTOTUNE_POLL_MS)
        self.tune_timer.timeout.connect(self._poll_autotune)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(_scroll_page(body))

    @staticmethod
    def _spin(minimum: float, maximum: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        return spin

    def refresh(self) -> None:
        if self.window.config.simulation_mode:
            self.status_label.setText("Simulation mode: switch to Watlow hardware on the Setup page to tune.")

    # ----------------------------------------------------------- hardware

    def _session(self):
        """Open a short hardware session, or explain why not."""
        from asc_oven_control.services.oven_backend import WatlowOven

        config = self.window.config
        if config.simulation_mode or config.serial.port is None:
            self.status_label.setText("Select and save Watlow hardware on the Setup page first.")
            return None
        if self.window.engine.active:
            self.status_label.setText("A run is active; tuning is available when the oven is idle.")
            return None
        if not self.window.acquire_port():
            self.status_label.setText("The serial port is busy; try again in a moment.")
            return None
        backend = WatlowOven(config)
        try:
            backend.connect()
        except Exception as exc:  # noqa: BLE001
            self.window.release_port()
            self.status_label.setText(f"Cannot open {config.serial.port}: {exc}")
            return None
        return backend

    def _end(self, backend) -> None:
        backend.close()
        self.window.release_port()

    def _read_pids(self) -> None:
        backend = self._session()
        if backend is None:
            return
        try:
            lines = []
            for index, zone in enumerate(backend.zones):
                pid = zone.read_pid()
                band, integral, derivative, cycle = self.pid_rows[index]
                band.setValue(pid.prop_band_c)
                integral.setValue(pid.integral_min)
                derivative.setValue(pid.derivative_min)
                cycle.setText(f"{pid.cycle_time_s:g} s")
                errors = zone.read_errors()
                lines.append(
                    f"Zone {index + 1}: PB {pid.prop_band_c:.0f} °C · Ti {pid.integral_min:.2f} min/rep"
                    f" · Td {pid.derivative_min:.2f} min" + (f" · {', '.join(errors)}" if errors else "")
                )
            self.status_label.setText("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Read failed: {exc}")
        finally:
            self._end(backend)

    def _write_pid(self, index: int) -> None:
        from asc_oven_control.infrastructure.watlow96 import PidSettings

        band, integral, derivative, _cycle = self.pid_rows[index]
        answer = QMessageBox.question(
            self,
            "Write PID",
            f"Write to Zone {index + 1}: prop band {band.value():.0f} °C, integral "
            f"{integral.value():.2f} min/repeat, derivative {derivative.value():.2f} min?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        backend = self._session()
        if backend is None:
            return
        try:
            zone = backend.zones[index]
            current = zone.read_pid()
            zone.write_pid(PidSettings(band.value(), integral.value(), derivative.value(), current.cycle_time_s))
            written = zone.read_pid()
            self.status_label.setText(
                f"Zone {index + 1} PID now PB {written.prop_band_c:.0f} °C · Ti {written.integral_min:.2f}"
                f" · Td {written.derivative_min:.2f} (was PB {current.prop_band_c:.0f} · Ti "
                f"{current.integral_min:.2f} · Td {current.derivative_min:.2f})"
            )
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Write failed: {exc}")
        finally:
            self._end(backend)

    def _start_autotune(self) -> None:
        temperature = round(self.tune_temp_spin.value())
        percent = self.tune_percent_spin.value()
        answer = QMessageBox.question(
            self,
            "Start auto-tune",
            f"Set all three zones to {temperature} °C and auto-tune around "
            f"{temperature * percent / 100:.0f} °C? The oven will heat.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        backend = self._session()
        if backend is None:
            return
        try:
            backend.take_control()  # the oven panel's remote set point would override ours
            for zone in backend.zones:
                zone.write_setpoint(temperature)
            for zone in backend.zones:
                zone.start_autotune(percent)
            self.status_label.setText("Auto-tune running on all zones…")
            self.tune_timer.start()
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Auto-tune start failed: {exc}")
        finally:
            self._end(backend)

    def _poll_autotune(self) -> None:
        backend = self._session()
        if backend is None:
            self.tune_timer.stop()
            return
        try:
            active = []
            parts = []
            for index, zone in enumerate(backend.zones):
                status = zone.read_status()
                running = zone.autotune_active()
                active.append(running)
                parts.append(
                    f"Zone {index + 1}: {status.process_c:.0f} °C, out {status.power_pct or 0:.0f} %, "
                    + ("tuning" if running else "done")
                )
            self.status_label.setText("\n".join(parts))
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Auto-tune poll failed: {exc}")
            active = [True]
        finally:
            self._end(backend)
        if not any(active):
            self.tune_timer.stop()
            self._read_pids()
            self.status_label.setText(self.status_label.text() + "\nAuto-tune complete; new PID values shown above.")

    def _cancel_autotune(self) -> None:
        self.tune_timer.stop()
        backend = self._session()
        if backend is None:
            return
        try:
            for zone in backend.zones:
                zone.cancel_autotune()
            self.status_label.setText("Auto-tune cancelled; controllers keep their previous PID values.")
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Cancel failed: {exc}")
        finally:
            self._end(backend)

    def _heaters_off(self) -> None:
        self.tune_timer.stop()
        backend = self._session()
        if backend is None:
            return
        try:
            for zone in backend.zones:
                if zone.autotune_active():
                    zone.cancel_autotune()
            backend.safe_shutdown()
            self.status_label.setText("All zones set to their lowest set point (heaters off).")
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"Heaters-off failed: {exc}")
        finally:
            self._end(backend)


class InstrumentPage(QWidget):
    """Read-only reference: recovered protocol evidence and configuration."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 8)
        layout.setSpacing(18)

        protocol = Card(
            "Watlow Series 96 over Modbus RTU (verified on hardware)",
            "Verified 2026-09-29 on COM4 (Silicon Labs CP210x USB-UART to RS-485). "
            "See LABVIEW_MIGRATION.md for the evidence record.",
        )
        evidence = QLabel(
            "Three Watlow Series 96 controllers, one per zone, answer as Modbus slaves "
            "1, 2 and 3 at 9600 baud 8N1. Function 0x03 reads holding registers and 0x06 "
            "writes one register; the CRC is Modbus CRC-16 (poly 0xA001, init 0xFFFF), low "
            "byte first, exactly as recovered from Calc CRC-sub.vi.\n\n"
            "Registers used: 0 model (96) · 100 process value · 101 input error · 103 "
            "output ×10 % · 106 alarm 2 status · 300 set point (Change SP.vi: Reg-H 1, "
            "Reg-L 44) · 301 auto/manual · 304/305 auto-tune · 500 prop band · 501 "
            "integral ×100 min/rep · 503 derivative ×100 min · 506 cycle time ×10 s · "
            "602/603 set point range · 606 decimal · 900 PID units (2 = SI) · 901 °C/°F.\n\n"
            "Controller configuration read from the oven: °C, whole degrees, type E "
            "thermocouple, set point range 0–800 °C, internal ramping off."
        )
        evidence.setObjectName("muted")
        evidence.setWordWrap(True)
        protocol.body.addWidget(evidence)
        layout.addWidget(protocol)

        frames = Card(
            "Example frames",
            "Requests as sent on the bus; the first two were captured on the oven.",
        )
        frame_form = QFormLayout()
        frame_form.setSpacing(10)
        self.frame_labels = []
        for name, frame in (
            ("Read Zone 1 process value", read_request(1, 100)),
            ("Read Zone 3 process value", read_request(3, 100)),
            ("Read Zone 2 PID block (7 registers)", read_request(2, 500, 7)),
            ("Write Zone 1 set point 590 °C", write_request(1, 300, 590)),
            ("Write Zone 1 set point 0 °C (heaters off)", write_request(1, 300, 0)),
        ):
            label = QLabel(bytes(frame).hex(" ").upper())
            label.setObjectName("recoveredNote")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            frame_form.addRow(name, label)
        frames.body.addLayout(frame_form)
        layout.addWidget(frames)

        config = Card("Runtime configuration")
        self.config_labels: list[tuple[QLabel, QLabel]] = []
        config_form = QFormLayout()
        config_form.setSpacing(10)
        for label_text in ("Simulation mode", "Poll interval", "Serial port", "Baud rate", "Zone addresses", "Data dir"):
            label = QLabel(label_text)
            label.setObjectName("muted")
            value = QLabel("")
            config_form.addRow(label, value)
            self.config_labels.append((label, value))
        config.body.addLayout(config_form)
        layout.addWidget(config)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(_scroll_page(body))

    def refresh(self) -> None:
        config = self.window.config
        serial = config.serial
        values = (
            "Yes" if config.simulation_mode else "No (Watlow hardware)",
            f"{config.poll_seconds:g} s (hardware minimum 2 s)",
            serial.port or "not configured",
            str(serial.baudrate),
            ", ".join(str(a) for a in config.zone_addresses),
            config.data_dir or "platform default",
        )
        for (_, value_label), text in zip(self.config_labels, values):
            value_label.setText(text)
