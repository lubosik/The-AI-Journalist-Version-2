import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from openai import OpenAI
from dotenv import load_dotenv

from config import MODELS, OPENROUTER_BASE_URL
from db.client import get_client as get_db_client
from db.queries import (
    get_db_stats,
    get_recent_content_items,
    insert_content_item,
    semantic_search,
)
from ingestion.youtube import ingest_single_youtube_video
from processing.chunker import chunk_text
from processing.dedup import generate_content_hash, is_duplicate
from processing.embedder import embed_and_store_chunks, embed_texts
from processing.tagger import generate_tags
from intelligence.relevance import check_relevance

load_dotenv()

logger = logging.getLogger(__name__)

WEB_RESEARCH_SYSTEM_PROMPT = (
    "You are a research assistant for a top-tier venture capital and tech intelligence newsletter. "
    "Search for current, specific, factual information about the given query. "
    "Only use information from the last 48 hours unless it is clearly labelled as background. "
    "Prioritise specific, human, anecdotal stories only when they are new. Do not repeat older viral anecdotes. "
    "Focus on: Anthropic, OpenAI, SpaceX, Anduril, xAI, Stripe, Databricks and peers; "
    "fundraises, cap table moves, pre-IPO secondary trades, rumors, the Musk vs Altman lawsuit, and insider commentary from top VCs. "
    "Never reuse the Storm Duncan/Bay Area estate/Anthropic-home-payment anecdote unless the query provides a new update from the last 48 hours. "
    "Do NOT surface generic PE, mid-market buyouts, or content unrelated to named prominent tech companies. "
    "Return findings with source citations."
)


