"""Review findings on commit bbc09cb (M1-M4, L1-L6), end to end on the local Postgres and FTP.
L1 and the Remote-level part of L2 are in tests/test_ftp_write_root.py; the matcher part of M3 in
tests/test_footage_matcher.py. Senders and addresses are made up."""
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text, update

from app import tasks
from app.db import SessionLocal, session_scope
from app.intake import add_message
from app.jobs import enqueue
from app.models import KV, Audit, Item
from app.services import footage, mail as mail_service

HADSA = "161026_AJM_HADSA_AV_SHUBHAM"
HADSA_PATH = "/Ajmer/2026/OCT 2026/151026/161026_AJM_HADSA_AV_SHUBHAM"  # in the newsroom_ftp fixture tree


def mail(mid, subject, body="खबर का पाठ।", sender="stringer@example.com", name="A Stringer",
         received="2026-10-16T06:00:00Z", conversation=None):
    return {"id": mid, "subject": subject, "conversationId": conversation or f"CONV-{mid}",
            "hasAttachments": False, "receivedDateTime": received,
            "from": {"emailAddress": {"name": name, "address": sender}}, "body": {"content": body}}


def poll(drain):
    with session_scope() as db:
        enqueue(db, "fetch_mail", {"manual": True}, dedupe_key="fetch_mail")
    drain()


def all_kinds(desk):
    return desk.get("/desk/items", params={"kind": "all"}).json()


def version(client, item_id) -> int:
    return client.get(f"/desk/items/{item_id}").json()["item"]["version"]


def _only(desk) -> dict:
    items = all_kinds(desk)["items"]
    assert len(items) == 1
    return items[0]


# --- M1 slug with a trailing newline ----------------------------------------------------------------------

def test_m1_hand_typed_slug_with_a_trailing_newline_is_422(desk, filed_item, item):
    item_id = filed_item()
    before = item(item_id).slug
    for bad in ("171026_UDR_HAND_TYPED_AV_SUNITA\n", "171026_UDR_HAND_TYPED_AV_SUNITA\r\n"):
        r = desk.patch(f"/desk/items/{item_id}", json={"version": version(desk, item_id), "slug": bad})
        assert r.status_code == 422, r.text
    assert item(item_id).slug == before


# --- M2 explicit null for NOT NULL columns --------------------------------------------------------------

def test_m2_null_for_a_not_null_field_is_422_not_500(desk, filed_item, item):
    item_id = filed_item()
    before = item(item_id)
    for field in ("headline", "body", "priority", "tags"):
        r = desk.patch(f"/desk/items/{item_id}", json={"version": before.version, field: None})
        assert r.status_code == 422, (field, r.status_code, r.text)
        assert field in r.json()["detail"]
    after = item(item_id)
    assert (after.headline, after.body, after.version) == (before.headline, before.body, before.version)
    # nullable fields may still be cleared, and an empty string is a valid headline
    r = desk.patch(f"/desk/items/{item_id}", json={"version": before.version, "category": None, "headline": ""})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["headline"] == ""


# --- M3 recheck racing a manual pick -------------------------------------------------------------------------

GARBA_PATH = "/Ajmer/2026/OCT 2026/16 Oct/161026_AJM03_GARBA_RAS_AV_MUKESH_SHARMA_BEAWAR"


def test_m3_recheck_does_not_overwrite_a_manual_pick_made_meanwhile(stack, drain, desk, newsroom_ftp, monkeypatch):
    stack.mail_batch.append(mail("R1", HADSA))
    poll(drain)
    item_id = _only(desk)["id"]
    picked = [{"name": "garba.mp4", "size": 1, "modified": None}]
    real_compute = footage.compute

    def compute_then_desk_picks(lister, row):
        match = real_compute(lister, row)  # the automatic match: HADSA_PATH and its files
        assert match is not None and match.path == HADSA_PATH
        with session_scope() as db:  # the desk picks another folder while the FTP is being read
            db.execute(update(Item).where(Item.id == uuid.UUID(item_id)).values(
                footage_status="manual", footage_path=GARBA_PATH, footage_files=picked, footage_bytes=1))
        return match

    monkeypatch.setattr(footage, "compute", compute_then_desk_picks)
    r = desk.post(f"/desk/items/{item_id}/footage/recheck")
    assert r.status_code == 200, r.text
    f = r.json()["item"]["footage"]
    assert f["status"] == "manual" and f["path"] == GARBA_PATH
    assert f["files"] == picked and f["bytes"] == 1, "files from the automatic match must not land under the pick"


