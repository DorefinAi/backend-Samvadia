"""What happens when FTP, Meta or a worker fails."""
from datetime import timedelta

from sqlalchemy import select, update

from app import jobs
from app.db import session_scope
from app.models import Event, Item, Job, Media


def test_ftp_outage_ends_in_ftp_failed_then_admin_retry_works(stack, filed_item, drain, item, desk, admin):
    stack.ftp_up = False
    item_id = filed_item()  # drain runs every retry
    row = item(item_id)
    assert row.intake_state == "ftp_failed"
    assert "FTP is down" in row.intake_note
    with session_scope() as db:
        job = db.scalars(select(Job).where(Job.type == "ftp_upload")).one()
        assert job.status == "failed" and job.attempts == job.max_attempts

    assert desk.post(f"/desk/items/{item_id}/retry-ftp").status_code == 403, "FTP retries are admin-only"
    stack.ftp_up = True
    assert admin.post(f"/desk/items/{item_id}/retry-ftp").status_code == 200
    drain()
    row = item(item_id)
    assert row.intake_state == "on_ftp"
    assert (stack.ftp_folder(row.slug) / "clip.mp4").read_bytes() == b"video-bytes"


def test_approved_during_ftp_outage_waits_then_publishes(stack, filed_item, drain, desk, admin, octopus):
    stack.ftp_up = False
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/approve").json()["story"]["state"] == "waiting"
    assert b"<item>" not in octopus().content
    stack.ftp_up = True
    admin.post(f"/desk/items/{item_id}/retry-ftp")
    drain()
    assert b"<item>" in octopus().content


def test_whatsapp_media_failure_then_desk_retry(stack, make_reporter, whatsapp_post, drain, desk, item):
    make_reporter()
    stack.wa_fail.add("M1")
    whatsapp_post([{"id": "wamid.1", "type": "image", "image": {"id": "M1", "mime_type": "image/jpeg",
                                                                "caption": "बीकानेर में ऊंट उत्सव"}}])
    drain()
    with session_scope() as db:
        media = db.scalars(select(Media)).one()
        assert media.state == "failed" and "Meta is down" in media.error
        item_id = media.item_id
    assert item(item_id).intake_state == "on_ftp", "the text still went to FTP without the photo"

    stack.wa_fail.clear()
    assert desk.post(f"/desk/media/{media.id}/retry").status_code == 200
    drain()
    row = item(item_id)
    assert row.intake_state == "on_ftp"
    assert [m.state for m in row.media] == ["on_ftp"]
    assert len(list(stack.ftp_folder(row.slug).iterdir())) == 1


def test_portal_media_cannot_be_retried(desk, filed_item, item):
    item_id = filed_item()
    media_id = item(item_id).media[0].id
    assert desk.post(f"/desk/media/{media_id}/retry").status_code == 409


def test_retry_later_does_not_use_up_attempts(make_reporter, whatsapp_post, stack):
    """finalize waits while a file is still downloading, without burning its attempts."""
    make_reporter()
    whatsapp_post([{"id": "wamid.1", "type": "image", "image": {"id": "M1", "mime_type": "image/jpeg",
                                                                "caption": "खबर"}}])
    # run only the default queue: the media download (media queue) never happens
    for _ in range(10):
        while jobs.run_one(["default"]):
            pass
        with session_scope() as db:
            db.execute(update(Job).where(Job.status == "pending").values(run_after=jobs.now()))
    with session_scope() as db:
        fin = db.scalars(select(Job).where(Job.type == "finalize_item")).one()
        assert fin.status == "pending" and fin.attempts == 0
        assert "still arriving" in fin.last_error


def test_worker_crash_job_is_reset_after_30_minutes(stack):
    with session_scope() as db:
        jobs.enqueue(db, "cleanup", {})
    with session_scope() as db:
        db.execute(update(Job).values(status="running", locked_at=jobs.now() - timedelta(minutes=31)))
    jobs.reset_stale()
    with session_scope() as db:
        assert db.scalars(select(Job)).one().status == "pending"


def test_cleanup_drops_events_older_than_a_day(stack):
    with session_scope() as db:
        db.add_all([Event(app="desk", type="old", payload={}), Event(app="desk", type="new", payload={})])
    with session_scope() as db:
        db.execute(update(Event).where(Event.type == "old").values(created_at=jobs.now() - timedelta(hours=25)))
        jobs.enqueue(db, "cleanup", {})
    assert jobs.run_one()
    with session_scope() as db:
        assert [e.type for e in db.scalars(select(Event))] == ["new"]


def test_dedupe_key_keeps_one_pending_job(stack):
    with session_scope() as db:
        for _ in range(3):
            jobs.enqueue(db, "fetch_mail", {}, dedupe_key="fetch_mail")
    with session_scope() as db:
        assert len(db.scalars(select(Job)).all()) == 1


def test_mail_poison_message_does_not_block_others(make_reporter, stack, drain):
    make_reporter(email="pro@example.com", phone=None)
    good = {"id": "GOOD", "subject": "अच्छी खबर", "conversationId": "C1", "hasAttachments": False,
            "receivedDateTime": "2026-10-17T06:00:00Z", "from": {"emailAddress": {"address": "pro@example.com"}},
            "body": {"content": "पाठ"}}
    bad = {**good, "id": "BAD", "conversationId": "C2", "receivedDateTime": "not a date"}
    stack.mail_batch[:] = [bad, good]
    with session_scope() as db:
        jobs.enqueue(db, "fetch_mail", {})
    drain()
    with session_scope() as db:
        assert [i.headline for i in db.scalars(select(Item))] == ["अच्छी खबर"]
        from app.models import KV
        assert "BAD" in db.get(KV, "mail_retry").value
