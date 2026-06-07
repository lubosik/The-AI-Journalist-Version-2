"""Post-ingestion analysis for content Dom submits."""

import asyncio
import os

from openai import OpenAI

from config import MODELS, OPENROUTER_BASE_URL
from filters.response_filter import filter_response
from intelligence.master_prompts import TRANSCRIPT_ANALYSIS
from memory.dom_profile import get_all_active_preferences_summary
from scheduler.edition_manager import get_current_edition_state
from tracking.topic_store import get_all_topics_for_edition


async def _call_openrouter(system: str, user: str) -> str:
    client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )
    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=MODELS["writer"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
    )
    return response.choices[0].message.content or ""


async def analyse_transcript_for_newsletter(
    content: str,
    source_name: str,
    source_type: str,
    dom_instruction: str = "",
) -> str:
    dom_preferences = await get_all_active_preferences_summary()
    edition = await get_current_edition_state()
    topics = get_all_topics_for_edition(edition["active_edition"])
    topics_text = "\n".join(f"- {topic['topic']}" for topic in topics) or "None saved yet"

    response = await _call_openrouter(
        "You are HERALD. Be specific, opinionated, conversational. Sound like a sharp colleague.",
        TRANSCRIPT_ANALYSIS.format(
            dom_instruction=dom_instruction or "review for newsletter angles",
            source_name=source_name,
            source_type=source_type,
            full_content=(content or "")[:8000],
            dom_preferences=dom_preferences,
            current_topics=topics_text,
        ),
    )
    return filter_response(response)
