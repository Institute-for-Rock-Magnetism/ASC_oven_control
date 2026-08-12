"""CRC-framed register protocol recovered from the LabVIEW VIs.

Evidence chain (see ``LABVIEW_MIGRATION.md`` and ``reconstructions/labview``):

- ``Calc CRC-sub.vi`` exposes the frame parts — Command, Adress, Reg H,
  Reg L, Data H, Data L, Number of Byte — and emits the CRC as separate
  H byte / L byte outputs. Its block diagram shifts the register through
  ``Shift right.vi`` (D0–D15, LSB) which is the LSB-first, right-shifting
  CRC-16 family; the standard configuration (poly 0xA001, init 0xFFFF) is
  the Modbus RTU CRC.
- ``Watlow Read.vi`` / ``Watlow Write.vi`` send these frames over NI-VISA
  serial (9600 baud, 8N1, no flow control) and surface a raw "Read buffer".
- ``Change SP.vi`` (Set Point), ``Adjust_ramp_rate.vi`` (Ramp rate), and
  ``stop_program.vi`` build the same frame for their operations.

NOT recovered from the binary-only evidence: the controller slave address,
the register addresses for temperature/setpoint/ramp/alarm, the 16-bit word
order inside a register, temperature scaling, and response validation. The
builders below are a reference for commissioning — the GUI never opens them
or a hardware transport, so nothing here can drive live hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CRC16_POLY = 0xA001
CRC16_INIT = 0xFFFF

# Recovered frame parts (see module docstring). Byte order inside the frame
# follows the Modbus RTU convention (CRC low byte first); the LabVIEW VI
# only proves the parts exist, not their final order.
READ = "READ"
WRITE = "WRITE"
SET_POINT = "SET_POINT"
RAMP_RATE = "RAMP_RATE"
STOP = "STOP"


class ProtocolValidationError(ValueError):
    """Raised when a frame cannot be built or parsed."""


def crc16(data: bytes) -> int:
    """Modbus-style CRC-16 (poly 0xA001, init 0xFFFF) over ``data``."""
    crc = CRC16_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ CRC16_POLY if crc & 1 else crc >> 1
    return crc & 0xFFFF


def verify_frame(frame: bytes) -> bool:
    """True when the trailing two bytes are the CRC of the frame body."""
    if len(frame) < 3:
        return False
    body, crc = frame[:-2], frame[-2:]
    expected = crc16(body)
    return crc == bytes((expected & 0xFF, expected >> 8))


def _byte(name: str, value: Any, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{name} must be an integer")
        return 0
    if not 0 <= value <= 0xFF:
        errors.append(f"{name} must be in 0..255")
        return 0
    return value


def _word(name: str, value: Any, errors: list[str]) -> tuple[int, int]:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{name} must be an integer")
        return 0, 0
    if not 0 <= value <= 0xFFFF:
        errors.append(f"{name} must be in 0..65535")
        return 0, 0
    return (value >> 8) & 0xFF, value & 0xFF


def _word_or_raise(name: str, value: Any) -> tuple[int, int]:
    errors: list[str] = []
    hi, lo = _word(name, value, errors)
    if errors:
        raise ProtocolValidationError("; ".join(errors))
    return hi, lo


@dataclass(frozen=True, slots=True)
class WatlowFrame:
    """One CRC-framed register transaction (recovered layout, see module doc).

    ``to_bytes`` serializes to: address, register high byte, register low
    byte, data byte count, payload bytes, CRC low byte, CRC high byte.
    """

    operation: str
    address: int
    register: int | None = None
    count: int | None = None
    payload: bytes = b""

    def __post_init__(self) -> None:
        if self.operation not in (READ, WRITE, SET_POINT, RAMP_RATE, STOP):
            raise ProtocolValidationError(f"unknown operation {self.operation!r}")

    def to_bytes(self) -> bytes:
        errors: list[str] = []
        address = _byte("address", self.address, errors)
        if self.register is None:
            register_hi, register_lo = 0x00, 0x00
        else:
            register_hi, register_lo = _word("register", self.register, errors)
        count = 0 if self.count is None else _byte("count", self.count, errors)
        if errors:
            raise ProtocolValidationError("; ".join(errors))
        body = bytes((address, register_hi, register_lo, count)) + self.payload
        crc = crc16(body)
        return body + bytes((crc & 0xFF, crc >> 8))

    def __bytes__(self) -> bytes:
        return self.to_bytes()


class WatlowCommands:
    """Builders for the operations recovered from the LabVIEW program.

    ``register`` values are placeholders: the exact register addresses must
    be supplied from hardware commissioning.
    """

    @staticmethod
    def read(address: int, register: int, count: int) -> WatlowFrame:
        return WatlowFrame(READ, address, register=register, count=count)

    @staticmethod
    def write(address: int, register: int, value: int, count: int = 2) -> WatlowFrame:
        hi, lo = _word_or_raise("value", value)
        return WatlowFrame(WRITE, address, register=register, count=count, payload=bytes((hi, lo)))

    @staticmethod
    def setpoint(address: int, register: int, setpoint_raw: int) -> WatlowFrame:
        hi, lo = _word_or_raise("setpoint_raw", setpoint_raw)
        return WatlowFrame(SET_POINT, address, register=register, count=2, payload=bytes((hi, lo)))

    @staticmethod
    def ramp_rate(address: int, register: int, ramp_raw: int) -> WatlowFrame:
        hi, lo = _word_or_raise("ramp_raw", ramp_raw)
        return WatlowFrame(RAMP_RATE, address, register=register, count=2, payload=bytes((hi, lo)))

    @staticmethod
    def stop(address: int, register: int) -> WatlowFrame:
        return WatlowFrame(STOP, address, register=register, count=0)


def parse_read_reply(payload: bytes, expected_count: int) -> bytes:
    """Validate a read reply and return the raw register bytes.

    The LabVIEW read operation returns an "unsigned byte array"; this
    parser only enforces the byte count, leaving word order and scaling to
    commissioning.
    """
    if len(payload) < expected_count:
        raise ProtocolValidationError(
            f"read reply too short: {len(payload)} bytes, expected at least {expected_count}"
        )
    return payload[:expected_count]
