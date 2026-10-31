"""one slug, one FTP folder: items.slug is no longer unique

A slug sent again (reporters resend slugs, or a second WhatsApp burst after the merge window) used to become
<slug>_2 with its own folder. Now both items keep the same slug and share <FTP_BASE_DIR>/<slug>/; a file name that
is already in the folder gets _2 instead (app/tasks.py ftp_upload).

Runs on existing data, nothing is dropped or rewritten on upgrade: items already named <slug>_2 keep that name and
folder. The unique constraint is replaced by an index on lower(slug): a new slug that differs only in letter case
joins the existing folder (the newsroom FTP runs on Windows).
lock_timeout is 5s: if a busy items table cannot be locked in time the migration fails; run it again when quieter.

Downgrade puts the unique constraint back, so it refuses while two items share a slug (it lists a few).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE items DROP CONSTRAINT IF EXISTS items_slug_key")
    op.execute("CREATE INDEX IF NOT EXISTS items_slug_lower_idx ON items (lower(slug))")


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    shared = op.get_bind().execute(sa.text(
        "SELECT slug FROM items WHERE slug IS NOT NULL GROUP BY slug HAVING count(*) > 1 LIMIT 5")).scalars().all()
    if shared:
        raise RuntimeError(f"Refusing to make items.slug unique again: these slugs are shared by more than one item "
                           f"({', '.join(shared)}). Give each item its own slug first.")
    op.execute("DROP INDEX IF EXISTS items_slug_lower_idx")
    op.execute("ALTER TABLE items ADD CONSTRAINT items_slug_key UNIQUE (slug)")
