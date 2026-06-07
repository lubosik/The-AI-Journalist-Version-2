import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv

from config import APIFY_ACTORS, LOOKBACK_DAYS
from db.queries import insert_content_item, mark_content_as_voice_sample, update_source_last_scraped
from ingestion.apify_runner import run_actor
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()

logger = logging.getLogger(__name__)


def _parse_tiktok_timestamp(value) -> Optional[datetime]:
    """Parse TikTok's createTime field (unix timestamp or ISO string)."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            # Try ISO format
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception as e:
        logger.debug(f"Failed to parse TikTok timestamp {value}: {e}")
    return None


async def ingest_tiktok_profile(
    handle: str,
    is_voice_sample: bool = False,
    results_limit: int = 30,
) -> int:
    """
    Ingest TikTok profile videos.
    Returns count of new items stored.
    """
    logger.info(f"Ingesting TikTok profile: {handle} (limit={results_limit})")
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    try:
        items = await run_actor(
            APIFY_ACTORS["tiktok_profile"],
            input_data={"profiles": [handle], "resultsPerPage": results_limit},
            timeout_secs=300,
        )
    except Exception as e:
        logger.error(f"TikTok Apify error for {handle}: {e}")
        return 0

    logger.info(f"TikTok {handle}: got {len(items)} raw items from Apify")
    stored_count = 0

    async def _fetch_transcript(video_url: str) -> str:
        if not video_url:
            return ""
        try:
            transcript_items = await run_actor(
                APIFY_ACTORS["tiktok_transcripts"],
                input_data={"tiktokUrl": video_url},
                timeout_secs=120,
            )
            if not transcript_items:
                return ""
            first = transcript_items[0] if isinstance(transcript_items, list) else transcript_items
            if not isinstance(first, dict):
                return ""
            transcript = (first.get("transcript") or first.get("text") or "").strip()
            lower = transcript.lower()
            if not transcript or "no transcript available" in lower or "processing failed" in lower:
                return ""
            return transcript
        except Exception as exc:
            logger.warning("TikTok transcript fetch failed for %s: %s", video_url, exc)
            return ""

    for item in items:
        try:
            # Extract fields
            caption = item.get("text") or item.get("desc") or ""
            url = item.get("webVideoUrl") or item.get("videoUrl") or ""
            create_time = item.get("createTime")
            published_at = _parse_tiktok_timestamp(create_time)

            # Filter by lookback unless voice sample
            if not is_voice_sample and published_at and published_at < cutoff:
                logger.debug(f"TikTok item too old: {published_at}")
                continue

            transcript = ""
            if not is_voice_sample:
                transcript = await _fetch_transcript(url)

            text_parts = []
            if caption.strip():
                text_parts.append(caption.strip())
            if transcript.strip() and transcript.strip() != caption.strip():
                text_parts.append("Transcript:\n" + transcript.strip())
            text = "\n\n".join(text_parts).strip()

            if not text:
                logger.debug(f"Skipping empty TikTok item from {handle}")
                continue

            # Dedup
            if is_duplicate(url or None, text):
                logger.debug(f"TikTok duplicate skipped: {url}")
                continue

            # Relevance check
            if not is_voice_sample:
                relevance = await check_relevance(text)
                if relevance["score"] < 4:
                    logger.debug(f"TikTok low relevance ({relevance['score']}/10) skipped")
                    continue
            else:
                relevance = {"score": 7, "relevant": True, "reason": "voice_sample"}

            # Tags
            tags = await generate_tags(text, {"source": "tiktok", "handle": handle})

            content_hash = generate_content_hash(text)

            item_record = {
                "source_type": "tiktok",
                "source_name": handle,
                "source_url": url if url else None,
                "author_handle": handle,
                "title": text[:200] if text else None,
                "raw_text": text,
                "published_at": published_at.isoformat() if published_at else None,
                "language": "en",
                "is_voice_sample": is_voice_sample,
                "is_deal_signal": tags.get("is_deal_signal", False),
                "topics": tags.get("topics", []),
                "metadata": {
                    "content_hash": content_hash,
                    "relevance_score": relevance["score"],
                    "summary": tags.get("summary", ""),
                    "tiktok_id": item.get("id"),
                    "play_count": item.get("playCount"),
                    "like_count": item.get("diggCount"),
                    "has_transcript": bool(transcript),
                },
            }

            content_id = insert_content_item(item_record)
            logger.info(f"Stored TikTok item from {handle}: {text[:60]} (id={content_id})")

            if is_voice_sample:
                mark_content_as_voice_sample(content_id)

            chunks = chunk_text(text)
            await embed_and_store_chunks(content_id, chunks)

            stored_count += 1

        except Exception as e:
            logger.error(f"TikTok item processing error for {handle}: {e}", exc_info=True)
            continue

    update_source_last_scraped("tiktok", handle)
    logger.info(f"TikTok {handle}: stored {stored_count} new items")
    return stored_count
