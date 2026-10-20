"""Assignment desk decisions and the Octopus feed."""
from lxml import etree
from sqlalchemy import select

from app.db import session_scope
from app.models import Decision, FeedLog, Story


def version(client, item_id) -> int:
    return client.get(f"/desk/items/{item_id}").json()["item"]["version"]


def feed_items(response):
    root = etree.fromstring(response.content)
    return [{c.tag: c.text for c in it} for it in root.iter("item")]


# --- editing ----------------------------------------------------------------------------------------

def test_stale_version_is_409(desk, filed_item):
    item_id = filed_item()
    v = version(desk, item_id)
    assert desk.patch(f"/desk/items/{item_id}", json={"version": v, "headline": "नई हेडलाइन"}).status_code == 200
    r = desk.patch(f"/desk/items/{item_id}", json={"version": v, "headline": "पुरानी"})
    assert r.status_code == 409


def test_edit_validates_pick_lists(desk, filed_item):
    item_id = filed_item()
    v = version(desk, item_id)
    for bad in ({"category": "Gossip"}, {"priority": "urgent"}, {"story_type": "XX"}, {"city_code": "ZZZ"},
                {"slug": "not a slug"}):
        r = desk.patch(f"/desk/items/{item_id}", json={"version": v, **bad})
        assert r.status_code == 422, bad


def test_changing_subject_renames_ftp_folder(desk, filed_item, drain, item, stack):
    item_id = filed_item()
    old = item(item_id)
    r = desk.patch(f"/desk/items/{item_id}", json={"version": old.version, "slug_subject": "Lake festival"})
    assert r.status_code == 200
    drain()
    new = item(item_id)
    assert "_LAKE_FESTIVAL_AV_SUNITA" in new.slug
    assert stack.ftp_folder(new.slug).is_dir() and not stack.ftp_folder(old.slug).exists()
    assert all(new.slug in m.ftp_path for m in new.media)


def test_city_override_changes_slug(desk, filed_item, drain, item):
    item_id = filed_item()
    r = desk.patch(f"/desk/items/{item_id}", json={"version": version(desk, item_id), "city_code": "jpr"})
    assert r.status_code == 200, r.text
    drain()
    assert "_JPR_" in item(item_id).slug


def test_hand_typed_slug_sticks_until_reset(desk, filed_item, item):
    item_id = filed_item()
    v = version(desk, item_id)
    r = desk.patch(f"/desk/items/{item_id}", json={"version": v, "slug": "171026_UDR_HAND_TYPED_AV_SUNITA"})
    assert r.status_code == 200
    r = desk.patch(f"/desk/items/{item_id}", json={"version": v + 1, "slug_subject": "Something else"})
    assert r.json()["item"]["slug"] == "171026_UDR_HAND_TYPED_AV_SUNITA"
    r = desk.post(f"/desk/items/{item_id}/slug/reset")
    assert r.json()["item"]["slug"].endswith("_SOMETHING_ELSE_AV_SUNITA")


def test_hand_typed_slug_must_be_unique(desk, filed_item, item):
    a, b = filed_item(), filed_item(subject="Other story")
    r = desk.patch(f"/desk/items/{b}", json={"version": version(desk, b), "slug": item(a).slug})
    assert r.status_code == 409


def test_every_edit_is_audited(desk, admin, filed_item):
    item_id = filed_item()
    desk.patch(f"/desk/items/{item_id}", json={"version": version(desk, item_id), "headline": "बदली"})
    actions = [a["action"] for a in desk.get("/desk/activity").json()["activity"]]
    assert "item.edit" in actions


# --- decisions --------------------------------------------------------------------------------------

def test_reject_needs_a_known_reason(desk, filed_item):
    item_id = filed_item()
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "boring"}).status_code == 422
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "duplicate", "note": "same as 10am"}).status_code == 200


