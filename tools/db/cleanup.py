"""
Herald database retention policy.
Runs daily. Keeps what matters, purges what doesn't.

KEEP FOREVER:
  - newsletter_issues where status IN ('published', 'sent', 'approved')
  - content_items where is_voice_sample = true
  - style_bible, sources (all rows)
  - pipeline_state permanent config keys

DELETE AFTER 21 DAYS:
  - content_items (non-voice) older than 21 days
  - content_chunks orphaned after content_items deletion

DELETE AFTER 30 DAYS:
  - pipeline_state keys matching 'edit_log_%' for issues older than 30 days

DELETE AFTER 60 DAYS:
  - newsletter_issues where status IN ('draft', 'reviewed') older than 60 days
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_PERMANENT_PIPELINE_KEYS = {
    "paused", "newsletter_paused", "beehiiv_api_key", "beehiiv_publication_id",
    "last_newsletter_sent", "current_issue_number", "newsletter_edition_deals",
    "recent_x_search_queries", "awaiting_newsletter_edit",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff(days: int) -> str:
    return (_now() - timedelta(days=days)).isoformat()


async def run_cleanup() -> dict:
    """
    Run all retention cleanup tasks. Returns summary of what was deleted.
    Fire-and-forget safe -- never raises.
    """
    from db.client import get_client

    summary: dict[str, int] = {}

    try:
        db = get_client()

        # 1. Delete non-voice content_items older than 21 days
        try:
            cut = _cutoff(21)
            # Get IDs to delete first (for chunk cascade)
            old_items = (
                db.table("content_items")
                .select("id")
                .eq("is_voice_sample", False)
                .lt("scraped_at", cut)
                .execute()
            )
            old_ids = [r["id"] for r in (old_items.data or [])]

            if old_ids:
                # Delete orphaned chunks first
                chunk_del = (
                    db.table("content_chunks")
                    .delete()
                    .in_("content_item_id", old_ids)
                    .execute()
                )
                summary["content_chunks_deleted"] = len(chunk_del.data or [])

                # Delete content items in batches of 100
                deleted_items = 0
                for i in range(0, len(old_ids), 100):
                    batch = old_ids[i:i + 100]
                    r = db.table("content_items").delete().in_("id", batch).execute()
                    deleted_items += len(r.data or [])
                summary["content_items_deleted"] = deleted_items
            else:
                summary["content_items_deleted"] = 0
                summary["content_chunks_deleted"] = 0

            logger.info("cleanup: content_items deleted=%d, chunks deleted=%d",
                        summary["content_items_deleted"], summary["content_chunks_deleted"])
        except Exception as e:
            logger.error("cleanup: content_items step failed: %s", e)
            summary["content_items_error"] = str(e)[:100]

        # 2. Delete old draft newsletter_issues older than 60 days
        try:
            cut = _cutoff(60)
            r = (
                db.table("newsletter_issues")
                .delete()
                .in_("status", ["draft", "reviewed"])
                .lt("created_at", cut)
                .execute()
            )
            summary["old_drafts_deleted"] = len(r.data or [])
            logger.info("cleanup: old_drafts deleted=%d", summary["old_drafts_deleted"])
        except Exception as e:
            logger.error("cleanup: old_drafts step failed: %s", e)
            summary["old_drafts_error"] = str(e)[:100]

        # 4. Delete edit_log pipeline_state entries for old issues
        try:
            cut_issue_ids = set()
            old_issues = (
                db.table("newsletter_issues")
                .select("id")
                .in_("status", ["published", "sent", "approved"])
                .lt("published_at", _cutoff(30))
                .execute()
            )
            for row in (old_issues.data or []):
                cut_issue_ids.add(str(row["id"]))

            if cut_issue_ids:
                # Get all edit_log keys
                all_keys = db.table("pipeline_state").select("key").like("key", "edit_log_%").execute()
                keys_to_delete = [
                    r["key"] for r in (all_keys.data or [])
                    if r["key"].replace("edit_log_", "") in cut_issue_ids
                ]
                if keys_to_delete:
                    r = db.table("pipeline_state").delete().in_("key", keys_to_delete).execute()
                    summary["edit_logs_deleted"] = len(r.data or [])
                else:
                    summary["edit_logs_deleted"] = 0
            else:
                summary["edit_logs_deleted"] = 0
            logger.info("cleanup: edit_logs deleted=%d", summary["edit_logs_deleted"])
        except Exception as e:
            logger.error("cleanup: edit_logs step failed: %s", e)
            summary["edit_logs_error"] = str(e)[:100]

        summary["ran_at"] = _now().isoformat()
        summary["status"] = "complete"
        logger.info("cleanup: finished. summary=%s", summary)

    except Exception as e:
        logger.error("cleanup: unexpected error: %s", e)
        summary["fatal_error"] = str(e)[:200]
        summary["status"] = "failed"

    return summary
