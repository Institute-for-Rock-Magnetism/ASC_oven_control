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

    def make(self, prof, commands=(), stop_after=None, csv=None):
        pending = list(commands)
        backend = RecordingOven()
        run_id = self.logger.start_run(prof)
        ticks = {"n": 0}

        def next_command(_timeout):
            ticks["n"] += 1
            return pending.pop(0) if pending else None

        def stop_requested():
            return stop_after is not None and ticks["n"] >= stop_after

        controller = RunController(
            profile=prof, run_id=run_id, logger=self.logger, backend=backend,
            settings=GradientSettings(soak_band_c=3.0, strict_soak=False),
            poll_seconds=0.0, emit=lambda *e: self.events.append(e), next_command=next_command,
            stop_requested=stop_requested, csv_log=csv, time_scale=1.0,
        )
        # Advance simulated time in fixed steps regardless of wall clock.
        controller._tick_real = controller._tick
        controller._tick = lambda last, advance_control: controller._tick_real(last - 2.0, advance_control)
        return controller, backend, run_id

    def test_run_completes_and_turns_heaters_off(self):
        csv_path = Path(self.tmp.name) / "run.csv"
        csv = LiveCsvLog(csv_path, ["test"])
        controller, backend, run_id = self.make(profile(), csv=csv)
        self.assertEqual(controller.run(), "Complete")
        csv.close()
        self.assertEqual(backend.shutdowns, 1)
        self.assertEqual(backend.setpoints, (0.0, 0.0, 0.0))
        self.assertGreater(len(self.logger.get_samples(run_id)), 10)
        self.assertGreater(len(csv_path.read_text(encoding="utf-8").splitlines()), 10)
        self.assertTrue(any(e[0] == "snapshot" for e in self.events))

    def test_stop_command_aborts_with_heaters_off(self):
        controller, backend, _ = self.make(profile(target=300.0), commands=[None, None, ("stop",)])
        self.assertEqual(controller.run(), "Aborted")
        self.assertEqual(backend.shutdowns, 1)

    def test_stop_file_aborts(self):
        controller, backend, _ = self.make(profile(target=300.0), stop_after=5)
        self.assertEqual(controller.run(), "Aborted")
        self.assertEqual(backend.shutdowns, 1)

    def test_over_temperature_trip(self):
        controller, backend, _ = self.make(profile(target=300.0, alarm_high=30.0))
        self.assertEqual(controller.run(), "Tripped")
        self.assertEqual(backend.shutdowns, 1)

    def test_max_run_time_ends_run_with_heaters_off(self):
        from dataclasses import replace

        prof = replace(profile(target=300.0), max_run_time_sec=60.0)
        controller, backend, run_id = self.make(prof)
        self.assertEqual(controller.run(), "Timed out")
        self.assertEqual(backend.shutdowns, 1)
        self.assertLess(controller.elapsed_sec, 70.0)
        status = self.logger.conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        self.assertEqual(status, "timed out")

    def test_settings_round_trip(self):
        s = GradientSettings(zone_offsets_c=(-8.0, 0.0, -8.0), max_gradient_c=5.0)
        self.assertEqual(settings_from_dict(settings_to_dict(s)), s)

    def test_no_active_run_without_marker(self):
        self.assertIsNone(read_active_run(Path(self.tmp.name)))


if __name__ == "__main__":
    unittest.main()