def _upcoming_friday_iso() -> str:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc)
    days_ahead = (6 - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # Sunday's edition is out — target next Sunday
    return (now + timedelta(days=days_ahead)).date().isoformat()


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _row_age_hours(row: dict) -> float | None:
    """Best-effort age calculation using published_at first, then scraped_at."""
    ts_raw = row.get("published_at") or row.get("scraped_at") or ""
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return None


def _fallback_research_brief(findings: str) -> dict:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", findings or "") if len(s.strip()) > 20]
    fact_sentences = [
        s for s in sentences
        if re.search(r"\d|\$|%|\b(raised|acquired|closed|launched|reported|announced|fund|deal|vehicle)\b", s, re.I)
    ]
    return {
        "summary": _clip(sentences[0] if sentences else findings, 500),
        "key_facts": [_clip(s, 180) for s in (fact_sentences or sentences)[:3]],
        "interesting_notes": [_clip(s, 180) for s in sentences[3:5] or sentences[1:3]],
    }


async def summarize_research_for_editors(query: str, findings: str) -> dict:
    """Return a concise editor-facing brief for raw research findings."""
    if not findings:
        return {"summary": "", "key_facts": [], "interesting_notes": []}
    try:
        client = _get_client()
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[{
                "role": "user",
                "content": (
                    "Summarize this research for two newsletter editors. Return JSON only: "
                    "{\"summary\":\"max 80 words\",\"key_facts\":[\"3 concrete facts\"],"
                    "\"interesting_notes\":[\"1-2 editorial notes\"]}. "
                    "Preserve names, numbers, and dates.\n\n"
                    f"Query: {query}\n\nFindings:\n{findings[:5000]}"
                ),
            }],
            temperature=0,
            max_tokens=600,
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
        parsed = json.loads(raw)
        return {
            "summary": _clip(str(parsed.get("summary", "")), 500),
            "key_facts": [_clip(str(x), 200) for x in (parsed.get("key_facts") or [])[:4]],
            "interesting_notes": [_clip(str(x), 200) for x in (parsed.get("interesting_notes") or [])[:3]],
        }
    except Exception as e:
        logger.warning("summarize_research_for_editors failed for %r: %s", query[:60], e)
        return _fallback_research_brief(findings)


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


async def search_database(query: str, days_back: int = 2) -> dict:
    """
    Semantic search over the internal knowledge base.
    Defaults to the last 48 hours. Widens to the current week only as
    background if no fresh results are found.
    """
    try:
        embeddings = await embed_texts([query])
        query_embedding = embeddings[0]

        results = semantic_search(query_embedding, days_back=days_back, limit=20)
        # Exclude Dom's LinkedIn posts — style training data, not content sources
        results = [r for r in results if r.get("source_type") != "linkedin"]
        if days_back <= 2:
            results = [r for r in results if (_row_age_hours(r) or 0) <= 48]
        widened = False
        background_only = False

        if not results and days_back < 7:
            logger.info(f"No results in {days_back} days, widening to 7 days as background")
            results = semantic_search(query_embedding, days_back=7, limit=20)
            results = [r for r in results if r.get("source_type") != "linkedin"]
            days_back = 7
            widened = True
            background_only = True

        return {
            "results": results,
            "count": len(results),
            "days_back_used": days_back,
            "widened": widened,
            "background_only": background_only,
            "freshness_note": (
                "Use widened 7-day results only as background. Do not present anything older than 48 hours as new."
                if background_only else
                "Fresh 48-hour search window."
            ),
        }
    except Exception as e:
        logger.error(f"search_database error: {e}")
        return {
            "results": [],
            "count": 0,
            "days_back_used": days_back,
            "widened": False,
            "background_only": False,
            "error": str(e),
        }


async def web_research(query: str, deep: bool = False) -> dict:
    """
    Search the live web using Perplexity Sonar via OpenRouter.
    Returns {"findings": str, "sources": list[str], "timestamp": str, "model_used": str}.
    """
    model = MODELS["deep_research"] if deep else MODELS["research"]
    client = _get_client()
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=[
                {"role": "system", "content": WEB_RESEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
        )

        content = response.choices[0].message.content or ""

        # Extract citations if available (Perplexity returns them in the response object)
        sources = []
        try:
            if hasattr(response, "citations"):
                sources = list(response.citations or [])
            elif hasattr(response.choices[0].message, "citations"):
                sources = list(response.choices[0].message.citations or [])
        except Exception:
            pass

        return {
            "findings": content,
            "sources": sources,
            "timestamp": timestamp,
            "model_used": model,
        }

    except Exception as e:
        logger.error(f"web_research error for query '{query[:60]}': {e}")
        return {
            "findings": f"Research failed: {str(e)}",
            "sources": [],
            "timestamp": timestamp,
            "model_used": model,
            "error": str(e),
        }


async def store_research(
    content: str,
    source_url: str,
    topic: str,
    source_name: str = "telegram_research",
    dom_requested: bool = False,
) -> dict:
    """
    Store research findings to the knowledge base after relevance check.
    dom_requested=True bypasses the relevance gate — Dom explicitly asked for this content.
    Returns {"stored": bool, "reason": str, "content_id": str | None, "relevance_score": int}.
    """
    try:
        relevance = await check_relevance(content)

        # Dom-requested content skips the gate entirely — he knows what he wants.
        # Standard content requires score >= 4 to avoid noise.
        min_score = 1 if dom_requested else 4
        if relevance["score"] < min_score:
            return {
                "stored": False,
                "reason": f"Low relevance score: {relevance['score']}/10 — {relevance['reason']}",
                "content_id": None,
                "relevance_score": relevance["score"],
            }

        if is_duplicate(source_url if source_url else None, content):
            return {
                "stored": False,
                "reason": "Already in database",
                "content_id": None,
                "relevance_score": relevance["score"],
            }

        tags = await generate_tags(content, {"topic": topic})
        content_hash = generate_content_hash(content)

        item = {
            "source_type": "telegram_tip",
            "source_name": source_name,
            "source_url": source_url if source_url else None,
            "title": topic[:500],
            "raw_text": content,
            "language": "en",
            "is_voice_sample": False,
            "is_deal_signal": tags.get("is_deal_signal", False),
            "topics": tags.get("topics", []),
            "metadata": {
                "content_hash": content_hash,
                "relevance_score": relevance["score"],
                "summary": tags.get("summary", ""),
                "topic": topic,
                "source_name": source_name,
                "edition_date": _upcoming_friday_iso(),
                **({"newsletter_include": True, "dom_requested": True} if dom_requested else {}),
            },
        }

        content_id = insert_content_item(item)
        try:
            get_db_client().table("content_items").update(
                {"assigned_edition_date": item["metadata"]["edition_date"]}
            ).eq("id", content_id).execute()
        except Exception as exc:
            logger.debug("assigned_edition_date update skipped for %s: %s", content_id, exc)
        chunks = chunk_text(content)
        await embed_and_store_chunks(content_id, chunks)

        logger.info(f"Stored research: {topic[:60]} (id={content_id})")
        try:
            from tracking.edition_tracker import track_content
            track_content(
                content_type='research',
                title=str(topic)[:100],
                body=str(content)[:2000],
                source_url=source_url if source_url else None,
                source_type='research',
                source_name=source_name,
                content_item_id=str(content_id) if content_id else None,
                added_by='dom' if dom_requested else 'system',
            )
        except Exception:
            pass
        return {
            "stored": True,
            "reason": "Stored successfully",
            "content_id": content_id,
            "relevance_score": relevance["score"],
        }

    except Exception as e:
        logger.error(f"store_research error: {e}")
        return {
            "stored": False,
            "reason": str(e),
            "content_id": None,
            "relevance_score": 0,
        }


async def get_recent_content_window(
    days_back: int = 2,
    topic: str | None = None,
    fresh_only: bool = True,
) -> dict:
    """
    Return everything ingested in the last `days_back` days, with the metadata the
    agent needs to synthesise a newsletter recap or weekly piece. Optionally
    filter by topic keyword overlap (case-insensitive substring match against
    title / topics array / first 800 chars of raw_text).

    Shape:
    {
        "days_back": int,
        "count": int,
        "items": [
            {
                "title": str,
                "source_type": str,
                "source_name": str,
                "source_url": str,
                "published_at": str,
                "topics": list[str],
                "is_deal_signal": bool,
                "summary": str,  # first ~400 chars of raw_text
            },
            ...
        ],
        "filtered_by_topic": str | None,
    }
    """
    try:
        requested_days = max(1, int(days_back or 2))
        items = await asyncio.to_thread(get_recent_content_items, requested_days, 200, fresh_only)

        topic_lower = (topic or "").strip().lower()
        filtered: list[dict] = []
        for item in items:
            title = (item.get("title") or "").strip()
            raw = (item.get("raw_text") or "").strip()
            topics_list = item.get("topics") or []

            if topic_lower:
                hay = (
                    title.lower()
                    + " "
                    + " ".join(str(t).lower() for t in topics_list)
                    + " "
                    + raw[:800].lower()
                )
                if topic_lower not in hay:
                    continue

            summary = raw[:400].replace("\n", " ").strip()
            # Compute age_days from the most reliable timestamp available.
            # Prefer published_at (when the source actually published) over
            # scraped_at (when we ingested it) because publish date is what
            # determines newsworthiness.
            ts_iso = item.get("published_at") or item.get("scraped_at") or ""
            age_days = None
            freshness = "unknown"
            if ts_iso:
                try:
                    ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_days = (datetime.now(timezone.utc) - ts).days
                    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                    freshness = "fresh" if age_hours <= 48 else "background"
                except Exception:
                    age_days = None
            if requested_days > 2 and freshness == "background":
                summary = "BACKGROUND ONLY, do not present as new: " + summary
            filtered.append({
                "title": title[:240],
                "source_type": item.get("source_type", ""),
                "source_name": item.get("source_name", ""),
                "source_url": item.get("source_url") or "",
                "published_at": ts_iso,
                "age_days": age_days,
                "freshness": freshness,
                "topics": topics_list,
                "is_deal_signal": bool(item.get("is_deal_signal")),
                "summary": summary,
            })

        return {
            "days_back": requested_days,
            "count": len(filtered),
            "items": filtered,
            "filtered_by_topic": topic if topic_lower else None,
            "freshness_note": (
                "Items marked background are older than 48 hours. They can explain context but must not be presented as new."
                if not fresh_only else
                "Only items from the last 48 hours are returned. Anything older is excluded."
            ),
        }
    except Exception as e:
        logger.error(f"get_recent_content_window error: {e}")
        return {
            "days_back": days_back,
            "count": 0,
            "items": [],
            "filtered_by_topic": topic,
            "error": str(e),
        }


# ── Source-check tracking ─────────────────────────────────────────────────
# Background flag so Dom can ask "check my sources" without triggering
# parallel Apify runs. Resets when the task finishes.
_source_check_state: dict = {
    "running": False,
    "started_at": None,
    "task": None,
}

def _web_fallback_queries() -> list[str]:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    return [
        f"Anthropic OpenAI SpaceX latest secondary funding rumor last 48 hours {today}",
        f"SpaceX Anduril pre-IPO secondary latest buyer seller rumor {today}",
        f"Musk Altman lawsuit latest testimony ruling filing last 48 hours {today}",
        f"OpenAI Anthropic xAI investor behavior fresh rumor {today}",
        f"Stripe Databricks IPO secondary market latest update {today}",
        f"top venture company named founder investor incident last 48 hours {today}",
    ]


def _looks_stale_or_repeated_research(text: str) -> bool:
    """Reject stale anecdotes Dom has already called out as bad signal."""
    lower = (text or "").lower()
    stale_markers = (
        "storm duncan",
        "bay area estate",
        "accepted anthropic shares as payment",
        "anthropic shares as payment for a home",
        "vika ventures",
        "iakovou",
        "keyport venture",
        "late stage asset management",
    )
    if any(marker in lower for marker in stale_markers):
        return True
    current_year = str(datetime.now(timezone.utc).year)
    old_years = ("2019", "2020", "2021", "2022", "2023", "2024")
    return any(year in lower for year in old_years) and current_year not in lower


async def _run_web_fallback(queries: list[str]) -> list[dict]:
    """
    Run a batch of web research queries in parallel via Perplexity.
    For each result, store each source URL as a separate content item so Dom
    gets real links. Falls back to storing the full findings blob if no source
    URLs are returned by the model.

    Returns a list of dicts:
      {title, source_url, topic, logged_at, relevance_score, content_id}
    """
    from datetime import datetime as _dt, timezone as _tz

    found: list[dict] = []

    async def _one(query: str) -> list[dict]:
        try:
            result = await web_research(query)
            findings = result.get("findings", "")
            sources = result.get("sources") or []
            if len(findings) < 200:
                return []
            if _looks_stale_or_repeated_research(findings):
                logger.info("[web_fallback] filtered stale/repeated result for query=%r", query[:80])
                return []
            brief = await summarize_research_for_editors(query, findings)
            logged_at = _dt.now(_tz.utc).isoformat()
            items_stored: list[dict] = []
            base_item = {
                "title": query,
                "topic": query,
                "logged_at": logged_at,
                "findings": findings,
                "summary": brief.get("summary", ""),
                "key_facts": brief.get("key_facts", []),
                "interesting_notes": brief.get("interesting_notes", []),
                "sources": sources,
            }
            if sources:
                # Store each cited URL as its own content item so Dom gets
                # a proper link + metadata entry rather than one blob.
                for src_url in sources[:3]:
                    stored = await store_research(
                        content=findings,
                        source_url=src_url,
                        topic=query,
                        source_name="herald_web_search",
                    )
                    if stored.get("stored"):
                        items_stored.append({
                            **base_item,
                            "source_url": src_url,
                            "relevance_score": stored.get("relevance_score", 0),
                            "content_id": stored.get("content_id"),
                        })
            else:
                # No source URLs returned — store the findings blob
                stored = await store_research(
                    content=findings,
                    source_url="",
                    topic=query,
                    source_name="herald_web_search",
                )
                if stored.get("stored"):
                    items_stored.append({
                        **base_item,
                        "source_url": "",
                        "relevance_score": stored.get("relevance_score", 0),
                        "content_id": stored.get("content_id"),
                    })
            return items_stored
        except Exception as exc:
            logger.error(f"[web_fallback] query='{query[:60]}' error: {exc}")
            return []

    results = await asyncio.gather(*[_one(q) for q in queries])
    for batch in results:
        found.extend(batch)
    return found


async def _generate_quick_angles(items: list[dict], topic: str = "") -> str:
    """
    Generate 1-2 sharp newsletter angles from fresh content titles.
    Uses MODELS["fast"] (Gemini Flash Lite) for cost efficiency.
    Output is plain text — no markdown or HTML.
    """
    try:
        import os as _os
        from config import MODELS, OPENROUTER_BASE_URL
        from openai import OpenAI as _OAI

        titles = "\n".join(
            f"- {it.get('title', '')[:150]} [{it.get('source_name', '')}]"
            for it in items[:10]
        )
        topic_line = f"Dom is interested in: {topic}\n" if topic else ""
        client = _OAI(base_url=OPENROUTER_BASE_URL, api_key=_os.getenv("OPENROUTER_API_KEY"))
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an editorial assistant for a VC secondaries newsletter. "
                        "Given newly ingested content titles, suggest 1-2 sharp newsletter "
                        "angles in plain text. Each angle: one working headline + one sentence "
                        "on the hook. No markdown, no HTML, no bullet symbols. Use plain text only. "
                        "Insider tone. Name deals, funds, people if present in the data."
                    ),
                },
                {
                    "role": "user",
                    "content": f"{topic_line}New content just ingested:\n{titles}\n\nSuggest 1-2 angles for Dom's newsletter:",
                },
            ],
            max_tokens=280,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"_generate_quick_angles error: {e}")
        return ""


