"""Tests for configuration safety and round-tripping."""

import unittest

from asc_oven_control.infrastructure.config import (
    ApplicationConfig,
    ConfigValidationError,
    SerialProfile,
)


class SerialProfileTest(unittest.TestCase):
    def test_defaults_are_hardware_free(self):
        profile = SerialProfile()
        self.assertIsNone(profile.port)
        self.assertEqual(profile.baudrate, 9600)

    def test_round_trip(self):
        profile = SerialProfile(port="/dev/ttyUSB0", baudrate=19200)
        restored = SerialProfile.from_dict(profile.to_dict())
        self.assertEqual(profile, restored)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigValidationError):
            SerialProfile.from_dict({"port": "x", "baudrate": 9600, "surprise": 1})

    def test_bad_values_rejected(self):
        for bad in ({"baudrate": 1234}, {"parity": "X"}, {"stopbits": 3}, {"bytesize": 9}):
            with self.assertRaises(ConfigValidationError):
                SerialProfile(**bad)


class ApplicationConfigTest(unittest.TestCase):
    def test_defaults_are_safe(self):
        config = ApplicationConfig()
        self.assertTrue(config.simulation_mode)
        self.assertIsNone(config.serial.port)
        self.assertIsInstance(config.serial, SerialProfile)

    def test_round_trip(self):
        config = ApplicationConfig(data_dir="/tmp/oven", poll_seconds=1.0)
        restored = ApplicationConfig.from_dict(config.to_dict())
        self.assertEqual(config, restored)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigValidationError):
            ApplicationConfig.from_dict({"simulation_mode": True, "extra": 1})

    def test_non_bool_simulation_rejected(self):
        with self.assertRaises(ConfigValidationError):
            ApplicationConfig(simulation_mode="yes")


if __name__ == "__main__":
    unittest.main()
