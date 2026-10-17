"""Reporter portal (news-portal). Two steps, so big videos never pass through the API:

1. POST /reporter/items            text + list of files -> item id + one signed upload URL per file
   (browser PUTs each file to its URL, straight into Supabase Storage)
2. POST /reporter/items/{id}/submit  checks the files arrived, builds the slug, queues the FTP upload
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.events import item_changed
from app.intake import add_media, new_item, schedule_finalize
from app.jobs import now
from app.models import STORY_TYPES, Item, Media
from app.security import Caller, require
from app.serialize import item_out
from app.services import storage
from app.services.filenames import safe_name

router = APIRouter(prefix="/reporter", tags=["reporter portal"])


def clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        tag = " ".join(tag.split())[:40]
        if tag and tag.lower() not in (t.lower() for t in out):
            out.append(tag)
    return out


class FileIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    content_type: str = Field(default="application/octet-stream", max_length=127)


class ReportIn(BaseModel):
    headline: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=20000)
    subject: str | None = Field(default=None, max_length=80)  # English keywords for the slug, optional
    story_type: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=10)  # e.g. "Breaking News", "Politics"
    files: list[FileIn] = Field(default_factory=list, max_length=20)


def _reporter(caller: Caller):
    if caller.reporter is None:
        raise HTTPException(403, "This login is not linked to a reporter. Ask the desk lead.")
    return caller.reporter


def _own_item(db: Session, caller: Caller, item_id: uuid.UUID) -> Item:
    item = db.get(Item, item_id)
    if item is None or item.reporter_id != _reporter(caller).id:
        raise HTTPException(404, "Report not found.")
    return item


@router.post("/items")
def create_report(body: ReportIn, caller: Caller = Depends(require("report.submit")), db: Session = Depends(get_db)):
    reporter = _reporter(caller)
    if body.story_type and body.story_type not in STORY_TYPES:
        raise HTTPException(422, f"story_type must be one of {', '.join(STORY_TYPES)}.")
    limit = get_settings().max_upload_mb * 1024 * 1024
    too_big = [f.name for f in body.files if f.size > limit]
    if too_big:
        raise HTTPException(413, f"Over {get_settings().max_upload_mb} MB, send by FTP instead: {', '.join(too_big)}")
    text = body.body if not body.notes else f"{body.body}\n\n{body.notes}"
    item = new_item(db, source="portal", reporter=reporter, sender_name=reporter.name,
                    sender_contact=reporter.phone or reporter.email, conversation_key=None,
                    received_at=now(), headline=body.headline.strip(), body=text.strip())
    item.slug_subject = (body.subject or "").strip() or None
    item.story_type = body.story_type
    item.tags = clean_tags(body.tags)
    if any("breaking" in t.lower() for t in item.tags):
        item.priority = "breaking"
    uploads = []
    for f in body.files:
        media = add_media(db, item, source="portal", state="awaiting_upload", original_name=safe_name(f.name) or None,
                          content_type=f.content_type, size_bytes=f.size)
        uploads.append({"media_id": str(media.id), "name": f.name, "upload_url": storage.signed_upload_url(media.storage_key)})
    db.commit()
    return {"item": item_out(item), "uploads": uploads}


@router.post("/items/{item_id}/submit")
def submit_report(item_id: uuid.UUID, caller: Caller = Depends(require("report.submit")), db: Session = Depends(get_db)):
    item = _own_item(db, caller, item_id)
    if item.intake_state != "collecting":
        return {"item": item_out(item)}
    waiting = db.scalars(select(Media).where(Media.item_id == item.id, Media.state == "awaiting_upload")).all()
    if waiting:
        stored = storage.list_sizes(f"items/{item.id}")
        missing = [m.original_name or str(m.id) for m in waiting if str(m.id) not in stored]
        if missing:
            raise HTTPException(409, f"These files have not finished uploading: {', '.join(missing)}")
        for m in waiting:
            m.state, m.size_bytes = "in_storage", stored[str(m.id)]
    schedule_finalize(db, item)
    item_changed(db, item, "item.created")
    db.commit()
    return {"item": item_out(item)}


@router.get("/items")
def my_reports(caller: Caller = Depends(require("report.submit")), db: Session = Depends(get_db), limit: int = 50):
    reporter = _reporter(caller)
    items = db.scalars(select(Item).where(Item.reporter_id == reporter.id)
                       .order_by(Item.received_at.desc()).limit(min(limit, 200))).all()
    return {"items": [item_out(i) for i in items]}


@router.get("/items/{item_id}")
def my_report(item_id: uuid.UUID, caller: Caller = Depends(require("report.submit")), db: Session = Depends(get_db)):
    return {"item": item_out(_own_item(db, caller, item_id), full=True)}
