"""End-to-end smoke test against a real Postgres and a real FTP server, both started locally.

    pip install pgserver pyftpdlib
    python tests/smoke/run_smoke.py

Supabase Storage, Meta and Microsoft Graph are replaced by local fakes; everything else is the
real code: migrations, API, job queue, slug builder, FTP upload with size check.
"""
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
WORK = Path(tempfile.mkdtemp(prefix="vai-smoke-"))
FTP_ROOT, STORE = WORK / "ftp", WORK / "storage"
FTP_ROOT.mkdir(), STORE.mkdir()

import pgserver  # noqa: E402

pg = pgserver.get_server(str(WORK / "pg"), cleanup_mode="stop")
os.environ.update(
    DATABASE_URL=pg.get_uri(), COOKIE_SECURE="false", FTP_PROTOCOL="ftp", FTP_HOST="127.0.0.1", FTP_PORT="2121",
    FTP_USER="vai", FTP_PASSWORD="vai-pass", FTP_BASE_DIR="/vartmaan/incoming", WHATSAPP_MERGE_SECONDS="0",
    WHATSAPP_APP_SECRET="app-secret", WHATSAPP_TOKEN="wa-token", WHATSAPP_VERIFY_TOKEN="verify-me",
    MS_TENANT_ID="t", MS_CLIENT_ID="c", MS_CLIENT_SECRET="s", MS_MAILBOX="desk@example.com",
    SUPABASE_URL="http://fake-storage", SUPABASE_SERVICE_KEY="k",
    FEED_SECRET="feed-secret", OCTOPUS_ALLOWED_IPS="10.9.8.7", FEED_LINK_BASE="https://dorefinai.com/wire",
)

# --- local FTP server ---
from pyftpdlib.authorizers import DummyAuthorizer  # noqa: E402
from pyftpdlib.handlers import FTPHandler  # noqa: E402
from pyftpdlib.servers import FTPServer  # noqa: E402

auth = DummyAuthorizer()
auth.add_user("vai", "vai-pass", str(FTP_ROOT), perm="elradfmwMT")
FTPHandler.authorizer = auth
FTPHandler.encoding = "utf-8"
ftpd = FTPServer(("127.0.0.1", 2121), FTPHandler)
threading.Thread(target=ftpd.serve_forever, daemon=True).start()

# --- migrations ---
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

os.chdir(ROOT)
command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

# --- fakes for Supabase Storage, Meta and Graph ---
from app.services import mail, storage, whatsapp  # noqa: E402


def fake_upload_url(key):
    return f"fake://{key}"


def fake_list_sizes(prefix):
    folder = STORE / prefix
    return {p.name: p.stat().st_size for p in folder.iterdir()} if folder.exists() else {}


def fake_upload_chunks(key, chunks, content_type, tmp_path):
    data = b"".join(chunks)
    (STORE / key).parent.mkdir(parents=True, exist_ok=True)
    (STORE / key).write_bytes(data)
    return storage.LocalCopy(tmp_path, len(data), hashlib.sha256(data).hexdigest())


def fake_download(key, tmp_path):
    data = (STORE / key).read_bytes()
    Path(tmp_path).write_bytes(data)
    return storage.LocalCopy(tmp_path, len(data), hashlib.sha256(data).hexdigest())


def fake_delete(keys):
    for k in keys:
        (STORE / k).unlink(missing_ok=True)


storage.signed_upload_url, storage.list_sizes = fake_upload_url, fake_list_sizes
storage.upload_chunks, storage.download, storage.delete = fake_upload_chunks, fake_download, fake_delete
whatsapp.media_info = lambda media_id: {"url": f"https://meta/{media_id}", "mime_type": "image/jpeg"}
whatsapp.stream_media = lambda url: iter([b"JPEGDATA-" + url.encode()])
mail.inbox_delta = lambda link: ([{
    "id": "MAIL1", "subject": "प्रेस नोट: अजमेर में रोजगार मेला", "conversationId": "CONV1", "hasAttachments": True,
    "receivedDateTime": "2026-10-17T06:00:00Z",
    "from": {"emailAddress": {"name": "PRO Ajmer", "address": "pro.ajmer@example.com"}},
    "body": {"content": "अजमेर में 20 अक्टूबर को रोजगार मेला लगेगा।\n\nSent from my iPhone\nFrom: old"},
}], "DELTA2")
mail.list_attachments = lambda mid: [
    {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "press note.pdf",
     "contentType": "application/pdf", "size": 7, "isInline": False},
    {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A2", "name": "logo.png",
     "contentType": "image/png", "size": 3, "isInline": True},
]
mail.stream_attachment = lambda mid, aid: iter([b"PDFDATA"])

