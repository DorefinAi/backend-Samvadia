"""Stories: what the desk publishes to Octopus. Approve creates version 1; Update and Kill add versions.

A story waits until the item's files are on FTP, then enters the feed (state ready, published_at set),
so Octopus never gets a story whose FTP folder is still empty.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.events import emit
from app.jobs import now
from app.models import FeedLog, Item, Location, Story, User
from app.services.rss import description_for, guid_for, read_seconds, title_for


def _slug_city(slug: str | None) -> str:
    """The city part of DDMMYY_CITY_SUBJECT_TYPE_REPORTER, for a hand-typed slug on a story with no reporter."""
    parts = (slug or "").split("_")
    return parts[1] if len(parts) > 2 else ""


def location_for(db: Session, item: Item) -> tuple[str, str, str]:
    """(code, English name, Hindi name) for the story's city."""
    code = item.city_code or (item.reporter.city_code if item.reporter else "") or ""
    if not code and db.get(Location, _slug_city(item.slug) or "-") is not None:
        code = _slug_city(item.slug)
    loc = db.get(Location, code) if code else None
    if loc is None:
        return code, code, code
    return code, loc.name, loc.name_hi or loc.name


def latest_story(db: Session, item: Item) -> Story | None:
    return db.scalar(select(Story).where(Story.item_id == item.id).order_by(Story.version.desc()).limit(1))


def create_story(db: Session, item: Item, user: User, kind: str) -> Story:
    previous = latest_story(db, item)
    version = (previous.version if previous else 0) + 1
    _, district, city_hi = location_for(db, item)
    reporter = item.reporter
    reporter_name = (reporter.name_hi or reporter.name) if reporter else (item.sender_name or "")
    description = description_for(kind=kind, city_hi=city_hi, body=item.body, reporter=reporter_name,
                                  replaces_slug=previous.slug if previous else None)
    story = Story(
        org_id=get_settings().org_id, item_id=item.id, version=version, kind=kind,
        replaces_id=previous.id if previous else None, slug=item.slug,
        title=title_for(item.slug, kind, item.priority), description=description, district=district,
        guid=guid_for(item.slug, version), read_seconds=read_seconds(description),
        state="waiting", created_by=user.id,
    )
    db.add(story)
    db.flush()
    # A kill carries no files; anything else waits for the FTP upload to finish.
    if kind == "kill" or item.intake_state == "on_ftp":
        publish(db, story)
    return story


def publish(db: Session, story: Story) -> None:
    story.state, story.published_at = "ready", now()
    emit(db, "desk", "story.ready", {"item_id": str(story.item_id), "story_id": str(story.id),
                                     "guid": story.guid, "title": story.title})


def release_waiting(db: Session, item: Item) -> None:
    """Called when an item's files reach FTP: its waiting stories enter the feed."""
    for story in db.scalars(select(Story).where(Story.item_id == item.id, Story.state == "waiting")
                            .order_by(Story.version)):
        publish(db, story)


def collected_at_subquery():
    """First successful feed fetch at or after the story went live: our proof Octopus has it."""
    return (select(func.min(FeedLog.at))
            .where(FeedLog.status.in_((200, 304)), FeedLog.at >= Story.published_at)
            .correlate(Story).scalar_subquery())
