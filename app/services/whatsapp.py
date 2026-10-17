"""WhatsApp Cloud API: webhook signature check, payload parsing, media download."""
import hashlib
import hmac
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import get_settings

MEDIA_TYPES = ("image", "video", "audio", "document", "voice")


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Meta signs every POST with X-Hub-Signature-256: sha256=<hex hmac of the raw body>."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def digits(phone: str | None) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


@dataclass
class IncomingMessage:
    id: str
    wa_id: str
    sender_name: str | None
    received_at: datetime
    text: str
    media_id: str | None = None
    media_type: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    raw: dict = field(default_factory=dict)


def parse_webhook(payload: dict) -> list[IncomingMessage]:
    """Flatten Meta's entry/changes/value structure into messages. Status updates are ignored."""
    out: list[IncomingMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind = m.get("type")
                received = datetime.fromtimestamp(int(m.get("timestamp", 0)), tz=timezone.utc)
                msg = IncomingMessage(id=m["id"], wa_id=m.get("from", ""), sender_name=names.get(m.get("from")),
                                      received_at=received, text="", raw=m)
                if kind == "text":
                    msg.text = (m.get("text") or {}).get("body", "")
                elif kind in MEDIA_TYPES:
                    media = m.get(kind) or {}
                    msg.media_id = media.get("id")
                    msg.media_type = kind
                    msg.mime_type = media.get("mime_type")
                    msg.filename = media.get("filename")  # only documents carry a name
                    msg.text = media.get("caption", "")
                else:
                    continue  # stickers, reactions, locations: not news
                out.append(msg)
    return out


def _auth() -> dict:
    return {"Authorization": f"Bearer {get_settings().whatsapp_token}"}


def media_info(media_id: str) -> dict:
    """{url, mime_type, file_size}. The url is valid for 5 minutes; ask again if it expires."""
    s = get_settings()
    r = httpx.get(f"https://graph.facebook.com/{s.graph_api_version}/{media_id}", headers=_auth(), timeout=30)
    r.raise_for_status()
    return r.json()


def stream_media(url: str) -> Iterator[bytes]:
    with httpx.stream("GET", url, headers=_auth(), timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        yield from r.iter_bytes(1024 * 1024)
