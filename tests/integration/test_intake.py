"""The three intake channels end to end: portal, WhatsApp, Outlook -> item -> slug -> FTP."""
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import session_scope
from app.models import KV, Item, Media, Message
from app.services.slug import IST


def today() -> str:
    return datetime.now(timezone.utc).astimezone(IST).strftime("%d%m%y")


def file_report(client, stack, files=(("clip.mp4", b"video-bytes"),), **body):
    payload = {"headline": "उदयपुर में पर्यटन सीजन शुरू", "body": "झीलों के शहर में पर्यटकों की भीड़।",
               "subject": "Paryatan season", "story_type": "AV",
               "files": [{"name": n, "size": len(b), "content_type": "video/mp4"} for n, b in files], **body}
    r = client.post("/reporter/items", json=payload)
    assert r.status_code == 200, r.text
    for (_, data), up in zip(files, r.json()["uploads"]):
        key = up["upload_url"].removeprefix("fake://")
        (stack.store / key).parent.mkdir(parents=True, exist_ok=True)
        (stack.store / key).write_bytes(data)
    return r.json()["item"]["id"]


# --- portal ---------------------------------------------------------------------------------------

def test_portal_report_reaches_ftp(reporter_client, stack, drain, item):
    item_id = file_report(reporter_client, stack, files=(("a.jpg", b"one"), ("a.jpg", b"two"), ("नोट.txt", b"3")))
    assert reporter_client.post(f"/reporter/items/{item_id}/submit").status_code == 200
    drain()
    row = item(item_id)
    assert row.slug == f"{today()}_UDR_PARYATAN_SEASON_AV_SUNITA"
    assert row.intake_state == "on_ftp"
    files = sorted(p.name for p in stack.ftp_folder(row.slug).iterdir())
    assert files == ["a.jpg", "a_2.jpg", "नोट.txt"]
    assert (stack.ftp_folder(row.slug) / "a_2.jpg").read_bytes() == b"two"
    assert not any(p.is_file() for p in stack.store.rglob("*")), "Storage copies are deleted after FTP"


def test_portal_submit_waits_for_uploads(reporter_client):
    r = reporter_client.post("/reporter/items", json={"headline": "h", "body": "b", "files": [{"name": "x.jpg", "size": 3}]})
    item_id = r.json()["item"]["id"]
    r = reporter_client.post(f"/reporter/items/{item_id}/submit")
    assert r.status_code == 409 and "x.jpg" in r.json()["detail"]


def test_portal_text_only_report(reporter_client, stack, drain, item):
    item_id = file_report(reporter_client, stack, files=())
    reporter_client.post(f"/reporter/items/{item_id}/submit")
    drain()
    row = item(item_id)
    assert row.intake_state == "on_ftp" and stack.ftp_folder(row.slug).is_dir()


def test_portal_refuses_files_over_the_limit(reporter_client):
    r = reporter_client.post("/reporter/items", json={"headline": "h", "body": "b",
                                                      "files": [{"name": "big.mp4", "size": 2 * 1024 * 1024}]})
    assert r.status_code == 413 and "big.mp4" in r.json()["detail"]


def test_portal_breaking_tag_and_tag_cleanup(reporter_client, stack):
    item_id = file_report(reporter_client, stack, files=(), tags=["Breaking News", "  Politics ", "breaking news"])
    out = reporter_client.post(f"/reporter/items/{item_id}/submit").json()["item"]
    assert out["tags"] == ["Breaking News", "Politics"]
    assert out["priority"] == "breaking"


def test_portal_bad_story_type_is_422(reporter_client):
    r = reporter_client.post("/reporter/items", json={"headline": "h", "body": "b", "story_type": "XX"})
    assert r.status_code == 422


def test_reporter_sees_only_own_reports(reporter_client, stack, make_user, make_reporter, login):
    item_id = file_report(reporter_client, stack, files=())
    other = make_user("mahendra", "reporter")
    make_reporter(slug_code="MAHENDRA", city_code="JDP", phone="919800000001", user_id=other, name="Mahendra")
    them = login("mahendra")
    assert them.get(f"/reporter/items/{item_id}").status_code == 404
    assert them.post(f"/reporter/items/{item_id}/submit").status_code == 404
    assert them.get("/reporter/items").json()["items"] == []


