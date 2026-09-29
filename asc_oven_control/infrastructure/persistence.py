"""Run persistence: SQLite run/sample logging and atomic JSON writes.

The logger schema mirrors the legacy LabVIEW records (three temperature
zones plus heater current) and is safe to call from the engine worker
thread (``check_same_thread=False`` with an internal lock).
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from asc_oven_control.domain.models import RunProfile, SamplePoint

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    operator TEXT, batch_id TEXT, sample_id TEXT, user_name TEXT,
    atmosphere TEXT, target_setpoint_c REAL,
    ramp_rate_c_per_min REAL, soak_time_sec REAL,
    alarm_high_c REAL, alarm_low_c REAL, notes TEXT,
    field_enabled INTEGER, field_amplitude_uT REAL,
    stopped_at TEXT, status TEXT
);
"""

SAMPLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, timestamp TEXT NOT NULL,
    elapsed_sec REAL NOT NULL,
    zone1_c REAL NOT NULL, zone2_c REAL NOT NULL, zone3_c REAL NOT NULL,
    current_a REAL,
    output_setpoint_c REAL NOT NULL, target_setpoint_c REAL NOT NULL,
    phase TEXT NOT NULL, alarm TEXT, connected INTEGER NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);
"""

# Per-zone controller setpoints and output power, added for PID tuning and
# gradient analysis. Older databases are migrated in place (nullable).
DETAIL_COLUMNS = ("zone1_sp_c", "zone2_sp_c", "zone3_sp_c", "zone1_power_pct", "zone2_power_pct", "zone3_power_pct")


class RunLogger:
    """Thread-safe SQLite store for runs and per-sample observations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(RUNS_SCHEMA + SAMPLES_SCHEMA)
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(samples)")}
        for column in DETAIL_COLUMNS:
            if column not in existing:
                self.conn.execute(f"ALTER TABLE samples ADD COLUMN {column} REAL")
        self.conn.commit()

    def start_run(self, profile: RunProfile) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO runs (
                    started_at, operator, batch_id, sample_id, user_name,
                    atmosphere, target_setpoint_c, ramp_rate_c_per_min,
                    soak_time_sec, alarm_high_c, alarm_low_c, notes,
                    field_enabled, field_amplitude_uT, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    profile.operator,
                    profile.batch_id,
                    profile.sample_id,
                    profile.user_name,
                    str(profile.atmosphere),
                    profile.target_setpoint_c,
                    profile.ramp_rate_c_per_min,
                    profile.soak_time_sec,
                    profile.alarm_high_c,
                    profile.alarm_low_c,
                    profile.notes,
                    int(profile.field_enabled),
                    profile.field_amplitude_uT,
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str = "stopped") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE runs SET stopped_at = ?, status = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), status, run_id),
            )
            self.conn.commit()

    def log_sample(self, run_id: int, sample: SamplePoint) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO samples (
                    run_id, timestamp, elapsed_sec,
                    zone1_c, zone2_c, zone3_c, current_a,
                    output_setpoint_c, target_setpoint_c, phase, alarm, connected,
                    zone1_sp_c, zone2_sp_c, zone3_sp_c,
                    zone1_power_pct, zone2_power_pct, zone3_power_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    datetime.fromtimestamp(sample.timestamp).isoformat(timespec="seconds"),
                    sample.elapsed_sec,
                    sample.zone_temps_c[0],
                    sample.zone_temps_c[1],
                    sample.zone_temps_c[2],
                    sample.current_a,
                    sample.output_setpoint_c,
                    sample.target_setpoint_c,
                    str(sample.phase),
                    sample.alarm,
                    int(sample.connected),
                    *sample.zone_setpoints_c,
                    *sample.zone_power_pct,
                ),
            )
            self.conn.commit()

    def latest_run_id(self) -> int | None:
        with self._lock:
            row = self.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None

    def get_samples(self, run_id: int, limit: int = 2000) -> list[tuple]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT timestamp, elapsed_sec, zone1_c, zone2_c, zone3_c,
                       current_a, output_setpoint_c, target_setpoint_c, phase, alarm, connected
                FROM samples WHERE run_id = ? ORDER BY id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def get_detailed_samples(self, run_id: int, limit: int = 10_000_000) -> list[tuple]:
        """Samples with the per-zone setpoint/power columns appended."""
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT timestamp, elapsed_sec, zone1_c, zone2_c, zone3_c,
                       current_a, output_setpoint_c, target_setpoint_c, phase, alarm, connected,
                       {", ".join(DETAIL_COLUMNS)}
                FROM samples WHERE run_id = ? ORDER BY id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def export_samples_csv(rows: Iterable[tuple], target: Path | str) -> None:
    """Write logged samples as a CSV with the canonical column order."""
    with open(target, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            [
                "timestamp",
                "elapsed_sec",
                "zone1_c",
                "zone2_c",
                "zone3_c",
                "current_a",
                "output_setpoint_c",
                "target_setpoint_c",
                "phase",
                "alarm",
                "connected",
                *DETAIL_COLUMNS,
            ]
        )
        writer.writerows(rows)


LIVE_CSV_COLUMNS = (
    "timestamp", "elapsed_min",
    "zone1_c", "zone2_c", "zone3_c",
    "zone1_sp_c", "zone2_sp_c", "zone3_sp_c",
    "zone1_power_pct", "zone2_power_pct", "zone3_power_pct",
    "ramp_setpoint_c", "target_c", "gradient_c",
    "phase", "control_phase", "soak_elapsed_s", "out_of_band_s", "alarm",
)


class LiveCsvLog:
    """Appends one row per engine snapshot and flushes it immediately.

    The file is complete up to the last poll even if the application is
    closed mid-run; the SQLite database holds the same samples.
    """

    def __init__(self, path: Path | str, header_lines: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "w", newline="", encoding="utf-8")
        for line in header_lines:
            self._handle.write(f"# {line}\n")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(LIVE_CSV_COLUMNS)
        self._handle.flush()

    def write_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._handle.closed:
            return
        zones = snapshot["zones"]
        setpoints = snapshot.get("zone_setpoints", (None, None, None))
        powers = snapshot.get("zone_power", (None, None, None))
        self._writer.writerow(
            (
                datetime.fromtimestamp(snapshot["timestamp"]).isoformat(timespec="seconds"),
                f"{snapshot['elapsed_sec'] / 60.0:.3f}",
                *zones,
                *setpoints,
                *(None if p is None else f"{p:.1f}" for p in powers),
                f"{snapshot['output_setpoint_c']:.2f}",
                snapshot["target_setpoint_c"],
                snapshot.get("gradient_c", max(zones) - min(zones)),
                snapshot["phase"],
                snapshot.get("control_phase", ""),
                f"{snapshot.get('soak_elapsed_s', 0.0):.0f}",
                f"{snapshot.get('out_of_band_s', 0.0):.0f}",
                snapshot.get("alarm", ""),
            )
        )
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


def atomic_write_json(path: Path | str, data: dict[str, Any], create_backup: bool = True) -> None:
    """Write ``data`` as pretty JSON via temp file + fsync + atomic replace.

    When ``create_backup`` is set the previous file is preserved as
    ``<path>.bak`` so a corrupt write can be recovered.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"

    descriptor, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if create_backup and target.exists():
            _replace_file(target, target.with_suffix(target.suffix + ".bak"))
        _replace_file(Path(tmp_name), target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _replace_file(source: Path, destination: Path) -> None:
    """Move ``source`` over ``destination`` and fsync the directory."""
    os.replace(source, destination)
    try:
        directory_fd = os.open(destination.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
