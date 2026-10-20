"""The whole backend on this computer, with nothing outside it: a local Postgres, a local FTP server,
files kept in a folder instead of Supabase, the API on http://localhost:8000 and the worker.

    pip install -e ".[dev]"            # once: adds pgserver and pyftpdlib
    python -m app.devstack             # first run loads sample users, reporters and messages
    python -m app.devstack --reset     # wipe the local database and files, then start fresh

Everything lives in backend/.local/ (git-ignored). FTP uploads land in .local/ftp/vartmaan/incoming/<slug>/.
Ctrl+C stops it all. WhatsApp, Outlook and the real FTP stay off; their settings come later on Render.

Sample logins (local only, never used anywhere else):
    admin / admin-pass-1     super admin
    desk  / desk-pass-1      assignment desk
    chief / chief-pass-1     chief editor
    sunita / sunita-pass-1   reporter (for the reporter portal)
"""
import argparse
import logging
import os
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
API_PORT = 8000
FTP_PORT = 2121

USERS = [
    ("Admin", "admin", "admin-pass-1", "super_admin"),
    ("Desk", "desk", "desk-pass-1", "assignment_desk"),
    ("Chief editor", "chief", "chief-pass-1", "chief_editor"),
]
# name, slug code, city, district, phone
REPORTERS = [
    ("Mahendra Singh Rathore", "MAHENDRA", "JDH", "Jodhpur", "919000000101"),
    ("Anil Sharma", "ANIL", "JPR", "Jaipur", "919000000105"),
    ("Ramesh Jat", "RAMESH", "BHL", "Bhilwara", "919000000117"),
    ("Pooja Meena", "POOJA", "KOT", "Kota", "919000000122"),
    ("Kavita Paliwal", "KAVITA", "UDR", "Udaipur", "919000000131"),
    ("Shyam Sundar Swami", "SHYAM", "BKN", "Bikaner", "919000000144"),
    ("Sunita Sharma", "SUNITA", "UDR", "Udaipur", "919876543210"),
]
# source, minutes ago, sender name, phone or email, subject (email only), text, [(file name, MB, state)]
MESSAGES = [
    ("whatsapp", 6, "Mahendra Singh Rathore", "919000000101", None,
     "जोधपुर में तेज बारिश से कई इलाकों में जलभराव। पावटा, सरदारपुरा और रातानाडा में सड़कों पर 2 फीट तक पानी भरा। नगर निगम की टीमें मौके पर पहुंचीं, पंप लगाकर पानी निकाला जा रहा है।",
     [("VID-20261015-WA0012.mp4", 48.2, "in_storage"), ("IMG-20261015-WA0014.jpg", 2.1, "in_storage")]),
    ("outlook", 14, "DIPR Rajasthan", "press.dipr@gov.example.in", "मुख्यमंत्री ने जयपुर में 1,200 करोड़ की विकास परियोजनाओं का लोकार्पण किया",
     "प्रेस विज्ञप्ति। मुख्यमंत्री ने आज जयपुर के बिड़ला सभागार में आयोजित कार्यक्रम में 1,200 करोड़ रुपये की 46 विकास परियोजनाओं का लोकार्पण एवं शिलान्यास किया।\n\nसंलग्न: प्रेस नोट एवं फोटो",
     [("CM_Jaipur_Press_Note.pdf", 0.4, "in_storage"), ("CM_Jaipur_01.jpg", 3.2, "in_storage")]),
    ("whatsapp", 19, "Ramesh Jat", "919000000117", None,
     "भीलवाड़ा के सांगानेरी गेट पर ज्वेलरी शॉप में दिनदहाड़े चोरी। 2 युवक ग्राहक बनकर आए और सोने की चेन लेकर भागे। पूरी वारदात CCTV में कैद।",
     [("VID-20261015-WA0031.mp4", 12.4, "in_storage")]),
    ("whatsapp", 27, "Pooja Meena", "919000000122", None,
     "कोटा में डेंगू का कहर, 24 घंटे में 40 नए मरीज। MBS अस्पताल के मेडिसिन वार्ड में बेड फुल।",
     [("PTT-20261015-WA0040.opus", 0.6, "in_storage"), ("IMG-20261015-WA0041.jpg", 1.8, "failed")]),
    ("outlook", 33, "Jaipur Police Commissionerate", "pro.police@gov.example.in", "जयपुर पुलिस: साइबर ठगी गिरोह का भंडाफोड़, 5 आरोपी गिरफ्तार",
     "जयपुर पुलिस कमिश्नरेट की साइबर थाना टीम ने ऑनलाइन निवेश के नाम पर ठगी करने वाले गिरोह का भंडाफोड़ कर 5 आरोपियों को गिरफ्तार किया है।",
     [("Cyber_Gang_Press_Note.pdf", 0.3, "in_storage")]),
    ("whatsapp", 38, "Anil Sharma", "919000000105", None,
     "साइबर ठगी गैंग का भंडाफोड़, 5 गिरफ्तार, 80 लाख की ठगी। DCP साइबर की बाइट भेज दी है।",
     [("VID-20261015-WA0050.mp4", 64.0, "in_storage")]),
    ("whatsapp", 61, "Kavita Paliwal", "919000000131", None,
     "फतेहसागर झील ओवरफ्लो, देखने उमड़े लोग। मानसून के बाद पहली बार चली चादर।",
     [("IMG-20261015-WA0061.jpg", 3.4, "in_storage")]),
    ("whatsapp", 96, "Shyam Sundar Swami", "919000000144", None,
     "बीकानेर में अंतरराष्ट्रीय ऊंट उत्सव की तैयारियां शुरू। इस बार विदेशी पर्यटकों के लिए नए कार्यक्रम।",
     [("VID-20261015-WA0070.mp4", 41.0, "in_storage")]),
]


