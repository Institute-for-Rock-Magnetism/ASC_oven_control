"""Supervisory gradient control: unit behavior and closed-loop comparison."""

import unittest

from asc_oven_control.domain.calculations import ramp_step
from asc_oven_control.domain.oven_plant import OvenPlant, WatlowPid
from asc_oven_control.domain.zone_control import GradientSettings, ZoneCoordinator, ZonePhase


class CoordinatorUnitTest(unittest.TestCase):
    def test_ramp_starts_at_coldest_zone_and_advances(self):
        c = ZoneCoordinator(target_c=500.0, ramp_rate_c_per_min=10.0, soak_time_s=60.0)
        d = c.step((25.0, 22.0, 23.0), 6.0)
        self.assertAlmostEqual(d.master_setpoint_c, 23.0)  # 22 + 10 C/min * 6 s
        self.assertEqual(d.phase, ZonePhase.RAMPING)

    def test_lagging_zone_holds_ramp(self):
        c = ZoneCoordinator(500.0, 10.0, 60.0, GradientSettings(hold_band_c=5.0))
        c.master_c = 200.0
        d = c.step((199.0, 190.0, 198.0), 6.0)
        self.assertEqual(d.phase, ZonePhase.HOLDING)
        self.assertEqual(d.held_by, 1)
        self.assertEqual(d.master_setpoint_c, 200.0)

    def test_leading_zone_is_capped_but_lagging_zone_keeps_full_drive(self):
        c = ZoneCoordinator(500.0, 10.0, 60.0, GradientSettings(max_gradient_c=3.0, hold_band_c=20.0))
        c.master_c = 210.0
        d = c.step((208.0, 196.0, 200.0), 1.0)
        z1, z2, z3 = d.zone_setpoints_c
        self.assertAlmostEqual(z1, 199.0)  # coldest 196 + 3
        self.assertAlmostEqual(z3, 199.0)
        self.assertGreater(z2, 210.0)  # the trailing zone follows the master
        self.assertEqual(d.limited, (True, False, True))
        # Once the gradient closes the caps release.
        d = c.step((209.0, 209.0, 209.0), 1.0)
        self.assertEqual(d.limited, (False, False, False))

    def test_approach_slows_ramp(self):
        s = GradientSettings(approach_band_c=20.0, approach_rate_fraction=0.5)
        c = ZoneCoordinator(500.0, 12.0, 60.0, s)
        c.master_c = 490.0
        d = c.step((490.0, 490.0, 490.0), 10.0)
        self.assertEqual(d.phase, ZonePhase.APPROACH)
        self.assertAlmostEqual(d.master_setpoint_c, 491.0)  # 6 C/min for 10 s

    def test_soak_clock_runs_only_in_band(self):
        c = ZoneCoordinator(500.0, 10.0, 30.0, GradientSettings(soak_band_c=2.0))
        c.master_c = 500.0
        d = c.step((500.0, 495.0, 500.0), 10.0)
        self.assertEqual(d.phase, ZonePhase.SETTLING)
        self.assertEqual(d.soak_elapsed_s, 0.0)
        for _ in range(2):
            d = c.step((500.0, 499.0, 501.0), 10.0)
        self.assertEqual(d.phase, ZonePhase.SOAKING)
        self.assertEqual(d.soak_elapsed_s, 20.0)
        d = c.step((500.0, 490.0, 500.0), 10.0)  # excursion: clock pauses
        self.assertEqual(d.soak_elapsed_s, 20.0)
        d = c.step((500.0, 500.0, 500.0), 10.0)
        self.assertEqual(d.phase, ZonePhase.COMPLETE)

    def test_continuous_soak_counts_excursions(self):
        c = ZoneCoordinator(100.0, 10.0, 30.0, GradientSettings(soak_band_c=1.0, strict_soak=False))
        c.master_c = 100.0
        d = c.step((100.0, 100.0, 101.0), 10.0)
        self.assertEqual(d.phase, ZonePhase.SOAKING)
        d = c.step((100.0, 98.0, 100.0), 10.0)  # excursion: clock keeps running
        self.assertEqual(d.soak_elapsed_s, 20.0)
        self.assertEqual(c.out_of_band_s, 10.0)
        d = c.step((100.0, 100.0, 100.0), 10.0)
        self.assertEqual(d.phase, ZonePhase.COMPLETE)

    def test_retarget_restarts_ramp_from_soak(self):
        c = ZoneCoordinator(100.0, 10.0, 600.0)
        c.master_c = 100.0
        c.step((100.0, 100.0, 100.0), 1.0)
        c.retarget(150.0)
        d = c.step((100.0, 100.0, 100.0), 6.0)
        self.assertEqual(d.phase, ZonePhase.RAMPING)
        self.assertAlmostEqual(d.master_setpoint_c, 101.0)

    def test_offsets_apply_per_zone(self):
        s = GradientSettings(zone_offsets_c=(0.0, 2.0, -1.0), max_gradient_c=50.0)
        c = ZoneCoordinator(300.0, 0.0, 60.0, s)
        c.master_c = 300.0
        d = c.step((299.0, 299.0, 299.0), 1.0)
        self.assertEqual(d.zone_setpoints_c, (300.0, 302.0, 299.0))

    def test_invalid_settings_rejected(self):
        for bad in ({"hold_band_c": 0}, {"approach_rate_fraction": 0}, {"zone_offsets_c": (0.0,)}):
            with self.assertRaises(ValueError):
                GradientSettings(**bad)


