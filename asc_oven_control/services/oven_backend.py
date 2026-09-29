"""Oven backends: one interface over the simulated plant and the real Watlows.

The run engine only talks to ``OvenBackend``. ``SimulatedOven`` integrates
the physics plant in-process; ``WatlowOven`` polls the three Series 96
controllers over Modbus RTU. Both report whole-degree readings so the
supervisory control behaves identically in simulation and on hardware.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from asc_oven_control.domain.oven_plant import OvenPlant
from asc_oven_control.infrastructure.config import ApplicationConfig
from asc_oven_control.infrastructure.modbus_rtu import ModbusError, ModbusRtuClient
from asc_oven_control.infrastructure.serial_transport import CommunicationError, create_transport
from asc_oven_control.infrastructure.watlow96 import Watlow96, ZoneStatus


@dataclass(frozen=True, slots=True)
class OvenReading:
    zones_c: tuple[float, float, float]
    setpoints_c: tuple[float, float, float]
    power_pct: tuple[float | None, float | None, float | None]
    alarms: tuple[str, ...]
    connected: bool
    remote: tuple[bool, bool, bool] = (False, False, False)


class OvenBackend(ABC):
    """What the run engine needs from an oven."""

    label = "backend"
    is_hardware = False

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> OvenReading: ...

    @abstractmethod
    def write_setpoints(self, setpoints_c: tuple[float, float, float]) -> None: ...

    def advance(self, dt_s: float) -> None:
        """Advance simulated time; real hardware runs on its own clock."""

    def take_control(self) -> list[str]:
        """Make the PC-written set points the active ones; returns notes."""
        return []

    def safe_shutdown(self) -> None:
        """Drive every zone to its lowest allowed setpoint (heaters off)."""


class SimulatedOven(OvenBackend):
    label = "Simulation"

    def __init__(self, plant: OvenPlant | None = None) -> None:
        self.plant = plant or OvenPlant()
        self.setpoints: tuple[float, float, float] = tuple(self.plant.readings())

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self) -> OvenReading:
        return OvenReading(
            zones_c=self.plant.readings(),
            setpoints_c=self.setpoints,
            power_pct=tuple(self.plant.power_pct),
            alarms=(),
            connected=True,
        )

    def write_setpoints(self, setpoints_c: tuple[float, float, float]) -> None:
        # Same whole-degree resolution as the Series 96 set point register.
        self.setpoints = tuple(float(round(sp)) for sp in setpoints_c)

    def advance(self, dt_s: float) -> None:
        self.plant.step(self.setpoints, dt_s)

    def safe_shutdown(self) -> None:
        self.setpoints = (0.0, 0.0, 0.0)


class WatlowOven(OvenBackend):
    """Three Watlow Series 96 controllers on one RS-485 bus."""

    label = "Watlow Series 96"
    is_hardware = True

    def __init__(self, config: ApplicationConfig, max_consecutive_failures: int = 5) -> None:
        if config.serial.port is None:
            raise CommunicationError("no serial port configured")
        self.config = config
        self.transport = create_transport(config.serial, simulation=False)
        self.client = ModbusRtuClient(self.transport)
        self.zones = [Watlow96(self.client, address) for address in config.zone_addresses]
        self.max_consecutive_failures = max_consecutive_failures
        self._failures = 0
        self._last_written: list[int | None] = [None, None, None]
        self._last: list[ZoneStatus | None] = [None, None, None]

    def connect(self) -> None:
        self.transport.connect()
        try:
            for zone in self.zones:
                zone.identify()
                self._last_written[self.zones.index(zone)] = zone.read_setpoint()
        except Exception:
            self.transport.disconnect()
            raise

    def close(self) -> None:
        self.transport.disconnect()

    def read(self) -> OvenReading:
        statuses = []
        for index, zone in enumerate(self.zones):
            try:
                status = zone.read_status()
                self._last[index] = status
                self._failures = 0
            except ModbusError:
                self._failures += 1
                if self._failures >= self.max_consecutive_failures or self._last[index] is None:
                    raise
                status = self._last[index]
            statuses.append(status)
        alarms = tuple(
            f"Zone {i + 1}: {message}" for i, s in enumerate(statuses) for message in s.alarms
        )
        return OvenReading(
            zones_c=tuple(s.process_c for s in statuses),
            setpoints_c=tuple(s.setpoint_c for s in statuses),
            power_pct=tuple(s.power_pct for s in statuses),
            alarms=alarms,
            connected=True,
            remote=tuple(s.remote for s in statuses),
        )

    def take_control(self) -> list[str]:
        """Switch every zone from the oven panel's remote set point to Local.

        The local set point is first written to the zone's lowest value so
        the switch itself never commands heat; the run then ramps from there.
        """
        notes = []
        for index, zone in enumerate(self.zones):
            if zone.identity is not None and zone.identity.remote_setpoint:
                low = zone.setpoint_limits()[0]
                zone.write_setpoint(low)
                self._last_written[index] = low
                zone.set_local_setpoint()
                notes.append(f"Zone {index + 1}")
        return notes

    def release_control(self) -> None:
        """Hand every zone back to the oven panel (remote set point)."""
        errors = []
        for index, zone in enumerate(self.zones):
            try:
                low = zone.setpoint_limits()[0]
                zone.write_setpoint(low)
                zone.set_remote_setpoint()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Zone {index + 1}: {exc}")
        if errors:
            raise CommunicationError("hand-back incomplete: " + "; ".join(errors))

    def write_setpoints(self, setpoints_c: tuple[float, float, float]) -> None:
        # Only write on change: the set point is whole degrees, so a
        # 10 C/min ramp costs ~10 writes/min/zone rather than one per poll,
        # which also spares the controllers' non-volatile memory.
        for index, (zone, value) in enumerate(zip(self.zones, setpoints_c)):
            raw = zone.clamp_setpoint(round(value))
            if raw != self._last_written[index]:
                zone.write_setpoint(raw)
                self._last_written[index] = raw

    def safe_shutdown(self) -> None:
        errors = []
        for index, zone in enumerate(self.zones):
            try:
                low = zone.setpoint_limits()[0]
                zone.write_setpoint(low)
                self._last_written[index] = low
            except Exception as exc:  # noqa: BLE001 - try every zone
                errors.append(f"Zone {index + 1}: {exc}")
        if errors:
            raise CommunicationError("safe shutdown incomplete: " + "; ".join(errors))


def create_backend(config: ApplicationConfig) -> OvenBackend:
    if config.simulation_mode:
        return SimulatedOven()
    return WatlowOven(config)


def probe_hardware(config: ApplicationConfig) -> list[str]:
    """Read-only connection test: identify each zone and read its state."""
    backend = WatlowOven(config)
    lines = []
    started = time.monotonic()
    backend.connect()
    try:
        for index, zone in enumerate(backend.zones):
            status = zone.read_status()
            ident = zone.identity
            power = "--" if status.power_pct is None else f"{status.power_pct:.0f}%"
            source = "oven panel (remote)" if status.remote else "PC (local)"
            lines.append(
                f"Zone {index + 1} (addr {zone.address}): Series {ident.model} sw {ident.software}"
                f" · PV {status.process_c:.0f} °C · SP {status.setpoint_c:.0f} °C from {source} · output {power}"
                + (f" · {', '.join(status.alarms)}" if status.alarms else "")
            )
    finally:
        backend.close()
    lines.append(f"{backend.client.transactions} transactions in {time.monotonic() - started:.1f} s")
    return lines