# --- the app ---
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import jobs  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Item, Job, Media, Reporter, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.config import get_settings  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)
        with session_scope() as db:  # show the queue so a failure explains itself
            for j in db.scalars(select(Job).order_by(Job.id)):
                print(f"     job {j.id} {j.type} {j.status} attempts={j.attempts} {j.payload} {(j.last_error or '')[:160]}")


def drain(max_rounds=200):
    """Run due jobs until the queue is empty. When nothing is due, jump the clock to the next job
    (only that one), the way waiting would in production."""
    for _ in range(max_rounds):
        if jobs.run_one():
            continue
        with session_scope() as db:
            nxt = db.scalar(select(Job).where(Job.status == "pending").order_by(Job.run_after, Job.id).limit(1))
            if nxt is None:
                return
            nxt.run_after = jobs.now()


def login(client, username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF"] = r.json()["csrf_token"]
    return r.json()


org = get_settings().org_id
with session_scope() as db:
    desk_user = User(org_id=org, name="Desk", username="desk", password_hash=hash_password("desk-pass-1"), role="assignment_desk")
    lead = User(org_id=org, name="Lead", username="lead", password_hash=hash_password("lead-pass-1"), role="admin")
    rep_user = User(org_id=org, name="Sunita", username="sunita", password_hash=hash_password("sunita-pass"), role="reporter")
    db.add_all([desk_user, lead, rep_user])
    db.flush()
    db.add(Reporter(org_id=org, name="Sunita Sharma", slug_code="SUNITA", city_code="UDR", district="Udaipur",
                    phone="919876543210", user_id=rep_user.id))

today = datetime.now(timezone.utc).astimezone(timezone.utc)

# 1. Reporter portal: two files with the same name, Hindi headline, English subject
reporter = TestClient(app)
login(reporter, "sunita", "sunita-pass")
r = reporter.post("/reporter/items", json={
    "headline": "उदयपुर में पर्यटन सीजन शुरू", "body": "झीलों के शहर में पर्यटकों की भीड़।", "subject": "Paryatan season",
    "story_type": "AV", "tags": ["Breaking News", "Tourism", "breaking news"], "files": [{"name": "IMG_0001.jpg", "size": 5, "content_type": "image/jpeg"},
                                  {"name": "IMG_0001.jpg", "size": 6, "content_type": "image/jpeg"}],
})
check("portal: create report", r.status_code == 200, r.text)
item_id = r.json()["item"]["id"]
r2 = reporter.post(f"/reporter/items/{item_id}/submit")
check("portal: submit refused until files are uploaded", r2.status_code == 409, r2.text)
for i, up in enumerate(r.json()["uploads"]):
    key = up["upload_url"].removeprefix("fake://")
    (STORE / key).parent.mkdir(parents=True, exist_ok=True)
    (STORE / key).write_bytes(b"x" * (5 + i))
r2 = reporter.post(f"/reporter/items/{item_id}/submit")
check("portal: submit", r2.status_code == 200, r2.text)
drain()
with session_scope() as db:
    item = db.get(Item, item_id)
    slug = item.slug
    check("portal: slug built", slug == datetime.now(timezone.utc).astimezone(
        __import__("app.services.slug", fromlist=["IST"]).IST).strftime("%d%m%y") + "_UDR_PARYATAN_SEASON_AV_SUNITA", slug)
    check("portal: item on FTP", item.intake_state == "on_ftp", item.intake_state + " " + str(item.intake_note))
    names = sorted(p.name for p in (FTP_ROOT / "vartmaan/incoming" / slug).iterdir())
    check("portal: FTP folder has original names, second renamed", names == ["IMG_0001.jpg", "IMG_0001_2.jpg"], str(names))
    check("portal: Storage emptied", not any(STORE.rglob("*.*")) and not list((STORE / "items" / item_id).glob("*")),
          str(list(STORE.rglob("*"))))
check("portal: tags kept, Breaking tag sets priority",
      r2.json()["item"]["tags"] == ["Breaking News", "Tourism"] and r2.json()["item"]["priority"] == "breaking",
      str(r2.json()["item"].get("tags")))
mine = reporter.get("/reporter/items").json()["items"]
check("portal: reporter sees own report as reviewing", mine and mine[0]["status"] == "reviewing", json.dumps(mine)[:200])

# 2. WhatsApp from an unknown number: text + photo merge into one item, parked for the desk
payload = {"entry": [{"changes": [{"value": {
    "contacts": [{"wa_id": "919999000111", "profile": {"name": "New Stringer"}}],
    "messages": [
        {"id": "wamid.A", "from": "919999000111", "timestamp": str(int(time.time())), "type": "text",
         "text": {"body": "जयपुर में भारी बारिश, कई इलाकों में जलभराव"}},
        {"id": "wamid.B", "from": "919999000111", "timestamp": str(int(time.time())), "type": "image",
         "image": {"id": "MEDIA-1", "mime_type": "image/jpeg"}},
    ]}}]}]}
raw = json.dumps(payload).encode()
sig = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
hook = TestClient(app)
check("whatsapp: bad signature refused", hook.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": "sha256=0"}).status_code == 401)
r = hook.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
check("whatsapp: webhook accepted", r.status_code == 200, r.text)
hook.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
check("whatsapp: verify challenge", hook.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}).text == "42")
drain()
with session_scope() as db:
    wa = db.scalars(select(Item).where(Item.source == "whatsapp")).all()
    check("whatsapp: one item for text + photo, duplicate delivery ignored", len(wa) == 1, str(len(wa)))
    wa_item = wa[0]
    check("whatsapp: unknown sender parked for desk", wa_item.intake_state == "needs_desk", wa_item.intake_state)

