"""File names inside a slug folder: keep the original name, make it safe and unique."""
import mimetypes
import re
from datetime import datetime, timezone

from app.services.slug import IST

MAX_NAME = 120
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_EXT_OVERRIDES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/3gpp": ".3gp",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/aac": ".aac", "audio/mp4": ".m4a", "audio/amr": ".amr",
    "application/pdf": ".pdf",
}


def kind_of(content_type: str | None) -> str:
    major = (content_type or "").split("/")[0]
    if major in ("image", "video", "audio"):
        return major
    return "document" if content_type else "other"


def extension_for(content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    return _EXT_OVERRIDES.get(ct) or mimetypes.guess_extension(ct) or ".bin"


def safe_name(name: str | None) -> str:
    """Drop folders and characters FTP cannot take; keep Hindi, spaces and the extension."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: MAX_NAME - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:MAX_NAME]
    return name


def generated_name(received_at: datetime, index: int, content_type: str | None) -> str:
    """For WhatsApp photos, videos and voice notes, which arrive without a name: 20261017-1042_01.jpg"""
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    stamp = received_at.astimezone(IST).strftime("%Y%m%d-%H%M")
    return f"{stamp}_{index:02d}{extension_for(content_type)}"


def unique_name(name: str, taken: set[str]) -> str:
    """IMG_0001.jpg twice in one folder -> IMG_0001_2.jpg. Case-insensitive, as some FTP servers are."""
    lower = {t.lower() for t in taken}
    if name.lower() not in lower:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem}_{n}" + (f".{ext}" if ext else "")
        if candidate.lower() not in lower:
            return candidate
        n += 1
