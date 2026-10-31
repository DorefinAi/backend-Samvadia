"""Background jobs. Each handler gets its own DB session and the job payload.

finalize_item          build the slug (or park for the desk), queue ftp_upload
ftp_upload             Storage -> FTP <base>/<slug>/<original name>, size check, delete from Storage
ftp_rename             the desk changed the slug of an item already on FTP
fetch_whatsapp_media   Meta -> Storage
fetch_mail             Outlook inbox delta -> items, messages, attachment jobs
fetch_mail_attachment  Graph -> Storage
purge_storage          retry Storage deletes that failed after a good FTP upload
footage_scan           look for reporters' own footage on the newsroom FTP (read-only), see services/footage.py
"""
import logging
import os
import posixpath
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.events import emit, item_changed
from app.intake import WAITING_STATES, add_media, add_message, find_reporter, finalize, record_fetch, schedule_finalize
from app.jobs import GiveUp, RetryLater, enqueue, now
from app.models import KV, Event, FeedLog, Item, Job, Media
from app.services import footage, ftp, mail, storage, whatsapp
from app.services.filenames import generated_name, safe_name, unique_name
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
    if not get_settings().ftp_host:  # not set up yet: wait, and upload once FTP_HOST is set and the worker restarted
        raise RetryLater(300, "FTP_HOST is not set")
    if not item.ftp_folder:
        item.ftp_folder = ftp.folder_for(item.slug)  # one slug, one folder: a slug sent again joins it
        db.commit()
    folder = item.ftp_folder
    with _folder_lock(folder):  # other stories with this slug may be writing to the same folder
        db.refresh(item)
        if item.ftp_folder != folder:
            raise RetryLater(5, "the FTP folder was renamed while this upload waited")
        files = db.scalars(select(Media).where(Media.item_id == item.id, Media.state == "in_storage")
                           .order_by(Media.received_at, Media.created_at)).all()
        workdir = tempfile.mkdtemp(prefix="vai-")
        remote = ftp.connect()
        try:
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


@contextmanager
def _folder_lock(*folders: str) -> Iterator[None]:
    """One writer per FTP folder at a time, across workers, so two stories never pick the same file name.
    Held on its own connection: the job's session commits after every file. Busy: try again shortly."""
    keys = sorted({f.lower() for f in folders})  # the newsroom FTP runs on Windows; sorted, so no deadlock
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:  # not idle in transaction
        held = []
        try:
            for key in keys:
                if not conn.scalar(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}):
                    raise RetryLater(20, f"Another story is writing to {key}")
                held.append(key)
            yield
        finally:
            try:
                for key in held:
                    conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
            except Exception as e:  # never hand a connection that may still hold a lock back to the pool
                log.warning("folder unlock failed, dropping the connection: %s", e)
                conn.invalidate()


def ftp_rename_failed(db: Session, payload: dict, error: str) -> None:
    """The folder kept its old name: say so on the item, where the desk sees it."""
    item = _item(db, payload)
    if item:
        item.intake_note = f"FTP folder not renamed (some files may already be in the new folder): {error}"[:500]
        item_changed(db, item)


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
    if new.lower() == old.lower():
        return  # only the letter case changed: the newsroom FTP (Windows) sees the same folder, so keep it
    if not get_settings().ftp_host:
        raise RetryLater(300, "FTP_HOST is not set")
    with _folder_lock(old, new):
        media_rows = db.scalars(select(Media).where(Media.item_id == item.id, Media.ftp_path.isnot(None))).all()
        shared = db.scalar(select(Item.id).where(func.lower(Item.ftp_folder) == old.lower(), Item.id != item.id)
                           .limit(1)) is not None
        remote = ftp.connect()
        try:
            if not shared and remote.exists(old) and not remote.exists(new):
                remote.rename(old, new)
                for media in media_rows:
                    media.ftp_path = posixpath.join(new, posixpath.basename(media.ftp_path))
            else:
                # The new slug's folder is already there (join it), or other stories still use the old one:
                # move only this story's files, one at a time, so a retry picks up where this one stopped.
                remote.makedirs(new)
                taken = set(remote.listdir(new))
                for media in media_rows:
                    if posixpath.dirname(media.ftp_path).lower() == new.lower():
                        continue  # moved by an earlier attempt
                    current = posixpath.basename(media.ftp_path)
                    if remote.exists(media.ftp_path):
                        name = unique_name(current, taken)
                        remote.rename(media.ftp_path, posixpath.join(new, name))
                    elif remote.exists(posixpath.join(new, current)) and not _ftp_path_taken(db, item, new, current):
                        name = current  # an earlier attempt moved it (or the whole folder), then stopped
                    else:
                        raise GiveUp(f"{media.ftp_path} is missing on the FTP server, so it can't move to {new}.")
                    taken.add(name)
                    media.file_name, media.ftp_path = name, posixpath.join(new, name)
                    db.commit()
        finally:
            remote.close()
        item.ftp_folder = new  # committed before the lock is released, so a waiting upload sees the new folder
        item_changed(db, item)
        db.commit()