# 3. Desk picks the reporter -> slug -> FTP
desk = TestClient(app)
login(desk, "desk", "desk-pass-1")
inbox = desk.get("/desk/items").json()["items"]
check("desk: inbox lists both items", len(inbox) == 2, str(len(inbox)))
check("desk: PATCH without CSRF refused", TestClient(app, cookies=desk.cookies).patch(f"/desk/items/{wa_item.id}", json={"version": 1}).status_code == 403)
sunita_id = desk.get("/desk/reporters", params={"q": "sunita"}).json()["reporters"][0]["id"]
r = desk.patch(f"/desk/items/{wa_item.id}", json={"version": wa_item.version, "reporter_id": sunita_id})
check("desk: assign reporter", r.status_code == 200, r.text)
stale = desk.patch(f"/desk/items/{wa_item.id}", json={"version": wa_item.version, "headline": "x"})
check("desk: stale version gets 409", stale.status_code == 409, stale.text)
drain()
with session_scope() as db:
    wa_item = db.get(Item, wa_item.id)
    check("whatsapp: slug from Hindi text", wa_item.slug and wa_item.slug.endswith("_UDR_JAYPUR_BHARI_BARISH_AV_SUNITA"), str(wa_item.slug))
    check("whatsapp: on FTP", wa_item.intake_state == "on_ftp", wa_item.intake_state)
    files = [p.name for p in (FTP_ROOT / "vartmaan/incoming" / wa_item.slug).iterdir()]
    check("whatsapp: photo saved with a time-based name", len(files) == 1 and files[0].endswith("_01.jpg"), str(files))

# 3b. Desk moves the story to Jaipur: slug and FTP folder follow; a hand-typed slug sticks until reset
wa = desk.get(f"/desk/items/{wa_item.id}").json()["item"]
r = desk.patch(f"/desk/items/{wa_item.id}", json={"version": wa["version"], "city_code": "jpr", "category": "Weather", "priority": "high"})
check("desk: location override rebuilds slug", r.status_code == 200 and "_JPR_JAYPUR_" in r.json()["item"]["slug"], r.text[:300])
check("desk: unknown location refused", desk.patch(f"/desk/items/{wa_item.id}", json={"version": r.json()["item"]["version"], "city_code": "XYZ"}).status_code == 422)
check("desk: unknown category refused", desk.patch(f"/desk/items/{wa_item.id}", json={"version": r.json()["item"]["version"], "category": "Gossip"}).status_code == 422)
drain()
moved = r.json()["item"]["slug"]
check("desk: FTP folder renamed to new slug", (FTP_ROOT / "vartmaan/incoming" / moved).is_dir()
      and not (FTP_ROOT / "vartmaan/incoming" / wa_item.slug).exists(), moved)
v = r.json()["item"]["version"]
hand = moved.rsplit("_", 3)[0] + "_RAIN_AV_SUNITA"
r = desk.patch(f"/desk/items/{wa_item.id}", json={"version": v, "slug": hand})
check("desk: hand-typed slug accepted", r.status_code == 200 and r.json()["item"]["slug_edited"], r.text[:200])
r = desk.patch(f"/desk/items/{wa_item.id}", json={"version": r.json()["item"]["version"], "story_type": "PKG"})
check("desk: hand-typed slug not overwritten", r.json()["item"]["slug"] == hand, r.json()["item"]["slug"])
r = desk.post(f"/desk/items/{wa_item.id}/slug/reset")
check("desk: slug reset rebuilds", r.status_code == 200 and r.json()["item"]["slug"].endswith("_PKG_SUNITA")
      and not r.json()["item"]["slug_edited"], r.text[:200])