async def check_all_sources(topic: str = "") -> dict:
    """
    Agentic 3-round research loop. Never returns empty-handed.

    Round 1 — Scrapers: Run all configured sources (YouTube, TikTok, Twitter, RSS).
      If new items found: generate angles, report, done.
      If 0: send interim message and proceed to Round 2.

    Round 2 — Web research (parallel): Run all _WEB_FALLBACK_QUERIES + topic query
      via Perplexity. Store each source URL as a separate content item.
      If items stored > 0: send rich summary with links + angles, done.
      If still 0: proceed to Round 3.

    Round 3 — Targeted research: Run 4 topic-specific or deep-market queries.
      If items stored > 0: send summary, done.
      Absolute last resort (all 3 rounds = 0): fetch 5 most recent DB items,
      summarise what's already known, tell Dom to check back later.

    All Telegram messages use plain text (no HTML tags).
    Idempotent: if a check is already running, returns status instead of starting
    a second one.
    """
    from datetime import datetime as _dt, timezone as _tz

    if _source_check_state["running"]:
        return {
            "already_running": True,
            "started_at": _source_check_state["started_at"],
            "note": "Already checking sources. I'll ping you when it's done.",
        }

    async def _run():
        try:
            from ingestion.rss import ingest_rss_feed
            from ingestion.tiktok import ingest_tiktok_profile
            from ingestion.twitter import ingest_twitter_account, ingest_twitter_searches
            from ingestion.youtube import ingest_youtube_channel
            from config import YOUTUBE_CHANNELS, TIKTOK_PROFILES, TWITTER_ACCOUNTS, RSS_FEEDS
            from telegram_bot.sender import send_to_client

            # ── Immediate start ping so Dom knows it's running ───────────────
            yt_names = ", ".join(ch["name"] for ch in YOUTUBE_CHANNELS)
            tt_handles = ", ".join(f"@{p}" for p in TIKTOK_PROFILES)
            tw_handles = ", ".join(f"@{a['handle']}" for a in TWITTER_ACCOUNTS)
            rss_names = ", ".join(f["name"] for f in RSS_FEEDS)
            source_count = len(YOUTUBE_CHANNELS) + len(TIKTOK_PROFILES) + len(TWITTER_ACCOUNTS) + len(RSS_FEEDS) + 1
            await send_to_client(
                f"Scanning {source_count} sources in parallel now...\n"
                f"YouTube: {yt_names}\n"
                f"TikTok: {tt_handles}\n"
                f"Twitter/X: {tw_handles} + company search sweep\n"
                f"RSS: {rss_names}\n\n"
                f"Back in ~2-3 min.",
                parse_mode="",
            )

            # ── ROUND 1: Scrapers — ALL IN PARALLEL ─────────────────────────
            # Each source fires its own Apify run simultaneously instead of
            # waiting in line. Cuts total scrape time from 8-15 min to ~2-3 min.
            async def _yt(channel):
                key = f"youtube_{channel['name']}"
                try:
                    return key, await ingest_youtube_channel(channel)
                except Exception as e:
                    logger.error(f"check_all_sources YouTube {key}: {e}")
                    return key, 0

            async def _tt(profile):
                key = f"tiktok_{profile}"
                try:
                    return key, await ingest_tiktok_profile(profile)
                except Exception as e:
                    logger.error(f"check_all_sources TikTok {key}: {e}")
                    return key, 0

            async def _tw(account):
                key = f"twitter_{account['handle']}"
                try:
                    return key, await ingest_twitter_account(account)
                except Exception as e:
                    logger.error(f"check_all_sources Twitter {key}: {e}")
                    return key, 0

            async def _x_search():
                key = "twitter_x_signal_search"
                try:
                    return key, await ingest_twitter_searches()
                except Exception as e:
                    logger.error("check_all_sources X signal search: %s", e)
                    return key, 0

            async def _rss(feed):
                key = f"rss_{feed['name']}"
                try:
                    return key, await ingest_rss_feed(feed)
                except Exception as e:
                    logger.error(f"check_all_sources RSS {key}: {e}")
                    return key, 0

            all_tasks = (
                [_yt(ch) for ch in YOUTUBE_CHANNELS]
                + [_tt(p) for p in TIKTOK_PROFILES]
                + [_tw(a) for a in TWITTER_ACCOUNTS]
                + [_x_search()]
                + [_rss(f) for f in RSS_FEEDS]
            )
            raw_results = await asyncio.gather(*all_tasks, return_exceptions=True)
            results: dict[str, int] = {}
            for item in raw_results:
                if isinstance(item, Exception):
                    logger.error(f"check_all_sources parallel task error: {item}")
                elif isinstance(item, tuple):
                    key, count = item
                    results[key] = count

            total_new = sum(results.values())
            logger.info(f"check_all_sources round 1 complete: {total_new} new items")

            # Build plain-text scraper summary block
            scraper_lines: list[str] = ["SOURCE CHECK COMPLETE", ""]

            yt = [(k[len("youtube_"):], v) for k, v in results.items() if k.startswith("youtube_")]
            if yt:
                scraper_lines.append("YOUTUBE")
                for name, count in yt:
                    scraper_lines.append(f"+ {name}: {count} new" if count else f"- {name}: nothing new")
                scraper_lines.append("")

            tt = [(k[len("tiktok_"):], v) for k, v in results.items() if k.startswith("tiktok_")]
            if tt:
                scraper_lines.append("TIKTOK")
                for handle, count in tt:
                    scraper_lines.append(f"+ @{handle}: {count} new" if count else f"- @{handle}: nothing new")
                scraper_lines.append("")

            tw = [(k[len("twitter_"):], v) for k, v in results.items() if k.startswith("twitter_")]
            if tw:
                scraper_lines.append("TWITTER / X")
                for handle, count in tw:
                    scraper_lines.append(f"+ @{handle}: {count} new" if count else f"- @{handle}: nothing new")
                scraper_lines.append("")

            rss = [(k[len("rss_"):], v) for k, v in results.items() if k.startswith("rss_")]
            if rss:
                scraper_lines.append("RSS FEEDS")
                for name, count in rss:
                    scraper_lines.append(f"+ {name}: {count} new" if count else f"- {name}: nothing new")
                scraper_lines.append("")

            scraper_lines.append(f"Total new: {total_new}")

            if total_new > 0:
                # Round 1 produced results — generate angles and report
                fresh = await get_recent_content_window(days_back=1, topic=topic, fresh_only=True)
                fresh_items = fresh.get("items", [])
                if fresh_items:
                    angle_text = await _generate_quick_angles(fresh_items, topic)
                    if angle_text:
                        scraper_lines += ["", "ANGLES FROM THIS BATCH", angle_text, ""]
                scraper_lines.append("Want me to build a full pitch from this, or add a topic directive to lock it into the next newsletter?")
                await send_to_client("\n".join(scraper_lines), parse_mode="")
                return

            # Round 1 = 0 — send interim and pivot to web research
            scraper_lines.append("")
            scraper_lines.append("Nothing new from direct sources. Pivoting to live web research...")
            await send_to_client("\n".join(scraper_lines), parse_mode="")

            # ── ROUND 2: Web research (parallel, all 8 fallback queries) ─────
            current_month_year = _dt.now(_tz.utc).strftime("%B %Y")
            round2_queries = list(_web_fallback_queries())
            if topic:
                round2_queries.insert(0, f"{topic} venture tech news {current_month_year}")

            logger.info(f"check_all_sources round 2: running {len(round2_queries)} web queries in parallel")
            web_found = await _run_web_fallback(round2_queries)

            if web_found:
                logged_str = _dt.now(_tz.utc).strftime("%d %b %Y %H:%M UTC")
                lines: list[str] = [f"WEB RESEARCH COMPLETE -- {len(web_found)} new item(s) indexed", ""]
                for item in web_found:
                    src = item.get("source_url") or "no link"
                    title = item.get("title") or "VC secondaries"
                    logged_raw = item.get("logged_at", "")
                    logged = (logged_raw[:19].replace("T", " ") + " UTC") if logged_raw else logged_str
                    lines.append(f"Topic: {title}")
                    lines.append(f"  Source: {src}")
                    lines.append(f"  Logged: {logged}")
                    if item.get("summary"):
                        lines.append(f"  Summary: {_clip(item.get('summary', ''), 360)}")
                    facts = item.get("key_facts") or []
                    if facts:
                        lines.append("  Key facts:")
                        for fact in facts[:3]:
                            lines.append(f"    * {_clip(str(fact), 180)}")
                    notes = item.get("interesting_notes") or []
                    if notes:
                        lines.append("  Interesting:")
                        for note in notes[:2]:
                            lines.append(f"    * {_clip(str(note), 180)}")
                    lines.append("")
                # Generate angles from freshly stored items
                fresh = await get_recent_content_window(days_back=1, topic=topic, fresh_only=True)
                fresh_items = fresh.get("items", [])
                if fresh_items:
                    angle_text = await _generate_quick_angles(fresh_items, topic)
                    if angle_text:
                        lines += ["ANGLES FROM THIS SWEEP", angle_text, ""]
                lines.append("Want me to build a full pitch, or lock one of these into the next newsletter?")
                await send_to_client("\n".join(lines), parse_mode="")
                return

            # ── ROUND 3: Targeted / deeper research ──────────────────────────
            logger.info("check_all_sources round 3: running targeted deep queries")
            _now = _dt.now(_tz.utc)
            _current_year = _now.year
            _current_quarter = f"Q{(_now.month - 1) // 3 + 1} {_current_year}"
            if topic:
                round3_queries = [
                    f"{topic} Anthropic OpenAI SpaceX news {current_month_year}",
                    f"{topic} top venture company fundraise {_current_year}",
                    f"{topic} pre-IPO secondary market prominent startup",
                    f"{topic} Musk Altman tech news {current_month_year}",
                ]
            else:
                round3_queries = [
                    f"Anthropic OpenAI fundraise valuation {current_month_year}",
                    f"SpaceX Anduril secondary market news {current_month_year}",
                    f"Musk Altman lawsuit update {_current_year}",
                    f"top venture company cap table activity {_current_quarter}",
                ]

            deep_found = await _run_web_fallback(round3_queries)

            if deep_found:
                logged_str = _dt.now(_tz.utc).strftime("%d %b %Y %H:%M UTC")
                lines = [f"DEEP RESEARCH COMPLETE -- {len(deep_found)} new item(s) indexed", ""]
                for item in deep_found:
                    src = item.get("source_url") or "no link"
                    title = item.get("title") or "VC secondaries"
                    logged_raw = item.get("logged_at", "")
                    logged = (logged_raw[:19].replace("T", " ") + " UTC") if logged_raw else logged_str
                    lines.append(f"Topic: {title}")
                    lines.append(f"  Source: {src}")
                    lines.append(f"  Logged: {logged}")
                    if item.get("summary"):
                        lines.append(f"  Summary: {_clip(item.get('summary', ''), 360)}")
                    facts = item.get("key_facts") or []
                    if facts:
                        lines.append("  Key facts:")
                        for fact in facts[:3]:
                            lines.append(f"    * {_clip(str(fact), 180)}")
                    notes = item.get("interesting_notes") or []
                    if notes:
                        lines.append("  Interesting:")
                        for note in notes[:2]:
                            lines.append(f"    * {_clip(str(note), 180)}")
                    lines.append("")
                fresh = await get_recent_content_window(days_back=1, topic=topic, fresh_only=True)
                fresh_items = fresh.get("items", [])
                if fresh_items:
                    angle_text = await _generate_quick_angles(fresh_items, topic)
                    if angle_text:
                        lines += ["ANGLES FROM THIS SWEEP", angle_text, ""]
                lines.append("Want me to build a full pitch, or lock one of these into the next newsletter?")
                await send_to_client("\n".join(lines), parse_mode="")
                return

            # ── ABSOLUTE LAST RESORT ─────────────────────────────────────────
            # All 3 rounds produced 0 new items. Fetch recent DB content and
            # give Dom a summary of what's already in the knowledge base.
            logger.info("check_all_sources: all 3 rounds empty — falling back to DB summary")
            try:
                recent = await get_recent_content_window(days_back=7, topic=topic, fresh_only=False)
                recent_items = recent.get("items", [])[:5]
                summary_lines: list[str] = [
                    "No fresh items landed in the last 48 hours.",
                    "",
                    "Here is background material already in the knowledge base:",
                    "",
                ]
                for it in recent_items:
                    title = it.get("title") or "(untitled)"
                    src = it.get("source_name") or ""
                    age = it.get("age_days")
                    age_str = f" ({age}d ago)" if age is not None else ""
                    summary_lines.append(f"- {title[:160]} [{src}]{age_str}")
                summary_lines += [
                    "",
                    "Try checking back in a few hours, or ask me to research a specific topic.",
                ]
                await send_to_client("\n".join(summary_lines), parse_mode="")
            except Exception as last_e:
                logger.error(f"check_all_sources last-resort summary error: {last_e}")
                await send_to_client(
                    "All sources are up to date and no new web results found. Try again in a few hours.",
                    parse_mode="",
                )

        except Exception as exc:
            logger.error(f"check_all_sources background error: {exc}", exc_info=True)
            try:
                from telegram_bot.sender import send_to_client
                await send_to_client(f"Source check failed: {str(exc)[:200]}", parse_mode="")
            except Exception:
                pass
        finally:
            _source_check_state["running"] = False
            _source_check_state["started_at"] = None
            _source_check_state["task"] = None

    _source_check_state["running"] = True
    _source_check_state["started_at"] = datetime.now(timezone.utc).isoformat()
    task = asyncio.create_task(_run())
    _source_check_state["task"] = task

    return {
        "started": True,
        "topic_filter": topic or None,
        "note": (
            "Checking all your sources now — YouTube, TikTok, Twitter, RSS. "
            "If scrapers come up empty I'll automatically sweep the web. "
            "I'll ping you on Telegram when done, usually 2-5 minutes."
        ),
    }


