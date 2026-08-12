# LabVIEW reconstruction evidence

Machine-extracted evidence from the historical ASC LabVIEW binaries, one
folder per VI:

- `<VI>.xml` — pylabview `readRSRC` dump (front panel, block diagram,
  connector pane, type descriptors, defaults). Generated with
  `python -m pylabview.readRSRC -x -i <VI>.vi -m <VI>.xml`.
- `<VI>_FPHb.xml` — front panel bytes (control/indicator labels, enum
  values, defaults).
- `<VI>_BDHb.xml` — block diagram bytes (sub-VI references, structure
  types, numeric constants).
- `strings.txt` — printable strings from the binary (names, error text,
  serial settings, dependencies).

Regenerate everything with:

```bash
python tools/extract_vi.py Labview reconstructions/labview
```

The 2009 run records in `Labview/Testing/` are primary evidence for the
physical 3-zone behavior and the legacy data-table format, and are
referenced by `tests/test_legacy_table.py` (via a reproduced excerpt).
