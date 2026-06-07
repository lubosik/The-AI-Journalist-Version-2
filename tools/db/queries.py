import logging
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from db.client import get_client

logger = logging.getLogger(__name__)


STALE_STORY_MARKERS = (
    "storm duncan",
    "bay area estate",
    "accepted anthropic shares as payment",
    "anthropic shares as payment for a home",
    "vika ventures",
    "iakovou",
    "keyport venture",
    "late stage asset management",
)


def is_stale_known_bad_content(text: str) -> bool:
    """Return True for stale anecdotes Dom has explicitly rejected."""
    lower = (text or "").lower()
    return any(marker in lower for marker in STALE_STORY_MARKERS)


def _effective_age_hours(row: dict) -> float | None:
    """Return age in hours using published_at first, then scraped_at."""
    ts_raw = row.get("published_at") or row.get("scraped_at") or ""
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return None


def get_upcoming_friday_edition(reference: datetime | None = None) -> date:
    """Return the Sunday edition date in America/New_York."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        now = reference.astimezone(et) if reference else datetime.now(et)
    except Exception:
        now = reference or datetime.now(timezone.utc)
    days_ahead = (6 - now.weekday()) % 7
    return (now + timedelta(days=days_ahead)).date()


def content_exists_by_url(source_url: str) -> bool:
    """Check if a content item with the given URL already exists (case-insensitive)."""
    try:
        client = get_client()
        result = client.table("content_items").select("id").ilike("source_url", source_url).execute()
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"content_exists_by_url error for {source_url}: {e}")
        return False


def content_exists_by_hash(content_hash: str) -> bool:
    """Check if content with this hash exists (stored in metadata jsonb)."""
    try:
        client = get_client()
        result = (
            client.table("content_items")
            .select("id")
            .eq("metadata->>content_hash", content_hash)
            .execute()
        )
        return len(result.data) > 0
    except Exception as e:
        logger.error(f"content_exists_by_hash error: {e}")
        return False


def insert_content_item(item: dict) -> str:
    """Insert a content item and return its UUID."""
    try:
        client = get_client()
        result = client.table("content_items").insert(item).execute()
        if result.data:
            return result.data[0]["id"]
        raise ValueError("No data returned from insert")
    except Exception as e:
        logger.error(f"insert_content_item error: {e}")
        raise


def insert_content_chunk(chunk: dict) -> str:
    """Insert a content chunk and return its UUID."""
    try:
        client = get_client()
        result = client.table("content_chunks").insert(chunk).execute()
        if result.data:
            return result.data[0]["id"]
        raise ValueError("No data returned from chunk insert")
    except Exception as e:
        logger.error(f"insert_content_chunk error: {e}")
        raise


def update_content_embedding(chunk_id: str, embedding: list) -> None:
    """Update the embedding vector on a content chunk."""
    try:
        client = get_client()
        client.table("content_chunks").update({"embedding": embedding}).eq("id", chunk_id).execute()
    except Exception as e:
        logger.error(f"update_content_embedding error for chunk {chunk_id}: {e}")
        raise


def semantic_search(query_embedding: list, days_back: int = 2, limit: int = 20) -> list:
    """Semantic similarity search using the match_content_chunks RPC function."""
    try:
        client = get_client()
        result = client.rpc(
            "match_content_chunks",
            {
                "query_embedding": query_embedding,
                "match_threshold": 0.7,
                "match_count": limit,
                "days_back": days_back,
            },
        ).execute()
        return result.data or []
    except Exception as e:
        logger.error(f"semantic_search error: {e}")
        return []


def get_db_stats() -> dict:
    """Get counts by source_type and last scraped timestamps."""
    try:
        client = get_client()

        # Total content items
        total_result = client.table("content_items").select("id", count="exact").execute()
        total_items = total_result.count or 0

        # Items by source_type
        all_items = client.table("content_items").select("source_type, scraped_at").execute()
        by_type: dict = {}
        latest_by_type: dict = {}

        for row in (all_items.data or []):
            st = row.get("source_type", "unknown")
            by_type[st] = by_type.get(st, 0) + 1
            scraped = row.get("scraped_at")
            if scraped:
                if st not in latest_by_type or scraped > latest_by_type[st]:
                    latest_by_type[st] = scraped

        # Total chunks
        chunks_result = client.table("content_chunks").select("id", count="exact").execute()
        total_chunks = chunks_result.count or 0

        return {
            "total_items": total_items,
            "total_chunks": total_chunks,
            "by_source_type": by_type,
            "last_scraped": latest_by_type,
        }
    except Exception as e:
        logger.error(f"get_db_stats error: {e}")
        return {"error": str(e)}


def get_latest_items(limit: int = 5) -> list:
    """Get the most recently scraped content items."""
    try:
        client = get_client()
        result = (
            client.table("content_items")
            .select("id, source_type, source_name, title, source_url, published_at, scraped_at, summary")
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"get_latest_items error: {e}")
        return []


def insert_telegram_tip(tip: dict) -> str:
    """Insert a telegram tip and return its UUID."""
    try:
        client = get_client()
        result = client.table("telegram_tips").insert(tip).execute()
        if result.data:
            return result.data[0]["id"]
        raise ValueError("No data returned from telegram_tips insert")
    except Exception as e:
        logger.error(f"insert_telegram_tip error: {e}")
        raise


def update_tip_validation(tip_id: str, validated: bool, notes: str) -> None:
    """Update the validation status of a telegram tip."""
    try:
        client = get_client()
        client.table("telegram_tips").update(
            {"validated": validated, "validation_notes": notes}
        ).eq("id", tip_id).execute()
    except Exception as e:
        logger.error(f"update_tip_validation error for tip {tip_id}: {e}")
        raise


def get_source_last_scraped(source_type: str, identifier: str) -> Optional[datetime]:
    """Get the last scraped datetime for a source."""
    try:
        client = get_client()
        result = (
            client.table("sources")
            .select("last_scraped_at")
            .eq("source_type", source_type)
            .eq("identifier", identifier)
            .execute()
        )
        if result.data and result.data[0].get("last_scraped_at"):
            raw = result.data[0]["last_scraped_at"]
            if isinstance(raw, str):
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return raw
        return None
    except Exception as e:
        logger.error(f"get_source_last_scraped error for {source_type}/{identifier}: {e}")
        return None


def update_source_last_scraped(source_type: str, identifier: str) -> None:
    """Upsert the last scraped timestamp for a source."""
    try:
        client = get_client()
        now_iso = datetime.now(timezone.utc).isoformat()
        # Try update first
        result = (
            client.table("sources")
            .select("id")
            .eq("source_type", source_type)
            .eq("identifier", identifier)
            .execute()
        )
        if result.data:
            client.table("sources").update({"last_scraped_at": now_iso}).eq(
                "source_type", source_type
            ).eq("identifier", identifier).execute()
        else:
            client.table("sources").insert(
                {
                    "source_type": source_type,
                    "identifier": identifier,
                    "name": identifier,
                    "active": True,
                    "last_scraped_at": now_iso,
                }
            ).execute()
    except Exception as e:
        logger.error(f"update_source_last_scraped error for {source_type}/{identifier}: {e}")


def mark_content_as_voice_sample(content_id: str) -> None:
    """Mark a content item as a voice sample."""
    try:
        client = get_client()
        client.table("content_items").update({"is_voice_sample": True}).eq("id", content_id).execute()
    except Exception as e:
        logger.error(f"mark_content_as_voice_sample error for {content_id}: {e}")
        raise


def get_pipeline_state(key: str) -> str:
    """Get a pipeline state value by key. Returns empty string if not found."""
    try:
        client = get_client()
        result = client.table("pipeline_state").select("value").eq("key", key).execute()
        if result.data:
            return result.data[0]["value"]
        return ""
    except Exception as e:
        logger.error(f"get_pipeline_state error for key={key}: {e}")
        return ""


def set_pipeline_state(key: str, value: str) -> None:
    """Upsert a pipeline state value."""
    try:
        client = get_client()
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        existing = client.table("pipeline_state").select("id").eq("key", key).execute()
        if existing.data:
            client.table("pipeline_state").update(
                {"value": value, "updated_at": now_iso}
            ).eq("key", key).execute()
        else:
            client.table("pipeline_state").insert(
                {"key": key, "value": value, "updated_at": now_iso}
            ).execute()
    except Exception as e:
        logger.error(f"set_pipeline_state error for key={key}: {e}")


def is_pipeline_paused() -> bool:
    """Check if the main pipeline is paused."""
    return get_pipeline_state("paused") == "true"


def is_newsletter_paused() -> bool:
    """Check if the newsletter pipeline is paused."""
    val = get_pipeline_state("newsletter_paused")
    return val == "true" or get_pipeline_state("paused") == "true"


def get_next_issue_number() -> int:
    """
    Return the next newsletter issue number.
    Sources of truth in priority order:
    1. The highest issue_number in newsletter_issues across ALL statuses — this is
       the real ceiling. A discarded issue does not free up its number.
    2. The pipeline_state counter as a fallback if the table is empty.
    This prevents the counter from jumping ahead when issues are discarded and
    re-run, which previously caused issue #5 to be followed by issue #6 even
    though #5 was never published.
    """
    try:
        client = get_client()
        # Check the actual max issue number in the DB regardless of status
        result = (
            client.table("newsletter_issues")
            .select("issue_number")
            .order("issue_number", desc=True)
            .limit(1)
            .execute()
        )
        db_max = 0
        if result.data:
            db_max = int(result.data[0].get("issue_number") or 0)

        current_str = get_pipeline_state("current_issue_number")
        state_current = int(current_str) if current_str.isdigit() else 0

        # Use whichever is higher so we never produce a duplicate number
        next_num = max(db_max, state_current) + 1
        set_pipeline_state("current_issue_number", str(next_num))
        return next_num
    except Exception as e:
        logger.error(f"get_next_issue_number error: {e}")
        return 1


def get_recent_content_items(days: int = 2, limit: int = 100, fresh_only: bool = False) -> list:
    """Get content items ingested in the last N days.
    Excludes Dom's LinkedIn posts (source_type='linkedin') — those are voice
    training data only and must never appear as newsletter content sources.
    """
    try:
        client = get_client()
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        result = (
            client.table("content_items")
            .select("id, source_type, source_name, source_url, title, raw_text, published_at, scraped_at, topics, is_deal_signal")
            .gte("scraped_at", cutoff)
            .neq("source_type", "linkedin")
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        return [
            row for row in rows
            if not is_stale_known_bad_content(
                " ".join(str(row.get(k) or "") for k in ("title", "raw_text", "summary"))
            ) and (not fresh_only or ((_effective_age_hours(row) or 0) <= 48))
        ]
    except Exception as e:
        logger.error(f"get_recent_content_items error: {e}")
        return []


def get_voice_samples(limit: int = 200) -> list:
    """Get all content items marked as voice samples."""
    try:
        client = get_client()
        result = (
            client.table("content_items")
            .select("id, source_name, title, raw_text, source_type")
            .eq("is_voice_sample", True)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"get_voice_samples error: {e}")
        return []


def get_recent_newsletter_topics(days: int = 60) -> list[str]:
    """
    Return a list of subject lines and key data points covered in recent issues.
    Used to prevent Hermes from repeating specific facts across editions.
    """
    try:
        client = get_client()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        result = (
            client.table("newsletter_issues")
            .select("issue_number, subject_line, sections")
            .gte("created_at", cutoff)
            .in_("status", ["draft", "approved", "scheduled", "published"])
            .order("created_at", desc=True)
            .limit(12)
            .execute()
        )
        entries: list[str] = []
        for issue in (result.data or []):
            num = issue.get("issue_number", "?")
            subject = issue.get("subject_line") or ""
            if subject:
                entries.append(f"Issue #{num}: {subject}")
            # Pull lead section content for a richer signal
            sections = issue.get("sections") or []
            for sec in sections:
                if sec.get("id") == "lead":
                    snippet = (sec.get("content") or "")[:200].replace("\n", " ")
                    if snippet:
                        entries.append(f"  Lead: {snippet}")
                    break
        return entries
    except Exception as e:
        logger.error(f"get_recent_newsletter_topics error: {e}")
        return []


def insert_newsletter_issue(issue: dict) -> str:
    """Insert a newsletter issue record. Returns the UUID."""
    try:
        client = get_client()
        result = client.table("newsletter_issues").insert(issue).execute()
        if result.data:
            return result.data[0]["id"]
        raise ValueError("No data returned from newsletter_issues insert")
    except Exception as e:
        logger.error(f"insert_newsletter_issue error: {e}")
        raise


def update_newsletter_issue(issue_id: str, updates: dict) -> None:
    """Update fields on a newsletter issue."""
    try:
        client = get_client()
        client.table("newsletter_issues").update(updates).eq("id", issue_id).execute()
    except Exception as e:
        logger.error(f"update_newsletter_issue error for {issue_id}: {e}")
        raise


def update_newsletter_issue_optional(issue_id: str, updates: dict) -> None:
    """Best-effort update for columns that may require a pending migration."""
    if not updates:
        return
    try:
        update_newsletter_issue(issue_id, updates)
    except Exception as e:
        logger.warning(
            "Optional newsletter issue update skipped for %s. Run latest migrations if this column is needed: %s",
            issue_id,
            str(e)[:200],
        )


def get_latest_newsletter_issue(status: str = None) -> Optional[dict]:
    """Get the most recently created newsletter issue, optionally filtered by status."""
    try:
        client = get_client()
        query = client.table("newsletter_issues").select("*").order("created_at", desc=True).limit(1)
        if status:
            query = query.eq("status", status)
        result = query.execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"get_latest_newsletter_issue error: {e}")
        return None


def find_approved_issue_for_current_week() -> Optional[dict]:
    """
    Return an approved or already-published newsletter issue created within the
    current ISO week (Monday-Sunday, UTC). Used by the Friday weekly job to
    skip generation when an ad-hoc draft has already been approved earlier in
    the week and is queued for Sunday.

    "Approved" means the issue has any of these statuses indicating Dom has
    locked it in: 'approved', 'scheduled', 'published'.
    """
    try:
        client = get_client()
        edition_date = get_upcoming_friday_edition().isoformat()
        result = (
            client.table("newsletter_issues")
            .select("id, issue_number, status, created_at, subject_line, week_end")
            .eq("week_end", edition_date)
            .in_("status", ["approved", "scheduled", "published"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as e:
        logger.error(f"find_approved_issue_for_current_week error: {e}")
        return None


def get_doms_active_deals() -> list:
    """Return all of Dom's currently active deals."""
    try:
        client = get_client()
        result = (
            client.table("pipeline_state")
            .select("key, value, updated_at")
            .like("key", "doms_deal_%")
            .execute()
        )
        deals = []
        for row in result.data or []:
            try:
                import json as _json
                deal = _json.loads(row["value"])
                deal["_key"] = row["key"]
                deal["_updated_at"] = row["updated_at"]
                if deal.get("status", "active") == "active":
                    deals.append(deal)
            except Exception:
                pass
        return deals
    except Exception as e:
        logger.error(f"get_doms_active_deals error: {e}")
        return []