def _ftp_path_taken(db: Session, item: Item, folder: str, name: str) -> bool:
    """Another story's file already recorded at folder/name: never adopt it as this story's."""
    path = posixpath.join(folder, name).lower()
    return db.scalar(select(Media.id).where(func.lower(Media.ftp_path) == path, Media.item_id != item.id)
                     .limit(1)) is not None


# --- WhatsApp ---------------------------------------------------------------------------------

def fetch_whatsapp_media(db: Session, payload: dict) -> None:
    media = db.get(Media, payload["media_id"])
    if media is None or media.state != "downloading":
        return
    info = whatsapp.media_info(media.external_ref)  # fresh URL each attempt; they expire after 5 minutes
    if _too_big(info.get("file_size")):
        raise GiveUp(_too_big_note())
    _to_storage(db, media, whatsapp.stream_media(info["url"]), info.get("mime_type") or media.content_type)


def _limit_bytes() -> int:
    return get_settings().max_upload_mb * 1024 * 1024


def _too_big(size: int | str | None) -> bool:
    try:
        return int(size or 0) > _limit_bytes()
    except (TypeError, ValueError):
        return False


def _too_big_note() -> str:
    return f"Over {get_settings().max_upload_mb} MB. Ask the reporter to send it by FTP."


def _capped(chunks):
    """Stop a download that runs past the size limit, for files whose declared size was missing or wrong."""
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > _limit_bytes():
            raise GiveUp(_too_big_note())
        yield chunk


def _to_storage(db: Session, media: Media, chunks, content_type: str | None) -> None:
    workdir = tempfile.mkdtemp(prefix="vai-")
    try:
        local = storage.upload_chunks(media.storage_key, _capped(chunks), content_type, os.path.join(workdir, "f"))
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

MAIL_MAX_ATTEMPTS = 3
MAIL_GIVE_UP_AFTER = timedelta(hours=24)  # throttling and outages do not count as attempts, but not forever
MAIL_DEAD_KEEP = 200


def mail_fetch_since(db: Session) -> datetime:
    """MAIL_FETCH_SINCE, or the time of the first fetch (stored in kv.mail_fetch_since and kept)."""
    raw = get_settings().mail_fetch_since.strip()
    if raw:
        since = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    row = db.get(KV, "mail_fetch_since")
    if row:
        return datetime.fromisoformat(row.value["at"])
    since = now()
    db.merge(KV(key="mail_fetch_since", value={"at": since.isoformat()}))
    log.info("first mail fetch: only mail received from %s on is taken", since.isoformat())
    return since


def _parse_time(value: str | None) -> datetime | None:
    try:
        at = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _received(m: dict) -> datetime | None:
    return _parse_time(m.get("receivedDateTime"))


