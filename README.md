# ASC Oven Control

A modern PySide6 desktop replacement for the historical ASC LabVIEW TD48
thermal oven controller. The oven's three zones are each run by a Watlow
Series 96 PID controller; this application talks to them over Modbus RTU
and coordinates their set points to keep the zone-to-zone temperature
gradient small.

## Run the application

Requires Python ≥ 3.10.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
python -m asc_oven_control
```

or install the package and use the console script:

```bash
pip install -e .
asc-oven-control
```

The application starts in simulation mode. To drive the oven, open
**Setup → Instrument connection**, choose *Watlow hardware*, pick the
serial port (the USB–RS-485 adapter, a Silicon Labs CP210x — COM4 on the
lab PC), press **Test connection** (read-only) and then **Save
connection**. Runtime files (configuration and the run database) live in
the platform application-data directory; set `ASC_OVEN_HOME` to use a
specific runtime directory.

## Hardware (verified 2026-09-29)

- Three Watlow Series 96 controllers, Modbus slaves 1, 2, 3 (Zone 1–3),
  9600 baud 8N1, °C, whole degrees, type E thermocouples, set point range
  0–800 °C, PID in SI units.
- Registers: 100 process value, 103 output (×10 %), 106 alarm status,
  300 set point, 305 auto-tune, 500/501/503 prop band / integral /
  derivative. Full list in `asc_oven_control/infrastructure/watlow96.py`.

## Temperature control and gradient

The Watlow PID loops do the fast control; the PC supervises them
(`asc_oven_control/domain/zone_control.py`):

1. **Guaranteed ramp** – one master ramp advances at the profile rate
   but pauses if any zone falls more than the hold band behind (e.g.
   heater power off), so the oven never "jumps" when power returns.
2. **Leader limiting** – no zone's set point may exceed the coldest zone
   by more than the allowed gradient; the coldest zone keeps full drive.
3. **Approach deceleration** – the ramp slows near the target to avoid
   overshoot.
4. **Guaranteed soak** – the soak clock only runs while all zones are in
   band.

Set points are written only when their whole-degree value changes.
Stop, completion and communication failure set every zone to its lowest
set point (heaters off).

The PID terms found on the oven (prop band 47/65/47 °C, integral
12.5/60/12.5 min/repeat) are very loose: zones sit 20–30 °C behind a
moving set point and by different amounts, which is what caused the
10+ °C ramp gradients in the 2009 records. The **Controller tuning** page
reads/writes PID values and runs the Series 96 auto-tune on all three
zones together; do this once near the working temperature with heater
power on.

## Build the macOS application

```bash
pip install -e '.[build]'
pyinstaller ASC-Oven-Control.spec --noconfirm --clean
```

The native bundle is written to `dist/ASC Oven Control.app`. The build
uses the multi-resolution icon in `assets/ASC-Oven.icns` and includes the
PNG master for Qt window metadata.

## Features

- Five-page interface: **Setup**, **Live control**, **Run data**,
  **Controller tuning**, and **Instrument reference**.
- Three-zone temperature chart (Zone 1, Zone 2, Zone 3) plus the master
  ramp setpoint, drawn with a dependency-free QPainter widget; live zone
  set points, output power and gradient.
- Coordinated ramp/soak with pause (hold), resume, abort, and high/low
  alarms; per-zone set point and output power logged for tuning.
- Field coil control (ON/OFF, amplitude in µT) and controlled atmosphere
  (Air, Argon, Helium, Nitrogen), recovered from the LabVIEW front panel.
- Manual target, ramp-rate, and field adjustment during a run.
- Watlow PID read/write and simultaneous 3-zone auto-tune.
- SQLite run logging (`runs`/`samples` tables, WAL mode) and CSV export.
- Legacy data-table import/export in the exact format of the 2009 run
  records (`Time / Zone 1 / Zone 2 / Zone 3 / Current`, 0.5-minute steps).

## Architecture

- `asc_oven_control/domain` — validated run profile, phase, and
  atmosphere models; the gradient coordinator (`zone_control.py`); the
  physics-based 3-zone plant with Watlow-style PID used by simulation
  (`oven_plant.py`); alarm evaluation. Pure Python, no Qt.
- `asc_oven_control/infrastructure` — versioned configuration, SQLite run
  logger with atomic JSON helpers, serial transports, the Modbus RTU
  client (`modbus_rtu.py`), the Watlow Series 96 driver (`watlow96.py`),
  and the legacy data-table parser/renderer. Importing it has no side
  effects.
- `asc_oven_control/services` — oven backends (simulation / Watlow) and
  `RunEngine`: a QThread worker with signal-only communication and
  event-based pause/abort.
- `asc_oven_control/ui` — sidebar navigation, pages, theme (single QSS
  string), and the trend chart.
- `tools/extract_vi.py` — regenerates the reconstruction evidence from the
  LabVIEW binaries.
- `reconstructions/` — per-VI extraction evidence (XML + strings).
- `Labview/` — the original LabVIEW project, untouched.
- `legacy/` — the earlier single-file prototype, kept for reference.

## Safety status

Communication (reads and set point writes) is verified on the real
controllers. A heated hardware run has not been performed yet; see the
commissioning checklist in [LABVIEW_MIGRATION.md](LABVIEW_MIGRATION.md).
The Watlow controllers' own high alarm (Zone 2 output 2, latching) is
independent of this software and remains the primary over-temperature
protection.

## Tests

```bash
python -m pytest
```

The suite is Qt-free: it covers the Modbus RTU client (framing, CRC,
exceptions, retries, line noise), the Series 96 driver and hardware
backend against a fake bus seeded with register values read from the
oven, the gradient coordinator, closed-loop simulations comparing it to
the legacy shared-set-point strategy, configuration, persistence, and the
legacy data-table round trip.