def test_reject_reopen_approve_is_logged(desk, filed_item):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/reject", json={"reason": "unverified"})
    assert desk.post(f"/desk/items/{item_id}/reject", json={"reason": "unverified"}).status_code == 409
    assert desk.post(f"/desk/items/{item_id}/reopen").status_code == 200
    assert desk.post(f"/desk/items/{item_id}/approve").status_code == 200
    assert desk.post(f"/desk/items/{item_id}/reopen").status_code == 409, "an approved story cannot be reopened"
    with session_scope() as db:
        actions = [d.action for d in db.scalars(select(Decision).order_by(Decision.created_at))]
        assert actions == ["reject", "reopen", "approve"]
        assert all(d.snapshot["id"] == item_id for d in db.scalars(select(Decision)))


def test_approve_without_slug_is_409(desk, whatsapp_post, drain):
    whatsapp_post([{"id": "wamid.x", "type": "text", "text": {"body": "खबर"}}], wa_id="919111111111")
    drain()
    item_id = desk.get("/desk/items").json()["items"][0]["id"]
    assert desk.post(f"/desk/items/{item_id}/approve").status_code == 409


def test_collecting_items_are_hidden_from_the_inbox(desk, filed_item):
    filed_item(submit=False)
    assert desk.get("/desk/items").json()["items"] == []


def test_inbox_search_and_filters(desk, filed_item):
    a = filed_item(headline="जोधपुर पेयजल पाइपलाइन", subject="Peyjal pipeline")
    filed_item()
    assert [i["id"] for i in desk.get("/desk/items", params={"q": "PEYJAL"}).json()["items"]] == [a]
    desk.post(f"/desk/items/{a}/reject", json={"reason": "duplicate"})
    pending = desk.get("/desk/items", params={"review_state": "pending"}).json()["items"]
    assert a not in [i["id"] for i in pending] and len(pending) == 1


def test_inbox_pages_with_before(desk, filed_item):
    ids = [filed_item(subject=f"Story {n}") for n in ("one", "two", "three")]
    first = desk.get("/desk/items", params={"limit": 2}).json()
    assert len(first["items"]) == 2 and first["next_before"]
    rest = desk.get("/desk/items", params={"limit": 2, "before": first["next_before"]}).json()
    seen = [i["id"] for i in first["items"] + rest["items"]]
    assert sorted(seen) == sorted(ids)


# --- feed -------------------------------------------------------------------------------------------

def test_feed_locks(octopus, stack):
    assert octopus(secret="wrong").status_code == 404
    assert octopus(ip="1.2.3.4").status_code == 404
    assert octopus(ip="10.9.8.7, 1.2.3.4").status_code == 404, "the forged first X-Forwarded-For entry is ignored"
    assert octopus(ip="1.2.3.4, 10.9.8.7").status_code == 200
    with session_scope() as db:
        assert [r.status for r in db.scalars(select(FeedLog).order_by(FeedLog.id))] == [404, 404, 404, 200]


def test_approved_story_appears_in_feed(desk, filed_item, octopus):
    item_id = filed_item(tags=["Breaking"])
    r = desk.post(f"/desk/items/{item_id}/approve")
    assert r.json()["story"]["state"] == "ready"
    items = feed_items(octopus())
    assert len(items) == 1
    slug = desk.get(f"/desk/items/{item_id}").json()["item"]["slug"]
    assert items[0]["title"] == f"FLASH {slug}"
    assert items[0]["guid"] == f"vartmaan-{slug}-v1"
    assert items[0]["description"].strip().startswith("उदयपुर:")
    assert "संवाददाता: Sunita Sharma, उदयपुर ब्यूरो" in items[0]["description"]
    assert items[0]["category"] == "Udaipur"


