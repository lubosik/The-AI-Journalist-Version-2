import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from db.client import get_client

logger = logging.getLogger(__name__)

_FEEDBACK_CATEGORIES = [
    'tone', 'structure', 'content', 'visual', 'factual',
    'style', 'length', 'topic', 'format', 'other'
]

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def next_friday(reference: date | None = None) -> date:
    """Return the upcoming Sunday edition date."""
    today = reference or datetime.now().date()
    days_ahead = (6 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def parse_edition_date(text: str, reference: date | None = None) -> date:
    """
    Best-effort parser for edition dates in editor instructions.
    Defaults to the upcoming Sunday edition.
    """
    today = reference or datetime.now().date()
    lower = (text or "").lower()

    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", lower)
    if iso_match:
        return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))

    if "in two weeks" in lower or "two weeks" in lower:
        return next_friday(today + timedelta(days=14))

    if "next sunday" in lower:
        base = next_friday(today)
        return base + timedelta(days=7) if base == today else base

    if "this sunday" in lower:
        return next_friday(today)

    if "next week" in lower:
        base = next_friday(today)
        return base + timedelta(days=7)

    if "this week" in lower or "this edition" in lower:
        return next_friday(today)

    if "next thursday" in lower:
        days_ahead = (3 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_ahead) + timedelta(days=7)

    if "this thursday" in lower:
        days_ahead = (3 - today.weekday()) % 7
        return today + timedelta(days=days_ahead or 7)

    month_day = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        lower,
    )
    day_month = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        lower,
    )
    if month_day:
        month = _MONTHS[month_day.group(1)]
        day = int(month_day.group(2))
        candidate = date(today.year, month, day)
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate
    if day_month:
        day = int(day_month.group(1))
        month = _MONTHS[day_month.group(2)]
        candidate = date(today.year, month, day)
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate

    return next_friday(today)


async def is_feedback(message: str) -> dict:
    """
    Determine if a message is a feedback/instruction about the newsletter.
    Returns {"is_feedback": bool, "category": str, "instruction": str}
    """
    from openai import OpenAI
    from config import MODELS, OPENROUTER_BASE_URL

    try:
        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.getenv("OPENROUTER_API_KEY"))

        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You decide if a message from Dom is a correction, preference, or instruction "
                        "about how his HERALD newsletter should be written.\n\n"
                        "Examples of feedback: 'make it shorter', 'stop using the word synergies', "
                        "'the tone is too formal', 'always mention the deal size', 'less jargon'.\n\n"
                        "Examples of NOT feedback: 'what is Sequoia up to?', 'show me the latest items', "
                        "'search for GP-led deals', 'what did you find this week?', 'run the ingestion'.\n\n"
                        f"Categories: {', '.join(_FEEDBACK_CATEGORIES)}\n\n"
                        "Return only valid JSON: "
                        '{"is_feedback": true/false, "category": "category_string", "instruction": "clean actionable instruction"}'
                    ),
                },
                {"role": "user", "content": message},
            ],
            temperature=0,
        )

        content = response.choices[0].message.content or "{}"
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

        parsed = json.loads(content)
        return {
            "is_feedback": bool(parsed.get("is_feedback", False)),
            "category": str(parsed.get("category", "other")),
            "instruction": str(parsed.get("instruction", message)),
        }

    except Exception as e:
        logger.error(f"is_feedback error: {e}")
        return {"is_feedback": False, "category": "other", "instruction": message}


async def store_feedback(raw_message: str, category: str, instruction: str) -> Optional[str]:
    """Store feedback in feedback_log. Returns the record ID."""
    try:
        client = get_client()
        result = await asyncio.to_thread(
            lambda: client.table("feedback_log").insert({
                "raw_message": raw_message,
                "category": category if category in _FEEDBACK_CATEGORIES else "other",
                "instruction": instruction,
                "is_active": True,
            }).execute()
        )
        if result.data:
            record_id = result.data[0]["id"]
            try:
                from tracking.edition_tracker import track_content
                track_content(
                    content_type='feedback_applied',
                    title=f"Writing rule: {str(instruction)[:60]}",
                    body=str(instruction),
                    added_by='dom',
                )
            except Exception:
                pass
            return record_id
        return None
    except Exception as e:
        logger.error(f"store_feedback error: {e}")
        return None


async def get_all_active_feedback() -> list[dict]:
    """Retrieve all active feedback instructions."""
    try:
        client = get_client()
        result = await asyncio.to_thread(
            lambda: client.table("feedback_log")
            .select("id, category, instruction, applied_count, created_at")
            .eq("is_active", True)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"get_all_active_feedback error: {e}")
        return []


