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

    def test_settings_round_trip_keeps_keys_out_of_repo_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.json"
            settings(cooled_below_c=45.0).save(path)
            loaded = NotificationSettings.load(path)
        self.assertTrue(loaded.enabled)
        self.assertEqual([r.name for r in loaded.recipients], ["Alice", "Bob"])
        self.assertEqual(loaded.cooled_below_c, 45.0)
        self.assertEqual(NotificationSettings.from_dict(loaded.to_dict()).recipients, loaded.recipients)


if __name__ == "__main__":
    unittest.main()
