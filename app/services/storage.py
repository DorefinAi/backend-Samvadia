"""Supabase Storage: the short stop-over for files before they go to FTP.

Browsers upload straight to Storage with a signed URL, so large videos never pass through the API.
The worker downloads to a temp file, uploads to FTP, checks the size, then deletes the object.

STORAGE_BACKEND=local keeps the same flow in a folder on this machine (LOCAL_STORAGE_DIR), for
running everything locally; uploads then go to PUT /dev-storage/<key> on this API.
"""
import hashlib
import hmac
import secrets
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import get_settings

# Signs local upload links. Only the API both signs and checks them, so one per process is enough.
_LOCAL_SECRET = secrets.token_bytes(32)


@dataclass
class LocalCopy:
    path: str
    size: int
    sha256: str


def is_local() -> bool:
    return get_settings().storage_backend == "local"


def local_path(key: str) -> Path:
    """The file for a key. Keys are made by the backend (items/<uuid>/<uuid>), never by a client."""
    root = Path(get_settings().local_storage_dir).resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise ValueError("Storage key outside the storage folder.")
    return path


def local_token(key: str) -> str:
    return hmac.new(_LOCAL_SECRET, key.encode(), hashlib.sha256).hexdigest()


def _base() -> str:
    return get_settings().supabase_url.rstrip("/") + "/storage/v1"


def _headers() -> dict:
    key = get_settings().supabase_service_key
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _object_path(key: str) -> str:
    return f"{get_settings().storage_bucket}/{quote(key)}"


def signed_upload_url(key: str) -> str:
    """URL the browser PUTs the file to (valid for 2 hours)."""
    if is_local():
        return f"{get_settings().public_api_url.rstrip('/')}/dev-storage/{quote(key)}?token={local_token(key)}"
    r = httpx.post(f"{_base()}/object/upload/sign/{_object_path(key)}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return _base() + r.json()["url"]


def list_sizes(prefix: str) -> dict[str, int]:
    """{object name: size} for every object under prefix/ (one level)."""
    if is_local():
        folder = local_path(prefix)
        return {p.name: p.stat().st_size for p in folder.iterdir() if p.is_file()} if folder.is_dir() else {}
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
    if is_local():
        target = local_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_path, target)
        return LocalCopy(tmp_path, size, digest.hexdigest())
    with open(tmp_path, "rb") as f:
        r = httpx.post(
            f"{_base()}/object/{_object_path(key)}",
            headers={**_headers(), "Content-Type": content_type or "application/octet-stream", "x-upsert": "true"},
            content=f, timeout=600,
        )
    r.raise_for_status()
    return LocalCopy(tmp_path, size, digest.hexdigest())


def _stream(key: str) -> Iterator[bytes]:
    if is_local():
        with open(local_path(key), "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
        return
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
    if is_local():
        for key in keys:
            local_path(key).unlink(missing_ok=True)
        return
    r = httpx.request("DELETE", f"{_base()}/object/{get_settings().storage_bucket}",
                      headers=_headers(), json={"prefixes": keys}, timeout=30)
    r.raise_for_status()
