"""Integration tests: the real API, job queue, migrations, Postgres (pgserver) and FTP (pyftpdlib).

    .venv/Scripts/python -m pytest tests/integration

Supabase Storage, Meta and Microsoft Graph are replaced by local fakes, as in tests/smoke/run_smoke.py.
Every test starts from empty tables, an empty FTP folder and an empty fake Storage.
"""
import hashlib
import hmac
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    """Paths and helpers shared by the tests."""

    def __init__(self, work: Path, ftp_port: int):
        self.work = work
        self.ftp_root = work / "ftp"
        self.store = work / "storage"
        self.ftp_port = ftp_port
        self.ftp_up = True  # tests flip this to simulate an FTP outage
        # what the fake Meta / Graph return; tests change these
        self.wa_media: dict[str, bytes] = {}
        self.wa_fail: set[str] = set()
        self.mail_batch: list[dict] = []
        self.mail_attachments: dict[str, list[dict]] = {}

    def ftp_folder(self, slug: str) -> Path:
        return self.ftp_root / "vartmaan" / "incoming" / slug


@pytest.fixture(scope="session")
def stack():
    import pgserver
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    work = Path(tempfile.mkdtemp(prefix="vai-it-"))
    st = Stack(work, _free_port())
    st.ftp_root.mkdir()
    st.store.mkdir()
    pg = pgserver.get_server(str(work / "pg"), cleanup_mode="stop")

    os.environ.update(
        DATABASE_URL=pg.get_uri(), COOKIE_SECURE="false", FTP_PROTOCOL="ftp", FTP_HOST="127.0.0.1",
        FTP_PORT=str(st.ftp_port), FTP_USER="vai", FTP_PASSWORD="vai-pass", FTP_BASE_DIR="/vartmaan/incoming",
        WHATSAPP_MERGE_SECONDS="0", WHATSAPP_APP_SECRET="app-secret", WHATSAPP_TOKEN="wa-token",
        WHATSAPP_VERIFY_TOKEN="verify-me", MS_TENANT_ID="t", MS_CLIENT_ID="c", MS_CLIENT_SECRET="s",
        MS_MAILBOX="desk@example.com", SUPABASE_URL="http://fake-storage", SUPABASE_SERVICE_KEY="k",
        FEED_SECRET="feed-secret", OCTOPUS_ALLOWED_IPS="10.9.8.7", FEED_LINK_BASE="https://dorefinai.com/wire",
        MAX_UPLOAD_MB="1", BLIND_RATE="0.10",
    )

    # Settings and the engine may already exist if a unit test imported app.db first: rebuild both.
    from sqlalchemy import create_engine

    import app.db
    from app.config import get_settings
    get_settings.cache_clear()
    app.db.engine.dispose()
    app.db.engine = create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    app.db.SessionLocal.configure(bind=app.db.engine)

    auth = DummyAuthorizer()
    auth.add_user("vai", "vai-pass", str(st.ftp_root), perm="elradfmwMT")
    FTPHandler.authorizer = auth
    FTPHandler.encoding = "utf-8"
    ftpd = FTPServer(("127.0.0.1", st.ftp_port), FTPHandler)
    threading.Thread(target=ftpd.serve_forever, kwargs={"timeout": 0.2}, daemon=True).start()

    from alembic import command
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")

    _install_fakes(st)
    yield st
    ftpd.close_all()
    pg.cleanup()
    shutil.rmtree(work, ignore_errors=True)


def _install_fakes(st: Stack) -> None:
    from app.services import ftp, mail, storage, whatsapp

    def upload_url(key):
        return f"fake://{key}"

    def list_sizes(prefix):
        folder = st.store / prefix
        return {p.name: p.stat().st_size for p in folder.iterdir()} if folder.exists() else {}

    def upload_chunks(key, chunks, content_type, tmp_path):
        data = b"".join(chunks)
        (st.store / key).parent.mkdir(parents=True, exist_ok=True)
        (st.store / key).write_bytes(data)
        return storage.LocalCopy(tmp_path, len(data), hashlib.sha256(data).hexdigest())

    def download(key, tmp_path):
        data = (st.store / key).read_bytes()
        Path(tmp_path).write_bytes(data)
        return storage.LocalCopy(tmp_path, len(data), hashlib.sha256(data).hexdigest())

    def delete(keys):
        for k in keys:
            (st.store / k).unlink(missing_ok=True)

    storage.signed_upload_url, storage.list_sizes = upload_url, list_sizes
    storage.upload_chunks, storage.download, storage.delete = upload_chunks, download, delete

    def media_info(media_id):
        if media_id in st.wa_fail:
            raise RuntimeError(f"Meta is down for {media_id}")
        data = st.wa_media.get(media_id, b"JPEG-" + media_id.encode())
        return {"url": f"https://meta/{media_id}", "mime_type": "image/jpeg", "file_size": len(data)}

    whatsapp.media_info = media_info
    whatsapp.stream_media = lambda url: iter([st.wa_media.get(url.rsplit("/", 1)[1], b"JPEG-" + url.rsplit("/", 1)[1].encode())])

    mail.inbox_delta = lambda link: (list(st.mail_batch), "DELTA-NEXT")
    mail.list_attachments = lambda mid: st.mail_attachments.get(mid, [])
    mail.stream_attachment = lambda mid, aid: iter([b"ATTACHMENT-" + aid.encode()])
    mail.get_message = lambda mid: next(m for m in st.mail_batch if m["id"] == mid)

    real_connect = ftp.connect

    def connect():
        if not st.ftp_up:
            raise ConnectionRefusedError("FTP is down (test)")
        return real_connect()

    ftp.connect = connect


