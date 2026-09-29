"""Qt-free run controller, run in its own process so the UI cannot stop it.

The first heated run (2026-09-29) was lost when the UI thread hung and
Windows closed the application: the control loop lived in that process, so
the soak never finished and nothing turned the heaters off. The controller
therefore runs in a separate process (``run_service``):

- it owns the serial port, the oven backend and the coordinator;
- it writes every sample to the SQLite run log and the live CSV itself, so
  the record continues if the UI freezes, crashes or is closed;
- it finishes the run (or stops on command) and always ends with the
  heaters off (every zone at its lowest set point);
- it publishes a marker file (``active_run.json``) while running, so a
  restarted UI can reattach and read the live CSV, and it watches a stop
  file so a restarted UI can stop it without the original queues.

Commands arrive on ``commands`` (a ``multiprocessing.Queue``) as tuples;
events go out on ``events``. If the UI process disappears, putting events
simply stops being read; the run carries on.

Safety behavior (unchanged from the in-process engine):

- Pause holds the set points currently in the controllers.
- Stop, completion, an over-temperature trip and a communication failure
  all end with a best-effort safe shutdown.
"""

from __future__ import annotations

import json
import os
import queue
import time
from dataclasses import asdict, fields, replace
from pathlib import Path

from asc_oven_control.domain.calculations import evaluate_alarm
from asc_oven_control.domain.models import OvenPhase, RunProfile, SamplePoint
from asc_oven_control.domain.zone_control import GradientSettings, ZoneCoordinator, ZonePhase
from asc_oven_control.infrastructure.config import ApplicationConfig
from asc_oven_control.infrastructure.persistence import LiveCsvLog, RunLogger

HARDWARE_MIN_POLL_S = 2.0
ACTIVE_RUN_FILE = "active_run.json"

_PHASES = {
    ZonePhase.RAMPING: OvenPhase.RAMPING,
    ZonePhase.HOLDING: OvenPhase.RAMPING,
    ZonePhase.APPROACH: OvenPhase.RAMPING,
    ZonePhase.SETTLING: OvenPhase.SOAKING,
    ZonePhase.SOAKING: OvenPhase.SOAKING,
    ZonePhase.COMPLETE: OvenPhase.COMPLETE,
}


def settings_to_dict(settings: GradientSettings) -> dict:
    return asdict(settings)


def settings_from_dict(data: dict) -> GradientSettings:
    known = {f.name for f in fields(GradientSettings)}
    values = {k: v for k, v in data.items() if k in known}
    if "zone_offsets_c" in values:
        values["zone_offsets_c"] = tuple(values["zone_offsets_c"])
    return GradientSettings(**values)


def stop_file(control_dir: Path, run_id: int) -> Path:
    return Path(control_dir) / f"stop-{run_id}"


