"""
intelligence/pitch_engine.py

HERALD's "junior journalist pitching to senior editor" engine.

Combines:
  1. The last 7 days of ingested content (DB) — what we've actually learned.
  2. DataForSEO keyword research — what audiences are actively searching for.
  3. Dom's recent pitch verdicts — what he liked / rejected, so we bias toward
     his taste over time (the learning loop).
  4. The trained style bible — so the headlines and angles already feel like
     HERALD's voice.

Produces 3-5 ranked story pitches as structured dicts. Each pitch is also
persisted into content_items with source_type='herald_pitch' so:
  - Dom's verdict can be recorded later via record_pitch_feedback
  - Approved pitches enter the knowledge base for future newsletter generation
  - Past pitches inform future pitch generation (the learning loop)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# content_items.source_type has a check constraint limiting it to known
# producer types (tiktok, youtube, rss, twitter, podcast, linkedin,
# telegram_tip). We slot pitches in as 'telegram_tip' (the same bucket used
# for any Dom-curated content) and identify them via metadata.origin =
# 'pitch_engine'. Querying that combination gives us a clean pitch view.
PITCH_SOURCE_TYPE = "telegram_tip"
PITCH_ORIGIN_TAG = "pitch_engine"


def _client() -> OpenAI:
    from config import OPENROUTER_BASE_URL
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


# ── Persistence helpers ───────────────────────────────────────────────────


def _store_pitch(pitch: dict) -> str | None:
    """
    Persist a pitch into content_items with source_type='herald_pitch'.
    Returns the new content_items.id (also acts as the pitch id).
    """
    try:
        from db.client import get_client
        from db.queries import insert_content_item
        record = {
            "source_type": PITCH_SOURCE_TYPE,
            "source_name": "herald_pitch_engine",
            # Use a uuid so concurrent pitches in the same batch don't collide
            "source_url": f"herald://pitch/{uuid.uuid4()}",
            "title": pitch.get("headline", "")[:500],
            "raw_text": pitch.get("summary", ""),
            "language": "en",
            "is_voice_sample": False,
            "is_deal_signal": bool(pitch.get("is_deal_signal")),
            "topics": pitch.get("topic_tags", []) or [],
            "metadata": {
                "pitch_status": "pitched",
                "angle": pitch.get("angle", ""),
                "source_links": pitch.get("source_links", []),
                "keyword_signal": pitch.get("keyword_signal", {}),
                "rank": pitch.get("rank"),
                "reasoning": pitch.get("reasoning", ""),
                "origin": PITCH_ORIGIN_TAG,
            },
        }
        return insert_content_item(record)
    except Exception as e:
        logger.error(f"_store_pitch error: {e}")
        return None


def get_recent_pitches(days: int = 30, limit: int = 60) -> list[dict]:
    """Recent pitches, newest first. Used for the learning loop."""
    try:
        from db.client import get_client
        client = get_client()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        # We filter by source_type=telegram_tip + metadata.origin=pitch_engine.
        # PostgREST supports JSON path filters on jsonb columns, but the
        # supabase-py wrapper varies — fetch by source_type then filter
        # client-side, which is fast at our row counts.
        result = (
            client.table("content_items")
            .select("id, title, raw_text, topics, metadata, scraped_at")
            .eq("source_type", PITCH_SOURCE_TYPE)
            .gte("scraped_at", cutoff)
            .order("scraped_at", desc=True)
            .limit(limit * 4)  # over-fetch to allow for non-pitch tips
            .execute()
        )
        rows = result.data or []
        out: list[dict] = []
        for row in rows:
            meta = row.get("metadata") or {}
            if meta.get("origin") == PITCH_ORIGIN_TAG:
                out.append(row)
                if len(out) >= limit:
                    break
        return out
    except Exception as e:
        logger.error(f"get_recent_pitches error: {e}")
        return []


def update_pitch_status(
    pitch_id: str,
    status: str,
    reaction: str = "",
    drafted_issue_id: str | None = None,
) -> bool:
    """Update a pitch's status and Dom's reaction. Returns True on success."""
    try:
        from db.client import get_client
        client = get_client()
        # Pull the current row so we can merge metadata.
        cur = client.table("content_items").select("metadata").eq("id", pitch_id).execute()
        if not cur.data:
            logger.warning(f"update_pitch_status: pitch {pitch_id} not found")
            return False
        meta = cur.data[0].get("metadata") or {}
        meta["pitch_status"] = status
        if reaction:
            meta["dom_reaction"] = reaction[:600]
        if drafted_issue_id:
            meta["drafted_issue_id"] = drafted_issue_id
        meta["status_updated_at"] = datetime.now(timezone.utc).isoformat()
        client.table("content_items").update({"metadata": meta}).eq("id", pitch_id).execute()
        return True
    except Exception as e:
        logger.error(f"update_pitch_status error: {e}")
        return False


async def get_performance_signal(limit: int = 8) -> dict:
    """
    Pull the last N published issues' open/click rates from Beehiiv and
    classify each by performance vs the rolling average. Used by pitch
    generation to bias toward winning patterns.

    Returns:
      {
        "winners": [{"subject_line": ..., "open_rate": ..., "click_rate": ...}],
        "losers":  [{...}],
        "baseline": {"avg_open_rate": float, "avg_click_rate": float},
        "samples":  int,
      }
    "winners" = top quartile on open_rate. "losers" = bottom quartile.
    Empty arrays if no data or Beehiiv not configured.
    """
    try:
        from newsletter.beehiiv import get_recent_posts_performance
        posts = await get_recent_posts_performance(limit=limit)
    except Exception as e:
        logger.warning(f"[pitch_engine] performance fetch failed: {e}")
        return {"winners": [], "losers": [], "baseline": {}, "samples": 0}

    if not posts:
        return {"winners": [], "losers": [], "baseline": {}, "samples": 0}

    avg_open = sum(p.get("open_rate", 0) for p in posts) / max(1, len(posts))
    avg_click = sum(p.get("click_rate", 0) for p in posts) / max(1, len(posts))

    sorted_by_open = sorted(posts, key=lambda p: p.get("open_rate", 0), reverse=True)
    cutoff = max(1, len(posts) // 4)
    winners = [
        {
            "subject_line": p.get("subject_line", ""),
            "title": p.get("title", ""),
            "open_rate": round(p.get("open_rate", 0) * 100, 1),
            "click_rate": round(p.get("click_rate", 0) * 100, 1),
            "publish_date": p.get("publish_date", ""),
        }
        for p in sorted_by_open[:cutoff]
    ]
    losers = [
        {
            "subject_line": p.get("subject_line", ""),
            "title": p.get("title", ""),
            "open_rate": round(p.get("open_rate", 0) * 100, 1),
            "click_rate": round(p.get("click_rate", 0) * 100, 1),
            "publish_date": p.get("publish_date", ""),
        }
        for p in sorted_by_open[-cutoff:]
    ]
    return {
        "winners": winners,
        "losers": losers,
        "baseline": {
            "avg_open_rate": round(avg_open * 100, 1),
            "avg_click_rate": round(avg_click * 100, 1),
        },
        "samples": len(posts),
    }


def get_dom_taste_signal(limit: int = 20) -> dict:
    """
    Return a compact summary of what Dom has liked vs. rejected lately.
    Used to bias pitch generation toward his demonstrated preferences.
    """
    pitches = get_recent_pitches(days=60, limit=limit * 3)
    approved: list[dict] = []
    rejected: list[dict] = []
    for p in pitches:
        meta = p.get("metadata") or {}
        status = meta.get("pitch_status", "pitched")
        compact = {
            "headline": p.get("title", ""),
            "angle": meta.get("angle", ""),
            "reaction": meta.get("dom_reaction", ""),
            "topics": p.get("topics") or [],
        }
        if status in ("approved", "drafted"):
            approved.append(compact)
        elif status == "rejected":
            rejected.append(compact)
        if len(approved) >= limit and len(rejected) >= limit:
            break
    return {
        "approved_examples": approved[:limit],
        "rejected_examples": rejected[:limit],
        "total_pitched": len(pitches),
    }


async def _ingest_thin_db_fallback(seeds: list[str], days_back: int) -> dict:
    """
    When the DB has thin coverage for the requested window, fire 2-3 web
    research calls on the core beat, store the findings, and return what
    was added so the caller can re-fetch.

    Returns:
      {"queries_run": int, "items_stored": int, "queries": [str, ...]}
    """
    from intelligence.tools import web_research, store_research

    # Build date-aware queries so Perplexity returns CURRENT-period results,
    # not 2024-era articles. Pin the actual current month/year and ISO range.
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=days_back)).date()
    window_end = now.date()
    month_year = now.strftime("%B %Y")
    range_str = f"{window_start.isoformat()} to {window_end.isoformat()}"

    base_queries = [
        f"Anthropic OpenAI SpaceX insider story anecdote behavior {month_year} ({range_str})",
        f"top venture company specific incident named person secondary trade {month_year}",
        f"Musk Altman lawsuit pre-IPO shares unusual transaction story {month_year} ({range_str})",
    ]
    extra: list[str] = []
    for seed in (seeds or [])[:2]:
        extra.append(
            f"{seed} insider story anecdote investor behavior {month_year} ({range_str})"
        )

    queries = list(dict.fromkeys(base_queries + extra))[:5]
    queries_run = 0
    items_stored = 0

    for q in queries:
        try:
            res = await web_research(q, deep=False)
            findings = res.get("findings", "") or ""
            if len(findings) < 200:
                continue
            queries_run += 1
            stored = await store_research(
                content=findings,
                source_url=res.get("sources", [None])[0] or f"herald://research/{int(asyncio.get_event_loop().time())}",
                topic=q[:150],
                source_name="pitch_thin_db_fallback",
            )
            if stored.get("stored"):
                items_stored += 1
        except Exception as e:
            logger.warning(f"[pitch_engine] thin-db fallback query failed: {e}")
            continue

    return {
        "queries_run": queries_run,
        "items_stored": items_stored,
        "queries": queries,
    }


# ── Pitch generation ──────────────────────────────────────────────────────


PITCH_SYSTEM = """You are HERALD acting as a sharp junior journalist pitching story ideas to your senior editor, Dom Pandolfo. Dom runs a weekly newsletter covering the top tier of the venture and tech ecosystem.