def upsert_doms_deal(deal_name: str, deal_data: dict) -> None:
    """Add or update one of Dom's active deals."""
    import json as _json
    import re
    key = "doms_deal_" + re.sub(r"[^a-z0-9_]", "_", deal_name.lower())[:40]
    deal_data["name"] = deal_name
    deal_data.setdefault("status", "active")
    set_pipeline_state(key, _json.dumps(deal_data))


def close_doms_deal(deal_name: str) -> bool:
    """Mark one of Dom's deals as closed. Returns True if found."""
    import json as _json
    import re
    key = "doms_deal_" + re.sub(r"[^a-z0-9_]", "_", deal_name.lower())[:40]
    try:
        client = get_client()
        result = client.table("pipeline_state").select("value").eq("key", key).execute()
        if not result.data:
            return False
        deal = _json.loads(result.data[0]["value"])
        deal["status"] = "closed"
        set_pipeline_state(key, _json.dumps(deal))
        return True
    except Exception as e:
        logger.error(f"close_doms_deal error: {e}")
        return False


# ---------------------------------------------------------------------------
# Newsletter edition deals — Supply / Demand section
# ---------------------------------------------------------------------------

def get_newsletter_edition_deals() -> dict:
    """Return the current supply/demand deals for the newsletter edition.

    Returns a dict: {"supply": [...], "demand": [...]}.
    """
    import json as _json
    raw = get_pipeline_state("newsletter_edition_deals")
    if not raw:
        return {"supply": [], "demand": []}
    try:
        return _json.loads(raw)
    except Exception:
        return {"supply": [], "demand": []}


