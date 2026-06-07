import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

from config import MODELS, OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)


def _build_research_system_prompt() -> str:
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%B %d, %Y")  # e.g. "May 03, 2026"
    current_year = now.year
    prev_years = f"{current_year - 2} or {current_year - 1}"
    return (
        f"You are a research agent for a VC secondaries newsletter. "
        f"Today's date is {today_str}.\n"
        "Find specific, current, factual information about the given query in the context of VC secondaries, "
        "LP transactions, pre-IPO markets, and private equity liquidity.\n"
        "Return findings as dense factual paragraphs with specific names, numbers, and deals mentioned.\n"
        "Focus on what has happened in the last 48 hours. If nothing fresh exists, say that plainly. "
        "Current-week background is allowed only after the fresh update is established. Be a reporter, not a summariser.\n\n"
        "COMPANY UNIVERSE — STRICT FILTER: This newsletter covers ONLY Anthropic, OpenAI, SpaceX, Anduril, "
        "xAI, Stripe, Databricks, and direct peers at exactly this scale, plus the Musk vs Altman federal trial. "
        "Do NOT return findings about general PE secondaries, mid-market funds, broad VC market trends, "
        "or companies outside this universe. If a query returns no relevant results within this universe, "
        "say so explicitly rather than broadening to off-universe content.\n\n"
        f"CRITICAL DATE RULE: All data and events you report must be from {current_year} unless you are "
        "explicitly providing historical context. If you reference a statistic from "
        f"{prev_years}, you MUST label it clearly as historical background "
        f"(e.g., \"For context, in {current_year - 1}...\"). "
        f"Never present data older than {current_year} as if it describes current conditions. "
        f"The newsletter audience is reading on {today_str}. "
        "Do not reuse older viral anecdotes like Storm Duncan, Anthropic shares for a home, Vika Ventures, "
        "Keyport Venture, or Late Stage Asset Management unless there is a new development in the last 48 hours."
    )

_DATA_POINT_EXTRACTION_PROMPT = """Extract 3-5 specific key data points from the research findings below.
Each data point must contain a concrete number, name, fund size, valuation, discount rate, or deal term.
Do not include vague statements. Numbers and proper nouns only.

Return a JSON array of strings. Example:
["Sequoia raised $3.5B continuation vehicle at 12% discount to NAV",
 "Lexington Partners acquired LP stake in Andreessen Horowitz Fund VII",
 "Secondary market volume reached $68B in H1 2026"]

Research findings:
{findings}

Respond with a JSON array only. No markdown fences, no explanation."""


def _get_openrouter_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY must be set in environment")
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)


async def research_topic(topic: str, context: str = "", deep: bool = False) -> dict:
    """
    Research a specific topic using Perplexity via OpenRouter.
    Returns {"topic": str, "findings": str, "sources": list[str], "key_data_points": list[str]}
    """
    result: dict = {
        "topic": topic,
        "findings": "",
        "sources": [],
        "key_data_points": [],
    }

    model = MODELS["deep_research"] if deep else MODELS["research"]

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%B %d, %Y")
    user_prompt = (
        f"Today is {today_str}. "
        f"Research the following topic for a VC secondaries newsletter covering the last 48 hours:\n\n"
        f"TOPIC: {topic}\n"
    )
    if context:
        user_prompt += f"\nADDITIONAL CONTEXT: {context}\n"

    user_prompt += (
        "\nProvide 2-3 dense factual paragraphs. Include specific fund names, dollar amounts, "
        "deal terms, participant names, and dates wherever available. If results are older than 48 hours, "
        "label them as background and say there is no fresh update. Be precise and reportorial."
    )

    try:
        client = _get_openrouter_client()

        # Run the blocking OpenAI call in a thread pool to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _build_research_system_prompt()},
                    {"role": "user", "content": user_prompt},
                ],
            ),
        )

        findings = response.choices[0].message.content or ""
        result["findings"] = findings

        # Extract citations if Perplexity returns them
        if hasattr(response, "citations") and response.citations:
            result["sources"] = list(response.citations)

    except Exception as e:
        logger.error(f"research_topic error for '{topic}' using model '{model}': {e}")
        result["findings"] = ""
        result["sources"] = []
        # Return early with empty data points — skip extraction step
        return result

    if not result["findings"]:
        return result

    # Use gemini-flash to pull out concrete key data points from the findings
    try:
        client = _get_openrouter_client()
        extraction_prompt = _DATA_POINT_EXTRACTION_PROMPT.format(findings=result["findings"])

        loop = asyncio.get_running_loop()
        extraction_response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=MODELS["fast"],
                messages=[
                    {"role": "user", "content": extraction_prompt},
                ],
            ),
        )

        raw = (extraction_response.choices[0].message.content or "").strip()

        # Strip markdown fences if the model wrapped them anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data_points = json.loads(raw)
        if isinstance(data_points, list):
            result["key_data_points"] = [str(p) for p in data_points[:5]]

    except Exception as e:
        logger.warning(f"key_data_points extraction failed for '{topic}': {e}")
        result["key_data_points"] = []

    return result


