"""Assignment desk: inbox, edit, reporter picker, approve / reject / reopen, retries, districts,
FTP footage (recheck, set by hand, read-only browse)."""
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.events import item_changed
from app.intake import reporter_copy, reporter_linked, same_slug_ci, schedule_finalize, slug_for
from app.jobs import enqueue, now
from app.api.reporter import clean_tags
from app.models import (
    CATEGORIES, FOOTAGE_STATES, ITEM_KINDS, PRIORITIES, REVIEW_STATES, STORY_TYPES, ApproveReason, Audit, Decision,
    DecisionAction, FeedLog, FetchRun, Item, Location, Media, RejectReason, Reporter, ReporterEmail, Story,
)
from app.security import ANY_DESK_PAGE, ITEM_PAGES, Caller, require
from app.serialize import district_out, item_out, reporter_out, story_out
from app.services import districts as district_service
from app.services import edits
from app.services import footage, ftp
from app.services.slug import SlugError, slug_error
from app.services.whatsapp import digits
from app.stories import collected_at_subquery, create_story, latest_story

router = APIRouter(prefix="/desk", tags=["assignment desk"])


def _item(db: Session, item_id: uuid.UUID, lock: bool = False) -> Item:
    item = db.get(Item, item_id, with_for_update=lock)
    if item is None:
        raise HTTPException(404, "Item not found.")
    return item


def _snapshot(item: Item) -> dict:
    return item_out(item, full=True)


def _refuse_slug_change(db: Session, item: Item, old_slug: str | None) -> None:
    """Once a story exists, Octopus holds (or is about to hold) its slug as the title and the newsroom opens
    the FTP folder of that name, so the slug and the folder are frozen. Text changes go out as an UPDATE."""
    if old_slug and item.slug != old_slug and latest_story(db, item) is not None:
        raise HTTPException(409, f"This story is in the Octopus feed as {old_slug}, so its slug can't change. "
                                 "Send the change as an update instead.")


# --- inbox ------------------------------------------------------------------------------------

def _csv(value: str | None, allowed: tuple[str, ...] | None, name: str, upper: bool = False) -> list[str]:
    """A comma list from the query string; 422 on a value that is not allowed."""
    out = [v.strip().upper() if upper else v.strip() for v in (value or "").split(",") if v.strip()]
    if allowed is not None:
        bad = [v for v in out if v not in allowed]
        if bad:
            raise HTTPException(422, f"{name} must be a comma list of {', '.join(allowed)}; got {', '.join(bad)}.")
    return out


def _aware(dt: datetime | None, name: str) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        raise HTTPException(422, f"{name} needs a time zone offset, e.g. 2026-10-27T00:00:00+05:30.")
    return dt


@router.get("/items")
def list_items(
    review_state: str | None = None, intake_state: str | None = None, source: str | None = None,
    q: str | None = None, before: datetime | None = None, limit: int = Query(50, ge=1, le=200), full: bool = False,
    kind: str | None = Query(None, description="comma list of story, press_release, day_plan, not_a_story, or all; "
                                                 "default story"),
    from_: datetime | None = Query(None, alias="from", description="received_at >= from (ISO with offset)"),
    to: datetime | None = Query(None, description="received_at < to (ISO with offset)"),
    district: str | None = Query(None, description="comma list of district codes"),
    footage_status: str | None = Query(None, alias="footage", description="comma list of footage statuses"),
    caller: Caller = Depends(require("desk.view", ITEM_PAGES)), db: Session = Depends(get_db),
):
    """full=true adds each item's text, files and messages, so the desk can open any of them without another call;
    it needs page.inbox (403 otherwise), and so does matching q against the story text (without it q skips the body).
    review_state is one state or a comma list (e.g. pending,held).
    counts (one per review state, plus all) use the same kind, from, to and district filters;
    kind_counts covers the from/to window only."""
    inbox = caller.can("page.inbox")
    if full and not inbox:
        raise HTTPException(403, "You cannot open the inbox, so full=true (story text, files, messages) is refused.")
    kinds = ITEM_KINDS if (kind or "").strip().lower() == "all" else (_csv(kind, ITEM_KINDS, "kind") or ["story"])
    districts = _csv(district, None, "district", upper=True)
    footages = _csv(footage_status, FOOTAGE_STATES, "footage")
    _aware(from_, "from")
    _aware(to, "to")
    review_states = _csv(review_state, REVIEW_STATES, "review_state")

    base = [Item.intake_state != "collecting" if not intake_state else Item.intake_state == intake_state]
    if from_:
        base.append(Item.received_at >= from_)
    if to:
        base.append(Item.received_at < to)
    window = list(base)
    base.append(Item.kind.in_(kinds))
    if districts:
        base.append(Item.district_code.in_(districts))

    stmt = select(Item).where(*base)
    if review_states:
        stmt = stmt.where(Item.review_state.in_(review_states))
    if footages:
        stmt = stmt.where(Item.footage_status.in_(footages))
    if source:
        stmt = stmt.where(Item.source == source)
    if q:
        like = f"%{q}%"
        fields = [Item.headline, Item.slug, Item.sender_name, Item.reporter_slug] + ([Item.body] if inbox else [])
        stmt = stmt.where(or_(*(f.ilike(like) for f in fields)))
    if before:
        stmt = stmt.where(Item.received_at < before)
    if full:
        stmt = stmt.options(selectinload(Item.media), selectinload(Item.messages))
    items = db.scalars(stmt.order_by(Item.received_at.desc()).limit(limit)).all()

    by_state = dict(db.execute(select(Item.review_state, func.count()).where(*base).group_by(Item.review_state)).all())
    by_kind = dict(db.execute(select(Item.kind, func.count()).where(*window).group_by(Item.kind)).all())
    return {"items": [item_out(i, full=full) for i in items],
            "next_before": items[-1].received_at.isoformat() if len(items) == limit else None,
            "counts": {**{s: by_state.get(s, 0) for s in REVIEW_STATES}, "all": sum(by_state.values())},
            "kind_counts": {k: by_kind.get(k, 0) for k in ITEM_KINDS}}


