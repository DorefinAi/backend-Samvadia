"""ops log: every log line (ops_log) and a heartbeat per process (ops_heartbeat) for Sentry's Logs page

dev-docs/ops-logs-contract.md section 1. Runs on existing data: both tables are new, nothing else is touched.
ops_log.id is the cursor for GET /owner/logs; ix_ops_log_at serves the 14-day retention delete (cleanup job).

Downgrade drops both tables, so the stored log lines and heartbeats are lost (operational data only).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE IF NOT EXISTS ops_log (
        id bigserial PRIMARY KEY,
        at timestamptz NOT NULL DEFAULT now(),
        system text NOT NULL,
        level text NOT NULL,
        logger text,
        message text NOT NULL,
        slug text,
        item_id text,
        fields jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ops_log_at ON ops_log (at)",
    """
    CREATE TABLE IF NOT EXISTS ops_heartbeat (
        process text PRIMARY KEY,
        system text NOT NULL,
        host text NOT NULL,
        pid integer NOT NULL,
        started_at timestamptz NOT NULL,
        beat_at timestamptz NOT NULL,
        info jsonb
    )
    """,
]

DOWNGRADE = [
    "DROP TABLE IF EXISTS ops_heartbeat",
    "DROP TABLE IF EXISTS ops_log",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