def read_active_run(control_dir: Path) -> dict | None:
    """The marker of a run whose controller process is still alive, if any."""
    path = Path(control_dir) / ACTIVE_RUN_FILE
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _pid_alive(int(info.get("pid", 0))):
        return None
    return info


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RunController:
    """The control loop; ``emit(kind, *payload)`` reports to whoever listens."""

    def __init__(
        self,
        profile: RunProfile,
        run_id: int,
        logger: RunLogger,
        backend,
        settings: GradientSettings,
        poll_seconds: float,
        emit,
        next_command,
        stop_requested=lambda: False,
        csv_log: LiveCsvLog | None = None,
        time_scale: float = 1.0,
    ) -> None:
        self.profile = profile
        self.run_id = run_id
        self.logger = logger
        self.backend = backend
        self.poll_seconds = poll_seconds
        self.emit = emit
        self.next_command = next_command
        self.stop_requested = stop_requested
        self.csv_log = csv_log
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
        self.paused = False
        self.aborted = False
        self.tripped = ""

    # -------------------------------------------------------------- commands

    def _apply(self, command: tuple) -> None:
        kind, *args = command
        if kind == "stop":
            self.aborted = True
        elif kind == "pause":
            self.paused = True
            self.emit("state", str(OvenPhase.PAUSED), "Run paused — holding current setpoints")
        elif kind == "resume":
            self.paused = False
            self.emit("state", str(self.phase), "Run resumed")
        elif kind == "target":
            self.profile = replace(self.profile, target_setpoint_c=float(args[0]))
            self.coordinator.retarget(float(args[0]))
        elif kind == "ramp":
            self.coordinator.ramp_rate_c_per_min = max(0.0, float(args[0]))
        elif kind == "settings":
            self.coordinator.settings = settings_from_dict(args[0])
        elif kind == "field":
            self.field_enabled, self.field_amplitude_uT = bool(args[0]), float(args[1])

    def _wait(self, seconds: float) -> None:
        """Sleep up to ``seconds`` while applying commands as they arrive."""
        deadline = time.monotonic() + seconds
        while not self.aborted:
            # Always look at least once, even when the poll is overdue.
            remaining = max(deadline - time.monotonic(), 0.0)
            command = self.next_command(min(remaining, 0.5))
            if command is not None:
                self._apply(command)
            if self.stop_requested():
                self.aborted = True
            if time.monotonic() >= deadline:
                return

    # ------------------------------------------------------------------ run

    def run(self) -> str:
        try:
            self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            self.emit("failed", f"cannot connect: {exc}")
            return "Failed"
        outcome = "Failed"
        try:
            try:
                taken = self.backend.take_control()
                if taken:
                    self.emit(
                        "state", str(self.phase),
                        f"Took set point control from the oven panel: {', '.join(taken)} now Local",
                    )
                outcome = self._loop()
            finally:
                self._shutdown_heaters()
                self.backend.close()
        except Exception as exc:  # noqa: BLE001 - report and finish
            self.emit("failed", str(exc))
            outcome = "Failed"
        status = {"Complete": "complete"}.get(outcome, outcome.lower())
        self.logger.finish_run(self.run_id, status=status)
        return outcome

    def _shutdown_heaters(self) -> None:
        try:
            self.backend.safe_shutdown()
        except Exception as exc:  # noqa: BLE001 - surface, never mask the outcome
            self.emit("state", str(self.phase), f"Safe shutdown failed: {exc}")

    def _loop(self) -> str:
        last = time.monotonic()
        while not self.aborted:
            started = time.monotonic()
            self._tick(last, advance_control=not self.paused)
            last = started
            if self.phase == OvenPhase.COMPLETE:
                return "Complete"
            self._wait(max(self.poll_seconds - (time.monotonic() - started), 0.0))
        return "Tripped" if self.tripped else "Aborted"

    def _tick(self, last: float, advance_control: bool) -> None:
        dt = (time.monotonic() - last) * self.time_scale
        self.elapsed_sec += dt
        self.backend.advance(dt)
        reading = self.backend.read()
        if advance_control:
            decision = self.coordinator.step(reading.zones_c, dt)
            self.backend.write_setpoints(decision.zone_setpoints_c)
            if decision.phase != self.detail_phase:
                self.detail_phase = decision.phase
                self.emit("state", str(_PHASES[decision.phase]), _phase_message(decision))
            self.phase = _PHASES[decision.phase]
        master = self.coordinator.master_c if self.coordinator.master_c is not None else reading.zones_c[0]
        zones = reading.zones_c
        alarm = evaluate_alarm(zones, self.profile.alarm_high_c, self.profile.alarm_low_c)
        if not alarm and reading.alarms:
            alarm = reading.alarms[0]
        if max(zones) >= self.profile.alarm_high_c and not self.aborted:
            self.tripped = f"Over-temperature trip: {alarm}"
            self.emit("state", str(self.phase), self.tripped + " — heaters off")
            self.aborted = True

        now = time.time()
        phase = OvenPhase.PAUSED if self.paused else self.phase
        self.logger.log_sample(
            self.run_id,
            SamplePoint(
                timestamp=now,
                elapsed_sec=self.elapsed_sec,
                zone_temps_c=zones,
                current_a=None,
                output_setpoint_c=master,
                target_setpoint_c=self.profile.target_setpoint_c,
                phase=phase,
                alarm=alarm,
                connected=reading.connected and self.backend.is_hardware,
                zone_setpoints_c=reading.setpoints_c,
                zone_power_pct=reading.power_pct,
            ),
        )
        snapshot = {
            "timestamp": now,
            "elapsed_sec": self.elapsed_sec,
            "zones": zones,
            "zone_setpoints": reading.setpoints_c,
            "zone_power": reading.power_pct,
            "gradient_c": max(zones) - min(zones),
            "current_a": None,
            "output_setpoint_c": master,
            "target_setpoint_c": self.profile.target_setpoint_c,
            "phase": str(phase),
            "control_phase": self.detail_phase,
            "soak_elapsed_s": self.coordinator.soak_elapsed_s,
            "out_of_band_s": self.coordinator.out_of_band_s,
            "soak_band_c": self.coordinator.settings.soak_band_c,
            "center_comp_c": self.coordinator.center_comp_c,
            "zone_targets": tuple(self.profile.target_setpoint_c + o for o in self.coordinator.offsets()),
            "alarm": alarm,
            "field_enabled": self.field_enabled,
            "field_amplitude_uT": self.field_amplitude_uT,
            "hardware": self.backend.is_hardware,
        }
        if self.csv_log is not None:
            try:
                self.csv_log.write_snapshot(snapshot)
            except OSError as exc:
                self.emit("state", str(self.phase), f"CSV log write failed: {exc}")
                self.csv_log = None
        self.emit("snapshot", snapshot)


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