# --- M4 duplicate chains -----------------------------------------------------------------------------------

def test_m4_an_original_cannot_become_a_duplicate(desk, filed_item):
    a, b, c = filed_item(), filed_item(subject="Copy of A"), filed_item(subject="Another event")
    assert desk.post(f"/desk/items/{b}/duplicate", json={"duplicate_of": a}).status_code == 200
    r = desk.post(f"/desk/items/{a}/duplicate", json={"duplicate_of": c})
    assert r.status_code == 409, r.text
    assert "duplicates of this one" in r.json()["detail"] and b in r.json()["detail"]
    assert desk.get(f"/desk/items/{a}").json()["item"]["review_state"] == "pending"
    # once B is reopened (its duplicate_of cleared), A is free to be marked
    assert desk.post(f"/desk/items/{b}/reopen").status_code == 200
    assert desk.post(f"/desk/items/{a}/duplicate", json={"duplicate_of": c}).status_code == 200


# --- L2 the folder rename never lands on an existing folder ----------------------------------------------------

def test_l2_ftp_rename_refuses_an_existing_target(desk, filed_item, drain, item, stack):
    item_id = filed_item()
    old = item(item_id)
    target = "171026_UDR_HAND_TYPED_AV_SUNITA"
    stack.ftp_folder(target).mkdir(parents=True)
    (stack.ftp_folder(target) / "someone-elses.mp4").write_bytes(b"theirs")
    r = desk.patch(f"/desk/items/{item_id}", json={"version": old.version, "slug": target})
    assert r.status_code == 200, r.text
    drain()  # the rename job gives up at once (no retries): the target exists
    after = item(item_id)
    assert after.ftp_folder == old.ftp_folder and stack.ftp_folder(old.slug).is_dir()
    assert sorted(p.name for p in stack.ftp_folder(target).iterdir()) == ["someone-elses.mp4"]
    assert "already exists" in (after.intake_note or "")
    assert all(m.ftp_path.startswith(old.ftp_folder + "/") for m in after.media)


# --- L3 control characters in a footage path --------------------------------------------------------------------

def test_l3_control_characters_in_a_path_are_422(stack, drain, desk, newsroom_ftp):
    stack.mail_batch.append(mail("R1", HADSA))
    poll(drain)
    item = _only(desk)
    for bad in (HADSA_PATH + "\r\nDELE x", HADSA_PATH + "\n", "/Ajmer/2026\x00/x", "/Ajmer/\x1b2026"):
        r = desk.put(f"/desk/items/{item['id']}/footage", json={"path": bad, "version": item["version"]})
        assert r.status_code == 422, (repr(bad), r.status_code, r.text)
        assert desk.get("/desk/ftp/browse", params={"path": bad}).status_code == 422, repr(bad)


# --- L4 footage from another district's folder ---------------------------------------------------------------------

BARAN_PATH = "/BARAN/oct2026/161026/161026_BRN 4_SUCIDE_AV_RAJKUMAR"


def test_l4_other_districts_footage_needs_an_admin(stack, drain, desk, admin, newsroom_ftp):
    stack.mail_batch.append(mail("R1", HADSA))
    poll(drain)
    item = _only(desk)
    assert item["district"]["code"] == "AJM"
    url = f"/desk/items/{item['id']}/footage"
    r = desk.put(url, json={"path": BARAN_PATH, "version": item["version"]})
    assert r.status_code == 409, r.text
    assert "AJM" in r.json()["detail"]
    # the same district is fine for the desk
    r = desk.put(url, json={"path": HADSA_PATH, "version": item["version"]})
    assert r.status_code == 200, r.text
    # an admin may pick another district's folder
    r = admin.put(url, json={"path": BARAN_PATH, "version": r.json()["item"]["version"]})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["footage"]["status"] == "manual" and r.json()["item"]["footage"]["path"] == BARAN_PATH


