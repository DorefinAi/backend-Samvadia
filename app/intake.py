r"""What every channel shares: find or create the item, add messages and files, build the slug,
and hand the item to the FTP upload job.

    message/file arrives -> item (collecting) -> finalize -> slug -> uploading -> on_ftp
                                                       \-> needs_desk (reporter or subject missing)
"""
import random
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.events import emit, item_changed
from app.jobs import RetryLater, enqueue
from app.models import FetchRun, Item, Media, Message, Reporter
from app.services.filenames import kind_of
from app.services.slug import SlugError, build_slug, unique_slug
from app.services.whatsapp import digits

HEADLINE_MAX = 160
EMAIL_THREAD_WINDOW = timedelta(days=3)
WAITING_STATES = ("awaiting_upload", "downloading")


def find_reporter(db: Session, *, phone: str | None = None, email: str | None = None) -> Reporter | None:
    if phone:
        return db.scalar(select(Reporter).where(Reporter.phone == digits(phone), Reporter.active))
    if email:
        return db.scalar(select(Reporter).where(Reporter.email == email.strip().lower(), Reporter.active))
    return None


def first_line(text: str) -> str:
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    return line[:HEADLINE_MAX]


def new_item(db: Session, *, source: str, reporter: Reporter | None, sender_name: str | None,
             sender_contact: str | None, conversation_key: str | None, received_at: datetime,
             headline: str = "", body: str = "") -> Item:
    item = Item(
        id=uuid.uuid4(), org_id=get_settings().org_id, source=source, reporter_id=reporter.id if reporter else None,
        sender_name=sender_name or (reporter.name if reporter else None), sender_contact=sender_contact,
        conversation_key=conversation_key, headline=headline, body=body, received_at=received_at,
        last_message_at=received_at, intake_state="collecting", review_state="pending",
        blind=random.random() < get_settings().blind_rate,
    )
    db.add(item)
    db.flush()
    db.refresh(item)  # loads the reporter relationship
    return item


def add_message(db: Session, *, source: str, external_id: str, sender_contact: str | None, sender_name: str | None,
                text: str, received_at: datetime, raw: dict, conversation_key: str,
                reporter: Reporter | None, subject: str | None = None) -> tuple[Item, Message, bool] | None:
    """Attach a message to the open item for this sender/thread, or start a new item.
    Returns (item, message, item_is_new), or None for a message we already have
    (Meta and Graph can deliver the same message twice)."""
    if db.scalar(select(Message.id).where(Message.source == source, Message.external_id == external_id)):
        return None
    item = _open_item(db, source, conversation_key, received_at)
    created = item is None
    if created:
        item = new_item(db, source=source, reporter=reporter, sender_name=sender_name, sender_contact=sender_contact,
                        conversation_key=conversation_key, received_at=received_at,
                        headline=(subject or first_line(text))[:HEADLINE_MAX], body=text)
    else:
        if text:
            item.body = f"{item.body}\n\n{text}".strip()
        if not item.headline:
            item.headline = (subject or first_line(text))[:HEADLINE_MAX]
        item.last_message_at = max(item.last_message_at, received_at)
        if item.intake_state in ("on_ftp", "ftp_failed"):
            item.intake_state = "collecting"  # a reply with more files: upload the new ones too
    message = Message(item_id=item.id, source=source, external_id=external_id, sender_contact=sender_contact,
                      text=text, raw=raw, received_at=received_at)
    db.add(message)
    db.flush()
    return item, message, created


def _open_item(db: Session, source: str, conversation_key: str, received_at: datetime) -> Item | None:
    q = select(Item).where(Item.source == source, Item.conversation_key == conversation_key)
    if source == "whatsapp":
        window = timedelta(seconds=get_settings().whatsapp_merge_seconds)
        q = q.where(Item.intake_state == "collecting", Item.last_message_at >= received_at - window)
    else:  # email: replies in the same thread within 3 days, while the desk has not decided
        q = q.where(Item.review_state == "pending", Item.received_at >= received_at - EMAIL_THREAD_WINDOW)
    return db.scalar(q.order_by(Item.last_message_at.desc()).limit(1))


def add_media(db: Session, item: Item, *, source: str, state: str, message: Message | None = None,
              original_name: str | None = None, content_type: str | None = None, external_ref: str | None = None,
              size_bytes: int | None = None, received_at: datetime | None = None) -> Media:
    media_id = uuid.uuid4()
    media = Media(
        id=media_id, item_id=item.id, message_id=message.id if message else None, source=source,
        original_name=original_name, content_type=content_type, kind=kind_of(content_type),
        external_ref=external_ref, size_bytes=size_bytes, state=state,
        storage_key=f"items/{item.id}/{media_id}", received_at=received_at or item.last_message_at,
    )
    db.add(media)
    db.flush()
    return media


def record_fetch(db: Session, source: str, titles: list[str], always: bool = False) -> None:
    """One fetch_runs row per pull that added something (or every manual Refresh)."""
    if titles or always:
        db.add(FetchRun(source=source, added=len(titles), titles=titles[:20]))
        emit(db, "desk", "fetch.run", {"source": source, "added": len(titles), "titles": titles[:20]})


def schedule_finalize(db: Session, item: Item, delay: float = 0) -> None:
    """Finalize after the quiet period. A later message schedules a later finalize; the earlier one then does nothing."""
    enqueue(db, "finalize_item", {"item_id": str(item.id), "after": item.last_message_at.isoformat()},
            delay=delay, max_attempts=6)


def slug_for(db: Session, item: Item) -> str:
    reporter = item.reporter
    if reporter is None:
        raise SlugError("Unknown sender: pick a reporter to build the slug.")
    subject = item.slug_subject or item.headline or item.body
    slug = build_slug(item.received_at, item.city_code or reporter.city_code, subject, item.story_type or get_settings().default_story_type,
                      reporter.slug_code, get_settings().slug_subject_max_words)
    taken = set(db.scalars(select(Item.slug).where(Item.slug.like(f"{slug}%"), Item.id != item.id)).all())
    return unique_slug(slug, taken)


def finalize(db: Session, item: Item) -> None:
    """Build the slug and queue the FTP upload, or park the item for the desk."""
    waiting = db.scalar(select(func.count()).select_from(Media).where(Media.item_id == item.id, Media.state.in_(WAITING_STATES)))
    if waiting:
        raise RetryLater(15, f"{waiting} file(s) still arriving")
    if not item.slug:
        try:
            item.slug = slug_for(db, item)
        except SlugError as e:
            item.intake_state, item.intake_note = "needs_desk", str(e)
            item_changed(db, item)
            return
    item.intake_state, item.intake_note = "uploading", None
    enqueue(db, "ftp_upload", {"item_id": str(item.id)}, dedupe_key=f"ftp_upload:{item.id}")
    item_changed(db, item)
