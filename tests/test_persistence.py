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

    def test_detail_columns_logged_and_old_database_migrated(self):
        import sqlite3

        from asc_oven_control.infrastructure.persistence import RUNS_SCHEMA, SAMPLES_SCHEMA

        legacy = sqlite3.connect(str(self.db_path))
        legacy.executescript(RUNS_SCHEMA + SAMPLES_SCHEMA)  # pre-migration schema
        legacy.close()
        logger = RunLogger(self.db_path)
        try:
            run_id = logger.start_run(profile())
            logger.log_sample(
                run_id,
                SamplePoint(1.0, 2.0, (100.0, 99.0, 98.0), None, 101.0, 590.0, OvenPhase.RAMPING, "", True,
                            zone_setpoints_c=(101.0, 103.0, 102.0), zone_power_pct=(40.0, 55.5, 60.0)),
            )
            row = logger.get_detailed_samples(run_id)[0]
            self.assertEqual(row[11:], (101.0, 103.0, 102.0, 40.0, 55.5, 60.0))
            self.assertEqual(len(logger.get_samples(run_id)[0]), 11)
        finally:
            logger.close()

    def test_live_csv_is_flushed_per_row(self):
        from asc_oven_control.infrastructure.persistence import LIVE_CSV_COLUMNS, LiveCsvLog

        path = Path(self.temp_dir.name) / "runs" / "run.csv"
        log = LiveCsvLog(path, ["ASC oven run 1"])
        snapshot = {
            "timestamp": 1000.0, "elapsed_sec": 90.0, "zones": (99.0, 100.0, 101.0),
            "zone_setpoints": (100.0, 100.0, 100.0), "zone_power": (40.0, None, 12.5),
            "output_setpoint_c": 100.0, "target_setpoint_c": 100.0, "gradient_c": 2.0,
            "phase": "Soaking", "control_phase": "Soaking", "soak_elapsed_s": 30.0,
            "out_of_band_s": 4.0, "alarm": "",
        }
        log.write_snapshot(snapshot)
        # Readable before close: a crash mid-run must not lose rows.
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "# ASC oven run 1")
        rows = list(csv.reader(lines[1:]))
        self.assertEqual(tuple(rows[0]), LIVE_CSV_COLUMNS)
        self.assertEqual(rows[1][1], "1.500")
        self.assertEqual(rows[1][2:5], ["99.0", "100.0", "101.0"])
        log.close()

    def test_orphaned_running_runs_marked_interrupted(self):
        logger = RunLogger(self.db_path)
        try:
            orphan = logger.start_run(profile())
            live = logger.start_run(profile())
            self.assertEqual(logger.mark_interrupted(keep_run_id=live), [orphan])
            statuses = dict(logger.conn.execute("SELECT id, status FROM runs").fetchall())
            self.assertEqual(statuses, {orphan: "interrupted", live: "running"})
        finally:
            logger.close()

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
