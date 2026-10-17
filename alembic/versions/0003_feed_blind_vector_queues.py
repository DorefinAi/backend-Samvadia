"""Octopus feed (stories, feed_log), 10% blind flag, pgvector embedding column, named job queues,
Hindi names for locations and reporters

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

HINDI_NAMES = {
    "JPR": "जयपुर", "JDH": "जोधपुर", "KOT": "कोटा", "UDR": "उदयपुर", "AJM": "अजमेर", "BKN": "बीकानेर",
    "ALW": "अलवर", "BHL": "भीलवाड़ा", "SKR": "सीकर", "BRT": "भरतपुर", "SGN": "श्रीगंगानगर",
    "SWM": "सवाई माधोपुर", "BMR": "बाड़मेर", "DEL": "दिल्ली",
}

UPGRADE = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "ALTER TABLE items ADD COLUMN blind BOOLEAN DEFAULT false NOT NULL",
    "ALTER TABLE items ADD COLUMN embedding vector(1024)",
    "CREATE INDEX items_embedding_idx ON items USING hnsw (embedding vector_cosine_ops)",
    "ALTER TABLE reporters ADD COLUMN name_hi TEXT",
    "ALTER TABLE locations ADD COLUMN name_hi TEXT",
    "ALTER TABLE jobs ADD COLUMN queue VARCHAR(16) DEFAULT 'default' NOT NULL",
    "DROP INDEX jobs_pick_idx",
    "CREATE INDEX jobs_pick_idx ON jobs (status, queue, run_after)",
    """CREATE TABLE stories (
        id UUID DEFAULT gen_random_uuid() NOT NULL,
        org_id UUID NOT NULL,
        item_id UUID NOT NULL,
        version INTEGER NOT NULL,
        kind VARCHAR(16) NOT NULL,
        replaces_id UUID,
        slug VARCHAR(96) NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        district TEXT NOT NULL,
        guid TEXT NOT NULL,
        read_seconds INTEGER NOT NULL,
        state VARCHAR(16) NOT NULL,
        published_at TIMESTAMP WITH TIME ZONE,
        created_by UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT stories_state_check CHECK (state IN ('waiting', 'ready')),
        CONSTRAINT stories_kind_check CHECK (kind IN ('original', 'update', 'kill')),
        CONSTRAINT stories_item_version_key UNIQUE (item_id, version),
        FOREIGN KEY(item_id) REFERENCES items (id) ON DELETE CASCADE,
        FOREIGN KEY(replaces_id) REFERENCES stories (id),
        UNIQUE (guid),
        FOREIGN KEY(created_by) REFERENCES users (id)
    )""",
    "CREATE INDEX ix_stories_item_id ON stories (item_id)",
    "CREATE INDEX stories_feed_idx ON stories (state, published_at)",
    """CREATE TABLE feed_log (
        id BIGSERIAL NOT NULL,
        at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        client_ip TEXT,
        status INTEGER NOT NULL,
        item_count INTEGER DEFAULT 0 NOT NULL,
        user_agent TEXT,
        PRIMARY KEY (id)
    )""",
    "CREATE INDEX ix_feed_log_at ON feed_log (at)",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)
    for code, name in HINDI_NAMES.items():
        op.execute(f"UPDATE locations SET name_hi = '{name}' WHERE code = '{code}'")


def downgrade() -> None:
    for statement in [
        "DROP TABLE feed_log", "DROP TABLE stories",
        "DROP INDEX jobs_pick_idx", "ALTER TABLE jobs DROP COLUMN queue",
        "CREATE INDEX jobs_pick_idx ON jobs (status, run_after)",
        "ALTER TABLE locations DROP COLUMN name_hi", "ALTER TABLE reporters DROP COLUMN name_hi",
        "DROP INDEX items_embedding_idx", "ALTER TABLE items DROP COLUMN embedding",
        "ALTER TABLE items DROP COLUMN blind",
    ]:
        op.execute(statement)