# --- L5 a reply racing a desk decision ----------------------------------------------------------------------------

def _wait_for_lock_waiter(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session_scope() as db:
            waiting = db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                     "AND wait_event_type = 'Lock'"))
        if waiting:
            return True
        time.sleep(0.05)
    return False


REPLY_AT = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)  # inside the thread's 3-day window


def test_l5_reply_joins_the_thread_when_nothing_races(stack, drain, desk):
    """Control for the race below: without a concurrent decision the reply does reopen the sent-back story."""
    stack.mail_batch.append(mail("R1", HADSA, conversation="THREAD-1"))
    poll(drain)
    item_id = uuid.UUID(_only(desk)["id"])
    with session_scope() as db:
        db.execute(update(Item).where(Item.id == item_id).values(review_state="sent_back", reject_note="Need a byte"))
    with session_scope() as db:
        result = add_message(db, source="outlook", external_id="R2", sender_contact="stringer@example.com",
                             sender_name="A Stringer", text="बाइट", received_at=REPLY_AT, raw={},
                             conversation_key="THREAD-1", reporter=None, subject="Re: " + HADSA)
        assert result is not None and result[0].id == item_id
    with session_scope() as db:
        assert db.get(Item, item_id).review_state == "pending"


def test_l5_reply_does_not_undo_a_desk_decision_made_meanwhile(stack, drain, desk):
    stack.mail_batch.append(mail("R1", HADSA, conversation="THREAD-1"))
    poll(drain)
    item_id = uuid.UUID(_only(desk)["id"])
    with session_scope() as db:
        db.execute(update(Item).where(Item.id == item_id).values(review_state="sent_back", reject_note="Need a byte"))

    # The desk holds the row (as every decision endpoint does) and rejects the story, not yet committed
    desk_db = SessionLocal()
    row = desk_db.get(Item, item_id, with_for_update=True)
    row.review_state, row.reject_reason = "rejected", "not_newsworthy"
    desk_db.flush()

    errors = []

    def reply():
        try:
            with session_scope() as db:
                add_message(db, source="outlook", external_id="R2", sender_contact="stringer@example.com",
                            sender_name="A Stringer", text="SP का बाइट भेज दिया।", received_at=REPLY_AT,
                            raw={"subject": "Re: " + HADSA}, conversation_key="THREAD-1", reporter=None,
                            subject="Re: " + HADSA)
        except Exception as e:  # surfaced below
            errors.append(e)

    t = threading.Thread(target=reply)
    t.start()
    try:
        assert _wait_for_lock_waiter(), "the reply should wait for the desk's row lock"
    finally:
        desk_db.commit()
        desk_db.close()
    t.join(15)
    assert not t.is_alive() and not errors, errors
    with session_scope() as db:
        assert db.get(Item, item_id).review_state == "rejected"
        assert not db.scalars(select(Audit).where(Audit.action == "item.reply_reopen")).all()


# --- L6 the fetch-since cut-off and the retry list -----------------------------------------------------------------

def test_l6_retried_mail_older_than_the_cut_off_is_dropped_unfetched(stack, drain, monkeypatch):
    from app.config import get_settings

    fetched = []
    real_get = mail_service.get_message
    monkeypatch.setattr(mail_service, "get_message", lambda mid: fetched.append(mid) or real_get(mid))
    stack.mail_batch.append(mail("FAILS", HADSA, received="2026-10-17T06:00:00Z"))
    monkeypatch.setattr(tasks, "_ingest_mail", lambda db, m: (_ for _ in ()).throw(RuntimeError("boom")))
    poll(drain)
    with session_scope() as db:
        entry = db.get(KV, "mail_retry").value["FAILS"]
        assert entry["received"].startswith("2026-10-17T06:00:00")
    stack.mail_batch.clear()  # no longer in the delta: only the retry list would bring it back

    monkeypatch.setenv("MAIL_FETCH_SINCE", "2026-10-18T00:00:00+00:00")  # the cut-off moves past it
    get_settings.cache_clear()
    try:
        poll(drain)
    finally:
        get_settings.cache_clear()
    assert fetched == [], "a retry older than the cut-off must not be fetched again"
    with session_scope() as db:
        assert "FAILS" not in db.get(KV, "mail_retry").value


