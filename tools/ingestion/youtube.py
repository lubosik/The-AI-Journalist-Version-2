import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from config import APIFY_ACTORS, LOOKBACK_DAYS
from db.queries import insert_content_item, update_source_last_scraped
from ingestion.apify_runner import run_actor
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()

logger = logging.getLogger(__name__)


def normalise_youtube_url(url: str) -> str:
    """Convert YouTube share and Shorts URLs to a canonical watch URL."""
    if not url:
        return url
    short = re.match(r"https?://youtu\.be/([a-zA-Z0-9_-]+)", url)
    if short:
        return f"https://www.youtube.com/watch?v={short.group(1)}"
    shorts = re.match(
        r"https?://(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]+)",
        url,
    )
    if shorts:
        return f"https://www.youtube.com/watch?v={shorts.group(1)}"
    parsed = urlparse(url)
    if "youtube.com" in parsed.netloc:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    return url


def _extract_transcript(item: dict) -> str:
    transcript = (
        item.get("transcript")
        or item.get("text")
        or item.get("captions")
        or item.get("subtitles")
        or item.get("searchResult")
        or item.get("data")
        or ""
    )
    if isinstance(transcript, list):
        return " ".join(
            str(part.get("text") or part.get("content") or "")
            for part in transcript
            if isinstance(part, dict)
        ).strip()
    if isinstance(transcript, dict):
        nested = transcript.get("transcript") or transcript.get("text") or ""
        return str(nested).strip()
    return str(transcript).strip()


