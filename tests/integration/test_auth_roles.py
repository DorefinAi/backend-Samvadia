"""Login, sessions, CSRF and who may do what."""
from fastapi.testclient import TestClient

from app.main import app


def test_health(stack):
    assert TestClient(app).get("/health").json() == {"db": "ok"}


def test_wrong_password_is_401(make_user):
    make_user("desk", "assignment_desk")
    r = TestClient(app).post("/auth/login", json={"username": "desk", "password": "nope-nope"})
    assert r.status_code == 401


def test_username_is_case_insensitive(make_user, login):
    make_user("desk", "assignment_desk")
    login(" DESK ")


def test_login_locks_after_ten_failures(make_user):
    make_user("desk", "assignment_desk")
    client = TestClient(app)
    for _ in range(10):
        assert client.post("/auth/login", json={"username": "desk", "password": "wrong-pass"}).status_code == 401
    r = client.post("/auth/login", json={"username": "desk", "password": "test-pass-123"})
    assert r.status_code == 429, "the right password is still refused while locked out"


def test_disabled_user_cannot_log_in(make_user):
    make_user("gone", "assignment_desk", active=False)
    r = TestClient(app).post("/auth/login", json={"username": "gone", "password": "test-pass-123"})
    assert r.status_code == 401


def test_disabling_a_user_ends_their_open_session(make_user, login, admin):
    user_id = make_user("desk2", "assignment_desk")
    desk = login("desk2")
    assert desk.get("/desk/items").status_code == 200
    assert admin.patch(f"/admin/users/{user_id}", json={"active": False}).status_code == 200
    assert desk.get("/desk/items").status_code == 401


def test_me_returns_permissions_and_csrf(desk):
    me = desk.get("/me").json()
    assert me["user"]["role"] == "assignment_desk"
    assert "desk.review" in me["permissions"] and "feed.manage" not in me["permissions"]
    assert me["csrf_token"]


def test_no_cookie_is_401(stack):
    assert TestClient(app).get("/desk/items").status_code == 401


def test_post_without_csrf_is_403(desk):
    del desk.headers["X-CSRF"]
    assert desk.post("/desk/refresh").status_code == 403


def test_logout_ends_the_session(desk):
    assert desk.post("/auth/logout").status_code == 200
    assert desk.get("/me").status_code == 401


def test_role_matrix(make_user, login, make_reporter):
    make_user("chief", "chief_editor")
    make_user("desk", "assignment_desk")
    make_user("lead", "admin")
    rep = make_user("sunita", "reporter")
    make_reporter(user_id=rep)
    chief, desk, lead, reporter = login("chief"), login("desk"), login("lead"), login("sunita")

    assert chief.get("/desk/items").status_code == 200
    assert chief.post("/desk/refresh").status_code == 403          # read only
    assert desk.get("/desk/stories").status_code == 403            # feed is admin-only
    assert desk.get("/desk/feed-log").status_code == 403
    assert lead.get("/desk/stories").status_code == 200
    assert reporter.get("/desk/items").status_code == 403
    assert desk.get("/reporter/items").status_code == 403
    assert desk.get("/admin/users").status_code == 403
    assert lead.get("/admin/users").status_code == 200


def test_admin_cannot_create_or_edit_equal_or_higher_roles(admin, make_user):
    for role in ("super_admin", "admin"):
        r = admin.post("/admin/users", json={"name": "X", "username": f"x{role}", "password": "long-enough", "role": role})
        assert r.status_code == 403, role
    boss = make_user("boss", "super_admin")
    assert admin.patch(f"/admin/users/{boss}", json={"active": False}).status_code == 403


def test_reporter_login_needs_a_free_reporter(admin, make_reporter, make_user):
    r = admin.post("/admin/users", json={"name": "R", "username": "rep1", "password": "long-enough", "role": "reporter"})
    assert r.status_code == 422
    rid = make_reporter()
    ok = admin.post("/admin/users", json={"name": "R", "username": "rep1", "password": "long-enough", "role": "reporter",
                                          "reporter_id": str(rid)})
    assert ok.status_code == 200, ok.text
    again = admin.post("/admin/users", json={"name": "R2", "username": "rep2", "password": "long-enough", "role": "reporter",
                                             "reporter_id": str(rid)})
    assert again.status_code == 409


def test_duplicate_username_is_409(admin):
    body = {"name": "D", "username": "Dup.User", "password": "long-enough", "role": "assignment_desk"}
    assert admin.post("/admin/users", json=body).status_code == 200
    assert admin.post("/admin/users", json={**body, "username": "dup.user"}).status_code == 409
