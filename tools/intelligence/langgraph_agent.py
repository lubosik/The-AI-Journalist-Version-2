"""
LangGraph ReAct agent for HERALD.
Replaces the intent classifier in intelligence/agent.py.
Uses AsyncPostgresSaver for persistent memory across sessions.
"""
import asyncio
import json
import logging
import os
import re

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from openai import AsyncOpenAI
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

load_dotenv()

logger = logging.getLogger(__name__)

from config import MODELS


async def call_openrouter(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 2000,
) -> str:
    client = AsyncOpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        base_url="https://openrouter.ai/api/v1",
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def parse_json_response(value: str):
    clean = (value or "").strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.splitlines()[1:-1])
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start_candidates = [i for i in (clean.find("{"), clean.find("[")) if i >= 0]
        if not start_candidates:
            return {}
        start = min(start_candidates)
        end = max(clean.rfind("}"), clean.rfind("]"))
        if end <= start:
            return {}
        try:
            return json.loads(clean[start:end + 1])
        except json.JSONDecodeError:
            return {}

# ─── LLM setup ───────────────────────────────────────────────────────────────
_llm = ChatOpenAI(
    model=MODELS.get("agent", "anthropic/claude-sonnet-4-5"),
    openai_api_key=os.getenv("OPENROUTER_API_KEY", ""),
    openai_api_base="https://openrouter.ai/api/v1",
    temperature=0.7,
    max_tokens=2000,
)

# ─── Checkpointer ────────────────────────────────────────────────────────────
# AsyncPostgresSaver is initialized lazily on first use via _get_checkpointer().
# Lock prevents duplicate pool creation on concurrent startup messages.
_checkpointer = None
_checkpointer_ready = False
_checkpointer_lock = asyncio.Lock()

async def _get_checkpointer():
    """Return the required production PostgreSQL checkpointer."""
    global _checkpointer, _checkpointer_ready
    if _checkpointer_ready:
        return _checkpointer

    async with _checkpointer_lock:
        if _checkpointer_ready:  # re-check inside lock
            return _checkpointer

        db_uri = os.getenv("SUPABASE_DB_URI", "")
        if not db_uri:
            raise RuntimeError("SUPABASE_DB_URI is required for LangGraph persistence")

        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        pool = AsyncConnectionPool(
            conninfo=db_uri,
            max_size=5,
            kwargs={"autocommit": True},
            open=False,
        )
        await pool.open()
        cp = AsyncPostgresSaver(pool)
        await cp.setup()
        _checkpointer = cp
        _checkpointer_ready = True
        logger.info("[langgraph_agent] Using AsyncPostgresSaver for persistent memory")
        return _checkpointer

# ─── Tools ───────────────────────────────────────────────────────────────────

@tool
async def search_knowledge_base(query: str, days_back: int = 30) -> str:
    """Search HERALD's internal knowledge base for information about
    VC secondaries, deals, funds, and market activity. Use this when
    Dom asks about a specific topic, company, fund, or market trend
    and you need to find what we already have stored."""
    from intelligence.tools import search_database
    results_dict = await search_database(query, days_back=min(days_back, 30))
    results = results_dict.get("results", [])
    if not results:
        return "Nothing found in the knowledge base for that query."
    formatted = []
    for r in results[:5]:
        formatted.append(
            f"Source: {r.get('source_name', '')} ({r.get('source_type', '')})"
            f" | Date: {(r.get('published_at') or '')[:10] or 'unknown'}\n"
            f"Excerpt: {(r.get('chunk_text') or '')[:300]}..."
        )
    return "\n\n---\n\n".join(formatted)


@tool
async def research_topic(topic: str, deep: bool = False) -> str:
    """Research a topic on the live web using Perplexity. Use this when
    Dom asks you to research something, mentions a company or deal he
    wants more information on, or when the knowledge base returns nothing
    useful. Set deep=True only when Dom explicitly asks for deep research."""
    from intelligence.tools import web_research
    result = await web_research(topic, deep=deep)
    return result.get("findings", "No research results found.")


@tool
async def ingest_url(url: str, instruction: str = "") -> str:
    """Ingest content from a URL into the HERALD knowledge base.
    Use this whenever Dom shares a URL — Spotify podcast, YouTube video,
    TikTok, news article, or any web link. Automatically detects the
    platform and handles ingestion appropriately."""
    from intelligence.agent import detect_platform, handle_add_content
    try:
        platform = detect_platform(url)
        result_str = await handle_add_content(url, instruction, f"{url} {instruction}".strip())
        return result_str
    except Exception as e:
        return f"Could not ingest {url}: {e}"


