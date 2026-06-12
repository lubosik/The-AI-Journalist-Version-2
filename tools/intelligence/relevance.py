import asyncio
import json
import logging
import os

from openai import OpenAI
from dotenv import load_dotenv

from config import MODELS, OPENROUTER_BASE_URL
from intelligence.prompt_architecture import build_relevance_prompt

load_dotenv()

logger = logging.getLogger(__name__)

RELEVANCE_PROMPT = build_relevance_prompt()


def _get_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )


async def check_relevance(text: str) -> dict:
    """
    Check if text is relevant to VC secondaries.
    Returns {"relevant": bool, "reason": str, "score": int}.
    On parse failure returns {"relevant": True, "reason": "parse error", "score": 5}.
    """
    if not text or not text.strip():
        return {"relevant": False, "reason": "empty text", "score": 0}

    truncated = text[:1500]
    if os.getenv("HERALD_USE_LEGACY_AI", "false").lower() != "true":
        keywords = (
            "secondary", "secondaries", "pre-ipo", "tender", "liquidity",
            "valuation", "fundraise", "openai", "anthropic", "spacex",
            "anduril", "databricks", "stripe", "xai", "venture", "vc",
        )
        matches = sum(keyword in truncated.lower() for keyword in keywords)
        score = min(10, 5 + matches)
        return {
            "relevant": True,
            "reason": "Stored for Hermes analysis; legacy model disabled",
            "score": score,
        }
    client = _get_client()

    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODELS["fast"],
            messages=[
                {"role": "system", "content": RELEVANCE_PROMPT},
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
            "relevant": bool(parsed.get("relevant", True)),
            "reason": str(parsed.get("reason", ""))[:300],
            "score": int(parsed.get("score", 5)),
        }

    except json.JSONDecodeError as e:
        logger.warning(f"check_relevance JSON parse error: {e}")
        return {"relevant": True, "reason": "parse error", "score": 5}
    except Exception as e:
        logger.error(f"check_relevance error: {e}")
        return {"relevant": True, "reason": "parse error", "score": 5}
