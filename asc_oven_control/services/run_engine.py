"""Run engine: a QThread worker supervising the three oven zones.

The engine communicates with the UI exclusively through Qt signals (no
shared queues). Pause and abort use ``threading.Event``s so the worker
stops at safe boundaries. The worker is the only place that touches the
oven backend; the GUI remains passive.

Every poll the worker reads all three zones, lets ``ZoneCoordinator``
choose per-zone setpoints (guaranteed ramp, leader limiting, approach
deceleration, guaranteed soak — see ``domain/zone_control.py``) and writes
them to the backend. The Watlow PID loops do the fast control.

Safety behavior:

- Pause holds the setpoints currently in the controllers.
- Stop, abort and completion drive every zone to its lowest allowed
  setpoint (heaters off) before the worker exits.
- A communication failure fails the run after a best-effort safe shutdown.
  If the bus is gone the controllers keep holding their last setpoint,
  which is never above the current ramp position.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from asc_oven_control.domain.calculations import evaluate_alarm
from asc_oven_control.domain.models import OvenPhase, RunProfile, SamplePoint
from asc_oven_control.domain.zone_control import GradientSettings, ZoneCoordinator, ZonePhase
from asc_oven_control.infrastructure.persistence import RunLogger
from asc_oven_control.services.oven_backend import OvenBackend, SimulatedOven

HARDWARE_MIN_POLL_S = 2.0

_PHASES = {
    ZonePhase.RAMPING: OvenPhase.RAMPING,
    ZonePhase.HOLDING: OvenPhase.RAMPING,
    ZonePhase.APPROACH: OvenPhase.RAMPING,
    ZonePhase.SETTLING: OvenPhase.SOAKING,
    ZonePhase.SOAKING: OvenPhase.SOAKING,
    ZonePhase.COMPLETE: OvenPhase.COMPLETE,
}


class RunEngineError(RuntimeError):
    """Raised for invalid engine operations (e.g. starting twice)."""


class _RunWorker(QObject):
    """Owns the backend session and the run state machine; lives on a QThread."""

    snapshot_ready = Signal(object)
    state_changed = Signal(str, str)
    failed = Signal(str)
    finished = Signal(str)

    def __init__(
        self,
        profile: RunProfile,
        run_id: int,
        logger: RunLogger,
        poll_seconds: float,
        backend: OvenBackend,
        settings: GradientSettings,
        time_scale: float = 1.0,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.run_id = run_id
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.backend = backend
        self.time_scale = time_scale
        self.coordinator = ZoneCoordinator(
            target_c=profile.target_setpoint_c,
            ramp_rate_c_per_min=profile.ramp_rate_c_per_min,
            soak_time_s=profile.soak_time_sec,
            settings=settings,
        )
        self.phase = OvenPhase.RAMPING
        self.detail_phase = ZonePhase.RAMPING
        self.field_enabled = profile.field_enabled
        self.field_amplitude_uT = profile.field_amplitude_uT
        self.elapsed_sec = 0.0
        self.tripped = ""
        self._pause = threading.Event()
        self._pause.set()
        self._abort = threading.Event()
        self._commands: list = []
        self._commands_lock = threading.Lock()

    # Commands arrive from the UI thread; they are applied inside the loop.
    def pause(self) -> None:
        self._pause.clear()
        self.state_changed.emit(str(OvenPhase.PAUSED), "Run paused — holding current setpoints")

    def resume(self) -> None:
        self._pause.set()
        self.state_changed.emit(str(self.phase), "Run resumed")

    def abort(self) -> None:
        self._abort.set()
        self._pause.set()

    def _queue(self, command) -> None:
        with self._commands_lock:
            self._commands.append(command)

    def set_manual_target(self, target: float) -> None:
        self._queue(lambda: self._retarget(float(target)))

    def set_ramp_rate(self, rate: float) -> None:
        self._queue(lambda: setattr(self.coordinator, "ramp_rate_c_per_min", max(0.0, float(rate))))

    def set_settings(self, settings: GradientSettings) -> None:
        self._queue(lambda: setattr(self.coordinator, "settings", settings))

    def set_field(self, enabled: bool, amplitude_uT: float) -> None:
        self.field_enabled = enabled
        self.field_amplitude_uT = amplitude_uT

    def _retarget(self, target: float) -> None:
        self.profile = replace(self.profile, target_setpoint_c=target)
        self.coordinator.retarget(target)

    def run(self) -> None:
        outcome = "Stopped"
        try:
            self.backend.connect()
            try:
                taken = self.backend.take_control()
                if taken:
                    self.state_changed.emit(
                        str(self.phase),
                        f"Took set point control from the oven panel: {', '.join(taken)} now Local",
                    )
                outcome = self._loop()
            finally:
                self._shutdown_heaters()
                self.backend.close()
        except Exception as exc:  # noqa: BLE001 - report and finish
            self.failed.emit(str(exc))
            self.finished.emit("Failed")
            return
        self.finished.emit(outcome)

    def _shutdown_heaters(self) -> None:
        try:
            self.backend.safe_shutdown()
        except Exception as exc:  # noqa: BLE001 - surface, never mask the run outcome
            self.state_changed.emit(str(self.phase), f"Safe shutdown failed: {exc}")

    def _loop(self) -> str:
        last = time.monotonic()
        while not self._abort.is_set():
            if not self._pause.is_set():
                # Paused: keep reading so the display stays live, but do not
                # move the setpoints.
                self._tick(last, advance_control=False)
                last = time.monotonic()
                self._pause.wait(self.poll_seconds)
                continue
            started = time.monotonic()
            self._tick(last, advance_control=True)
            last = started
            if self.phase == OvenPhase.COMPLETE:
                return "Complete"
            elapsed = time.monotonic() - started
            self._abort.wait(max(self.poll_seconds - elapsed, 0.0))
        return "Tripped" if self.tripped else "Aborted"

    def _tick(self, last: float, advance_control: bool) -> None:
        now_mono = time.monotonic()
        dt = (now_mono - last) * self.time_scale
        self.elapsed_sec += dt
        with self._commands_lock:
            commands, self._commands = self._commands, []
        for command in commands:
            command()

        self.backend.advance(dt)
        reading = self.backend.read()
        if advance_control:
            decision = self.coordinator.step(reading.zones_c, dt)
            self.backend.write_setpoints(decision.zone_setpoints_c)
            if decision.phase != self.detail_phase:
                self.detail_phase = decision.phase
                self.state_changed.emit(str(_PHASES[decision.phase]), _phase_message(decision))
            new_phase = _PHASES[decision.phase]
            if new_phase == OvenPhase.COMPLETE and self.phase != OvenPhase.COMPLETE:
                self.logger.finish_run(self.run_id, status="complete")
            self.phase = new_phase
        master = self.coordinator.master_c if self.coordinator.master_c is not None else reading.zones_c[0]
        zones = reading.zones_c
        gradient = max(zones) - min(zones)
        alarm = evaluate_alarm(zones, self.profile.alarm_high_c, self.profile.alarm_low_c)
        if not alarm and reading.alarms:
            alarm = reading.alarms[0]
        if max(zones) >= self.profile.alarm_high_c and not self._abort.is_set():
            # Software over-temperature trip: end the run; the worker's exit
            # path drives every zone to its lowest set point.
            self.tripped = f"Over-temperature trip: {alarm}"
            self.state_changed.emit(str(self.phase), self.tripped + " — heaters off")
            self._abort.set()

        now = time.time()
        sample = SamplePoint(
            timestamp=now,
            elapsed_sec=self.elapsed_sec,
            zone_temps_c=zones,
            current_a=None,
            output_setpoint_c=master,
            target_setpoint_c=self.profile.target_setpoint_c,
            phase=self.phase if self._pause.is_set() else OvenPhase.PAUSED,
            alarm=alarm,
            connected=reading.connected and self.backend.is_hardware,
            zone_setpoints_c=reading.setpoints_c,
            zone_power_pct=reading.power_pct,
        )
        self.logger.log_sample(self.run_id, sample)
        self.snapshot_ready.emit(
            {
                "timestamp": now,
                "elapsed_sec": self.elapsed_sec,
                "zones": zones,
                "zone_setpoints": reading.setpoints_c,
                "zone_power": reading.power_pct,
                "gradient_c": gradient,
                "current_a": None,
                "output_setpoint_c": master,
                "target_setpoint_c": self.profile.target_setpoint_c,
                "phase": str(sample.phase),
                "control_phase": self.detail_phase,
                "soak_elapsed_s": self.coordinator.soak_elapsed_s,
                "out_of_band_s": self.coordinator.out_of_band_s,
                "soak_band_c": self.coordinator.settings.soak_band_c,
                "alarm": alarm,
                "field_enabled": self.field_enabled,
                "field_amplitude_uT": self.field_amplitude_uT,
                "hardware": self.backend.is_hardware,
            }
        )


def _phase_message(decision) -> str:
    if decision.phase == ZonePhase.HOLDING and decision.held_by is not None:
        return f"Ramp held for Zone {decision.held_by + 1} to catch up"
    return {
        ZonePhase.RAMPING: "Ramping",
        ZonePhase.APPROACH: "Approaching target at reduced rate",
        ZonePhase.SETTLING: "At target, waiting for all zones to settle",
        ZonePhase.SOAKING: "All zones in band, soaking",
        ZonePhase.COMPLETE: "Run complete, heaters off",
    }.get(decision.phase, decision.phase)


class RunEngine(QObject):
    """UI-thread facade owning the worker lifecycle."""

    snapshot_ready = Signal(object)
    state_changed = Signal(str, str)
    failed = Signal(str)
    finished = Signal(str)

    def __init__(
        self,
        logger: RunLogger,
        poll_seconds: float = 0.5,
        backend_factory=None,
        simulation_time_scale: float = 1.0,
    ) -> None:
        super().__init__()
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.backend_factory = backend_factory or SimulatedOven
        self.simulation_time_scale = simulation_time_scale
        self.settings = GradientSettings()
        self.state = "Idle"
        self.profile: Optional[RunProfile] = None
        self.run_id: Optional[int] = None
        self.last_run_id: Optional[int] = None
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
            "zone_setpoints": (25.0, 25.0, 25.0),
            "zone_power": (None, None, None),
            "gradient_c": 0.0,
            "current_a": None,
            "output_setpoint_c": 25.0,
            "target_setpoint_c": 25.0,
            "phase": "Idle",
            "control_phase": "",
            "soak_elapsed_s": 0.0,
            "alarm": "",
            "field_enabled": False,
            "field_amplitude_uT": 0.0,
            "hardware": False,
        }

    def start(self, profile: RunProfile) -> int:
        if self.active:
            raise RunEngineError("a run is already active")
        backend = self.backend_factory()
        run_id = self.logger.start_run(profile)
        self.profile = profile
        self.run_id = run_id
        self.state = "Running"
        self._thread = QThread(self)
        # One hardware poll (3 zones x 3 transactions) takes ~0.9 s.
        poll = max(self.poll_seconds, HARDWARE_MIN_POLL_S) if backend.is_hardware else self.poll_seconds
        self._worker = _RunWorker(
            profile,
            run_id,
            self.logger,
            poll,
            backend,
            self.settings,
            time_scale=1.0 if backend.is_hardware else self.simulation_time_scale,
        )
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
        """Abort the run at the next safe boundary (heaters driven off)."""
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

    def set_settings(self, settings: GradientSettings) -> None:
        self.settings = settings
        if self._worker is not None:
            self._worker.set_settings(settings)

    def set_field(self, enabled: bool, amplitude_uT: float) -> None:
        if self._worker is not None:
            self._worker.set_field(enabled, amplitude_uT)

    def shutdown(self) -> None:
        """Stop any active run and wait for the thread to finish."""
        if self._worker is not None and self.active:
            self._worker.abort()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(10000)

    # -- internal signal handlers (UI thread) --

    def _store_snapshot(self, snapshot: dict) -> None:
        self._last_snapshot = snapshot

    def _state_changed(self, state: str, message: str) -> None:
        # Worker phase strings map onto engine-level states.
        if state in ("Ramping", "Soaking"):
            self.state = "Paused" if self.state == "Paused" else "Running"
        elif state == "Paused":
            self.state = "Paused"
        elif state == "Complete":
            pass  # finished() sets the terminal state once heaters are off
        else:
            self.state = state

    def _worker_finished(self, outcome: str) -> None:
        self.last_run_id = self.run_id
        if self.run_id is not None and outcome in ("Stopped", "Aborted", "Failed", "Tripped"):
            self.logger.finish_run(self.run_id, status=outcome.lower())
        self.state = "Completed" if outcome == "Complete" else outcome
        self.run_id = None
        self._thread = None
        self._worker = None
