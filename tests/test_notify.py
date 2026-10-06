"""WhatsApp (CallMeBot) notices: URL building, delivery, filtering, persistence."""

import tempfile
import unittest
import urllib.parse
from pathlib import Path

from asc_oven_control.infrastructure.notify import (
    NotificationSettings,
    Notifier,
    Recipient,
    build_url,
)


class FakeResponse:
    def __init__(self, status=200, body=b"Message queued"):
        self.status = status
        self.body = body

    def read(self, _n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, responses=None):
        self.urls = []
        self.responses = list(responses or [])

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


ALICE = Recipient("Alice", "+16125550123", "123456")
BOB = Recipient("Bob", "+16125550456", "654321")


def settings(**kw):
    values = dict(enabled=True, recipients=[ALICE, BOB])
    values.update(kw)
    return NotificationSettings(**values)


class BuildUrlTest(unittest.TestCase):
    def test_parameters_are_encoded(self):
        url = build_url(ALICE, "Hold complete: 425 °C & fan\nnext")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(url.startswith("https://api.callmebot.com/whatsapp.php?"))
        self.assertEqual(query["phone"], ["+16125550123"])
        self.assertEqual(query["apikey"], ["123456"])
        self.assertEqual(query["text"], ["Hold complete: 425 °C & fan\nnext"])
        self.assertNotIn(" ", url)


class NotifierTest(unittest.TestCase):
    def test_one_message_per_recipient(self):
        opener = FakeOpener()
        notifier = Notifier(settings(), opener=opener)
        notifier.send("done", kind="done")
        notifier.wait(5)
        self.assertEqual(len(opener.urls), 2)

    def test_disabled_or_filtered_sends_nothing(self):
        opener = FakeOpener()
        Notifier(settings(enabled=False), opener=opener).send("x")
        Notifier(settings(notify_faults=False), opener=opener).send("x", kind="fault")
        Notifier(settings(notify_heating_done=False), opener=opener).send("x", kind="done")
        self.assertEqual(opener.urls, [])

    def test_failure_is_retried_logged_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "notifications.log"
            opener = FakeOpener([OSError("no network"), FakeResponse()])
            notifier = Notifier(settings(recipients=[ALICE]), log_path=log, opener=opener, retries=1)
            notifier._deliver.__func__  # exists
            import asc_oven_control.infrastructure.notify as notify_module

            sleep, notify_module.time.sleep = notify_module.time.sleep, lambda _s: None
            try:
                result = notifier.send_blocking("hello")
            finally:
                notify_module.time.sleep = sleep
            self.assertEqual(len(opener.urls), 2)
            self.assertIn("sent to Alice", result[0])
            text = log.read_text(encoding="utf-8")
            self.assertIn("sent to Alice", text)
            self.assertNotIn("123456", text)  # API keys never written to the log

    def test_recipient_without_key_is_kept_but_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            settings(recipients=[Recipient("Pending", "+16125550999", ""), ALICE]).save(path)
            loaded = NotificationSettings.load(path)
        self.assertEqual([r.name for r in loaded.recipients], ["Pending", "Alice"])
        opener = FakeOpener()
        notifier = Notifier(loaded, opener=opener)
        notifier.send("x", kind="done")
        notifier.wait(5)
        self.assertEqual(len(opener.urls), 1)
        self.assertIn("no CallMeBot API key yet", Notifier(settings(recipients=[loaded.recipients[0]]),
                                                         opener=opener).send_blocking("x")[0])

    def test_settings_round_trip_keeps_keys_out_of_repo_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            settings(cooled_below_c=45.0).save(path)
            loaded = NotificationSettings.load(path)
        self.assertTrue(loaded.enabled)
        self.assertEqual([r.name for r in loaded.recipients], ["Alice", "Bob"])
        self.assertEqual(loaded.cooled_below_c, 45.0)
        self.assertEqual(NotificationSettings.from_dict(loaded.to_dict()).recipients, loaded.recipients)


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout, use_ssl):
        self.host, self.port, self.use_ssl = host, port, use_ssl
        self.started_tls = False
        self.login_args = None
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.messages.append(message)


def email_settings(**kw):
    from asc_oven_control.infrastructure.notify import EmailSettings

    email = EmailSettings(recipients=["yiming-z@umn.edu"], username="lab@gmail.com", sender="lab@gmail.com")
    email.set_password("abcd efgh ijkl mnop".replace(" ", ""))
    for key, value in kw.items():
        setattr(email, key, value)
    return NotificationSettings(email_enabled=True, email=email)


class EmailTest(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []

    def test_password_is_stored_encrypted_and_recovered(self):
        settings = email_settings()
        self.assertNotIn("abcdefghijklmnop", settings.email.password_protected)
        self.assertEqual(settings.email.password(), "abcdefghijklmnop")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            settings.save(path)
            self.assertNotIn("abcdefghijklmnop", path.read_text(encoding="utf-8"))
            loaded = NotificationSettings.load(path)
        self.assertEqual(loaded.email.password(), "abcdefghijklmnop")
        self.assertEqual(NotificationSettings.from_dict(loaded.to_dict()).email.recipients, ["yiming-z@umn.edu"])

    def test_smtp_starttls_login_and_message(self):
        notifier = Notifier(email_settings(), smtp_factory=FakeSMTP)
        result = notifier.send_email_blocking("ASC oven: test notice", "hello")
        self.assertIn("email sent", result)
        client = FakeSMTP.instances[0]
        self.assertEqual((client.host, client.port, client.use_ssl), ("smtp.gmail.com", 587, False))
        self.assertTrue(client.started_tls)
        self.assertEqual(client.login_args, ("lab@gmail.com", "abcdefghijklmnop"))
        message = client.messages[0]
        self.assertEqual(message["To"], "yiming-z@umn.edu")
        self.assertEqual(message["Subject"], "ASC oven: test notice")

    def test_run_notices_go_to_email_with_a_subject(self):
        notifier = Notifier(email_settings(), smtp_factory=FakeSMTP)
        notifier.send("ASC oven run 14 (450 °C): hold complete (20 of 20 min hold).", kind="done")
        notifier.wait(5)
        subject = FakeSMTP.instances[0].messages[0]["Subject"]
        self.assertEqual(subject, "ASC oven: Heating done (ASC oven run 14 (450 °C))")

    def test_email_failure_is_reported_not_raised(self):
        class Broken(FakeSMTP):
            def login(self, user, password):
                raise OSError("535 bad credentials")

        result = Notifier(email_settings(), smtp_factory=Broken).send_email_blocking("s", "t")
        self.assertIn("failed: 535 bad credentials", result)

    def test_outlook_method_uses_outlook_sender(self):
        sent = []
        settings = email_settings(method="outlook")
        Notifier(settings, outlook_sender=lambda to, s, t, timeout: sent.append((to, s))).send_email_blocking("s", "t")
        self.assertEqual(sent, [(["yiming-z@umn.edu"], "s")])

    def test_email_and_whatsapp_are_independent(self):
        opener = FakeOpener()
        settings = email_settings()
        settings.enabled = True
        settings.recipients = [ALICE]
        notifier = Notifier(settings, opener=opener, smtp_factory=FakeSMTP)
        notifier.send("x", kind="fault")
        notifier.wait(5)
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual(len(FakeSMTP.instances), 1)


if __name__ == "__main__":
    unittest.main()