# --- follow-up review: row locks are not held for a whole mail poll ----------------------------------------------

def test_fetch_mail_releases_a_reopened_items_lock_before_the_next_message(stack, drain, desk, monkeypatch):
    """The reply reopening a sent-back story locks its row; the poll must commit that message before going on,
    so a desk edit on the story is not blocked until the poll ends."""
    stack.mail_batch.append(mail("R1", HADSA, conversation="THREAD-1"))
    poll(drain)
    item_id = uuid.UUID(_only(desk)["id"])
    with session_scope() as db:
        db.execute(update(Item).where(Item.id == item_id).values(review_state="sent_back", reject_note="Need a byte"))

    seen = []

    def list_attachments(mid):
        # Runs while the poll is on the next message: another connection must get the reopened row at once
        with SessionLocal() as other:
            other.execute(text("SET LOCAL lock_timeout = '500ms'"))
            try:
                seen.append(other.execute(select(Item.review_state).where(Item.id == item_id)
                                          .with_for_update(nowait=True)).scalar_one())
            except Exception as e:
                seen.append(type(e).__name__)
            other.rollback()
        return []

    monkeypatch.setattr(mail_service, "list_attachments", list_attachments)
    reply = mail("R2", "Re: " + HADSA, conversation="THREAD-1", received="2026-10-16T07:00:00Z")
    other_story = {**mail("N1", "Press Release | x", sender="pr@agency.example"), "hasAttachments": True}
    stack.mail_batch[:] = [reply, other_story]
    poll(drain)
    assert seen == ["pending"], "the reopened item was still locked (or not yet committed) during the poll"
    with session_scope() as db:
        assert db.get(Item, item_id).review_state == "pending"


# --- follow-up review: "old gone, new exists" is adopted only when the folder is this story's --------------------

def _move_folder_away(stack, old_slug, target):
    """What an earlier rename attempt that died before its commit leaves behind: old gone, new holding the files."""
    shutil.move(str(stack.ftp_folder(old_slug)), str(stack.ftp_folder(target)))


def test_ftp_rename_adopts_a_folder_holding_this_storys_files(desk, filed_item, drain, item, stack):
    item_id = filed_item()
    old = item(item_id)
    target = "171026_UDR_HAND_TYPED_AV_SUNITA"
    _move_folder_away(stack, old.slug, target)
    assert desk.patch(f"/desk/items/{item_id}", json={"version": old.version, "slug": target}).status_code == 200
    drain()
    after = item(item_id)
    assert after.ftp_folder.endswith("/" + target) and not after.intake_note
    assert after.media and all(m.ftp_path.startswith(after.ftp_folder + "/") for m in after.media)


def test_ftp_rename_adopts_an_empty_folder(desk, filed_item, drain, item, stack):
    item_id = filed_item()
    old = item(item_id)
    target = "171026_UDR_HAND_TYPED_AV_SUNITA"
    shutil.rmtree(stack.ftp_folder(old.slug))
    stack.ftp_folder(target).mkdir(parents=True)
    assert desk.patch(f"/desk/items/{item_id}", json={"version": old.version, "slug": target}).status_code == 200
    drain()
    assert item(item_id).ftp_folder.endswith("/" + target)


def test_ftp_rename_refuses_someone_elses_folder_when_old_is_gone(desk, filed_item, drain, item, stack):
    item_id = filed_item()
    old = item(item_id)
    target = "171026_UDR_HAND_TYPED_AV_SUNITA"
    shutil.rmtree(stack.ftp_folder(old.slug))
    stack.ftp_folder(target).mkdir(parents=True)
    (stack.ftp_folder(target) / "someone-elses.mp4").write_bytes(b"theirs")
    assert desk.patch(f"/desk/items/{item_id}", json={"version": old.version, "slug": target}).status_code == 200
    drain()  # gives up at once, no retries
    after = item(item_id)
    assert after.ftp_folder == old.ftp_folder
    assert "isn't this story's" in (after.intake_note or "")
    assert sorted(p.name for p in stack.ftp_folder(target).iterdir()) == ["someone-elses.mp4"]