@tool
async def ingest_twitter(url_or_handle: str, instruction: str = "") -> str:
    """Ingest a Twitter/X URL, handle, or search query and report useful angles."""
    from ingestion.twitter import (
        ingest_twitter_handle,
        ingest_twitter_search,
        ingest_twitter_url,
    )
    if "twitter.com" in url_or_handle or "x.com" in url_or_handle:
        result = await ingest_twitter_url(url_or_handle)
    elif url_or_handle.startswith("@") or re.fullmatch(r"[A-Za-z0-9_]+", url_or_handle):
        result = await ingest_twitter_handle(url_or_handle)
    else:
        result = await ingest_twitter_search(url_or_handle)
    if not result.get("stored"):
        return f"Could not pull that. {result.get('reason', 'No new tweets found')}."
    content = result.get("combined_text", "")
    if content:
        from intelligence.transcript_analyst import analyse_transcript_for_newsletter
        return await analyse_transcript_for_newsletter(
            content, "Twitter/X", "twitter", instruction
        )
    return f"Pulled {result.get('count', 0)} tweets."


@tool
async def ingest_instagram(url_or_handle: str, instruction: str = "") -> str:
    """Ingest an Instagram post, reel, profile URL, or profile handle."""
    from ingestion.instagram import ingest_instagram_url
    value = url_or_handle
    if "instagram.com" not in value and not value.startswith("http"):
        value = f"https://www.instagram.com/{value.lstrip('@')}/"
    result = await ingest_instagram_url(value)
    if not result.get("stored"):
        return f"Could not pull that. {result.get('reason', 'No new posts found')}."
    content = result.get("combined_text", "")
    if content:
        from intelligence.transcript_analyst import analyse_transcript_for_newsletter
        return await analyse_transcript_for_newsletter(
            content, "Instagram", "instagram", instruction
        )
    return f"Pulled {result.get('count', 0)} Instagram posts."


@tool
async def store_research_finding(content: str, topic: str) -> str:
    """Store a research finding or tip into the knowledge base.
    Use this when Dom tells you something he heard, a rumour, a market
    observation, or any intelligence he wants saved for the newsletter."""
    from intelligence.tools import store_research
    result = await store_research(content, "", topic, dom_requested=True)
    if result.get("stored"):
        return f"Saved to knowledge base under topic: {topic}"
    return f"Did not store: {result.get('reason', 'relevance check failed')}."


@tool
async def get_system_status() -> str:
    """Get the current status of the HERALD system including database
    stats, current edition state, and pipeline health. Use when Dom
    asks how the system is doing, what edition we are on, or wants
    an overview."""
    from intelligence.tools import get_db_status
    from scheduler.edition_manager import get_current_edition_state
    stats = await get_db_status()
    try:
        edition = await get_current_edition_state()
        return (
            f"Database: {stats.get('total_items', stats.get('count', 0))} items total\n"
            f"Edition {edition['active_edition']} — {edition['window'].upper()}\n"
            f"Publishes: {edition['publish_date']}\n"
            f"Draft opens: {edition['draft_date']} 6pm ET\n"
            f"Can draft now: {'Yes' if edition.get('can_draft') else 'No'}"
        )
    except Exception:
        return f"Database: {stats.get('total_items', stats.get('count', 0))} items total"


@tool
async def check_all_sources_tool() -> str:
    """Scan all configured sources (YouTube, TikTok, Twitter, RSS) for new content.
    Use this when Dom says 'go find new stuff', 'check my channels',
    'go learn something new', or asks for a fresh sweep of sources."""
    from intelligence.tools import check_all_sources
    result = await check_all_sources()
    return result.get("note", "Scan started. Results coming in 2-3 minutes.")


@tool
async def get_recent_content() -> str:
    """Get a summary of content ingested in the last 48 hours.
    Use when Dom asks 'what do we have this week', 'what's new',
    'what did you learn', or wants to know what's in the pipeline."""
    from intelligence.tools import get_recent_content_window
    result = await get_recent_content_window(days_back=2)
    items = result.get("items", [])
    if not items:
        return "Nothing new in the last 48 hours."
    lines = [f"Recent content ({result['count']} items, last 48h):"]
    for item in items[:10]:
        age = f"{item['age_days']}d ago" if item.get('age_days') is not None else ""
        lines.append(
            f"- [{item.get('source_name')} / {item.get('source_type')}] {age}\n"
            f"  {item.get('title', '')[:120]}"
        )
    return "\n".join(lines)