@pytest.fixture(autouse=True)
def clean(stack):
    """Empty tables (locations stay: they are seeded by a migration), FTP and Storage."""
    from sqlalchemy import text

    from app.db import session_scope
    from app.security import login_limiter

    with session_scope() as db:
        tables = [r[0] for r in db.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename NOT IN ('alembic_version', 'locations')"))]
        db.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    for folder in (stack.ftp_root, stack.store):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
    stack.ftp_up = True
    stack.wa_media.clear()
    stack.wa_fail.clear()
    stack.mail_batch.clear()
    stack.mail_attachments.clear()
    login_limiter.failures.clear()
    yield


# --- helpers ------------------------------------------------------------------------------------

PASSWORD = "test-pass-123"


@pytest.fixture
def make_user():
    from app.config import get_settings
    from app.db import session_scope
    from app.models import User
    from app.security import hash_password

    def make(username: str, role: str, active: bool = True):
        with session_scope() as db:
            user = User(org_id=get_settings().org_id, name=username.title(), username=username,
                        password_hash=hash_password(PASSWORD), role=role, active=active)
            db.add(user)
            db.flush()
            return user.id
    return make


@pytest.fixture
def make_reporter():
    from app.config import get_settings
    from app.db import session_scope
    from app.models import Reporter

    def make(slug_code="SUNITA", city_code="UDR", phone="919876543210", email=None, user_id=None, name="Sunita Sharma"):
        with session_scope() as db:
            r = Reporter(org_id=get_settings().org_id, name=name, slug_code=slug_code, city_code=city_code,
                         district="Udaipur", phone=phone, email=email, user_id=user_id)
            db.add(r)
            db.flush()
            return r.id
    return make


@pytest.fixture
def login():
    from fastapi.testclient import TestClient

    from app.main import app

    def do(username: str, password: str = PASSWORD) -> TestClient:
        client = TestClient(app)
        r = client.post("/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        client.headers["X-CSRF"] = r.json()["csrf_token"]
        return client
    return do


@pytest.fixture
def desk(make_user, login):
    make_user("desk", "assignment_desk")
    return login("desk")


@pytest.fixture
def admin(make_user, login):
    make_user("lead", "admin")
    return login("lead")


@pytest.fixture
def reporter_client(make_user, make_reporter, login):
    user_id = make_user("sunita", "reporter")
    make_reporter(user_id=user_id)
    return login("sunita")


@pytest.fixture
def drain():
    """Run due jobs until the queue is empty. When nothing is due, move every pending job to now, the way
    waiting would (jumping only the earliest lets a job that keeps saying RetryLater starve the others)."""
    from sqlalchemy import select, update

    from app import jobs
    from app.db import session_scope
    from app.models import Job

    def run(max_rounds: int = 300, queues=None):
        for _ in range(max_rounds):
            if jobs.run_one(queues):
                continue
            with session_scope() as db:
                q = update(Job).where(Job.status == "pending")
                if queues:
                    q = q.where(Job.queue.in_(queues))
                if not db.execute(q.values(run_after=jobs.now())).rowcount:
                    return
        with session_scope() as db:
            pending = [f"{j.type} attempts={j.attempts} {j.payload} {(j.last_error or '')[:120]}"
                       for j in db.scalars(select(Job).where(Job.status == "pending"))]
        raise AssertionError("job queue did not drain; still pending:\n" + "\n".join(pending))
    return run


@pytest.fixture
def whatsapp_post():
    from fastapi.testclient import TestClient

    from app.main import app
    hook = TestClient(app)

    def post(messages: list[dict], wa_id="919876543210", name="Sunita", secret=b"app-secret"):
        payload = {"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
            "messages": [{"from": wa_id, "timestamp": str(int(time.time())), **m} for m in messages],
        }}]}]}
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        return hook.post("/webhooks/whatsapp", content=raw,
                         headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    return post


@pytest.fixture
def item():
    """item(id) -> the Item row as it is now, detached (its media and reporter loaded)."""
    from app.db import session_scope
    from app.models import Item

    def load(item_id):
        with session_scope() as db:
            row = db.get(Item, item_id)
            if row is not None:
                _ = row.media, row.reporter
                db.expunge_all()
            return row
    return load


@pytest.fixture
def filed_item(reporter_client, stack, drain):
    """filed_item(**report) -> id of a portal report that is through intake (on FTP unless submit=False)."""
    def make(files=(("clip.mp4", b"video-bytes"),), submit=True, run_jobs=True, **body):
        payload = {"headline": "उदयपुर में पर्यटन सीजन शुरू", "body": "झीलों के शहर में पर्यटकों की भीड़।",
                   "subject": "Paryatan season", "story_type": "AV",
                   "files": [{"name": n, "size": len(b), "content_type": "video/mp4"} for n, b in files], **body}
        r = reporter_client.post("/reporter/items", json=payload)
        assert r.status_code == 200, r.text
        for (_, data), up in zip(files, r.json()["uploads"]):
            key = up["upload_url"].removeprefix("fake://")
            (stack.store / key).parent.mkdir(parents=True, exist_ok=True)
            (stack.store / key).write_bytes(data)
        item_id = r.json()["item"]["id"]
        if submit:
            assert reporter_client.post(f"/reporter/items/{item_id}/submit").status_code == 200
            if run_jobs:
                drain()
        return item_id
    return make


@pytest.fixture
def octopus():
    """GET the feed the way Octopus does: the secret path, from the allowed IP."""
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)

    def get(headers=None, secret="feed-secret", ip="10.9.8.7"):
        return client.get(f"/feed/{secret}/feed.xml", headers={"X-Forwarded-For": ip, **(headers or {})})
    return get
