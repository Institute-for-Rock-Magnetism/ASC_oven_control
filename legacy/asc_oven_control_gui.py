from __future__ import annotations

import csv
import math
import queue
import random
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCloseEvent, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = APP_DIR / "asc_oven_runs.db"


class CommunicationError(RuntimeError):
    pass


def calculate_crc16(data: bytes, poly: int = 0xA001, init: int = 0xFFFF) -> int:
    """Return a Modbus-style CRC-16 retained for the future Watlow adapter."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    return crc & 0xFFFF


@dataclass(frozen=True)
class RunProfile:
    operator: str
    batch_id: str
    sample_id: str
    user_name: str
    atmosphere: bool
    target_setpoint_c: float
    ramp_rate_c_per_min: float
    soak_time_sec: float
    alarm_high_c: float
    alarm_low_c: float
    notes: str = ""


@dataclass(frozen=True)
class SamplePoint:
    timestamp: float
    elapsed_sec: float
    current_temp_c: float
    output_setpoint_c: float
    target_setpoint_c: float
    phase: str
    alarm: str
    connected: bool


class RunLogger:
    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                operator TEXT, batch_id TEXT, sample_id TEXT, user_name TEXT,
                atmosphere INTEGER, target_setpoint_c REAL,
                ramp_rate_c_per_min REAL, soak_time_sec REAL,
                alarm_high_c REAL, alarm_low_c REAL, notes TEXT,
                stopped_at TEXT, status TEXT
            );
            CREATE TABLE IF NOT EXISTS samples(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL, timestamp TEXT NOT NULL,
                elapsed_sec REAL NOT NULL, current_temp_c REAL NOT NULL,
                output_setpoint_c REAL NOT NULL, target_setpoint_c REAL NOT NULL,
                phase TEXT NOT NULL, alarm TEXT, connected INTEGER NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            """
        )
        self.conn.commit()

    def start_run(self, profile: RunProfile) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO runs (
                    started_at, operator, batch_id, sample_id, user_name,
                    atmosphere, target_setpoint_c, ramp_rate_c_per_min,
                    soak_time_sec, alarm_high_c, alarm_low_c, notes, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    profile.operator,
                    profile.batch_id,
                    profile.sample_id,
                    profile.user_name,
                    int(profile.atmosphere),
                    profile.target_setpoint_c,
                    profile.ramp_rate_c_per_min,
                    profile.soak_time_sec,
                    profile.alarm_high_c,
                    profile.alarm_low_c,
                    profile.notes,
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str = "stopped") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET stopped_at = ?, status = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), status, run_id),
            )
            self.conn.commit()

    def log_sample(self, run_id: int, sample: SamplePoint) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO samples (
                    run_id, timestamp, elapsed_sec, current_temp_c,
                    output_setpoint_c, target_setpoint_c, phase, alarm, connected
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    datetime.fromtimestamp(sample.timestamp).isoformat(timespec="seconds"),
                    sample.elapsed_sec,
                    sample.current_temp_c,
                    sample.output_setpoint_c,
                    sample.target_setpoint_c,
                    sample.phase,
                    sample.alarm,
                    int(sample.connected),
                ),
            )
            self.conn.commit()

    def latest_run_id(self) -> Optional[int]:
        with self._lock:
            row = self.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None

    def get_samples(self, run_id: int, limit: int = 1000) -> list[tuple]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT timestamp, elapsed_sec, current_temp_c,
                       output_setpoint_c, target_setpoint_c, phase, alarm, connected
                FROM samples WHERE run_id = ? ORDER BY id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return rows[::-1]

    def export_run_csv(self, run_id: int, target: str) -> None:
        rows = self.get_samples(run_id, limit=10_000_000)
        with open(target, "w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(
                [
                    "timestamp",
                    "elapsed_sec",
                    "current_temp_c",
                    "output_setpoint_c",
                    "target_setpoint_c",
                    "phase",
                    "alarm",
                    "connected",
                ]
            )
            writer.writerows(rows)

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class BaseTransport:
    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def read_state(self) -> tuple[float, str]:
        raise NotImplementedError

    def write_setpoint(self, setpoint_c: float) -> None:
        raise NotImplementedError


