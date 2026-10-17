"""Assignment desk: inbox, edit, reporter picker, approve / reject / reopen, retries."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.events import item_changed
from app.intake import schedule_finalize, slug_for
from app.jobs import enqueue, now
from app.models import REJECT_REASONS, STORY_TYPES, Audit, Decision, Item, Media, Reporter
from app.security import Caller, require
from app.serialize import item_out, reporter_out
from app.services.slug import SlugError, slug_error
from app.services.whatsapp import digits

router = APIRouter(prefix="/desk", tags=["assignment desk"])


def _item(db: Session, item_id: uuid.UUID, lock: bool = False) -> Item:
    item = db.get(Item, item_id, with_for_update=lock)
    if item is None:
        raise HTTPException(404, "Item not found.")
    return item


def _snapshot(item: Item) -> dict:
    return item_out(item, full=True)


# --- inbox ------------------------------------------------------------------------------------

@router.get("/items")
def list_items(
    review_state: str | None = None, intake_state: str | None = None, source: str | None = None,
    q: str | None = None, before: datetime | None = None, limit: int = Query(50, le=200),
    caller: Caller = Depends(require("desk.view")), db: Session = Depends(get_db),
):
    stmt = select(Item).where(Item.intake_state != "collecting" if not intake_state else Item.intake_state == intake_state)
    if review_state:
        stmt = stmt.where(Item.review_state == review_state)
    if source:
        stmt = stmt.where(Item.source == source)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Item.headline.ilike(like), Item.body.ilike(like), Item.slug.ilike(like),
                              Item.sender_name.ilike(like)))
    if before:
        stmt = stmt.where(Item.received_at < before)
    items = db.scalars(stmt.order_by(Item.received_at.desc()).limit(limit)).all()
    return {"items": [item_out(i) for i in items],
            "next_before": items[-1].received_at.isoformat() if len(items) == limit else None}


@router.get("/items/{item_id}")
def get_item(item_id: uuid.UUID, caller: Caller = Depends(require("desk.view")), db: Session = Depends(get_db)):
    return {"item": item_out(_item(db, item_id), full=True)}


class ItemPatch(BaseModel):
    version: int
    headline: str | None = Field(default=None, max_length=160)
    body: str | None = Field(default=None, max_length=20000)
    slug_subject: str | None = Field(default=None, max_length=80)
    story_type: str | None = None
    reporter_id: uuid.UUID | None = None
    slug: str | None = None  # set by hand; must pass the slug check


@router.patch("/items/{item_id}")
def edit_item(item_id: uuid.UUID, patch: ItemPatch, caller: Caller = Depends(require("desk.review")),
              db: Session = Depends(get_db)):
    """Edit the draft. `version` must match, so two editors cannot overwrite each other (409)."""
    item = _item(db, item_id, lock=True)
    if patch.version != item.version:
        raise HTTPException(409, "Someone else changed this story. Reload it and try again.")
    if patch.story_type is not None and patch.story_type not in STORY_TYPES:
        raise HTTPException(422, f"story_type must be one of {', '.join(STORY_TYPES)}.")
    before = _snapshot(item)
    fields = patch.model_dump(exclude_unset=True, exclude={"version", "slug"})
    if "reporter_id" in fields and fields["reporter_id"] is not None and db.get(Reporter, fields["reporter_id"]) is None:
        raise HTTPException(422, "Unknown reporter.")
    for key, value in fields.items():
        setattr(item, key, value)
    db.flush()
    db.refresh(item)  # reload the reporter relationship

    old_slug, old_folder = item.slug, item.ftp_folder
    slug_inputs_changed = bool({"slug_subject", "story_type", "reporter_id"} & fields.keys())
    if patch.slug is not None:
        error = slug_error(patch.slug)
        if error:
            raise HTTPException(422, error)
        if db.scalar(select(Item.id).where(Item.slug == patch.slug, Item.id != item.id)):
            raise HTTPException(409, f"Slug {patch.slug} is already used by another story.")
        item.slug = patch.slug
    elif slug_inputs_changed and item.reporter_id:
        try:
            item.slug = slug_for(db, item)
        except SlugError as e:
            raise HTTPException(422, str(e))

    if item.intake_state == "needs_desk" and item.reporter_id:
        schedule_finalize(db, item)  # builds the slug (if needed) and uploads
    elif old_folder and item.slug != old_slug:
        enqueue(db, "ftp_rename", {"item_id": str(item.id), "old_folder": old_folder})

    item.version += 1
    db.add(Audit(actor_id=caller.user.id, action="item.edit", target_type="item", target_id=str(item.id),
                 before=before, after=_snapshot(item)))
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item, full=True)}


# --- decisions --------------------------------------------------------------------------------

class RejectIn(BaseModel):
    reason: str
    note: str | None = Field(default=None, max_length=1000)


def _decide(db: Session, caller: Caller, item: Item, action: str, reason: str | None = None, note: str | None = None):
    db.add(Decision(org_id=get_settings().org_id, item_id=item.id, action=action, reason=reason, note=note,
                    user_id=caller.user.id, snapshot=_snapshot(item)))
    item.reviewed_by, item.reviewed_at = caller.user.id, now()
    item.version += 1
    item_changed(db, item)
    db.commit()
    return {"item": item_out(item, full=True)}


@router.post("/items/{item_id}/approve")
def approve(item_id: uuid.UUID, caller: Caller = Depends(require("desk.review")), db: Session = Depends(get_db)):
    item = _item(db, item_id, lock=True)
    if item.review_state != "pending":
        raise HTTPException(409, f"This story is already {item.review_state}.")
    if not item.slug:
        raise HTTPException(409, item.intake_note or "The story has no slug yet.")
    item.review_state, item.reject_reason, item.reject_note = "approved", None, None
    return _decide(db, caller, item, "approve")


@router.post("/items/{item_id}/reject")
def reject(item_id: uuid.UUID, body: RejectIn, caller: Caller = Depends(require("desk.review")),
           db: Session = Depends(get_db)):
    if body.reason not in REJECT_REASONS:
        raise HTTPException(422, f"reason must be one of {', '.join(REJECT_REASONS)}.")
    item = _item(db, item_id, lock=True)
    if item.review_state != "pending":
        raise HTTPException(409, f"This story is already {item.review_state}.")
    item.review_state, item.reject_reason, item.reject_note = "rejected", body.reason, body.note
    return _decide(db, caller, item, "reject", body.reason, body.note)


@router.post("/items/{item_id}/reopen")
def reopen(item_id: uuid.UUID, caller: Caller = Depends(require("desk.review")), db: Session = Depends(get_db)):
    item = _item(db, item_id, lock=True)
    if item.review_state != "rejected":
        raise HTTPException(409, "Only a rejected story can be reopened.")
    item.review_state = "pending"
    return _decide(db, caller, item, "reopen")


# --- retries and refresh ----------------------------------------------------------------------

@router.post("/items/{item_id}/retry-ftp")
def retry_ftp(item_id: uuid.UUID, caller: Caller = Depends(require("desk.review")), db: Session = Depends(get_db)):
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
    if item.intake_state == "on_ftp":
        item.intake_state = "collecting"
    schedule_finalize(db, item, delay=5)
    db.commit()
    return {"ok": True}


@router.post("/refresh")
def refresh(caller: Caller = Depends(require("desk.fetch")), db: Session = Depends(get_db)):
    """Check the mailbox now instead of waiting for the next minute. WhatsApp arrives by itself."""
    if get_settings().mail_enabled:
        enqueue(db, "fetch_mail", {}, dedupe_key="fetch_mail")
    db.commit()
    return {"ok": True, "mail": get_settings().mail_enabled}


# --- reporters (the picker and the reporter list) ---------------------------------------------

class ReporterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug_code: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    city_code: str = Field(min_length=2, max_length=5, pattern=r"^[A-Za-z]+$")
    district: str | None = None
    phone: str | None = None
    email: str | None = None
    active: bool = True


@router.get("/reporters")
def list_reporters(q: str | None = None, caller: Caller = Depends(require("desk.view")), db: Session = Depends(get_db)):
    stmt = select(Reporter).where(Reporter.active)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Reporter.name.ilike(like), Reporter.phone.ilike(f"%{digits(q) or q}%"),
                              Reporter.email.ilike(like), Reporter.district.ilike(like), Reporter.slug_code.ilike(like)))
    return {"reporters": [reporter_out(r) for r in db.scalars(stmt.order_by(Reporter.name).limit(100))]}


def _reporter_values(body: ReporterIn) -> dict:
    return {"name": body.name.strip(), "slug_code": body.slug_code.upper(), "city_code": body.city_code.upper(),
            "district": body.district, "phone": digits(body.phone) or None,
            "email": body.email.strip().lower() if body.email else None, "active": body.active}


@router.post("/reporters")
def add_reporter(body: ReporterIn, caller: Caller = Depends(require("reporters.manage")), db: Session = Depends(get_db)):
    """The lead adds a new stringer from the reporter picker. Future messages from this phone/email match automatically."""
    reporter = Reporter(org_id=get_settings().org_id, **_reporter_values(body))
    db.add(reporter)
    db.flush()
    db.add(Audit(actor_id=caller.user.id, action="reporter.add", target_type="reporter", target_id=str(reporter.id),
                 after=reporter_out(reporter)))
    db.commit()
    return {"reporter": reporter_out(reporter)}


@router.put("/reporters/{reporter_id}")
def update_reporter(reporter_id: uuid.UUID, body: ReporterIn, caller: Caller = Depends(require("reporters.manage")),
                    db: Session = Depends(get_db)):
    reporter = db.get(Reporter, reporter_id)
    if reporter is None:
        raise HTTPException(404, "Reporter not found.")
    before = reporter_out(reporter)
    for key, value in _reporter_values(body).items():
        setattr(reporter, key, value)
    db.add(Audit(actor_id=caller.user.id, action="reporter.edit", target_type="reporter", target_id=str(reporter.id),
                 before=before, after=reporter_out(reporter)))
    db.commit()
    return {"reporter": reporter_out(reporter)}


@router.get("/activity")
def activity(limit: int = Query(50, le=200), caller: Caller = Depends(require("desk.view")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Audit).order_by(Audit.id.desc()).limit(limit)).all()
    return {"activity": [{"id": a.id, "action": a.action, "target_type": a.target_type, "target_id": a.target_id,
                          "actor_id": str(a.actor_id) if a.actor_id else None, "at": a.created_at.isoformat()}
                         for a in rows]}
