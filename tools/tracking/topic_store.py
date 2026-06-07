"""
tracking/topic_store.py — Single source of truth for edition topics.
All paths that save a topic Dom wants in the newsletter write here.
Hermes reads from here before drafting. No exceptions.
"""
import json
import logging
from datetime import date, datetime

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


def _db():
    from db.client import get_client
    return get_client()


async def save_topic(
    topic: str,
    topic_type: str = "topic",
    source_content: str = None,
    source_url: str = None,
    dom_instruction: str = None,
    edition_number: int = None,
    priority: int = 5,
) -> dict:
    """
    The single function for saving any topic Dom wants included.
    Called from: Telegram agent tools, dashboard API, voice notes.
    Writes to edition_topics (primary store for Hermes) AND edition_content (dashboard visibility).
    Returns confirmation dict with edition number and topic ID.
    """
    if not edition_number:
        from scheduler.edition_manager import get_current_edition_state
        edition_state = await get_current_edition_state()
        edition_number = edition_state['active_edition']

    try:
        week_result = _db().table('edition_weeks') \
            .select('*').eq('edition_number', edition_number).execute()
        week = week_result.data[0] if week_result.data else {
            'week_start': date.today().isoformat(),
            'week_end': date.today().isoformat(),
        }
    except Exception:
        week = {'week_start': date.today().isoformat(), 'week_end': date.today().isoformat()}

    valid_types = {'topic', 'deal', 'headline', 'dom_instruction'}
    safe_type = topic_type if topic_type in valid_types else 'topic'

    topic_id = None
    try:
        topic_result = _db().table('edition_topics').insert({
            'edition_number': edition_number,
            'topic': topic,
            'topic_type': safe_type,
            'priority': priority,
            'source': source_url or 'telegram',
            'added_by': 'dom',
            'used': False,
        }).execute()
        topic_id = topic_result.data[0]['id'] if topic_result.data else None
    except Exception as e:
        logger.error(f"[topic_store] edition_topics insert failed: {e}")

    try:
        _db().table('edition_content').insert({
            'edition_number': edition_number,
            'week_start': week.get('week_start', date.today().isoformat()),
            'week_end': week.get('week_end', date.today().isoformat()),
            'content_type': safe_type if safe_type in ['topic', 'deal', 'headline', 'dom_instruction'] else 'topic',
            'title': topic[:100],
            'body': source_content or topic,
            'source_url': source_url,
            'priority': priority,
            'added_by': 'dom',
        }).execute()
    except Exception as e:
        logger.warning(f"[topic_store] edition_content insert failed (non-fatal): {e}")

    return {
        'saved': True,
        'topic_id': topic_id,
        'edition_number': edition_number,
        'topic': topic,
    }


def get_all_topics_for_edition(edition_number: int) -> list:
    """
    Returns ALL unused topics for an edition sorted by priority desc.
    This is what Hermes reads before drafting.
    """
    try:
        result = _db().table('edition_topics') \
            .select('*') \
            .eq('edition_number', edition_number) \
            .eq('used', False) \
            .order('priority', desc=True) \
            .execute()
        return result.data or []
    except Exception as e:
        logger.error(f"[topic_store] get_all_topics_for_edition error: {e}")
        return []


def mark_topics_used(edition_number: int) -> None:
    """Called after draft generation to mark all topics as used."""
    try:
        _db().table('edition_topics') \
            .update({'used': True}) \
            .eq('edition_number', edition_number) \
            .execute()
    except Exception as e:
        logger.error(f"[topic_store] mark_topics_used error: {e}")


def remove_topic(topic_id) -> bool:
    """Remove a topic from an edition (hard delete from edition_topics). Accepts int or str id."""
    try:
        _db().table('edition_topics').delete().eq('id', topic_id).execute()
        return True
    except Exception as e:
        logger.error(f"[topic_store] remove_topic error: {e}")
        return False
