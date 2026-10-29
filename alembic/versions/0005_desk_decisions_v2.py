"""desk decisions v2: lookup tables for actions and reasons (English/Hindi labels), richer decision rows for
training, three new review states (held, sent_back, duplicate), ten story types, items.event_key and duplicate_of

Runs on existing data, nothing is dropped on upgrade:
- decision_actions, reject_reasons, approve_reasons are created and seeded (re-running the inserts is harmless)
- decisions.reason is renamed reject_reason; 'poor_visuals' becomes 'no_visuals' (decisions and items); a reject
  with no reason, or a reason that is not in reject_reasons, becomes 'other' so the foreign key holds
- decisions.action keeps its values (approve, reject, reopen are all seeded); the CHECK becomes a foreign key
- decisions.was_blind is copied from items.blind for existing rows (blind is set at intake and never changes)
- every other new column is NULL or has a default

Downgrade is lossy where the old schema cannot hold the new values:
- decisions rows with action needs_more / hold / duplicate are DELETED (the old CHECK allows only approve, reject,
  reopen) and every new decisions column is dropped
- items in held / sent_back / duplicate go back to pending; story types other than AV, PKG, SF become NULL
- 'no_visuals' goes back to 'poor_visuals'; items.event_key and items.duplicate_of are dropped

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Frozen copies of the seed: code, label_en, label_hi, target/owner, sort_order
ACTIONS = [
    ("approve", "Approve", "स्वीकार", "positive", 1),
    ("needs_more", "Send back to reporter", "रिपोर्टर को वापस", "weak_positive", 2),
    ("hold", "Hold", "रोके रखें", "exclude", 3),
    ("duplicate", "Duplicate of…", "डुप्लिकेट", "exclude", 4),
    ("reject", "Reject", "अस्वीकार", "negative", 5),
    ("reopen", "Reopen", "फिर से खोलें", "exclude", 6),  # undoes a wrong decision; left out of training
]
REJECT = [
    ("duplicate", "Already have this story", "खबर पहले से है", "dedupe", 1),
    ("not_newsworthy", "Not newsworthy for us", "हमारे लिए खबर नहीं", "scorer", 2),
    ("pr_puff", "Release / event, no news value", "केवल विज्ञप्ति, खबर नहीं", "scorer", 3),
    ("routine_event", "Routine diary event", "रूटीन कार्यक्रम", "scorer", 4),
    ("unverified", "Cannot stand it up", "पुष्टि नहीं", "policy", 5),
    ("no_visuals", "No usable visuals", "विजुअल नहीं", "intake", 6),
    ("incomplete", "Missing quote / key facts", "अपूर्ण सामग्री", "intake", 7),
    ("wrong_beat", "Not our coverage area", "हमारा क्षेत्र नहीं", "scorer", 8),
    ("too_late", "Missed the slot / stale", "समय निकल गया", "intake", 9),
    ("legal_risk", "Legal risk", "कानूनी जोखिम", "policy", 10),
    ("communal_risk", "Communally sensitive", "सांप्रदायिक संवेदनशीलता", "policy", 11),
    ("quality", "Copy unusable", "कॉपी अनुपयोगी", "intake", 12),
    ("other", "Other (explain)", "अन्य (कारण लिखें)", "desk", 13),
]
APPROVE = [
    ("must_carry", "Must carry (official / VIP)", "अनिवार्य (आधिकारिक)", 1),
    ("exclusive", "Exclusive / ours first", "एक्सक्लूसिव", 2),
    ("good_visual", "Strong visuals", "अच्छे विजुअल", 3),
    ("audience", "High audience interest", "दर्शक रुचि", 4),
    ("ready_to_air", "Fully produced, ready to air", "पूरी तैयार स्क्रिप्ट", 5),
    ("follow_up", "Follow-up on a running story", "फॉलो-अप", 6),
    ("filler", "Filler / slow day", "फिलर", 7),
]

OLD_STORY_TYPES = "('AV', 'PKG', 'SF')"
NEW_STORY_TYPES = "('AV', 'AVB', 'PKG', 'SF', 'WT', 'PTC', 'BYTE', 'LIVE', 'TEXT', 'BRIEF')"
OLD_REVIEW_STATES = "('pending', 'approved', 'rejected')"
NEW_REVIEW_STATES = "('pending', 'approved', 'rejected', 'held', 'sent_back', 'duplicate')"

TABLES = [
    """CREATE TABLE decision_actions (
        code VARCHAR(16) NOT NULL,
        label_en TEXT NOT NULL,
        label_hi TEXT NOT NULL,
        target VARCHAR(16) NOT NULL,
        sort_order SMALLINT DEFAULT '0' NOT NULL,
        active BOOLEAN DEFAULT true NOT NULL,
        PRIMARY KEY (code),
        CONSTRAINT decision_actions_target_check CHECK (target IN ('positive', 'negative', 'weak_positive', 'exclude'))
    )""",
    """CREATE TABLE reject_reasons (
        code VARCHAR(32) NOT NULL,
        label_en TEXT NOT NULL,
        label_hi TEXT NOT NULL,
        owner VARCHAR(16) NOT NULL,
        sort_order SMALLINT DEFAULT '0' NOT NULL,
        active BOOLEAN DEFAULT true NOT NULL,
        PRIMARY KEY (code),
        CONSTRAINT reject_reasons_owner_check CHECK (owner IN ('scorer', 'dedupe', 'intake', 'policy', 'desk'))
    )""",
    """CREATE TABLE approve_reasons (
        code VARCHAR(32) NOT NULL,
        label_en TEXT NOT NULL,
        label_hi TEXT NOT NULL,
        sort_order SMALLINT DEFAULT '0' NOT NULL,
        active BOOLEAN DEFAULT true NOT NULL,
        PRIMARY KEY (code)
    )""",
]

DECISIONS = [
    "ALTER TABLE decisions DROP CONSTRAINT decisions_action_check",
    "ALTER TABLE decisions RENAME COLUMN reason TO reject_reason",
    "UPDATE decisions SET reject_reason = 'no_visuals' WHERE reject_reason = 'poor_visuals'",
    "UPDATE decisions SET reject_reason = 'other' WHERE reject_reason IS NOT NULL "
    "AND reject_reason NOT IN (SELECT code FROM reject_reasons)",
    "UPDATE decisions SET reject_reason = 'other' WHERE action = 'reject' AND reject_reason IS NULL",
    "UPDATE items SET reject_reason = 'no_visuals' WHERE reject_reason = 'poor_visuals'",
    "ALTER TABLE decisions ADD COLUMN approve_reason VARCHAR(32)",
    "ALTER TABLE decisions ADD COLUMN importance SMALLINT",
    "ALTER TABLE decisions ADD COLUMN duplicate_of UUID",
    "ALTER TABLE decisions ADD COLUMN seconds_on_item INTEGER",
    "ALTER TABLE decisions ADD COLUMN was_blind BOOLEAN DEFAULT false NOT NULL",
    "ALTER TABLE decisions ADD COLUMN ai_score_shown SMALLINT",
    "ALTER TABLE decisions ADD COLUMN ai_verdict_shown TEXT",
    "ALTER TABLE decisions ADD COLUMN ai_model_version TEXT",
    "ALTER TABLE decisions ADD COLUMN headline_final TEXT",
    "ALTER TABLE decisions ADD COLUMN body_final TEXT",
    "ALTER TABLE decisions ADD COLUMN slug_final TEXT",
    "ALTER TABLE decisions ADD COLUMN headline_changed BOOLEAN",
    "ALTER TABLE decisions ADD COLUMN body_edit_distance INTEGER",
    "ALTER TABLE decisions ADD COLUMN aired BOOLEAN",
    "ALTER TABLE decisions ADD COLUMN bulletin TEXT",
    "ALTER TABLE decisions ADD COLUMN was_lead BOOLEAN",
    "UPDATE decisions d SET was_blind = i.blind FROM items i WHERE i.id = d.item_id AND i.blind",
    "ALTER TABLE decisions ADD CONSTRAINT decisions_action_fkey "
    "FOREIGN KEY (action) REFERENCES decision_actions (code)",
    "ALTER TABLE decisions ADD CONSTRAINT decisions_reject_reason_fkey "
    "FOREIGN KEY (reject_reason) REFERENCES reject_reasons (code)",
    "ALTER TABLE decisions ADD CONSTRAINT decisions_approve_reason_fkey "
    "FOREIGN KEY (approve_reason) REFERENCES approve_reasons (code)",
    "ALTER TABLE decisions ADD CONSTRAINT decisions_duplicate_of_fkey "
    "FOREIGN KEY (duplicate_of) REFERENCES items (id)",
    "ALTER TABLE decisions ADD CONSTRAINT reject_needs_reason "
    "CHECK (action <> 'reject' OR reject_reason IS NOT NULL)",
    "ALTER TABLE decisions ADD CONSTRAINT duplicate_needs_target "
    "CHECK (action <> 'duplicate' OR duplicate_of IS NOT NULL)",
    "ALTER TABLE decisions ADD CONSTRAINT no_self_duplicate CHECK (duplicate_of IS NULL OR duplicate_of <> item_id)",
    "ALTER TABLE decisions ADD CONSTRAINT decisions_importance_check "
    "CHECK (importance IS NULL OR importance BETWEEN 1 AND 5)",
    "CREATE INDEX IF NOT EXISTS decisions_created_idx ON decisions (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS decisions_user_created_idx ON decisions (user_id, created_at DESC)",
]

ITEMS = [
    "ALTER TABLE items ADD COLUMN event_key TEXT",
    "ALTER TABLE items ADD COLUMN duplicate_of UUID",
    "ALTER TABLE items ADD CONSTRAINT items_duplicate_of_fkey "
    "FOREIGN KEY (duplicate_of) REFERENCES items (id) ON DELETE SET NULL",
    "ALTER TABLE items ADD CONSTRAINT items_no_self_duplicate CHECK (duplicate_of IS NULL OR duplicate_of <> id)",
    "CREATE INDEX items_event_key_idx ON items (event_key) WHERE event_key IS NOT NULL",
    "CREATE INDEX items_duplicate_of_idx ON items (duplicate_of) WHERE duplicate_of IS NOT NULL",
    "ALTER TABLE items DROP CONSTRAINT items_review_state_check",
    f"ALTER TABLE items ADD CONSTRAINT items_review_state_check CHECK (review_state IN {NEW_REVIEW_STATES})",
    "ALTER TABLE items DROP CONSTRAINT items_story_type_check",
    "ALTER TABLE items ADD CONSTRAINT items_story_type_check "
    f"CHECK (story_type IS NULL OR story_type IN {NEW_STORY_TYPES})",
]


def _seed() -> None:
    bind = op.get_bind()
    for code, en, hi, target, order in ACTIONS:
        bind.execute(sa.text(
            "INSERT INTO decision_actions (code, label_en, label_hi, target, sort_order) "
            "VALUES (:code, :en, :hi, :target, :order) ON CONFLICT (code) DO NOTHING"),
            {"code": code, "en": en, "hi": hi, "target": target, "order": order})
    for code, en, hi, owner, order in REJECT:
        bind.execute(sa.text(
            "INSERT INTO reject_reasons (code, label_en, label_hi, owner, sort_order) "
            "VALUES (:code, :en, :hi, :owner, :order) ON CONFLICT (code) DO NOTHING"),
            {"code": code, "en": en, "hi": hi, "owner": owner, "order": order})
    for code, en, hi, order in APPROVE:
        bind.execute(sa.text(
            "INSERT INTO approve_reasons (code, label_en, label_hi, sort_order) "
            "VALUES (:code, :en, :hi, :order) ON CONFLICT (code) DO NOTHING"),
            {"code": code, "en": en, "hi": hi, "order": order})


def upgrade() -> None:
    for statement in TABLES:
        op.execute(statement)
    _seed()  # before DECISIONS: the reason clean-up and the foreign keys need the rows
    for statement in DECISIONS + ITEMS:
        op.execute(statement)


def downgrade() -> None:
    for statement in [
        # items
        "DROP INDEX items_duplicate_of_idx", "DROP INDEX items_event_key_idx",
        "ALTER TABLE items DROP CONSTRAINT items_no_self_duplicate",
        "ALTER TABLE items DROP CONSTRAINT items_duplicate_of_fkey",
        "ALTER TABLE items DROP COLUMN duplicate_of", "ALTER TABLE items DROP COLUMN event_key",
        "ALTER TABLE items DROP CONSTRAINT items_review_state_check",
        "UPDATE items SET review_state = 'pending' WHERE review_state IN ('held', 'sent_back', 'duplicate')",
        f"ALTER TABLE items ADD CONSTRAINT items_review_state_check CHECK (review_state IN {OLD_REVIEW_STATES})",
        "ALTER TABLE items DROP CONSTRAINT items_story_type_check",
        f"UPDATE items SET story_type = NULL WHERE story_type NOT IN {OLD_STORY_TYPES}",
        "ALTER TABLE items ADD CONSTRAINT items_story_type_check "
        f"CHECK (story_type IS NULL OR story_type IN {OLD_STORY_TYPES})",
        "UPDATE items SET reject_reason = 'poor_visuals' WHERE reject_reason = 'no_visuals'",
        # decisions
        "DROP INDEX IF EXISTS decisions_user_created_idx", "DROP INDEX IF EXISTS decisions_created_idx",
        *(f"ALTER TABLE decisions DROP CONSTRAINT {c}" for c in (
            "decisions_importance_check", "no_self_duplicate", "duplicate_needs_target", "reject_needs_reason",
            "decisions_duplicate_of_fkey", "decisions_approve_reason_fkey", "decisions_reject_reason_fkey",
            "decisions_action_fkey")),
        *(f"ALTER TABLE decisions DROP COLUMN {c}" for c in (
            "was_lead", "bulletin", "aired", "body_edit_distance", "headline_changed", "slug_final", "body_final",
            "headline_final", "ai_model_version", "ai_verdict_shown", "ai_score_shown", "was_blind",
            "seconds_on_item", "duplicate_of", "importance", "approve_reason")),
        "DELETE FROM decisions WHERE action NOT IN ('approve', 'reject', 'reopen')",
        "UPDATE decisions SET reject_reason = 'poor_visuals' WHERE reject_reason = 'no_visuals'",
        "ALTER TABLE decisions RENAME COLUMN reject_reason TO reason",
        "ALTER TABLE decisions ADD CONSTRAINT decisions_action_check CHECK (action IN ('approve', 'reject', 'reopen'))",
        "DROP TABLE approve_reasons", "DROP TABLE reject_reasons", "DROP TABLE decision_actions",
    ]:
        op.execute(statement)
