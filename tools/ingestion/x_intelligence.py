"""
X Intelligence module — Grok-powered X/Twitter search via xAI Responses API.

Replaces the limited Apify Twitter scraper with Grok's x_search tool, which:
- Searches X in real time with date bounds
- Supports up to 10 handle filters per call
- Returns synthesized analysis + citation URLs pointing to real X posts
- Costs $0.005 per x_search invocation + token cost

This module is async but the underlying OpenAI SDK is sync.
All SDK calls are wrapped in asyncio.to_thread().
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

from config import MODELS, X_INTELLIGENCE_ACCOUNTS, X_KEYWORD_SEARCHES
from db.queries import insert_content_item, update_source_last_scraped
from intelligence.relevance import check_relevance
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks
from processing.tagger import generate_tags

load_dotenv()

logger = logging.getLogger(__name__)

# Regex to extract handle and post ID from X / Twitter citation URLs.
# Matches https://x.com/handle/status/123 and https://twitter.com/handle/status/123
_X_URL_RE = re.compile(
    r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)",
    re.IGNORECASE,
)


def _build_grok_client():
    """Return an OpenAI client pointed at the xAI Responses API endpoint."""
    from openai import OpenAI

    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        raise EnvironmentError("GROK_API_KEY is not set in the environment")
    return OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")


def _date_range(lookback_days: int) -> tuple[str, str]:
    """Return (from_date, to_date) as ISO-8601 date strings."""
    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(days=lookback_days)
    return from_dt.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def _call_grok_responses(
    prompt: str,
    from_date: str,
    to_date: str,
    allowed_handles: Optional[list[str]] = None,
    max_results: int = 50,
) -> tuple[str, list[str]]:
    """
    Synchronous Grok Responses API call with x_search tool.
    Returns (output_text, citations).
    Must be called inside asyncio.to_thread().

    Citations live in content.annotations on the message output item, not in a
    top-level response.citations field. Each annotation is an AnnotationURLCitation
    with a .url attribute.
    """
    client = _build_grok_client()

    tool: dict = {
        "type": "x_search",
        "from_date": from_date,
        "to_date": to_date,
        "max_search_results": max_results,
        "return_citations": True,
    }
    if allowed_handles:
        tool["allowed_x_handles"] = allowed_handles

    response = client.responses.create(
        model=MODELS["grok_x"],
        input=[{"role": "user", "content": prompt}],
        tools=[tool],
    )

    output_text: str = response.output_text or ""

    # Extract citation URLs from annotations on the message output item
    citations: list[str] = []
    for item in (response.output or []):
        if getattr(item, "type", None) == "message":
            for content_block in (getattr(item, "content", None) or []):
                for ann in (getattr(content_block, "annotations", None) or []):
                    url = getattr(ann, "url", None)
                    if url:
                        citations.append(url)

    return output_text, citations


def _extract_x_posts_from_citations(citations: list[str]) -> list[dict]:
    """
    Parse X/Twitter citation URLs and return list of dicts with handle and post_id.
    Skips non-post URLs (profile pages, search pages, etc.).
    """
    posts = []
    seen_ids: set[str] = set()
    for url in citations:
        m = _X_URL_RE.search(url)
        if m:
            handle = m.group(1)
            post_id = m.group(2)
            if post_id not in seen_ids:
                seen_ids.add(post_id)
                posts.append(
                    {
                        "handle": handle.lower(),
                        "post_id": post_id,
                        "url": f"https://x.com/{handle}/status/{post_id}",
                    }
                )
    return posts


async def _store_grok_result(
    output_text: str,
    citations: list[str],
    source_type: str,
    source_name: str,
    from_date: str,
    query_context: str = "",
) -> int:
    """
    Store a Grok synthesized result and its citation posts in Supabase.

    Strategy:
    - The synthesized output_text is the primary intelligence item.
    - Each citation URL that points to a real X post is stored as a separate
      lightweight item so Dom can drill into individual sources.

    Returns total count of new items stored (synthesis + individual citations).
    """
    stored = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- 1. Store the synthesized analysis as one item ---
    if output_text.strip():
        if is_duplicate(None, output_text):
            logger.debug("Grok synthesis duplicate — skipping: %s...", output_text[:60])
        else:
            relevance = await check_relevance(output_text)
            if relevance["score"] >= 4:
                tags = await generate_tags(
                    output_text,
                    {"source": source_type, "query": query_context},
                )
                content_hash = generate_content_hash(output_text)
                record = {
                    "source_type": source_type,
                    "source_name": source_name,
                    "source_url": None,
                    "author_handle": None,
                    "title": output_text[:200],
                    "raw_text": output_text,
                    "published_at": now_iso,
                    "language": "en",
                    "is_voice_sample": False,
                    "is_deal_signal": tags.get("is_deal_signal", False),
                    "topics": tags.get("topics", []),
                    "metadata": {
                        "content_hash": content_hash,
                        "relevance_score": relevance["score"],
                        "summary": tags.get("summary", ""),
                        "grok_query": query_context,
                        "citation_count": len(citations),
                        "from_date": from_date,
                        "ingested_via": "grok_x_search",
                    },
                }
                content_id = insert_content_item(record)
                chunks = chunk_text(output_text)
                await embed_and_store_chunks(content_id, chunks)
                stored += 1
                logger.info(
                    "Stored Grok synthesis [%s] relevance=%d/10 (id=%s)",
                    source_name,
                    relevance["score"],
                    content_id,
                )
            else:
                logger.debug(
                    "Grok synthesis low relevance (%d/10) for %s — skipping",
                    relevance["score"],
                    source_name,
                )

    # --- 2. Store individual citation posts ---
    citation_posts = _extract_x_posts_from_citations(citations)
    logger.debug("Grok [%s]: %d citation URLs -> %d parsed X posts", source_name, len(citations), len(citation_posts))

    for post in citation_posts:
        try:
            post_url = post["url"]
            handle = post["handle"]

            # We only have the URL from citations; the actual text comes from
            # the synthesized output. Store a lightweight stub so URL dedup
            # works correctly and Dom can click through.
            stub_text = f"[X post by @{handle} — cited in Grok intelligence sweep: {query_context}]"

            if is_duplicate(post_url, stub_text):
                logger.debug("Citation duplicate skipped: %s", post_url)
                continue

            record = {
                "source_type": "twitter",
                "source_name": f"@{handle}",
                "source_url": post_url,
                "author_handle": handle,
                "title": f"@{handle} post cited in Grok sweep",
                "raw_text": stub_text,
                "published_at": now_iso,
                "language": "en",
                "is_voice_sample": False,
                "is_deal_signal": False,
                "topics": [],
                "metadata": {
                    "content_hash": generate_content_hash(stub_text),
                    "post_id": post["post_id"],
                    "grok_query": query_context,
                    "ingested_via": "grok_citation",
                },
            }
            insert_content_item(record)
            stored += 1
            logger.debug("Stored citation stub: %s", post_url)

        except Exception as exc:
            logger.error("Citation post storage error for %s: %s", post.get("url"), exc)
            continue

    return stored


async def ingest_x_accounts_batch(handles: list[str], lookback_days: int) -> int:
    """
    Run a single x_search call filtered to up to 10 handles.
    Asks Grok to surface the most relevant posts from those accounts
    about VC secondaries, pre-IPO markets, and the tracked company universe.

    Args:
        handles: List of X handles WITHOUT @ prefix. Max 10 per API call.
        lookback_days: Number of days back to search.

    Returns:
        Count of new items stored.
    """
    if not handles:
        return 0
    if len(handles) > 10:
        logger.warning("ingest_x_accounts_batch received %d handles; API limit is 10. Truncating.", len(handles))
        handles = handles[:10]

    from_date, to_date = _date_range(lookback_days)
    handles_display = ", ".join(f"@{h}" for h in handles)
    logger.info("Grok account sweep: %s [%s -> %s]", handles_display, from_date, to_date)

    prompt = (
        "Find the most relevant and insightful posts from these accounts about "
        "VC secondaries, pre-IPO markets, venture capital, fundraising, and the companies "
        "Anthropic, OpenAI, SpaceX, Anduril, xAI, Databricks, Stripe. "
        "For each post include the author handle, post content, date, and any key data points. "
        "Focus on deal signals, valuation changes, tender offers, cap table moves, "
        "fundraise announcements, and analyst commentary on these companies."
    )

    try:
        output_text, citations = await asyncio.to_thread(
            _call_grok_responses,
            prompt,
            from_date,
            to_date,
            allowed_handles=handles,
            max_results=50,
        )
    except Exception as exc:
        logger.error(
            "Grok account sweep failed for handles [%s]: %s",
            handles_display,
            exc,
            exc_info=True,
        )
        return 0

    logger.info(
        "Grok account sweep [%s]: %d chars output, %d citations",
        handles_display,
        len(output_text),
        len(citations),
    )

    stored = await _store_grok_result(
        output_text=output_text,
        citations=citations,
        source_type="twitter",
        source_name=f"Grok sweep: {handles_display}",
        from_date=from_date,
        query_context=f"account sweep: {handles_display}",
    )

    for handle in handles:
        update_source_last_scraped("x_intelligence", handle)

    logger.info("Grok account sweep [%s]: stored %d new items", handles_display, stored)
    return stored


async def ingest_x_keyword_searches(queries: list[str], lookback_days: int) -> int:
    """
    Run a broad x_search (no handle filter) for each keyword query.
    Captures market-wide chatter about VC secondaries and tracked companies.

    Args:
        queries: List of search query strings.
        lookback_days: Number of days back to search.

    Returns:
        Total count of new items stored across all queries.
    """
    if not queries:
        return 0

    from_date, to_date = _date_range(lookback_days)
    total_stored = 0

    async def _run_one_query(query: str) -> int:
        logger.info("Grok keyword search: %r [%s -> %s]", query, from_date, to_date)
        prompt = (
            f"Search for recent X posts about: {query}. "
            "Synthesize the most relevant findings. Include author handles, "
            "key facts, dates, and any deal signals, valuation figures, or "
            "insider information that a VC secondaries analyst would find valuable."
        )
        try:
            output_text, citations = await asyncio.to_thread(
                _call_grok_responses,
                prompt,
                from_date,
                to_date,
                allowed_handles=None,  # broad search, no handle filter
                max_results=50,
            )
        except Exception as exc:
            logger.error("Grok keyword search failed for %r: %s", query, exc, exc_info=True)
            return 0

        logger.info(
            "Grok keyword %r: %d chars output, %d citations",
            query,
            len(output_text),
            len(citations),
        )

        stored = await _store_grok_result(
            output_text=output_text,
            citations=citations,
            source_type="twitter",
            source_name=f"Grok keyword: {query[:60]}",
            from_date=from_date,
            query_context=query,
        )
        logger.info("Grok keyword %r: stored %d new items", query, stored)
        return stored

    # Run all queries concurrently
    tasks = [_run_one_query(q) for q in queries]
    counts = await asyncio.gather(*tasks, return_exceptions=True)

    for q, result in zip(queries, counts):
        if isinstance(result, Exception):
            logger.error("Grok keyword search task raised: %s (query=%r)", result, q)
        else:
            total_stored += result

    update_source_last_scraped("x_intelligence", "keyword_sweep")
    logger.info("Grok keyword searches complete: %d total new items", total_stored)
    return total_stored


async def run_full_x_intelligence_sweep(lookback_days: int = 2) -> dict:
    """
    Orchestrate the full daily Grok X intelligence sweep.

    Steps:
    1. Batch X_INTELLIGENCE_ACCOUNTS into groups of 10 (API max per call).
    2. Run all account batches with a 0.5s stagger between them to be
       respectful of rate limits and avoid hammering the xAI API.
    3. Run all keyword searches concurrently.

    Returns:
        dict with keys:
          - "x_accounts_<batch_n>": items stored per batch
          - "x_keywords": total items from keyword sweep
    """
    handles = [acc["handle"] for acc in X_INTELLIGENCE_ACCOUNTS]
    # Chunk into batches of 10
    batches: list[list[str]] = [handles[i : i + 10] for i in range(0, len(handles), 10)]

    results: dict[str, int] = {}

    # --- Account batches (staggered, not all concurrent) ---
    logger.info(
        "Grok X Intelligence: starting %d account batch(es) + %d keyword search(es)",
        len(batches),
        len(X_KEYWORD_SEARCHES),
    )

    # Run batches sequentially with a stagger to avoid hammering the xAI API.
    # Coroutines are not started until awaited, so we must await each batch
    # in turn rather than gathering them all at once.
    for i, batch in enumerate(batches):
        key = f"x_accounts_batch_{i + 1}"
        if i > 0:
            await asyncio.sleep(0.5)  # stagger between batches
        try:
            count = await ingest_x_accounts_batch(batch, lookback_days)
            results[key] = count
        except Exception as exc:
            logger.error("Account batch %d raised: %s", i + 1, exc, exc_info=True)
            results[key] = 0

    # --- Keyword searches (concurrent) ---
    try:
        keyword_count = await ingest_x_keyword_searches(X_KEYWORD_SEARCHES, lookback_days)
        results["x_keywords"] = keyword_count
    except Exception as exc:
        logger.error("Grok keyword sweep raised: %s", exc, exc_info=True)
        results["x_keywords"] = 0

    total = sum(results.values())
    logger.info(
        "Grok X Intelligence sweep complete: %d total items. Breakdown: %s",
        total,
        results,
    )
    return results
