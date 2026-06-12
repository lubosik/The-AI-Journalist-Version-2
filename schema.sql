-- HERALD Intelligence — Chainlit + App Schema
-- Run this once in Supabase SQL Editor (Database → SQL Editor → New query)

-- ── Chainlit core tables ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
  "id"          TEXT PRIMARY KEY,
  "identifier"  TEXT NOT NULL UNIQUE,
  "metadata"    JSONB NOT NULL DEFAULT '{}',
  "createdAt"   TEXT
);

CREATE TABLE IF NOT EXISTS threads (
  "id"             TEXT PRIMARY KEY,
  "createdAt"      TEXT,
  "name"           TEXT,
  "userId"         TEXT REFERENCES users("id") ON DELETE SET NULL,
  "userIdentifier" TEXT,
  "tags"           TEXT[],
  "metadata"       JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS steps (
  "id"            TEXT PRIMARY KEY,
  "name"          TEXT NOT NULL,
  "type"          TEXT NOT NULL,
  "threadId"      TEXT NOT NULL REFERENCES threads("id") ON DELETE CASCADE,
  "parentId"      TEXT,
  "streaming"     BOOLEAN NOT NULL DEFAULT FALSE,
  "waitForAnswer" BOOLEAN,
  "isError"       BOOLEAN,
  "metadata"      JSONB DEFAULT '{}',
  "tags"          TEXT[],
  "input"         TEXT,
  "output"        TEXT,
  "createdAt"     TEXT,
  "start"         TEXT,
  "end"           TEXT,
  "generation"    JSONB,
  "showInput"     TEXT,
  "language"      TEXT,
  "indent"        INTEGER
);

CREATE TABLE IF NOT EXISTS elements (
  "id"           TEXT PRIMARY KEY,
  "threadId"     TEXT REFERENCES threads("id") ON DELETE CASCADE,
  "type"         TEXT,
  "chainlitKey"  TEXT,
  "url"          TEXT,
  "objectKey"    TEXT,
  "name"         TEXT NOT NULL,
  "display"      TEXT,
  "size"         TEXT,
  "language"     TEXT,
  "page"         INTEGER,
  "forId"        TEXT,
  "mime"         TEXT,
  "props"        JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS feedbacks (
  "id"       TEXT PRIMARY KEY,
  "forId"    TEXT NOT NULL,
  "threadId" TEXT NOT NULL,
  "value"    NUMERIC NOT NULL,
  "comment"  TEXT
);

-- ── HERALD app tables ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS content_items (
  "id"           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "source_url"   TEXT,
  "source_name"  TEXT,
  "title"        TEXT,
  "content"      TEXT,
  "raw_text"     TEXT,
  "summary"      TEXT,
  "tags"         TEXT[],
  "source_type"  TEXT,
  "published_at" TIMESTAMPTZ,
  "scraped_at"   TIMESTAMPTZ DEFAULT NOW(),
  "created_at"   TIMESTAMPTZ DEFAULT NOW(),
  "metadata"     JSONB DEFAULT '{}'
);

-- Safe column additions for environments where content_items already exists
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS "source_name" TEXT;
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS "raw_text"    TEXT;
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS "published_at" TIMESTAMPTZ;
ALTER TABLE content_items ADD COLUMN IF NOT EXISTS "scraped_at"  TIMESTAMPTZ DEFAULT NOW();

CREATE TABLE IF NOT EXISTS edition_topics (
  "id"          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "topic"       TEXT NOT NULL,
  "notes"       TEXT,
  "status"      TEXT DEFAULT 'pending',
  "created_at"  TIMESTAMPTZ DEFAULT NOW(),
  "metadata"    JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversation_memory (
  "id"         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "role"       TEXT NOT NULL,
  "content"    TEXT NOT NULL,
  "created_at" TIMESTAMPTZ DEFAULT NOW()
);

-- ── Collaboration and notifications ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workspaces (
  "id"          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "name"        TEXT NOT NULL,
  "slug"        TEXT NOT NULL UNIQUE,
  "created_by"  TEXT REFERENCES users("id") ON DELETE SET NULL,
  "metadata"    JSONB NOT NULL DEFAULT '{}',
  "created_at"  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  "updated_at"  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workspace_memberships (
  "id"            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "workspace_id"  UUID NOT NULL REFERENCES workspaces("id") ON DELETE CASCADE,
  "user_id"       TEXT NOT NULL REFERENCES users("id") ON DELETE CASCADE,
  "role"          TEXT NOT NULL DEFAULT 'member'
                  CHECK ("role" IN ('owner', 'admin', 'editor', 'member', 'viewer')),
  "invited_by"    TEXT REFERENCES users("id") ON DELETE SET NULL,
  "created_at"    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  "updated_at"    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE ("workspace_id", "user_id")
);

CREATE TABLE IF NOT EXISTS notifications (
  "id"             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "workspace_id"   UUID REFERENCES workspaces("id") ON DELETE CASCADE,
  "recipient_id"   TEXT NOT NULL REFERENCES users("id") ON DELETE CASCADE,
  "actor_id"       TEXT REFERENCES users("id") ON DELETE SET NULL,
  "kind"           TEXT NOT NULL,
  "title"          TEXT NOT NULL,
  "body"           TEXT,
  "resource_type"  TEXT,
  "resource_id"    TEXT,
  "data"           JSONB NOT NULL DEFAULT '{}',
  "read_at"        TIMESTAMPTZ,
  "push_sent_at"   TIMESTAMPTZ,
  "created_at"     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mentions (
  "id"             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "workspace_id"   UUID NOT NULL REFERENCES workspaces("id") ON DELETE CASCADE,
  "mentioned_id"   TEXT NOT NULL REFERENCES users("id") ON DELETE CASCADE,
  "actor_id"       TEXT REFERENCES users("id") ON DELETE SET NULL,
  "resource_type"  TEXT NOT NULL,
  "resource_id"    TEXT NOT NULL,
  "excerpt"        TEXT,
  "notification_id" UUID REFERENCES notifications("id") ON DELETE SET NULL,
  "created_at"     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE ("workspace_id", "mentioned_id", "resource_type", "resource_id")
);

CREATE TABLE IF NOT EXISTS web_push_subscriptions (
  "id"          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  "user_id"     TEXT NOT NULL REFERENCES users("id") ON DELETE CASCADE,
  "endpoint"    TEXT NOT NULL UNIQUE,
  "p256dh"      TEXT NOT NULL,
  "auth"        TEXT NOT NULL,
  "user_agent"  TEXT,
  "created_at"  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  "updated_at"  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Indexes for performance ───────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_threads_userid     ON threads("userId");
CREATE INDEX IF NOT EXISTS idx_steps_threadid     ON steps("threadId");
CREATE INDEX IF NOT EXISTS idx_steps_type         ON steps("type");
CREATE INDEX IF NOT EXISTS idx_elements_threadid  ON elements("threadId");
CREATE INDEX IF NOT EXISTS idx_feedbacks_forid    ON feedbacks("forId");
CREATE INDEX IF NOT EXISTS idx_content_created    ON content_items("created_at" DESC);
CREATE INDEX IF NOT EXISTS idx_content_source     ON content_items("source_name");
CREATE INDEX IF NOT EXISTS idx_content_scraped    ON content_items("scraped_at" DESC);
CREATE INDEX IF NOT EXISTS idx_memberships_user   ON workspace_memberships("user_id");
CREATE INDEX IF NOT EXISTS idx_mentions_recipient ON mentions("mentioned_id", "created_at" DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_inbox
  ON notifications("recipient_id", "read_at", "created_at" DESC);
CREATE INDEX IF NOT EXISTS idx_push_user           ON web_push_subscriptions("user_id");

-- Remove fields from the retired external newsletter publishing integration.
ALTER TABLE IF EXISTS newsletter_issues
  DROP COLUMN IF EXISTS beehiiv_post_id,
  DROP COLUMN IF EXISTS beehiiv_url;
