"""Thermal calculations for the ASC oven.

``ramp_step`` and ``evaluate_alarm`` are plain math used by the run engine.
``ThermalModel`` is the deterministic three-zone simulation: Zone 1 (the
sample zone) responds fastest to the commanded setpoint while Zones 2 and 3
follow with a lag, mirroring the 2009 test records
(``Labview/Testing/test36_full_590deg``) where Zone 1 leads and the outer
zones trail it. The model is deliberately deterministic so tests and replays
are reproducible.
"""

from __future__ import annotations

import math

# Time constants (seconds) for the first-order zone responses. These are
# simulation parameters chosen to match the shape of the 2009 records; they
# are NOT recovered controller constants and must not be used for hardware.
ZONE_1_TAU_S = 140.0
ZONE_2_TAU_S = 300.0
ZONE_3_TAU_S = 430.0

# Deterministic ripple on the simulated zones (amplitude, period seconds).
RIPPLE_AMPLITUDE_C = 0.12
RIPPLE_PERIOD_S = 37.0

# Field heating: an applied field adds a small steady heat term (deg C/s at
# 100 uT), scaled linearly with the amplitude.
FIELD_HEAT_SCALE_C_S_PER_100UT = 0.006


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def ramp_step(
    setpoint: float,
    target: float,
    rate_c_per_min: float,
    dt_sec: float,
) -> float:
    """Move ``setpoint`` toward ``target`` by at most ``rate`` * ``dt``.

    A rate of zero jumps straight to the target (instantaneous setpoint
    change), matching the manual-adjust behavior of the LabVIEW program.
    """
    step = rate_c_per_min / 60.0 * dt_sec
    difference = target - setpoint
    if step <= 0:
        return target
    return setpoint + clamp(difference, -step, step)


def evaluate_alarm(
    zone_temps_c: tuple[float, float, float],
    alarm_high_c: float,
    alarm_low_c: float,
) -> str:
    """Return an alarm message when any zone leaves the alarm window.

    The first violating zone is reported, scanning the sample zone first.
    """
    labels = ("Zone 1", "Zone 2", "Zone 3")
    for label, temperature in zip(labels, zone_temps_c):
        if temperature >= alarm_high_c:
            return f"{label} high: {temperature:.1f} °C"
        if temperature <= alarm_low_c:
            return f"{label} low: {temperature:.1f} °C"
    return ""


class ThermalModel:
    """Deterministic first-order three-zone oven simulation.

    Only used when the app runs in simulation mode; never constructed by a
    hardware adapter.
    """

    def __init__(self, initial_c: tuple[float, float, float] = (25.0, 25.0, 25.0)) -> None:
        self._zones = [float(value) for value in initial_c]
        self._current_a = 0.0
        self._elapsed = 0.0

    @property
    def zones(self) -> tuple[float, float, float]:
        return tuple(self._zones)

    @property
    def current_a(self) -> float:
        return self._current_a

    def reset(self, initial_c: tuple[float, float, float] = (25.0, 25.0, 25.0)) -> None:
        self._zones = [float(value) for value in initial_c]
        self._current_a = 0.0
        self._elapsed = 0.0

    def update(
        self,
        setpoint: float,
        dt_sec: float,
        field_enabled: bool = False,
        field_amplitude_uT: float = 0.0,
    ) -> None:
        """Advance the model by ``dt_sec`` toward ``setpoint``."""
        self._elapsed += dt_sec
        taus = (ZONE_1_TAU_S, ZONE_2_TAU_S, ZONE_3_TAU_S)
        field_heat = (
            FIELD_HEAT_SCALE_C_S_PER_100UT * max(field_amplitude_uT, 0.0) / 100.0
            if field_enabled
            else 0.0
        )
        ripple = RIPPLE_AMPLITUDE_C * math.sin(2.0 * math.pi * self._elapsed / RIPPLE_PERIOD_S)
        for index, tau in enumerate(taus):
            approach = (setpoint - self._zones[index]) * (1.0 - math.exp(-dt_sec / tau))
            self._zones[index] += approach + field_heat * dt_sec + ripple
            self._zones[index] = max(0.0, self._zones[index])
        # Heater current is proportional to the largest remaining demand.
        demand = max(setpoint - zone for zone in self._zones)
        span = max(setpoint, 100.0)
        self._current_a = 0.4 + 8.6 * clamp(demand / span, 0.0, 1.0)