@router.get("/items/{item_id}")
def get_item(item_id: uuid.UUID, caller: Caller = Depends(require("desk.view", ("page.inbox",))),
             db: Session = Depends(get_db)):
    return {"item": item_out(_item(db, item_id), full=True)}


class ItemPatch(BaseModel):
    version: int
    headline: str | None = Field(default=None, max_length=160)
    body: str | None = Field(default=None, max_length=20000)
    slug_subject: str | None = Field(default=None, max_length=80)
    story_type: str | None = None
    reporter_id: uuid.UUID | None = None
    category: str | None = None
    priority: str | None = None
    tags: list[str] | None = Field(default=None, max_length=10)
    city_code: str | None = None  # this story's location, if not the reporter's city
    slug: str | None = None  # set by hand; must pass the slug check, and stops automatic rebuilds


# Columns that are NOT NULL on items: leaving the field out keeps it, an explicit null is a 422 (not a 500)
_NOT_NULL_FIELDS = ("headline", "body", "priority", "tags")


@router.patch("/items/{item_id}")
def edit_item(item_id: uuid.UUID, patch: ItemPatch, caller: Caller = Depends(require("desk.review")),
              db: Session = Depends(get_db)):
    """Edit the draft. `version` must match, so two editors cannot overwrite each other (409)."""
    item = _item(db, item_id, lock=True)
    if patch.version != item.version:
        raise HTTPException(409, "Someone else changed this story. Reload it and try again.")
    nulls = [f for f in _NOT_NULL_FIELDS if f in patch.model_fields_set and getattr(patch, f) is None]
    if nulls:
        raise HTTPException(422, f"{', '.join(nulls)} cannot be null; leave the field out to keep it "
                                 "(an empty headline or body is \"\").")
    if patch.story_type is not None and patch.story_type not in STORY_TYPES:
        raise HTTPException(422, f"story_type must be one of {', '.join(STORY_TYPES)}.")
    if patch.category is not None and patch.category not in CATEGORIES:
        raise HTTPException(422, f"category must be one of {', '.join(CATEGORIES)}.")
    if patch.priority is not None and patch.priority not in PRIORITIES:
        raise HTTPException(422, f"priority must be one of {', '.join(PRIORITIES)}.")
    if patch.city_code == "":
        patch.city_code = None  # back to the reporter's city
    if patch.city_code:
        patch.city_code = patch.city_code.upper()
        if not db.scalar(select(Location.code).where(Location.code == patch.city_code, Location.active)):
            raise HTTPException(422, f"Unknown location code {patch.city_code}.")
    if patch.tags is not None:
        patch.tags = clean_tags(patch.tags)
    before = _snapshot(item)
    fields = patch.model_dump(exclude_unset=True, exclude={"version", "slug"})
    if "reporter_id" in fields and fields["reporter_id"] is not None and db.get(Reporter, fields["reporter_id"]) is None:
        raise HTTPException(422, "Unknown reporter.")
    for key, value in fields.items():
        setattr(item, key, value)
    db.flush()
    db.refresh(item)  # reload the reporter relationship
    if {"reporter_id", "city_code"} & fields.keys():
        reporter_linked(db, item)  # kind (a known reporter's mail is a story), district, footage search

    old_slug, old_folder = item.slug, item.ftp_folder
    slug_inputs_changed = bool({"slug_subject", "story_type", "reporter_id", "city_code"} & fields.keys())
    if patch.slug is not None:
        error = slug_error(patch.slug)
        if error:
            raise HTTPException(422, error)
        item.slug, item.slug_edited = same_slug_ci(db, item, patch.slug), True  # a slug in use shares its folder
    elif slug_inputs_changed and item.reporter_id and not item.slug_edited:
        try:
            item.slug = slug_for(db, item)
        except SlugError as e:
            raise HTTPException(422, str(e))
    _refuse_slug_change(db, item, old_slug)

    # A reporter lets finalize build the slug; a hand-typed slug is enough on its own (unknown senders)
    if item.intake_state == "needs_desk" and (item.reporter_id or item.slug):
        schedule_finalize(db, item)  # builds the slug (if needed) and uploads
    elif old_folder and item.slug != old_slug:
        enqueue(db, "ftp_rename", {"item_id": str(item.id), "old_folder": old_folder})

    item.version += 1
    db.add(Audit(actor_id=caller.user.id, action="item.edit", target_type="item", target_id=str(item.id),
                 before=before, after=_snapshot(item)))
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item, full=caller.can("page.inbox"))}


