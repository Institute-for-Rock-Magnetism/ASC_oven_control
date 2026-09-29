"""Physics-based three-zone oven plant with Watlow-style PID per zone.

Used by simulation mode and by the tests that compare control strategies.
Each zone is a lumped thermal mass with its own heater, convective and
radiative loss to ambient, and conductive coupling to Zone 1 (the sample
zone sits between the two outer zones). With the controllers' actual PID
settings (``ASC_2026_PIDS``) the naive "same setpoint everywhere" strategy
reproduces the character of the 2009 records: at 5 C/min Zone 1 leads by
10-15 C and the zones overshoot 5-7 C at the top of the ramp. The thermal
parameters are NOT measured values for the real oven; refine them from a
logged hardware run.

``WatlowPid`` mirrors the Series 96 parameterization: proportional band in
degrees, reset (integral) in repeats per minute, rate (derivative) in
minutes, output clamped to 0-100 % with anti-windup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from asc_oven_control.domain.calculations import clamp

STEFAN_BOLTZMANN = 5.670e-8
KELVIN = 273.15


@dataclass(slots=True)
class WatlowPid:
    """Discrete PID with Watlow units (PB degrees, reset repeats/min, rate min)."""

    prop_band_c: float = 25.0
    reset_per_min: float = 0.6
    rate_min: float = 0.1
    dead_band_c: float = 0.0
    # Input filter ahead of the derivative: the Series 96 reports whole
    # degrees, and differentiating raw 1-degree steps makes the output chatter.
    filter_s: float = 8.0
    integral: float = 0.0
    _filtered_pv: float | None = None

    def update(self, setpoint: float, pv: float, dt_s: float) -> float:
        error = setpoint - pv
        if abs(error) <= self.dead_band_c:
            error = 0.0
        derivative = 0.0
        if self._filtered_pv is None:
            self._filtered_pv = pv
        elif dt_s > 0:
            alpha = dt_s / (self.filter_s + dt_s)
            previous = self._filtered_pv
            self._filtered_pv += alpha * (pv - previous)
            # Derivative on measurement avoids kicks when the setpoint moves.
            derivative = -(self._filtered_pv - previous) / dt_s * self.rate_min * 60.0
        gain = 100.0 / self.prop_band_c
        candidate = self.integral + error * self.reset_per_min / 60.0 * dt_s
        output = gain * (error + candidate + derivative)
        # Conditional integration: stop integrating into saturation.
        if 0.0 < output < 100.0 or (output >= 100.0 and error < 0) or (output <= 0.0 and error > 0):
            self.integral = candidate
        return clamp(gain * (error + self.integral + derivative), 0.0, 100.0)


@dataclass(frozen=True, slots=True)
class ZoneParameters:
    capacity_j_per_k: float
    heater_w: float
    loss_w_per_k: float
    emissive_area_m2: float


DEFAULT_ZONES = (
    # Zones 1 and 3: outer zones (end caps), lossy.
    ZoneParameters(capacity_j_per_k=1500.0, heater_w=1000.0, loss_w_per_k=0.9, emissive_area_m2=0.006),
    # Zone 2: middle zone. Gains heat from both neighbours and sheds little of
    # its own (first heated run: 116 C at 0 % output with Zones 1/3 at 100 C).
    ZoneParameters(capacity_j_per_k=2600.0, heater_w=1200.0, loss_w_per_k=0.6, emissive_area_m2=0.004),
    ZoneParameters(capacity_j_per_k=1500.0, heater_w=1000.0, loss_w_per_k=0.9, emissive_area_m2=0.006),
)
# Conductance Zone 2 <-> Zone 1 and Zone 2 <-> Zone 3 (Zone 2 is in the middle).
COUPLING_W_PER_K = (1.2, 0.0, 1.2)
# Heater elements have their own mass: power heats the element, which heats
# the zone with a lag of minutes (Zone 3 kept rising 10+ C/min at 0 % output
# after a burst). Outer elements also warm the middle zone directly.
# Fitted (2026-09-29) to two real data points: the 100 C run (Zone 2 peaked
# ~16 C high, Zones 1/3 ~10 C) and the 2009 590 C shared-set-point run
# (zones settle within ~2 C). Zone 2's element is heavy and slow; a small
# share of the outer elements' heat goes straight into the middle zone.
ELEMENT_CAPACITY_J_PER_K = 600.0
ELEMENT_TO_ZONE_W_PER_K = 4.0
MIDDLE_ELEMENT_CAPACITY_J_PER_K = 1500.0
MIDDLE_ELEMENT_TO_ZONE_W_PER_K = 2.0
OUTER_ELEMENT_TO_MIDDLE_FRACTION = 0.1

# PID set 1 as read from the ASC oven's controllers on 2026-09-29 (SI
# units): prop band 47/65/47 C, integral 12.5/60/12.5 min/repeat,
# derivative 0.90/2.25/0.90 min. The wide band and very long integral make
# each zone sit PB x power% behind a moving setpoint, which is the source of
# the ramp gradient in the 2009 records.
ASC_2026_PIDS = ((47.0, 12.5, 0.90), (65.0, 60.0, 2.25), (47.0, 12.5, 0.90))


def asc_pids() -> list["WatlowPid"]:
    return [WatlowPid(band, 1.0 / integral, derivative) for band, integral, derivative in ASC_2026_PIDS]


@dataclass(slots=True)
class OvenPlant:
    """Integrates the three zones; ``step`` takes per-zone setpoints."""

    ambient_c: float = 22.0
    zones: list[ZoneParameters] = field(default_factory=lambda: list(DEFAULT_ZONES))
    pids: list[WatlowPid] = field(default_factory=asc_pids)
    heater_enabled: bool = True
    quantize: bool = True
    temps_c: list[float] = field(default_factory=list)
    element_c: list[float] = field(default_factory=list)
    power_pct: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    substep_s: float = 0.25
    outer_to_middle_fraction: float = OUTER_ELEMENT_TO_MIDDLE_FRACTION
    element_capacity: list[float] = field(
        default_factory=lambda: [ELEMENT_CAPACITY_J_PER_K, MIDDLE_ELEMENT_CAPACITY_J_PER_K, ELEMENT_CAPACITY_J_PER_K]
    )
    element_conductance: list[float] = field(
        default_factory=lambda: [ELEMENT_TO_ZONE_W_PER_K, MIDDLE_ELEMENT_TO_ZONE_W_PER_K, ELEMENT_TO_ZONE_W_PER_K]
    )

    def set_uniform(self, temperature_c: float) -> None:
        """Start from thermal equilibrium at ``temperature_c`` (zones and elements)."""
        self.temps_c = [float(temperature_c)] * 3
        self.element_c = [float(temperature_c)] * 3

    def __post_init__(self) -> None:
        if not self.temps_c:
            self.temps_c = [self.ambient_c] * 3
        if not self.element_c:
            self.element_c = list(self.temps_c)

    def readings(self) -> tuple[float, float, float]:
        """What the controllers report (whole degrees on the real Series 96)."""
        if self.quantize:
            return tuple(float(round(t)) for t in self.temps_c)
        return tuple(self.temps_c)

    def step(self, setpoints_c: tuple[float, float, float], dt_s: float) -> None:
        remaining = dt_s
        while remaining > 1e-9:
            h = min(self.substep_s, remaining)
            pv = self.readings()
            self.power_pct = [
                pid.update(sp, value, h) for pid, sp, value in zip(self.pids, setpoints_c, pv)
            ]
            self._integrate(h)
            remaining -= h

    def _integrate(self, h: float) -> None:
        t = self.temps_c
        e = self.element_c
        ambient_k = self.ambient_c + KELVIN
        flows = [0.0, 0.0, 0.0]
        for i in (0, 2):
            q = COUPLING_W_PER_K[i] * (t[1] - t[i])
            flows[1] -= q
            flows[i] += q
        new_elements = []
        for i, zone in enumerate(self.zones):
            heater = zone.heater_w * self.power_pct[i] / 100.0 if self.heater_enabled else 0.0
            to_zone = self.element_conductance[i] * (e[i] - t[i])
            if i == 1:
                flows[1] += to_zone
            else:
                flows[i] += to_zone * (1.0 - self.outer_to_middle_fraction)
                flows[1] += to_zone * self.outer_to_middle_fraction
            new_elements.append(e[i] + (heater - to_zone) / self.element_capacity[i] * h)
        new = []
        for i, zone in enumerate(self.zones):
            convective = zone.loss_w_per_k * (t[i] - self.ambient_c)
            tk = t[i] + KELVIN
            radiative = STEFAN_BOLTZMANN * zone.emissive_area_m2 * (tk**4 - ambient_k**4)
            net = -convective - radiative + flows[i]
            new.append(t[i] + net / zone.capacity_j_per_k * h)
        self.temps_c = new
        self.element_c = new_elements