@tool
async def pitch_ideas(days_back: int = 7, user_focus: str = "") -> str:
    """Generate 3-5 ranked newsletter story pitches based on recent content.
    Use when Dom asks 'what should I write about', 'pitch me', 'what's worth covering',
    or any open editorial question."""
    from intelligence.pitch_engine import generate_pitches
    result = await generate_pitches(days_back=days_back, desired_count=4, user_focus=user_focus)
    return str(result)


@tool
async def trigger_newsletter_draft(trigger_reason: str = "Dom requested via chat") -> str:
    """Manually trigger a newsletter draft generation. Only use this
    when Dom explicitly asks to draft the newsletter or generate this
    week's edition. Will confirm the plan with Dom before running."""
    from intelligence.tools import draft_full_weekly_newsletter
    result = await draft_full_weekly_newsletter(trigger_reason=trigger_reason)
    if result.get("already_running"):
        return f"Newsletter pipeline already running. Queued your request."
    if result.get("started"):
        return f"Draft generation started. Will send to Telegram when ready (3-6 minutes)."
    return f"Could not start: {result.get('error', 'unknown error')}"


@tool
async def add_newsletter_section(content: str, section_title: str = "", position: str = "end") -> str:
    """Add a new section to the current newsletter draft. Use this when Dom says
    'add this to the newsletter', 'slot this into this week', 'put this in the draft'."""
    from intelligence.agent import handle_inject_newsletter_section
    params = {"content": content, "section_title": section_title, "position": position}
    return await handle_inject_newsletter_section(params, content, [])


@tool
async def edit_newsletter_section(section_id: str, new_content: str) -> str:
    """Edit an existing section in the current newsletter draft by section ID.
    Section IDs are: lead, tldr, market_pulse, angle.
    Use this when Dom asks to rewrite, fix, or update a specific section.
    Updates sections, html_content, and plain_text in Supabase so the
    dashboard reflects the change immediately."""
    import json
    from db.client import get_client
    from newsletter.builder import build_newsletter_html, build_plain_text
    from datetime import date

    try:
        db = get_client()
        # Find the current draft
        result = db.table("newsletter_issues").select("*").eq("status", "draft").order("created_at", desc=True).limit(1).execute()
        if not result.data:
            return "No active draft found."

        issue = result.data[0]
        sections = issue.get("sections") or []
        if isinstance(sections, str):
            sections = json.loads(sections)

        # Update or add the section
        found = False
        for s in sections:
            if s.get("id") == section_id:
                s["content"] = new_content
                found = True
                break
        if not found:
            sections.append({"id": section_id, "title": section_id.replace("_", " ").title(), "content": new_content})

        # Rebuild HTML and plain text
        visuals = issue.get("visuals") or []
        if isinstance(visuals, str):
            visuals = json.loads(visuals)
        week_start_raw = issue.get("week_start")
        week_start = date.fromisoformat(week_start_raw) if week_start_raw else None

        html_content = await build_newsletter_html(
            sections=sections,
            visuals=visuals,
            issue_number=issue.get("issue_number", 1),
            subject_line=issue.get("subject_line", ""),
            week_start=week_start,
        )
        plain_text = build_plain_text(sections)

        db.table("newsletter_issues").update({
            "sections": sections,
            "html_content": html_content,
            "plain_text": plain_text,
        }).eq("id", issue["id"]).execute()

        return f"Section '{section_id}' updated. Dashboard preview refreshed."
    except Exception as e:
        return f"Edit failed: {e}"


@tool
async def store_topic_for_edition(topic: str, edition_offset: int = 0) -> str:
    """Save a topic for a specific newsletter edition.
    edition_offset=0 means current edition, 1 means next edition.
    Use when Dom says 'save this for next week', 'include in the next edition', etc."""
    from tracking.edition_tracker import get_edition_for_date
    from tracking.topic_store import save_topic
    try:
        week = get_edition_for_date()
        edition_number = week['edition_number'] + edition_offset
        result = await save_topic(topic=topic, edition_number=edition_number, priority=7)
        week_str = week.get('week_start', '')
        if edition_offset > 0:
            return f"Saved for Edition {edition_number} (next week): {topic[:100]}"
        return f"Saved for Edition {edition_number} (week of {week_str}): {topic[:100]}"
    except Exception as e:
        return f"Stored topic: {topic[:100]} (error: {e})"


