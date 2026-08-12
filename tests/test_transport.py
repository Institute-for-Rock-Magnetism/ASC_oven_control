"""Tests for the transport factory and its fail-closed safety model."""

import unittest

from asc_oven_control.infrastructure.config import SerialProfile
from asc_oven_control.infrastructure.serial_transport import (
    CommunicationError,
    DisconnectedTransport,
    PySerialTransport,
    SimulatedSerialTransport,
    create_transport,
)


class TransportFactoryTest(unittest.TestCase):
    def test_simulation_is_the_default(self):
        transport = create_transport(SerialProfile())
        self.assertIsInstance(transport, SimulatedSerialTransport)

    def test_no_port_means_disconnected(self):
        transport = create_transport(SerialProfile(), simulation=False)
        self.assertIsInstance(transport, DisconnectedTransport)

    def test_port_required_for_hardware(self):
        with self.assertRaises(CommunicationError):
            PySerialTransport(SerialProfile())  # port is None

    def test_hardware_transport_constructed_but_not_opened(self):
        # Constructing must perform no I/O; only open() touches pyserial.
        transport = create_transport(SerialProfile(port="/dev/ttyUSB0"), simulation=False)
        self.assertIsInstance(transport, PySerialTransport)
        self.assertFalse(transport.is_connected())


class DisconnectedTransportTest(unittest.TestCase):
    def test_fails_closed(self):
        transport = DisconnectedTransport(SerialProfile())
        for call in (transport.connect, lambda: transport.write(b"x"), lambda: transport.read(1)):
            with self.assertRaises(CommunicationError):
                call()
        self.assertFalse(transport.is_connected())
        transport.disconnect()  # never raises


class SimulatedTransportTest(unittest.TestCase):
    def test_round_trip(self):
        transport = SimulatedSerialTransport(SerialProfile())
        transport.connect()
        transport.queue_response(b"abc")
        self.assertEqual(transport.read(3), b"abc")
        transport.write(b"xyz")
        self.assertEqual(transport.writes, [b"xyz"])
        transport.disconnect()
        with self.assertRaises(CommunicationError):
            transport.write(b"nope")

    def test_read_returns_empty_when_nothing_queued(self):
        transport = SimulatedSerialTransport(SerialProfile())
        transport.connect()
        self.assertEqual(transport.read(4), b"")


if __name__ == "__main__":
    unittest.main()
