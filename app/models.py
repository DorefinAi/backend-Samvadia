"""Tables. The SQL in alembic/versions/0001_initial.py is generated from this file.

items      one incoming story (portal submission, email thread, or a burst of WhatsApp messages)
messages   every raw message that fed an item, kept as received (training data)
media      every file: Supabase Storage stop-over, then FTP <base>/<slug>/<original name>
decisions  append-only desk decisions (training labels)
decision_actions, reject_reasons, approve_reasons   the desk's choices with English/Hindi labels (seeded in 0005)
locations  the districts: slug city codes, Hindi names, aliases reporters type, the newsroom FTP top folder
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLES = ("super_admin", "admin", "assignment_desk", "chief_editor", "reporter")
SOURCES = ("portal", "outlook", "whatsapp")
PRIORITIES = ("normal", "high", "breaking")
# Same list as assignment-desk/src/lib/slug.ts CATEGORIES
CATEGORIES = ("Politics", "Crime", "Accident", "Courts", "Civic", "Education", "Weather", "Health", "Business",
              "Agriculture", "Sports", "Entertainment")
STORY_TYPES = ("AV", "AVB", "PKG", "SF", "WT", "PTC", "BYTE", "LIVE", "TEXT", "BRIEF")
# collecting: messages/files still arriving · needs_desk: slug cannot be built (unknown sender,
# no usable subject); intake_note says why · uploading: slug built, files going to FTP
# on_ftp: done · ftp_failed: retries used up
INTAKE_STATES = ("collecting", "needs_desk", "uploading", "on_ftp", "ftp_failed")
# held: parked by the desk · sent_back: back to the reporter for more · duplicate: items.duplicate_of says of what
REVIEW_STATES = ("pending", "approved", "rejected", "held", "sent_back", "duplicate")
MEDIA_STATES = ("awaiting_upload", "downloading", "in_storage", "on_ftp", "failed", "skipped", "link_only")
# The codes seeded in 0005 (decision_actions, reject_reasons, approve_reasons); the tables hold the labels.
# reopen undoes a wrong decision and is left out of training (target 'exclude').
DECISION_ACTIONS = ("approve", "needs_more", "hold", "duplicate", "reject", "reopen")
DECISION_TARGETS = ("positive", "negative", "weak_positive", "exclude")
REJECT_REASONS = ("duplicate", "not_newsworthy", "pr_puff", "routine_event", "unverified", "no_visuals", "incomplete",
                  "wrong_beat", "too_late", "legal_risk", "communal_risk", "quality", "other")
REASON_OWNERS = ("scorer", "dedupe", "intake", "policy", "desk")  # which stage should have caught it
APPROVE_REASONS = ("must_carry", "exclusive", "good_visual", "audience", "ready_to_air", "follow_up", "filler")
# waiting: approved, files not yet on FTP · ready: in the feed (an UPDATE is a new ready row, the old one stays)
STORY_STATES = ("waiting", "ready")
STORY_KINDS = ("original", "update", "kill")
QUEUES = ("default", "media", "gpu", "cloud")
# story: news for the desk · press_release · day_plan · not_a_story: bounces, bulletins, newsletters
ITEM_KINDS = ("story", "press_release", "day_plan", "not_a_story")
# not_checked: nothing to match on (no district or no reporter slug, or not a story) · waiting: looked for on the
# newsroom FTP until footage_wait_until · found · not_found: still nothing at footage_wait_until · manual: the desk
FOOTAGE_STATES = ("not_checked", "waiting", "found", "not_found", "manual")
EMBEDDING_DIM = 1024  # BGE-M3


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


class UserPermissionOverride(Base):
    """One permission set on or off for one user, whatever their role says (owner console). No row = the role decides.
    Keys are checked against app.security.CATALOGUE when written; a key that later leaves the catalogue is ignored."""
    __tablename__ = "user_permission_overrides"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(Text, primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = _updated()


class Reporter(Base):
    """The reporter master: phone/email -> city code and reporter code for the slug."""
    __tablename__ = "reporters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_hi: Mapped[str | None] = mapped_column(Text)  # for the reporter line in the story: संवाददाता: …
    slug_code: Mapped[str] = mapped_column(String(16), nullable=False)  # e.g. SUNITA
    city_code: Mapped[str | None] = mapped_column(String(8))  # e.g. UDR; the slug's city (defaults to the district)
    district: Mapped[str | None] = mapped_column(Text)  # free text, kept for older clients
    district_code: Mapped[str | None] = mapped_column(ForeignKey("locations.code"))  # links items to a district
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)  # digits only, with country code
    # The first address, kept for older clients. All addresses (this one included) are in reporter_emails.
    email: Mapped[str | None] = mapped_column(Text, unique=True)  # stored lowercase
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    emails: Mapped[list["ReporterEmail"]] = relationship(
        lazy="selectin", order_by="ReporterEmail.position", cascade="all, delete-orphan")

    @property
    def email_list(self) -> list[str]:
        """Every address, the first one first (a reporter created the old way may only have `email`)."""
        out = [e.email for e in self.emails]
        if self.email and self.email not in out:
            out.insert(0, self.email)
        return out


class ReporterEmail(Base):
    """A reporter can mail from several addresses; a message from any of them is that reporter's."""
    __tablename__ = "reporter_emails"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reporter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reporters.id", ondelete="CASCADE"), nullable=False,
                                                   index=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # stored lowercase
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    created_at: Mapped[datetime] = _created()


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
        CheckConstraint(_in("kind", ITEM_KINDS), name="items_kind_check"),
        CheckConstraint(_in("footage_status", FOOTAGE_STATES), name="items_footage_status_check"),
        CheckConstraint("duplicate_of IS NULL OR duplicate_of <> id", name="items_no_self_duplicate"),
        Index("items_received_idx", "received_at"),
        Index("items_conversation_idx", "source", "conversation_key"),
        Index("items_kind_received_idx", "kind", "received_at"),
        Index("items_slug_key_idx", "slug_key"),
        Index("items_slug_lower_idx", text("lower(slug)")),  # same_slug_ci: one folder per slug, any case
        Index("items_footage_due_idx", "footage_checked_at",
              postgresql_where=text("footage_status IN ('waiting', 'found')")),
        Index("items_event_key_idx", "event_key", postgresql_where=text("event_key IS NOT NULL")),
        Index("items_duplicate_of_idx", "duplicate_of", postgresql_where=text("duplicate_of IS NOT NULL")),
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
    slug: Mapped[str | None] = mapped_column(String(96))  # not unique: one slug, one FTP folder
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
    # True for a random 10%: the desk sees no AI panel or AI draft, so these decisions are clean labels.
    blind: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    embedding = mapped_column(Vector(EMBEDDING_DIM), nullable=True)  # filled by the dedupe step (AI stage)
    event_key: Mapped[str | None] = mapped_column(Text)  # one real-world event across items (AI stage), with embedding
    # Set when the desk marks this item a duplicate, so the card can show of what
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    # The slug the reporter typed (subject, else first line), what it says, and what kind of message this is
    reporter_slug: Mapped[str | None] = mapped_column(Text)
    slug_key: Mapped[str | None] = mapped_column(Text)  # lowercase a-z 0-9 only: matching resends and FTP folders
    parsed: Mapped[dict | None] = mapped_column(JSONB)  # {date, city_token, type, reporter_name}
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="story", default="story")
    district_code: Mapped[str | None] = mapped_column(ForeignKey("locations.code"))
    # The reporter's own footage on the newsroom FTP (app/services/footage.py)
    footage_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="not_checked",
                                                default="not_checked")
    footage_path: Mapped[str | None] = mapped_column(Text)
    footage_files: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list)
    footage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", default=0)
    footage_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    footage_wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    reporter: Mapped["Reporter | None"] = relationship(lazy="selectin")  # not "joined": rows are locked FOR UPDATE
    district: Mapped["Location | None"] = relationship(lazy="selectin")
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


