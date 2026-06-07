import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv

from config import APIFY_ACTORS, LOOKBACK_DAYS, X_TRACKED_COMPANIES
from db.queries import content_exists_by_url, insert_content_item, update_source_last_scraped
from ingestion.apify_runner import run_actor
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()

logger = logging.getLogger(__name__)


def extract_twitter_handle(url: str) -> str:
    match = re.search(r"(?:twitter\.com|x\.com)/([^/?#]+)", url or "")
    handle = match.group(1) if match else ""
    return "" if handle.lower() in {"home", "search", "explore", "i"} else handle


def _tweet_text(item: dict) -> str:
    return str(
        item.get("text")
        or item.get("full_text")
        or item.get("fullText")
        or item.get("content")
        or ""
    ).strip()


def _tweet_url(item: dict, handle: str = "") -> str:
    url = item.get("url") or item.get("tweet_url") or item.get("tweetUrl") or item.get("twitterUrl") or ""
    tweet_id = item.get("id_str") or item.get("id") or item.get("tweetId")
    if not url and tweet_id and handle:
        url = f"https://x.com/{handle.lstrip('@')}/status/{tweet_id}"
    return str(url)


async def _store_tweet(item: dict, handle: str, source_name: str) -> tuple[bool, str]:
    text = _tweet_text(item)
    if not text or text.startswith("RT @"):
        return False, ""

    tweet_url = _tweet_url(item, handle)
    if tweet_url and content_exists_by_url(tweet_url):
        return False, ""
    if is_duplicate(tweet_url or None, text):
        return False, ""

    created_at = item.get("created_at") or item.get("createdAt")
    published_at = _parse_twitter_date(created_at)
    tags = await generate_tags(text, {"source": "twitter", "handle": handle})
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    metadata = {
        "content_hash": generate_content_hash(text),
        "tweet_id": str(item.get("id_str") or item.get("id") or item.get("tweetId") or ""),
        "retweet_count": item.get("retweet_count") or item.get("retweetCount") or 0,
        "like_count": item.get("like_count") or item.get("likeCount") or 0,
        "reply_count": item.get("reply_count") or item.get("replyCount") or 0,
        "view_count": item.get("view_count") or item.get("viewCount") or 0,
        "author_name": author.get("name"),
    }
    content_id = insert_content_item({
        "source_type": "twitter",
        "source_name": source_name,
        "source_url": tweet_url or None,
        "author_handle": handle.lstrip("@"),
        "title": text[:200],
        "raw_text": text,
        "published_at": published_at.isoformat() if published_at else None,
        "language": "en",
        "is_voice_sample": False,
        "is_deal_signal": tags.get("is_deal_signal", False),
        "topics": tags.get("topics", []),
        "metadata": metadata,
    })
    await embed_and_store_chunks(content_id, chunk_text(text))
    return True, text


async def ingest_twitter_url(url: str) -> dict:
    handle = extract_twitter_handle(url)
    if not handle:
        return {"stored": False, "reason": "Could not extract handle from URL"}
    return await ingest_twitter_handle(handle)


async def ingest_twitter_handle(handle: str, max_tweets: int = 30) -> dict:
    clean_handle = handle.lstrip("@")
    try:
        items = await run_actor(
            APIFY_ACTORS["twitter_profile"],
            {
                "handles": [clean_handle],
                "maxTweets": max_tweets,
                "includeReplies": False,
            },
            timeout_secs=300,
        )
    except Exception as e:
        logger.error("Twitter profile actor failed for @%s: %s", clean_handle, e)
        return {"stored": False, "count": 0, "reason": str(e), "combined_text": ""}

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    stored = 0
    texts = []
    for item in items or []:
        published_at = _parse_twitter_date(item.get("created_at") or item.get("createdAt"))
        if published_at and published_at < cutoff:
            continue
        try:
            was_stored, text = await _store_tweet(item, clean_handle, clean_handle)
            if was_stored:
                stored += 1
                texts.append(text)
        except Exception as e:
            logger.error("Twitter item processing failed for @%s: %s", clean_handle, e)
    update_source_last_scraped("twitter", clean_handle)
    return {
        "stored": stored > 0,
        "count": stored,
        "combined_text": "\n\n".join(texts),
        "reason": "" if stored else "No new tweets found",
    }


