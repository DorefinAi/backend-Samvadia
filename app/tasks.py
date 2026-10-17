"""Background jobs. Each handler gets its own DB session and the job payload.

finalize_item          build the slug (or park for the desk), queue ftp_upload
ftp_upload             Storage -> FTP <base>/<slug>/<original name>, size check, delete from Storage
ftp_rename             the desk changed the slug of an item already on FTP
fetch_whatsapp_media   Meta -> Storage
fetch_mail             Outlook inbox delta -> items, messages, attachment jobs
fetch_mail_attachment  Graph -> Storage
purge_storage          retry Storage deletes that failed after a good FTP upload
"""
import logging
import os
import posixpath
import shutil
import tempfile
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.events import emit, item_changed
from app.intake import WAITING_STATES, add_media, add_message, find_reporter, finalize, record_fetch, schedule_finalize
from app.jobs import RetryLater, enqueue, now
from app.models import KV, Event, FeedLog, Item, Job, Media
from app.services import ftp, mail, storage, whatsapp
from app.services.filenames import generated_name, safe_name, unique_name
from app.services.slug import unique_slug
from app.stories import release_waiting

log = logging.getLogger("tasks")


def _item(db: Session, payload: dict) -> Item | None:
    return db.get(Item, payload["item_id"], with_for_update=True)


# --- finalize ---------------------------------------------------------------------------------

def finalize_item(db: Session, payload: dict) -> None:
    item = _item(db, payload)
    if item is None or item.intake_state not in ("collecting", "needs_desk"):
        return
    after = payload.get("after")
    if after and item.last_message_at > datetime.fromisoformat(after):
        return  # a newer message arrived; its own finalize job will run later
    finalize(db, item)


# --- FTP --------------------------------------------------------------------------------------

def ftp_upload(db: Session, payload: dict) -> None:
    item = _item(db, payload)
    if item is None or not item.slug:
        return
    files = db.scalars(select(Media).where(Media.item_id == item.id, Media.state == "in_storage")
                       .order_by(Media.received_at, Media.created_at)).all()
    workdir = tempfile.mkdtemp(prefix="vai-")
    remote = ftp.connect()
    try:
        if not item.ftp_folder:
            item.ftp_folder = _claim_folder(db, remote, item)
            db.commit()
        remote.makedirs(item.ftp_folder)
        taken = set(remote.listdir(item.ftp_folder))
        for index, media in enumerate(files, start=1):
            if not media.file_name:
                base = safe_name(media.original_name) or generated_name(media.received_at, index, media.content_type)
                media.file_name = unique_name(base, taken)
            taken.add(media.file_name)
            local = storage.download(media.storage_key, os.path.join(workdir, str(media.id)))
            target = posixpath.join(item.ftp_folder, media.file_name)
            ftp.upload_verified(remote, local.path, target, local.size)
            os.remove(local.path)
            media.state, media.ftp_path, media.size_bytes, media.error = "on_ftp", target, local.size, None
            media.sha256 = media.sha256 or local.sha256
            emit(db, "desk", "media.on_ftp", {"item_id": str(item.id), "media_id": str(media.id), "ftp_path": target})
            db.commit()  # each finished file is recorded even if a later one fails
            try:
                storage.delete([media.storage_key])
                media.storage_deleted_at = now()
                db.commit()
            except Exception as e:  # the file is safe on FTP; purge_storage retries the delete
                log.warning("storage delete failed for %s: %s", media.id, e)
                db.rollback()
    finally:
        remote.close()
        shutil.rmtree(workdir, ignore_errors=True)
    item = _item(db, payload)
    pending = db.scalar(select(func.count()).select_from(Media).where(
        Media.item_id == item.id, Media.state.in_(("in_storage", *WAITING_STATES))))
    if pending:
        raise RetryLater(15, f"{pending} more file(s) arrived during the upload")
    item.intake_state = "on_ftp"
    release_waiting(db, item)  # approved stories enter the feed only once their files are on FTP
    item_changed(db, item)


def _claim_folder(db: Session, remote: ftp.Remote, item: Item) -> str:
    """The slug is the folder. If a folder of that name already exists on FTP, move to _2, _3 ..."""
    slug = item.slug
    taken_db = set(db.scalars(select(Item.slug).where(Item.slug.like(f"{slug}%"), Item.id != item.id)).all())
    while remote.exists(ftp.folder_for(slug)) or slug in taken_db:
        taken_db.add(slug)
        slug = unique_slug(item.slug, taken_db)
    item.slug = slug
    return ftp.folder_for(slug)


def ftp_upload_failed(db: Session, payload: dict, error: str) -> None:
    item = _item(db, payload)
    if item:
        item.intake_state, item.intake_note = "ftp_failed", error[:500]
        item_changed(db, item)


def ftp_rename(db: Session, payload: dict) -> None:
    item = _item(db, payload)
    old = payload["old_folder"]
    if item is None or not item.slug or item.ftp_folder != old:
        return
    new = ftp.folder_for(item.slug)
    remote = ftp.connect()
    try:
        if remote.exists(old):
            remote.rename(old, new)
        else:
            remote.makedirs(new)
    finally:
        remote.close()
    item.ftp_folder = new
    for media in db.scalars(select(Media).where(Media.item_id == item.id, Media.ftp_path.isnot(None))):
        media.ftp_path = posixpath.join(new, posixpath.basename(media.ftp_path))
    item_changed(db, item)