@tool
async def save_writing_rule(instruction: str, category: str = "style") -> str:
    """Save a writing instruction or feedback rule for future newsletters.
    Use this when Dom gives a style preference, correction, or instruction
    about how the newsletter or content should be written."""
    from memory.feedback import store_feedback
    await store_feedback(instruction, category, instruction)
    return f"Writing rule saved: {instruction}"


@tool
async def create_linkedin_post(topic: str) -> str:
    """Generate a LinkedIn post for Dom based on a topic or instruction.
    Use when Dom asks to create a LinkedIn post or repurpose something to LinkedIn."""
    from intelligence.agent import handle_linkedin_repurpose
    return await handle_linkedin_repurpose({"topic": topic}, [])


@tool
async def resend_draft() -> str:
    """Resend the latest newsletter draft preview to Dom via Telegram.
    Use when Dom asks to see the current draft, show the draft, or send it again."""
    from intelligence.tools import resend_draft_preview
    result = await resend_draft_preview()
    if result.get("success"):
        return f"Draft preview resent (Issue #{result.get('issue_number')})."
    return result.get("note", "No draft available.")


@tool
async def include_in_newsletter(
    topic: str,
    source_url: str = None,
    topic_type: str = "topic",
    edition_offset: int = 0,
) -> str:
    """Save a topic, link, deal, headline, or instruction to include in the newsletter.
    Use this whenever Dom says 'include this', 'add this to the newsletter',
    'make sure you cover this', 'I want this in the newsletter', or shares
    a URL with the intent to include it. This GUARANTEES the topic appears
    in the draft. topic_type options: topic, deal, headline, dom_instruction.
    edition_offset=0 means current edition, 1 means next week."""
    from tracking.edition_tracker import get_edition_for_date
    from tracking.topic_store import save_topic

    week = get_edition_for_date()
    edition_number = week['edition_number'] + edition_offset

    ingested_content = None
    if source_url:
        try:
            from intelligence.agent import detect_platform, handle_add_content
            ingest_result = await handle_add_content(source_url, topic, f"{source_url} {topic}".strip())
            if isinstance(ingest_result, str) and len(ingest_result) > 50:
                ingested_content = ingest_result[:2000]
        except Exception:
            pass

    result = await save_topic(
        topic=topic,
        topic_type=topic_type,
        source_content=ingested_content,
        source_url=source_url,
        edition_number=edition_number,
        priority=8,
    )

    week_str = week['week_start']
    if edition_offset > 0:
        edition_str = f"Edition {edition_number} (next week)"
    else:
        edition_str = f"Edition {edition_number} (week of {week_str})"

    return (
        f"Saved for {edition_str}: {topic}\n"
        f"This will be included when the newsletter drafts on Friday."
    )


@tool
async def search_podcast_for_topic(show_name: str, topic: str, max_episodes: int = 3) -> str:
    """Search recent episodes of a podcast/YouTube show for specific content.
    Use this when Dom mentions something from a podcast or YouTube show without providing a URL.
    Examples: 'pull the Bill Gurley All-In segment', 'find what Gurley said about Anthropic on All-In',
    'get the transcript from All-In where they talked about AI'.
    This will automatically find the show, get transcripts, search for the topic,
    and store the relevant content in the knowledge base.
    If not found, it will ask Dom for clarification rather than giving up."""
    from intelligence.tools import search_youtube_channel_for_topic
    result = await search_youtube_channel_for_topic(show_name, topic, max_episodes=max_episodes)

    if result.get("found"):
        lines = [
            f"Found and stored from {result.get('channel_name', show_name)}.",
            f"Episode: {result.get('episode_title', '')}",
            f"URL: {result.get('episode_url', '')}",
            "",
            "Here's the relevant excerpt:",
            result.get("segment", ""),
            "",
            f"(Checked {result.get('episodes_checked', 0)} episode(s). Stored: {'yes' if result.get('stored') else 'already in DB'})",
        ]
        return "\n".join(lines)
    else:
        return result.get("clarification_question", "Could not find that content. Do you have the episode URL?")


