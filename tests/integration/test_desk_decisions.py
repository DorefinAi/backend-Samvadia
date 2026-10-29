"""Desk decisions v2: hold, send back, duplicate, reasons from the lookup tables, and the training fields."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.models import Decision, DecisionAction, Item, RejectReason


def decisions(item_id) -> list[Decision]:
    with session_scope() as db:
        rows = db.scalars(select(Decision).where(Decision.item_id == item_id).order_by(Decision.created_at)).all()
        db.expunge_all()
        return list(rows)


def state(desk, item_id) -> str:
    return desk.get(f"/desk/items/{item_id}").json()["item"]["review_state"]


# --- /desk/meta ------------------------------------------------------------------------------------

def test_meta_serves_the_lookup_tables(desk):
    meta = desk.get("/desk/meta").json()
    assert [a["code"] for a in meta["actions"]] == ["approve", "needs_more", "hold", "duplicate", "reject", "reopen"]
    assert meta["actions"][0] == {"code": "approve", "label_en": "Approve", "label_hi": "स्वीकार", "target": "positive"}
    assert len(meta["reject_reasons"]) == 13 and meta["reject_reasons"][-1]["code"] == "other"
    assert {"code": "duplicate", "label_en": "Already have this story", "label_hi": "खबर पहले से है",
            "owner": "dedupe"} in meta["reject_reasons"]
    assert [r["code"] for r in meta["approve_reasons"]][:2] == ["must_carry", "exclusive"]
    assert "owner" not in meta["approve_reasons"][0]
    assert "sent_back" in meta["review_states"] and "BRIEF" in meta["story_types"]


def test_meta_hides_inactive_rows_and_reject_refuses_them(desk, filed_item):
    with session_scope() as db:
        db.execute(update(RejectReason).where(RejectReason.code == "too_late").values(active=False))
    try:
        assert "too_late" not in [r["code"] for r in desk.get("/desk/meta").json()["reject_reasons"]]
        item_id = filed_item()
        assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "too_late"}).status_code == 422
    finally:
        with session_scope() as db:
            db.execute(update(RejectReason).where(RejectReason.code == "too_late").values(active=True))


# --- reject / approve ----------------------------------------------------------------------------------

def test_reject_other_needs_a_note(desk, filed_item):
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "other"}).status_code == 422
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "other", "note": "  "}).status_code == 422
    r = desk.post(f"/desk/items/{item_id}/reject", json={"reason": "other", "note": "Advertorial"})
    assert r.status_code == 200 and r.json()["item"]["reject_note"] == "Advertorial"
    assert decisions(item_id)[0].reject_reason == "other"


def test_old_reason_poor_visuals_is_gone(desk, filed_item):
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "poor_visuals"}).status_code == 422
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "no_visuals"}).status_code == 200


def test_approve_with_reason_importance_and_time(desk, filed_item):
    item_id = filed_item()
    r = desk.post(f"/desk/items/{item_id}/approve",
                  json={"approve_reason": "exclusive", "importance": 4, "seconds_on_item": 42, "note": "lead"})
    assert r.status_code == 200, r.text
    d = decisions(item_id)[0]
    assert (d.action, d.approve_reason, d.importance, d.seconds_on_item, d.note) == ("approve", "exclusive", 4, 42,
                                                                                    "lead")
    assert d.reject_reason is None and d.slug_final == r.json()["item"]["slug"]
    assert d.headline_final == "उदयपुर में पर्यटन सीजन शुरू" and d.body_final.startswith("झीलों")
    assert d.aired is None and d.was_lead is None and d.ai_score_shown is None


def test_approve_validates_reason_and_importance(desk, filed_item):
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/approve", json={"approve_reason": "vibes"}).status_code == 422
    for bad in (0, 6):
        assert desk.post(f"/desk/items/{item_id}/approve", json={"importance": bad}).status_code == 422
    assert state(desk, item_id) == "pending"


def test_approve_without_a_body_still_works(desk, filed_item):
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/approve").status_code == 200
    d = decisions(item_id)[0]
    assert d.approve_reason is None and d.importance is None and d.seconds_on_item is None


@pytest.mark.parametrize("sent, stored", [(-5, 0), (999999, 86400), (17, 17)])
def test_seconds_on_item_is_clamped(desk, filed_item, sent, stored):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/hold", json={"seconds_on_item": sent})
    assert decisions(item_id)[0].seconds_on_item == stored


def test_opened_at_gives_seconds_on_item(desk, filed_item):
    item_id = filed_item()
    opened = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    assert desk.post(f"/desk/items/{item_id}/hold", json={"opened_at": opened}).status_code == 200
    assert 85 <= decisions(item_id)[0].seconds_on_item <= 150
    other = filed_item(subject="Naive time")
    r = desk.post(f"/desk/items/{other}/hold", json={"opened_at": "2026-10-27T10:00:00"})
    assert r.status_code == 422, "opened_at needs a time zone"


def test_was_blind_is_copied_from_the_item(desk, filed_item):
    blind, seen = filed_item(), filed_item(subject="Seen story")
    with session_scope() as db:
        db.execute(update(Item).where(Item.id == uuid.UUID(blind)).values(blind=True))
        db.execute(update(Item).where(Item.id == uuid.UUID(seen)).values(blind=False))
    desk.post(f"/desk/items/{blind}/reject", json={"reason": "pr_puff"})
    desk.post(f"/desk/items/{seen}/approve")
    assert decisions(blind)[0].was_blind is True
    assert decisions(seen)[0].was_blind is False


# --- hold ----------------------------------------------------------------------------------------------

def test_hold_then_approve(desk, filed_item):
    item_id = filed_item()
    r = desk.post(f"/desk/items/{item_id}/hold", json={"note": "wait for the DM's statement"})
    assert r.status_code == 200 and r.json()["item"]["review_state"] == "held"
    assert desk.post(f"/desk/items/{item_id}/hold").status_code == 409, "already held"
    assert desk.post(f"/desk/items/{item_id}/approve").status_code == 200
    assert [d.action for d in decisions(item_id)] == ["hold", "approve"]
    assert decisions(item_id)[0].note == "wait for the DM's statement"


def test_hold_then_reject_or_reopen(desk, filed_item):
    a, b = filed_item(), filed_item(subject="Second story")
    desk.post(f"/desk/items/{a}/hold")
    desk.post(f"/desk/items/{b}/hold")
    assert desk.post(f"/desk/items/{a}/reject", json={"reason": "unverified"}).status_code == 200
    assert desk.post(f"/desk/items/{b}/reopen").status_code == 200
    assert state(desk, a) == "rejected" and state(desk, b) == "pending"


def test_held_story_shows_as_reviewing_to_the_reporter(desk, filed_item, reporter_client):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/hold")
    item = reporter_client.get(f"/reporter/items/{item_id}").json()["item"]
    assert item["status"] == "reviewing" and item["review_state"] == "held"


# --- send back -----------------------------------------------------------------------------------------

def test_send_back_keeps_the_note_for_the_reporter(desk, filed_item, reporter_client):
    item_id = filed_item()
    r = desk.post(f"/desk/items/{item_id}/send-back", json={"note": "Send the SP's byte"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["review_state"] == "sent_back"
    d = decisions(item_id)[0]
    assert d.action == "needs_more" and d.note == "Send the SP's byte" and d.reject_reason is None
    mine = reporter_client.get(f"/reporter/items/{item_id}").json()["item"]
    assert mine["review_state"] == "sent_back" and mine["reject_note"] == "Send the SP's byte"
    assert desk.post(f"/desk/items/{item_id}/approve").status_code == 409, "a sent-back story is reopened first"
    assert desk.post(f"/desk/items/{item_id}/reopen").status_code == 200
    assert desk.get(f"/desk/items/{item_id}").json()["item"]["reject_note"] is None


# --- duplicate -----------------------------------------------------------------------------------------

def test_duplicate_points_at_the_other_story(desk, filed_item):
    original, copy = filed_item(), filed_item(subject="Same event again")
    r = desk.post(f"/desk/items/{copy}/duplicate", json={"duplicate_of": original})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["review_state"] == "duplicate" and r.json()["item"]["duplicate_of"] == original
    d = decisions(copy)[0]
    assert d.action == "duplicate" and str(d.duplicate_of) == original
    assert desk.get("/desk/items", params={"review_state": "duplicate"}).json()["counts"]["duplicate"] == 1


def test_duplicate_refuses_itself_unknown_and_missing(desk, filed_item):
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/duplicate", json={"duplicate_of": item_id}).status_code == 422
    assert desk.post(f"/desk/items/{item_id}/duplicate", json={"duplicate_of": str(uuid.uuid4())}).status_code == 422
    assert desk.post(f"/desk/items/{item_id}/duplicate", json={}).status_code == 422
    assert desk.post(f"/desk/items/{item_id}/duplicate", json={"duplicate_of": "nope"}).status_code == 422
    assert state(desk, item_id) == "pending" and decisions(item_id) == []


def test_duplicate_refuses_another_org(desk, filed_item):
    item_id, other = filed_item(), filed_item(subject="Other org story")
    with session_scope() as db:
        db.execute(update(Item).where(Item.id == uuid.UUID(other)).values(org_id=uuid.uuid4()))
    assert desk.post(f"/desk/items/{item_id}/duplicate", json={"duplicate_of": other}).status_code == 422


def test_duplicate_refuses_a_cycle(desk, filed_item):
    a, b = filed_item(), filed_item(subject="Same event again")
    assert desk.post(f"/desk/items/{a}/duplicate", json={"duplicate_of": b}).status_code == 200
    r = desk.post(f"/desk/items/{b}/duplicate", json={"duplicate_of": a})
    assert r.status_code == 409, r.text
    assert state(desk, b) == "pending" and decisions(b) == []


def test_duplicate_refuses_a_target_that_is_itself_a_duplicate(desk, filed_item):
    original, copy, third = filed_item(), filed_item(subject="Copy"), filed_item(subject="Third")
    assert desk.post(f"/desk/items/{copy}/duplicate", json={"duplicate_of": original}).status_code == 200
    r = desk.post(f"/desk/items/{third}/duplicate", json={"duplicate_of": copy})
    assert r.status_code == 409 and original in r.json()["detail"]
    assert desk.post(f"/desk/items/{third}/duplicate", json={"duplicate_of": original}).status_code == 200


def test_duplicate_of_a_rejected_story_is_allowed(desk, filed_item):
    original, copy = filed_item(), filed_item(subject="Same event again")
    desk.post(f"/desk/items/{original}/reject", json={"reason": "not_newsworthy"})
    assert desk.post(f"/desk/items/{copy}/duplicate", json={"duplicate_of": original}).status_code == 200


def test_reopen_clears_duplicate_of(desk, filed_item):
    original, copy = filed_item(), filed_item(subject="Same event again")
    desk.post(f"/desk/items/{copy}/duplicate", json={"duplicate_of": original})
    r = desk.post(f"/desk/items/{copy}/reopen")
    assert r.status_code == 200 and r.json()["item"]["duplicate_of"] is None
    assert r.json()["item"]["review_state"] == "pending"
    assert [d.action for d in decisions(copy)] == ["duplicate", "reopen"]


# --- state rules ----------------------------------------------------------------------------------------

def test_approved_story_cannot_be_moved(desk, filed_item):
    item_id, other = filed_item(), filed_item(subject="Other")
    desk.post(f"/desk/items/{item_id}/approve")
    assert desk.post(f"/desk/items/{item_id}/reopen").status_code == 409
    assert desk.post(f"/desk/items/{item_id}/hold").status_code == 409
    assert desk.post(f"/desk/items/{item_id}/send-back").status_code == 409
    assert desk.post(f"/desk/items/{item_id}/duplicate", json={"duplicate_of": other}).status_code == 409
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "quality"}).status_code == 409


def test_rejected_story_must_be_reopened_first(desk, filed_item):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/reject", json={"reason": "wrong_beat"})
    assert desk.post(f"/desk/items/{item_id}/hold").status_code == 409
    assert desk.post(f"/desk/items/{item_id}/send-back").status_code == 409
    assert desk.post(f"/desk/items/{item_id}/reopen").status_code == 200
    assert desk.get(f"/desk/items/{item_id}").json()["item"]["reject_reason"] is None


def test_pending_story_cannot_be_reopened(desk, filed_item):
    assert desk.post(f"/desk/items/{filed_item()}/reopen").status_code == 409


def test_switched_off_action_is_409(desk, filed_item):
    item_id = filed_item()
    with session_scope() as db:
        db.execute(update(DecisionAction).where(DecisionAction.code == "hold").values(active=False))
    try:
        assert desk.post(f"/desk/items/{item_id}/hold").status_code == 409
        assert state(desk, item_id) == "pending"
    finally:
        with session_scope() as db:
            db.execute(update(DecisionAction).where(DecisionAction.code == "hold").values(active=True))


def test_inbox_filters_and_counts_new_states(desk, filed_item):
    a, b, c = filed_item(), filed_item(subject="Second"), filed_item(subject="Third")
    desk.post(f"/desk/items/{a}/hold")
    desk.post(f"/desk/items/{b}/send-back")
    held = desk.get("/desk/items", params={"review_state": "held"}).json()
    assert [i["id"] for i in held["items"]] == [a]
    assert held["counts"] == {"pending": 1, "approved": 0, "rejected": 0, "held": 1, "sent_back": 1, "duplicate": 0,
                              "all": 3}
    both = desk.get("/desk/items", params={"review_state": "pending,held"}).json()["items"]
    assert {i["id"] for i in both} == {a, c}
    assert desk.get("/desk/items", params={"review_state": "parked"}).status_code == 422


def test_reporter_cannot_decide(reporter_client, filed_item):
    item_id = filed_item()
    for path in ("hold", "send-back", "reopen"):
        assert reporter_client.post(f"/desk/items/{item_id}/{path}").status_code == 403


# --- database constraints -------------------------------------------------------------------------------

def _raw_decision(item_id, user_id, **fields) -> None:
    with session_scope() as db:
        db.add(Decision(org_id=uuid.UUID("00000000-0000-0000-0000-000000000001"), item_id=uuid.UUID(item_id),
                        user_id=user_id, snapshot={}, **fields))


@pytest.mark.parametrize("fields", [
    {"action": "reject"},  # reject_needs_reason
    {"action": "duplicate"},  # duplicate_needs_target
    {"action": "hold", "duplicate_of": "SELF"},  # no_self_duplicate
    {"action": "approve", "importance": 6},
    {"action": "approve", "approve_reason": "vibes"},
    {"action": "reject", "reject_reason": "poor_visuals"},
    {"action": "shelve"},
])
def test_decision_constraints(filed_item, make_user, fields):
    item_id = filed_item()
    user_id = make_user("checker", "assignment_desk")
    if fields.get("duplicate_of") == "SELF":
        fields = {**fields, "duplicate_of": uuid.UUID(item_id)}
    with pytest.raises(IntegrityError):
        _raw_decision(item_id, user_id, **fields)


def test_decision_constraints_accept_good_rows(filed_item, make_user):
    a, b = filed_item(), filed_item(subject="Target")
    user_id = make_user("checker", "assignment_desk")
    _raw_decision(a, user_id, action="reject", reject_reason="legal_risk")
    _raw_decision(a, user_id, action="duplicate", duplicate_of=uuid.UUID(b))
    _raw_decision(a, user_id, action="approve", approve_reason="must_carry", importance=5)
    assert len(decisions(a)) == 3


def test_item_cannot_duplicate_itself(filed_item):
    item_id = uuid.UUID(filed_item())
    with pytest.raises(IntegrityError), session_scope() as db:
        db.execute(update(Item).where(Item.id == item_id).values(duplicate_of=item_id, review_state="duplicate"))
    with pytest.raises(IntegrityError), session_scope() as db:
        db.execute(update(Item).where(Item.id == item_id).values(review_state="parked"))


# --- what the decision row records ----------------------------------------------------------------------

def test_snapshot_is_the_item_before_the_verdict(desk, filed_item):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/hold")
    desk.post(f"/desk/items/{item_id}/reject", json={"reason": "legal_risk", "note": "Defamatory"})
    held, rejected = decisions(item_id)
    assert held.snapshot["review_state"] == "pending"
    assert rejected.snapshot["review_state"] == "held"
    assert rejected.snapshot["reject_reason"] is None and rejected.snapshot["reject_note"] is None

    other = filed_item(subject="Fresh one")
    desk.post(f"/desk/items/{other}/reject", json={"reason": "unverified"})
    snap = decisions(other)[0].snapshot
    assert (snap["review_state"], snap["reject_reason"], snap["reject_note"]) == ("pending", None, None)
    approved = filed_item(subject="Third one")
    desk.post(f"/desk/items/{approved}/approve")
    assert decisions(approved)[0].snapshot["review_state"] == "pending"


def test_reopen_snapshot_keeps_what_was_undone(desk, filed_item):
    original, copy = filed_item(), filed_item(subject="Same event again")
    desk.post(f"/desk/items/{copy}/duplicate", json={"duplicate_of": original})
    desk.post(f"/desk/items/{copy}/reopen")
    duplicate, reopen = decisions(copy)
    assert duplicate.snapshot["review_state"] == "pending" and duplicate.snapshot["duplicate_of"] is None
    assert reopen.snapshot["review_state"] == "duplicate" and reopen.snapshot["duplicate_of"] == original


def test_send_back_snapshot_has_no_note_yet(desk, filed_item):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/send-back", json={"note": "Send the SP's byte"})
    snap = decisions(item_id)[0].snapshot
    assert snap["review_state"] == "pending" and snap["reject_note"] is None


def _edit(desk, item_id, **fields):
    version = desk.get(f"/desk/items/{item_id}").json()["item"]["version"]
    r = desk.patch(f"/desk/items/{item_id}", json={"version": version, **fields})
    assert r.status_code == 200, r.text


def test_approve_unchanged_copy(desk, filed_item):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/approve")
    d = decisions(item_id)[0]
    assert (d.headline_changed, d.body_edit_distance) == (False, 0)


def test_approve_records_an_edited_headline(desk, filed_item):
    item_id = filed_item()
    _edit(desk, item_id, headline="उदयपुर में पर्यटन सीजन की शुरुआत")
    desk.post(f"/desk/items/{item_id}/approve")
    d = decisions(item_id)[0]
    assert (d.headline_changed, d.body_edit_distance) == (True, 0)


def test_approve_records_body_edit_distance_against_the_reporters_copy(desk, filed_item):
    item_id = filed_item()  # body: "झीलों के शहर में पर्यटकों की भीड़।"
    _edit(desk, item_id, body="झीलों के शहर में पर्यटकों की भारी भीड़।")  # + "भारी " (5 characters)
    _edit(desk, item_id, body="झीलों के शहर में पर्यटकों की भारी भीड़")  # and the full stop dropped
    desk.post(f"/desk/items/{item_id}/approve")
    d = decisions(item_id)[0]
    assert (d.headline_changed, d.body_edit_distance) == (False, 6), "measured from the reporter, not the last edit"


def test_email_reply_after_a_desk_edit_is_the_reporters_copy(desk, make_reporter, stack, drain):
    from app.jobs import enqueue

    def mail(mid, subject, body, received):
        return {"id": mid, "subject": subject, "conversationId": "CONV1", "hasAttachments": False,
                "receivedDateTime": received, "from": {"emailAddress": {"name": "S", "address": "s@example.com"}},
                "body": {"content": body}}

    def poll():
        with session_scope() as db:
            enqueue(db, "fetch_mail", {"manual": True}, dedupe_key="fetch_mail")
        drain()

    make_reporter(email="s@example.com", phone=None)
    stack.mail_batch[:] = [mail("M1", "अजमेर में मेला", "मेले में भीड़।", "2026-10-17T06:00:00Z")]
    poll()
    with session_scope() as db:
        item_id = str(db.scalars(select(Item)).one().id)
    _edit(desk, item_id, body="मेले में भारी भीड़।")  # + "भारी " (5)
    stack.mail_batch[:] = [mail("M2", "RE: अजमेर में मेला", "फोटो भेज दिए हैं।", "2026-10-17T07:00:00Z")]
    poll()
    assert desk.post(f"/desk/items/{item_id}/approve").status_code == 200
    d = decisions(item_id)[0]
    assert d.body_final == "मेले में भारी भीड़।\n\nफोटो भेज दिए हैं।"
    assert (d.headline_changed, d.body_edit_distance) == (False, 5)


# --- migration 0005 on data ---------------------------------------------------------------------------------

def test_migration_0005_downgrades_and_upgrades_with_data(filed_item, make_user):
    """0005 runs on a database with decisions in it, both ways, and converts what the other schema cannot hold."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    import app.db
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    user = make_user("desk", "assignment_desk")
    org = "00000000-0000-0000-0000-000000000001"
    poor, no_reason, blind = filed_item(), filed_item(subject="No reason"), filed_item(subject="Blind")
    held, sent, dup, wt = (filed_item(subject=s) for s in ("Held", "Sent", "Copy", "Walkthrough"))

    def sql(statement, **params) -> list:
        with session_scope() as db:
            result = db.execute(text(statement), params)
            return result.all() if result.returns_rows else []

    def migrate(target):
        app.db.engine.dispose()
        if target == "head":
            command.upgrade(cfg, target)
        else:
            command.downgrade(cfg, target)
        app.db.engine.dispose()

    migrate("0004")
    try:
        # --- the old schema: reason, three actions, three review states
        insert = ("INSERT INTO decisions (org_id, item_id, action, reason, user_id, snapshot) "
                  "VALUES (:org, :item, :action, :reason, :user, '{}')")
        sql(insert, org=org, item=poor, action="reject", reason="poor_visuals", user=user)
        sql(insert, org=org, item=no_reason, action="reject", reason=None, user=user)
        sql(insert, org=org, item=blind, action="approve", reason=None, user=user)
        sql("UPDATE items SET review_state = 'rejected', reject_reason = 'poor_visuals' WHERE id = :id", id=poor)
        sql("UPDATE items SET blind = true WHERE id = :id", id=blind)

        migrate("head")
        rows = {str(r.item_id): r for r in sql("SELECT item_id, reject_reason, was_blind FROM decisions")}
        assert rows[poor].reject_reason == "no_visuals" and not rows[poor].was_blind
        assert rows[no_reason].reject_reason == "other"
        assert rows[blind].was_blind is True and rows[blind].reject_reason is None
        assert sql("SELECT reject_reason FROM items WHERE id = :id", id=poor)[0].reject_reason == "no_visuals"

        # --- the new schema: what 0004 cannot hold
        sql("UPDATE items SET review_state = 'held' WHERE id = :id", id=held)
        sql("UPDATE items SET review_state = 'sent_back', reject_note = 'byte?' WHERE id = :id", id=sent)
        sql("UPDATE items SET review_state = 'duplicate', duplicate_of = :of WHERE id = :id", id=dup, of=poor)
        sql("UPDATE items SET story_type = 'WT' WHERE id = :id", id=wt)
        insert = ("INSERT INTO decisions (org_id, item_id, action, reject_reason, duplicate_of, user_id, snapshot) "
                  "VALUES (:org, :item, :action, :reason, :of, :user, '{}')")
        for item_id, action, reason, of in [(held, "hold", None, None), (sent, "needs_more", None, None),
                                            (dup, "duplicate", None, poor), (wt, "approve", None, None),
                                            (wt, "reject", "no_visuals", None), (wt, "reopen", None, None)]:
            sql(insert, org=org, item=item_id, action=action, reason=reason, of=of, user=user)

        migrate("0004")
        states = dict(sql("SELECT id::text, review_state FROM items"))
        assert states[held] == states[sent] == states[dup] == "pending" and states[poor] == "rejected"
        assert sql("SELECT story_type FROM items WHERE id = :id", id=wt)[0].story_type is None
        assert sql("SELECT story_type FROM items WHERE id = :id", id=poor)[0].story_type == "AV"
        assert sql("SELECT reject_reason FROM items WHERE id = :id", id=poor)[0].reject_reason == "poor_visuals"
        actions = sorted((str(r.item_id), r.action, r.reason)
                         for r in sql("SELECT item_id, action, reason FROM decisions"))
        assert actions == sorted([(poor, "reject", "poor_visuals"), (no_reason, "reject", "other"),
                                  (blind, "approve", None), (wt, "approve", None), (wt, "reject", "poor_visuals"),
                                  (wt, "reopen", None)])
        columns = {r.column_name for r in sql("SELECT column_name FROM information_schema.columns "
                                              "WHERE table_name = 'items'")}
        assert not {"duplicate_of", "event_key"} & columns
    finally:
        migrate("head")
    assert sql("SELECT count(*) AS n FROM decisions WHERE reject_reason = 'no_visuals'")[0].n == 2
    assert sql("SELECT count(*) AS n FROM reject_reasons")[0].n == 13
