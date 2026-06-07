"""
training/transcript_extractor.py

Bulk-extracts ALL available transcripts from Elena Nisonoff's TikTok profile
and all configured YouTube channels. Intended to be run once via the /train
command or CLI to populate the training corpus.

TikTok flow:
  1. Use clockworks/tiktok-profile-scraper to pull ALL video URLs from the profile.
  2. Pass those URLs in batches of 20 to sian.agency/best-tiktok-ai-transcript-extractor.
  3. Store every item as is_voice_sample=True — no relevance gate.

YouTube flow:
  1. Use scrape-creators/best-youtube-transcripts-scraper with max_results=50
     and no date filter to get the video list.
  2. Use pintostudio/youtube-transcript-scraper per video to get the full transcript.
  3. Store as is_voice_sample=False — relevance gate applies.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from config import APIFY_ACTORS, TIKTOK_PROFILES, YOUTUBE_CHANNELS
from db.queries import insert_content_item, update_source_last_scraped
from ingestion.apify_runner import run_actor
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Actor IDs used exclusively by the training pipeline (not in APIFY_ACTORS).
# Update config.py APIFY_ACTORS separately if you want these in the main dict.
# ---------------------------------------------------------------------------
TIKTOK_TRANSCRIPT_ACTOR = "sian.agency/best-tiktok-ai-transcript-extractor"
YOUTUBE_TRANSCRIPT_ACTOR_V2 = "pintostudio/youtube-transcript-scraper"

# Batching constants
TIKTOK_BATCH_SIZE = 20


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_unix_or_iso(value) -> Optional[datetime]:
    """Parse a unix timestamp (int/float) or ISO string into a UTC datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception as e:
        logger.debug(f"Could not parse timestamp {value!r}: {e}")
    return None


