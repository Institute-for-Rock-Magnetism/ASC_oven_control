"""Notices to the lab: WhatsApp through CallMeBot, and email.

Email goes out either over SMTP (the password is stored encrypted with
Windows DPAPI, readable only by this Windows user on this PC) or through
classic Outlook on this PC (no password in the app; needs an Outlook
profile signed in to an account).

WhatsApp through CallMeBot (free personal-use API):

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
import os
import threading
import time
import urllib.error
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


def protect_secret(secret: str) -> str:
    """Encrypt for this Windows user (DPAPI); base64 text. Plain prefix elsewhere."""
    if not secret:
        return ""
    if os.name != "nt":
        return "plain:" + secret
    import base64

    blob = _dpapi(secret.encode("utf-8"), protect=True)
    return "dpapi:" + base64.b64encode(blob).decode("ascii")


def reveal_secret(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("plain:"):
        return stored[6:]
    if stored.startswith("dpapi:") and os.name == "nt":
        import base64

        try:
            return _dpapi(base64.b64decode(stored[6:]), protect=False).decode("utf-8")
        except OSError:
            return ""
    return ""


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt32 = ctypes.windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = function(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


@dataclass(slots=True)
class EmailSettings:
    """``method``: "smtp" or "outlook" (classic Outlook on this PC)."""

    recipients: list[str] = field(default_factory=list)
    method: str = "smtp"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    security: str = "starttls"  # "starttls", "ssl" or "none"
    username: str = ""
    password_protected: str = ""  # see protect_secret(); never plain text on Windows
    sender: str = ""

    @property
    def configured(self) -> bool:
        if not self.recipients:
            return False
        if self.method == "outlook":
            return True
        return bool(self.smtp_host and (self.sender or self.username))

    def set_password(self, password: str) -> None:
        self.password_protected = protect_secret(password)

    def password(self) -> str:
        return reveal_secret(self.password_protected)


@dataclass(slots=True)
class CloudApiSettings:
    """WhatsApp Cloud API (Meta, official): delivery in seconds.

    Business-initiated messages must use an approved template; the app uses
    a template with one body variable that carries the notice text (e.g.
    name ``asc_oven_notice``, body "ASC oven notice: {{1}}"). Without a
    template name it sends Meta's built-in ``hello_world`` (connection test).
    """

    phone_number_id: str = ""
    token_protected: str = ""  # DPAPI-encrypted access token
    api_version: str = "v23.0"
    template_name: str = "asc_oven_notice"
    template_language: str = "en_US"

    @property
    def configured(self) -> bool:
        return bool(self.phone_number_id and self.token_protected)

    def set_token(self, token: str) -> None:
        self.token_protected = protect_secret(token)

    def token(self) -> str:
        return reveal_secret(self.token_protected)


@dataclass(slots=True)
class NotificationSettings:
    enabled: bool = False
    recipients: list[Recipient] = field(default_factory=list)
    whatsapp_provider: str = "cloud"  # "cloud" (Meta Cloud API) or "callmebot"
    cloud: CloudApiSettings = field(default_factory=CloudApiSettings)
    notify_heating_done: bool = True
    notify_faults: bool = True
    cooled_below_c: float | None = 50.0  # None: no "cooled" notice
    email_enabled: bool = False
    email: EmailSettings = field(default_factory=EmailSettings)

    @classmethod
    def load(cls, path: Path | str) -> "NotificationSettings":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["about"] = (
            "WhatsApp notices via the official WhatsApp Cloud API (Meta; access token stored "
            f"DPAPI-encrypted) or CallMeBot (each person adds {BOT_NUMBER} and sends "
            f"'{ACTIVATION_TEXT}' to get their own API key), and email (SMTP password stored "
            "DPAPI-encrypted for this Windows user, or classic Outlook on this PC)."
        )
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NotificationSettings":
        settings = cls()
        settings.enabled = bool(data.get("enabled", False))
        # A recipient may be saved before their CallMeBot key arrives; such
        # entries are kept but skipped when sending.
        settings.recipients = [
            Recipient(str(r.get("name") or r["phone"]), str(r["phone"]).replace(" ", ""),
                      str(r.get("apikey") or "").strip())
            for r in data.get("recipients", [])
            if r.get("phone")
        ]
        settings.notify_heating_done = bool(data.get("notify_heating_done", True))
        settings.notify_faults = bool(data.get("notify_faults", True))
        settings.cooled_below_c = data.get("cooled_below_c", 50.0)
        settings.whatsapp_provider = str(data.get("whatsapp_provider", "cloud"))
        cloud = data.get("cloud") or {}
        settings.cloud = CloudApiSettings(
            phone_number_id=str(cloud.get("phone_number_id", "")).strip(),
            token_protected=str(cloud.get("token_protected", "")),
            api_version=str(cloud.get("api_version", "v23.0")),
            template_name=str(cloud.get("template_name", "asc_oven_notice")),
            template_language=str(cloud.get("template_language", "en_US")),
        )
        settings.email_enabled = bool(data.get("email_enabled", False))
        email = data.get("email") or {}
        settings.email = EmailSettings(
            recipients=[str(a).strip() for a in email.get("recipients", []) if str(a).strip()],
            method=str(email.get("method", "smtp")),
            smtp_host=str(email.get("smtp_host", "smtp.gmail.com")),
            smtp_port=int(email.get("smtp_port", 587)),
            security=str(email.get("security", "starttls")),
            username=str(email.get("username", "")),
            password_protected=str(email.get("password_protected", "")),
            sender=str(email.get("sender", "")),
        )
        return settings


def subject_for(kind: str, text: str) -> str:
    head = {"done": "Heating done", "cooled": "Oven cooled - fan can go off", "fault": "ALERT"}.get(kind, "Notice")
    run = text.split(":", 1)[0].replace("ALERT ", "")[:80]
    return f"ASC oven: {head} ({run})" if run.startswith("ASC oven run") else f"ASC oven: {head}"


_OUTLOOK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$outlook = New-Object -ComObject Outlook.Application
$mail = $outlook.CreateItem(0)
$mail.To = $env:ASC_MAIL_TO
$mail.Subject = $env:ASC_MAIL_SUBJECT
$mail.Body = $env:ASC_MAIL_BODY
$mail.Send()
"""