@router.post("/items/{item_id}/slug/reset")
def reset_slug(item_id: uuid.UUID, caller: Caller = Depends(require("desk.review")), db: Session = Depends(get_db)):
    """Drop a hand-typed slug and rebuild it from the reporter, city, subject and type."""
    item = _item(db, item_id, lock=True)
    before, old_slug, old_folder = _snapshot(item), item.slug, item.ftp_folder
    item.slug_edited = False
    try:
        item.slug = slug_for(db, item)
    except SlugError as e:
        raise HTTPException(422, str(e))
    _refuse_slug_change(db, item, old_slug)
    if old_folder and item.slug != old_slug:
        enqueue(db, "ftp_rename", {"item_id": str(item.id), "old_folder": old_folder})
    item.version += 1
    db.add(Audit(actor_id=caller.user.id, action="item.slug_reset", target_type="item", target_id=str(item.id),
                 before=before, after=_snapshot(item)))
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item, full=caller.can("page.inbox"))}


# --- decisions --------------------------------------------------------------------------------

# Every decision is a training label (models.Decision). How the item's review_state moves:
#   pending -> approved | rejected | held | sent_back | duplicate
#   held    -> approved | rejected | sent_back | duplicate   (hold parks a story; it is not a verdict)
#   rejected | held | sent_back | duplicate -> pending        (reopen; an approved story is already in the feed)
#   sent_back -> pending also when the reporter's email reply lands in the thread (app/intake.py, audit only)

MAX_SECONDS_ON_ITEM = 86400


class DecisionIn(BaseModel):
    """What any decision may carry. The body is optional, so an older desk that sends nothing still works."""
    note: str | None = Field(default=None, max_length=1000)
    seconds_on_item: int | None = None  # from opening the item to deciding; clamped to 0..86400
    opened_at: datetime | None = None  # used when seconds_on_item is not sent (ISO with offset)


class RejectIn(DecisionIn):
    reason: str  # a reject_reasons code


class ApproveIn(DecisionIn):
    approve_reason: str | None = None  # an approve_reasons code
    importance: int | None = Field(default=None, ge=1, le=5)


class DuplicateIn(DecisionIn):
    duplicate_of: uuid.UUID


def _seconds(body: DecisionIn | None) -> int | None:
    if body is None:
        return None
    if body.seconds_on_item is not None:
        seconds = body.seconds_on_item
    elif body.opened_at is not None:
        seconds = int((now() - _aware(body.opened_at, "opened_at")).total_seconds())
    else:
        return None
    return max(0, min(MAX_SECONDS_ON_ITEM, seconds))


def _note(body: DecisionIn | None) -> str | None:
    return ((body.note if body else None) or "").strip() or None


def _from(item: Item, allowed: tuple[str, ...], done: str) -> None:
    """409 unless the item is in one of the allowed review states."""
    if item.review_state not in allowed:
        allowed_text = " or ".join(allowed)
        raise HTTPException(409, f"This story is {item.review_state}; only a {allowed_text} story can be {done}.")


def _known(db: Session, model, code: str | None, field: str) -> None:
    """422 unless code is an active row of the lookup table (reject_reasons / approve_reasons)."""
    if code is None or db.scalar(select(model.code).where(model.code == code, model.active)):
        return
    codes = db.scalars(select(model.code).where(model.active).order_by(model.sort_order)).all()
    raise HTTPException(422, f"{field} must be one of {', '.join(codes)}.")


def _decide(db: Session, caller: Caller, item: Item, action: str, snapshot: dict, body: DecisionIn | None = None,
            *, reject_reason: str | None = None, approve_reason: str | None = None, importance: int | None = None,
            duplicate_of: uuid.UUID | None = None, story: Story | None = None):
    """Record the decision row and finish the request. snapshot is the item as the desk saw it, taken by the
    endpoint before it changed anything (review_state, reasons, duplicate_of, the story): the label's input must
    not contain the label."""
    if not db.scalar(select(DecisionAction.code).where(DecisionAction.code == action, DecisionAction.active)):
        raise HTTPException(409, f"The {action} action is switched off.")
    decision = Decision(
        org_id=get_settings().org_id, item_id=item.id, action=action, reject_reason=reject_reason,
        approve_reason=approve_reason, importance=importance, duplicate_of=duplicate_of, note=_note(body),
        user_id=caller.user.id, snapshot=snapshot, seconds_on_item=_seconds(body), was_blind=item.blind)
    if action == "approve":  # the copy as it went out
        decision.headline_final, decision.body_final, decision.slug_final = item.headline, item.body, item.slug
        headline, body_text = reporter_copy(db, item)
        decision.headline_changed = edits.headline_changed(headline, item.headline)
        decision.body_edit_distance = edits.edit_distance(body_text, item.body)  # None for a huge rewrite
    db.add(decision)
    item.reviewed_by, item.reviewed_at = caller.user.id, now()
    item.version += 1
    item_changed(db, item)
    db.commit()
    out = {"item": item_out(item, full=caller.can("page.inbox"))}
    if story:
        out["story"] = story_out(story)
    return out