async def research_all_topics(topics: list[str]) -> list[dict]:
    """Run research on all topics in parallel. Ongoing stories get 'latest update' appended."""
    if not topics:
        return []
    tasks = [research_topic(_make_fresh_query(t)) for t in topics]
    return await asyncio.gather(*tasks, return_exceptions=False)


# Dom's preference universe — only these seeds are used for SEO signals
_DOM_UNIVERSE_SEEDS = [
    "OpenAI", "Anthropic", "SpaceX", "Anduril", "xAI", "Stripe", "Databricks",
    "Musk Altman trial", "OpenAI valuation", "Anthropic funding round",
    "SpaceX secondary shares", "Anduril funding", "xAI Grok valuation",
    "pre-IPO secondary market", "venture secondaries", "OpenAI IPO",
    "Anthropic Series E", "SpaceX IPO", "OpenAI Sam Altman",
]

# Ongoing story markers — when detected, append "latest update" to research queries
_ONGOING_STORY_PATTERNS = [
    "trial", "lawsuit", "case", "hearing", "verdict", "ruling",
    "musk altman", "vs altman", "vs musk",
    "acquisition", "merger", "ongoing",
]


async def _get_universe_trending_signal() -> list[dict]:
    """
    Query DataForSEO for live search volume on Dom's preference universe seeds.
    Returns list of {keyword, search_volume} sorted by volume desc.
    Used as a signal for topic selection — not as topics themselves.
    """
    try:
        from intelligence.dataforseo import get_search_volume
        results = await get_search_volume(_DOM_UNIVERSE_SEEDS)
        top = [r for r in results if r.get("search_volume", 0) > 0]
        logger.info("[seo_signal] top universe seeds: %s", [(r["keyword"], r["search_volume"]) for r in top[:5]])
        return top
    except Exception as e:
        logger.warning("[seo_signal] DataForSEO universe signal failed: %s", e)
        return []


async def _seo_rank_topics(candidate_topics: list[str]) -> list[str]:
    """
    Rank candidate topics by live DataForSEO search volume.
    Only the candidates themselves are ranked — no universe seeds added as topics.
    Falls back to original order on any error.
    """
    if not candidate_topics:
        return candidate_topics
    try:
        from intelligence.dataforseo import get_search_volume

        # Build short keyword seeds from each candidate (first 4 words, strip year)
        seeds = []
        for t in candidate_topics:
            words = t.replace("2026", "").replace("2025", "").split()
            seed = " ".join(words[:4]).strip(" .,")
            seeds.append(seed if seed else t[:40])

        volumes = await get_search_volume(list(dict.fromkeys(seeds)))
        vol_map = {r["keyword"].lower(): r["search_volume"] for r in volumes}

        def _score(seed: str) -> int:
            return vol_map.get(seed.lower(), 0)

        scored = sorted(zip(candidate_topics, seeds), key=lambda x: _score(x[1]), reverse=True)
        ranked = [t for t, _ in scored]
        logger.info("[seo_rank] ranked: %s", [(t[:50], _score(s)) for t, s in scored[:3]])
        return ranked
    except Exception as e:
        logger.warning("[seo_rank] DataForSEO ranking failed (%s) — using original order", e)
        return candidate_topics


def _make_fresh_query(topic: str) -> str:
    """
    For ongoing stories (trials, lawsuits, etc.), append 'latest update' so
    research returns new developments, not a rehash of the overall event.
    """
    lower = topic.lower()
    if any(p in lower for p in _ONGOING_STORY_PATTERNS):
        now = datetime.now(timezone.utc)
        return f"{topic} latest update {now.strftime('%B %Y')}"
    return topic