# --- WhatsApp ---------------------------------------------------------------------------------

def fetch_whatsapp_media(db: Session, payload: dict) -> None:
    media = db.get(Media, payload["media_id"])
    if media is None or media.state != "downloading":
        return
    info = whatsapp.media_info(media.external_ref)  # fresh URL each attempt; they expire after 5 minutes
    _to_storage(db, media, whatsapp.stream_media(info["url"]), info.get("mime_type") or media.content_type)


def _to_storage(db: Session, media: Media, chunks, content_type: str | None) -> None:
    workdir = tempfile.mkdtemp(prefix="vai-")
    try:
        local = storage.upload_chunks(media.storage_key, chunks, content_type, os.path.join(workdir, "f"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    media.state, media.size_bytes, media.sha256, media.content_type, media.error = (
        "in_storage", local.size, local.sha256, content_type, None)
    emit(db, "desk", "media.in_storage", {"item_id": str(media.item_id), "media_id": str(media.id)})


def media_download_failed(db: Session, payload: dict, error: str) -> None:
    """Give up on one file; the rest of the item still goes to FTP. The desk can retry it."""
    media = db.get(Media, payload["media_id"])
    if media is None:
        return
    media.state, media.error = "failed", error[:500]
    emit(db, "desk", "media.failed", {"item_id": str(media.item_id), "media_id": str(media.id)})
    item = db.get(Item, media.item_id)
    if item and item.intake_state == "collecting":
        schedule_finalize(db, item)


# --- Outlook ----------------------------------------------------------------------------------

def fetch_mail(db: Session, payload: dict) -> None:
    state = db.get(KV, "mail_delta")
    messages, delta_link = mail.inbox_delta(state.value["link"] if state else None)
    new_titles: list[str] = []
    for m in messages:
        sender = ((m.get("from") or {}).get("emailAddress") or {})
        address = (sender.get("address") or "").lower()
        received = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
        result = add_message(
            db, source="outlook", external_id=m["id"], sender_contact=address, sender_name=sender.get("name"),
            text=mail.clean_body((m.get("body") or {}).get("content", "")), received_at=received,
            raw={k: m.get(k) for k in ("subject", "internetMessageId", "conversationId", "body")},
            conversation_key=m.get("conversationId") or m["id"], reporter=find_reporter(db, email=address),
            subject=m.get("subject"),
        )
        if result is None:
            continue
        item, message, created = result
        if created:
            new_titles.append(item.headline)
        if m.get("hasAttachments"):
            for a in mail.list_attachments(m["id"]):
                kind = a.get("@odata.type", "")
                if a.get("isInline"):
                    continue  # signature logos and pasted images
                if kind.endswith("referenceAttachment"):  # OneDrive/SharePoint link, not a file
                    add_media(db, item, source="outlook", state="link_only", message=message,
                              original_name=a.get("name"), received_at=received)
                    continue
                media = add_media(db, item, source="outlook", state="downloading", message=message,
                                  original_name=a.get("name"), content_type=a.get("contentType"),
                                  external_ref=f"{m['id']}/{a['id']}", size_bytes=a.get("size"), received_at=received)
                enqueue(db, "fetch_mail_attachment", {"media_id": str(media.id)})
        item_changed(db, item, "item.created" if created else "item.updated")
        schedule_finalize(db, item, delay=5)
    db.merge(KV(key="mail_delta", value={"link": delta_link}))
    record_fetch(db, "outlook", new_titles, always=bool(payload.get("manual")))


def fetch_mail_attachment(db: Session, payload: dict) -> None:
    media = db.get(Media, payload["media_id"])
    if media is None or media.state != "downloading":
        return
    message_id, attachment_id = media.external_ref.split("/", 1)
    _to_storage(db, media, mail.stream_attachment(message_id, attachment_id), media.content_type)


# --- housekeeping -----------------------------------------------------------------------------

def purge_storage(db: Session, payload: dict) -> None:
    rows = db.scalars(select(Media).where(Media.state == "on_ftp", Media.storage_deleted_at.is_(None)).limit(200)).all()
    if rows:
        storage.delete([m.storage_key for m in rows])
        for m in rows:
            m.storage_deleted_at = now()


def cleanup(db: Session, payload: dict) -> None:
    db.execute(delete(Event).where(Event.created_at < now() - timedelta(hours=24)))
    db.execute(delete(Job).where(Job.status == "done", Job.updated_at < now() - timedelta(days=7)))
    db.execute(delete(FeedLog).where(FeedLog.at < now() - timedelta(days=90)))


HANDLERS = {
    "finalize_item": (finalize_item, None),
    "ftp_upload": (ftp_upload, ftp_upload_failed),
    "ftp_rename": (ftp_rename, None),
    "fetch_whatsapp_media": (fetch_whatsapp_media, media_download_failed),
    "fetch_mail": (fetch_mail, None),
    "fetch_mail_attachment": (fetch_mail_attachment, media_download_failed),
    "purge_storage": (purge_storage, None),
    "cleanup": (cleanup, None),
}
