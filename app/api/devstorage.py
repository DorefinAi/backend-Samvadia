"""PUT /dev-storage/<key>?token=: where browsers upload files when STORAGE_BACKEND=local.

Stands in for Supabase Storage's signed upload URL on a local run. Only mounted in local mode.
"""
import hmac

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.services import storage

router = APIRouter(prefix="/dev-storage", tags=["local storage"])


@router.put("/{key:path}")
async def put_file(key: str, token: str, request: Request):
    if not hmac.compare_digest(token, storage.local_token(key)):
        raise HTTPException(403, "Upload link is not valid.")
    try:
        target = storage.local_path(key)
    except ValueError:
        raise HTTPException(400, "Bad storage key.") from None
    limit = get_settings().max_upload_mb * 1024 * 1024
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(target, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                f.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"Over {get_settings().max_upload_mb} MB.")
            f.write(chunk)
    return {"key": key, "size": size}