def run_service(job: dict, commands, events) -> None:
    """Entry point of the run process (must stay importable at top level)."""
    control_dir = Path(job["control_dir"])
    control_dir.mkdir(parents=True, exist_ok=True)
    run_id = int(job["run_id"])
    # pythonw has no console: keep this process's errors in a file.
    import faulthandler
    import sys

    err = open(control_dir / f"run-{run_id:04d}-process.log", "a", encoding="utf-8", buffering=1)
    sys.stderr = err
    faulthandler.enable(err, all_threads=True)

    from asc_oven_control.services.oven_backend import create_backend

    def emit(kind, *payload):
        try:
            events.put((kind, *payload))
        except Exception:  # noqa: BLE001 - UI gone: keep running
            pass

    def next_command(timeout):
        try:
            return commands.get(timeout=timeout)
        except queue.Empty:
            return None
        except (EOFError, OSError):
            time.sleep(timeout)  # UI gone: keep the loop's pacing
            return None

    stop_path = stop_file(control_dir, run_id)
    marker = control_dir / ACTIVE_RUN_FILE
    config = ApplicationConfig.from_dict(job["config"])
    logger = RunLogger(job["db_path"])
    csv_log = LiveCsvLog(job["csv_path"], job.get("csv_header", ()))
    marker.write_text(
        json.dumps({"pid": os.getpid(), "run_id": run_id, "csv_path": job["csv_path"],
                    "started": time.time(), "target_c": job["profile"]["target_setpoint_c"]}),
        encoding="utf-8",
    )
    outcome = "Failed"
    try:
        controller = RunController(
            profile=RunProfile.from_dict(job["profile"]),
            run_id=run_id,
            logger=logger,
            backend=create_backend(config),
            settings=settings_from_dict(job["settings"]),
            poll_seconds=float(job["poll_seconds"]),
            emit=emit,
            next_command=next_command,
            stop_requested=stop_path.exists,
            csv_log=csv_log,
            time_scale=float(job.get("time_scale", 1.0)),
        )
        outcome = controller.run()
    except Exception as exc:  # noqa: BLE001
        emit("failed", str(exc))
    finally:
        csv_log.close()
        logger.close()
        for path in (marker, stop_path):
            try:
                path.unlink()
            except OSError:
                pass
        emit("finished", outcome)
        err.flush()
