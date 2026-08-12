# ASC Oven Control

A modern PySide6 desktop replacement for the historical ASC LabVIEW TD48
thermal oven controller, built with the same simulation-first approach as
the [Long Core Control](https://github.com/) migration: the LabVIEW
binaries are reverse-engineered into a documented evidence record, and the
application runs a deterministic 3-zone simulation until the Watlow
protocol is independently verified on hardware.

## Run the application

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m asc_oven_control
```

or install the package and use the console script:

```bash
pip install -e .
asc-oven-control
```

The application always starts in simulation mode and never opens a
physical serial port. Runtime files (configuration and the run database)
live in the platform application-data directory; set `ASC_OVEN_HOME` to
use a specific runtime directory.

## Features

- Four-page interface: **Setup**, **Live control**, **Run data**, and
  **Instrument reference** (read-only protocol evidence).
- Three-zone temperature chart (Zone 1, Zone 2, Zone 3) plus heater
  current and commanded setpoint, drawn with a dependency-free QPainter
  widget.
- Ramp/soak state machine with pause, resume, abort, and high/low alarms.
- Field coil control (ON/OFF, amplitude in µT) and controlled atmosphere
  (Air, Argon, Helium, Nitrogen), recovered from the LabVIEW front panel.
- Manual target, ramp-rate, and field adjustment during a run.
- Watlow PID reference fields (proportional band, integral, derivative) —
  display only, marked unverified.
- SQLite run logging (`runs`/`samples` tables, WAL mode) and CSV export.
- Legacy data-table import/export in the exact format of the 2009 run
  records (`Time / Zone 1 / Zone 2 / Zone 3 / Current`, 0.5-minute steps).

## Architecture

- `asc_oven_control/domain` — validated run profile, phase, and
  atmosphere models; ramp/soak math; the deterministic 3-zone thermal
  simulation; alarm evaluation. Pure Python, no Qt.
- `asc_oven_control/infrastructure` — versioned configuration, SQLite run
  logger with atomic JSON helpers, serial transports (simulation-first
  factory), the recovered Watlow CRC-framed protocol builders, and the
  legacy data-table parser/renderer. Importing it has no side effects.
- `asc_oven_control/services` — `RunEngine`: a QThread worker with
  signal-only communication and event-based pause/abort.
- `asc_oven_control/ui` — sidebar navigation, pages, theme (single QSS
  string), and the trend chart.
- `tools/extract_vi.py` — regenerates the reconstruction evidence from the
  LabVIEW binaries.
- `reconstructions/` — per-VI extraction evidence (XML + strings).
- `Labview/` — the original LabVIEW project, untouched.
- `legacy/` — the earlier single-file prototype, kept for reference.

## Safety status

The software is production-structured and the simulation workflow is
usable, but physical hardware operation is intentionally locked. The
binary LabVIEW evidence establishes the protocol family (CRC-framed
register reads/writes over NI-VISA serial at 9600 baud 8N1) without the
values needed for safe live control: slave address, register map, word
order, scaling, and response validation. See
[LABVIEW_MIGRATION.md](LABVIEW_MIGRATION.md) for the full evidence record
and the commissioning checklist.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite is Qt-free: it covers the CRC-16 builders against Modbus
reference vectors, configuration safety (simulation default, no ports),
transport fail-closed behavior, domain validation, and the legacy
data-table round trip.
