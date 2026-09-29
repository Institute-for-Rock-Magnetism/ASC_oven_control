"""Watlow Series 96 driver (one controller per oven zone).

Register numbers are the relative Modbus numbers from the Series 96 User's
Manual (July 2005), appendix A.3 and chapter 6, cross-checked by reading
the ASC oven's three controllers on 2026-09-29:

- Model register 0 reads 96 on slaves 1, 2 and 3.
- Units: reg 901 = 1 (°C), decimal reg 606 = 0 (whole degrees), input
  reg 601 = 3 (type E thermocouple), set point range 0..800 °C (602/603).
- PID units reg 900 = 2 (SI): prop band in degrees, integral in
  minutes/repeat (x100), derivative in minutes (x100).
- Percent output reg 103 is taken as x10 (1000 = 100.0 %). UNVERIFIED:
  with the heater coil unpowered it read 1000 on all zones both with the
  set point above and far below the process value, so it may not be the
  live PID output. Confirm against the front-panel output LED once the
  heater is powered before relying on it.

The legacy LabVIEW program used the same registers: ``Change SP.vi``
writes 300 (Reg-H 1, Reg-L 44), ``Ck_alarm.vi`` polls 106, and
``Adjust_ramp_rate.vi`` touches the 1100-1102 ramp block.
"""

from __future__ import annotations

from dataclasses import dataclass

from asc_oven_control.infrastructure.modbus_rtu import ModbusError, ModbusRtuClient, to_s16

# Identity
REG_MODEL = 0
REG_SOFTWARE_ID = 3
REG_SOFTWARE_REV = 4
# Process block (read together: 100..103)
REG_PROCESS = 100
REG_INPUT_ERROR = 101
REG_PERCENT_OUTPUT = 103
REG_ALARM2_STATUS = 106
REG_ALARM3_STATUS = 110
REG_SYSTEM_ERROR = 209
REG_OPEN_LOOP = 210
# Set point / mode
REG_SETPOINT = 300
REG_AUTO_MANUAL = 301
REG_AUTOTUNE_SETPOINT = 304
REG_AUTOTUNE = 305
REG_CLEAR_INPUT_ERRORS = 311
REG_CLEAR_ALARMS = 331
# PID set 1 (SI units when reg 900 = 2; US units use 502/504)
REG_PROP_BAND = 500
REG_INTEGRAL = 501
REG_RESET = 502
REG_DERIVATIVE = 503
REG_RATE = 504
REG_DEAD_BAND = 505
REG_CYCLE_TIME = 506
# Input
REG_RANGE_LOW = 602
REG_RANGE_HIGH = 603
REG_INPUT_FILTER = 604
REG_CAL_OFFSET = 605
REG_DECIMAL = 606
REG_PID_UNITS = 900
REG_TEMP_UNITS = 901
# Internal ramp-to-set-point (kept OFF: the PC does coordinated ramping)
REG_RAMP_MODE = 1100

INPUT_ERRORS = {
    1: "input too low to measure",
    2: "input under sensor range",
    3: "input over sensor range (open thermocouple?)",
    4: "input too large to measure",
}
ALARM_STATES = {
    1: "alarm high",
    2: "alarm low",
    3: "alarm high (latched)",
    4: "alarm low (latched)",
    5: "alarm high (silenced)",
    6: "alarm low (silenced)",
    7: "alarm high (latched, silenced)",
    8: "alarm low (latched, silenced)",
    11: "alarm error",
}
SYSTEM_ERRORS = {
    4: "RAM error",
    5: "non-volatile memory checksum error",
    6: "ROM error",
    7: "hardware error",
    8: "module error",
    9: "configuration error",
    10: "module changed",
    11: "new software installed",
    12: "calibration data corrupted",
    13: "A/D timeout",
    14: "serial EEPROM time-out",
    15: "new unit",
    16: "EEPROM invalid address",
}
UNAVAILABLE = -32000  # what this unit returns for parameters it does not have


class Watlow96Error(ModbusError):
    """The controller is not configured the way this driver requires."""


@dataclass(frozen=True, slots=True)
class Identity:
    model: int
    software: str
    celsius: bool
    decimals: int
    si_pid_units: bool
    range_low: int
    range_high: int


@dataclass(frozen=True, slots=True)
class ZoneStatus:
    process_c: float
    setpoint_c: float
    power_pct: float | None
    alarms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PidSettings:
    """PID set 1 in engineering units (SI: integral min/repeat, derivative min)."""

    prop_band_c: float
    integral_min: float
    derivative_min: float
    cycle_time_s: float

    @property
    def reset_per_min(self) -> float:
        return 0.0 if self.integral_min <= 0 else 1.0 / self.integral_min