def test_reporter_status_moves_with_the_desk(reporter_client, stack, drain, desk):
    item_id = file_report(reporter_client, stack, files=())
    reporter_client.post(f"/reporter/items/{item_id}/submit")
    assert reporter_client.get(f"/reporter/items/{item_id}").json()["item"]["status"] == "submitted"
    drain()
    assert reporter_client.get(f"/reporter/items/{item_id}").json()["item"]["status"] == "reviewing"
    desk.post(f"/desk/items/{item_id}/reject", json={"reason": "duplicate"})
    assert reporter_client.get(f"/reporter/items/{item_id}").json()["item"]["status"] == "rejected"


def test_two_reports_same_subject_same_day_get_suffix(reporter_client, stack, drain, item):
    first = file_report(reporter_client, stack, files=())
    second = file_report(reporter_client, stack, files=())
    for i in (first, second):
        reporter_client.post(f"/reporter/items/{i}/submit")
    drain()
    slugs = {item(first).slug, item(second).slug}
    base = f"{today()}_UDR_PARYATAN_SEASON_AV_SUNITA"
    assert slugs == {base, base + "_2"}


# --- WhatsApp -------------------------------------------------------------------------------------

def test_whatsapp_verify_handshake(stack):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    ok = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.text == "42"
    bad = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_whatsapp_bad_signature_is_401(whatsapp_post):
    r = whatsapp_post([{"id": "wamid.1", "type": "text", "text": {"body": "x"}}], secret=b"wrong")
    assert r.status_code == 401


def test_whatsapp_known_reporter_text_and_photo(make_reporter, whatsapp_post, drain, stack):
    make_reporter()
    stack.wa_media["MEDIA-1"] = b"photo-bytes"
    r = whatsapp_post([
        {"id": "wamid.1", "type": "text", "text": {"body": "जयपुर में भारी बारिश\nकई इलाकों में जलभराव"}},
        {"id": "wamid.2", "type": "image", "image": {"id": "MEDIA-1", "mime_type": "image/jpeg"}},
    ])
    assert r.status_code == 200
    drain()
    with session_scope() as db:
        items = db.scalars(select(Item)).all()
        assert len(items) == 1
        row = items[0]
        assert row.intake_state == "on_ftp", row.intake_note
        assert row.headline == "जयपुर में भारी बारिश"
        assert row.slug.startswith(f"{today()}_UDR_") and row.slug.endswith("_AV_SUNITA")
        media = db.scalars(select(Media)).one()
        assert media.state == "on_ftp"
        # WhatsApp photos have no name: named by time
        assert media.file_name.endswith("_01.jpg")
        assert (stack.ftp_root / media.ftp_path.lstrip("/")).read_bytes() == b"photo-bytes"


def test_whatsapp_duplicate_delivery_is_ignored(make_reporter, whatsapp_post, drain):
    make_reporter()
    msg = [{"id": "wamid.dup", "type": "text", "text": {"body": "एक खबर"}}]
    whatsapp_post(msg)
    whatsapp_post(msg)
    drain()
    with session_scope() as db:
        assert len(db.scalars(select(Message)).all()) == 1
        assert len(db.scalars(select(Item)).all()) == 1


def test_whatsapp_stickers_and_reactions_are_ignored(make_reporter, whatsapp_post):
    make_reporter()
    r = whatsapp_post([{"id": "wamid.s", "type": "sticker", "sticker": {"id": "S"}},
                       {"id": "wamid.r", "type": "reaction", "reaction": {"emoji": "👍"}}])
    assert r.status_code == 200
    with session_scope() as db:
        assert db.scalars(select(Item)).all() == []


def test_whatsapp_unknown_sender_waits_for_desk(whatsapp_post, drain, desk, make_reporter, item, stack):
    reporter_id = make_reporter(phone="919000000000")
    whatsapp_post([{"id": "wamid.u", "type": "text", "text": {"body": "अजमेर में मेला"}},
                   {"id": "wamid.u2", "type": "image", "image": {"id": "M2", "mime_type": "image/jpeg"}}],
                  wa_id="919999000111", name="New Stringer")
    drain()
    inbox = desk.get("/desk/items").json()["items"]
    assert len(inbox) == 1 and inbox[0]["intake_state"] == "needs_desk"
    assert "reporter" in inbox[0]["intake_note"].lower()
    r = desk.patch(f"/desk/items/{inbox[0]['id']}", json={"version": inbox[0]["version"], "reporter_id": str(reporter_id)})
    assert r.status_code == 200, r.text
    drain()
    row = item(inbox[0]["id"])
    assert row.intake_state == "on_ftp" and row.slug.endswith("_SUNITA")
    assert len(list(stack.ftp_folder(row.slug).iterdir())) == 1