def format_feedback_for_prompt(feedback_items: list[dict]) -> str:
    """Format feedback list as a numbered instruction block for LLM prompts."""
    if not feedback_items:
        return "No specific instructions on file."
    lines = []
    for i, item in enumerate(feedback_items, 1):
        lines.append(f"{i}. [{item['category']}] {item['instruction']}")
    return "\n".join(lines)


async def deactivate_feedback(feedback_id: str) -> bool:
    """Mark a feedback item as inactive (Dom said to ignore it)."""
    try:
        client = get_client()
        await asyncio.to_thread(
            lambda: client.table("feedback_log")
            .update({"is_active": False})
            .eq("id", feedback_id)
            .execute()
        )
        return True
    except Exception as e:
        logger.error(f"deactivate_feedback error: {e}")
        return False


async def delete_feedback_by_index(index: int) -> dict:
    """Delete the Nth active feedback item (1-based index). Returns {success, deleted_instruction}."""
    items = await get_all_active_feedback()
    if index < 1 or index > len(items):
        return {"success": False, "error": f"No feedback item #{index}. There are {len(items)} active items."}
    target = items[index - 1]
    ok = await deactivate_feedback(target["id"])
    if ok:
        return {"success": True, "deleted_instruction": target["instruction"]}
    return {"success": False, "error": "Database update failed."}


async def store_topic_directive(topics: str, edition_date: str | None = None) -> Optional[str]:
    """Store a one-time topic directive for a Sunday newsletter edition."""
    try:
        client = get_client()
        target = date.fromisoformat(edition_date) if edition_date else parse_edition_date(topics)
        instruction = f"[Edition {target.isoformat()}] {topics.strip()}"
        result = await asyncio.to_thread(
            lambda: client.table("feedback_log").insert({
                "raw_message": topics,
                "category": "topic",
                "instruction": instruction,
                "is_active": True,
            }).execute()
        )
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        logger.error(f"store_topic_directive error: {e}")
        return None


async def get_active_topic_directives(edition_date: str | None = None) -> list[str]:
    """Return active one-time topic directives, optionally scoped to an edition."""
    try:
        client = get_client()
        result = await asyncio.to_thread(
            lambda: client.table("feedback_log")
            .select("id, instruction")
            .eq("is_active", True)
            .in_("category", ["topic_directive", "topic"])
            .execute()
        )
        directives = [r["instruction"] for r in (result.data or [])]
        if not edition_date:
            return directives
        scoped: list[str] = []
        current_prefix = f"[Edition {edition_date}]"
        for directive in directives:
            if directive.startswith("[Edition "):
                if directive.startswith(current_prefix):
                    scoped.append(directive)
            else:
                scoped.append(directive)
        return scoped
    except Exception as e:
        logger.error(f"get_active_topic_directives error: {e}")
        return []


async def clear_topic_directives(edition_date: str | None = None) -> None:
    """Deactivate consumed topic directives."""
    try:
        client = get_client()
        if edition_date:
            rows = await asyncio.to_thread(
                lambda: client.table("feedback_log")
                .select("id, instruction")
                .eq("is_active", True)
                .in_("category", ["topic_directive", "topic"])
                .execute()
            )
            ids = [
                row["id"] for row in (rows.data or [])
                if not row.get("instruction", "").startswith("[Edition ")
                or row.get("instruction", "").startswith(f"[Edition {edition_date}]")
            ]
            if ids:
                await asyncio.to_thread(
                    lambda: client.table("feedback_log")
                    .update({"is_active": False})
                    .in_("id", ids)
                    .execute()
                )
            return
        await asyncio.to_thread(
            lambda: client.table("feedback_log")
            .update({"is_active": False})
            .in_("category", ["topic_directive", "topic"])
            .eq("is_active", True)
            .execute()
        )
    except Exception as e:
        logger.error(f"clear_topic_directives error: {e}")


async def increment_applied_count(feedback_id: str) -> None:
    """Increment how many times this feedback has been applied to a newsletter."""
    try:
        client = get_client()
        # Fetch current count
        result = await asyncio.to_thread(
            lambda: client.table("feedback_log")
            .select("applied_count")
            .eq("id", feedback_id)
            .execute()
        )
        if result.data:
            current = result.data[0].get("applied_count", 0) or 0
            await asyncio.to_thread(
                lambda: client.table("feedback_log")
                .update({"applied_count": current + 1})
                .eq("id", feedback_id)
                .execute()
            )
    except Exception as e:
        logger.error(f"increment_applied_count error: {e}")
