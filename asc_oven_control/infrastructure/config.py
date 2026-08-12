"""Versioned application configuration.

The recovered LabVIEW evidence pins the serial framing (NI-VISA serial,
9600 baud, 8 data bits, no parity, one stop bit, no flow control) but not
the exact Watlow register map. The defaults below are therefore safe for
simulation and everything hardware-related stays unset (``port=None``)
until a commissioning procedure provides the missing values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

APPLICATION_CONFIG_VERSION = 1
SERIAL_PROFILE_VERSION = 1


class ConfigValidationError(ValueError):
    """Raised when a configuration object fails validation."""


@dataclass(frozen=True, slots=True)
class SerialProfile:
    """Serial framing recovered from the LabVIEW VISA configuration.

    ``port`` defaults to None: None means "no hardware", which is the only
    state the application is allowed to run in until the register map is
    independently verified.
    """

    version: int = SERIAL_PROFILE_VERSION
    port: str | None = None
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if self.version != SERIAL_PROFILE_VERSION:
            raise ConfigValidationError(f"unsupported serial profile version {self.version}")
        if self.port is not None and not isinstance(self.port, str):
            raise ConfigValidationError("port must be a string or None")
        if self.baudrate not in (9600, 19200, 38400, 57600, 115200):
            raise ConfigValidationError(f"unsupported baud rate {self.baudrate}")
        if self.bytesize not in (7, 8):
            raise ConfigValidationError(f"unsupported byte size {self.bytesize}")
        if self.parity not in ("N", "E", "O"):
            raise ConfigValidationError(f"unsupported parity {self.parity!r}")
        if self.stopbits not in (1, 2):
            raise ConfigValidationError(f"unsupported stop bits {self.stopbits}")
        if self.timeout_s <= 0:
            raise ConfigValidationError("timeout must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "port": self.port,
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "timeout_s": self.timeout_s,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SerialProfile":
        unknown = set(data) - {
            "version", "port", "baudrate", "bytesize", "parity", "stopbits", "timeout_s",
        }
        if unknown:
            raise ConfigValidationError(f"unknown serial profile fields: {sorted(unknown)}")
        return cls(
            version=data.get("version", SERIAL_PROFILE_VERSION),
            port=data.get("port"),
            baudrate=data.get("baudrate", 9600),
            bytesize=data.get("bytesize", 8),
            parity=data.get("parity", "N"),
            stopbits=data.get("stopbits", 1),
            timeout_s=data.get("timeout_s", 1.0),
        )


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """Top-level configuration; ``simulation_mode`` defaults to True."""

    version: int = APPLICATION_CONFIG_VERSION
    simulation_mode: bool = True
    data_dir: str | None = None
    poll_seconds: float = 0.5
    serial: SerialProfile = SerialProfile()

    def __post_init__(self) -> None:
        if self.version != APPLICATION_CONFIG_VERSION:
            raise ConfigValidationError(f"unsupported application config version {self.version}")
        if not isinstance(self.simulation_mode, bool):
            raise ConfigValidationError("simulation_mode must be a boolean")
        if self.data_dir is not None and not isinstance(self.data_dir, str):
            raise ConfigValidationError("data_dir must be a string or None")
        if self.poll_seconds <= 0:
            raise ConfigValidationError("poll_seconds must be positive")
        if not isinstance(self.serial, SerialProfile):
            raise ConfigValidationError("serial must be a SerialProfile")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "simulation_mode": self.simulation_mode,
            "data_dir": self.data_dir,
            "poll_seconds": self.poll_seconds,
            "serial": self.serial.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApplicationConfig":
        unknown = set(data) - {"version", "simulation_mode", "data_dir", "poll_seconds", "serial"}
        if unknown:
            raise ConfigValidationError(f"unknown application config fields: {sorted(unknown)}")
        serial_data = data.get("serial")
        serial = SerialProfile.from_dict(serial_data) if isinstance(serial_data, dict) else SerialProfile()
        return cls(
            version=data.get("version", APPLICATION_CONFIG_VERSION),
            simulation_mode=bool(data.get("simulation_mode", True)),
            data_dir=data.get("data_dir"),
            poll_seconds=data.get("poll_seconds", 0.5),
            serial=serial,
        )
