"""WhatsApp Cloud API webhook. Meta calls this; nothing here trusts a request before the signature check."""
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import session_scope
from app.events import item_changed
from app.intake import add_media, add_message, find_reporter, schedule_finalize
from app.jobs import enqueue
from app.services import whatsapp

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp", response_class=PlainTextResponse)
def verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
           challenge: str = Query("", alias="hub.challenge")):
    """Meta's one-time check when the webhook URL is saved in the app dashboard."""
    expected = get_settings().whatsapp_verify_token
    if mode == "subscribe" and expected and token == expected:
        return challenge
    raise HTTPException(403, "Verification failed.")


@router.post("/whatsapp")
async def receive(request: Request):
    raw = await request.body()
    if not whatsapp.verify_signature(get_settings().whatsapp_app_secret, raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Bad signature.")
    await run_in_threadpool(_store, json.loads(raw))
    return {"ok": True}


def _store(payload: dict) -> None:
    with session_scope() as db:
        _store_messages(db, payload)


def _store_messages(db: Session, payload: dict) -> None:
    merge = get_settings().whatsapp_merge_seconds
    for msg in whatsapp.parse_webhook(payload):
        result = add_message(
            db, source="whatsapp", external_id=msg.id, sender_contact=msg.wa_id, sender_name=msg.sender_name,
            text=msg.text, received_at=msg.received_at, raw=msg.raw, conversation_key=msg.wa_id,
            reporter=find_reporter(db, phone=msg.wa_id),
        )
        if result is None:
            continue  # Meta delivered this message before
        item, message, created = result
        if msg.media_id:
            media = add_media(db, item, source="whatsapp", state="downloading", message=message,
                              original_name=msg.filename, content_type=msg.mime_type, external_ref=msg.media_id,
                              received_at=msg.received_at)
            enqueue(db, "fetch_whatsapp_media", {"media_id": str(media.id)})
        # Texts, photos and a voice note sent within the merge window become one item.
        schedule_finalize(db, item, delay=merge)
        item_changed(db, item, "item.created" if created else "item.updated")
