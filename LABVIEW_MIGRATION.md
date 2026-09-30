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

### Constants recovered from the block-diagram default data (DFDS)

- `Watlow Read.vi`: Command = 3 (Modbus read holding registers), NR-L = 1.
- `Change SP.vi` / `Watlow Write.vi`: Command = 6 (write single
  register); `Change SP.vi` Reg-H = 1, Reg-L = 44 → register 300.
- `Ck_alarm.vi`: Adress = 2, Register = 106 (alarm 2 status).
- `Adjust_ramp_rate.vi`: registers 1100, 1101, 1102.
- `ASC_thermal2.0.vi`: ST-H = 100 (process value register), VISA COM3.

## Hardware verification (2026-09-29)

Performed with heater coil power off, oven at room temperature, on the
lab PC's COM4 (Silicon Labs CP210x USB-UART → RS-485).

1. **Model and bus.** Slaves 1, 2, 3 answer at 9600 8N1; register 0 = 96
   (Watlow Series 96, software 1 rev 5.00). Slaves 4–16 are silent.
   Register numbers match the Series 96 User's Manual (July 2005) A.3.
2. **Units/scaling.** Reg 901 = 1 (°C), 606 = 0 (whole degrees), 601 = 3
   (type E), 602/603 = 0/800 °C, 900 = 2 (SI PID units), output reg 103
   ×10 %, internal ramping (1100) off. PV 22/22/23 °C at ambient.
3. **Reliability.** 300 consecutive process-value reads, 0 errors, 0
   retries, ~100 ms per transaction; multi-register reads work. One early
   reply carried two trailing `00` bytes; the client reads exact lengths
   and flushes input before each request.
4. **Writes.** Set point (300) written on all three zones with the value
   already present; each echo verified and read back unchanged.
5. **Controller state found.** Set points 98/97/97 °C. PID set 1: prop
   band 47/65/47 °C, integral 12.5/60/12.5 min/repeat, derivative
   0.90/2.25/0.90 min, cycle 0.5 s. Zone 2 output 2 is a latching
   process high alarm. Register 103 ("percent output") read 1000 on all
   zones both before and after the set points were lowered to 0 °C, so
   its meaning is unverified until checked with heater power on.
6. **Engine dry run.** The run engine drove all three zones for 75 s
   (10 °C/min toward 100 °C, heater coil switched off): 384
   transactions, 0 failures, leader limiting active (Zone 3 at 23 °C held
   at 25 °C). Stop set every zone to 0 °C, confirmed by read-back and by
   the ramping set point register 203 following to 0.

7. **Set point source (first heated attempt).** All three controllers
   were in **Remote** set point mode (reg 316 = 1): the active set point
   came from Input 2 (0–5 V, scaled 0–800 °C, monitor reg 202), driven
   by the oven's onboard timer/controller, which also switches the fan.
   Writes to reg 300 were stored but ignored, and when the onboard timer
   reached 0 the remote set point fell to ~2 °C (outputs 0 %). Switching
   to Local (316 = 0) gave the PC control: with SP = PV + 5 °C the
   outputs read 10.8 / ~8 / 10.8 %, matching the 47/65/47 °C prop
   bands. Runs now switch every zone to Local (at set point 0 first);
   Setup → "Hand set point to oven panel" switches back to Remote. The
   earlier "outputs at 100 %" reading was the onboard remote set point.
   The fan is not wired to the Watlows; it is switched by the onboard
   timer circuit.

8. **Heated runs at 100 °C.** Run 2 (20 °C/min): Zones 1/3 overshot
   +10 °C and Zone 2, the middle zone, +16 °C at 0 % output (heater
   element lag plus heat from its neighbours); the UI hang at 53 min
   ended it (now the control loop runs in its own process). Run 3
   (10 °C/min, approach 40 °C at 20 %): completed unattended in 36 min,
   hold from 26 min, Zones 1/3 +2 °C, Zone 2 drifted to +5 °C during the
   hold at 0 % output with Zones 1/3 at 101 °C on 2–3 % output, gradient
   1–4 °C after the first 8 min. Next: Zones 1/3 offset about −4 °C at
   100 °C, and retune Zone 2 (it lags early in the ramp, output capped
   near 30 % by its 65 °C prop band).

## Remaining commissioning steps

0. Confirm whether the onboard timer also gates heater power (outputs
   above 0 % but no temperature rise ⇒ it does); if so, set it longer
   than the run.

1. First heated run at a low target (e.g. 100 °C, 5 °C/min) in hardware
   mode, watching all three zones and the gradient readout. The heater
   coil has a manual mains switch that Modbus cannot operate; switch it
   on only with the set points at 0 °C (the state Stop leaves). Check the
   front-panel output LEDs against register 103.
2. Auto-tune all three zones together near the working temperature
   (Controller tuning page), then compare logged runs before/after.
3. Refine `oven_plant.py` thermal parameters from a logged run so the
   simulation predicts the real gradient.
4. Confirm the Watlow alarm limits (321/322, currently −270/800 °C on
   all zones) are appropriate; they are the independent over-temperature
   protection.
5. Decide whether register 24 ("Disable Nonvolatile Memory") should be
   used; the app already writes set points only when their whole-degree
   value changes (~10 writes/min/zone at 10 °C/min).

`asc_oven_control/infrastructure/watlow_protocol.py` is the earlier
reconstruction of the frame builder (it predates the discovery that the
frames are standard Modbus RTU with a function code); the live code path
uses `modbus_rtu.py` and `watlow96.py`.
