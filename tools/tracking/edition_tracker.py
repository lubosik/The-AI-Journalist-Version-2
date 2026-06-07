"""
Per-edition content tracker. Records all content, instructions, research,
and edits tied to a specific newsletter edition.
All DB calls use the sync supabase client — no await on DB operations.
"""
import logging
from datetime import datetime, date, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


def _db():
    from db.client import get_client
    return get_client()


def _get_pipeline_state(key: str) -> Optional[str]:
    result = _db().table('pipeline_state').select('value').eq('key', key).execute()
    return result.data[0]['value'] if result.data else None


def get_edition_for_date(target_date: date = None) -> dict:
    """
    Returns the edition week record for a given date (defaults to today ET).
    Auto-creates the week record if it doesn't exist yet.
    """
    if not target_date:
        target_date = datetime.now(ET).date()

    result = _db().table('edition_weeks') \
        .select('*') \
        .lte('week_start', target_date.isoformat()) \
        .gte('week_end', target_date.isoformat()) \
        .execute()

    if result.data:
        return result.data[0]

    # Fall back: calculate from pipeline_state
    try:
        current_num = int(_get_pipeline_state('current_edition_number') or '1')
        next_publish_str = _get_pipeline_state('next_publish_date')
        next_publish = date.fromisoformat(next_publish_str) if next_publish_str else target_date

        # Find the Sunday of the week containing next_publish
        # (editions always publish on Sunday)
        days_to_sunday = (6 - next_publish.weekday()) % 7
        base_sunday = next_publish + timedelta(days=days_to_sunday)

        # Find which week offset this date is
        delta_days = (target_date - base_sunday).days
        weeks_offset = max(0, delta_days // 7)
        edition_number = current_num + weeks_offset
        week_start = base_sunday + timedelta(weeks=weeks_offset)  # Sunday
        week_end = week_start + timedelta(days=6)  # Saturday
        publish_date = week_start  # Sunday
        draft_date = week_start - timedelta(days=2)  # Friday

        week_record = {
            'edition_number': edition_number,
            'week_start': week_start.isoformat(),
            'week_end': week_end.isoformat(),
            'publish_date': publish_date.isoformat(),
            'draft_date': draft_date.isoformat(),
        }
        _db().table('edition_weeks').upsert(week_record, on_conflict='edition_number').execute()
        return week_record
    except Exception as e:
        logger.error(f"[edition_tracker] get_edition_for_date fallback error: {e}")
        # Last resort: return a plausible record so callers don't crash
        return {
            'edition_number': 1,
            'week_start': target_date.isoformat(),
            'week_end': (target_date + timedelta(days=6)).isoformat(),
            'publish_date': target_date.isoformat(),
            'draft_date': target_date.isoformat(),
        }


def track_content(
    content_type: str,
    title: str,
    body: str,
    source_url: str = None,
    source_type: str = None,
    source_name: str = None,
    tags: list = None,
    priority: int = 5,
    content_item_id: str = None,
    added_by: str = 'system',
    edition_number: int = None,
) -> Optional[str]:
    """
    Record any piece of content for the current (or specified) edition.
    Fire-and-forget safe — never raises, logs errors instead.
    Returns the new record ID or None on failure.
    """
    try:
        week = get_edition_for_date()
        if edition_number:
            week_result = _db().table('edition_weeks') \
                .select('*').eq('edition_number', edition_number).execute()
            if week_result.data:
                week = week_result.data[0]

        valid_types = {
            'topic', 'research', 'url_ingested', 'dom_instruction',
            'headline', 'deal', 'draft_edit', 'feedback_applied',
            'voice_note', 'telegram_tip',
        }
        if content_type not in valid_types:
            content_type = 'topic'

        result = _db().table('edition_content').insert({
            'edition_number': week['edition_number'],
            'week_start': week['week_start'],
            'week_end': week['week_end'],
            'content_type': content_type,
            'title': (title or '')[:100],
            'body': (body or '')[:5000],
            'source_url': source_url,
            'source_type': source_type,
            'source_name': source_name,
            'tags': tags or [],
            'priority': priority,
            'content_item_id': content_item_id,
            'added_by': added_by,
        }).execute()

        return result.data[0]['id'] if result.data else None
    except Exception as e:
        logger.error(f"[edition_tracker] track_content error: {e}")
        return None


def remove_content(content_id: str, reason: str = None):
    """Soft-delete a content item from an edition."""
    try:
        _db().table('edition_content').update({
            'removed': True,
            'removed_at': datetime.now(ET).isoformat(),
            'removed_reason': reason or 'Removed',
        }).eq('id', content_id).execute()
    except Exception as e:
        logger.error(f"[edition_tracker] remove_content error: {e}")


def restore_content(content_id: str):
    """Restore a previously removed content item."""
    try:
        _db().table('edition_content').update({
            'removed': False,
            'removed_at': None,
            'removed_reason': None,
        }).eq('id', content_id).execute()
    except Exception as e:
        logger.error(f"[edition_tracker] restore_content error: {e}")


def mark_included_in_draft(edition_number: int):
    """Mark all non-removed content for an edition as included in draft."""
    try:
        _db().table('edition_content').update({'included_in_draft': True}) \
            .eq('edition_number', edition_number) \
            .eq('removed', False) \
            .execute()
    except Exception as e:
        logger.error(f"[edition_tracker] mark_included_in_draft error: {e}")


def get_edition_content(edition_number: int, include_removed: bool = False) -> dict:
    """Get all content for an edition grouped by type."""
    try:
        query = _db().table('edition_content') \
            .select('*') \
            .eq('edition_number', edition_number)
        if not include_removed:
            query = query.eq('removed', False)
        result = query.order('created_at', desc=False).execute()
        items = result.data or []

        grouped = {t: [] for t in [
            'topics', 'research', 'url_ingested', 'dom_instruction',
            'headline', 'deal', 'draft_edit', 'feedback_applied',
            'voice_note', 'telegram_tip',
        ]}
        for item in items:
            ct = item['content_type']
            key = ct if ct != 'topic' else 'topics'
            if key in grouped:
                grouped[key].append(item)
            else:
                grouped.setdefault(ct, []).append(item)

        return {
            'edition_number': edition_number,
            'total_items': len(items),
            'active_items': len([i for i in items if not i.get('removed')]),
            'grouped': grouped,
            'all_items': items,
        }
    except Exception as e:
        logger.error(f"[edition_tracker] get_edition_content error: {e}")
        return {'edition_number': edition_number, 'total_items': 0, 'active_items': 0, 'grouped': {}, 'all_items': []}


def add_topic_to_edition(
    topic: str,
    topic_type: str = 'topic',
    priority: int = 5,
    edition_number: int = None,
    added_by: str = 'dom',
) -> Optional[str]:
    """Add a topic instruction to an edition."""
    valid = {'topic', 'headline', 'deal', 'dom_instruction'}
    ct = topic_type if topic_type in valid else 'topic'
    return track_content(
        content_type=ct,
        title=topic[:100],
        body=topic,
        priority=priority,
        added_by=added_by,
        edition_number=edition_number,
    )
