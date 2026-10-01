"""Run controller (the loop that runs in its own process), exercised in-process."""

import tempfile
import unittest
from pathlib import Path

from asc_oven_control.domain.models import Atmosphere, RunProfile
from asc_oven_control.domain.zone_control import GradientSettings
from asc_oven_control.infrastructure.persistence import LiveCsvLog, RunLogger
from asc_oven_control.services.oven_backend import SimulatedOven
from asc_oven_control.services.run_controller import (
    RunController,
    read_active_run,
    settings_from_dict,
    settings_to_dict,
)


def profile(target=40.0, soak=60.0, alarm_high=500.0):
    return RunProfile(
        operator="test", batch_id="", sample_id="", user_name="", atmosphere=Atmosphere.AIR,
        target_setpoint_c=target, ramp_rate_c_per_min=20.0, soak_time_sec=soak,
        alarm_high_c=alarm_high, alarm_low_c=-50.0,
    )


class RecordingOven(SimulatedOven):
    def __init__(self):
        super().__init__()
        self.shutdowns = 0

    def safe_shutdown(self):
        self.shutdowns += 1
        super().safe_shutdown()


class RunControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = RunLogger(Path(self.tmp.name) / "runs.db")
        self.events = []

    def tearDown(self):
        self.logger.close()
        self.tmp.cleanup()

    def make(self, prof, commands=(), stop_after=None, csv=None, cooling_ticks=5, backend=None):
        """Stop file after ``stop_after`` polls, or ``cooling_ticks`` polls into cooling."""
        pending = list(commands)
        backend = backend or RecordingOven()
        run_id = self.logger.start_run(prof)
        ticks = {"n": 0, "cooling": 0}
        holder = {}

        def next_command(_timeout):
            ticks["n"] += 1
            if holder["c"].cooling:
                ticks["cooling"] += 1
            return pending.pop(0) if pending else None

        def stop_requested():
            if stop_after is not None and ticks["n"] >= stop_after:
                return True
            return ticks["cooling"] >= cooling_ticks

        controller = RunController(
            profile=prof, run_id=run_id, logger=self.logger, backend=backend,
            settings=GradientSettings(soak_band_c=3.0, strict_soak=False),
            poll_seconds=0.0, emit=lambda *e: self.events.append(e), next_command=next_command,
            stop_requested=stop_requested, csv_log=csv, time_scale=1.0,
        )
        holder["c"] = controller
        # Advance simulated time in fixed steps regardless of wall clock.
        controller._tick_real = controller._tick
        controller._tick = lambda last, advance_control: controller._tick_real(last - 2.0, advance_control)
        return controller, backend, run_id

    def test_hold_complete_turns_heaters_off_then_records_cooling_until_stop(self):
        csv_path = Path(self.tmp.name) / "run.csv"
        csv = LiveCsvLog(csv_path, ["test"])
        controller, backend, run_id = self.make(profile(), csv=csv, cooling_ticks=20)
        self.assertEqual(controller.run(), "Complete")
        csv.close()
        self.assertGreaterEqual(backend.shutdowns, 1)
        self.assertEqual(backend.setpoints, (0.0, 0.0, 0.0))
        phases = [row[8] for row in self.logger.get_samples(run_id)]
        self.assertIn("Soaking", phases)
        self.assertGreaterEqual(phases.count("Cooling"), 15)  # cool-down recorded
        self.assertEqual(phases[-1], "Cooling")
        cooling = [e for e in self.events if e[0] == "state" and e[1] == "Cooling"]
        self.assertEqual(len(cooling), 1)
        self.assertIn("Cooling", csv_path.read_text(encoding="utf-8"))
        status = self.logger.conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        self.assertEqual(status, "complete")

    def test_heaters_go_off_as_soon_as_cooling_starts(self):
        controller, backend, _ = self.make(profile(), cooling_ticks=3)
        writes_after_cooling = []
        original = backend.write_setpoints

        def watch(setpoints):
            if controller.cooling:
                writes_after_cooling.append(setpoints)
            original(setpoints)

        backend.write_setpoints = watch
        controller.run()
        self.assertEqual(writes_after_cooling, [])  # no heating set points while cooling

    def test_cooling_ends_quietly_when_controllers_go_away(self):
        from asc_oven_control.infrastructure.serial_transport import CommunicationError

        class PowerOff(RecordingOven):
            def __init__(self):
                super().__init__()
                self.reads = 0
                self.controller = None

            def read(self):
                if self.controller is not None and self.controller.cooling:
                    self.reads += 1
                    if self.reads > 3:
                        raise CommunicationError("timeout")
                return super().read()

        backend = PowerOff()
        controller, _, run_id = self.make(profile(), backend=backend, cooling_ticks=10_000)
        backend.controller = controller
        self.assertEqual(controller.run(), "Complete")
        self.assertFalse(any(e[0] == "failed" for e in self.events))

    def test_stop_command_aborts_with_heaters_off(self):
        controller, backend, _ = self.make(profile(target=300.0), commands=[None, None, ("stop",)])
        self.assertEqual(controller.run(), "Aborted")
        self.assertEqual(backend.shutdowns, 1)

    def test_stop_file_aborts(self):
        controller, backend, _ = self.make(profile(target=300.0), stop_after=5)
        self.assertEqual(controller.run(), "Aborted")
        self.assertEqual(backend.shutdowns, 1)

    def test_over_temperature_trip_turns_heaters_off_and_keeps_recording(self):
        controller, backend, run_id = self.make(profile(target=300.0, alarm_high=30.0))
        self.assertEqual(controller.run(), "Tripped")
        self.assertGreaterEqual(backend.shutdowns, 1)
        self.assertIn("Cooling", [row[8] for row in self.logger.get_samples(run_id)])

    def test_max_run_time_ends_run_with_heaters_off(self):
        from dataclasses import replace

        prof = replace(profile(target=300.0), max_run_time_sec=60.0)
        controller, backend, run_id = self.make(prof)
        self.assertEqual(controller.run(), "Timed out")
        self.assertGreaterEqual(backend.shutdowns, 1)
        heating = [row for row in self.logger.get_samples(run_id) if row[8] != "Cooling"]
        self.assertLess(heating[-1][1], 70.0)  # heating ended at the limit
        status = self.logger.conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        self.assertEqual(status, "timed out")

    def test_settings_round_trip(self):
        s = GradientSettings(zone_offsets_c=(-8.0, 0.0, -8.0), max_gradient_c=5.0)
        self.assertEqual(settings_from_dict(settings_to_dict(s)), s)

    def test_no_active_run_without_marker(self):
        self.assertIsNone(read_active_run(Path(self.tmp.name)))


if __name__ == "__main__":
    unittest.main()
