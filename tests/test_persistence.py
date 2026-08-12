"""Tests for the SQLite run logger and CSV export."""

import csv
import tempfile
import unittest
from pathlib import Path

from asc_oven_control.domain.models import Atmosphere, OvenPhase, RunProfile, SamplePoint
from asc_oven_control.infrastructure.persistence import RunLogger, export_samples_csv


def profile() -> RunProfile:
    return RunProfile(
        operator="Tester",
        batch_id="batch-1",
        sample_id="sample-1",
        user_name="lab-user",
        atmosphere=Atmosphere.NITROGEN,
        target_setpoint_c=590.0,
        ramp_rate_c_per_min=20.0,
        soak_time_sec=600.0,
        alarm_high_c=1200.0,
        alarm_low_c=10.0,
        field_enabled=True,
        field_amplitude_uT=50.0,
    )


class RunLoggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "runs.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_run_lifecycle(self):
        logger = RunLogger(self.db_path)
        try:
            run_id = logger.start_run(profile())
            self.assertIsNotNone(run_id)
            sample = SamplePoint(
                timestamp=1234.0,
                elapsed_sec=12.0,
                zone_temps_c=(100.0, 80.0, 60.0),
                current_a=5.2,
                output_setpoint_c=90.0,
                target_setpoint_c=590.0,
                phase=OvenPhase.RAMPING,
                alarm="",
                connected=False,
            )
            logger.log_sample(run_id, sample)
            logger.finish_run(run_id, status="complete")
            rows = logger.get_samples(run_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][2], 100.0)  # zone1
            self.assertEqual(rows[0][3], 80.0)   # zone2
            self.assertEqual(rows[0][4], 60.0)   # zone3
        finally:
            logger.close()

    def test_latest_run_id(self):
        logger = RunLogger(self.db_path)
        try:
            first = logger.start_run(profile())
            second = logger.start_run(profile())
            self.assertEqual(logger.latest_run_id(), second)
            self.assertNotEqual(first, second)
        finally:
            logger.close()

    def test_csv_export(self):
        logger = RunLogger(self.db_path)
        try:
            run_id = logger.start_run(profile())
            logger.log_sample(
                run_id,
                SamplePoint(1234.0, 1.0, (50.0, 40.0, 30.0), None, 50.0, 590.0,
                            OvenPhase.RAMPING, "", False),
            )
            target = Path(self.temp_dir.name) / "out.csv"
            export_samples_csv(logger.get_samples(run_id), target)
            with open(target, encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[0][2], "zone1_c")
            self.assertEqual(rows[1][2], "50.0")
            self.assertEqual(rows[1][5], "")  # empty current exported as blank
        finally:
            logger.close()


if __name__ == "__main__":
    unittest.main()
