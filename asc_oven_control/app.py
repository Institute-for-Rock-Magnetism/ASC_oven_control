"""Application bootstrap: configuration, logging, and window construction.

Runtime files (config, run database) live in the platform application-data
directory unless ``ASC_OVEN_HOME`` is set. The application always starts in
simulation mode; see ``LABVIEW_MIGRATION.md`` for the hardware boundary.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from asc_oven_control.infrastructure.config import ApplicationConfig, ConfigValidationError
from asc_oven_control.infrastructure.persistence import RunLogger
from asc_oven_control.ui.main_window import MainWindow
from asc_oven_control.ui.theme import APP_STYLE

APP_NAME = "ASC Oven Control"
APP_VERSION = "0.1.0"


def app_home() -> Path:
    """Runtime directory: ``ASC_OVEN_HOME`` override or platform app data."""
    override = os.environ.get("ASC_OVEN_HOME")
    if override:
        return Path(override).expanduser()
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(base) / "ASC_oven_control"


def load_config(home: Path) -> ApplicationConfig:
    """Load the application config, recovering from corruption with defaults.

    The config file is optional: its absence (first launch) yields safe
    simulation defaults, so hardware is never accidentally configured.
    """
    path = home / "config" / "application.json"
    if not path.exists():
        return ApplicationConfig(data_dir=str(home))
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ApplicationConfig.from_dict(data)
    except (OSError, ValueError, ConfigValidationError):
        quarantine = path.with_name(f"application.corrupt-{os.getpid()}.json")
        try:
            path.rename(quarantine)
        except OSError:
            pass
        return ApplicationConfig(data_dir=str(home), simulation_mode=True)


def create_application(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    """Build the QApplication and main window with safe defaults."""
    app = QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)

    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    config = load_config(home)
    logger = RunLogger(home / "asc_oven_runs.db")
    window = MainWindow(config, logger)
    return app, window


def main() -> int:
    app, window = create_application()

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:  # noqa: ANN001
        import traceback

        traceback.print_exception(exc_type, exc_value, exc_traceback)
        QMessageBox.critical(window, "Unexpected error", f"{exc_type.__name__}: {exc_value}")

    sys.excepthook = handle_exception
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
