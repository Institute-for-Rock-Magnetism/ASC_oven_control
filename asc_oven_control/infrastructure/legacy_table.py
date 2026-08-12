"""Parser and renderer for the legacy ASC data-table format.

Recovered from the 2009 test records in ``Labview/Testing`` (e.g.
``test36_full_590deg``). Files are tab-separated ASCII:

    <date>\\t<time>
    <blank>
    <target> deg C\\t<field> uT\\t<atmosphere>
    <blank>
    Time\\tZone 1\\tZone 2\\tZone 3\\tCurrent
    0.0\\t36\\t25\\t27
    0.5\\t45\\t26\\t28

Samples are logged every 0.5 minutes. The Current column is optional and is
omitted from rows when the record has no current value, matching the
original writer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

COLUMN_HEADERS = ("Time", "Zone 1", "Zone 2", "Zone 3", "Current")


class LegacyTableError(ValueError):
    """Raised when a legacy table cannot be parsed."""


@dataclass(frozen=True, slots=True)
class LegacyRow:
    time_min: float
    zone1_c: float
    zone2_c: float
    zone3_c: float
    current_a: float | None = None


@dataclass(frozen=True, slots=True)
class LegacyTable:
    date: str
    time: str
    target_c: float | None
    field_uT: float | None
    atmosphere: str
    rows: tuple[LegacyRow, ...]

    def __len__(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "time": self.time,
            "target_c": self.target_c,
            "field_uT": self.field_uT,
            "atmosphere": self.atmosphere,
            "rows": [
                {
                    "time_min": row.time_min,
                    "zone1_c": row.zone1_c,
                    "zone2_c": row.zone2_c,
                    "zone3_c": row.zone3_c,
                    "current_a": row.current_a,
                }
                for row in self.rows
            ],
        }


def parse_legacy_table(text: str) -> LegacyTable:
    """Parse a legacy table from its text form."""
    lines = [line.rstrip("\n").rstrip("\r") for line in text.splitlines()]
    if len(lines) < 6:
        raise LegacyTableError("table is too short to be a legacy record")

    header = lines[0].split("\t")
    if len(header) < 2:
        raise LegacyTableError(f"expected date and time on line 1, got {lines[0]!r}")
    date, clock = header[0], header[1]

    if lines[1].strip() != "":
        raise LegacyTableError(f"expected a blank line after the date, got {lines[1]!r}")
    recipe_parts = lines[2].split("\t")
    target_match = re.fullmatch(r"\s*([0-9.]+)\s+deg C", recipe_parts[0])
    field_match = re.fullmatch(r"\s*([0-9.]+)\s+uT", recipe_parts[1]) if len(recipe_parts) > 1 else None
    atmosphere = recipe_parts[2].strip() if len(recipe_parts) > 2 else ""
    if target_match is None:
        raise LegacyTableError(f"expected '<target> deg C' on line 3, got {lines[2]!r}")
    target_c = float(target_match.group(1))
    field_uT = float(field_match.group(1)) if field_match else None

    headers = lines[4].split("\t")
    if not headers or headers[0].strip() != "Time":
        raise LegacyTableError(f"expected column header row on line 5, got {lines[4]!r}")

    rows: list[LegacyRow] = []
    for line_number, line in enumerate(lines[5:], start=6):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 4:
            raise LegacyTableError(
                f"line {line_number}: expected at least 4 columns, got {len(fields)}"
            )
        try:
            time_min = float(fields[0])
            zones = tuple(float(value) for value in fields[1:4])
            current = float(fields[4]) if len(fields) > 4 and fields[4].strip() else None
        except ValueError as exc:
            raise LegacyTableError(f"line {line_number}: non-numeric value {exc}") from exc
        rows.append(LegacyRow(time_min, zones[0], zones[1], zones[2], current))

    if not rows:
        raise LegacyTableError("table contains no data rows")
    return LegacyTable(date, clock, target_c, field_uT, atmosphere, tuple(rows))


def render_legacy_table(table: LegacyTable) -> str:
    """Render a legacy table back into the recovered ASCII layout."""
    target = "" if table.target_c is None else f"{table.target_c:g} deg C"
    field = "" if table.field_uT is None else f"{table.field_uT:g} uT"
    lines = [
        f"{table.date}\t{table.time}",
        "",
        "\t".join(part for part in (target, field, table.atmosphere) if part),
        "",
        "\t".join(COLUMN_HEADERS),
    ]
    for row in table.rows:
        fields = [f"{row.time_min:g}", f"{row.zone1_c:g}", f"{row.zone2_c:g}", f"{row.zone3_c:g}"]
        if row.current_a is not None:
            fields.append(f"{row.current_a:g}")
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"


def parse_legacy_file(path: Path | str) -> LegacyTable:
    """Parse a legacy table from disk (UTF-8, tolerating BOM)."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return parse_legacy_table(text)


def write_legacy_file(path: Path | str, table: LegacyTable) -> None:
    """Write a legacy table to disk."""
    Path(path).write_text(render_legacy_table(table), encoding="utf-8")