CONTENT FOCUS — MANDATORY. Only pitch stories from this universe:
- Prominent venture-backed companies: Anthropic, OpenAI, SpaceX, Anduril, xAI, Stripe, Databricks, and direct peers at this scale.
- Specific, human, behavioural stories that reveal how investors are actually acting around these assets. The gold standard: someone listed their home and accepted Anthropic shares as payment. That one story tells you more than a valuation table. Hunt for these.
- Breaking news, rumors, specific incidents, and insider reports about these companies and their founders.
- The Musk vs Altman lawsuit and other high-profile legal/regulatory actions involving named top-tier tech companies.
- Insider commentary from leading VCs and operators — specific quotes, named takes, not paraphrased consensus.
DO NOT pitch: generic private equity, mid-market deals, LBO financing, broad macro, aggregate market data without a specific human story attached, or anything not involving a named top-tier venture-backed company.

Your job: read the past 7 days of ingested content + the keyword research data + Dom's prior pitch verdicts, and propose 3-5 story angles he should consider for this week's newsletter. Each pitch is a real journalist's pitch — not a topic, an ANGLE.

Each pitch object MUST have these fields:
{
  "headline": "Punchy 6-12 word working headline. No clickbait, no \\"You won't believe\\". Insider tone.",
  "angle": "1-2 sentences on the actual journalistic hook — why does this story matter NOW, and what's the unique read only HERALD would have on it?",
  "summary": "2-4 sentences. The lede + the so-what. Specific numbers where the source data has them.",
  "source_links": [{"title": "...", "url": "...", "source_type": "rss|twitter|youtube|tiktok|website|web_research"}],
  "topic_tags": ["3-6 short tags", "secondaries", "etc"],
  "keyword_signal": {"seed": "primary keyword", "search_volume": int_or_null, "related_top": ["k1","k2"]},
  "is_deal_signal": true_if_about_a_specific_named_deal_else_false,
  "reasoning": "Why this beats the other angles. 1-2 sentences. Reference Dom's recent likes/dislikes if relevant."
}

