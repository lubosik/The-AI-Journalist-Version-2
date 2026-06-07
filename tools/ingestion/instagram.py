import logging
import re
from datetime import datetime, timezone
from typing import Optional

from config import APIFY_ACTORS
from db.queries import content_exists_by_url, insert_content_item, update_source_last_scraped
from ingestion.apify_runner import run_actor
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

logger = logging.getLogger(__name__)


def extract_instagram_handle(url: str) -> str:
    match = re.search(r"instagram\.com/([^/?#]+)", url or "")
    handle = match.group(1) if match else ""
    return handle if handle.lower() not in {"p", "reel", "stories", "explore"} else ""


def _parse_instagram_date(value) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _post_url(item: dict, fallback: str = "") -> str:
    url = item.get("url") or item.get("postUrl") or fallback
    shortcode = item.get("shortCode") or item.get("shortcode")
    if not url and shortcode:
        url = f"https://www.instagram.com/p/{shortcode}/"
    elif url and not str(url).startswith("http"):
        url = f"https://www.instagram.com/p/{url}/"
    return str(url or "")


async def _store_post(item: dict, source_name: str, fallback_url: str = "") -> tuple[bool, str]:
    caption = str(item.get("caption") or item.get("text") or "").strip()
    if not caption:
        return False, ""
    post_url = _post_url(item, fallback_url)
    if post_url and content_exists_by_url(post_url):
        return False, ""
    if is_duplicate(post_url or None, caption):
        return False, ""

    tags = await generate_tags(caption, {"source": "instagram", "handle": source_name})
    relevance = await check_relevance(caption)
    content_id = insert_content_item({
        "source_type": "instagram",
        "source_name": source_name,
        "source_url": post_url or None,
        "title": caption[:200],
        "raw_text": caption,
        "published_at": _parse_instagram_date(
            item.get("timestamp") or item.get("takenAt") or item.get("date")
        ),
        "language": "en",
        "is_voice_sample": False,
        "is_deal_signal": tags.get("is_deal_signal", False),
        "topics": tags.get("topics", []),
        "metadata": {
            "content_hash": generate_content_hash(caption),
            "relevance_score": relevance.get("score", 0),
            "likes": item.get("likesCount") or item.get("likes") or 0,
            "comments": item.get("commentsCount") or item.get("comments") or 0,
            "handle": source_name,
        },
    })
    await embed_and_store_chunks(content_id, chunk_text(caption))
    return True, caption


async def ingest_instagram_url(url: str) -> dict:
    if "/p/" in url or "/reel/" in url:
        return await ingest_instagram_post(url)
    handle = extract_instagram_handle(url)
    if handle:
        return await ingest_instagram_profile(handle)
    return {"stored": False, "count": 0, "reason": "Could not parse Instagram URL"}


async def ingest_instagram_profile(handle: str, limit: int = 20) -> dict:
    clean_handle = handle.lstrip("@")
    try:
        items = await run_actor(
            APIFY_ACTORS["instagram_profile"],
            {
                "usernames": [clean_handle],
                "resultsType": "posts",
                "resultsLimit": limit,
            },
            timeout_secs=300,
        )
    except Exception as e:
        logger.error("[instagram] Profile actor failed for @%s: %s", clean_handle, e)
        return {"stored": False, "count": 0, "reason": str(e), "combined_text": ""}

    stored = 0
    texts = []
    for item in items or []:
        try:
            was_stored, caption = await _store_post(item, clean_handle)
            if was_stored:
                stored += 1
                texts.append(caption)
        except Exception as e:
            logger.error("[instagram] Item error for @%s: %s", clean_handle, e)
    update_source_last_scraped("instagram", clean_handle)
    return {
        "stored": stored > 0,
        "count": stored,
        "combined_text": "\n\n".join(texts),
        "reason": "" if stored else "No new posts found",
    }


async def ingest_instagram_post(post_url: str) -> dict:
    try:
        items = await run_actor(
            APIFY_ACTORS["instagram_post"],
            {"directUrls": [post_url]},
            timeout_secs=240,
        )
    except Exception as e:
        logger.error("[instagram] Single-post actor failed: %s", e)
        return {"stored": False, "count": 0, "reason": str(e)}
    if not items:
        return {"stored": False, "count": 0, "reason": "Actor returned no results"}

    item = items[0]
    source_name = item.get("ownerUsername") or extract_instagram_handle(post_url) or "instagram"
    try:
        stored, caption = await _store_post(item, source_name, post_url)
    except Exception as e:
        return {"stored": False, "count": 0, "reason": str(e)}
    return {
        "stored": stored,
        "count": 1 if stored else 0,
        "combined_text": caption,
        "reason": "" if stored else "Already in database or no caption found",
    }


async def ingest_instagram_account(account: dict) -> int:
    """Compatibility wrapper for scheduled/configured Instagram accounts."""
    handle = account.get("handle") or extract_instagram_handle(account.get("url", ""))
    if not handle:
        logger.warning("[instagram] No handle for configured account")
        return 0
    result = await ingest_instagram_profile(handle, account.get("results_limit", 30))
    return int(result.get("count", 0))