@tool
async def view_newsletter_topics(edition_offset: int = 0) -> str:
    """Show all topics saved for the current or next newsletter edition.
    Use when Dom asks what is in the newsletter, what topics are saved,
    what will be included, or wants to review the plan."""
    from tracking.edition_tracker import get_edition_for_date
    from tracking.topic_store import get_all_topics_for_edition

    week = get_edition_for_date()
    edition_number = week['edition_number'] + edition_offset
    topics = get_all_topics_for_edition(edition_number)

    if not topics:
        return (
            f"No topics saved for Edition {edition_number} yet.\n"
            f"Send me anything you want included and I will add it."
        )

    lines = [f"Edition {edition_number} topics ({len(topics)} saved):"]
    for i, t in enumerate(topics, 1):
        type_label = t['topic_type'].upper() if t.get('topic_type') not in ('topic', None) else ''
        prefix = f"[{type_label}] " if type_label else ""
        lines.append(f"{i}. {prefix}{t['topic']}")

    lines.append("\nI will confirm these with you before drafting on Friday.")
    return "\n".join(lines)


@tool
async def remove_newsletter_topic(topic_description: str) -> str:
    """Remove a topic from the current edition newsletter plan.
    Use when Dom says 'remove that', 'take out the topic about X',
    'don't include Y', or 'drop that deal'."""
    from tracking.edition_tracker import get_edition_for_date
    from tracking.topic_store import get_all_topics_for_edition, remove_topic

    week = get_edition_for_date()
    edition_number = week['edition_number']
    topics = get_all_topics_for_edition(edition_number)

    if not topics:
        return f"No topics saved for Edition {edition_number} to remove."

    desc_lower = topic_description.lower()
    match = None
    for t in topics:
        if desc_lower in t['topic'].lower():
            match = t
            break

    if not match:
        topic_list = "\n".join([f"- {t['topic']}" for t in topics[:5]])
        return f"Could not find '{topic_description}'. Current topics:\n{topic_list}"

    remove_topic(match['id'])
    return f"Removed: {match['topic']}"


@tool
async def send_conversation_draft_to_pipeline(
    draft_text: str,
    issue_number: int,
    subject_line: str = "",
    preview_text: str = "",
) -> str:
    """Send a pre-approved conversation draft directly to the newsletter pipeline.
    Use this INSTEAD OF trigger_newsletter_draft when Dom has just finished
    collaboratively writing an issue via Telegram conversation and approves it
    by saying 'send it to pipeline', 'push it', 'send it', 'that's it', or similar.
    Do NOT call this with an empty draft_text — only call it when you have the
    full approved conversation draft content.
    This skips all automated generation and delivers the exact approved text."""
    from intelligence.tools import send_approved_draft_to_pipeline
    result = await send_approved_draft_to_pipeline(
        draft_text=draft_text,
        issue_number=issue_number,
        subject_line=subject_line,
        preview_text=preview_text,
    )
    if result.get("success"):
        return (
            f"Issue #{issue_number} built from conversation draft and pushed to Beehiiv. "
            "Sent to Dom for final approve/decline."
        )
    return f"Pipeline error: {result.get('note', 'unknown error')}"


@tool
async def find_transcript_segment(query: str, days_back: int = 30) -> str:
    """Search stored podcast and YouTube transcripts for a specific topic,
    quote, or segment. Use this when Dom asks about something said on a podcast
    or YouTube show without providing a direct URL — e.g. 'what was the quote
    Gurley read on All-In', 'find what Dario said about machines', 'pull the
    part where they talked about Anthropic on TBPN'.
    This searches the knowledge base first; if nothing is found it automatically
    scrapes the most relevant channels. Never give up without trying both."""
    from intelligence.tools import search_transcript_by_topic
    result = await search_transcript_by_topic(query, days_back=days_back)
    if result.get("found"):
        lines = [result.get("note", "Found in transcripts."), ""]
        for r in result.get("results", [])[:3]:
            lines.append(f"Source: {r.get('source_name', '')} — {r.get('episode_title', '')}")
            if r.get("source_url"):
                lines.append(f"URL: {r.get('source_url', '')}")
            if r.get("published_at"):
                lines.append(f"Date: {r.get('published_at', '')}")
            lines.append("")
            lines.append(r.get("segment", "")[:600])
            lines.append("")
        return "\n".join(lines).strip()
    return result.get("note", "Not found in any stored transcripts or recent channel episodes.")


