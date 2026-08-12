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
from asc_oven_control.infrastructure.legacy_table import (
    LegacyRow,
    LegacyTable,
    LegacyTableError,
    parse_legacy_file,
    write_legacy_file,
)
from asc_oven_control.infrastructure.watlow_protocol import WatlowCommands
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
            "The LabVIEW program talked to the Watlow controller over NI-VISA serial "
            "(9600 baud, 8N1, no flow control) with a CRC-framed register protocol. "
            "The exact register map is not yet verified, so this build runs in "
            "simulation only and never opens a physical port.",
        )
        simulation_note = QLabel("SIMULATION SAFE · No physical ports opened")
        simulation_note.setObjectName("recoveredNote")
        connection.body.addWidget(simulation_note)
        serial_form = QFormLayout()
        serial_form.setSpacing(10)
        serial_form.addRow("Protocol", QLabel("Watlow CRC register (recovered)"))
        serial_form.addRow("Framing", QLabel("9600 baud · 8 data bits · no parity · 1 stop bit"))
        serial_form.addRow("Mode", QLabel("Oven simulation (deterministic 3-zone model)"))
        connection.body.addLayout(serial_form)
        grid.addWidget(connection, 0, 0)

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
        self.alarm_high_spin = self._temperature_spin(1200.0, -100.0, 1600.0, " °C")
        self.alarm_low_spin = self._temperature_spin(10.0, -100.0, 1600.0, " °C")
        for label, widget in (
            ("Target", self.target_spin),
            ("Ramp rate", self.ramp_spin),
            ("Soak time", self.soak_spin),
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

        pid = Card(
            "Watlow PID (reference)",
            "Recovered from PID_globals.vi (proportional band, integral, derivative). "
            "Shown for reference only — calibrated values are unverified.",
        )
        pid_form = QFormLayout()
        pid_form.setSpacing(12)
        self.prop_band_spin = self._temperature_spin(100.0, 0.0, 10000.0, "")
        self.integral_spin = self._temperature_spin(10.0, 0.0, 10000.0, " s")
        self.derivative_spin = self._temperature_spin(0.0, 0.0, 10000.0, " s")
        for label, widget in (
            ("Prop band", self.prop_band_spin),
            ("Integral", self.integral_spin),
            ("Derivative", self.derivative_spin),
        ):
            pid_form.addRow(label, widget)
        pid.body.addLayout(pid_form)
        grid.addWidget(pid, 3, 0)

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
            )
        except DomainValidationError as exc:
            raise ValueError(str(exc)) from exc


