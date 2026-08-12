"""Validated domain models for the ASC thermal oven.

Recovered from the LabVIEW binary evidence (see ``LABVIEW_MIGRATION.md``):

- ``ASC_thermal2.0.vi`` front panel exposes a three-zone chart ("Wetlow
  Chart"), elapsed/setpoint time tracking, a field coil (Field ON/OFF,
  Amplitude), fan and scale controls.
- ``Get_run_info.vi`` / ``stop_program.vi`` carry the atmosphere choices
  (Air, Argon, Helium, Nitrogen) and the run identity fields.
- ``PID_globals.vi`` holds the proportional band, integral, and derivative
  terms of the Watlow controller.

Values that must be established on the real instrument stay optional with
``None`` defaults instead of unsafe assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any


class DomainValidationError(ValueError):
    """Raised when a domain object fails validation.

    ``errors`` maps field names to message tuples so each failure can be
    addressed independently.
    """

    def __init__(self, errors: dict[str, tuple[str, ...]]) -> None:
        self.errors = errors
        message = "; ".join(
            f"{name}: {'; '.join(msgs)}" for name, msgs in errors.items()
        )
        super().__init__(message)


class StringEnum(str, Enum):
    """Enum whose ``str()`` and ``.value`` agree."""

    def __str__(self) -> str:
        return self.value


class Atmosphere(StringEnum):
    """Controlled atmosphere choices recovered from Get_run_info.vi."""

    AIR = "Air"
    ARGON = "Argon"
    HELIUM = "Helium"
    NITROGEN = "Nitrogen"


class OvenPhase(StringEnum):
    """Run lifecycle phases used by the controller state machine."""

    IDLE = "Idle"
    RAMPING = "Ramping"
    SOAKING = "Soaking"
    PAUSED = "Paused"
    COMPLETE = "Complete"
    ABORTED = "Aborted"
    FAILED = "Failed"


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _finite_required(name: str, value: Any, errors: dict[str, tuple[str, ...]]) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors[name] = errors.get(name, ()) + ("must be a number",)
        return False
    if value != value or value in (float("inf"), float("-inf")):
        errors[name] = errors.get(name, ()) + ("must be finite",)
        return False
    return True


def _nonnegative(name: str, value: float, errors: dict[str, tuple[str, ...]]) -> bool:
    if value < 0:
        errors[name] = errors.get(name, ()) + ("must be >= 0",)
        return False
    return True


def _positive(name: str, value: float, errors: dict[str, tuple[str, ...]]) -> bool:
    if value <= 0:
        errors[name] = errors.get(name, ()) + ("must be > 0",)
        return False
    return True


def _enum_value(enum_type, value: Any, errors: dict[str, tuple[str, ...]], name: str):
    try:
        return enum_type(value)
    except ValueError:
        choices = ", ".join(str(item.value) for item in enum_type)
        errors[name] = errors.get(name, ()) + (f"must be one of {choices}",)
        return None


def _check_unknown(data: dict[str, Any], known: set[str], errors: dict[str, tuple[str, ...]]) -> None:
    unknown = set(data) - known
    if unknown:
        errors["__unknown__"] = (f"unknown fields: {', '.join(sorted(unknown))}",)


def _optional_number(name: str, value: Any, errors: dict[str, tuple[str, ...]]) -> bool:
    if value is None:
        return True
    return _finite_required(name, value, errors)


def _optional_nonnegative(name: str, value: Any, errors: dict[str, tuple[str, ...]]) -> bool:
    if value is None:
        return True
    ok = _finite_required(name, value, errors)
    return ok and _nonnegative(name, float(value), errors)


def _optional_positive(name: str, value: Any, errors: dict[str, tuple[str, ...]]) -> bool:
    if value is None:
        return True
    ok = _finite_required(name, value, errors)
    return ok and _positive(name, float(value), errors)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PidParameters:
    """Watlow PID terms recovered from PID_globals.vi.

    All values are optional: the recovery evidence establishes that these
    terms exist on the controller front panel, not their calibrated values.
    """

    prop_band: float | None = None
    integral: float | None = None
    derivative: float | None = None

    def __post_init__(self) -> None:
        errors: dict[str, tuple[str, ...]] = {}
        _optional_positive("prop_band", self.prop_band, errors)
        _optional_nonnegative("integral", self.integral, errors)
        _optional_nonnegative("derivative", self.derivative, errors)
        if errors:
            raise DomainValidationError(errors)

    def to_dict(self) -> dict[str, Any]:
        return {"prop_band": self.prop_band, "integral": self.integral, "derivative": self.derivative}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PidParameters":
        errors: dict[str, tuple[str, ...]] = {}
        _check_unknown(data, {"prop_band", "integral", "derivative"}, errors)
        if errors:
            raise DomainValidationError(errors)
        return cls(
            prop_band=data.get("prop_band"),
            integral=data.get("integral"),
            derivative=data.get("derivative"),
        )


@dataclass(frozen=True, slots=True)
class RunProfile:
    """One thermal run: identity, thermal recipe, field, and alarms."""

    operator: str
    batch_id: str
    sample_id: str
    user_name: str
    atmosphere: Atmosphere
    target_setpoint_c: float
    ramp_rate_c_per_min: float
    soak_time_sec: float
    alarm_high_c: float
    alarm_low_c: float
    notes: str = ""
    field_enabled: bool = False
    field_amplitude_uT: float = 0.0
    pid: PidParameters = PidParameters()

    def __post_init__(self) -> None:
        errors: dict[str, tuple[str, ...]] = {}
        if not isinstance(self.operator, str) or not self.operator.strip():
            errors["operator"] = ("required",)
        if not isinstance(self.notes, str):
            errors["notes"] = ("must be a string",)
        for name in ("batch_id", "sample_id", "user_name"):
            if not isinstance(getattr(self, name), str):
                errors[name] = ("must be a string",)
        if _finite_required("target_setpoint_c", self.target_setpoint_c, errors):
            _nonnegative("target_setpoint_c", float(self.target_setpoint_c), errors)
        if _finite_required("ramp_rate_c_per_min", self.ramp_rate_c_per_min, errors):
            _nonnegative("ramp_rate_c_per_min", float(self.ramp_rate_c_per_min), errors)
        if _finite_required("soak_time_sec", self.soak_time_sec, errors):
            _nonnegative("soak_time_sec", float(self.soak_time_sec), errors)
        _finite_required("alarm_high_c", self.alarm_high_c, errors)
        _finite_required("alarm_low_c", self.alarm_low_c, errors)
        if "alarm_high_c" not in errors and "alarm_low_c" not in errors:
            if self.alarm_low_c >= self.alarm_high_c:
                errors["alarm_low_c"] = ("must be below alarm_high_c",)
        if not isinstance(self.field_enabled, bool):
            errors["field_enabled"] = ("must be a boolean",)
        if _finite_required("field_amplitude_uT", self.field_amplitude_uT, errors):
            _nonnegative("field_amplitude_uT", float(self.field_amplitude_uT), errors)
        if not isinstance(self.pid, PidParameters):
            errors["pid"] = ("must be a PidParameters",)
        if errors:
            raise DomainValidationError(errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "batch_id": self.batch_id,
            "sample_id": self.sample_id,
            "user_name": self.user_name,
            "atmosphere": str(self.atmosphere),
            "target_setpoint_c": self.target_setpoint_c,
            "ramp_rate_c_per_min": self.ramp_rate_c_per_min,
            "soak_time_sec": self.soak_time_sec,
            "alarm_high_c": self.alarm_high_c,
            "alarm_low_c": self.alarm_low_c,
            "notes": self.notes,
            "field_enabled": self.field_enabled,
            "field_amplitude_uT": self.field_amplitude_uT,
            "pid": self.pid.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunProfile":
        errors: dict[str, tuple[str, ...]] = {}
        known = {field.name for field in fields(cls)}
        _check_unknown(data, known, errors)
        if errors:
            raise DomainValidationError(errors)
        atmosphere = _enum_value(Atmosphere, data.get("atmosphere", "Air"), errors, "atmosphere")
        pid = PidParameters.from_dict(data.get("pid", {})) if isinstance(data.get("pid"), dict) else PidParameters()
        return cls(
            operator=data.get("operator", ""),
            batch_id=data.get("batch_id", ""),
            sample_id=data.get("sample_id", ""),
            user_name=data.get("user_name", ""),
            atmosphere=atmosphere or Atmosphere.AIR,
            target_setpoint_c=data.get("target_setpoint_c", 0.0),
            ramp_rate_c_per_min=data.get("ramp_rate_c_per_min", 0.0),
            soak_time_sec=data.get("soak_time_sec", 0.0),
            alarm_high_c=data.get("alarm_high_c", 0.0),
            alarm_low_c=data.get("alarm_low_c", 0.0),
            notes=data.get("notes", ""),
            field_enabled=bool(data.get("field_enabled", False)),
            field_amplitude_uT=data.get("field_amplitude_uT", 0.0),
            pid=pid,
        )


@dataclass(frozen=True, slots=True)
class SamplePoint:
    """One logged observation of all three zones plus the commanded values."""

    timestamp: float
    elapsed_sec: float
    zone_temps_c: tuple[float, float, float]
    current_a: float | None
    output_setpoint_c: float
    target_setpoint_c: float
    phase: OvenPhase
    alarm: str
    connected: bool