class DecisionAction(Base):
    """What the desk can do with an item, and how a training run reads it (target)."""
    __tablename__ = "decision_actions"
    __table_args__ = (CheckConstraint(_in("target", DECISION_TARGETS), name="decision_actions_target_check"),)

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    label_en: Mapped[str] = mapped_column(Text, nullable=False)
    label_hi: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0", default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)


class RejectReason(Base):
    """Why the desk said no, and which stage (owner) should learn from it."""
    __tablename__ = "reject_reasons"
    __table_args__ = (CheckConstraint(_in("owner", REASON_OWNERS), name="reject_reasons_owner_check"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    label_en: Mapped[str] = mapped_column(Text, nullable=False)
    label_hi: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0", default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)


class ApproveReason(Base):
    """Why the desk said yes (optional on approve)."""
    __tablename__ = "approve_reasons"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    label_en: Mapped[str] = mapped_column(Text, nullable=False)
    label_hi: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0", default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)


class Decision(Base):
    """Append-only. A changed mind is a new row. Each row is a training label: what the desk saw, did, and why."""
    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint("action <> 'reject' OR reject_reason IS NOT NULL", name="reject_needs_reason"),
        CheckConstraint("action <> 'duplicate' OR duplicate_of IS NOT NULL", name="duplicate_needs_target"),
        CheckConstraint("duplicate_of IS NULL OR duplicate_of <> item_id", name="no_self_duplicate"),
        CheckConstraint("importance IS NULL OR importance BETWEEN 1 AND 5", name="decisions_importance_check"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), ForeignKey("decision_actions.code"), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String(32), ForeignKey("reject_reasons.code"))
    approve_reason: Mapped[str | None] = mapped_column(String(32), ForeignKey("approve_reasons.code"))
    importance: Mapped[int | None] = mapped_column(SmallInteger)  # 1-5, the desk's call
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("items.id"))
    note: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)  # the item as the desk saw it
    seconds_on_item: Mapped[int | None] = mapped_column(Integer)  # from opening the item to deciding
    was_blind: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)
    # What the AI panel showed (empty for blind items, and until the AI stage exists)
    ai_score_shown: Mapped[int | None] = mapped_column(SmallInteger)
    ai_verdict_shown: Mapped[str | None] = mapped_column(Text)
    ai_model_version: Mapped[str | None] = mapped_column(Text)
    # The copy as it went out, and how far the desk moved it from the reporter's
    headline_final: Mapped[str | None] = mapped_column(Text)
    body_final: Mapped[str | None] = mapped_column(Text)
    slug_final: Mapped[str | None] = mapped_column(Text)
    headline_changed: Mapped[bool | None] = mapped_column(Boolean)
    body_edit_distance: Mapped[int | None] = mapped_column(Integer)
    # Filled later by the Octopus read-back
    aired: Mapped[bool | None] = mapped_column(Boolean)
    bulletin: Mapped[str | None] = mapped_column(Text)
    was_lead: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = _created()