def set_newsletter_edition_deals(supply: list[str], demand: list[str]) -> None:
    """Persist the supply/demand deals for the current newsletter edition."""
    import json as _json
    data = {"supply": supply, "demand": demand}
    set_pipeline_state("newsletter_edition_deals", _json.dumps(data))


def clear_newsletter_edition_deals() -> None:
    """Clear the stored edition deals (call after publishing)."""
    set_pipeline_state("newsletter_edition_deals", "")


# ---------------------------------------------------------------------------
# Newsletter edit log -- persistent revision memory per issue
# ---------------------------------------------------------------------------

def get_edit_log(issue_id: str) -> list:
    """Load the edit log for an issue from pipeline_state."""
    import json as _json
    try:
        client = get_client()
        result = (
            client.table("pipeline_state")
            .select("value")
            .eq("key", f"edit_log_{issue_id}")
            .execute()
        )
        if result.data:
            raw = result.data[0].get("value")
            if isinstance(raw, list):
                return raw
            if isinstance(raw, str):
                return _json.loads(raw)
        return []
    except Exception as e:
        logger.warning("get_edit_log error for issue %s: %s", issue_id, e)
        return []


def append_edit_log(issue_id: str, entry: dict) -> None:
    """Append an entry to the issue edit log in pipeline_state."""
    import json as _json
    import datetime as _dt
    try:
        client = get_client()
        key = f"edit_log_{issue_id}"
        existing = get_edit_log(issue_id)
        entry["ts"] = _dt.datetime.utcnow().isoformat()
        existing.append(entry)
        # Keep last 50 entries max
        existing = existing[-50:]
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        db_check = client.table("pipeline_state").select("id").eq("key", key).execute()
        if db_check.data:
            client.table("pipeline_state").update(
                {"value": _json.dumps(existing), "updated_at": now_iso}
            ).eq("key", key).execute()
        else:
            client.table("pipeline_state").insert(
                {"key": key, "value": _json.dumps(existing), "updated_at": now_iso}
            ).execute()
    except Exception as e:
        logger.warning("append_edit_log error for issue %s: %s", issue_id, e)
