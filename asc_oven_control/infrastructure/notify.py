"""WhatsApp notices through CallMeBot (free personal-use API).

Each recipient registers their own phone once: add +34 623 75 84 18 in
WhatsApp and send "I allow callmebot to send me messages"; the bot replies
with a personal API key. A key only delivers to its owner (no groups), so
the app sends one message per recipient.

Sending never blocks the caller: each notice is delivered on a daemon
thread with a timeout and two retries, and every attempt is appended to a
log file. Phone numbers and keys live in the app's config folder
(``config/notifications.json``), never in the repository.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

API_URL = "https://api.callmebot.com/whatsapp.php"
BOT_NUMBER = "+34 623 75 84 18"
ACTIVATION_TEXT = "I allow callmebot to send me messages"
MAX_TEXT = 900


@dataclass(frozen=True, slots=True)
class Recipient:
    name: str
    phone: str  # international format, e.g. +16125550123
    apikey: str

    def masked(self) -> str:
        return f"{self.name} ({self.phone[:4]}…{self.phone[-2:]})"


@dataclass(slots=True)
class NotificationSettings:
    enabled: bool = False
    recipients: list[Recipient] = field(default_factory=list)
    notify_heating_done: bool = True
    notify_faults: bool = True
    cooled_below_c: float | None = 50.0  # None: no "cooled" notice

    @classmethod
    def load(cls, path: Path | str) -> "NotificationSettings":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            enabled=bool(data.get("enabled", False)),
            recipients=[
                Recipient(str(r["name"]), str(r["phone"]).replace(" ", ""), str(r["apikey"]).strip())
                for r in data.get("recipients", [])
                if r.get("phone") and r.get("apikey")
            ],
            notify_heating_done=bool(data.get("notify_heating_done", True)),
            notify_faults=bool(data.get("notify_faults", True)),
            cooled_below_c=data.get("cooled_below_c", 50.0),
        )

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["about"] = (
            f"WhatsApp notices via CallMeBot. Each person adds {BOT_NUMBER} and sends "
            f"'{ACTIVATION_TEXT}' to get their own API key."
        )
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NotificationSettings":
        settings = cls()
        settings.enabled = bool(data.get("enabled", False))
        settings.recipients = [Recipient(**r) for r in data.get("recipients", [])]
        settings.notify_heating_done = bool(data.get("notify_heating_done", True))
        settings.notify_faults = bool(data.get("notify_faults", True))
        settings.cooled_below_c = data.get("cooled_below_c", 50.0)
        return settings


def build_url(recipient: Recipient, text: str) -> str:
    query = urllib.parse.urlencode(
        {"phone": recipient.phone, "text": text[:MAX_TEXT], "apikey": recipient.apikey},
        quote_via=urllib.parse.quote,
    )
    return f"{API_URL}?{query}"


class Notifier:
    """Fire-and-forget delivery to every recipient; never raises, never blocks."""

    def __init__(self, settings: NotificationSettings, log_path: Path | str | None = None,
                 opener=None, retries: int = 2, timeout_s: float = 20.0) -> None:
        self.settings = settings
        self.log_path = Path(log_path) if log_path else None
        self.opener = opener or urllib.request.urlopen
        self.retries = retries
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    @property
    def active(self) -> bool:
        return self.settings.enabled and bool(self.settings.recipients)

    def send(self, text: str, kind: str = "info") -> None:
        if not self.active:
            return
        if kind == "fault" and not self.settings.notify_faults:
            return
        if kind in ("done", "cooled") and not self.settings.notify_heating_done:
            return
        for recipient in self.settings.recipients:
            thread = threading.Thread(target=self._deliver, args=(recipient, text), daemon=True,
                                      name=f"notify-{recipient.name}")
            thread.start()
            self.threads.append(thread)

    def send_blocking(self, text: str) -> list[str]:
        """Deliver now (for the Test button); returns one result line per recipient."""
        return [self._deliver(r, text) for r in self.settings.recipients]

    def wait(self, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        for thread in self.threads:
            thread.join(max(deadline - time.monotonic(), 0.0))

    def _deliver(self, recipient: Recipient, text: str) -> str:
        url = build_url(recipient, text)
        result = ""
        for attempt in range(self.retries + 1):
            try:
                with self.opener(url, timeout=self.timeout_s) as response:
                    status = getattr(response, "status", 200)
                    body = response.read(2000).decode("utf-8", "replace")
                if status == 200 and "error" not in body.lower():
                    result = f"sent to {recipient.masked()}"
                    break
                result = f"failed for {recipient.masked()}: HTTP {status} {body[:120]!r}"
            except Exception as exc:  # noqa: BLE001 - never let a notice break a run
                result = f"failed for {recipient.masked()}: {exc}"
            time.sleep(min(5.0 * (attempt + 1), 15.0))
        self._log(f"{result} | {text[:160]!r}")
        return result

    def _log(self, line: str) -> None:
        if self.log_path is None:
            return
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
            except OSError:
                pass
