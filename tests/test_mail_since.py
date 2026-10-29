"""Fetch window (build 1, #11): the first Graph delta asks only for mail received at or after MAIL_FETCH_SINCE.
No network: httpx.Client is replaced by a recorder."""
from datetime import datetime, timezone

from app.services import mail

REAL_INBOX_DELTA = mail.inbox_delta  # the integration tests swap mail.inbox_delta for a fake; keep the real one


class Recorder:
    urls: list[str] = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        Recorder.urls.append(url)
        resp = type("R", (), {})()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"value": [], "@odata.deltaLink": "https://graph/delta?token=next"}
        return resp


def test_first_delta_filters_on_received_time(monkeypatch):
    monkeypatch.setattr(mail.httpx, "Client", Recorder)
    monkeypatch.setattr(mail, "_headers", lambda: {})
    Recorder.urls = []
    since = datetime(2026, 10, 27, 5, 30, tzinfo=timezone.utc)
    messages, link = REAL_INBOX_DELTA(None, since=since)
    assert messages == [] and link.endswith("token=next")
    assert Recorder.urls[0].endswith("&$filter=receivedDateTime+ge+2026-10-27T05:30:00Z")


def test_a_stored_delta_link_is_used_as_is(monkeypatch):
    monkeypatch.setattr(mail.httpx, "Client", Recorder)
    monkeypatch.setattr(mail, "_headers", lambda: {})
    Recorder.urls = []
    REAL_INBOX_DELTA("https://graph/delta?token=abc", since=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert Recorder.urls == ["https://graph/delta?token=abc"]
