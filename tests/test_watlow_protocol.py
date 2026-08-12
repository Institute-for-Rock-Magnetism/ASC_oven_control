"""Tests for the recovered Watlow CRC-framed protocol builders."""

import unittest

from asc_oven_control.infrastructure.watlow_protocol import (
    ProtocolValidationError,
    WatlowCommands,
    WatlowFrame,
    crc16,
    parse_read_reply,
    verify_frame,
)


class Crc16Test(unittest.TestCase):
    def test_modbus_check_value(self):
        # The CRC-16/MODBUS catalogue check value for "123456789" is 0x4B37.
        self.assertEqual(crc16(b"123456789"), 0x4B37)

    def test_empty_input(self):
        self.assertEqual(crc16(b""), 0xFFFF)

    def test_frame_round_trip(self):
        frame = WatlowCommands.read(1, 0x00A8, 2).to_bytes()
        self.assertTrue(verify_frame(frame))
        self.assertFalse(verify_frame(frame[:-2] + b"\x00\x00"))

    def test_frame_layout(self):
        # address, register hi, register lo, count, then CRC low, CRC high.
        raw = WatlowCommands.read(0x01, 0x00A8, 2).to_bytes()
        self.assertEqual(raw[:4], bytes((0x01, 0x00, 0xA8, 0x02)))
        crc = crc16(raw[:-2])
        self.assertEqual(raw[-2:], bytes((crc & 0xFF, crc >> 8)))

    def test_setpoint_payload(self):
        raw = WatlowCommands.setpoint(1, 0x00A8, 590).to_bytes()
        self.assertEqual(raw[3], 2)
        self.assertEqual(raw[4:6], bytes((0x02, 0x4E)))  # 590 = 0x024E
        self.assertTrue(verify_frame(raw))

    def test_stop_frame(self):
        raw = WatlowCommands.stop(1, 0x00AA).to_bytes()
        self.assertEqual(raw[3], 0)
        self.assertTrue(verify_frame(raw))

    def test_out_of_range_rejected(self):
        with self.assertRaises(ProtocolValidationError):
            WatlowCommands.read(256, 0x00A8, 2).to_bytes()
        with self.assertRaises(ProtocolValidationError):
            WatlowCommands.write(1, 0x00A8, 70000).to_bytes()
        with self.assertRaises(ProtocolValidationError):
            WatlowFrame("BOGUS", 1).to_bytes()

    def test_parse_read_reply(self):
        self.assertEqual(parse_read_reply(b"\x01\x02\x03\x04", 4), b"\x01\x02\x03\x04")
        with self.assertRaises(ProtocolValidationError):
            parse_read_reply(b"\x01\x02", 4)


if __name__ == "__main__":
    unittest.main()
