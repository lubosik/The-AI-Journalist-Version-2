import asyncio
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import html2text
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from config import LOOKBACK_DAYS
from db.queries import (
    insert_content_item,
    mark_content_as_voice_sample,
    update_source_last_scraped,
)
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()

logger = logging.getLogger(__name__)

_html_converter = html2text.HTML2Text()
_html_converter.ignore_links = False
_html_converter.ignore_images = True
_html_converter.body_width = 0


def _clean_html(html_content: str) -> str:
    """Convert HTML to clean plain text."""
    if not html_content:
        return ""
    try:
        return _html_converter.handle(html_content).strip()
    except Exception:
        # Fallback to BeautifulSoup
        return BeautifulSoup(html_content, "lxml").get_text(separator=" ").strip()


def _parse_published_at(entry: dict) -> Optional[datetime]:
    """Parse published_at from a feedparser entry."""
    for field in ("published", "updated", "created"):
        val = entry.get(field)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc)
            except Exception:
                pass

    # Try parsed tuple
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass

    return None


def _fetch_feed(url: str) -> feedparser.FeedParserDict:
    """Synchronously fetch and parse RSS feed."""
    return feedparser.parse(url)


async def ingest_rss_feed(
    feed: dict,
    lookback_days: int = None,
    is_voice_sample: bool = False,
) -> int:
    """
    Ingest an RSS feed, storing new relevant items.
    Returns count of new items stored.
    """
    if lookback_days is None:
        lookback_days = LOOKBACK_DAYS

    feed_name = feed.get("name", feed["url"])
    feed_url = feed["url"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    logger.info(f"Ingesting RSS: {feed_name} ({feed_url}) lookback={lookback_days}d")

    try:
        parsed = await asyncio.to_thread(_fetch_feed, feed_url)
    except Exception as e:
        logger.error(f"RSS fetch error for {feed_url}: {e}")
        return 0

    entries = parsed.get("entries", [])
    logger.info(f"RSS {feed_name}: found {len(entries)} entries")

    stored_count = 0

    for entry in entries:
        try:
            # Parse published date
            published_at = _parse_published_at(entry)
            if published_at and published_at < cutoff:
                logger.debug(f"Skipping old entry: {entry.get('title', '')[:60]}")
                continue

            # Extract content
            content_list = entry.get("content", [])
            if content_list:
                raw_html = content_list[0].get("value", "")
            else:
                raw_html = entry.get("summary", "")

            cleaned_content = _clean_html(raw_html)
            title = entry.get("title", "")
            link = entry.get("link", "")

            raw_text = f"{title}\n\n{cleaned_content}".strip()
            if not raw_text:
                logger.debug(f"Skipping empty entry: {link}")
                continue

            # Dedup check
            if is_duplicate(link or None, raw_text):
                logger.debug(f"Duplicate skipped: {link}")
                continue

            # Relevance check (skip unless voice sample or score >= 4)
            if not is_voice_sample:
                relevance = await check_relevance(raw_text)
                if relevance["score"] < 4:
                    logger.debug(
                        f"Low relevance ({relevance['score']}/10) skipped: {title[:60]}"
                    )
                    continue
            else:
                relevance = {"score": 7, "relevant": True, "reason": "voice_sample"}

            # Generate tags
            tags = await generate_tags(raw_text)

            # Build content item
            content_hash = generate_content_hash(raw_text)

            item = {
                "source_type": "rss",
                "source_name": feed_name,
                "source_url": link if link else None,
                "title": title[:500] if title else None,
                "raw_text": raw_text,
                "published_at": published_at.isoformat() if published_at else None,
                "language": "en",
                "is_voice_sample": is_voice_sample,
                "is_deal_signal": tags.get("is_deal_signal", False),
                "topics": tags.get("topics", []),
                "metadata": {
                    "content_hash": content_hash,
                    "relevance_score": relevance["score"],
                    "summary": tags.get("summary", ""),
                    "feed_url": feed_url,
                },
            }

            content_id = insert_content_item(item)
            logger.info(f"Stored RSS item: {title[:60]} (id={content_id})")

            if is_voice_sample:
                mark_content_as_voice_sample(content_id)

            # Chunk and embed
            chunks = chunk_text(raw_text)
            await embed_and_store_chunks(content_id, chunks)

            stored_count += 1

        except Exception as e:
            logger.error(f"RSS entry processing error for {feed_name}: {e}", exc_info=True)
            continue

    update_source_last_scraped("rss", feed_name)
    logger.info(f"RSS {feed_name}: stored {stored_count} new items")
    return stored_count
