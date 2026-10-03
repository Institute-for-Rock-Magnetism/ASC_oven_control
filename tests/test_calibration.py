"""Zone offset calibration learned from run logs."""

import tempfile
import unittest
from pathlib import Path

from asc_oven_control.domain.calibration import (
    CalibrationError,
    HoldMeasurement,
    OffsetCalibration,
    measure_hold,
)
from asc_oven_control.infrastructure.persistence import LiveCsvLog


def write_run(path, target, zone_pvs, setpoints, trims=(0.0, 0.0, 0.0), hold_rows=200):
    log = LiveCsvLog(path, [f"hold_band=30C trims={trims} cap_depth=10C"])
    t = 0.0
    for i in range(20):  # ramp rows
        log.write_snapshot(_snap(t, (50.0, 50.0, 50.0), setpoints, target, "Ramping", "Ramping"))
        t += 2
    for i in range(hold_rows):
        log.write_snapshot(_snap(t, zone_pvs, setpoints, target, "Soaking", "Soaking"))
        t += 2
    for i in range(30):  # cooling rows are ignored
        log.write_snapshot(_snap(t, (80.0, 90.0, 80.0), (0.0, 0.0, 0.0), target, "Cooling", "Cooling"))
        t += 2
    log.close()


def _snap(t, zones, setpoints, target, phase, control_phase):
    return {
        "timestamp": 1000.0 + t, "elapsed_sec": t, "zones": zones, "zone_setpoints": setpoints,
        "zone_power": (5.0, 0.0, 5.0), "output_setpoint_c": target, "target_setpoint_c": target,
        "gradient_c": max(zones) - min(zones), "phase": phase, "control_phase": control_phase,
        "soak_elapsed_s": 0.0, "out_of_band_s": 0.0, "alarm": "",
    }


def measurement(run, target, typical, peak):
    return HoldMeasurement(run, target, (0.0, 0.0, 0.0), typical, peak, 10.0)


class MeasureHoldTest(unittest.TestCase):
    def test_excess_relative_to_each_zones_setpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-0011.csv"
            write_run(path, 300.0, (293.0, 305.0, 293.0), (290.0, 295.0, 290.0), trims=(-10.0, -5.0, -10.0))
            m = measure_hold(path)
        self.assertEqual(m.target_c, 300.0)
        self.assertEqual(m.offsets_c, (-10.0, -5.0, -10.0))
        self.assertEqual(m.typical_excess_c, (3.0, 10.0, 3.0))
        self.assertEqual(m.peak_excess_c, (3.0, 10.0, 3.0))

    def test_run_without_hold_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.csv"
            write_run(path, 100.0, (100.0, 100.0, 100.0), (100.0, 100.0, 100.0), hold_rows=10)
            with self.assertRaises(CalibrationError):
                measure_hold(path)

    def test_heat_cut_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.csv"
            log = LiveCsvLog(path, [])
            for i in range(150):
                log.write_snapshot(_snap(i * 2.0, (202.0, 207.0, 202.0), (195.0, 200.0, 195.0), 200.0, "Soaking", "Soaking"))
            cut = _snap(400.0, (150.0, 140.0, 120.0), (195.0, 200.0, 195.0), 200.0, "Soaking", "Soaking")
            cut["zone_power"] = (100.0, 100.0, 100.0)
            log.write_snapshot(cut)
            log.close()
            m = measure_hold(path)
        self.assertEqual(m.typical_excess_c, (7.0, 7.0, 7.0))


class OffsetCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.cal = OffsetCalibration(center_margin_c=1.0)
        self.cal.add(measurement("run-0003", 100.0, (1.0, 4.0, 1.0), (2.0, 5.0, 2.0)))
        self.cal.add(measurement("run-0008", 200.0, (4.0, 10.0, 4.0), (5.0, 11.0, 5.0)))
        self.cal.add(measurement("run-0009", 200.0, (3.0, 7.0, 3.0), (5.0, 7.0, 4.0)))
        self.cal.add(measurement("run-0011", 300.0, (4.0, 10.0, 4.0), (6.0, 10.0, 5.0)))

    def test_offsets_cancel_expected_excess_with_zone2_margin(self):
        self.assertEqual(self.cal.offsets_for(100.0), (-1.0, -6.0, -1.0))
        self.assertEqual(self.cal.offsets_for(300.0), (-4.0, -11.0, -4.0))

    def test_newer_runs_weigh_more_at_a_repeated_target(self):
        excess = self.cal.expected_excess(200.0)
        # Zone 2 peak: run-0008 11 (weight 1), run-0009 7 (weight 2) -> 25/3
        self.assertAlmostEqual(excess[1], 25.0 / 3.0)

    def test_interpolates_between_tested_targets(self):
        excess = self.cal.expected_excess(250.0)
        self.assertAlmostEqual(excess[1], (25.0 / 3.0 + 10.0) / 2.0)

    def test_extrapolates_the_trend_above_the_tested_range_with_a_cap(self):
        above = self.cal.expected_excess(400.0)
        self.assertGreater(above[1], 10.0)  # Zone 2 keeps growing: more correction, not less
        self.assertLessEqual(self.cal.expected_excess(2000.0)[1], 2 * 10.0)

    def test_round_trip_and_replace_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            self.cal.save(path)
            loaded = OffsetCalibration.load(path)
        self.assertEqual(loaded.offsets_for(300.0), self.cal.offsets_for(300.0))
        loaded.add(measurement("run-0011", 300.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
        self.assertEqual(len(loaded.measurements), 4)

    def test_empty_calibration(self):
        self.assertIsNone(OffsetCalibration().offsets_for(300.0))
        self.assertIsNone(OffsetCalibration.load(Path("missing-file.json")).offsets_for(100.0))


if __name__ == "__main__":
    unittest.main()