@router.get("/meta")
def meta(caller: Caller = Depends(require("desk.view", ("page.inbox",))), db: Session = Depends(get_db)):
    """The desk's pick lists with English and Hindi labels: active rows only, in sort_order."""
    def rows(model, *extra: str) -> list[dict]:
        stmt = select(model).where(model.active).order_by(model.sort_order, model.code)
        return [{"code": r.code, "label_en": r.label_en, "label_hi": r.label_hi, **{k: getattr(r, k) for k in extra}}
                for r in db.scalars(stmt)]
    return {"actions": rows(DecisionAction, "target"), "reject_reasons": rows(RejectReason, "owner"),
            "approve_reasons": rows(ApproveReason), "review_states": list(REVIEW_STATES),
            "story_types": list(STORY_TYPES), "categories": list(CATEGORIES), "priorities": list(PRIORITIES),
            "max_seconds_on_item": MAX_SECONDS_ON_ITEM}


@router.post("/items/{item_id}/approve")
def approve(item_id: uuid.UUID, body: ApproveIn | None = None, caller: Caller = Depends(require("desk.review")),
            db: Session = Depends(get_db)):
    """Approve = publish. The story enters the Octopus feed as soon as the item's files are on FTP.
    Optional body: approve_reason, importance 1-5, note, seconds_on_item / opened_at."""
    body = body or ApproveIn()
    _known(db, ApproveReason, body.approve_reason, "approve_reason")
    item = _item(db, item_id, lock=True)
    _from(item, ("pending", "held"), "approved")
    if not item.slug:
        raise HTTPException(409, item.intake_note or "The story has no slug yet.")
    if not item.body.strip():
        raise HTTPException(409, "The story has no text.")
    snapshot = _snapshot(item)
    item.review_state, item.reject_reason, item.reject_note, item.duplicate_of = "approved", None, None, None
    story = create_story(db, item, caller.user, "original")
    return _decide(db, caller, item, "approve", snapshot, body, approve_reason=body.approve_reason,
                   importance=body.importance, story=story)


@router.post("/items/{item_id}/reject")
def reject(item_id: uuid.UUID, body: RejectIn, caller: Caller = Depends(require("desk.review")),
           db: Session = Depends(get_db)):
    """reason: a reject_reasons code; 'other' needs a note saying why."""
    _known(db, RejectReason, body.reason, "reason")
    if body.reason == "other" and not _note(body):
        raise HTTPException(422, "Reason 'other' needs a note saying why.")
    item = _item(db, item_id, lock=True)
    _from(item, ("pending", "held"), "rejected")
    snapshot = _snapshot(item)
    item.review_state, item.reject_reason, item.reject_note = "rejected", body.reason, _note(body)
    return _decide(db, caller, item, "reject", snapshot, body, reject_reason=body.reason)


@router.post("/items/{item_id}/hold")
def hold(item_id: uuid.UUID, body: DecisionIn | None = None, caller: Caller = Depends(require("desk.review")),
         db: Session = Depends(get_db)):
    """Park a pending story (review_state held). From there: approve, reject, send back, duplicate or reopen."""
    item = _item(db, item_id, lock=True)
    _from(item, ("pending",), "held")
    snapshot = _snapshot(item)
    item.review_state = "held"
    return _decide(db, caller, item, "hold", snapshot, body)


@router.post("/items/{item_id}/send-back")
def send_back(item_id: uuid.UUID, body: DecisionIn | None = None, caller: Caller = Depends(require("desk.review")),
              db: Session = Depends(get_db)):
    """Back to the reporter for more (action needs_more, review_state sent_back). note: what the reporter
    should add; it is kept on the item as reject_note, which the reporter portal shows."""
    item = _item(db, item_id, lock=True)
    _from(item, ("pending", "held"), "sent back")
    snapshot = _snapshot(item)
    item.review_state, item.reject_reason, item.reject_note = "sent_back", None, _note(body)
    return _decide(db, caller, item, "needs_more", snapshot, body)


@router.post("/items/{item_id}/duplicate")
def duplicate(item_id: uuid.UUID, body: DuplicateIn, caller: Caller = Depends(require("desk.review")),
              db: Session = Depends(get_db)):
    """Mark this item a duplicate of another story (review_state duplicate, items.duplicate_of set).
    The target must not itself be a duplicate (409), so there are no chains or A <-> B cycles."""
    if body.duplicate_of == item_id:
        raise HTTPException(422, "A story cannot be a duplicate of itself.")
    # Both rows locked, always in id order: two editors marking A -> B and B -> A at once cannot deadlock,
    # and the second one sees the first one's duplicate state.
    rows = {i: db.get(Item, i, with_for_update=True) for i in sorted((item_id, body.duplicate_of))}
    item, target = rows[item_id], rows[body.duplicate_of]
    if item is None:
        raise HTTPException(404, "Item not found.")
    if target is None or target.org_id != item.org_id:
        raise HTTPException(422, "duplicate_of is not a known story.")
    _from(item, ("pending", "held"), "marked a duplicate")
    if target.review_state == "duplicate" or target.duplicate_of is not None:
        root = f" of {target.duplicate_of}" if target.duplicate_of else ""
        raise HTTPException(409, f"That story is itself a duplicate{root}. Mark this one a duplicate of the "
                                 "original instead.")
    # The other end of a chain: this item is already the original of others (B -> A, then A -> C).
    # Safe against a concurrent B -> A: that call locks this row too, so the two are serialised.
    pointing = db.scalars(select(Item.id).where(Item.duplicate_of == item.id).order_by(Item.id).limit(1)).first()
    if pointing is not None:
        raise HTTPException(409, f"Other stories (e.g. {pointing}) are marked duplicates of this one, so it cannot "
                                 "become a duplicate itself. Reopen those first, or mark the other story a duplicate "
                                 "of this one instead.")
    snapshot = _snapshot(item)
    item.review_state, item.reject_reason, item.reject_note = "duplicate", None, _note(body)
    item.duplicate_of = target.id
    return _decide(db, caller, item, "duplicate", snapshot, body, duplicate_of=target.id)


