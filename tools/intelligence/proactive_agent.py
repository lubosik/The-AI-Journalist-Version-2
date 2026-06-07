"""Selective proactive suggestions from newly ingested content."""

import asyncio
import json
import logging
import os
import re

from openai import OpenAI

from config import MODELS, OPENROUTER_BASE_URL, VC_SECONDARIES_KEYWORDS
from filters.response_filter import filter_response
from intelligence.master_prompts import PROACTIVE_SUGGESTION
from memory.dom_profile import get_all_active_preferences_summary
from scheduler.edition_manager import get_current_edition_state
from telegram_bot.sender import send_to_client
from tracking.edition_tracker import track_content
from tracking.topic_store import get_all_topics_for_edition

logger = logging.getLogger(__name__)

# VC secondaries terms we always want to catch in transcript windows
_SECONDARIES_TERMS = [
    "secondary", "secondaries", "spv", "pre-ipo", "tender offer",
    "cap table", "lp", "gp-led", "continuation vehicle", "fund stake",
    "anthropic", "openai", "spacex", "anduril", "xai", "stripe", "databricks",
    "valuation", "fundraise", "ipo", "liquidity", "carried interest",
]


def _extract_relevant_sections(text: str, max_chars: int = 6000) -> str:
    """
    Extract the most VC-secondaries-relevant sections from a long transcript.
    Finds paragraphs/windows containing key terms and returns them concatenated.
    Falls back to the first max_chars if no relevant section found.
    """
    if len(text) <= max_chars:
        return text

    text_lower = text.lower()
    windows = []
    window_size = 800  # chars per context window
    step = 400         # overlap so we don't split mid-sentence

    for start in range(0, len(text) - window_size, step):
        chunk = text[start:start + window_size]
        chunk_lower = chunk.lower()
        score = sum(1 for term in _SECONDARIES_TERMS if term in chunk_lower)
        if score > 0:
            windows.append((score, start, chunk))

    if not windows:
        return text[:max_chars]

    # Sort by relevance score descending, then take the top chunks up to max_chars
    windows.sort(key=lambda x: -x[0])
    selected = []
    total = 0
    seen_starts = set()
    for score, start, chunk in windows:
        # De-overlap: skip if a nearby window was already selected
        if any(abs(start - s) < window_size for s in seen_starts):
            continue
        selected.append((start, chunk))
        seen_starts.add(start)
        total += len(chunk)
        if total >= max_chars:
            break

    # Re-sort by position so text reads in order
    selected.sort(key=lambda x: x[0])
    result = "\n\n[...]\n\n".join(chunk for _, chunk in selected)
    return result[:max_chars]


def _parse_json_response(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


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
        temperature=0.2,
    )
    return response.choices[0].message.content or ""


async def analyse_and_suggest(
    content_item_id: str,
    source_name: str,
    raw_text: str,
) -> dict:
    """Message Dom only when the model returns a confidence score of at least 7."""
    try:
        dom_preferences = await get_all_active_preferences_summary()
        edition = await get_current_edition_state()
        edition_number = edition["active_edition"]
        topics = get_all_topics_for_edition(edition_number)
        topics_text = "\n".join(f"- {topic['topic']}" for topic in topics) or "None"

        # Extract the most VC-secondaries-relevant sections from the transcript
        # (handles long YouTube/TikTok transcripts where key content may be mid-video)
        content_preview = _extract_relevant_sections(raw_text or "", max_chars=6000)

        # Provide a fallback dom_preferences if none stored yet
        if not dom_preferences or dom_preferences.strip() == "No stored preferences yet.":
            dom_preferences = (
                "Dom runs a pre-IPO VC secondaries advisory. He covers Anthropic, OpenAI, SpaceX, "
                "Anduril, xAI, Stripe, and Databricks. He publishes a weekly newsletter to institutional "
                "capital allocators. He is specifically interested in: GP-led continuation vehicles, "
                "LP secondary interest, fund stake sales, pre-IPO secondaries, tender offers, cap table "
                "liquidity, SPVs, and secondary market deal flow for top-tier AI and tech companies."
            )

        result = _parse_json_response(await _call_openrouter(
            "You are HERALD. Return only valid JSON.",
            PROACTIVE_SUGGESTION.format(
                dom_preferences=dom_preferences,
                edition_number=edition_number,
                current_topics=topics_text,
                source_name=source_name,
                content_preview=content_preview,
            ),
        ))
        confidence = int(result.get("confidence", 0) or 0)
        message = filter_response(str(result.get("message") or ""))
        if not result.get("worth_surfacing") or confidence < 7 or not message:
            return {"sent": False, "confidence": confidence}

        auto_add = bool(result.get("auto_add")) and confidence >= 9
        suggested_topic = result.get("suggested_topic")

        # Auto-add to edition topics when confidence is very high
        if auto_add and suggested_topic:
            try:
                from tracking.topic_store import save_topic
                await save_topic(
                    topic=suggested_topic,
                    topic_type="topic",
                    edition_number=edition_number,
                    priority=7,
                )
                logger.info("[proactive_agent] Auto-added topic to edition %d: %s", edition_number, suggested_topic)
            except Exception as auto_add_err:
                logger.warning("[proactive_agent] Auto-add failed: %s", auto_add_err)

        track_content(
            content_type="dom_instruction",
            title="HERALD proactive suggestion",
            body=message,
            content_item_id=content_item_id,
            source_name=source_name,
            added_by="system",
            edition_number=edition_number,
        )

        # Build the message to Dom — if auto-added, tell him
        if auto_add and suggested_topic:
            delivery_message = (
                f"{message}\n\n"
                f"I've added \"{suggested_topic}\" to Edition {edition_number} topics automatically. "
                "Reply to adjust or remove it."
            )
        else:
            delivery_message = message

        sent = await send_to_client(
            filter_response(delivery_message),
            parse_mode=None,
        )
        return {
            "sent": bool(sent),
            "confidence": confidence,
            "suggested_topic": suggested_topic,
            "auto_added": auto_add,
        }
    except Exception as e:
        logger.error("[proactive_agent] Analysis failed: %s", e)
        return {"sent": False, "confidence": 0, "error": str(e)}
