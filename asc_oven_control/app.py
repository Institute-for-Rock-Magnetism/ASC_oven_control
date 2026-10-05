"""Application bootstrap: configuration, logging, and window construction.

Runtime files (config, run database) live in the platform application-data
directory unless ``ASC_OVEN_HOME`` is set. The application starts in
simulation mode until hardware mode is saved from the Setup page.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from asc_oven_control.infrastructure.config import ApplicationConfig, ConfigValidationError
from asc_oven_control.infrastructure.persistence import RunLogger
from asc_oven_control.ui.main_window import MainWindow
from asc_oven_control.ui.theme import build_style

APP_NAME = "ASC Oven Control"
APP_VERSION = "0.1.0"
APP_USER_MODEL_ID = "edu.umn.irm.asc-oven-control"


def resource_path(relative_path: str) -> Path:
    """Resolve project assets in source and PyInstaller bundles."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / relative_path


def app_home() -> Path:
    """Runtime directory: ``ASC_OVEN_HOME`` override, else ``Documents/ASC Oven Control``.

    Not AppData: on Windows, programs started from a packaged (MSIX) app
    such as the Claude desktop app have their AppData writes redirected
    into that package's private folder, so two launches of this app could
    see different settings and run histories (2026-10-05: an instance
    started from Explorer found no settings and fell back to simulation).
    Documents is never redirected.
    """
    override = os.environ.get("ASC_OVEN_HOME")
    if override:
        return Path(override).expanduser()
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    if documents:
        return Path(documents) / "ASC Oven Control"
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
    if sys.platform == "win32":
        # Own taskbar identity: otherwise Windows groups the window under
        # python(w).exe and shows that (blank) icon instead of ours.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    app = QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("ASC Laboratory")
    icon_file = "assets/asc_oven_icon.ico" if sys.platform == "win32" else "assets/asc_oven_icon.png"
    icon_path = resource_path(icon_file)
    if not icon_path.exists():
        icon_path = resource_path("assets/asc_oven_icon.png")
    app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyle("Fusion")
    app.setStyleSheet(build_style(resource_path("assets/ui")))

    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    config = load_config(home)
    logger = RunLogger(home / "asc_oven_runs.db")
    window = MainWindow(config, logger, config_path=home / "config" / "application.json")
    return app, window


def install_diagnostics(app: QApplication, log_dir: Path):
    """Crash log plus a hang recorder for the UI thread.

    The first heated run was lost to a UI hang that Windows closed without
    any trace. Now: stderr (absent under pythonw) and fatal errors go to
    ``logs/app.log``, and if the UI thread stops processing events for more
    than 4 s, every thread's stack is written to ``logs/hang-*.txt``.
    """
    import faulthandler
    import threading
    import time
    from datetime import datetime

    from PySide6.QtCore import QTimer

    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    log.write(f"\n--- started {datetime.now():%Y-%m-%d %H:%M:%S} pid {os.getpid()}\n")
    if sys.stderr is None or not sys.stderr.isatty():
        sys.stderr = log
    faulthandler.enable(log, all_threads=True)

    beat = [time.monotonic()]
    timer = QTimer(app)
    timer.timeout.connect(lambda: beat.__setitem__(0, time.monotonic()))
    timer.start(500)

    def watch() -> None:
        dumped = False
        while True:
            time.sleep(1.0)
            stalled = time.monotonic() - beat[0]
            if stalled > 4.0 and not dumped:
                path = log_dir / f"hang-{datetime.now():%Y%m%d-%H%M%S}.txt"
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(f"UI thread has not processed events for {stalled:.1f} s\n\n")
                    faulthandler.dump_traceback(handle, all_threads=True)
                log.write(f"UI hang recorded in {path}\n")
                dumped = True
            elif stalled < 2.0:
                dumped = False

    threading.Thread(target=watch, name="hang-recorder", daemon=True).start()
    return timer


def main() -> int:
    app, window = create_application()
    app._diagnostics = install_diagnostics(app, app_home() / "logs")  # keep the timer alive

    def handle_exception(exc_type, exc_value, exc_traceback) -> None:  # noqa: ANN001
        import traceback

        traceback.print_exception(exc_type, exc_value, exc_traceback)
        QMessageBox.critical(window, "Unexpected error", f"{exc_type.__name__}: {exc_value}")

    sys.excepthook = handle_exception
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
