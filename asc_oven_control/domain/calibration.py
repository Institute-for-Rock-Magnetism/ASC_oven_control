"""Zone offset calibration learned from completed runs.

During the hold each zone settles some distance from its own set point:
Zone 2, the middle zone, gains heat from Zones 1 and 3 and drifts above it,
while the outer zones sit a little above theirs. Measured on the ASC oven
(2026-09-30 .. 10-02): Zone 2 +4..5 C at 100 C, +7..10 C at 200 C, +10 C at
300 C (each relative to its set point).

``measure_hold`` extracts, from one run's live CSV, each zone's excess
(temperature minus its own set point) during the hold. ``OffsetCalibration``
keeps those measurements by target temperature and returns, for any new
target, the per-zone offsets that should put every zone *on* the target:
offset = -(expected excess). Zone 2 uses its highest excess plus a small
margin so its drift ends at, not past, the target. Targets between tested
temperatures are interpolated linearly; outside them the trend of the
nearest two points is continued, kept between 0 and twice the largest
measured excess. Every finished run adds its measurement (the app does
this automatically), so the table improves and extends as steps are run.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

CENTER_ZONE = 1


class CalibrationError(ValueError):
    """A run cannot be measured (no usable hold)."""


@dataclass(frozen=True, slots=True)
class HoldMeasurement:
    """Excess of each zone over its own set point during one run's hold."""

    run: str
    target_c: float
    offsets_c: tuple[float, float, float]  # offsets the run used
    typical_excess_c: tuple[float, float, float]  # median over the settled hold
    peak_excess_c: tuple[float, float, float]  # highest over the settled hold
    hold_minutes: float


def measure_hold(path: Path | str, settle_minutes: float = 3.0) -> HoldMeasurement:
    """Measure a run CSV written by the app (``run_logs/run-*.csv``).

    Uses the Soaking rows after the first ``settle_minutes`` (the arrival
    transient) and ignores rows where the heating was evidently cut (a zone
    more than 15 C below its set point while its output is high, e.g. the
    oven's onboard timer switched the heaters off before the hold ended).
    """
    path = Path(path)
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(line for line in handle if not line.startswith("#")))
    soak = [r for r in rows if r.get("phase") == "Soaking" and r.get("control_phase") == "Soaking"]
    if not soak:
        raise CalibrationError(f"{path.name}: no hold recorded")
    start = float(soak[0]["elapsed_min"])
    usable = []
    for r in soak:
        if float(r["elapsed_min"]) - start < settle_minutes:
            continue
        pv = [float(r[f"zone{i}_c"]) for i in (1, 2, 3)]
        sp = [float(r[f"zone{i}_sp_c"]) for i in (1, 2, 3)]
        powers = [float(r[f"zone{i}_power_pct"] or 0.0) for i in (1, 2, 3)]
        if any(s - p > 15.0 and w > 50.0 for p, s, w in zip(pv, sp, powers)):
            continue
        usable.append([p - s for p, s in zip(pv, sp)])
    if len(usable) < 30:  # about a minute at 2 s polls
        raise CalibrationError(f"{path.name}: hold too short to measure ({len(usable)} samples)")
    target = float(soak[0]["target_c"])
    offsets = _header_offsets(path) or (0.0, 0.0, 0.0)
    columns = list(zip(*usable))
    end = float(soak[-1]["elapsed_min"])
    return HoldMeasurement(
        run=path.name,
        target_c=target,
        offsets_c=offsets,
        typical_excess_c=tuple(round(statistics.median(c), 2) for c in columns),
        peak_excess_c=tuple(round(max(c), 2) for c in columns),
        hold_minutes=round(end - start, 1),
    )


def _header_offsets(path: Path) -> tuple[float, float, float] | None:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("#"):
                return None
            if "trims=(" in line:
                inside = line.split("trims=(", 1)[1].split(")", 1)[0]
                values = tuple(float(v) for v in inside.split(","))
                return values if len(values) == 3 else None
    return None