async def ingest_twitter_search(query: str, limit: int = 50) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    try:
        items = await run_actor(
            APIFY_ACTORS["twitter_search"],
            {"searchTerms": [query], "maxTweets": limit, "since": cutoff},
            timeout_secs=360,
        )
    except Exception as e:
        logger.error("Twitter search actor failed for %r: %s", query, e)
        return {"stored": False, "count": 0, "reason": str(e), "combined_text": ""}

    stored = 0
    texts = []
    for item in items or []:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        handle = (
            item.get("username")
            or item.get("authorUsername")
            or author.get("username")
            or author.get("userName")
            or "x_search"
        )
        try:
            was_stored, text = await _store_tweet(item, str(handle), f"search:{query}")
            if was_stored:
                stored += 1
                texts.append(text)
        except Exception as e:
            logger.error("Twitter search item processing failed: %s", e)
    return {
        "stored": stored > 0,
        "count": stored,
        "combined_text": "\n\n".join(texts),
        "reason": "" if stored else "No new tweets found",
    }


def _parse_twitter_date(value) -> Optional[datetime]:
    """Parse Twitter date strings."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        # Twitter format: "Mon Jan 01 00:00:00 +0000 2024"
        return datetime.strptime(str(value), "%a %b %d %H:%M:%S +0000 %Y").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        pass
    return None


async def ingest_twitter_account(account: dict) -> int:
    """
    Ingest tweets from a Twitter account.
    Returns count of new items stored.

    Uses apidojo/tweet-scraper advanced search rather than profile scraping,
    because query searches are cheaper, date-bounded, and dedupe cleanly.
    """
    handle = account.get("handle", account.get("name", "unknown"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    logger.info(f"Ingesting Twitter account: {handle}")

    try:
        items = await run_actor(
            APIFY_ACTORS["twitter_profile"],
            input_data={
                "handles": [handle.lstrip("@")],
                "maxTweets": 50,
                "includeReplies": False,
            },
            timeout_secs=300,
        )
    except Exception as e:
        logger.error(f"Twitter Apify error for {handle}: {e}")
        return 0

    logger.info(f"Twitter {handle}: got {len(items)} raw items from Apify")
    stored_count = 0

    for item in items:
        try:
            text = item.get("text") or item.get("fullText") or item.get("content") or ""

            # Skip retweets
            if text.startswith("RT @"):
                logger.debug(f"Skipping retweet: {text[:60]}")
                continue

            if not text.strip():
                continue

            # Parse date
            created_at = item.get("createdAt") or item.get("created_at")
            published_at = _parse_twitter_date(created_at)

            # Filter by lookback
            if published_at and published_at < cutoff:
                logger.debug(f"Twitter item too old: {published_at}")
                continue

            # Build URL
            tweet_url = item.get("url") or item.get("tweetUrl") or ""
            if not tweet_url and item.get("id"):
                tweet_url = f"https://x.com/{handle}/status/{item['id']}"

            # Dedup
            if is_duplicate(tweet_url or None, text):
                logger.debug(f"Twitter duplicate skipped: {tweet_url}")
                continue

            # Relevance check
            relevance = await check_relevance(text)
            if relevance["score"] < 4:
                logger.debug(f"Twitter low relevance ({relevance['score']}/10): {text[:60]}")
                continue

            tags = await generate_tags(text, {"source": "twitter", "handle": handle})
            content_hash = generate_content_hash(text)

            item_record = {
                "source_type": "twitter",
                "source_name": account.get("name", handle),
                "source_url": tweet_url if tweet_url else None,
                "author_handle": handle,
                "title": text[:200],
                "raw_text": text,
                "published_at": published_at.isoformat() if published_at else None,
                "language": "en",
                "is_voice_sample": False,
                "is_deal_signal": tags.get("is_deal_signal", False),
                "topics": tags.get("topics", []),
                "metadata": {
                    "content_hash": content_hash,
                    "relevance_score": relevance["score"],
                    "summary": tags.get("summary", ""),
                    "tweet_id": str(item.get("id", "")),
                    "retweet_count": item.get("retweetCount"),
                    "like_count": item.get("likeCount"),
                    "reply_count": item.get("replyCount"),
                },
            }

            content_id = insert_content_item(item_record)
            logger.info(f"Stored tweet from {handle}: {text[:60]} (id={content_id})")

            chunks = chunk_text(text)
            await embed_and_store_chunks(content_id, chunks)

            stored_count += 1

        except Exception as e:
            logger.error(f"Twitter item processing error for {handle}: {e}", exc_info=True)
            continue

    update_source_last_scraped("twitter", handle)
    logger.info(f"Twitter {handle}: stored {stored_count} new items")
    return stored_count


def build_x_signal_queries(days_back: int = 2) -> list[str]:
    """
    Build batched X advanced-search queries for Dom's company universe.
    Queries are date-bounded and broad enough to satisfy the actor's 50-item
    minimum while still aimed at secondaries, rumors, and named-company news.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()
    company_terms = " OR ".join(f'"{name}"' if " " in name else name for name in X_TRACKED_COMPANIES)
    queries = [
        f"({company_terms}) (secondary OR secondaries OR pre-IPO OR valuation OR tender OR fundraise OR cap table) since:{since} lang:en -filter:retweets",
        f"(Anthropic OR OpenAI OR xAI) (rumor OR leaked OR trial OR lawsuit OR Altman OR Musk OR compute OR GPUs) since:{since} lang:en -filter:retweets",
        f"(SpaceX OR Anduril OR Databricks OR Stripe) (secondary OR valuation OR shares OR tender OR fundraise) since:{since} lang:en -filter:retweets",
    ]
    try:
        import json
        from db.queries import get_pipeline_state
        recent = json.loads(get_pipeline_state("recent_x_search_queries") or "[]")
        recent_set = {str(q) for q in recent[-20:]}
        filtered = [q for q in queries if q not in recent_set]
        return filtered or queries
    except Exception:
        return queries