@router.post("/items/{item_id}/reopen")
def reopen(item_id: uuid.UUID, body: DecisionIn | None = None, caller: Caller = Depends(require("desk.review")),
           db: Session = Depends(get_db)):
    """Undo a reject, hold, send-back or duplicate: back to pending (left out of training).
    An approved story is already in the feed, so it cannot be reopened: update or kill it instead (409)."""
    item = _item(db, item_id, lock=True)
    _from(item, ("rejected", "held", "sent_back", "duplicate"), "reopened")
    snapshot = _snapshot(item)
    item.review_state, item.reject_reason, item.reject_note, item.duplicate_of = "pending", None, None, None
    return _decide(db, caller, item, "reopen", snapshot, body)


# --- retries and refresh ----------------------------------------------------------------------

@router.post("/items/{item_id}/retry-ftp")
def retry_ftp(item_id: uuid.UUID, caller: Caller = Depends(require("ftp.manage")), db: Session = Depends(get_db)):
    item = _item(db, item_id, lock=True)
    if not item.slug:
        raise HTTPException(409, item.intake_note or "The story has no slug yet.")
    item.intake_state, item.intake_note = "uploading", None
    enqueue(db, "ftp_upload", {"item_id": str(item.id)}, dedupe_key=f"ftp_upload:{item.id}")
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item)}


@router.post("/media/{media_id}/retry")
def retry_media(media_id: uuid.UUID, caller: Caller = Depends(require("desk.review")), db: Session = Depends(get_db)):
    media = db.get(Media, media_id)
    if media is None:
        raise HTTPException(404, "File not found.")
    if media.state != "failed" or media.source == "portal":
        raise HTTPException(409, "Only a failed WhatsApp or email download can be retried.")
    media.state, media.error = "downloading", None
    task = "fetch_whatsapp_media" if media.source == "whatsapp" else "fetch_mail_attachment"
    enqueue(db, task, {"media_id": str(media.id)})
    item = db.get(Item, media.item_id)
    if item.intake_state in ("on_ftp", "ftp_failed"):
        # back to collecting, or finalize skips the item and the file never reaches FTP
        item.intake_state, item.intake_note = "collecting", None
    schedule_finalize(db, item, delay=5)
    db.commit()
    return {"ok": True}


@router.post("/refresh")
def refresh(caller: Caller = Depends(require("desk.fetch")), db: Session = Depends(get_db)):
    """Check the mailbox now instead of waiting for the next minute. WhatsApp arrives by itself."""
    if get_settings().mail_enabled:
        enqueue(db, "fetch_mail", {"manual": True}, dedupe_key="fetch_mail")
    db.commit()
    return {"ok": True, "mail": get_settings().mail_enabled}


# --- reporters (the picker and the reporter list) ---------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class ReporterIn(BaseModel):
    """POST and PUT. New fields: district_code, emails[] (several addresses). The old ones still work: city_code
    (defaults to district_code), email (one address). When both are sent, emails[] wins over email.
    Unknown fields are ignored."""
    name: str = Field(min_length=1, max_length=120)
    slug_code: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    city_code: str | None = Field(default=None, max_length=8)
    district_code: str | None = Field(default=None, max_length=8)
    district: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    emails: list[str] | None = Field(default=None, max_length=20)
    active: bool = True


class ReporterPatch(BaseModel):
    """PATCH: only the fields sent change."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug_code: str | None = Field(default=None, min_length=2, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    city_code: str | None = Field(default=None, max_length=8)
    district_code: str | None = Field(default=None, max_length=8)
    district: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    emails: list[str] | None = Field(default=None, max_length=20)
    active: bool | None = None


@router.get("/reporters")
def list_reporters(q: str | None = None, all: bool = Query(False, description="include inactive reporters"),
                   district: str | None = None, limit: int = Query(100, ge=1, le=1000),
                   caller: Caller = Depends(require("desk.view", ("page.reporters", "page.inbox"))),
                   db: Session = Depends(get_db)):
    stmt = select(Reporter)
    if not all:
        stmt = stmt.where(Reporter.active)
    if district:
        stmt = stmt.where(Reporter.district_code.in_(_csv(district, None, "district", upper=True)))
    if q:
        like = f"%{q}%"
        listed = select(ReporterEmail.reporter_id).where(ReporterEmail.email.ilike(like))
        stmt = stmt.where(or_(Reporter.name.ilike(like), Reporter.phone.ilike(f"%{digits(q) or q}%"),
                              Reporter.email.ilike(like), Reporter.id.in_(listed), Reporter.district.ilike(like),
                              Reporter.slug_code.ilike(like), Reporter.district_code.ilike(like)))
    return {"reporters": [reporter_out(r) for r in db.scalars(stmt.order_by(Reporter.name).limit(limit))]}


def _code(db: Session, value: str | None, field: str) -> str | None:
    code = (value or "").strip().upper() or None
    if code and not db.scalar(select(Location.code).where(Location.code == code)):
        raise HTTPException(422, f"Unknown {field} {code}.")
    return code


def _clean_emails(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        email = (raw or "").strip().lower()
        if not email:
            continue
        if not EMAIL_RE.match(email):
            raise HTTPException(422, f"Not an email address: {email}")
        if email not in out:
            out.append(email)
    return out


def _set_emails(db: Session, reporter: Reporter, emails: list[str]) -> None:
    """Replace the reporter's addresses; 409 when another reporter already has one of them."""
    if emails:
        taken = db.scalars(select(Reporter.name).where(Reporter.id != reporter.id, or_(
            Reporter.email.in_(emails),
            Reporter.id.in_(select(ReporterEmail.reporter_id).where(ReporterEmail.email.in_(emails)))))).all()
        if taken:
            raise HTTPException(409, f"That email address already belongs to {taken[0]}.")
    keep = {e.email: e for e in reporter.emails}
    reporter.emails[:] = [keep.get(email) or ReporterEmail(email=email) for email in emails]
    for position, row in enumerate(reporter.emails):
        row.position = position
    reporter.email = emails[0] if emails else None
    db.flush()


