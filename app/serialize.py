"""JSON shapes the frontends read."""
from app.models import Item, Media, Message, Reporter, User

# What the reporter portal shows for an item (news-portal Report.status).
REPORT_STATUS = {"pending": "reviewing", "approved": "published", "rejected": "rejected"}


def reporter_out(r: Reporter | None) -> dict | None:
    if r is None:
        return None
    return {"id": str(r.id), "name": r.name, "slug_code": r.slug_code, "city_code": r.city_code,
            "district": r.district, "phone": r.phone, "email": r.email, "active": r.active,
            "has_login": r.user_id is not None}


def user_out(u: User) -> dict:
    return {"id": str(u.id), "name": u.name, "username": u.username, "email": u.email, "role": u.role, "active": u.active}


def media_out(m: Media) -> dict:
    return {"id": str(m.id), "kind": m.kind, "original_name": m.original_name, "file_name": m.file_name,
            "content_type": m.content_type, "size_bytes": m.size_bytes, "state": m.state, "ftp_path": m.ftp_path,
            "error": m.error, "received_at": m.received_at.isoformat()}


def message_out(m: Message) -> dict:
    return {"id": str(m.id), "source": m.source, "text": m.text, "sender_contact": m.sender_contact,
            "received_at": m.received_at.isoformat()}


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
        "last_message_at": item.last_message_at.isoformat(), "version": item.version,
        "status": "submitted" if item.intake_state == "collecting" else REPORT_STATUS[item.review_state],
    }
    if full:
        out["body"] = item.body
        out["media"] = [media_out(m) for m in item.media]
        out["messages"] = [message_out(m) for m in item.messages]
    return out
