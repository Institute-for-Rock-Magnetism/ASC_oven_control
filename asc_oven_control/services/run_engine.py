"""Run engine: Qt facade over the run controller process.

The control loop itself lives in ``run_controller.py`` and runs in its own
process, so a frozen, crashed or closed UI cannot stop a run halfway: the
process keeps logging, finishes (or stops on command) and always ends with
the heaters off. This facade starts that process, forwards commands, and
turns its events back into Qt signals on the UI thread.

A restarted UI can ``attach`` to a run whose process is still alive (found
through its ``active_run.json`` marker): it follows the live CSV and can stop
the run through the stop file.
"""

from __future__ import annotations

import csv
import multiprocessing
import queue
from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from asc_oven_control.domain.models import RunProfile
from asc_oven_control.domain.zone_control import GradientSettings
from asc_oven_control.infrastructure.persistence import RunLogger
from asc_oven_control.services.run_controller import (
    HARDWARE_MIN_POLL_S,
    read_active_run,
    run_service,
    settings_to_dict,
    stop_file,
)

__all__ = ["HARDWARE_MIN_POLL_S", "RunEngine", "RunEngineError"]


class RunEngineError(RuntimeError):
    """Raised for invalid engine operations (e.g. starting twice)."""


class RunEngine(QObject):
    """UI-thread facade owning the run process lifecycle."""

    snapshot_ready = Signal(object)
    state_changed = Signal(str, str)
    failed = Signal(str)
    finished = Signal(str)

    def __init__(
        self,
        logger: RunLogger,
        poll_seconds: float = 0.5,
        config_provider=None,
        simulation_time_scale: float = 1.0,
        control_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.config_provider = config_provider
        self.simulation_time_scale = simulation_time_scale
        self.control_dir = Path(control_dir) if control_dir is not None else Path(logger.path).parent / "control"
        self.settings = GradientSettings()
        self.notifications_provider = None  # () -> dict for the run process, or None
        self.state = "Idle"
        self.profile: Optional[RunProfile] = None
        self.run_id: Optional[int] = None
        self.last_run_id: Optional[int] = None
        self.attached = False
        self.cooling = False  # heating finished; still recording until Stop
        self._process: Optional[multiprocessing.Process] = None
        self._commands = None
        self._events = None
        self._last_snapshot: Optional[dict] = None
        self._attach_info: Optional[dict] = None
        self._attach_rows = 0
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._pump)

    @property
    def active(self) -> bool:
        return self.state in {"Running", "Paused"}

    @property
    def snapshot(self) -> dict:
        """Most recent snapshot (empty defaults before the first one)."""
        if self._last_snapshot is not None:
            return self._last_snapshot
        return {
            "timestamp": 0.0, "elapsed_sec": 0.0, "zones": (25.0, 25.0, 25.0),
            "zone_setpoints": (25.0, 25.0, 25.0), "zone_power": (None, None, None),
            "gradient_c": 0.0, "current_a": None, "output_setpoint_c": 25.0,
            "target_setpoint_c": 25.0, "phase": "Idle", "control_phase": "",
            "soak_elapsed_s": 0.0, "alarm": "", "field_enabled": False,
            "field_amplitude_uT": 0.0, "hardware": False,
        }

    # --------------------------------------------------------------- start

    def start(self, profile: RunProfile, csv_path: str | None = None, csv_header=()) -> int:
        if self.active:
            raise RunEngineError("a run is already active")
        if read_active_run(self.control_dir) is not None:
            raise RunEngineError("a run is still active in a background process")
        config = self.config_provider()
        hardware = not config.simulation_mode
        run_id = self.logger.start_run(profile)
        poll = max(self.poll_seconds, HARDWARE_MIN_POLL_S) if hardware else self.poll_seconds
        if csv_path is None:
            csv_path = str(self.control_dir / f"run-{run_id:04d}.csv")
        job = {
            "run_id": run_id,
            "profile": profile.to_dict(),
            "settings": settings_to_dict(self.settings),
            "config": config.to_dict(),
            "db_path": str(self.logger.path),
            "csv_path": csv_path,
            "csv_header": list(csv_header),
            "control_dir": str(self.control_dir),
            "poll_seconds": poll,
            "time_scale": 1.0 if hardware else self.simulation_time_scale,
            "notifications": self.notifications_provider() if self.notifications_provider else None,
        }
        context = multiprocessing.get_context("spawn")
        self._commands = context.Queue()
        self._events = context.Queue()
        # Not a daemon: the run must outlive the UI if the UI dies.
        self._process = context.Process(
            target=run_service, args=(job, self._commands, self._events), name=f"oven-run-{run_id}"
        )
        self._process.start()
        self.profile = profile
        self.run_id = run_id
        self.attached = False
        self.cooling = False
        self._last_snapshot = None
        self.state = "Running"
        self.state_changed.emit(self.state, "Run started")
        self._timer.start()
        return run_id

    def attach(self) -> Optional[dict]:
        """Follow a run whose process outlived a previous UI session."""
        info = read_active_run(self.control_dir)
        if info is None or self.active:
            return None
        self._attach_info = info
        self._attach_rows = 0
        self.run_id = int(info["run_id"])
        self.attached = True
        self.state = "Running"
        self.state_changed.emit(self.state, f"Reattached to background run {self.run_id}")
        self._timer.start()
        return info

    # ------------------------------------------------------------ commands

    def _send(self, *command) -> None:
        if self._commands is not None:
            self._commands.put(command)

    def pause(self) -> None:
        if self.active and not self.attached and not self.cooling:
            self.state = "Paused"
            self._send("pause")

    def resume(self) -> None:
        if self.active and not self.attached:
            self.state = "Running"
            self._send("resume")

    def stop(self) -> None:
        """Stop the run at the next safe boundary (heaters driven off)."""
        if not self.active:
            return
        if self.attached:
            stop_file(self.control_dir, self.run_id).touch()
        else:
            self._send("stop")

    def set_manual_target(self, target: float) -> None:
        if self.profile is not None:
            self.profile = replace(self.profile, target_setpoint_c=float(target))
        self._send("target", float(target))

    def set_ramp_rate(self, rate: float) -> None:
        if self.profile is not None:
            self.profile = replace(self.profile, ramp_rate_c_per_min=max(0.0, float(rate)))
        self._send("ramp", float(rate))

    def set_settings(self, settings: GradientSettings) -> None:
        self.settings = settings
        if self.active:
            self._send("settings", settings_to_dict(settings))

    def set_field(self, enabled: bool, amplitude_uT: float) -> None:
        self._send("field", bool(enabled), float(amplitude_uT))

    def detach(self) -> None:
        """Leave the run going in the background (used when the UI closes)."""
        self._timer.stop()
        if self._process is not None:
            # multiprocessing joins live children at interpreter exit; a
            # detached run must not keep the closed UI's process alive.
            from multiprocessing import process as mp_process

            mp_process._children.discard(self._process)  # noqa: SLF001

    def shutdown(self) -> None:
        """Stop any active run and wait for its process to end."""
        if self.active:
            self.stop()
        if self._process is not None:
            self._process.join(15)

    # --------------------------------------------------------------- events

    def _pump(self) -> None:
        if self.attached:
            self._pump_attached()
            return
        for _ in range(200):
            try:
                kind, *payload = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle(kind, payload)
        if self.active and self._process is not None and not self._process.is_alive():
            # Process died without reporting: say so rather than hang in Running.
            self._handle("failed", ["run process exited unexpectedly"])
            self._handle("finished", ["Failed"])

    def _handle(self, kind: str, payload: list) -> None:
        if kind == "snapshot":
            self._last_snapshot = payload[0]
            self.snapshot_ready.emit(payload[0])
        elif kind == "state":
            state, message = payload
            if state == "Cooling":
                self.cooling = True
                self.state = "Running"
            elif state in ("Ramping", "Soaking"):
                self.state = "Paused" if self.state == "Paused" else "Running"
            elif state == "Paused":
                self.state = "Paused"
            self.state_changed.emit(state, message)
        elif kind == "failed":
            self.failed.emit(payload[0])
        elif kind == "finished":
            self._finish(payload[0])

    def _finish(self, outcome: str) -> None:
        self._timer.stop()
        self.last_run_id = self.run_id
        self.state = "Completed" if outcome == "Complete" else outcome
        self.run_id = None
        self.attached = False
        self.cooling = False
        self._attach_info = None
        if self._process is not None:
            self._process.join(2)
            self._process = None
        self.finished.emit(outcome)

    def _pump_attached(self) -> None:
        info = self._attach_info
        rows = _read_csv_rows(info["csv_path"])
        for row in rows[self._attach_rows:]:
            snapshot = _snapshot_from_row(row)
            if snapshot is not None:
                self.cooling = snapshot["phase"] == "Cooling"
                self._last_snapshot = snapshot
                self.snapshot_ready.emit(snapshot)
        self._attach_rows = len(rows)
        if read_active_run(self.control_dir) is None:
            self._finish("Stopped")


