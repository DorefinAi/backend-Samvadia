r"""What every channel shares: find or create the item, add messages and files, build the slug,
and hand the item to the FTP upload job.

    message/file arrives -> item (collecting) -> finalize -> slug -> uploading -> on_ftp
                                                       \-> needs_desk (reporter or subject missing)

Every email and WhatsApp message is also read for the slug the reporter typed (app/services/reporter_slug.py),
the district and the item kind (app/services/triage.py). With a reporter's slug the folder name comes from it,
so an unknown sender's story goes to FTP without waiting for the desk, and the FTP footage matcher can look for
the reporter's own upload (app/services/footage.py).
"""
import logging
import random
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.events import emit, item_changed
from app.jobs import RetryLater, enqueue
from app.models import Audit, FetchRun, Item, Location, Media, Message, Reporter, ReporterEmail
from app.services import districts as district_service
from app.services import footage
from app.services.filenames import kind_of
from app.services.reporter_slug import folder_slug, parse
from app.services.slug import IST, STORY_TYPES, SlugError, build_slug, with_suffix
from app.services.triage import classify
from app.services.whatsapp import digits

log = logging.getLogger("intake")

HEADLINE_MAX = 160
EMAIL_THREAD_WINDOW = timedelta(days=3)
WAITING_STATES = ("awaiting_upload", "downloading")
# Not a verdict yet: an email reply in the thread still joins the item (held = parked, sent_back = waiting for
# exactly that reply)
OPEN_REVIEW_STATES = ("pending", "held", "sent_back")


def find_reporter(db: Session, *, phone: str | None = None, email: str | None = None) -> Reporter | None:
    if phone:
        return db.scalar(select(Reporter).where(Reporter.phone == digits(phone), Reporter.active))
    if email:
        address = email.strip().lower()
        if not address:
            return None
        listed = select(ReporterEmail.reporter_id).where(ReporterEmail.email == address)
        return db.scalar(select(Reporter).where(Reporter.active, or_(Reporter.email == address, Reporter.id.in_(listed)))
                         .order_by(Reporter.created_at).limit(1))
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
        district_code=reporter.district_code if reporter else None,
    )
    db.add(item)
    db.flush()
    db.refresh(item)  # loads the reporter and district relationships
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
        if item.review_state == "sent_back":
            _reply_reopens(db, item)
    message = Message(item_id=item.id, source=source, external_id=external_id, sender_contact=sender_contact,
                      text=text, raw=raw, received_at=received_at)
    db.add(message)
    db.flush()
    safe_annotate(db, item, subject=subject, text=text, sender_address=sender_contact, sender_name=sender_name,
                  created=created)
    return item, message, created


def reporter_copy(db: Session, item: Item) -> tuple[str, str]:
    """The headline and body as the reporter sent them, before any desk edit (for decisions.headline_changed and
    body_edit_distance on approve).
    - WhatsApp and email: rebuilt from the item's messages with the same rule add_message uses (headline = the
      first subject, else the first line of text; bodies joined by a blank line), so a reply that lands after a
      desk edit still counts as the reporter's copy, not the desk's. Messages are taken in received_at order;
      add_message saw them in arrival order, which differs only if a channel delivers out of order.
    - Portal (no messages): the text before the desk's first edit (the earliest item.edit audit row), else the
      item as it is now (never edited)."""
    messages = sorted(item.messages, key=lambda m: (m.received_at, m.created_at))
    if messages:
        headline, body = "", None
        for m in messages:
            subject = (m.raw or {}).get("subject") if isinstance(m.raw, dict) else None
            if not headline:
                headline = ((subject if isinstance(subject, str) else None) or first_line(m.text))[:HEADLINE_MAX]
            if body is None:
                body = m.text
            elif m.text:
                body = f"{body}\n\n{m.text}".strip()
        return headline, body or ""
    first_edit = db.scalar(select(Audit).where(Audit.action == "item.edit", Audit.target_type == "item",
                                               Audit.target_id == str(item.id)).order_by(Audit.id).limit(1))
    if first_edit is not None and first_edit.before:
        return first_edit.before.get("headline") or "", first_edit.before.get("body") or ""
    return item.headline, item.body


def _reply_reopens(db: Session, item: Item) -> None:
    """The reporter answered a send-back: the item is back in the desk's pending queue. No decisions row (those
    are the desk's labels and need a user); the audit log keeps what was asked, and so does the needs_more row."""
    db.add(Audit(actor_id=None, action="item.reply_reopen", target_type="item", target_id=str(item.id),
                 before={"review_state": item.review_state, "reject_note": item.reject_note},
                 after={"review_state": "pending"}))
    item.review_state, item.reject_note = "pending", None
    item.version += 1


