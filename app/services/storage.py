"""Supabase Storage: the short stop-over for files before they go to FTP.

Browsers upload straight to Storage with a signed URL, so large videos never pass through the API.
The worker downloads to a temp file, uploads to FTP, checks the size, then deletes the object.
"""
import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.config import get_settings


@dataclass
class LocalCopy:
    path: str
    size: int
    sha256: str


def _base() -> str:
    return get_settings().supabase_url.rstrip("/") + "/storage/v1"


def _headers() -> dict:
    key = get_settings().supabase_service_key
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _object_path(key: str) -> str:
    return f"{get_settings().storage_bucket}/{quote(key)}"


def signed_upload_url(key: str) -> str:
    """URL the browser PUTs the file to (valid for 2 hours)."""
    r = httpx.post(f"{_base()}/object/upload/sign/{_object_path(key)}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return _base() + r.json()["url"]


def list_sizes(prefix: str) -> dict[str, int]:
    """{object name: size} for every object under prefix/ (one level)."""
    r = httpx.post(
        f"{_base()}/object/list/{get_settings().storage_bucket}",
        headers=_headers(), json={"prefix": prefix, "limit": 1000, "offset": 0}, timeout=30,
    )
    r.raise_for_status()
    return {o["name"]: int((o.get("metadata") or {}).get("size") or 0) for o in r.json() if o.get("id")}


def upload_chunks(key: str, chunks: Iterable[bytes], content_type: str | None, tmp_path: str) -> LocalCopy:
    """Write chunks to a temp file (hashing as we go), then upload that file to Storage."""
    digest, size = hashlib.sha256(), 0
    with open(tmp_path, "wb") as f:
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
            f.write(chunk)
    with open(tmp_path, "rb") as f:
        r = httpx.post(
            f"{_base()}/object/{_object_path(key)}",
            headers={**_headers(), "Content-Type": content_type or "application/octet-stream", "x-upsert": "true"},
            content=f, timeout=600,
        )
    r.raise_for_status()
    return LocalCopy(tmp_path, size, digest.hexdigest())


def _stream(key: str) -> Iterator[bytes]:
    with httpx.stream("GET", f"{_base()}/object/{_object_path(key)}", headers=_headers(), timeout=600) as r:
        r.raise_for_status()
        yield from r.iter_bytes(1024 * 1024)


def download(key: str, tmp_path: str) -> LocalCopy:
    digest, size = hashlib.sha256(), 0
    with open(tmp_path, "wb") as f:
        for chunk in _stream(key):
            digest.update(chunk)
            size += len(chunk)
            f.write(chunk)
    return LocalCopy(tmp_path, size, digest.hexdigest())


def delete(keys: list[str]) -> None:
    if not keys:
        return
    r = httpx.request("DELETE", f"{_base()}/object/{get_settings().storage_bucket}",
                      headers=_headers(), json={"prefixes": keys}, timeout=30)
    r.raise_for_status()