@dataclass(slots=True)
class OffsetCalibration:
    """Measured hold excesses by target, and the offsets they imply."""

    measurements: list[HoldMeasurement] = field(default_factory=list)
    center_margin_c: float = 1.0  # aim Zone 2 this much below the target

    # ---------------------------------------------------------- persistence

    @classmethod
    def load(cls, path: Path | str) -> "OffsetCalibration":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            measurements=[
                HoldMeasurement(
                    run=m["run"],
                    target_c=float(m["target_c"]),
                    offsets_c=tuple(m["offsets_c"]),
                    typical_excess_c=tuple(m["typical_excess_c"]),
                    peak_excess_c=tuple(m["peak_excess_c"]),
                    hold_minutes=float(m["hold_minutes"]),
                )
                for m in data.get("measurements", [])
            ],
            center_margin_c=float(data.get("center_margin_c", 1.0)),
        )

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "about": "Hold excess (zone temperature minus its own set point) per run, by target. "
            "Written by the ASC Oven Control app; offsets for a new target = -(expected excess).",
            "center_margin_c": self.center_margin_c,
            "measurements": [asdict(m) for m in sorted(self.measurements, key=lambda m: (m.target_c, m.run))],
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def add(self, measurement: HoldMeasurement) -> None:
        self.measurements = [m for m in self.measurements if m.run != measurement.run]
        self.measurements.append(measurement)

    # ------------------------------------------------------------ prediction

    def _points(self) -> list[tuple[float, tuple[float, float, float]]]:
        """Expected excess per zone at each tested target (newest runs weigh most)."""
        by_target: dict[float, list[HoldMeasurement]] = {}
        for m in self.measurements:
            by_target.setdefault(round(m.target_c), []).append(m)
        points = []
        for target, group in sorted(by_target.items()):
            group = sorted(group, key=lambda m: m.run)
            weights = [i + 1 for i in range(len(group))]  # later runs count more
            total = sum(weights)
            excess = []
            for zone in range(3):
                pick = (lambda m: m.peak_excess_c[zone]) if zone == CENTER_ZONE else (
                    lambda m: m.typical_excess_c[zone])
                excess.append(sum(w * pick(m) for w, m in zip(weights, group)) / total)
            points.append((float(target), tuple(excess)))
        return points

    def expected_excess(self, target_c: float) -> tuple[float, float, float] | None:
        points = self._points()
        if not points:
            return None
        if len(points) == 1:
            return points[0][1]
        if target_c <= points[0][0]:
            low, high = points[0], points[1]
        elif target_c >= points[-1][0]:
            low, high = points[-2], points[-1]
        else:
            low, high = next((a, b) for a, b in zip(points, points[1:]) if a[0] <= target_c <= b[0])
        span = high[0] - low[0]
        fraction = (target_c - low[0]) / span if span else 0.0
        result = []
        for zone in range(3):
            value = low[1][zone] + fraction * (high[1][zone] - low[1][zone])
            # Outside the tested range continue the trend of the nearest two
            # points (the middle zone's excess grows with temperature, so
            # holding it flat would under-correct and risk overshoot), but
            # keep it between 0 and twice the largest measured excess.
            measured = [abs(p[1][zone]) for p in points]
            if target_c > points[-1][0] or target_c < points[0][0]:
                value = min(max(value, 0.0), 2.0 * max(measured))
            result.append(value)
        return tuple(result)

    def offsets_for(self, target_c: float) -> tuple[float, float, float] | None:
        """Zone offsets that should land every zone on ``target_c`` (whole degrees)."""
        excess = self.expected_excess(target_c)
        if excess is None:
            return None
        offsets = [-e for e in excess]
        offsets[CENTER_ZONE] -= self.center_margin_c
        return tuple(float(round(o)) for o in offsets)

    def tested_targets(self) -> list[float]:
        return [p[0] for p in self._points()]