Index("decisions_created_idx", Decision.created_at.desc())
Index("decisions_user_created_idx", Decision.user_id, Decision.created_at.desc())


class Audit(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # Who acted when it was not a signed-in user: "owner:<sentry username>" (owner console). NULL otherwise.
    actor: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


Index("audit_target_created_idx", Audit.target_type, Audit.created_at.desc(), Audit.id.desc())


class Job(Base):
    """Background work. The worker claims rows with FOR UPDATE SKIP LOCKED."""
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'done', 'failed')", name="jobs_status_check"),
        Index("jobs_pick_idx", "status", "queue", "run_after"),
        Index("jobs_dedupe_key", "dedupe_key", unique=True, postgresql_where=text("status IN ('pending', 'running')")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    queue: Mapped[str] = mapped_column(String(16), nullable=False, server_default="default", default="default")
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
    name: Mapped[str] = mapped_column(Text, nullable=False)  # English: the feed's <category>
    name_hi: Mapped[str | None] = mapped_column(Text)  # Hindi: the city line and bureau in the story text
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), default=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"), default=list)
    ftp_folder: Mapped[str | None] = mapped_column(Text)  # the district's top folder on the newsroom FTP
    # Optional matcher overrides, e.g. {"containers": false} or {"max_depth": 5}; see app/services/footage.py
    folder_rules: Mapped[dict | None] = mapped_column(JSONB)


class FetchRun(Base):
    """What each pull added: backs the desk's Refresh result and fetch history."""
    __tablename__ = "fetch_runs"
    __table_args__ = (CheckConstraint("source IN ('whatsapp', 'outlook', 'octopus')", name="fetch_runs_source_check"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    added: Mapped[int] = mapped_column(Integer, nullable=False)
    titles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"), default=list)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class Story(Base):
    """What we published to Octopus. One item can have several stories (original, then UPDATEs, then KILL).
    A story row is never edited after it is ready: Octopus ignores a GUID it has seen, so a change is a new row."""
    __tablename__ = "stories"
    __table_args__ = (
        CheckConstraint(_in("state", STORY_STATES), name="stories_state_check"),
        CheckConstraint(_in("kind", STORY_KINDS), name="stories_kind_check"),
        UniqueConstraint("item_id", "version", name="stories_item_version_key"),
        Index("stories_feed_idx", "state", "published_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stories.id"))
    slug: Mapped[str] = mapped_column(String(96), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)  # FLASH / UPDATE / KILL prefix + slug
    description: Mapped[str] = mapped_column(Text, nullable=False)  # city line, body, reporter line, (( )) block
    district: Mapped[str] = mapped_column(Text, nullable=False)  # the feed's first <category>
    guid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # vartmaan-{story id}-v{version}, never reused
    read_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when it enters the feed
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = _created()


class FeedLog(Base):
    """Every request for the Octopus feed. RSS has no delivery receipt; this log is our only proof of collection."""
    __tablename__ = "feed_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    client_ip: Mapped[str | None] = mapped_column(Text)
    status: Mapped[int] = mapped_column(Integer, nullable=False)  # 200, 304, or 404 when refused
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    user_agent: Mapped[str | None] = mapped_column(Text)


class KV(Base):
    """Small bits of state, e.g. the Outlook delta link."""
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _updated()
