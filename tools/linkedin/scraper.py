"""
linkedin/scraper.py

Scrapes Dom's LinkedIn posts via Apify and stores them in linkedin_posts + content_items.
"""

import logging

from db.client import get_client
from db.queries import content_exists_by_url, insert_content_item
from ingestion.apify_runner import run_actor
from processing.chunker import chunk_text
from processing.embedder import embed_and_store_chunks

logger = logging.getLogger(__name__)

DOM_LINKEDIN_URL = "https://www.linkedin.com/in/dominickpandolfo/"
LINKEDIN_ACTOR = "Wpp1BZ6yGWjySadk3"


def _safe_int(value) -> int:
    """
    Extract an integer from a field that may be an int, float, string, list, or None.
    - If it's a list, return its length (Apify sometimes returns comment/reaction lists).
    - If it's a dict, return 0.
    - Otherwise, coerce to int safely.
    """
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def scrape_dom_linkedin() -> int:
    """
    Scrape Dom's LinkedIn posts via Apify and store in linkedin_posts + content_items.
    Returns count of newly stored posts.
    """
    logger.info("[linkedin_scraper] Starting Dom's LinkedIn scrape")

    try:
        result = await run_actor(
            LINKEDIN_ACTOR,
            {
                "urls": [DOM_LINKEDIN_URL],
                "limitPerSource": 100,
                "deepScrape": True,
                "rawData": False,
            },
            timeout_secs=300,
        )
    except Exception as e:
        logger.error(f"[linkedin_scraper] Apify actor failed: {e}")
        raise

    logger.info(f"[linkedin_scraper] Apify returned {len(result)} posts")

    client_db = get_client()
    stored = 0

    for post in result:
        try:
            post_url = post.get("url") or post.get("postUrl") or post.get("shareUrl") or ""
            post_text = post.get("text") or post.get("commentary") or post.get("description") or ""

            if not post_text or len(post_text.strip()) < 20:
                continue

            post_text = post_text.strip()

            # Dedup by URL
            if post_url and content_exists_by_url(post_url):
                logger.debug(f"[linkedin_scraper] Duplicate, skipping: {post_url[:80]}")
                continue

            likes = _safe_int(post.get("likeCount") or post.get("likes"))
            comments = _safe_int(post.get("commentCount") or post.get("comments"))
            shares = _safe_int(post.get("shareCount") or post.get("shares"))
            impressions = _safe_int(post.get("impressionCount"))
            posted_at = post.get("postedAt") or post.get("date") or post.get("createdAt")

            # Store in linkedin_posts table
            try:
                client_db.table("linkedin_posts").insert({
                    "post_url": post_url or None,
                    "post_text": post_text,
                    "post_date": posted_at,
                    "likes": likes,
                    "comments": comments,
                    "shares": shares,
                    "impressions": impressions,
                    "post_type": post.get("type") or "post",
                    "is_voice_sample": True,
                    "metadata": post,
                }).execute()
            except Exception as e:
                logger.warning(f"[linkedin_scraper] linkedin_posts insert failed: {e}")

            # Also store in content_items for embedding/search
            content_id = insert_content_item({
                "source_type": "linkedin",
                "source_name": "dominickpandolfo",
                "source_url": post_url or None,
                "title": post_text[:200],
                "raw_text": post_text,
                "published_at": posted_at,
                "language": "en",
                "is_voice_sample": True,
                "is_deal_signal": False,
                "topics": ["linkedin", "personal_brand"],
                "metadata": {
                    "post_type": post.get("type", "post"),
                    "likes": likes,
                    "comments": comments,
                },
            })

            chunks = chunk_text(post_text)
            await embed_and_store_chunks(content_id, chunks)
            stored += 1

        except Exception as e:
            logger.error(f"[linkedin_scraper] Error processing post: {e}", exc_info=True)
            continue

    logger.info(f"[linkedin_scraper] Stored {stored} new LinkedIn posts")
    return stored