class Watlow96:
    """One Series 96 controller at a Modbus slave address."""

    def __init__(self, client: ModbusRtuClient, address: int) -> None:
        self.client = client
        self.address = address
        self.identity: Identity | None = None

    # ------------------------------------------------------------ identity

    def identify(self) -> Identity:
        """Read and validate model, units and scaling; required before writes."""
        c, a = self.client, self.address
        model = c.read_register(a, REG_MODEL)
        if model != 96:
            raise Watlow96Error(f"slave {a}: model register reads {model}, expected 96 (Series 96)")
        software_id, revision = c.read_registers(a, REG_SOFTWARE_ID, 2)
        range_low, range_high = (to_s16(v) for v in c.read_registers(a, REG_RANGE_LOW, 2))
        decimals = c.read_register(a, REG_DECIMAL)
        pid_units, temp_units = c.read_registers(a, REG_PID_UNITS, 2)
        identity = Identity(
            model=model,
            software=f"{software_id} rev {revision / 100:.2f}",
            celsius=temp_units == 1,
            decimals=decimals,
            si_pid_units=pid_units == 2,
            range_low=range_low,
            range_high=range_high,
        )
        if not identity.celsius:
            raise Watlow96Error(f"slave {a}: controller is set to °F (reg 901); set it to °C")
        if decimals not in (0, 1):
            raise Watlow96Error(f"slave {a}: unsupported decimal setting {decimals}")
        self.identity = identity
        return identity

    @property
    def _scale(self) -> float:
        return 10.0 if self.identity is not None and self.identity.decimals == 1 else 1.0

    def _require_identity(self) -> Identity:
        if self.identity is None:
            return self.identify()
        return self.identity

    # -------------------------------------------------------------- status

    def read_status(self) -> ZoneStatus:
        self._require_identity()
        c, a = self.client, self.address
        process_raw, input_error, _unused, output_raw = c.read_registers(a, REG_PROCESS, 4)
        setpoint_raw = c.read_register(a, REG_SETPOINT)
        alarm2 = c.read_register(a, REG_ALARM2_STATUS)
        alarms = []
        if input_error:
            alarms.append(INPUT_ERRORS.get(input_error, f"input error {input_error}"))
        if alarm2 in ALARM_STATES:
            alarms.append(ALARM_STATES[alarm2])
        output = to_s16(output_raw)
        return ZoneStatus(
            process_c=to_s16(process_raw) / self._scale,
            setpoint_c=to_s16(setpoint_raw) / self._scale,
            power_pct=None if output == UNAVAILABLE else output / 10.0,
            alarms=tuple(alarms),
        )

    def read_errors(self) -> tuple[str, ...]:
        c, a = self.client, self.address
        system_error, open_loop = c.read_registers(a, REG_SYSTEM_ERROR, 2)
        errors = []
        if system_error:
            errors.append(SYSTEM_ERRORS.get(system_error, f"system error {system_error}"))
        if open_loop:
            errors.append("open loop detected (heater or thermocouple fault)")
        return tuple(errors)

    # ----------------------------------------------------------- set point

    def setpoint_limits(self) -> tuple[int, int]:
        """Allowed set point range in whole degrees (reg 602/603)."""
        identity = self._require_identity()
        return round(identity.range_low / self._scale), round(identity.range_high / self._scale)

    def clamp_setpoint(self, value_c: float) -> int:
        low, high = self.setpoint_limits()
        return int(max(low, min(high, round(value_c))))

    def read_setpoint(self) -> int:
        self._require_identity()
        return round(to_s16(self.client.read_register(self.address, REG_SETPOINT)) / self._scale)

    def write_setpoint(self, value_c: float) -> None:
        low, high = self.setpoint_limits()
        if not low <= value_c <= high:
            raise ValueError(f"set point {value_c} outside controller range {low}..{high} °C")
        self.client.write_register(self.address, REG_SETPOINT, round(value_c * self._scale))

    # ----------------------------------------------------------------- PID

    def read_pid(self) -> PidSettings:
        identity = self._require_identity()
        c, a = self.client, self.address
        band, integral, reset, derivative, rate, _dead, cycle = c.read_registers(a, REG_PROP_BAND, 7)
        if identity.si_pid_units:
            integral_min = integral / 100.0
            derivative_min = derivative / 100.0
        else:
            integral_min = 0.0 if reset == 0 else 100.0 / reset
            derivative_min = rate / 100.0
        return PidSettings(
            prop_band_c=float(band),
            integral_min=integral_min,
            derivative_min=derivative_min,
            cycle_time_s=cycle / 10.0,
        )

    def write_pid(self, pid: PidSettings) -> None:
        """Write prop band, integral/reset and derivative/rate of PID set 1."""
        identity = self._require_identity()
        if not 1 <= pid.prop_band_c <= 9999:
            raise ValueError("prop band must be 1..9999 degrees (0 would select on/off control)")
        if not 0 <= pid.integral_min <= 99.99:
            raise ValueError("integral must be 0..99.99 min/repeat")
        if not 0 <= pid.derivative_min <= 9.99:
            raise ValueError("derivative must be 0..9.99 min")
        c, a = self.client, self.address
        c.write_register(a, REG_PROP_BAND, round(pid.prop_band_c))
        if identity.si_pid_units:
            c.write_register(a, REG_INTEGRAL, round(pid.integral_min * 100))
            c.write_register(a, REG_DERIVATIVE, round(pid.derivative_min * 100))
        else:
            reset = 0 if pid.integral_min <= 0 else round(100.0 / pid.integral_min)
            c.write_register(a, REG_RESET, min(reset, 9999))
            c.write_register(a, REG_RATE, round(pid.derivative_min * 100))

    # ----------------------------------------------------------- auto-tune

    def start_autotune(self, setpoint_percent: int = 90) -> None:
        """Start the controller's built-in auto-tune (Auto mode only).

        The Series 96 tunes around ``setpoint_percent`` of the active set
        point (reg 304, 50..150 %), so it oscillates below the target.
        """
        if not 50 <= setpoint_percent <= 150:
            raise ValueError("auto-tune set point must be 50..150 %")
        c, a = self.client, self.address
        if c.read_register(a, REG_AUTO_MANUAL) != 0:
            raise Watlow96Error(f"slave {a}: auto-tune needs Auto mode")
        c.write_register(a, REG_AUTOTUNE_SETPOINT, setpoint_percent)
        c.write_register(a, REG_AUTOTUNE, 1)

    def cancel_autotune(self) -> None:
        self.client.write_register(self.address, REG_AUTOTUNE, 0)

    def autotune_active(self) -> bool:
        return self.client.read_register(self.address, REG_AUTOTUNE) != 0
