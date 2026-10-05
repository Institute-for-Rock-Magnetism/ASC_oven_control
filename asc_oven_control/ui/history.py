"""Run history: browse and view every recorded heating.

The list is built from the run CSV logs (``run_logs/run-*.csv`` on the lab
PC), so it shows every run that was ever recorded, including runs from
other installs or before a database move; the database adds the outcome
(complete / aborted / interrupted ...) where it knows the run. Selecting a
run plots its whole time-temperature record, cool-down included, and
summarizes the ramp, the hold and the cooling.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from asc_oven_control.ui.widgets import Card, button

RUN_FILE = re.compile(r"run-(\d+)-(\d{8})-(\d{4})\.csv$")


@dataclass
class RunRecord:
    path: Path
    run_id: int
    started: str = ""
    header: dict = field(default_factory=dict)
    operator: str = ""
    batch: str = ""
    sample: str = ""
    target_c: float | None = None
    soak_s: float | None = None
    duration_min: float = 0.0
    rows: int = 0
    status: str = ""


def _header_fields(lines: list[str]) -> dict:
    fields = {}
    for line in lines:
        if not line.startswith("#"):
            break
        for key, value in re.findall(r"(\w+)=(\([^)]*\)|\S+)", line):
            fields[key] = value
        if line.startswith("# ASC oven run"):
            match = re.search(r"started (\S+ \S+)", line)
            if match:
                fields["started"] = match.group(1)
    return fields


def _number(text: str | None) -> float | None:
    if not text:
        return None
    match = re.match(r"-?\d+(\.\d+)?", text)
    return float(match.group(0)) if match else None


def scan_runs(directory: Path, statuses: dict[int, str]) -> list[RunRecord]:
    records = []
    if not directory or not Path(directory).is_dir():
        return records
    for path in Path(directory).glob("run-*.csv"):
        match = RUN_FILE.search(path.name)
        if not match:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        header = _header_fields(lines[:8])
        data = [line for line in lines if not line.startswith("#")]
        record = RunRecord(path=path, run_id=int(match.group(1)), header=header)
        record.started = header.get("started", f"{match.group(2)} {match.group(3)}")
        record.operator = header.get("operator", "")
        record.batch = header.get("batch", "")
        record.sample = header.get("sample", "")
        record.target_c = _number(header.get("target"))
        record.soak_s = _number(header.get("soak"))
        record.rows = max(len(data) - 1, 0)
        if record.rows:
            last = data[-1].split(",")
            try:
                record.duration_min = float(last[1])
            except (IndexError, ValueError):
                pass
        record.status = statuses.get(record.run_id, "")
        records.append(record)
    records.sort(key=lambda r: r.run_id, reverse=True)
    return records


def load_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return list(csv.DictReader(line for line in handle if not line.startswith("#")))


def summarize(rows: list[dict], target: float | None) -> str:
    """Plain-language summary of one run's ramp, hold and cool-down."""

    def f(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    if not rows:
        return "No samples recorded."
    lines = []
    hold = [r for r in rows if r.get("control_phase") == "Soaking"]
    cooling = [r for r in rows if r.get("phase") == "Cooling"]
    ramp = [r for r in rows if r.get("phase") in ("Ramping",)]
    if ramp:
        grads = [f(r["gradient_c"]) for r in ramp]
        lines.append(f"Ramp: {f(ramp[-1]['elapsed_min']):.1f} min, largest zone gradient {max(grads):.0f} °C.")
    if hold:
        start, end = f(hold[0]["elapsed_min"]), f(hold[-1]["elapsed_min"])
        ranges = []
        for i in (1, 2, 3):
            values = [f(r[f"zone{i}_c"]) for r in hold]
            ranges.append(f"Z{i} {min(values):.0f}–{max(values):.0f}")
        grads = [f(r["gradient_c"]) for r in hold]
        text = f"Hold: from {start:.1f} min for {end - start:.1f} min; " + ", ".join(ranges) + " °C"
        if target is not None:
            peak = max(max(f(r[f"zone{i}_c"]) for r in hold) for i in (1, 2, 3))
            text += f"; peak {peak - target:+.0f} °C vs target"
        text += f"; gradient up to {max(grads):.0f} °C."
        lines.append(text)
    else:
        lines.append("Hold: not reached.")
    if cooling:
        first, last = cooling[0], cooling[-1]
        lines.append(
            f"Cool-down recorded for {f(last['elapsed_min']) - f(first['elapsed_min']):.0f} min: "
            f"{f(first['zone1_c']):.0f}/{f(first['zone2_c']):.0f}/{f(first['zone3_c']):.0f} → "
            f"{f(last['zone1_c']):.0f}/{f(last['zone2_c']):.0f}/{f(last['zone3_c']):.0f} °C."
        )
    end = rows[-1]
    lines.append(f"Last sample at {f(end['elapsed_min']):.1f} min, phase {end.get('phase', '')}.")
    return "\n".join(lines)


class RunHistoryPage(QWidget):
    """List of all recorded runs with a full plot and summary of the selected one."""

    COLUMNS = ("Run", "Started", "Target", "Hold", "Operator / batch", "Outcome", "Length")

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.records: list[RunRecord] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        actions = QHBoxLayout()
        self.folder_label = QLabel("")
        self.folder_label.setObjectName("muted")
        actions.addWidget(self.folder_label)
        actions.addStretch()
        actions.addWidget(button("Refresh", "secondary", self.refresh))
        actions.addWidget(button("Open logs folder", "secondary", self._open_folder))
        actions.addWidget(button("Export legacy table", "secondary", self._export_legacy))
        actions.addWidget(button("Import legacy table", "quiet", self._import_legacy))
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        list_card = Card("Recorded runs")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_selected)
        list_card.body.addWidget(self.table)
        splitter.addWidget(list_card)

        view_card = Card()
        self.title = QLabel("Select a run")
        self.title.setObjectName("cardTitle")
        view_card.body.addWidget(self.title)
        frame = QFrame()
        frame.setObjectName("chartFrame")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(10, 10, 10, 10)
        from asc_oven_control.ui.live_plot import create_trend_chart

        self.chart = create_trend_chart()
        frame_layout.addWidget(self.chart)
        view_card.body.addWidget(frame, 1)
        self.summary = QLabel("")
        self.summary.setObjectName("recoveredNote")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        view_card.body.addWidget(self.summary)
        splitter.addWidget(view_card)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([520, 900])
        layout.addWidget(splitter, 1)

    # ----------------------------------------------------------------- data

    def _statuses(self) -> dict[int, str]:
        try:
            with self.window.logger._lock:  # noqa: SLF001 - read-only query
                return dict(self.window.logger.conn.execute("SELECT id, status FROM runs").fetchall())
        except Exception:  # noqa: BLE001
            return {}

    def refresh(self) -> None:
        directory = self.window.runs_dir()
        self.folder_label.setText(f"Logs: {directory}" if directory else "No logs folder configured")
        selected = self._selected()
        self.records = scan_runs(directory, self._statuses()) if directory else []
        self.table.setRowCount(len(self.records))
        for row, record in enumerate(self.records):
            target = f"{record.target_c:g} °C" if record.target_c is not None else ""
            hold = f"{record.soak_s / 60:g} min" if record.soak_s else ""
            who = " / ".join(part for part in (record.operator, record.batch, record.sample) if part)
            values = (str(record.run_id), record.started, target, hold, who, record.status or "—",
                      f"{record.duration_min:.0f} min")
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        if self.records:
            index = next((i for i, r in enumerate(self.records) if selected and r.run_id == selected.run_id), 0)
            self.table.selectRow(index)

    def _selected(self) -> RunRecord | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or rows[0].row() >= len(self.records):
            return None
        return self.records[rows[0].row()]

    def _show_selected(self) -> None:
        record = self._selected()
        if record is None:
            return
        try:
            rows = load_rows(record.path)
        except OSError as exc:
            self.summary.setText(f"Cannot read {record.path.name}: {exc}")
            return

        def f(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return float("nan")

        minutes = [f(r["elapsed_min"]) for r in rows]
        zones = [[f(r[f"zone{i}_c"]) for r in rows] for i in (1, 2, 3)]
        setpoints = [f(r["ramp_setpoint_c"]) for r in rows]
        band = _number(record.header.get("soak_band"))
        self.chart.load_series(minutes, zones, setpoints, record.target_c, band)
        target = f"{record.target_c:g} °C" if record.target_c is not None else "?"
        self.title.setText(f"Run {record.run_id} · {record.started} · {target}"
                           + (f" · {record.batch}" if record.batch else ""))
        settings = ", ".join(
            f"{key}={record.header[key]}"
            for key in ("ramp", "soak", "hold_band", "approach_band", "trims", "max_run_time")
            if key in record.header
        )
        self.summary.setText(summarize(rows, record.target_c) + (f"\nSettings: {settings}" if settings else "")
                             + f"\nFile: {record.path}")

    # -------------------------------------------------------------- actions

    def _open_folder(self) -> None:
        directory = self.window.runs_dir()
        if directory and Path(directory).is_dir():
            os.startfile(str(directory))  # noqa: S606 - Windows lab PC

    def _export_legacy(self) -> None:
        from asc_oven_control.infrastructure.legacy_table import LegacyRow, LegacyTable, write_legacy_file

        record = self._selected()
        if record is None:
            QMessageBox.information(self, "Export", "Select a run first.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Export legacy table", f"asc-run-{record.run_id}-legacy.txt", "Text files (*.txt)"
        )
        if not target:
            return
        rows = load_rows(record.path)
        date, _, clock = record.started.partition(" ")
        table = LegacyTable(
            date=date,
            time=clock,
            target_c=record.target_c,
            field_uT=0.0,
            atmosphere=record.header.get("atmosphere", ""),
            rows=tuple(
                LegacyRow(
                    time_min=float(r["elapsed_min"]),
                    zone1_c=float(r["zone1_c"]),
                    zone2_c=float(r["zone2_c"]),
                    zone3_c=float(r["zone3_c"]),
                    current_a=None,
                )
                for r in rows
            ),
        )
        write_legacy_file(target, table)
        self.window.show_status_text(f"Exported run {record.run_id} in legacy format")

    def _import_legacy(self) -> None:
        from asc_oven_control.infrastructure.legacy_table import LegacyTableError, parse_legacy_file
        from asc_oven_control.ui.pages import LegacyPreviewDialog

        source, _ = QFileDialog.getOpenFileName(self, "Import legacy table", "", "Text files (*.txt);;All files (*)")
        if not source:
            return
        try:
            table = parse_legacy_file(source)
        except LegacyTableError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        LegacyPreviewDialog(table, source, self).exec()
