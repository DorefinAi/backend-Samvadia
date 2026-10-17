"""GET /feed/{secret}/feed.xml: the RSS feed Octopus polls every minute.

Two locks: the secret path, and an IP allow-list (OCTOPUS_ALLOWED_IPS). A refused request gets 404,
not 403, so the path does not advertise itself. ETag / Last-Modified let most polls end in 304.
Every request, served or refused, is written to feed_log.
"""
import hashlib
import secrets
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import FeedLog, Story
from app.security import client_ip
from app.services.rss import build_feed

router = APIRouter(tags=["octopus feed"])


def _log(ip: str, status: int, count: int, user_agent: str | None) -> None:
    with session_scope() as db:
        db.add(FeedLog(client_ip=ip, status=status, item_count=count, user_agent=(user_agent or "")[:300]))


@router.get("/feed/{feed_secret}/feed.xml", include_in_schema=False)
def feed(feed_secret: str, request: Request):
    s = get_settings()
    ip = client_ip(request)
    agent = request.headers.get("user-agent")
    allowed = s.octopus_ip_list
    if not s.feed_secret or not secrets.compare_digest(feed_secret, s.feed_secret) or (allowed and ip not in allowed):
        _log(ip, 404, 0, agent)
        return Response(status_code=404)

    with session_scope() as db:
        rows = db.scalars(select(Story).where(Story.state == "ready")
                          .order_by(Story.published_at.desc()).limit(s.feed_limit)).all()
        stories = [{"title": r.title, "description": r.description, "district": r.district, "guid": r.guid,
                    "slug": r.slug, "published_at": r.published_at} for r in rows]

    body = build_feed(stories, self_url=str(request.url), link_base=s.feed_link_base)
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if stories:
        headers["Last-Modified"] = format_datetime(stories[0]["published_at"].astimezone(timezone.utc), usegmt=True)

    if _not_modified(request, etag, stories):
        _log(ip, 304, len(stories), agent)
        return Response(status_code=304, headers=headers)
    _log(ip, 200, len(stories), agent)
    return Response(content=body, media_type="application/rss+xml; charset=utf-8", headers=headers)


def _not_modified(request: Request, etag: str, stories: list[dict]) -> bool:
    if_none_match = request.headers.get("if-none-match")
    if if_none_match:
        return etag in [t.strip() for t in if_none_match.split(",")]
    since = request.headers.get("if-modified-since")
    if since and stories:
        try:
            return stories[0]["published_at"].replace(microsecond=0) <= parsedate_to_datetime(since)
        except (TypeError, ValueError):
            return False
    return False