def _apply_reporter(db: Session, reporter: Reporter, values: dict) -> None:
    """values: the fields that were sent (ReporterIn / ReporterPatch .model_dump(exclude_unset=True))."""
    if "name" in values and values["name"] is not None:
        reporter.name = values["name"].strip()
    if "slug_code" in values and values["slug_code"] is not None:
        reporter.slug_code = values["slug_code"].upper()
    if "district_code" in values:
        reporter.district_code = _code(db, values["district_code"], "district_code")
    if "city_code" in values:
        city = (values["city_code"] or "").strip().upper() or None
        if city and not re.fullmatch(r"[A-Z]{2,5}", city):
            raise HTTPException(422, "city_code must be 2-5 letters.")
        reporter.city_code = city
    if not reporter.city_code:
        reporter.city_code = reporter.district_code  # the slug's city defaults to the district
    if "district" in values:
        reporter.district = (values["district"] or "").strip() or None
    if "phone" in values:
        phone = digits(values["phone"]) or None
        if phone and db.scalar(select(Reporter.id).where(Reporter.phone == phone, Reporter.id != reporter.id)):
            raise HTTPException(409, "Another reporter has that phone number.")
        reporter.phone = phone
    if values.get("emails") is not None:
        _set_emails(db, reporter, _clean_emails(values["emails"]))
    elif values.get("email"):
        # an older client sends one address: it becomes the first, the others stay
        first = _clean_emails([values["email"]])
        _set_emails(db, reporter, first + [e for e in reporter.email_list if e not in first])
    if "active" in values and values["active"] is not None:
        reporter.active = values["active"]


@router.post("/reporters")
def add_reporter(body: ReporterIn, caller: Caller = Depends(require("reporters.manage")), db: Session = Depends(get_db)):
    """The lead adds a new stringer from the reporter picker. Future messages from these phones/emails match
    automatically."""
    reporter = Reporter(org_id=get_settings().org_id, name=body.name.strip(), slug_code=body.slug_code.upper())
    db.add(reporter)
    _apply_reporter(db, reporter, body.model_dump(exclude_unset=True))
    db.flush()
    db.add(Audit(actor_id=caller.user.id, action="reporter.add", target_type="reporter", target_id=str(reporter.id),
                 after=reporter_out(reporter)))
    db.commit()
    return {"reporter": reporter_out(reporter)}


def _edit_reporter(db: Session, caller: Caller, reporter_id: uuid.UUID, values: dict) -> dict:
    reporter = db.get(Reporter, reporter_id)
    if reporter is None:
        raise HTTPException(404, "Reporter not found.")
    before = reporter_out(reporter)
    _apply_reporter(db, reporter, values)
    db.add(Audit(actor_id=caller.user.id, action="reporter.edit", target_type="reporter", target_id=str(reporter.id),
                 before=before, after=reporter_out(reporter)))
    db.commit()
    return {"reporter": reporter_out(reporter)}


@router.put("/reporters/{reporter_id}")
def update_reporter(reporter_id: uuid.UUID, body: ReporterIn, caller: Caller = Depends(require("reporters.manage")),
                    db: Session = Depends(get_db)):
    """Fields left out keep their value; emails[] wins over email when both are sent."""
    return _edit_reporter(db, caller, reporter_id, body.model_dump(exclude_unset=True))


@router.patch("/reporters/{reporter_id}")
def patch_reporter(reporter_id: uuid.UUID, body: ReporterPatch, caller: Caller = Depends(require("reporters.manage")),
                   db: Session = Depends(get_db)):
    return _edit_reporter(db, caller, reporter_id, body.model_dump(exclude_unset=True))


@router.get("/districts")
def list_districts(caller: Caller = Depends(require("desk.view", ANY_DESK_PAGE)), db: Session = Depends(get_db)):
    rows = db.scalars(select(Location).where(Location.active).order_by(Location.name))
    return {"districts": [district_out(r, folder=True) for r in rows]}


@router.get("/locations")
def locations(caller: Caller = Depends(require("desk.view", ("page.inbox",))), db: Session = Depends(get_db)):
    rows = db.scalars(select(Location).where(Location.active).order_by(Location.name))
    return {"locations": [{"code": l.code, "name": l.name} for l in rows],
            "categories": list(CATEGORIES), "priorities": list(PRIORITIES), "story_types": list(STORY_TYPES),
            "reject_reasons": db.scalars(select(RejectReason.code).where(RejectReason.active)
                                         .order_by(RejectReason.sort_order)).all()}  # labels: GET /desk/meta