def send_with_outlook(recipients: list[str], subject: str, text: str, timeout_s: float = 60.0) -> None:
    """Send through classic Outlook on this PC (its signed-in account).

    The text travels in environment variables, never in the command line,
    so nothing in a message can be interpreted as a command.
    """
    import subprocess

    env = dict(os.environ, ASC_MAIL_TO="; ".join(recipients), ASC_MAIL_SUBJECT=subject,
               ASC_MAIL_BODY=text + "\r\n\r\n-- ASC Oven Control (automatic notice)")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _OUTLOOK_SCRIPT],
        env=env, capture_output=True, text=True, timeout=timeout_s, creationflags=flags,
    )
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout or "Outlook send failed").strip().splitlines()[-1][:200])


def cloud_url(cloud: CloudApiSettings) -> str:
    return f"https://graph.facebook.com/{cloud.api_version}/{cloud.phone_number_id}/messages"


def cloud_payload(cloud: CloudApiSettings, phone: str, text: str, hello_world: bool = False) -> dict:
    """Template message (business-initiated): the notice travels as body variable {{1}}."""
    to = "".join(ch for ch in phone if ch.isdigit())
    if hello_world or not cloud.template_name:
        template = {"name": "hello_world", "language": {"code": "en_US"}}
    else:
        # Template variables may not contain newlines, tabs or >4 spaces in a row.
        flat = " ".join(text.split())[:1000]
        template = {
            "name": cloud.template_name,
            "language": {"code": cloud.template_language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": flat}]}],
        }
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
            "type": "template", "template": template}


def cloud_error(exc) -> str:
    try:
        error = json.loads(exc.read().decode("utf-8", "replace")).get("error", {})
        detail = error.get("error_data", {}).get("details") or error.get("message", "")
        return f"HTTP {exc.code}: {detail} (code {error.get('code')})"[:300]
    except Exception:  # noqa: BLE001
        return f"HTTP {exc.code}"


def build_url(recipient: Recipient, text: str) -> str:
    query = urllib.parse.urlencode(
        {"phone": recipient.phone, "text": text[:MAX_TEXT], "apikey": recipient.apikey},
        quote_via=urllib.parse.quote,
    )
    return f"{API_URL}?{query}"