def fetch_mail(db: Session, payload: dict) -> None:
    """The delta cursor always moves on. A message that fails goes on a retry list and is fetched
    again by id on the next poll, so one bad email never holds up the rest of the inbox. After
    MAIL_MAX_ATTEMPTS it is logged in kv.mail_dead and dropped.
    Only mail received at or after mail_fetch_since() is taken; older mail is skipped without a trace."""
    since = mail_fetch_since(db)
    state = db.get(KV, "mail_delta")
    retry_row = db.get(KV, "mail_retry")
    retry: dict[str, dict] = dict(retry_row.value) if retry_row else {}
    messages, delta_link = mail.inbox_delta(state.value["link"] if state else None, since=since)
    in_delta = {m["id"] for m in messages}
    for message_id, entry in list(retry.items()):  # the cut-off applies to retries too: no fetch, no ingest
        received = _parse_time(entry.get("received"))
        if received is not None and received < since:
            retry.pop(message_id)
    for message_id in [i for i in retry if i not in in_delta]:
        try:
            messages.append(mail.get_message(message_id))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:  # deleted from the inbox since
                retry.pop(message_id)
            else:
                _mail_failed(db, retry, message_id, e)
        except Exception as e:
            _mail_failed(db, retry, message_id, e)

    new_titles: list[str] = []
    old = [m for m in messages if (_received(m) or since) < since]
    if old:
        log.info("skipped %s email(s) received before %s", len(old), since.isoformat())
        for m in old:
            retry.pop(m.get("id"), None)
    messages = [m for m in messages if (_received(m) or since) >= since]
    db.commit()  # keep what came before the loop (mail_fetch_since, mail_dead) whatever a message does
    for m in messages:
        # One transaction per message: the row locks it takes (a sent-back item it reopens) are released at once,
        # not held for the whole poll. A crash mid-batch is safe: the delta link is stored only after the loop,
        # so the next poll sees these messages again and add_message skips the ones already stored.
        try:
            title = _ingest_mail(db, m)
            db.commit()
        except Exception as e:
            db.rollback()  # undoes only this message
            log.warning("mail %s failed: %s: %s", m.get("id"), type(e).__name__, e)
            _mail_failed(db, retry, m["id"], e, received=_received(m))
            continue
        retry.pop(m["id"], None)
        if title:
            new_titles.append(title)
    db.merge(KV(key="mail_delta", value={"link": delta_link}))
    db.merge(KV(key="mail_retry", value=retry))
    record_fetch(db, "outlook", new_titles, always=bool(payload.get("manual")))


def _ingest_mail(db: Session, m: dict) -> str | None:
    """One email -> item, message and attachment jobs. Returns the headline when it started a new item."""
    sender = ((m.get("from") or {}).get("emailAddress") or {})
    address = (sender.get("address") or "").lower()
    received = _received(m)
    if received is None:
        raise ValueError(f"receivedDateTime missing or not a date: {m.get('receivedDateTime')!r}")
    # Ask Graph before add_message: no network call while this transaction may hold an item's row lock
    attachments = mail.list_attachments(m["id"]) if m.get("hasAttachments") else []
    result = add_message(
        db, source="outlook", external_id=m["id"], sender_contact=address, sender_name=sender.get("name"),
        text=mail.clean_body((m.get("body") or {}).get("content", "")), received_at=received,
        raw={k: m.get(k) for k in ("subject", "internetMessageId", "conversationId", "body")},
        conversation_key=m.get("conversationId") or m["id"], reporter=find_reporter(db, email=address),
        subject=m.get("subject"),
    )
    if result is None:
        return None
    item, message, created = result
    if attachments:
        for a in attachments:
            kind = a.get("@odata.type", "")
            if a.get("isInline"):
                continue  # signature logos and pasted images
            if kind.endswith("referenceAttachment"):  # OneDrive/SharePoint link, not a file
                add_media(db, item, source="outlook", state="link_only", message=message,
                          original_name=a.get("name"), received_at=received)
                continue
            if _too_big(a.get("size")):
                media = add_media(db, item, source="outlook", state="failed", message=message,
                                  original_name=a.get("name"), content_type=a.get("contentType"),
                                  external_ref=f"{m['id']}/{a['id']}", size_bytes=a.get("size"), received_at=received)
                media.error = _too_big_note()
                continue
            media = add_media(db, item, source="outlook", state="downloading", message=message,
                              original_name=a.get("name"), content_type=a.get("contentType"),
                              external_ref=f"{m['id']}/{a['id']}", size_bytes=a.get("size"), received_at=received)
            enqueue(db, "fetch_mail_attachment", {"media_id": str(media.id)})
    item_changed(db, item, "item.created" if created else "item.updated")
    schedule_finalize(db, item, delay=5)
    return item.headline if created else None