def _parse_youtube_date(value) -> Optional[datetime]:
    """Parse YouTube date strings into timezone-aware datetimes."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(value)).astimezone(timezone.utc)
    except Exception:
        pass
    return None


async def _get_channel_video_urls(channel: dict, max_videos: int = 10) -> list[dict]:
    """
    Step 1: Use streamers/youtube-scraper to fetch recent video metadata from a channel.
    Returns list of dicts with at minimum 'url', 'title', 'date' keys.
    """
    channel_name = channel.get("name", channel.get("handle", "unknown"))
    try:
        # apidojo/youtube-scraper requires at least 10 results per channel run.
        requested = max(max_videos, 10)
        items = await run_actor(
            APIFY_ACTORS["youtube_channel"],
            input_data={
                "startUrls": [normalise_youtube_url(channel["url"])],
                "youtubeHandles": [channel.get("handle", "").lstrip("@")] if channel.get("handle") else [],
                "maxItems": requested,
                "sort": "u",
            },
            timeout_secs=300,
        )
        logger.info(f"YouTube channel scraper returned {len(items)} videos for {channel_name}")
        return items
    except Exception as e:
        logger.error(f"Failed to get video list for {channel_name}: {e}")
        return []


async def _get_transcripts_for_urls(video_urls: list[str]) -> dict[str, str]:
    """
    Step 2: Use scrape-creators/best-youtube-transcripts-scraper to get transcripts.
    Returns dict of {video_url: transcript_text}.
    """
    if not video_urls:
        return {}
    normalised_urls = [normalise_youtube_url(url) for url in video_urls]
    try:
        items = await run_actor(
            APIFY_ACTORS["youtube_transcripts"],
            input_data={"videoUrls": normalised_urls},
            timeout_secs=300,
        )
        result = {}
        for item in items:
            url = normalise_youtube_url(item.get("url") or item.get("videoUrl") or "")
            transcript = _extract_transcript(item)
            if url and transcript:
                result[url] = str(transcript)
        logger.info(f"Transcript scraper returned transcripts for {len(result)}/{len(normalised_urls)} videos")
        return result
    except Exception as e:
        logger.error(f"Transcript scraper failed: {e}")
        return {}


async def ingest_youtube_channel(channel: dict, max_videos: int = 5) -> int:
    """
    Ingest YouTube channel transcripts using a two-step Apify approach:
    1. Get recent video URLs via streamers/youtube-scraper
    2. Get transcripts via scrape-creators/best-youtube-transcripts-scraper
    Returns count of new items stored.
    """
    channel_name = channel.get("name", channel.get("handle", "unknown"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    logger.info(f"Ingesting YouTube channel: {channel_name} (max={max_videos})")

    # Step 1: Get video metadata (URL + date + title)
    video_items = await _get_channel_video_urls(channel, max_videos=max_videos)
    if not video_items:
        logger.warning(f"YouTube {channel_name}: no videos returned from channel scraper")
        update_source_last_scraped("youtube", channel_name)
        return 0

    # Filter to recent videos only
    recent_items = []
    for item in video_items:
        published_at = _parse_youtube_date(
            item.get("date") or item.get("publishedAt") or item.get("publishDate") or item.get("uploadDate")
        )
        if published_at and published_at < cutoff:
            logger.debug(f"Skipping old video: {item.get('title', '')[:60]} ({published_at.date()})")
            continue
        recent_items.append(item)

    if not recent_items:
        logger.info(f"YouTube {channel_name}: no videos within {LOOKBACK_DAYS} days")
        update_source_last_scraped("youtube", channel_name)
        return 0

    logger.info(f"YouTube {channel_name}: {len(recent_items)} recent videos to process")

    # Collect video URLs for transcript fetching
    video_urls = []
    for item in recent_items:
        url = item.get("url") or item.get("videoUrl") or ""
        if not url and item.get("id"):
            url = f"https://www.youtube.com/watch?v={item['id']}"
        if url:
            video_urls.append(normalise_youtube_url(url))
    video_urls = [u for u in video_urls if u]

    if not video_urls:
        logger.warning(f"YouTube {channel_name}: no valid URLs in recent items")
        update_source_last_scraped("youtube", channel_name)
        return 0

    # Step 2: Fetch transcripts
    transcripts = await _get_transcripts_for_urls(video_urls)

    stored_count = 0
    for item in recent_items:
        url = item.get("url") or item.get("videoUrl") or ""
        if not url and item.get("id"):
            url = f"https://www.youtube.com/watch?v={item['id']}"
        url = normalise_youtube_url(url)
        title = item.get("title") or ""
        published_at = _parse_youtube_date(
            item.get("date") or item.get("publishedAt") or item.get("publishDate") or item.get("uploadDate")
        )

        transcript = transcripts.get(url, "")
        raw_text = f"{title}\n\n{transcript}".strip() if transcript else title.strip()

        if not raw_text:
            logger.debug(f"Skipping empty item: {url}")
            continue

        try:
            if is_duplicate(url or None, raw_text):
                logger.debug(f"YouTube duplicate skipped: {url}")
                continue

            relevance = await check_relevance(raw_text[:1500])
            if relevance["score"] < 4:
                logger.debug(f"YouTube low relevance ({relevance['score']}/10): {title[:60]}")
                continue

            tags = await generate_tags(raw_text)
            content_hash = generate_content_hash(raw_text)

            item_record = {
                "source_type": "youtube",
                "source_name": channel_name,
                "source_url": url if url else None,
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
                    "channel_url": channel.get("url"),
                    "channel_handle": channel.get("handle"),
                    "has_transcript": bool(transcript),
                    "view_count": item.get("viewCount"),
                },
            }

            content_id = insert_content_item(item_record)
            logger.info(f"Stored YouTube item: {title[:60]} (id={content_id})")

            chunks = chunk_text(raw_text)
            await embed_and_store_chunks(content_id, chunks)
            stored_count += 1

        except Exception as e:
            logger.error(f"YouTube item processing error for {channel_name}/{url}: {e}", exc_info=True)
            continue

    update_source_last_scraped("youtube", channel_name)
    logger.info(f"YouTube {channel_name}: stored {stored_count} new items")
    return stored_count


async def ingest_single_youtube_video(url: str) -> dict:
    """
    Process a single YouTube video regardless of age.
    Uses the transcript scraper directly with the video URL.
    Returns {"stored": bool, "title": str, "chunks": int, "reason": str}.
    """
    url = normalise_youtube_url(url)
    logger.info(f"Ingesting single YouTube video: {url}")

    try:
        items = await run_actor(
            APIFY_ACTORS["youtube_transcript_v2"],
            input_data={"videoUrl": url},
            timeout_secs=300,
        )
    except Exception as e:
        logger.error(f"YouTube single video Apify error for {url}: {e}")
        return {"stored": False, "title": "", "chunks": 0, "reason": str(e)}

    if not items:
        return {"stored": False, "title": "", "chunks": 0, "reason": "No items returned from Apify"}

    item = items[0]

    try:
        transcript = _extract_transcript(item)
        title = item.get("title") or item.get("videoTitle") or ""
        published_raw = item.get("publishedAt") or item.get("uploadDate") or item.get("date")
        published_at = _parse_youtube_date(published_raw)

        raw_text = f"{title}\n\n{transcript}".strip() if transcript else title.strip()

        if not raw_text:
            return {"stored": False, "title": title, "chunks": 0, "reason": "Empty transcript and title"}

        if is_duplicate(url, raw_text):
            return {"stored": False, "title": title, "chunks": 0, "reason": "Already in database"}

        tags = await generate_tags(raw_text)
        relevance = await check_relevance(raw_text[:1500])
        content_hash = generate_content_hash(raw_text)

        item_record = {
            "source_type": "youtube",
            "source_name": item.get("channelName") or "manual",
            "source_url": url,
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
                "manual_add": True,
            },
        }

        content_id = insert_content_item(item_record)
        chunks = chunk_text(raw_text)
        chunk_count = len(chunks)
        await embed_and_store_chunks(content_id, chunks)

        logger.info(f"Stored single YouTube video: {title[:60]} ({chunk_count} chunks)")
        return {"stored": True, "title": title, "chunks": chunk_count, "reason": "Stored successfully"}

    except Exception as e:
        logger.error(f"YouTube single video processing error for {url}: {e}", exc_info=True)
        return {"stored": False, "title": "", "chunks": 0, "reason": str(e)}