class Notifier:
    """Fire-and-forget delivery to every recipient; never raises, never blocks."""

    def __init__(self, settings: NotificationSettings, log_path: Path | str | None = None,
                 opener=None, retries: int = 2, timeout_s: float = 20.0, smtp_factory=None,
                 outlook_sender=None) -> None:
        self.settings = settings
        self.log_path = Path(log_path) if log_path else None
        self.opener = opener or urllib.request.urlopen
        self.retries = retries
        self.timeout_s = timeout_s
        self.smtp_factory = smtp_factory  # (host, port, timeout, ssl) -> smtplib-like client
        self.outlook_sender = outlook_sender or send_with_outlook
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def ready_recipients(self) -> list[Recipient]:
        if self.settings.whatsapp_provider == "cloud":
            if not self.settings.cloud.configured:
                return []
            return [r for r in self.settings.recipients if r.phone]
        return [r for r in self.settings.recipients if r.phone and r.apikey]

    @property
    def whatsapp_active(self) -> bool:
        return self.settings.enabled and bool(self.ready_recipients())

    @property
    def email_active(self) -> bool:
        return self.settings.email_enabled and self.settings.email.configured

    @property
    def active(self) -> bool:
        return self.whatsapp_active or self.email_active

    def _wanted(self, kind: str) -> bool:
        if kind == "fault" and not self.settings.notify_faults:
            return False
        if kind in ("done", "cooled") and not self.settings.notify_heating_done:
            return False
        return True

    def send(self, text: str, kind: str = "info") -> None:
        if not self.active or not self._wanted(kind):
            return
        jobs = []
        if self.whatsapp_active:
            jobs += [(self._deliver, (r, text), f"notify-{r.name}") for r in self.ready_recipients()]
        if self.email_active:
            jobs.append((self._deliver_email, (subject_for(kind, text), text), "notify-email"))
        for target, args, name in jobs:
            thread = threading.Thread(target=target, args=args, daemon=True, name=name)
            thread.start()
            self.threads.append(thread)

    def send_blocking(self, text: str, test: bool = False) -> list[str]:
        """Deliver now on WhatsApp (Test button); one result line per recipient.

        ``test`` with the Cloud API sends Meta's built-in hello_world template,
        which works before the app's own template is approved.
        """
        if self.settings.whatsapp_provider == "cloud":
            if not self.settings.cloud.configured:
                return ["WhatsApp Cloud API: enter the phone number ID and access token first"]
            return [self._deliver(r, text, hello_world=test) for r in self.settings.recipients if r.phone]
        return [
            self._deliver(r, text) if r.apikey else f"skipped {r.masked()}: no CallMeBot API key yet"
            for r in self.settings.recipients
        ]

    def send_email_blocking(self, subject: str, text: str) -> str:
        """Deliver now by email (Test button)."""
        if not self.settings.email.recipients:
            return "no email recipients"
        return self._deliver_email(subject, text, retries=0)

    # ------------------------------------------------------- WhatsApp Cloud API

    def _deliver_cloud(self, recipient: Recipient, text: str, hello_world: bool = False) -> str:
        cloud = self.settings.cloud
        request = urllib.request.Request(
            cloud_url(cloud),
            data=json.dumps(cloud_payload(cloud, recipient.phone, text, hello_world)).encode("utf-8"),
            headers={"Authorization": f"Bearer {cloud.token()}", "Content-Type": "application/json"},
            method="POST",
        )
        result = ""
        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=self.timeout_s) as response:
                    body = json.loads(response.read(4000).decode("utf-8", "replace") or "{}")
                if body.get("messages"):
                    result = f"sent to {recipient.masked()} via WhatsApp Cloud API"
                    break
                result = f"failed for {recipient.masked()}: unexpected reply {str(body)[:160]}"
            except urllib.error.HTTPError as exc:
                result = f"failed for {recipient.masked()}: {cloud_error(exc)}"
                if exc.code in (400, 401, 403, 404):
                    break  # configuration problem: retrying will not help
            except Exception as exc:  # noqa: BLE001 - never let a notice break a run
                result = f"failed for {recipient.masked()}: {exc}"
            if attempt < self.retries:
                time.sleep(min(5.0 * (attempt + 1), 15.0))
        self._log(f"{result} | {text[:160]!r}")
        return result

    # ----------------------------------------------------------------- email

    def _deliver_email(self, subject: str, text: str, retries: int | None = None) -> str:
        email = self.settings.email
        targets = ", ".join(email.recipients)
        attempts = (self.retries if retries is None else retries) + 1
        result = ""
        for attempt in range(attempts):
            try:
                if email.method == "outlook":
                    self.outlook_sender(email.recipients, subject, text, self.timeout_s * 3)
                else:
                    self._send_smtp(email, subject, text)
                result = f"email sent to {targets} via {email.method}"
                break
            except Exception as exc:  # noqa: BLE001 - never let a notice break a run
                result = f"email to {targets} via {email.method} failed: {exc}"
                if attempt + 1 < attempts:
                    time.sleep(min(10.0 * (attempt + 1), 30.0))
        self._log(f"{result} | {subject!r}")
        return result

    def _send_smtp(self, email: EmailSettings, subject: str, text: str) -> None:
        import smtplib
        import ssl
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = email.sender or email.username
        message["To"] = ", ".join(email.recipients)
        message["Subject"] = subject
        message.set_content(text + "\n\n-- ASC Oven Control (automatic notice)")
        context = ssl.create_default_context()
        if self.smtp_factory is not None:
            client = self.smtp_factory(email.smtp_host, email.smtp_port, self.timeout_s, email.security == "ssl")
        elif email.security == "ssl":
            client = smtplib.SMTP_SSL(email.smtp_host, email.smtp_port, timeout=self.timeout_s, context=context)
        else:
            client = smtplib.SMTP(email.smtp_host, email.smtp_port, timeout=self.timeout_s)
        with client:
            if email.security == "starttls":
                client.starttls(context=context)
            if email.username:
                client.login(email.username, email.password())
            client.send_message(message)

    def wait(self, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        for thread in self.threads:
            thread.join(max(deadline - time.monotonic(), 0.0))

    def _deliver(self, recipient: Recipient, text: str, hello_world: bool = False) -> str:
        if self.settings.whatsapp_provider == "cloud":
            return self._deliver_cloud(recipient, text, hello_world)
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
