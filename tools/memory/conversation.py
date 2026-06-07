import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from db.client import get_client

logger = logging.getLogger(__name__)


async def store_message(
    role: str,
    content: str,
    telegram_message_id: str = None,
    tool_calls_made: list = None,
    newsletter_issue_id: str = None,
) -> Optional[str]:
    """Store a message in conversation_memory. Returns the record ID."""
    try:
        client = get_client()
        row = {
            "role": role,
            "content": content,
            "tool_calls_made": tool_calls_made or [],
        }
        if telegram_message_id:
            row["telegram_message_id"] = str(telegram_message_id)
        if newsletter_issue_id:
            row["newsletter_issue_id"] = newsletter_issue_id

        result = await asyncio.to_thread(
            lambda: client.table("conversation_memory").insert(row).execute()
        )
        if result.data:
            return result.data[0]["id"]
        return None
    except Exception as e:
        logger.error(f"store_message error: {e}")
        return None


async def get_recent_context(limit: int = 20) -> list[dict]:
    """Retrieve the last N messages for agent context injection."""
    try:
        client = get_client()
        result = await asyncio.to_thread(
            lambda: client.table("conversation_memory")
            .select("role, content, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        # Reverse so oldest first (chronological order for prompts)
        messages = list(reversed(result.data or []))
        return [{"role": m["role"], "content": m["content"]} for m in messages]
    except Exception as e:
        logger.error(f"get_recent_context error: {e}")
        return []


async def get_all_context_summary(days: int = 30) -> str:
    """
    Summarise all conversations from the last N days.
    Used by Hermes to understand what Dom has been focused on.
    Returns a 400-500 word summary string.
    """
    from openai import OpenAI
    from config import MODELS, OPENROUTER_BASE_URL

    try:
        client = get_client()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        result = await asyncio.to_thread(
            lambda: client.table("conversation_memory")
            .select("role, content, created_at")
            .gte("created_at", cutoff)
            .order("created_at", desc=False)
            .execute()
        )
        messages = result.data or []

        if not messages:
            return "No recent conversations found."

        # Build a readable transcript (truncated to avoid token limits)
        transcript_lines = []
        for m in messages[-60:]:  # last 60 messages max
            role_label = "Dom" if m["role"] == "user" else "HERALD"
            content = m["content"][:300].replace("\n", " ")
            transcript_lines.append(f"{role_label}: {content}")

        transcript = "\n".join(transcript_lines)

        llm = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.getenv("OPENROUTER_API_KEY"))

        response = await asyncio.to_thread(
            llm.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are summarising recent conversations between Dom (a VC secondaries advisor) "
                        "and his AI assistant HERALD. Extract: what deals or companies he mentioned, "
                        "what topics he cared about, any preferences or instructions he expressed, "
                        "and what market themes came up. Be specific. Max 400 words. Plain prose, no bullet points."
                    ),
                },
                {"role": "user", "content": f"Conversation transcript:\n\n{transcript}"},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or "Could not summarise conversations."

    except Exception as e:
        logger.error(f"get_all_context_summary error: {e}")
        return "Conversation summary unavailable."
