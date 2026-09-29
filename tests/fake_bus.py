"""In-memory Modbus RTU slave bus for tests, seeded from the real oven."""

from asc_oven_control.infrastructure.config import SerialProfile
from asc_oven_control.infrastructure.modbus_rtu import build_frame, check_crc
from asc_oven_control.infrastructure.serial_transport import BaseTransport

MINUS_32000 = 0x8300


def series96_registers(process=22, setpoint=98, band=47, integral=1250, derivative=90, remote=False):
    """Register image captured from the ASC oven's Zone 1 on 2026-09-29.

    ``remote=True`` reproduces the oven as found: set point from Input 2
    (reg 316 = 1, remote set point monitor reg 202).
    """
    return {
        0: 96, 3: 1, 4: 500,
        100: process, 101: 0, 102: MINUS_32000, 103: 1000,
        106: 0, 110: 0, 202: 2, 209: 0, 210: 0,
        300: setpoint, 301: 0, 304: 90, 305: 0,
        316: 1 if remote else 0,
        500: band, 501: integral, 502: 8, 503: derivative, 504: derivative, 505: 0, 506: 5,
        602: 0, 603: 800, 606: 0,
        900: 2, 901: 1,
        1100: 0, 1101: 300, 1102: 1,
    }


class FakeBus(BaseTransport):
    """Answers Modbus requests from per-slave register dictionaries."""

    def __init__(self, slaves):
        super().__init__(SerialProfile())
        self.slaves = slaves
        self.pending = b""
        self.requests = []
        self.drop_next = 0
        self.corrupt_next = 0
        self.noise = b""
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def reset_input(self):
        self.pending = b""

    def write(self, data):
        self.requests.append(data)
        if self.drop_next:
            self.drop_next -= 1
            return
        reply = self._reply(data)
        if reply is None:
            return
        if self.corrupt_next:
            self.corrupt_next -= 1
            reply = reply[:-1] + bytes(((reply[-1] ^ 0xFF),))
        self.pending += reply + self.noise

    def read(self, size):
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def writes_to(self, address, register):
        return [
            (r[4] << 8) | r[5]
            for r in self.requests
            if r[0] == address and r[1] == 6 and ((r[2] << 8) | r[3]) == register
        ]

    def _reply(self, frame):
        assert check_crc(frame)
        address, function = frame[0], frame[1]
        registers = self.slaves.get(address)
        if registers is None:
            return None
        register = (frame[2] << 8) | frame[3]
        if function == 3:
            count = (frame[4] << 8) | frame[5]
            if any(register + i not in registers for i in range(count)):
                return build_frame(bytes((address, 0x83, 2)))
            data = b"".join(registers[register + i].to_bytes(2, "big") for i in range(count))
            return build_frame(bytes((address, 3, len(data))) + data)
        if function == 6:
            if register not in registers:
                return build_frame(bytes((address, 0x86, 2)))
            registers[register] = (frame[4] << 8) | frame[5]
            return frame
        return build_frame(bytes((address, function | 0x80, 1)))