def configure(reset: bool) -> None:
    """Start Postgres and FTP, and point every setting at them. Runs before any app module is imported."""
    import pgserver

    if reset and LOCAL.exists():
        if (LOCAL / "pg" / "postmaster.pid").exists():
            # A run that was killed (not Ctrl+C) leaves its Postgres up, holding the files: stop it first.
            # pg_ctl directly: pgserver.get_server() would try to start it, and caches the server by folder.
            import subprocess

            from pgserver._commands import POSTGRES_BIN_PATH

            subprocess.run([str(Path(POSTGRES_BIN_PATH) / "pg_ctl"), "-D", str(LOCAL / "pg"), "stop", "-m", "fast", "-w"],
                           capture_output=True, timeout=30)
        try:
            shutil.rmtree(LOCAL)
        except PermissionError as e:
            sys.exit(f"Could not wipe {LOCAL}: a file is still in use ({e.filename}). Stop the old run, then try again.")
    for folder in ("pg", "ftp", "storage"):
        (LOCAL / folder).mkdir(parents=True, exist_ok=True)

    pg = pgserver.get_server(str(LOCAL / "pg"), cleanup_mode="stop")
    os.environ.update(
        DATABASE_URL=pg.get_uri(), COOKIE_SECURE="false",
        CORS_ORIGINS="http://localhost:3000,http://localhost:3001,http://localhost:3002,http://localhost:3005",
        STORAGE_BACKEND="local", LOCAL_STORAGE_DIR=str(LOCAL / "storage"), PUBLIC_API_URL=f"http://localhost:{API_PORT}",
        FTP_PROTOCOL="ftp", FTP_HOST="127.0.0.1", FTP_PORT=str(FTP_PORT), FTP_USER="vai", FTP_PASSWORD="vai-local",
        FTP_BASE_DIR="/vartmaan/incoming", FEED_SECRET="local-feed", OCTOPUS_ALLOWED_IPS="",
        FEED_LINK_BASE="http://localhost:8000/wire", MS_TENANT_ID="", MS_CLIENT_ID="", MS_CLIENT_SECRET="", MS_MAILBOX="",
    )

    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    auth = DummyAuthorizer()
    auth.add_user("vai", "vai-local", str(LOCAL / "ftp"), perm="elradfmwMT")
    handler = type("LocalFtp", (FTPHandler,), {"authorizer": auth, "encoding": "utf-8"})
    logging.getLogger("pyftpdlib").setLevel(logging.WARNING)
    ftpd = FTPServer(("127.0.0.1", FTP_PORT), handler)
    threading.Thread(target=ftpd.serve_forever, daemon=True, name="ftp").start()


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    finally:
        os.chdir(cwd)


def seed() -> None:
    """Sample users, reporters and messages, once. The worker then builds slugs and files them on FTP."""
    from sqlalchemy import select

    from app.config import get_settings
    from app.db import session_scope
    from app.intake import add_media, add_message, find_reporter, schedule_finalize
    from app.models import Reporter, User
    from app.security import hash_password
    from app.services import storage

    org = get_settings().org_id
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        if db.scalar(select(User.id).limit(1)):
            return
        for name, username, password, role in USERS:
            db.add(User(org_id=org, name=name, username=username, password_hash=hash_password(password), role=role))
        sunita_user = User(org_id=org, name="Sunita Sharma", username="sunita", password_hash=hash_password("sunita-pass-1"), role="reporter")
        db.add(sunita_user)
        db.flush()
        for name, code, city, district, phone in REPORTERS:
            db.add(Reporter(org_id=org, name=name, slug_code=code, city_code=city, district=district, phone=phone,
                            user_id=sunita_user.id if code == "SUNITA" else None))
        db.flush()

        for i, (source, minutes, sender, contact, subject, text, files) in enumerate(MESSAGES):
            received = now - timedelta(minutes=minutes)
            email = source == "outlook"
            reporter = find_reporter(db, email=contact) if email else find_reporter(db, phone=contact)
            item, message, _ = add_message(
                db, source=source, external_id=f"seed-{i}", sender_contact=contact, sender_name=sender, text=text,
                received_at=received, raw={"seed": True, "subject": subject}, conversation_key=f"seed-{i}",
                reporter=reporter, subject=subject,
            )
            for name, mb, state in files:
                kind = {"mp4": "video/mp4", "jpg": "image/jpeg", "pdf": "application/pdf", "opus": "audio/ogg"}[name.rsplit(".", 1)[1]]
                media = add_media(db, item, source=source, state=state, message=message, original_name=name,
                                  content_type=kind, size_bytes=int(mb * 1024 * 1024), received_at=received)
                if state == "in_storage":
                    # A small stand-in file, so FTP gets something to upload
                    path = storage.local_path(media.storage_key)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f"{name}\n".encode() * 2048)
                else:
                    media.error = "Sample: this file failed to download."
            schedule_finalize(db, item)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.devstack")
    parser.add_argument("--reset", action="store_true", help="wipe .local/ (database, files, FTP) first")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    configure(args.reset)
    migrate()
    seed()

    import uvicorn

    from app.jobs import ALL_QUEUES, reset_stale, work_forever

    threading.Thread(target=work_forever, args=([(60, reset_stale)], ALL_QUEUES), daemon=True, name="worker").start()
    print(f"\nLocal backend: http://localhost:{API_PORT}  (API docs at /docs)")
    print(f"FTP uploads:   {LOCAL / 'ftp'}")
    print("Sample logins are listed at the top of backend/app/devstack.py. Ctrl+C stops everything.\n")
    uvicorn.run("app.main:app", host="127.0.0.1", port=API_PORT, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
