"""initial schema: users, reporters, sessions, items, messages, media, decisions, audit, jobs, events, kv

Revision ID: 0001
Revises:
Create Date: 2026-10-17
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = r"""
CREATE TABLE events (
	id BIGSERIAL NOT NULL, 
	app VARCHAR(16) NOT NULL, 
	type VARCHAR(48) NOT NULL, 
	payload JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE jobs (
	id BIGSERIAL NOT NULL, 
	type VARCHAR(48) NOT NULL, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	status VARCHAR(16) DEFAULT 'pending' NOT NULL, 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	max_attempts INTEGER DEFAULT '4' NOT NULL, 
	run_after TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	locked_at TIMESTAMP WITH TIME ZONE, 
	last_error TEXT, 
	dedupe_key TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT jobs_status_check CHECK (status IN ('pending', 'running', 'done', 'failed'))
);

CREATE UNIQUE INDEX jobs_dedupe_key ON jobs (dedupe_key) WHERE status IN ('pending', 'running');

CREATE INDEX jobs_pick_idx ON jobs (status, run_after);

CREATE TABLE kv (
	key TEXT NOT NULL, 
	value JSONB NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (key)
);

CREATE TABLE users (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	org_id UUID NOT NULL, 
	name TEXT NOT NULL, 
	username TEXT NOT NULL, 
	email TEXT, 
	password_hash TEXT NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	active BOOLEAN DEFAULT true NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT users_role_check CHECK (role IN ('super_admin', 'admin', 'assignment_desk', 'chief_editor', 'reporter')), 
	UNIQUE (username)
);

CREATE TABLE audit (
	id BIGSERIAL NOT NULL, 
	actor_id UUID, 
	action TEXT NOT NULL, 
	target_type TEXT NOT NULL, 
	target_id TEXT NOT NULL, 
	before JSONB, 
	after JSONB, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(actor_id) REFERENCES users (id)
);

CREATE TABLE reporters (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	org_id UUID NOT NULL, 
	name TEXT NOT NULL, 
	slug_code VARCHAR(16) NOT NULL, 
	city_code VARCHAR(8) NOT NULL, 
	district TEXT, 
	phone VARCHAR(20), 
	email TEXT, 
	user_id UUID, 
	active BOOLEAN DEFAULT true NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (phone), 
	UNIQUE (email), 
	UNIQUE (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE sessions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	user_id UUID NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	csrf_token VARCHAR(64) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (token_hash)
);

CREATE TABLE items (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	org_id UUID NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	reporter_id UUID, 
	sender_name TEXT, 
	sender_contact TEXT, 
	conversation_key TEXT, 
	headline TEXT DEFAULT '' NOT NULL, 
	body TEXT DEFAULT '' NOT NULL, 
	slug_subject TEXT, 
	story_type VARCHAR(8), 
	slug VARCHAR(96), 
	ftp_folder TEXT, 
	intake_state VARCHAR(24) DEFAULT 'collecting' NOT NULL, 
	intake_note TEXT, 
	review_state VARCHAR(16) DEFAULT 'pending' NOT NULL, 
	reject_reason VARCHAR(32), 
	reject_note TEXT, 
	reviewed_by UUID, 
	reviewed_at TIMESTAMP WITH TIME ZONE, 
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	last_message_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	version INTEGER DEFAULT '1' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT items_source_check CHECK (source IN ('portal', 'email', 'whatsapp')), 
	CONSTRAINT items_intake_state_check CHECK (intake_state IN ('collecting', 'needs_desk', 'uploading', 'on_ftp', 'ftp_failed')), 
	CONSTRAINT items_review_state_check CHECK (review_state IN ('pending', 'approved', 'rejected')), 
	CONSTRAINT items_story_type_check CHECK (story_type IS NULL OR story_type IN ('AV', 'PKG', 'SF')), 
	FOREIGN KEY(reporter_id) REFERENCES reporters (id), 
	UNIQUE (slug), 
	FOREIGN KEY(reviewed_by) REFERENCES users (id)
);

CREATE INDEX items_conversation_idx ON items (source, conversation_key);

CREATE INDEX items_received_idx ON items (received_at);

CREATE TABLE decisions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	org_id UUID NOT NULL, 
	item_id UUID NOT NULL, 
	action VARCHAR(16) NOT NULL, 
	reason VARCHAR(32), 
	note TEXT, 
	user_id UUID NOT NULL, 
	snapshot JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT decisions_action_check CHECK (action IN ('approve', 'reject', 'reopen')), 
	FOREIGN KEY(item_id) REFERENCES items (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_decisions_item_id ON decisions (item_id);

CREATE TABLE messages (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	item_id UUID NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	external_id TEXT NOT NULL, 
	sender_contact TEXT, 
	text TEXT DEFAULT '' NOT NULL, 
	raw JSONB, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT messages_source_external_id_key UNIQUE (source, external_id), 
	CONSTRAINT messages_source_check CHECK (source IN ('portal', 'email', 'whatsapp')), 
	FOREIGN KEY(item_id) REFERENCES items (id) ON DELETE CASCADE
);

CREATE INDEX ix_messages_item_id ON messages (item_id);

CREATE TABLE media (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	item_id UUID NOT NULL, 
	message_id UUID, 
	source VARCHAR(16) NOT NULL, 
	original_name TEXT, 
	file_name TEXT, 
	content_type TEXT, 
	kind VARCHAR(16) DEFAULT 'other' NOT NULL, 
	size_bytes BIGINT, 
	sha256 VARCHAR(64), 
	external_ref TEXT, 
	storage_key TEXT, 
	state VARCHAR(24) NOT NULL, 
	ftp_path TEXT, 
	error TEXT, 
	storage_deleted_at TIMESTAMP WITH TIME ZONE, 
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT media_state_check CHECK (state IN ('awaiting_upload', 'downloading', 'in_storage', 'on_ftp', 'failed', 'skipped', 'link_only')), 
	FOREIGN KEY(item_id) REFERENCES items (id) ON DELETE CASCADE, 
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE SET NULL
);

CREATE INDEX ix_media_item_id ON media (item_id);
"""


def upgrade() -> None:
    # gen_random_uuid() is built into Postgres 13+ (Supabase runs 15+), so no extension is needed.
    # One statement per execute: the driver refuses several statements in one call.
    for statement in SCHEMA.split(";\n"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in ['media', 'messages', 'decisions', 'items', 'sessions', 'reporters', 'audit', 'users', 'kv', 'jobs', 'events']:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
