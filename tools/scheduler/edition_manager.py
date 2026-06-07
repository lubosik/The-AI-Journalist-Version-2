import logging
from datetime import datetime, date, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


async def get_pipeline_state(key: str) -> Optional[str]:
    """Read a value from pipeline_state table."""
    from db.client import get_client
    try:
        client = get_client()
        result = (
            client.table("pipeline_state")
            .select("value")
            .eq("key", key)
            .execute()
        )
        if result.data:
            return result.data[0]["value"]
        return None
    except Exception as e:
        logger.error(f"[edition_manager] get_pipeline_state({key}) error: {e}")
        return None


async def set_pipeline_state(key: str, value: str) -> None:
    """Write a value to pipeline_state table."""
    from db.client import get_client
    try:
        client = get_client()
        client.table("pipeline_state").upsert(
            {"key": key, "value": value},
            on_conflict="key"
        ).execute()
    except Exception as e:
        logger.error(f"[edition_manager] set_pipeline_state({key}) error: {e}")


async def get_current_edition_state() -> dict:
    """
    Returns the definitive current state of where we are in the edition cycle.
    Called before ANY newsletter action to determine what is allowed.
    """
    now_et = datetime.now(ET)
    today = now_et.date()

    current_num_str = await get_pipeline_state('current_edition_number')
    next_publish_str = await get_pipeline_state('next_publish_date')
    locked_after_str = await get_pipeline_state('edition_locked_after')

    current_num = int(current_num_str) if current_num_str else 1
    next_publish = date.fromisoformat(next_publish_str) if next_publish_str else today
    locked_after: Optional[datetime] = None
    if locked_after_str:
        try:
            dt = datetime.fromisoformat(locked_after_str)
            if dt.tzinfo is None:
                locked_after = ET.localize(dt)
            else:
                locked_after = dt.astimezone(ET)
        except Exception:
            pass

    edition_closed = locked_after is not None and now_et > locked_after

    if edition_closed:
        active_edition = current_num + 1
        active_publish_date = next_publish + timedelta(days=7)
        active_draft_date = active_publish_date - timedelta(days=2)
        window = 'open'
    else:
        active_edition = current_num
        active_publish_date = next_publish
        active_draft_date = next_publish - timedelta(days=2)
        draft_opens = ET.localize(
            datetime.combine(active_draft_date, datetime.min.time())
        ).replace(hour=18, minute=0, second=0, microsecond=0)
        window = 'drafting' if now_et >= draft_opens else 'research'

    return {
        'active_edition': active_edition,
        'publish_date': active_publish_date,
        'draft_date': active_draft_date,
        'window': window,
        'edition_closed': edition_closed,
        'now_et': now_et,
        'can_draft': window == 'drafting',
        'days_until_publish': (active_publish_date - today).days,
    }


async def advance_edition_after_publish(published_edition: int) -> int:
    """
    Called after Dom approves and publishes an edition.
    Advances all state to the next edition.
    """
    from db.client import get_client
    state = await get_current_edition_state()
    next_num = published_edition + 1
    next_publish = state['publish_date'] + timedelta(days=7)
    locked_after = ET.localize(
        datetime.combine(next_publish, datetime.min.time())
    ).replace(hour=18, minute=0, second=0, microsecond=0)

    await set_pipeline_state('current_edition_number', str(next_num))
    await set_pipeline_state('next_publish_date', next_publish.isoformat())
    await set_pipeline_state('edition_locked_after', locked_after.isoformat())

    try:
        client = get_client()
        client.table('edition_calendar').upsert({
            'edition_number': next_num,
            'planned_headline': '',
            'planned_angle': (
                f"Edition {next_num}, publishing {next_publish.isoformat()}"
            ),
        }, on_conflict='edition_number').execute()
    except Exception as e:
        logger.error(f"[edition_manager] advance_edition_after_publish calendar upsert error: {e}")

    logger.info(f"[edition_manager] Advanced to Edition {next_num}, publishes {next_publish}")
    return next_num


async def can_draft_edition(edition_number: int) -> dict:
    """
    Hard gate. Call this before starting ANY draft generation.
    Returns {'allowed': bool, 'reason': str}
    """
    state = await get_current_edition_state()

    if edition_number != state['active_edition']:
        return {
            'allowed': False,
            'reason': (
                f"Edition {edition_number} is not the active edition. "
                f"Active edition is {state['active_edition']} "
                f"(publishes {state['publish_date']})."
            ),
        }

    if not state['can_draft']:
        return {
            'allowed': False,
            'reason': (
                f"Edition {edition_number} drafting window has not opened yet. "
                f"Drafting opens Friday {state['draft_date']} at 6pm ET. "
                f"Publication is Sunday {state['publish_date']}. "
                f"{state['days_until_publish']} days away."
            ),
        }

    return {'allowed': True, 'reason': 'Drafting window is open.'}