def _mail_failed(db: Session, retry: dict[str, dict], message_id: str, error: Exception,
                 received: datetime | None = None) -> None:
    entry = retry.get(message_id) or {"attempts": 0, "first_failed_at": now().isoformat()}
    if received is not None:
        entry["received"] = received.isoformat()  # so a later cut-off drops it without fetching it again
    if not mail.is_transient(error):
        entry["attempts"] += 1
    entry["error"] = f"{type(error).__name__}: {error}"[:300]
    expired = now() - datetime.fromisoformat(entry["first_failed_at"]) > MAIL_GIVE_UP_AFTER
    if entry["attempts"] < MAIL_MAX_ATTEMPTS and not expired:
        retry[message_id] = entry
        return
    retry.pop(message_id, None)
    log.error("mail %s dropped after %s attempts: %s", message_id, entry["attempts"], entry["error"])
    dead = db.get(KV, "mail_dead")
    entries = list(dead.value.get("messages", [])) if dead else []
    entries.append({"id": message_id, **entry, "dropped_at": now().isoformat()})
    db.merge(KV(key="mail_dead", value={"messages": entries[-MAIL_DEAD_KEEP:]}))


def fetch_mail_attachment(db: Session, payload: dict) -> None:
    media = db.get(Media, payload["media_id"])
    if media is None or media.state != "downloading":
        return
    message_id, attachment_id = media.external_ref.split("/", 1)
    try:
        _to_storage(db, media, mail.stream_attachment(message_id, attachment_id), media.content_type)
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (405, 415):
            raise
        # A OneDrive/SharePoint link or a forwarded item that Graph cannot hand over as a file
        media.state, media.error = "link_only", "A link, not a file. Open the email in Outlook to get it."
        item = db.get(Item, media.item_id)
        if item:
            item_changed(db, item)


# --- housekeeping -----------------------------------------------------------------------------

def purge_storage(db: Session, payload: dict) -> None:
    rows = db.scalars(select(Media).where(Media.state == "on_ftp", Media.storage_deleted_at.is_(None)).limit(200)).all()
    if rows:
        storage.delete([m.storage_key for m in rows])
        for m in rows:
            m.storage_deleted_at = now()


# --- FTP footage ------------------------------------------------------------------------------------

def footage_scan(db: Session, payload: dict) -> None:
    """Check every item that is due (waiting, or found and still inside its wait window). FTP work happens
    with no row locked; each result is written under a short FOR UPDATE, so a desk edit in between wins."""
    at = now()
    ids = footage.due_ids(db, at)
    if not ids:
        return
    db.commit()  # nothing held while we talk to the FTP server
    remote = ftp.connect()
    try:
        lister = footage.Lister(remote)
        for item_id in ids:
            item = db.get(Item, item_id)
            if item is None:
                continue
            try:
                match = footage.compute(lister, item)
            except Exception as e:  # one odd folder must not stop the scan
                log.warning("footage check for %s failed: %s: %s", item_id, type(e).__name__, e)
                db.rollback()
                continue
            db.rollback()  # drop the unlocked copy; re-read under a lock
            item = db.get(Item, item_id, with_for_update=True, populate_existing=True)
            if item is None or item.footage_status not in ("waiting", "found"):
                db.rollback()
                continue
            if footage.apply(item, match, now()):
                item_changed(db, item)
            db.commit()
    finally:
        remote.close()


def cleanup(db: Session, payload: dict) -> None:
    db.execute(delete(Event).where(Event.created_at < now() - timedelta(hours=24)))
    db.execute(delete(Job).where(Job.status == "done", Job.updated_at < now() - timedelta(days=7)))
    db.execute(delete(FeedLog).where(FeedLog.at < now() - timedelta(days=90)))


HANDLERS = {
    "finalize_item": (finalize_item, None),
    "ftp_upload": (ftp_upload, ftp_upload_failed),
    "ftp_rename": (ftp_rename, ftp_rename_failed),
    "fetch_whatsapp_media": (fetch_whatsapp_media, media_download_failed),
    "fetch_mail": (fetch_mail, None),
    "fetch_mail_attachment": (fetch_mail_attachment, media_download_failed),
    "purge_storage": (purge_storage, None),
    "footage_scan": (footage_scan, None),
    "cleanup": (cleanup, None),
}
