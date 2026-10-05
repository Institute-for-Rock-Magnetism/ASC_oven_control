"""Idle hardware monitor: live thermocouple readings when no run is active.

Owns the serial port while monitoring. Anything else that needs the port
(the run engine, Test connection, the tuning page) calls ``suspend()``,
which blocks until the monitor has closed the port, and ``resume()``
afterwards. Reads only, with one safety exception: on connecting it sets
any zone on PC control still holding a set point (left by a killed run)
back to its lowest value, because idle must mean heaters off.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, QThread, Signal


class _MonitorWorker(QObject):
    reading_ready = Signal(object)
    error = Signal(str)
    notice = Signal(str)

    def __init__(self, backend_factory, poll_seconds: float) -> None:
        super().__init__()
        self.backend_factory = backend_factory
        self.poll_seconds = poll_seconds
        self._active = threading.Event()
        self._released = threading.Event()
        self._released.set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._backend = None

    def run(self) -> None:
        while not self._stop.is_set():
            if not self._active.is_set():
                self._close()
                self._released.set()
                self._active.wait(0.5)
                continue
            self._released.clear()
            if not self._active.is_set():
                continue
            delay = self.poll_seconds
            try:
                if self._backend is None:
                    backend = self.backend_factory()
                    backend.connect()
                    self._backend = backend
                    # Idle means heaters off: clear set points a killed run left behind.
                    clear = getattr(backend, "clear_leftover_setpoints", None)
                    if clear is not None:
                        cleared = clear()
                        if cleared:
                            self.notice.emit("No run active: cleared leftover set points on " + ", ".join(cleared))
                started = time.monotonic()
                reading = self._backend.read()
                self.reading_ready.emit(reading)
                delay = max(self.poll_seconds - (time.monotonic() - started), 0.1)
            except Exception as exc:  # noqa: BLE001 - report and retry
                self.error.emit(str(exc))
                self._close()
                delay = 5.0
            self._wake.wait(delay)
            self._wake.clear()
        self._close()
        self._released.set()

    def _close(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                pass


class HardwareMonitor(QObject):
    """UI-thread facade; starts paused."""

    reading_ready = Signal(object)
    error = Signal(str)
    notice = Signal(str)

    def __init__(self, backend_factory, poll_seconds: float = 2.0) -> None:
        super().__init__()
        self._worker = _MonitorWorker(backend_factory, poll_seconds)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.reading_ready.connect(self.reading_ready)
        self._worker.error.connect(self.error)
        self._worker.notice.connect(self.notice)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._worker._active.is_set()

    def resume(self) -> None:
        self._worker._released.clear()
        self._worker._active.set()

    def suspend(self, timeout: float = 6.0) -> bool:
        """Stop monitoring and wait until the serial port is closed."""
        self._worker._active.clear()
        self._worker._wake.set()
        return self._worker._released.wait(timeout)

    def shutdown(self) -> None:
        self.suspend(3.0)
        self._worker._stop.set()
        self._worker._wake.set()
        self._thread.quit()
        self._thread.wait(3000)