drain()
meta = desk.get("/desk/locations").json()
check("desk: locations and pick-lists", len(meta["locations"]) == 14 and "Weather" in meta["categories"], str(meta)[:200])

# 4. Email: subject + cleaned body to DB, inline logo skipped, PDF to FTP; sender unknown -> desk
with session_scope() as db:
    jobs.enqueue(db, "fetch_mail", {})
drain()
with session_scope() as db:
    em = db.scalars(select(Item).where(Item.source == "outlook")).one()
    check("email: headline from subject", em.headline.startswith("प्रेस नोट"), em.headline)
    check("email: signature and quoted mail removed", em.body == "अजमेर में 20 अक्टूबर को रोजगार मेला लगेगा।", repr(em.body))
    em_media = db.scalars(select(Media).where(Media.item_id == em.id)).all()
    check("email: inline logo skipped", [m.original_name for m in em_media] == ["press note.pdf"], str([m.original_name for m in em_media]))
    check("email: unknown sender parked for desk", em.intake_state == "needs_desk", em.intake_state)

runs = desk.get("/desk/fetch-runs").json()["runs"]
check("desk: fetch history for WhatsApp and Outlook", {r["source"] for r in runs} == {"whatsapp", "outlook"}
      and all(r["added"] == 1 for r in runs), str(runs))

# 5. Decisions
r = desk.post(f"/desk/items/{item_id}/reject", json={"reason": "bad"})
check("desk: unknown reject reason refused", r.status_code == 422)
r = desk.post(f"/desk/items/{item_id}/reject", json={"reason": "duplicate", "note": "same as ol-202"})
check("desk: reject", r.status_code == 200 and r.json()["item"]["review_state"] == "rejected", r.text)
check("desk: reopen", desk.post(f"/desk/items/{item_id}/reopen").json()["item"]["review_state"] == "pending")
approved = desk.post(f"/desk/items/{item_id}/approve").json()
check("desk: approve", approved["item"]["review_state"] == "approved")
story1 = approved.get("story") or {}
check("feed: approve creates a ready story (files already on FTP)", story1.get("state") == "ready", str(story1)[:200])
check("feed: breaking story titled FLASH, GUID v1", story1.get("title") == "FLASH " + slug
      and story1.get("guid") == f"vartmaan-{slug}-v1", str(story1)[:200])
check("feed: story text has city line and reporter line",
      story1.get("description", "").startswith("उदयपुर:\n\n") and "संवाददाता: Sunita Sharma, उदयपुर ब्यूरो" in story1.get("description", ""),
      repr(story1.get("description")))
check("desk: approve without slug refused", desk.post(f"/desk/items/{em.id}/approve").status_code == 409)

# 5b. Octopus feed: two locks, CDATA, ETag, delivery log
octo = TestClient(app)
OK_IP = {"X-Forwarded-For": "203.0.113.5, 10.9.8.7"}
check("feed: wrong secret is 404", octo.get("/feed/nope/feed.xml", headers=OK_IP).status_code == 404)
check("feed: unknown IP is 404", octo.get("/feed/feed-secret/feed.xml").status_code == 404)
check("feed: forged first X-Forwarded-For is 404",
      octo.get("/feed/feed-secret/feed.xml", headers={"X-Forwarded-For": "10.9.8.7, 198.51.100.9"}).status_code == 404)
r = octo.get("/feed/feed-secret/feed.xml", headers=OK_IP)
check("feed: allowed IP gets RSS", r.status_code == 200 and r.headers["content-type"].startswith("application/rss+xml"), str(r.status_code))
check("feed: title in CDATA, GUID present", f"<![CDATA[FLASH {slug}]]>" in r.text and f"vartmaan-{slug}-v1" in r.text, r.text[:400])
etag = r.headers.get("etag")
r304 = octo.get("/feed/feed-secret/feed.xml", headers={**OK_IP, "If-None-Match": etag})
check("feed: unchanged feed answers 304", r304.status_code == 304, str(r304.status_code))