def safe_annotate(db: Session, item: Item, **kwargs) -> bool:
    """annotate() in a savepoint: if reading the slug, district or kind fails, the message is still stored,
    just without them (logged). Returns False on failure."""
    try:
        with db.begin_nested():
            annotate(db, item, **kwargs)
        return True
    except Exception:
        log.exception("could not read slug/district/kind for item %s; stored without them", item.id)
        return False


def annotate(db: Session, item: Item, *, subject: str | None, text: str | None, sender_address: str | None = None,
             sender_name: str | None = None, created: bool = True,
             districts: list[district_service.District] | None = None) -> None:
    """Fill reporter_slug, slug_key, parsed, district and kind from one message, then start the footage search.
    The slug fields are set by the first message that carries a slug (WhatsApp: the slug may come after the photo)."""
    districts = districts if districts is not None else district_service.load(db)
    line = first_line(text or "")
    found_slug = False
    if not item.reporter_slug and item.source != "portal":  # portal reports have no typed slug
        parsed = parse(subject, line, received=item.received_at.astimezone(IST).date(),
                       aliases=district_service.all_aliases(districts), hindi=district_service.hindi_names(districts))
        if parsed:
            item.reporter_slug, item.slug_key, item.parsed = parsed.reporter_slug, parsed.slug_key, parsed.stored()
            found_slug = True
            if parsed.type in STORY_TYPES and not item.story_type:
                item.story_type = parsed.type
    if found_slug or item.district_code is None:
        assign_district(db, item, districts)
    if created or found_slug:
        item.kind = classify(source=item.source, subject=subject, first_line=line, sender_address=sender_address,
                             sender_name=sender_name, known_reporter=item.reporter_id is not None,
                             has_slug=bool(item.reporter_slug))
    footage.start(item)


def assign_district(db: Session, item: Item, districts: list[district_service.District] | None = None) -> None:
    """The slug's city token first, then the reporter's district, then the reporter's (or the item's) city."""
    districts = districts if districts is not None else district_service.load(db)
    codes = {d.code for d in districts}
    found = district_service.match_token((item.parsed or {}).get("city_token"), districts)
    code = found.code if found else None
    reporter = item.reporter
    for candidate in (reporter.district_code if reporter else None, reporter.city_code if reporter else None,
                      item.city_code):
        if code is None and candidate in codes:
            code = candidate
    if code and code != item.district_code:
        item.district_code = code
        item.district = db.get(Location, code)


def reporter_linked(db: Session, item: Item) -> None:
    """The desk picked a reporter: a known reporter's mail is never not_a_story, and their district applies when
    the slug named none."""
    if item.reporter_id and item.kind == "not_a_story":
        item.kind = "story"
    assign_district(db, item)
    footage.start(item)


def _open_item(db: Session, source: str, conversation_key: str, received_at: datetime) -> Item | None:
    q = select(Item).where(Item.source == source, Item.conversation_key == conversation_key)
    if source == "whatsapp":
        window = timedelta(seconds=get_settings().whatsapp_merge_seconds)
        q = q.where(Item.intake_state == "collecting", Item.last_message_at >= received_at - window)
    else:  # email: replies in the same thread within 3 days, while the desk has not decided
        q = q.where(Item.review_state.in_(OPEN_REVIEW_STATES), Item.received_at >= received_at - EMAIL_THREAD_WINDOW)
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


def unique_slug_ci(db: Session, item: Item, base: str) -> str:
    """base, or base_2, base_3 ... compared without letter case: the newsroom FTP runs on Windows."""
    pattern = re.sub(r"([\\%_])", r"\\\1", base[:80]) + "%"
    taken = {s.lower() for s in db.scalars(select(Item.slug).where(Item.slug.ilike(pattern), Item.id != item.id))}
    n = 1
    while with_suffix(base, n).lower() in taken:
        n += 1
    return with_suffix(base, n)


def slug_for(db: Session, item: Item) -> str:
    """The folder-safe form of the slug the reporter typed (ASCII, no reporter needed); only without one, the
    house slug built from the reporter, city, subject and type."""
    base = folder_slug(item.reporter_slug) if item.reporter_slug else None
    if base:
        return unique_slug_ci(db, item, base[:93].rstrip("_"))  # room for _NN within 96
    reporter = item.reporter
    if reporter is None:
        raise SlugError("Unknown sender: pick a reporter to build the slug.")
    subject = item.slug_subject or item.headline or item.body
    slug = build_slug(item.received_at, item.city_code or reporter.city_code, subject, item.story_type or get_settings().default_story_type,
                      reporter.slug_code, get_settings().slug_subject_max_words)
    return unique_slug_ci(db, item, slug)


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