def test_whatsapp_media_over_limit_fails_without_retries(make_reporter, whatsapp_post, drain, stack):
    make_reporter()
    stack.wa_media["BIG"] = b"x" * (1024 * 1024 + 1)
    whatsapp_post([{"id": "wamid.big", "type": "video", "video": {"id": "BIG", "mime_type": "video/mp4",
                                                                       "caption": "कोटा में हादसा"}}])
    drain()
    with session_scope() as db:
        media = db.scalars(select(Media)).one()
        assert media.state == "failed" and "FTP" in media.error
        item = db.scalars(select(Item)).one()
        assert item.intake_state == "on_ftp", "the rest of the item still goes to FTP"


# --- Outlook --------------------------------------------------------------------------------------

def mail_message(mid="MAIL1", sender="pro.ajmer@example.com", subject="प्रेस नोट: अजमेर में रोजगार मेला",
                 body="अजमेर में 20 अक्टूबर को रोजगार मेला लगेगा।\n\nSent from my iPhone", attachments=False,
                 conversation="CONV1", received="2026-10-17T06:00:00Z"):
    return {"id": mid, "subject": subject, "conversationId": conversation, "hasAttachments": attachments,
            "receivedDateTime": received, "from": {"emailAddress": {"name": "PRO", "address": sender}},
            "body": {"content": body}}


def run_mail_poll():
    from app.jobs import enqueue
    with session_scope() as db:
        enqueue(db, "fetch_mail", {"manual": True}, dedupe_key="fetch_mail")


def test_outlook_known_reporter_email_with_attachment(make_reporter, stack, drain, item):
    make_reporter(email="pro.ajmer@example.com", phone=None)
    stack.mail_batch.append(mail_message(attachments=True))
    stack.mail_attachments["MAIL1"] = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "press note.pdf",
         "contentType": "application/pdf", "size": 10, "isInline": False},
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A2", "name": "logo.png",
         "contentType": "image/png", "size": 3, "isInline": True},
        {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "A3", "name": "OneDrive video",
         "size": 0, "isInline": False},
    ]
    run_mail_poll()
    drain()
    with session_scope() as db:
        row = db.scalars(select(Item)).one()
        assert row.headline.startswith("प्रेस नोट")
        assert "Sent from my iPhone" not in row.body
        assert row.intake_state == "on_ftp"
        states = sorted((m.original_name, m.state) for m in db.scalars(select(Media)))
        assert states == [("OneDrive video", "link_only"), ("press note.pdf", "on_ftp")]
        assert db.get(KV, "mail_delta").value == {"link": "DELTA-NEXT"}


def test_outlook_reply_in_same_thread_joins_the_item(make_reporter, stack, drain):
    make_reporter(email="pro.ajmer@example.com", phone=None)
    stack.mail_batch.append(mail_message())
    run_mail_poll()
    drain()
    stack.mail_batch[:] = [mail_message(mid="MAIL2", subject="RE: प्रेस नोट", body="एक और जानकारी।",
                                        received="2026-10-17T07:00:00Z")]
    run_mail_poll()
    drain()
    with session_scope() as db:
        items = db.scalars(select(Item)).all()
        assert len(items) == 1
        assert "एक और जानकारी" in items[0].body
        assert len(db.scalars(select(Message)).all()) == 2


def test_outlook_same_email_twice_is_one_message(make_reporter, stack, drain):
    make_reporter(email="pro.ajmer@example.com", phone=None)
    stack.mail_batch.append(mail_message())
    run_mail_poll()
    drain()
    run_mail_poll()
    drain()
    with session_scope() as db:
        assert len(db.scalars(select(Message)).all()) == 1


def test_manual_refresh_records_a_fetch_run(desk, stack, drain):
    assert desk.post("/desk/refresh").json() == {"ok": True, "mail": True}
    drain()
    runs = desk.get("/desk/fetch-runs").json()["runs"]
    assert runs and runs[0]["source"] == "outlook" and runs[0]["added"] == 0