@router.get("/fetch-runs")
def fetch_runs(limit: int = Query(30, le=200), caller: Caller = Depends(require("desk.view", ANY_DESK_PAGE)),
               db: Session = Depends(get_db)):
    rows = db.scalars(select(FetchRun).order_by(FetchRun.id.desc()).limit(limit))
    poll = get_settings().mail_poll_seconds if get_settings().mail_enabled else None
    return {"runs": [{"id": r.id, "source": r.source, "added": r.added, "titles": list(r.titles), "at": r.at.isoformat()}
                     for r in rows],
            "mail_poll_seconds": poll}


# --- FTP footage ------------------------------------------------------------------------------------

class FootageIn(BaseModel):
    path: str | None = Field(default=None, max_length=1000)
    version: int


def _connect():
    try:
        return ftp.connect()
    except Exception as e:
        raise HTTPException(502, f"The FTP server cannot be reached: {type(e).__name__}.")


def _footage_snapshot(item: Item) -> dict:
    return {"footage": footage.footage_out(item)}


@router.post("/items/{item_id}/footage/recheck")
def recheck_footage(item_id: uuid.UUID, caller: Caller = Depends(require("desk.review")),
                    db: Session = Depends(get_db)):
    """Run the matcher now, without the listing cache. A not_found story is reopened for another
    FOOTAGE_WAIT_HOURS. A manual pick is re-listed (files keep arriving)."""
    item = _item(db, item_id, lock=True)
    if item.footage_status != "manual" and not footage.eligible(item):
        reason = ("it is not a story" if item.kind != "story" else "it has no district" if item.district is None
                  else "its district has no FTP folder" if not item.district.ftp_folder
                  else "it has no reporter slug")
        raise HTTPException(409, f"Nothing to look for: {reason}. Pick the footage by hand instead.")
    if item.footage_status in ("not_found", "not_checked"):
        item.footage_status, item.footage_wait_until = "waiting", now() + footage.wait_hours()
    checked = (item.footage_status, item.footage_path)  # what this check is computed for
    db.commit()
    remote = _connect()
    try:
        match = footage.compute(footage.Lister(remote, ttl=0), item)
    except Exception as e:
        raise HTTPException(502, f"Reading the FTP server failed: {type(e).__name__}.")
    finally:
        remote.close()
    item = db.get(Item, item_id, with_for_update=True, populate_existing=True)
    # No lock was held while the FTP answered: if the desk picked (or changed, or cleared) a manual path
    # meanwhile, this result belongs to the old state; drop it, as footage_scan does.
    if "manual" in (checked[0], item.footage_status) and (item.footage_status, item.footage_path) != checked:
        db.rollback()
        return {"item": item_out(db.get(Item, item_id), full=caller.can("page.inbox"))}
    footage.apply(item, match, now())
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item, full=caller.can("page.inbox"))}


@router.put("/items/{item_id}/footage")
def set_footage(item_id: uuid.UUID, body: FootageIn, caller: Caller = Depends(require("desk.review")),
                db: Session = Depends(get_db)):
    """The desk picks the footage by hand (status manual), or clears its pick (path null: back to waiting)."""
    item = _item(db, item_id, lock=True)
    if body.version != item.version:
        raise HTTPException(409, "Someone else changed this story. Reload it and try again.")
    before = _footage_snapshot(item)
    if body.path is None or not body.path.strip():
        item.footage_status, item.footage_path, item.footage_files, item.footage_bytes = "not_checked", None, [], 0
        item.footage_wait_until = None
        footage.start(item)
        if item.footage_status == "waiting":
            item.footage_wait_until = max(item.received_at + footage.wait_hours(), now() + footage.wait_hours())
    else:
        path = _safe_path(body.path)
        district = footage.under_district(path, district_service.load(db))
        if district is None or path.count("/") < 2:
            raise HTTPException(422, "Pick a folder or file inside a district folder.")
        # Footage from another district's folder is almost always a mis-click; admins may still pick it.
        if item.district_code and district.code != item.district_code and not caller.can("ftp.manage"):
            raise HTTPException(409, f"{path} is in {district.name}'s folder, but this story is from "
                                     f"{item.district_code}. Fix the story's district, or ask an admin to pick it.")
        db.commit()  # no lock held while the FTP answers
        remote = _connect()
        try:
            match = footage.files_at(footage.Lister(remote, ttl=0), path)
        finally:
            remote.close()
        if match is None:
            raise HTTPException(422, f"{path} is not on the FTP server.")
        item = db.get(Item, item_id, with_for_update=True, populate_existing=True)
        if body.version != item.version:
            raise HTTPException(409, "Someone else changed this story. Reload it and try again.")
        item.footage_status, item.footage_path = "manual", match.path
        item.footage_files, item.footage_bytes, item.footage_checked_at = match.files, match.bytes, now()
    item.version += 1
    db.add(Audit(actor_id=caller.user.id, action="item.footage", target_type="item", target_id=str(item.id),
                 before=before, after=_footage_snapshot(item)))
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item, full=caller.can("page.inbox"))}


