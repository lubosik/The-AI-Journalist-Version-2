import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from db.client import get_client

logger = logging.getLogger(__name__)


def _get_active_style_bible() -> dict | None:
    """
    Fetch the most recent active style_bible row from Supabase.
    Returns the row dict or None if none exists.
    """
    try:
        client = get_client()
        result = (
            client.table("style_bible")
            .select("id, version, source_sample_count, created_at, is_active")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"_get_active_style_bible error: {e}")
        return None


def _count_voice_samples() -> int:
    """Return the count of content_items marked as is_voice_sample=True."""
    try:
        client = get_client()
        result = (
            client.table("content_items")
            .select("id", count="exact")
            .eq("is_voice_sample", True)
            .execute()
        )
        return result.count or 0
    except Exception as e:
        logger.error(f"_count_voice_samples error: {e}")
        return 0


async def get_style_age_days() -> int | None:
    """Return how many days old the current style bible is, or None if none exists."""
    loop = asyncio.get_running_loop()
    bible = await loop.run_in_executor(None, _get_active_style_bible)
    if bible is None:
        return None

    raw_created = bible.get("created_at")
    if not raw_created:
        return None

    try:
        if isinstance(raw_created, str):
            created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
        elif isinstance(raw_created, datetime):
            created_at = raw_created
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            return None

        now = datetime.now(timezone.utc)
        delta = now - created_at
        return delta.days

    except Exception as e:
        logger.error(f"get_style_age_days: failed to parse created_at '{raw_created}': {e}")
        return None


async def run(force_refresh: bool = False) -> str:
    """
    Check if style bible needs updating and run analysis if so.
    Returns a status message string to send to Dom via Telegram.
    """
    # Check for existing voice samples first — nothing to analyse without them
    loop = asyncio.get_running_loop()
    sample_count = await loop.run_in_executor(None, _count_voice_samples)

    if sample_count == 0:
        return (
            "No voice samples in database yet. Run /train first to extract transcripts."
        )

    # Check the current style bible age
    age_days = await get_style_age_days()
    bible = await loop.run_in_executor(None, _get_active_style_bible)

    if bible is not None and age_days is not None and age_days < 7 and not force_refresh:
        # Style bible is fresh enough — report status and skip re-analysis
        raw_created = bible.get("created_at", "")
        try:
            if isinstance(raw_created, str):
                created_dt = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
                date_label = created_dt.strftime("%d %b %Y")
            else:
                date_label = str(raw_created)[:10]
        except Exception:
            date_label = str(raw_created)[:10]

        stored_count = bible.get("source_sample_count", sample_count)
        return (
            f"Style bible is current. Last updated {date_label}. "
            f"{stored_count} voice samples analysed."
        )

    # Run full analysis — import here so a missing style_analyser.py gives a clear error message
    try:
        from training.style_analyser import analyse_style_corpus
    except ImportError as e:
        logger.error(f"style_agent.run: could not import style_analyser: {e}")
        return (
            "Style analyser module not found. "
            "Ensure training/style_analyser.py exists and is importable."
        )

    try:
        logger.info("style_agent.run: starting style corpus analysis")
        result = await analyse_style_corpus(force=force_refresh)

        # analyse_style_corpus is expected to return a dict with at least
        # {"sample_count": int} — handle both sync return and None gracefully
        if result and isinstance(result, dict):
            analysed_count = result.get("sample_count", sample_count)
        else:
            analysed_count = sample_count

        logger.info(f"style_agent.run: analysis complete, {analysed_count} samples analysed")
        return (
            f"Style analysis complete. {analysed_count} voice samples analysed. "
            "Writing bible updated."
        )

    except Exception as e:
        logger.error(f"style_agent.run: analyse_style_corpus raised an error: {e}")
        return f"Style analysis failed: {e}"
