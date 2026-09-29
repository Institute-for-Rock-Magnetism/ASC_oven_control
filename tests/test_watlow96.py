"""Series 96 driver and hardware backend against a fake bus seeded from the oven."""

import unittest

from fake_bus import FakeBus, series96_registers

from asc_oven_control.infrastructure.config import ApplicationConfig, SerialProfile
from asc_oven_control.infrastructure.modbus_rtu import ModbusError, ModbusRtuClient
from asc_oven_control.infrastructure.watlow96 import PidSettings, Watlow96, Watlow96Error
from asc_oven_control.services.oven_backend import WatlowOven


def oven_bus():
    return FakeBus({
        1: series96_registers(process=22, setpoint=98),
        2: series96_registers(process=21, setpoint=97, band=65, integral=6000, derivative=225),
        3: series96_registers(process=23, setpoint=97),
    })


class Watlow96Test(unittest.TestCase):
    def setUp(self):
        self.bus = oven_bus()
        self.client = ModbusRtuClient(self.bus, turnaround_s=0.0)
        self.zone = Watlow96(self.client, 1)

    def test_identify_reads_units_and_limits(self):
        identity = self.zone.identify()
        self.assertEqual(identity.model, 96)
        self.assertTrue(identity.celsius)
        self.assertTrue(identity.si_pid_units)
        self.assertEqual(self.zone.setpoint_limits(), (0, 800))

    def test_rejects_non_series96(self):
        self.bus.slaves[1][0] = 97
        with self.assertRaises(Watlow96Error):
            self.zone.identify()

    def test_rejects_fahrenheit(self):
        self.bus.slaves[1][901] = 0
        with self.assertRaises(Watlow96Error):
            self.zone.identify()

    def test_status_decodes_hardware_values(self):
        status = self.zone.read_status()
        self.assertEqual(status.process_c, 22.0)
        self.assertEqual(status.setpoint_c, 98.0)
        self.assertEqual(status.power_pct, 100.0)
        self.assertEqual(status.alarms, ())

    def test_status_reports_open_thermocouple_and_alarm(self):
        self.bus.slaves[1][101] = 3
        self.bus.slaves[1][106] = 3
        alarms = self.zone.read_status().alarms
        self.assertIn("input over sensor range (open thermocouple?)", alarms)
        self.assertIn("alarm high (latched)", alarms)

    def test_decimal_scaling(self):
        self.bus.slaves[1][606] = 1
        self.bus.slaves[1][100] = 1275
        self.assertEqual(self.zone.read_status().process_c, 127.5)
        self.zone.write_setpoint(50.5)
        self.assertEqual(self.bus.slaves[1][300], 505)

    def test_setpoint_write_is_range_checked(self):
        self.zone.write_setpoint(590)
        self.assertEqual(self.bus.slaves[1][300], 590)
        with self.assertRaises(ValueError):
            self.zone.write_setpoint(900)
        self.assertEqual(self.zone.clamp_setpoint(-5), 0)

    def test_read_pid_matches_oven(self):
        self.assertEqual(self.zone.read_pid(), PidSettings(47.0, 12.5, 0.9, 0.5))
        zone2 = Watlow96(self.client, 2).read_pid()
        self.assertEqual((zone2.prop_band_c, zone2.integral_min, zone2.derivative_min), (65.0, 60.0, 2.25))

    def test_write_pid_si_units(self):
        self.zone.write_pid(PidSettings(15.0, 3.0, 0.3, 0.5))
        registers = self.bus.slaves[1]
        self.assertEqual((registers[500], registers[501], registers[503]), (15, 300, 30))
        with self.assertRaises(ValueError):
            self.zone.write_pid(PidSettings(0.0, 3.0, 0.3, 0.5))  # would select on/off

    def test_autotune_requires_auto_mode(self):
        self.zone.start_autotune(90)
        self.assertEqual(self.bus.slaves[1][305], 1)
        self.bus.slaves[1][301] = 1
        with self.assertRaises(Watlow96Error):
            self.zone.start_autotune(90)


class WatlowOvenBackendTest(unittest.TestCase):
    def make(self):
        config = ApplicationConfig(simulation_mode=False, serial=SerialProfile(port="COM4"))
        backend = WatlowOven(config)
        bus = oven_bus()
        backend.transport = bus
        backend.client.transport = bus
        return backend, bus

    def test_read_all_zones(self):
        backend, _ = self.make()
        backend.connect()
        reading = backend.read()
        self.assertEqual(reading.zones_c, (22.0, 21.0, 23.0))
        self.assertEqual(reading.setpoints_c, (98.0, 97.0, 97.0))

    def test_setpoints_written_only_on_change(self):
        backend, bus = self.make()
        backend.connect()
        backend.write_setpoints((98.2, 97.0, 150.4))  # zones 1/2 unchanged after rounding
        self.assertEqual(bus.writes_to(1, 300), [])
        self.assertEqual(bus.writes_to(2, 300), [])
        self.assertEqual(bus.writes_to(3, 300), [150])
        backend.write_setpoints((98.0, 97.0, 150.0))
        self.assertEqual(bus.writes_to(3, 300), [150])

    def test_safe_shutdown_drives_all_zones_to_range_low(self):
        backend, bus = self.make()
        backend.connect()
        backend.safe_shutdown()
        for address in (1, 2, 3):
            self.assertEqual(bus.slaves[address][300], 0)

    def test_transient_failure_uses_last_value_then_fails(self):
        backend, bus = self.make()
        backend.connect()
        backend.read()
        bus.drop_next = 3  # one read of zone 1 exhausts its retries
        self.assertEqual(backend.read().zones_c, (22.0, 21.0, 23.0))
        bus.drop_next = 10_000
        with self.assertRaises(ModbusError):
            for _ in range(10):
                backend.read()


if __name__ == "__main__":
    unittest.main()
