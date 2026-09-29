"""Modbus RTU master tests against an in-memory slave bus."""

import unittest

from fake_bus import FakeBus

from asc_oven_control.infrastructure.modbus_rtu import (
    ModbusError,
    ModbusExceptionReply,
    ModbusRtuClient,
    check_crc,
    read_request,
    to_s16,
    write_request,
)


class FrameTest(unittest.TestCase):
    def test_read_request_matches_hardware_capture(self):
        # Captured on the ASC oven: slave 1, register 100 (process value).
        self.assertEqual(read_request(1, 100).hex(" "), "01 03 00 64 00 01 c5 d5")
        self.assertEqual(read_request(3, 100).hex(" "), "03 03 00 64 00 01 c4 37")

    def test_write_request_matches_legacy_change_sp(self):
        # Change SP.vi: function 6, Reg-H 1, Reg-L 44 (register 300).
        frame = write_request(2, 300, 590)
        self.assertEqual(frame[:6], bytes((2, 6, 1, 44, 590 >> 8, 590 & 0xFF)))
        self.assertTrue(check_crc(frame))

    def test_signed_decoding(self):
        self.assertEqual(to_s16(0x8300), -32000)
        self.assertEqual(to_s16(22), 22)

    def test_rejects_bad_arguments(self):
        for call in (
            lambda: read_request(0, 100),
            lambda: read_request(1, 70000),
            lambda: write_request(1, 300, 70000),
            lambda: write_request(1, 300, True),
        ):
            with self.assertRaises(ValueError):
                call()


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus({1: {100: 22, 300: 98}, 2: {100: 21, 300: 97}})
        self.client = ModbusRtuClient(self.bus, turnaround_s=0.0)

    def test_read_and_write(self):
        self.assertEqual(self.client.read_register(1, 100), 22)
        self.client.write_register(2, 300, 150)
        self.assertEqual(self.bus.slaves[2][300], 150)

    def test_hardware_reply_with_trailing_noise(self):
        # A reply followed by two stray bytes must still parse, and the
        # stray bytes must not leak into the next transaction.
        self.bus.noise = b"\x00\x00"
        self.assertEqual(self.client.read_register(1, 100), 22)
        self.assertEqual(self.client.read_register(2, 100), 21)

    def test_exception_reply_is_not_retried(self):
        with self.assertRaises(ModbusExceptionReply) as ctx:
            self.client.read_register(1, 999)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(len(self.bus.requests), 1)

    def test_retries_timeout_then_succeeds(self):
        self.bus.drop_next = 1
        self.assertEqual(self.client.read_register(1, 100), 22)
        self.assertEqual(len(self.bus.requests), 2)

    def test_retries_crc_error_then_succeeds(self):
        self.bus.corrupt_next = 1
        self.assertEqual(self.client.read_register(1, 100), 22)

    def test_silent_slave_raises_after_retries(self):
        with self.assertRaises(ModbusError):
            self.client.read_register(9, 100)
        self.assertEqual(len(self.bus.requests), 3)


if __name__ == "__main__":
    unittest.main()