def _safe_path(raw: str) -> str:
    # Control characters (NUL, CR, LF ...) make ftplib raise ValueError (a 500); refuse them here
    if ".." in re.split(r"[\\/]", raw) or _CONTROL_CHARS.search(raw):
        raise HTTPException(422, "Bad path.")
    return ftp.clean_path("/" + raw.strip().lstrip("/"))


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


@router.get("/ftp/browse")
def browse_ftp(path: str = Query("/", max_length=1000),
               caller: Caller = Depends(require("desk.review", ("page.inbox",))), db: Session = Depends(get_db)):
    """Read-only listing of the newsroom FTP, only inside district folders and FTP_WRITE_ROOT.
    path=/ lists the district folders (and the write root) that exist on the server."""
    path = _safe_path(path)
    districts = district_service.load(db)
    root = ftp.write_root()
    allowed_tops = {d.ftp_folder.strip("/") for d in districts if d.ftp_folder}
    if root != "/":
        allowed_tops.add(root.strip("/").split("/")[0])
    in_root = root != "/" and (path == root or path.startswith(root + "/"))
    if path != "/" and footage.under_district(path, districts) is None and not in_root:
        raise HTTPException(403, "Only district folders and the Samvadia folder can be browsed.")
    remote = _connect()
    try:
        entries = remote.scan(path)
    except Exception as e:
        raise HTTPException(502, f"Reading the FTP server failed: {type(e).__name__}.")
    finally:
        remote.close()
    if path == "/":
        entries = [e for e in entries if e.is_dir and e.name in allowed_tops]
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return {"path": path, "entries": [{"name": e.name, "dir": e.is_dir, "size": e.size, "modified": _iso(e.modified)}
                                      for e in entries]}


# --- stories and the Octopus feed -----------------------------------------------------------------

def _published(db: Session, item: Item) -> Story:
    if item.review_state != "approved":
        raise HTTPException(409, "Only an approved story can be updated or killed.")
    story = latest_story(db, item)
    if story is None:
        raise HTTPException(409, "This story was never published.")
    if story.kind == "kill":
        raise HTTPException(409, "This story was killed.")
    return story


@router.post("/items/{item_id}/publish-update")
def publish_update(item_id: uuid.UUID, caller: Caller = Depends(require("feed.manage")), db: Session = Depends(get_db)):
    """Send the item's current text as a correction: a new story, version + 1, UPDATE in the title,
    and a (( )) line naming the slug it replaces. The old story is never edited; Octopus would ignore it."""
    item = _item(db, item_id, lock=True)
    _published(db, item)
    story = create_story(db, item, caller.user, "update")
    db.add(Audit(actor_id=caller.user.id, action="story.update", target_type="item", target_id=str(item.id),
                 after=story_out(story)))
    item_changed(db, item)
    db.commit()
    return {"story": story_out(story)}


@router.post("/items/{item_id}/kill")
def kill(item_id: uuid.UUID, caller: Caller = Depends(require("feed.manage")), db: Session = Depends(get_db)):
    """Withdraw a published story: a new version titled KILL <slug>. Octopus cannot delete a wire, so this
    is a message to the desk; it needs a wire rule ('Title contains KILL' -> colour grey, tag KILL)."""
    item = _item(db, item_id, lock=True)
    _published(db, item)
    story = create_story(db, item, caller.user, "kill")
    db.add(Audit(actor_id=caller.user.id, action="story.kill", target_type="item", target_id=str(item.id),
                 after=story_out(story)))
    item_changed(db, item)
    db.commit()
    return {"story": story_out(story)}


@router.get("/stories")
def list_stories(item_id: uuid.UUID | None = None, limit: int = Query(100, le=500),
                 caller: Caller = Depends(require("feed.view", ("page.feed",))), db: Session = Depends(get_db)):
    """The feed page: every story with its state and when Octopus first collected it."""
    collected = collected_at_subquery().label("collected_at")
    stmt = select(Story, collected)
    if item_id:
        stmt = stmt.where(Story.item_id == item_id)
    rows = db.execute(stmt.order_by(Story.created_at.desc()).limit(limit)).all()
    return {"stories": [story_out(s, c) for s, c in rows]}


@router.get("/feed-log")
def feed_log(limit: int = Query(50, le=500), caller: Caller = Depends(require("feed.manage", ("page.feed",))),
             db: Session = Depends(get_db)):
    rows = db.scalars(select(FeedLog).order_by(FeedLog.id.desc()).limit(limit))
    last_ok = db.scalar(select(func.max(FeedLog.at)).where(FeedLog.status.in_((200, 304))))
    return {"last_collected_at": last_ok.isoformat() if last_ok else None,
            "requests": [{"at": r.at.isoformat(), "client_ip": r.client_ip, "status": r.status,
                          "item_count": r.item_count} for r in rows]}


@router.get("/activity")
def activity(limit: int = Query(50, le=200), caller: Caller = Depends(require("desk.view", ("page.dashboard",))),
             db: Session = Depends(get_db)):
    rows = db.scalars(select(Audit).order_by(Audit.id.desc()).limit(limit)).all()
    return {"activity": [{"id": a.id, "action": a.action, "target_type": a.target_type, "target_id": a.target_id,
                          "actor_id": str(a.actor_id) if a.actor_id else None, "actor": a.actor,
                          "at": a.created_at.isoformat()}
                         for a in rows]}
