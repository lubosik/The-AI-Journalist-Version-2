import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from config import (
    SCHEDULE_HOUR_ET,
    TIKTOK_PROFILES,
    YOUTUBE_CHANNELS,
    MODELS,
    OPENROUTER_BASE_URL,
)

load_dotenv()

logger = logging.getLogger(__name__)

def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _get_et_now() -> datetime:
    """Return current datetime in ET, falling back to UTC if zoneinfo is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


async def _fetch_fresh_samples(cutoff_hours: int = 2) -> dict:
    """
    Query content_items for entries ingested within the last cutoff_hours.
    Returns {source_name: [{"title": str, "url": str}, ...]} - max 5 per source.
    Excludes satire/style-only items (topics contains 'satire').
    """
    try:
        from db.client import get_client
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)).isoformat()
        client = get_client()
        result = (
            client.table("content_items")
            .select("title, source_name, source_type, source_url, raw_text, topics, published_at")
            .gte("scraped_at", cutoff)
            .in_("source_type", ["tiktok", "youtube"])
            .order("scraped_at", desc=True)
            .limit(60)
            .execute()
        )
        samples: dict = {}
        for row in (result.data or []):
            # Skip satire/style-only items - they're not intelligence
            topics = row.get("topics") or []
            if "satire" in topics or "comedy_style" in topics:
                continue
            published_at_str = row.get("published_at")
            scraped_at_str = row.get("scraped_at", "")
            now_utc = datetime.now(timezone.utc)
            cutoff_48h = now_utc - timedelta(hours=48)
            if published_at_str:
                try:
                    pub_dt = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
                    if pub_dt < cutoff_48h:
                        continue
                except Exception:
                    pass
            else:
                # No published_at — fall back to scraped_at as proxy.
                # If we can't determine when the video was published, only include it
                # if it was scraped within the last 24 hours (i.e. from today's run).
                if scraped_at_str:
                    try:
                        scraped_dt = datetime.fromisoformat(scraped_at_str.replace("Z", "+00:00"))
                        if scraped_dt < now_utc - timedelta(hours=24):
                            continue
                    except Exception:
                        continue
                else:
                    continue
            name = row.get("source_name") or "unknown"
            title = (row.get("title") or "").strip()
            raw_text = (row.get("raw_text") or "").strip()
            if not title:
                title = raw_text[:80] + ("..." if len(raw_text) > 80 else "")
            url = (row.get("source_url") or "").strip()
            # Pass up to 3000 chars of transcript so the LLM has real content to work with
            transcript_excerpt = raw_text[len(title):].strip()[:3000] if raw_text else ""
            if title:
                samples.setdefault(name, [])
                if len(samples[name]) < 5:
                    samples[name].append({"title": title, "url": url, "transcript": transcript_excerpt})
        return samples
    except Exception as e:
        logger.error(f"[daily] _fetch_fresh_samples error: {e}")
        return {}


async def _get_todays_market_pulse(samples: dict) -> str:
    """
    One punchy sentence summarising today's most interesting top-tier venture/tech finding.
    Incorporates actual sample titles so the signal is specific.
    """
    from openai import OpenAI
    from db.queries import get_recent_content_items

    try:
        items = [i for i in get_recent_content_items(days=1, limit=20) if i.get("source_type") in ("tiktok", "youtube")][:10]
        # Build a combined text: sample titles first (most specific), then raw_text snippets
        title_lines = []
        for source_name, items_data in samples.items():
            for item in items_data:
                title = item["title"] if isinstance(item, dict) else item
                title_lines.append(title)
        titles_blob = " | ".join(title_lines)[:500]

        raw_blob = ""
        if items:
            raw_blob = " ".join([item.get("raw_text", "")[:200] for item in items])

        combined = (titles_blob + " " + raw_blob).strip()
        if not combined:
            return ""

        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.getenv("OPENROUTER_API_KEY"))
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write one punchy sentence about today's most interesting top-tier venture or tech news. "
                        "Focus on: elenanisonoff (TikTok), TBPN, All-In Podcast, viral X tweets, and breaking news about Anthropic, OpenAI, or SpaceX. These are Dom's primary intelligence sources. "
                        "No AI slop. Direct insider tone. No em dashes. Be specific - name a company, deal, "
                        "number, or person if the content supports it."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"What is the most interesting finding in today's content? "
                        f"One sentence only:\n{combined[:3000]}"
                    ),
                },
            ],
            max_tokens=100,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"[daily] get_todays_market_pulse error: {e}")
        return ""


def _build_morning_brief(
    results: dict,
    research_items: int,
    research_queries_total: int,
    samples: dict,
    pulse: str,
    research_results: list[dict] | None = None,
) -> str:
    """
    Build the morning intelligence brief - only shows sources that found something.
    For every item found: shows the headline/content AND the source URL.
    Zero-result sources are silently omitted.
    """
    et_now = _get_et_now()
    day_str = et_now.strftime("%A, %B %-d")

    total_scraped = sum(results.values())
    total_all = total_scraped + research_items

    lines = [f"HERALD Morning Brief -- {day_str}", ""]

    if total_all == 0:
        lines.append("Quiet day. No new intelligence items found across scrapers or web research.")
        return "\n".join(lines)

    lines.append(f"{total_all} new items indexed.")
    lines.append("")

    # --- YOUTUBE (only sources with new content) ---
    # Merge sample items across all keys that match this channel (handles source_name variants like "TBPN Podcast" vs "TBPN")
    def _get_channel_samples(ch_name: str) -> list:
        items: list = []
        seen_urls: set = set()
        for key, val in samples.items():
            if ch_name.lower() in key.lower() or key.lower() in ch_name.lower():
                for item in val:
                    url = item.get("url", "")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        items.append(item)
        return items[:5]

    youtube_found = []
    for ch in YOUTUBE_CHANNELS:
        key = f"youtube_{ch['name']}"
        count = results.get(key, 0)
        items_data = _get_channel_samples(ch["name"])
        if count > 0 or items_data:
            youtube_found.append((ch["name"], max(count, len(items_data)), items_data))
    if youtube_found:
        lines.append("━━━ YOUTUBE ━━━")
        for name, count, items_data in youtube_found:
            lines.append(f"{name} - {count} new")
            for item in items_data:
                lines.append(f"  * {item['title']}")
                if item.get("url"):
                    lines.append(f"    {item['url']}")
                if item.get("transcript"):
                    lines.append(f"    [TRANSCRIPT]: {item['transcript']}")
        lines.append("")

    # --- TIKTOK (only sources with new content) ---
    def _get_tiktok_samples(profile: str) -> list:
        items: list = []
        seen_urls: set = set()
        for key, val in samples.items():
            if profile.lower() in key.lower() or key.lstrip("@").lower() == profile.lower():
                for item in val:
                    url = item.get("url", "")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        items.append(item)
        return items[:5]

    tiktok_found = []
    for profile in TIKTOK_PROFILES:
        key = f"tiktok_{profile}"
        count = results.get(key, 0)
        items_data = _get_tiktok_samples(profile)
        if count > 0 or items_data:
            tiktok_found.append((profile, max(count, len(items_data)), items_data))
    if tiktok_found:
        lines.append("━━━ TIKTOK ━━━")
        for profile, count, items_data in tiktok_found:
            lines.append(f"@{profile} - {count} new")
            for item in items_data:
                lines.append(f"  * {item['title']}")
                if item.get("url"):
                    lines.append(f"    {item['url']}")
                if item.get("transcript"):
                    lines.append(f"    [TRANSCRIPT]: {item['transcript']}")
        lines.append("")

    # --- TODAY'S SIGNAL ---
    if pulse:
        lines.append(f"Signal: {pulse}")

    return "\n".join(lines)


_BRIEF_REFUSAL_MARKERS = (
    "i cannot produce",
    "i will not",
    "i won't",
    "cannot produce a credible",
    "contaminated with",
    "speculative fiction",
    "i'd rather deliver nothing",
    "i would rather deliver nothing",
    "what's the actual source material",
    "what is the actual source material",
    "this is a test of my filtering",
)


def _looks_like_refusal(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _BRIEF_REFUSAL_MARKERS)


async def _consolidate_morning_brief(raw_brief: str, today_date_str: str) -> str:
    """
    Run a consolidation pass on the raw morning brief using the fast LLM.
    Strips background facts, old context, and anything not genuinely new in the last 48 hours.
    Falls back to raw_brief unchanged on any error or if the LLM refuses to produce a brief.
    """
    try:
        from openai import OpenAI
        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.getenv("OPENROUTER_API_KEY"))
        system_prompt = (
            "You are an editor formatting a morning intelligence brief for a VC secondaries advisor.\n"
            "The raw brief below contains ONLY content from three approved sources: "
            "@elenanisonoff (TikTok), TBPN (YouTube), and All-In Podcast (YouTube).\n\n"
            "ABSOLUTE RULE — NEVER VIOLATE:\n"
            "You MUST NOT add ANY facts, claims, statistics, company names, events, or information "
            "that are not explicitly stated in the raw brief below. "
            "Do NOT use your training data. Do NOT fill gaps. Do NOT add context from memory. "
            "If the raw brief has limited content, the output should also have limited content. "
            "A short, accurate brief is infinitely better than a long, invented one.\n\n"
            "OUTPUT FORMAT — STRICT:\n"
            "Your output must contain ONLY these sections, in this order, and nothing else:\n"
            "  1. Title line: 'HERALD Morning Brief. [Day, Month D, YYYY]'\n"
            "  2. One ━━━ SOURCE: [name] ━━━ block per source that had content.\n"
            "     Under each block: one headline, then 2-3 bullet points FROM THE TRANSCRIPT ONLY.\n"
            "     If a source had no content, write: 'No summarizable content available from today's indexed item.'\n"
            "  3. A blank line then 'SIGNAL OF THE DAY:' — one sentence from the actual content found.\n"
            "  4. NOTHING AFTER SIGNAL OF THE DAY. The message ends there.\n\n"
            "FORBIDDEN PATTERNS — these must NEVER appear in your output:\n"
            "  - Any section titled 'VC SECONDARIES MORNING BRIEF' or similar\n"
            "  - Any '━━━ IMPLICATION ━━━' blocks\n"
            "  - Any bullet points with revenue figures, valuations, or cap table details not explicitly in the transcript\n"
            "  - Any content about Anthropic, OpenAI, SpaceX, xAI, Anduril, or any company unless that company "
            "    is explicitly named in the raw brief transcript\n"
            "  - Any secondary or supplementary intelligence section after SIGNAL OF THE DAY\n\n"
            "OTHER RULES:\n"
            "Keep the total under 600 words.\n"
            "FRESHNESS RULE — CRITICAL: Only include items that represent a genuinely NEW development "
            "within the last 48 hours. If a video covers an ongoing saga but has no new development, "
            "skip that item entirely. Do not surface old news just because a podcast mentioned it today."
        )
        user_message = f"Today is {today_date_str}. Here is the raw morning brief:\n\n{raw_brief}"
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            max_tokens=2000,
        )
        consolidated = (response.choices[0].message.content or "").strip()
        if consolidated and not _looks_like_refusal(consolidated):
            logger.info("[morning_brief] Consolidation pass complete (%d chars -> %d chars)", len(raw_brief), len(consolidated))
            return consolidated
        if _looks_like_refusal(consolidated):
            logger.warning("[morning_brief] Consolidation produced a refusal message - using raw brief instead")
        else:
            logger.warning("[morning_brief] Consolidation returned empty - using raw brief")
    except Exception as e:
        logger.warning("[morning_brief] Consolidation pass failed: %s - using raw brief", e)
    return raw_brief


async def run_daily_ingestion() -> dict:
    """
    Run ingestion for approved sources: elenanisonoff TikTok, TBPN, All-In Podcast.
    Automated web search sweep has been removed — research is only triggered ad-hoc by Dom.
    Returns dict of {source_key: items_stored}.
    Sends Telegram message only if at least 1 new item found.
    """
    from ingestion.tiktok import ingest_tiktok_profile
    from ingestion.youtube import ingest_youtube_channel
    from db.queries import is_pipeline_paused

    if is_pipeline_paused():
        logger.info("Daily ingestion skipped -- pipeline is paused")
        return {}

    logger.info("Starting daily ingestion run (Elena TikTok, TBPN, All-In only)")
    results: dict = {}

    # TikTok
    for profile in TIKTOK_PROFILES:
        key = f"tiktok_{profile}"
        try:
            count = await ingest_tiktok_profile(profile)
            results[key] = count
            logger.info(f"Daily ingestion {key}: {count} items")
        except Exception as e:
            logger.error(f"Daily ingestion error for {key}: {e}")
            results[key] = 0

    # YouTube channels
    et_now = _get_et_now()
    is_friday = et_now.weekday() == 4  # 0=Monday, 4=Friday
    for channel in YOUTUBE_CHANNELS:
        if channel.get("friday_only") and not is_friday:
            logger.info(f"Skipping {channel['name']} (friday_only, today is {et_now.strftime('%A')})")
            continue
        key = f"youtube_{channel['name']}"
        try:
            count = await ingest_youtube_channel(channel, max_videos=15)
            results[key] = count
            logger.info(f"Daily ingestion {key}: {count} items")
        except Exception as e:
            logger.error(f"Daily ingestion error for {key}: {e}")
            results[key] = 0

    research_results = []
    research_items = 0
    research_queries_total = 0

    total = sum(results.values())
    logger.info(f"Daily ingestion complete: {total} total new items from approved sources")

    # Stay silent when approved sources return no new content.
    if total == 0:
        try:
            from db.client import get_client
            client_db = get_client()
            client_db.table("morning_brief_log").insert({
                "items_ingested": 0,
                "breakdown": {**results, "daily_research": []},
                "summary_text": "",
            }).execute()
        except Exception as e:
            logger.warning(f"Failed to write morning_brief_log: {e}")
        return results

    # Fetch items ingested in the last 2 hours (tiktok/youtube only)
    samples = await _fetch_fresh_samples(cutoff_hours=2)

    # ── Proactive analysis: run HERALD's perspective engine on new content ────
    # For each newly ingested item with a transcript, evaluate whether it
    # contains an insight worth surfacing to Dom or auto-adding to edition topics.
    await _run_proactive_analysis(samples)

    # Market pulse sentence
    pulse = await _get_todays_market_pulse(samples)

    # Single intelligence brief — only shows what was found, with content + source URLs
    brief = _build_morning_brief(results, research_items, research_queries_total, samples, pulse, research_results)
    brief = await _consolidate_morning_brief(brief, _get_et_now().strftime("%Y-%m-%d"))

    await _send_telegram_message(brief)
    await _write_log(brief)

    try:
        from db.client import get_client
        client_db = get_client()
        client_db.table("morning_brief_log").insert({
            "items_ingested": total,
            "breakdown": {**results, "daily_research": research_results},
            "summary_text": brief,
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to write morning_brief_log: {e}")

    return results


async def _run_proactive_analysis(samples: dict) -> None:
    """
    Run HERALD's proactive perspective engine on newly ingested content.
    For each item with a real transcript, call analyse_and_suggest.
    Items that score >= 7 confidence get surfaced to Dom via Telegram.
    Items that score >= 9 with auto_add=true are added to edition topics.
    """
    from intelligence.proactive_agent import analyse_and_suggest
    from db.client import get_client

    if not samples:
        return

    # Fetch IDs of the items we just ingested so we can pass content_item_id
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        db = get_client()
        result = (
            db.table("content_items")
            .select("id, source_name, raw_text")
            .gte("scraped_at", cutoff)
            .in_("source_type", ["tiktok", "youtube"])
            .order("scraped_at", desc=True)
            .limit(20)
            .execute()
        )
        fresh_items = result.data or []
    except Exception as e:
        logger.warning("[proactive] Could not fetch fresh items for analysis: %s", e)
        return

    surfaced = 0
    for item in fresh_items:
        raw_text = (item.get("raw_text") or "").strip()
        if len(raw_text) < 200:
            continue  # skip items with no real transcript
        try:
            outcome = await analyse_and_suggest(
                content_item_id=str(item.get("id", "")),
                source_name=item.get("source_name", "unknown"),
                raw_text=raw_text,
            )
            if outcome.get("sent"):
                surfaced += 1
                logger.info(
                    "[proactive] Surfaced insight to Dom from %s (confidence=%s, auto_add=%s)",
                    item.get("source_name"), outcome.get("confidence"), outcome.get("auto_added"),
                )
            else:
                logger.info(
                    "[proactive] Skipped %s (confidence=%s, worth_surfacing=False)",
                    item.get("source_name"), outcome.get("confidence"),
                )
        except Exception as e:
            logger.warning("[proactive] analyse_and_suggest failed for item %s: %s", item.get("id"), e)

    logger.info("[proactive] Analysis complete: %d/%d items surfaced to Dom", surfaced, len(fresh_items))


async def _send_telegram_message(text: str) -> None:
    """Send a single message to the Telegram chat (plain text, no parse_mode)."""
    try:
        from filters.response_filter import send_telegram_message_safe
        await send_telegram_message_safe(text)
        logger.info("Telegram message sent successfully")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


async def _write_log(summary: str) -> None:
    """Write the daily summary to a log file."""
    try:
        os.makedirs("logs", exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = f"logs/daily_{date_str}.log"
        timestamp = datetime.now(timezone.utc).isoformat()

        with open(log_path, "a") as f:
            f.write(f"\n[{timestamp}]\n{summary}\n")
        logger.info(f"Daily log written to {log_path}")
    except Exception as e:
        logger.error(f"Failed to write daily log: {e}")


# APScheduler setup - persistent SQLite job store prevents double-fire on PM2 restart
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

_SCHEDULER_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scheduler.db")
_SCHEDULER_DB_URL = f"sqlite:///{os.path.abspath(_SCHEDULER_DB_PATH)}"

jobstores = {
    'default': SQLAlchemyJobStore(url=_SCHEDULER_DB_URL)
}
scheduler = AsyncIOScheduler(jobstores=jobstores)
scheduler.add_job(
    run_daily_ingestion,
    CronTrigger(hour=SCHEDULE_HOUR_ET, minute=0, timezone="America/New_York"),
    id="daily_ingestion",
    name="HERALD Daily Ingestion",
    misfire_grace_time=300,
    replace_existing=True,
)


async def _run_cleanup_job():
    from db.cleanup import run_cleanup
    summary = await run_cleanup()
    logger.info("Daily cleanup complete: %s", summary)


scheduler.add_job(
    _run_cleanup_job,
    CronTrigger(hour=3, minute=0, timezone="UTC"),
    id="herald_daily_cleanup",
    name="HERALD Daily Cleanup",
    replace_existing=True,
    misfire_grace_time=300,
)


async def check_pending_research():
    """Check if a research request was triggered from the dashboard."""
    import json
    from db.client import get_client
    from intelligence.tools import web_research, store_research
    from telegram_bot.sender import send_to_client

    try:
        db = get_client()
        result = db.table("pipeline_state").select("value").eq("key", "pending_research").execute()
        if not result.data:
            return

        raw = result.data[0].get("value", "{}")
        try:
            pending = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return

        topic = pending.get("topic", "")
        if not topic:
            return

        # Clear immediately to prevent double-processing
        db.table("pipeline_state").upsert({"key": "pending_research", "value": "{}"}, on_conflict="key").execute()

        deep = pending.get("deep", False)
        logger.info(f"[pending_research] Processing research for: {topic} (deep={deep})")

        research = await web_research(topic, deep=deep)
        findings = research.get("findings", "")

        if findings and len(findings) > 100:
            await store_research(findings, "", topic, dom_requested=True)
            preview = findings[:400] + ("..." if len(findings) > 400 else "")
            await send_to_client(
                f"Research complete (triggered from dashboard):\n\n"
                f"Topic: {topic}\n\n"
                f"{preview}",
                parse_mode="",
            )

    except Exception as e:
        logger.error(f"[pending_research] Error: {e}", exc_info=True)


async def check_html_rebuild_requests():
    """Check if the dashboard triggered an HTML rebuild for a newsletter edition."""
    import json
    from db.client import get_client
    from newsletter.builder import build_newsletter_html
    from datetime import date

    try:
        db = get_client()
        result = db.table("pipeline_state").select("value").eq("key", "rebuild_html_request").execute()
        if not result.data:
            return

        raw = result.data[0].get("value", "")
        if not raw:
            return

        # Value stored as JSON: {"edition_id": "<uuid>"}
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            edition_id = payload.get("edition_id", "") if isinstance(payload, dict) else str(payload)
        except (json.JSONDecodeError, ValueError):
            edition_id = str(raw).strip().strip('"')

        if not edition_id:
            return

        # Clear immediately to prevent double-processing
        db.table("pipeline_state").upsert({"key": "rebuild_html_request", "value": ""}, on_conflict="key").execute()

        row = db.table("newsletter_issues").select("*").eq("id", edition_id).single().execute()
        if not row.data:
            logger.warning(f"[html_rebuild] Edition {edition_id} not found")
            return

        issue = row.data
        sections = issue.get("sections") or []
        if isinstance(sections, str):
            try:
                sections = json.loads(sections)
            except Exception:
                sections = []

        visuals = issue.get("visuals") or []
        if isinstance(visuals, str):
            try:
                visuals = json.loads(visuals)
            except Exception:
                visuals = []

        issue_number = issue.get("issue_number", 1)
        subject_line = issue.get("subject_line", "")
        week_start_raw = issue.get("week_start")
        week_start = date.fromisoformat(week_start_raw) if week_start_raw else None

        logger.info(f"[html_rebuild] Rebuilding HTML for edition {edition_id} (issue #{issue_number})")

        html = await build_newsletter_html(
            sections=sections,
            visuals=visuals,
            issue_number=issue_number,
            subject_line=subject_line,
            week_start=week_start,
        )

        from newsletter.builder import build_plain_text
        plain_text = build_plain_text(sections)

        db.table("newsletter_issues").update({
            "html_content": html,
            "plain_text": plain_text,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", edition_id).execute()
        logger.info(f"[html_rebuild] HTML + plain text rebuilt for edition {edition_id}")

        # Notify Dom on Telegram that the rebuild is done
        try:
            from telegram_bot.sender import send_to_client
            await send_to_client(
                f"Draft rebuilt for Edition {issue_number}. Check the dashboard to review the updated HTML.",
                parse_mode="",
            )
        except Exception as tg_err:
            logger.warning(f"[html_rebuild] Telegram notify failed: {tg_err}")

    except Exception as e:
        logger.error(f"[html_rebuild] Error: {e}", exc_info=True)


from apscheduler.triggers.interval import IntervalTrigger

scheduler.add_job(
    check_pending_research,
    IntervalTrigger(seconds=30),
    id="check_pending_research",
    name="HERALD Pending Research Checker",
    replace_existing=True,
    misfire_grace_time=10,
)

scheduler.add_job(
    check_html_rebuild_requests,
    IntervalTrigger(seconds=30),
    id="check_html_rebuild",
    name="HERALD HTML Rebuild Checker",
    replace_existing=True,
    misfire_grace_time=10,
)