Rules:
- The user prompt includes today's date and a per-item FRESHNESS label (e.g. "2 days old (pub 2026-04-26)"). USE these. Items < 7 days old are CURRENT and should anchor "this week" pitches. Items 7-30 days old are still relevant background but should NOT be pitched as "new" or "breaking". Items > 30 days old are reference material only — never the headline angle. If a pitch leans on a single old data point, your `reasoning` MUST acknowledge the recency gap and why it still matters.
- Rank pitches strongest first. The strongest pitch should be the one with (a) the freshest data, (b) the clearest insider angle, (c) keyword signal that suggests audience interest, (d) alignment with what Dom has approved in the past, AND (e) pattern-match to past WINNERS in the open-rate / click-rate analytics.
- If Dom has REJECTED similar angles recently, EXPLICITLY avoid them — note this in `reasoning`.
- If Dom has APPROVED similar angles, lean into the same lane — note this too.
- Past WINNERS section shows newsletter issues with above-average opens / clicks. Bias toward the same subject-line PATTERNS (specificity, named entities, contrarian framings) — but DO NOT just rewrite the same angle. A "variation on a winner" is encouraged; a "duplicate of a winner" is not.
- PREVIOUSLY-PITCHED list contains angles HERALD has already pitched in the last 30 days. You MUST NOT pitch the same headline verbatim. You MAY pitch a fresh ANGLE on the same underlying topic (different lens, different data point, different stakeholder, different stage of the story) — explicitly note in `reasoning` how it's distinct from the prior pitch.
- Do NOT pitch generic "AI is changing everything" stories. Pitch specific deals, named funds, named LPs, concrete data points.
- If the database is thin, say so — pitch fewer items (even just 2) rather than padding.
- Source links MUST come from the actual content provided — do not hallucinate URLs.

