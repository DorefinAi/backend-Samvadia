"""Outlook / Microsoft 365 through Microsoft Graph, read-only (Mail.Read on one mailbox).

We never mark mail as read or move it, so the desk's Outlook looks the same as before.
"""
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone

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


# Script lines the desk relies on: notes to the desk, the header, the anchor intro. Never dropped.
SCRIPT_LINE = re.compile(r"^[\W_]*(?:नोट|हैडर|हेडर|एंकर|anchor|header)", re.I)
_HEADER = re.compile(r"^(?:From|Sent|Date|To|Cc|Bcc|Subject)\s*:", re.I)
_HEADER_FOLLOW = re.compile(r"^(?:Sent|Date|To|Cc|Subject)\s*:", re.I)
_SIGN_OFF = re.compile(r"^(?:Sent from my |Get Outlook for )", re.I)
_WROTE_ONE_LINE = re.compile(r"^On .+wrote:\s*$", re.I)
_BANNER = re.compile(r"^(?:-{2,}\s*(?:Original|Forwarded) Message\s*-{2,}|_{5,}|--)\s*$", re.I)


def clean_body(text: str) -> str:
    """Keep the new part of the email; drop replies, forwards, signatures and disclaimers.

    Never loses a script: when the new part is empty (a forward of the reporter's own mail) or has no
    नोट / हैडर / एंकर line while the quoted part does (a resend that says "feed sent" above the script),
    the quoted script is kept too, without its From/Sent/To header lines."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = "\n".join(_tidy(_clean(lines, 0))).strip()
    return re.sub(r"\n{3,}", "\n\n", out)


def _cut_at(lines: list[str], i: int) -> bool:
    stripped = lines[i].strip()
    return any(m.match(stripped) for m in _CUT_MARKERS) or _wrapped_wrote(lines, i)


def _header_block(lines: list[str], i: int) -> bool:
    """A real mail header: From: ... followed within four lines by Sent:/Date:/To:/Subject:."""
    return bool(re.match(r"^From\s*:", lines[i].strip(), re.I)) and any(
        _HEADER_FOLLOW.match(n.strip()) for n in lines[i + 1:i + 5])


def _meaningful(line: str) -> bool:
    """Text of the sender's own, not a banner, a sign-off or a header line."""
    s = line.strip()
    return bool(s) and not (_BANNER.match(s) or _SIGN_OFF.match(s) or _HEADER.match(s))


def _has_script(lines: list[str]) -> bool:
    return any(SCRIPT_LINE.match(re.sub(r"^[\s>]+", "", l)) for l in lines)


def _clean(lines: list[str], depth: int) -> list[str]:
    kept: list[str] = []
    own = False  # has the sender written anything of their own yet (not a banner, header or "On ... wrote:")?
    for i, line in enumerate(lines):
        if line.strip().startswith(">"):
            continue
        marker = _cut_at(lines, i)
        if marker and own:
            rest = lines[i:]
            if depth < 3 and _has_script(rest) and not _has_script(kept):
                return kept + [""] + _clean(_unquote(_skip_header(rest)), depth + 1)
            return kept
        own = own or (not marker and _meaningful(line) and not _in_wrote(lines, i))
        kept.append(line.rstrip())
    return kept


def _in_wrote(lines: list[str], i: int) -> bool:
    """Line i is the second or third line of a wrapped "On ... wrote:" header."""
    return any(_wrapped_wrote(lines, j) for j in range(max(0, i - 2), i))


def _skip_header(lines: list[str]) -> list[str]:
    """Drop the banner, the From/Sent/To/Subject lines and an "On ... wrote:" line at the top."""
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or _BANNER.match(s) or _HEADER.match(s) or _SIGN_OFF.match(s) or _WROTE_ONE_LINE.match(s):
            i += 1
        elif _WROTE_START.match(s) and _wrapped_wrote(lines, i):
            while i < len(lines) and not _WROTE_END.search(lines[i].strip()):
                i += 1
            i += 1
        else:
            break
    return lines[i:]


def _unquote(lines: list[str]) -> list[str]:
    return [re.sub(r"^\s*>\s?", "", l) for l in lines]


def _tidy(lines: list[str]) -> list[str]:
    """A forward with nothing above it: drop the banner and the header block before the forwarded text."""
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is None:
        return lines
    s = lines[first].strip()
    if (_BANNER.match(s) or _SIGN_OFF.match(s) or _header_block(lines, first) or _WROTE_ONE_LINE.match(s)
            or _wrapped_wrote(lines, first)):
        rest = _skip_header(lines[first:])
        if any(_meaningful(l) for l in rest):
            return rest
    return lines


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


def graph_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inbox_delta(delta_link: str | None, since: datetime | None = None) -> tuple[list[dict], str]:
    """New inbox messages since the last call. Returns (messages, next delta link).
    The first call (no delta link) asks Graph only for mail received at or after `since`, so the first sync
    never walks the whole old inbox; the caller also drops anything older (app/tasks.py)."""
    s = get_settings()
    url = delta_link or f"{GRAPH}/users/{s.ms_mailbox}/mailFolders/inbox/messages/delta?$select={MESSAGE_FIELDS}"
    if not delta_link and since is not None:
        url += f"&$filter=receivedDateTime+ge+{graph_time(since)}"
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
