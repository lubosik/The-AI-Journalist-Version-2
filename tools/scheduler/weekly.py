import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


async def friday_draft_conversation_job() -> None:
    """
    Friday 6pm ET: start the topic confirmation conversation with Dom.
    NEVER generates the newsletter directly.
    Generation only happens after Dom approves.
    """
    from db.queries import is_newsletter_paused
    from scheduler.draft_conversation import get_draft_state, start_friday_conversation

    try:
        if is_newsletter_paused():
            logger.info("Friday conversation job skipped — newsletter is paused")
            return

        current_state = await get_draft_state()
        if current_state != 'idle':
            logger.info(f"[weekly] Draft conversation already active ({current_state}), skipping")
            return

        logger.info("[weekly] Starting Friday pre-draft conversation")
        await start_friday_conversation()

    except Exception as e:
        logger.error(f"[weekly] friday_draft_conversation_job error: {e}", exc_info=True)
        try:
            from telegram_bot.sender import send_to_client
            from filters.response_filter import filter_response
            await send_to_client(
                filter_response(f"Friday conversation failed to start. Error: {str(e)[:200]}"),
                parse_mode="",
            )
        except Exception:
            pass


def register_weekly_job(scheduler: AsyncIOScheduler) -> None:
    """Register the Friday 6pm ET draft conversation job on an existing scheduler instance."""
    scheduler.add_job(
        friday_draft_conversation_job,
        CronTrigger(
            day_of_week="fri",
            hour=18,
            minute=0,
            timezone="America/New_York",
        ),
        id="friday_draft_conversation",
        name="HERALD Friday Draft Conversation",
        misfire_grace_time=300,
        replace_existing=True,
    )
    logger.info("Friday draft conversation job registered (Friday 6pm ET)")
