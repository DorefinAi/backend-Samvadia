from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import admin, auth, desk, events, feed, owner, reporter, webhooks
from app.config import get_settings
from app.db import SessionLocal

app = FastAPI(title="Vartmaan AI backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF", "Last-Event-ID"],
)

for module in (auth, reporter, desk, admin, webhooks, events, feed):
    app.include_router(module.router)

# Owner console API: mounted only when OWNER_API_KEY is set at start-up. The gate runs in front of FastAPI either way,
# so a key removed later still turns every /owner path into 404 before anything is parsed.
app.add_middleware(owner.OwnerGate)
app.add_exception_handler(owner.OwnerError, owner.owner_error_response)
app.add_exception_handler(RequestValidationError, owner.owner_validation_response)
if get_settings().owner_api_enabled:
    app.include_router(owner.router)

if get_settings().storage_backend == "local":
    from app.api import devstorage

    app.include_router(devstorage.router)


@app.get("/health")
def health():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"db": "ok"}