@tool
async def approve_newsletter_draft() -> str:
    """Dom has approved the topic list and wants the newsletter drafted now.
    Use this when Dom says yes, approved, go ahead, looks good, draft it,
    or any clear affirmation during the pre-draft conversation."""
    import asyncio
    from scheduler.draft_conversation import get_draft_state, set_draft_state, execute_approved_draft

    state = await get_draft_state()
    if state not in ['awaiting_approval', 'in_revision']:
        return "No newsletter draft is currently pending approval."

    await set_draft_state('approved')
    asyncio.create_task(execute_approved_draft())

    return (
        "Drafting now. I will send the newsletter to you when it is ready. "
        "This usually takes 5 to 10 minutes."
    )


@tool
async def view_edition_tracker(edition_offset: int = 0) -> str:
    """Show what has been tracked for a newsletter edition.
    edition_offset=0 is current edition, 1 is next edition.
    Use when Dom asks what is in the current edition, what topics are saved,
    what research has been done this week, or wants a summary of edition content."""
    return await view_newsletter_topics.ainvoke({"edition_offset": edition_offset})


HERALD_TOOLS = [
    search_knowledge_base,
    research_topic,
    ingest_url,
    ingest_twitter,
    ingest_instagram,
    store_research_finding,
    get_system_status,
    check_all_sources_tool,
    get_recent_content,
    pitch_ideas,
    trigger_newsletter_draft,
    add_newsletter_section,
    edit_newsletter_section,
    store_topic_for_edition,
    save_writing_rule,
    create_linkedin_post,
    resend_draft,
    include_in_newsletter,
    search_podcast_for_topic,
    view_newsletter_topics,
    remove_newsletter_topic,
    approve_newsletter_draft,
    view_edition_tracker,
    send_conversation_draft_to_pipeline,
    find_transcript_segment,
]

# ─── System prompt ────────────────────────────────────────────────────────────