class LiveControlPage(QWidget):
    """Monitor and guide the oven: metrics, trend, commands, manual adjust."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        metrics = QGridLayout()
        metrics.setSpacing(14)
        self.zone_metrics = [
            MetricCard("Zone 1", "-- °C", "#56D6C9"),
            MetricCard("Zone 2", "-- °C", "#F4A261"),
            MetricCard("Zone 3", "-- °C", "#5FA8D3"),
            MetricCard("Current", "-- A", "#8CA4AD"),
        ]
        for column, metric in enumerate(self.zone_metrics):
            metrics.addWidget(metric, 0, column)
        layout.addLayout(metrics)

        status_row = QHBoxLayout()
        self.phase_pill = pill("Idle", "phaseChip")
        self.field_pill = pill("Field OFF", "fieldBadgeOff")
        self.elapsed_label = QLabel("Elapsed 00:00:00")
        self.elapsed_label.setObjectName("muted")
        self.setpoint_label = QLabel("Setpoint -- °C")
        self.setpoint_label.setObjectName("muted")
        status_row.addWidget(self.phase_pill)
        status_row.addWidget(self.field_pill)
        status_row.addWidget(self.elapsed_label)
        status_row.addWidget(self.setpoint_label)
        status_row.addStretch()
        self.alarm_label = QLabel("No active alarm")
        self.alarm_label.setObjectName("alarmClear")
        status_row.addWidget(self.alarm_label)
        layout.addLayout(status_row)

        chart_card = Card("Temperature trend", "Three zones, commanded setpoint, and heater current")
        self.chart = window.chart
        chart_card.body.addWidget(self.chart, 1)
        layout.addWidget(chart_card, 1)

        command_row = QHBoxLayout()
        self.start_button = button("Start run", "primary", window.start_run)
        self.pause_button = button("Pause", "secondary", window.pause_run)
        self.resume_button = button("Resume", "secondary", window.resume_run)
        self.stop_button = button("Stop", "danger", window.stop_run)
        for widget in (self.start_button, self.pause_button, self.resume_button, self.stop_button):
            command_row.addWidget(widget)
        command_row.addStretch()
        layout.addLayout(command_row)

        manual = Card(
            "Manual adjustment",
            "Apply a new target or ramp rate to the active profile; the field can be "
            "toggled live with the amplitude shown on the badge.",
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
        layout.addWidget(manual)

    @staticmethod
    def _temperature_spin(value: float, minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def refresh(self) -> None:
        snapshot = self.window.engine.snapshot
        running = self.window.engine.active
        self.start_button.setEnabled(not running)
        self.pause_button.setEnabled(running and self.window.engine.state == "Running")
        self.resume_button.setEnabled(running and self.window.engine.state == "Paused")
        self.stop_button.setEnabled(running)
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: dict) -> None:
        zones = snapshot["zones"]
        for metric, value in zip(self.zone_metrics[:3], zones):
            metric.set_value(f"{value:.1f} °C")
        current = snapshot["current_a"]
        self.zone_metrics[3].set_value(f"{current:.2f} A")
        self.phase_pill.setText(snapshot["phase"])
        elapsed = time.strftime("%H:%M:%S", time.gmtime(snapshot["elapsed_sec"]))
        self.elapsed_label.setText(f"Elapsed {elapsed}")
        self.setpoint_label.setText(f"Setpoint {snapshot['output_setpoint_c']:.1f} °C")
        field = snapshot.get("field_enabled", False)
        self.field_pill.setText(
            f"Field ON · {snapshot.get('field_amplitude_uT', 0.0):.0f} µT" if field else "Field OFF"
        )
        self.field_pill.setObjectName("fieldBadge" if field else "fieldBadgeOff")
        self.field_pill.style().unpolish(self.field_pill)
        self.field_pill.style().polish(self.field_pill)
        self.live_field_check.setChecked(field)
        alarm = snapshot.get("alarm", "")
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
        rows = self.window.logger.get_samples(run_id, limit=10_000_000)
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
            "Recovered Watlow protocol",
            "What the LabVIEW block diagrams establish, and what still needs hardware "
            "verification. See LABVIEW_MIGRATION.md for the full record.",
        )
        evidence = QLabel(
            "Frame parts recovered from Calc CRC-sub.vi: Command, Adress, Reg H, Reg L, "
            "Data H, Data L, Number of Byte, CRC (H byte / L byte). The CRC is LSB-first "
            "with a right-shift register (D0–D15), i.e. the Modbus RTU CRC-16 "
            "(poly 0xA001, init 0xFFFF).\n\n"
            "Watlow Read.vi and Watlow Write.vi send these frames over NI-VISA serial "
            "configured at 9600 baud, 8 data bits, no parity, one stop bit, no flow "
            "control. Change SP.vi writes the Set Point parameter, Adjust_ramp_rate.vi "
            "the ramp rate, and stop_program.vi stops the run.\n\n"
            "NOT recovered: controller slave address, register addresses, word order, "
            "temperature scaling, and response validation. Until a commissioning "
            "procedure supplies and independently verifies these, the application "
            "stays in simulation mode and the protocol builder below is never opened."
        )
        evidence.setObjectName("muted")
        evidence.setWordWrap(True)
        protocol.body.addWidget(evidence)
        layout.addWidget(protocol)

        frames = Card(
            "Frame builder (reference only)",
            "Example frames produced by the recovered builders — nothing here is sent.",
        )
        frame_form = QFormLayout()
        frame_form.setSpacing(10)
        self.frame_labels = []
        for name, frame in (
            ("Read (addr 1, reg 0x00A8, 2 bytes)", WatlowCommands.read(1, 0x00A8, 2)),
            ("Write (addr 1, reg 0x00A8, 590)", WatlowCommands.write(1, 0x00A8, 590)),
            ("Set point (addr 1, reg 0x00A8, 590)", WatlowCommands.setpoint(1, 0x00A8, 590)),
            ("Ramp rate (addr 1, reg 0x00A9, 20)", WatlowCommands.ramp_rate(1, 0x00A9, 20)),
            ("Stop (addr 1, reg 0x00AA)", WatlowCommands.stop(1, 0x00AA)),
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
        for label_text in ("Simulation mode", "Poll interval", "Serial port", "Baud rate", "Data dir"):
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
            "Yes (hardware locked)" if config.simulation_mode else "No",
            f"{config.poll_seconds:g} s",
            serial.port or "not configured (locked)",
            str(serial.baudrate),
            config.data_dir or "platform default",
        )
        for (_, value_label), text in zip(self.config_labels, values):
            value_label.setText(text)