def simulate(strategy, target=590.0, rate=10.0, minutes=140, dt=2.0):
    """Closed loop on the physics plant; returns (max ramp gradient, max overshoot)."""
    plant = OvenPlant()
    coordinator = ZoneCoordinator(target, rate, 1800.0)
    master = plant.ambient_c
    worst_gradient = 0.0
    worst_overshoot = 0.0
    for _ in range(int(minutes * 60 / dt)):
        pv = plant.readings()
        if strategy == "shared":
            master = ramp_step(master, target, rate, dt)
            setpoints = (master,) * 3
        else:
            setpoints = coordinator.step(pv, dt).zone_setpoints_c
        plant.step(tuple(float(round(sp)) for sp in setpoints), dt)
        pv = plant.readings()
        worst_gradient = max(worst_gradient, max(pv) - min(pv))
        worst_overshoot = max(worst_overshoot, max(pv) - target)
    return worst_gradient, worst_overshoot, plant.readings()


class ClosedLoopTest(unittest.TestCase):
    def test_pid_holds_setpoint_without_chatter(self):
        plant = OvenPlant(pids=[WatlowPid(15.0, 0.3, 0.3) for _ in range(3)])
        plant.temps_c = [300.0, 300.0, 300.0]
        for _ in range(900):
            plant.step((300.0, 300.0, 300.0), 2.0)
        for value in plant.readings():
            self.assertAlmostEqual(value, 300.0, delta=1.0)
        # Whole-degree readings must not make the output bang between 0/100.
        powers = []
        for _ in range(60):
            plant.step((300.0, 300.0, 300.0), 2.0)
            powers.append(plant.power_pct[0])
        self.assertLess(max(powers) - min(powers), 40.0)

    def test_coordinated_control_reduces_gradient_and_overshoot(self):
        # Uses the PID settings read from the oven (the plant default).
        shared_gradient, shared_overshoot, _ = simulate("shared", rate=5.0)
        coordinated_gradient, coordinated_overshoot, final = simulate("coordinated", rate=5.0)
        self.assertLess(coordinated_gradient, shared_gradient / 2)
        self.assertLess(coordinated_overshoot, shared_overshoot)
        for value in final:
            self.assertAlmostEqual(value, 590.0, delta=3.0)

    def test_heater_off_holds_the_ramp(self):
        # Coil power off (as during commissioning): the master must not run
        # away to the target, or the oven would jump when power returns.
        plant = OvenPlant(heater_enabled=False)
        coordinator = ZoneCoordinator(590.0, 10.0, 60.0)
        for _ in range(600):
            decision = coordinator.step(plant.readings(), 2.0)
            plant.step(decision.zone_setpoints_c, 2.0)
        self.assertEqual(decision.phase, ZonePhase.HOLDING)
        self.assertLessEqual(decision.master_setpoint_c, plant.ambient_c + 31.0)

    def test_watlow_pid_output_is_clamped(self):
        pid = WatlowPid(prop_band_c=10.0, reset_per_min=1.0, rate_min=0.0)
        self.assertEqual(pid.update(500.0, 20.0, 1.0), 100.0)
        self.assertEqual(pid.update(20.0, 500.0, 1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
