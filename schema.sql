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
  "title"        TEXT,
  "content"      TEXT,
  "summary"      TEXT,
  "tags"         TEXT[],
  "source_type"  TEXT,
  "created_at"   TIMESTAMPTZ DEFAULT NOW(),
  "metadata"     JSONB DEFAULT '{}'
);

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

-- ── Indexes for performance ───────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_threads_userid     ON threads("userId");
CREATE INDEX IF NOT EXISTS idx_steps_threadid     ON steps("threadId");
CREATE INDEX IF NOT EXISTS idx_steps_type         ON steps("type");
CREATE INDEX IF NOT EXISTS idx_elements_threadid  ON elements("threadId");
CREATE INDEX IF NOT EXISTS idx_feedbacks_forid    ON feedbacks("forId");
CREATE INDEX IF NOT EXISTS idx_content_created    ON content_items("created_at" DESC);
