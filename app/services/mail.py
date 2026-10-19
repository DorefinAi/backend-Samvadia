"""Outlook / Microsoft 365 through Microsoft Graph, read-only (Mail.Read on one mailbox).

We never mark mail as read or move it, so the desk's Outlook looks the same as before.
"""
import re
import time
from collections.abc import Iterator

import httpx

from app.config import get_settings

GRAPH = "https://graph.microsoft.com/v1.0"
MESSAGE_FIELDS = "subject,body,from,receivedDateTime,hasAttachments,conversationId,internetMessageId"
_token: dict = {"value": "", "expires": 0.0}

# Where the new text of an email ends and quoted/forwarded/signature text begins.
_CUT_MARKERS = [
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.I),
    re.compile(r"^_{5,}\s*$"),
    re.compile(r"^From:\s.+", re.I),
    re.compile(r"^On .+wrote:\s*$", re.I),
    re.compile(r"^Sent from my ", re.I),
    re.compile(r"^Get Outlook for ", re.I),
    re.compile(r"^--\s*$"),
    re.compile(r"^(DISCLAIMER|CONFIDENTIALITY NOTICE)\b", re.I),
    re.compile(r"^This (e-?mail|message) and any (files|attachments)", re.I),
]
# Gmail and Outlook wrap a long "On <date>, <name> <address> wrote:" over two or three lines.
_WROTE_START = re.compile(r"^On\s.+", re.I)
_WROTE_END = re.compile(r"\bwrote:\s*$", re.I)


def clean_body(text: str) -> str:
    """Keep the new part of the email; drop replies, forwards, signatures and disclaimers."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if (any(m.match(stripped) for m in _CUT_MARKERS) or _wrapped_wrote(lines, i)) and any(k.strip() for k in kept):
            break
        kept.append(line.rstrip())
    out = "\n".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def _wrapped_wrote(lines: list[str], i: int) -> bool:
    """True when line i starts an "On ... wrote:" header that ends on one of the next two lines."""
    if not _WROTE_START.match(lines[i].strip()):
        return False
    joined = lines[i].strip()
    for nxt in lines[i + 1:i + 3]:
        joined += " " + nxt.strip()
        if _WROTE_END.search(joined):
            return True
    return False


def is_transient(error: Exception) -> bool:
    """Throttling, a Graph outage or a dropped connection: worth trying again later without counting it."""
    if isinstance(error, httpx.TransportError):
        return True
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (408, 429, 500, 502, 503, 504)


def _access_token() -> str:
    s = get_settings()
    if _token["value"] and _token["expires"] > time.time() + 60:
        return _token["value"]
    r = httpx.post(
        f"https://login.microsoftonline.com/{s.ms_tenant_id}/oauth2/v2.0/token",
        data={"client_id": s.ms_client_id, "client_secret": s.ms_client_secret,
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    _token.update(value=body["access_token"], expires=time.time() + int(body.get("expires_in", 3600)))
    return _token["value"]


def _headers() -> dict:
    # Ask Graph for the body as plain text instead of HTML.
    return {"Authorization": f"Bearer {_access_token()}", "Prefer": 'outlook.body-content-type="text"'}


def inbox_delta(delta_link: str | None) -> tuple[list[dict], str]:
    """New inbox messages since the last call. Returns (messages, next delta link)."""
    s = get_settings()
    url = delta_link or f"{GRAPH}/users/{s.ms_mailbox}/mailFolders/inbox/messages/delta?$select={MESSAGE_FIELDS}"
    messages: list[dict] = []
    with httpx.Client(timeout=60) as client:
        while True:
            r = client.get(url, headers=_headers())
            r.raise_for_status()
            body = r.json()
            messages += [m for m in body.get("value", []) if "@removed" not in m]
            if "@odata.nextLink" in body:
                url = body["@odata.nextLink"]
                continue
            return messages, body["@odata.deltaLink"]


def get_message(message_id: str) -> dict:
    """One message by id, the same fields as the delta. Used to retry a message that failed."""
    s = get_settings()
    r = httpx.get(f"{GRAPH}/users/{s.ms_mailbox}/messages/{message_id}?$select={MESSAGE_FIELDS}",
                  headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def list_attachments(message_id: str) -> list[dict]:
    """Metadata only. $select keeps Graph from sending every file inline as base64; it can also drop
    @odata.type, so a OneDrive link is caught again when $value answers 405 or 415."""
    s = get_settings()
    r = httpx.get(
        f"{GRAPH}/users/{s.ms_mailbox}/messages/{message_id}/attachments?$select=id,name,contentType,size,isInline",
        headers=_headers(), timeout=60,
    )
    r.raise_for_status()
    return r.json().get("value", [])


def stream_attachment(message_id: str, attachment_id: str) -> Iterator[bytes]:
    s = get_settings()
    url = f"{GRAPH}/users/{s.ms_mailbox}/messages/{message_id}/attachments/{attachment_id}/$value"
    with httpx.stream("GET", url, headers=_headers(), timeout=300) as r:
        r.raise_for_status()
        yield from r.iter_bytes(1024 * 1024)