# ── Pipeline-in-flight tracking ────────────────────────────────────────────
# In-process flag so we never fire two newsletter pipelines at the same time.
# If Dom asks for a piece, then 30 seconds later asks again ("did it start?"),
# the second call must NOT kick off a parallel run — it should report status.
_pipeline_state: dict = {
    "running": False,
    "started_at": None,
    "trigger_reason": "",
    "task": None,         # asyncio.Task handle so cancel_pipeline can kill it
    "issue_id": None,     # newsletter_issues row created by the pipeline
    "last_issue_id": None,
    "issue_future": None,
}

# One-slot queue: if a pipeline is already running when Dom requests another,
# we store the queued request here and auto-fire it when the current one finishes.
_queued_pipeline: dict = {
    "queued": False,
    "trigger_reason": "",
}


def _pipeline_eta_remaining(started_at_iso: str | None) -> str:
    """Best-effort remaining time string for an in-flight pipeline."""
    if not started_at_iso:
        return "unknown"
    try:
        started = datetime.fromisoformat(started_at_iso)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        # Pipeline typically takes 3-6 minutes
        remaining_min = max(0, int((360 - elapsed) // 60))
        return f"about {remaining_min} more minute(s)"
    except Exception:
        return "unknown"


def register_pipeline_issue_id(issue_id: str) -> None:
    """Called by the orchestrator after creating its tracking row so cancel
    can mark the right issue as cancelled."""
    if _pipeline_state.get("running"):
        _pipeline_state["issue_id"] = issue_id
        _pipeline_state["last_issue_id"] = issue_id
        issue_future = _pipeline_state.get("issue_future")
        if issue_future and not issue_future.done():
            issue_future.set_result(issue_id)


async def cancel_pipeline(reason: str = "") -> dict:
    """Cancel the currently-running newsletter pipeline. Returns immediately.

    Use when Dom changes his mind ('cancel that', 'stop the pipeline, run
    this other one instead'). After cancellation the in-flight asyncio task
    is killed; the issue row is marked as a cancelled draft so it doesn't
    contaminate dedup or future runs.
    """
    import asyncio as _asyncio
    if not _pipeline_state.get("running"):
        return {"cancelled": False, "reason": "no pipeline currently running"}
    task = _pipeline_state.get("task")
    issue_id = _pipeline_state.get("issue_id")
    started = _pipeline_state.get("started_at")
    trigger = _pipeline_state.get("trigger_reason")
    if task and not task.done():
        task.cancel()
    return {
        "cancelled": True,
        "was_running_since": started,
        "trigger_reason_of_cancelled": trigger,
        "cancelled_issue_id": issue_id,
        "reason": reason[:200],
        "note": "Pipeline killed. You can fire a new one (or draft an approved pitch) immediately.",
    }


async def get_pipeline_status() -> dict:
    """Return whether the newsletter pipeline is currently running, and how long left."""
    if not _pipeline_state["running"]:
        return {
            "running": False,
            "issue_id": _pipeline_state.get("last_issue_id"),
        }
    return {
        "running": True,
        "issue_id": _pipeline_state.get("issue_id"),
        "started_at": _pipeline_state["started_at"],
        "trigger_reason": _pipeline_state["trigger_reason"],
        "eta_remaining": _pipeline_eta_remaining(_pipeline_state["started_at"]),
        "note": "When the pipeline finishes, Dom gets the HTML preview plus approve / edit / discard buttons in Telegram automatically.",
    }


async def draft_full_weekly_newsletter(
    trigger_reason: str = "",
    issue_number: int | None = None,
    return_issue_handle: bool = False,
) -> dict:
    """
    Kick off the full Mon-Thu newsletter pipeline as a background task.

    Used when Dom asks for "a piece on last week" / "a recap of this week" /
    "draft me the newsletter now" — i.e. he wants a full multi-section
    issue with the same HTML + approval buttons the Friday cron produces.

    Idempotent: if a pipeline is already running, returns its current status
    instead of firing a second one. The orchestrator's _deliver_to_dom step
    Telegram-blasts the HTML + buttons when complete, so Dom doesn't need to
    poll — he can keep chatting and the draft will arrive on its own.

    Returns:
      {"started": True, ...}                — newly kicked off
      {"started": False, "already_running": True, ...}  — duplicate request
    """
    import asyncio as _asyncio
    issue_future = None

    if _pipeline_state["running"]:
        logger.info(
            "draft_full_weekly_newsletter — pipeline already running, queuing request "
            f"since {_pipeline_state['started_at']}"
        )
        _queued_pipeline["queued"] = True
        _queued_pipeline["trigger_reason"] = trigger_reason[:200]
        return {
            "started": False,
            "already_running": True,
            "queued": True,
            "started_at": _pipeline_state["started_at"],
            "eta_remaining": _pipeline_eta_remaining(_pipeline_state["started_at"]),
            "original_trigger_reason": _pipeline_state["trigger_reason"],
            "note": (
                "One already in the pipeline — yours is queued and will run automatically "
                "as soon as this one finishes. Dom will get both drafts in sequence."
            ),
        }

    async def _run():
        try:
            from agents.orchestrator import run_newsletter_generation
            await run_newsletter_generation(issue_number_override=issue_number)
        except _asyncio.CancelledError:
            logger.warning("draft_full_weekly_newsletter pipeline cancelled by request")
            # Mark the in-flight issue as abandoned if we have its id
            issue_id = _pipeline_state.get("issue_id")
            if issue_id:
                try:
                    from db.queries import update_newsletter_issue
                    update_newsletter_issue(issue_id, {
                        "status": "draft",
                        "subject_line": "[CANCELLED — Dom cancelled mid-pipeline]",
                        "preview_text": "Cancelled before completion.",
                        "plain_text": "[CANCELLED]",
                    })
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.error(f"draft_full_weekly_newsletter pipeline error: {exc}", exc_info=True)
            try:
                from telegram_bot.sender import send_to_client
                await send_to_client("Newsletter generation hit an error — check the logs.")
            except Exception:
                pass
        finally:
            if issue_future and not issue_future.done():
                issue_future.set_result(None)
            _pipeline_state["running"] = False
            _pipeline_state["started_at"] = None
            _pipeline_state["trigger_reason"] = ""
            _pipeline_state["task"] = None
            _pipeline_state["issue_id"] = None
            logger.info("draft_full_weekly_newsletter pipeline flag cleared")
            # Auto-fire any queued pipeline request
            if _queued_pipeline["queued"]:
                queued_reason = _queued_pipeline["trigger_reason"]
                _queued_pipeline["queued"] = False
                _queued_pipeline["trigger_reason"] = ""
                logger.info(f"Auto-firing queued pipeline (trigger={queued_reason!r})")
                import asyncio as _asyncio2
                _asyncio2.create_task(draft_full_weekly_newsletter(queued_reason))

    try:
        _pipeline_state["running"] = True
        _pipeline_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _pipeline_state["trigger_reason"] = trigger_reason[:200]
        _pipeline_state["last_issue_id"] = None
        issue_future = _asyncio.get_running_loop().create_future()
        _pipeline_state["issue_future"] = issue_future
        task = _asyncio.create_task(_run())
        _pipeline_state["task"] = task
        logger.info(
            f"draft_full_weekly_newsletter kicked off in background "
            f"(trigger_reason={trigger_reason[:120]!r})"
        )
        response = {
            "started": True,
            "eta_minutes": "3-6",
            "trigger_reason": trigger_reason[:200],
            "delivery": "Telegram — HTML attachment plus Approve / Request Edits / Discard buttons",
            "note": "Dom can keep chatting normally while this runs. The draft arrives on its own when complete.",
        }
        if return_issue_handle:
            response["_issue_future"] = issue_future
            response["_task"] = task
        return response
    except Exception as exc:
        # Reset flag if dispatch failed
        _pipeline_state["running"] = False
        _pipeline_state["started_at"] = None
        _pipeline_state["trigger_reason"] = ""
        if issue_future and not issue_future.done():
            issue_future.set_result(None)
        logger.error(f"draft_full_weekly_newsletter dispatch error: {exc}")
        return {"started": False, "error": str(exc)[:300]}


async def get_db_status() -> dict:
    """Return formatted database statistics."""
    try:
        stats = get_db_stats()
        return stats
    except Exception as e:
        logger.error(f"get_db_status error: {e}")
        return {"error": str(e)}


async def add_youtube_video(url: str) -> dict:
    """Process a single YouTube video and store it."""
    try:
        result = await ingest_single_youtube_video(url)
        return result
    except Exception as e:
        logger.error(f"add_youtube_video error for {url}: {e}")
        return {"stored": False, "title": "", "chunks": 0, "reason": str(e)}


async def get_newsletter_analytics() -> dict:
    """Fetch Beehiiv newsletter performance data for the agent."""
    try:
        from newsletter.beehiiv import get_publication_overview
        from newsletter.performance import format_performance_for_telegram
        overview = await get_publication_overview()
        return {
            "success": overview.get("success", False),
            "summary": format_performance_for_telegram(overview),
            "raw": overview,
        }
    except Exception as e:
        logger.error(f"get_newsletter_analytics error: {e}")
        return {"success": False, "summary": "Analytics hit an error — check the logs.", "raw": {}}


async def search_youtube_channel_for_topic(
    channel_name: str,
    topic: str,
    max_episodes: int = 3,
) -> dict:
    """
    Search recent episodes of a known YouTube channel for content about a specific topic.

    Flow:
    1. Find the channel in YOUTUBE_CHANNELS config by name or handle keyword match
    2. Get the N most recent video URLs from that channel via apidojo/youtube-scraper
    3. For each video, fetch transcript via pintostudio/youtube-transcript-scraper
    4. Search the transcript for topic keywords
    5. If found: extract the relevant segment, store it in the DB, return result
    6. If not found after max_episodes: return a clarification prompt

    Returns:
        {
            "found": bool,
            "episode_title": str,
            "episode_url": str,
            "segment": str,       # the excerpt mentioning the topic (~500 chars)
            "content_id": str | None,
            "stored": bool,
            "clarification_needed": bool,
            "clarification_question": str,  # only if not found
            "episodes_checked": int,
        }
    """
    from config import YOUTUBE_CHANNELS, APIFY_ACTORS
    from ingestion.apify_runner import run_actor
    from processing.dedup import generate_content_hash, is_duplicate
    from processing.chunker import chunk_text
    from processing.embedder import embed_and_store_chunks
    from processing.tagger import generate_tags
    from db.queries import insert_content_item

    # --- Find the channel ---
    channel = None
    name_lower = channel_name.lower()
    for ch in YOUTUBE_CHANNELS:
        ch_name = ch.get("name", "").lower()
        ch_handle = ch.get("handle", "").lower().lstrip("@")
        if name_lower in ch_name or name_lower in ch_handle or ch_name in name_lower:
            channel = ch
            break

    if not channel:
        return {
            "found": False,
            "clarification_needed": True,
            "clarification_question": (
                f"I don't have '{channel_name}' in my configured channels. "
                f"Known channels: {', '.join(ch['name'] for ch in YOUTUBE_CHANNELS)}. "
                "Drop the YouTube channel URL and I'll pull from it directly."
            ),
            "episodes_checked": 0,
            "stored": False,
            "segment": "",
            "episode_title": "",
            "episode_url": "",
            "content_id": None,
        }

    logger.info(f"[yt_topic_search] Searching '{channel['name']}' for topic: {topic!r}")

    # --- Get recent video list ---
    try:
        video_items = await run_actor(
            APIFY_ACTORS["youtube_channel"],
            input_data={
                "startUrls": [channel["url"]],
                "youtubeHandles": [channel.get("handle", "").lstrip("@")] if channel.get("handle") else [],
                "maxItems": max(max_episodes * 2, 10),
                "sort": "u",
            },
            timeout_secs=300,
        )
    except Exception as exc:
        logger.error(f"[yt_topic_search] Channel scrape failed: {exc}")
        return {
            "found": False,
            "clarification_needed": True,
            "clarification_question": (
                f"Couldn't reach the {channel['name']} YouTube channel right now. "
                "Do you have a direct link to the episode? Drop it and I'll pull it."
            ),
            "episodes_checked": 0,
            "stored": False,
            "segment": "",
            "episode_title": "",
            "episode_url": "",
            "content_id": None,
        }

    if not video_items:
        return {
            "found": False,
            "clarification_needed": True,
            "clarification_question": (
                f"No recent videos found on {channel['name']}. "
                "Do you have the episode URL? Drop it and I'll pull it directly."
            ),
            "episodes_checked": 0,
            "stored": False,
            "segment": "",
            "episode_title": "",
            "episode_url": "",
            "content_id": None,
        }

    # Normalise topic into keyword list for matching
    topic_keywords = [kw.strip().lower() for kw in re.split(r"[\s,]+", topic) if len(kw.strip()) > 3]

    episodes_checked = 0
    for item in video_items[:max_episodes]:
        video_url = item.get("url") or item.get("videoUrl") or ""
        if not video_url or "youtube.com/watch" not in video_url:
            continue

        episode_title = item.get("title") or video_url
        logger.info(f"[yt_topic_search] Checking episode: {episode_title!r}")

        # Get transcript
        transcript = ""
        try:
            result = await run_actor(
                APIFY_ACTORS["youtube_transcript_v2"],
                input_data={"videoUrl": video_url},
                timeout_secs=180,
            )
            if result:
                first = result[0] if isinstance(result, list) else result
                if isinstance(first, dict):
                    segments = first.get("data") or first.get("searchResult") or []
                    if isinstance(segments, list):
                        transcript = " ".join(
                            seg.get("text", "") for seg in segments
                            if isinstance(seg, dict) and seg.get("text")
                        )
                    if not transcript:
                        transcript = first.get("transcript") or first.get("text") or ""
        except Exception as exc:
            logger.warning(f"[yt_topic_search] Transcript fetch failed for {video_url}: {exc}")

        # Fallback to scrape-creators actor
        if not transcript or len(transcript) < 100:
            try:
                result2 = await run_actor(
                    "scrape-creators/best-youtube-transcripts-scraper",
                    input_data={"startUrls": [video_url]},
                    timeout_secs=180,
                )
                if result2:
                    first2 = result2[0] if isinstance(result2, list) else result2
                    if isinstance(first2, dict):
                        raw = first2.get("transcript") or first2.get("captions") or ""
                        if isinstance(raw, list):
                            transcript = " ".join(
                                seg.get("text", "") if isinstance(seg, dict) else str(seg)
                                for seg in raw if seg
                            )
                        elif isinstance(raw, str):
                            transcript = raw
            except Exception as exc:
                logger.warning(f"[yt_topic_search] Fallback transcript failed for {video_url}: {exc}")

        episodes_checked += 1

        if not transcript or len(transcript) < 100:
            logger.info(f"[yt_topic_search] No transcript for {episode_title!r}, skipping")
            continue

        # Search transcript for topic keywords
        transcript_lower = transcript.lower()
        matches = [kw for kw in topic_keywords if kw in transcript_lower]

        if not matches:
            logger.info(f"[yt_topic_search] Topic not found in {episode_title!r}")
            continue

        # Extract the relevant segment (window around first keyword match)
        first_kw = matches[0]
        idx = transcript_lower.find(first_kw)
        start = max(0, idx - 300)
        end = min(len(transcript), idx + 700)
        segment = transcript[start:end].strip()

        logger.info(f"[yt_topic_search] Found '{first_kw}' in {episode_title!r} — storing")

        # Store in DB
        content_id = None
        stored = False
        raw_text = f"{episode_title}\n\n{transcript}"
        if not is_duplicate(video_url, raw_text):
            try:
                tags = await generate_tags(raw_text)
                content_hash = generate_content_hash(raw_text)
                record = {
                    "source_type": "youtube",
                    "source_name": channel["name"],
                    "source_url": video_url,
                    "title": episode_title[:500],
                    "raw_text": raw_text,
                    "published_at": (
                        item.get("date") or item.get("publishedAt") or
                        item.get("published_at") or None
                    ),
                    "language": "en",
                    "is_voice_sample": False,
                    "is_deal_signal": tags.get("is_deal_signal", False),
                    "topics": tags.get("topics", []),
                    "metadata": {
                        "content_hash": content_hash,
                        "ingested_via": "topic_search",
                        "topic_query": topic,
                        "dom_requested": True,
                        "newsletter_include": True,
                    },
                }
                content_id = insert_content_item(record)
                chunks = chunk_text(raw_text)
                await embed_and_store_chunks(content_id, chunks)
                stored = True
                logger.info(f"[yt_topic_search] Stored episode (id={content_id})")
            except Exception as exc:
                logger.error(f"[yt_topic_search] Store failed: {exc}")

        return {
            "found": True,
            "episode_title": episode_title,
            "episode_url": video_url,
            "segment": segment,
            "content_id": content_id,
            "stored": stored,
            "clarification_needed": False,
            "clarification_question": "",
            "episodes_checked": episodes_checked,
            "keywords_matched": matches,
            "channel_name": channel["name"],
        }

    # Not found after all episodes checked
    return {
        "found": False,
        "clarification_needed": True,
        "clarification_question": (
            f"I checked the last {episodes_checked} episodes of {channel['name']} "
            f"and didn't find content about '{topic}'. "
            "A few things that would help: "
            "1) Was this episode posted in the last 2 weeks? "
            "2) Do you have the specific episode URL? "
            "3) Who specifically mentioned it — and roughly what date?"
        ),
        "episodes_checked": episodes_checked,
        "stored": False,
        "segment": "",
        "episode_title": "",
        "episode_url": "",
        "content_id": None,
    }


async def send_approved_draft_to_pipeline(
    draft_text: str,
    issue_number: int,
    subject_line: str = "",
    preview_text: str = "",
) -> dict:
    """
    Submit a pre-approved conversation draft directly to the newsletter pipeline.

    Call this when Dom has finished collaboratively drafting an issue via Telegram
    conversation and has approved the final content. This skips ALL automated
    generation (no web research, no Hermes LLM, no voice scoring) and sends
    the exact approved text to Beehiiv + Dom for final Approve/Decline.

    Args:
        draft_text:   Full approved plain-text content. Use "\\n---\\n" lines to
                      separate distinct story sections.
        issue_number: The issue number Dom confirmed (e.g. 4).
        subject_line: Email subject line.
        preview_text: Email preview text.

    Returns:
        {"success": bool, "beehiiv_post_id": str, "note": str}
    """
    try:
        from agents.orchestrator import run_newsletter_from_conversation_draft
        post_id = await run_newsletter_from_conversation_draft(
            draft_text=draft_text,
            issue_number=issue_number,
            subject_line=subject_line,
            preview_text=preview_text,
        )
        if post_id:
            return {
                "success": True,
                "beehiiv_post_id": post_id,
                "note": (
                    f"Issue #{issue_number} built from conversation draft and pushed to Beehiiv. "
                    "Sent to Dom for final approve/decline."
                ),
            }
        else:
            return {
                "success": False,
                "beehiiv_post_id": "",
                "note": "Pipeline completed but Beehiiv push returned no post ID — check logs.",
            }
    except Exception as exc:
        logger.error("send_approved_draft_to_pipeline error: %s", exc, exc_info=True)
        return {
            "success": False,
            "beehiiv_post_id": "",
            "note": f"Pipeline error: {str(exc)[:200]}",
        }


async def search_transcript_by_topic(query: str, days_back: int = 30) -> dict:
    """
    Search stored transcripts in the knowledge base for a specific topic,
    quote, or segment. First tries the DB with vector embeddings; if nothing
    useful is found, attempts to scrape the relevant YouTube channel fresh.

    Args:
        query:     The topic, quote fragment, or person to search for.
        days_back: How far back to look in the DB (default 30 days).

    Returns:
        {
            "found": bool,
            "results": [{"source_name", "episode_title", "source_url",
                          "segment", "published_at"}],
            "searched_channels": list[str],
            "note": str,
        }
    """
    from db.client import get_client as _get_db_client
    from processing.embedder import embed_texts

    found_results: list[dict] = []

    # Step 1: Vector search in DB
    try:
        embeddings = await embed_texts([query])
        query_embedding = embeddings[0]
        db_results = semantic_search(query_embedding, days_back=days_back, limit=20)
        # Filter to only transcript-type sources (YouTube, podcast)
        transcript_sources = {"youtube", "tiktok", "podcast"}
        for r in db_results:
            if r.get("source_type", "").lower() in transcript_sources:
                chunk = (r.get("chunk_text") or "")
                if len(chunk.strip()) > 50:
                    found_results.append({
                        "source_name": r.get("source_name", ""),
                        "episode_title": r.get("title", ""),
                        "source_url": r.get("source_url", ""),
                        "segment": chunk[:800],
                        "published_at": (r.get("published_at") or "")[:10],
                        "similarity": r.get("similarity", 0),
                    })
        # Sort by similarity descending
        found_results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
        logger.info("search_transcript_by_topic: DB returned %d transcript results for %r", len(found_results), query[:60])
    except Exception as exc:
        logger.warning("search_transcript_by_topic: DB search failed: %s", exc)

    if found_results:
        return {
            "found": True,
            "results": found_results[:5],
            "searched_channels": [],
            "note": f"Found {len(found_results)} relevant transcript segments in the knowledge base.",
        }

    # Step 2: No DB results — try scraping the most relevant configured channels
    logger.info("search_transcript_by_topic: nothing in DB for %r — attempting live channel search", query[:60])
    try:
        from config import YOUTUBE_CHANNELS
        # Pick channels most likely to contain the query based on name/handle keyword overlap
        query_lower = query.lower()
        channel_keywords = {
            "all-in": ["all-in", "allin", "sacks", "chamath", "friedberg", "jason"],
            "tbpn": ["tbpn", "tvpn"],
            "elenanisonoff": ["elena", "nisonoff"],
        }
        candidate_channels = []
        for ch in YOUTUBE_CHANNELS:
            ch_name = ch.get("name", "").lower()
            for _key, _kws in channel_keywords.items():
                if any(kw in ch_name for kw in _kws):
                    candidate_channels.append(ch["name"])
                    break
        if not candidate_channels:
            # Fall back to all channels
            candidate_channels = [ch["name"] for ch in YOUTUBE_CHANNELS]

        searched: list[str] = []
        for ch_name in candidate_channels[:2]:
            try:
                result = await search_youtube_channel_for_topic(ch_name, query, max_episodes=3)
                searched.append(ch_name)
                if result.get("found"):
                    return {
                        "found": True,
                        "results": [{
                            "source_name": result.get("channel_name", ch_name),
                            "episode_title": result.get("episode_title", ""),
                            "source_url": result.get("episode_url", ""),
                            "segment": result.get("segment", ""),
                            "published_at": "",
                            "similarity": 1.0,
                        }],
                        "searched_channels": searched,
                        "note": (
                            f"Found in {result.get('channel_name', ch_name)} — "
                            f"episode: {result.get('episode_title', '')}. "
                            f"Stored in knowledge base for future searches."
                        ),
                    }
            except Exception as ch_exc:
                logger.warning("search_transcript_by_topic: channel %s search failed: %s", ch_name, ch_exc)

        return {
            "found": False,
            "results": [],
            "searched_channels": searched,
            "note": (
                f"Searched {len(searched)} channel(s) — {query[:80]!r} not found. "
                "If you have the episode URL or date, drop it and I'll pull the transcript directly."
            ),
        }
    except Exception as exc:
        logger.error("search_transcript_by_topic fallback error: %s", exc)
        return {
            "found": False,
            "results": [],
            "searched_channels": [],
            "note": f"Transcript search failed: {str(exc)[:200]}. Try dropping the episode URL directly.",
        }


async def resend_draft_preview() -> dict:
    """
    Resend the latest newsletter draft preview to Dom via Telegram.
    Use when Dom asks to 'see the draft', 'show me the draft', 'present the draft',
    'what does the draft look like', etc. — i.e. he wants to view an existing draft,
    NOT start a new pipeline.
    """
    try:
        from db.queries import get_latest_newsletter_issue
        from telegram import Bot
        from telegram_bot.newsletter_flow import send_newsletter_draft_preview

        issue = get_latest_newsletter_issue()
        if not issue:
            return {
                "success": False,
                "note": "No newsletter draft found yet. The next one generates Friday 8pm ET.",
            }
        if issue.get("status") == "published":
            return {
                "success": False,
                "note": f"Issue #{issue.get('issue_number')} has already been published.",
            }

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
        if not token or not chat_id:
            return {"success": False, "note": "Telegram credentials not configured."}

        bot = Bot(token=token)
        await send_newsletter_draft_preview(
            bot=bot,
            chat_id=chat_id,
            issue_number=issue.get("issue_number", "?"),
            subject_line=issue.get("subject_line", ""),
            preview_text=issue.get("preview_text", ""),
            plain_text=issue.get("plain_text", ""),
            html_content=issue.get("html_content", ""),
            visual_count=3,
            beehiiv_post_id=issue.get("beehiiv_post_id", ""),
            beehiiv_url=issue.get("beehiiv_url", ""),
            sources=issue.get("sources", []),
            research_topics=issue.get("research_topics", []),
            review_summary=issue.get("review_summary", ""),
        )
        return {
            "success": True,
            "issue_number": issue.get("issue_number"),
            "subject_line": issue.get("subject_line"),
            "note": "Draft preview resent to Dom.",
        }
    except Exception as e:
        logger.error(f"resend_draft_preview error: {e}", exc_info=True)
        return {"success": False, "note": "Couldn't resend the draft — check the logs."}