Return ONLY a JSON object: {"pitches": [...]}. No markdown fences. No prose around the JSON."""


def _seed_keywords_from_content(items: list[dict], max_seeds: int = 8) -> list[str]:
    """Pull plausible keyword seeds from the recent content window."""
    seeds: list[str] = []
    seen: set[str] = set()
    # Take topics first
    for item in items[:40]:
        for t in item.get("topics") or []:
            tl = (t or "").lower().strip()
            if tl and tl not in seen and len(tl) >= 3:
                seen.add(tl)
                seeds.append(t)
                if len(seeds) >= max_seeds:
                    return seeds
    # Fall back to bigrams from titles if topics are sparse
    for item in items[:25]:
        title = (item.get("title") or "").strip().lower()
        words = re.findall(r"[a-z][a-z\-]+", title)
        for i in range(len(words) - 1):
            phrase = f"{words[i]} {words[i+1]}"
            if phrase not in seen and len(phrase) > 6:
                seen.add(phrase)
                seeds.append(phrase)
                if len(seeds) >= max_seeds:
                    return seeds
    return seeds


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(text))
        except Exception:
            return {}


async def generate_pitches(
    days_back: int = 7,
    desired_count: int = 4,
    user_focus: str = "",
) -> dict:
    """
    Build 3-5 ranked pitches. Stores each one in content_items so they can be
    referenced later by Dom's verdict.

    Args:
      days_back: window of recent content to draw from.
      desired_count: how many pitches to aim for (3-5 typical).
      user_focus: optional Dom-supplied steer (e.g. "focus on continuation
                  vehicles" / "skip the AI angles this week").

    Returns:
      {
        "count": int,
        "pitches": [<pitch dict with id>, ...],
        "window": "last 7 days",
        "content_items_considered": int,
        "keyword_data_points": int,
      }
    """
    from intelligence.tools import get_recent_content_window
    from intelligence.dataforseo import get_search_volume, get_related_keywords
    from training.style_analyser import get_style_bible_for_prompt

    # Step 1: pull recent content window
    window = await get_recent_content_window(days_back=days_back, fresh_only=False)
    items = window.get("items", [])
    logger.info(f"[pitch_engine] {len(items)} items in {days_back}-day window")

    # If the DB is thin (< 5 items), fire web research to top up before
    # we even try to pitch. This is the "let me go and learn" path —
    # HERALD never returns 'I don't have anything', it goes ingests fresh.
    auto_ingest_summary: dict | None = None
    THIN_THRESHOLD = 5
    if len(items) < THIN_THRESHOLD:
        logger.info(
            f"[pitch_engine] DB thin ({len(items)} items < {THIN_THRESHOLD}); "
            "running web-research fallback before pitching"
        )
        # Pull seeds from whatever IS in the DB so research is steered
        thin_seeds = _seed_keywords_from_content(items, max_seeds=4)
        if user_focus.strip():
            thin_seeds = [user_focus.strip()] + thin_seeds
        auto_ingest_summary = await _ingest_thin_db_fallback(thin_seeds, days_back)
        logger.info(
            f"[pitch_engine] thin-db fallback: {auto_ingest_summary['items_stored']} "
            f"new items stored from {auto_ingest_summary['queries_run']} research queries"
        )
        # Re-fetch the window now that fresh research has landed
        if auto_ingest_summary["items_stored"] > 0:
            window = await get_recent_content_window(days_back=days_back, fresh_only=False)
            items = window.get("items", [])
            logger.info(f"[pitch_engine] post-ingest: {len(items)} items now in window")

    # Step 2: derive keyword seeds and call DataForSEO in parallel with style fetch
    seeds = _seed_keywords_from_content(items, max_seeds=8)
    if user_focus.strip():
        seeds = [user_focus.strip()] + seeds
    seeds = list(dict.fromkeys(seeds))[:8]
    logger.info(f"[pitch_engine] seeds: {seeds}")

    sv_task = get_search_volume(seeds) if seeds else asyncio.sleep(0, [])
    rel_task = get_related_keywords(seeds[0], limit=20) if seeds else asyncio.sleep(0, [])
    style_task = get_style_bible_for_prompt()
    perf_task = get_performance_signal(limit=8)
    # Dedupe corpus: previously-pitched angles in the last 30 days
    recent_pitches_task = asyncio.to_thread(get_recent_pitches, 30, 30)

    sv_data, rel_data, style_text, perf_signal, prev_pitches = await asyncio.gather(
        sv_task, rel_task, style_task, perf_task, recent_pitches_task,
        return_exceptions=True,
    )
    if isinstance(sv_data, Exception):
        logger.warning(f"sv error: {sv_data}")
        sv_data = []
    if isinstance(rel_data, Exception):
        logger.warning(f"rel error: {rel_data}")
        rel_data = []
    if isinstance(style_text, Exception):
        style_text = ""
    if isinstance(perf_signal, Exception):
        logger.warning(f"perf error: {perf_signal}")
        perf_signal = {"winners": [], "losers": [], "baseline": {}, "samples": 0}
    if isinstance(prev_pitches, Exception):
        logger.warning(f"prev_pitches error: {prev_pitches}")
        prev_pitches = []

    # Step 3: pull Dom's taste signal
    taste = await asyncio.to_thread(get_dom_taste_signal)

    # Step 4: build the pitch prompt
    # Per-item freshness: include age_days + published_at so the LLM ranks
    # current events higher than stale material and never pitches a 4-month-
    # old story as "new this week".
    def _age_label(it: dict) -> str:
        age = it.get("age_days")
        pub = it.get("published_at", "") or ""
        if age is None:
            return f"undated (published_at={pub or '?'})"
        if age == 0:
            return f"today (pub {pub[:10]})"
        if age == 1:
            return f"1 day old (pub {pub[:10]})"
        return f"{age} days old (pub {pub[:10]})"

    content_brief = "\n\n".join([
        f"[{i+1}] [{it.get('source_type','?')} | {it.get('source_name','?')}] "
        f"FRESHNESS: {_age_label(it)}\n"
        f"{it.get('title','')}\nURL: {it.get('source_url','')}\n"
        f"{(it.get('summary') or '')[:400]}"
        for i, it in enumerate(items[:25])
    ]) or "(database has no items in this window)"

    sv_brief = "\n".join([
        f"  {r['keyword']!r}: vol={r['search_volume']}, comp={r.get('competition','?')}, cpc=${r.get('cpc',0):.2f}"
        for r in (sv_data or [])[:10]
    ]) or "  (no keyword volume data)"

    rel_brief = "\n".join([
        f"  {r['keyword']!r}: vol={r['search_volume']}"
        for r in (rel_data or [])[:15]
    ]) or "  (no related keywords)"

    approved_lines = "\n".join([
        f"  ✓ {p['headline']!r} — {p.get('reaction', 'liked')[:120]}"
        for p in (taste.get("approved_examples") or [])[:6]
    ]) or "  (no prior approvals on file)"
    rejected_lines = "\n".join([
        f"  ✗ {p['headline']!r} — {p.get('reaction', 'rejected')[:120]}"
        for p in (taste.get("rejected_examples") or [])[:6]
    ]) or "  (no prior rejections on file)"

    # Performance signal — past WINNERS / LOSERS by open + click rate
    winners_lines = "\n".join([
        f"  WIN  {w.get('subject_line', '')!r}  open={w.get('open_rate')}% click={w.get('click_rate')}%"
        for w in (perf_signal.get("winners") or [])[:5]
    ]) or "  (no winners on file)"
    losers_lines = "\n".join([
        f"  LOSE {l.get('subject_line', '')!r}  open={l.get('open_rate')}% click={l.get('click_rate')}%"
        for l in (perf_signal.get("losers") or [])[:5]
    ]) or "  (no losers on file)"
    baseline = perf_signal.get("baseline") or {}
    baseline_line = (
        f"avg open={baseline.get('avg_open_rate', 0)}% click={baseline.get('avg_click_rate', 0)}% "
        f"(n={perf_signal.get('samples', 0)})"
    )

    # Previously-pitched dedupe corpus — last 30 days of pitches we've already
    # surfaced to Dom. Tagged with status so the model knows what's been
    # drafted vs still on the table.
    prev_lines = "\n".join([
        f"  · [{(p.get('metadata') or {}).get('pitch_status', 'pitched')}] {p.get('title', '')!r} — "
        f"{((p.get('metadata') or {}).get('angle') or '')[:120]}"
        for p in (prev_pitches or [])[:25]
    ]) or "  (no recent pitches on file)"

    user_prompt = f"""=== Past 7 days of ingested content ===
{content_brief}

=== Search-volume data (from DataForSEO, US, English) ===
{sv_brief}

=== Related searches around top seed ({seeds[0] if seeds else 'n/a'}) ===
{rel_brief}

=== Newsletter performance baseline ===
{baseline_line}

=== Past WINNERS (above-average open/click — pattern-match the framing) ===
{winners_lines}

=== Past LOSERS (below-average — avoid these patterns) ===
{losers_lines}

=== Dom's RECENT APPROVED pitches (lean into this lane) ===
{approved_lines}

=== Dom's RECENT REJECTED pitches (avoid these patterns) ===
{rejected_lines}

=== PREVIOUSLY PITCHED — last 30 days (do NOT duplicate; variations welcome) ===
{prev_lines}

=== HERALD voice (just so the headlines feel right) ===
{(style_text or '')[:1500]}

=== Editor steer ===
{user_focus.strip() or '(no specific steer this round)'}

Pitch up to {desired_count} stories. Rank strongest first."""

    # Step 5: generate pitches
    from config import MODELS, OPENROUTER_TOOL_PROVIDER_PREFS
    cli = _client()
    try:
        resp = await asyncio.to_thread(
            cli.chat.completions.create,
            model=MODELS["agent"],
            messages=[
                {"role": "system", "content": PITCH_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.55,
            max_tokens=2400,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"[pitch_engine] LLM call failed: {e}")
        return {"count": 0, "pitches": [], "error": str(e)[:200]}

    parsed = _parse_json_response(raw)
    pitches = parsed.get("pitches") or []
    if not isinstance(pitches, list):
        return {"count": 0, "pitches": [], "error": "model did not return pitches array"}

    # Step 6: persist each pitch and attach the id
    enriched: list[dict] = []
    for idx, p in enumerate(pitches, start=1):
        if not isinstance(p, dict):
            continue
        p.setdefault("rank", idx)
        pid = await asyncio.to_thread(_store_pitch, p)
        if pid:
            p["id"] = pid
        enriched.append(p)

    return {
        "count": len(enriched),
        "pitches": enriched,
        "window": f"last {days_back} days",
        "content_items_considered": len(items),
        "keyword_data_points": len(sv_data or []) + len(rel_data or []),
        "auto_ingest": auto_ingest_summary,  # None if DB was healthy; dict if we ran research first
    }


async def record_pitch_feedback(
    pitch_id: str,
    status: str,
    reaction: str = "",
) -> dict:
    """
    Record Dom's verdict on a specific pitch. status must be one of:
      'approved' | 'rejected' | 'drafted' | 'pitched'.
    """
    valid = {"approved", "rejected", "drafted", "pitched"}
    if status not in valid:
        return {"success": False, "error": f"status must be one of {sorted(valid)}"}
    ok = await asyncio.to_thread(update_pitch_status, pitch_id, status, reaction)
    return {"success": ok, "pitch_id": pitch_id, "status": status, "reaction": reaction}


async def list_active_pitches(days: int = 14, limit: int = 12) -> dict:
    """
    Return recent pitches that are still ON THE TABLE — i.e. status='pitched'
    (Dom hasn't approved, rejected, or drafted them yet). This is the canonical
    source for resolving pronouns like 'the other one' or 'the third one' that
    Dom uses in subsequent messages, even if the original pitch message has
    aged out of the conversation history window.

    Returns:
      {
        "count": int,
        "pitches": [
          {"id": str, "headline": str, "angle": str, "topic_tags": list,
           "pitched_at": iso_string, "rank": int_or_null},
          ...
        ],
        "window_days": int,
      }

    Sorted newest first. If nothing on the table, count=0.
    """
    rows = await asyncio.to_thread(get_recent_pitches, days, limit * 4)
    active: list[dict] = []
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("pitch_status", "pitched") != "pitched":
            continue
        active.append({
            "id": row.get("id"),
            "headline": row.get("title", ""),
            "angle": meta.get("angle", ""),
            "topic_tags": row.get("topics") or [],
            "pitched_at": row.get("scraped_at", ""),
            "rank": meta.get("rank"),
        })
        if len(active) >= limit:
            break
    return {
        "count": len(active),
        "pitches": active,
        "window_days": days,
        "note": "Use these IDs when Dom references 'the X one' / 'the other one' / 'the LP angle' — these are the pitches still on the table.",
    }


async def get_pitch_by_id(pitch_id: str) -> dict | None:
    """Fetch a single pitch by id. Returns None if not found."""
    try:
        from db.client import get_client
        client = get_client()
        result = (
            client.table("content_items")
            .select("id, title, raw_text, topics, metadata")
            .eq("id", pitch_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as e:
        logger.error(f"get_pitch_by_id error: {e}")
        return None


async def draft_approved_pitch(
    pitch_id: str,
    reaction: str = "",
) -> dict:
    """
    Atomic 'Dom approved this pitch, draft it now' operation. This:
      1. Marks the pitch status as 'drafted' with Dom's reaction.
      2. Fires draft_full_weekly_newsletter with the pitch's exact headline +
         angle as the trigger_reason — so the orchestrator focuses the issue
         on the approved angle, not whatever the agent last had in context.

    Use this whenever Dom approves a specific pitch by id ("yeah do #2",
    "let's run the LP angle", "draft pitch X"). The combined tool guarantees
    the drafted issue actually matches the approved pitch.
    """
    pitch = await get_pitch_by_id(pitch_id)
    if not pitch:
        return {"success": False, "error": f"pitch {pitch_id} not found"}

    # Mark drafted with reaction
    await asyncio.to_thread(update_pitch_status, pitch_id, "drafted", reaction)

    # Build the trigger_reason from the actual pitch fields
    headline = pitch.get("title", "").strip()
    meta = pitch.get("metadata") or {}
    angle = (meta.get("angle") or "").strip()
    summary = (pitch.get("raw_text") or "").strip()
    trigger_reason = (
        f"Dom approved pitch \"{headline}\". "
        f"Angle: {angle} "
        f"Lead: {summary[:400]}"
    )[:1200]

    # Fire the pipeline (idempotent inside)
    from intelligence.tools import draft_full_weekly_newsletter
    pipeline_result = await draft_full_weekly_newsletter(trigger_reason=trigger_reason)

    return {
        "success": True,
        "pitch_id": pitch_id,
        "headline": headline,
        "pitch_status": "drafted",
        "pipeline": pipeline_result,
    }