async def build_herald_system_prompt(current_message: str = "") -> str:
    from intelligence.master_prompts import HERALD_IDENTITY

    edition_ctx = ""
    topics_summary = "No topics saved yet."
    try:
        from scheduler.edition_manager import get_current_edition_state
        from tracking.topic_store import get_all_topics_for_edition
        state = await get_current_edition_state()
        edition_ctx = (
            f"EDITION {state['active_edition']} | {state['window'].upper()}\n"
            f"Publishes: {state['publish_date']}"
        )
        topics = get_all_topics_for_edition(state["active_edition"])
        if topics:
            topics_summary = "\n".join(f"- {t['topic']}" for t in topics)
    except Exception:
        pass

    voice_ctx = ""
    try:
        from voice_cloning.generator import pull_voice_clone_data
        voice_data = await asyncio.to_thread(pull_voice_clone_data)
        voice_ctx = (voice_data.get("claude_md") or "")[:4000]
    except Exception:
        pass

    preference_summary = "No stored preferences yet."
    relevant_preferences = []
    try:
        from memory.dom_profile import (
            get_all_active_preferences_summary,
            get_relevant_preferences,
        )
        preference_summary = await get_all_active_preferences_summary()
        relevant_preferences = await get_relevant_preferences(
            current_message or "general",
            limit=8,
        )
    except Exception as exc:
        logger.warning("[langgraph_agent] preference context unavailable: %s", exc)

    feedback_text = "None yet."
    try:
        from memory.feedback import get_all_active_feedback
        feedback = await get_all_active_feedback()
        feedback_text = "\n".join(
            f"- {item['instruction']}" for item in feedback[:8]
        ) or "None yet."
    except Exception:
        pass

    draft_context = ""
    try:
        from scheduler.draft_conversation import get_draft_state
        draft_state = await get_draft_state()
        if draft_state in ['awaiting_approval', 'in_revision']:
            draft_context = (
                f"\n\nIMPORTANT: A newsletter draft approval conversation is active. "
                f"State: {draft_state}. "
                f"If Dom says yes, approved, go ahead, draft it, or similar: use approve_newsletter_draft tool immediately. "
                f"If Dom wants to add topics: use include_in_newsletter tool then confirm the updated list. "
                f"If Dom wants to remove topics: use remove_newsletter_topic tool then confirm the updated list. "
                f"If Dom wants research: use research_topic tool, store findings, then present updated list. "
                f"After any change: use view_newsletter_topics to show the updated full list and ask for approval again."
            )
    except Exception:
        pass

    relevant_text = "\n".join(
        f"- {item.get('content', '')}" for item in relevant_preferences
    ) or "None specifically relevant."

    return f"""{HERALD_IDENTITY}

{voice_ctx}

{edition_ctx}

TOPICS SAVED FOR THIS EDITION:
{topics_summary}

DOM'S ACTIVE PREFERENCES:
{preference_summary}

MOST RELEVANT PREFERENCES FOR THIS MESSAGE:
{relevant_text}

ACTIVE WRITING RULES:
{feedback_text}
{draft_context}

PODCAST AND YOUTUBE CONTENT: When Dom mentions something from a podcast or YouTube show (e.g. "Bill Gurley went off on Anthropic on All-In"), do NOT ask if you should pull the transcript — just DO IT. Call search_podcast_for_topic immediately with the show name and the topic. If you find it, store it and report back the excerpt. If you don't find it in 3 episodes, then ask Dom for the specific episode URL or date.

TRANSCRIPT / QUOTE LOOKUP: When Dom asks about a specific quote, segment, or thing someone said on a podcast or YouTube show, call find_transcript_segment immediately with the topic or quote fragment. Search the DB first; if not found, it will automatically scrape the relevant channels. Never say "not landing it cleanly" and give up — always try find_transcript_segment before admitting defeat.

CONVERSATION DRAFT PIPELINE — CRITICAL RULE:
When Dom and you have been collaboratively writing a newsletter issue through conversation and Dom approves the final draft by saying "send it to pipeline", "push it to pipeline", "send it", "that's it", "looks good send it", or any similar approval phrase after a collaborative drafting session:
- You MUST call send_conversation_draft_to_pipeline with the full approved draft text.
- You MUST NOT call trigger_newsletter_draft — that runs the automated pipeline from scratch and will generate completely different content, ignoring everything you and Dom just wrote together.
- Include the complete approved text in draft_text. Include the subject_line and preview_text if Dom provided them.
- If Dom says "Issue #N" after approving, use that as the issue_number.
The distinction: trigger_newsletter_draft = start the automated LLM pipeline. send_conversation_draft_to_pipeline = push the text we just wrote together."""


# ─── Agent creation ───────────────────────────────────────────────────────────

async def _get_agent(current_message: str):
    """Build the agent lazily with a fresh system prompt."""
    prompt_text = await build_herald_system_prompt(current_message)
    cp = await _get_checkpointer()
    return create_react_agent(
        model=_llm,
        tools=HERALD_TOOLS,
        checkpointer=cp,
        prompt=SystemMessage(content=prompt_text),
    )


# ─── Public interface ─────────────────────────────────────────────────────────

async def process_message(user_message: str, thread_id: str = "dom", **kwargs) -> str:
    """
    Process a message through the LangGraph ReAct agent.
    thread_id is always "dom" — single user, persistent context.
    The checkpointer persists conversation history across sessions.

    Accepts **kwargs for compatibility with the legacy process_message signature
    (which accepts telegram_message_id and other optional params).
    """
    from filters.response_filter import filter_response

    from memory.conversation import store_message

    config = {"configurable": {"thread_id": "dom"}}

    try:
        agent = await _get_agent(user_message)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config=config,
        )
        last_message = result["messages"][-1]
        content = last_message.content if hasattr(last_message, "content") else str(last_message)
        if isinstance(content, list):
            # Handle structured content blocks
            text_parts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
                if not isinstance(block, dict) or block.get("type") != "tool_use"
            ]
            content = " ".join(text_parts).strip()
        clean = filter_response(str(content))
        await store_message(
            "user",
            user_message,
            telegram_message_id=kwargs.get("telegram_message_id"),
        )
        await store_message("assistant", clean)
        try:
            from memory.dom_profile import extract_and_store_preference
            asyncio.create_task(extract_and_store_preference(user_message, clean))
        except Exception as exc:
            logger.warning("[langgraph_agent] preference extraction not started: %s", exc)
        return clean
    except Exception as e:
        logger.error(f"[langgraph_agent] process_message error: {e}", exc_info=True)
        return "Hit a snag. Try again in a moment."
