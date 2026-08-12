# LabVIEW migration record — ASC TD48 oven control

## Evidence retained

The original binaries and 2009 run records are the primary evidence:

- `Labview/` — the complete historical project: `ASC_thermal.lvproj`,
  `ASC_thermal2.0.vi` (top-level program), the Watlow protocol VIs, the
  database VIs, and the `Testing/` folder of real run data (2009).
- `reconstructions/labview/<VI>/` — machine-extracted evidence per VI:
  pylabview `readRSRC` XML dumps (front panel, block diagram, connector
  pane, type descriptors) plus a printable-strings dump. Regenerate with
  `python tools/extract_vi.py Labview reconstructions/labview`.
- `Labview/Testing/test*.txt` — actual 2009 oven runs, e.g.
  `test36_full_590deg` (10/14/2009, 590 °C, Air), which pin the legacy
  data-table format and the physical 3-zone behavior.

## Recovered behavior

### Instrument

- The oven is a 3-zone thermal demagnetizer with an applied-field coil:
  the top-level front panel (`ASC_thermal2.0.vi`) charts three zones and
  heater current, exposes Field ON/OFF with an amplitude, and has fan,
  scale, and STOP controls. Run records show Zone 1 (sample zone) leading
  Zones 2 and 3 on every ramp.
- Controlled atmosphere: `Get_run_info.vi` carries Air, Argon, Helium, and
  Nitrogen choices; IRM mode and a user/batch identity form are present.
- A Gmail notification VI (`GmailLV80.vi`) and a Carleton Paleomag user
  database were part of the workflow; both are outside instrument control
  and are not implemented here.

### Watlow communication protocol

- NI-VISA serial: 9600 baud, 8 data bits, no parity, one stop bit, no flow
  control (`Watlow Read.vi`, `Watlow Write.vi`, `Change SP.vi`).
- Frame parts recovered from `Calc CRC-sub.vi`: Command, Adress, Reg H,
  Reg L, Data H, Data L, Number of Byte, and a CRC emitted as separate
  H byte / L byte outputs. The CRC register shifts right LSB-first
  (D0–D15) — the Modbus RTU CRC-16 family (poly 0xA001, init 0xFFFF).
- Operations: register read (`Watlow Read.vi`), register write
  (`Watlow Write.vi`), setpoint write (`Change SP.vi`), ramp-rate write
  (`Adjust_ramp_rate.vi`), stop (`stop_program.vi`), alarm poll
  (`Ck_alarm.vi`).
- PID terms (`PID_globals.vi`: PropBand, Integral, Derivative) and setpoint
  tracking (`Time_globals.vi`: SP, SP-20, Start Time) exist as global
  parameters.

### Legacy data-table format

Tab-separated ASCII; header of date/time, target (`590 deg C`), field
(`0 uT`), atmosphere; then `Time\tZone 1\tZone 2\tZone 3\tCurrent` rows at
0.5-minute intervals. The Current column is optional in rows. Parsed and
written by `asc_oven_control.infrastructure.legacy_table`.

## Hardware commissioning boundary

The binary evidence establishes the protocol family and frame parts but
NOT the values needed to talk to a real controller safely. Before a live
adapter can be enabled, supply and independently verify:

1. The exact Watlow model and its menu configuration (from the physical
   controller label): bus address, baud rate, data format, protocol mode.
2. Register addresses for temperature (all 3 zones), setpoint, ramp rate,
   alarm state, and stop; 16-bit word order; temperature scaling/units.
3. Complete command-response documentation and representative raw serial
   logs from a session captured on the real instrument.
4. Response validation: expected reply length, byte order, error frames.
5. Physical safety interlocks: ramp limits, soak termination, STOP path,
   and emergency-stop behavior.

Until those items are available, simulation is the only enabled execution
mode. The protocol builders in
`asc_oven_control/infrastructure/watlow_protocol.py` are a reference for
commissioning; the transport factory (`create_transport`) defaults to
simulation, requires an explicit non-None port plus `simulation=False` to
reach `PySerialTransport`, and the GUI never instantiates a hardware
transport.
