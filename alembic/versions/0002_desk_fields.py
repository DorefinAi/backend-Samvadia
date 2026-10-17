"""desk and portal fields: category, priority, tags, city override, slug_edited, locations, fetch_runs;
source 'email' renamed to 'outlook' to match the desk UI

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Same codes as assignment-desk/src/lib/slug.ts LOCATIONS
LOCATIONS = [
    ("JPR", "Jaipur"), ("JDH", "Jodhpur"), ("KOT", "Kota"), ("UDR", "Udaipur"), ("AJM", "Ajmer"),
    ("BKN", "Bikaner"), ("ALW", "Alwar"), ("BHL", "Bhilwara"), ("SKR", "Sikar"), ("BRT", "Bharatpur"),
    ("SGN", "Sri Ganganagar"), ("SWM", "Sawai Madhopur"), ("BMR", "Barmer"), ("DEL", "Delhi / National"),
]

UPGRADE = [
    "ALTER TABLE items ADD COLUMN category TEXT",
    "ALTER TABLE items ADD COLUMN priority VARCHAR(16) DEFAULT 'normal' NOT NULL",
    "ALTER TABLE items ADD COLUMN tags TEXT[] DEFAULT '{}' NOT NULL",
    "ALTER TABLE items ADD COLUMN city_code VARCHAR(8)",
    "ALTER TABLE items ADD COLUMN slug_edited BOOLEAN DEFAULT false NOT NULL",
    "ALTER TABLE items ADD CONSTRAINT items_priority_check CHECK (priority IN ('normal', 'high', 'breaking'))",
    # email -> outlook
    "ALTER TABLE items DROP CONSTRAINT items_source_check",
    "ALTER TABLE messages DROP CONSTRAINT messages_source_check",
    "UPDATE items SET source = 'outlook' WHERE source = 'email'",
    "UPDATE messages SET source = 'outlook' WHERE source = 'email'",
    "UPDATE media SET source = 'outlook' WHERE source = 'email'",
    "ALTER TABLE items ADD CONSTRAINT items_source_check CHECK (source IN ('portal', 'outlook', 'whatsapp'))",
    "ALTER TABLE messages ADD CONSTRAINT messages_source_check CHECK (source IN ('portal', 'outlook', 'whatsapp'))",
    """CREATE TABLE locations (
        code VARCHAR(8) NOT NULL,
        name TEXT NOT NULL,
        active BOOLEAN DEFAULT true NOT NULL,
        PRIMARY KEY (code)
    )""",
    """CREATE TABLE fetch_runs (
        id BIGSERIAL NOT NULL,
        source VARCHAR(16) NOT NULL,
        added INTEGER NOT NULL,
        titles TEXT[] DEFAULT '{}' NOT NULL,
        at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT fetch_runs_source_check CHECK (source IN ('whatsapp', 'outlook', 'octopus'))
    )""",
    "CREATE INDEX ix_fetch_runs_at ON fetch_runs (at)",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)
    values = ", ".join(f"('{code}', '{name}')" for code, name in LOCATIONS)
    op.execute(f"INSERT INTO locations (code, name) VALUES {values}")


def downgrade() -> None:
    for statement in [
        "DROP TABLE fetch_runs", "DROP TABLE locations",
        "ALTER TABLE items DROP CONSTRAINT items_source_check",
        "ALTER TABLE messages DROP CONSTRAINT messages_source_check",
        "UPDATE items SET source = 'email' WHERE source = 'outlook'",
        "UPDATE messages SET source = 'email' WHERE source = 'outlook'",
        "UPDATE media SET source = 'email' WHERE source = 'outlook'",
        "ALTER TABLE items ADD CONSTRAINT items_source_check CHECK (source IN ('portal', 'email', 'whatsapp'))",
        "ALTER TABLE messages ADD CONSTRAINT messages_source_check CHECK (source IN ('portal', 'email', 'whatsapp'))",
        "ALTER TABLE items DROP CONSTRAINT items_priority_check",
        "ALTER TABLE items DROP COLUMN slug_edited", "ALTER TABLE items DROP COLUMN city_code",
        "ALTER TABLE items DROP COLUMN tags", "ALTER TABLE items DROP COLUMN priority",
        "ALTER TABLE items DROP COLUMN category",
    ]:
        op.execute(statement)
