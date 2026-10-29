"""JSON shapes the frontends read."""
from app.models import Item, Location, Media, Message, Reporter, Story, User
from app.services.footage import footage_out
from app.services.reporter_slug import parsed_out

# What the reporter portal shows for an item (news-portal Report.status).
# The portal knows only these four; it can read review_state (and reject_note) for held / sent_back / duplicate.
REPORT_STATUS = {"pending": "reviewing", "approved": "published", "rejected": "rejected", "held": "reviewing",
                 "sent_back": "reviewing", "duplicate": "rejected"}


def reporter_out(r: Reporter | None) -> dict | None:
    if r is None:
        return None
    emails = r.email_list
    return {"id": str(r.id), "name": r.name, "name_hi": r.name_hi, "slug_code": r.slug_code, "city_code": r.city_code,
            "district": r.district, "district_code": r.district_code, "phone": r.phone,
            "email": emails[0] if emails else None, "emails": emails, "active": r.active,
            "has_login": r.user_id is not None}


def district_out(loc: Location | None, *, folder: bool = False) -> dict | None:
    if loc is None:
        return None
    out = {"code": loc.code, "name": loc.name, "name_hi": loc.name_hi}
    if folder:
        out["ftp_folder"] = loc.ftp_folder
    return out


def user_out(u: User) -> dict:
    return {"id": str(u.id), "name": u.name, "username": u.username, "email": u.email, "role": u.role, "active": u.active}


def media_out(m: Media) -> dict:
    return {"id": str(m.id), "kind": m.kind, "original_name": m.original_name, "file_name": m.file_name,
            "content_type": m.content_type, "size_bytes": m.size_bytes, "state": m.state, "ftp_path": m.ftp_path,
            "error": m.error, "received_at": m.received_at.isoformat()}


def message_out(m: Message) -> dict:
    return {"id": str(m.id), "source": m.source, "text": m.text, "sender_contact": m.sender_contact,
            "subject": (m.raw or {}).get("subject"), "received_at": m.received_at.isoformat()}


def story_out(s: Story, collected_at=None) -> dict:
    return {"id": str(s.id), "item_id": str(s.item_id), "version": s.version, "kind": s.kind, "slug": s.slug,
            "title": s.title, "description": s.description, "district": s.district, "guid": s.guid,
            "read_seconds": s.read_seconds, "state": s.state,
            "published_at": s.published_at.isoformat() if s.published_at else None,
            "collected_at": collected_at.isoformat() if collected_at else None}


def item_out(item: Item, *, full: bool = False) -> dict:
    out = {
        "id": str(item.id), "source": item.source, "headline": item.headline, "slug": item.slug,
        "slug_subject": item.slug_subject, "story_type": item.story_type, "slug_edited": item.slug_edited,
        "category": item.category, "priority": item.priority, "tags": list(item.tags or []),
        "city_code": item.city_code,
        "location_code": item.city_code or (item.reporter.city_code if item.reporter else None),
        "sender_name": item.sender_name, "sender_contact": item.sender_contact,
        "reporter": reporter_out(item.reporter), "intake_state": item.intake_state, "intake_note": item.intake_note,
        "review_state": item.review_state, "reject_reason": item.reject_reason, "reject_note": item.reject_note,
        "ftp_folder": item.ftp_folder, "received_at": item.received_at.isoformat(),
        "last_message_at": item.last_message_at.isoformat(), "version": item.version, "blind": item.blind,
        "status": "submitted" if item.intake_state == "collecting" else REPORT_STATUS[item.review_state],
        "reporter_slug": item.reporter_slug, "slug_key": item.slug_key if item.reporter_slug else None,
        "parsed": parsed_out(item.parsed), "district": district_out(item.district), "kind": item.kind,
        "footage": footage_out(item),
        "duplicate_of": str(item.duplicate_of) if item.duplicate_of else None, "event_key": item.event_key,
    }
    if full:
        out["body"] = item.body
        out["media"] = [media_out(m) for m in item.media]
        out["messages"] = [message_out(m) for m in item.messages]
    return out
