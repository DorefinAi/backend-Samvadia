"""Owner console: permission catalogue, per-user overrides, page keys on the desk reads, the /owner API
(handoff/owner-console-contract.md sections 2 and 3) and migration 0006."""
from pathlib import Path

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app

KEY = "owner-key-for-tests-0123456789-abcdefghij"  # 40 chars


@pytest.fixture(autouse=True)
def _reset_owner_limiter():
    from app.security import owner_limiter
    owner_limiter.failures.clear()
    yield
    owner_limiter.failures.clear()


@pytest.fixture
def owner_key(monkeypatch, stack):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "owner_api_key", KEY)
    return KEY


@pytest.fixture
def owner(owner_key):
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {owner_key}", "X-Owner-Actor": "vansh"})
    return client


def _sessions(user_id) -> int:
    from app.db import session_scope
    from app.models import UserSession
    with session_scope() as db:
        return db.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == user_id))


def _set(owner, user_id, **overrides):
    body = {"overrides": [{"permission": k.replace("__", "."), "allowed": v} for k, v in overrides.items()]}
    r = owner.put(f"/owner/people/{user_id}/overrides", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- catalogue and defaults ---------------------------------------------------------------------

def test_role_defaults_are_the_old_rules():
    """The action keys give every role exactly what the role table gave before the console."""
    from app.security import CATALOGUE, PAGE_ROUTES, PERMISSIONS
    desk_read = ("super_admin", "admin", "assignment_desk", "chief_editor")
    desk_write = ("super_admin", "admin", "assignment_desk")
    admins = ("super_admin", "admin")
    before = {"desk.view": desk_read, "desk.review": desk_write, "desk.fetch": desk_write,
              "reporters.manage": admins, "feed.view": admins, "feed.manage": admins, "ftp.manage": admins,
              "users.manage": admins, "report.submit": ("reporter",)}
    for key, roles in before.items():
        assert PERMISSIONS[key] == roles, key
    # page defaults = the desk app's rbac.ts ROUTE_PERMISSION
    assert PERMISSIONS["page.dashboard"] == desk_read and PERMISSIONS["page.inbox"] == desk_read
    assert PERMISSIONS["page.reporters"] == desk_write
    assert PERMISSIONS["page.feed"] == PERMISSIONS["page.ftp"] == PERMISSIONS["page.access"] == admins
    assert set(PAGE_ROUTES) == {p["key"] for p in CATALOGUE if p["group"] == "pages"}
    assert all(p["label"] and p["description"] and p["group"] in ("pages", "actions") for p in CATALOGUE)


@pytest.mark.parametrize("role,pages", [
    ("super_admin", ["/dashboard", "/inbox", "/feed", "/ftp", "/reporters", "/access"]),
    ("admin", ["/dashboard", "/inbox", "/feed", "/ftp", "/reporters", "/access"]),
    ("assignment_desk", ["/dashboard", "/inbox", "/reporters"]),
    ("chief_editor", ["/dashboard", "/inbox"]),
])
def test_me_lists_pages_by_role(make_user, login, role, pages):
    make_user("someone", role)
    me = login("someone").get("/me").json()
    assert me["pages"] == pages
    assert [p for p in me["permissions"] if p.startswith("page.")] == [
        k for k, v in {"page.dashboard": "/dashboard", "page.inbox": "/inbox", "page.feed": "/feed", "page.ftp": "/ftp",
                       "page.reporters": "/reporters", "page.access": "/access"}.items() if v in pages]


def test_reporter_has_no_desk_pages_and_keeps_the_portal(reporter_client):
    me = reporter_client.get("/me").json()
    assert me["pages"] == [] and me["permissions"] == ["report.submit"]
    assert reporter_client.get("/reporter/items").status_code == 200
    assert reporter_client.get("/desk/items").status_code == 403


# --- overrides: precedence and page enforcement ------------------------------------------------

def test_override_beats_role_both_ways_and_applies_at_once(owner, make_user, login):
    uid = make_user("desk2", "assignment_desk")
    desk = login("desk2")
    assert desk.get("/desk/stories").status_code == 403
    assert desk.post("/desk/refresh").status_code != 403
    _set(owner, uid, feed__view=True, page__feed=True, desk__fetch=False)
    # no new login needed: overrides are read on every request
    assert desk.get("/desk/stories").status_code == 200
    assert desk.post("/desk/refresh").status_code == 403
    me = desk.get("/me").json()
    assert "/feed" in me["pages"] and "feed.view" in me["permissions"] and "desk.fetch" not in me["permissions"]
    # [] clears: back to the role
    _set(owner, uid)
    assert desk.get("/desk/stories").status_code == 403
    assert "desk.fetch" in desk.get("/me").json()["permissions"]


def test_login_response_uses_overrides(owner, make_user, login):
    uid = make_user("chief", "chief_editor")
    _set(owner, uid, page__inbox=False)
    r = TestClient(app).post("/auth/login", json={"username": "chief", "password": "test-pass-123"})
    assert r.json()["pages"] == ["/dashboard"]


def test_inbox_page_key_guards_inbox_reads(owner, make_user, login, filed_item):
    item_id = filed_item()
    uid = make_user("desk2", "assignment_desk")
    desk = login("desk2")
    assert desk.get(f"/desk/items/{item_id}").status_code == 200
    _set(owner, uid, page__inbox=False)
    assert desk.get(f"/desk/items/{item_id}").status_code == 403
    assert desk.get("/desk/meta").status_code == 403
    assert desk.get("/desk/locations").status_code == 403
    assert desk.get("/desk/ftp/browse").status_code == 403
    # the overview and the reporter list still load what they need
    assert desk.get("/desk/items").status_code == 200
    assert desk.get("/desk/fetch-runs").status_code == 200
    assert desk.get("/desk/districts").status_code == 200
    assert desk.get("/desk/reporters").status_code == 200  # page.reporters
    _set(owner, uid, page__inbox=False, page__reporters=False)
    assert desk.get("/desk/reporters").status_code == 403


def test_story_list_needs_one_of_the_pages_built_from_it(owner, make_user, login):
    uid = make_user("chief", "chief_editor")
    chief = login("chief")
    _set(owner, uid, page__inbox=False)
    assert chief.get("/desk/items").status_code == 200  # the overview still shows counts
    _set(owner, uid, page__inbox=False, page__dashboard=False)
    assert chief.get("/desk/items").status_code == 403
    assert chief.get("/desk/fetch-runs").status_code == 403
    assert chief.get("/events", params={"app": "desk"}).status_code == 403
    assert chief.get("/desk/activity").status_code == 403
    # desk.view off stops every read even with the page on
    _set(owner, uid, desk__view=False)
    assert chief.get("/desk/items").status_code == 403


def test_feed_and_access_pages_guard_their_reads_but_not_the_actions(owner, make_user, login):
    uid = make_user("lead2", "admin")
    lead = login("lead2")
    assert lead.get("/desk/stories").status_code == 200 and lead.get("/desk/feed-log").status_code == 200
    _set(owner, uid, page__feed=False, page__access=False)
    assert lead.get("/desk/stories").status_code == 403
    assert lead.get("/desk/feed-log").status_code == 403
    assert lead.get("/admin/users").status_code == 403
    # page keys guard reads only; adding a login is the users.manage action
    r = lead.post("/admin/users", json={"name": "N", "username": "newdesk", "password": "long-enough",
                                         "role": "assignment_desk"})
    assert r.status_code == 200, r.text


def test_reporter_portal_follows_report_submit(owner, make_user, make_reporter, login):
    uid = make_user("sunita", "reporter")
    make_reporter(user_id=uid)
    rep = login("sunita")
    assert rep.get("/reporter/items").status_code == 200
    _set(owner, uid, report__submit=False)
    assert rep.get("/reporter/items").status_code == 403
    _set(owner, uid)
    assert rep.get("/reporter/items").status_code == 200


# --- disabling ends sessions ---------------------------------------------------------------------

def test_admin_disable_deletes_sessions(make_user, login, admin):
    uid = make_user("desk2", "assignment_desk")
    login("desk2")
    login("desk2")
    assert _sessions(uid) == 2
    assert admin.patch(f"/admin/users/{uid}", json={"active": False}).status_code == 200
    assert _sessions(uid) == 0


def test_admin_cannot_remove_the_last_super_admin(make_user, login):
    boss = make_user("boss", "super_admin")
    client = login("boss")
    r = client.patch(f"/admin/users/{boss}", json={"active": False})
    assert r.status_code == 409
    assert client.patch(f"/admin/users/{boss}", json={"role": "admin"}).status_code == 409
    make_user("boss2", "super_admin")
    assert client.patch(f"/admin/users/{boss}", json={"role": "admin"}).status_code == 200


# --- owner API: auth -------------------------------------------------------------------------------

def test_owner_api_is_404_without_a_long_enough_key(monkeypatch, stack):
    from app.config import get_settings
    headers = {"Authorization": "Bearer ", "X-Owner-Actor": "vansh"}
    monkeypatch.setattr(get_settings(), "owner_api_key", "")
    assert TestClient(app).get("/owner/people", headers=headers).status_code == 404
    monkeypatch.setattr(get_settings(), "owner_api_key", "short-key")
    r = TestClient(app).get("/owner/people", headers={**headers, "Authorization": "Bearer short-key"})
    assert r.status_code == 404
    # off is decided before the body: even a broken body is a plain 404
    r = TestClient(app).put("/owner/people/x/overrides", content=b"{", headers={"Content-Type": "application/json"})
    assert r.status_code == 404


def test_owner_api_needs_the_bearer_key(owner_key):
    client = TestClient(app)
    for auth in (None, "Bearer wrong-key", f"Basic {owner_key}", owner_key, f"Bearer {owner_key}x"):
        headers = {"X-Owner-Actor": "vansh", **({"Authorization": auth} if auth else {})}
        r = client.get("/owner/people", headers=headers)
        assert r.status_code == 401, auth
        assert r.json()["error"] == "unauthorized"
    # no session cookie works instead of the key
    assert client.get("/owner/history", headers={"X-Owner-Actor": "vansh"}).status_code == 401


@pytest.mark.parametrize("actor", [None, "", "Vansh", "van sh", "a" * 65, "vansh;drop", "वंश"])
def test_owner_actor_header_is_validated(owner_key, actor):
    headers = {"Authorization": f"Bearer {owner_key}"}
    if actor is not None:
        headers["X-Owner-Actor"] = actor.encode()  # bytes: httpx would refuse the non-ASCII one as text
    r = TestClient(app).get("/owner/people", headers=headers)
    assert r.status_code == 400, actor
    assert r.json()["error"] == "invalid_actor"


def test_owner_failures_are_rate_limited_per_ip(owner):
    bad = TestClient(app)
    hdr = {"Authorization": "Bearer wrong", "X-Owner-Actor": "vansh", "X-Forwarded-For": "10.1.1.1"}
    for _ in range(20):
        assert bad.get("/owner/people", headers=hdr).status_code == 401
    r = bad.get("/owner/people", headers=hdr)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited" and r.headers["retry-after"] == "600"
    # the right key always passes, even from the blocked IP; another IP is not blocked
    assert owner.get("/owner/people", headers={"X-Forwarded-For": "10.1.1.1"}).status_code == 200
    assert bad.get("/owner/people", headers=hdr).status_code == 429
    assert bad.get("/owner/people", headers={**hdr, "X-Forwarded-For": "10.2.2.2"}).status_code == 401


def test_successful_owner_calls_are_not_counted(owner):
    for _ in range(25):
        assert owner.get("/owner/people", headers={"X-Forwarded-For": "10.3.3.3"}).status_code == 200


# --- owner API: routes ---------------------------------------------------------------------------

def test_people_lists_everyone_with_catalogue_roles_and_overrides(owner, make_user, login):
    uid = make_user("desk2", "assignment_desk")
    make_user("gone", "chief_editor", active=False)
    login("desk2")
    _set(owner, uid, page__feed=True)
    body = owner.get("/owner/people").json()
    assert body["system"] == "samvadia"
    assert {"key": "page.inbox", "group": "pages", "label": "Inbox"}.items() <= next(
        c for c in body["catalogue"] if c["key"] == "page.inbox").items()
    assert {c["key"] for c in body["catalogue"]} >= {"page.dashboard", "desk.review", "users.manage", "report.submit"}
    assert {"key": "chief_editor", "label": "Chief editor"} in body["roles"]
    applies = {c["key"]: c["appliesTo"] for c in body["catalogue"]}
    assert applies["report.submit"] == ["reporter"]
    assert applies["page.inbox"] == applies["desk.review"] == ["super_admin", "admin", "assignment_desk", "chief_editor"]
    people = {u["username"]: u for u in body["users"]}
    desk = people["desk2"]
    assert desk["id"] == str(uid) and desk["role"] == "assignment_desk" and desk["roleLabel"] == "Assignment desk"
    assert desk["active"] is True and desk["lastLoginAt"] and desk["email"] is None
    assert desk["overrides"] == [{"permission": "page.feed", "allowed": True}]
    assert people["gone"]["active"] is False and people["gone"]["lastLoginAt"] is None
    assert set(desk) == {"id", "username", "name", "email", "role", "roleLabel", "active", "lastLoginAt", "overrides"}


def test_permissions_shows_source(owner, make_user):
    uid = make_user("desk2", "assignment_desk")
    body = _set(owner, uid, desk__review=False, feed__view=True)
    assert body == owner.get(f"/owner/people/{uid}/permissions").json()
    perms = {p["key"]: p for p in body["permissions"]}
    assert body["role"] == "assignment_desk"
    assert perms["desk.review"] == {"key": "desk.review", "allowed": False, "source": "override", "roleAllowed": True}
    assert perms["feed.view"] == {"key": "feed.view", "allowed": True, "source": "override", "roleAllowed": False}
    assert perms["page.inbox"] == {"key": "page.inbox", "allowed": True, "source": "role", "roleAllowed": True}
    assert perms["users.manage"] == {"key": "users.manage", "allowed": False, "source": "role", "roleAllowed": False}


def _invalid(r):
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid" and r.json()["message"]


def test_owner_bodies_are_strict(owner, make_user):
    uid = make_user("desk2", "assignment_desk")
    _invalid(owner.patch(f"/owner/people/{uid}", json={"active": True, "password": "x"}))
    _invalid(owner.patch(f"/owner/people/{uid}", json={"active": "false"}))  # strict: no "false" -> False
    _invalid(owner.patch(f"/owner/people/{uid}", json={"active": 0}))
    _invalid(owner.put(f"/owner/people/{uid}/overrides", json={"overrides": [{"permission": "desk.view", "allowed": 1}]}))
    _invalid(owner.put(f"/owner/people/{uid}/overrides",
                       json={"overrides": [{"permission": "desk.view", "allowed": True, "extra": 1}]}))
    _invalid(owner.put(f"/owner/people/{uid}/overrides", json={"overrides": [], "more": 1}))
    _invalid(owner.get("/owner/people/not-a-uuid/permissions"))
    r = owner.put(f"/owner/people/{uid}/overrides", json={"overrides": [{"permission": "desk.nuke", "allowed": True}]})
    assert r.status_code == 422 and r.json()["error"] == "unknown_permission" and r.json()["keys"] == ["desk.nuke"]
    dup = [{"permission": "desk.view", "allowed": True}, {"permission": "desk.view", "allowed": False}]
    assert owner.put(f"/owner/people/{uid}/overrides", json={"overrides": dup}).json()["error"] == "duplicate_permission"
    assert owner.patch(f"/owner/people/{uid}", json={"role": "boss"}).json()["error"] == "unknown_role"
    r = owner.get("/owner/people/00000000-0000-0000-0000-00000000abcd/permissions")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    # nothing above changed anything
    assert owner.get(f"/owner/people/{uid}/permissions").json()["permissions"][0]["source"] == "role"


def test_owner_checks_run_before_the_body_is_read(owner_key, owner):
    client = TestClient(app)
    junk = {"Content-Type": "application/json", "X-Owner-Actor": "vansh"}
    # a wrong key on a broken body is 401, not a parse error: nothing is parsed before the key check
    r = client.put("/owner/people/x/overrides", content=b"{not json", headers={**junk, "Authorization": "Bearer no"})
    assert r.status_code == 401
    r = client.put("/owner/people/x/overrides", content=b"{not json", headers={**junk, "X-Owner-Actor": "BAD",
                                                                                  "Authorization": f"Bearer {owner_key}"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_actor"
    big = b'{"overrides": []' + b" " * (64 * 1024) + b"}"
    r = owner.put("/owner/people/00000000-0000-0000-0000-000000000001/overrides", content=big,
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"

    def chunks():
        yield b'{"overrides": []}'
    r = owner.put("/owner/people/00000000-0000-0000-0000-000000000001/overrides", content=chunks(),
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 411 and r.json()["error"] == "length_required"
    # broken JSON with the right key: the usual validation path, as {error: invalid}
    _invalid(owner.put("/owner/people/00000000-0000-0000-0000-000000000001/overrides", content=b"{not json",
                       headers={"Content-Type": "application/json"}))


def test_limiter_holds_a_bounded_number_of_keys():
    from app.security import LoginLimiter
    lim = LoginLimiter(limit=3, window=600, max_keys=100)
    for i in range(1000):
        lim.fail(f"10.0.{i // 256}.{i % 256}")
    assert len(lim.failures) <= 100
    for _ in range(3):
        lim.fail("1.2.3.4")
    assert lim.blocked("1.2.3.4") and not lim.blocked("9.9.9.9")
    assert "9.9.9.9" not in lim.failures  # checking a key does not store it


def test_owner_disable_signs_out_everywhere_and_is_audited(owner, make_user, login):
    uid = make_user("desk2", "assignment_desk")
    a, b = login("desk2"), login("desk2")
    assert _sessions(uid) == 2
    r = owner.patch(f"/owner/people/{uid}", json={"active": False})
    assert r.status_code == 200 and r.json()["user"]["active"] is False
    assert _sessions(uid) == 0
    assert a.get("/me").status_code == 401 and b.get("/me").status_code == 401

    from app.db import session_scope
    from app.models import Audit
    with session_scope() as db:
        row = db.scalar(select(Audit).where(Audit.target_id == str(uid)))
        assert row.actor_id is None and row.actor == "owner:vansh" and row.action == "user.edit"
        assert row.before["active"] is True and row.after["active"] is False


def test_owner_role_change_keeps_the_session(owner, make_user, login):
    uid = make_user("chief", "chief_editor")
    chief = login("chief")
    assert chief.get("/desk/stories").status_code == 403
    r = owner.patch(f"/owner/people/{uid}", json={"role": "admin"})
    assert r.json()["user"]["role"] == "admin" and r.json()["user"]["roleLabel"] == "Admin"
    assert chief.get("/desk/stories").status_code == 200


def test_owner_cannot_make_an_unlinked_reporter(owner, make_user, make_reporter):
    uid = make_user("desk2", "assignment_desk")
    r = owner.patch(f"/owner/people/{uid}", json={"role": "reporter"})
    assert r.status_code == 422 and r.json()["error"] == "reporter_link_required"
    make_reporter(user_id=uid)
    assert owner.patch(f"/owner/people/{uid}", json={"role": "reporter"}).status_code == 200


def test_last_super_admin_is_protected(owner, make_user):
    boss = make_user("boss", "super_admin")
    for body in ({"active": False}, {"role": "admin"}):
        r = owner.patch(f"/owner/people/{boss}", json=body)
        assert r.status_code == 409 and r.json()["error"] == "last_admin", body
    for key in ("users.manage", "page.access", "desk.view"):
        r = owner.put(f"/owner/people/{boss}/overrides", json={"overrides": [{"permission": key, "allowed": False}]})
        assert r.status_code == 409 and r.json()["error"] == "last_admin", key
    # other overrides are fine
    _set(owner, boss, feed__manage=False)

    # a second super admin who cannot manage users (or open /access, or see the desk) does not count
    boss2 = make_user("boss2", "super_admin")
    for key in ("users__manage", "page__access", "desk__view"):
        _set(owner, boss2, **{key: False})
        assert owner.patch(f"/owner/people/{boss}", json={"active": False}).status_code == 409, key
    # nor does a disabled one
    _set(owner, boss2)
    assert owner.patch(f"/owner/people/{boss2}", json={"active": False}).status_code == 200
    assert owner.patch(f"/owner/people/{boss}", json={"active": False}).status_code == 409
    # with a working second super admin, the first can go
    assert owner.patch(f"/owner/people/{boss2}", json={"active": True}).status_code == 200
    assert owner.patch(f"/owner/people/{boss}", json={"role": "admin"}).status_code == 200
    # admins are not protected: only super admins count
    lead = make_user("lead2", "admin")
    assert owner.patch(f"/owner/people/{lead}", json={"active": False}).status_code == 200


def test_history_newest_first_with_actors_and_paging(owner, make_user, admin):
    uid = make_user("desk2", "assignment_desk")
    assert admin.patch(f"/admin/users/{uid}", json={"role": "chief_editor"}).status_code == 200
    _set(owner, uid, page__inbox=False)
    owner.patch(f"/owner/people/{uid}", json={"active": False})
    entries = owner.get("/owner/history").json()["entries"]
    assert [(e["actor"], e["actorKind"], e["action"]) for e in entries] == [
        ("owner:vansh", "owner", "user.edit"), ("owner:vansh", "owner", "user.permissions"),
        ("lead", "user", "user.edit")]
    assert all(e["target"] == {"id": str(uid), "name": "Desk2"} for e in entries)
    assert entries[1]["before"]["overrides"] == [] and entries[1]["after"]["overrides"] == [
        {"permission": "page.inbox", "allowed": False}]
    # keyset paging: one entry a page, following next until it is null
    seen, cursor = [], None
    while True:
        body = owner.get("/owner/history", params={"limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        seen += [e["action"] for e in body["entries"]]
        cursor = body["next"]
        if cursor is None:
            break
        assert all(c.isalnum() or c in "-_" for c in cursor)  # URL-safe as is
    assert seen == [e["action"] for e in entries]
    # before=<iso with offset> still works as a time filter; a naive time is refused
    page = owner.get("/owner/history", params={"before": entries[0]["at"]}).json()["entries"]
    assert [e["action"] for e in page] == ["user.permissions", "user.edit"]
    _invalid(owner.get("/owner/history", params={"before": "2026-10-30T10:00:00"}))
    _invalid(owner.get("/owner/history", params={"cursor": "not-a-cursor"}))
    _invalid(owner.get("/owner/history", params={"limit": 0}))


def test_history_cursor_does_not_skip_entries_with_the_same_time(owner, make_user):
    from app.db import session_scope
    from app.models import Audit
    uid = make_user("desk2", "assignment_desk")
    with session_scope() as db:  # one transaction: all three share created_at = now()
        for n in range(3):
            db.add(Audit(actor_id=None, actor="owner:vansh", action=f"user.edit{n}", target_type="user",
                         target_id=str(uid), before={}, after={"name": "Desk2"}))
    first = owner.get("/owner/history", params={"limit": 2}).json()
    rest = owner.get("/owner/history", params={"limit": 2, "cursor": first["next"]}).json()
    assert [e["action"] for e in first["entries"] + rest["entries"]] == ["user.edit2", "user.edit1", "user.edit0"]
    assert rest["next"] is None


# --- migration 0006 ---------------------------------------------------------------------------------

def test_migration_0006_down_and_up_keeps_users(stack, make_user, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from app import db as appdb
    from app.db import session_scope
    from app.models import User, UserPermissionOverride

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    uid = make_user("desk2", "assignment_desk")
    with session_scope() as db:
        db.add(UserPermissionOverride(user_id=uid, permission="page.feed", allowed=True))
    appdb.engine.dispose()  # no pooled connection may hold a lock on the tables being altered
    monkeypatch.delenv("ALLOW_OVERRIDE_DATA_LOSS", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to delete 1 permission override"):
        command.downgrade(cfg, "0005")
    appdb.engine.dispose()
    insp = inspect(appdb.engine)
    assert "user_permission_overrides" in insp.get_table_names()  # refused: nothing dropped
    monkeypatch.setenv("ALLOW_OVERRIDE_DATA_LOSS", "1")
    try:
        command.downgrade(cfg, "0005")
        insp = inspect(appdb.engine)
        assert "user_permission_overrides" not in insp.get_table_names()
        assert "actor" not in {c["name"] for c in insp.get_columns("audit")}
    finally:
        appdb.engine.dispose()
        command.upgrade(cfg, "head")
        appdb.engine.dispose()
    insp = inspect(appdb.engine)
    assert "user_permission_overrides" in insp.get_table_names()
    assert "actor" in {c["name"] for c in insp.get_columns("audit")}
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(UserPermissionOverride)) == 0  # dropped with the table
        assert db.get(User, uid) is not None


# --- page.inbox guards the story text everywhere (review fix 1) --------------------------------------

def test_full_list_and_body_search_need_the_inbox(owner, make_user, login, filed_item):
    filed_item()
    uid = make_user("chief", "chief_editor")
    chief = login("chief")
    assert "body" in chief.get("/desk/items", params={"full": "true"}).json()["items"][0]
    assert len(chief.get("/desk/items", params={"q": "पर्यटकों"}).json()["items"]) == 1  # a word only in the body
    _set(owner, uid, page__inbox=False)
    r = chief.get("/desk/items", params={"full": "true"})
    assert r.status_code == 403
    listed = chief.get("/desk/items").json()["items"]
    assert len(listed) == 1 and "body" not in listed[0] and "messages" not in listed[0]
    assert chief.get("/desk/items", params={"q": "पर्यटकों"}).json()["items"] == []  # body not searched
    assert len(chief.get("/desk/items", params={"q": "उदयपुर"}).json()["items"]) == 1  # headline still is


def test_write_routes_return_the_full_item_only_with_the_inbox(owner, make_user, login, filed_item):
    item_id = filed_item()
    uid = make_user("desk2", "assignment_desk")
    desk = login("desk2")
    r = desk.post(f"/desk/items/{item_id}/slug/reset")
    assert r.status_code == 200, r.text
    assert "body" in r.json()["item"]
    _set(owner, uid, page__inbox=False)
    r = desk.post(f"/desk/items/{item_id}/slug/reset")
    assert r.status_code == 200, r.text
    assert {"body", "media", "messages"}.isdisjoint(r.json()["item"])


# --- an open live stream ends when access does (review fix 2) ------------------------------------

def _listen(client, params, out):
    out["response"] = client.get("/events", params=params)


@pytest.mark.parametrize("change", ["disable", "deny_page"])
def test_open_desk_stream_closes_when_access_ends(monkeypatch, owner, make_user, login, change):
    from app.api import events
    monkeypatch.setattr(events, "RECHECK_SECONDS", 0.2)
    monkeypatch.setattr(events, "PING_SECONDS", 0.2)
    uid = make_user("desk2", "assignment_desk")
    desk = login("desk2")
    out = {}
    listener = threading.Thread(target=_listen, args=(desk, {"app": "desk"}, out), daemon=True)
    listener.start()
    time.sleep(1.0)
    assert listener.is_alive(), "the stream stays open while access holds"
    if change == "disable":
        assert owner.patch(f"/owner/people/{uid}", json={"active": False}).status_code == 200
    else:
        _set(owner, uid, page__dashboard=False, page__inbox=False, page__reporters=False)
    listener.join(timeout=10)
    assert not listener.is_alive(), "the stream did not close"
    assert out["response"].status_code == 200 and "retry: 3000" in out["response"].text


# --- concurrent admin changes (review fix 3) -------------------------------------------------------

def test_two_owners_cannot_remove_both_last_super_admins_at_once(monkeypatch, owner, make_user):
    """Each request checks 'is someone else still admin?' and then saves. A pause between the check and the save
    makes the race certain without the lock; with it the second request waits, then sees the first and gets 409."""
    from app.api import owner as owner_api
    real = owner_api.leaves_an_admin

    def slow(*args, **kwargs):
        ok = real(*args, **kwargs)
        time.sleep(0.5)
        return ok
    monkeypatch.setattr(owner_api, "leaves_an_admin", slow)
    a, b = make_user("boss", "super_admin"), make_user("boss2", "super_admin")
    gate = threading.Barrier(2)
    statuses = []

    def disable(uid):
        client = TestClient(app)
        client.headers.update(owner.headers)
        gate.wait()
        statuses.append(client.patch(f"/owner/people/{uid}", json={"active": False}).status_code)
    threads = [threading.Thread(target=disable, args=(u,)) for u in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(statuses) == [200, 409]


# --- admin route: coverage and password reset (review fix 6) ------------------------------------

def test_admin_cannot_change_someone_with_access_they_lack(owner, make_user, login):
    lead = make_user("lead2", "admin")
    target = make_user("desk2", "assignment_desk")
    client = login("lead2")
    _set(owner, lead, feed__manage=False)
    assert client.patch(f"/admin/users/{target}", json={"active": False}).status_code == 200
    assert client.patch(f"/admin/users/{target}", json={"active": True}).status_code == 200
    _set(owner, target, feed__manage=True)  # now the target can do something the admin cannot
    r = client.patch(f"/admin/users/{target}", json={"active": False})
    assert r.status_code == 403 and "feed.manage" in r.json()["detail"]
    # nor may the admin give a role whose access they lack
    other = make_user("chief", "chief_editor")
    _set(owner, lead, page__ftp=False)
    r = client.patch(f"/admin/users/{other}", json={"role": "admin"})
    assert r.status_code == 403  # admins cannot give admin anyway; super admins below:
    boss = make_user("boss", "super_admin")
    make_user("boss2", "super_admin")
    boss_client = login("boss")
    _set(owner, boss, page__ftp=False)
    r = boss_client.patch(f"/admin/users/{other}", json={"role": "admin"})
    assert r.status_code == 403 and "page.ftp" in r.json()["detail"]
    # a reporter login has report.submit, which no desk role has: that alone does not block
    rep = make_user("sunita", "reporter")
    assert client.patch(f"/admin/users/{rep}", json={"active": False}).status_code == 200


def test_admin_password_reset_ends_sessions(make_user, login, admin):
    uid = make_user("desk2", "assignment_desk")
    old = login("desk2")
    assert admin.patch(f"/admin/users/{uid}", json={"password": "a-new-password"}).status_code == 200
    assert _sessions(uid) == 0 and old.get("/me").status_code == 401


def test_changing_your_own_password_keeps_your_session(make_user, login):
    boss = make_user("boss", "super_admin")
    here, elsewhere = login("boss"), login("boss")
    assert here.patch(f"/admin/users/{boss}", json={"password": "a-new-password"}).status_code == 200
    assert here.get("/me").status_code == 200
    assert elsewhere.get("/me").status_code == 401


def test_history_actor_kind_is_not_fooled_by_a_username(owner, make_user, login):
    """A desk login named like an owner is still a user: actorKind comes from the audit row, not the name."""
    from app.db import session_scope
    from app.models import Audit
    uid = make_user("desk2", "assignment_desk")
    lookalike = make_user("owner.vansh", "admin")
    assert login("owner.vansh").patch(f"/admin/users/{uid}", json={"active": False}).status_code == 200
    with session_scope() as db:  # an actor that is neither: no owner name, no user id
        db.add(Audit(actor_id=None, action="user.edit", target_type="user", target_id=str(uid), before={}, after={}))
    entries = owner.get("/owner/history").json()["entries"]
    assert [(e["actor"], e["actorKind"]) for e in entries] == [(None, None), ("owner.vansh", "user")]
    assert lookalike
