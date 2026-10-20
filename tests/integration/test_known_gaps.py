"""Regression tests for the gaps found in the 20 Oct 2026 review, all fixed since. Each note says what
used to go wrong; a failure here is a bug in app/, not in the test.
"""
import warnings

import pytest


def version(client, item_id) -> int:
    return client.get(f"/desk/items/{item_id}").json()["item"]["version"]


# Was (gap 1): GET /desk/items?limit=0 crashes (items[-1] on an empty list); a negative limit reaches Postgres.
@pytest.mark.parametrize("limit", [0, -1])
def test_inbox_rejects_a_zero_or_negative_limit(desk, limit):
    r = desk.get("/desk/items", params={"limit": limit})
    assert r.status_code == 422, f"got {r.status_code}"


# Was (gap 2): retrying a failed WhatsApp/email file on an item in ftp_failed: the file reaches Storage, but
# finalize_item ignores ftp_failed items, so it is never uploaded and the item stays ftp_failed.
def test_media_retry_on_ftp_failed_item_uploads_the_file(stack, make_reporter, whatsapp_post, drain, desk, item):
    make_reporter()
    stack.wa_fail.add("M1")
    stack.ftp_up = False
    whatsapp_post([{"id": "wamid.1", "type": "image", "image": {"id": "M1", "mime_type": "image/jpeg",
                                                                "caption": "बाड़मेर में बारिश"}}])
    drain()
    item_id = desk.get("/desk/items").json()["items"][0]["id"]
    row = item(item_id)
    assert row.intake_state == "ftp_failed" and row.media[0].state == "failed"

    stack.wa_fail.clear()
    stack.ftp_up = True
    assert desk.post(f"/desk/media/{row.media[0].id}/retry").status_code == 200
    drain()
    row = item(item_id)
    assert row.media[0].state == "on_ftp", f"file is {row.media[0].state}, item is {row.intake_state}"
    assert row.intake_state == "on_ftp"


# Was (gap 3): an unknown sender's item gets a hand-typed slug but no reporter. finalize is only scheduled
# when a reporter is set, so the item stays needs_desk, its files never reach FTP, and an approved
# story waits for FTP forever.
def test_hand_typed_slug_without_reporter_still_uploads(desk, whatsapp_post, drain, item, octopus, admin):
    whatsapp_post([{"id": "wamid.u", "type": "text", "text": {"body": "अजमेर में मेला"}},
                   {"id": "wamid.u2", "type": "image", "image": {"id": "M2", "mime_type": "image/jpeg"}}],
                  wa_id="919999000111", name="New Stringer")
    drain()
    item_id = desk.get("/desk/items").json()["items"][0]["id"]
    r = desk.patch(f"/desk/items/{item_id}", json={"version": version(desk, item_id),
                                                   "slug": "201026_AJM_MELA_AV_STRINGER"})
    assert r.status_code == 200, r.text
    desk.post(f"/desk/items/{item_id}/approve")
    drain()
    row = item(item_id)
    assert row.intake_state == "on_ftp", f"stuck in {row.intake_state}"
    assert b"<item>" in octopus().content, "the approved story never entered the feed"


# Was (gap 4): the slug can be changed after the story is in the Octopus feed. The FTP folder is renamed,
# but Octopus already holds the old slug as the title, so the newsroom looks for a folder that is gone.
def test_slug_cannot_change_once_published(desk, filed_item, drain, item, stack):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/approve")
    published_slug = item(item_id).slug
    r = desk.patch(f"/desk/items/{item_id}", json={"version": version(desk, item_id), "slug_subject": "Renamed"})
    drain()
    assert r.status_code == 409 or item(item_id).slug == published_slug, "slug changed after publish"
    assert stack.ftp_folder(published_slug).is_dir(), "the folder Octopus points to was renamed away"


# Was (gap 5): app/intake.py's docstring has "\-", a SyntaxWarning today and a SyntaxError in a future Python.
def test_sources_compile_without_warnings():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "app"
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        for path in root.rglob("*.py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

