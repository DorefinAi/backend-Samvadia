"""owner console: per-user permission overrides, and a text actor on audit rows

Runs on existing data, nothing is dropped or rewritten on upgrade:
- user_permission_overrides is new and empty, so every user keeps exactly what their role gives them today
- audit.actor is a new NULL column; it holds "owner:<sentry username>" for changes made through /owner (those rows
  have actor_id NULL). Existing rows keep their actor_id and stay NULL here.
- audit_target_created_idx (target_type, created_at DESC, id DESC) serves GET /owner/history and its cursor
- lock_timeout is 5s: if a busy audit table cannot be locked in time the migration fails instead of queueing every
  request behind it; run it again when quieter

Downgrade is lossy, so it refuses while any override exists unless ALLOW_OVERRIDE_DATA_LOSS=1 is set:
- every override is DELETED with its table (users fall back to their role); the count is printed first
- audit.actor is dropped, so owner changes in the audit log lose who made them (their actor_id is NULL)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

UPGRADE = [
    LOCK_TIMEOUT,
    """
    CREATE TABLE IF NOT EXISTS user_permission_overrides (
        user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        permission text NOT NULL,
        allowed boolean NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, permission)
    )
    """,
    "ALTER TABLE audit ADD COLUMN IF NOT EXISTS actor text",
    "CREATE INDEX IF NOT EXISTS audit_target_created_idx ON audit (target_type, created_at DESC, id DESC)",
]

DOWNGRADE = [
    "DROP INDEX IF EXISTS audit_target_created_idx",
    "ALTER TABLE audit DROP COLUMN IF EXISTS actor",
    "DROP TABLE IF EXISTS user_permission_overrides",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    bind = op.get_bind()
    exists = bind.execute(sa.text("SELECT to_regclass('user_permission_overrides')")).scalar()
    count = bind.execute(sa.text("SELECT count(*) FROM user_permission_overrides")).scalar() if exists else 0
    if count:
        print(f"0006 downgrade: {count} permission override(s) will be deleted.")
        if os.environ.get("ALLOW_OVERRIDE_DATA_LOSS") != "1":
            raise RuntimeError(f"Refusing to delete {count} permission override(s). "
                               "Set ALLOW_OVERRIDE_DATA_LOSS=1 to downgrade anyway.")
    for statement in DOWNGRADE:
        op.execute(statement)
