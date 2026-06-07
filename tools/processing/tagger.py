import asyncio
import json
import logging
import os

from openai import OpenAI
from dotenv import load_dotenv

from config import MODELS, OPENROUTER_BASE_URL

load_dotenv()

logger = logging.getLogger(__name__)

TAGGER_SYSTEM_PROMPT = (
    "You are a metadata tagging system for a VC secondaries newsletter intelligence database. "
    "Given a piece of content, return a JSON object with these fields:\n"
    "- topics: array of 2-5 short topic strings (e.g. ['GP-led secondaries', 'Sequoia', 'Series D liquidity'])\n"
    "- is_deal_signal: boolean — true if this content mentions a specific company, fund, or deal in the VC/PE secondaries context\n"
    "- summary: one sentence (max 30 words) describing what this content is about\n"
    "- relevance_score: integer 1-10 — how relevant is this to VC secondaries specifically "
    "(10 = extremely relevant, 1 = not relevant)\n"
    "Return only valid JSON. No markdown, no explanation."
)

_DEFAULT_TAGS = {
    "topics": [],
    "is_deal_signal": False,
    "summary": "",
    "relevance_score": 5,
}


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


async def generate_tags(raw_text: str, source_metadata: dict = {}) -> dict:
    """
    Generate metadata tags for content using the fast LLM.
    Returns {"topics": list, "is_deal_signal": bool, "summary": str, "relevance_score": int}.
    """
    if not raw_text or not raw_text.strip():
        return _DEFAULT_TAGS.copy()

    truncated = raw_text[:2000]
    if os.getenv("HERALD_USE_LEGACY_AI", "false").lower() != "true":
        known = (
            "OpenAI", "Anthropic", "SpaceX", "Anduril", "Databricks",
            "Stripe", "xAI", "GP-led", "secondaries", "tender offer",
        )
        topics = [topic for topic in known if topic.lower() in truncated.lower()]
        return {
            "topics": topics[:5],
            "is_deal_signal": bool(topics),
            "summary": truncated.replace("\n", " ")[:200],
            "relevance_score": min(10, 5 + len(topics)),
        }
    client = _get_client()

    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {"role": "system", "content": TAGGER_SYSTEM_PROMPT},
                {"role": "user", "content": truncated},
            ],
            temperature=0,
        )

        content = response.choices[0].message.content or ""
        content = content.strip()

        # Strip markdown code fences if present
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

        parsed = json.loads(content)

        return {
            "topics": parsed.get("topics", []),
            "is_deal_signal": bool(parsed.get("is_deal_signal", False)),
            "summary": str(parsed.get("summary", ""))[:200],
            "relevance_score": int(parsed.get("relevance_score", 5)),
        }

    except json.JSONDecodeError as e:
        logger.warning(f"generate_tags JSON parse error: {e} — returning safe defaults")
        return _DEFAULT_TAGS.copy()
    except Exception as e:
        logger.error(f"generate_tags error: {e}")
        return _DEFAULT_TAGS.copy()
