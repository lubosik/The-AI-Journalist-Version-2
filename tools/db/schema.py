"""Idempotent database compatibility upgrades for the deployed HERALD schema."""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

_schema_lock = asyncio.Lock()
_schema_ready = False


def normalise_async_database_url(raw_url: str) -> tuple[str, dict]:
    """Return an asyncpg URL and connect args without libpq-only SSL options."""
    if not raw_url:
        return "", {}

    parsed = urlsplit(raw_url)
    scheme = parsed.scheme
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql+asyncpg"
    elif scheme.startswith("postgresql+") and scheme != "postgresql+asyncpg":
        scheme = "postgresql+asyncpg"

    query = []
    ssl_mode = ""
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"sslmode", "ssl"}:
            ssl_mode = value.lower()
        else:
            query.append((key, value))

    connect_args: dict = {}
    if ssl_mode not in {"", "disable", "false", "0", "off", "allow", "prefer"}:
        context = ssl.create_default_context()
        if ssl_mode == "require":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = context

    return (
        urlunsplit(
            (
                scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            )
        ),
        connect_args,
    )


SCHEMA_UPGRADE_SQL = """
ALTER TABLE content_items
    ADD COLUMN IF NOT EXISTS content text,
    ADD COLUMN IF NOT EXISTS created_at timestamptz,
    ADD COLUMN IF NOT EXISTS metadata jsonb,
    ADD COLUMN IF NOT EXISTS source_name text,
    ADD COLUMN IF NOT EXISTS author_handle text,
    ADD COLUMN IF NOT EXISTS raw_text text,
    ADD COLUMN IF NOT EXISTS published_at timestamptz,
    ADD COLUMN IF NOT EXISTS scraped_at timestamptz,
    ADD COLUMN IF NOT EXISTS language text,
    ADD COLUMN IF NOT EXISTS is_voice_sample boolean,
    ADD COLUMN IF NOT EXISTS is_deal_signal boolean,
    ADD COLUMN IF NOT EXISTS topics jsonb,
    ADD COLUMN IF NOT EXISTS assigned_edition_date date;

UPDATE content_items
SET raw_text = COALESCE(raw_text, content),
    scraped_at = COALESCE(scraped_at, created_at, now()),
    created_at = COALESCE(created_at, scraped_at, now()),
    metadata = COALESCE(metadata, '{}'::jsonb),
    source_name = COALESCE(
        source_name,
        metadata->>'source_name',
        metadata->>'author',
        source_type
    ),
    published_at = COALESCE(
        published_at,
        CASE
            WHEN (metadata->>'published_at') ~
                 '^\\d{4}-\\d{2}-\\d{2}T'
            THEN (metadata->>'published_at')::timestamptz
            ELSE created_at
        END
    ),
    language = COALESCE(language, 'en'),
    is_voice_sample = COALESCE(is_voice_sample, false),
    is_deal_signal = COALESCE(is_deal_signal, false),
    topics = COALESCE(topics, '[]'::jsonb)
WHERE raw_text IS NULL
   OR scraped_at IS NULL
   OR source_name IS NULL
   OR published_at IS NULL
   OR language IS NULL
   OR is_voice_sample IS NULL
   OR is_deal_signal IS NULL
   OR topics IS NULL;

ALTER TABLE content_items
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN metadata SET DEFAULT '{}'::jsonb,
    ALTER COLUMN scraped_at SET DEFAULT now(),
    ALTER COLUMN language SET DEFAULT 'en',
    ALTER COLUMN is_voice_sample SET DEFAULT false,
    ALTER COLUMN is_deal_signal SET DEFAULT false,
    ALTER COLUMN topics SET DEFAULT '[]'::jsonb;

ALTER TABLE edition_topics
    ADD COLUMN IF NOT EXISTS metadata jsonb,
    ADD COLUMN IF NOT EXISTS status text,
    ADD COLUMN IF NOT EXISTS edition_number integer,
    ADD COLUMN IF NOT EXISTS topic_type text,
    ADD COLUMN IF NOT EXISTS priority integer,
    ADD COLUMN IF NOT EXISTS source text,
    ADD COLUMN IF NOT EXISTS added_by text,
    ADD COLUMN IF NOT EXISTS used boolean;

UPDATE edition_topics
SET metadata = COALESCE(metadata, '{}'::jsonb),
    edition_number = COALESCE(
        edition_number,
        CASE
            WHEN (metadata->>'edition_number') ~ '^\\d+$'
            THEN (metadata->>'edition_number')::integer
        END,
        1
    ),
    topic_type = COALESCE(topic_type, metadata->>'topic_type', 'topic'),
    priority = COALESCE(
        priority,
        CASE
            WHEN (metadata->>'priority') ~ '^\\d+$'
            THEN (metadata->>'priority')::integer
        END,
        5
    ),
    source = COALESCE(source, metadata->>'source'),
    added_by = COALESCE(added_by, metadata->>'added_by', 'dom'),
    used = CASE WHEN status = 'used' THEN true ELSE COALESCE(used, false) END;

ALTER TABLE edition_topics
    ALTER COLUMN metadata SET DEFAULT '{}'::jsonb,
    ALTER COLUMN edition_number SET DEFAULT 1,
    ALTER COLUMN topic_type SET DEFAULT 'topic',
    ALTER COLUMN priority SET DEFAULT 5,
    ALTER COLUMN added_by SET DEFAULT 'dom',
    ALTER COLUMN used SET DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_content_items_scraped_at
    ON content_items (scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_items_source_name
    ON content_items (source_name);
CREATE INDEX IF NOT EXISTS idx_edition_topics_edition_used
    ON edition_topics (edition_number, used);
"""


async def ensure_application_schema(raw_url: str | None = None) -> None:
    """Upgrade old Supabase tables once per process before editorial queries run."""
    global _schema_ready
    if _schema_ready:
        return

    async with _schema_lock:
        if _schema_ready:
            return
        raw_url = raw_url or os.getenv("SUPABASE_DB_URI_ASYNC") or os.getenv(
            "SUPABASE_DB_URI", ""
        )
        database_url, connect_args = normalise_async_database_url(raw_url)
        if not database_url:
            logger.warning("Database schema check skipped: no PostgreSQL URI configured")
            return

        engine = create_async_engine(database_url, connect_args=connect_args)
        try:
            async with engine.begin() as connection:
                for statement in SCHEMA_UPGRADE_SQL.split(";"):
                    if statement.strip():
                        await connection.execute(text(statement))
            _schema_ready = True
            logger.info("HERALD database schema compatibility check complete")
        finally:
            await engine.dispose()