class MockTransport(BaseTransport):
    def __init__(self, initial_temp: float = 25.0) -> None:
        self.temperature = initial_temp
        self.setpoint = initial_temp
        self.connected = False
        self.last_update = time.monotonic()

    def connect(self) -> None:
        self.connected = True
        self.last_update = time.monotonic()

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def read_state(self) -> tuple[float, str]:
        if not self.connected:
            raise CommunicationError("Simulation is disconnected")
        now = time.monotonic()
        dt = max(now - self.last_update, 0.0)
        self.last_update = now
        self.temperature += (self.setpoint - self.temperature) * (1 - math.exp(-0.32 * dt))
        self.temperature += random.uniform(-0.06, 0.06)
        return self.temperature, ""

    def write_setpoint(self, setpoint_c: float) -> None:
        if not self.connected:
            raise CommunicationError("Simulation is disconnected")
        self.setpoint = float(setpoint_c)


class AsciiSerialTransport(BaseTransport):
    """Advanced fallback only; it is not yet the LabVIEW/Watlow protocol."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        read_command: str,
        setpoint_command: str,
        timeout: float = 1.0,
    ) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for a live serial connection") from exc
        self.serial_module = serial
        self.port = port
        self.baudrate = baudrate
        self.read_command = self._unescape(read_command)
        self.setpoint_command = self._unescape(setpoint_command)
        self.timeout = timeout
        self.connection = None

    @staticmethod
    def _unescape(value: str) -> bytes:
        return value.replace("\\r", "\r").replace("\\n", "\n").encode("ascii")

    def connect(self) -> None:
        self.connection = self.serial_module.Serial(
            self.port,
            baudrate=self.baudrate,
            bytesize=self.serial_module.EIGHTBITS,
            parity=self.serial_module.PARITY_NONE,
            stopbits=self.serial_module.STOPBITS_ONE,
            timeout=self.timeout,
        )

    def disconnect(self) -> None:
        if self.connection and self.connection.is_open:
            self.connection.close()
        self.connection = None

    def is_connected(self) -> bool:
        return bool(self.connection and self.connection.is_open)

    def read_state(self) -> tuple[float, str]:
        if not self.is_connected():
            raise CommunicationError("Serial transport is disconnected")
        self.connection.reset_input_buffer()
        self.connection.write(self.read_command)
        response = self.connection.readline()
        values = re.findall(rb"-?\d+(?:\.\d+)?", response)
        if not values:
            raise CommunicationError(f"No temperature found in response {response!r}")
        return float(values[0]), ""

    def write_setpoint(self, setpoint_c: float) -> None:
        if not self.is_connected():
            raise CommunicationError("Serial transport is disconnected")
        command = self.setpoint_command.decode("ascii").format(setpoint=setpoint_c).encode("ascii")
        self.connection.write(command)
        self.connection.flush()


class OvenController:
    POLL_SECONDS = 0.5

    def __init__(self, transport: BaseTransport, events: queue.Queue, logger: RunLogger) -> None:
        self.transport = transport
        self.events = events
        self.logger = logger
        self.profile: Optional[RunProfile] = None
        self.run_id: Optional[int] = None
        self.running = False
        self.paused = False
        self.phase = "Idle"
        self.current_temp = 25.0
        self.output_setpoint = 25.0
        self.started_at = 0.0
        self.soak_started_at: Optional[float] = None
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="oven-controller", daemon=True)
        self._worker.start()

    def replace_transport(self, transport: BaseTransport) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("Stop the active run before changing the connection")
            try:
                self.transport.disconnect()
            except Exception:
                pass
            self.transport = transport

    def start(self, profile: RunProfile) -> None:
        with self._lock:
            if not self.transport.is_connected():
                raise CommunicationError("Connect to a transport before starting")
            if self.running:
                raise RuntimeError("A run is already active")
            self.profile = profile
            self.run_id = self.logger.start_run(profile)
            self.running = True
            self.paused = False
            self.phase = "Ramping"
            self.started_at = time.monotonic()
            self.soak_started_at = None
            self.output_setpoint = self.current_temp

    def pause(self) -> None:
        with self._lock:
            if self.running:
                self.paused = True
                self.phase = "Paused"

    def resume(self) -> None:
        with self._lock:
            if self.running:
                self.paused = False
                self.phase = "Ramping" if self.soak_started_at is None else "Soaking"

    def stop(self, status: str = "stopped") -> None:
        with self._lock:
            if self.run_id is not None:
                self.logger.finish_run(self.run_id, status)
            self.running = False
            self.paused = False
            self.phase = "Idle"
            self.run_id = None
            self.soak_started_at = None

    def set_manual_target(self, target: float) -> None:
        with self._lock:
            if self.profile is None:
                self.transport.write_setpoint(target)
                self.output_setpoint = target
            else:
                self.profile = RunProfile(
                    **{**self.profile.__dict__, "target_setpoint_c": float(target)}
                )

    def set_ramp_rate(self, rate: float) -> None:
        with self._lock:
            if self.profile is not None:
                self.profile = RunProfile(
                    **{**self.profile.__dict__, "ramp_rate_c_per_min": max(0.0, float(rate))}
                )

    def shutdown(self) -> None:
        self._shutdown.set()
        self._worker.join(timeout=2.0)
        try:
            self.transport.disconnect()
        except Exception:
            pass

    @staticmethod
    def _profile_alarm(profile: Optional[RunProfile], temperature: float) -> str:
        if profile is None:
            return ""
        if temperature >= profile.alarm_high_c:
            return "High temperature"
        if temperature <= profile.alarm_low_c:
            return "Low temperature"
        return ""

    def _advance_run(self, now: float) -> None:
        if not self.running or self.paused or self.profile is None:
            return
        target = self.profile.target_setpoint_c
        step = self.profile.ramp_rate_c_per_min / 60.0 * self.POLL_SECONDS
        difference = target - self.output_setpoint
        self.output_setpoint += max(-step, min(step, difference)) if step > 0 else difference
        self.transport.write_setpoint(self.output_setpoint)
        if abs(target - self.output_setpoint) <= 0.05:
            if self.soak_started_at is None:
                self.soak_started_at = now
            self.phase = "Soaking"
            if now - self.soak_started_at >= self.profile.soak_time_sec:
                self.phase = "Complete"
                self.stop(status="complete")

    def _emit_snapshot(self, now: float, alarm: str, error: str = "") -> None:
        profile = self.profile
        elapsed = max(now - self.started_at, 0.0) if self.running else 0.0
        target = profile.target_setpoint_c if profile else self.output_setpoint
        combined_alarm = "; ".join(filter(None, (alarm, self._profile_alarm(profile, self.current_temp))))
        self.events.put(
            {
                "timestamp": time.time(),
                "elapsed_sec": elapsed,
                "current_temp_c": self.current_temp,
                "output_setpoint_c": self.output_setpoint,
                "target_setpoint_c": target,
                "phase": self.phase,
                "alarm": combined_alarm,
                "error": error,
                "connected": self.transport.is_connected(),
                "running": self.running,
                "paused": self.paused,
            }
        )

    def _loop(self) -> None:
        while not self._shutdown.wait(self.POLL_SECONDS):
            now = time.monotonic()
            alarm = ""
            error = ""
            with self._lock:
                try:
                    if self.transport.is_connected():
                        self.current_temp, alarm = self.transport.read_state()
                        self._advance_run(now)
                except Exception as exc:
                    error = str(exc)
                if self.running and self.run_id is not None and self.profile is not None:
                    sample = SamplePoint(
                        timestamp=time.time(),
                        elapsed_sec=max(now - self.started_at, 0.0),
                        current_temp_c=self.current_temp,
                        output_setpoint_c=self.output_setpoint,
                        target_setpoint_c=self.profile.target_setpoint_c,
                        phase=self.phase,
                        alarm=alarm or error,
                        connected=self.transport.is_connected(),
                    )
                    self.logger.log_sample(self.run_id, sample)
                self._emit_snapshot(now, alarm, error)


class Card(QFrame):
    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(22, 20, 22, 22)
        self.body.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        self.body.addWidget(heading)
        if subtitle:
            detail = QLabel(subtitle)
            detail.setObjectName("muted")
            detail.setWordWrap(True)
            self.body.addWidget(detail)


class MetricCard(QFrame):
    def __init__(self, caption: str, value: str, accent: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        caption_label = QLabel(caption.upper())
        caption_label.setObjectName("metricCaption")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        bar = QFrame()
        bar.setFixedHeight(4)
        bar.setStyleSheet(f"background: {accent}; border-radius: 2px;")
        layout.addWidget(caption_label)
        layout.addWidget(self.value_label)
        layout.addStretch()
        layout.addWidget(bar)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class TrendChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(330)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.points: Deque[tuple[float, float, float]] = deque(maxlen=600)

    def append_point(self, timestamp: float, temperature: float, setpoint: float) -> None:
        self.points.append((timestamp, temperature, setpoint))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#102A36"))
        area = QRectF(56, 24, max(self.width() - 82, 1), max(self.height() - 68, 1))
        painter.setPen(QPen(QColor("#294753"), 1))
        for index in range(5):
            y = area.top() + area.height() * index / 4
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        if len(self.points) < 2:
            painter.setPen(QColor("#8CA4AD"))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Trend appears after connection")
            return
        times = [point[0] for point in self.points]
        values = [point[1] for point in self.points] + [point[2] for point in self.points]
        low, high = min(values), max(values)
        margin = max((high - low) * 0.12, 2.0)
        low -= margin
        high += margin

        def map_point(timestamp: float, value: float) -> QPointF:
            x = area.left() + (timestamp - times[0]) / max(times[-1] - times[0], 1.0) * area.width()
            y = area.bottom() - (value - low) / max(high - low, 1.0) * area.height()
            return QPointF(x, y)

        for color, value_index in (("#56D6C9", 1), ("#F4A261", 2)):
            path = QPainterPath(map_point(self.points[0][0], self.points[0][value_index]))
            for point in list(self.points)[1:]:
                path.lineTo(map_point(point[0], point[value_index]))
            painter.setPen(QPen(QColor(color), 2.5))
            painter.drawPath(path)
        painter.setPen(QColor("#B9CAD0"))
        painter.setFont(QFont("Avenir Next", 9))
        painter.drawText(QRectF(8, area.top() - 8, 44, 20), Qt.AlignmentFlag.AlignRight, f"{high:.0f}")
        painter.drawText(QRectF(8, area.bottom() - 10, 44, 20), Qt.AlignmentFlag.AlignRight, f"{low:.0f}")
        painter.setPen(QColor("#56D6C9"))
        painter.drawText(area.left(), self.height() - 16, "● Temperature")
        painter.setPen(QColor("#F4A261"))
        painter.drawText(area.left() + 118, self.height() - 16, "● Setpoint")


class OvenMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ASC Oven Control")
        self.resize(1280, 820)
        self.setMinimumSize(1040, 700)
        self.events: queue.Queue = queue.Queue()
        self.logger = RunLogger()
        self.transport: BaseTransport = MockTransport()
        self.controller = OvenController(self.transport, self.events, self.logger)
        self.nav_buttons: list[QPushButton] = []
        self.last_error = ""
        self._build_ui()
        self._apply_styles()
        self._toggle_serial_fields(True)
        self._set_page(0)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_events)
        self.poll_timer.start(150)

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
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_setup_page())
        self.pages.addWidget(self._build_control_page())
        self.pages.addWidget(self._build_data_page())
        content_layout.addWidget(self.pages, 1)
        shell.addWidget(content, 1)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(20, 26, 20, 22)
        logo = QLabel("ASC")
        logo.setObjectName("logo")
        product = QLabel("OVEN CONTROL")
        product.setObjectName("product")
        layout.addWidget(logo)
        layout.addWidget(product)
        layout.addSpacing(36)
        for index, label in enumerate(("01   Setup", "02   Live control", "03   Run data")):
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, page=index: self._set_page(page))
            self.nav_buttons.append(button)
            layout.addWidget(button)
        layout.addStretch()
        protocol = QLabel("LABVIEW PROTOCOL\nMAPPING PENDING")
        protocol.setObjectName("protocolFlag")
        layout.addWidget(protocol)
        return sidebar

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        title_box = QVBoxLayout()
        self.page_eyebrow = QLabel("WORKSPACE / SETUP")
        self.page_eyebrow.setObjectName("eyebrow")
        self.page_title = QLabel("Prepare a thermal run")
        self.page_title.setObjectName("pageTitle")
        title_box.addWidget(self.page_eyebrow)
        title_box.addWidget(self.page_title)
        layout.addLayout(title_box)
        layout.addStretch()
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_text = QLabel("Disconnected")
        self.status_text.setObjectName("statusText")
        layout.addWidget(self.status_dot)
        layout.addWidget(self.status_text)
        return layout

    def _scroll_page(self, body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        return scroll

    def _build_setup_page(self) -> QWidget:
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 8, 8)
        grid.setSpacing(18)
        connection = Card(
            "Instrument connection",
            "Simulation is safe for workflow testing. Live ASCII mode is an advanced placeholder until the LabVIEW byte protocol is mapped.",
        )
        self.simulation_check = QCheckBox("Use oven simulation")
        self.simulation_check.setChecked(True)
        self.simulation_check.toggled.connect(self._toggle_serial_fields)
        connection.body.addWidget(self.simulation_check)
        connection_form = QFormLayout()
        connection_form.setSpacing(12)
        port_row = QHBoxLayout()
        self.port_edit = QLineEdit("/dev/ttyUSB0")
        refresh = QPushButton("Refresh")
        refresh.setObjectName("quietButton")
        refresh.clicked.connect(self._refresh_ports)
        port_row.addWidget(self.port_edit)
        port_row.addWidget(refresh)
        connection_form.addRow("Serial port", port_row)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        connection_form.addRow("Baud rate", self.baud_combo)
        self.read_command_edit = QLineEdit(r"RD\n")
        self.write_command_edit = QLineEdit(r"SP {setpoint:.2f}\n")
        connection_form.addRow("ASCII read", self.read_command_edit)
        connection_form.addRow("ASCII setpoint", self.write_command_edit)
        connection.body.addLayout(connection_form)
        actions = QHBoxLayout()
        connect_button = QPushButton("Connect")
        connect_button.setObjectName("primaryButton")
        connect_button.clicked.connect(self._connect)
        disconnect_button = QPushButton("Disconnect")
        disconnect_button.setObjectName("secondaryButton")
        disconnect_button.clicked.connect(self._disconnect)
        actions.addWidget(connect_button)
        actions.addWidget(disconnect_button)
        connection.body.addLayout(actions)

        profile = Card("Run identity", "These fields travel with every logged temperature sample.")
        profile_form = QFormLayout()
        profile_form.setSpacing(12)
        self.operator_edit = QLineEdit("ASC Operator")
        self.batch_edit = QLineEdit("batch-001")
        self.sample_edit = QLineEdit()
        self.user_edit = QLineEdit("lab-user")
        self.atmosphere_check = QCheckBox("Atmosphere controlled")
        self.atmosphere_check.setChecked(True)
        self.notes_edit = QTextEdit()
        self.notes_edit.setFixedHeight(74)
        for label, widget in (
            ("Operator", self.operator_edit),
            ("Batch ID", self.batch_edit),
            ("Sample ID", self.sample_edit),
            ("Database user", self.user_edit),
            ("Environment", self.atmosphere_check),
            ("Notes", self.notes_edit),
        ):
            profile_form.addRow(label, widget)
        profile.body.addLayout(profile_form)

        thermal = Card("Thermal profile", "Set the target, ramp behavior, soak duration, and independent safety notifications.")
        thermal_form = QFormLayout()
        thermal_form.setSpacing(12)
        self.target_spin = self._temperature_spin(800.0, 0.0, 1400.0, " °C")
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
        go_live = QPushButton("Continue to live control  →")
        go_live.setObjectName("primaryButton")
        go_live.clicked.connect(lambda: self._set_page(1))
        thermal.body.addWidget(go_live)
        grid.addWidget(connection, 0, 0)
        grid.addWidget(profile, 0, 1, 2, 1)
        grid.addWidget(thermal, 1, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return self._scroll_page(body)

    @staticmethod
    def _temperature_spin(value: float, minimum: float, maximum: float, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def _build_control_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        metrics = QGridLayout()
        metrics.setSpacing(14)
        self.temp_metric = MetricCard("Chamber", "-- °C", "#56D6C9")
        self.setpoint_metric = MetricCard("Output", "-- °C", "#F4A261")
        self.phase_metric = MetricCard("Phase", "Idle", "#E9C46A")
        self.elapsed_metric = MetricCard("Elapsed", "00:00:00", "#5FA8D3")
        for column, metric in enumerate(
            (self.temp_metric, self.setpoint_metric, self.phase_metric, self.elapsed_metric)
        ):
            metrics.addWidget(metric, 0, column)
        layout.addLayout(metrics)

        command_row = QHBoxLayout()
        for text, object_name, handler in (
            ("Start run", "primaryButton", self._start_run),
            ("Pause", "secondaryButton", self.controller.pause),
            ("Resume", "secondaryButton", self.controller.resume),
            ("Stop", "dangerButton", self._stop_run),
        ):
            button = QPushButton(text)
            button.setObjectName(object_name)
            button.clicked.connect(handler)
            command_row.addWidget(button)
        command_row.addStretch()
        self.alarm_label = QLabel("No active alarm")
        self.alarm_label.setObjectName("alarmClear")
        command_row.addWidget(self.alarm_label)
        layout.addLayout(command_row)

        chart_card = Card("Temperature trend", "Live chamber temperature and commanded setpoint · latest five minutes")
        self.chart = TrendChart()
        chart_card.body.addWidget(self.chart, 1)
        layout.addWidget(chart_card, 1)

        manual = Card("Manual adjustment", "Changes apply to the active profile. Use direct setpoint only after confirming the instrument protocol.")
        manual_row = QHBoxLayout()
        self.manual_target_spin = self._temperature_spin(800.0, 0.0, 1400.0, " °C")
        self.manual_ramp_spin = self._temperature_spin(20.0, 0.0, 500.0, " °C/min")
        apply_target = QPushButton("Apply target")
        apply_target.setObjectName("secondaryButton")
        apply_target.clicked.connect(self._apply_manual_target)
        apply_ramp = QPushButton("Apply ramp")
        apply_ramp.setObjectName("secondaryButton")
        apply_ramp.clicked.connect(self._apply_manual_ramp)
        manual_row.addWidget(QLabel("Target"))
        manual_row.addWidget(self.manual_target_spin)
        manual_row.addWidget(apply_target)
        manual_row.addSpacing(18)
        manual_row.addWidget(QLabel("Ramp"))
        manual_row.addWidget(self.manual_ramp_spin)
        manual_row.addWidget(apply_ramp)
        manual_row.addStretch()
        manual.body.addLayout(manual_row)
        layout.addWidget(manual)
        return page

    def _build_data_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        actions = QHBoxLayout()
        description = QLabel("Inspect the latest run or export the complete sample stream.")
        description.setObjectName("muted")
        actions.addWidget(description)
        actions.addStretch()
        refresh = QPushButton("Refresh")
        refresh.setObjectName("secondaryButton")
        refresh.clicked.connect(self._refresh_table)
        export = QPushButton("Export CSV")
        export.setObjectName("primaryButton")
        export.clicked.connect(self._export_csv)
        actions.addWidget(refresh)
        actions.addWidget(export)
        layout.addLayout(actions)
        card = Card("Recorded samples")
        self.data_table = QTableWidget(0, 7)
        self.data_table.setHorizontalHeaderLabels(
            ["Timestamp", "Elapsed", "Temperature", "Setpoint", "Target", "Phase", "Alarm"]
        )
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.data_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.data_table.verticalHeader().setVisible(False)
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        card.body.addWidget(self.data_table)
        layout.addWidget(card, 1)
        return page

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            * { font-family: "Avenir Next", "Segoe UI"; font-size: 13px; color: #17313B; }
            QMainWindow, #content, QScrollArea, QScrollArea > QWidget > QWidget { background: #F3F0E8; }
            #sidebar { background: #102A36; }
            #logo { color: #F4A261; font-size: 36px; font-weight: 800; letter-spacing: 3px; }
            #product { color: #AFC2C8; font-size: 10px; font-weight: 700; letter-spacing: 3px; }
            #navButton { background: transparent; color: #AFC2C8; border: 0; border-radius: 9px;
                         text-align: left; padding: 13px 12px; font-weight: 600; }
            #navButton:hover { background: #1B3B47; color: white; }
            #navButton:checked { background: #F4A261; color: #102A36; }
            #protocolFlag { color: #718B95; border-top: 1px solid #294753; padding-top: 14px;
                            font-size: 9px; font-weight: 700; letter-spacing: 1px; }
            #eyebrow { color: #C16C37; font-size: 10px; font-weight: 800; letter-spacing: 2px; }
            #pageTitle { color: #102A36; font-size: 27px; font-weight: 700; }
            #statusDot { color: #BE3A34; font-size: 18px; }
            #statusText { color: #52666D; font-weight: 600; }
            #card, #metricCard { background: #FFFEFA; border: 1px solid #DDD9CE; border-radius: 14px; }
            #cardTitle { color: #102A36; font-size: 18px; font-weight: 700; }
            #muted { color: #6D7D82; }
            #metricCaption { color: #78898E; font-size: 9px; font-weight: 800; letter-spacing: 2px; }
            #metricValue { color: #102A36; font-size: 25px; font-weight: 700; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit { background: #F8F6F0;
                border: 1px solid #D5D0C4; border-radius: 7px; padding: 8px; selection-background-color: #F4A261; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
                border: 1px solid #C16C37; }
            QCheckBox { spacing: 9px; }
            QCheckBox::indicator { width: 18px; height: 18px; }
            QPushButton { border-radius: 8px; padding: 10px 16px; font-weight: 700; }
            #primaryButton { background: #C76532; color: white; border: 1px solid #C76532; }
            #primaryButton:hover { background: #AE5226; }
            #secondaryButton, #quietButton { background: #FFFEFA; color: #17313B; border: 1px solid #C9C5BA; }
            #secondaryButton:hover, #quietButton:hover { background: #ECE8DF; }
            #dangerButton { background: #FFF3F0; color: #A9322B; border: 1px solid #E4AAA5; }
            #alarmClear { background: #E5F3EC; color: #267150; border-radius: 12px; padding: 7px 12px; font-weight: 700; }
            #alarmActive { background: #FCE8E5; color: #A9322B; border-radius: 12px; padding: 7px 12px; font-weight: 700; }
            QTableWidget { background: #FFFEFA; alternate-background-color: #F5F2EA; border: 0;
                           gridline-color: #E5E0D5; selection-background-color: #F3D5BF; }
            QHeaderView::section { background: #E9E4DA; color: #53666C; border: 0; border-bottom: 1px solid #D2CCC0;
                                   padding: 10px; font-size: 10px; font-weight: 800; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
            QScrollBar::handle:vertical { background: #C9C4B9; border-radius: 5px; min-height: 30px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """
        )

    def _set_page(self, index: int) -> None:
        titles = (
            ("WORKSPACE / SETUP", "Prepare a thermal run"),
            ("WORKSPACE / LIVE CONTROL", "Monitor and guide the oven"),
            ("WORKSPACE / RUN DATA", "Review the temperature record"),
        )
        self.pages.setCurrentIndex(index)
        self.page_eyebrow.setText(titles[index][0])
        self.page_title.setText(titles[index][1])
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)
        if index == 2:
            self._refresh_table()

    def _toggle_serial_fields(self, simulation: bool) -> None:
        for widget in (
            self.port_edit,
            self.baud_combo,
            self.read_command_edit,
            self.write_command_edit,
        ):
            widget.setEnabled(not simulation)

    def _refresh_ports(self) -> None:
        try:
            from serial.tools import list_ports

            ports = [port.device for port in list_ports.comports()]
        except ImportError:
            QMessageBox.information(self, "Serial ports", "Install pyserial to discover serial ports.")
            return
        if ports:
            self.port_edit.setText(ports[0])
        else:
            QMessageBox.information(self, "Serial ports", "No serial ports were discovered.")

    def _connect(self) -> None:
        try:
            if self.simulation_check.isChecked():
                transport: BaseTransport = MockTransport(self.controller.current_temp)
            else:
                if not self.port_edit.text().strip():
                    raise ValueError("Enter a serial port")
                transport = AsciiSerialTransport(
                    self.port_edit.text().strip(),
                    int(self.baud_combo.currentText()),
                    self.read_command_edit.text(),
                    self.write_command_edit.text(),
                )
            self.controller.replace_transport(transport)
            self.transport = transport
            self.transport.connect()
            self._show_status("Connected", True)
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            self._show_status("Connection failed", False)

    def _disconnect(self) -> None:
        if self.controller.running:
            self.controller.stop()
        self.transport.disconnect()
        self._show_status("Disconnected", False)

    def _collect_profile(self) -> RunProfile:
        if not self.operator_edit.text().strip():
            raise ValueError("Operator is required")
        if self.alarm_low_spin.value() >= self.alarm_high_spin.value():
            raise ValueError("Low alarm must be below high alarm")
        return RunProfile(
            operator=self.operator_edit.text().strip(),
            batch_id=self.batch_edit.text().strip(),
            sample_id=self.sample_edit.text().strip(),
            user_name=self.user_edit.text().strip(),
            atmosphere=self.atmosphere_check.isChecked(),
            target_setpoint_c=self.target_spin.value(),
            ramp_rate_c_per_min=self.ramp_spin.value(),
            soak_time_sec=float(self.soak_spin.value()),
            alarm_high_c=self.alarm_high_spin.value(),
            alarm_low_c=self.alarm_low_spin.value(),
            notes=self.notes_edit.toPlainText().strip(),
        )

    def _start_run(self) -> None:
        try:
            profile = self._collect_profile()
            self.controller.start(profile)
            self.manual_target_spin.setValue(profile.target_setpoint_c)
            self.manual_ramp_spin.setValue(profile.ramp_rate_c_per_min)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot start run", str(exc))

    def _stop_run(self) -> None:
        self.controller.stop()
        self._refresh_table()

    def _apply_manual_target(self) -> None:
        try:
            value = self.manual_target_spin.value()
            self.controller.set_manual_target(value)
            self.target_spin.setValue(value)
        except Exception as exc:
            QMessageBox.warning(self, "Setpoint not applied", str(exc))

    def _apply_manual_ramp(self) -> None:
        value = self.manual_ramp_spin.value()
        self.controller.set_ramp_rate(value)
        self.ramp_spin.setValue(value)

    def _show_status(self, text: str, connected: bool) -> None:
        self.status_text.setText(text)
        self.status_dot.setStyleSheet(f"color: {'#2A9D75' if connected else '#BE3A34'};")

    def _poll_events(self) -> None:
        snapshot = None
        while True:
            try:
                snapshot = self.events.get_nowait()
            except queue.Empty:
                break
        if snapshot is None:
            return
        self.temp_metric.set_value(f"{snapshot['current_temp_c']:.1f} °C")
        self.setpoint_metric.set_value(f"{snapshot['output_setpoint_c']:.1f} °C")
        self.phase_metric.set_value(snapshot["phase"])
        elapsed = time.strftime("%H:%M:%S", time.gmtime(snapshot["elapsed_sec"]))
        self.elapsed_metric.set_value(elapsed)
        self.chart.append_point(
            snapshot["timestamp"], snapshot["current_temp_c"], snapshot["output_setpoint_c"]
        )
        alarm = snapshot["alarm"] or snapshot["error"]
        self.alarm_label.setText(alarm or "No active alarm")
        self.alarm_label.setObjectName("alarmActive" if alarm else "alarmClear")
        self.alarm_label.style().unpolish(self.alarm_label)
        self.alarm_label.style().polish(self.alarm_label)
        if snapshot["error"] and snapshot["error"] != self.last_error:
            self.last_error = snapshot["error"]
            self.status_text.setText("Communication warning")
        elif snapshot["connected"]:
            state = "Paused" if snapshot["paused"] else "Running" if snapshot["running"] else "Ready"
            self._show_status(state, True)
        else:
            self._show_status("Disconnected", False)

    def _refresh_table(self) -> None:
        run_id = self.controller.run_id or self.logger.latest_run_id()
        if run_id is None:
            self.data_table.setRowCount(0)
            return
        rows = self.logger.get_samples(run_id)
        self.data_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            timestamp, elapsed, temp, setpoint, target, phase, alarm, _connected = row
            values = (
                timestamp,
                time.strftime("%H:%M:%S", time.gmtime(elapsed)),
                f"{temp:.2f} °C",
                f"{setpoint:.2f} °C",
                f"{target:.2f} °C",
                phase,
                alarm or "",
            )
            for column, value in enumerate(values):
                self.data_table.setItem(row_index, column, QTableWidgetItem(str(value)))

    def _export_csv(self) -> None:
        run_id = self.controller.run_id or self.logger.latest_run_id()
        if run_id is None:
            QMessageBox.information(self, "Export", "There is no run to export yet.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Export run", f"asc-run-{run_id}.csv", "CSV files (*.csv)"
        )
        if target:
            self.logger.export_run_csv(run_id, target)
            self.status_text.setText(f"Exported run {run_id}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.controller.running:
            self.controller.stop()
        self.controller.shutdown()
        self.logger.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ASC Oven Control")
    app.setStyle("Fusion")
    window = OvenMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
