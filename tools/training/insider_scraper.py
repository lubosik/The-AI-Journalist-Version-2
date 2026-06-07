"""
training/insider_scraper.py

Insider newsletter training data scraper.

Two modes:
  1. initial_insider_scrape() — runs 12 broad queries once to seed the DB with
     insider newsletter writing style examples from well-known publications.
  2. scrape_new_insider_samples() — daily rotation of 20+ queries, picking 3-4
     per run based on day-of-year round-robin, to continuously find new examples.

All results are stored in content_items with source_type='insider_sample' and
is_voice_sample=True, which feeds directly into the style analyser's corpus
(style_analyser already pulls is_voice_sample=True rows when building the bible).
"""

import asyncio
import logging
from datetime import date, datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Query banks
# ---------------------------------------------------------------------------

# 12 broad seed queries — used only on first run
_INITIAL_QUERIES = [
    "The Information newsletter writing style examples insider tech",
    "Puck newsletter writing style insider media",
    "Axios Pro newsletter format insider examples",
    "WSJ Heard on the Street column writing style",
    "The Economist dry wit concise financial journalism examples",
    "Bloomberg Odd Lots insider finance podcast newsletter",
    "Matt Levine Money Stuff Bloomberg newsletter style examples",
    "Morning Brew insider tone newsletter examples",
    "Lenny's Newsletter insider product newsletter examples",
    "The Information VC funding insider newsletter format",
    "Dealbook NYT insider finance newsletter writing",
    "Financial Times Lex column concise punchy analysis style",
]

# 20+ rotation queries — used daily, 3-4 per run based on day-of-year
_ROTATION_QUERIES = [
    "Matt Levine Money Stuff dry wit financial journalism examples",
    "Michael Lewis financial storytelling style prose examples",
    "Axios Pro Rata VC newsletter concise format",
    "The Information paywall insider tech journalism writing",
    "Puck media industry newsletter insider commentary",
    "Bloomberg Five Things finance newsletter format",
    "FT alphaville dry commentary financial markets writing",
    "WSJ heard on the street column format dry analysis",
    "Dealbook NYT insider finance tone examples",
    "Lenny's Newsletter subscriber product insights writing style",
    "The Economist leaders section concise punchy argumentative",
    "Morning Brew witty business newsletter writing examples",
    "Stratechery Ben Thompson insider tech commentary",
    "Axios login tech newsletter insider format examples",
    "The Information weekly newsletter venture capital insider",
    "Bloomberg CityLab insider urban policy newsletter writing",
    "Politico Playbook insider political newsletter tone",
    "Semafor insider media company newsletter format",
    "The Ken insider Asian business newsletter writing examples",
    "Substack growth financial newsletter insider commentary style",
    "Private equity news insider newsletter tone and format",
    "Secondaries Investor newsletter writing style format",
    "PitchBook VC newsletter insider market commentary",
    "Bloomberg PE newsletter insider private equity tone",
]

# Number of daily rotation queries per run
_DAILY_BATCH_SIZE = 4


def _build_source_url(query: str) -> str:
    """Build a deterministic dedup key from the query string."""
    slug = query.replace(" ", "_")[:60]
    return f"insider_sample:{slug}"


async def _research_and_store(query: str) -> dict:
    """
    Research one insider newsletter query and store the result if new.

    Returns:
        {"query": str, "stored": bool, "content_id": str|None, "skipped": bool}
    """
    from agents.research_agent import research_topic
    from db.queries import content_exists_by_url, insert_content_item

    source_url = _build_source_url(query)

    # Dedup check — skip if this query has already been stored
    if content_exists_by_url(source_url):
        logger.info("[insider_scraper] Skipping already-stored query: %s", query[:60])
        return {"query": query, "stored": False, "content_id": None, "skipped": True}

    try:
        result = await research_topic(query, deep=False)
        findings = result.get("findings", "")

        if not findings or len(findings) < 100:
            logger.warning(
                "[insider_scraper] Query returned insufficient content (<100 chars): %s",
                query[:60],
            )
            return {"query": query, "stored": False, "content_id": None, "skipped": False}

        # source_type must match the DB CHECK constraint — use 'rss' as the
        # closest valid type for synthetic training samples. The is_voice_sample
        # flag and metadata.insider_training distinguish these from real RSS items.
        item = {
            "source_type": "rss",
            "source_name": "insider_training_scraper",
            "title": f"Insider Newsletter Sample: {query}",
            "raw_text": findings,
            "source_url": source_url,
            "is_voice_sample": True,
            "metadata": {
                "insider_training": True,
                "query": query,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        content_id = insert_content_item(item)
        logger.info(
            "[insider_scraper] Stored insider sample for query: %s (id=%s)",
            query[:60],
            content_id,
        )
        return {"query": query, "stored": True, "content_id": content_id, "skipped": False}

    except Exception as e:
        logger.error("[insider_scraper] Failed for query '%s': %s", query[:60], e)
        return {"query": query, "stored": False, "content_id": None, "skipped": False}


async def initial_insider_scrape() -> list[dict]:
    """
    Seed the DB with insider newsletter style examples.
    Runs all 12 initial queries in parallel. Skips any already stored.

    Call this once manually or from a one-time setup script.

    Returns:
        List of result dicts with keys: query, stored, content_id, skipped
    """
    logger.info(
        "[insider_scraper] Starting initial seed scrape (%d queries)", len(_INITIAL_QUERIES)
    )

    results = await asyncio.gather(
        *[_research_and_store(q) for q in _INITIAL_QUERIES],
        return_exceptions=False,
    )

    stored_count = sum(1 for r in results if r.get("stored"))
    skipped_count = sum(1 for r in results if r.get("skipped"))
    logger.info(
        "[insider_scraper] Initial scrape complete. stored=%d skipped=%d total=%d",
        stored_count,
        skipped_count,
        len(results),
    )
    return results


async def scrape_new_insider_samples() -> list[dict]:
    """
    Daily rotation scrape — picks _DAILY_BATCH_SIZE queries from _ROTATION_QUERIES
    based on a round-robin derived from day-of-year. This ensures different queries
    run each day without complex state tracking.

    Only stores items not already in the DB (checked by source_url dedup key).
    Called daily from the scheduler.

    Returns:
        List of result dicts with keys: query, stored, content_id, skipped
    """
    day_of_year = date.today().timetuple().tm_yday
    total = len(_ROTATION_QUERIES)

    # Pick _DAILY_BATCH_SIZE queries starting at today's offset, wrapping around
    start_idx = (day_of_year * _DAILY_BATCH_SIZE) % total
    indices = [(start_idx + i) % total for i in range(_DAILY_BATCH_SIZE)]
    queries = [_ROTATION_QUERIES[i] for i in indices]

    logger.info(
        "[insider_scraper] Daily rotation scrape: day_of_year=%d start_idx=%d queries=%s",
        day_of_year,
        start_idx,
        [q[:40] for q in queries],
    )

    results = await asyncio.gather(
        *[_research_and_store(q) for q in queries],
        return_exceptions=False,
    )

    stored_count = sum(1 for r in results if r.get("stored"))
    skipped_count = sum(1 for r in results if r.get("skipped"))
    logger.info(
        "[insider_scraper] Daily rotation complete. stored=%d skipped=%d",
        stored_count,
        skipped_count,
    )
    return results
