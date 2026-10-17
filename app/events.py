"""Live updates. emit() writes a row in the caller's transaction; /events streams new rows."""
from sqlalchemy.orm import Session

from app.models import Event, Item


def emit(db: Session, app: str, type_: str, payload: dict) -> None:
    db.add(Event(app=app, type=type_, payload=payload))


def item_changed(db: Session, item: Item, type_: str = "item.updated") -> None:
    """Tell the desk, and the reporter who filed it, that an item moved."""
    payload = {
        "item_id": str(item.id), "intake_state": item.intake_state, "review_state": item.review_state,
        "slug": item.slug, "reporter_id": str(item.reporter_id) if item.reporter_id else None,
    }
    emit(db, "desk", type_, payload)
    if item.reporter_id:
        emit(db, "reporter", "report.status", payload)
