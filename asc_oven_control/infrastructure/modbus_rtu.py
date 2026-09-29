"""Modbus RTU master over a byte transport.

Verified on the ASC oven (2026-09-29, COM4, CP210x USB-UART): three Watlow
Series 96 controllers answer as slaves 1, 2 and 3 at 9600 baud 8N1. The
legacy LabVIEW VIs use exactly these two functions:

- ``Watlow Read.vi``: function 0x03 (read holding registers), one register.
- ``Change SP.vi`` / ``Watlow Write.vi``: function 0x06 (write single
  register), which the slave echoes back verbatim.

Replies are read to their exact expected length so stray line bytes (seen
once on the RS-485 bus as a trailing ``00 00``) can never be mistaken for
the next reply; the input buffer is also flushed before every request.
"""

from __future__ import annotations

import threading
import time

from asc_oven_control.infrastructure.serial_transport import BaseTransport, CommunicationError
from asc_oven_control.infrastructure.watlow_protocol import crc16

READ_HOLDING = 0x03
WRITE_SINGLE = 0x06

EXCEPTION_NAMES = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "slave device failure",
    6: "slave device busy",
}


class ModbusError(CommunicationError):
    """Timeout, CRC failure, malformed reply, or a Modbus exception reply."""


class ModbusExceptionReply(ModbusError):
    """The slave answered with a Modbus exception frame."""

    def __init__(self, address: int, function: int, code: int) -> None:
        self.address = address
        self.function = function
        self.code = code
        name = EXCEPTION_NAMES.get(code, "unknown")
        super().__init__(f"slave {address} function {function:#04x}: exception {code} ({name})")


def build_frame(body: bytes) -> bytes:
    """Append the Modbus CRC (low byte first) to ``body``."""
    crc = crc16(body)
    return body + bytes((crc & 0xFF, crc >> 8))


def check_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == frame[-2] | (frame[-1] << 8)


def read_request(address: int, register: int, count: int = 1) -> bytes:
    _check_address(address)
    _check_u16("register", register)
    if not 1 <= count <= 125:
        raise ValueError("count must be in 1..125")
    return build_frame(bytes((address, READ_HOLDING, register >> 8, register & 0xFF, 0, count)))


def write_request(address: int, register: int, value: int) -> bytes:
    _check_address(address)
    _check_u16("register", register)
    raw = to_u16(value)
    return build_frame(bytes((address, WRITE_SINGLE, register >> 8, register & 0xFF, raw >> 8, raw & 0xFF)))


def to_u16(value: int) -> int:
    """Encode a signed or unsigned 16-bit integer as its register word."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("register value must be an integer")
    if not -0x8000 <= value <= 0xFFFF:
        raise ValueError(f"register value {value} does not fit in 16 bits")
    return value & 0xFFFF


def to_s16(raw: int) -> int:
    """Decode a register word as a signed 16-bit integer (Watlow values are signed)."""
    return raw - 0x10000 if raw & 0x8000 else raw


def _check_address(address: int) -> None:
    if isinstance(address, bool) or not isinstance(address, int) or not 1 <= address <= 247:
        raise ValueError("slave address must be in 1..247")


def _check_u16(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must be in 0..65535")


class ModbusRtuClient:
    """Thread-safe Modbus RTU master with retries and exact-length framing."""

    def __init__(
        self,
        transport: BaseTransport,
        *,
        retries: int = 2,
        turnaround_s: float = 0.01,
    ) -> None:
        self.transport = transport
        self.retries = retries
        self.turnaround_s = turnaround_s
        self._lock = threading.Lock()
        self._last_io = 0.0
        self.transactions = 0
        self.failures = 0

    def read_registers(self, address: int, register: int, count: int = 1) -> list[int]:
        """Read ``count`` holding registers; returns unsigned 16-bit words."""
        request = read_request(address, register, count)
        reply = self._transact(request, address, READ_HOLDING, 5 + 2 * count)
        if reply[2] != 2 * count:
            raise ModbusError(f"slave {address}: byte count {reply[2]}, expected {2 * count}")
        data = reply[3:-2]
        return [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 2)]

    def read_register(self, address: int, register: int) -> int:
        return self.read_registers(address, register, 1)[0]

    def write_register(self, address: int, register: int, value: int) -> None:
        """Write one register (function 0x06) and verify the slave's echo."""
        request = write_request(address, register, value)
        reply = self._transact(request, address, WRITE_SINGLE, 8)
        if reply != request:
            raise ModbusError(f"slave {address}: write echo mismatch ({reply.hex(' ')})")

    # ------------------------------------------------------------ internals

    def _transact(self, request: bytes, address: int, function: int, expected: int) -> bytes:
        last_error: ModbusError | None = None
        with self._lock:
            for _attempt in range(self.retries + 1):
                self.transactions += 1
                try:
                    return self._attempt(request, address, function, expected)
                except ModbusExceptionReply:
                    # A well-formed refusal is an answer, not line noise.
                    self.failures += 1
                    raise
                except ModbusError as exc:
                    self.failures += 1
                    last_error = exc
        assert last_error is not None
        raise last_error

    def _attempt(self, request: bytes, address: int, function: int, expected: int) -> bytes:
        gap = self.turnaround_s - (time.monotonic() - self._last_io)
        if gap > 0:
            time.sleep(gap)
        self.transport.reset_input()
        self.transport.write(request)
        try:
            header = self._read_exact(3)
            if header[0] != address:
                raise ModbusError(f"reply from slave {header[0]}, expected {address}")
            if header[1] == function | 0x80:
                tail = self._read_exact(2)
                frame = header + tail
                if not check_crc(frame):
                    raise ModbusError(f"slave {address}: exception reply CRC error")
                raise ModbusExceptionReply(address, function, header[2])
            if header[1] != function:
                raise ModbusError(f"slave {address}: unexpected function {header[1]:#04x}")
            frame = header + self._read_exact(expected - 3)
            if not check_crc(frame):
                raise ModbusError(f"slave {address}: CRC error in {frame.hex(' ')}")
            return frame
        finally:
            self._last_io = time.monotonic()

    def _read_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.transport.read(size - len(data))
            if not chunk:
                raise ModbusError(f"timeout: got {len(data)} of {size} bytes")
            data += chunk
        return data
