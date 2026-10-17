"""Tables. The SQL in alembic/versions/0001_initial.py is generated from this file.

items      one incoming story (portal submission, email thread, or a burst of WhatsApp messages)
messages   every raw message that fed an item, kept as received (training data)
media      every file: Supabase Storage stop-over, then FTP <base>/<slug>/<original name>
decisions  append-only desk decisions (training labels)
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLES = ("super_admin", "admin", "assignment_desk", "chief_editor", "reporter")
SOURCES = ("portal", "outlook", "whatsapp")
PRIORITIES = ("normal", "high", "breaking")
# Same list as assignment-desk/src/lib/slug.ts CATEGORIES
CATEGORIES = ("Politics", "Crime", "Accident", "Courts", "Civic", "Education", "Weather", "Health", "Business",
              "Agriculture", "Sports", "Entertainment")
STORY_TYPES = ("AV", "PKG", "SF")
# collecting: messages/files still arriving · needs_desk: slug cannot be built (unknown sender,
# no usable subject); intake_note says why · uploading: slug built, files going to FTP
# on_ftp: done · ftp_failed: retries used up
INTAKE_STATES = ("collecting", "needs_desk", "uploading", "on_ftp", "ftp_failed")
REVIEW_STATES = ("pending", "approved", "rejected")
MEDIA_STATES = ("awaiting_upload", "downloading", "in_storage", "on_ftp", "failed", "skipped", "link_only")
REJECT_REASONS = ("duplicate", "not_newsworthy", "poor_visuals", "unverified", "other")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="users_role_check"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Reporter(Base):
    """The reporter master: phone/email -> city code and reporter code for the slug."""
    __tablename__ = "reporters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug_code: Mapped[str] = mapped_column(String(16), nullable=False)  # e.g. SUNITA
    city_code: Mapped[str] = mapped_column(String(8), nullable=False)  # e.g. UDR
    district: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)  # digits only, with country code
    email: Mapped[str | None] = mapped_column(Text, unique=True)  # stored lowercase
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint(_in("source", SOURCES), name="items_source_check"),
        CheckConstraint(_in("intake_state", INTAKE_STATES), name="items_intake_state_check"),
        CheckConstraint(_in("review_state", REVIEW_STATES), name="items_review_state_check"),
        CheckConstraint(f"story_type IS NULL OR {_in('story_type', STORY_TYPES)}", name="items_story_type_check"),
        CheckConstraint(_in("priority", PRIORITIES), name="items_priority_check"),
        Index("items_received_idx", "received_at"),
        Index("items_conversation_idx", "source", "conversation_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reporters.id"))
    sender_name: Mapped[str | None] = mapped_column(Text)
    sender_contact: Mapped[str | None] = mapped_column(Text)  # phone digits or email
    conversation_key: Mapped[str | None] = mapped_column(Text)  # WhatsApp wa_id or email conversationId
    headline: Mapped[str] = mapped_column(Text, nullable=False, server_default="", default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="", default="")
    slug_subject: Mapped[str | None] = mapped_column(Text)  # English keywords for the slug, if given
    story_type: Mapped[str | None] = mapped_column(String(8))
    category: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal", default="normal")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"), default=list)
    city_code: Mapped[str | None] = mapped_column(String(8))  # overrides the reporter's city for this story
    slug: Mapped[str | None] = mapped_column(String(96), unique=True)
    slug_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    ftp_folder: Mapped[str | None] = mapped_column(Text)
    intake_state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="collecting", default="collecting")
    intake_note: Mapped[str | None] = mapped_column(Text)
    review_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", default="pending")
    reject_reason: Mapped[str | None] = mapped_column(String(32))
    reject_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    reporter: Mapped["Reporter | None"] = relationship(lazy="selectin")  # not "joined": rows are locked FOR UPDATE
    media: Mapped[list["Media"]] = relationship(back_populates="item", order_by="Media.created_at")
    messages: Mapped[list["Message"]] = relationship(order_by="Message.received_at")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="messages_source_external_id_key"),
        CheckConstraint(_in("source", SOURCES), name="messages_source_check"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    sender_contact: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="", default="")
    raw: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created()


class Media(Base):
    __tablename__ = "media"
    __table_args__ = (CheckConstraint(_in("state", MEDIA_STATES), name="media_state_check"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text)  # as sent; None for WhatsApp photos/videos
    file_name: Mapped[str | None] = mapped_column(Text)  # final name inside the slug folder
    content_type: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="other", default="other")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    external_ref: Mapped[str | None] = mapped_column(Text)  # WhatsApp media id or Graph attachment id
    storage_key: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    ftp_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    storage_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    item: Mapped[Item] = relationship(back_populates="media")


class Decision(Base):
    """Append-only. A changed mind is a new row."""
    __tablename__ = "decisions"
    __table_args__ = (CheckConstraint("action IN ('approve', 'reject', 'reopen')", name="decisions_action_check"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)  # the item as the desk saw it
    created_at: Mapped[datetime] = _created()


class Audit(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class Job(Base):
    """Background work. The worker claims rows with FOR UPDATE SKIP LOCKED."""
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'done', 'failed')", name="jobs_status_check"),
        Index("jobs_pick_idx", "status", "run_after"),
        Index("jobs_dedupe_key", "dedupe_key", unique=True, postgresql_where=text("status IN ('pending', 'running')")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending", default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="4", default=4)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Event(Base):
    """Live updates for the SSE stream. Kept 24 hours so a reconnecting browser can catch up."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    app: Mapped[str] = mapped_column(String(16), nullable=False)  # desk | reporter
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class Location(Base):
    """City codes for the slug and the desk's location picker (seeded from the desk UI's list)."""
    __tablename__ = "locations"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)


class FetchRun(Base):
    """What each pull added: backs the desk's Refresh result and fetch history."""
    __tablename__ = "fetch_runs"
    __table_args__ = (CheckConstraint("source IN ('whatsapp', 'outlook', 'octopus')", name="fetch_runs_source_check"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    added: Mapped[int] = mapped_column(Integer, nullable=False)
    titles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"), default=list)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class KV(Base):
    """Small bits of state, e.g. the Outlook delta link."""
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _updated()