check("feed: desk role cannot send an update", desk.post(f"/desk/items/{item_id}/publish-update").status_code == 403)
upd = lead_c_pre = TestClient(app)
login(upd, "lead", "lead-pass-1")
r = upd.post(f"/desk/items/{item_id}/publish-update")
story2 = r.json().get("story", {})
check("feed: update is a new story v2 titled UPDATE", r.status_code == 200 and story2.get("title") == "UPDATE " + slug
      and story2.get("guid") == f"vartmaan-{slug}-v2", r.text[:300])
check("feed: update starts with the (( )) cancel line", story2.get("description", "").startswith(f"((पिछला संस्करण रद्द करें: {slug}))"))
r = octo.get("/feed/feed-secret/feed.xml", headers={**OK_IP, "If-None-Match": etag})
check("feed: changed feed answers 200 with both versions", r.status_code == 200 and r.text.count("<item>") == 2, str(r.status_code))
stories = desk.get("/desk/stories", params={"item_id": item_id}).json()["stories"]
v1 = next(s_ for s_ in stories if s_["version"] == 1)
check("feed: story shows when Octopus collected it", v1["collected_at"] is not None, str(v1))
log = upd.get("/desk/feed-log").json()
check("feed: every request logged", sorted({r_["status"] for r_ in log["requests"]}) == [200, 304, 404]
      and log["last_collected_at"], str(log)[:300])

# 5c. A story approved before its files reach FTP waits, then enters the feed
em_now = desk.get(f"/desk/items/{em.id}").json()["item"]
r = desk.patch(f"/desk/items/{em.id}", json={"version": em_now["version"], "reporter_id": sunita_id})
check("email: desk assigns reporter, slug built", r.status_code == 200 and r.json()["item"]["slug"], r.text[:200])
r = desk.post(f"/desk/items/{em.id}/approve")
check("feed: approved before upload -> story waits", r.status_code == 200 and r.json()["story"]["state"] == "waiting", r.text[:300])
drain()
waited = desk.get("/desk/stories", params={"item_id": str(em.id)}).json()["stories"][0]
check("feed: story released once files are on FTP", waited["state"] == "ready" and waited["published_at"], str(waited)[:200])

# 5d. Blind flag and named queues
items_all = desk.get("/desk/items").json()["items"]
check("blind: every item carries the flag", all(isinstance(i["blind"], bool) for i in items_all), str(items_all[:1])[:200])
with session_scope() as db:
    jobs.enqueue(db, "purge_storage", {})
check("queues: default worker skips media jobs", jobs.run_one(["default"]) is False)
check("queues: media worker takes it", jobs.run_one(["media"]) is True)

# 6. Roles
chief = TestClient(app)
with session_scope() as db:
    db.add(User(org_id=org, name="Chief", username="chief", password_hash=hash_password("chief-pass-1"), role="chief_editor"))
login(chief, "chief", "chief-pass-1")
check("roles: chief editor can read", chief.get("/desk/items").status_code == 200)
check("roles: chief editor cannot approve", chief.post(f"/desk/items/{em.id}/approve").status_code == 403)
check("roles: reporter cannot open the desk", reporter.get("/desk/items").status_code == 403)
check("roles: desk cannot create users", desk.post("/admin/users", json={"name": "x", "username": "xyz", "password": "12345678", "role": "admin"}).status_code == 403)
lead_c = TestClient(app)
login(lead_c, "lead", "lead-pass-1")
check("roles: admin cannot create super admin", lead_c.post("/admin/users", json={"name": "x", "username": "boss", "password": "12345678", "role": "super_admin"}).status_code == 403)

# 7. Live events were written
with session_scope() as db:
    from app.models import Event
    types = {e.type for e in db.scalars(select(Event))}
    check("events: desk and reporter events recorded", {"item.created", "item.updated", "report.status", "media.on_ftp"} <= types, str(types))

# 8. SSE over a real HTTP server: the desk stream receives an event emitted after it connects
import httpx  # noqa: E402
import uvicorn  # noqa: E402

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)
received = []


def listen():
    with httpx.stream("GET", "http://127.0.0.1:8765/events?app=desk", cookies=dict(desk.cookies), timeout=10) as r:
        received.append(r.status_code)
        for line in r.iter_lines():
            if line.startswith("event: "):
                received.append(line)
                return


listener = threading.Thread(target=listen, daemon=True)
listener.start()
time.sleep(2)
with session_scope() as db:
    from app.events import emit
    emit(db, "desk", "smoke.ping", {"ok": True})
listener.join(timeout=8)
check("events: SSE stream delivers new events", received[:2] == [200, "event: smoke.ping"], str(received))
server.should_exit = True

ftpd.close_all()
pg.cleanup()
shutil.rmtree(WORK, ignore_errors=True)
print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
