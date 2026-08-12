"""Tests for domain models, validation, and thermal calculations."""

import unittest

from asc_oven_control.domain.calculations import ThermalModel, evaluate_alarm, ramp_step
from asc_oven_control.domain.models import (
    Atmosphere,
    DomainValidationError,
    PidParameters,
    RunProfile,
)


def good_profile(**overrides) -> RunProfile:
    values = dict(
        operator="Tester",
        batch_id="batch-1",
        sample_id="sample-1",
        user_name="lab-user",
        atmosphere=Atmosphere.AIR,
        target_setpoint_c=590.0,
        ramp_rate_c_per_min=20.0,
        soak_time_sec=600.0,
        alarm_high_c=1200.0,
        alarm_low_c=10.0,
    )
    values.update(overrides)
    return RunProfile(**values)


class RunProfileTest(unittest.TestCase):
    def test_valid_profile(self):
        profile = good_profile()
        self.assertEqual(str(profile.atmosphere), "Air")

    def test_operator_required(self):
        with self.assertRaises(DomainValidationError) as context:
            good_profile(operator="   ")
        self.assertIn("operator", context.exception.errors)

    def test_alarm_ordering(self):
        with self.assertRaises(DomainValidationError) as context:
            good_profile(alarm_low_c=800.0, alarm_high_c=590.0)
        self.assertIn("alarm_low_c", context.exception.errors)

    def test_nonnegative_recipe(self):
        with self.assertRaises(DomainValidationError) as context:
            good_profile(ramp_rate_c_per_min=-1.0)
        self.assertIn("ramp_rate_c_per_min", context.exception.errors)

    def test_atmosphere_enum(self):
        for atmosphere in Atmosphere:
            profile = good_profile(atmosphere=atmosphere)
            self.assertEqual(profile.atmosphere, atmosphere)

    def test_json_round_trip(self):
        profile = good_profile(
            field_enabled=True, field_amplitude_uT=50.0, pid=PidParameters(prop_band=100.0)
        )
        restored = RunProfile.from_dict(profile.to_dict())
        self.assertEqual(profile, restored)

    def test_unknown_field_rejected(self):
        with self.assertRaises(DomainValidationError):
            RunProfile.from_dict({**good_profile().to_dict(), "surprise": 1})

    def test_field_amplitude_nonnegative(self):
        with self.assertRaises(DomainValidationError):
            good_profile(field_amplitude_uT=-5.0)

    def test_pid_values_optional(self):
        # Hardware values stay None instead of unsafe assumptions.
        profile = good_profile()
        self.assertIsNone(profile.pid.prop_band)


class CalculationsTest(unittest.TestCase):
    def test_ramp_step_clamps_rate(self):
        self.assertAlmostEqual(ramp_step(100.0, 200.0, 60.0, 1.0), 101.0)
        self.assertAlmostEqual(ramp_step(100.0, 110.0, 60.0, 1.0), 101.0)

    def test_ramp_step_zero_rate_jumps(self):
        self.assertEqual(ramp_step(100.0, 200.0, 0.0, 1.0), 200.0)

    def test_ramp_step_never_overshoots(self):
        self.assertEqual(ramp_step(100.0, 100.5, 60.0, 1.0), 100.5)

    def test_alarm_high(self):
        self.assertTrue(evaluate_alarm((1201.0, 25.0, 25.0), 1200.0, 10.0))

    def test_alarm_low(self):
        self.assertTrue(evaluate_alarm((25.0, 9.0, 25.0), 1200.0, 10.0))

    def test_no_alarm(self):
        self.assertEqual(evaluate_alarm((100.0, 80.0, 90.0), 1200.0, 10.0), "")

    def test_thermal_model_deterministic(self):
        first = ThermalModel()
        second = ThermalModel()
        for _ in range(10):
            first.update(590.0, 0.5)
            second.update(590.0, 0.5)
        self.assertEqual(first.zones, second.zones)

    def test_thermal_model_zone_order(self):
        # Zone 1 (sample zone) must lead the outer zones on a heating ramp.
        model = ThermalModel()
        for _ in range(240):
            model.update(590.0, 0.5)
        z1, z2, z3 = model.zones
        self.assertGreater(z1, z2)
        self.assertGreater(z2, z3)

    def test_thermal_model_converges(self):
        model = ThermalModel()
        for _ in range(4000):
            model.update(590.0, 0.5)
        z1, z2, z3 = model.zones
        self.assertGreater(z1, 585.0)
        self.assertGreater(z2, 580.0)
        self.assertGreater(z3, 575.0)

    def test_field_adds_heat(self):
        no_field = ThermalModel()
        with_field = ThermalModel()
        for _ in range(240):
            no_field.update(590.0, 0.5)
            with_field.update(590.0, 0.5, field_enabled=True, field_amplitude_uT=100.0)
        self.assertGreater(with_field.zones[0], no_field.zones[0])


if __name__ == "__main__":
    unittest.main()
