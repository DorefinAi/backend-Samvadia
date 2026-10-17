from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import admin, auth, desk, events, feed, reporter, webhooks
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


@app.get("/health")
def health():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"db": "ok"}
