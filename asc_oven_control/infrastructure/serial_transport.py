"""Serial transports with a simulation-first safety model.

The application runs in simulation mode by default and never opens a
physical serial port. ``create_transport`` only reaches the real
``PySerialTransport`` when BOTH ``simulation=False`` AND a concrete port are
supplied — and even then pyserial is imported lazily inside ``open()`` so
constructing transports performs no I/O. See ``LABVIEW_MIGRATION.md`` for
the hardware commissioning boundary.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque

from asc_oven_control.infrastructure.config import SerialProfile


class CommunicationError(RuntimeError):
    """Raised for transport-level failures (open, read, write)."""


class BaseTransport(ABC):
    """Byte-level serial transport contract used by the run engine."""

    def __init__(self, profile: SerialProfile) -> None:
        self.profile = profile

    @abstractmethod
    def connect(self) -> None:
        """Open the connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection; never raises."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True when the transport holds an open connection."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Send raw bytes."""

    @abstractmethod
    def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes; returns fewer at EOF/timeout."""


class DisconnectedTransport(BaseTransport):
    """Fail-closed transport: every operation raises.

    This is what the application uses until a commissioning procedure
    produces a verified protocol.
    """

    def connect(self) -> None:
        raise CommunicationError("hardware transport is locked (simulation only)")

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        raise CommunicationError("hardware transport is locked (simulation only)")

    def read(self, size: int) -> bytes:
        raise CommunicationError("hardware transport is locked (simulation only)")


class SimulatedSerialTransport(BaseTransport):
    """In-memory transport: records writes and drains queued responses."""

    def __init__(self, profile: SerialProfile) -> None:
        super().__init__(profile)
        self._lock = threading.RLock()
        self._connected = False
        self.writes: list[bytes] = []
        self._responses: Deque[bytes] = deque()

    def connect(self) -> None:
        with self._lock:
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def queue_response(self, data: bytes) -> None:
        with self._lock:
            self._responses.append(data)

    def write(self, data: bytes) -> None:
        with self._lock:
            if not self._connected:
                raise CommunicationError("simulation transport is disconnected")
            self.writes.append(data)

    def read(self, size: int) -> bytes:
        with self._lock:
            if not self._connected:
                raise CommunicationError("simulation transport is disconnected")
            if not self._responses:
                return b""
            data = self._responses.popleft()
            return data[:size]


class PySerialTransport(BaseTransport):
    """Real serial transport; pyserial is imported only inside ``open()``."""

    def __init__(self, profile: SerialProfile) -> None:
        super().__init__(profile)
        if profile.port is None:
            raise CommunicationError("no serial port configured")
        self._connection = None

    def connect(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise CommunicationError("pyserial is required for a live serial connection") from exc
        try:
            self._connection = serial.Serial(
                self.profile.port,
                baudrate=self.profile.baudrate,
                bytesize=self.profile.bytesize,
                parity=self.profile.parity,
                stopbits=self.profile.stopbits,
                timeout=self.profile.timeout_s,
            )
        except Exception as exc:
            raise CommunicationError(f"cannot open {self.profile.port}: {exc}") from exc

    def disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None and connection.is_open:
            connection.close()

    def is_connected(self) -> bool:
        return bool(self._connection is not None and self._connection.is_open)

    def write(self, data: bytes) -> None:
        if not self.is_connected():
            raise CommunicationError("serial transport is disconnected")
        self._connection.write(data)
        self._connection.flush()

    def read(self, size: int) -> bytes:
        if not self.is_connected():
            raise CommunicationError("serial transport is disconnected")
        return self._connection.read(size)


def create_transport(profile: SerialProfile, *, simulation: bool = True) -> BaseTransport:
    """Build the transport for a profile under the simulation-first rule."""
    if simulation:
        return SimulatedSerialTransport(profile)
    if profile.port is None:
        return DisconnectedTransport(profile)
    return PySerialTransport(profile)