async def ingest_twitter_searches(queries: list[str] | None = None, max_items: int = 150) -> int:
    """
    Run programmatic X searches through apidojo/tweet-scraper and store fresh,
    relevant tweets. One actor run can batch up to five search terms.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    search_terms = (queries or build_x_signal_queries(days_back=2))[:5]
    logger.info("Ingesting X signal search: %d queries", len(search_terms))

    try:
        items = await run_actor(
            APIFY_ACTORS["twitter_search"],
            input_data={
                "searchTerms": search_terms,
                "maxTweets": max(max_items, 50),
                "since": cutoff.date().isoformat(),
            },
            timeout_secs=420,
        )
    except Exception as e:
        logger.error("X signal search Apify error: %s", e)
        return 0

    logger.info("X signal search returned %d raw items", len(items))
    stored_count = 0
    for item in items:
        try:
            text = item.get("text") or item.get("fullText") or item.get("content") or ""
            if not text.strip() or text.startswith("RT @"):
                continue

            created_at = item.get("createdAt") or item.get("created_at")
            published_at = _parse_twitter_date(created_at)
            if published_at and published_at < cutoff:
                continue

            author = item.get("author") or {}
            handle = (
                author.get("userName")
                or author.get("username")
                or item.get("authorUsername")
                or "x_search"
            )
            tweet_url = item.get("url") or item.get("tweetUrl") or item.get("twitterUrl") or ""
            if not tweet_url and item.get("id"):
                tweet_url = f"https://x.com/{handle}/status/{item['id']}"

            if is_duplicate(tweet_url or None, text):
                continue

            relevance = await check_relevance(text)
            if relevance["score"] < 5:
                continue

            tags = await generate_tags(text, {"source": "twitter_search", "handle": handle})
            content_hash = generate_content_hash(text)
            record = {
                "source_type": "twitter",
                "source_name": f"@{handle}",
                "source_url": tweet_url or None,
                "author_handle": handle,
                "title": text[:200],
                "raw_text": text,
                "published_at": published_at.isoformat() if published_at else None,
                "language": "en",
                "is_voice_sample": False,
                "is_deal_signal": tags.get("is_deal_signal", False),
                "topics": tags.get("topics", []),
                "metadata": {
                    "content_hash": content_hash,
                    "relevance_score": relevance["score"],
                    "summary": tags.get("summary", ""),
                    "tweet_id": str(item.get("id", "")),
                    "retweet_count": item.get("retweetCount"),
                    "like_count": item.get("likeCount"),
                    "reply_count": item.get("replyCount"),
                    "search_term": item.get("searchTerm") or item.get("searchTerms"),
                    "ingested_via": "x_signal_search",
                },
            }
            content_id = insert_content_item(record)
            chunks = chunk_text(text)
            await embed_and_store_chunks(content_id, chunks)
            stored_count += 1
            logger.info("Stored X signal tweet from @%s (id=%s)", handle, content_id)
        except Exception as e:
            logger.error("X signal item processing error: %s", e, exc_info=True)
            continue

    update_source_last_scraped("twitter_search", "x_signal_search")
    try:
        import json
        from db.queries import get_pipeline_state, set_pipeline_state
        recent = json.loads(get_pipeline_state("recent_x_search_queries") or "[]")
        recent.extend(search_terms)
        set_pipeline_state("recent_x_search_queries", json.dumps(recent[-40:]))
    except Exception:
        pass
    logger.info("X signal search stored %d new items", stored_count)
    return stored_count


async def ingest_style_twitter_account(account: dict) -> int:
    """
    Scrape a style/comedy Twitter account for satirical voice training.
    No relevance check, no cookie requirement, no embeddings.
    Stores with topics=["satire","comedy_style"] and is_voice_sample=False.
    Safe to run daily — deduplicates by URL and content hash.
    Returns count of new items stored.
    """
    handle = account.get("handle", account.get("name", "unknown"))
    style_category = account.get("style_category", "satire")

    logger.info(f"Ingesting style account: @{handle}")

    try:
        items = await run_actor(
            APIFY_ACTORS["twitter_profile"],
            input_data={
                "handles": [handle.lstrip("@")],
                "maxTweets": 50,
                "includeReplies": False,
            },
            timeout_secs=300,
        )
    except Exception as e:
        logger.error(f"Style account Apify error for @{handle}: {e}")
        return 0

    logger.info(f"Style @{handle}: got {len(items)} raw items from Apify")
    stored_count = 0

    for item in items:
        try:
            text = item.get("full_text") or item.get("text") or item.get("content") or ""
            if not text.strip() or text.startswith("RT @"):
                continue

            tweet_url = item.get("url") or item.get("tweetUrl") or ""
            if not tweet_url:
                tweet_id = item.get("id_str") or item.get("id") or item.get("tweetId")
                if tweet_id:
                    tweet_url = f"https://x.com/{handle}/status/{tweet_id}"

            if is_duplicate(tweet_url or None, text):
                continue

            created_at = item.get("created_at") or item.get("createdAt")
            published_at = _parse_twitter_date(created_at)

            item_record = {
                "source_type": "twitter",
                "source_name": handle,
                "source_url": tweet_url or None,
                "author_handle": handle,
                "title": text[:200],
                "raw_text": text,
                "published_at": published_at.isoformat() if published_at else None,
                "language": "en",
                "is_voice_sample": False,
                "is_deal_signal": False,
                "topics": ["satire", "comedy_style"],
                "metadata": {
                    "content_hash": generate_content_hash(text),
                    "style_category": style_category,
                    "source_account": handle,
                },
            }

            insert_content_item(item_record)
            stored_count += 1

        except Exception as e:
            logger.error(f"Style account item error for @{handle}: {e}")
            continue

    update_source_last_scraped("twitter", handle)
    logger.info(f"Style @{handle}: stored {stored_count} new items")
    return stored_count
