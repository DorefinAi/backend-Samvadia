import httpx
import pytest

from app.jobs import GiveUp
from app.services.mail import clean_body, is_transient
from app.tasks import _capped, _too_big


def test_clean_body_cuts_wrapped_wrote_line():
    body = ("अजमेर में रोजगार मेला।\n\nOn Mon, 19 Oct 2026 at 10:02, Ram Kumar <ram.kumar@example.com>\n"
            "wrote:\nपिछला मेल")
    assert clean_body(body) == "अजमेर में रोजगार मेला।"


def test_clean_body_cuts_header_wrapped_over_three_lines():
    body = "Photos attached.\nOn Monday, 19 October 2026,\nPRO Ajmer <pro@example.com>\nwrote:\nold"
    assert clean_body(body) == "Photos attached."


def test_clean_body_keeps_sentence_starting_with_on():
    body = "Update.\nOn Monday the CM will visit Ajmer.\nShe wrote the speech herself."
    assert clean_body(body) == body


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://graph.microsoft.com/x")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize("code,transient", [(429, True), (503, True), (400, False), (404, False), (405, False)])
def test_transient_errors(code, transient):
    assert is_transient(_status_error(code)) is transient


def test_connection_drop_is_transient():
    assert is_transient(httpx.ConnectError("reset"))
    assert not is_transient(ValueError("bad attachment"))


def test_size_limit(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    assert _too_big(2 * 1024 * 1024)
    assert not _too_big(1024)
    assert not _too_big(None) and not _too_big("not a number")
    assert list(_capped(iter([b"x" * 1000]))) == [b"x" * 1000]
    with pytest.raises(GiveUp, match="Over 1 MB"):
        list(_capped(iter([b"x" * 700_000, b"x" * 700_000])))