async def identify_weekly_topics(db_content: list[dict]) -> list[str]:
    """
    Analyse this week's ingested content and identify 5-7 specific topics to research.
    Topics are SEO-ranked via DataForSEO so the highest-demand stories surface first.
    When no directives are set, the orchestrator picks the top 1-3 from this ranked list.

    db_content: list of content item dicts from Supabase (48h window)
    Returns: list of specific topic strings, sorted by live search volume (highest first)
    """
    _FALLBACK_TOPICS = [
        "Anthropic latest fundraise or valuation news 2026",
        "Musk Altman trial latest development today",
        "OpenAI SpaceX Anduril specific incident investor behavior 2026",
        "xAI Grok valuation cap table secondary market 2026",
        "Stripe Databricks pre-IPO secondary trade 2026",
        "Anduril defense tech fundraise insider story 2026",
        "SpaceX secondary market specific buyer seller story 2026",
    ]

    # Fetch DataForSEO trending signal for Dom's universe in parallel with content processing
    seo_signal_task = asyncio.ensure_future(_get_universe_trending_signal())

    if not db_content:
        logger.warning("identify_weekly_topics: no db_content provided, using SEO-ranked fallback")
        await seo_signal_task  # let it complete, then rank fallbacks
        return await _seo_rank_topics(_FALLBACK_TOPICS)

    # Build a compact digest — priority sources (TBPN, elenanisonoff, X) surfaced first
    _PRIORITY_SOURCES = {"tbpn", "elenanisonoff", "unusual_whales", "citrini7"}
    priority_items = [c for c in db_content if c.get("source_name", "").lower() in _PRIORITY_SOURCES]
    other_items = [c for c in db_content if c.get("source_name", "").lower() not in _PRIORITY_SOURCES]

    content_digest_parts = []
    for item in (priority_items + other_items):
        title = item.get("title") or ""
        summary = item.get("summary") or item.get("content") or ""
        snippet = summary[:200] if summary else ""
        source = item.get("source_name", "")
        prefix = f"[PRIORITY:{source}]" if source.lower() in _PRIORITY_SOURCES else f"[{source}]"
        if title or snippet:
            content_digest_parts.append(f"{prefix} {title}: {snippet}")

    content_digest = "\n".join(content_digest_parts[:60])

    # Wait for the SEO signal — format it as a hint for the LLM
    seo_signal = await seo_signal_task
    seo_hint = ""
    if seo_signal:
        top_trending = [f"{r['keyword']} ({r['search_volume']:,}/mo)" for r in seo_signal[:8]]
        seo_hint = (
            "\nLIVE SEARCH SIGNAL — what Dom's audience is actively searching for right now:\n"
            + ", ".join(top_trending)
            + "\nPrioritise topics that overlap with high-volume terms above — those are what readers want.\n"
        )

    prompt = (
        "You are an editor for a venture capital intelligence newsletter focused exclusively on the top tier of tech.\n\n"
        "Below is a digest of content ingested in the LAST 48 HOURS. "
        "Identify 5-7 specific research topics for this week's issue.\n\n"
        "FRESHNESS — MANDATORY: Only surface topics from the last 48 hours. "
        "For any ONGOING story (trial, lawsuit, ongoing deal), identify the LATEST UPDATE or NEW DEVELOPMENT only — "
        "not a recap of what the story is. Example: not 'Musk vs Altman trial' but 'Musk Altman trial — [specific new testimony/ruling this week]'.\n\n"
        f"{seo_hint}"
        "PREFERENCE UNIVERSE — MANDATORY — ONLY THESE TOPICS ARE ELIGIBLE:\n"
        "- Prominent fundraises at: Anthropic, OpenAI, SpaceX, Anduril, xAI, Stripe, Databricks, and direct peers\n"
        "- Pre-IPO secondary trades, cap table activity, valuation events at these specific companies\n"
        "- Breaking news, rumors, insider stories about these companies and their named founders\n"
        "- The Musk vs Altman federal trial — ONLY new developments, testimony, rulings from this week\n"
        "- Human-behaviour stories tied to these companies (someone accepting shares as payment, a buyer unable to get filled, etc.)\n\n"
        "REJECT COMPLETELY — DO NOT NAME THESE AS TOPICS:\n"
        "- General PE, mid-market buyouts, LBO financing\n"
        "- Companies outside the preference universe above\n"
        "- Broad macro, interest rates, generic VC market statistics\n"
        "- Any story that is not from the last 48 hours\n\n"
        "RULES:\n"
        "- Topics must be SPECIFIC — company name + specific incident, not just a company name.\n"
        "- For ongoing stories: the topic MUST name the specific new development, not the story in general.\n"
        "- Items labelled [PRIORITY:tbpn], [PRIORITY:elenanisonoff], [PRIORITY:unusual_whales], or [PRIORITY:citrini7] "
        "are from primary sources. Strongly prefer these.\n\n"
        "THIS WEEK'S CONTENT DIGEST (last 48 hours only):\n"
        f"{content_digest}\n\n"
        "Respond with a JSON array of topic strings only. No markdown fences, no explanation.\n"
        'Example: ["Musk Altman trial — new witness testimony on OpenAI governance", "Anthropic Series F at $850B valuation — specific LP reactions", "SpaceX secondary block trade specific buyer identified"]'
    )

    try:
        client = _get_openrouter_client()
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=MODELS["fast"],
                messages=[{"role": "user", "content": prompt}],
            ),
        )

        raw = (response.choices[0].message.content or "").strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        topics = json.loads(raw)

        if isinstance(topics, list) and topics:
            clean_topics = [str(t).strip() for t in topics if str(t).strip()]
            if clean_topics:
                logger.info(f"identify_weekly_topics: {len(clean_topics)} candidates — ranking by SEO volume")
                ranked = await _seo_rank_topics(clean_topics[:7])
                logger.info(f"identify_weekly_topics: ranked order: {ranked}")
                return ranked

        logger.warning("identify_weekly_topics: model returned empty list, using SEO-ranked fallback")
        return await _seo_rank_topics(_FALLBACK_TOPICS)

    except json.JSONDecodeError as e:
        logger.warning(f"identify_weekly_topics: JSON parse failed ({e}), using SEO-ranked fallback")
        return await _seo_rank_topics(_FALLBACK_TOPICS)
    except Exception as e:
        logger.error(f"identify_weekly_topics error: {e}")
        return _FALLBACK_TOPICS
