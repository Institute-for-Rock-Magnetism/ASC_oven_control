"""Run engine: a QThread worker running the 3-zone oven simulation.

The engine communicates with the UI exclusively through Qt signals (no
shared queues). Pause and abort use ``threading.Event``s so the worker
stops at safe boundaries. The worker is the only place that touches the
thermal model; the GUI remains passive.

In simulation mode the worker drives the deterministic ``ThermalModel``.
A hardware transport would be swapped in here during commissioning, but the
GUI never creates one — see ``LABVIEW_MIGRATION.md``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from asc_oven_control.domain.calculations import ThermalModel, evaluate_alarm, ramp_step
from asc_oven_control.domain.models import OvenPhase, RunProfile, SamplePoint
from asc_oven_control.infrastructure.persistence import RunLogger


class RunEngineError(RuntimeError):
    """Raised for invalid engine operations (e.g. starting twice)."""


class _RunWorker(QObject):
    """Owns the thermal model and the run state machine; lives on a QThread."""

    snapshot_ready = Signal(object)
    state_changed = Signal(str, str)
    failed = Signal(str)
    finished = Signal(str)

    def __init__(self, profile: RunProfile, run_id: int, logger: RunLogger, poll_seconds: float) -> None:
        super().__init__()
        self.profile = profile
        self.run_id = run_id
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.model = ThermalModel()
        self.phase = OvenPhase.RAMPING
        # The commanded setpoint starts at the current chamber temperature
        # (ambient), matching the original LabVIEW behavior.
        self.output_setpoint = self.model.zones[0]
        self.field_enabled = profile.field_enabled
        self.field_amplitude_uT = profile.field_amplitude_uT
        self.elapsed_sec = 0.0
        self.started_monotonic = time.monotonic()
        self._pause = threading.Event()
        self._pause.set()
        self._abort = threading.Event()
        self._finished = False

    def pause(self) -> None:
        self._pause.clear()
        self.phase = OvenPhase.PAUSED
        self.state_changed.emit(str(self.phase), "Run paused")

    def resume(self) -> None:
        self._pause.set()
        self.phase = OvenPhase.RAMPING
        self.state_changed.emit(str(self.phase), "Run resumed")

    def abort(self) -> None:
        self._abort.set()
        self._pause.set()

    def set_manual_target(self, target: float) -> None:
        self.profile = replace(self.profile, target_setpoint_c=float(target))

    def set_ramp_rate(self, rate: float) -> None:
        self.profile = replace(self.profile, ramp_rate_c_per_min=max(0.0, float(rate)))

    def set_field(self, enabled: bool, amplitude_uT: float) -> None:
        self.field_enabled = enabled
        self.field_amplitude_uT = amplitude_uT

    def run(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # noqa: BLE001 - report and finish
            self.failed.emit(str(exc))
            self.finished.emit("Failed")
            return
        if self._abort.is_set():
            self.finished.emit("Aborted")
        elif self.phase == OvenPhase.COMPLETE:
            self.finished.emit("Complete")
        else:
            self.finished.emit("Stopped")

    def _loop(self) -> None:
        while not self._abort.is_set():
            self._pause.wait()
            if self._abort.is_set():
                return
            started = time.monotonic()
            now = time.time()
            self.elapsed_sec = started - self.started_monotonic

            # Advance the ramp/soak state machine and the thermal model.
            self._advance_run()
            self.model.update(
                self.output_setpoint,
                self.poll_seconds,
                self.field_enabled,
                self.field_amplitude_uT,
            )
            alarm = evaluate_alarm(
                self.model.zones, self.profile.alarm_high_c, self.profile.alarm_low_c
            )

            sample = SamplePoint(
                timestamp=now,
                elapsed_sec=self.elapsed_sec,
                zone_temps_c=self.model.zones,
                current_a=self.model.current_a,
                output_setpoint_c=self.output_setpoint,
                target_setpoint_c=self.profile.target_setpoint_c,
                phase=self.phase,
                alarm=alarm,
                connected=False,
            )
            self.logger.log_sample(self.run_id, sample)
            self.snapshot_ready.emit(
                {
                    "timestamp": now,
                    "elapsed_sec": self.elapsed_sec,
                    "zones": self.model.zones,
                    "current_a": self.model.current_a,
                    "output_setpoint_c": self.output_setpoint,
                    "target_setpoint_c": self.profile.target_setpoint_c,
                    "phase": str(self.phase),
                    "alarm": alarm,
                    "field_enabled": self.field_enabled,
                    "field_amplitude_uT": self.field_amplitude_uT,
                }
            )
            if self.phase == OvenPhase.COMPLETE:
                return
            # Keep the loop cadence accurate regardless of work duration.
            elapsed = time.monotonic() - started
            time.sleep(max(self.poll_seconds - elapsed, 0.0))

    def _advance_run(self) -> None:
        """Move the commanded setpoint along the ramp, then the soak."""
        target = self.profile.target_setpoint_c
        self.output_setpoint = ramp_step(
            self.output_setpoint, target, self.profile.ramp_rate_c_per_min, self.poll_seconds
        )
        if abs(target - self.output_setpoint) <= 0.05:
            if self.phase == OvenPhase.RAMPING:
                self.phase = OvenPhase.SOAKING
                self.soak_started = self.elapsed_sec
                self.state_changed.emit(str(self.phase), "Target reached, soaking")
            elif self.phase == OvenPhase.SOAKING:
                if self.elapsed_sec - self.soak_started >= self.profile.soak_time_sec:
                    self.phase = OvenPhase.COMPLETE
                    self.logger.finish_run(self.run_id, status="complete")
                    self.state_changed.emit(str(self.phase), "Run complete")


class RunEngine(QObject):
    """UI-thread facade owning the worker lifecycle."""

    snapshot_ready = Signal(object)
    state_changed = Signal(str, str)
    failed = Signal(str)
    finished = Signal(str)

    def __init__(self, logger: RunLogger, poll_seconds: float = 0.5) -> None:
        super().__init__()
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.state = "Idle"
        self.profile: Optional[RunProfile] = None
        self.run_id: Optional[int] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RunWorker] = None
        self._last_snapshot: Optional[dict] = None

    @property
    def active(self) -> bool:
        return self.state in {"Running", "Paused"}

    @property
    def snapshot(self) -> dict:
        """Most recent worker snapshot (empty defaults before first tick)."""
        if self._last_snapshot is not None:
            return self._last_snapshot
        return {
            "timestamp": 0.0,
            "elapsed_sec": 0.0,
            "zones": (25.0, 25.0, 25.0),
            "current_a": 0.0,
            "output_setpoint_c": 25.0,
            "target_setpoint_c": 25.0,
            "phase": "Idle",
            "alarm": "",
            "field_enabled": False,
            "field_amplitude_uT": 0.0,
        }

    def start(self, profile: RunProfile) -> int:
        if self.active:
            raise RunEngineError("a run is already active")
        run_id = self.logger.start_run(profile)
        self.profile = profile
        self.run_id = run_id
        self.state = "Running"
        self._thread = QThread(self)
        self._worker = _RunWorker(profile, run_id, self.logger, self.poll_seconds)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        for signal_name in ("snapshot_ready", "state_changed", "failed", "finished"):
            worker_signal = getattr(self._worker, signal_name)
            engine_signal = getattr(self, signal_name)
            worker_signal.connect(engine_signal)
        self._worker.snapshot_ready.connect(self._store_snapshot)
        self._worker.state_changed.connect(self._state_changed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self.state_changed.emit(self.state, "Run started")
        self._thread.start()
        return run_id

    def pause(self) -> None:
        if self._worker is not None and self.active:
            self.state = "Paused"
            self._worker.pause()

    def resume(self) -> None:
        if self._worker is not None and self.active:
            self.state = "Running"
            self._worker.resume()

    def stop(self) -> None:
        """Abort the run at the next safe boundary."""
        if self._worker is not None and self.active:
            self._worker.abort()

    def set_manual_target(self, target: float) -> None:
        if self.profile is not None:
            self.profile = replace(self.profile, target_setpoint_c=float(target))
        if self._worker is not None:
            self._worker.set_manual_target(target)

    def set_ramp_rate(self, rate: float) -> None:
        if self.profile is not None:
            self.profile = replace(self.profile, ramp_rate_c_per_min=max(0.0, float(rate)))
        if self._worker is not None:
            self._worker.set_ramp_rate(rate)

    def set_field(self, enabled: bool, amplitude_uT: float) -> None:
        if self._worker is not None:
            self._worker.set_field(enabled, amplitude_uT)

    def shutdown(self) -> None:
        """Stop any active run and wait for the thread to finish."""
        if self._worker is not None and self.active:
            self._worker.abort()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)

    # -- internal signal handlers (UI thread) --

    def _store_snapshot(self, snapshot: dict) -> None:
        self._last_snapshot = snapshot

    def _state_changed(self, state: str, message: str) -> None:
        # Worker phase strings map onto engine-level states.
        if state in ("Ramping", "Soaking"):
            self.state = "Running"
        elif state == "Paused":
            self.state = "Paused"
        else:
            self.state = state

    def _worker_finished(self, outcome: str) -> None:
        if self.run_id is not None and outcome in ("Stopped", "Aborted", "Failed"):
            self.logger.finish_run(self.run_id, status=outcome.lower())
        self.state = "Completed" if outcome == "Complete" else outcome
        self.run_id = None
        self._thread = None
        self._worker = None
