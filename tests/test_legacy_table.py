"""Tests for the legacy data-table parser, using a real 2009 record."""

import unittest

from asc_oven_control.infrastructure.legacy_table import (
    LegacyRow,
    LegacyTable,
    LegacyTableError,
    parse_legacy_table,
    render_legacy_table,
)

# Reproduced from Labview/Testing/test36_full_590deg (2009-10-14).
REAL_RECORD = """10/14/2009\t11:28 AM

590 deg C\t0 uT\tAir

Time\tZone 1\tZone 2\tZone 3\tCurrent
0.0\t36\t25\t27
0.5\t45\t26\t28
1.0\t56\t28\t31
1.5\t62\t32\t34
2.0\t65\t35\t38
"""


class ParseTest(unittest.TestCase):
    def test_parses_real_record(self):
        table = parse_legacy_table(REAL_RECORD)
        self.assertEqual(table.date, "10/14/2009")
        self.assertEqual(table.time, "11:28 AM")
        self.assertEqual(table.target_c, 590.0)
        self.assertEqual(table.field_uT, 0.0)
        self.assertEqual(table.atmosphere, "Air")
        self.assertEqual(len(table), 5)
        self.assertEqual(table.rows[0], LegacyRow(0.0, 36.0, 25.0, 27.0, None))

    def test_round_trip(self):
        table = parse_legacy_table(REAL_RECORD)
        rendered = render_legacy_table(table)
        reparsed = parse_legacy_table(rendered)
        self.assertEqual(table, reparsed)

    def test_current_column_optional(self):
        text = render_legacy_table(
            LegacyTable(
                date="10/14/2009",
                time="11:28 AM",
                target_c=590.0,
                field_uT=0.0,
                atmosphere="Air",
                rows=(LegacyRow(0.0, 36.0, 25.0, 27.0, 2.5),),
            )
        )
        self.assertIn("Current", text)
        self.assertIn("2.5", text)

    def test_short_input_rejected(self):
        with self.assertRaises(LegacyTableError):
            parse_legacy_table("too\nshort")

    def test_bad_header_rejected(self):
        with self.assertRaises(LegacyTableError):
            parse_legacy_table("10/14/2009\t11:28 AM\n\nnot a target\n\nTime\tZone 1\n0.0\t1\n")

    def test_empty_rows_rejected(self):
        with self.assertRaises(LegacyTableError):
            parse_legacy_table("10/14/2009\t11:28 AM\n\n590 deg C\t0 uT\tAir\n\nTime\tZone 1\n")


if __name__ == "__main__":
    unittest.main()