def test_feed_304_until_something_changes(desk, filed_item, octopus):
    first = octopus()
    again = octopus({"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    desk.post(f"/desk/items/{filed_item()}/approve")
    assert octopus({"If-None-Match": first.headers["etag"]}).status_code == 200


def test_update_and_kill_are_new_versions(admin, desk, filed_item, octopus):
    item_id = filed_item()
    desk.post(f"/desk/items/{item_id}/approve")
    assert desk.post(f"/desk/items/{item_id}/publish-update").status_code == 403, "desk role cannot change the feed"
    up = admin.post(f"/desk/items/{item_id}/publish-update").json()["story"]
    assert up["version"] == 2 and up["title"].startswith("UPDATE ")
    assert up["description"].startswith("((")
    kill = admin.post(f"/desk/items/{item_id}/kill").json()["story"]
    assert kill["version"] == 3 and kill["title"].startswith("KILL ") and kill["state"] == "ready"
    assert admin.post(f"/desk/items/{item_id}/publish-update").status_code == 409, "a killed story stays killed"
    guids = [i["guid"] for i in feed_items(octopus())]
    assert len(guids) == 3 and len(set(guids)) == 3


def test_update_needs_an_approved_story(admin, filed_item):
    item_id = filed_item()
    assert admin.post(f"/desk/items/{item_id}/publish-update").status_code == 409
    assert admin.post(f"/desk/items/{item_id}/kill").status_code == 409


def test_story_waits_for_ftp_then_enters_feed(desk, filed_item, drain, octopus, item):
    item_id = filed_item(run_jobs=False)
    drain(queues=["default"])  # slug built, upload queued but not run
    assert item(item_id).intake_state == "uploading"
    r = desk.post(f"/desk/items/{item_id}/approve")
    assert r.json()["story"]["state"] == "waiting"
    assert feed_items(octopus()) == []
    drain()
    assert len(feed_items(octopus())) == 1


def test_collected_at_shows_first_poll_after_publish(admin, filed_item, octopus):
    item_id = filed_item()
    admin.post(f"/desk/items/{item_id}/approve")
    assert admin.get("/desk/stories").json()["stories"][0]["collected_at"] is None
    octopus()
    assert admin.get("/desk/stories").json()["stories"][0]["collected_at"] is not None
    assert admin.get("/desk/feed-log").json()["last_collected_at"] is not None


def test_blind_flag_rate_is_roughly_ten_percent(filed_item, desk):
    for n in range(40):
        filed_item(files=(), subject=f"Blind {n}", run_jobs=False)
    with session_scope() as db:
        from app.models import Item
        blind = sum(1 for i in db.scalars(select(Item)) if i.blind)
    assert 0 <= blind <= 15  # 10% of 40 = 4; this only catches a broken rate, not bad luck


def test_reporter_master_add_and_match(admin, desk, whatsapp_post, drain):
    body = {"name": "Ramesh", "slug_code": "ramesh", "city_code": "kota", "phone": "+91 98000 11111"}
    assert desk.post("/desk/reporters", json=body).status_code == 403
    r = admin.post("/desk/reporters", json=body)
    assert r.status_code == 200 and r.json()["reporter"]["phone"] == "919800011111"
    whatsapp_post([{"id": "wamid.k", "type": "text", "text": {"body": "कोटा में कोचिंग छात्रों का प्रदर्शन"}}],
                  wa_id="919800011111")
    drain()
    slug = desk.get("/desk/items").json()["items"][0]["slug"]
    assert slug and "_KOTA_" in slug and slug.endswith("_AV_RAMESH")


def test_slug_reset_is_refused_once_published(desk, filed_item, item):
    item_id = filed_item()
    desk.patch(f"/desk/items/{item_id}", json={"version": version(desk, item_id), "slug": "201026_UDR_HAND_AV_SUNITA"})
    desk.post(f"/desk/items/{item_id}/approve")
    r = desk.post(f"/desk/items/{item_id}/slug/reset")
    assert r.status_code == 409, r.text
    assert item(item_id).slug == "201026_UDR_HAND_AV_SUNITA"


def test_feed_answers_head_like_get(octopus, filed_item, desk):
    from fastapi.testclient import TestClient

    from app.main import app
    desk.post(f"/desk/items/{filed_item()}/approve")
    get = octopus()
    head = TestClient(app).head("/feed/feed-secret/feed.xml", headers={"X-Forwarded-For": "10.9.8.7"})
    assert head.status_code == 200 and head.content == b""
    assert head.headers["etag"] == get.headers["etag"]
    with session_scope() as db:
        assert db.scalar(select(FeedLog).order_by(FeedLog.id.desc()).limit(1)).status == 200