def _parse_youtube_date(value) -> Optional[datetime]:
    """Parse YouTube date string into a UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(value)).astimezone(timezone.utc)
    except Exception:
        pass
    return None


async def _store_tiktok_item(item: dict, handle: str) -> bool:
    """
    Persist a single TikTok transcript item.
    Returns True if stored, False if skipped (duplicate, empty, error).
    Always treated as is_voice_sample=True — no relevance gate.
    """
    try:
        transcript_raw = (
            item.get("transcript")
            or item.get("text")
            or item.get("videoDescription")
            or ""
        )
        # agentx/tiktok-transcript returns transcript as a nested dict: {"text": "...", ...}
        if isinstance(transcript_raw, dict):
            transcript = transcript_raw.get("text", "").strip()
        elif isinstance(transcript_raw, list):
            transcript = " ".join(
                t.get("text", "") for t in transcript_raw if isinstance(t, dict)
            )
        else:
            transcript = str(transcript_raw).strip()

        description = item.get("videoDescription") or ""
        raw_text = f"{description}\n\n{transcript}".strip() if description else transcript

        if not raw_text:
            logger.debug(f"Skipping empty TikTok item from {handle}")
            return False
        if "no transcript available" in raw_text.lower() or "input validation failed" in raw_text.lower():
            logger.warning("Skipping TikTok transcript error item from %s: %s", handle, raw_text[:120])
            return False

        # URL from the source item (profile scraper) or transcript actor
        url = (
            item.get("webVideoUrl")
            or item.get("videoUrl")
            or item.get("url")
            or ""
        )

        if is_duplicate(url or None, raw_text):
            logger.debug(f"TikTok training duplicate skipped: {url}")
            return False

        published_at = _parse_unix_or_iso(item.get("createTime"))
        content_hash = generate_content_hash(raw_text)

        # Generate tags — even for voice samples we want metadata
        tags = await generate_tags(raw_text, {"source": "tiktok", "handle": handle})

        item_record = {
            "source_type": "tiktok",
            "source_name": handle,
            "source_url": url if url else None,
            "title": (description[:200] or raw_text[:200]) if raw_text else None,
            "raw_text": raw_text,
            "published_at": published_at.isoformat() if published_at else None,
            "language": "en",
            "is_voice_sample": True,
            "is_deal_signal": tags.get("is_deal_signal", False),
            "topics": tags.get("topics", []),
            "metadata": {
                "content_hash": content_hash,
                "relevance_score": 7,
                "summary": tags.get("summary", ""),
                "views_count": item.get("viewsCount"),
                "likes_count": item.get("likesCount"),
                "hashtags": item.get("hashtags", []),
                "video_duration": item.get("videoDuration"),
                "training_source": "transcript_extractor",
            },
        }

        content_id = insert_content_item(item_record)
        logger.info(
            f"Stored TikTok voice sample from {handle}: "
            f"{raw_text[:60]!r} (id={content_id})"
        )

        chunks = chunk_text(raw_text)
        await embed_and_store_chunks(content_id, chunks)
        return True

    except Exception as e:
        logger.error(f"_store_tiktok_item error for {handle}: {e}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_all_tiktok_transcripts(handle: str) -> int:
    """
    Bulk-extract ALL transcripts from a TikTok profile handle.

    Steps:
    1. Pull every video URL from the profile via clockworks/tiktok-profile-scraper
       (no resultsPerPage limit — pass a very high ceiling).
    2. Collect all URLs, then call sian.agency/best-tiktok-ai-transcript-extractor
       in batches of 20.
    3. Store each transcript as is_voice_sample=True.

    Returns the count of items successfully stored.
    """
    logger.info(f"[TikTok training] Starting full extraction for @{handle}")

    # Step 1: Get all video metadata from the profile
    try:
        profile_items = await run_actor(
            APIFY_ACTORS["tiktok_profile"],
            input_data={"profiles": [handle], "resultsPerPage": 10000},
            timeout_secs=600,
        )
    except Exception as e:
        logger.error(f"[TikTok training] Profile scraper failed for @{handle}: {e}")
        return 0

    logger.info(
        f"[TikTok training] Profile scraper returned {len(profile_items)} items for @{handle}"
    )

    if not profile_items:
        logger.warning(f"[TikTok training] No videos found for @{handle}")
        update_source_last_scraped("tiktok_training", handle)
        return 0

    # Collect all video URLs from the profile results
    all_urls: list[str] = []
    for item in profile_items:
        url = (
            item.get("webVideoUrl")
            or item.get("videoUrl")
            or item.get("url")
            or ""
        )
        if url:
            all_urls.append(url)

    logger.info(
        f"[TikTok training] Collected {len(all_urls)} video URLs from @{handle}"
    )

    if not all_urls:
        logger.warning(f"[TikTok training] No valid URLs extracted from profile items for @{handle}")
        update_source_last_scraped("tiktok_training", handle)
        return 0

    # Step 2: Fetch transcripts one at a time using tiktokUrl (actor does not support bulkUrls)
    # Run up to 5 concurrent requests to balance speed vs Apify rate limits
    stored_count = 0
    CONCURRENCY = 5
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def _fetch_one(url: str, idx: int) -> bool:
        async with semaphore:
            logger.info(f"[TikTok training] Fetching transcript {idx}/{len(all_urls)}: {url}")
            try:
                items = await run_actor(
                    TIKTOK_TRANSCRIPT_ACTOR,
                    input_data={"tiktokUrl": url},
                    timeout_secs=120,
                )
                if items:
                    return await _store_tiktok_item(items[0], handle)
                return False
            except Exception as e:
                logger.error(f"[TikTok training] Transcript fetch failed for {url}: {e}")
                return False

    tasks = [_fetch_one(url, i + 1) for i, url in enumerate(all_urls)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    stored_count = sum(1 for r in results if r is True)

    update_source_last_scraped("tiktok_training", handle)
    logger.info(
        f"[TikTok training] @{handle} complete: {stored_count} items stored "
        f"from {len(all_urls)} videos"
    )
    return stored_count


async def extract_youtube_channel_transcripts(
    channel: dict,
    max_videos: int = 50,
) -> int:
    """
    Bulk-extract transcripts from a YouTube channel using two actors:
    1. scrape-creators/best-youtube-transcripts-scraper for the video list
       (max_results=50, no date filter).
    2. pintostudio/youtube-transcript-scraper per video for the actual transcript.

    Stored as is_voice_sample=False — relevance gate applies.
    Returns count of items successfully stored.
    """
    channel_name = channel.get("name", channel.get("handle", "unknown"))
    channel_url = channel.get("url", "")
    logger.info(
        f"[YouTube training] Starting extraction for {channel_name} "
        f"(max_videos={max_videos})"
    )

    # Step 1: Get list of videos from the channel using streamers/youtube-scraper
    try:
        video_list_items = await run_actor(
            APIFY_ACTORS["youtube_channel"],  # streamers/youtube-scraper
            input_data={
                "startUrls": [{"url": channel_url}],
                "maxResults": max_videos,
                "sortVideosBy": "NEWEST",
            },
            timeout_secs=600,
        )
    except Exception as e:
        logger.error(
            f"[YouTube training] Video list actor failed for {channel_name}: {e}"
        )
        update_source_last_scraped("youtube_training", channel_name)
        return 0

    logger.info(
        f"[YouTube training] Video list actor returned {len(video_list_items)} items "
        f"for {channel_name}"
    )

    if not video_list_items:
        logger.warning(f"[YouTube training] No videos returned for {channel_name}")
        update_source_last_scraped("youtube_training", channel_name)
        return 0

    stored_count = 0

    for idx, video_meta in enumerate(video_list_items, 1):
        # Extract the video URL from whatever field the actor uses
        video_url = (
            video_meta.get("url")
            or video_meta.get("videoUrl")
            or video_meta.get("id")  # sometimes just the video ID
            or ""
        )

        # Normalise bare video IDs to full URLs
        if video_url and not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_url}"

        if not video_url:
            logger.debug(f"[YouTube training] No URL in item {idx} for {channel_name}, skipping")
            continue

        title = (
            video_meta.get("title")
            or video_meta.get("videoTitle")
            or ""
        )
        published_raw = (
            video_meta.get("date")
            or video_meta.get("publishedAt")
            or video_meta.get("uploadDate")
        )
        published_at = _parse_youtube_date(published_raw)

        logger.info(
            f"[YouTube training] Fetching transcript {idx}/{len(video_list_items)}: "
            f"{title[:60]!r} ({video_url})"
        )

        # Step 2: Fetch individual transcript
        try:
            transcript_result = await run_actor(
                YOUTUBE_TRANSCRIPT_ACTOR_V2,
                input_data={"videoUrl": video_url},
                timeout_secs=180,
            )
        except Exception as e:
            logger.error(
                f"[YouTube training] Transcript fetch failed for {video_url}: {e}"
            )
            continue

        # pintostudio/youtube-transcript-scraper returns:
        # [{"data": [{"start": "0.12", "dur": "5.12", "text": "..."}, ...]}]
        transcript_text = ""
        if transcript_result:
            first = transcript_result[0] if isinstance(transcript_result, list) else transcript_result
            if isinstance(first, dict):
                # Try "data" key first (confirmed format), then "searchResult" as fallback
                segments = first.get("data") or first.get("searchResult") or []
                if isinstance(segments, list):
                    transcript_text = " ".join(
                        seg.get("text", "")
                        for seg in segments
                        if isinstance(seg, dict) and seg.get("text")
                    )

        raw_text = f"{title}\n\n{transcript_text}".strip() if transcript_text else title.strip()

        if not raw_text:
            logger.debug(f"[YouTube training] Empty content for {video_url}, skipping")
            continue

        try:
            if is_duplicate(video_url, raw_text):
                logger.debug(f"[YouTube training] Duplicate skipped: {video_url}")
                continue

            # Relevance gate applies for non-voice-sample content
            relevance = await check_relevance(raw_text[:1500])
            if relevance["score"] < 4:
                logger.debug(
                    f"[YouTube training] Low relevance ({relevance['score']}/10) "
                    f"skipped: {title[:60]}"
                )
                continue

            tags = await generate_tags(raw_text)
            content_hash = generate_content_hash(raw_text)

            item_record = {
                "source_type": "youtube",
                "source_name": channel_name,
                "source_url": video_url,
                "title": title[:500] if title else None,
                "raw_text": raw_text,
                "published_at": published_at.isoformat() if published_at else None,
                "language": "en",
                "is_voice_sample": False,
                "is_deal_signal": tags.get("is_deal_signal", False),
                "topics": tags.get("topics", []),
                "metadata": {
                    "content_hash": content_hash,
                    "relevance_score": relevance["score"],
                    "summary": tags.get("summary", ""),
                    "channel_url": channel_url,
                    "channel_handle": channel.get("handle"),
                    "has_transcript": bool(transcript_text),
                    "training_source": "transcript_extractor",
                },
            }

            content_id = insert_content_item(item_record)
            logger.info(
                f"[YouTube training] Stored: {title[:60]!r} (id={content_id})"
            )

            chunks = chunk_text(raw_text)
            await embed_and_store_chunks(content_id, chunks)
            stored_count += 1

        except Exception as e:
            logger.error(
                f"[YouTube training] Processing error for {video_url}: {e}",
                exc_info=True,
            )
            continue

    update_source_last_scraped("youtube_training", channel_name)
    logger.info(
        f"[YouTube training] {channel_name} complete: {stored_count} items stored"
    )
    return stored_count


async def run_full_training_extraction() -> dict:
    """
    Orchestrates the full training data extraction run:
    - All TikTok profiles in TIKTOK_PROFILES (is_voice_sample=True)
    - All YouTube channels in YOUTUBE_CHANNELS (is_voice_sample=False)

    Returns a summary dict with counts per source and totals.
    """
    logger.info("[Training extraction] Starting full training corpus extraction")

    summary: dict = {
        "tiktok": {},
        "youtube": {},
        "total_tiktok": 0,
        "total_youtube": 0,
        "total_stored": 0,
        "errors": [],
    }

    # --- TikTok profiles ---
    for handle in TIKTOK_PROFILES:
        logger.info(f"[Training extraction] Processing TikTok profile: @{handle}")
        try:
            count = await extract_all_tiktok_transcripts(handle)
            summary["tiktok"][handle] = count
            summary["total_tiktok"] += count
        except Exception as e:
            msg = f"TikTok @{handle} extraction failed: {e}"
            logger.error(f"[Training extraction] {msg}", exc_info=True)
            summary["errors"].append(msg)
            summary["tiktok"][handle] = 0

    # --- YouTube channels ---
    for channel in YOUTUBE_CHANNELS:
        channel_name = channel.get("name", channel.get("handle", "unknown"))
        logger.info(f"[Training extraction] Processing YouTube channel: {channel_name}")
        try:
            count = await extract_youtube_channel_transcripts(channel, max_videos=50)
            summary["youtube"][channel_name] = count
            summary["total_youtube"] += count
        except Exception as e:
            msg = f"YouTube {channel_name} extraction failed: {e}"
            logger.error(f"[Training extraction] {msg}", exc_info=True)
            summary["errors"].append(msg)
            summary["youtube"][channel_name] = 0

    summary["total_stored"] = summary["total_tiktok"] + summary["total_youtube"]

    logger.info(
        f"[Training extraction] Complete. "
        f"TikTok: {summary['total_tiktok']} | "
        f"YouTube: {summary['total_youtube']} | "
        f"Total: {summary['total_stored']} | "
        f"Errors: {len(summary['errors'])}"
    )

    return summary