def _read_csv_rows(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(line for line in handle if not line.startswith("#")))
    except OSError:
        return []


def _snapshot_from_row(row: dict) -> Optional[dict]:
    """Rebuild a UI snapshot from one live-CSV row (reattached runs)."""

    def number(key, default=None):
        try:
            return float(row[key])
        except (KeyError, TypeError, ValueError):
            return default

    try:
        zones = tuple(float(row[f"zone{i}_c"]) for i in (1, 2, 3))
    except (KeyError, ValueError):
        return None
    from datetime import datetime

    try:
        timestamp = datetime.fromisoformat(row["timestamp"]).timestamp()
    except (KeyError, ValueError):
        timestamp = 0.0
    return {
        "timestamp": timestamp,
        "elapsed_sec": (number("elapsed_min", 0.0) or 0.0) * 60.0,
        "zones": zones,
        "zone_setpoints": tuple(number(f"zone{i}_sp_c") for i in (1, 2, 3)),
        "zone_power": tuple(number(f"zone{i}_power_pct") for i in (1, 2, 3)),
        "gradient_c": number("gradient_c", max(zones) - min(zones)),
        "current_a": None,
        "output_setpoint_c": number("ramp_setpoint_c", zones[0]),
        "target_setpoint_c": number("target_c", 0.0),
        "phase": row.get("phase", ""),
        "control_phase": row.get("control_phase", ""),
        "soak_elapsed_s": number("soak_elapsed_s", 0.0),
        "out_of_band_s": number("out_of_band_s", 0.0),
        "zone_targets": tuple(number(f"zone{i}_target_c", 0.0) for i in (1, 2, 3)),
        "center_comp_c": number("center_comp_c", 0.0),
        "alarm": row.get("alarm", ""),
        "field_enabled": False,
        "field_amplitude_uT": 0.0,
        "hardware": True,
    }
