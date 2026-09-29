"""Coordinated three-zone setpoint control that minimizes the zone gradient.

Each zone has its own Watlow Series 96 running a closed PID loop on its own
thermocouple; those loops are fast, independent and keep running even if the
PC stops talking. This module is the slow supervisory layer on top: every
poll it decides the setpoint each controller should hold.

The 2009 records show the failure modes of commanding all three zones with
one shared setpoint (``Labview/Testing/test36_full_590deg``): Zone 1 runs
8-12 C ahead of Zones 2/3 for the whole ramp, and at the end of the ramp
Zone 2 overshoots and briefly leads. The strategy here addresses both:

1. **Guaranteed ramp.** One master ramp setpoint advances at the profile
   rate, but only while every zone is within ``hold_band_c`` of it. A
   lagging zone pauses the ramp instead of being left behind.
2. **Leader limiting.** No zone's setpoint may exceed the coldest zone by
   more than ``max_gradient_c``. The lagging zone keeps full drive (its
   setpoint is the master) while leading zones are throttled until it
   catches up. This bounds the gradient directly.
3. **Approach deceleration.** Within ``approach_band_c`` of the target the
   ramp slows to ``approach_rate_fraction`` of the profile rate, so the PID
   loops arrive with little stored heat and do not overshoot.
4. **Guaranteed soak.** The soak clock runs only while every zone is within
   ``soak_band_c`` of the target.

Optional ``zone_offsets_c`` add a fixed per-zone trim to compensate for
known thermocouple placement errors found during commissioning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from asc_oven_control.domain.calculations import clamp


class ZonePhase:
    RAMPING = "Ramping"
    HOLDING = "Holding"  # ramp paused for a lagging zone
    APPROACH = "Approach"
    SETTLING = "Settling"  # master at target, zones not yet in band
    SOAKING = "Soaking"
    COMPLETE = "Complete"


@dataclass(frozen=True, slots=True)
class GradientSettings:
    """Tuning of the supervisory layer (not the Watlow PID terms)."""

    # The hold band must exceed the PID loops' own ramp-tracking lag (a
    # proportional-dominant loop sits PB x power% behind a moving setpoint),
    # otherwise the ramp stalls. It guards against the whole oven falling
    # behind (e.g. heater power off); leader limiting handles the gradient.
    hold_band_c: float = 30.0
    max_gradient_c: float = 3.0
    approach_band_c: float = 25.0
    approach_rate_fraction: float = 0.35
    soak_band_c: float = 2.0
    zone_offsets_c: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # True: the soak clock pauses whenever a zone leaves the band.
    # False: the soak starts once all zones are in band and then runs
    # continuously; excursions are counted in ``out_of_band_s``.
    strict_soak: bool = True
    # Setpoint trim (outer integral): once the ramp is at the target, each
    # zone's setpoint is raised by trim_i, which grows at trim_rate_per_min
    # degrees per minute per degree of remaining error. It removes the
    # steady offset of a proportional-dominant or wound-up controller loop
    # without touching the controller's own PID. 0 disables it.
    trim_rate_per_min: float = 0.0
    trim_limit_c: float = 20.0

    def __post_init__(self) -> None:
        for name in ("hold_band_c", "max_gradient_c", "soak_band_c"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        if self.approach_band_c < 0:
            raise ValueError("approach_band_c must be >= 0")
        if not 0 < self.approach_rate_fraction <= 1:
            raise ValueError("approach_rate_fraction must be in (0, 1]")
        if len(self.zone_offsets_c) != 3:
            raise ValueError("zone_offsets_c needs three values")
        if self.trim_rate_per_min < 0 or self.trim_limit_c < 0:
            raise ValueError("trim settings must be >= 0")


@dataclass(slots=True)
class ControlDecision:
    """Output of one supervisory step."""

    master_setpoint_c: float
    zone_setpoints_c: tuple[float, float, float]
    phase: str
    gradient_c: float
    soak_elapsed_s: float
    held_by: int | None = None  # zone index that paused the ramp
    limited: tuple[bool, bool, bool] = (False, False, False)


@dataclass(slots=True)
class ZoneCoordinator:
    """Stateful supervisor; call ``step`` once per poll with fresh readings."""

    target_c: float
    ramp_rate_c_per_min: float
    soak_time_s: float
    settings: GradientSettings = field(default_factory=GradientSettings)
    master_c: float | None = None
    soak_elapsed_s: float = 0.0
    out_of_band_s: float = 0.0
    phase: str = ZonePhase.RAMPING
    holding: bool = False
    trims_c: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])

    # A hold releases once the lag is back under this fraction of the band,
    # so the ramp does not toggle on and off at the band edge.
    HOLD_RELEASE_FRACTION = 0.7

    def retarget(self, target_c: float) -> None:
        self.target_c = float(target_c)
        if self.phase in (ZonePhase.SOAKING, ZonePhase.SETTLING, ZonePhase.COMPLETE):
            self.phase = ZonePhase.RAMPING
            self.soak_elapsed_s = 0.0
            self.out_of_band_s = 0.0
        self.trims_c = [0.0, 0.0, 0.0]

    def step(self, zones_c: tuple[float, float, float], dt_s: float) -> ControlDecision:
        s = self.settings
        zones = tuple(float(z) for z in zones_c)
        coldest, hottest = min(zones), max(zones)
        gradient = hottest - coldest
        heating = self.target_c >= (self.master_c if self.master_c is not None else coldest)

        if self.master_c is None:
            # Start the ramp from the zone furthest from the target so no
            # zone is commanded to jump ahead of the others.
            self.master_c = coldest if heating else hottest

        held_by = None
        if self.phase not in (ZonePhase.SOAKING, ZonePhase.COMPLETE):
            remaining = self.target_c - self.master_c
            if abs(remaining) > 1e-9:
                lags = [
                    (self.master_c - z) if heating else (z - self.master_c) for z in zones
                ]
                worst = max(range(3), key=lambda i: lags[i])
                if lags[worst] > s.hold_band_c:
                    self.holding = True
                elif lags[worst] < s.hold_band_c * self.HOLD_RELEASE_FRACTION:
                    self.holding = False
                if self.holding:
                    held_by = worst
                    self.phase = ZonePhase.HOLDING
                else:
                    rate = self.ramp_rate_c_per_min
                    near = abs(remaining) <= s.approach_band_c
                    if near:
                        rate *= s.approach_rate_fraction
                    self.phase = ZonePhase.APPROACH if near else ZonePhase.RAMPING
                    if rate <= 0:
                        self.master_c = self.target_c
                    else:
                        step = rate / 60.0 * dt_s
                        self.master_c += clamp(remaining, -step, step)
            if abs(self.target_c - self.master_c) <= 1e-9:
                self.master_c = self.target_c
                in_band = all(abs(z - self.target_c) <= s.soak_band_c for z in zones)
                self.phase = ZonePhase.SOAKING if in_band else ZonePhase.SETTLING

        if self.phase == ZonePhase.SOAKING:
            in_band = all(abs(z - self.target_c) <= s.soak_band_c for z in zones)
            if in_band or not s.strict_soak:
                self.soak_elapsed_s += dt_s
            if not in_band:
                self.out_of_band_s += dt_s
            if self.soak_elapsed_s >= self.soak_time_s:
                self.phase = ZonePhase.COMPLETE

        if self.phase in (ZonePhase.SETTLING, ZonePhase.SOAKING) and s.trim_rate_per_min > 0:
            self._update_trims(zones, heating, dt_s)

        setpoints, limited = self._zone_setpoints(zones, heating)
        return ControlDecision(
            master_setpoint_c=self.master_c,
            zone_setpoints_c=setpoints,
            phase=self.phase,
            gradient_c=gradient,
            soak_elapsed_s=self.soak_elapsed_s,
            held_by=held_by,
            limited=limited,
        )

    def _effective_targets(self, zones: tuple[float, ...], heating: bool) -> list[float]:
        """Where each zone should sit: the target, or the gradient cap if lower."""
        s = self.settings
        if heating:
            trailing = min(zones)
            return [
                self.target_c if z <= trailing else min(self.target_c, trailing + s.max_gradient_c)
                for z in zones
            ]
        trailing = max(zones)
        return [
            self.target_c if z >= trailing else max(self.target_c, trailing - s.max_gradient_c)
            for z in zones
        ]

    def _update_trims(self, zones: tuple[float, ...], heating: bool, dt_s: float) -> None:
        s = self.settings
        for index, (zone, goal) in enumerate(zip(zones, self._effective_targets(zones, heating))):
            trim = self.trims_c[index] + s.trim_rate_per_min / 60.0 * (goal - zone) * dt_s
            self.trims_c[index] = clamp(trim, -s.trim_limit_c, s.trim_limit_c)

    def _zone_setpoints(
        self, zones: tuple[float, ...], heating: bool
    ) -> tuple[tuple[float, float, float], tuple[bool, bool, bool]]:
        s = self.settings
        master = self.master_c
        setpoints = []
        limited = []
        # The trailing zone (coldest when heating) is never capped: it keeps
        # the full master setpoint so it gets maximum drive to catch up.
        # Offsets and trims shift both the wanted setpoint and the cap, since
        # the cap is a limit on the zone's temperature, not its setpoint.
        if heating:
            trailing = min(zones)
            ceiling = trailing + s.max_gradient_c
            for index in range(3):
                shift = s.zone_offsets_c[index] + self.trims_c[index]
                wanted = master + shift
                capped = wanted if zones[index] <= trailing else min(wanted, ceiling + shift)
                setpoints.append(capped)
                limited.append(capped < wanted)
        else:
            trailing = max(zones)
            floor = trailing - s.max_gradient_c
            for index in range(3):
                shift = s.zone_offsets_c[index] + self.trims_c[index]
                wanted = master + shift
                capped = wanted if zones[index] >= trailing else max(wanted, floor + shift)
                setpoints.append(capped)
                limited.append(capped > wanted)
        return tuple(setpoints), tuple(limited)
